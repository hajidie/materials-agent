from dataclasses import replace
from types import SimpleNamespace

import pytest
pytest.importorskip("mcp")

from materialsagent.application.materials_ml_tools import build_ml_tools, LocalMLAuthorization
from materialsagent.application.mcp_executor import MCPExecutor
from materialsagent.application.tools import ToolCatalogService
from materialsagent.application.tool_invocations import InvocationService, ExecutorRouter
from materialsagent.application.tool_registry import ToolRegistry
from materialsagent.application.agent_tools import RegistryAgentGateway
from materialsagent.application.errors import ApplicationConflictError
from materialsagent.domain.ports.mcp import MCPFailure
from materialsagent.domain.models.tool_invocation import InvocationStatus
from materialsagent.infrastructure.config import AppSettings
from test_tool_invocation_service import _store, _Uow, _Clock, _ids, _resolved, ACTOR


class Client:
    def __init__(self, store):
        self.store, self.calls, self.preparations = store, 0, 0
        self.fail = None
        self.lookup_calls = []

    def prepare(self, binding, arguments, context):
        self.preparations += 1
        run = self.store.runs[context.invocation_run_id]
        assert run.binding_snapshot and run.dispatch_started_at is None
        if binding.remote_tool_name == "analyze_tabular_dataset":
            return None
        return {"operation": "training.submit", "idempotency_key": "key-" + context.invocation_run_id,
                "request_digest": "a" * 64, "digest_version": "training-submit-v1"}

    def call(self, binding, arguments, context):
        self.calls += 1
        run = self.store.runs[context.invocation_run_id]
        assert run.dispatch_started_at
        if binding.remote_tool_name == "train_tabular_regression":
            assert run.remote_operation == context.remote_operation
        if self.fail:
            raise self.fail
        return {"resource": {"id": "run-1", "scope_id": context.conversation_id, "status": "PENDING"}}

    def lookup(self, binding, scope, operation):
        self.lookup_calls.append(dict(operation))
        return {"lookup_status": "FOUND", "resource": {"id": "run-1", "status": "PENDING", "scope_id": scope}}


def setup(name="train_tabular_regression", *, authorized=True):
    store = _store()
    registry = ToolRegistry(build_ml_tools(binding_version="1", endpoint_digest="b" * 64))
    client = Client(store)
    service = InvocationService(lambda: _Uow(store), registry, ExecutorRouter((MCPExecutor(client),)),
        authorization=LocalMLAuthorization(registry.list_registered()) if authorized else None,
        clock=_Clock(), id_factory=_ids())
    registration = registry.resolve("materials_ml_" + name)
    args = {"dataset_id": "dataset"} if name == "analyze_tabular_dataset" else {
        "dataset_id": "dataset", "features": ["x"], "target": "y"}
    public = service.create_from_proposal(ACTOR, _resolved(registration, args), request_id="req", idempotency_key="key")
    return store, registry, client, service, public.run


def test_ml_catalog_health_probes_validate_each_pinned_mcp_binding():
    checked = []
    registry = ToolRegistry(build_ml_tools(
        binding_version="1",
        endpoint_digest="b" * 64,
        health_probe=lambda binding: checked.append(binding.remote_tool_name) or "AVAILABLE",
    ))

    entries = ToolCatalogService(registry).list_entries()

    assert {entry["availability"] for entry in entries} == {"AVAILABLE"}
    assert set(checked) == {
        "analyze_tabular_dataset",
        "train_tabular_regression",
        "get_training_run",
        "predict_with_model",
    }


def test_mcp_readiness_is_safe_and_briefly_cached():
    from materialsagent.infrastructure.tool_clients import mcp_client as module

    client = object.__new__(module.MCPClient)
    client._readiness_cache = {}
    calls = []
    client.invoke = lambda binding, arguments, context, check_only: calls.append(
        (binding.remote_tool_name, arguments, context.conversation_id, check_only)
    )
    binding = SimpleNamespace(remote_tool_name="analyze_tabular_dataset", remote_schema_hash="a" * 64)

    assert client.readiness(binding) == "AVAILABLE"
    assert client.readiness(binding) == "AVAILABLE"
    assert calls == [("analyze_tabular_dataset", {}, "mcp-readiness", True)]

    failing = SimpleNamespace(remote_tool_name="get_training_run", remote_schema_hash="b" * 64)
    client.invoke = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private detail"))
    assert client.readiness(failing) == "UNAVAILABLE"


def test_write_confirmation_and_frozen_identity_precede_network_dispatch():
    store, registry, client, service, run = setup()
    assert run.status is InvocationStatus.PENDING_CONFIRMATION
    assert client.calls == client.preparations == 0
    finished = service.confirm(ACTOR, run.invocation_run_id).run
    assert finished.status is InvocationStatus.SUCCEEDED
    assert client.calls == client.preparations == 1
    assert finished.remote_operation["request_digest"] == "a" * 64
    service.confirm(ACTOR, run.invocation_run_id)
    assert client.calls == 1
    assert "训练已提交" in store.results[finished.invocation_result_id].data["summary"]


