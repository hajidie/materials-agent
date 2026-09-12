from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import count
from types import SimpleNamespace

import pytest

from materialsagent.application.context import ActorContext
from materialsagent.application.errors import (
    ApplicationConflictError,
    ApplicationValidationError,
)
from materialsagent.application.fake_side_effect_tool import (
    FAKE_SIDE_EFFECT_PERMISSION,
    FakeSideEffectSink,
    build_fake_side_effect_registered_tool,
)
from materialsagent.application.tool_invocations import (
    ExecutorRouter,
    InvocationService,
    ManagedExecutor,
    StandardSyncExecutor,
)
from materialsagent.application.tool_proposals import ResolvedToolInvocationProposal
from materialsagent.application.tool_registry import ToolRegistry
from materialsagent.application.unit_conversion_tool import (
    build_unit_conversion_registered_tool,
)
from materialsagent.application.zta35g_tool import build_zta35g_tool_definition
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.models.message import Message
from materialsagent.domain.models.task import Task
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.models.tool_invocation import (
    ALLOWED_INVOCATION_TRANSITIONS,
    InvocationRun,
    InvocationStatus,
    InvocationTrigger,
    ProposalOrigin,
    ToolInvocationProposal,
)
from materialsagent.domain.ports.tool_authorization import (
    ExactPermissionAuthorizationService,
)
from materialsagent.domain.ports.tool_registry import (
    RegisteredTool,
    ToolExecutionProfile,
)


BASE = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)
ACTOR = ActorContext("actor_1")


def test_invocation_state_machine_has_one_exact_authoritative_transition_table() -> None:
    assert ALLOWED_INVOCATION_TRANSITIONS == {
        InvocationStatus.PENDING: frozenset(
            {
                InvocationStatus.PENDING_CONFIRMATION,
                InvocationStatus.RUNNING,
                InvocationStatus.DENIED,
                InvocationStatus.FAILED,
            }
        ),
        InvocationStatus.PENDING_CONFIRMATION: frozenset(
            {
                InvocationStatus.PENDING,
                InvocationStatus.REJECTED,
                InvocationStatus.EXPIRED,
            }
        ),
        InvocationStatus.RUNNING: frozenset(
            {
                InvocationStatus.SUCCEEDED,
                InvocationStatus.FAILED,
                InvocationStatus.OUTCOME_UNKNOWN,
            }
        ),
    }
    assert "PARTIALLY_SUCCEEDED" not in {status.value for status in InvocationStatus}


class _Clock:
    def __init__(self, current: datetime = BASE) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


class _InvocationRepo:
    def __init__(self, store: SimpleNamespace) -> None:
        self.store = store

    def get(self, invocation_run_id):
        return self.store.runs.get(invocation_run_id)

    def get_owned(self, invocation_run_id, actor_id):
        run = self.get(invocation_run_id)
        return run if run is not None and run.actor_id == actor_id else None

    def get_owned_for_update(self, invocation_run_id, actor_id):
        return self.get_owned(invocation_run_id, actor_id)

    def get_by_message(self, source_message_id):
        return next(
            (
                run
                for run in self.store.runs.values()
                if run.source_message_id == source_message_id
                and run.trigger is InvocationTrigger.MESSAGE_PROPOSAL
            ),
            None,
        )

    def get_by_idempotency(self, actor_id, idempotency_key):
        return next(
            (
                run
                for run in self.store.runs.values()
                if run.actor_id == actor_id and run.idempotency_key == idempotency_key
            ),
            None,
        )

    def list_for_conversation(self, conversation_id, actor_id):
        return [
            run
            for run in self.store.runs.values()
            if run.conversation_id == conversation_id and run.actor_id == actor_id
        ]

    def add(self, run):
        self.store.runs[run.invocation_run_id] = run

    def update(
        self,
        run,
        *,
        expected_status,
        expected_claim_token=None,
    ):
        current = self.store.runs.get(run.invocation_run_id)
        if current is None or current.status is not expected_status:
            return None
        if (
            expected_claim_token is not None
            and current.execution_claim_token != expected_claim_token
        ):
            return None
        self.store.runs[run.invocation_run_id] = run
        return run


class _ResultRepo:
    def __init__(self, store: SimpleNamespace) -> None:
        self.store = store

    def get(self, result_id):
        return self.store.results.get(result_id)

    def add(self, result):
        self.store.results[result.invocation_result_id] = result


