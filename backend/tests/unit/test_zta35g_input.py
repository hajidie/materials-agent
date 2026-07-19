from __future__ import annotations

from decimal import Decimal
import json
from typing import Any

import pytest


FIELDS = (
    "material",
    "solution_temperature",
    "solution_time",
    "aging_temperature",
    "aging_time",
    "requested_outputs",
    "missing_fields",
    "ambiguous_fields",
)


def _types() -> dict[str, Any]:
    from materialsagent.application.zta35g_input import (
        normalize_zta35g_candidate,
    )
    from materialsagent.domain.ports.chat_orchestration import (
        AmbiguousValue,
        NeedsInputCandidate,
        ParameterCandidate,
        ToolCandidate,
        ZTA35GParameterCandidates,
    )

    return locals()


def _candidate(
    *,
    material: object = "ZTA35G",
    solution_temperature: tuple[object, object] | None = (1000, "°C"),
    solution_time: tuple[object, object] | None = (3, "h"),
    aging_temperature: tuple[object, object] | None = (730, "°C"),
    aging_time: tuple[object, object] | None = (3, "h"),
    requested_outputs: tuple[object, ...] = (
        "sem_image",
        "mechanical_properties",
    ),
    missing_field_hints: tuple[str, ...] = (),
    ambiguous_fields: tuple[str, ...] = (),
) -> object:
    types = _types()
    parameter_type = types["ParameterCandidate"]
    parameters_type = types["ZTA35GParameterCandidates"]

    def parameter(value: tuple[object, object] | None) -> object | None:
        if value is None:
            return None
        return parameter_type(value=value[0], unit=value[1])

    parameters = parameters_type(
        solution_temperature=parameter(solution_temperature),
        solution_time=parameter(solution_time),
        aging_temperature=parameter(aging_temperature),
        aging_time=parameter(aging_time),
    )
    if missing_field_hints or ambiguous_fields:
        return types["NeedsInputCandidate"](
            tool_id="zta35g_sem_virtual_lab",
            material=material,
            candidate_parameters=parameters,
            missing_fields=missing_field_hints,
            ambiguous_fields=ambiguous_fields,
            follow_up_suggestion="请确认输入。",
            requested_outputs=requested_outputs,
        )
    return types["ToolCandidate"](
        tool_id="zta35g_sem_virtual_lab",
        material=material,
        candidate_parameters=parameters,
        requested_outputs=requested_outputs,
    )


def _normalize(**overrides: object) -> object:
    types = _types()
    return types["normalize_zta35g_candidate"](_candidate(**overrides))


def _errors(result: object) -> list[tuple[str, str]]:
    return [(error.field, error.code) for error in result.validation_errors]


def test_valid_zta35g_input_is_normalized_without_defaults() -> None:
    result = _normalize(
        solution_time=(66, "min"),
        aging_time=(90, "min"),
        requested_outputs=(
            "mechanical_properties",
            "sem_image",
            "mechanical_properties",
        ),
    )

    assert result.missing_fields == ()
    assert result.ambiguous_fields == ()
    assert result.validation_errors == ()
    assert result.normalized_input.material == "ZTA35G"
    assert result.normalized_input.solution_temperature.value == 1000
    assert result.normalized_input.solution_temperature.unit == "°C"
    assert result.normalized_input.solution_time.value == Decimal("1.1")
    assert result.normalized_input.solution_time.unit == "h"
    assert result.normalized_input.aging_time.value == Decimal("1.5")
    assert result.normalized_input.requested_outputs == (
        "mechanical_properties",
        "sem_image",
    )


def test_raw_input_preserves_original_values_units_order_and_duplicates() -> None:
    result = _normalize(
        solution_time=(66, "min"),
        requested_outputs=("sem_image", "sem_image"),
    )

    assert result.raw_input.material == "ZTA35G"
    assert result.raw_input.parameters.solution_time.value == 66
    assert result.raw_input.parameters.solution_time.unit == "min"
    assert result.raw_input.requested_outputs == ("sem_image", "sem_image")


