from __future__ import annotations

import base64
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO

import numpy as np
from sqlalchemy import func, select, text

from materialsagent.domain.models.asset import Asset
from materialsagent.domain.ports.storage import (
    StorageConflictError,
    StorageObjectNotFoundError,
    StoredObjectMetadata,
    StorageUnavailableError,
)
from materialsagent.domain.ports.tool_execution import (
    ToolExecutionOutput,
    ToolImagePayload,
)
from materialsagent.infrastructure.db.asset import AssetRow
from materialsagent.infrastructure.db.conversation_cleanup import (
    ConversationObjectCleanupRow,
)
from materialsagent.infrastructure.db.conversation_task import (
    ConversationRow,
    TaskInputRevisionRow,
    TaskRow,
)
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.tool_run import ToolRunRow
from materialsagent.infrastructure.llm.mock import MockChatOrchestrationAdapter


BASE = datetime(2026, 7, 22, 1, 0, tzinfo=timezone.utc)


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


class _ToolClient:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _metadata, validated_input, _context):
        self.calls += 1
        return ToolExecutionOutput(
            status="SUCCEEDED",
            requested_outputs=tuple(validated_input.requested_outputs),
            completed_outputs=tuple(validated_input.requested_outputs),
            failed_outputs=(),
            data={"mechanical_properties": {"yield_strength_mpa": 1000.0}},
            images=(_valid_image(),),
            warnings=(),
            diagnostics=(),
            actual_runtime_parameters=dict(validated_input.runtime_parameters),
            model_bundle_id="mock-zta35g-v1",
            error=None,
        )

    def readiness(self, _metadata):
        return "AVAILABLE"


class _PartialToolClient(_ToolClient):
    def execute(self, _metadata, validated_input, _context):
        self.calls += 1
        return ToolExecutionOutput(
            status="PARTIALLY_SUCCEEDED",
            requested_outputs=("sem_image", "mechanical_properties"),
            completed_outputs=("sem_image",),
            failed_outputs=("mechanical_properties",),
            data={},
            images=(_valid_image(),),
            warnings=(),
            diagnostics=(),
            actual_runtime_parameters=dict(validated_input.runtime_parameters),
            model_bundle_id="mock-zta35g-v1",
            error={
                "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
                "safe_message": "Runtime-controlled partial failure text.",
                "retryable": False,
                "failed_step": "runtime_predictor",
                "details": {"traceback": "runtime-controlled traceback"},
            },
        )


def _settings(api_harness):
    return api_harness.settings.model_copy(
        update={"m5_dev_routes_enabled": True, "app_env": "test"}
    )


def _create_valid_revision(client, api_harness) -> tuple[str, str]:
    conversation = client.post(
        "/api/v1/conversations",
        headers={"Idempotency-Key": "asset-conversation-create"},
        json={},
    )
    assert conversation.status_code == 201
    message = client.post(
        f"/api/v1/conversations/{conversation.json()['data']['conversation_id']}/messages",
        headers={"Idempotency-Key": "asset-valid-revision"},
        json={
            "submission_mode": "NEW_TASK",
            "content_text": "完整合法 Tool 请求",
        },
    )
    assert message.status_code == 200, message.text
    task_id = message.json()["data"]["task"]["task_id"]
    with create_session_factory(api_harness.engine)() as session:
        revision = session.scalar(
            select(TaskInputRevisionRow).where(
                TaskInputRevisionRow.task_id == task_id
            )
        )
        assert revision is not None
        return task_id, revision.task_input_revision_id


def _execute_asset(client, api_harness) -> tuple[str, str, dict[str, object]]:
    task_id, revision_id = _create_valid_revision(client, api_harness)
    response = client.post(
        f"/api/v1/dev/tasks/{task_id}/tool-runs",
        json={"task_input_revision_id": revision_id},
    )
    assert response.status_code == 201, response.text
    data = response.json()["data"]
    assert len(data["asset_ids"]) == 1
    return task_id, data["asset_ids"][0], data


