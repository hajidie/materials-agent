from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Final

from materialsagent.domain.ports.chat_orchestration import (
    AmbiguousValue,
    NeedsInputCandidate,
    ParameterCandidate,
    ToolCandidate,
    ZTA35GParameterCandidates,
)


PARAMETER_FIELDS: Final = (
    "solution_temperature",
    "solution_time",
    "aging_temperature",
    "aging_time",
)
KNOWN_FIELDS: Final = ("material",) + PARAMETER_FIELDS
TEMPERATURE_FIELDS: Final = frozenset(
    {"solution_temperature", "aging_temperature"}
)
FIELD_RANGES: Final = {
    "solution_temperature": (Decimal("900"), Decimal("1100")),
    "solution_time": (Decimal("1.0"), Decimal("5.0")),
    "aging_temperature": (Decimal("670"), Decimal("790")),
    "aging_time": (Decimal("1.0"), Decimal("5.0")),
}
ALLOWED_OUTPUTS: Final = frozenset(
    {"sem_image", "mechanical_properties"}
)


@dataclass(frozen=True, slots=True)
class NormalizedParameter:
    value: int | Decimal
    unit: str


@dataclass(frozen=True, slots=True)
class ZTA35GRawInput:
    material: object
    parameters: ZTA35GParameterCandidates
    requested_outputs: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class ZTA35GNormalizedInput:
    material: str | None
    solution_temperature: NormalizedParameter | None
    solution_time: NormalizedParameter | None
    aging_temperature: NormalizedParameter | None
    aging_time: NormalizedParameter | None
    requested_outputs: tuple[str, ...]

    def for_field(self, field_name: str) -> NormalizedParameter | None:
        if field_name not in PARAMETER_FIELDS:
            raise ValueError("field_name is not a ZTA35G process parameter.")
        return getattr(self, field_name)


@dataclass(frozen=True, slots=True)
class AmbiguousField:
    field: str
    candidates: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class ValidationError:
    field: str
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class ZTA35GValidationResult:
    raw_input: ZTA35GRawInput
    normalized_input: ZTA35GNormalizedInput
    missing_fields: tuple[str, ...]
    ambiguous_fields: tuple[AmbiguousField, ...]
    validation_errors: tuple[ValidationError, ...]

    def to_revision_payloads(self) -> dict[str, object]:
        raw_parameters: dict[str, object] = {}
        normalized_parameters: dict[str, object] = {}
        for field_name in PARAMETER_FIELDS:
            raw_parameter = getattr(self.raw_input.parameters, field_name)
            raw_parameters[field_name] = _raw_parameter_payload(raw_parameter)
            normalized_parameter = self.normalized_input.for_field(field_name)
            normalized_parameters[field_name] = _normalized_parameter_payload(
                normalized_parameter
            )

        return {
            "raw_input": {
                "material": _candidate_json_value(self.raw_input.material),
                "parameters": raw_parameters,
                "requested_outputs": [
                    _candidate_json_value(value)
                    for value in self.raw_input.requested_outputs
                ],
            },
            "normalized_input": {
                "material": self.normalized_input.material,
                **normalized_parameters,
                "requested_outputs": list(
                    self.normalized_input.requested_outputs
                ),
            },
            "missing_fields": list(self.missing_fields),
            "ambiguous_fields": [
                {
                    "field": item.field,
                    "candidates": [
                        _candidate_json_value(candidate)
                        for candidate in item.candidates
                    ],
                }
                for item in self.ambiguous_fields
            ],
            "validation_errors": [
                {
                    "field": item.field,
                    "code": item.code,
                    "message": item.message,
                }
                for item in self.validation_errors
            ],
        }


def _json_number(value: int | float | Decimal) -> int | float:
    if type(value) is int:
        return value
    if type(value) is float:
        if not Decimal(str(value)).is_finite():
            raise ValueError("candidate value is not a finite JSON number.")
        return value
    if not isinstance(value, Decimal):
        raise ValueError("candidate value is not a JSON number.")
    if not value.is_finite():
        raise ValueError("candidate value is not a finite JSON number.")
    if value == value.to_integral_value():
        return int(value)
    projected = float(value)
    if Decimal(str(projected)) != value:
        raise ValueError("candidate Decimal is not losslessly JSON-ready.")
    return projected


