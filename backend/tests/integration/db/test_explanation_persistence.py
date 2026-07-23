from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine

from backend.tests.integration.db.test_result_commit import (
    ACTOR,
    BASE,
    _factory,
    _seed,
)
from materialsagent.application.errors import ApplicationConflictError
from materialsagent.application.explanation_service import (
    ExplanationPersistenceError,
    ExplanationService,
)
from materialsagent.application.result_service import ResultService
from materialsagent.domain.models.conversation import Conversation
from materialsagent.domain.ports.unit_of_work import PersistenceError
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
from materialsagent.domain.ports.explanation import ExplanationOutcome
from materialsagent.infrastructure.llm.mock_explanation import (
    MockExplanationAdapter,
)


def _committed_result(
    engine: Engine,
    *,
    requested: tuple[str, ...] = ("sem_image", "mechanical_properties"),
    completed: tuple[str, ...] = ("sem_image", "mechanical_properties"),
    failed: tuple[str, ...] = (),
):
    receipt, assets = _seed(
        engine,
        requested=requested,
        completed=completed,
        failed=failed,
    )
    return ResultService(
        _factory(engine),
        clock=lambda: BASE + timedelta(seconds=5),
        id_factory=lambda: "result_1",
    ).commit_result(ACTOR, receipt=receipt, assets=assets)


def _service(
    engine: Engine,
    adapter: MockExplanationAdapter,
) -> ExplanationService:
    return ExplanationService(
        _factory(engine),
        adapter,
        clock=lambda: BASE + timedelta(seconds=6),
        explanation_id_factory=lambda: "explanation_1",
        llm_call_id_factory=lambda: "llm_explanation_1",
    )


def test_prepare_commits_pending_facts_then_reloads_safe_projection(
    migrated_database_engine: Engine,
) -> None:
    _committed_result(migrated_database_engine)
    service = _service(
        migrated_database_engine,
        MockExplanationAdapter(mode="success"),
    )

    prepared = service.prepare(ACTOR, result_id="result_1")

    assert prepared.projection.result_id == "result_1"
    assert prepared.projection.artifacts[0].asset_id == "asset_1"
    assert prepared.projection.process_parameters == {
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
    }
    assert not hasattr(prepared.projection.artifacts[0], "object_key")
    with _factory(migrated_database_engine)() as unit_of_work:
        call = unit_of_work.llm_calls.get("llm_explanation_1")
        explanation = unit_of_work.explanations.get("explanation_1")
    assert call.status == "PENDING"
    assert call.purpose == "TOOL_RESULT_EXPLANATION"
    assert call.input_result_id == "result_1"
    assert explanation.status == "PENDING"
    assert explanation.attempt_no == 1


def test_prepare_rejects_cross_actor_asset_source(
    migrated_database_engine: Engine,
) -> None:
    _committed_result(migrated_database_engine)
    with _factory(migrated_database_engine)() as unit_of_work:
        from materialsagent.domain.models.actor import Actor

        unit_of_work.actors.add(
            Actor.local_anonymous("actor_other", created_at=BASE)
        )
        unit_of_work.commit()
    with migrated_database_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE asset SET actor_id = 'actor_other' "
                "WHERE asset_id = 'asset_1'"
            )
        )

    with pytest.raises(ApplicationConflictError):
        _service(
            migrated_database_engine,
            MockExplanationAdapter(mode="success"),
        ).prepare(ACTOR, result_id="result_1")


def test_finalize_rejects_cross_conversation_llm_source(
    migrated_database_engine: Engine,
) -> None:
    _committed_result(migrated_database_engine)
    adapter = MockExplanationAdapter(mode="success")
    service = _service(migrated_database_engine, adapter)
    prepared = service.prepare(ACTOR, result_id="result_1")
    with _factory(migrated_database_engine)() as unit_of_work:
        unit_of_work.conversations.add(
            Conversation(
                conversation_id="conversation_other",
                actor_id=ACTOR.actor_id,
                title=None,
                created_at=BASE,
                updated_at=BASE,
            )
        )
        unit_of_work.commit()
    with migrated_database_engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE llm_call "
                "SET conversation_id = 'conversation_other' "
                "WHERE llm_call_id = 'llm_explanation_1'"
            )
        )

    with pytest.raises(ApplicationConflictError):
        service.finalize(
            ACTOR,
            prepared=prepared,
            outcome=adapter.explain(prepared.projection),
        )


