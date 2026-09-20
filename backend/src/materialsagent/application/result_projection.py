"""Deterministic semantic facts for chat and Agent context; never raw-result fallback."""
from collections.abc import Mapping
from math import isfinite

OUTCOMES = {"PENDING": "等待处理", "RUNNING": "正在处理", "AVAILABLE": "可用",
    "SUCCEEDED": "已完成", "PARTIALLY_SUCCEEDED": "部分完成", "FAILED": "未能完成",
    "CANCELLED": "已取消", "OUTCOME_UNKNOWN": "结果尚未确认"}
WARNINGS = {"IID_NOT_VERIFIED": "尚未验证样本相互独立且来自相同分布。",
    "DUPLICATE_ROWS_PRESENT": "数据中存在重复行，评估结果可能偏乐观。",
    "UNIT_UNKNOWN": "部分变量未声明单位，结果解释需核实单位。",
    "UNIT_UNVERIFIED": "尚未确认预测数据与训练数据的单位一致，请核实后使用结果。"}
LABELS = {"yield_strength": "屈服强度", "elongation": "延伸率", "r2": "R²", "mae": "MAE", "mse": "MSE", "rmse": "RMSE"}
FIELD_LABELS = {"semantic_annotations": "请确认模型推断的字段单位", "dataset_id": "数据文件", "training_run_id": "训练结果", "model_id": "预测模型", "input_dataset_id": "预测输入文件",
    "ebsd_asset_id": "EBSD 图片", "algorithm": "算法", "target": "预测目标", "features": "输入变量", "test_size": "测试集比例",
    "random_state": "随机种子", "units": "变量单位", "solution_temperature": "固溶温度", "solution_time": "固溶时间",
    "aging_temperature": "时效温度", "aging_time": "时效时间", "requested_outputs": "所需结果", "from_unit": "原始单位",
    "to_unit": "目标单位", "value": "数值", "material": "材料"}

OUTPUT_STATES = frozenset({"SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED"})
ARTIFACT_ROLES = frozenset({"requested_output", "intermediate", "supporting"})
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg"})


def number(value):
    return type(value) in (int, float) and isfinite(value)


def warning_notes(values):
    notes = []
    for value in values or []:
        code = value.get("code") if isinstance(value, Mapping) else value
        note = WARNINGS.get(code, "本次结果存在需要核实的限制，请结合实验验证。") if isinstance(code, str) else "本次结果存在需要核实的限制。"
        if note not in notes:
            notes.append(note)
    return notes


def project_output_outcome(observation):
    """Project authoritative completion facts without result or storage identity."""
    result = observation.result_summary
    if not isinstance(result, Mapping):
        return None
    outcome = {
        "status": observation.status if observation.status in OUTPUT_STATES else "UNKNOWN",
        "requested_outputs": [],
        "completed_outputs": [],
        "failed_outputs": [],
    }
    for key in ("requested_outputs", "completed_outputs", "failed_outputs"):
        values = result.get(key)
        if (isinstance(values, (list, tuple)) and len(values) <= 32
                and all(isinstance(item, str) and 0 < len(item) <= 128 for item in values)):
            outcome[key] = list(values)
    return outcome


def project_result_artifacts(observation):
    """Describe user-visible result assets; never expose IDs, URLs or digests."""
    artifacts = []
    for artifact in observation.artifacts[:16]:
        if not isinstance(artifact, Mapping):
            continue
        role = artifact.get("role")
        media_type = artifact.get("media_type")
        if role not in ARTIFACT_ROLES or media_type not in IMAGE_MEDIA_TYPES:
            continue
        item = {
            "kind": "sem_image" if observation.tool_name == "zta35g_sem_virtual_lab" else "image",
            "role": role,
            "available_to_user": artifact.get("status") == "AVAILABLE",
            "media_type": media_type,
        }
        for key in ("width", "height"):
            value = artifact.get(key)
            if type(value) is int and 0 < value <= 16384:
                item[key] = value
        artifacts.append(item)
    return artifacts


def project_resource(resource, *, kind=None):
    try:
        return _project_resource(resource, kind=kind)
    except (AttributeError, KeyError, TypeError, ValueError):
        return {"title": "结果暂不可解释", "summary": "结果已保存，暂时无法生成可靠的结果说明。",
            "facts": {}, "metrics": [], "notes": ["请稍后核查结果，当前不展示未经核验的内容。"]}