def test_unknown_has_safe_observation_and_reconcile_does_not_change_history():
    store, registry, client, service, run = setup()
    client.fail = MCPFailure("MCP_OUTCOME_UNKNOWN", unknown=True)
    unknown = service.confirm(ACTOR, run.invocation_run_id).run
    assert unknown.status is InvocationStatus.OUTCOME_UNKNOWN and unknown.invocation_result_id is None
    gateway = RegistryAgentGateway(registry, lambda: _Uow(store), service, None, None)
    observation = gateway.repair(SimpleNamespace(actor_id=ACTOR.actor_id, conversation_id="conversation_1", agent_run_id="agent"),
        SimpleNamespace(invocation_run_id=run.invocation_run_id, action_id="step", tool_name=run.tool_id, unit_annotations=[]))
    assert observation.status == "FAILED"
    assert observation.error["retryable"] is False and observation.error["outcome"] == "UNKNOWN"
    assert "不能判断资源已创建或未创建" in observation.error["message"]
    resolved = service.reconcile_mcp(ACTOR, run.invocation_run_id).run
    assert resolved.status is InvocationStatus.OUTCOME_UNKNOWN
    assert client.lookup_calls == [dict(unknown.remote_operation)]
    assert resolved.remote_receipt["lookup_status"] == "FOUND"
    assert client.calls == 1 and client.preparations == 1 and store.results == {}


def test_exact_permission_and_binding_drift_fail_closed():
    _, _, client, _, run = setup(authorized=False)
    assert run.status is InvocationStatus.DENIED and client.calls == 0
    _, registry, client, service, run = setup()
    registration = registry.resolve(run.tool_id)
    changed = replace(registration, binding=replace(registration.binding,
        execution_target=replace(registration.binding.execution_target, binding_version="changed")))
    service._registry = ToolRegistry(tuple(changed if r.tool_id == run.tool_id else r for r in registry.list_registered()))
    with pytest.raises(ApplicationConflictError):
        service.confirm(ACTOR, run.invocation_run_id)
    assert client.calls == client.preparations == 0


def test_mcp_unknown_is_valid_for_standard_but_remote_facts_cannot_move_to_native():
    store, registry, client, service, run = setup("analyze_tabular_dataset")
    # Read-only registration uses the normal Standard path without confirmation.
    assert run.status is InvocationStatus.SUCCEEDED
    with pytest.raises(ValueError, match="Remote facts"):
        replace(run, executor_id="standard_sync")


def test_ml_tools_are_explicitly_disabled_and_production_cannot_enable_them():
    assert AppSettings().enable_dev_materials_ml_tools is False
    with pytest.raises(ValueError):
        AppSettings(app_env="production", enable_dev_materials_ml_tools=True,
            materials_ml_mcp_token="a" * 32, materials_ml_resource_token="b" * 32)
    with pytest.raises(ValueError):
        AppSettings(enable_dev_materials_ml_tools=True, materials_ml_mcp_token="a" * 32, materials_ml_resource_token="a" * 32)


def test_lost_local_commit_receipt_keeps_success_without_second_dispatch(monkeypatch):
    from materialsagent.domain.ports.unit_of_work import PersistenceError
    store, _, client, service, run = setup()
    commit = service._commit_output
    def lost(*args):
        commit(*args)
        raise PersistenceError("Lost acknowledgement")
    monkeypatch.setattr(service, "_commit_output", lost)
    final = service.confirm(ACTOR, run.invocation_run_id).run
    assert final.status is InvocationStatus.SUCCEEDED
    assert client.calls == 1 and len(store.results) == 1


def test_reconcile_preserves_found_and_concurrent_newer_receipts(monkeypatch):
    store, _, client, service, run = setup()
    client.fail = MCPFailure("MCP_OUTCOME_UNKNOWN", unknown=True)
    service.confirm(ACTOR, run.invocation_run_id)
    found = service.reconcile_mcp(ACTOR, run.invocation_run_id).run.remote_receipt
    def unavailable(*args):
        raise MCPFailure("MCP_RESOURCE_UNAVAILABLE")
    monkeypatch.setattr(client, "lookup", unavailable)
    checked = service.reconcile_mcp(ACTOR, run.invocation_run_id).run.remote_receipt
    assert checked["resource"] == found["resource"] and checked["lookup_status"] == "FOUND"
    assert checked["last_check"]["lookup_status"] == "UNAVAILABLE"
    def stale(*args):
        current = store.runs[run.invocation_run_id]
        store.runs[run.invocation_run_id] = replace(current, version=current.version + 1,
            remote_receipt={"lookup_status": "FOUND", "resource": {"id": "newer"}})
        return {"lookup_status": "NOT_FOUND", "resource": None}
    monkeypatch.setattr(client, "lookup", stale)
    assert service.reconcile_mcp(ACTOR, run.invocation_run_id).run.remote_receipt["resource"]["id"] == "newer"


