from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol
from uuid import uuid4

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationConflictError,
    ApplicationInternalError,
    ApplicationValidationError,
    ResourceNotFoundError,
)
from materialsagent.application.tool_projections import ToolProjectionService
from materialsagent.application.tool_proposals import (
    ResolvedToolInvocationProposal,
)
from materialsagent.application.tool_registry import ToolRegistry, UnknownToolError
from materialsagent.domain.models.tool_invocation import (
    InvocationResult,
    InvocationRun,
    InvocationStatus,
    InvocationTrigger,
    TERMINAL_INVOCATION_STATUSES,
)
from materialsagent.domain.ports.tool_authorization import (
    EmptyPermissionAuthorizationService,
    ToolAuthorizationPort,
    ToolAuthorizationRequest,
)
from materialsagent.domain.ports.tool_registry import (
    RegisteredTool,
    ToolExecutionProfile,
)
from materialsagent.domain.ports.unit_of_work import PersistenceError, UnitOfWorkFactory


Clock = Callable[[], datetime]
IdFactory = Callable[[str], str]


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _id_factory(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


@dataclass(frozen=True, slots=True)
class ToolExecutorContext:
    invocation_run_id: str
    request_id: str
    idempotency_key: str
    actor_id: str
    conversation_id: str
    execution_claim_token: str


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    data: Mapping[str, object]
    presentation: Mapping[str, object]


class ToolExecutor(Protocol):
    executor_id: str
    supported_profiles: frozenset[ToolExecutionProfile]

    def execute(
        self,
        registration: RegisteredTool,
        arguments: Mapping[str, object],
        context: ToolExecutorContext,
    ) -> ToolExecutionResult: ...


class HTTPExecutorContract(ToolExecutor, Protocol):
    """Reserved contract only; the first release has no HTTP implementation."""

    executor_id: Literal["http"]


class MCPExecutorContract(ToolExecutor, Protocol):
    """Reserved contract only; the first release has no MCP implementation."""

    executor_id: Literal["mcp"]


class StandardSyncExecutor:
    executor_id = "standard_sync"
    supported_profiles = frozenset(
        {ToolExecutionProfile.STANDARD, ToolExecutionProfile.SIDE_EFFECT}
    )

    def execute(
        self,
        registration: RegisteredTool,
        arguments: Mapping[str, object],
        context: ToolExecutorContext,
    ) -> ToolExecutionResult:
        validator = registration.binding.validator
        if validator is None:
            raise ValueError("Tool input validator is unavailable.")
        validated = validator(arguments)
        target = registration.binding.execution_target
        invoke_with_context = getattr(target, "invoke_with_context", None)
        invoke = getattr(target, "invoke", None)
        def call_target():
            if callable(invoke_with_context):
                return invoke_with_context(dict(validated), context)
            if callable(invoke):
                return invoke(dict(validated))
            raise ValueError("Synchronous execution target is invalid.")
        from concurrent.futures import ThreadPoolExecutor
        from contextvars import copy_context
        from materialsagent.application.execution_deadline import remaining_timeout
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-call")
        try:
            future = executor.submit(copy_context().run, call_target)
            raw_result = future.result(timeout=remaining_timeout(10))
        finally:
            # Timeout never claims that a non-cooperative target was forcibly cancelled.
            executor.shutdown(wait=False, cancel_futures=True)
        codec = registration.binding.codec
        presenter = registration.binding.presenter
        if codec is None or presenter is None:
            raise ValueError("Tool result binding is incomplete.")
        data = codec(raw_result)
        presentation = presenter(data)
        return ToolExecutionResult(data=data, presentation=presentation)


class ManagedExecutor:
    """Router-owned marker for the existing strict Managed ToolWorkflow."""

    executor_id = "managed_runtime"
    supported_profiles = frozenset({ToolExecutionProfile.MANAGED})

    def execute(
        self,
        registration: RegisteredTool,
        arguments: Mapping[str, object],
        context: ToolExecutorContext,
    ) -> ToolExecutionResult:
        del registration, arguments, context
        raise ValueError("Managed execution requires the strict ToolWorkflow context.")

    @staticmethod
    def execute_workflow(
        workflow_service: object,
        actor: ActorContext,
        *,
        task_id: str,
        task_input_revision_id: str,
        request_id: str,
    ) -> object:
        execute = getattr(workflow_service, "execute", None)
        if not callable(execute):
            raise ValueError("Managed ToolWorkflow is unavailable.")
        return execute(
            actor,
            task_id=task_id,
            task_input_revision_id=task_input_revision_id,
            request_id=request_id,
        )

    @staticmethod
    def execute_retry(
        workflow_service: object,
        actor: ActorContext,
        *,
        tool_run_id: str,
    ) -> object:
        execute = getattr(workflow_service, "execute_reserved_retry", None)
        if not callable(execute):
            raise ValueError("Managed Tool retry workflow is unavailable.")
        return execute(actor, tool_run_id=tool_run_id)


class ExecutorRouter:
    """Owns stateless Executor instances; Tool bindings never do."""

    def __init__(self, executors: tuple[ToolExecutor, ...]) -> None:
        values: dict[str, ToolExecutor] = {}
        for executor in executors:
            if executor.executor_id in values:
                raise ValueError("Duplicate executor_id.")
            values[executor.executor_id] = executor
        self._executors = values

    def validate_registration(self, registration: RegisteredTool) -> None:
        executor = self._executors.get(registration.executor_id)
        if executor is None:
            raise ValueError("Registered Tool references an unknown executor_id.")
        if registration.execution_profile not in executor.supported_profiles:
            raise ValueError("Registered Tool execution profile is incompatible.")

    def execute(
        self,
        registration: RegisteredTool,
        arguments: Mapping[str, object],
        context: ToolExecutorContext,
    ) -> ToolExecutionResult:
        try:
            executor = self._executors[registration.executor_id]
        except KeyError:
            raise ValueError("Registered Tool references an unknown executor_id.") from None
        return executor.execute(registration, arguments, context)

    def execute_managed(
        self,
        workflow_service: object,
        actor: ActorContext,
        *,
        task_id: str,
        task_input_revision_id: str,
        request_id: str,
    ) -> object:
        executor = self._executors.get("managed_runtime")
        execute_workflow = getattr(executor, "execute_workflow", None)
        if not callable(execute_workflow):
            raise ValueError("Managed executor is unavailable.")
        return execute_workflow(
            workflow_service,
            actor,
            task_id=task_id,
            task_input_revision_id=task_input_revision_id,
            request_id=request_id,
        )

    def execute_managed_retry(
        self,
        workflow_service: object,
        actor: ActorContext,
        *,
        tool_run_id: str,
    ) -> object:
        executor = self._executors.get("managed_runtime")
        execute_retry = getattr(executor, "execute_retry", None)
        if not callable(execute_retry):
            raise ValueError("Managed retry executor is unavailable.")
        return execute_retry(
            workflow_service,
            actor,
            tool_run_id=tool_run_id,
        )


@dataclass(frozen=True, slots=True)
class PublicInvocation:
    run: InvocationRun
    result: InvocationResult | None
    tool: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "invocation_run_id": self.run.invocation_run_id,
            "conversation_id": self.run.conversation_id,
            "source_message_id": self.run.source_message_id,
            "task_id": self.run.task_id,
            "status": self.run.status.value,
            "tool": _plain_json(self.tool),
            "confirmation_required": self.run.confirmation_required,
            "confirmation_expires_at": _utc_text(self.run.confirmation_expires_at),
            "confirmed_at": _utc_text(self.run.confirmed_at),
            "rejected_at": _utc_text(self.run.rejected_at),
            "expired_at": _utc_text(self.run.expired_at),
            "dispatch_started_at": _utc_text(self.run.dispatch_started_at),
            "error_code": self.run.error_code,
            "safe_error_message": self.run.safe_error_message,
            "result": (
                None
                if self.result is None
                else {
                    "data": _plain_json(self.result.data),
                    "presentation": _plain_json(self.result.presentation),
                }
            ),
            "created_at": _utc_text(self.run.created_at),
            "updated_at": _utc_text(self.run.updated_at),
            "completed_at": _utc_text(self.run.completed_at),
        }


