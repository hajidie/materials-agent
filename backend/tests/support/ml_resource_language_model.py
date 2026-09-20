"""Scripted semantic transcript for integration tests, not an NLP implementation."""


def respond(role, payload):
    resources = (payload.get("resource_context") or {}).get("resources", [])

    def candidates(kind):
        values = [item for item in resources if item.get("resource_type") == kind]
        if kind == "dataset":
            values.sort(key=lambda item: (item.get("dataset_ordinal", 0), item["resource_ref"]))
        return values

    def choose(kind, text="", *, default="latest"):
        values = candidates(kind)
        if not values:
            return {"unresolved": True}
        if "还没有确定" in text or "没决定" in text or "都可能" in text:
            return {"unresolved": True}
        if "第二" in text and len(values) > 1:
            selected = values[1]
        elif "先上传" in text:
            selected = values[0]
        elif "后传" in text or default == "latest":
            selected = values[-1]
        else:
            selected = values[0]
        return {"resource_ref": selected["resource_ref"]}

    if role == "tool_arg_resolution":
        text = payload.get("user_input", "")
        return {field: choose("dataset", text) for field in payload["draft"]["issues"]}
    if role == "final_answer":
        return {"text": "已完成本次工具调用。", "sources": [o["source"] for o in payload["observations"]]}
    results = payload["observations"]
    if results:
        return {"type": "Finish", "sources": [o["source"] for o in results]}
    draft = payload.get("draft")
    if draft:
        if draft["issues"]:
            return {"type": "AskUser", "reason": "TOOL_ARGUMENT_CLARIFICATION", "question": "请选择输入",
                "tool_name": draft["tool_name"], "fields": list(draft["issues"])}
        return {"type": "CallTool", "tool_name": draft["tool_name"], "arguments": {}}
    text = payload["goal"]
    if "预测" in text:
        tool, args = "predict_with_model", {"model_reference": choose("model", text),
            "input_dataset_reference": choose("dataset", text)}
    elif "查询" in text:
        tool, args = "get_training_run", {"training_reference": choose("training_run", text)}
    elif "训练" in text:
        tool, args = "train_tabular_regression", {"dataset_reference": choose("dataset", text),
            "features": ["x", "z"], "target": "strength_MPa", "algorithm": "RF" if "RF" in text else "LR"}
    else:
        tool, args = "analyze_tabular_dataset", {"dataset_reference": choose("dataset", text)}
    return {"type": "CallTool", "tool_name": "materials_ml_" + tool, "arguments": args}