def test_asset_owner_metadata_inline_attachment_and_other_actor_404(
    api_harness,
) -> None:
    from materialsagent.application.tools import build_tool_registry

    owner_id = "actor_asset_owner"
    other_id = "actor_asset_other"
    api_harness.persist_actor(owner_id)
    api_harness.persist_actor(other_id)
    storage = _MemoryStorage()
    tool_client = _ToolClient()
    registry = build_tool_registry(tool_client)

    with api_harness.create_client(
        owner_id,
        settings=_settings(api_harness),
        tool_registry=registry,
        storage_service=storage,
    ) as owner:
        _task_id, asset_id, run_data = _execute_asset(owner, api_harness)
        metadata = owner.get(f"/api/v1/assets/{asset_id}")
        inline = owner.get(f"/api/v1/assets/{asset_id}/content")
        attachment = owner.get(
            f"/api/v1/assets/{asset_id}/content?disposition=attachment"
        )

    with api_harness.create_client(
        other_id,
        settings=_settings(api_harness),
        tool_registry=registry,
        storage_service=storage,
    ) as other:
        hidden = other.get(f"/api/v1/assets/{asset_id}")

    assert tool_client.calls == 1
    assert run_data["status"] == "RUNNING"
    assert metadata.status_code == 200
    body = metadata.json()["data"]
    assert body["status"] == "AVAILABLE"
    assert (body["width"], body["height"], body["bit_depth"]) == (512, 512, 8)
    assert "object_key" not in metadata.text
    assert "bucket" not in metadata.text.lower()
    assert inline.status_code == attachment.status_code == 200
    assert inline.headers["content-type"] == "image/png"
    assert int(inline.headers["content-length"]) == len(inline.content)
    assert inline.headers["content-disposition"].startswith("inline;")
    assert attachment.headers["content-disposition"].startswith("attachment;")
    assert inline.content == attachment.content
    assert hidden.status_code == 404
    with create_session_factory(api_harness.engine)() as session:
        assert session.scalar(select(func.count()).select_from(AssetRow)) == 1
        assert session.scalar(select(func.count()).select_from(ToolRunRow)) == 1


def test_conversation_delete_snapshots_real_asset_and_cleans_storage_after_commit(
    api_harness,
) -> None:
    from materialsagent.application.tools import build_tool_registry

    actor_id = "actor_asset_delete"
    api_harness.persist_actor(actor_id)
    storage = _MemoryStorage()
    registry = build_tool_registry(_ToolClient())
    settings = _settings(api_harness)

    def responder(_input):
        return {
            "route": "TOOL_CANDIDATES",
            "candidates": [{
                "tool_id": "zta35g_sem_virtual_lab",
                "candidate_input_delta": {
                    "material": "ZTA35G",
                    "solution_temperature": {"value": 1000, "unit": "°C"},
                    "solution_time": {"value": 3, "unit": "h"},
                    "aging_temperature": {"value": 730, "unit": "°C"},
                    "aging_time": {"value": 3, "unit": "h"},
                    "requested_outputs": ["sem_image", "mechanical_properties"],
                },
            }],
        }

    with api_harness.create_client(
        actor_id,
        settings=settings,
        tool_registry=registry,
        storage_service=storage,
        chat_orchestration_port=MockChatOrchestrationAdapter(responder),
    ) as client:
        task_id, asset_id, run_data = _execute_asset(client, api_harness)
        object_key = next(iter(storage.objects))
        with create_session_factory(api_harness.engine)() as session:
            task_row = session.get(TaskRow, task_id)
            run_row = session.get(ToolRunRow, run_data["tool_run_id"])
            assert task_row is not None and run_row is not None
            conversation_id = task_row.conversation_id
            completed_at = BASE.replace(hour=2)
            run_row.current_status = "FAILED"
            run_row.completed_at = completed_at
            run_row.duration_ms = int(
                (completed_at - run_row.started_at).total_seconds() * 1000
            )
            run_row.completed_outputs = []
            run_row.failed_outputs = list(run_row.requested_outputs)
            run_row.error_code = "CONTROLLED_TEST_STOP"
            run_row.safe_error_message = "Controlled test terminal state."
            task_row.current_status = "FAILED"
            task_row.updated_at = completed_at
            task_row.completed_at = completed_at
            task_row.error_code = "CONTROLLED_TEST_STOP"
            task_row.safe_error_message = "Controlled test terminal state."
            session.commit()

        response = client.delete(
            f"/api/v1/conversations/{conversation_id}"
        )

    assert response.status_code == 200, response.text
    assert object_key not in storage.objects
    with create_session_factory(api_harness.engine)() as session:
        cleanup_row = session.scalar(
            select(ConversationObjectCleanupRow).where(
                ConversationObjectCleanupRow.asset_id == asset_id
            )
        )
        assert cleanup_row is not None
        assert cleanup_row.status == "COMPLETED"
        assert cleanup_row.object_key == object_key
        assert cleanup_row.bucket == settings.minio_bucket
        assert cleanup_row.storage_namespace.endswith(
            str(settings.minio_endpoint)
        )
        assert session.get(TaskRow, task_id) is None
        assert session.get(AssetRow, asset_id) is None


