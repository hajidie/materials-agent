"""Model annotations are hypotheses, never canonical resource metadata."""
from typing import Literal
from types import MappingProxyType
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


UNIT_DIMENSIONS = MappingProxyType({
    "°C": "temperature", "K": "temperature", "°F": "temperature",
    "s": "time", "min": "time", "h": "time",
    "Pa": "pressure", "MPa": "pressure", "GPa": "pressure",
})


def normalize_unit(value: object) -> str:
    if type(value) is not str or value not in UNIT_DIMENSIONS:
        raise ValueError("Unsupported unit expression.")
    return value


class UnitAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    resource_parameter: str = Field(min_length=1, max_length=80)
    column: str = Field(min_length=1, max_length=200)
    unit: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9µμ°%*/^().· _-]+$")
    source: Literal["model_inference"] = "model_inference"
    evidence: str | None = Field(default=None, min_length=1, max_length=500)
    usage: Literal["interpretation", "numeric"] = "interpretation"


ANNOTATIONS = TypeAdapter(list[UnitAnnotation])
ANNOTATION_TOOLS = {
    "materials_ml_train_tabular_regression": ("dataset_reference",),
    "materials_ml_analyze_tabular_dataset": ("dataset_reference",),
    "materials_ml_predict_with_model": ("model_reference", "input_dataset_reference"),
    "materials_unit_conversion": ("from_unit", "to_unit"),
}


def annotation_schema(tool):
    item = UnitAnnotation.model_json_schema()
    item["properties"]["resource_parameter"]["enum"] = list(ANNOTATION_TOOLS[tool])
    item["properties"]["unit"]["enum"] = list(UNIT_DIMENSIONS)
    return {"type": "array", "maxItems": 16, "items": item,
        "description": "独立的单位语义推断，source 只能为 model_inference。evidence 仅记录来源解释，不参与有效性判断；不等于已登记/确认单位，需补充确认且不改写数据。涉及数值换算、阈值、比较或合并时 usage=numeric。"}
