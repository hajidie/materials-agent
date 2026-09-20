"""Acceptance-only Backend process. Configuration arrives on stdin, never in argv/logs."""
from pathlib import Path
import json
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    from alembic import command
    from alembic.config import Config
    import uvicorn
    from materialsagent.application.context import ActorContext
    from materialsagent.domain.models.actor import Actor
    from materialsagent.infrastructure.config import AppSettings
    from materialsagent.infrastructure.db.session import create_engine_from_settings, create_session_factory
    from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
    from materialsagent.infrastructure.db.agent import SQLAlchemyAgentStore
    from materialsagent.infrastructure.llm.agent_model import MockAgentModel
    from materialsagent.main import create_app

    setup = json.loads(sys.stdin.readline())
    settings = AppSettings(**setup["settings"])
    assert settings.postgres_db.startswith("materialsagent_test_") and len(settings.postgres_db) == 36
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    config.attributes["settings"] = settings
    if setup.get("fault") == "historical-migration":
        command.downgrade(config, "0017_mcp_invocation")
    command.upgrade(config, "head")
    engine = create_engine_from_settings(settings)
    sessions = create_session_factory(engine)
    factory = lambda: SQLAlchemyUnitOfWork(sessions)
    with factory() as uow:
        if uow.actors.get("p4-actor") is None:
            uow.actors.add(Actor.local_anonymous("p4-actor"))
            uow.commit()

    def respond(role, payload):
        # Scripted P4 fixture goals contain local candidate positions, never IDs.
        try:
            intent = json.loads(payload.get("goal", ""))
        except (ValueError, TypeError):
            intent = None
        if not isinstance(intent, dict) or "acceptance_tool" not in intent:
            from ml_resource_language_model import respond as language_response
            return language_response(role, payload)
        if role == "final_answer":
            return {"text": "本次受控工具调用已完成。", "sources": [o["source"] for o in payload["observations"]]}
        if role == "tool_arg_resolution":
            arguments = intent["arguments"]
            return {field: arguments[field] for field in payload["draft"]["issues"] if field in arguments}
        if payload.get("observations"):
            return {"type": "Finish", "sources": [o["source"] for o in payload["observations"]]}
        if payload.get("draft"):
            if payload["draft"]["issues"]:
                return {"type": "AskUser", "reason": "TOOL_ARGUMENT_CLARIFICATION",
                    "question": "请确认单位信息。", "tool_name": payload["draft"]["tool_name"],
                    "fields": list(payload["draft"]["issues"])}
            return {"type": "CallTool", "tool_name": payload["draft"]["tool_name"], "arguments": {}}
        args = dict(intent["arguments"])
        for field, selector in intent["reference_selectors"].items():
            candidate = next(item for item in payload["resource_context"]["resources"]
                if item["resource_type"] == selector["resource_type"]
                and (selector.get("dataset_ordinal") is None
                     or item.get("dataset_ordinal") == selector["dataset_ordinal"]))
            args[field] = {"resource_ref": candidate["resource_ref"]}
        return {"type": "CallTool", "tool_name": intent["acceptance_tool"], "arguments": args}

    app = create_app(settings=settings, unit_of_work_factory=factory,
        agent_store=SQLAlchemyAgentStore(sessions), actor_context=ActorContext("p4-actor"),
        agent_model=None if settings.llm_adapter == "provider" else MockAgentModel(respond))
    @app.get("/acceptance/runs/{identity}")
    def internal_run(identity: str):
        # This server exists only in isolated acceptance databases. Ordinary
        # endpoints continue to use the new presentation DTO without diagnostics.
        run = app.state.agent_runtime.store.get(identity, "p4-actor")
        return {"data": run.model_dump(mode="json", exclude={"actor_id", "context"})}
    if settings.enable_materials_ml_resource_context and settings.llm_adapter == "provider":
        from materialsagent.infrastructure.llm.agent_model import _count
        model = app.state.agent_runtime.model
        # Explicit integration profile, equivalent to the documented llm.toml role
        # setting. Production defaults and the normal budget gate stay unchanged.
        if setup.get("decision_prompt_limit_tokens") is not None:
            from dataclasses import replace
            limit = setup["decision_prompt_limit_tokens"]
            assert type(limit) is int and limit == 24576
            role = model.configurations["agent_decision"]
            assert limit <= role.context_window_tokens
            model.configurations = {**model.configurations, "agent_decision": replace(role, prompt_limit_tokens=limit)}
        original_prepare = model.prepare
        budget_diagnostic = {}
        def measured_prepare(role, payload, *args):
            # Metadata only: do not expose prompts, user text or dataset contents.
            budget_diagnostic.update(role=role, execution_count=len(payload.get("execution_facts", [])),
                observation_statuses=[o.get("status") for o in payload.get("observations", [])],
                tool_names=[t["tool_name"] for t in payload.get("tools", [])])
            try:
                return original_prepare(role, payload, *args)
            except Exception:
                budget_diagnostic.update(role=role, sections={k: _count(v) for k, v in payload.items()})
                raise
        model.prepare = measured_prepare
        @app.get("/acceptance/p6-budget")
        def diagnostic_budget():
            return budget_diagnostic
    if settings.enable_materials_ml_resources:
        # Acceptance-only diagnosis: types/frames, never request contents, locals or exception values.
        from fastapi.responses import JSONResponse
        @app.exception_handler(Exception)
        async def diagnostic(_, error):
            import traceback
            frames = [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                      for f in traceback.extract_tb(error.__traceback__)[-6:]]
            return JSONResponse({"error_type": type(error).__name__, "frames": frames}, 500)
        from materialsagent.application.errors import DependencyUnavailableError
        import os
        resource_client = app.state.ml_resources.client
        if setup.get("fault") == "p6-registration-unavailable":
            original_descriptor = resource_client.descriptor
            def unavailable_registration(scope, kind, identity, *args):
                if kind == "training_run":
                    raise DependencyUnavailableError(code="ML_RESOURCE_UNAVAILABLE")
                return original_descriptor(scope, kind, identity, *args)
            resource_client.descriptor = unavailable_registration
        if setup.get("fault") in {"delete-commit-before", "delete-commit-after"}:
            cleanup = app.state.ml_deletion_coordinator.cleanup
            original_delete = cleanup._delete_local
            def uncertain_delete(*args, **kwargs):
                if setup["fault"] == "delete-commit-after":
                    original_delete(*args, **kwargs)
                raise DependencyUnavailableError()
            cleanup._delete_local = uncertain_delete
        if setup.get("fault") in {"upload-kill", "upload-receipt-loss"}:
            original_upload, original_lookup = resource_client.upload, resource_client.lookup
            lost = False
            def lose_upload(*args):
                value = original_upload(*args)
                if setup["fault"] == "upload-kill":
                    os._exit(73)
                raise DependencyUnavailableError()
            def lose_lookup(*args):
                nonlocal lost
                if not lost:
                    lost = True
                    raise DependencyUnavailableError()
                return original_lookup(*args)
            resource_client.upload, resource_client.lookup = lose_upload, lose_lookup
        if setup.get("fault") in {"close-kill", "close-receipt-loss"}:
            original_close = resource_client.close_scope
            def lose_close(*args, **kwargs):
                value = original_close(*args, **kwargs)
                if not kwargs.get("lookup"):
                    if setup["fault"] == "close-kill":
                        os._exit(74)
                    raise DependencyUnavailableError()
                return value
            resource_client.close_scope = lose_close
    if setup.get("fault") in {"commit-before", "commit-after"}:
        from materialsagent.domain.ports.unit_of_work import PersistenceError
        original = app.state.invocation_service._commit_output
        def uncertain(*args):
            if setup["fault"] == "commit-after":
                original(*args)
            raise PersistenceError("Injected local commit acknowledgement loss")
        app.state.invocation_service._commit_output = uncertain
    if setup.get("frontend"):
        from fastapi.staticfiles import StaticFiles
        app.mount("/", StaticFiles(directory=Path(__file__).resolve().parents[3] / "frontend/dist", html=True), name="acceptance-frontend")
    try:
        uvicorn.run(app, host="127.0.0.1", port=setup["port"], access_log=False, log_level="critical")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
