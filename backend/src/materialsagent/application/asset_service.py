from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import logging
import re
from uuid import uuid4

from PIL import Image

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationConflictError,
    ApplicationError,
    ApplicationInternalError,
    DependencyUnavailableError,
    ResourceNotFoundError,
    from_persistence_error,
)
from materialsagent.application.image_payload import (
    InvalidModelOutputError,
    decode_image_payload,
)
from materialsagent.application.png_encoder import (
    EncodedPng,
    PngEncodingError,
    encode_grayscale_png,
)
from materialsagent.application.tool_execution import (
    normalize_tool_output_summary,
)
from materialsagent.domain.models.asset import Asset
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.storage import (
    MAX_STORAGE_GET_BYTES,
    StorageError,
    StorageIntegrityError,
    StorageObjectNotFoundError,
    StoredObjectMetadata,
    StorageService,
    StorageUnavailableError,
    StorageWriteOutcomeUnknownError,
)
from materialsagent.domain.ports.tool_execution import (
    ToolExecutionOutput,
    ToolImagePayload,
)
from materialsagent.domain.ports.unit_of_work import UnitOfWorkFactory


Clock = Callable[[], datetime]
IdFactory = Callable[[], str]
ImageDecoder = Callable[[ToolImagePayload], object]
PngEncoder = Callable[[object], EncodedPng]
_ENVIRONMENT_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")
_ENCODING_RULE = "linear[-1,1]-to-uint8-half-up;png-gray8"
MAX_PNG_BYTES = 1024 * 1024
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_logger = logging.getLogger("materialsagent.asset_lifecycle")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _opaque_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


class AssetLifecycleOutcomeError(ApplicationError):
    def __init__(
        self,
        *,
        asset_id: str,
        current_status: str,
        code: str,
        message: str,
        status_code: int,
        task_id: str,
    ) -> None:
        super().__init__(
            message,
            code=code,
            status_code=status_code,
            task_id=task_id,
        )
        self.asset_id = asset_id
        self.current_status = current_status


@dataclass(frozen=True, slots=True)
class AssetContent:
    payload: bytes
    media_type: str
    filename: str