def _project_resource(resource, *, kind=None):
    """Only descriptive facts; no ID, digest, storage, worker or transport state."""
    facts, metrics = {}, []
    analysis = resource.get("analysis") or {}
    if isinstance(analysis, Mapping):
        count = analysis.get("dataset", {}).get("row_count")
        if number(count):
            facts["row_count"] = count
        if "columns" in analysis:
            facts["columns"] = [{k: c[k] for k in ("name", "dtype", "numeric", "missing_count", "constant") if k in c}
                                for c in analysis["columns"][:256] if isinstance(c, Mapping)]
        if number(analysis.get("duplicate_row_count")):
            facts["duplicate_row_count"] = analysis["duplicate_row_count"]
    for key in ("display_name", "row_count", "target", "target_unit"):
        if type(resource.get(key)) in (str, int, float):
            facts[key] = resource[key]
    units = resource.get("units")
    if isinstance(units, Mapping):
        facts["units"] = {str(k): v for k, v in units.items() if v is None or isinstance(v, str)}
    spec = resource.get("spec") or resource.get("manifest", {}).get("spec", {})
    if isinstance(spec, Mapping):
        for key in ("features", "target", "test_size"):
            if key in spec:
                facts[key] = spec[key]
        if spec.get("algorithm") in ("LR", "RF"):
            facts["model_type"] = {"LR": "线性回归", "RF": "随机森林"}[spec["algorithm"]]
    target_unit = resource.get("target_unit") or (units or {}).get(facts.get("target"))
    if target_unit:
        facts["target_unit"] = target_unit
    def metric_values(values, group="验证"):
        if not isinstance(values, Mapping):
            return
        for key, value in values.items():
            if key in ("train", "test", "cv"):
                metric_values(value, {"train": "训练", "test": "测试", "cv": "交叉验证"}[key])
            elif key in LABELS and number(value):
                metrics.append({"label": group + " " + LABELS[key], "value": value,
                                "unit": target_unit if key in ("mae", "rmse") else f"{target_unit}²" if key == "mse" and target_unit else None})
    metric_values(resource.get("metrics"))
    state = OUTCOMES.get(resource.get("status"), "当前进度尚未确认")
    title = {"dataset": "数据概况", "training_run": "模型训练", "model": "模型评估", "prediction": "预测结果"}.get(kind, "处理结果")
    notes = warning_notes([*resource.get("warnings", []), *analysis.get("assumptions", [])])
    return {"title": title, "summary": title + "：" + state, "facts": facts, "metrics": metrics, "notes": notes}


def project_result(observation):
    if observation.error and observation.error.get("code") == "MCP_OUTCOME_UNKNOWN":
        return {"title": "结果待确认", "summary": "暂时无法确认处理结果，请检查原提交，不要重复执行。", "facts": {}, "metrics": [], "notes": []}
    if observation.status == "FAILED":
        code = (observation.error or {}).get("code")
        summary = {
            "ML_UNIT_CONFLICT": "执行参数中的单位与数据集登记的单位不一致，预检未通过。单位推断应作为独立语义注解，不覆盖已登记单位。",
            "ML_INVALID_UNITS": "提供的单位格式无效，预检未通过。请核实数据集的单位信息。",
            "ML_MISSING_COLUMNS": "所选变量不在数据集中，预检未通过。请核实输入变量和预测目标的列名。",
            "ML_DATASET_NOT_AVAILABLE": "数据文件当前不可用，预检未通过。请核实附件状态。",
            "ML_UNSUPPORTED_ALGORITHM": "所选训练算法不受支持，预检未通过。当前仅支持线性回归（LR）和随机森林回归（RF）。",
            "MCP_RESOURCE_UNAVAILABLE": "暂时无法完成执行前校验或原操作核查。附件仍保留，请稍后核查。",
        }.get(code, "本次处理未能完成，已保存的结果仍可查看。")
        return {"title": "处理未完成", "summary": summary, "facts": {}, "metrics": [], "notes": warning_notes(observation.warnings)}
    data = observation.data
    if observation.tool_name.startswith("materials_ml_") and isinstance(data.get("resource"), Mapping):
        kind = "prediction" if observation.tool_name.endswith("predict_with_model") else "dataset" if observation.tool_name.endswith("analyze_tabular_dataset") else "training_run"
        result = project_resource(data["resource"], kind=kind)
        if observation.tool_name.endswith("train_tabular_regression"):
            result["summary"] = "已提交模型训练，训练完成后将追加结果。"
        from .unit_resolution import project_units
        return project_units(result, observation.unit_annotations)
    facts, metrics = {}, []
    for key in ("yield_strength", "elongation"):
        quantity = data.get(key)
        if isinstance(quantity, Mapping) and number(quantity.get("value")) and isinstance(quantity.get("unit"), str):
            facts[key] = {"value": quantity["value"], "unit": quantity["unit"]}
            metrics.append({"label": LABELS[key], **facts[key]})
    # A scalar quantity contract, not generic ToolResult traversal.
    if number(data.get("value")):
        facts["value"] = data["value"]
        for key in ("unit", "input_unit"):
            if isinstance(data.get(key), str):
                facts[key] = data[key]
        if number(data.get("input_value")):
            facts["input_value"] = data["input_value"]
    summary = observation.presentation.get("summary") if observation.tool_name in (
        "materials_unit_conversion", "ebsd_yield_strength_predictor", "zta35g_sem_virtual_lab") else None
    if not isinstance(summary, str) or not summary.strip():
        summary = "；".join(f"{m['label']}：{m['value']:.4g} {m['unit']}" for m in metrics) or "处理结果已保存。"
    from .unit_resolution import project_units
    return project_units({"title": "处理结果", "summary": summary, "facts": facts, "metrics": metrics,
                          "notes": warning_notes(observation.warnings)}, observation.unit_annotations)
