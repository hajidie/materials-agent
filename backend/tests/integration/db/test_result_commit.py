from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from materialsagent.application.explanation_service import ExplanationService
from materialsagent.application.errors import ApplicationConflictError
from materialsagent.application.context import ActorContext
from materialsagent.application.result_service import (
    ResultPersistenceError,
    ResultService,
)
from materialsagent.application.tool_execution import (
    ToolExecutionReceipt,
    normalize_tool_output_summary,
)
from materialsagent.application.tool_workflow import ToolWorkflowService
from materialsagent.domain.models.actor import Actor
from materialsagent.domain.models.asset import Asset
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.tool_execution import (
    ToolExecutionOutput,
    ToolImagePayload,
)
from materialsagent.domain.ports.unit_of_work import PersistenceError
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
from materialsagent.infrastructure.llm.mock_explanation import (
    MockExplanationAdapter,
)


BASE = datetime(2026, 7, 23, 1, 0, tzinfo=timezone.utc)
ACTOR = ActorContext(actor_id="actor_1", user_id=None)


def _factory(engine: Engine):
    return lambda: SQLAlchemyUnitOfWork(create_session_factory(engine))


class _RevisionOverrideRepository:
    def __init__(self, repository, transform) -> None:
        self._repository = repository
        self._transform = transform
        self.requested_ids: list[str] = []

    def get(self, task_input_revision_id: str):
        self.requested_ids.append(task_input_revision_id)
        revision = self._repository.get(task_input_revision_id)
        return None if revision is None else self._transform(revision)

    def __getattr__(self, name: str):
        return getattr(self._repository, name)


class _RevisionOverrideUnitOfWork:
    def __init__(self, unit_of_work, transform) -> None:
        self._unit_of_work = unit_of_work
        self._transform = transform
        self.revisions = None

    def __enter__(self):
        self._unit_of_work.__enter__()
        self.revisions = _RevisionOverrideRepository(
            self._unit_of_work.task_input_revisions,
            self._transform,
        )
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._unit_of_work.__exit__(
            exc_type,
            exc_value,
            traceback,
        )

    @property
    def task_input_revisions(self):
        return self.revisions

    def __getattr__(self, name: str):
        return getattr(self._unit_of_work, name)


class _RevisionOverrideFactory:
    def __init__(self, factory, transform) -> None:
        self._factory = factory
        self._transform = transform
        self.created: list[_RevisionOverrideUnitOfWork] = []

    def __call__(self):
        unit_of_work = _RevisionOverrideUnitOfWork(
            self._factory(),
            self._transform,
        )
        self.created.append(unit_of_work)
        return unit_of_work


def _image(*, requested_output: bool) -> ToolImagePayload:
    return ToolImagePayload(
        image_role="generated_sem",
        requested_output=requested_output,
        dtype="float32",
        numpy_dtype="<f4",
        shape=(512, 512),
        channel_layout="grayscale",
        value_range=(-1.0, 1.0),
        encoding="npy_base64",
        byte_order="little",
        array_order="C",
        sha256="a" * 64,
        data_base64="not-persisted",
    )


