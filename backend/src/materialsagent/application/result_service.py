from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from uuid import uuid4

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationConflictError,
    ApplicationInternalError,
    ResourceNotFoundError,
)
from materialsagent.application.tool_execution import (
    ToolExecutionReceipt,
    _tool_output_fingerprint,
    normalize_tool_output_summary,
)
from materialsagent.application.managed_input_contracts import source_for, provenance_for
from materialsagent.domain.models.asset import Asset
from materialsagent.domain.models.result_asset_link import ResultAssetLink
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.tool_execution import ToolExecutionOutput
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWork,
    UnitOfWorkFactory,
)


class ResultPersistenceError(ApplicationInternalError):
    default_message = "Tool result could not be persisted."
    default_code = "RESULT_PERSISTENCE_FAILED"


@dataclass(frozen=True, slots=True)
class ResultArtifactProjection:
    asset_id: str
    status: str
    role: str
    media_type: str
    width: int
    height: int
    bit_depth: int
    size_bytes: int
    sha256: str
    content_url: str


@dataclass(frozen=True, slots=True)
class ToolResultProjection:
    result: ToolResult
    artifacts: tuple[ResultArtifactProjection, ...]


@dataclass(frozen=True, slots=True)
class _ValidatedResultSources:
    task: Task
    tool_run: ToolRun
    revision_number: int
    normalized_process_parameters: dict[str, object]
    stored_assets: tuple[Asset, ...]


@dataclass(frozen=True, slots=True)
class _ResultTerminalFacts:
    error_code: str | None
    safe_error_message: str | None
    task_status: str


@dataclass(frozen=True, slots=True)
class _ExpectedResultCommit:
    result: ToolResult
    links: tuple[ResultAssetLink, ...]
    tool_run: ToolRun
    task: Task
    assets: tuple[Asset, ...]


class ToolResultQueryService:
    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def get(
        self,
        actor: ActorContext,
        result_id: str,
    ) -> ToolResultProjection:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                result = unit_of_work.tool_results.get_owned(
                    result_id,
                    actor.actor_id,
                )
                if result is None:
                    raise ResourceNotFoundError()
                links = unit_of_work.result_asset_links.list_for_result(
                    result.result_id
                )
                artifacts: list[ResultArtifactProjection] = []
                for link in links:
                    asset = unit_of_work.assets.get_owned(
                        link.asset_id,
                        actor.actor_id,
                    )
                    if (
                        asset is None
                        or asset.current_status != "AVAILABLE"
                        or asset.task_id != result.task_id
                        or asset.producer_tool_run_id != result.tool_run_id
                        or asset.media_type is None
                        or asset.width is None
                        or asset.height is None
                        or asset.bit_depth is None
                        or asset.size_bytes is None
                        or asset.sha256 is None
                    ):
                        raise ApplicationConflictError(
                            task_id=result.task_id
                        )
                    artifacts.append(
                        ResultArtifactProjection(
                            asset_id=asset.asset_id,
                            status=asset.current_status,
                            role=asset.role,
                            media_type=asset.media_type,
                            width=asset.width,
                            height=asset.height,
                            bit_depth=asset.bit_depth,
                            size_bytes=asset.size_bytes,
                            sha256=asset.sha256,
                            content_url=(
                                f"/api/v1/assets/{asset.asset_id}/content"
                            ),
                        )
                    )
        except PersistenceError as error:
            raise ResultPersistenceError() from error
        return ToolResultProjection(
            result=result,
            artifacts=tuple(artifacts),
        )


