"""Closed, tool-specific result contracts for the two local model tools."""
import math
from collections.abc import Mapping

EBSD_TOOL_ID = "ebsd_yield_strength_predictor"


def outputs_for(tool_id):
    return frozenset({"yield_strength"}) if tool_id == EBSD_TOOL_ID else frozenset({"sem_image", "mechanical_properties"})


def validate_performance(tool_id, data, completed):
    output = "yield_strength" if tool_id == EBSD_TOOL_ID else "mechanical_properties"
    fields = {"yield_strength": "MPa"} if tool_id == EBSD_TOOL_ID else {"yield_strength": "MPa", "elongation": "%"}
    if output not in completed:
        if data:
            raise ValueError("data must be empty without a completed performance output.")
        return
    if set(data) != set(fields):
        raise ValueError("data fields do not match the tool contract.")
    for name, unit in fields.items():
        metric = data[name]
        if (not isinstance(metric, Mapping) or set(metric) != {"value", "unit"}
                or type(metric["value"]) not in (int, float) or not math.isfinite(metric["value"])
                or metric["unit"] != unit):
            raise ValueError("data must contain finite values in the tool's fixed units.")


def validate_provenance(tool_id, value):
    fields = {"input_revision", "normalized_process_parameters", "actual_runtime_parameters"}
    if tool_id == EBSD_TOOL_ID:
        fields = {"input_revision", "input_asset", "material", "model_version", "preprocessing_version", "actual_runtime_parameters"}
        asset = value.get("input_asset")
        if (not isinstance(asset, Mapping) or set(asset) != {"asset_id", "sha256"}
                or not isinstance(asset["asset_id"], str) or not asset["asset_id"]
                or not isinstance(asset["sha256"], str) or len(asset["sha256"]) != 64
                or any(c not in "0123456789abcdef" for c in asset["sha256"])
                or value.get("material") != "Inconel 625"
                or value.get("model_version") not in {"inconel625-cnn1-v1", "mock-ebsd-bundle"}
                or value.get("preprocessing_version") != "rgb-tensor-resize128-v1"):
            raise ValueError("Invalid EBSD provenance.")
    elif not isinstance(value.get("normalized_process_parameters"), Mapping):
        raise ValueError("Invalid SEM provenance.")
    if (set(value) != fields or type(value.get("input_revision")) is not int or value["input_revision"] <= 0
            or not isinstance(value.get("actual_runtime_parameters"), Mapping)):
        raise ValueError("Invalid tool provenance.")
