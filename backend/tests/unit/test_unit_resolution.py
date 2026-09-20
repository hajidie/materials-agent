from copy import deepcopy
from types import SimpleNamespace
import pytest
from materialsagent.application.unit_resolution import UnitResolutionPolicy, project_units
from materialsagent.domain.models.semantic_units import UnitAnnotation
from materialsagent.domain.models.agent import ResourceBinding


TOOL = "materials_ml_train_tabular_regression"
ANNOTATION = {"resource_parameter": "dataset_reference", "column": "strength_MPa", "unit": "MPa",
    "source": "model_inference", "evidence": "strength_MPa", "usage": "interpretation"}


def resolve(annotation=ANNOTATION, *, declared=None, tool=TOOL, bindings=None, metadata_override=None):
    metadata = metadata_override or {"units": {"strength_MPa": declared},
        "analysis": {"columns": [{"name": "strength_MPa", "numeric": True}]}}
    before = deepcopy(metadata)
    result = UnitResolutionPolicy().annotations(tool, [annotation],
        {"dataset_id": ResourceBinding(provider="ml_resource", resource_type="dataset",
            model_argument="dataset_reference", execution_argument="dataset_id",
            platform_resource_id="private", execution_value="remote", identity_digest="a" * 64)}
        if bindings is None else bindings, metadata=lambda _: metadata)
    assert metadata == before
    return result


def test_unknown_is_not_conflict_and_inference_does_not_change_facts_or_metrics():
    annotations, issues = resolve()
    assert issues == {"semantic_annotations": "Ambiguous"} and annotations[0]["provenance"] == "inferred"
    assert not annotations[0]["conflict"] and annotations[0]["requires_confirmation"]
    presentation = {"facts": {"units": {"strength_MPa": None}},
        "metrics": [{"label": "MAE", "value": 7.18, "unit": None}], "notes": ["原始单位未登记。"]}
    projected = project_units(presentation, annotations)
    assert projected["facts"] == presentation["facts"] and projected["metrics"] == presentation["metrics"]
    assert "模型语义推断" in projected["notes"][-1] and "MPa" in projected["notes"][-1]
    assert len(presentation["notes"]) == 1


@pytest.mark.parametrize("declared,conflict", [("MPa", False), ("GPa", True)])
def test_registered_unit_wins_and_conflicts_are_explained(declared, conflict):
    annotations, issues = resolve(declared=declared)
    item = annotations[0]
    assert not issues and item["unit"] == declared and item["provenance"] == "declared"
    assert item["conflict"] == conflict
    note = project_units({"notes": []}, annotations)["notes"][0]
    assert ("未采用该推断" in note) == conflict


def test_numeric_use_requires_trusted_units_not_model_claimed_confirmation():
    annotations, issues = resolve({**ANNOTATION, "usage": "numeric"})
    assert issues == {"semantic_annotations": "Ambiguous"} and annotations[0]["requires_confirmation"]
    assert resolve({**ANNOTATION, "usage": "numeric"}, declared="MPa")[1] == {}
    trusted = UnitResolutionPolicy.resolve(UnitAnnotation(**ANNOTATION), confirmed_unit="GPa", numerical=True)
    assert trusted["provenance"] == "confirmed" and trusted["unit"] == "GPa" and not trusted["requires_confirmation"]
    assert resolve({**ANNOTATION, "source": "confirmed"})[1] == {"semantic_annotations": "Invalid"}
    conversion = {**ANNOTATION, "resource_parameter": "from_unit", "column": "from_unit", "evidence": "from_unit"}
    # Trusted tool purpose wins even if a model labels a conversion as interpretation.
    assert resolve(conversion, tool="materials_unit_conversion")[1] == {"semantic_annotations": "Ambiguous"}


@pytest.mark.parametrize("changes", [{"column": "invented_MPa", "evidence": "invented_MPa"},
    {"resource_parameter": "another_dataset"}, {"confirmed": True}, {"unit": "<script>"},
    {"unit": "furlong"}])
def test_annotation_is_bound_to_verified_resource_and_supported_origin(changes):
    assert resolve({**ANNOTATION, **changes})[1] == {"semantic_annotations": "Invalid"}


@pytest.mark.parametrize("evidence", [None, "自由措辞，不含字段名"])
def test_evidence_is_provenance_only(evidence):
    annotations, issues = resolve({**ANNOTATION, "evidence": evidence})
    assert issues == {"semantic_annotations": "Ambiguous"} and annotations[0]["evidence"] == evidence


def test_unresolved_resource_cannot_silently_discard_annotation():
    assert resolve(bindings={})[1] == {"semantic_annotations": "Ambiguous"}


def test_unit_annotation_rejects_a_real_but_non_numeric_field():
    metadata = {"units": {"strength_MPa": None},
        "analysis": {"columns": [{"name": "strength_MPa", "numeric": False}]}}
    assert resolve(metadata_override=metadata)[1] == {"semantic_annotations": "Invalid"}


@pytest.mark.parametrize("values", [None, {}, False, 0])
def test_invalid_annotation_container_is_not_silently_ignored(values):
    assert UnitResolutionPolicy().annotations(TOOL, values, {}, metadata=lambda _: {})[1] == {"semantic_annotations": "Invalid"}