class AssetService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        storage: StorageService,
        *,
        environment: str,
        bucket: str,
        storage_namespace: str,
        clock: Clock | None = None,
        asset_id_factory: IdFactory | None = None,
        operation_id_factory: IdFactory | None = None,
        image_decoder: ImageDecoder | None = None,
        png_encoder: PngEncoder | None = None,
    ) -> None:
        if _ENVIRONMENT_PATTERN.fullmatch(environment) is None:
            raise ValueError("environment is not safe for object keys.")
        if not isinstance(bucket, str) or not bucket.strip():
            raise ValueError("bucket is required.")
        if not isinstance(storage_namespace, str) or not storage_namespace.strip():
            raise ValueError("storage_namespace is required.")
        self._unit_of_work_factory = unit_of_work_factory
        self._storage = storage
        self._environment = environment
        self._bucket = bucket
        self._storage_namespace = storage_namespace
        self._clock = clock or _utc_now
        self._asset_id_factory = asset_id_factory or (
            lambda: _opaque_id("asset")
        )
        self._operation_id_factory = operation_id_factory or (
            lambda: _opaque_id("asset_op")
        )
        self._image_decoder = image_decoder or decode_image_payload
        self._png_encoder = png_encoder or encode_grayscale_png

    def create_from_output(
        self,
        actor_context: ActorContext,
        *,
        task_id: str,
        tool_run_id: str,
        output: ToolExecutionOutput,
    ) -> list[Asset]:
        if not isinstance(output, ToolExecutionOutput) or not output.images:
            raise ApplicationConflictError(task_id=task_id)
        assets: list[Asset] = []
        for image in output.images:
            pending, tool_version, model_bundle_id = self._create_pending(
                actor_context,
                task_id=task_id,
                tool_run_id=tool_run_id,
                image=image,
                output=output,
            )
            assets.append(
                self._finalize(
                    actor_context,
                    pending,
                    image=image,
                    tool_version=tool_version,
                    model_bundle_id=model_bundle_id,
                )
            )
        return assets

    def _create_pending(
        self,
        actor_context: ActorContext,
        *,
        task_id: str,
        tool_run_id: str,
        image: ToolImagePayload,
        output: ToolExecutionOutput,
    ) -> tuple[Asset, str, str | None]:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned(
                    task_id,
                    actor_context.actor_id,
                )
                tool_run = unit_of_work.tool_runs.get_owned(
                    tool_run_id,
                    actor_context.actor_id,
                )
                if (
                    task is None
                    or tool_run is None
                    or tool_run.task_id != task_id
                ):
                    raise ResourceNotFoundError(task_id=task_id)
                summary = tool_run.output_summary
                image_summaries = summary.get("images") if isinstance(
                    summary,
                    dict,
                ) else None
                if (
                    tool_run.current_status != "RUNNING"
                    or not isinstance(summary, dict)
                    or summary != normalize_tool_output_summary(output)
                    or tool_run.model_bundle_id != output.model_bundle_id
                    or not isinstance(image_summaries, list)
                    or summary.get("image_count") != len(image_summaries)
                    or image_summaries.count(_image_summary(image)) != 1
                ):
                    raise ApplicationConflictError(task_id=task_id)
                role = _asset_role(image)
                now = self._clock()
                asset_id = self._asset_id_factory()
                pending = Asset.pending(
                    asset_id=asset_id,
                    task_id=task_id,
                    producer_tool_run_id=tool_run_id,
                    actor_id=actor_context.actor_id,
                    operation_id=self._operation_id_factory(),
                    role=role,
                    object_key=(
                        f"assets/{self._environment}/{asset_id}.png"
                    ),
                    pending_since=now,
                    created_at=now,
                    storage_bucket=self._bucket,
                    storage_namespace=self._storage_namespace,
                )
                unit_of_work.assets.add(pending)
                unit_of_work.commit()
                return pending, tool_run.tool_version, tool_run.model_bundle_id
        except ApplicationError:
            raise
        except Exception as error:
            raise from_persistence_error(error, task_id=task_id) from None

    def _finalize(
        self,
        actor_context: ActorContext,
        asset: Asset,
        *,
        image: ToolImagePayload,
        tool_version: str,
        model_bundle_id: str | None,
    ) -> Asset:
        try:
            decoded = self._image_decoder(image)
            array = getattr(decoded, "array")
            npy_sha256 = getattr(decoded, "npy_sha256")
        except Exception as error:
            if not isinstance(error, InvalidModelOutputError):
                _logger.warning(
                    "asset_model_output_validation_failed",
                    extra={"event": "asset_model_output_validation_failed"},
                )
            self._persist_failed(
                asset,
                code="INVALID_MODEL_OUTPUT",
                message="Tool Runtime returned invalid model output.",
            )
        try:
            encoded = self._png_encoder(array)
        except Exception:
            self._persist_failed(
                asset,
                code="PNG_ENCODING_FAILED",
                message="Asset image encoding failed.",
            )

        custom_metadata = _source_metadata(
            asset,
            tool_version=tool_version,
            model_bundle_id=model_bundle_id,
            source_npy_sha256=npy_sha256,
        )
        expected = StoredObjectMetadata(
            object_key=asset.object_key,
            size_bytes=encoded.size_bytes,
            sha256=encoded.sha256,
            content_type=encoded.media_type,
            metadata=custom_metadata,
        )
        try:
            self._storage.put(
                asset.object_key,
                encoded.payload,
                encoded.media_type,
                custom_metadata,
            )
        except StorageWriteOutcomeUnknownError:
            raise DependencyUnavailableError(task_id=asset.task_id) from None
        except StorageUnavailableError:
            try:
                self._persist_failed(
                    asset,
                    code=DependencyUnavailableError.default_code,
                    message=DependencyUnavailableError.default_message,
                )
            except AssetLifecycleOutcomeError:
                raise DependencyUnavailableError(
                    task_id=asset.task_id
                ) from None
        except StorageIntegrityError:
            self._persist_orphaned(
                asset,
                reason="OBJECT_INTEGRITY_INVALID",
            )
        except StorageError:
            try:
                headed = self._storage.head(asset.object_key)
            except StorageUnavailableError:
                raise DependencyUnavailableError(
                    task_id=asset.task_id
                ) from None
            except StorageIntegrityError:
                self._persist_orphaned(
                    asset,
                    reason="OBJECT_INTEGRITY_INVALID",
                )
            if headed is not None and _metadata_matches(headed, expected):
                return self._commit_available(asset, encoded)
            if headed is None:
                self._persist_failed(
                    asset,
                    code="ASSET_UPLOAD_FAILED",
                    message="Asset storage failed.",
                )
            self._compensate_mismatched_object(asset, headed)

        try:
            headed = self._storage.head(asset.object_key)
        except StorageUnavailableError:
            raise DependencyUnavailableError(task_id=asset.task_id) from None
        except StorageIntegrityError:
            self._persist_orphaned(
                asset,
                reason="OBJECT_INTEGRITY_INVALID",
            )
        if headed is None or not _metadata_matches(headed, expected):
            self._compensate_mismatched_object(asset, headed)
        return self._commit_available(asset, encoded)

    def _compensate_mismatched_object(
        self,
        asset: Asset,
        headed: StoredObjectMetadata | None,
    ) -> None:
        if headed is None:
            self._persist_failed(
                asset,
                code="ASSET_UPLOAD_FAILED",
                message="Asset storage failed.",
            )
        if not _ownership_matches(headed, asset):
            self._persist_orphaned(
                asset,
                reason="OBJECT_OWNERSHIP_UNVERIFIED",
            )
        self._delete_then_fail_or_orphan(asset)

    def _delete_then_fail_or_orphan(self, asset: Asset) -> None:
        try:
            self._storage.delete(asset.object_key)
        except StorageError:
            self._persist_orphaned(
                asset,
                reason="COMPENSATION_DELETE_FAILED",
            )
        self._persist_failed(
            asset,
            code="ASSET_UPLOAD_FAILED",
            message="Asset storage failed.",
        )

    def _persist_failed(
        self,
        asset: Asset,
        *,
        code: str,
        message: str,
    ) -> None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                current = unit_of_work.assets.get(asset.asset_id)
                if current is None:
                    raise ApplicationInternalError(task_id=asset.task_id)
                failed = current.mark_failed(
                    error_code=code,
                    safe_error_message=message,
                    failed_at=self._clock(),
                )
                updated = unit_of_work.assets.update(
                    failed,
                    expected_status=current.current_status,
                )
                if updated is None:
                    raise ApplicationConflictError(task_id=asset.task_id)
                unit_of_work.commit()
        except ApplicationError:
            raise
        except Exception as error:
            raise from_persistence_error(error, task_id=asset.task_id) from None
        status_code = 502 if code == "INVALID_MODEL_OUTPUT" else 500
        raise AssetLifecycleOutcomeError(
            asset_id=asset.asset_id,
            current_status="FAILED",
            code=code,
            message=message,
            status_code=status_code,
            task_id=asset.task_id,
        )

    def list_for_tool_run(
        self,
        actor_context: ActorContext,
        tool_run_id: str,
    ) -> list[Asset]:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                tool_run = unit_of_work.tool_runs.get_owned(
                    tool_run_id,
                    actor_context.actor_id,
                )
                if tool_run is None:
                    raise ResourceNotFoundError()
                assets = unit_of_work.assets.list_for_tool_run(tool_run_id)
        except ApplicationError:
            raise
        except Exception as error:
            raise from_persistence_error(error) from None
        return assets

    def _persist_orphaned(self, asset: Asset, *, reason: str) -> None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                current = unit_of_work.assets.get(asset.asset_id)
                if current is None:
                    raise ApplicationInternalError(task_id=asset.task_id)
                if current.current_status != "ORPHANED":
                    orphaned = current.mark_orphaned(
                        orphan_reason=reason,
                        orphan_details={
                            "operation": "asset_lifecycle",
                            "object_state": "unverified",
                        },
                        orphaned_at=self._clock(),
                    )
                    updated = unit_of_work.assets.update(
                        orphaned,
                        expected_status=current.current_status,
                    )
                    if updated is None:
                        raise ApplicationConflictError(task_id=asset.task_id)
                    unit_of_work.commit()
        except ApplicationError:
            raise
        except Exception as error:
            raise from_persistence_error(error, task_id=asset.task_id) from None
        raise AssetLifecycleOutcomeError(
            asset_id=asset.asset_id,
            current_status="ORPHANED",
            code="ASSET_ORPHANED",
            message="Asset storage outcome requires recovery.",
            status_code=500,
            task_id=asset.task_id,
        )

    def _commit_available(self, asset: Asset, encoded: EncodedPng) -> Asset:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                current = unit_of_work.assets.get(asset.asset_id)
                if current is None:
                    raise ApplicationInternalError(task_id=asset.task_id)
                if current.current_status == "AVAILABLE":
                    if _available_matches(current, encoded):
                        return current
                    raise ApplicationConflictError(task_id=asset.task_id)
                if current.current_status not in {"PENDING", "ORPHANED"}:
                    raise ApplicationConflictError(task_id=asset.task_id)
                available = current.mark_available(
                    sha256=encoded.sha256,
                    size_bytes=encoded.size_bytes,
                    width=encoded.width,
                    height=encoded.height,
                    bit_depth=encoded.bit_depth,
                    media_type=encoded.media_type,
                    encoding_rule=_ENCODING_RULE,
                    available_at=self._clock(),
                )
                updated = unit_of_work.assets.update(
                    available,
                    expected_status=current.current_status,
                )
                if updated is None:
                    observed = unit_of_work.assets.get(asset.asset_id)
                    if observed is not None and _available_matches(
                        observed,
                        encoded,
                    ):
                        return observed
                    raise ApplicationConflictError(task_id=asset.task_id)
                unit_of_work.commit()
                return updated
        except ApplicationError:
            raise
        except Exception as error:
            raise from_persistence_error(error, task_id=asset.task_id) from None

    def get(self, actor_context: ActorContext, asset_id: str) -> Asset:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                asset = unit_of_work.assets.get_owned(
                    asset_id,
                    actor_context.actor_id,
                )
        except Exception as error:
            raise from_persistence_error(error) from None
        if asset is None:
            raise ResourceNotFoundError()
        return asset

    def _get_source_tool_run(
        self,
        actor_context: ActorContext,
        asset: Asset,
    ) -> ToolRun:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                tool_run = unit_of_work.tool_runs.get_owned(
                    asset.producer_tool_run_id,
                    actor_context.actor_id,
                )
        except Exception as error:
            raise from_persistence_error(
                error,
                task_id=asset.task_id,
            ) from None
        if tool_run is None or tool_run.task_id != asset.task_id:
            raise ApplicationConflictError(task_id=asset.task_id)
        return tool_run

    def recover(self, actor_context: ActorContext, asset_id: str) -> Asset:
        asset = self.get(actor_context, asset_id)
        if asset.current_status == "AVAILABLE":
            return asset
        if asset.current_status not in {"PENDING", "ORPHANED"}:
            raise ApplicationConflictError(task_id=asset.task_id)
        tool_run = self._get_source_tool_run(actor_context, asset)
        try:
            expected_source = _expected_source_metadata(asset, tool_run)
        except ValueError:
            self._persist_orphaned(
                asset,
                reason="SOURCE_METADATA_INVALID",
            )
        try:
            headed = self._storage.head(asset.object_key)
            if headed is None:
                self._persist_failed(
                    asset,
                    code="ASSET_UPLOAD_FAILED",
                    message="Asset storage failed.",
                )
        except AssetLifecycleOutcomeError:
            raise
        except StorageUnavailableError:
            raise DependencyUnavailableError(task_id=asset.task_id) from None
        except StorageIntegrityError:
            self._persist_orphaned(
                asset,
                reason="OBJECT_INTEGRITY_INVALID",
            )
        if (
            headed.content_type != "image/png"
            or headed.metadata != expected_source
            or headed.size_bytes > MAX_PNG_BYTES
            or (
                asset.size_bytes is not None
                and (
                    headed.size_bytes != asset.size_bytes
                    or headed.sha256 != asset.sha256
                    or headed.content_type != asset.media_type
                )
            )
        ):
            self._persist_orphaned(
                asset,
                reason="SOURCE_METADATA_MISMATCH",
            )
        try:
            payload = self._storage.get(
                asset.object_key,
                max_bytes=MAX_PNG_BYTES,
            )
        except AssetLifecycleOutcomeError:
            raise
        except StorageUnavailableError:
            raise DependencyUnavailableError(task_id=asset.task_id) from None
        except (StorageIntegrityError, StorageObjectNotFoundError):
            self._persist_orphaned(
                asset,
                reason="CONTENT_INTEGRITY_MISMATCH",
            )
        if (
            len(payload) != headed.size_bytes
            or sha256(payload).hexdigest() != headed.sha256
        ):
            self._persist_orphaned(
                asset,
                reason="CONTENT_INTEGRITY_MISMATCH",
            )
        try:
            with Image.open(BytesIO(payload)) as image:
                image.load()
                if (
                    image.format != "PNG"
                    or image.mode != "L"
                    or image.size != (512, 512)
                ):
                    raise ValueError
        except Exception:
            self._persist_orphaned(
                asset,
                reason="PNG_CONTRACT_MISMATCH",
            )
        encoded = EncodedPng(
            payload=payload,
            sha256=headed.sha256,
            size_bytes=headed.size_bytes,
        )
        if asset.sha256 is not None and not _available_matches(asset, encoded):
            self._persist_orphaned(
                asset,
                reason="CONTENT_INTEGRITY_MISMATCH",
            )
        return self._commit_available(asset, encoded)

    def get_content(
        self,
        actor_context: ActorContext,
        asset_id: str,
    ) -> AssetContent:
        asset = self.get(actor_context, asset_id)
        if asset.asset_type == "ebsd_image":
            from .ebsd_assets import content
            return content(self, asset)
        if asset.current_status != "AVAILABLE":
            raise ApplicationConflictError(task_id=asset.task_id)
        tool_run = self._get_source_tool_run(actor_context, asset)
        try:
            expected_source = _expected_source_metadata(asset, tool_run)
        except ValueError:
            self._mark_content_orphaned(asset, "SOURCE_METADATA_INVALID")
        if (
            asset.media_type != "image/png"
            or asset.size_bytes is None
            or asset.size_bytes > MAX_PNG_BYTES
            or asset.sha256 is None
        ):
            self._mark_content_orphaned(asset, "CONTENT_INTEGRITY_MISMATCH")
        try:
            headed = self._storage.head(asset.object_key)
            if headed is None:
                self._mark_content_orphaned(asset, "OBJECT_MISSING")
        except ApplicationError:
            raise
        except StorageUnavailableError:
            raise DependencyUnavailableError(task_id=asset.task_id) from None
        except StorageIntegrityError:
            self._mark_content_orphaned(asset, "OBJECT_INTEGRITY_INVALID")
        if (
            headed.size_bytes != asset.size_bytes
            or headed.sha256 != asset.sha256
            or headed.content_type != asset.media_type
            or headed.metadata != expected_source
        ):
            self._mark_content_orphaned(asset, "CONTENT_INTEGRITY_MISMATCH")
        try:
            payload = self._storage.get(
                asset.object_key,
                max_bytes=asset.size_bytes,
            )
        except StorageUnavailableError:
            raise DependencyUnavailableError(task_id=asset.task_id) from None
        except StorageObjectNotFoundError:
            self._mark_content_orphaned(asset, "OBJECT_MISSING")
        except StorageIntegrityError:
            self._mark_content_orphaned(asset, "CONTENT_INTEGRITY_MISMATCH")
        if (
            len(payload) != asset.size_bytes
            or sha256(payload).hexdigest() != asset.sha256
        ):
            self._mark_content_orphaned(asset, "CONTENT_INTEGRITY_MISMATCH")
        return AssetContent(
            payload=payload,
            media_type="image/png",
            filename=_safe_download_filename(asset.asset_id),
        )

    def _mark_content_orphaned(self, asset: Asset, reason: str) -> None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                current = unit_of_work.assets.get(asset.asset_id)
                if current is None or current.current_status != "AVAILABLE":
                    raise ApplicationConflictError(task_id=asset.task_id)
                orphaned = current.mark_orphaned(
                    orphan_reason=reason,
                    orphan_details={
                        "operation": "asset_content_read",
                        "object_state": "invalid",
                    },
                    orphaned_at=self._clock(),
                )
                if unit_of_work.assets.update(
                    orphaned,
                    expected_status="AVAILABLE",
                ) is None:
                    raise ApplicationConflictError(task_id=asset.task_id)
                unit_of_work.commit()
        except ApplicationError:
            raise
        except Exception as error:
            raise from_persistence_error(error, task_id=asset.task_id) from None
        raise ApplicationConflictError(task_id=asset.task_id)


