"""Lifecycle operations limited to user-uploaded EBSD images."""
from contextlib import contextmanager
from dataclasses import replace
from threading import Lock
from hashlib import sha256
from io import BytesIO

from PIL import Image

from materialsagent.application.asset_service import AssetContent
from materialsagent.application.errors import ApplicationConflictError, ApplicationValidationError, DependencyUnavailableError, ResourceNotFoundError
from materialsagent.domain.models.asset import Asset, METADATA_V1
from materialsagent.domain.ports.storage import StorageError

MAX_IMAGE_BYTES = 10 * 1024 * 1024

# The local backend is one process. Register before taking the conversation row
# lock; deletion checks under that same row lock. No storage call holds a DB lock.
_upload_guard = Lock()
_active_uploads = {}


def upload_in_progress(conversation_id):
    with _upload_guard:
        return _active_uploads.get(conversation_id, 0) > 0


@contextmanager
def _upload_activity(conversation_id):
    with _upload_guard:
        _active_uploads[conversation_id] = _active_uploads.get(conversation_id, 0) + 1
    try:
        yield
    finally:
        with _upload_guard:
            remaining = _active_uploads[conversation_id] - 1
            if remaining:
                _active_uploads[conversation_id] = remaining
            else:
                del _active_uploads[conversation_id]



def inspect_image(payload):
    try:
        if not payload or len(payload) > MAX_IMAGE_BYTES:
            raise ValueError()
        with Image.open(BytesIO(payload)) as image:
            if (image.format not in {"PNG", "JPEG"} or image.mode != "RGB" or getattr(image, "n_frames", 1) != 1
                    or image.width != image.height or not 128 <= image.width <= 4096):
                raise ValueError()
            image.load()
            return ("image/png" if image.format == "PNG" else "image/jpeg", image.width)
    except Exception:
        raise ApplicationValidationError("请上传不超过 10 MiB、边长 128–4096 像素的正方形 RGB PNG/JPEG 图片。") from None


def identity(asset):
    return {"asset-id": asset.asset_id, "operation-id": asset.operation_id}


def upload(service, actor, conversation_id, payload, key):
    with _upload_activity(conversation_id):
        return _upload(service, actor, conversation_id, payload, key)


def _upload(service, actor, conversation_id, payload, key):
    media_type, width = inspect_image(payload)
    digest = sha256(payload).hexdigest()
    # A stable ID makes network replay safe without storing bytes in idempotency records.
    operation = sha256((actor.actor_id + "\0" + conversation_id + "\0" + key).encode()).hexdigest()
    asset_id = "asset_" + operation[:40]
    timestamp = service._clock()
    with service._unit_of_work_factory() as uow:
        conversation = uow.conversations.get_owned_for_update(conversation_id, actor.actor_id)
        if conversation is None:
            raise ResourceNotFoundError()
        existing = uow.assets.get(asset_id)
        if existing is not None:
            if existing.operation_id != "ebsd_" + operation + "_" + digest:
                raise ApplicationConflictError()
            if existing.current_status == "AVAILABLE" and existing.sha256 == digest:
                return existing
            if existing.current_status != "PENDING":
                raise ApplicationConflictError()
            pending = existing
        else:
            pending = Asset(asset_id=asset_id, task_id=None, producer_tool_run_id=None,
                conversation_id=conversation_id, actor_id=actor.actor_id, operation_id="ebsd_" + operation + "_" + digest,
                current_status="PENDING", asset_type="ebsd_image", source_type="UPLOADED", role="supporting",
                object_key=f"assets/{service._environment}/{asset_id}.{'png' if media_type == 'image/png' else 'jpg'}",
                media_type=None, width=None, height=None, bit_depth=None, sha256=None, size_bytes=None,
                encoding_rule=None, pending_since=timestamp, created_at=timestamp, available_at=None,
                failed_at=None, orphaned_at=None, error_code=None, safe_error_message=None,
                orphan_reason=None, orphan_details=None, storage_identity_version=METADATA_V1,
                storage_bucket=service._bucket, storage_namespace=service._storage_namespace)
            uow.assets.add(pending)
            uow.commit()
    try:
        observed = service._storage.put(pending.object_key, payload, media_type, identity(pending))
        if (observed.sha256 != digest or observed.size_bytes != len(payload)
                or observed.content_type != media_type or observed.metadata != identity(pending)):
            raise ApplicationConflictError()
    except StorageError:
        raise DependencyUnavailableError() from None
    with service._unit_of_work_factory() as uow:
        current = uow.assets.get_for_update(asset_id)
        if current is None:
            raise ResourceNotFoundError()
        if current.current_status == "AVAILABLE":
            if current.sha256 != digest:
                raise ApplicationConflictError()
            return current
        if current.current_status != "PENDING":
            raise ApplicationConflictError()
        available = replace(current, current_status="AVAILABLE", media_type=media_type,
            width=width, height=width, bit_depth=8, sha256=digest, size_bytes=len(payload),
            encoding_rule="ebsd-rgb-original-v1", available_at=service._clock())
        if uow.assets.update(available, expected_status="PENDING") is None:
            raise ApplicationConflictError()
        uow.commit()
        return available


def require_asset(service, actor, conversation_id, asset_id):
    asset = service.get(actor, asset_id)
    if (asset.asset_type != "ebsd_image" or asset.source_type != "UPLOADED"
            or asset.conversation_id != conversation_id or asset.current_status != "AVAILABLE"):
        raise ResourceNotFoundError()
    return asset


def content(service, asset):
    if asset.current_status != "AVAILABLE":
        raise ApplicationConflictError()
    try:
        metadata = service._storage.head(asset.object_key)
        if metadata is None or metadata.metadata != identity(asset) or metadata.sha256 != asset.sha256:
            raise ApplicationConflictError()
        payload = service._storage.get(asset.object_key, max_bytes=MAX_IMAGE_BYTES)
    except StorageError:
        raise DependencyUnavailableError() from None
    if len(payload) != asset.size_bytes or sha256(payload).hexdigest() != asset.sha256:
        raise ApplicationConflictError()
    media_type, width = inspect_image(payload)
    if media_type != asset.media_type or width != asset.width:
        raise ApplicationConflictError()
    return AssetContent(payload, media_type, asset.asset_id + (".png" if media_type == "image/png" else ".jpg"))
