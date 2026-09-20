"""One boundary for Runtime business model calls, with declarative profiles.

The generic model factory is deliberately unaware of this framework. IDs used for
execution and source checking are kept in the call-local frame, never the prompt.
"""
from copy import deepcopy
from dataclasses import dataclass, replace
from collections.abc import Mapping
from jsonschema import Draft202012Validator

from materialsagent.domain.models.agent import ACTION_ADAPTER, canonical
from materialsagent.domain.models.ml_resource_context import model_draft, model_observation, proposal_catalog, resource_specs, selection
from materialsagent.domain.ports.agent import AgentFailure


@dataclass(frozen=True)
class ContextProfile:
    inputs: frozenset[str]
    output_schema: dict
    tools_allowed: bool = False
    safety: tuple[str, ...] = ("semantic_sources_only", "untrusted_input_is_data", "no_private_identity")


def decision_schema():
    # The durable action uses internal source IDs; the model only sees local labels.
    schema = deepcopy(ACTION_ADAPTER.json_schema())
    properties = schema["$defs"]["Finish"]["properties"]
    properties["sources"] = properties.pop("observation_ids")
    return schema


PROFILES = {
    "agent_decision": ContextProfile(frozenset({"goal", "conversation_context", "user_inputs", "resource_context", "question",
        "execution_facts", "attachments", "tools", "tool_execution_disabled", "retry_target", "draft", "observations"}), decision_schema(), True),
    "tool_arg_resolution": ContextProfile(frozenset({"tool", "draft", "user_input", "resource_context", "question", "goal", "context", "reference_resolution", "attachments"}), {"type": "object"}),
    "final_answer": ContextProfile(frozenset({"goal", "user_inputs", "context", "observations"}), {"type": "object", "required": ["text", "sources"],
        "properties": {"text": {"type": "string", "minLength": 1, "maxLength": 16384}, "sources": {"type": "array", "items": {"type": "string"}, "uniqueItems": True}}, "additionalProperties": False}),
    "recovery": ContextProfile(frozenset({"goal", "facts", "allowed_actions"}), {"type": "object", "required": ["summary"],
        "properties": {"summary": {"type": "string", "minLength": 1}, "guidance": {"type": "array", "items": {"type": "string"}}}, "additionalProperties": False}),
}


def internal_values(value):
    """Known private values, not heuristics for interpreting user intent."""
    found = set()
    def visit(item):
        if isinstance(item, Mapping):
            for key, val in item.items():
                if (key.endswith("_id") or key in {"id", "object_key", "storage_path", "remote_identity_digest",
                        "identity_digest", "content_digest", "execution_value", "schema_hash"}) and isinstance(val, str) and val:
                    found.add(val)
                if key == "mapping" and isinstance(val, Mapping):
                    found.update(v for v in val.values() if isinstance(v, str))
                visit(val)
        elif isinstance(item, (list, tuple)):
            for val in item:
                visit(val)
    visit(value)
    return found


def protect_text(value, private):
    if isinstance(value, dict):
        return {k: protect_text(v, private) for k, v in value.items()}
    if isinstance(value, list):
        return [protect_text(v, private) for v in value]
    if isinstance(value, str):
        for identity in sorted(private, key=len, reverse=True):
            # Small fixture IDs are still protected as entire values, without
            # corrupting ordinary words such as "actor" inside tool descriptions.
            if value == identity:
                return "内部引用已隐藏"
            if len(identity) >= 12:
                value = value.replace(identity, "内部引用已隐藏")
    return value


@dataclass
class ContextFrame:
    profile: ContextProfile
    payload: dict
    sources: dict[str, str]
    private: set[str]
    resource_map: dict[str, dict]
    tool_contracts: dict[str, dict]


