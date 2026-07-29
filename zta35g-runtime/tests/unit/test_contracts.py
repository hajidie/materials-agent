import ast
import base64
from dataclasses import dataclass
from hashlib import sha256
import json
import math
from pathlib import Path

import pytest

import materialsagent_zta35g_runtime.contracts as contracts_module
from materialsagent_zta35g_runtime.constants import (
    GUIDE_SCALE,
    MAX_REQUEST_BYTES,
    MODEL_BUNDLE_ID,
    NUM_SAMPLES,
    TIMESTEPS,
)
from materialsagent_zta35g_runtime.contracts import (
    ContractError,
    build_execute_response,
    parse_execute_request,
    safe_error,
    serialize_response,
)


def _payload(**overrides):
    payload = {
        "runtime_contract_version": "1.0",
        "request_id": "req_contract",
        "task_id": "task_contract",
        "tool_run_id": "trun_contract",
        "tool_id": "zta35g_sem_virtual_lab",
        "tool_version": "0.1.0",
        "schema_version": "1.0",
        "process_parameters": {
            "solution_temperature": 1000,
            "solution_time": 3.0,
            "aging_temperature": 730,
            "aging_time": 3.0,
        },
        "requested_outputs": ["sem_image", "mechanical_properties"],
        "runtime_parameters": {
            "seed": 123,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
    }
    payload.update(overrides)
    return payload


def _parse(payload=None, content_type="application/json"):
    body = json.dumps(
        payload if payload is not None else _payload(),
        allow_nan=True,
    ).encode("utf-8")
    return parse_execute_request(body, content_type=content_type)


@pytest.mark.parametrize(
    "outputs",
    [
        ["sem_image"],
        ["mechanical_properties"],
        ["sem_image", "mechanical_properties"],
    ],
)
def test_three_legal_output_modes_are_strictly_parsed(outputs):
    request = _parse(_payload(requested_outputs=outputs))

    assert request.requested_outputs == tuple(outputs)
    assert request.runtime_parameters == {
        "seed": 123,
        "num_samples": NUM_SAMPLES,
        "guide_scale": GUIDE_SCALE,
        "timesteps": TIMESTEPS,
    }


@pytest.mark.parametrize(
    ("mutator", "expected_code"),
    [
        (lambda value: value.update({"extra": "no"}), "INVALID_RUNTIME_REQUEST"),
        (
            lambda value: value["process_parameters"].update({"extra": 1}),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value["runtime_parameters"].update({"extra": 1}),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value.update({"request_id": "   "}),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value.update({"request_id": "x" * 257}),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value["runtime_parameters"].update({"seed": True}),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value["runtime_parameters"].update({"seed": -1}),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value["runtime_parameters"].update(
                {"seed": 2**63}
            ),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value["runtime_parameters"].update(
                {"guide_scale": 2.1}
            ),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value["runtime_parameters"].update(
                {"timesteps": 999}
            ),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value["runtime_parameters"].update(
                {"num_samples": 2}
            ),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value["process_parameters"].update(
                {"solution_temperature": 899}
            ),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value["process_parameters"].update(
                {"aging_temperature": 790.5}
            ),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value["process_parameters"].update(
                {"solution_time": 2.55}
            ),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value.update(
                {"requested_outputs": ["sem_image", "sem_image"]}
            ),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value.update({"requested_outputs": ["unknown"]}),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value["process_parameters"].update(
                {"aging_time": math.inf}
            ),
            "INVALID_RUNTIME_REQUEST",
        ),
        (
            lambda value: value.update({"runtime_contract_version": "2.0"}),
            "SCHEMA_VERSION_MISMATCH",
        ),
        (
            lambda value: value.update({"tool_id": "other"}),
            "UNSUPPORTED_TOOL",
        ),
        (
            lambda value: value.update({"tool_version": "9.9.9"}),
            "TOOL_VERSION_MISMATCH",
        ),
        (
            lambda value: value.update({"schema_version": "9.9"}),
            "SCHEMA_VERSION_MISMATCH",
        ),
    ],
)
def test_invalid_contract_facts_fail_closed(mutator, expected_code):
    payload = _payload()
    mutator(payload)

    with pytest.raises(ContractError) as raised:
        _parse(payload)

    assert raised.value.code == expected_code
    assert "req_contract" not in str(raised.value)


def test_wrong_content_type_invalid_json_and_request_limit_are_rejected():
    with pytest.raises(ContractError) as wrong_type:
        _parse(content_type="text/plain")
    assert wrong_type.value.http_status == 415

    with pytest.raises(ContractError):
        parse_execute_request(b"{", content_type="application/json")

    with pytest.raises(ContractError):
        parse_execute_request(
            b" " * (MAX_REQUEST_BYTES + 1),
            content_type="application/json",
        )


def test_seed_accepts_the_exact_signed_64_bit_nonnegative_boundary():
    payload = _payload()
    payload["runtime_parameters"]["seed"] = (2**63) - 1

    request = _parse(payload)

    assert request.runtime_parameters["seed"] == (2**63) - 1


def test_json_decoder_recursion_error_is_a_bounded_bad_request(monkeypatch):
    def raise_recursion_error(*_args, **_kwargs):
        raise RecursionError("private decoder recursion detail")

    monkeypatch.setattr(
        contracts_module.json,
        "loads",
        raise_recursion_error,
    )
    with pytest.raises(ContractError) as raised:
        parse_execute_request(
            b"{}",
            content_type="application/json",
        )

    assert raised.value.http_status == 400
    assert raised.value.code == "INVALID_RUNTIME_REQUEST"
    assert "private decoder recursion detail" not in str(raised.value)


def test_safe_error_details_are_scalar_bounded_and_do_not_echo_unsafe_values():
    body = safe_error(
        code="MODEL_LOAD_FAILED",
        safe_message="模型加载失败。",
        retryable=False,
        failed_step="model_loading",
        details={"file": "ddpm_512_epoch_800.pth", "count": 4},
    )
    assert body["error"]["details"]["count"] == 4

    for details in (
        {"x" * 65: 1},
        {"secret": "x" * 257},
        {"nested": {"no": "objects"}},
        dict(("k%s" % index, index) for index in range(11)),
    ):
        with pytest.raises(ValueError):
            safe_error(
                code="MODEL_LOAD_FAILED",
                safe_message="模型加载失败。",
                retryable=False,
                failed_step="model_loading",
                details=details,
            )


def test_response_serialization_is_compact_finite_and_size_bounded():
    encoded = serialize_response({"ok": "值"})
    assert encoded == b'{"ok":"\\u503c"}'

    with pytest.raises(ContractError):
        serialize_response({"bad": float("nan")})

    with pytest.raises(ContractError):
        serialize_response({"large": "x" * (4 * 1024 * 1024)})


@dataclass(frozen=True)
class _Result:
    status: str
    completed_outputs: tuple
    failed_outputs: tuple
    data: dict
    images: tuple
    warnings: tuple
    diagnostics: tuple
    model_bundle_id: str
    error: object


def _image():
    payload = b"fake-npy"
    return {
        "image_role": "generated_sem",
        "requested_output": True,
        "dtype": "float32",
        "numpy_dtype": "<f4",
        "shape": [512, 512],
        "channel_layout": "GRAYSCALE_2D",
        "value_range": [-1.0, 1.0],
        "encoding": "base64+npy",
        "byte_order": "little",
        "array_order": "C",
        "sha256": sha256(payload).hexdigest(),
        "data_base64": base64.b64encode(payload).decode("ascii"),
    }


def _result(**overrides):
    values = {
        "status": "SUCCEEDED",
        "completed_outputs": ("sem_image", "mechanical_properties"),
        "failed_outputs": (),
        "data": {
            "yield_strength": {"value": 650.0, "unit": "MPa"},
            "elongation": {"value": 3.2, "unit": "%"},
        },
        "images": (_image(),),
        "warnings": (),
        "diagnostics": (
            {
                "step": "sem_generation",
                "status": "SUCCEEDED",
                "started_at": "2026-07-29T00:00:00.000Z",
                "completed_at": "2026-07-29T00:00:01.000Z",
                "duration_ms": 1000.0,
                "error_code": None,
                "safe_error_message": None,
            },
        ),
        "model_bundle_id": "zta35g-sem-original-bundle",
        "error": None,
    }
    values.update(overrides)
    return _Result(**values)


def test_execute_response_contract_strictly_validates_all_nested_records():
    request = _parse()
    response = build_execute_response(request, _result())

    assert response["status"] == "SUCCEEDED"
    assert response["images"][0]["sha256"] == _image()["sha256"]
    assert response["diagnostics"][0]["duration_ms"] == 1000.0


@pytest.mark.parametrize(
    "model_bundle_id",
    (
        "",
        "other-bundle",
        " zta35g-sem-original-bundle",
        "zta35g-sem-original-bundle ",
        "x" * 257,
    ),
)
def test_execute_response_rejects_every_noncanonical_bundle_id(
    model_bundle_id,
):
    request = _parse()

    with pytest.raises(ContractError) as raised:
        build_execute_response(
            request,
            _result(model_bundle_id=model_bundle_id),
        )

    assert raised.value.code == "INTERNAL_RUNTIME_ERROR"
    assert MODEL_BUNDLE_ID not in str(raised.value)


@pytest.mark.parametrize(
    "result",
    [
        _result(completed_outputs=("sem_image", "sem_image")),
        _result(completed_outputs=("sem_image",), failed_outputs=()),
        _result(status="PARTIALLY_SUCCEEDED", error=None),
        _result(
            diagnostics=(
                {
                    **_result().diagnostics[0],
                    "extra": "no",
                },
            )
        ),
        _result(
            diagnostics=(
                {
                    **_result().diagnostics[0],
                    "started_at": "2026-07-29T00:00:00Z",
                },
            )
        ),
        _result(
            images=(
                {
                    **_image(),
                    "sha256": "0" * 64,
                },
            )
        ),
        _result(
            data={
                "yield_strength": {"value": float("inf"), "unit": "MPa"},
                "elongation": {"value": 3.2, "unit": "%"},
            }
        ),
        _result(
            warnings=(
                {"code": "X", "safe_message": "x" * 257},
            )
        ),
        _result(model_bundle_id="   "),
    ],
)
def test_invalid_execute_response_facts_fail_closed(result):
    request = _parse()

    with pytest.raises(ContractError) as raised:
        build_execute_response(request, result)

    assert raised.value.code == "INTERNAL_RUNTIME_ERROR"


def test_runtime_production_sources_obey_python_38_syntax_and_banned_syntax():
    source_root = Path(__file__).resolve().parents[2] / "src"
    sources = sorted(source_root.rglob("*.py"))
    assert sources
    for source_path in sources:
        source = source_path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(source_path), feature_version=(3, 8))
        assert "slots=True" not in source
        annotations = []
        for node in ast.walk(tree):
            assert not isinstance(node, getattr(ast, "Match", ()))
            if isinstance(node, ast.Subscript) and isinstance(
                node.value, ast.Name
            ):
                assert node.value.id not in {
                    "list",
                    "dict",
                    "tuple",
                    "set",
                    "frozenset",
                    "type",
                }
            if isinstance(node, ast.AnnAssign):
                annotations.append(node.annotation)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotations.extend(
                    argument.annotation
                    for argument in (
                        list(node.args.posonlyargs)
                        + list(node.args.args)
                        + list(node.args.kwonlyargs)
                    )
                    if argument.annotation is not None
                )
                if (
                    node.args.vararg is not None
                    and node.args.vararg.annotation is not None
                ):
                    annotations.append(node.args.vararg.annotation)
                if (
                    node.args.kwarg is not None
                    and node.args.kwarg.annotation is not None
                ):
                    annotations.append(node.args.kwarg.annotation)
                if node.returns is not None:
                    annotations.append(node.returns)
        for annotation in annotations:
            for node in ast.walk(annotation):
                if isinstance(node, ast.BinOp):
                    assert not isinstance(node.op, ast.BitOr)
