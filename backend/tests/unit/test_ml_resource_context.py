from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from materialsagent.application.ml_resource_context import ResourceContextResolver, MLResourceResultRegistrar
from materialsagent.domain.models.agent import AgentRun, ExecutionRecord, canonical
from materialsagent.domain.models.ml_resource_context import ResourceSelection, proposal_catalog
from materialsagent.domain.ports.tool_registry import ResourceParameterSpec, ResourceProvider, ResourceType
from materialsagent.infrastructure.config import AppSettings


def ref(i, kind="dataset", **extra):
    return {"reference_id": f"ref-{i}", "resource_id": f"remote-{i}", "resource_type": kind,
        "dataset_ordinal": i if kind == "dataset" else None, "remote_identity_digest": f"{i:x}" * 64,
        "identity_contract_version": "ml-resource-identity-v1", "identity": {},
        "source": "upload:operation", "created_at": f"2026-09-{i:02d}T00:00:00Z",
        "description": {"name": f"数据 {i}", "columns": ["x", "strength_MPa"],
            "units": {"strength_MPa": "MPa"}}, **extra}


class Repo:
    def __init__(self, refs):
        self.refs = refs

    @contextmanager
    def transaction(self, *args, **kwargs):
        yield SimpleNamespace(rows=lambda table, **filters: [{"document": value} for value in self.refs]
                              if table == "reference" else [])


class Resources:
    def __init__(self, refs):
        self.refs = {value["reference_id"]: value for value in refs}
        self.repo = Repo(refs)
        self.client = SimpleNamespace(service={"service_id": "materials_ml"})

    def _client(self):
        return self.client

    def reference(self, actor, conversation, identity):
        return self.refs[identity]

    def verified(self, actor, conversation, identity):
        value = self.refs[identity]
        return {**value, "status": "AVAILABLE", "scope_id": conversation}


def run(**values):
    return AgentRun(conversation_id="scope", actor_id="actor", source_message_id="message",
                    goal="分析我上传的数据", **values)


def resolver(refs):
    registry = SimpleNamespace(resolve=lambda _: SimpleNamespace(resource_parameters=()))
    return ResourceContextResolver(Resources(refs), registry, None)


def test_resource_selection_is_strict_and_has_no_natural_language_fallback():
    assert ResourceSelection.model_validate({"resource_ref": "r1"}).resource_ref == "r1"
    assert ResourceSelection.model_validate({"unresolved": True}).unresolved
    for value in ({}, {"resource_ref": "r1", "unresolved": True},
                  {"resource_ref": "r1", "unresolved": False},
                  {"resource_ref": None, "unresolved": True}, {"resource_ref": "ref-private"},
                  {"resource_ref": "r1", "evidence": "刚才上传"}):
        with pytest.raises(ValueError):
            ResourceSelection.model_validate(value)


def test_context_is_bounded_safe_local_and_prioritizes_current_attachment():
    refs = [ref(i) for i in range(1, 30)]
    current = {"attachment_id": "ref-1", "kind": "dataset", "name": "01-training.csv"}
    value = resolver(refs).context(run(attachments=[current]))
    view = value["view"]
    assert len(view["resources"]) == 20 and not view["complete"] and view["omitted_count"] == 9
    assert view["resources"][0]["source"] == "current_message_attachment"
    assert view["resources"][0]["resource_ref"] == "r1"
    assert len(canonical(view).encode()) < 16_384
    wire = canonical(view)
    assert "reference_id" not in wire and "resource_id" not in wire
    assert "availability" not in wire and "status" not in wire and "remote_identity_digest" not in wire
    assert view["resources"][0]["details"]["declared_units"] == {"strength_MPa": "MPa"}


def test_context_samples_remaining_candidates_fairly_by_resource_type():
    refs = [ref(i) for i in range(1, 31)]
    refs += [ref(31, "model"), ref(32, "training_run")]
    view = resolver(refs).context(run())["view"]
    assert [item["resource_type"] for item in view["resources"][:3]] == [
        "dataset", "model", "training_run"]
    assert len(view["resources"]) == 20 and view["omitted_count"] == 12


def test_unresolved_and_unknown_refs_do_not_fall_back_to_only_candidate():
    value = ref(1)
    service = resolver([value])
    spec = ResourceParameterSpec("dataset_reference", "dataset_id", ResourceType.DATASET, ResourceProvider.ML_RESOURCE)
    registration = SimpleNamespace(resource_parameters=(spec,))
    unresolved = service.resolve(run(), registration, {"dataset_id": {"_resource_unresolved": True}}, None)
    invalid = service.resolve(run(), registration, {"dataset_id": {"_resource_invalid": True}}, None)
    assert unresolved[1] == {"dataset_id": "Ambiguous"} and not unresolved[2]
    assert invalid[1] == {"dataset_id": "Invalid"} and not invalid[2]


def test_selected_ref_becomes_typed_binding_and_is_reverified():
    value = ref(1)
    service = resolver([value])
    spec = ResourceParameterSpec("dataset_reference", "dataset_id", ResourceType.DATASET, ResourceProvider.ML_RESOURCE)
    registration = SimpleNamespace(resource_parameters=(spec,))
    handle = {"provider": "ml_resource", "resource_type": "dataset", "platform_resource_id": "ref-1"}
    normalized, issues, bindings = service.resolve(run(), registration, {"dataset_id": handle}, None)
    binding = bindings["dataset_id"]
    assert issues == {} and normalized["dataset_id"] == "remote-1"
    assert binding.model_argument == "dataset_reference" and binding.execution_value == "remote-1"
    service.verify(run(), binding)