def test_attempt_one_is_unique_before_external_call(
    migrated_database_engine: Engine,
) -> None:
    _committed_result(migrated_database_engine)
    service = _service(
        migrated_database_engine,
        MockExplanationAdapter(mode="success"),
    )
    service.prepare(ACTOR, result_id="result_1")

    with pytest.raises(ApplicationConflictError):
        service.prepare(ACTOR, result_id="result_1")


class _TrackingUoW(SQLAlchemyUnitOfWork):
    def __init__(self, session_factory, owner) -> None:
        super().__init__(session_factory)
        self._owner = owner

    def __enter__(self):
        value = super().__enter__()
        self._owner.active += 1
        return value

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        try:
            super().__exit__(exc_type, exc_value, traceback)
        finally:
            self._owner.active -= 1


class _TrackingFactory:
    def __init__(self, engine: Engine) -> None:
        self._session_factory = create_session_factory(engine)
        self.active = 0

    def __call__(self) -> SQLAlchemyUnitOfWork:
        return _TrackingUoW(self._session_factory, self)


class _TransactionAssertingAdapter(MockExplanationAdapter):
    def __init__(self, factory: _TrackingFactory) -> None:
        super().__init__(mode="success")
        self._factory = factory

    def explain(self, value):
        assert self._factory.active == 0
        return super().explain(value)


class _UnexpectedExplanationAdapter:
    provider = "mock"
    model_name = "unexpected-explanation"
    prompt_template_id = "tool-result-explanation"
    prompt_template_version = "1"

    def __init__(self, behavior: str) -> None:
        self.behavior = behavior
        self.call_count = 0

    def explain(self, _value):
        self.call_count += 1
        if self.behavior == "runtime_error":
            raise RuntimeError("private provider failure")
        if self.behavior == "value_error":
            raise ValueError("raw provider response invalid")
        if self.behavior == "type_error":
            raise TypeError("private provider payload type")
        if self.behavior == "none":
            return None
        if self.behavior == "dict":
            return {"text": "raw provider payload"}
        if self.behavior == "invalid_outcome":
            return ExplanationOutcome(
                text=None,
                usage=None,
                provider_request_id=None,
                error_code=None,
                safe_error_message=None,
            )
        if self.behavior == "keyboard_interrupt":
            raise KeyboardInterrupt()
        if self.behavior == "system_exit":
            raise SystemExit(2)
        raise AssertionError("Unknown test behavior.")


def test_external_explanation_call_occurs_outside_database_transaction(
    migrated_database_engine: Engine,
) -> None:
    _committed_result(migrated_database_engine)
    factory = _TrackingFactory(migrated_database_engine)
    adapter = _TransactionAssertingAdapter(factory)
    service = ExplanationService(
        factory,
        adapter,
        clock=lambda: BASE + timedelta(seconds=6),
        explanation_id_factory=lambda: "explanation_1",
        llm_call_id_factory=lambda: "llm_explanation_1",
    )

    explanation = service.explain(ACTOR, result_id="result_1")

    assert explanation.status == "SUCCEEDED"
    assert adapter.call_count == 1
    assert factory.active == 0


def test_explain_success_jointly_finalizes_call_explanation_and_task(
    migrated_database_engine: Engine,
) -> None:
    _committed_result(migrated_database_engine)
    adapter = MockExplanationAdapter(mode="success")

    explanation = _service(migrated_database_engine, adapter).explain(
        ACTOR,
        result_id="result_1",
    )

    assert explanation.status == "SUCCEEDED"
    assert explanation.text
    assert adapter.call_count == 1
    with _factory(migrated_database_engine)() as unit_of_work:
        call = unit_of_work.llm_calls.get("llm_explanation_1")
        task = unit_of_work.tasks.get("task_1")
        result = unit_of_work.tool_results.get("result_1")
        links = unit_of_work.result_asset_links.list_for_result("result_1")
        messages = unit_of_work.messages.list_for_task("task_1")
    assert call.status == "SUCCEEDED"
    assert task.current_status == "SUCCEEDED"
    assert task.selected_result_id == "result_1"
    assert result.status == "SUCCEEDED"
    assert [link.asset_id for link in links] == ["asset_1"]
    assert messages == []