def _output(
    *,
    requested: tuple[str, ...],
    completed: tuple[str, ...],
    failed: tuple[str, ...],
    include_image: bool = True,
) -> ToolExecutionOutput:
    mechanical_completed = "mechanical_properties" in completed
    sem_needed = "sem_image" in completed or "mechanical_properties" in requested
    return ToolExecutionOutput(
        status=(
            "SUCCEEDED"
            if completed == requested and not failed
            else "PARTIALLY_SUCCEEDED"
            if completed
            else "FAILED"
        ),
        requested_outputs=requested,
        completed_outputs=completed,
        failed_outputs=failed,
        data=(
            {
                "yield_strength": {"value": 650.0, "unit": "MPa"},
                "elongation": {"value": 3.2, "unit": "%"},
            }
            if mechanical_completed
            else {}
        ),
        images=(
            (
                _image(
                    requested_output="sem_image" in requested,
                ),
            )
            if sem_needed and include_image
            else ()
        ),
        warnings=(),
        diagnostics=(),
        actual_runtime_parameters={
            "seed": 101,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
        model_bundle_id="internal-bundle-not-copied",
        error=(
            None
            if not failed
            else {
                "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
                "safe_message": "Mechanical property prediction failed.",
                "retryable": False,
            }
        ),
    )


def _seed(
    engine: Engine,
    *,
    requested: tuple[str, ...],
    completed: tuple[str, ...],
    failed: tuple[str, ...],
    asset_status: str = "AVAILABLE",
    asset_actor_id: str = "actor_1",
    asset_task_id: str = "task_1",
    asset_tool_run_id: str = "tool_run_1",
    include_image: bool = True,
    revision_request_id: str = "request_1",
    revision_material: str = "ZTA35G",
    revision_requested_outputs: tuple[str, ...] | None = None,
    execution_requested_outputs: tuple[str, ...] | None = None,
    normalized_unit_overrides: dict[str, str] | None = None,
) -> tuple[ToolExecutionReceipt, list[Asset]]:
    revision_requested_outputs = (
        requested
        if revision_requested_outputs is None
        else revision_requested_outputs
    )
    execution_requested_outputs = (
        requested
        if execution_requested_outputs is None
        else execution_requested_outputs
    )
    normalized_units = {
        "solution_temperature": "°C",
        "solution_time": "h",
        "aging_temperature": "°C",
        "aging_time": "h",
        **(normalized_unit_overrides or {}),
    }
    output = _output(
        requested=requested,
        completed=completed,
        failed=failed,
        include_image=include_image,
    )
    factory = _factory(engine)
    with factory() as unit_of_work:
        unit_of_work.actors.add(Actor.local_anonymous("actor_1", created_at=BASE))
        if asset_actor_id != "actor_1":
            unit_of_work.actors.add(
                Actor.local_anonymous(asset_actor_id, created_at=BASE)
            )
        unit_of_work.conversations.add(
            Conversation(
                conversation_id="conversation_1",
                actor_id="actor_1",
                title=None,
                created_at=BASE,
                updated_at=BASE,
            )
        )
        task = Task(
            task_id="task_1",
            conversation_id="conversation_1",
            actor_id="actor_1",
            task_type="TOOL_EXECUTION",
            current_status="RUNNING",
            selected_tool_run_id=None,
            selected_result_id=None,
            created_at=BASE,
            started_at=BASE,
            updated_at=BASE,
            completed_at=None,
            error_code=None,
            safe_error_message=None,
        )
        unit_of_work.tasks.add(task)
        if asset_task_id != "task_1":
            unit_of_work.tasks.add(
                replace(
                    task,
                    task_id=asset_task_id,
                )
            )
        unit_of_work.llm_calls.add(
            LLMCall(
                llm_call_id="llm_chat_1",
                task_id="task_1",
                conversation_id="conversation_1",
                request_id="request_1",
                purpose="CHAT_ORCHESTRATION",
                input_result_id=None,
                provider="mock",
                model_name="mock-chat",
                prompt_template_id="chat-orchestration",
                prompt_template_version="1",
                prompt_digest="b" * 64,
                generation_parameters={"temperature": 0, "max_tokens": 256},
                structured_output_summary={
                    "route": "TOOL_EXECUTION",
                    "tool_id": "zta35g_sem_virtual_lab",
                },
                usage={"input_tokens": 1, "output_tokens": 1},
                provider_request_id=None,
                status="SUCCEEDED",
                created_at=BASE,
                started_at=BASE,
                completed_at=BASE,
                duration_ms=0,
                error_code=None,
                safe_error_message=None,
            )
        )
        unit_of_work.task_input_revisions.add(
            TaskInputRevision(
                task_input_revision_id="revision_1",
                task_id="task_1",
                request_id=revision_request_id,
                source_llm_call_id="llm_chat_1",
                source_message_ids=["message_1"],
                revision=1,
                raw_input={"material": "ZTA35G"},
                normalized_input={
                    "material": revision_material,
                    "solution_temperature": {
                        "value": 1000,
                        "unit": normalized_units[
                            "solution_temperature"
                        ],
                    },
                    "solution_time": {
                        "value": 3,
                        "unit": normalized_units["solution_time"],
                    },
                    "aging_temperature": {
                        "value": 730,
                        "unit": normalized_units[
                            "aging_temperature"
                        ],
                    },
                    "aging_time": {
                        "value": 3,
                        "unit": normalized_units["aging_time"],
                    },
                    "requested_outputs": list(
                        revision_requested_outputs
                    ),
                },
                missing_fields=[],
                ambiguous_fields=[],
                validation_errors=[],
                created_at=BASE,
            )
        )
        run = ToolRun.pending(
            tool_run_id="tool_run_1",
            task_id="task_1",
            request_id="request_1",
            task_input_revision_id="revision_1",
            attempt_no=1,
            tool_id="zta35g_sem_virtual_lab",
            tool_version="0.1.0",
            schema_version="1.0",
            execution_input={
                "process_parameters": {
                    "solution_temperature": 1000,
                    "solution_time": 3.0,
                    "aging_temperature": 730,
                    "aging_time": 3.0,
                },
                "requested_outputs": list(
                    execution_requested_outputs
                ),
                "runtime_parameters": {
                    "seed": 101,
                    "num_samples": 1,
                    "guide_scale": 2.0,
                    "timesteps": 1000,
                },
            },
            requested_outputs=list(requested),
            created_at=BASE + timedelta(seconds=1),
        ).start(
            started_at=BASE + timedelta(seconds=2)
        ).record_runtime_output(
            actual_runtime_parameters=dict(output.actual_runtime_parameters),
            diagnostics=[],
            output_summary=normalize_tool_output_summary(output),
            model_bundle_id=output.model_bundle_id,
        )
        unit_of_work.tool_runs.add(run)
        if asset_tool_run_id != "tool_run_1":
            unit_of_work.tool_runs.add(
                replace(
                    run,
                    tool_run_id=asset_tool_run_id,
                    attempt_no=2,
                )
            )
        pending = Asset.pending(
            asset_id="asset_1",
            task_id=asset_task_id,
            producer_tool_run_id=asset_tool_run_id,
            actor_id=asset_actor_id,
            operation_id="asset_operation_1",
            role=(
                "requested_output"
                if "sem_image" in requested
                else "intermediate"
            ),
            object_key="assets/test/asset_1.png",
            pending_since=BASE + timedelta(seconds=3),
            created_at=BASE + timedelta(seconds=3),
        )
        asset = (
            pending.mark_available(
                sha256="c" * 64,
                size_bytes=1480,
                width=512,
                height=512,
                bit_depth=8,
                media_type="image/png",
                encoding_rule="linear[-1,1]-half-up-uint8-png-l",
                available_at=BASE + timedelta(seconds=4),
            )
            if asset_status == "AVAILABLE"
            else pending
        )
        if include_image:
            unit_of_work.assets.add(asset)
        unit_of_work.commit()
    return (
        ToolExecutionReceipt(tool_run=run, output=output),
        [asset] if include_image else [],
    )


@pytest.mark.parametrize(
    ("requested", "completed", "failed", "expected_status", "task_status"),
    [
        (
            ("sem_image", "mechanical_properties"),
            ("sem_image", "mechanical_properties"),
            (),
            "SUCCEEDED",
            "RUNNING",
        ),
        (
            ("sem_image",),
            ("sem_image",),
            (),
            "SUCCEEDED",
            "RUNNING",
        ),
        (
            ("sem_image", "mechanical_properties"),
            ("sem_image",),
            ("mechanical_properties",),
            "PARTIALLY_SUCCEEDED",
            "PARTIALLY_SUCCEEDED",
        ),
        (
            ("mechanical_properties",),
            ("mechanical_properties",),
            (),
            "SUCCEEDED",
            "RUNNING",
        ),
        (
            ("mechanical_properties",),
            (),
            ("mechanical_properties",),
            "FAILED",
            "FAILED",
        ),
    ],
)
def test_result_commit_is_atomic_and_mechanically_terminalizes_sources(
    migrated_database_engine: Engine,
    requested: tuple[str, ...],
    completed: tuple[str, ...],
    failed: tuple[str, ...],
    expected_status: str,
    task_status: str,
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=requested,
        completed=completed,
        failed=failed,
    )
    service = ResultService(
        _factory(migrated_database_engine),
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    )

    result = service.commit_result(ACTOR, receipt=receipt, assets=assets)

    assert result.status == expected_status
    assert result.data == receipt.output.data
    assert result.provenance == {
        "input_revision": 1,
        "normalized_process_parameters": {
            "solution_temperature": {
                "value": 1000,
                "unit": "°C",
            },
            "solution_time": {"value": 3, "unit": "h"},
            "aging_temperature": {
                "value": 730,
                "unit": "°C",
            },
            "aging_time": {"value": 3, "unit": "h"},
        },
        "actual_runtime_parameters": {
            "seed": 101,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
    }
    assert "task_input_revision_id" not in result.provenance
    assert "model_bundle_id" not in result.provenance
    with _factory(migrated_database_engine)() as unit_of_work:
        stored = unit_of_work.tool_results.get("result_1")
        links = unit_of_work.result_asset_links.list_for_result("result_1")
        task = unit_of_work.tasks.get("task_1")
        run = unit_of_work.tool_runs.get("tool_run_1")
    assert stored == result
    assert [link.asset_id for link in links] == ["asset_1"]
    assert task.current_status == task_status
    assert task.selected_tool_run_id == "tool_run_1"
    assert task.selected_result_id == "result_1"
    assert run.current_status == expected_status
    assert run.completed_outputs == list(completed)
    assert run.failed_outputs == list(failed)


@pytest.mark.parametrize(
    ("transform", "case"),
    [
        (lambda revision: replace(revision, task_id="task_2"), "cross-task"),
        (
            lambda revision: replace(
                revision,
                task_input_revision_id="revision_other",
            ),
            "revision-id-mismatch",
        ),
        (
            lambda revision: replace(revision, normalized_input=None),
            "missing-normalized-input",
        ),
    ],
)
def test_result_commit_rejects_untrusted_task_input_revision_sources(
    migrated_database_engine: Engine,
    transform,
    case: str,
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=("sem_image",),
        completed=("sem_image",),
        failed=(),
    )
    factory = _RevisionOverrideFactory(
        _factory(migrated_database_engine),
        transform,
    )
    service = ResultService(
        factory,
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    )

    with pytest.raises(ApplicationConflictError):
        service.commit_result(ACTOR, receipt=receipt, assets=assets)

    assert case
    assert factory.created[0].revisions.requested_ids == ["revision_1"]
    with _factory(migrated_database_engine)() as unit_of_work:
        assert unit_of_work.tool_results.get("result_1") is None
        task = unit_of_work.tasks.get("task_1")
        run = unit_of_work.tool_runs.get("tool_run_1")
    assert task.current_status == "RUNNING"
    assert task.selected_tool_run_id is None
    assert task.selected_result_id is None
    assert run.current_status == "RUNNING"


@pytest.mark.parametrize(
    ("seed_overrides", "case"),
    [
        (
            {"revision_request_id": "request_other"},
            "revision-request-id",
        ),
        (
            {"revision_material": "OTHER_MATERIAL"},
            "revision-material",
        ),
        (
            {"revision_requested_outputs": ("mechanical_properties",)},
            "revision-requested-outputs",
        ),
        (
            {"execution_requested_outputs": ("mechanical_properties",)},
            "execution-requested-outputs",
        ),
        (
            {"normalized_unit_overrides": {"solution_time": "min"}},
            "time-unit",
        ),
        (
            {
                "normalized_unit_overrides": {
                    "solution_temperature": "K"
                }
            },
            "temperature-unit",
        ),
    ],
)
def test_result_commit_rejects_inconsistent_revision_and_tool_run_sources(
    migrated_database_engine: Engine,
    seed_overrides: dict[str, object],
    case: str,
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=("sem_image",),
        completed=("sem_image",),
        failed=(),
        **seed_overrides,
    )
    service = ResultService(
        _factory(migrated_database_engine),
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    )

    with pytest.raises(ApplicationConflictError):
        service.commit_result(ACTOR, receipt=receipt, assets=assets)

    assert case
    with _factory(migrated_database_engine)() as unit_of_work:
        assert unit_of_work.tool_results.get("result_1") is None
        assert (
            unit_of_work.result_asset_links.list_for_result("result_1")
            == []
        )
        task = unit_of_work.tasks.get("task_1")
        run = unit_of_work.tool_runs.get("tool_run_1")
    assert task.current_status == "RUNNING"
    assert task.selected_tool_run_id is None
    assert task.selected_result_id is None
    assert run.current_status == "RUNNING"
    assert run.completed_outputs == []
    assert run.failed_outputs == []


def test_result_commit_rejects_duplicate_and_non_available_or_foreign_assets(
    migrated_database_engine: Engine,
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=("sem_image",),
        completed=("sem_image",),
        failed=(),
    )
    service = ResultService(
        _factory(migrated_database_engine),
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    )
    service.commit_result(ACTOR, receipt=receipt, assets=assets)

    with pytest.raises(ApplicationConflictError):
        service.commit_result(ACTOR, receipt=receipt, assets=assets)


@pytest.mark.parametrize(
    "seed_overrides",
    [
        {"asset_actor_id": "actor_2"},
        {"asset_task_id": "task_2"},
        {"asset_tool_run_id": "tool_run_2"},
    ],
)
def test_result_commit_rejects_cross_actor_task_or_tool_run_asset(
    migrated_database_engine: Engine,
    seed_overrides: dict[str, str],
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=("sem_image",),
        completed=("sem_image",),
        failed=(),
        **seed_overrides,
    )
    service = ResultService(
        _factory(migrated_database_engine),
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    )

    with pytest.raises(ApplicationConflictError):
        service.commit_result(ACTOR, receipt=receipt, assets=assets)

    with _factory(migrated_database_engine)() as unit_of_work:
        assert unit_of_work.tool_results.get("result_1") is None


def test_result_commit_rejects_non_available_asset(
    migrated_database_engine: Engine,
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=("sem_image",),
        completed=("sem_image",),
        failed=(),
        asset_status="PENDING",
    )
    service = ResultService(
        _factory(migrated_database_engine),
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    )

    with pytest.raises(ApplicationConflictError):
        service.commit_result(ACTOR, receipt=receipt, assets=assets)

    with _factory(migrated_database_engine)() as unit_of_work:
        assert unit_of_work.tool_results.get("result_1") is None
        task = unit_of_work.tasks.get("task_1")
        assert task.selected_result_id is None
        assert task.selected_tool_run_id is None


def test_result_commit_rejects_mechanical_success_without_available_sem_source(
    migrated_database_engine: Engine,
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=("mechanical_properties",),
        completed=("mechanical_properties",),
        failed=(),
        include_image=False,
    )
    service = ResultService(
        _factory(migrated_database_engine),
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    )

    with pytest.raises(ApplicationConflictError):
        service.commit_result(ACTOR, receipt=receipt, assets=assets)

    with _factory(migrated_database_engine)() as unit_of_work:
        assert unit_of_work.tool_results.get("result_1") is None


def test_result_commit_rejects_post_receipt_scientific_value_mutation(
    migrated_database_engine: Engine,
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=("sem_image", "mechanical_properties"),
        completed=("sem_image", "mechanical_properties"),
        failed=(),
    )
    receipt.output.data["yield_strength"]["value"] = 9999.0
    service = ResultService(
        _factory(migrated_database_engine),
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    )

    with pytest.raises(ApplicationConflictError):
        service.commit_result(ACTOR, receipt=receipt, assets=assets)

    with _factory(migrated_database_engine)() as unit_of_work:
        assert unit_of_work.tool_results.get("result_1") is None


def test_result_commit_rejects_version_and_selected_reference_mismatches(
    migrated_database_engine: Engine,
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=("sem_image",),
        completed=("sem_image",),
        failed=(),
    )
    service = ResultService(
        _factory(migrated_database_engine),
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    )

    with pytest.raises(ApplicationConflictError):
        service.commit_result(
            ACTOR,
            receipt=replace(
                receipt,
                tool_run=replace(
                    receipt.tool_run,
                    tool_version="different-version",
                ),
            ),
            assets=assets,
        )

    with _factory(migrated_database_engine)() as unit_of_work:
        task = unit_of_work.tasks.get("task_1")
        selected = replace(
            task,
            selected_tool_run_id="tool_run_1",
            updated_at=BASE + timedelta(seconds=5),
        )
        assert unit_of_work.tasks.update(
            selected,
            expected_status="RUNNING",
        )
        unit_of_work.commit()

    with pytest.raises(ApplicationConflictError):
        service.commit_result(ACTOR, receipt=receipt, assets=assets)


class _FailFirstCommitUoW(SQLAlchemyUnitOfWork):
    def __init__(self, session_factory, failure_state: dict[str, bool]) -> None:
        super().__init__(session_factory)
        self._failure_state = failure_state

    def commit(self) -> None:
        if not self._failure_state["failed"]:
            self._failure_state["failed"] = True
            self.rollback()
            raise PersistenceError("Persistence operation failed.")
        super().commit()


class _FailFirstCommitFactory:
    def __init__(self, engine: Engine) -> None:
        self._session_factory = create_session_factory(engine)
        self.failure_state = {"failed": False}

    def __call__(self) -> _FailFirstCommitUoW:
        return _FailFirstCommitUoW(
            self._session_factory,
            self.failure_state,
        )


class _StaticExecutionService:
    def __init__(self, receipt: ToolExecutionReceipt) -> None:
        self.receipt = receipt
        self.calls = 0

    def execute_revision_with_output(self, *_args, **_kwargs):
        self.calls += 1
        return self.receipt


class _StaticAssetService:
    def __init__(self, assets: list[Asset]) -> None:
        self.assets = assets
        self.calls = 0

    def create_from_output(self, *_args, **_kwargs):
        self.calls += 1
        return self.assets


class _CommitThenReportFailureResultService:
    def __init__(self, service: ResultService) -> None:
        self._service = service

    def commit_result(self, actor, *, receipt, assets):
        self._service.commit_result(
            actor,
            receipt=receipt,
            assets=assets,
        )
        raise ResultPersistenceError(task_id=receipt.tool_run.task_id)


def _workflow_for_result_failure(
    engine: Engine,
    receipt: ToolExecutionReceipt,
    assets: list[Asset],
    *,
    terminalization_factory=None,
) -> tuple[
    ToolWorkflowService,
    _StaticExecutionService,
    _StaticAssetService,
    MockExplanationAdapter,
]:
    execution = _StaticExecutionService(receipt)
    asset_service = _StaticAssetService(assets)
    explanation_adapter = MockExplanationAdapter(mode="success")
    normal_factory = _factory(engine)
    result_service = ResultService(
        _FailFirstCommitFactory(engine),
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    )
    explanation_service = ExplanationService(
        normal_factory,
        explanation_adapter,
        clock=lambda: BASE + timedelta(seconds=6),
    )
    workflow = ToolWorkflowService(
        terminalization_factory or normal_factory,
        execution,
        asset_service,
        result_service,
        explanation_service,
        clock=lambda: BASE + timedelta(seconds=6),
    )
    return workflow, execution, asset_service, explanation_adapter


def test_result_commit_failure_rolls_back_and_returns_no_memory_result(
    migrated_database_engine: Engine,
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=("sem_image",),
        completed=("sem_image",),
        failed=(),
    )
    workflow, execution, asset_service, explanation_adapter = (
        _workflow_for_result_failure(
            migrated_database_engine,
            receipt,
            assets,
        )
    )

    with pytest.raises(ResultPersistenceError):
        workflow.execute(
            ACTOR,
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="request_1",
        )

    with _factory(migrated_database_engine)() as unit_of_work:
        assert unit_of_work.tool_results.get("result_1") is None
        assert unit_of_work.result_asset_links.list_for_result("result_1") == []
        task = unit_of_work.tasks.get("task_1")
        run = unit_of_work.tool_runs.get("tool_run_1")
        asset = unit_of_work.assets.get("asset_1")
        assert task.current_status == "FAILED"
        assert task.selected_tool_run_id == "tool_run_1"
        assert task.selected_result_id is None
        assert task.error_code == "RESULT_PERSISTENCE_FAILED"
        assert run.current_status == "FAILED"
        assert run.completed_outputs == []
        assert run.failed_outputs == list(run.requested_outputs)
        assert run.error_code == "RESULT_PERSISTENCE_FAILED"
        assert run.diagnostics == receipt.tool_run.diagnostics
        assert run.output_summary == receipt.tool_run.output_summary
        assert asset.current_status == "AVAILABLE"
    with migrated_database_engine.connect() as connection:
        assert connection.scalar(
            text("SELECT count(*) FROM natural_language_explanation")
        ) == 0
        assert connection.scalar(
            text(
                "SELECT count(*) FROM llm_call "
                "WHERE purpose = 'TOOL_RESULT_EXPLANATION'"
            )
        ) == 0
    assert execution.calls == 1
    assert asset_service.calls == 1
    assert explanation_adapter.call_count == 0


def test_uncertain_result_commit_does_not_overwrite_persisted_result(
    migrated_database_engine: Engine,
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=("sem_image",),
        completed=("sem_image",),
        failed=(),
    )
    normal_factory = _factory(migrated_database_engine)
    execution = _StaticExecutionService(receipt)
    asset_service = _StaticAssetService(assets)
    explanation_adapter = MockExplanationAdapter(mode="success")
    result_service = _CommitThenReportFailureResultService(
        ResultService(
            normal_factory,
            clock=lambda: BASE + timedelta(seconds=5),
            id_factory=lambda: "result_1",
        )
    )
    workflow = ToolWorkflowService(
        normal_factory,
        execution,
        asset_service,
        result_service,
        ExplanationService(
            normal_factory,
            explanation_adapter,
            clock=lambda: BASE + timedelta(seconds=6),
        ),
        clock=lambda: BASE + timedelta(seconds=6),
    )

    with pytest.raises(ResultPersistenceError):
        workflow.execute(
            ACTOR,
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="request_1",
        )

    with normal_factory() as unit_of_work:
        result = unit_of_work.tool_results.get("result_1")
        links = unit_of_work.result_asset_links.list_for_result("result_1")
        task = unit_of_work.tasks.get("task_1")
        run = unit_of_work.tool_runs.get("tool_run_1")
        assert result is not None
        assert len(links) == 1
        assert task.current_status == "RUNNING"
        assert task.selected_tool_run_id == "tool_run_1"
        assert task.selected_result_id == "result_1"
        assert task.error_code is None
        assert run.current_status == "SUCCEEDED"
        assert run.completed_outputs == ["sem_image"]
        assert run.failed_outputs == []
        assert run.error_code is None
    assert execution.calls == 1
    assert asset_service.calls == 1
    assert explanation_adapter.call_count == 0


def test_result_failure_terminalization_commit_failure_preserves_real_state(
    migrated_database_engine: Engine,
) -> None:
    receipt, assets = _seed(
        migrated_database_engine,
        requested=("sem_image",),
        completed=("sem_image",),
        failed=(),
    )
    terminalization_factory = _FailFirstCommitFactory(
        migrated_database_engine
    )
    workflow, execution, asset_service, explanation_adapter = (
        _workflow_for_result_failure(
            migrated_database_engine,
            receipt,
            assets,
            terminalization_factory=terminalization_factory,
        )
    )

    with pytest.raises(ResultPersistenceError):
        workflow.execute(
            ACTOR,
            task_id="task_1",
            task_input_revision_id="revision_1",
            request_id="request_1",
        )

    with _factory(migrated_database_engine)() as unit_of_work:
        assert unit_of_work.tool_results.get("result_1") is None
        assert unit_of_work.result_asset_links.list_for_result("result_1") == []
        task = unit_of_work.tasks.get("task_1")
        run = unit_of_work.tool_runs.get("tool_run_1")
        asset = unit_of_work.assets.get("asset_1")
        assert task.current_status == "RUNNING"
        assert task.selected_tool_run_id is None
        assert task.selected_result_id is None
        assert task.error_code is None
        assert run.current_status == "RUNNING"
        assert run.completed_outputs == []
        assert run.failed_outputs == []
        assert run.error_code is None
        assert run.diagnostics == receipt.tool_run.diagnostics
        assert run.output_summary == receipt.tool_run.output_summary
        assert asset.current_status == "AVAILABLE"
    with migrated_database_engine.connect() as connection:
        assert connection.scalar(
            text("SELECT count(*) FROM natural_language_explanation")
        ) == 0
        assert connection.scalar(
            text(
                "SELECT count(*) FROM llm_call "
                "WHERE purpose = 'TOOL_RESULT_EXPLANATION'"
            )
        ) == 0
    assert execution.calls == 1
    assert asset_service.calls == 1
    assert explanation_adapter.call_count == 0