def test_material_missing_ambiguity_and_explicit_other_material_are_separate() -> None:
    types = _types()
    missing = _normalize(material=None)
    ambiguous = _normalize(
        material=types["AmbiguousValue"](("ZTA35G", "ZTA35G?")),
        ambiguous_fields=("material",),
    )
    unsupported = _normalize(material="OTHER")

    assert missing.missing_fields == ("material",)
    assert missing.ambiguous_fields == ()
    assert missing.validation_errors == ()
    assert tuple(field.field for field in ambiguous.ambiguous_fields) == (
        "material",
    )
    assert ambiguous.missing_fields == ()
    assert ambiguous.validation_errors == ()
    assert _errors(unsupported) == [("material", "UNSUPPORTED_MATERIAL")]


def test_all_four_parameters_can_be_missing_without_fabricated_values() -> None:
    result = _normalize(
        solution_temperature=None,
        solution_time=None,
        aging_temperature=None,
        aging_time=None,
    )

    assert result.missing_fields == (
        "solution_temperature",
        "solution_time",
        "aging_temperature",
        "aging_time",
    )
    assert result.normalized_input.solution_temperature is None
    assert result.normalized_input.solution_time is None
    assert result.normalized_input.aging_temperature is None
    assert result.normalized_input.aging_time is None


def test_missing_field_hints_are_recomputed_and_unknown_names_are_rejected() -> None:
    bogus_known_hint = _normalize(
        missing_field_hints=("solution_time",),
    )
    unknown_hint = _normalize(
        missing_field_hints=("unknown_field",),
    )

    assert bogus_known_hint.missing_fields == ()
    assert bogus_known_hint.validation_errors == ()
    assert _errors(unknown_hint) == [("missing_fields", "UNKNOWN_FIELD")]


@pytest.mark.parametrize(
    ("field", "value", "unit"),
    [
        ("solution_temperature", 900, "°C"),
        ("solution_temperature", 1100, "°C"),
        ("solution_time", 1, "h"),
        ("solution_time", 5, "h"),
        ("aging_temperature", 670, "°C"),
        ("aging_temperature", 790, "°C"),
        ("aging_time", 1, "h"),
        ("aging_time", 5, "h"),
        ("aging_time", 300, "min"),
    ],
)
def test_inclusive_range_boundaries_are_valid(
    field: str,
    value: object,
    unit: object,
) -> None:
    result = _normalize(**{field: (value, unit)})

    assert result.validation_errors == ()


@pytest.mark.parametrize(
    ("field", "value", "unit"),
    [
        ("solution_temperature", 899, "°C"),
        ("solution_temperature", 1101, "°C"),
        ("solution_time", 0.9, "h"),
        ("solution_time", 5.1, "h"),
        ("aging_temperature", 669, "°C"),
        ("aging_temperature", 791, "°C"),
        ("aging_time", 59, "min"),
        ("aging_time", 301, "min"),
    ],
)
def test_out_of_range_values_are_hard_errors(
    field: str,
    value: object,
    unit: object,
) -> None:
    result = _normalize(**{field: (value, unit)})

    assert _errors(result) == [
        (field, "PROCESS_PARAMETERS_OUT_OF_RANGE")
    ]
    assert field not in result.missing_fields
    assert result.normalized_input.for_field(field) is None


@pytest.mark.parametrize("value", [1000, 1000.0])
def test_temperature_accepts_lossless_integer_values(value: object) -> None:
    result = _normalize(solution_temperature=(value, "°C"))

    assert result.validation_errors == ()
    assert result.normalized_input.solution_temperature.value == 1000


def test_temperature_rejects_fractional_precision() -> None:
    result = _normalize(solution_temperature=(1000.5, "°C"))

    assert _errors(result) == [
        ("solution_temperature", "INVALID_PROCESS_PARAMETER_PRECISION")
    ]


@pytest.mark.parametrize("value", [3, 3.0, 3.5])
def test_time_accepts_at_most_one_decimal_place(value: object) -> None:
    result = _normalize(solution_time=(value, "h"))

    assert result.validation_errors == ()


def test_time_rejects_more_than_one_decimal_place() -> None:
    result = _normalize(solution_time=(3.25, "h"))

    assert _errors(result) == [
        ("solution_time", "INVALID_PROCESS_PARAMETER_PRECISION")
    ]


