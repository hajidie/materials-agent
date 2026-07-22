from __future__ import annotations

from hashlib import sha256
from io import BytesIO

import numpy as np
from PIL import Image
import pytest


def _valid_array() -> np.ndarray:
    return np.zeros((512, 512), dtype=np.dtype("<f4"))


def test_fixed_half_up_quantization_maps_boundaries_zero_and_midpoints() -> None:
    from materialsagent.application.png_encoder import encode_grayscale_png

    array = _valid_array()
    array[0, :5] = [-1.0, -0.5, 0.0, 0.5, 1.0]

    encoded = encode_grayscale_png(array)

    with Image.open(BytesIO(encoded.payload)) as image:
        assert list(image.tobytes())[:5] == [0, 64, 128, 191, 255]


def test_png_encoding_is_deterministic_and_metadata_matches_final_bytes() -> None:
    from materialsagent.application.png_encoder import encode_grayscale_png

    array = np.linspace(
        -1.0,
        1.0,
        512 * 512,
        dtype=np.dtype("<f4"),
    ).reshape((512, 512))

    first = encode_grayscale_png(array)
    second = encode_grayscale_png(array.copy(order="C"))

    assert first == second
    assert first.payload.startswith(b"\x89PNG\r\n\x1a\n")
    assert first.sha256 == sha256(first.payload).hexdigest()
    assert first.size_bytes == len(first.payload)
    assert first.sha256 == "f7ab771d76dd6eea15b9df201b81c7286624c831bacf71aa1a4fd55894bfa269"
    assert first.size_bytes == 1480
    assert first.width == 512
    assert first.height == 512
    assert first.bit_depth == 8
    assert first.media_type == "image/png"
    with Image.open(BytesIO(first.payload)) as image:
        assert image.format == "PNG"
        assert image.mode == "L"
        assert image.size == (512, 512)
        assert "transparency" not in image.info


@pytest.mark.parametrize(
    "array",
    [
        np.zeros((512, 512), dtype="<f8"),
        np.zeros((511, 512), dtype="<f4"),
        np.asfortranarray(np.zeros((512, 512), dtype="<f4")),
    ],
    ids=["wrong-dtype", "wrong-shape", "fortran-order"],
)
def test_invalid_array_contract_is_rejected_without_repair(
    array: np.ndarray,
) -> None:
    from materialsagent.application.png_encoder import InvalidPngInputError
    from materialsagent.application.png_encoder import encode_grayscale_png

    with pytest.raises(
        InvalidPngInputError,
        match=r"^PNG input does not satisfy the fixed image contract\.$",
    ):
        encode_grayscale_png(array)


@pytest.mark.parametrize("value", [np.nan, np.inf, -np.inf, -1.01, 1.01])
def test_invalid_values_are_rejected_without_clip_or_replacement(
    value: float,
) -> None:
    from materialsagent.application.png_encoder import InvalidPngInputError
    from materialsagent.application.png_encoder import encode_grayscale_png

    array = _valid_array()
    array[0, 0] = value

    with pytest.raises(InvalidPngInputError):
        encode_grayscale_png(array)
