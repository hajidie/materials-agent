from backend.tests.agent_state import agent_run
from copy import deepcopy
import json
import pytest
from materialsagent.application.context_framework import ContextFramework, PROFILES
from materialsagent.application.native_tool_protocol import decode_tool_arguments, UNIT_ASSERTIONS_SCHEMA
from materialsagent.domain.models.agent import AgentRun, Observation
from materialsagent.domain.ports.agent import AgentFailure


def example():
    run = agent_run(conversation_id="conversation-private", actor_id="actor-private", source_message_id="message-private", goal="解释结果")
    run.observations = [Observation(tool_call_id="step-private", tool_name="materials_ml_get_training_run", kind="TOOL_RESULT", status="SUCCEEDED",
        data={"resource": {"id": "training-private", "scope_id": "conversation-private", "model_id": "model-private",
            "status": "SUCCEEDED", "storage_path": "private/storage/location", "metrics": {"r2": .957, "mae": 7.18},
            "target_unit": "MPa", "spec": {"algorithm": "RF", "target": "屈服强度"}, "warnings": ["IID_NOT_VERIFIED"]}})]
    return run


@pytest.mark.parametrize("code,expected", [
    ("CONTEXT_BUDGET_EXCEEDED", "上下文超出处理上限"),
    ("LLM_TOKEN_BUDGET_EXCEEDED", "推理额度上限"),
    ("UNEXPECTED_ERROR", "本次处理未完成"),
    (None, None),
])
def test_public_failure_explains_budget_without_internal_code(code, expected):
    from materialsagent.api.routes.agent_runs import public
    run = example()
    run.error_code = code
    message = public(run)["error_message"]
    if expected is None:
        assert message is None
    else:
        assert expected in message and code not in message
        assert "conversation-private" not in message


@pytest.mark.parametrize("role", ["agent_decision", "answer_regeneration", "recovery"])
def test_profiles_share_one_boundary_and_drop_noncontract_fields(role):
    framework, run = ContextFramework(), example()
    payload = {"goal": run.goal, "runtime_debug": {"secret": "do not send"}, "observations": [],
        "context": [{"agent_run_id": "old-private"}], "tools": [], "facts": {"summary": "已核验"}}
    before = deepcopy(run.model_dump())
    frame = framework.build(role, payload, run)
    wire = json.dumps(frame.payload, ensure_ascii=False)
    for forbidden in ("runtime_debug", "conversation-private", "training-private", "model-private", "private/storage", "SUCCEEDED", "IID_NOT_VERIFIED", "observation_id"):
        assert forbidden not in wire
    assert frame.profile is PROFILES[role] and frame.profile.safety
    assert run.model_dump() == before


def test_regeneration_accepts_only_projected_facts_and_local_source_labels():
    framework, run = ContextFramework(), example()
    frame = framework.build("answer_regeneration", {"goal": run.goal, "observations": [],
        "selected_observation_ids": [run.observations[0].observation_id]}, run)
    assert frame.payload["observations"][0]["presentation"]["metrics"][1]["unit"] == "MPa"
    assert frame.sources == {"结果 1": run.observations[0].observation_id}
    assert "training-private" not in json.dumps(frame.payload)