class _OwnedRepo:
    def __init__(self, values: dict[str, object]) -> None:
        self.values = values

    def get(self, identity):
        return self.values.get(identity)

    def get_owned(self, identity, actor_id):
        value = self.get(identity)
        return value if value is not None and value.actor_id == actor_id else None


class _MessageRepo(_OwnedRepo):
    def list_for_task(self, task_id):
        return [value for value in self.values.values() if value.task_id == task_id]


class _RevisionRepo(_OwnedRepo):
    def list_for_task(self, task_id):
        return [value for value in self.values.values() if value.task_id == task_id]


class _ToolRunRepo(_OwnedRepo):
    def list_for_task(self, task_id):
        return [value for value in self.values.values() if value.task_id == task_id]


class _Uow:
    def __init__(self, store: SimpleNamespace) -> None:
        self.conversations = _OwnedRepo(store.conversations)
        self.messages = _MessageRepo(store.messages)
        self.tasks = _OwnedRepo(store.tasks)
        self.task_input_revisions = _RevisionRepo(store.revisions)
        self.tool_runs = _ToolRunRepo(store.tool_runs)
        self.invocation_runs = _InvocationRepo(store)
        self.invocation_results = _ResultRepo(store)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def commit(self):
        return None

    def rollback(self):
        return None


def _store(*, task_id: str | None = None) -> SimpleNamespace:
    conversation = Conversation("conversation_1", ACTOR.actor_id, None, BASE, BASE)
    message = Message.user(
        message_id="message_1",
        conversation_id=conversation.conversation_id,
        task_id=task_id,
        actor_id=ACTOR.actor_id,
        request_id="message_request_1",
        content_text="test",
        created_at=BASE,
    )
    return SimpleNamespace(
        conversations={conversation.conversation_id: conversation},
        messages={message.message_id: message},
        tasks={},
        revisions={},
        tool_runs={},
        runs={},
        results={},
    )


def _ids():
    sequence = count(1)
    return lambda prefix: f"{prefix}_{next(sequence)}"


def _service(
    store: SimpleNamespace,
    registrations: tuple[RegisteredTool, ...],
    clock: _Clock,
    *,
    managed_workflow_service: object | None = None,
) -> InvocationService:
    return InvocationService(
        lambda: _Uow(store),
        ToolRegistry(registrations),
        ExecutorRouter((StandardSyncExecutor(), ManagedExecutor())),
        authorization=ExactPermissionAuthorizationService(
            (FAKE_SIDE_EFFECT_PERMISSION,)
        ),
        clock=clock,
        id_factory=_ids(),
        lease_seconds=60,
        managed_workflow_service=managed_workflow_service,
    )


def _resolved(registration: RegisteredTool, arguments: dict[str, object]):
    if not registration.schema_hash:
        registration = ToolRegistry((registration,)).resolve(registration.tool_id)
    proposal = ToolInvocationProposal(
        conversation_id="conversation_1",
        source_message_id="message_1",
        llm_call_id="llm_1",
        model_tool_name=registration.tool_id,
        proposed_arguments=arguments,
        origin=ProposalOrigin.STRUCTURED,
    )
    return ResolvedToolInvocationProposal(proposal, registration)


def _running(
    registration: RegisteredTool,
    *,
    clock: _Clock,
    dispatch_started: bool,
    confirmed: bool = False,
    task_id: str | None = None,
) -> InvocationRun:
    confirmation_required = registration.execution_profile is ToolExecutionProfile.SIDE_EFFECT
    return InvocationRun(
        invocation_run_id="inv_crash",
        actor_id=ACTOR.actor_id,
        conversation_id="conversation_1",
        source_message_id="message_1",
        task_id=task_id,
        retry_of_invocation_run_id=None,
        request_id="request_crash",
        idempotency_key="idem_crash",
        trigger=InvocationTrigger.MESSAGE_PROPOSAL,
        tool_id=registration.tool_id,
        tool_version=registration.version,
        schema_hash=registration.schema_hash,
        execution_profile=registration.execution_profile,
        executor_id=registration.executor_id,
        proposed_arguments=(
            {"value": 1, "from_unit": "MPa", "to_unit": "Pa"}
            if registration.execution_profile is ToolExecutionProfile.STANDARD
            else {"message": "write once"}
            if registration.execution_profile is ToolExecutionProfile.SIDE_EFFECT
            else {"material": "ZTA35G"}
        ),
        policy_snapshot={
            "required_permissions": list(
                registration.definition.tool_execution_policy.required_permissions
            )
        },
        tool_projection={
            "tool_id": registration.tool_id,
            "version": registration.version,
            "display_name": registration.display_name,
            "execution_profile": registration.execution_profile.value,
            "confirmation_required": confirmation_required,
            "confirmation_prompt": registration.definition.confirmation_prompt,
        },
        status=InvocationStatus.RUNNING,
        confirmation_required=confirmation_required,
        confirmation_expires_at=(clock.current + timedelta(minutes=15) if confirmation_required else None),
        confirmed_at=(clock.current - timedelta(minutes=2) if confirmed else None),
        confirmed_by=(ACTOR.actor_id if confirmed else None),
        rejected_at=None,
        rejected_by=None,
        expired_at=None,
        authorization_checked_at=clock.current - timedelta(minutes=2),
        denied_at=None,
        execution_claim_token="claim_old",
        execution_lease_expires_at=clock.current - timedelta(seconds=1),
        dispatch_started_at=(clock.current - timedelta(minutes=1) if dispatch_started else None),
        execution_attempt_count=1,
        invocation_result_id=None,
        managed_tool_run_id=None,
        error_code=None,
        safe_error_message=None,
        created_at=clock.current - timedelta(minutes=3),
        updated_at=clock.current - timedelta(minutes=1),
        completed_at=None,
    )


