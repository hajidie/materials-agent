from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO

import numpy as np
from PIL import Image


EXPECTED_DTYPE = np.dtype("<f4")
EXPECTED_SHAPE = (512, 512)


class InvalidPngInputError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PNG input does not satisfy the fixed image contract.")


class PngEncodingError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("PNG encoding failed.")


@dataclass(frozen=True, slots=True)
class EncodedPng:
    payload: bytes
    sha256: str
    size_bytes: int
    width: int = 512
    height: int = 512
    bit_depth: int = 8
    media_type: str = "image/png"


def _validate_array(array: np.ndarray) -> None:
    if (
        not isinstance(array, np.ndarray)
        or array.dtype.fields is not None
        or array.dtype.hasobject
        or array.dtype.str != "<f4"
        or array.dtype != EXPECTED_DTYPE
        or array.ndim != 2
        or array.shape != EXPECTED_SHAPE
        or not array.flags.c_contiguous
        or array.flags.f_contiguous
        or not np.isfinite(array).all()
        or bool(np.any(array < -1.0))
        or bool(np.any(array > 1.0))
    ):
        raise InvalidPngInputError()


def encode_grayscale_png(array: np.ndarray) -> EncodedPng:
    _validate_array(array)
    quantized = np.floor(
        ((array.astype(np.float64) + 1.0) / 2.0) * 255.0 + 0.5
    ).astype(np.uint8)
    output = BytesIO()
    try:
        image = Image.fromarray(quantized)
        if image.mode != "L" or image.size != EXPECTED_SHAPE:
            raise PngEncodingError()
        image.save(
            output,
            format="PNG",
            optimize=False,
            compress_level=9,
        )
    except PngEncodingError:
        raise
    except Exception:
        raise PngEncodingError() from None
    payload = output.getvalue()
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise PngEncodingError()
    return EncodedPng(
        payload=payload,
        sha256=sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