def test_cleanup_snapshot_failure_rolls_back_conversation_delete(
    api_harness,
) -> None:
    from materialsagent.application.tools import build_tool_registry

    actor_id = "actor_asset_delete_atomicity"
    api_harness.persist_actor(actor_id)
    storage = _MemoryStorage()
    settings = _settings(api_harness)
    registry = build_tool_registry(_ToolClient())

    with api_harness.create_client(
        actor_id,
        settings=settings,
        tool_registry=registry,
        storage_service=storage,
    ) as client:
        task_id, asset_id, run_data = _execute_asset(client, api_harness)
        object_key = next(iter(storage.objects))
        with create_session_factory(api_harness.engine)() as session:
            task_row = session.get(TaskRow, task_id)
            run_row = session.get(ToolRunRow, run_data["tool_run_id"])
            asset_row = session.get(AssetRow, asset_id)
            assert task_row is not None and run_row is not None and asset_row is not None
            conversation_id = task_row.conversation_id
            completed_at = BASE.replace(hour=2)
            run_row.current_status = "FAILED"
            run_row.completed_at = completed_at
            run_row.duration_ms = int(
                (completed_at - run_row.started_at).total_seconds() * 1000
            )
            run_row.completed_outputs = []
            run_row.failed_outputs = list(run_row.requested_outputs)
            run_row.error_code = "CONTROLLED_TEST_STOP"
            run_row.safe_error_message = "Controlled test terminal state."
            task_row.current_status = "FAILED"
            task_row.updated_at = completed_at
            task_row.completed_at = completed_at
            task_row.error_code = "CONTROLLED_TEST_STOP"
            task_row.safe_error_message = "Controlled test terminal state."
            session.add(
                ConversationObjectCleanupRow(
                    cleanup_id="cleanup_existing_asset_snapshot",
                    actor_id=actor_id,
                    conversation_id=conversation_id,
                    asset_id=asset_id,
                    operation_id=asset_row.operation_id,
                    producer_tool_run_id=asset_row.producer_tool_run_id,
                    object_key=asset_row.object_key,
                    bucket=asset_row.storage_bucket,
                    storage_namespace=asset_row.storage_namespace,
                    identity_version=asset_row.storage_identity_version,
                    status="PENDING",
                    attempts=0,
                    created_at=completed_at,
                    last_attempt_at=None,
                    completed_at=None,
                    safety_error_code=None,
                )
            )
            session.commit()

        response = client.delete(
            f"/api/v1/conversations/{conversation_id}"
        )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESOURCE_CONFLICT"
    assert object_key in storage.objects
    with create_session_factory(api_harness.engine)() as session:
        assert session.get(ConversationRow, conversation_id) is not None
        assert session.get(TaskRow, task_id) is not None
        assert session.get(AssetRow, asset_id) is not None


def test_partial_success_persists_safe_summary_and_available_asset(
    api_harness,
) -> None:
    from materialsagent.application.tools import build_tool_registry

    actor_id = "actor_asset_partial"
    api_harness.persist_actor(actor_id)
    storage = _MemoryStorage()
    tool_client = _PartialToolClient()
    registry = build_tool_registry(tool_client)

    with api_harness.create_client(
        actor_id,
        settings=_settings(api_harness),
        tool_registry=registry,
        storage_service=storage,
    ) as client:
        task_id, asset_id, run_data = _execute_asset(client, api_harness)
        metadata = client.get(f"/api/v1/assets/{asset_id}")
        content = client.get(f"/api/v1/assets/{asset_id}/content")
        task = client.get(f"/api/v1/tasks/{task_id}")

    expected_error = {
        "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
        "safe_message": "Mechanical property prediction failed.",
        "retryable": False,
    }
    assert tool_client.calls == 1
    assert run_data["status"] == "RUNNING"
    assert run_data["output_summary"]["runtime_status"] == (
        "PARTIALLY_SUCCEEDED"
    )
    assert run_data["output_summary"]["error"] == expected_error
    assert metadata.status_code == 200
    assert metadata.json()["data"]["status"] == "AVAILABLE"
    assert content.status_code == 200
    assert content.headers["content-type"] == "image/png"
    assert task.status_code == 200
    task_data = task.json()["data"]
    assert task_data["status"] == "RUNNING"
    assert task_data["error_code"] is None
    assert task_data["selected_tool_run_id"] is None
    assert task_data["selected_result_id"] is None

    with create_session_factory(api_harness.engine)() as session:
        stored_run = session.get(ToolRunRow, run_data["tool_run_id"])
        stored_task = session.get(TaskRow, task_id)
        stored_asset = session.get(AssetRow, asset_id)
        assert stored_run is not None
        assert stored_run.current_status == "RUNNING"
        assert stored_run.output_summary["error"] == expected_error
        persisted_summary = json.dumps(
            stored_run.output_summary,
            ensure_ascii=False,
            sort_keys=True,
        )
        assert "Runtime-controlled partial failure text." not in persisted_summary
        assert "runtime_predictor" not in persisted_summary
        assert "runtime-controlled traceback" not in persisted_summary
        assert stored_task is not None
        assert stored_task.current_status == "RUNNING"
        assert stored_task.error_code is None
        assert stored_task.selected_tool_run_id is None
        assert stored_task.selected_result_id is None
        assert stored_asset is not None
        assert stored_asset.current_status == "AVAILABLE"

    with api_harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM asset")) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM tool_result")
        ) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM natural_language_explanation")
        ) == 0