@pytest.mark.parametrize("role", ["agent_decision", "answer_regeneration"])
def test_sem_result_projection_preserves_output_and_safe_image_facts(role):
    framework = ContextFramework()
    run = agent_run(conversation_id="conversation-private", actor_id="actor-private",
        source_message_id="message-private", goal="生成 SEM 图像并预测力学性能")
    observation = Observation(tool_call_id="step-private", tool_name="zta35g_sem_virtual_lab",
        kind="TOOL_RESULT", status="SUCCEEDED",
        data={"yield_strength": {"value": 409.2, "unit": "MPa"},
              "elongation": {"value": 2.921, "unit": "%"}},
        result_summary={"result_id": "result-private", "tool_run_id": "tool-run-private",
            "tool_id": "zta35g_sem_virtual_lab", "status": "SUCCEEDED",
            "requested_outputs": ["sem_image", "mechanical_properties"],
            "completed_outputs": ["sem_image", "mechanical_properties"], "failed_outputs": [],
            "data": {}, "warnings": [], "provenance": {}, "error": None,
            "tool_version": "0.1.0", "schema_hash": "a" * 64, "created_at": "2026-09-20T00:00:00Z"},
        presentation={"summary": "SEM 图像已生成，可在结果图片中查看。屈服强度：409.2 MPa；延伸率：2.921 %。"},
        artifacts=[{"asset_id": "asset-private", "status": "AVAILABLE", "role": "requested_output",
            "media_type": "image/png", "width": 512, "height": 512, "bit_depth": 8,
            "size_bytes": 1024, "sha256": "b" * 64, "content_url": "/private/content"}])
    run.observations = [observation]
    payload = {"goal": run.goal, "observations": [],
        "selected_observation_ids": [observation.observation_id]}
    if role == "agent_decision":
        payload.update(execution_facts=[], tools=[])
    frame = framework.build(role, payload, run)
    projected = frame.payload["observations"][0]

    assert projected["tool_name"] == "zta35g_sem_virtual_lab"
    assert projected["outcome"] == {
        "status": "SUCCEEDED",
        "requested_outputs": ["sem_image", "mechanical_properties"],
        "completed_outputs": ["sem_image", "mechanical_properties"],
        "failed_outputs": [],
    }
    assert projected["artifacts"] == [{
        "kind": "sem_image", "role": "requested_output", "available_to_user": True,
        "media_type": "image/png", "width": 512, "height": 512,
    }]
    if role == "agent_decision":
        assert frame.payload["execution_facts"][0]["outcome"] == projected["outcome"]
        assert frame.payload["execution_facts"][0]["artifacts"] == projected["artifacts"]
    wire = json.dumps(frame.payload, ensure_ascii=False)
    for forbidden in ("result-private", "tool-run-private", "asset-private", "/private/content", "b" * 64):
        assert forbidden not in wire


def test_intermediate_sem_is_not_projected_as_requested_output():
    from materialsagent.domain.models.ml_resource_context import model_observation
    observation = Observation(tool_call_id="step", tool_name="zta35g_sem_virtual_lab",
        kind="TOOL_RESULT", status="SUCCEEDED",
        data={"yield_strength": {"value": 409.2, "unit": "MPa"},
              "elongation": {"value": 2.921, "unit": "%"}},
        result_summary={"status": "SUCCEEDED", "requested_outputs": ["mechanical_properties"],
            "completed_outputs": ["mechanical_properties"], "failed_outputs": []},
        artifacts=[{"status": "AVAILABLE", "role": "intermediate", "media_type": "image/png"}])

    projected = model_observation(observation)

    assert projected["outcome"]["requested_outputs"] == ["mechanical_properties"]
    assert projected["outcome"]["completed_outputs"] == ["mechanical_properties"]
    assert projected["artifacts"] == [{
        "kind": "sem_image", "role": "intermediate", "available_to_user": True,
        "media_type": "image/png",
    }]


def test_provider_cannot_emit_internal_resource_parameters():
    framework, run = ContextFramework(), example()
    name = "materials_ml_get_training_run"
    frame = framework.build("agent_decision", {"tools": [{"tool_name": name, "schema": {
        "type": "object", "properties": {"training_run_id": {"type": "string"}},
        "required": ["training_run_id"]},
        "resource_parameters": [{"model_argument": "training_reference", "execution_argument": "training_run_id",
            "expected_resource_type": "training_run", "provider": "ml_resource", "required": True}]}],
        "resource_context": {"view": {"resources": [{"resource_ref": "r1", "resource_type": "training_run",
            "name": "训练 1", "source": "tool_result"}], "complete": True, "omitted_count": 0},
            "mapping": {"r1": {"provider": "ml_resource", "resource_type": "training_run",
                "platform_resource_id": "local-private"}}}}, run)
    schema = frame.payload["tools"][0]["schema"]
    assert "training_reference" in schema["properties"] and "training_run_id" not in schema["properties"]
    with pytest.raises(AgentFailure):
        decode_tool_arguments(frame, run, name, {"training_run_id": "invented"})
    decoded, _ = decode_tool_arguments(frame, run, name,
        {"training_reference": {"resource_ref": "r1"}})
    assert decoded == {"training_run_id": {"provider": "ml_resource",
        "resource_type": "training_run", "platform_resource_id": "local-private"}}