def _asset_role(image: ToolImagePayload) -> str:
    if image.image_role == "generated_sem" and image.requested_output is True:
        return "requested_output"
    if image.image_role == "intermediate_sem" and image.requested_output is False:
        return "intermediate"
    raise ApplicationConflictError()


def _image_summary(image: ToolImagePayload) -> dict[str, object]:
    return {
        "image_role": image.image_role,
        "requested_output": image.requested_output,
        "sha256": image.sha256,
        "encoding": image.encoding,
        "shape": list(image.shape),
    }


def _source_metadata(
    asset: Asset,
    *,
    tool_version: str,
    model_bundle_id: str | None,
    source_npy_sha256: str,
) -> dict[str, str]:
    if (
        not isinstance(tool_version, str)
        or not tool_version.strip()
        or not isinstance(source_npy_sha256, str)
        or _SHA256_PATTERN.fullmatch(source_npy_sha256) is None
    ):
        raise ValueError("Asset source metadata is invalid.")
    metadata = {
        "asset-id": asset.asset_id,
        "operation-id": asset.operation_id,
        "producer-tool-run-id": asset.producer_tool_run_id,
        "tool-version": tool_version,
        "source-npy-sha256": source_npy_sha256,
    }
    if model_bundle_id is not None:
        if not isinstance(model_bundle_id, str) or not model_bundle_id.strip():
            raise ValueError("Asset source metadata is invalid.")
        metadata["model-bundle-id"] = model_bundle_id
    return metadata


