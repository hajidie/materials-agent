from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import uuid4

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationConflictError,
    ApplicationInternalError,
    ResourceNotFoundError,
)
from materialsagent.domain.models.explanation import NaturalLanguageExplanation
from materialsagent.domain.models.llm_call import LLMCall
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.tool_result import ToolResult
from materialsagent.domain.models.tool_run import ToolRun
from materialsagent.domain.ports.explanation import (
    ExplanationAssetReference,
    ExplanationInput,
    ExplanationOutcome,
    ExplanationPort,
    ExplanationProtocolError,
    ExplanationProviderUnavailableError,
    ExplanationTimeoutError,
)
from materialsagent.domain.ports.unit_of_work import (
    PersistenceError,
    UnitOfWork,
    UnitOfWorkFactory,
)


class ExplanationPersistenceError(ApplicationInternalError):
    default_message = "Explanation outcome could not be persisted."
    default_code = "EXPLANATION_PERSISTENCE_FAILED"


@dataclass(frozen=True, slots=True)
class PreparedExplanation:
    explanation_id: str
    llm_call_id: str
    task_id: str
    result_id: str
    projection: ExplanationInput


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain_json(item) for item in value]
    return value


def _prompt_digest(projection: ExplanationInput) -> str:
    safe_value = {
        "result_id": projection.result_id,
        "status": projection.status,
        "requested_outputs": list(projection.requested_outputs),
        "completed_outputs": list(projection.completed_outputs),
        "failed_outputs": list(projection.failed_outputs),
        "data": _plain_json(projection.data),
        "artifacts": [
            {
                "asset_id": item.asset_id,
                "role": item.role,
                "asset_type": item.asset_type,
            }
            for item in projection.artifacts
        ],
        "warnings": _plain_json(projection.warnings),
        "error": _plain_json(projection.error),
        "process_parameters": _plain_json(projection.process_parameters),
        "tool_id": projection.tool_id,
        "tool_version": projection.tool_version,
        "schema_version": projection.schema_version,
    }
    encoded = json.dumps(
        safe_value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


class ExplanationService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        port: ExplanationPort,
        *,
        clock: Callable[[], datetime] | None = None,
        explanation_id_factory: Callable[[], str] | None = None,
        llm_call_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._port = port
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._explanation_id_factory = (
            explanation_id_factory or (lambda: str(uuid4()))
        )
        self._llm_call_id_factory = (
            llm_call_id_factory or (lambda: str(uuid4()))
        )

    @staticmethod
    def _projection(
        unit_of_work: UnitOfWork,
        result: ToolResult,
    ) -> ExplanationInput:
        links = unit_of_work.result_asset_links.list_for_result(
            result.result_id
        )
        artifacts: list[ExplanationAssetReference] = []
        for link in links:
            asset = unit_of_work.assets.get(link.asset_id)
            if (
                asset is None
                or asset.current_status != "AVAILABLE"
                or asset.actor_id != result.actor_id
                or asset.task_id != result.task_id
                or asset.producer_tool_run_id != result.tool_run_id
            ):
                raise ApplicationConflictError(task_id=result.task_id)
            artifacts.append(
                ExplanationAssetReference(
                    asset_id=asset.asset_id,
                    role=asset.role,
                    asset_type=asset.asset_type,
                )
            )
        process_parameters = result.provenance.get(
            "normalized_process_parameters",
            {},
        )
        if not isinstance(process_parameters, Mapping):
            raise ApplicationConflictError(task_id=result.task_id)
        return ExplanationInput(
            result_id=result.result_id,
            status=result.status,
            requested_outputs=tuple(result.requested_outputs),
            completed_outputs=tuple(result.completed_outputs),
            failed_outputs=tuple(result.failed_outputs),
            data=result.data,
            artifacts=tuple(artifacts),
            warnings=tuple(result.warnings),
            error=result.error,
            process_parameters=process_parameters,
            tool_id=result.tool_id,
            tool_version=result.tool_version,
            schema_version=result.schema_version,
        )

    @staticmethod
    def _source_chain(
        unit_of_work: UnitOfWork,
        actor: ActorContext,
        prepared: PreparedExplanation,
        *,
        lock_task: bool,
    ) -> tuple[
        ToolResult,
        Task,
        ToolRun,
        LLMCall,
        NaturalLanguageExplanation,
    ]:
        result = unit_of_work.tool_results.get_owned(
            prepared.result_id,
            actor.actor_id,
        )
        task = (
            unit_of_work.tasks.get_owned_for_update(
                prepared.task_id,
                actor.actor_id,
            )
            if lock_task
            else unit_of_work.tasks.get_owned(
                prepared.task_id,
                actor.actor_id,
            )
        )
        conversation = (
            None
            if task is None
            else unit_of_work.conversations.get_owned(
                task.conversation_id,
                actor.actor_id,
            )
        )
        tool_run = (
            None
            if result is None
            else unit_of_work.tool_runs.get_owned(
                result.tool_run_id,
                actor.actor_id,
            )
        )
        call = unit_of_work.llm_calls.get(prepared.llm_call_id)
        explanation = unit_of_work.explanations.get(
            prepared.explanation_id
        )
        if (
            result is None
            or task is None
            or conversation is None
            or tool_run is None
            or call is None
            or explanation is None
            or result.result_id != prepared.result_id
            or result.task_id != prepared.task_id
            or task.task_id != prepared.task_id
            or task.selected_result_id != result.result_id
            or task.selected_tool_run_id != result.tool_run_id
            or tool_run.task_id != task.task_id
            or call.task_id != task.task_id
            or call.conversation_id != conversation.conversation_id
            or call.request_id != tool_run.request_id
            or call.purpose != "TOOL_RESULT_EXPLANATION"
            or call.input_result_id != result.result_id
            or explanation.task_id != task.task_id
            or explanation.result_id != result.result_id
            or explanation.llm_call_id != call.llm_call_id
            or explanation.attempt_no != 1
        ):
            raise ApplicationConflictError(task_id=prepared.task_id)
        return result, task, tool_run, call, explanation

    @staticmethod
    def _expected_task_status(
        result: ToolResult,
        outcome: ExplanationOutcome,
    ) -> str:
        if result.status != "SUCCEEDED":
            return result.status
        return (
            "SUCCEEDED"
            if outcome.text is not None
            else "PARTIALLY_SUCCEEDED"
        )

    def _read_equivalent_terminal(
        self,
        actor: ActorContext,
        prepared: PreparedExplanation,
        outcome: ExplanationOutcome,
    ) -> NaturalLanguageExplanation:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                result, task, _, call, explanation = self._source_chain(
                    unit_of_work,
                    actor,
                    prepared,
                    lock_task=False,
                )
                if (
                    not self._equivalent(call, explanation, outcome)
                    or task.current_status
                    != self._expected_task_status(result, outcome)
                    or task.completed_at is None
                ):
                    raise ApplicationConflictError(
                        task_id=prepared.task_id
                    )
                return explanation
        except PersistenceError as error:
            raise ExplanationPersistenceError(
                task_id=prepared.task_id
            ) from error

    def prepare(
        self,
        actor: ActorContext,
        *,
        result_id: str,
    ) -> PreparedExplanation:
        explanation_id = self._explanation_id_factory()
        llm_call_id = self._llm_call_id_factory()
        created_at = self._clock()
        try:
            with self._unit_of_work_factory() as unit_of_work:
                result = unit_of_work.tool_results.get_owned(
                    result_id,
                    actor.actor_id,
                )
                if result is None:
                    raise ResourceNotFoundError()
                task = unit_of_work.tasks.get_owned(
                    result.task_id,
                    actor.actor_id,
                )
                if (
                    task is None
                    or task.selected_result_id != result.result_id
                    or task.selected_tool_run_id != result.tool_run_id
                ):
                    raise ApplicationConflictError(task_id=result.task_id)
                conversation = unit_of_work.conversations.get_owned(
                    task.conversation_id,
                    actor.actor_id,
                )
                tool_run = unit_of_work.tool_runs.get_owned(
                    result.tool_run_id,
                    actor.actor_id,
                )
                if (
                    conversation is None
                    or tool_run is None
                    or tool_run.task_id != result.task_id
                    or unit_of_work.explanations.get_for_result_attempt(
                        result.result_id,
                        1,
                    )
                    is not None
                ):
                    raise ApplicationConflictError(task_id=result.task_id)
                projection = self._projection(unit_of_work, result)
                call = LLMCall(
                    llm_call_id=llm_call_id,
                    task_id=task.task_id,
                    conversation_id=conversation.conversation_id,
                    request_id=tool_run.request_id,
                    purpose="TOOL_RESULT_EXPLANATION",
                    input_result_id=result.result_id,
                    provider=self._port.provider,
                    model_name=self._port.model_name,
                    prompt_template_id=self._port.prompt_template_id,
                    prompt_template_version=(
                        self._port.prompt_template_version
                    ),
                    prompt_digest=_prompt_digest(projection),
                    generation_parameters={
                        "temperature": 0,
                        "max_tokens": 512,
                    },
                    structured_output_summary=None,
                    usage=None,
                    provider_request_id=None,
                    status="PENDING",
                    created_at=created_at,
                    started_at=None,
                    completed_at=None,
                    duration_ms=None,
                    error_code=None,
                    safe_error_message=None,
                )
                explanation = NaturalLanguageExplanation(
                    explanation_id=explanation_id,
                    task_id=result.task_id,
                    result_id=result.result_id,
                    llm_call_id=call.llm_call_id,
                    attempt_no=1,
                    status="PENDING",
                    language="zh-CN",
                    text=None,
                    created_at=created_at,
                    started_at=None,
                    completed_at=None,
                    duration_ms=None,
                    error_code=None,
                    safe_error_message=None,
                )
                unit_of_work.llm_calls.add(call)
                unit_of_work.explanations.add(explanation)
                unit_of_work.commit()
        except PersistenceError as error:
            raise ExplanationPersistenceError(
                task_id=getattr(locals().get("result"), "task_id", None)
            ) from error

        try:
            with self._unit_of_work_factory() as unit_of_work:
                persisted_result = unit_of_work.tool_results.get_owned(
                    result_id,
                    actor.actor_id,
                )
                if persisted_result is None:
                    raise ExplanationPersistenceError()
                projection = self._projection(
                    unit_of_work,
                    persisted_result,
                )
        except PersistenceError as error:
            raise ExplanationPersistenceError(
                task_id=getattr(locals().get("result"), "task_id", None)
            ) from error
        return PreparedExplanation(
            explanation_id=explanation_id,
            llm_call_id=llm_call_id,
            task_id=persisted_result.task_id,
            result_id=result_id,
            projection=projection,
        )

    def _start(
        self,
        actor: ActorContext,
        prepared: PreparedExplanation,
    ) -> None:
        started_at = self._clock()
        needs_fresh_read = False
        try:
            with self._unit_of_work_factory() as unit_of_work:
                _, _, _, call, explanation = self._source_chain(
                    unit_of_work,
                    actor,
                    prepared,
                    lock_task=False,
                )
                if call.status == "RUNNING" and explanation.status == "RUNNING":
                    return
                if (
                    call.status != "PENDING"
                    or explanation.status != "PENDING"
                ):
                    return
                running_call = replace(
                    call,
                    status="RUNNING",
                    started_at=started_at,
                )
                running_explanation = replace(
                    explanation,
                    status="RUNNING",
                    started_at=started_at,
                )
                if (
                    unit_of_work.llm_calls.update(
                        running_call,
                        expected_status="PENDING",
                    )
                    is None
                    or unit_of_work.explanations.update(
                        running_explanation,
                        expected_status="PENDING",
                    )
                    is None
                ):
                    unit_of_work.rollback()
                    needs_fresh_read = True
                else:
                    unit_of_work.commit()
        except PersistenceError as error:
            raise ExplanationPersistenceError(
                task_id=prepared.task_id
            ) from error
        if needs_fresh_read:
            try:
                with self._unit_of_work_factory() as unit_of_work:
                    _, _, _, call, explanation = self._source_chain(
                        unit_of_work,
                        actor,
                        prepared,
                        lock_task=False,
                    )
                    if (
                        call.status == explanation.status
                        and call.status
                        in {"RUNNING", "SUCCEEDED", "FAILED"}
                    ):
                        return
            except PersistenceError as error:
                raise ExplanationPersistenceError(
                    task_id=prepared.task_id
                ) from error
            raise ApplicationConflictError(task_id=prepared.task_id)

    @staticmethod
    def _equivalent(
        call: LLMCall,
        explanation: NaturalLanguageExplanation,
        outcome: ExplanationOutcome,
    ) -> bool:
        expected_status = (
            "SUCCEEDED" if outcome.text is not None else "FAILED"
        )
        return (
            call.status == expected_status
            and explanation.status == expected_status
            and explanation.text == outcome.text
            and explanation.error_code == outcome.error_code
            and explanation.safe_error_message
            == outcome.safe_error_message
            and call.error_code == outcome.error_code
            and call.safe_error_message == outcome.safe_error_message
            and call.provider_request_id == outcome.provider_request_id
            and call.usage == outcome.usage
        )

    def finalize(
        self,
        actor: ActorContext,
        *,
        prepared: PreparedExplanation,
        outcome: ExplanationOutcome,
    ) -> NaturalLanguageExplanation:
        self._start(actor, prepared)
        completed_at = self._clock()
        needs_fresh_read = False
        try:
            with self._unit_of_work_factory() as unit_of_work:
                result, task, _, call, explanation = self._source_chain(
                    unit_of_work,
                    actor,
                    prepared,
                    lock_task=True,
                )
                if call.status in {"SUCCEEDED", "FAILED"} or explanation.status in {
                    "SUCCEEDED",
                    "FAILED",
                }:
                    if (
                        self._equivalent(call, explanation, outcome)
                        and task.current_status
                        == self._expected_task_status(result, outcome)
                        and task.completed_at is not None
                    ):
                        return explanation
                    raise ApplicationConflictError(
                        task_id=prepared.task_id
                    )
                if (
                    call.status != "RUNNING"
                    or explanation.status != "RUNNING"
                    or call.started_at is None
                    or explanation.started_at is None
                ):
                    raise ApplicationConflictError(
                        task_id=prepared.task_id
                    )

                status = (
                    "SUCCEEDED" if outcome.text is not None else "FAILED"
                )
                call_duration = max(
                    0,
                    int(
                        (
                            completed_at - call.started_at
                        ).total_seconds()
                        * 1000
                    ),
                )
                explanation_duration = max(
                    0,
                    int(
                        (
                            completed_at - explanation.started_at
                        ).total_seconds()
                        * 1000
                    ),
                )
                terminal_call = replace(
                    call,
                    usage=outcome.usage,
                    provider_request_id=outcome.provider_request_id,
                    status=status,
                    completed_at=completed_at,
                    duration_ms=call_duration,
                    error_code=outcome.error_code,
                    safe_error_message=outcome.safe_error_message,
                )
                terminal_explanation = replace(
                    explanation,
                    status=status,
                    text=outcome.text,
                    completed_at=completed_at,
                    duration_ms=explanation_duration,
                    error_code=outcome.error_code,
                    safe_error_message=outcome.safe_error_message,
                )
                if (
                    unit_of_work.llm_calls.update(
                        terminal_call,
                        expected_status="RUNNING",
                    )
                    is None
                    or unit_of_work.explanations.update(
                        terminal_explanation,
                        expected_status="RUNNING",
                    )
                    is None
                ):
                    unit_of_work.rollback()
                    needs_fresh_read = True
                elif result.status == "SUCCEEDED":
                    task_status = (
                        "SUCCEEDED"
                        if status == "SUCCEEDED"
                        else "PARTIALLY_SUCCEEDED"
                    )
                    terminal_task = replace(
                        task,
                        current_status=task_status,
                        updated_at=completed_at,
                        completed_at=completed_at,
                        error_code=outcome.error_code,
                        safe_error_message=outcome.safe_error_message,
                    )
                    if (
                        unit_of_work.tasks.update(
                            terminal_task,
                            expected_status="RUNNING",
                        )
                        is None
                    ):
                        unit_of_work.rollback()
                        needs_fresh_read = True
                elif (
                    result.status == "PARTIALLY_SUCCEEDED"
                    and task.current_status != "PARTIALLY_SUCCEEDED"
                ) or (
                    result.status == "FAILED"
                    and task.current_status != "FAILED"
                ):
                    raise ApplicationConflictError(
                        task_id=prepared.task_id
                    )
                if not needs_fresh_read:
                    unit_of_work.commit()
        except PersistenceError as error:
            try:
                with self._unit_of_work_factory() as unit_of_work:
                    unit_of_work.explanations.get(prepared.explanation_id)
                    unit_of_work.tasks.get(prepared.task_id)
            except PersistenceError:
                pass
            raise ExplanationPersistenceError(
                task_id=prepared.task_id
            ) from error
        if needs_fresh_read:
            return self._read_equivalent_terminal(
                actor,
                prepared,
                outcome,
            )

        return self._read_equivalent_terminal(
            actor,
            prepared,
            outcome,
        )

    def explain(
        self,
        actor: ActorContext,
        *,
        result_id: str,
    ) -> NaturalLanguageExplanation:
        prepared = self.prepare(actor, result_id=result_id)
        self._start(actor, prepared)
        try:
            outcome = self._port.explain(prepared.projection)
            if not isinstance(outcome, ExplanationOutcome):
                raise ExplanationProtocolError()
        except ExplanationTimeoutError:
            outcome = ExplanationOutcome(
                text=None,
                usage=None,
                provider_request_id=None,
                error_code="EXPLANATION_TIMEOUT",
                safe_error_message="Explanation generation timed out.",
            )
        except ExplanationProviderUnavailableError:
            outcome = ExplanationOutcome(
                text=None,
                usage=None,
                provider_request_id=None,
                error_code="EXPLANATION_PROVIDER_UNAVAILABLE",
                safe_error_message="Explanation provider is unavailable.",
            )
        except (ExplanationProtocolError, ValueError, TypeError):
            outcome = ExplanationOutcome(
                text=None,
                usage=None,
                provider_request_id=None,
                error_code="EXPLANATION_PROTOCOL_ERROR",
                safe_error_message=(
                    "Explanation provider returned an invalid response."
                ),
            )
        except Exception:
            outcome = ExplanationOutcome(
                text=None,
                usage=None,
                provider_request_id=None,
                error_code="EXPLANATION_FAILED",
                safe_error_message="Explanation generation failed.",
            )
        return self.finalize(actor, prepared=prepared, outcome=outcome)