@pytest.mark.parametrize(
    ("mode", "expected_error"),
    [
        ("timeout", "EXPLANATION_TIMEOUT"),
        ("provider_unavailable", "EXPLANATION_PROVIDER_UNAVAILABLE"),
        ("protocol_error", "EXPLANATION_PROTOCOL_ERROR"),
        ("failure", "EXPLANATION_FAILED"),
    ],
)
def test_explain_failure_is_safe_and_result_survives(
    migrated_database_engine: Engine,
    mode: str,
    expected_error: str,
) -> None:
    _committed_result(migrated_database_engine)
    adapter = MockExplanationAdapter(mode=mode)

    explanation = _service(migrated_database_engine, adapter).explain(
        ACTOR,
        result_id="result_1",
    )

    assert explanation.status == "FAILED"
    assert explanation.text is None
    assert explanation.error_code == expected_error
    assert adapter.call_count == 1
    with _factory(migrated_database_engine)() as unit_of_work:
        call = unit_of_work.llm_calls.get("llm_explanation_1")
        task = unit_of_work.tasks.get("task_1")
        result = unit_of_work.tool_results.get("result_1")
    assert call.status == "FAILED"
    assert task.current_status == "PARTIALLY_SUCCEEDED"
    assert task.selected_result_id == "result_1"
    assert result.status == "SUCCEEDED"


@pytest.mark.parametrize(
    ("behavior", "expected_error", "safe_message"),
    [
        (
            "runtime_error",
            "EXPLANATION_FAILED",
            "Explanation generation failed.",
        ),
        (
            "value_error",
            "EXPLANATION_PROTOCOL_ERROR",
            "Explanation provider returned an invalid response.",
        ),
        (
            "type_error",
            "EXPLANATION_PROTOCOL_ERROR",
            "Explanation provider returned an invalid response.",
        ),
        (
            "none",
            "EXPLANATION_PROTOCOL_ERROR",
            "Explanation provider returned an invalid response.",
        ),
        (
            "dict",
            "EXPLANATION_PROTOCOL_ERROR",
            "Explanation provider returned an invalid response.",
        ),
        (
            "invalid_outcome",
            "EXPLANATION_PROTOCOL_ERROR",
            "Explanation provider returned an invalid response.",
        ),
    ],
)
def test_unexpected_adapter_failures_still_finalize_committed_explanation(
    migrated_database_engine: Engine,
    behavior: str,
    expected_error: str,
    safe_message: str,
) -> None:
    committed_result = _committed_result(migrated_database_engine)
    with _factory(migrated_database_engine)() as unit_of_work:
        original_run = unit_of_work.tool_runs.get("tool_run_1")
        original_asset = unit_of_work.assets.get("asset_1")
    adapter = _UnexpectedExplanationAdapter(behavior)

    explanation = ExplanationService(
        _factory(migrated_database_engine),
        adapter,
        clock=lambda: BASE + timedelta(seconds=6),
        explanation_id_factory=lambda: "explanation_1",
        llm_call_id_factory=lambda: "llm_explanation_1",
    ).explain(ACTOR, result_id="result_1")

    assert adapter.call_count == 1
    assert explanation.status == "FAILED"
    assert explanation.text is None
    assert explanation.error_code == expected_error
    assert explanation.safe_error_message == safe_message
    with _factory(migrated_database_engine)() as unit_of_work:
        call = unit_of_work.llm_calls.get("llm_explanation_1")
        persisted_explanation = unit_of_work.explanations.get(
            "explanation_1"
        )
        task = unit_of_work.tasks.get("task_1")
        result = unit_of_work.tool_results.get("result_1")
        links = unit_of_work.result_asset_links.list_for_result("result_1")
        run = unit_of_work.tool_runs.get("tool_run_1")
        asset = unit_of_work.assets.get("asset_1")
    assert call.status == "FAILED"
    assert call.error_code == expected_error
    assert call.safe_error_message == safe_message
    assert persisted_explanation == explanation
    assert task.current_status == "PARTIALLY_SUCCEEDED"
    assert task.selected_tool_run_id == "tool_run_1"
    assert task.selected_result_id == "result_1"
    assert result == committed_result
    assert [link.asset_id for link in links] == ["asset_1"]
    assert run == original_run
    assert asset == original_asset
    persisted_text = " ".join(
        (
            call.safe_error_message,
            persisted_explanation.safe_error_message,
        )
    )
    assert "private provider failure" not in persisted_text
    assert "raw provider response invalid" not in persisted_text
    assert "RuntimeError" not in persisted_text
    assert "ValueError" not in persisted_text
    assert "raw provider payload" not in persisted_text