@pytest.mark.parametrize(
    ("minutes", "expected_hours"),
    [(60, "1"), (66, "1.1"), (90, "1.5"), (180, "3"), (300, "5")],
)
def test_minutes_convert_deterministically_without_rounding(
    minutes: int,
    expected_hours: str,
) -> None:
    result = _normalize(solution_time=(minutes, "min"))

    assert result.validation_errors == ()
    assert result.normalized_input.solution_time.value == Decimal(
        expected_hours
    )


def test_nonrepresentable_minutes_are_rejected_not_rounded() -> None:
    result = _normalize(solution_time=(61, "min"))

    assert _errors(result) == [
        ("solution_time", "INVALID_PROCESS_PARAMETER_PRECISION")
    ]
    assert result.normalized_input.solution_time is None


@pytest.mark.parametrize(
    ("parameter", "expected_errors", "expected_ambiguity"),
    [
        (
            (None, "K"),
            [("solution_time", "INVALID_PROCESS_PARAMETER_UNIT")],
            (),
        ),
        (
            (True, None),
            [("solution_time", "INVALID_PROCESS_PARAMETER_TYPE")],
            (),
        ),
        (("AMBIGUOUS", None), [], ("solution_time",)),
        (
            ("AMBIGUOUS", "s"),
            [("solution_time", "INVALID_PROCESS_PARAMETER_UNIT")],
            (),
        ),
        (
            (True, "AMBIGUOUS"),
            [("solution_time", "INVALID_PROCESS_PARAMETER_TYPE")],
            (),
        ),
    ],
)
def test_explicit_invalid_components_outrank_missing_or_ambiguity(
    parameter: tuple[object, object],
    expected_errors: list[tuple[str, str]],
    expected_ambiguity: tuple[str, ...],
) -> None:
    types = _types()
    ambiguous_value = types["AmbiguousValue"]((2, 3))
    value, unit = parameter
    if value == "AMBIGUOUS":
        value = ambiguous_value
    if unit == "AMBIGUOUS":
        unit = types["AmbiguousValue"](("h", "min"))

    result = _normalize(
        solution_time=(value, unit),
        ambiguous_fields=("solution_time",),
    )

    assert _errors(result) == expected_errors
    assert tuple(field.field for field in result.ambiguous_fields) == (
        expected_ambiguity
    )
    assert "solution_time" not in result.missing_fields


@pytest.mark.parametrize(
    ("field", "value", "unit"),
    [
        ("solution_temperature", 1000, "K"),
        ("solution_temperature", 1000, "°F"),
        ("solution_time", 3, "s"),
        ("solution_time", 3, "day"),
        ("solution_time", 3, "hours"),
        ("solution_temperature", 1000, "℃"),
    ],
)
def test_only_exact_confirmed_units_are_accepted(
    field: str,
    value: object,
    unit: object,
) -> None:
    result = _normalize(**{field: (value, unit)})

    assert _errors(result) == [
        (field, "INVALID_PROCESS_PARAMETER_UNIT")
    ]


@pytest.mark.parametrize(
    "value",
    [True, "not-a-number"],
)
def test_invalid_numeric_types_are_stable_validation_errors(
    value: object,
) -> None:
    result = _normalize(solution_time=(value, "h"))

    assert _errors(result) == [
        ("solution_time", "INVALID_PROCESS_PARAMETER_TYPE")
    ]


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), object(), b"bytes", {"raw": "object"}],
)
def test_non_json_candidate_objects_are_rejected_at_contract(
    value: object,
) -> None:
    with pytest.raises(ValueError, match="candidate"):
        _candidate(solution_time=(value, "h"))


@pytest.mark.parametrize(
    ("outputs", "expected_errors", "expected_normalized"),
    [
        ((), [("requested_outputs", "INVALID_REQUESTED_OUTPUT")], ()),
        (
            ("unknown",),
            [("requested_outputs", "INVALID_REQUESTED_OUTPUT")],
            (),
        ),
        (("sem_image",), [], ("sem_image",)),
        (
            ("sem_image", "mechanical_properties"),
            [],
            ("sem_image", "mechanical_properties"),
        ),
        (
            ("mechanical_properties", "sem_image", "mechanical_properties"),
            [],
            ("mechanical_properties", "sem_image"),
        ),
    ],
)
def test_requested_outputs_rules(
    outputs: tuple[object, ...],
    expected_errors: list[tuple[str, str]],
    expected_normalized: tuple[str, ...],
) -> None:
    result = _normalize(requested_outputs=outputs)

    assert _errors(result) == expected_errors
    assert result.normalized_input.requested_outputs == expected_normalized