def test_unit_question_reply_confirms_only_explicit_unit_delta_and_survives_next_decision():
    from materialsagent.application.agent_tools import ToolArgResolver
    from materialsagent.application.tools import build_tool_registry
    from materialsagent.domain.models.agent import AgentRun, AskUser, now
    resolver = ToolArgResolver(build_tool_registry(), lambda: pytest.fail("No database access needed"), now)
    tool = "materials_unit_conversion"
    run = AgentRun(conversation_id="conversation", actor_id="actor", source_message_id="message",
        goal="把 strength_MPa 的数值转成 GPa")
    annotation = {**ANNOTATION, "resource_parameter": "from_unit", "column": "from_unit", "usage": "numeric"}
    run.draft = resolver.resolve(run, tool, {"value": 1000, "from_unit": "MPa", "to_unit": "GPa", "semantic_annotations": [annotation]})
    assert run.draft.issues == {"semantic_annotations": "Ambiguous"}
    assert "semantic_annotations" not in run.draft.normalized
    assert resolver.resolve(run, tool, {"semantic_annotations": []}).issues == {"semantic_annotations": "Ambiguous"}
    run.waiting = AskUser(type="AskUser", reason="TOOL_ARGUMENT_CLARIFICATION", question="请确认原始单位", tool_name=tool,
        fields=["semantic_annotations"])
    run.user_inputs = ["确认，原始数据的单位就是 MPa"]
    assert resolver.resolve(run, tool, {}).issues == {"semantic_annotations": "Ambiguous"}
    run.draft = resolver.resolve(run, tool, {"from_unit": "MPa"})
    assert not run.draft.issues and run.draft.unit_annotations[0]["provenance"] == "confirmed"
    run.waiting = None
    frozen = resolver.resolve(run, tool, {"value": 1000})
    assert not frozen.issues and frozen.unit_annotations == run.draft.unit_annotations
    assert resolver.resolve(run, tool, {"semantic_annotations": []}).unit_annotations == run.draft.unit_annotations
    # A changed numerical unit invalidates this temporary confirmation.
    assert resolver.resolve(run, tool, {"from_unit": "Pa"}).issues == {"semantic_annotations": "Ambiguous"}


def test_resource_unit_confirmation_survives_repeated_model_inference_for_same_binding():
    from materialsagent.application.agent_tools import ToolArgResolver
    from materialsagent.application.materials_ml_tools import build_ml_tools
    from materialsagent.application.tools import build_tool_registry
    from materialsagent.domain.models.agent import AgentRun, AskUser, now

    class ResourceContext:
        def __init__(self):
            self.resources = SimpleNamespace(resource=lambda *_: {
                "units": {"strength_MPa": None},
                "analysis": {"columns": [{"name": "x", "numeric": True},
                                            {"name": "z", "numeric": True},
                                            {"name": "strength_MPa", "numeric": True}]},
            })

        @staticmethod
        def resolve(_run, _registration, merged, previous):
            values = dict(merged)
            supplied = values.pop("dataset_id", None)
            reference = (supplied or {}).get("platform_resource_id")
            if reference is None and previous:
                reference = previous.resource_bindings["dataset_id"].platform_resource_id
            binding = ResourceBinding(provider="ml_resource", resource_type="dataset",
                model_argument="dataset_reference", execution_argument="dataset_id",
                platform_resource_id=reference, execution_value="remote-" + reference,
                identity_digest=("a" if reference == "ref-1" else "b") * 64)
            values["dataset_id"] = binding.execution_value
            return values, {}, {"dataset_id": binding}

    registry = build_tool_registry(ml_registrations=build_ml_tools(
        binding_version="test", endpoint_digest="a" * 64))
    resolver = ToolArgResolver(registry, lambda: pytest.fail("No database access needed"), now)
    resolver.resource_context = ResourceContext()
    run = AgentRun(conversation_id="conversation", actor_id="actor", source_message_id="message",
        goal="用这个数据集训练随机森林")
    arguments = {"dataset_id": {"platform_resource_id": "ref-1"}, "features": ["x", "z"],
        "target": "strength_MPa", "algorithm": "RF", "semantic_annotations": [ANNOTATION]}
    run.draft = resolver.resolve(run, TOOL, arguments)
    assert run.draft.issues == {"semantic_annotations": "Ambiguous"}

    run.waiting = AskUser(type="AskUser", reason="TOOL_ARGUMENT_CLARIFICATION",
        question="请确认 strength_MPa 是否为 MPa", tool_name=TOOL, fields=["semantic_annotations"])
    run.user_inputs = ["确认"]
    run.draft = resolver.resolve(run, TOOL, {"semantic_annotations": [ANNOTATION]})
    assert not run.draft.issues
    assert run.draft.unit_annotations[0]["provenance"] == "confirmed"

    run.waiting = None
    repeated = resolver.resolve(run, TOOL, arguments)
    assert not repeated.issues
    assert repeated.unit_annotations[0]["provenance"] == "confirmed"

    changed = resolver.resolve(run, TOOL, {**arguments,
        "dataset_id": {"platform_resource_id": "ref-2"}})
    assert changed.issues == {"semantic_annotations": "Ambiguous"}
    assert changed.unit_annotations[0]["provenance"] == "inferred"


@pytest.mark.parametrize("field,key,metadata", [
    ("model_reference", "model_id", {"target": "strength_MPa", "target_unit": None, "feature_order": ["x", "z"]}),
    ("input_dataset_reference", "input_dataset_id", {"units": {"strength_MPa": None}}),
])
def test_prediction_interpretation_is_scoped_to_each_input(field, key, metadata):
    binding = ResourceBinding(provider="ml_resource", resource_type="model" if field == "model_reference" else "dataset",
        model_argument=field, execution_argument=key, platform_resource_id="local", execution_value="remote",
        identity_digest="b" * 64)
    annotations, issues = UnitResolutionPolicy().annotations("materials_ml_predict_with_model",
        [{**ANNOTATION, "resource_parameter": field}], {key: binding}, metadata=lambda _: metadata)
    assert issues == {"semantic_annotations": "Ambiguous"} and annotations[0]["provenance"] == "inferred"