def _candidate_json_value(value: object) -> object:
    if isinstance(value, AmbiguousValue):
        return {
            "candidates": [
                _candidate_json_value(candidate)
                for candidate in value.candidates
            ]
        }
    if value is None or type(value) in (str, bool):
        return value
    if type(value) in (int, float):
        return _json_number(value)
    raise ValueError("candidate value is not JSON-ready.")


def _raw_parameter_payload(
    parameter: ParameterCandidate | None,
) -> dict[str, object] | None:
    if parameter is None:
        return None
    return {
        "value": _candidate_json_value(parameter.value),
        "unit": _candidate_json_value(parameter.unit),
    }


def _normalized_parameter_payload(
    parameter: NormalizedParameter | None,
) -> dict[str, object] | None:
    if parameter is None:
        return None
    return {
        "value": _json_number(parameter.value),
        "unit": parameter.unit,
    }


def _error(field: str, code: str, message: str) -> ValidationError:
    return ValidationError(field=field, code=code, message=message)


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(
        value,
        (int, float, Decimal),
    ):
        return None
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not converted.is_finite():
        return None
    return converted


def _parameter_component_error(
    field_name: str,
    candidate: ParameterCandidate,
) -> ValidationError | None:
    accepted_units = {"°C"} if field_name in TEMPERATURE_FIELDS else {"h", "min"}
    if (
        candidate.unit is not None
        and not isinstance(candidate.unit, AmbiguousValue)
        and (
            not isinstance(candidate.unit, str)
            or candidate.unit not in accepted_units
        )
    ):
        return _error(
            field_name,
            "INVALID_PROCESS_PARAMETER_UNIT",
            "The unit is not supported for this field.",
        )
    if (
        candidate.value is not None
        and not isinstance(candidate.value, AmbiguousValue)
        and _decimal(candidate.value) is None
    ):
        return _error(
            field_name,
            "INVALID_PROCESS_PARAMETER_TYPE",
            "The value must be a finite number and must not be boolean.",
        )
    return None


def _normalize_parameter(
    field_name: str,
    candidate: ParameterCandidate,
) -> tuple[NormalizedParameter | None, ValidationError | None]:
    expected_unit = "°C" if field_name in TEMPERATURE_FIELDS else "h"
    accepted_units = {"°C"} if field_name in TEMPERATURE_FIELDS else {"h", "min"}
    if not isinstance(candidate.unit, str) or candidate.unit not in accepted_units:
        return None, _error(
            field_name,
            "INVALID_PROCESS_PARAMETER_UNIT",
            "The unit is not supported for this field.",
        )

    value = _decimal(candidate.value)
    if value is None:
        return None, _error(
            field_name,
            "INVALID_PROCESS_PARAMETER_TYPE",
            "The value must be a finite number and must not be boolean.",
        )
    if candidate.unit == "min":
        value = value / Decimal("60")

    lower, upper = FIELD_RANGES[field_name]
    if value < lower or value > upper:
        return None, _error(
            field_name,
            "PROCESS_PARAMETERS_OUT_OF_RANGE",
            "The value is outside the supported inclusive range.",
        )

    if field_name in TEMPERATURE_FIELDS:
        if value != value.to_integral_value():
            return None, _error(
                field_name,
                "INVALID_PROCESS_PARAMETER_PRECISION",
                "Temperature must be losslessly representable as an integer.",
            )
        normalized_value: int | Decimal = int(value)
    else:
        scaled = value * Decimal("10")
        if scaled != scaled.to_integral_value():
            return None, _error(
                field_name,
                "INVALID_PROCESS_PARAMETER_PRECISION",
                "Time must have at most one decimal place in hours.",
            )
        normalized_value = value
    return NormalizedParameter(normalized_value, expected_unit), None


