"""Call-scoped resource proposals and safe model projections.

The model sees only temporary ``resource_ref`` values. Registry-owned resource
parameter declarations remain the only mapping to execution arguments.
"""
from copy import deepcopy
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


PROTOCOL_VERSION = "resource-ref-v1"
BINDING_VERSION = "resource-binding-v1"


class ResourceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    resource_ref: str | None = Field(default=None, pattern=r"^r[1-9][0-9]*$")
    unresolved: bool = False

    @model_validator(mode="after")
    def selected_or_unresolved(self):
        fields = self.model_fields_set
        if self.resource_ref is not None and fields == {"resource_ref"}:
            return self
        if self.unresolved is True and fields == {"unresolved"}:
            return self
        raise ValueError("Choose exactly one resource_ref or unresolved.")


def selection(value: object) -> ResourceSelection:
    return ResourceSelection.model_validate(value)


def selection_schema() -> dict[str, Any]:
    return {
        "oneOf": [
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["resource_ref"],
                "properties": {"resource_ref": {"type": "string", "pattern": "^r[1-9][0-9]*$"}},
            },
            {
                "type": "object",
                "additionalProperties": False,
                "required": ["unresolved"],
                "properties": {"unresolved": {"const": True}},
            },
        ],
        "description": "选择当前资源上下文中的临时引用；无法确定或用户所指资源不存在时返回 unresolved。",
    }


def resource_specs(tool: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    values = tool.get("resource_parameters", ())
    return tuple(dict(value) for value in values if isinstance(value, dict))


def proposal_catalog(catalog):
    """Project Registry definitions into schemas safe for a business model."""
    result = deepcopy(catalog)
    for tool in result:
        schema = tool["schema"]
        specs = resource_specs(tool)
        if tool["tool_name"] == "materials_ml_train_tabular_regression":
            schema["properties"].pop("units", None)
            schema["required"] = [field for field in schema.get("required", []) if field != "units"]
            tool["description"] = tool.get("description", "") + (
                "执行单位由服务从数据集已登记元数据继承；单位语义推断写入 semantic_annotations，"
                "evidence 只用于来源说明，未由资源声明或用户确认时需补充确认。"
            )
        from .semantic_units import ANNOTATION_TOOLS, annotation_schema
        if tool["tool_name"] in ANNOTATION_TOOLS:
            schema["properties"]["semantic_annotations"] = annotation_schema(tool["tool_name"])
        required = list(schema.get("required", ()))
        for spec in specs:
            execution = spec["execution_argument"]
            model = spec["model_argument"]
            schema["properties"].pop(execution, None)
            schema["properties"][model] = selection_schema()
            required = [model if field == execution else field for field in required]
        if "required" in schema:
            schema["required"] = required
        tool["description"] = tool.get("description", "").replace("ebsd_asset_id", "图片引用")
        tool.pop("resource_parameters", None)
        tool.pop("execution_profile", None)
    return result


def model_observation(observation, enabled=True):
    from materialsagent.application.result_projection import (
        project_output_outcome,
        project_result,
        project_result_artifacts,
    )
    value = project_result(observation)
    result = {
        "kind": "TOOL_RESULT",
        "tool_name": observation.tool_name,
        "artifacts": project_result_artifacts(observation),
        "data": value["facts"],
        "presentation": {key: item for key, item in value.items() if key != "facts"},
    }
    outcome = project_output_outcome(observation)
    if outcome is not None:
        result["outcome"] = outcome
    return result


def model_draft(draft, enabled=True):
    if draft is None:
        return None
    bound_fields = set(draft.resource_bindings)
    arguments = {key: deepcopy(value) for key, value in draft.arguments.items() if key not in bound_fields}
    normalized = {key: deepcopy(value) for key, value in draft.normalized.items() if key not in bound_fields}
    for binding in draft.resource_bindings.values():
        normalized[binding.model_argument] = {
            "selected": binding.safe_description.get("name", "已确认的输入"),
            "resource_type": binding.resource_type,
        }
    labels = {binding.execution_argument: binding.model_argument for binding in draft.resource_bindings.values()}
    issue_labels = {"Missing": "需要补充", "Invalid": "需要更正", "Conflict": "存在冲突", "Ambiguous": "需要明确"}
    return {
        "tool_name": draft.tool_name,
        "arguments": arguments,
        "normalized": normalized,
        "unit_annotations": draft.unit_annotations,
        "issues": {labels.get(key, key): issue_labels[value] for key, value in draft.issues.items()},
        "resolver_authoritative": draft.resolver_authoritative,
    }
