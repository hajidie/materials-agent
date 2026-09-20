"""Small deterministic policy; no natural-language parsing or metadata writes."""
from copy import deepcopy
from materialsagent.domain.models.semantic_units import ANNOTATIONS, ANNOTATION_TOOLS, normalize_unit


INTERPRETATION_TOOLS = frozenset({
    "materials_ml_train_tabular_regression", "materials_ml_analyze_tabular_dataset",
    "materials_ml_predict_with_model",
})


class UnitResolutionPolicy:
    @staticmethod
    def resolve(annotation, *, declared_unit=None, confirmed_unit=None, numerical=False):
        # Only trusted callers supply confirmed_unit; no model field can assert it.
        if any(value is not None and (not isinstance(value, str) or not value.strip())
               for value in (declared_unit, confirmed_unit)):
            raise ValueError("Invalid trusted unit fact")
        inferred_unit = normalize_unit(annotation.unit)
        declared_unit = normalize_unit(declared_unit) if declared_unit is not None else None
        confirmed_unit = normalize_unit(confirmed_unit) if confirmed_unit is not None else None
        trusted_conflict = declared_unit is not None and confirmed_unit is not None and declared_unit != confirmed_unit
        unit = confirmed_unit if confirmed_unit is not None else declared_unit
        provenance = "confirmed" if confirmed_unit is not None else "declared" if unit is not None else "inferred"
        conflict = unit is not None and unit != inferred_unit
        return {**annotation.model_dump(), "inferred_unit": inferred_unit,
            "unit": unit if unit is not None else inferred_unit, "provenance": provenance,
            # A model inference is explanatory provenance, never a confirmed
            # resource fact. Even interpretation-only annotations must be
            # confirmed before the annotated invocation can proceed.
            "requires_confirmation": unit is None, "conflict": conflict,
            "trusted_conflict": trusted_conflict}

    def annotations(self, tool, values, bindings, *, metadata, confirmed_units=None):
        if values == []:
            return [], {}
        try:
            annotations = ANNOTATIONS.validate_python(values)
            if len(annotations) > 16 or tool not in ANNOTATION_TOOLS:
                raise ValueError()
            result, seen = [], set()
            for item in annotations:
                key = (item.resource_parameter, item.column)
                if key in seen or item.resource_parameter not in ANNOTATION_TOOLS[tool]:
                    raise ValueError()
                seen.add(key)
                numerical = tool not in INTERPRETATION_TOOLS or item.usage == "numeric"
                if tool == "materials_unit_conversion":
                    if item.column != item.resource_parameter:
                        raise ValueError()
                    result.append(self.resolve(item, confirmed_unit=(confirmed_units or {}).get(item.resource_parameter), numerical=True))
                    continue
                binding = next((value for value in bindings.values()
                                if value.model_argument == item.resource_parameter), None)
                if binding is None:
                    return [], {"semantic_annotations": "Ambiguous"}
                facts = metadata(binding)
                units = facts.get("units") or facts.get("model_units") or {}
                spec = facts.get("spec") or facts.get("manifest", {}).get("spec", {})
                analyzed = facts.get("analysis", {}).get("columns", [])
                columns = {c["name"] for c in analyzed}
                columns.update(units)
                columns.update(spec.get("features", facts.get("feature_order", [])))
                target = spec.get("target", facts.get("target"))
                if target:
                    columns.add(target)
                if item.column not in columns:
                    raise ValueError()
                column_fact = next((column for column in analyzed if column.get("name") == item.column), None)
                if column_fact is not None and column_fact.get("numeric") is False:
                    raise ValueError()
                declared = units.get(item.column)
                if item.column == target and facts.get("target_unit") is not None:
                    declared = facts["target_unit"]
                confirmed = (confirmed_units or {}).get((item.resource_parameter, item.column))
                result.append(self.resolve(item, declared_unit=declared, confirmed_unit=confirmed, numerical=numerical))
            if any(r["trusted_conflict"] for r in result):
                return result, {"semantic_annotations": "Conflict"}
            return result, ({"semantic_annotations": "Ambiguous"} if any(r["requires_confirmation"] for r in result) else {})
        except (ValueError, TypeError, KeyError, AttributeError):
            return [], {"semantic_annotations": "Invalid"}


def project_units(presentation, annotations):
    """Display-only overlay. It never changes numeric values or canonical units."""
    result = deepcopy(presentation)
    if not annotations:
        return result
    result["unit_annotations"] = deepcopy(annotations)
    for item in annotations:
        column, unit = item["column"], item["unit"]
        role = {"model_reference": "模型", "input_dataset_reference": "输入数据集"}.get(item["resource_parameter"], "数据集")
        if item["provenance"] == "inferred":
            evidence = item.get("evidence") or "当前语境"
            note = f"{role}变量 {column} 的单位根据“{evidence}”推断为 {unit}（模型语义推断，尚未确认；未用于数值换算）。"
        else:
            note = f"变量 {column} 采用{'用户确认' if item['provenance'] == 'confirmed' else '已登记'}单位 {unit}。"
        if item["conflict"]:
            note += f"模型推断的 {item['inferred_unit']} 与确定信息不一致，未采用该推断。"
        if item["requires_confirmation"]:
            note += "该单位尚无声明或用户确认，需要确认后才能继续。"
        if note not in result["notes"]:
            result["notes"].append(note)
    return result