def test_context_configuration_remains_opt_in_for_ml_plane():
    assert not AppSettings().enable_materials_ml_resource_context
    with pytest.raises(ValueError):
        AppSettings(enable_materials_ml_resource_context=True)


def test_model_projection_uses_registry_specs_without_mutating_execution_schema():
    original = [{"tool_name": "materials_ml_analyze_tabular_dataset", "schema": {
        "type": "object", "properties": {"dataset_id": {"type": "string"}}, "required": ["dataset_id"]},
        "resource_parameters": [{"model_argument": "dataset_reference", "execution_argument": "dataset_id",
            "expected_resource_type": "dataset", "provider": "ml_resource", "required": True}]}]
    projected = proposal_catalog(original)
    schema = projected[0]["schema"]
    assert "dataset_reference" in schema["properties"] and "dataset_id" not in schema["properties"]
    assert schema["required"] == ["dataset_reference"]
    assert original[0]["schema"]["properties"]["dataset_id"] == {"type": "string"}


def test_authority_read_has_absolute_deadline_and_closes_transport(monkeypatch):
    import asyncio
    from materialsagent.application.ml_resource_context import reads
    from materialsagent.application.errors import DependencyUnavailableError
    from materialsagent.infrastructure.tool_clients.ml_resource_client import MLResourceClient
    closed = []
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): closed.append(True)
        def stream(self, *args, **kwargs): return self
        async def aiter_bytes(self):
            await asyncio.sleep(1)
            yield b'{}'
    monkeypatch.setattr("httpx.AsyncClient", Client)
    # Leave enough scheduler room to enter the transport before the absolute
    # deadline fires; the body itself remains blocked well beyond the limit.
    with reads(.1), pytest.raises(DependencyUnavailableError) as error:
        MLResourceClient("http://127.0.0.1:8200/mcp", "a" * 32, "1").descriptor("scope", "dataset", "id")
    assert error.value.code == "ML_RESOURCE_READ_TIMEOUT" and closed


def test_provider_window_reduction_only_removes_public_resources():
    import json
    from backend.tests.unit.test_llm_provider_factory import _role
    from materialsagent.infrastructure.llm.agent_model import AgentModelAdapter
    adapter = AgentModelAdapter({"agent_decision": _role("deepseek", "agent_decision", prompt_limit_tokens=8192)})
    original = {"complete": True, "omitted_count": 0,
        "resources": [{"resource_ref": f"r{i + 1}", "resource_type": "dataset", "name": "材料" * 500,
                       "source": "registered"} for i in range(20)]}
    request = adapter.prepare("agent_decision", {"goal": "请明确数据集", "resource_context": original}, 32000, 10)
    sent = json.loads(request.messages[-1]["content"])["resource_context"]
    assert not sent["complete"] and sent["omitted_count"] > 0 and len(sent["resources"]) < 20
    assert original["complete"] and len(original["resources"]) == 20


def test_argument_prompt_contains_resource_ref_protocol_not_events():
    from backend.tests.unit.test_llm_provider_factory import _role
    from materialsagent.infrastructure.llm.agent_model import AgentModelAdapter
    adapter = AgentModelAdapter({"tool_arg_resolution": _role("deepseek", "tool_arg_resolution", prompt_limit_tokens=8192)})
    request = adapter.prepare("tool_arg_resolution", {"user_input": "先上传的那份", "resource_context": {
        "resources": [{"resource_ref": "r1", "resource_type": "dataset", "name": "训练集", "source": "uploaded"}],
        "complete": True, "omitted_count": 0}}, 32000, 10)
    instructions = request.messages[0]["content"]
    assert "resource_ref" in instructions and "event_mapping" not in instructions
    assert "Finish" not in instructions and "CallTool" not in instructions


def test_execution_facts_do_not_depend_on_resource_snapshots():
    from materialsagent.application.agent_runtime import AgentRuntime
    current = run()
    current.executions = [ExecutionRecord(action_id="a", tool_name="materials_ml_analyze_tabular_dataset", version="1",
        schema_hash="hash", arguments={"dataset_id": "verified-id"}, execution_fingerprint="fp",
        status="SUCCEEDED", observation_id="result")]
    facts = AgentRuntime(None, None, SimpleNamespace(catalog=lambda: []))._context(current)["execution_facts"]
    assert facts == [{"tool_name": "materials_ml_analyze_tabular_dataset", "arguments": {"dataset_id": "verified-id"},
                      "status": "SUCCEEDED", "observation_id": "result"}]


def test_explicit_reconcile_rejects_retained_found_after_failed_latest_lookup():
    operation = {"operation": "training.submit", "request_digest": "a" * 64, "digest_version": "v1"}
    invocation = SimpleNamespace(conversation_id="scope", executor_id="mcp", status=SimpleNamespace(value="OUTCOME_UNKNOWN"),
        remote_operation=operation, remote_receipt={**operation, "lookup_status": "FOUND",
            "resource": {"id": "remote", "scope_id": "scope"}, "last_check": {"lookup_status": "UNAVAILABLE"}})
    calls = SimpleNamespace(get=lambda *args: SimpleNamespace(run=invocation), _current_registration=lambda *args: None)
    registrar = MLResourceResultRegistrar(None, calls)
    current = run()
    record = ExecutionRecord(action_id="a", tool_name="materials_ml_train_tabular_regression", version="1", schema_hash="x",
        arguments={"dataset_id": "data"}, execution_fingerprint="x", invocation_run_id="invocation")
    assert registrar.adopt(current, record, reconcile_operation="explicit") is None
    assert registrar.adopt(current, record) is None