@pytest.mark.parametrize(
    ("behavior", "error_type"),
    [
        ("keyboard_interrupt", KeyboardInterrupt),
        ("system_exit", SystemExit),
    ],
)
def test_process_control_base_exceptions_are_not_caught(
    migrated_database_engine: Engine,
    behavior: str,
    error_type: type[BaseException],
) -> None:
    _committed_result(migrated_database_engine)
    adapter = _UnexpectedExplanationAdapter(behavior)
    service = ExplanationService(
        _factory(migrated_database_engine),
        adapter,
        clock=lambda: BASE + timedelta(seconds=6),
        explanation_id_factory=lambda: "explanation_1",
        llm_call_id_factory=lambda: "llm_explanation_1",
    )

    with pytest.raises(error_type):
        service.explain(ACTOR, result_id="result_1")

    assert adapter.call_count == 1
    with _factory(migrated_database_engine)() as unit_of_work:
        call = unit_of_work.llm_calls.get("llm_explanation_1")
        explanation = unit_of_work.explanations.get("explanation_1")
        task = unit_of_work.tasks.get("task_1")
    assert call.status == "RUNNING"
    assert explanation.status == "RUNNING"
    assert explanation.text is None
    assert task.current_status == "RUNNING"


@pytest.mark.parametrize(
    ("completed", "failed", "expected_task"),
    [
        (("sem_image",), ("mechanical_properties",), "PARTIALLY_SUCCEEDED"),
        ((), ("mechanical_properties",), "FAILED"),
    ],
)
def test_result_terminal_status_cannot_be_promoted_by_explanation(
    migrated_database_engine: Engine,
    completed: tuple[str, ...],
    failed: tuple[str, ...],
    expected_task: str,
) -> None:
    requested = (
        ("sem_image", "mechanical_properties")
        if completed
        else ("mechanical_properties",)
    )
    _committed_result(
        migrated_database_engine,
        requested=requested,
        completed=completed,
        failed=failed,
    )

    explanation = _service(
        migrated_database_engine,
        MockExplanationAdapter(mode="success"),
    ).explain(ACTOR, result_id="result_1")

    assert explanation.status == "SUCCEEDED"
    with _factory(migrated_database_engine)() as unit_of_work:
        task = unit_of_work.tasks.get("task_1")
    assert task.current_status == expected_task


def test_finalize_is_idempotent_only_for_equivalent_outcome(
    migrated_database_engine: Engine,
) -> None:
    _committed_result(migrated_database_engine)
    adapter = MockExplanationAdapter(mode="success")
    service = _service(migrated_database_engine, adapter)
    prepared = service.prepare(ACTOR, result_id="result_1")
    outcome = adapter.explain(prepared.projection)

    first = service.finalize(ACTOR, prepared=prepared, outcome=outcome)
    second = service.finalize(ACTOR, prepared=prepared, outcome=outcome)

    assert second == first
    with pytest.raises(ApplicationConflictError):
        service.finalize(
            ACTOR,
            prepared=prepared,
            outcome=ExplanationOutcome(
                text="Different committed text.",
                usage={"input_tokens": 1, "output_tokens": 1},
                provider_request_id="different-request",
                error_code=None,
                safe_error_message=None,
            ),
        )


class _NoneUpdateLLMCalls:
    def __init__(self, delegate) -> None:
        self._delegate = delegate

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def update(self, call, *, expected_status):
        return None