def _normalize_outputs(
    values: tuple[object, ...],
) -> tuple[tuple[str, ...], ValidationError | None]:
    if not values:
        return (), _error(
            "requested_outputs",
            "INVALID_REQUESTED_OUTPUT",
            "At least one requested output is required.",
        )
    normalized: list[str] = []
    has_unknown = False
    for value in values:
        if not isinstance(value, str) or value not in ALLOWED_OUTPUTS:
            has_unknown = True
            continue
        if value not in normalized:
            normalized.append(value)
    if has_unknown:
        return tuple(normalized), _error(
            "requested_outputs",
            "INVALID_REQUESTED_OUTPUT",
            "A requested output is not supported.",
        )
    return tuple(normalized), None


def normalize_zta35g_candidate(
    candidate: ToolCandidate | NeedsInputCandidate,
) -> ZTA35GValidationResult:
    raw_input = ZTA35GRawInput(
        material=candidate.material,
        parameters=candidate.candidate_parameters,
        requested_outputs=candidate.requested_outputs,
    )
    hints = (
        candidate.ambiguous_fields
        if isinstance(candidate, NeedsInputCandidate)
        else ()
    )
    missing_hints = (
        candidate.missing_fields
        if isinstance(candidate, NeedsInputCandidate)
        else ()
    )
    unknown_missing_hints = tuple(
        hint for hint in missing_hints if hint not in KNOWN_FIELDS
    )
    unknown_hints = tuple(
        hint for hint in hints if hint not in KNOWN_FIELDS
    )

    missing: list[str] = []
    ambiguous: list[AmbiguousField] = []
    errors: list[ValidationError] = []
    normalized_material: str | None = None

    if isinstance(candidate.material, AmbiguousValue):
        ambiguous.append(
            AmbiguousField("material", candidate.material.candidates)
        )
    elif candidate.material is None or (
        isinstance(candidate.material, str) and not candidate.material.strip()
    ):
        missing.append("material")
    elif not isinstance(candidate.material, str):
        errors.append(
            _error(
                "material",
                "INVALID_MATERIAL_TYPE",
                "Material must be text.",
            )
        )
    elif candidate.material != "ZTA35G":
        errors.append(
            _error(
                "material",
                "UNSUPPORTED_MATERIAL",
                "Only ZTA35G is supported.",
            )
        )
    else:
        normalized_material = "ZTA35G"

    normalized_parameters: dict[str, NormalizedParameter | None] = {}
    for field_name in PARAMETER_FIELDS:
        raw_parameter = getattr(candidate.candidate_parameters, field_name)
        if raw_parameter is None:
            missing.append(field_name)
            normalized_parameters[field_name] = None
            continue
        component_error = _parameter_component_error(field_name, raw_parameter)
        if component_error is not None:
            errors.append(component_error)
            normalized_parameters[field_name] = None
            continue
        ambiguity = None
        if isinstance(raw_parameter.value, AmbiguousValue):
            ambiguity = raw_parameter.value
        elif isinstance(raw_parameter.unit, AmbiguousValue):
            ambiguity = raw_parameter.unit
        if ambiguity is not None:
            ambiguous.append(
                AmbiguousField(field_name, ambiguity.candidates)
            )
            normalized_parameters[field_name] = None
            continue
        if raw_parameter.value is None or raw_parameter.unit is None:
            missing.append(field_name)
            normalized_parameters[field_name] = None
            continue
        normalized, validation_error = _normalize_parameter(
            field_name,
            raw_parameter,
        )
        normalized_parameters[field_name] = normalized
        if validation_error is not None:
            errors.append(validation_error)

    normalized_outputs, output_error = _normalize_outputs(
        candidate.requested_outputs
    )
    if output_error is not None:
        errors.append(output_error)
    if unknown_missing_hints:
        errors.append(
            _error(
                "missing_fields",
                "UNKNOWN_FIELD",
                "A missing-field hint referenced an unknown field.",
            )
        )
    if unknown_hints:
        errors.append(
            _error(
                "ambiguous_fields",
                "UNKNOWN_FIELD",
                "An ambiguity hint referenced an unknown field.",
            )
        )

    return ZTA35GValidationResult(
        raw_input=raw_input,
        normalized_input=ZTA35GNormalizedInput(
            material=normalized_material,
            requested_outputs=normalized_outputs,
            **normalized_parameters,
        ),
        missing_fields=tuple(missing),
        ambiguous_fields=tuple(ambiguous),
        validation_errors=tuple(errors),
    )
