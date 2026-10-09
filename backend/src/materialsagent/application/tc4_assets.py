"""Deterministic PNG assets use the existing storage identity and lifecycle."""
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from PIL import Image
from materialsagent.application.errors import ApplicationConflictError, DependencyUnavailableError
from materialsagent.application.asset_service import AssetContent
from materialsagent.domain.models.asset import Asset, METADATA_V1
from materialsagent.domain.models.tc4 import MODEL_VERSION, PREPROCESSING_VERSION
from materialsagent.domain.ports.storage import StorageError

MAX_PNG_BYTES = 64 * 1024 * 1024


def inspect(payload, role, expected_size):
    if not payload or len(payload) > MAX_PNG_BYTES:
        raise ApplicationConflictError()
    with Image.open(BytesIO(payload)) as image:
        if image.format != "PNG" or image.mode != ("RGB" if role == "overlay" else "L") or image.size != expected_size:
            raise ApplicationConflictError()
        image.load()
        if role == "mask" and any(image.histogram()[1:255]):
            raise ApplicationConflictError()


def metadata(asset):
    return {"asset-id": asset.asset_id, "operation-id": asset.operation_id,
            "producer-tool-run-id": asset.producer_tool_run_id,
            "input-asset-id": asset.input_asset_id, "tool-run-item-id": asset.producer_tool_run_item_id,
            "model-version": asset.model_version, "preprocessing-version": asset.preprocessing_version}


def create(service, actor, context, item, role, payload):
    statistics = item.statistics
    inspect(payload, role, (statistics.width, statistics.height))
    if role == "mask":
        with Image.open(BytesIO(payload)) as image:
            if image.histogram()[255] != statistics.foreground_pixels:
                raise ApplicationConflictError()
    digest = sha256(payload).hexdigest()
    asset_id = "asset_" + sha256((item.item_id + ":" + role).encode()).hexdigest()[:40]
    operation = "tc4_" + item.item_id + "_" + role + "_" + digest
    timestamp = service._clock()
    with service._unit_of_work_factory() as uow:
        parent = uow.tool_runs.get_owned(context.tool_run_id, actor.actor_id)
        if parent is None or parent.task_id != context.task_id or parent.current_status != "RUNNING":
            raise ApplicationConflictError()
        asset = uow.assets.get(asset_id)
        if asset is None:
            asset = Asset(asset_id=asset_id, task_id=context.task_id, producer_tool_run_id=context.tool_run_id,
                actor_id=actor.actor_id, operation_id=operation, current_status="PENDING", asset_type="image",
                source_type="GENERATED", role="requested_output", object_key=f"assets/{service._environment}/{asset_id}.png",
                media_type=None, width=None, height=None, bit_depth=None, sha256=None, size_bytes=None, encoding_rule=None,
                pending_since=timestamp, created_at=timestamp, available_at=None, failed_at=None, orphaned_at=None,
                error_code=None, safe_error_message=None, orphan_reason=None, orphan_details=None,
                storage_identity_version=METADATA_V1, storage_bucket=service._bucket, storage_namespace=service._storage_namespace,
                producer_tool_run_item_id=item.item_id, input_asset_id=item.input_asset_id,
                model_version=item.receipt["model_version"], preprocessing_version=PREPROCESSING_VERSION)
            uow.assets.add(asset)
            uow.commit()
        elif asset.operation_id != operation:
            raise ApplicationConflictError()
    if asset.current_status == "AVAILABLE":
        content(service, asset)
        return asset
    if asset.current_status != "PENDING":
        raise ApplicationConflictError()
    try:
        observed = service._storage.head(asset.object_key)
        # A response lost during put is reconciled before another write.
        if observed is None:
            observed = service._storage.put(asset.object_key, payload, "image/png", metadata(asset))
        if (observed.metadata != metadata(asset) or observed.sha256 != digest
                or observed.content_type != "image/png" or observed.size_bytes != len(payload)):
            raise ApplicationConflictError()
    except StorageError:
        raise DependencyUnavailableError() from None
    available = replace(asset, current_status="AVAILABLE", media_type="image/png", width=statistics.width,
        height=statistics.height, bit_depth=8, sha256=digest, size_bytes=len(payload),
        encoding_rule="tc4-" + role + "-png-v1", available_at=service._clock())
    with service._unit_of_work_factory() as uow:
        current = uow.assets.get_for_update(asset.asset_id)
        if current.current_status == "AVAILABLE" and current.sha256 == digest:
            return current
        if uow.assets.update(available, expected_status="PENDING") is None:
            raise ApplicationConflictError()
        uow.commit()
    return available


def content(service, asset):
    try:
        observed = service._storage.head(asset.object_key)
        if (asset.current_status != "AVAILABLE" or observed is None or observed.metadata != metadata(asset)
                or observed.sha256 != asset.sha256 or observed.size_bytes != asset.size_bytes):
            raise ApplicationConflictError()
        payload = service._storage.get(asset.object_key, max_bytes=asset.size_bytes)
    except StorageError:
        raise DependencyUnavailableError() from None
    if len(payload) != asset.size_bytes or sha256(payload).hexdigest() != asset.sha256:
        raise ApplicationConflictError()
    role = "overlay" if asset.encoding_rule == "tc4-overlay-png-v1" else "mask"
    inspect(payload, role, (asset.width, asset.height))
    return AssetContent(payload, "image/png", role + ".png")
