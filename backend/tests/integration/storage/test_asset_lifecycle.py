from __future__ import annotations

import base64
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO
from itertools import count

import numpy as np
import pytest

from materialsagent.application.asset_service import (
    AssetLifecycleOutcomeError,
    AssetService,
)
from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationConflictError,
    ApplicationInternalError,
    DependencyUnavailableError,
    ResourceNotFoundError,
)
from materialsagent.application.png_encoder import PngEncodingError
from materialsagent.application.tool_execution import (
    normalize_tool_output_summary,
)
from materialsagent.domain.models.asset import Asset
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports import storage as storage_port
from materialsagent.domain.ports.storage import (
    StorageConflictError,
    StoredObjectMetadata,
    StorageUnavailableError,
)
from materialsagent.domain.ports.tool_execution import (
    ToolExecutionOutput,
    ToolImagePayload,
)
from materialsagent.domain.ports.tool_registry import ExecutionPolicy
from materialsagent.domain.ports.unit_of_work import PersistenceError


UTC = timezone.utc
BASE = datetime(2026, 7, 22, 1, 0, tzinfo=UTC)


def _image_payload(value: float = 0.0) -> ToolImagePayload:
    array = np.full((512, 512), value, dtype="<f4")
    buffer = BytesIO()
    np.save(buffer, array, allow_pickle=False)
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