def _plain_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


def _utc_text(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat().replace("+00:00", "Z")


class InvocationService:
    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        registry: ToolRegistry,
        executor_router: ExecutorRouter,
        *,
        authorization: ToolAuthorizationPort | None = None,
        clock: Clock | None = None,
        id_factory: IdFactory | None = None,
        lease_seconds: int = 60,
        managed_workflow_service: object | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._registry = registry
        self._router = executor_router
        self._authorization = authorization or EmptyPermissionAuthorizationService()
        self._clock = clock or _clock
        self._id_factory = id_factory or _id_factory
        self._lease_seconds = lease_seconds
        self._managed_workflow_service = managed_workflow_service
        for registration in registry.list_registered():
            self._router.validate_registration(registration)

    def create_from_proposal(
        self,
        actor: ActorContext,
        resolved: ResolvedToolInvocationProposal,
        *,
        request_id: str,
        idempotency_key: str,
        defer_execution: bool = False,
    ) -> PublicInvocation:
        registration = resolved.registration
        if registration.execution_profile is ToolExecutionProfile.MANAGED:
            raise ApplicationValidationError()
        validator = registration.binding.validator
        if validator is None:
            raise ApplicationInternalError()
        try:
            arguments = validator(resolved.proposal.proposed_arguments)
        except ValueError:
            raise ApplicationValidationError() from None
        now = self._clock()
        policy = registration.definition.tool_execution_policy
        expires_at = (
            now + timedelta(seconds=policy.confirmation_ttl_seconds)
            if policy.confirmation_required
            else None
        )
        run = InvocationRun(
            invocation_run_id=self._id_factory("inv"),
            actor_id=actor.actor_id,
            conversation_id=resolved.proposal.conversation_id,
            source_message_id=resolved.proposal.source_message_id,
            task_id=None,
            retry_of_invocation_run_id=None,
            request_id=request_id,
            idempotency_key=idempotency_key,
            trigger=InvocationTrigger.MESSAGE_PROPOSAL,
            tool_id=registration.tool_id,
            tool_version=registration.version,
            schema_hash=registration.schema_hash,
            execution_profile=registration.execution_profile,
            executor_id=registration.executor_id,
            proposed_arguments=arguments,
            policy_snapshot={
                "lifecycle_policy": registration.execution_policy.value,
                "required_permissions": list(policy.required_permissions),
                "confirmation_required": policy.confirmation_required,
                "confirmation_ttl_seconds": policy.confirmation_ttl_seconds,
                "idempotency_required": policy.idempotency_required,
                "audit_required": policy.audit_required,
            },
            tool_projection=ToolProjectionService.for_invocation(
                registration.definition
            ).to_dict(),
            status=InvocationStatus.PENDING,
            confirmation_required=policy.confirmation_required,
            confirmation_expires_at=expires_at,
            confirmed_at=None,
            confirmed_by=None,
            rejected_at=None,
            rejected_by=None,
            expired_at=None,
            authorization_checked_at=None,
            denied_at=None,
            execution_claim_token=None,
            execution_lease_expires_at=None,
            dispatch_started_at=None,
            execution_attempt_count=0,
            invocation_result_id=None,
            managed_tool_run_id=None,
            error_code=None,
            safe_error_message=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        try:
            with self._unit_of_work_factory() as uow:
                conversation = uow.conversations.get_owned(run.conversation_id, actor.actor_id)
                message = uow.messages.get(run.source_message_id)
                if (
                    conversation is None
                    or message is None
                    or message.actor_id != actor.actor_id
                    or message.conversation_id != run.conversation_id
                    or message.role != "USER"
                ):
                    raise ResourceNotFoundError()
                existing = uow.invocation_runs.get_by_idempotency(actor.actor_id, idempotency_key)
                if existing is not None:
                    existing_id = existing.invocation_run_id
                else:
                    existing_id = None
                    uow.invocation_runs.add(run)
                    uow.commit()
        except PersistenceError:
            with self._unit_of_work_factory() as uow:
                existing = uow.invocation_runs.get_by_idempotency(actor.actor_id, idempotency_key)
                if existing is None:
                    raise
                existing_id = existing.invocation_run_id
        if existing_id is not None:
            return self.get(actor, existing_id)
        run = self._authorize_initial(actor, run.invocation_run_id)
        if run.status is InvocationStatus.PENDING and not defer_execution:
            run = self._drive_pending(actor, run.invocation_run_id)
        return self.get(actor, run.invocation_run_id)

    def execute_managed(
        self,
        actor: ActorContext,
        *,
        registration: RegisteredTool,
        task_id: str,
        source_message_id: str,
        task_input_revision_id: str,
        proposed_arguments: Mapping[str, object],
        request_id: str,
        idempotency_key: str,
        workflow_service: object,
        defer_execution: bool = False,
    ) -> tuple[PublicInvocation, object]:
        if registration.execution_profile is not ToolExecutionProfile.MANAGED:
            raise ApplicationValidationError()
        now = self._clock()
        policy = registration.definition.tool_execution_policy
        run = InvocationRun(
            invocation_run_id=self._id_factory("inv"),
            actor_id=actor.actor_id,
            conversation_id=self._task_conversation(actor, task_id),
            source_message_id=source_message_id,
            task_id=task_id,
            retry_of_invocation_run_id=None,
            request_id=request_id,
            idempotency_key=idempotency_key,
            trigger=InvocationTrigger.MESSAGE_PROPOSAL,
            tool_id=registration.tool_id,
            tool_version=registration.version,
            schema_hash=registration.schema_hash,
            execution_profile=ToolExecutionProfile.MANAGED,
            executor_id=registration.executor_id,
            proposed_arguments=proposed_arguments,
            policy_snapshot={
                "lifecycle_policy": registration.execution_policy.value,
                "required_permissions": list(policy.required_permissions),
                "confirmation_required": False,
                "confirmation_ttl_seconds": policy.confirmation_ttl_seconds,
                "idempotency_required": policy.idempotency_required,
                "audit_required": policy.audit_required,
            },
            tool_projection=ToolProjectionService.for_invocation(
                registration.definition
            ).to_dict(),
            status=InvocationStatus.PENDING,
            confirmation_required=False,
            confirmation_expires_at=None,
            confirmed_at=None,
            confirmed_by=None,
            rejected_at=None,
            rejected_by=None,
            expired_at=None,
            authorization_checked_at=None,
            denied_at=None,
            execution_claim_token=None,
            execution_lease_expires_at=None,
            dispatch_started_at=None,
            execution_attempt_count=0,
            invocation_result_id=None,
            managed_tool_run_id=None,
            error_code=None,
            safe_error_message=None,
            created_at=now,
            updated_at=now,
            completed_at=None,
        )
        existing_id: str | None = None
        with self._unit_of_work_factory() as uow:
            message = uow.messages.get(source_message_id)
            task = uow.tasks.get_owned(task_id, actor.actor_id)
            if (
                message is None
                or task is None
                or message.task_id not in (None, task_id)
                or message.conversation_id != task.conversation_id
                or task.bound_tool_ref != registration.ref
            ):
                raise ResourceNotFoundError()
            existing = uow.invocation_runs.get_by_idempotency(actor.actor_id, idempotency_key)
            if existing is not None:
                if (
                    existing.task_id != task_id
                    or existing.tool_ref != registration.ref
                ):
                    raise ApplicationConflictError()
                existing_id = existing.invocation_run_id
            else:
                uow.invocation_runs.add(run)
                uow.commit()

        run_id = existing_id or run.invocation_run_id
        if defer_execution:
            with self._unit_of_work_factory() as uow:
                saved = uow.invocation_runs.get_owned(run_id, actor.actor_id)
                return self._public(uow, saved), None
        workflow = self._drive_managed(
            actor,
            run_id,
            workflow_service=workflow_service,
            task_input_revision_id=task_input_revision_id,
        )
        public = self.get(actor, run_id)
        if public.run.status is not InvocationStatus.SUCCEEDED:
            raise ApplicationConflictError()
        if workflow is None:
            workflow = workflow_service.load_current_for_task(
                actor,
                task_id=task_id,
            )
        return public, workflow

    def _drive_managed(
        self,
        actor: ActorContext,
        invocation_run_id: str,
        *,
        workflow_service: object,
        task_input_revision_id: str | None = None,
    ) -> object | None:
        with self._unit_of_work_factory() as uow:
            run = uow.invocation_runs.get_owned(invocation_run_id, actor.actor_id)
            if run is None:
                raise ResourceNotFoundError()
        if run.status in TERMINAL_INVOCATION_STATUSES:
            return None
        if run.execution_profile is not ToolExecutionProfile.MANAGED:
            raise ApplicationConflictError()
        if run.status is not InvocationStatus.PENDING:
            return None
        registration = self._current_registration(run)
        policy = registration.definition.tool_execution_policy
        decision = self._authorization.authorize(
            ToolAuthorizationRequest(
                actor_id=actor.actor_id,
                user_id=actor.user_id,
                tool_ref=registration.ref,
                required_permissions=policy.required_permissions,
                action="EXECUTE",
            )
        )
        claim_token = self._id_factory("claim")
        claimed_at = self._clock()
        with self._unit_of_work_factory() as uow:
            current = uow.invocation_runs.get_owned_for_update(
                invocation_run_id,
                actor.actor_id,
            )
            if current is None:
                raise ResourceNotFoundError()
            if not decision.allowed:
                if current.status is InvocationStatus.PENDING:
                    denied = current.transition(
                        InvocationStatus.DENIED,
                        now=claimed_at,
                        authorization_checked_at=claimed_at,
                        denied_at=claimed_at,
                        completed_at=claimed_at,
                        error_code=decision.reason_code,
                        safe_error_message="Tool authorization was denied.",
                    )
                else:
                    return None
                saved = uow.invocation_runs.update(
                    denied,
                    expected_status=current.status,
                    expected_claim_token=(
                        current.execution_claim_token
                        if current.status is InvocationStatus.RUNNING
                        else None
                    ),
                )
                if saved is None:
                    return None
                uow.commit()
                return None
            if current.status is InvocationStatus.PENDING:
                running = current.transition(
                    InvocationStatus.RUNNING,
                    now=claimed_at,
                    authorization_checked_at=claimed_at,
                    execution_claim_token=claim_token,
                    execution_lease_expires_at=(
                        claimed_at + timedelta(seconds=self._lease_seconds)
                    ),
                    execution_attempt_count=current.execution_attempt_count + 1,
                )
            else:
                return None
            saved = uow.invocation_runs.update(
                running,
                expected_status=current.status,
                expected_claim_token=(
                    current.execution_claim_token
                    if current.status is InvocationStatus.RUNNING
                    else None
                ),
            )
            if saved is None:
                return None
            uow.commit()
        if saved.trigger is InvocationTrigger.EXPLICIT_RETRY:
            if saved.managed_tool_run_id is None:
                raise ApplicationConflictError()
            return self._dispatch_managed_retry(
                actor,
                saved,
                workflow_service=workflow_service,
            )
        if task_input_revision_id is None:
            task_input_revision_id = self._managed_revision_id(actor, saved)
        return self._dispatch_managed(
            actor,
            saved,
            workflow_service=workflow_service,
            task_input_revision_id=task_input_revision_id,
        )

    def _dispatch_managed(
        self,
        actor: ActorContext,
        run: InvocationRun,
        *,
        workflow_service: object,
        task_input_revision_id: str,
    ) -> object:
        dispatch_at = self._clock()
        dispatched = replace(run, dispatch_started_at=dispatch_at, updated_at=dispatch_at)
        with self._unit_of_work_factory() as uow:
            persisted = uow.invocation_runs.update(
                dispatched,
                expected_status=InvocationStatus.RUNNING,
                expected_claim_token=run.execution_claim_token,
            )
            if persisted is None:
                raise ApplicationConflictError()
            uow.commit()
        try:
            workflow = self._router.execute_managed(
                workflow_service,
                actor,
                task_id=run.task_id or "",
                task_input_revision_id=task_input_revision_id,
                request_id=run.request_id,
            )
        except Exception:
            self._finalize_managed_failure(actor, persisted)
            raise
        tool_run = getattr(workflow, "tool_run", None)
        managed_tool_run_id = getattr(tool_run, "tool_run_id", None)
        if not isinstance(managed_tool_run_id, str):
            self._finalize_managed_failure(actor, persisted)
            raise ApplicationInternalError()
        completed_at = self._clock()
        with self._unit_of_work_factory() as uow:
            current = uow.invocation_runs.get_owned_for_update(
                persisted.invocation_run_id,
                actor.actor_id,
            )
            if current is None:
                raise ResourceNotFoundError()
            tool_result = getattr(workflow, "result", None)
            failed = getattr(tool_result, "status", None) == "FAILED"
            result_error = getattr(tool_result, "error", None) or {}
            succeeded = current.transition(
                InvocationStatus.FAILED if failed else InvocationStatus.SUCCEEDED,
                now=completed_at,
                managed_tool_run_id=managed_tool_run_id,
                completed_at=completed_at,
                error_code=result_error.get("code") if failed else None,
                safe_error_message="Managed Tool failed." if failed else None,
            )
            final = uow.invocation_runs.update(
                succeeded,
                expected_status=InvocationStatus.RUNNING,
                expected_claim_token=run.execution_claim_token,
            )
            if final is None:
                raise ApplicationConflictError()
            uow.commit()
        return workflow

    def _dispatch_managed_retry(
        self,
        actor: ActorContext,
        run: InvocationRun,
        *,
        workflow_service: object,
    ) -> object:
        if run.managed_tool_run_id is None:
            raise ApplicationConflictError()
        dispatch_at = self._clock()
        dispatched = replace(run, dispatch_started_at=dispatch_at, updated_at=dispatch_at)
        with self._unit_of_work_factory() as uow:
            persisted = uow.invocation_runs.update(
                dispatched,
                expected_status=InvocationStatus.RUNNING,
                expected_claim_token=run.execution_claim_token,
            )
            if persisted is None:
                raise ApplicationConflictError()
            uow.commit()
        try:
            workflow = self._router.execute_managed_retry(
                workflow_service,
                actor,
                tool_run_id=run.managed_tool_run_id,
            )
        except Exception:
            self._finalize_managed_failure(actor, persisted)
            raise
        completed_at = self._clock()
        result_missing = False
        with self._unit_of_work_factory() as uow:
            current = uow.invocation_runs.get_owned_for_update(
                run.invocation_run_id,
                actor.actor_id,
            )
            task = (
                None
                if current is None or current.task_id is None
                else uow.tasks.get_owned(current.task_id, actor.actor_id)
            )
            if current is None or task is None:
                raise ResourceNotFoundError()
            if (
                task.selected_tool_run_id != run.managed_tool_run_id
                or task.selected_result_id is None
            ):
                terminal = current.transition(
                    InvocationStatus.FAILED,
                    now=completed_at,
                    completed_at=completed_at,
                    error_code="MANAGED_RESULT_NOT_PERSISTED",
                    safe_error_message=(
                        "Managed Tool retry did not persist a selected result."
                    ),
                )
                result_missing = True
            else:
                failed = task.current_status == "FAILED"
                terminal = current.transition(
                    InvocationStatus.FAILED if failed else InvocationStatus.SUCCEEDED,
                    now=completed_at,
                    completed_at=completed_at,
                    error_code=task.error_code if failed else None,
                    safe_error_message="Managed Tool failed." if failed else None,
                )
            final = uow.invocation_runs.update(
                terminal,
                expected_status=InvocationStatus.RUNNING,
                expected_claim_token=run.execution_claim_token,
            )
            if final is None:
                raise ApplicationConflictError()
            uow.commit()
        if result_missing:
            raise ApplicationInternalError()
        return workflow

    def execute_managed_retry(
        self,
        actor: ActorContext,
        *,
        task_id: str,
        tool_run_id: str,
        request_id: str,
        idempotency_key: str,
        workflow_service: object,
        source_message_id: str | None = None,
        retry_of_invocation_run_id: str | None = None,
        defer_execution: bool = False,
    ) -> PublicInvocation:
        now = self._clock()
        with self._unit_of_work_factory() as uow:
            task = uow.tasks.get_owned(task_id, actor.actor_id)
            tool_run = uow.tool_runs.get_owned(tool_run_id, actor.actor_id)
            messages = uow.messages.list_for_task(task_id)
            revision = (
                None
                if tool_run is None
                else uow.task_input_revisions.get(tool_run.task_input_revision_id)
            )
            if (
                task is None
                or tool_run is None
                or tool_run.task_id != task_id
                or revision is None
                or revision.normalized_input is None
                or task.bound_tool_ref is None
            ):
                raise ResourceNotFoundError()
            source_message = next(
                (message for message in messages if message.role == "USER"),
                None,
            )
            if source_message_id is not None:
                source_message = uow.messages.get(source_message_id)
            if source_message is None or source_message.conversation_id != task.conversation_id:
                raise ResourceNotFoundError()
            try:
                registration = self._registry.resolve(task.bound_tool_ref.tool_id)
            except UnknownToolError:
                raise ApplicationConflictError() from None
            if registration.ref != task.bound_tool_ref:
                raise ApplicationConflictError()
            existing = uow.invocation_runs.get_by_idempotency(
                actor.actor_id,
                idempotency_key,
            )
            prior = max(
                (
                    item
                    for item in uow.invocation_runs.list_for_conversation(
                        task.conversation_id,
                        actor.actor_id,
                    )
                    if item.task_id == task_id
                ),
                key=lambda item: (item.created_at, item.invocation_run_id),
                default=None,
            )
        if existing is None:
            policy = registration.definition.tool_execution_policy
            run = InvocationRun(
                invocation_run_id=self._id_factory("inv"),
                actor_id=actor.actor_id,
                conversation_id=task.conversation_id,
                source_message_id=source_message.message_id,
                task_id=task_id,
                retry_of_invocation_run_id=(
                    retry_of_invocation_run_id or (None if prior is None else prior.invocation_run_id)
                ),
                request_id=request_id,
                idempotency_key=idempotency_key,
                trigger=InvocationTrigger.EXPLICIT_RETRY,
                tool_id=registration.tool_id,
                tool_version=registration.version,
                schema_hash=registration.schema_hash,
                execution_profile=ToolExecutionProfile.MANAGED,
                executor_id=registration.executor_id,
                proposed_arguments=revision.normalized_input,
                policy_snapshot={
                    "lifecycle_policy": registration.execution_policy.value,
                    "required_permissions": list(policy.required_permissions),
                    "confirmation_required": False,
                    "confirmation_ttl_seconds": policy.confirmation_ttl_seconds,
                    "idempotency_required": policy.idempotency_required,
                    "audit_required": policy.audit_required,
                },
                tool_projection=ToolProjectionService.for_invocation(
                    registration.definition
                ).to_dict(),
                status=InvocationStatus.PENDING,
                confirmation_required=False,
                confirmation_expires_at=None,
                confirmed_at=None,
                confirmed_by=None,
                rejected_at=None,
                rejected_by=None,
                expired_at=None,
                authorization_checked_at=None,
                denied_at=None,
                execution_claim_token=None,
                execution_lease_expires_at=None,
                dispatch_started_at=None,
                execution_attempt_count=0,
                invocation_result_id=None,
                managed_tool_run_id=tool_run_id,
                error_code=None,
                safe_error_message=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
            try:
                with self._unit_of_work_factory() as uow:
                    uow.invocation_runs.add(run)
                    uow.commit()
                existing = run
            except PersistenceError:
                with self._unit_of_work_factory() as uow:
                    existing = uow.invocation_runs.get_by_idempotency(
                        actor.actor_id,
                        idempotency_key,
                    )
                if existing is None:
                    raise
        if (
            existing.trigger is not InvocationTrigger.EXPLICIT_RETRY
            or existing.task_id != task_id
            or existing.managed_tool_run_id != tool_run_id
        ):
            raise ApplicationConflictError()
        if not defer_execution:
            self._drive_managed(actor, existing.invocation_run_id, workflow_service=workflow_service)
        return self.get(actor, existing.invocation_run_id)


    def _managed_revision_id(
        self,
        actor: ActorContext,
        run: InvocationRun,
    ) -> str:
        if run.task_id is None:
            raise ApplicationConflictError()
        with self._unit_of_work_factory() as uow:
            task = uow.tasks.get_owned(run.task_id, actor.actor_id)
            revisions = uow.task_input_revisions.list_for_task(run.task_id)
            if task is None:
                raise ResourceNotFoundError()
            matching = [
                revision
                for revision in revisions
                if revision.normalized_input is not None
                and dict(revision.normalized_input) == dict(run.proposed_arguments)
            ]
        if not matching:
            raise ApplicationConflictError()
        return max(
            matching,
            key=lambda revision: (revision.revision, revision.created_at),
        ).task_input_revision_id


    def _task_conversation(self, actor: ActorContext, task_id: str) -> str:
        with self._unit_of_work_factory() as uow:
            task = uow.tasks.get_owned(task_id, actor.actor_id)
            if task is None:
                raise ResourceNotFoundError()
            return task.conversation_id

    def _finalize_managed_failure(
        self,
        actor: ActorContext,
        run: InvocationRun,
    ) -> InvocationRun:
        completed_at = self._clock()
        with self._unit_of_work_factory() as uow:
            current = uow.invocation_runs.get_owned_for_update(
                run.invocation_run_id,
                actor.actor_id,
            )
            if current is None:
                raise ResourceNotFoundError()
            if current.status is not InvocationStatus.RUNNING:
                return current
            tool_runs = uow.tool_runs.list_for_task(run.task_id or "")
            latest = max(
                tool_runs,
                key=lambda item: (item.created_at, item.tool_run_id),
                default=None,
            )
            committed = None if latest is None else uow.tool_results.get_for_tool_run(latest.tool_run_id)
            succeeded = committed is not None and committed.status != "FAILED"
            failed = current.transition(
                InvocationStatus.SUCCEEDED if succeeded else InvocationStatus.FAILED,
                now=completed_at,
                managed_tool_run_id=None if latest is None else latest.tool_run_id,
                completed_at=completed_at,
                error_code=None if succeeded else "MANAGED_EXECUTION_FAILED",
                safe_error_message=None if succeeded else "Managed Tool execution failed; use explicit retry.",
            )
            saved = uow.invocation_runs.update(
                failed,
                expected_status=InvocationStatus.RUNNING,
                expected_claim_token=run.execution_claim_token,
            )
            if saved is None:
                raise ApplicationConflictError()
            uow.commit()
            return saved

    def get(self, actor: ActorContext, invocation_run_id: str) -> PublicInvocation:
        with self._unit_of_work_factory() as uow:
            run = uow.invocation_runs.get_owned(invocation_run_id, actor.actor_id)
            if run is None:
                raise ResourceNotFoundError()
            return self._public(uow, run)

    def list_for_conversation(
        self,
        actor: ActorContext,
        conversation_id: str,
    ) -> list[PublicInvocation]:
        with self._unit_of_work_factory() as uow:
            runs = uow.invocation_runs.list_for_conversation(conversation_id, actor.actor_id)
        return [self.get(actor, run.invocation_run_id) for run in runs]

    def confirm(self, actor: ActorContext, invocation_run_id: str, *, defer_execution: bool = False) -> PublicInvocation:
        now = self._clock()
        with self._unit_of_work_factory() as uow:
            run = uow.invocation_runs.get_owned_for_update(invocation_run_id, actor.actor_id)
            if run is None:
                raise ResourceNotFoundError()
            if run.status is InvocationStatus.PENDING_CONFIRMATION:
                if run.confirmation_expires_at is None or now >= run.confirmation_expires_at:
                    expired = run.transition(
                        InvocationStatus.EXPIRED,
                        now=now,
                        expired_at=now,
                        completed_at=now,
                    )
                    if uow.invocation_runs.update(
                        expired,
                        expected_status=InvocationStatus.PENDING_CONFIRMATION,
                    ) is None:
                        raise ApplicationConflictError()
                    uow.commit()
                    return self.get(actor, invocation_run_id)
                confirmed = run.transition(
                    InvocationStatus.PENDING,
                    now=now,
                    confirmed_at=now,
                    confirmed_by=actor.actor_id,
                )
                if uow.invocation_runs.update(
                    confirmed,
                    expected_status=InvocationStatus.PENDING_CONFIRMATION,
                ) is None:
                    raise ApplicationConflictError()
                uow.commit()
                run = confirmed
            elif run.status is InvocationStatus.PENDING and run.confirmed_at is not None:
                pass
            elif run.status in {
                InvocationStatus.RUNNING,
                InvocationStatus.SUCCEEDED,
                InvocationStatus.FAILED,
                InvocationStatus.DENIED,
                InvocationStatus.OUTCOME_UNKNOWN,
            } and run.confirmed_at is not None:
                return self.get(actor, invocation_run_id)
            else:
                raise ApplicationConflictError()
        if run.status is InvocationStatus.PENDING and not defer_execution:
            self._drive_pending(actor, invocation_run_id)
        return self.get(actor, invocation_run_id)

    def reject(self, actor: ActorContext, invocation_run_id: str) -> PublicInvocation:
        now = self._clock()
        with self._unit_of_work_factory() as uow:
            run = uow.invocation_runs.get_owned_for_update(invocation_run_id, actor.actor_id)
            if run is None:
                raise ResourceNotFoundError()
            if run.status is InvocationStatus.REJECTED:
                return self._public(uow, run)
            if run.status is not InvocationStatus.PENDING_CONFIRMATION:
                raise ApplicationConflictError()
            if run.confirmation_expires_at is None or now >= run.confirmation_expires_at:
                expired = run.transition(
                    InvocationStatus.EXPIRED,
                    now=now,
                    expired_at=now,
                    completed_at=now,
                )
                if uow.invocation_runs.update(
                    expired,
                    expected_status=InvocationStatus.PENDING_CONFIRMATION,
                ) is None:
                    raise ApplicationConflictError()
                uow.commit()
                return self._public(uow, expired)
            rejected = run.transition(
                InvocationStatus.REJECTED,
                now=now,
                rejected_at=now,
                rejected_by=actor.actor_id,
                completed_at=now,
            )
            if uow.invocation_runs.update(
                rejected,
                expected_status=InvocationStatus.PENDING_CONFIRMATION,
            ) is None:
                raise ApplicationConflictError()
            uow.commit()
        return self.get(actor, invocation_run_id)

    def _authorize_initial(self, actor: ActorContext, invocation_run_id: str) -> InvocationRun:
        with self._unit_of_work_factory() as uow:
            run = uow.invocation_runs.get_owned(invocation_run_id, actor.actor_id)
            if run is None:
                raise ResourceNotFoundError()
        registration = self._current_registration(run)
        decision = self._authorization.authorize(
            ToolAuthorizationRequest(
                actor_id=actor.actor_id,
                user_id=actor.user_id,
                tool_ref=run.tool_ref,
                required_permissions=tuple(
                    str(value) for value in run.policy_snapshot["required_permissions"]
                ),
                action="PROPOSE",
            )
        )
        now = self._clock()
        with self._unit_of_work_factory() as uow:
            current = uow.invocation_runs.get_owned_for_update(invocation_run_id, actor.actor_id)
            if current is None:
                raise ResourceNotFoundError()
            if current.status is not InvocationStatus.PENDING:
                return current
            if not decision.allowed:
                updated = current.transition(
                    InvocationStatus.DENIED,
                    now=now,
                    authorization_checked_at=now,
                    denied_at=now,
                    error_code=decision.reason_code,
                    safe_error_message="Tool authorization was denied.",
                    completed_at=now,
                )
            elif registration.definition.tool_execution_policy.confirmation_required:
                updated = current.transition(
                    InvocationStatus.PENDING_CONFIRMATION,
                    now=now,
                    authorization_checked_at=now,
                )
            else:
                updated = replace(current, authorization_checked_at=now, updated_at=now)
            saved = uow.invocation_runs.update(
                updated,
                expected_status=InvocationStatus.PENDING,
            )
            if saved is None:
                raise ApplicationConflictError()
            uow.commit()
            return saved

    def _drive_pending(self, actor: ActorContext, invocation_run_id: str) -> InvocationRun:
        with self._unit_of_work_factory() as uow:
            run = uow.invocation_runs.get_owned(invocation_run_id, actor.actor_id)
            if run is None:
                raise ResourceNotFoundError()
            if run.status is not InvocationStatus.PENDING:
                return run
        registration = self._current_registration(run)
        policy = registration.definition.tool_execution_policy
        decision = self._authorization.authorize(
            ToolAuthorizationRequest(
                actor_id=actor.actor_id,
                user_id=actor.user_id,
                tool_ref=run.tool_ref,
                required_permissions=policy.required_permissions,
                action="EXECUTE",
            )
        )
        now = self._clock()
        if not decision.allowed:
            with self._unit_of_work_factory() as uow:
                current = uow.invocation_runs.get_owned_for_update(invocation_run_id, actor.actor_id)
                if current is None:
                    raise ResourceNotFoundError()
                if current.status is not InvocationStatus.PENDING:
                    return current
                denied = current.transition(
                    InvocationStatus.DENIED,
                    now=now,
                    authorization_checked_at=now,
                    denied_at=now,
                    error_code=decision.reason_code,
                    safe_error_message="Tool authorization was denied.",
                    completed_at=now,
                )
                saved = uow.invocation_runs.update(denied, expected_status=InvocationStatus.PENDING)
                if saved is None:
                    raise ApplicationConflictError()
                uow.commit()
                return saved
        claim_token = self._id_factory("claim")
        with self._unit_of_work_factory() as uow:
            current = uow.invocation_runs.get_owned_for_update(invocation_run_id, actor.actor_id)
            if current is None:
                raise ResourceNotFoundError()
            if current.status is not InvocationStatus.PENDING:
                return current
            running = current.transition(
                InvocationStatus.RUNNING,
                now=now,
                authorization_checked_at=now,
                execution_claim_token=claim_token,
                execution_lease_expires_at=now + timedelta(seconds=self._lease_seconds),
                dispatch_started_at=None,
                execution_attempt_count=current.execution_attempt_count + 1,
            )
            saved = uow.invocation_runs.update(running, expected_status=InvocationStatus.PENDING)
            if saved is None:
                raise ApplicationConflictError()
            uow.commit()
        return self._dispatch(actor, saved, registration)

    def _dispatch(
        self,
        actor: ActorContext,
        run: InvocationRun,
        registration: RegisteredTool,
    ) -> InvocationRun:
        dispatch_at = self._clock()
        marked = replace(run, dispatch_started_at=dispatch_at, updated_at=dispatch_at)
        with self._unit_of_work_factory() as uow:
            saved = uow.invocation_runs.update(
                marked,
                expected_status=InvocationStatus.RUNNING,
                expected_claim_token=run.execution_claim_token,
            )
            if saved is None:
                raise ApplicationConflictError()
            uow.commit()
        try:
            assert saved.execution_claim_token is not None
            output = self._router.execute(
                registration,
                saved.proposed_arguments,
                ToolExecutorContext(
                    invocation_run_id=saved.invocation_run_id,
                    request_id=saved.request_id,
                    idempotency_key=saved.idempotency_key,
                    actor_id=saved.actor_id,
                    conversation_id=saved.conversation_id,
                    execution_claim_token=saved.execution_claim_token,
                ),
            )
        except TimeoutError:
            return self._finalize_unknown(actor, saved)
        except Exception:
            if saved.execution_profile is ToolExecutionProfile.SIDE_EFFECT:
                return self._finalize_unknown(actor, saved)
            return self._finalize_failure(actor, saved, "TOOL_EXECUTION_FAILED")
        completed_at = self._clock()
        result = InvocationResult(
            invocation_result_id=self._id_factory("invres"),
            invocation_run_id=saved.invocation_run_id,
            data=output.data,
            presentation=output.presentation,
            created_at=completed_at,
        )
        with self._unit_of_work_factory() as uow:
            current = uow.invocation_runs.get_owned_for_update(saved.invocation_run_id, actor.actor_id)
            if current is None:
                raise ResourceNotFoundError()
            if current.status is not InvocationStatus.RUNNING:
                return current
            succeeded = current.transition(
                InvocationStatus.SUCCEEDED,
                now=completed_at,
                invocation_result_id=result.invocation_result_id,
                completed_at=completed_at,
                error_code=None,
                safe_error_message=None,
            )
            uow.invocation_results.add(result)
            persisted = uow.invocation_runs.update(
                succeeded,
                expected_status=InvocationStatus.RUNNING,
                expected_claim_token=saved.execution_claim_token,
            )
            if persisted is None:
                raise ApplicationConflictError()
            uow.commit()
            return persisted

    def _finalize_failure(
        self,
        actor: ActorContext,
        run: InvocationRun,
        error_code: str,
    ) -> InvocationRun:
        completed_at = self._clock()
        with self._unit_of_work_factory() as uow:
            current = uow.invocation_runs.get_owned_for_update(run.invocation_run_id, actor.actor_id)
            if current is None:
                raise ResourceNotFoundError()
            if current.status is not InvocationStatus.RUNNING:
                return current
            failed = current.transition(
                InvocationStatus.FAILED,
                now=completed_at,
                completed_at=completed_at,
                error_code=error_code,
                safe_error_message="Tool execution failed.",
            )
            saved = uow.invocation_runs.update(
                failed,
                expected_status=InvocationStatus.RUNNING,
                expected_claim_token=run.execution_claim_token,
            )
            if saved is None:
                raise ApplicationConflictError()
            uow.commit()
            return saved

    def _finalize_unknown(
        self,
        actor: ActorContext,
        run: InvocationRun,
    ) -> InvocationRun:
        completed_at = self._clock()
        with self._unit_of_work_factory() as uow:
            current = uow.invocation_runs.get_owned_for_update(run.invocation_run_id, actor.actor_id)
            if current is None:
                raise ResourceNotFoundError()
            if current.status is not InvocationStatus.RUNNING:
                return current
            unknown = current.transition(
                InvocationStatus.OUTCOME_UNKNOWN,
                now=completed_at,
                completed_at=completed_at,
                error_code="SIDE_EFFECT_OUTCOME_UNKNOWN",
                safe_error_message="The external side effect may have occurred; it was not retried.",
            )
            saved = uow.invocation_runs.update(
                unknown,
                expected_status=InvocationStatus.RUNNING,
                expected_claim_token=run.execution_claim_token,
            )
            if saved is None:
                raise ApplicationConflictError()
            uow.commit()
            return saved


    def _current_registration(self, run: InvocationRun) -> RegisteredTool:
        try:
            registration = self._registry.resolve(run.tool_id)
        except UnknownToolError:
            raise ApplicationConflictError() from None
        if registration.ref != run.tool_ref or registration.executor_id != run.executor_id:
            raise ApplicationConflictError()
        return registration

    def _public(self, uow: object, run: InvocationRun) -> PublicInvocation:
        result = (
            None
            if run.invocation_result_id is None
            else uow.invocation_results.get(run.invocation_result_id)
        )
        return PublicInvocation(
            run=run,
            result=result,
            tool=dict(run.tool_projection),
        )