class _ConcurrentFinalizeUoW(SQLAlchemyUnitOfWork):
    def __init__(self, session_factory, owner) -> None:
        super().__init__(session_factory)
        self._owner = owner
        self._concurrent_commit_done = False

    def __enter__(self):
        value = super().__enter__()
        self._llm_calls = _NoneUpdateLLMCalls(self._llm_calls)
        return value

    def rollback(self) -> None:
        super().rollback()
        if self._concurrent_commit_done:
            return
        self._concurrent_commit_done = True
        completed_at = BASE + timedelta(seconds=6)
        outcome = self._owner.outcome
        assert outcome is not None
        with SQLAlchemyUnitOfWork(
            self._owner._session_factory
        ) as unit_of_work:
            call = unit_of_work.llm_calls.get("llm_explanation_1")
            explanation = unit_of_work.explanations.get("explanation_1")
            task = unit_of_work.tasks.get("task_1")
            assert call is not None
            assert explanation is not None
            assert task is not None
            assert unit_of_work.llm_calls.update(
                replace(
                    call,
                    usage=outcome.usage,
                    provider_request_id=outcome.provider_request_id,
                    status="SUCCEEDED",
                    completed_at=completed_at,
                    duration_ms=0,
                    error_code=None,
                    safe_error_message=None,
                ),
                expected_status="RUNNING",
            )
            assert unit_of_work.explanations.update(
                replace(
                    explanation,
                    status="SUCCEEDED",
                    text=outcome.text,
                    completed_at=completed_at,
                    duration_ms=0,
                    error_code=None,
                    safe_error_message=None,
                ),
                expected_status="RUNNING",
            )
            assert unit_of_work.tasks.update(
                replace(
                    task,
                    current_status="SUCCEEDED",
                    updated_at=completed_at,
                    completed_at=completed_at,
                    error_code=None,
                    safe_error_message=None,
                ),
                expected_status="RUNNING",
            )
            unit_of_work.commit()


class _ConcurrentFinalizeFactory:
    def __init__(self, engine: Engine) -> None:
        self._session_factory = create_session_factory(engine)
        self.calls = 0
        self.outcome: ExplanationOutcome | None = None

    def __call__(self) -> SQLAlchemyUnitOfWork:
        self.calls += 1
        if self.calls == 4:
            return _ConcurrentFinalizeUoW(
                self._session_factory,
                self,
            )
        return SQLAlchemyUnitOfWork(self._session_factory)


def test_conditional_update_none_rolls_back_then_reads_committed_winner(
    migrated_database_engine: Engine,
) -> None:
    _committed_result(migrated_database_engine)
    adapter = MockExplanationAdapter(mode="success")
    factory = _ConcurrentFinalizeFactory(migrated_database_engine)
    service = ExplanationService(
        factory,
        adapter,
        clock=lambda: BASE + timedelta(seconds=6),
        explanation_id_factory=lambda: "explanation_1",
        llm_call_id_factory=lambda: "llm_explanation_1",
    )
    prepared = service.prepare(ACTOR, result_id="result_1")
    outcome = adapter.explain(prepared.projection)
    factory.outcome = outcome

    persisted = service.finalize(
        ACTOR,
        prepared=prepared,
        outcome=outcome,
    )

    assert persisted.status == "SUCCEEDED"
    assert persisted.text == outcome.text
    with _factory(migrated_database_engine)() as unit_of_work:
        task = unit_of_work.tasks.get("task_1")
        call = unit_of_work.llm_calls.get("llm_explanation_1")
    assert task.current_status == "SUCCEEDED"
    assert call.status == "SUCCEEDED"


class _FailingFinalizeUoW(SQLAlchemyUnitOfWork):
    def commit(self) -> None:
        raise PersistenceError("Persistence operation failed.")


class _FailingFinalizeFactory:
    def __init__(self, engine: Engine) -> None:
        self._session_factory = create_session_factory(engine)
        self.calls = 0

    def __call__(self) -> SQLAlchemyUnitOfWork:
        self.calls += 1
        if self.calls == 5:
            return _FailingFinalizeUoW(self._session_factory)
        return SQLAlchemyUnitOfWork(self._session_factory)


def test_finalize_commit_failure_returns_no_uncommitted_text_or_task_success(
    migrated_database_engine: Engine,
) -> None:
    _committed_result(migrated_database_engine)
    factory = _FailingFinalizeFactory(migrated_database_engine)
    adapter = MockExplanationAdapter(mode="success")
    service = ExplanationService(
        factory,
        adapter,
        clock=lambda: BASE + timedelta(seconds=6),
        explanation_id_factory=lambda: "explanation_1",
        llm_call_id_factory=lambda: "llm_explanation_1",
    )

    with pytest.raises(ExplanationPersistenceError):
        service.explain(ACTOR, result_id="result_1")

    assert adapter.call_count == 1
    with _factory(migrated_database_engine)() as unit_of_work:
        explanation = unit_of_work.explanations.get("explanation_1")
        call = unit_of_work.llm_calls.get("llm_explanation_1")
        task = unit_of_work.tasks.get("task_1")
        result = unit_of_work.tool_results.get("result_1")
    assert explanation.status == "RUNNING"
    assert explanation.text is None
    assert call.status == "RUNNING"
    assert task.current_status == "RUNNING"
    assert result.status == "SUCCEEDED"