def test_pending_failed_and_orphaned_content_are_conflicts(api_harness) -> None:
    from materialsagent.application.tools import build_tool_registry

    actor_id = "actor_asset_status"
    api_harness.persist_actor(actor_id)
    storage = _MemoryStorage()
    registry = build_tool_registry(_ToolClient())
    with api_harness.create_client(
        actor_id,
        settings=_settings(api_harness),
        tool_registry=registry,
        storage_service=storage,
    ) as client:
        task_id, available_id, run_data = _execute_asset(client, api_harness)
        tool_run_id = run_data["tool_run_id"]
        with api_harness.unit_of_work_factory() as unit_of_work:
            pending = Asset.pending(
                asset_id="asset_pending_api",
                task_id=task_id,
                producer_tool_run_id=tool_run_id,
                actor_id=actor_id,
                operation_id="operation_pending_api",
                role="intermediate",
                object_key="assets/test/asset_pending_api.png",
                pending_since=BASE,
                created_at=BASE,
            )
            failed = Asset.pending(
                asset_id="asset_failed_api",
                task_id=task_id,
                producer_tool_run_id=tool_run_id,
                actor_id=actor_id,
                operation_id="operation_failed_api",
                role="intermediate",
                object_key="assets/test/asset_failed_api.png",
                pending_since=BASE,
                created_at=BASE,
            ).mark_failed(
                error_code="INVALID_MODEL_OUTPUT",
                safe_error_message="Tool Runtime returned invalid model output.",
                failed_at=BASE + timedelta(seconds=1),
            )
            available = unit_of_work.assets.get(available_id)
            assert available is not None
            orphaned = available.mark_orphaned(
                orphan_reason="CONTROLLED_TEST",
                orphan_details={"operation": "test"},
                orphaned_at=BASE + timedelta(seconds=2),
            )
            unit_of_work.assets.add(pending)
            unit_of_work.assets.add(failed)
            assert unit_of_work.assets.update(
                orphaned,
                expected_status="AVAILABLE",
            ) is not None
            unit_of_work.commit()

        responses = [
            client.get(f"/api/v1/assets/{asset_id}/content")
            for asset_id in (
                "asset_pending_api",
                "asset_failed_api",
                available_id,
            )
        ]

    assert [response.status_code for response in responses] == [409, 409, 409]


def test_missing_object_becomes_orphaned_and_never_returns_fake_png(
    api_harness,
) -> None:
    from materialsagent.application.tools import build_tool_registry

    actor_id = "actor_asset_missing"
    api_harness.persist_actor(actor_id)
    storage = _MemoryStorage()
    with api_harness.create_client(
        actor_id,
        settings=_settings(api_harness),
        tool_registry=build_tool_registry(_ToolClient()),
        storage_service=storage,
    ) as client:
        _task_id, asset_id, _run = _execute_asset(client, api_harness)
        with create_session_factory(api_harness.engine)() as session:
            row = session.get(AssetRow, asset_id)
            assert row is not None
            storage.delete(row.object_key)
        response = client.get(f"/api/v1/assets/{asset_id}/content")

    assert response.status_code == 409
    assert response.headers.get("content-type") != "image/png"
    with create_session_factory(api_harness.engine)() as session:
        row = session.get(AssetRow, asset_id)
        assert row is not None
        assert row.current_status == "ORPHANED"