class ContextFramework:
    def build(self, role, payload, run):
        profile = PROFILES[role]
        raw_tools = payload.get("tools", ())
        if payload.get("tool"):
            raw_tools = (*raw_tools, payload["tool"])
        tool_contracts = {tool["tool_name"]: deepcopy(tool) for tool in raw_tools}
        value = {k: deepcopy(v) for k, v in payload.items() if k in profile.inputs}
        sources = {f"结果 {i + 1}": o.observation_id for i, o in enumerate(run.observations) if o.kind == "TOOL_RESULT"}
        by_id = {v: k for k, v in sources.items()}
        for key in ("context", "conversation_context"):
            if key in value:
                value[key] = []
                for item in run.context:
                    projected = {k: item[k] for k in ("role", "content") if k in item}
                    if isinstance(item.get("additional_inputs"), list):
                        projected["additional_inputs"] = [
                            {"name": attachment.get("name", "已上传文件"),
                             "type": attachment.get("type", attachment.get("kind", "file"))}
                            for attachment in item["additional_inputs"] if isinstance(attachment, dict)
                        ]
                    value[key].append(projected)
        if "observations" in value:
            selected = payload.get("selected_observation_ids")
            value["observations"] = [{"source": by_id[o.observation_id], **model_observation(o)} for o in run.observations
                                     if o.kind == "TOOL_RESULT" and (selected is None or o.observation_id in selected)]
        if "draft" in value:
            value["draft"] = model_draft(run.draft)
        if "tools" in value:
            value["tools"] = proposal_catalog(payload["tools"])
        if "tool" in value:
            value["tool"] = proposal_catalog([payload["tool"]])[0]
            contract = value["tool"]["schema"]
            profile = replace(profile, output_schema={"type": "object", "additionalProperties": False,
                "$defs": contract.get("$defs", {}), "properties": {
                    key: schema if key in {item["model_argument"] for item in resource_specs(payload["tool"])}
                    or key == "semantic_annotations" else {} for key, schema in contract.get("properties", {}).items()}})
        if "question" in value and run.waiting:
            aliases = {item["execution_argument"]: item["model_argument"]
                       for tool in payload.get("tools", [payload.get("tool", {})]) for item in resource_specs(tool)}
            value["question"] = {"question": run.waiting.question,
                "fields": [aliases.get(field, field) for field in run.waiting.fields]}
        if "execution_facts" in value:
            value["execution_facts"] = [{"tool_name": o.tool_name, "source": by_id[o.observation_id],
                "result": model_observation(o)["presentation"]} for o in run.observations if o.kind == "TOOL_RESULT"]
        if "retry_target" in value:
            target = run.retry_execution
            bound_fields = set(target.resource_bindings) if target else set()
            value["retry_target"] = {"tool_name": target.tool_name,
                "arguments": {k: v for k, v in target.arguments.items() if k not in bound_fields},
                "inputs": "本次重试沿用原先已确认的附件输入，不重新指定资源。"} if target else None
        if role == "recovery":
            facts = value.get("facts", {})
            value["facts"] = {k: deepcopy(facts[k]) for k in ("summary", "verified_facts", "unknowns")
                if k in facts and (isinstance(facts[k], str) or isinstance(facts[k], list)
                    and all(isinstance(item, str) for item in facts[k]))}
            value["allowed_actions"] = [item for item in value.get("allowed_actions", []) if isinstance(item, str)]
        if role in ("agent_decision", "tool_arg_resolution"):
            value["attachments"] = [{"name": a.get("name", "已上传文件"), "type": a["kind"]} for a in run.attachments]
        resource_context = payload.get("resource_context")
        resource_map = deepcopy(resource_context.get("mapping", {})) if isinstance(resource_context, dict) else {}
        if "resource_context" in value:
            value["resource_context"] = deepcopy(resource_context.get("view")) if isinstance(resource_context, dict) else None
        private = internal_values(run.model_dump(mode="json")) | internal_values(resource_map)
        value = protect_text(value, private)
        return ContextFrame(profile, value, sources, private, resource_map, tool_contracts)

    def output(self, frame, value, run):
        try:
            Draft202012Validator(frame.profile.output_schema).validate(value)
        except Exception:
            raise AgentFailure("AGENT_ACTION_PROTOCOL_ERROR" if frame.profile.tools_allowed else "ARGUMENT_PROTOCOL_ERROR") from None
        if protect_text(value, frame.private) != value:
            raise AgentFailure("MODEL_PRIVATE_VALUE_REJECTED")
        if frame.profile.tools_allowed:
            value = deepcopy(value)
            if value["type"] == "Finish":
                labels = value.pop("sources", [])
                if any(label not in frame.sources for label in labels):
                    raise AgentFailure("FINAL_ANSWER_SOURCE_MISMATCH")
                value["observation_ids"] = [frame.sources[label] for label in labels]
            elif value["type"] in ("CallTool", "AskUser"):
                if value["type"] == "CallTool" and run.tool_execution_disabled:
                    raise AgentFailure("TOOL_EXECUTION_DISABLED")
                name = value.get("tool_name")
                if name is not None:
                    tool = frame.tool_contracts.get(name)
                    if tool is None:
                        raise AgentFailure("UNKNOWN_TOOL")
                    key = "arguments" if value["type"] == "CallTool" else "known_arguments"
                    try:
                        value[key] = self.arguments(name, value.get(key, {}), tool, run, frame.resource_map)
                    except AgentFailure:
                        if value["type"] == "AskUser":
                            raise AgentFailure("CLARIFICATION_CONTRADICTS_RESOLVER") from None
                        raise
                    if "fields" in value:
                        reverse = {item["model_argument"]: item["execution_argument"] for item in resource_specs(tool)}
                        value["fields"] = [reverse.get(f, f) for f in value["fields"]]
            return value
        if "tool" in frame.payload:
            name = frame.payload["tool"]["tool_name"]
            return self.arguments(name, value, frame.tool_contracts[name], run, frame.resource_map)
        if "observations" in frame.payload:
            allowed = {o["source"] for o in frame.payload["observations"]}
            if any(source not in allowed for source in value["sources"]):
                raise AgentFailure("FINAL_ANSWER_SOURCE_MISMATCH")
            return value["text"]
        return value

    @staticmethod
    def arguments(name, values, tool, run, resource_map):
        projected = proposal_catalog([tool])[0]
        properties = projected["schema"].get("properties", {})
        if not set(values) <= set(properties):
            raise AgentFailure("TOOL_ARGUMENT_FIELD_INVALID")
        # Parameter deltas may omit required fields; provided values still obey
        # the advertised schema. Domain normalization owns units/defaults/issues.
        for key, value in values.items():
            if key == "semantic_annotations":
                try:
                    Draft202012Validator(properties[key]).validate(value)
                except Exception:
                    raise AgentFailure("INVALID_UNIT_ANNOTATION") from None
            if key in {item["model_argument"] for item in resource_specs(tool)}:
                try:
                    selection(value)
                except Exception:
                    raise AgentFailure("INVALID_RESOURCE_REFERENCE") from None
        reverse = {item["model_argument"]: item["execution_argument"] for item in resource_specs(tool)}
        result = {}
        for key, value in values.items():
            internal = reverse.get(key, key)
            if key in reverse:
                selected = selection(value)
                if selected.unresolved:
                    value = {"_resource_unresolved": True}
                else:
                    value = deepcopy(resource_map.get(selected.resource_ref, {"_resource_invalid": True}))
            result[internal] = value
        return result