def test_no_projection_fallback_to_arbitrary_raw_result_or_presenter_text():
    from materialsagent.application.result_projection import project_result
    observation = Observation(tool_call_id="step", tool_name="unrecognized", kind="TOOL_RESULT", status="SUCCEEDED",
        data={"database_id": "hidden", "nested": {"storage_path": "private"}}, presentation={"summary": "hidden private"})
    result = project_result(observation)
    assert result["summary"] == "处理结果已保存。" and result["facts"] == {}




def test_recovery_contract_drops_raw_state_and_projection_failure_stays_safe():
    from materialsagent.application.result_projection import project_resource
    framework, run = ContextFramework(), example()
    frame = framework.build('recovery', {'facts': {'summary': '已核查', 'status': 'PENDING', 'storage_key': 'private', 'nested': {'id': 'secret'}},
        'allowed_actions': ['核查原操作']}, run)
    assert frame.payload['facts'] == {'summary': '已核查'}
    assert project_resource({'analysis': 123, 'id': 'secret'})['facts'] == {}
    assert 'secret' not in str(project_resource({'analysis': 123, 'id': 'secret'}))


def test_training_canonical_units_are_separate_from_model_annotations():
    from materialsagent.application.materials_ml_tools import CONTRACTS
    from backend.tests.unit.test_unit_resolution import ANNOTATION

    framework, run = ContextFramework(), example()
    schema = deepcopy(CONTRACTS["train_tabular_regression"]["inputSchema"])
    tool = {"tool_name": "materials_ml_train_tabular_regression", "schema": schema, "description": "训练",
        "resource_parameters": [{"model_argument": "dataset_reference", "execution_argument": "dataset_id",
            "expected_resource_type": "dataset", "provider": "ml_resource", "required": True}]}
    context = {"view": {"resources": [{"resource_ref": "r1", "resource_type": "dataset", "name": "训练集",
        "source": "current_message_attachment"}], "complete": True, "omitted_count": 0},
        "mapping": {"r1": {"provider": "ml_resource", "resource_type": "dataset", "platform_resource_id": "local-private"}}}
    frame = framework.build("agent_decision", {"tools": [tool], "resource_context": context}, run)
    view = frame.payload["tools"][0]
    assert "units" not in view["schema"]["properties"]
    assert "units" in schema["properties"]
    assert view["schema"]["properties"]["algorithm"]["enum"] == ["LR", "RF"]
    arguments = {"dataset_reference": {"resource_ref": "r1"}, "features": ["x", "z"],
                 "target": "strength_MPa", "algorithm": "RF"}
    valid, _ = decode_tool_arguments(frame, run, tool["tool_name"], arguments)
    assert "units" not in valid
    with pytest.raises(AgentFailure):
        decode_tool_arguments(frame, run, tool["tool_name"], {**arguments, "units": {"strength_MPa": "MPa"}})
    annotated = {**arguments, "semantic_annotations": [ANNOTATION]}
    valid, _ = decode_tool_arguments(frame, run, tool["tool_name"], annotated)
    assert valid["semantic_annotations"] == [ANNOTATION]
    for extra in ({"source": "confirmed"}, {"dataset_id": "private"}, {"usage": "unchecked"}):
        with pytest.raises(AgentFailure):
            decode_tool_arguments(frame, run, tool["tool_name"],
                                  {**arguments, "semantic_annotations": [{**ANNOTATION, **extra}]})


def test_training_backend_contract_rejects_noncanonical_algorithm_before_execution():
    from materialsagent.application.materials_ml_tools import build_ml_tools
    registration = next(item for item in build_ml_tools(binding_version="1", endpoint_digest="0" * 64)
                        if item.tool_id == "materials_ml_train_tabular_regression")
    arguments = {"dataset_id": "internal-dataset", "features": ["x", "z"], "target": "strength_MPa"}
    assert registration.binding.validator({**arguments, "algorithm": "RF"})["algorithm"] == "RF"
    with pytest.raises(ValueError, match="Invalid ML Tool arguments"):
        registration.binding.validator({**arguments, "algorithm": "随机森林"})


