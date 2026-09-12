from dataclasses import replace
import base64
from hashlib import sha256
from io import BytesIO
import numpy as np
from materialsagent.domain.ports.storage import StorageUnavailableError, StoredObjectMetadata, StorageConflictError, StorageObjectNotFoundError
from materialsagent.domain.ports.tool_execution import ToolExecutionOutput, ToolImagePayload, ToolClientUnavailableError
class _MemoryStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, StoredObjectMetadata]] = {}
        self.unavailable = False
        self.head_error: Exception | None = None
        self.get_calls = 0

    def put(self, object_key, payload, content_type, metadata=None):
        if self.unavailable:
            raise StorageUnavailableError("Object storage unavailable.")
        expected = StoredObjectMetadata(
            object_key=object_key,
            size_bytes=len(payload),
            sha256=sha256(payload).hexdigest(),
            content_type=content_type,
            metadata=dict(metadata or {}),
        )
        current = self.objects.get(object_key)
        if current is not None and current[1] != expected:
            raise StorageConflictError("Object storage conflict.")
        self.objects[object_key] = (payload, expected)
        return expected

    def head(self, object_key):
        if self.unavailable:
            raise StorageUnavailableError("Object storage unavailable.")
        if self.head_error is not None:
            raise self.head_error
        current = self.objects.get(object_key)
        return None if current is None else current[1]

    def get(self, object_key, *, max_bytes):
        if self.unavailable:
            raise StorageUnavailableError("Object storage unavailable.")
        self.get_calls += 1
        current = self.objects.get(object_key)
        if current is None:
            raise StorageObjectNotFoundError("Stored object not found.")
        if len(current[0]) > max_bytes:
            from materialsagent.domain.ports.storage import StorageIntegrityError

            raise StorageIntegrityError("Stored object integrity check failed.")
        return current[0]

    def delete(self, object_key):
        if self.unavailable:
            raise StorageUnavailableError("Object storage unavailable.")
        self.objects.pop(object_key, None)


def _valid_image() -> ToolImagePayload:
    values = np.linspace(-1.0, 1.0, 512 * 512, dtype="<f4").reshape(
        (512, 512)
    )
    buffer = BytesIO()
    np.save(buffer, values, allow_pickle=False)
    raw = buffer.getvalue()
    return ToolImagePayload(
        image_role="generated_sem",
        requested_output=True,
        dtype="float32",
        numpy_dtype="<f4",
        shape=(512, 512),
        channel_layout="GRAYSCALE_2D",
        value_range=(-1.0, 1.0),
        encoding="base64+npy",
        byte_order="little",
        array_order="C",
        sha256=sha256(raw).hexdigest(),
        data_base64=base64.b64encode(raw).decode("ascii"),
    )


class _Runtime:
    def __init__(
        self,
        *,
        partial: bool = False,
        failed: bool = False,
    ) -> None:
        self.partial = partial
        self.failed = failed
        self.calls = 0

    def execute(self, _metadata, validated_input, _context):
        self.calls += 1
        requested = tuple(validated_input.requested_outputs)
        image = replace(
            _valid_image(),
            requested_output="sem_image" in requested,
            image_role=(
                "generated_sem"
                if "sem_image" in requested
                else "intermediate_sem"
            ),
        )
        if self.failed:
            return ToolExecutionOutput(
                status="FAILED",
                requested_outputs=requested,
                completed_outputs=(),
                failed_outputs=requested,
                data={},
                images=(image,),
                warnings=(),
                diagnostics=(),
                actual_runtime_parameters=dict(
                    validated_input.runtime_parameters
                ),
                model_bundle_id="private-model-bundle",
                error={
                    "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
                    "safe_message": "Private Runtime wording.",
                    "retryable": False,
                },
            )
        if self.partial:
            return ToolExecutionOutput(
                status="PARTIALLY_SUCCEEDED",
                requested_outputs=tuple(validated_input.requested_outputs),
                completed_outputs=("sem_image",),
                failed_outputs=("mechanical_properties",),
                data={},
                images=(image,),
                warnings=(),
                diagnostics=(),
                actual_runtime_parameters=dict(
                    validated_input.runtime_parameters
                ),
                model_bundle_id="private-model-bundle",
                error={
                    "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
                    "safe_message": "Private Runtime wording.",
                    "retryable": False,
                },
            )
        return ToolExecutionOutput(
            status="SUCCEEDED",
            requested_outputs=tuple(validated_input.requested_outputs),
            completed_outputs=tuple(validated_input.requested_outputs),
            failed_outputs=(),
            data=(
                {
                    "yield_strength": {
                        "value": 1000.0,
                        "unit": "MPa",
                    },
                    "elongation": {"value": 8.2, "unit": "%"},
                }
                if "mechanical_properties" in requested
                else {}
            ),
            images=(image,),
            warnings=(),
            diagnostics=(),
            actual_runtime_parameters=dict(
                validated_input.runtime_parameters
            ),
            model_bundle_id="private-model-bundle",
            error=None,
        )

    def readiness(self, _metadata):
        return "AVAILABLE"


class _UnavailableRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _metadata, _validated_input, _context):
        self.calls += 1
        raise ToolClientUnavailableError()

    def readiness(self, _metadata):
        return "AVAILABLE"