def test_standard_invocation_succeeds_without_creating_managed_task() -> None:
    store = _store()
    clock = _Clock()
    registration = build_unit_conversion_registered_tool()
    service = _service(store, (registration,), clock)

    public = service.create_from_proposal(
        ACTOR,
        _resolved(
            registration,
            {"value": 2, "from_unit": "MPa", "to_unit": "Pa"},
        ),
        request_id="request_1",
        idempotency_key="idem_1",
    )

    assert public.run.status is InvocationStatus.SUCCEEDED
    assert public.run.task_id is None
    assert store.tasks == {}
    assert public.result is not None
    assert public.result.data["value"] == 2_000_000.0


def test_incomplete_standard_arguments_do_not_create_invocation_or_task() -> None:
    store = _store()
    registration = build_unit_conversion_registered_tool()
    service = _service(store, (registration,), _Clock())

    with pytest.raises(ApplicationValidationError):
        service.create_from_proposal(
            ACTOR,
            _resolved(registration, {"value": 2}),
            request_id="request_incomplete",
            idempotency_key="idem_incomplete",
        )

    assert store.runs == {}
    assert store.tasks == {}


def test_terminal_invocation_uses_persisted_safe_projection_after_tool_upgrade() -> None:
    store = _store()
    original = build_unit_conversion_registered_tool()
    first_service = _service(store, (original,), _Clock())
    completed = first_service.create_from_proposal(
        ACTOR,
        _resolved(
            original,
            {"value": 1, "from_unit": "MPa", "to_unit": "Pa"},
        ),
        request_id="request_original",
        idempotency_key="idem_original",
    )
    upgraded = replace(
        original,
        definition=replace(
            original.definition,
            version="2",
            display_name="Upgraded Unit Tool",
        ),
    )

    historical = _service(store, (upgraded,), _Clock()).get(
        ACTOR,
        completed.run.invocation_run_id,
    )

    assert historical.tool["version"] == "1"
    assert historical.tool["display_name"] == "材料单位换算"


def test_fake_side_effect_confirmation_rejection_and_sink_are_idempotent() -> None:
    sink = FakeSideEffectSink()
    registration = build_fake_side_effect_registered_tool(sink)
    store = _store()
    clock = _Clock()
    service = _service(store, (registration,), clock)

    pending = service.create_from_proposal(
        ACTOR,
        _resolved(registration, {"message": "write once"}),
        request_id="request_1",
        idempotency_key="idem_1",
    )
    assert pending.run.status is InvocationStatus.PENDING_CONFIRMATION
    assert sink.write_count == 0

    succeeded = service.confirm(ACTOR, pending.run.invocation_run_id)
    repeated = service.confirm(ACTOR, pending.run.invocation_run_id)
    assert succeeded.run.status is InvocationStatus.SUCCEEDED
    assert repeated.run.status is InvocationStatus.SUCCEEDED
    assert sink.write_count == 1
    with pytest.raises(ApplicationConflictError):
        service.reject(ACTOR, pending.run.invocation_run_id)

    other_store = _store()
    rejected_service = _service(other_store, (registration,), clock)
    rejected_pending = rejected_service.create_from_proposal(
        ACTOR,
        _resolved(registration, {"message": "do not write"}),
        request_id="request_2",
        idempotency_key="idem_2",
    )
    rejected = rejected_service.reject(ACTOR, rejected_pending.run.invocation_run_id)
    assert rejected_service.reject(ACTOR, rejected.run.invocation_run_id).run.status is InvocationStatus.REJECTED
    with pytest.raises(ApplicationConflictError):
        rejected_service.confirm(ACTOR, rejected.run.invocation_run_id)