def _expected_source_metadata(asset: Asset, tool_run: ToolRun) -> dict[str, str]:
    summary = tool_run.output_summary
    images = summary.get("images") if isinstance(summary, dict) else None
    if (
        not isinstance(images, list)
        or summary.get("image_count") != len(images)
    ):
        raise ValueError("ToolRun image summary is invalid.")
    expected_role = {
        "requested_output": ("generated_sem", True),
        "intermediate": ("intermediate_sem", False),
    }.get(asset.role)
    if expected_role is None:
        raise ValueError("Asset role has no recoverable image source.")
    candidates = [
        item
        for item in images
        if isinstance(item, dict)
        and item.get("image_role") == expected_role[0]
        and item.get("requested_output") is expected_role[1]
    ]
    if len(candidates) != 1:
        raise ValueError("ToolRun image source is ambiguous.")
    image = candidates[0]
    if (
        set(image) != {
            "image_role",
            "requested_output",
            "sha256",
            "encoding",
            "shape",
        }
        or not isinstance(image.get("sha256"), str)
        or _SHA256_PATTERN.fullmatch(image["sha256"]) is None
        or not isinstance(image.get("encoding"), str)
        or not image["encoding"]
        or image.get("shape") != [512, 512]
    ):
        raise ValueError("ToolRun image source is invalid.")
    return _source_metadata(
        asset,
        tool_version=tool_run.tool_version,
        model_bundle_id=tool_run.model_bundle_id,
        source_npy_sha256=image["sha256"],
    )