class ResultService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._id_factory = id_factory or (lambda: str(uuid4()))

    def commit_initial_result(
        self,
        actor: ActorContext,
        *,
        receipt: ToolExecutionReceipt,
        assets: list[Asset],
    ) -> ToolResult:
        return self._commit_result(
            actor,
            receipt=receipt,
            assets=assets,
            selection_policy="INITIAL",
        )

    def commit_retry_result(
        self,
        actor: ActorContext,
        *,
        receipt: ToolExecutionReceipt,
        assets: list[Asset],
    ) -> ToolResult:
        return self._commit_result(
            actor,
            receipt=receipt,
            assets=assets,
            selection_policy="RETRY",
        )

    def _commit_result(
        self,
        actor: ActorContext,
        *,
        receipt: ToolExecutionReceipt,
        assets: list[Asset],
        selection_policy: str,
    ) -> ToolResult:
        result_id = self._id_factory()
        completed_at = self._clock()
        task_id = receipt.tool_run.task_id
        output = receipt.output
        if _tool_output_fingerprint(output) != receipt.output_fingerprint:
            raise ApplicationConflictError(task_id=task_id)
        normalized_summary = normalize_tool_output_summary(output)
        expected_commit: _ExpectedResultCommit | None = None

        try:
            with self._unit_of_work_factory() as unit_of_work:
                sources = self._load_and_validate_initial_sources(
                    unit_of_work,
                    actor,
                    receipt=receipt,
                    assets=assets,
                    normalized_summary=normalized_summary,
                    selection_policy=selection_policy,
                )
                result = self._build_tool_result(
                    result_id=result_id,
                    actor=actor,
                    sources=sources,
                    output=output,
                    normalized_summary=normalized_summary,
                    completed_at=completed_at,
                )
                links = self._build_result_links(
                    result=result,
                    stored_assets=sources.stored_assets,
                    completed_at=completed_at,
                )
                terminal_facts = self._result_terminal_facts(result)
                completed_run, completed_task = (
                    self._build_terminal_run_and_task(
                        sources=sources,
                        result=result,
                        output=output,
                        terminal_facts=terminal_facts,
                        completed_at=completed_at,
                    )
                )
                expected_commit = _ExpectedResultCommit(
                    result=result,
                    links=links,
                    tool_run=completed_run,
                    task=completed_task,
                    assets=sources.stored_assets,
                )
                self._persist_result_and_links(
                    unit_of_work,
                    result=result,
                    links=links,
                )
                self._terminalize_tool_run_and_select_result(
                    unit_of_work,
                    completed_run=completed_run,
                    completed_task=completed_task,
                )
                unit_of_work.commit()
        except PersistenceError as error:
            if expected_commit is not None:
                return self._recover_committed_result(
                    actor,
                    expected=expected_commit,
                )
            raise ResultPersistenceError(task_id=task_id) from error

        try:
            with self._unit_of_work_factory() as unit_of_work:
                persisted = unit_of_work.tool_results.get_owned(
                    result_id,
                    actor.actor_id,
                )
        except PersistenceError as error:
            raise ResultPersistenceError(task_id=task_id) from error
        if persisted is None:
            raise ResultPersistenceError(task_id=task_id)
        return persisted

    def _recover_committed_result(
        self,
        actor: ActorContext,
        *,
        expected: _ExpectedResultCommit,
    ) -> ToolResult:
        task_id = expected.result.task_id
        try:
            with self._unit_of_work_factory() as unit_of_work:
                persisted = unit_of_work.tool_results.get_owned(
                    expected.result.result_id,
                    actor.actor_id,
                )
                if persisted is None:
                    raise ResultPersistenceError(task_id=task_id)
                links = tuple(
                    unit_of_work.result_asset_links.list_for_result(
                        expected.result.result_id
                    )
                )
                tool_run = unit_of_work.tool_runs.get_owned(
                    expected.tool_run.tool_run_id,
                    actor.actor_id,
                )
                task = unit_of_work.tasks.get_owned(
                    expected.task.task_id,
                    actor.actor_id,
                )
                stored_assets = tuple(
                    unit_of_work.assets.get_owned(
                        asset.asset_id,
                        actor.actor_id,
                    )
                    for asset in expected.assets
                )
        except PersistenceError as error:
            raise ResultPersistenceError(task_id=task_id) from error
        if (
            persisted != expected.result
            or links != expected.links
            or tool_run != expected.tool_run
            or task != expected.task
            or any(
                current is None
                or current.current_status != "AVAILABLE"
                or current.actor_id != actor.actor_id
                or current.task_id != expected.result.task_id
                or current.producer_tool_run_id
                != expected.result.tool_run_id
                for current in stored_assets
            )
        ):
            raise ApplicationConflictError(task_id=task_id)
        return persisted

    def _load_and_validate_initial_sources(
        self,
        unit_of_work: UnitOfWork,
        actor: ActorContext,
        *,
        receipt: ToolExecutionReceipt,
        assets: list[Asset],
        normalized_summary: dict[str, object],
        selection_policy: str,
    ) -> _ValidatedResultSources:
        output = receipt.output
        task_id = receipt.tool_run.task_id
        task = unit_of_work.tasks.get_owned_for_update(
            task_id,
            actor.actor_id,
        )
        if task is None:
            raise ResourceNotFoundError(task_id=task_id)
        tool_run = unit_of_work.tool_runs.get_owned_for_update(
            receipt.tool_run.tool_run_id,
            actor.actor_id,
        )
        if tool_run is None or tool_run.task_id != task.task_id:
            raise ResourceNotFoundError(task_id=task_id)

        if selection_policy == "RETRY":
            self._validate_retry_selection(
                unit_of_work,
                actor,
                task=task,
                retry_run=tool_run,
            )
        self._validate_initial_task_and_run(
            task,
            tool_run,
            receipt=receipt,
            normalized_summary=normalized_summary,
            selection_policy=selection_policy,
        )
        self._validate_output_partition(output, task_id=task_id)
        if (
            unit_of_work.tool_results.get_for_tool_run(
                tool_run.tool_run_id
            )
            is not None
        ):
            raise ApplicationConflictError(task_id=task_id)
        (
            revision_number,
            normalized_process_parameters,
        ) = self._load_normalized_process_parameters(
            unit_of_work,
            task=task,
            tool_run=tool_run,
            selection_policy=selection_policy,
        )
        stored_assets = self._load_and_validate_assets(
            unit_of_work,
            actor,
            task=task,
            tool_run=tool_run,
            assets=assets,
            output=output,
        )
        return _ValidatedResultSources(
            task=task,
            tool_run=tool_run,
            revision_number=revision_number,
            normalized_process_parameters=normalized_process_parameters,
            stored_assets=stored_assets,
        )

    @staticmethod
    def _require_initial_selection_empty(task: Task) -> None:
        if (
            task.selected_tool_run_id is not None
            or task.selected_result_id is not None
        ):
            raise ApplicationConflictError(task_id=task.task_id)

    @staticmethod
    def _validate_retry_selection(
        unit_of_work: UnitOfWork,
        actor: ActorContext,
        *,
        task: Task,
        retry_run: ToolRun,
    ) -> None:
        if (
            task.selected_result_id is not None
            and task.selected_tool_run_id is None
        ):
            raise ApplicationConflictError(task_id=task.task_id)

        task_runs = unit_of_work.tool_runs.list_for_task(task.task_id)
        active_runs = [
            item
            for item in task_runs
            if item.current_status in {"PENDING", "RUNNING"}
        ]
        prior_runs = [
            item
            for item in task_runs
            if item.tool_run_id != retry_run.tool_run_id
        ]
        if (
            len(active_runs) != 1
            or active_runs[0].tool_run_id != retry_run.tool_run_id
            or any(item.task_id != task.task_id for item in task_runs)
            or any(
                retry_run.attempt_no <= item.attempt_no
                for item in prior_runs
            )
        ):
            raise ApplicationConflictError(task_id=task.task_id)

        if task.selected_tool_run_id is None:
            if (
                task.selected_result_id is not None
                or prior_runs
                or retry_run.attempt_no != 1
            ):
                raise ApplicationConflictError(task_id=task.task_id)
            return

        selected_run = unit_of_work.tool_runs.get_owned_for_update(
            task.selected_tool_run_id,
            actor.actor_id,
        )
        if (
            selected_run is None
            or selected_run.task_id != task.task_id
            or selected_run.tool_run_id == retry_run.tool_run_id
            or selected_run.attempt_no >= retry_run.attempt_no
        ):
            raise ApplicationConflictError(task_id=task.task_id)

        if task.selected_result_id is None:
            if (
                selected_run.current_status != "FAILED"
                or unit_of_work.tool_results.get_for_tool_run(
                    selected_run.tool_run_id
                )
                is not None
            ):
                raise ApplicationConflictError(task_id=task.task_id)
            return

        selected_result = unit_of_work.tool_results.get_owned_for_update(
            task.selected_result_id,
            actor.actor_id,
        )
        expected_status = (
            "SUCCEEDED"
            if (
                selected_run.completed_outputs
                == selected_run.requested_outputs
                and not selected_run.failed_outputs
            )
            else "PARTIALLY_SUCCEEDED"
            if selected_run.completed_outputs
            else "FAILED"
        )
        if (
            selected_result is None
            or selected_result.actor_id != actor.actor_id
            or selected_result.task_id != task.task_id
            or selected_result.tool_run_id != selected_run.tool_run_id
            or selected_result.status != expected_status
            or selected_run.current_status != expected_status
            or tuple(selected_result.requested_outputs)
            != tuple(selected_run.requested_outputs)
            or tuple(selected_result.completed_outputs)
            != tuple(selected_run.completed_outputs)
            or tuple(selected_result.failed_outputs)
            != tuple(selected_run.failed_outputs)
        ):
            raise ApplicationConflictError(task_id=task.task_id)

    @classmethod
    def _validate_initial_task_and_run(
        cls,
        task: Task,
        tool_run: ToolRun,
        *,
        receipt: ToolExecutionReceipt,
        normalized_summary: dict[str, object],
        selection_policy: str,
    ) -> None:
        if selection_policy == "INITIAL":
            cls._require_initial_selection_empty(task)
        elif selection_policy != "RETRY":
            raise ValueError("selection_policy is invalid.")
        output = receipt.output
        if (
            task.task_type != "TOOL_EXECUTION"
            or task.current_status != "RUNNING"
            or tool_run.current_status != "RUNNING"
            or tool_run.output_summary != normalized_summary
            or receipt.tool_run.output_summary != normalized_summary
            or tool_run.tool_run_id != receipt.tool_run.tool_run_id
            or tool_run.requested_outputs
            != list(output.requested_outputs)
            or tool_run.tool_id != receipt.tool_run.tool_id
            or tool_run.tool_version != receipt.tool_run.tool_version
            or tool_run.schema_hash != receipt.tool_run.schema_hash
            or tool_run.task_id != receipt.tool_run.task_id
            or tool_run.request_id != receipt.tool_run.request_id
            or tool_run.task_input_revision_id
            != receipt.tool_run.task_input_revision_id
            or tool_run.attempt_no != receipt.tool_run.attempt_no
            or tool_run.execution_input != receipt.tool_run.execution_input
            or tool_run.actual_runtime_parameters
            != dict(output.actual_runtime_parameters)
            or tool_run.diagnostics
            != [dict(item) for item in output.diagnostics]
            or tool_run.model_bundle_id != output.model_bundle_id
        ):
            raise ApplicationConflictError(task_id=task.task_id)

    @staticmethod
    def _validate_output_partition(
        output: ToolExecutionOutput,
        *,
        task_id: str,
    ) -> None:
        if (
            set(output.completed_outputs) & set(output.failed_outputs)
            or set(output.completed_outputs) | set(output.failed_outputs)
            != set(output.requested_outputs)
        ):
            raise ApplicationConflictError(task_id=task_id)

    @staticmethod
    def _load_normalized_process_parameters(
        unit_of_work: UnitOfWork,
        *,
        task: Task,
        tool_run: ToolRun,
        selection_policy: str,
    ) -> tuple[int, dict[str, object]]:
        revision = unit_of_work.task_input_revisions.get(
            tool_run.task_input_revision_id
        )
        if (
            revision is None
            or revision.task_id != task.task_id
            or revision.task_input_revision_id
            != tool_run.task_input_revision_id
            or (
                selection_policy == "INITIAL"
                and revision.request_id != tool_run.request_id
            )
            or selection_policy not in {"INITIAL", "RETRY"}
            or revision.normalized_input is None
        ):
            raise ApplicationConflictError(task_id=task.task_id)
        return revision.revision, source_for(unit_of_work, task, tool_run, revision)

    @staticmethod
    def _load_and_validate_assets(
        unit_of_work: UnitOfWork,
        actor: ActorContext,
        *,
        task: Task,
        tool_run: ToolRun,
        assets: list[Asset],
        output: ToolExecutionOutput,
    ) -> tuple[Asset, ...]:
        if len(assets) != len(output.images) or len(
            {asset.asset_id for asset in assets}
        ) != len(assets):
            raise ApplicationConflictError(task_id=task.task_id)

        stored_assets: list[Asset] = []
        for index, asset in enumerate(assets):
            stored_asset = unit_of_work.assets.get_for_update(asset.asset_id)
            expected_role = (
                "requested_output"
                if output.images[index].requested_output
                else "intermediate"
            )
            if (
                stored_asset is None
                or stored_asset.current_status != "AVAILABLE"
                or stored_asset.actor_id != actor.actor_id
                or stored_asset.task_id != task.task_id
                or stored_asset.producer_tool_run_id
                != tool_run.tool_run_id
                or stored_asset.role != expected_role
            ):
                raise ApplicationConflictError(task_id=task.task_id)
            stored_assets.append(stored_asset)
        requested_asset_count = sum(
            asset.role == "requested_output" for asset in stored_assets
        )
        if (
            "mechanical_properties" in output.completed_outputs
            and not stored_assets
        ) or (
            "sem_image" in output.completed_outputs
            and requested_asset_count != 1
        ) or (
            "sem_image" not in output.completed_outputs
            and requested_asset_count != 0
        ):
            raise ApplicationConflictError(task_id=task.task_id)
        return tuple(stored_assets)

    @staticmethod
    def _build_tool_result(
        *,
        result_id: str,
        actor: ActorContext,
        sources: _ValidatedResultSources,
        output: ToolExecutionOutput,
        normalized_summary: dict[str, object],
        completed_at: datetime,
    ) -> ToolResult:
        normalized_error = normalized_summary.get("error")
        error = (
            None if normalized_error is None else dict(normalized_error)
        )
        status = (
            "SUCCEEDED"
            if len(output.completed_outputs) == len(output.requested_outputs)
            else "PARTIALLY_SUCCEEDED"
            if output.completed_outputs
            else "FAILED"
        )
        if (status == "SUCCEEDED") != (error is None):
            raise ApplicationConflictError(
                task_id=sources.task.task_id
            )
        return ToolResult(
            result_id=result_id,
            task_id=sources.task.task_id,
            tool_run_id=sources.tool_run.tool_run_id,
            actor_id=actor.actor_id,
            status=status,
            requested_outputs=list(output.requested_outputs),
            completed_outputs=list(output.completed_outputs),
            failed_outputs=list(output.failed_outputs),
            data=dict(output.data),
            warnings=[dict(item) for item in output.warnings],
            provenance=provenance_for(sources.tool_run.tool_id, sources.revision_number,
                sources.normalized_process_parameters, output.actual_runtime_parameters),
            error=error,
            tool_id=sources.tool_run.tool_id,
            tool_version=sources.tool_run.tool_version,
            schema_hash=sources.tool_run.schema_hash,
            created_at=completed_at,
        )

    @staticmethod
    def _persist_result_and_links(
        unit_of_work: UnitOfWork,
        *,
        result: ToolResult,
        links: tuple[ResultAssetLink, ...],
    ) -> None:
        unit_of_work.tool_results.add(result)
        for link in links:
            unit_of_work.result_asset_links.add(link)

    @staticmethod
    def _build_result_links(
        *,
        result: ToolResult,
        stored_assets: tuple[Asset, ...],
        completed_at: datetime,
    ) -> tuple[ResultAssetLink, ...]:
        return tuple(
            ResultAssetLink(
                result_id=result.result_id,
                asset_id=stored_asset.asset_id,
                artifact_order=artifact_order,
                created_at=completed_at,
            )
            for artifact_order, stored_asset in enumerate(stored_assets)
        )

    @staticmethod
    def _result_terminal_facts(
        result: ToolResult,
    ) -> _ResultTerminalFacts:
        error_code = (
            None if result.error is None else str(result.error["code"])
        )
        safe_error_message = (
            None
            if result.error is None
            else str(result.error["safe_message"])
        )
        return _ResultTerminalFacts(
            error_code=error_code,
            safe_error_message=safe_error_message,
            task_status=result.status,
        )

    @staticmethod
    def _terminalize_tool_run_and_select_result(
        unit_of_work: UnitOfWork,
        *,
        completed_run: ToolRun,
        completed_task: Task,
    ) -> None:
        if (
            unit_of_work.tool_runs.update(
                completed_run,
                expected_status="RUNNING",
            )
            is None
        ):
            raise ApplicationConflictError(
                task_id=completed_task.task_id
            )

        if (
            unit_of_work.tasks.update(
                completed_task,
                expected_status="RUNNING",
            )
            is None
        ):
            raise ApplicationConflictError(
                task_id=completed_task.task_id
            )

    @staticmethod
    def _build_terminal_run_and_task(
        *,
        sources: _ValidatedResultSources,
        result: ToolResult,
        output: ToolExecutionOutput,
        terminal_facts: _ResultTerminalFacts,
        completed_at: datetime,
    ) -> tuple[ToolRun, Task]:
        completed_run = sources.tool_run.complete_from_result(
            completed_outputs=list(output.completed_outputs),
            failed_outputs=list(output.failed_outputs),
            completed_at=completed_at,
            error_code=terminal_facts.error_code,
            safe_error_message=terminal_facts.safe_error_message,
        )
        completed_task = replace(
            sources.task,
            current_status=terminal_facts.task_status,
            selected_tool_run_id=sources.tool_run.tool_run_id,
            selected_result_id=result.result_id,
            updated_at=completed_at,
            completed_at=(
                None
                if terminal_facts.task_status == "RUNNING"
                else completed_at
            ),
            error_code=terminal_facts.error_code,
            safe_error_message=terminal_facts.safe_error_message,
        )
        return completed_run, completed_task