def test_storage_unavailable_and_invalid_disposition_are_safe(api_harness) -> None:
    from materialsagent.application.tools import build_tool_registry

    actor_id = "actor_asset_unavailable"
    api_harness.persist_actor(actor_id)
    storage = _MemoryStorage()
    with api_harness.create_client(
        actor_id,
        settings=_settings(api_harness),
        tool_registry=build_tool_registry(_ToolClient()),
        storage_service=storage,
    ) as client:
        _task_id, asset_id, _run = _execute_asset(client, api_harness)
        invalid = client.get(
            f"/api/v1/assets/{asset_id}/content?disposition=unsafe"
        )
        storage.unavailable = True
        unavailable = client.get(f"/api/v1/assets/{asset_id}/content")

    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_FAILED"
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert "MinIO" not in unavailable.text
    assert "assets/test" not in unavailable.text
    with create_session_factory(api_harness.engine)() as session:
        row = session.get(AssetRow, asset_id)
        assert row is not None
        assert row.current_status == "AVAILABLE"


def test_available_head_size_mismatch_orphans_without_get(api_harness) -> None:
    from materialsagent.application.tools import build_tool_registry

    actor_id = "actor_asset_head_mismatch"
    api_harness.persist_actor(actor_id)
    storage = _MemoryStorage()
    with api_harness.create_client(
        actor_id,
        settings=_settings(api_harness),
        tool_registry=build_tool_registry(_ToolClient()),
        storage_service=storage,
    ) as client:
        _task_id, asset_id, _run = _execute_asset(client, api_harness)
        with create_session_factory(api_harness.engine)() as session:
            row = session.get(AssetRow, asset_id)
            assert row is not None
            payload, headed = storage.objects[row.object_key]
            storage.objects[row.object_key] = (
                payload,
                replace(headed, size_bytes=headed.size_bytes + 1),
            )
        response = client.get(f"/api/v1/assets/{asset_id}/content")

    assert response.status_code == 409
    assert storage.get_calls == 0
    with create_session_factory(api_harness.engine)() as session:
        row = session.get(AssetRow, asset_id)
        assert row is not None
        assert row.current_status == "ORPHANED"


def test_available_storage_integrity_error_orphans_instead_of_503(
    api_harness,
) -> None:
    from materialsagent.application.tools import build_tool_registry
    from materialsagent.domain.ports.storage import StorageIntegrityError

    actor_id = "actor_asset_integrity"
    api_harness.persist_actor(actor_id)
    storage = _MemoryStorage()
    with api_harness.create_client(
        actor_id,
        settings=_settings(api_harness),
        tool_registry=build_tool_registry(_ToolClient()),
        storage_service=storage,
    ) as client:
        _task_id, asset_id, _run = _execute_asset(client, api_harness)
        storage.head_error = StorageIntegrityError(
            "Stored object integrity check failed."
        )
        response = client.get(f"/api/v1/assets/{asset_id}/content")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RESOURCE_CONFLICT"
    with create_session_factory(api_harness.engine)() as session:
        row = session.get(AssetRow, asset_id)
        assert row is not None
        assert row.current_status == "ORPHANED"


def test_tool_run_query_reads_asset_ids_without_storage_service(
    api_harness,
) -> None:
    from materialsagent.application.tools import build_tool_registry

    actor_id = "actor_asset_db_query"
    api_harness.persist_actor(actor_id)
    storage = _MemoryStorage()
    registry = build_tool_registry(_ToolClient())
    with api_harness.create_client(
        actor_id,
        settings=_settings(api_harness),
        tool_registry=registry,
        storage_service=storage,
    ) as client:
        _task_id, asset_id, run = _execute_asset(client, api_harness)

    no_storage_settings = _settings(api_harness).model_copy(
        update={
            "minio_endpoint": None,
            "minio_access_key": None,
            "minio_secret_key": None,
            "minio_bucket": None,
            "minio_secure": None,
        }
    )
    with api_harness.create_client(
        actor_id,
        settings=no_storage_settings,
        tool_registry=registry,
    ) as client:
        response = client.get(
            f"/api/v1/dev/tool-runs/{run['tool_run_id']}"
        )

    assert response.status_code == 200
    assert response.json()["data"]["asset_ids"] == [asset_id]