def _ownership_matches(
    observed: StoredObjectMetadata,
    asset: Asset,
) -> bool:
    return (
        observed.metadata.get("asset-id") == asset.asset_id
        and observed.metadata.get("operation-id") == asset.operation_id
        and observed.metadata.get("producer-tool-run-id")
        == asset.producer_tool_run_id
    )


def _metadata_matches(
    observed: StoredObjectMetadata,
    expected: StoredObjectMetadata,
) -> bool:
    return (
        observed.object_key == expected.object_key
        and observed.size_bytes == expected.size_bytes
        and observed.sha256 == expected.sha256
        and observed.content_type == expected.content_type
        and observed.metadata == expected.metadata
    )


def _available_matches(asset: Asset, encoded: EncodedPng) -> bool:
    return (
        asset.current_status in {"AVAILABLE", "ORPHANED"}
        and asset.sha256 == encoded.sha256
        and asset.size_bytes == encoded.size_bytes
        and asset.width == encoded.width
        and asset.height == encoded.height
        and asset.bit_depth == encoded.bit_depth
        and asset.media_type == encoded.media_type
        and asset.encoding_rule == _ENCODING_RULE
    )


def _safe_download_filename(asset_id: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]{1,96}", asset_id) is not None:
        return f"{asset_id}.png"
    digest = sha256(asset_id.encode("utf-8")).hexdigest()[:24]
    return f"asset-{digest}.png"
