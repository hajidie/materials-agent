from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import re

import numpy as np

from materialsagent.domain.ports.tool_execution import ToolImagePayload


MAX_NPY_BYTES = 4 * 1024 * 1024
MAX_BASE64_CHARS = 4 * ((MAX_NPY_BYTES + 2) // 3)
EXPECTED_DTYPE = np.dtype("<f4")
EXPECTED_SHAPE = (512, 512)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class InvalidModelOutputError(RuntimeError):
    code = "INVALID_MODEL_OUTPUT"

    def __init__(self) -> None:
        super().__init__("Tool Runtime returned invalid model output.")


@dataclass(frozen=True, slots=True)
class DecodedImagePayload:
    array: np.ndarray
    npy_sha256: str


def _metadata_is_valid(payload: ToolImagePayload) -> bool:
    return (
        payload.image_role in {"generated_sem", "intermediate_sem"}
        and payload.requested_output
        is (payload.image_role == "generated_sem")
        and payload.dtype == "float32"
        and payload.numpy_dtype == "<f4"
        and payload.shape == EXPECTED_SHAPE
        and payload.channel_layout == "GRAYSCALE_2D"
        and payload.value_range == (-1.0, 1.0)
        and payload.encoding == "base64+npy"
        and payload.byte_order == "little"
        and payload.array_order == "C"
        and isinstance(payload.sha256, str)
        and SHA256_PATTERN.fullmatch(payload.sha256) is not None
        and isinstance(payload.data_base64, str)
        and bool(payload.data_base64)
        and len(payload.data_base64) <= MAX_BASE64_CHARS
    )


def decode_image_payload(payload: ToolImagePayload) -> DecodedImagePayload:
    if not isinstance(payload, ToolImagePayload) or not _metadata_is_valid(payload):
        raise InvalidModelOutputError()

    try:
        encoded = payload.data_base64.encode("ascii")
        npy_bytes = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, ValueError, binascii.Error):
        raise InvalidModelOutputError() from None
    if not npy_bytes or len(npy_bytes) > MAX_NPY_BYTES:
        raise InvalidModelOutputError()

    digest = sha256(npy_bytes).hexdigest()
    if digest != payload.sha256:
        raise InvalidModelOutputError()

    buffer = BytesIO(npy_bytes)
    try:
        array = np.load(buffer, allow_pickle=False)
    except Exception:
        raise InvalidModelOutputError() from None
    if not isinstance(array, np.ndarray) or buffer.tell() != len(npy_bytes):
        raise InvalidModelOutputError()
    if (
        array.dtype.fields is not None
        or array.dtype.hasobject
        or array.dtype.str != "<f4"
        or array.dtype != EXPECTED_DTYPE
        or array.ndim != 2
        or array.shape != EXPECTED_SHAPE
        or not array.flags.c_contiguous
        or array.flags.f_contiguous
    ):
        raise InvalidModelOutputError()
    if not np.isfinite(array).all():
        raise InvalidModelOutputError()
    if bool(np.any(array < -1.0)) or bool(np.any(array > 1.0)):
        raise InvalidModelOutputError()

    array.setflags(write=False)
    return DecodedImagePayload(array=array, npy_sha256=digest)
