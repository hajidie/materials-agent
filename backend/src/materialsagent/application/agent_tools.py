"""Registry and Managed domain bridge; no Agent decisions inside executors."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import timezone
from typing import Any

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import ApplicationConflictError, ApplicationError, from_persistence_error
from materialsagent.application.tool_execution import ToolExecutionOutcomeError
from materialsagent.application.result_service import ResultPersistenceError
from materialsagent.domain.ports.unit_of_work import PersistenceError
from materialsagent.application.execution_deadline import tool_deadline, execution_owner
from materialsagent.application.tool_proposals import ResolvedToolInvocationProposal
from materialsagent.domain.models.agent import AgentRun, ArgumentDraft, ExecutionRecord, Observation, identifier, now, fingerprint
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.models.tool_invocation import ProposalOrigin, ToolInvocationProposal, InvocationStatus
from materialsagent.domain.ports.agent import AgentFailure
from materialsagent.domain.ports.tool_registry import InvalidNormalization, NeedsInputNormalization, ToolAction, ToolExecutionProfile


def plain(value):
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    return value


@dataclass(frozen=True)
class ManagedExecutionResult:
    task: Any
    tool_run: Any
    result: Any


class ManagedToolWorkflow:
    def __init__(self, uow_factory, execution, assets, results, *, clock=None):
        self.uow_factory, self.execution, self.assets, self.results = uow_factory, execution, assets, results
        self._unit_of_work_factory = uow_factory
        self._clock = clock or getattr(execution, "_clock", now)

    def execute(self, actor, *, task_id, task_input_revision_id, request_id):
        try:
            receipt = self.execution.execute_revision_with_output(actor, task_id=task_id,
                task_input_revision_id=task_input_revision_id, request_id=request_id)
        except ToolExecutionOutcomeError as error:
            self._select_failed_execution(actor, task_id=task_id, tool_run_id=error.tool_run_id,
                code=error.code, safe_message=str(error))
            raise
        return self._commit(actor, receipt, retry=False)

    def execute_reserved_retry(self, actor, *, tool_run_id):
        try:
            receipt = self.execution.execute_reserved_retry(actor, tool_run_id=tool_run_id)
        except ToolExecutionOutcomeError as error:
            self._select_failed_execution(actor, task_id=error.task_id, tool_run_id=error.tool_run_id,
                code=error.code, safe_message=str(error))
            raise
        if receipt is None:
            return None
        return self._commit(actor, receipt, retry=True)

    def _commit(self, actor, receipt, *, retry):
        task_id, tool_run_id = receipt.tool_run.task_id, receipt.tool_run.tool_run_id
        try:
            assets = self.assets.create_from_output(actor, task_id=task_id, tool_run_id=tool_run_id,
                output=receipt.output) if receipt.output.images else []
        except ApplicationError as error:
            self._terminalize_asset_failure(actor, task_id=task_id, tool_run_id=tool_run_id,
                code=error.code, safe_message=str(error))
            raise
        try:
            commit = self.results.commit_retry_result if retry else self.results.commit_initial_result
            commit(actor, receipt=receipt, assets=assets)
        except ResultPersistenceError as error:
            self._terminalize_result_failure(actor, task_id=task_id, tool_run_id=tool_run_id,
                code=error.code, safe_message=str(error))
            raise
        return self.load_current_for_task(actor, task_id=task_id)

    def load_current_for_task(self, actor, *, task_id):
        with self.uow_factory() as uow:
            task = uow.tasks.get_owned(task_id, actor.actor_id)
            if not task or not task.selected_result_id:
                raise AgentFailure("MANAGED_RESULT_MISSING")
            result = uow.tool_results.get_owned(task.selected_result_id, actor.actor_id)
            run = uow.tool_runs.get_owned(task.selected_tool_run_id, actor.actor_id)
            if not result or not run or result.tool_run_id != run.tool_run_id:
                raise AgentFailure("MANAGED_RESULT_MISMATCH")
            return ManagedExecutionResult(task, run, result)

    def _terminalize_result_failure(
        self,
        actor: ActorContext,
        *,
        task_id: str,
        tool_run_id: str,
        code: str,
        safe_message: str,
    ) -> None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned_for_update(
                    task_id,
                    actor.actor_id,
                )
                tool_run = unit_of_work.tool_runs.get_owned_for_update(
                    tool_run_id,
                    actor.actor_id,
                )
                if (
                    task is None
                    or tool_run is None
                    or task.task_type != "TOOL_EXECUTION"
                    or tool_run.task_id != task.task_id
                ):
                    raise ApplicationConflictError(task_id=task_id)

                existing_result = (
                    unit_of_work.tool_results.get_for_tool_run(
                        tool_run.tool_run_id
                    )
                )
                if existing_result is not None:
                    expected_task_status = existing_result.status
                    result_error_code = (
                        None
                        if existing_result.error is None
                        else str(existing_result.error["code"])
                    )
                    if (
                        existing_result.actor_id != actor.actor_id
                        or existing_result.task_id != task.task_id
                        or existing_result.tool_run_id
                        != tool_run.tool_run_id
                        or tool_run.current_status
                        != existing_result.status
                        or tuple(tool_run.requested_outputs)
                        != existing_result.requested_outputs
                        or tuple(tool_run.completed_outputs)
                        != existing_result.completed_outputs
                        or tuple(tool_run.failed_outputs)
                        != existing_result.failed_outputs
                        or tool_run.error_code != result_error_code
                        or task.current_status != expected_task_status
                        or task.selected_tool_run_id
                        != tool_run.tool_run_id
                        or task.selected_result_id
                        != existing_result.result_id
                        or task.error_code != result_error_code
                    ):
                        raise ApplicationConflictError(task_id=task_id)
                    return

                if (
                    task.current_status != "RUNNING"
                    or tool_run.current_status != "RUNNING"
                ):
                    raise ApplicationConflictError(task_id=task_id)

                completed_at = self._clock()
                if (
                    completed_at.tzinfo is None
                    or completed_at.utcoffset() is None
                    or completed_at.utcoffset()
                    != timezone.utc.utcoffset(completed_at)
                ):
                    raise ApplicationConflictError(task_id=task_id)
                failed_run = tool_run.complete_from_result(
                    completed_outputs=[],
                    failed_outputs=list(tool_run.requested_outputs),
                    completed_at=completed_at,
                    error_code=code,
                    safe_error_message=safe_message,
                )
                failed_task = replace(
                    task,
                    current_status="FAILED",
                    selected_tool_run_id=tool_run.tool_run_id,
                    selected_result_id=None,
                    updated_at=completed_at,
                    completed_at=completed_at,
                    error_code=code,
                    safe_error_message=safe_message,
                )
                if (
                    unit_of_work.tool_runs.update(
                        failed_run,
                        expected_status="RUNNING",
                    )
                    is None
                    or unit_of_work.tasks.update(
                        failed_task,
                        expected_status="RUNNING",
                    )
                    is None
                ):
                    raise ApplicationConflictError(task_id=task_id)
                unit_of_work.commit()
        except ApplicationError:
            raise
        except PersistenceError as error:
            raise ResultPersistenceError(task_id=task_id) from error

    def _terminalize_asset_failure(
        self,
        actor: ActorContext,
        *,
        task_id: str,
        tool_run_id: str,
        code: str,
        safe_message: str,
    ) -> None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned_for_update(
                    task_id,
                    actor.actor_id,
                )
                tool_run = unit_of_work.tool_runs.get_owned_for_update(
                    tool_run_id,
                    actor.actor_id,
                )
                if (
                    task is None
                    or tool_run is None
                    or task.current_status != "RUNNING"
                    or tool_run.current_status != "RUNNING"
                    or tool_run.task_id != task.task_id
                ):
                    raise ApplicationConflictError(task_id=task_id)
                completed_at = self._clock()
                if (
                    completed_at.tzinfo is None
                    or completed_at.utcoffset() is None
                    or completed_at.utcoffset()
                    != timezone.utc.utcoffset(completed_at)
                ):
                    raise ApplicationConflictError(task_id=task_id)
                failed_run = tool_run.complete_from_result(
                    completed_outputs=[],
                    failed_outputs=list(tool_run.requested_outputs),
                    completed_at=completed_at,
                    error_code=code,
                    safe_error_message=safe_message,
                )
                failed_task = replace(
                    task,
                    current_status="FAILED",
                    selected_tool_run_id=tool_run.tool_run_id,
                    selected_result_id=None,
                    updated_at=completed_at,
                    completed_at=completed_at,
                    error_code=code,
                    safe_error_message=safe_message,
                )
                if (
                    unit_of_work.tool_runs.update(
                        failed_run,
                        expected_status="RUNNING",
                    )
                    is None
                    or unit_of_work.tasks.update(
                        failed_task,
                        expected_status="RUNNING",
                    )
                    is None
                ):
                    raise ApplicationConflictError(task_id=task_id)
                unit_of_work.commit()
        except ApplicationError:
            raise
        except PersistenceError as error:
            raise from_persistence_error(error, task_id=task_id) from None

    def _select_failed_execution(
        self,
        actor: ActorContext,
        *,
        task_id: str,
        tool_run_id: str,
        code: str,
        safe_message: str,
    ) -> None:
        try:
            with self._unit_of_work_factory() as unit_of_work:
                task = unit_of_work.tasks.get_owned_for_update(
                    task_id,
                    actor.actor_id,
                )
                tool_run = unit_of_work.tool_runs.get_owned(
                    tool_run_id,
                    actor.actor_id,
                )
                if (
                    task is None
                    or tool_run is None
                    or task.current_status != "RUNNING"
                    or tool_run.current_status != "FAILED"
                    or tool_run.task_id != task.task_id
                    or tool_run.completed_at is None
                ):
                    raise ApplicationConflictError(task_id=task_id)
                failed_task = replace(
                    task,
                    current_status="FAILED",
                    selected_tool_run_id=tool_run.tool_run_id,
                    selected_result_id=None,
                    updated_at=tool_run.completed_at,
                    completed_at=tool_run.completed_at,
                    error_code=code,
                    safe_error_message=safe_message,
                )
                if (
                    unit_of_work.tasks.update(
                        failed_task,
                        expected_status="RUNNING",
                    )
                    is None
                ):
                    raise ApplicationConflictError(task_id=task_id)
                unit_of_work.commit()
        except ApplicationError:
            raise
        except PersistenceError as error:
            raise from_persistence_error(error, task_id=task_id) from None



class ToolArgResolver:
    """Initial candidates and merged resume deltas use this deterministic fact authority.

    Incremental language extraction is budgeted once by the Runtime before calling resolve.
    """
    def __init__(self, registry, uow_factory, clock):
        self.registry, self.uow_factory, self.clock = registry, uow_factory, clock

    def resolve(self, run, tool_name, arguments):
        registration = self.registry.resolve(tool_name)
        previous = run.draft
        if previous and (previous.tool_name != tool_name or previous.schema_hash != registration.schema_hash):
            raise AgentFailure("BOUND_TOOL_MISMATCH")
        self.registry.authorize(registration=registration, action=ToolAction.SUPPLEMENT if previous else ToolAction.NEW_BINDING,
            bound_ref=registration.ref if previous else None)
        merged = {**(previous.arguments if previous else {}), **arguments}
        properties = plain(registration.definition.proposal_schema).get("properties", {})
        if not set(arguments) <= set(properties):
            raise AgentFailure("TOOL_ARGUMENT_FIELD_INVALID")
        issues = {}
        normalized = dict(merged)
        normalizer = registration.binding.normalizer
        if normalizer:
            value = normalizer(arguments, previous.normalized if previous else None)
            normalized = plain(value.normalized_input)
            if isinstance(value, NeedsInputNormalization):
                issues.update({field: "Missing" for field in value.missing_fields})
                issues.update({field: "Ambiguous" for field in value.ambiguous_fields})
            elif isinstance(value, InvalidNormalization):
                for error in value.validation_errors:
                    field = error.get("field")
                    if field not in properties:
                        raise AgentFailure("TOOL_VALIDATION_PROTOCOL_ERROR")
                    issues[field] = "Invalid"
                for field, val in normalized.items():
                    if val is None and field not in issues:
                        issues[field] = "Missing"
        else:
            schema = plain(registration.definition.input_schema)
            for field in schema.get("required", []):
                if field not in merged or merged[field] is None:
                    issues[field] = "Missing"
            for field, val in merged.items():
                if isinstance(val, dict) and "candidates" in val:
                    issues[field] = "Ambiguous"
            if not issues:
                try:
                    normalized = plain(registration.binding.validator(merged))
                except (TypeError, ValueError):
                    issues.update({field: "Invalid" for field in merged})
        draft = ArgumentDraft(tool_name=tool_name, version=previous.version if previous else registration.version,
            schema_hash=registration.schema_hash, arguments=merged, normalized=normalized, issues=issues,
            task_id=previous.task_id if previous else None, revision_id=previous.revision_id if previous else None,
            resolver_authoritative=True)
        if registration.execution_profile is ToolExecutionProfile.MANAGED:
            if run.retry_execution:
                with self.uow_factory() as uow:
                    task = uow.tasks.get_owned(run.retry_execution.task_id, run.actor_id)
                    if task is None or task.bound_tool_ref != registration.ref:
                        raise AgentFailure("TOOL_RETRY_SOURCE_MISMATCH")
                    revisions = uow.task_input_revisions.list_for_task(task.task_id)
                    revision = max(revisions, key=lambda item: item.revision)
                    draft.task_id, draft.revision_id = task.task_id, revision.task_input_revision_id
            # A provider may restate defaults after resolution. An unchanged READY
            # input reuses its revision instead of attempting READY -> READY.
            elif not (previous and previous.normalized == draft.normalized and previous.issues == draft.issues
                    and (previous.arguments == draft.arguments or not draft.issues)):
                self._save_revision(run, draft, registration)
        return draft

    def _save_revision(self, run, draft, registration):
        at = self.clock()
        with self.uow_factory() as uow:
            task = uow.tasks.get_owned(draft.task_id, run.actor_id) if draft.task_id else None
            if draft.task_id and task is None:
                raise AgentFailure("MANAGED_TASK_NOT_FOUND")
            status = "NEEDS_INPUT" if draft.issues else "READY"
            if task is None:
                task = Task(task_id=identifier(), conversation_id=run.conversation_id, actor_id=run.actor_id,
                    task_type="TOOL_EXECUTION", current_status=status, selected_tool_run_id=None, selected_result_id=None,
                    created_at=at, started_at=None, updated_at=at, completed_at=None, error_code=None, safe_error_message=None,
                    tool_id=registration.tool_id, bound_tool_version=registration.version, bound_schema_hash=registration.schema_hash)
                uow.tasks.add(task)
                revision_number = 1
            else:
                if task.current_status not in {"NEEDS_INPUT", "READY"}:
                    raise AgentFailure("MANAGED_TASK_NOT_EDITABLE")
                previous_status = task.current_status
                task = replace(task, current_status=status, updated_at=at)
                if uow.tasks.update(task, expected_status=previous_status) is None:
                    raise AgentFailure("MANAGED_TASK_CONFLICT")
                revisions = uow.task_input_revisions.list_for_task(task.task_id)
                revision_number = max((r.revision for r in revisions), default=0) + 1
            revision = TaskInputRevision(task_input_revision_id=identifier(), task_id=task.task_id,
                request_id=run.agent_run_id, source_llm_call_id=None, source_message_ids=[run.source_message_id],
                revision=revision_number, raw_input=draft.arguments, normalized_input=draft.normalized,
                missing_fields=[f for f, issue in draft.issues.items() if issue == "Missing"],
                ambiguous_fields=[{"field": f, "candidates": []} for f, issue in draft.issues.items() if issue in {"Ambiguous", "Conflict"}],
                validation_errors=[{"field": f, "code": "INVALID_VALUE"} for f, issue in draft.issues.items() if issue == "Invalid"], created_at=at)
            uow.task_input_revisions.add(revision)
            uow.commit()
            draft.task_id, draft.revision_id = task.task_id, revision.task_input_revision_id


class RegistryAgentGateway:
    def __init__(self, registry, uow_factory, invocations, managed_workflow, result_query):
        self.registry, self.uow_factory = registry, uow_factory
        self.invocations, self.workflow, self.result_query = invocations, managed_workflow, result_query
        self.arg_resolver = ToolArgResolver(registry, uow_factory, invocations._clock)

    def catalog(self):
        result = []
        for entry in self.registry.routing_snapshot().entries:
            registration = self.registry.resolve(entry.tool_id)
            result.append({"tool_name": entry.tool_id, "description": entry.description,
                "schema": plain(entry.candidate_input_schema),
                "execution_profile": registration.execution_profile.value})
        return result

    def resolve(self, run, tool_name, arguments):
        return self.arg_resolver.resolve(run, tool_name, arguments)

    def prepare(self, run, record):
        if run.tool_execution_disabled:
            raise AgentFailure("TOOL_EXECUTION_DISABLED")
        registration = self.registry.resolve(record.tool_name)
        if registration.schema_hash != record.schema_hash or registration.version != record.version:
            raise AgentFailure("TOOL_SCHEMA_DRIFT")
        actor = ActorContext(actor_id=run.actor_id, user_id=None)
        if registration.execution_profile is ToolExecutionProfile.MANAGED:
            if self.workflow is None or run.draft is None:
                raise AgentFailure("RUNTIME_UNAVAILABLE")
            if run.retry_execution:
                reserved = self.workflow.execution.reserve_retry_attempt(actor, task_id=run.draft.task_id,
                    request_id=run.agent_run_id, idempotency_key="agent-action:" + record.action_id,
                    request_digest=record.execution_fingerprint)
                public = self.invocations.execute_managed_retry(actor, task_id=run.draft.task_id,
                    tool_run_id=reserved.tool_run.tool_run_id, source_message_id=run.source_message_id,
                    retry_of_invocation_run_id=record.retry_of_invocation_run_id,
                    request_id=run.agent_run_id, idempotency_key="agent-action:" + record.action_id,
                    workflow_service=self.workflow, defer_execution=True)
            else:
                public, _ = self.invocations.execute_managed(actor, registration=registration,
                    task_id=run.draft.task_id, source_message_id=run.source_message_id,
                    task_input_revision_id=run.draft.revision_id, proposed_arguments=record.arguments,
                    request_id=run.agent_run_id, idempotency_key="agent-action:" + record.action_id,
                    workflow_service=self.workflow, defer_execution=True)
        else:
            proposal = ToolInvocationProposal(conversation_id=run.conversation_id, source_message_id=run.source_message_id,
                llm_call_id=record.action_id, model_tool_name=record.tool_name, proposed_arguments=record.arguments, origin=ProposalOrigin.STRUCTURED)
            public = self.invocations.create_from_proposal(actor, ResolvedToolInvocationProposal(proposal, registration),
                request_id=run.agent_run_id, idempotency_key="agent-action:" + record.action_id, defer_execution=True)
        if record.retry_of_invocation_run_id and registration.execution_profile is not ToolExecutionProfile.MANAGED:
            with self.uow_factory() as uow:
                current = uow.invocation_runs.get_owned(public.run.invocation_run_id, run.actor_id)
                updated = replace(current, retry_of_invocation_run_id=record.retry_of_invocation_run_id)
                if uow.invocation_runs.update(updated, expected_status=current.status) is None:
                    raise AgentFailure("TOOL_RETRY_CONFLICT")
                uow.commit()
        record.invocation_run_id, record.status = public.run.invocation_run_id, public.run.status.value
        record.confirmation_expires_at = public.run.confirmation_expires_at
        if record.status in {"DENIED", "FAILED"}:
            raise AgentFailure("TOOL_EXECUTION_DENIED")
        return record

    def execute(self, run, record, timeout):
        if run.tool_execution_disabled:
            raise AgentFailure("TOOL_EXECUTION_DISABLED")
        actor = ActorContext(actor_id=run.actor_id, user_id=None)
        with tool_deadline(timeout), execution_owner(run.agent_run_id, run.version, run.claim):
            registration = self.registry.resolve(record.tool_name)
            if registration.execution_profile is ToolExecutionProfile.MANAGED:
                self.invocations._drive_managed(actor, record.invocation_run_id, workflow_service=self.workflow,
                    task_input_revision_id=run.draft.revision_id)
            else:
                self.invocations._drive_pending(actor, record.invocation_run_id)
        observation = self.repair(run, record)
        if observation is None:
            raise AgentFailure("OBSERVATION_INCONSISTENT")
        return observation

    def confirm(self, run, record, approved):
        actor = ActorContext(actor_id=run.actor_id, user_id=None)
        # Confirmation commits only permission; the Runtime owns subsequent dispatch.
        if approved:
            self.invocations.confirm(actor, record.invocation_run_id, defer_execution=True)
        else:
            self.invocations.reject(actor, record.invocation_run_id)

    def repair(self, run, record):
        def tool_observation(**values):
            return Observation(observation_id=fingerprint([run.agent_run_id, record.action_id, record.invocation_run_id]), **values)
        actor = ActorContext(actor_id=run.actor_id, user_id=None)
        with self.uow_factory() as uow:
            invocation = uow.invocation_runs.get_owned(record.invocation_run_id, run.actor_id)
            if not invocation or invocation.conversation_id != run.conversation_id:
                return None
            if invocation.execution_profile is not ToolExecutionProfile.MANAGED:
                result = uow.invocation_results.get(invocation.invocation_result_id) if invocation.invocation_result_id else None
                if result:
                    return tool_observation(step_id=record.action_id, kind="TOOL_RESULT", status="SUCCEEDED", tool_name=record.tool_name,
                        invocation_run_id=invocation.invocation_run_id, data=plain(result.data), presentation=plain(result.presentation))
                if invocation.status in {InvocationStatus.FAILED, InvocationStatus.OUTCOME_UNKNOWN}:
                    return tool_observation(step_id=record.action_id, kind="TOOL_RESULT", status="FAILED", tool_name=record.tool_name,
                        invocation_run_id=invocation.invocation_run_id, error={"code": invocation.error_code,
                        "retryable": invocation.status is InvocationStatus.FAILED})
                return None
            task = uow.tasks.get_owned(invocation.task_id, run.actor_id)
            result = uow.tool_results.get_owned(task.selected_result_id, run.actor_id) if task and task.selected_result_id else None
            if not result:
                if invocation.status is InvocationStatus.FAILED:
                    return tool_observation(step_id=record.action_id, kind="TOOL_RESULT", status="FAILED", tool_name=record.tool_name,
                        invocation_run_id=invocation.invocation_run_id, task_id=invocation.task_id,
                        tool_run_id=task.selected_tool_run_id if task else None,
                        error={"code": invocation.error_code, "retryable": bool(task and
                            task.current_status == "FAILED" and task.error_code in {"RUNTIME_BUSY", "RUNTIME_NOT_READY", "MODEL_LOAD_FAILED",
                                "SEM_GENERATION_FAILED", "MECHANICAL_PROPERTY_PREDICTION_FAILED"})})
                return None
            if result.task_id != invocation.task_id or (invocation.managed_tool_run_id and result.tool_run_id != invocation.managed_tool_run_id):
                raise AgentFailure("OBSERVATION_SOURCE_MISMATCH")
            result_id = result.result_id
            if invocation.status is InvocationStatus.RUNNING:
                repaired = invocation.transition(
                    InvocationStatus.FAILED if result.status == "FAILED" else InvocationStatus.SUCCEEDED,
                    now=self.invocations._clock(), completed_at=self.invocations._clock(),
                    managed_tool_run_id=result.tool_run_id,
                    error_code=result.error.get("code") if result.status == "FAILED" and result.error else None,
                    safe_error_message="Managed Tool failed." if result.status == "FAILED" else None)
                if uow.invocation_runs.update(repaired, expected_status=invocation.status,
                        expected_claim_token=invocation.execution_claim_token) is None:
                    raise AgentFailure("OBSERVATION_INCONSISTENT")
                with execution_owner(run.agent_run_id, run.version, run.claim):
                    uow.commit()
        projection = self.result_query.get(actor, result_id)
        artifacts = [{field: getattr(a, field) for field in a.__dataclass_fields__} for a in projection.artifacts]
        summary = {field: plain(getattr(result, field)) for field in (
            "result_id", "tool_run_id", "status", "requested_outputs", "completed_outputs", "failed_outputs",
            "data", "warnings", "provenance", "error", "tool_id", "tool_version", "schema_hash")}
        summary["created_at"] = result.created_at.isoformat()
        presenter = self.registry.resolve(record.tool_name).binding.presenter
        presentation = plain(presenter(summary)) if presenter else {}
        return tool_observation(step_id=record.action_id, kind="TOOL_RESULT", status=result.status, tool_name=record.tool_name,
            invocation_run_id=invocation.invocation_run_id, task_id=result.task_id, tool_run_id=result.tool_run_id,
            result_id=result.result_id, data=plain(result.data), artifacts=artifacts, result_summary=summary, presentation=presentation,
            warnings=plain(result.warnings), error=plain(result.error))
