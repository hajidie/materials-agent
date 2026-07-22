from __future__ import annotations

import base64
from hashlib import sha256
from io import BytesIO

import numpy as np
import pytest

from materialsagent.domain.ports.tool_execution import ToolImagePayload


def _payload(
    array: np.ndarray,
    *,
    image_role: str = "generated_sem",
    requested_output: bool = True,
    metadata: dict[str, object] | None = None,
) -> ToolImagePayload:
    buffer = BytesIO()
    np.save(buffer, array, allow_pickle=True)
    npy_bytes = buffer.getvalue()
    values: dict[str, object] = {
        "image_role": image_role,
        "requested_output": requested_output,
        "dtype": "float32",
        "numpy_dtype": "<f4",
        "shape": (512, 512),
        "channel_layout": "GRAYSCALE_2D",
        "value_range": (-1.0, 1.0),
        "encoding": "base64+npy",
        "byte_order": "little",
        "array_order": "C",
        "sha256": sha256(npy_bytes).hexdigest(),
        "data_base64": base64.b64encode(npy_bytes).decode("ascii"),
    }
    values.update(metadata or {})
    return ToolImagePayload(**values)  # type: ignore[arg-type]


def _valid_array() -> np.ndarray:
    return np.linspace(
        -1.0,
        1.0,
        512 * 512,
        dtype=np.dtype("<f4"),
    ).reshape((512, 512))


def _assert_invalid(payload: ToolImagePayload) -> None:
    from materialsagent.application.image_payload import (
        InvalidModelOutputError,
        decode_image_payload,
    )

    with pytest.raises(
        InvalidModelOutputError,
        match=r"^Tool Runtime returned invalid model output\.$",
    ) as exc_info:
        decode_image_payload(payload)
    assert exc_info.value.code == "INVALID_MODEL_OUTPUT"


def test_valid_deterministic_npy_is_strictly_decoded_in_memory() -> None:
    from materialsagent.application.image_payload import decode_image_payload

    payload = _payload(_valid_array())

    decoded = decode_image_payload(payload)

    assert decoded.array.shape == (512, 512)
    assert decoded.array.ndim == 2
    assert decoded.array.dtype.str == "<f4"
    assert decoded.array.flags.c_contiguous is True
    assert decoded.array.flags.f_contiguous is False
    assert decoded.array.flags.writeable is False
    assert decoded.npy_sha256 == payload.sha256


@pytest.mark.parametrize("data_base64", ["", "%%%", "AAAA="])
def test_illegal_empty_or_badly_padded_base64_is_rejected(
    data_base64: str,
) -> None:
    _assert_invalid(
        _payload(
            _valid_array(),
            metadata={"data_base64": data_base64},
        )
    )


def test_decoded_npy_larger_than_four_mib_is_rejected() -> None:
    oversized = b"x" * ((4 * 1024 * 1024) + 1)
    _assert_invalid(
        _payload(
            _valid_array(),
            metadata={
                "data_base64": base64.b64encode(oversized).decode("ascii"),
                "sha256": sha256(oversized).hexdigest(),
            },
        )
    )


def test_npy_sha256_mismatch_is_rejected() -> None:
    _assert_invalid(
        _payload(_valid_array(), metadata={"sha256": "0" * 64})
    )


@pytest.mark.parametrize(
    "array",
    [
        np.zeros((512, 512), dtype=object),
        np.zeros((512, 512), dtype=[("value", "<f4")]),
        np.zeros((512, 512), dtype=">f4"),
        np.zeros((512, 512), dtype="<f8"),
        np.zeros((512, 512), dtype="<i4"),
        np.zeros((511, 512), dtype="<f4"),
        np.zeros((1, 512, 512), dtype="<f4"),
        np.asfortranarray(np.zeros((512, 512), dtype="<f4")),
    ],
    ids=[
        "object",
        "structured",
        "big-endian",
        "float64",
        "integer",
        "wrong-shape",
        "three-dimensional",
        "fortran-order",
    ],
)
def test_unsafe_dtype_shape_dimension_or_order_is_rejected(
    array: np.ndarray,
) -> None:
    _assert_invalid(_payload(array))


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf, -1.0001, 1.0001])
def test_nonfinite_or_out_of_range_values_are_rejected(value: float) -> None:
    array = _valid_array()
    array[0, 0] = value
    _assert_invalid(_payload(array))


def test_truncated_or_trailing_npy_bytes_are_rejected() -> None:
    original = _payload(_valid_array())
    npy_bytes = base64.b64decode(original.data_base64, validate=True)

    for broken in (npy_bytes[:-1], npy_bytes + b"trailing"):
        _assert_invalid(
            _payload(
                _valid_array(),
                metadata={
                    "data_base64": base64.b64encode(broken).decode("ascii"),
                    "sha256": sha256(broken).hexdigest(),
                },
            )
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("dtype", "float64"),
        ("numpy_dtype", ">f4"),
        ("shape", (256, 1024)),
        ("channel_layout", "CHW"),
        ("value_range", (-2.0, 2.0)),
        ("encoding", "raw"),
        ("byte_order", "big"),
        ("array_order", "F"),
    ],
)
def test_payload_metadata_mismatch_is_rejected(
    field: str,
    value: object,
) -> None:
    _assert_invalid(_payload(_valid_array(), metadata={field: value}))