@pytest.mark.parametrize("mutation", ["scope", "identity", "contract", "text", "extra", "nonterminal", "oversize", "isError"])
def test_response_ambiguity_is_never_mapped_to_success(mutation):
    import json
    from mcp import types
    from materialsagent.infrastructure.tool_clients.mcp_client import MCPClient, Call
    registration = ToolRegistry(build_ml_tools(binding_version="1", endpoint_digest="b" * 64)).resolve("materials_ml_predict_with_model")
    call = Call(registration.binding.execution_target, {"model_id": "model", "input_dataset_id": "data"}, "scope", None, 5)
    resource = {"id": "prediction", "scope_id": "scope", "status": "SUCCEEDED", "model_id": "model", "input_dataset_id": "data"}
    output = {"contract_version": "materials-ml-tools-v2", "resource": resource, "error": None}
    if mutation == "scope": resource["scope_id"] = "other"
    if mutation == "identity": resource["model_id"] = "other"
    if mutation == "contract": output["contract_version"] = "old"
    if mutation == "extra": resource["storage_key"] = "private"
    if mutation == "nonterminal": resource["status"] = "RUNNING"
    if mutation == "oversize": resource["warnings"] = ["x" * 65536]
    response = types.CallToolResult(structuredContent=output, isError=mutation == "isError",
        content=[types.TextContent(type="text", text="{}" if mutation == "text" else json.dumps(output))])
    with pytest.raises(MCPFailure) as caught:
        MCPClient.validate_result(call, response)
    assert caught.value.unknown