def test_tool_failure_is_explained_once_without_leaking_remote_error():
    from materialsagent.api.routes.agent_runs import public
    run = example()
    run.error_code = "TOOL_EXECUTION_FAILED"
    run.observations[0].status = "FAILED"
    run.observations[0].error = {"code": "ML_UNIT_CONFLICT", "message": "private/storage/location"}
    view = public(run)
    assert view["error_message"] is None
    assert "单位不一致" in view["observations"][0]["presentation"]["summary"]
    wire = json.dumps(view["observations"][0]["presentation"], ensure_ascii=False)
    assert "ML_UNIT_CONFLICT" not in wire and "private/storage/location" not in wire


def test_confirmation_groups_annotation_notes_without_duplicate_display_keys():
    from materialsagent.api.routes.agent_runs import public
    from materialsagent.application.unit_resolution import UnitResolutionPolicy
    from materialsagent.domain.models.agent import ExecutionRecord
    from materialsagent.domain.models.semantic_units import UnitAnnotation
    from backend.tests.unit.test_unit_resolution import ANNOTATION
    run = example()
    annotations = [UnitResolutionPolicy.resolve(UnitAnnotation(**{**ANNOTATION, "column": column, "evidence": column}))
                   for column in ("strength_MPa", "stress_MPa")]
    run.pending_execution = ExecutionRecord(tool_call_id="step", tool_name="materials_ml_train_tabular_regression",
        version="1", schema_hash="a" * 64, arguments={"target": "strength_MPa"}, execution_fingerprint="b" * 64,
        unit_annotations=annotations)
    rows = public(run)["pending_execution"]["confirmation"]
    assert len({r["label"] for r in rows}) == len(rows)
    assert len(rows[-1]["value"]) == 2 and all("模型语义推断" in n for n in rows[-1]["value"])


def test_quantity_tool_rejects_resource_unit_assertions_but_accepts_argument_units():
    framework, run = ContextFramework(), example()
    tool = {"tool_name": "quantity", "schema": {"type": "object", "properties": {
        "temperature": {"type": "object", "properties": {"value": {"type": "number"}, "unit": {"type": "string"}},
                        "required": ["value", "unit"]}}}}
    frame = framework.build("agent_decision", {"goal": run.goal, "tools": [tool],
        "user_messages": run.user_messages}, run)
    arguments = {"temperature": {"value": 730, "unit": "C"}, "user_unit_assertions": []}
    assert decode_tool_arguments(frame, run, "quantity", arguments)[0] == {"temperature": {"value": 730, "unit": "C"}}
    arguments["user_unit_assertions"] = [{"resource_parameter": "temperature", "column": "temperature", "unit": "°C",
        "source_message": next(iter(frame.inputs)), "evidence": "解释"}]
    with pytest.raises(AgentFailure, match="INVALID_UNIT_ASSERTION"):
        decode_tool_arguments(frame, run, "quantity", arguments)


def test_resource_unit_assertion_schema_exposes_only_supported_units():
    from materialsagent.domain.models.semantic_units import UNIT_DIMENSIONS

    assertion = UNIT_ASSERTIONS_SCHEMA["items"]
    assert assertion["properties"]["unit"]["enum"] == list(UNIT_DIMENSIONS)
    assert "column" in assertion["required"]


@pytest.mark.parametrize("role", ["agent_decision", "answer_regeneration"])
def test_prediction_file_fact_survives_regeneration_without_storage_leakage(role):
    framework, run = ContextFramework(), example()
    observation = Observation(tool_call_id="step-private", tool_name="materials_ml_predict_with_model",
        kind="TOOL_RESULT", status="SUCCEEDED", data={"resource": {"status": "SUCCEEDED",
            "row_count": 5, "result_ref": {"object_key": "private/predictions.json", "bucket": "private-bucket"}}})
    run.observations = [observation]
    frame = framework.build(role, {"goal": run.goal, "observations": [], "execution_facts": [], "tools": []}, run)
    artifact = {"kind": "prediction", "role": "requested_output", "available_to_user": True, "download_formats": ["csv"]}
    assert frame.payload["observations"][0]["artifacts"] == [artifact]
    assert frame.payload["execution_facts"][0]["artifacts"] == [artifact]
    assert "private/predictions.json" not in json.dumps(frame.payload)
    assert "private-bucket" not in json.dumps(frame.payload)
    observation.data["resource"]["result_ref"] = None
    assert framework.build(role, {"observations": []}, run).payload["observations"][0]["artifacts"] == []