def test_classifications_are_mutually_exclusive_and_stably_ordered() -> None:
    types = _types()
    result = _normalize(
        material=None,
        solution_temperature=(1000.5, "°C"),
        solution_time=None,
        aging_temperature=(800, "°C"),
        aging_time=(types["AmbiguousValue"]((2, 3)), "h"),
        ambiguous_fields=("aging_time", "unknown_field"),
        requested_outputs=("unknown",),
    )

    assert result.missing_fields == ("material", "solution_time")
    assert tuple(field.field for field in result.ambiguous_fields) == (
        "aging_time",
    )
    assert _errors(result) == [
        (
            "solution_temperature",
            "INVALID_PROCESS_PARAMETER_PRECISION",
        ),
        ("aging_temperature", "PROCESS_PARAMETERS_OUT_OF_RANGE"),
        ("requested_outputs", "INVALID_REQUESTED_OUTPUT"),
        ("ambiguous_fields", "UNKNOWN_FIELD"),
    ]
    classified = set(result.missing_fields)
    classified.update(field.field for field in result.ambiguous_fields)
    assert all(error.field not in classified for error in result.validation_errors)
    assert [FIELDS.index(error.field) for error in result.validation_errors] == sorted(
        FIELDS.index(error.field) for error in result.validation_errors
    )


def test_revision_payloads_are_json_ready_and_preserve_raw_candidates() -> None:
    result = _normalize(
        solution_time=(66, "min"),
        requested_outputs=(
            "mechanical_properties",
            "sem_image",
            "mechanical_properties",
        ),
    )

    payloads = result.to_revision_payloads()

    assert json.dumps(payloads, ensure_ascii=False, allow_nan=False)
    assert result.normalized_input.solution_time.value == Decimal("1.1")
    assert payloads["raw_input"]["parameters"]["solution_time"] == {
        "value": 66,
        "unit": "min",
    }
    assert payloads["raw_input"]["requested_outputs"] == [
        "mechanical_properties",
        "sem_image",
        "mechanical_properties",
    ]
    assert payloads["normalized_input"]["solution_time"] == {
        "value": 1.1,
        "unit": "h",
    }
    assert type(
        payloads["normalized_input"]["solution_time"]["value"]
    ) is float
    assert payloads["normalized_input"]["requested_outputs"] == [
        "mechanical_properties",
        "sem_image",
    ]


@pytest.mark.parametrize("raw_value", [True, "not-a-number"])
def test_revision_payload_preserves_json_invalid_raw_values(
    raw_value: object,
) -> None:
    result = _normalize(solution_time=(raw_value, "h"))

    payloads = result.to_revision_payloads()

    assert payloads["raw_input"]["parameters"]["solution_time"][
        "value"
    ] is raw_value
    assert payloads["validation_errors"] == [
        {
            "field": "solution_time",
            "code": "INVALID_PROCESS_PARAMETER_TYPE",
            "message": "The value must be a finite number and must not be boolean.",
        }
    ]
    assert json.dumps(payloads, ensure_ascii=False, allow_nan=False)


def test_revision_payload_projects_ambiguous_candidates_safely() -> None:
    types = _types()
    result = _normalize(
        solution_time=(types["AmbiguousValue"]((2, 3)), "h"),
        ambiguous_fields=("solution_time",),
    )

    payloads = result.to_revision_payloads()

    assert payloads["raw_input"]["parameters"]["solution_time"][
        "value"
    ] == {"candidates": [2, 3]}
    assert payloads["ambiguous_fields"] == [
        {"field": "solution_time", "candidates": [2, 3]}
    ]
    assert json.dumps(payloads, ensure_ascii=False, allow_nan=False)