def test_backend_mcp_imports_do_not_pull_ml_dependencies():
    import ast
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / "src/materialsagent"
    forbidden = {"materials_ml", "materials_ml_service", "sklearn", "joblib", "torch"}
    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [n.name for n in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) else []
            assert not {name.split(".")[0] for name in names} & forbidden, source
    adapter = root / "infrastructure/tool_clients/mcp_client.py"
    for node in ast.walk(ast.parse(adapter.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in {"session", "initialized"}:
            assert not node.attr.startswith("_"), "SDK internals are not an extension point"


@pytest.mark.parametrize("drift", ["server", "version", "protocol", "contract", "schema"])
def test_remote_allowlist_drift_is_rejected_before_tool_call(drift):
    import asyncio
    from hashlib import sha256
    from materialsagent.application.materials_ml_tools import CONTRACTS, canonical
    from materialsagent.infrastructure.tool_clients.mcp_client import SessionSlot
    registration = ToolRegistry(build_ml_tools(binding_version="1", endpoint_digest="b" * 64)).resolve("materials_ml_analyze_tabular_dataset")
    binding = registration.binding.execution_target
    schema = CONTRACTS[binding.remote_tool_name]
    tool = SimpleNamespace(name=binding.remote_tool_name, inputSchema=schema["inputSchema"], outputSchema=schema["outputSchema"],
        meta={"contract_version": binding.contract_version, "schema_sha256": binding.remote_schema_hash})
    initialized = SimpleNamespace(protocolVersion=binding.protocol_version,
        serverInfo=SimpleNamespace(name=binding.server_name, version=binding.server_version))
    if drift == "server": initialized.serverInfo.name = "unapproved"
    if drift == "version": initialized.serverInfo.version = "future"
    if drift == "protocol": initialized.protocolVersion = "future"
    if drift == "contract": tool.meta["contract_version"] = "future"
    if drift == "schema":
        tool.inputSchema = {"type": "object"}
        tool.meta["schema_sha256"] = sha256(canonical({"input": tool.inputSchema, "output": tool.outputSchema})).hexdigest()
    class Catalog:
        async def list_tools(self): return SimpleNamespace(tools=[tool])
    with pytest.raises(MCPFailure) as caught:
        asyncio.run(SessionSlot.validate(None, Catalog(), initialized, binding))
    assert caught.value.code == "MCP_BINDING_MISMATCH" and not caught.value.unknown


def test_even_same_endpoint_redirect_cannot_resend_a_tool_post():
    import asyncio
    import httpx
    from materialsagent.infrastructure.tool_clients.mcp_client import BoundedTransport
    async def check():
        url = "http://127.0.0.1:8200/mcp"
        calls = []
        def redirect(request):
            calls.append(request)
            return httpx.Response(307, headers={"Location": url}, content=b"{}")
        transport = BoundedTransport(url)
        await transport.inner.aclose()
        transport.inner = httpx.MockTransport(redirect)
        async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
            with pytest.raises(MCPFailure, match="MCP_REDIRECT_REJECTED"):
                await client.post(url, json={"method": "tools/call"})
        assert len(calls) == 1
    asyncio.run(check())


@pytest.mark.parametrize("path,status,body,expected", [
    ("operation-identities/prepare", 422, {"error": {"code": "UNIT_CONFLICT"}}, "ML_UNIT_CONFLICT"),
    ("operation-identities/prepare", 422, {"error": {"code": "MISSING_COLUMNS"}}, "ML_MISSING_COLUMNS"),
    ("operation-identities/prepare", 422, {"error": {"code": "UNSUPPORTED_ALGORITHM"}}, "ML_UNSUPPORTED_ALGORITHM"),
    ("operation-identities/prepare", 422, {"error": {"code": "private-value"}}, "MCP_RESOURCE_UNAVAILABLE"),
    ("operation-identities/prepare", 503, {"error": {"code": "UNIT_CONFLICT"}}, "MCP_RESOURCE_UNAVAILABLE"),
    ("operation-receipts/lookup", 422, {"error": {"code": "UNIT_CONFLICT"}}, "MCP_RESOURCE_UNAVAILABLE"),
    ("operation-identities/prepare", 422, {"error": "invalid"}, "MCP_RESOURCE_UNAVAILABLE"),
])
def test_resource_preflight_preserves_only_allowlisted_domain_rejections(monkeypatch, path, status, body, expected):
    import httpx
    from materialsagent.infrastructure.tool_clients.mcp_client import MCPClient
    client = object.__new__(MCPClient)
    client.base, client.resource_token = "http://127.0.0.1:8200", "resource-test"
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(status, json=body)
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(MCPFailure) as caught:
        client.resource_request("scope", path, {})
    assert caught.value.code == expected and not caught.value.unknown
    assert len(calls) == 1


@pytest.mark.parametrize("failure", ["expired", "expired_twice", "tool_404", "unavailable"])
def test_session_renewal_is_bounded_and_only_before_tool_dispatch(monkeypatch, failure):
    from contextlib import asynccontextmanager
    import httpx
    from materialsagent.infrastructure.tool_clients import mcp_client as module

    events, calls = [], []
    session_count = 0

    @asynccontextmanager
    async def transport(*args, **kwargs):
        nonlocal session_count
        session_count += 1
        number = session_count
        events.append(("open", number))
        try:
            yield None, None, None
        finally:
            events.append(("closed", number))

    class Session:
        def __init__(self, *args): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def initialize(self): return None
        async def call_tool(self, name, arguments, **kwargs):
            slot = client.slots[0]
            request = httpx.Request("POST", client.url, json={"id": 2, "method": "tools/call"})
            await slot.request_hook(request)
            calls.append((request.headers["X-ML-Scope-ID"], arguments))
            if failure == "tool_404":
                await slot.response_hook(httpx.Response(404, request=request))
                raise RuntimeError("transport failed after tool dispatch")
            return {"ok": True}

    async def validate(slot, *args):
        # An already initialized protocol session can expire while its slot is idle.
        if failure == "expired_twice" or (failure == "expired" and session_count == 1):
            request = httpx.Request("POST", client.url, json={"id": 1, "method": "tools/list"})
            await slot.response_hook(httpx.Response(404, request=request))
            raise RuntimeError("session expired")
        if failure == "unavailable":
            raise RuntimeError("no explicit session expiry evidence")

    async def cancel(slot, session):
        events.append(("cancel", session_count))

    monkeypatch.setattr(module, "streamable_http_client", transport)
    monkeypatch.setattr(module, "ClientSession", Session)
    monkeypatch.setattr(module.SessionSlot, "validate", validate)
    monkeypatch.setattr(module.SessionSlot, "cancel", cancel)
    monkeypatch.setattr(module.MCPClient, "validate_result", staticmethod(lambda call, result: result))
    client = module.MCPClient(url="http://127.0.0.1:8200/mcp", token="mcp", resource_token="resource", slots=1)
    context = SimpleNamespace(conversation_id="original-scope", remote_operation=None)
    try:
        if failure == "expired":
            assert client.call(SimpleNamespace(remote_tool_name="train_tabular_regression"), {"algorithm": "RF"}, context) == {"ok": True}
            assert calls == [("original-scope", {"algorithm": "RF"})]
            assert events.index(("closed", 1)) < events.index(("open", 2))
        else:
            with pytest.raises(MCPFailure) as caught:
                client.call(SimpleNamespace(remote_tool_name="train_tabular_regression"), {}, context)
            assert caught.value.unknown == (failure == "tool_404")
            assert len(calls) == (1 if failure == "tool_404" else 0)
        assert session_count == (2 if failure.startswith("expired") else 1)
    finally:
        client.close()