def _output(image: ToolImagePayload | None = None) -> ToolExecutionOutput:
    return ToolExecutionOutput(
        status="SUCCEEDED",
        requested_outputs=("sem_image",),
        completed_outputs=("sem_image",),
        failed_outputs=(),
        data={},
        images=(image or _image_payload(),),
        warnings=(),
        diagnostics=(),
        actual_runtime_parameters={
            "seed": 101,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
        model_bundle_id="mock-zta35g-v1",
        error=None,
    )


def _partial_output(
    image: ToolImagePayload | None = None,
) -> ToolExecutionOutput:
    return ToolExecutionOutput(
        status="PARTIALLY_SUCCEEDED",
        requested_outputs=("sem_image", "mechanical_properties"),
        completed_outputs=("sem_image",),
        failed_outputs=("mechanical_properties",),
        data={},
        images=(image or _image_payload(),),
        warnings=(),
        diagnostics=(),
        actual_runtime_parameters={
            "seed": 101,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
        model_bundle_id="mock-zta35g-v1",
        error={
            "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
            "safe_message": "Runtime-controlled mechanical failure text.",
            "retryable": False,
            "failed_step": "runtime_predictor",
            "details": {"traceback": "runtime-controlled traceback"},
        },
    )


def _tool_run(
    *,
    task_id: str = "task_1",
    output: ToolExecutionOutput | None = None,
) -> ToolRun:
    runtime_output = output or _output()
    pending = ToolRun.pending(
        tool_run_id="tool_run_1",
        task_id=task_id,
        request_id="request_1",
        task_input_revision_id="revision_1",
        attempt_no=1,
        tool_id="zta35g_sem_virtual_lab",
        tool_version="1.0.0",
        schema_hash="f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07",
        normalized_input_snapshot={},
        execution_policy_snapshot=ExecutionPolicy.ANY_TASK,
        input_revision_no=1,
        execution_input={"runtime_parameters": {"seed": 101}},
        requested_outputs=list(runtime_output.requested_outputs),
        created_at=BASE,
    )
    return pending.start(started_at=BASE).record_runtime_output(
        actual_runtime_parameters=dict(runtime_output.actual_runtime_parameters),
        diagnostics=[],
        output_summary=normalize_tool_output_summary(runtime_output),
        model_bundle_id="mock-zta35g-v1",
    )


@dataclass
class _Task:
    task_id: str = "task_1"
    actor_id: str = "actor_1"


@dataclass
class _Store:
    tasks: dict[str, _Task]
    tool_runs: dict[str, ToolRun]
    assets: dict[str, Asset]
    active_uows: int = 0
    commit_count: int = 0
    fail_commits: set[int] | None = None
    force_update_conflict: bool = False


class _TaskRepository:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def get_owned(self, task_id: str, actor_id: str):
        task = self.store.tasks.get(task_id)
        return task if task is not None and task.actor_id == actor_id else None


class _ToolRunRepository:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def get(self, tool_run_id: str):
        return self.store.tool_runs.get(tool_run_id)

    def get_owned(self, tool_run_id: str, actor_id: str):
        run = self.get(tool_run_id)
        if run is None:
            return None
        task = self.store.tasks.get(run.task_id)
        return run if task is not None and task.actor_id == actor_id else None


class _AssetRepository:
    def __init__(self, store: _Store) -> None:
        self.store = store

    def get(self, asset_id: str):
        return self.store.assets.get(asset_id)

    def get_owned(self, asset_id: str, actor_id: str):
        asset = self.get(asset_id)
        return asset if asset is not None and asset.actor_id == actor_id else None

    def list_for_tool_run(self, tool_run_id: str):
        return [
            asset
            for asset in self.store.assets.values()
            if asset.producer_tool_run_id == tool_run_id
        ]

    def add(self, asset: Asset) -> None:
        if asset.asset_id in self.store.assets:
            raise PersistenceError("Persistence operation failed.")
        self.store.assets[asset.asset_id] = asset

    def update(self, asset: Asset, *, expected_status: str):
        current = self.get(asset.asset_id)
        if (
            self.store.force_update_conflict
            or current is None
            or current.current_status != expected_status
        ):
            return None
        self.store.assets[asset.asset_id] = asset
        return asset


class _Uow:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.tasks = _TaskRepository(store)
        self.tool_runs = _ToolRunRepository(store)
        self.assets = _AssetRepository(store)
        self._snapshot = dict(store.assets)
        self.committed = False

    def __enter__(self):
        self.store.active_uows += 1
        return self

    def __exit__(self, *_args: object) -> None:
        if not self.committed:
            self.store.assets.clear()
            self.store.assets.update(self._snapshot)
        self.store.active_uows -= 1

    def commit(self) -> None:
        self.store.commit_count += 1
        if self.store.commit_count in (self.store.fail_commits or set()):
            raise PersistenceError("Persistence operation failed.")
        self.committed = True

    def rollback(self) -> None:
        pass


class _Storage:
    def __init__(self, store: _Store) -> None:
        self.store = store
        self.objects: dict[str, tuple[bytes, StoredObjectMetadata]] = {}
        self.put_calls = 0
        self.head_calls = 0
        self.get_calls = 0
        self.delete_calls = 0
        self.statuses_at_put: list[list[str]] = []
        self.put_error: Exception | None = None
        self.delete_error: Exception | None = None
        self.head_override: StoredObjectMetadata | None | object = _UNSET

    def put(self, object_key, payload, content_type, metadata=None):
        assert self.store.active_uows == 0
        self.put_calls += 1
        self.statuses_at_put.append(
            sorted(asset.current_status for asset in self.store.assets.values())
        )
        if self.put_error is not None:
            raise self.put_error
        expected = StoredObjectMetadata(
            object_key=object_key,
            size_bytes=len(payload),
            sha256=sha256(payload).hexdigest(),
            content_type=content_type,
            metadata=dict(metadata or {}),
        )
        existing = self.objects.get(object_key)
        if existing is not None and existing[1] != expected:
            raise StorageConflictError("Object storage conflict.")
        self.objects[object_key] = (payload, expected)
        return expected

    def head(self, object_key):
        assert self.store.active_uows == 0
        self.head_calls += 1
        if self.head_override is not _UNSET:
            return self.head_override
        found = self.objects.get(object_key)
        return None if found is None else found[1]

    def get(self, object_key, *, max_bytes):
        assert self.store.active_uows == 0
        self.get_calls += 1
        payload = self.objects[object_key][0]
        if len(payload) > max_bytes:
            from materialsagent.domain.ports.storage import StorageIntegrityError

            raise StorageIntegrityError("Stored object integrity check failed.")
        return payload

    def delete(self, object_key):
        assert self.store.active_uows == 0
        self.delete_calls += 1
        if self.delete_error is not None:
            raise self.delete_error
        self.objects.pop(object_key, None)


_UNSET = object()


def _service(
    *,
    fail_commits: set[int] | None = None,
    encoder=None,
    output: ToolExecutionOutput | None = None,
):
    store = _Store(
        tasks={"task_1": _Task()},
        tool_runs={"tool_run_1": _tool_run(output=output)},
        assets={},
        fail_commits=fail_commits,
    )
    storage = _Storage(store)
    ticks = count()
    service = AssetService(
        lambda: _Uow(store),
        storage,
        environment="test",
        clock=lambda: BASE + timedelta(seconds=1 + next(ticks)),
        asset_id_factory=lambda: "asset_1",
        operation_id_factory=lambda: "operation_1",
        png_encoder=encoder,
    )
    return service, store, storage


def _create(service: AssetService, output: ToolExecutionOutput | None = None):
    return service.create_from_output(
        ActorContext(actor_id="actor_1", user_id=None),
        task_id="task_1",
        tool_run_id="tool_run_1",
        output=output or _output(),
    )


def test_tx1_precedes_external_work_and_success_becomes_available() -> None:
    service, store, storage = _service()
    original_run = store.tool_runs["tool_run_1"]

    assets = _create(service)

    assert len(assets) == 1
    asset = assets[0]
    assert asset.current_status == "AVAILABLE"
    assert asset.object_key == "assets/test/asset_1.png"
    assert (asset.media_type, asset.width, asset.height, asset.bit_depth) == (
        "image/png",
        512,
        512,
        8,
    )
    assert storage.put_calls == 1
    assert storage.head(asset.object_key).metadata == {
        "asset-id": "asset_1",
        "operation-id": "operation_1",
        "producer-tool-run-id": "tool_run_1",
        "model-bundle-id": "mock-zta35g-v1",
        "source-npy-sha256": _image_payload().sha256,
        "tool-version": "1.0.0",
    }
    assert store.commit_count == 2
    assert store.tasks["task_1"] == _Task()
    assert store.tool_runs["tool_run_1"] == original_run


def test_partial_success_uses_normalized_summary_and_creates_available_asset(
) -> None:
    output = _partial_output()
    service, store, storage = _service(output=output)
    original_run = store.tool_runs["tool_run_1"]

    assets = _create(service, output)

    assert [asset.current_status for asset in assets] == ["AVAILABLE"]
    assert storage.statuses_at_put == [["PENDING"]]
    assert store.commit_count == 2
    assert store.tasks["task_1"] == _Task()
    assert store.tool_runs["tool_run_1"] == original_run
    assert original_run.current_status == "RUNNING"
    assert original_run.output_summary["error"] == {
        "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
        "safe_message": "Mechanical property prediction failed.",
        "retryable": False,
    }
    persisted = repr(original_run.output_summary)
    assert "Runtime-controlled mechanical failure text." not in persisted
    assert "runtime_predictor" not in persisted
    assert "runtime-controlled traceback" not in persisted


def test_invalid_model_output_is_persisted_failed_without_put() -> None:
    service, store, storage = _service()
    invalid = replace(_image_payload(), data_base64="not-valid-base64")

    with pytest.raises(AssetLifecycleOutcomeError) as raised:
        _create(service, _output(invalid))

    assert (raised.value.code, raised.value.asset_id) == (
        "INVALID_MODEL_OUTPUT",
        "asset_1",
    )
    assert store.assets["asset_1"].current_status == "FAILED"
    assert store.assets["asset_1"].error_code == "INVALID_MODEL_OUTPUT"
    assert storage.put_calls == 0


def test_png_failure_is_persisted_failed_without_put() -> None:
    def fail_png(_array):
        raise PngEncodingError()

    service, store, storage = _service(encoder=fail_png)

    with pytest.raises(AssetLifecycleOutcomeError) as raised:
        _create(service)

    assert raised.value.code == "PNG_ENCODING_FAILED"
    assert store.assets["asset_1"].error_code == "PNG_ENCODING_FAILED"
    assert storage.put_calls == 0


def test_pre_write_storage_unavailable_persists_failed_then_returns_503() -> None:
    service, store, storage = _service()
    storage.put_error = StorageUnavailableError("Object storage unavailable.")

    with pytest.raises(DependencyUnavailableError) as raised:
        _create(service)

    assert raised.value.status_code == 503
    assert store.assets["asset_1"].current_status == "FAILED"
    assert store.assets["asset_1"].error_code is not None
    assert store.assets["asset_1"].safe_error_message is not None
    assert storage.delete_calls == 0


def test_write_outcome_unknown_returns_503_and_keeps_pending_fact() -> None:
    outcome_unknown_type = getattr(
        storage_port,
        "StorageWriteOutcomeUnknownError",
        None,
    )
    assert outcome_unknown_type is not None
    service, store, storage = _service()
    storage.put_error = outcome_unknown_type("Object storage unavailable.")

    with pytest.raises(DependencyUnavailableError) as raised:
        _create(service)

    assert raised.value.status_code == 503
    assert store.assets["asset_1"].current_status == "PENDING"
    assert store.assets["asset_1"].error_code is None
    assert store.assets["asset_1"].safe_error_message is None
    assert storage.delete_calls == 0


def test_head_mismatch_is_compensated_then_failed() -> None:
    service, store, storage = _service()
    storage.head_override = StoredObjectMetadata(
        object_key="assets/test/asset_1.png",
        size_bytes=99,
        sha256="f" * 64,
        content_type="image/png",
        metadata={
            "asset-id": "asset_1",
            "operation-id": "operation_1",
            "producer-tool-run-id": "tool_run_1",
            "model-bundle-id": "mock-zta35g-v1",
            "source-npy-sha256": _image_payload().sha256,
            "tool-version": "1.0.0",
        },
    )

    with pytest.raises(AssetLifecycleOutcomeError):
        _create(service)

    assert storage.delete_calls == 1
    assert store.assets["asset_1"].error_code == "ASSET_UPLOAD_FAILED"


def test_failed_compensation_persists_orphaned() -> None:
    service, store, storage = _service()
    storage.put_error = StorageConflictError("Object storage conflict.")
    storage.head_override = StoredObjectMetadata(
        object_key="assets/test/asset_1.png",
        size_bytes=12,
        sha256="f" * 64,
        content_type="image/png",
        metadata={
            "asset-id": "asset_1",
            "operation-id": "operation_1",
            "producer-tool-run-id": "tool_run_1",
            "model-bundle-id": "mock-zta35g-v1",
            "source-npy-sha256": _image_payload().sha256,
            "tool-version": "1.0.0",
        },
    )
    storage.delete_error = StorageUnavailableError("Object storage unavailable.")

    with pytest.raises(AssetLifecycleOutcomeError) as raised:
        _create(service)

    assert raised.value.code == "ASSET_ORPHANED"
    assert store.assets["asset_1"].current_status == "ORPHANED"
    assert store.assets["asset_1"].orphan_reason == "COMPENSATION_DELETE_FAILED"


def test_tx2_commit_failure_leaves_pending_and_recovery_reuses_object() -> None:
    service, store, storage = _service(fail_commits={2})

    with pytest.raises(ApplicationInternalError):
        _create(service)

    assert store.assets["asset_1"].current_status == "PENDING"
    assert storage.put_calls == 1
    store.fail_commits = set()

    recovered = service.recover(
        ActorContext(actor_id="actor_1", user_id=None),
        "asset_1",
    )
    replay = service.recover(
        ActorContext(actor_id="actor_1", user_id=None),
        "asset_1",
    )

    assert recovered.current_status == replay.current_status == "AVAILABLE"
    assert recovered.object_key == "assets/test/asset_1.png"
    assert storage.put_calls == 1
    assert storage.get_calls == 1


def test_orphaned_object_can_recover_to_available() -> None:
    service, store, storage = _service()
    available = _create(service)[0]
    store.assets["asset_1"] = available.mark_orphaned(
        orphan_reason="CONTENT_UNVERIFIED",
        orphan_details={"reason": "controlled_test"},
        orphaned_at=BASE + timedelta(seconds=20),
    )

    recovered = service.recover(
        ActorContext(actor_id="actor_1", user_id=None),
        "asset_1",
    )

    assert recovered.current_status == "AVAILABLE"
    assert storage.put_calls == 1


def test_conditional_update_conflict_does_not_claim_success() -> None:
    service, store, _storage = _service()
    store.force_update_conflict = True

    with pytest.raises(ApplicationConflictError):
        _create(service)

    assert store.assets["asset_1"].current_status == "PENDING"


def test_cross_task_actor_or_unrecorded_image_source_is_rejected_before_tx1() -> None:
    service, store, storage = _service()
    store.tool_runs["tool_run_1"] = _tool_run(task_id="other_task")
    with pytest.raises(ResourceNotFoundError):
        _create(service)
    assert store.assets == {}
    assert storage.put_calls == 0

    store.tool_runs["tool_run_1"] = replace(
        _tool_run(),
        output_summary={
            **_tool_run().output_summary,
            "image_count": 0,
            "image_roles": [],
            "images": [],
        },
    )
    with pytest.raises(ApplicationConflictError):
        _create(service)
    assert store.assets == {}

    with pytest.raises(ResourceNotFoundError):
        service.create_from_output(
            ActorContext(actor_id="actor_other", user_id=None),
            task_id="task_1",
            tool_run_id="tool_run_1",
            output=_output(),
        )


def test_pre_write_failure_fact_commit_failure_keeps_pending() -> None:
    service, store, storage = _service(fail_commits={2})
    storage.put_error = StorageUnavailableError("Object storage unavailable.")

    with pytest.raises(ApplicationInternalError):
        _create(service)

    assert store.assets["asset_1"].current_status == "PENDING"


@pytest.mark.parametrize(
    "metadata",
    [
        {
            "operation-id": "operation_1",
            "producer-tool-run-id": "tool_run_1",
        },
        {
            "asset-id": "asset_1",
            "producer-tool-run-id": "tool_run_1",
        },
        {
            "asset-id": "asset_1",
            "operation-id": "external_operation",
            "producer-tool-run-id": "tool_run_1",
        },
        {
            "asset-id": "asset_1",
            "operation-id": "operation_1",
        },
        {
            "asset-id": "asset_1",
            "operation-id": "operation_1",
            "producer-tool-run-id": "external_tool_run",
        },
    ],
    ids=(
        "missing-asset-id",
        "missing-operation-id",
        "foreign-operation-id",
        "missing-producer-tool-run-id",
        "foreign-producer-tool-run-id",
    ),
)
def test_unknown_object_ownership_is_orphaned_without_delete(
    metadata: dict[str, str],
) -> None:
    service, store, storage = _service()
    storage.head_override = StoredObjectMetadata(
        object_key="assets/test/asset_1.png",
        size_bytes=99,
        sha256="f" * 64,
        content_type="image/png",
        metadata=metadata,
    )

    with pytest.raises(AssetLifecycleOutcomeError) as raised:
        _create(service)

    assert raised.value.current_status == "ORPHANED"
    assert store.assets["asset_1"].current_status == "ORPHANED"
    assert storage.delete_calls == 0


def test_pending_recovery_rejects_source_metadata_mismatch_before_get() -> None:
    service, store, storage = _service(fail_commits={2})
    with pytest.raises(ApplicationInternalError):
        _create(service)
    store.fail_commits = set()
    payload, headed = storage.objects["assets/test/asset_1.png"]
    storage.objects["assets/test/asset_1.png"] = (
        payload,
        replace(
            headed,
            metadata={**headed.metadata, "operation-id": "foreign_operation"},
        ),
    )

    with pytest.raises(AssetLifecycleOutcomeError) as raised:
        service.recover(
            ActorContext(actor_id="actor_1", user_id=None),
            "asset_1",
        )

    assert raised.value.current_status == "ORPHANED"
    assert store.assets["asset_1"].current_status == "ORPHANED"
    assert storage.get_calls == 0


@pytest.mark.parametrize(
    "output",
    [
        _output(replace(_image_payload(), sha256="0" * 64)),
        _output(replace(_image_payload(), image_role="intermediate_sem")),
        _output(replace(_image_payload(), requested_output=False)),
        replace(_output(), model_bundle_id="foreign-bundle"),
        _output(replace(_image_payload(), shape=(256, 1024))),
        _output(replace(_image_payload(), encoding="raw-bytes")),
    ],
    ids=(
        "npy-sha",
        "image-role",
        "requested-output",
        "model-bundle",
        "shape",
        "encoding",
    ),
)
def test_runtime_image_must_match_persisted_tool_run_before_tx1(
    output: ToolExecutionOutput,
) -> None:
    service, store, storage = _service()

    with pytest.raises(ApplicationConflictError):
        _create(service, output)

    assert store.assets == {}
    assert store.commit_count == 0
    assert storage.put_calls == 0
