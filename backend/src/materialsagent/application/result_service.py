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
from materialsagent.application.zta35g_input import PARAMETER_FIELDS
from materialsagent.domain.models.asset import Asset
from materialsagent.domain.models.result_asset_link import ResultAssetLink
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
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

    def commit_result(
        self,
        actor: ActorContext,
        *,
        receipt: ToolExecutionReceipt,
        assets: list[Asset],
    ) -> ToolResult:
        result_id = self._id_factory()
        completed_at = self._clock()
        task_id = receipt.tool_run.task_id
        output = receipt.output
        if _tool_output_fingerprint(output) != receipt.output_fingerprint:
            raise ApplicationConflictError(task_id=task_id)
        normalized_summary = normalize_tool_output_summary(output)

        try:
            with self._unit_of_work_factory() as unit_of_work:
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
                if (
                    task.task_type != "TOOL_EXECUTION"
                    or task.current_status != "RUNNING"
                    or task.selected_tool_run_id is not None
                    or task.selected_result_id is not None
                    or tool_run.current_status != "RUNNING"
                    or tool_run.output_summary != normalized_summary
                    or receipt.tool_run.output_summary != normalized_summary
                    or tool_run.tool_run_id != receipt.tool_run.tool_run_id
                    or tool_run.requested_outputs
                    != list(output.requested_outputs)
                    or tool_run.tool_id != receipt.tool_run.tool_id
                    or tool_run.tool_version != receipt.tool_run.tool_version
                    or tool_run.schema_version != receipt.tool_run.schema_version
                    or tool_run.task_id != receipt.tool_run.task_id
                    or tool_run.request_id != receipt.tool_run.request_id
                    or tool_run.task_input_revision_id
                    != receipt.tool_run.task_input_revision_id
                    or tool_run.attempt_no != receipt.tool_run.attempt_no
                    or tool_run.execution_input
                    != receipt.tool_run.execution_input
                    or tool_run.actual_runtime_parameters
                    != dict(output.actual_runtime_parameters)
                    or tool_run.diagnostics
                    != [dict(item) for item in output.diagnostics]
                    or tool_run.model_bundle_id != output.model_bundle_id
                ):
                    raise ApplicationConflictError(task_id=task_id)
                if (
                    set(output.completed_outputs)
                    & set(output.failed_outputs)
                    or set(output.completed_outputs)
                    | set(output.failed_outputs)
                    != set(output.requested_outputs)
                ):
                    raise ApplicationConflictError(task_id=task_id)
                if (
                    unit_of_work.tool_results.get_for_tool_run(
                        tool_run.tool_run_id
                    )
                    is not None
                ):
                    raise ApplicationConflictError(task_id=task_id)
                revision = unit_of_work.task_input_revisions.get(
                    tool_run.task_input_revision_id
                )
                if (
                    revision is None
                    or revision.task_id != task.task_id
                    or revision.task_input_revision_id
                    != tool_run.task_input_revision_id
                    or revision.request_id != tool_run.request_id
                    or revision.normalized_input is None
                ):
                    raise ApplicationConflictError(task_id=task_id)
                normalized_input = revision.normalized_input
                execution_process_parameters = (
                    tool_run.execution_input.get("process_parameters")
                )
                expected_requested_outputs = list(
                    tool_run.requested_outputs
                )
                if (
                    normalized_input.get("material") != "ZTA35G"
                    or normalized_input.get("requested_outputs")
                    != expected_requested_outputs
                    or tool_run.execution_input.get(
                        "requested_outputs"
                    )
                    != expected_requested_outputs
                    or not isinstance(
                        execution_process_parameters,
                        dict,
                    )
                ):
                    raise ApplicationConflictError(task_id=task_id)
                expected_units = {
                    "solution_temperature": "°C",
                    "solution_time": "h",
                    "aging_temperature": "°C",
                    "aging_time": "h",
                }
                normalized_process_parameters: dict[
                    str,
                    object,
                ] = {}
                for field_name in PARAMETER_FIELDS:
                    parameter = normalized_input.get(field_name)
                    if (
                        not isinstance(parameter, dict)
                        or set(parameter) != {"value", "unit"}
                        or parameter["unit"]
                        != expected_units[field_name]
                        or field_name not in execution_process_parameters
                        or parameter["value"]
                        != execution_process_parameters[field_name]
                    ):
                        raise ApplicationConflictError(task_id=task_id)
                    normalized_process_parameters[field_name] = dict(
                        parameter
                    )
                if len(assets) != len(output.images) or len(
                    {asset.asset_id for asset in assets}
                ) != len(assets):
                    raise ApplicationConflictError(task_id=task_id)

                stored_assets: list[Asset] = []
                for index, asset in enumerate(assets):
                    stored_asset = unit_of_work.assets.get_for_update(
                        asset.asset_id
                    )
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
                        raise ApplicationConflictError(task_id=task_id)
                    stored_assets.append(stored_asset)
                requested_asset_count = sum(
                    asset.role == "requested_output"
                    for asset in stored_assets
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
                    raise ApplicationConflictError(task_id=task_id)

                normalized_error = normalized_summary.get("error")
                error = (
                    None
                    if normalized_error is None
                    else dict(normalized_error)
                )
                status = (
                    "SUCCEEDED"
                    if len(output.completed_outputs)
                    == len(output.requested_outputs)
                    else "PARTIALLY_SUCCEEDED"
                    if output.completed_outputs
                    else "FAILED"
                )
                if (status == "SUCCEEDED") != (error is None):
                    raise ApplicationConflictError(task_id=task_id)
                result = ToolResult(
                    result_id=result_id,
                    task_id=task.task_id,
                    tool_run_id=tool_run.tool_run_id,
                    actor_id=actor.actor_id,
                    status=status,
                    requested_outputs=list(output.requested_outputs),
                    completed_outputs=list(output.completed_outputs),
                    failed_outputs=list(output.failed_outputs),
                    data=dict(output.data),
                    warnings=[dict(item) for item in output.warnings],
                    provenance={
                        "input_revision": revision.revision,
                        "normalized_process_parameters": (
                            normalized_process_parameters
                        ),
                        "actual_runtime_parameters": dict(
                            output.actual_runtime_parameters
                        ),
                    },
                    error=error,
                    tool_id=tool_run.tool_id,
                    tool_version=tool_run.tool_version,
                    schema_version=tool_run.schema_version,
                    created_at=completed_at,
                )
                unit_of_work.tool_results.add(result)
                for artifact_order, stored_asset in enumerate(stored_assets):
                    unit_of_work.result_asset_links.add(
                        ResultAssetLink(
                            result_id=result.result_id,
                            asset_id=stored_asset.asset_id,
                            artifact_order=artifact_order,
                            created_at=completed_at,
                        )
                    )

                error_code = (
                    None if error is None else str(error["code"])
                )
                safe_error_message = (
                    None if error is None else str(error["safe_message"])
                )
                completed_run = tool_run.complete_from_result(
                    completed_outputs=list(output.completed_outputs),
                    failed_outputs=list(output.failed_outputs),
                    completed_at=completed_at,
                    error_code=error_code,
                    safe_error_message=safe_error_message,
                )
                if (
                    unit_of_work.tool_runs.update(
                        completed_run,
                        expected_status="RUNNING",
                    )
                    is None
                ):
                    raise ApplicationConflictError(task_id=task_id)

                task_status = (
                    "RUNNING"
                    if result.status == "SUCCEEDED"
                    else result.status
                )
                completed_task = replace(
                    task,
                    current_status=task_status,
                    selected_tool_run_id=tool_run.tool_run_id,
                    selected_result_id=result.result_id,
                    updated_at=completed_at,
                    completed_at=(
                        None
                        if task_status == "RUNNING"
                        else completed_at
                    ),
                    error_code=error_code,
                    safe_error_message=safe_error_message,
                )
                if (
                    unit_of_work.tasks.update(
                        completed_task,
                        expected_status="RUNNING",
                    )
                    is None
                ):
                    raise ApplicationConflictError(task_id=task_id)
                unit_of_work.commit()
        except PersistenceError as error:
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