def test_explicit_managed_retry_creates_linked_invocation_and_reuses_idempotency() -> None:
    registration = ToolRegistry(
        (build_zta35g_tool_definition(),)
    ).resolve("zta35g_sem_virtual_lab")
    store = _store(task_id="task_1")
    store.tasks["task_1"] = Task(
        task_id="task_1",
        conversation_id="conversation_1",
        actor_id=ACTOR.actor_id,
        task_type="TOOL_EXECUTION",
        current_status="RUNNING",
        selected_tool_run_id=None,
        selected_result_id=None,
        created_at=BASE - timedelta(minutes=10),
        started_at=BASE - timedelta(minutes=8),
        updated_at=BASE - timedelta(minutes=1),
        completed_at=None,
        error_code=None,
        safe_error_message=None,
        tool_id=registration.tool_id,
        bound_tool_version=registration.version,
        bound_schema_hash=registration.schema_hash,
    )
    normalized = {"material": "ZTA35G", "fixture": True}
    store.revisions["revision_retry"] = TaskInputRevision(
        task_input_revision_id="revision_retry",
        task_id="task_1",
        request_id="request_revision",
        source_llm_call_id="llm_1",
        source_message_ids=["message_1"],
        revision=1,
        raw_input=dict(normalized),
        normalized_input=dict(normalized),
        missing_fields=[],
        ambiguous_fields=[],
        validation_errors=[],
        created_at=BASE - timedelta(minutes=9),
    )
    store.tool_runs["tool_run_retry"] = SimpleNamespace(
        tool_run_id="tool_run_retry",
        task_id="task_1",
        actor_id=ACTOR.actor_id,
        task_input_revision_id="revision_retry",
        created_at=BASE - timedelta(minutes=1),
    )
    prior = _running(
        registration,
        clock=_Clock(),
        dispatch_started=True,
        task_id="task_1",
    ).transition(
        InvocationStatus.FAILED,
        now=BASE,
        completed_at=BASE,
        error_code="MANAGED_EXECUTION_FAILED",
        safe_error_message="failed",
    )
    store.runs[prior.invocation_run_id] = prior

    class _RetryWorkflow:
        def __init__(self) -> None:
            self.calls = 0

        def execute_reserved_retry(self, _actor, *, tool_run_id):
            self.calls += 1
            assert tool_run_id == "tool_run_retry"
            task = store.tasks["task_1"]
            task.selected_tool_run_id = tool_run_id
            task.selected_result_id = "result_retry"
            return SimpleNamespace(tool_run=store.tool_runs[tool_run_id])

    workflow = _RetryWorkflow()
    service = _service(store, (registration,), _Clock())

    first = service.execute_managed_retry(
        ACTOR,
        task_id="task_1",
        tool_run_id="tool_run_retry",
        request_id="request_retry",
        idempotency_key="idem_retry",
        workflow_service=workflow,
    )
    replay = service.execute_managed_retry(
        ACTOR,
        task_id="task_1",
        tool_run_id="tool_run_retry",
        request_id="request_retry_replay",
        idempotency_key="idem_retry",
        workflow_service=workflow,
    )

    assert first.run.status is InvocationStatus.SUCCEEDED
    assert first.run.trigger is InvocationTrigger.EXPLICIT_RETRY
    assert first.run.retry_of_invocation_run_id == prior.invocation_run_id
    assert first.run.managed_tool_run_id == "tool_run_retry"
    assert replay.run.invocation_run_id == first.run.invocation_run_id
    assert workflow.calls == 1


def test_queries_never_resume_dispatched_managed_invocation():
    registration = ToolRegistry((build_zta35g_tool_definition(),)).resolve("zta35g_sem_virtual_lab")
    store = _store(task_id="task_1")
    clock = _Clock()
    run = _running(registration, clock=clock, dispatch_started=True, task_id="task_1")
    store.runs[run.invocation_run_id] = run
    service = _service(store, (registration,), clock)
    for _ in range(3):
        assert service.get(ACTOR, run.invocation_run_id).run == run
