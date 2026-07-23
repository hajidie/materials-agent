from __future__ import annotations

from materialsagent.application.tool_execution import _tool_output_fingerprint
from materialsagent.domain.ports.tool_execution import ToolExecutionOutput


def _output() -> ToolExecutionOutput:
    return ToolExecutionOutput(
        status="SUCCEEDED",
        requested_outputs=("mechanical_properties",),
        completed_outputs=("mechanical_properties",),
        failed_outputs=(),
        data={
            "yield_strength": {"value": 1000.0, "unit": "MPa"},
            "elongation": {"value": 8.2, "unit": "%"},
        },
        images=(),
        warnings=(),
        diagnostics=(),
        actual_runtime_parameters={
            "seed": 101,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
        model_bundle_id="private-bundle",
        error=None,
    )


def test_full_internal_output_fingerprint_detects_scientific_value_mutation() -> None:
    output = _output()
    before = _tool_output_fingerprint(output)

    output.data["yield_strength"]["value"] = 9999.0

    assert _tool_output_fingerprint(output) != before


def test_safe_summary_does_not_persist_scientific_values_or_model_identity() -> None:
    summary = _output().safe_summary()

    assert summary["data_fields"] == ["elongation", "yield_strength"]
    assert "1000" not in repr(summary)
    assert "private-bundle" not in repr(summary)
