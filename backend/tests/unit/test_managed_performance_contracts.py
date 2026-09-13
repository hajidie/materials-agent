import pytest
from materialsagent.domain.models.managed_contracts import validate_performance


@pytest.mark.parametrize("data", [
    {"yield_strength": {"value": float("nan"), "unit": "MPa"}},
    {"yield_strength": {"value": float("inf"), "unit": "MPa"}},
    {"yield_strength": {"value": True, "unit": "MPa"}},
    {"yield_strength": {"value": 400, "unit": "GPa"}},
    {"yield_strength": {"value": 400, "unit": "MPa", "confidence": .9}},
    {"yield_strength": {"value": 400, "unit": "MPa"}, "elongation": {"value": 2, "unit": "%"}},
    {"cnn_output": 400}, {},
])
def test_ebsd_rejects_nonfinite_wrong_units_and_other_tool_outputs(data):
    with pytest.raises(ValueError):
        validate_performance("ebsd_yield_strength_predictor", data, ("yield_strength",))


def test_no_fake_prediction_on_failure_and_sem_still_requires_both_metrics():
    value = {"yield_strength": {"value": 404.9051513671875, "unit": "MPa"}}
    validate_performance("ebsd_yield_strength_predictor", value, ("yield_strength",))
    validate_performance("ebsd_yield_strength_predictor", {}, ())
    with pytest.raises(ValueError):
        validate_performance("ebsd_yield_strength_predictor", value, ())
    with pytest.raises(ValueError):
        validate_performance("zta35g_sem_virtual_lab", value, ("mechanical_properties",))
