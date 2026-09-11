from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from backend.tests.api.conftest import BASE_TIME
from backend.tests.api.test_assets import _MemoryStorage
from backend.tests.api.test_explanation_outcomes import (
    _CountingMemoryStorage,
    _Runtime,
    _chat_port_for_outputs,
)
from materialsagent.application.tool_execution import ToolExecutionService
from materialsagent.application.tools import build_tool_registry
from materialsagent.infrastructure.db.conversation_task import MessageRow
from materialsagent.infrastructure.db.explanation import (
    NaturalLanguageExplanationRow,
)
from materialsagent.infrastructure.db.idempotency_record import (
    IdempotencyRecordRow,
)
from materialsagent.infrastructure.db.llm_call import LLMCallRow
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.tool_run import ToolRunRow
from materialsagent.infrastructure.db.unit_of_work import SQLAlchemyUnitOfWork
from materialsagent.domain.ports.unit_of_work import PersistenceError
from materialsagent.infrastructure.llm.mock_explanation import (
    MockExplanationAdapter,
)


class _NthCommitUncertainUnitOfWork(SQLAlchemyUnitOfWork):
    def __init__(self, session_factory, controller) -> None:
        super().__init__(session_factory)
        self._controller = controller

    def commit(self) -> None:
        self._controller.commits += 1
        super().commit()
        if self._controller.commits == self._controller.fail_on_commit:
            raise PersistenceError("Commit result was uncertain.")


class _NthCommitUncertainFactory:
    def __init__(self, engine, *, fail_on_commit: int) -> None:
        self._session_factory = create_session_factory(engine)
        self.fail_on_commit = fail_on_commit
        self.commits = 0

    def __call__(self) -> SQLAlchemyUnitOfWork:
        return _NthCommitUncertainUnitOfWork(
            self._session_factory,
            self,
        )


def _client_options(
    api_harness,
    runtime,
    storage,
    explanation,
    *,
    unit_of_work_factory=None,
):
    factory = unit_of_work_factory or api_harness.unit_of_work_factory
    options = {
        "tool_execution_service": ToolExecutionService(
            factory,
            build_tool_registry(runtime),
            clock=lambda: BASE_TIME.replace(hour=1),
            seed_factory=lambda: 701,
        ),
        "storage_service": storage,
        "explanation_port": explanation,
        "chat_orchestration_port": _chat_port_for_outputs(
            ["sem_image", "mechanical_properties"]
        ),
        "m7_tool_chain_enabled": True,
    }
    if unit_of_work_factory is not None:
        options["unit_of_work_factory"] = unit_of_work_factory
    return options


def _create_explanation_failure(
    client,
    *,
    expected_result_status: str = "SUCCEEDED",
) -> dict[str, object]:
    conversation_id = client.post(
        "/api/v1/conversations",
        headers={"Idempotency-Key": "explanation-retry-conversation"},
        json={},
    ).json()["data"]["conversation_id"]
    response = client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": "initial-explanation-failure"},
        json={
            "submission_mode": "NEW_TASK",
            "content_text": "完整合法 Tool 请求",
        },
    )
    assert response.status_code == 200, response.json()
    data = response.json()["data"]
    assert data["task"]["status"] == (
        "FAILED"
        if expected_result_status == "FAILED"
        else "PARTIALLY_SUCCEEDED"
    )
    assert data["result_summary"]["status"] == expected_result_status
    assert data["explanation"]["status"] == "FAILED"
    return data


def _insert_later_failed_explanation(
    api_harness,
    *,
    conversation_id: str,
    task_id: str,
    result_id: str,
) -> None:
    started_at = datetime(2026, 7, 19, 2, 0, tzinfo=timezone.utc)
    completed_at = started_at + timedelta(seconds=1)
    call_id = "llm_legacy_latest_failed"
    explanation_id = "explanation_legacy_latest_failed"
    session_factory = create_session_factory(api_harness.engine)
    with session_factory() as session:
        source_message_id = session.scalar(
            select(MessageRow.message_id)
            .where(MessageRow.task_id == task_id, MessageRow.role == "USER")
            .order_by(MessageRow.created_at, MessageRow.message_id)
            .limit(1)
        )
        assert source_message_id is not None
        session.add(
            LLMCallRow(
                llm_call_id=call_id,
                task_id=task_id,
                conversation_id=conversation_id,
                source_message_id=source_message_id,
                request_id="req_legacy_latest_failed",
                purpose="TOOL_RESULT_EXPLANATION",
                input_result_id=result_id,
                provider="mock",
                model_name="mock-explanation",
                prompt_template_id="tool-result-explanation",
                prompt_template_version="1",
                prompt_digest="0" * 64,
                generation_parameters={
                    "temperature": 0,
                    "max_tokens": 512,
                },
                structured_output_summary=None,
                usage=None,
                provider_request_id="legacy-failure",
                status="FAILED",
                created_at=started_at,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=1000,
                error_code="EXPLANATION_PROTOCOL_ERROR",
                safe_error_message="Explanation response was invalid.",
            )
        )
        session.flush()
        session.add(
            NaturalLanguageExplanationRow(
                explanation_id=explanation_id,
                task_id=task_id,
                result_id=result_id,
                llm_call_id=call_id,
                attempt_no=3,
                status="FAILED",
                language="zh-CN",
                text=None,
                created_at=started_at,
                started_at=started_at,
                completed_at=completed_at,
                duration_ms=1000,
                error_code="EXPLANATION_PROTOCOL_ERROR",
                safe_error_message="Explanation response was invalid.",
            )
        )
        session.commit()


def test_explanation_retry_uses_http_request_attempt_two_and_no_tool_side_effect(
    api_harness,
) -> None:
    actor_id = "actor_m8_explanation_retry"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _MemoryStorage()
    explanation = MockExplanationAdapter(mode="failure")
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
        ),
    ) as client:
        initial = _create_explanation_failure(client)
        result_id = initial["result_summary"]["result_id"]
        tool_retry_rejected = client.post(
            f"/api/v1/tasks/{initial['task']['task_id']}/tool-runs",
            headers={"Idempotency-Key": "explanation-only-tool-retry"},
            json={},
        )
        explanation.mode = "success"
        first = client.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": "explanation-retry"},
            json={},
        )
        replay = client.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": "explanation-retry"},
            json={},
        )
        conflict = client.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": "explanation-retry"},
            json={"reason": "different"},
        )
        language_conflict = client.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": "explanation-retry"},
            json={"language": "en-US"},
        )
        ineligible = client.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": "new-key-after-success"},
            json={},
        )
        message_replay = client.post(
            f"/api/v1/conversations/{initial['conversation_id']}/messages",
            headers={"Idempotency-Key": "initial-explanation-failure"},
            json={
                "submission_mode": "NEW_TASK",
                "content_text": "完整合法 Tool 请求",
            },
        )
        _insert_later_failed_explanation(
            api_harness,
            conversation_id=initial["conversation_id"],
            task_id=initial["task"]["task_id"],
            result_id=result_id,
        )
        replay_with_later_failure = client.post(
            f"/api/v1/conversations/{initial['conversation_id']}/messages",
            headers={"Idempotency-Key": "initial-explanation-failure"},
            json={
                "submission_mode": "NEW_TASK",
                "content_text": "完整合法 Tool 请求",
            },
        )

    assert first.status_code == replay.status_code == 200, (
        first.json(),
        replay.json(),
    )
    assert first.json()["data"]["idempotency_replayed"] is False
    assert tool_retry_rejected.status_code == 409
    assert (
        tool_retry_rejected.json()["error"]["code"]
        == "TASK_NOT_RETRYABLE"
    )
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert first.json()["data"]["explanation"]["attempt_no"] == 2
    assert first.json()["data"]["explanation"]["status"] == "SUCCEEDED"
    assert first.json()["data"]["task_status"] == "SUCCEEDED"
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert language_conflict.status_code == 409
    assert (
        language_conflict.json()["error"]["code"]
        == "IDEMPOTENCY_CONFLICT"
    )
    assert ineligible.status_code == 409
    assert (
        ineligible.json()["error"]["code"]
        == "EXPLANATION_NOT_RETRYABLE"
    )
    assert runtime.calls == 1
    assert explanation.call_count == 2
    assert message_replay.status_code == 200, message_replay.json()
    replay_data = message_replay.json()["data"]
    assert replay_data["idempotency_replayed"] is True
    assert replay_data["result_summary"]["result_id"] == result_id
    assert (
        replay_data["explanation"]["explanation_id"]
        == first.json()["data"]["explanation"]["explanation_id"]
    )
    assert replay_data["explanation"]["status"] == "SUCCEEDED"
    assert replay_data["latest_explanation_failure"] is None
    assert replay_with_later_failure.status_code == 200
    later_data = replay_with_later_failure.json()["data"]
    assert (
        later_data["explanation"]["explanation_id"]
        == first.json()["data"]["explanation"]["explanation_id"]
    )
    assert (
        later_data["latest_explanation_failure"]["explanation_id"]
        == "explanation_legacy_latest_failed"
    )
    assert later_data["latest_explanation_failure"]["status"] == "FAILED"
    assert (
        later_data["latest_explanation_failure"]["safe_error_message"]
        == "Explanation response was invalid."
    )

    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(ToolRunRow)
        ) == 1
        assert connection.scalar(
            select(func.count()).select_from(NaturalLanguageExplanationRow)
        ) == 3
        retry_request_id = connection.scalar(
            select(IdempotencyRecordRow.first_request_id).where(
                IdempotencyRecordRow.operation == "EXPLANATION_RETRY"
            )
        )
        retry_call_request_id = connection.scalar(
            select(LLMCallRow.request_id)
            .where(
                LLMCallRow.purpose == "TOOL_RESULT_EXPLANATION",
                LLMCallRow.request_id == retry_request_id,
            )
        )
        tool_request_id = connection.scalar(select(ToolRunRow.request_id))
        assert retry_call_request_id == retry_request_id
        assert retry_request_id != tool_request_id


def test_twenty_concurrent_explanation_retries_create_one_attempt_and_call(
    api_harness,
) -> None:
    actor_id = "actor_m8_explanation_retry_concurrent"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    explanation = MockExplanationAdapter(mode="failure")
    storage = _CountingMemoryStorage()
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
        ),
    ) as client:
        initial = _create_explanation_failure(client)
        result_id = initial["result_summary"]["result_id"]
        puts_before_retry = storage.put_calls
        explanation.mode = "success"

        def retry(_: int):
            return client.post(
                f"/api/v1/tool-results/{result_id}/explanations",
                headers={"Idempotency-Key": "same-explanation-retry"},
                json={},
            )

        with ThreadPoolExecutor(max_workers=20) as executor:
            responses = list(executor.map(retry, range(20)))

    assert {response.status_code for response in responses} == {200}, [
        response.json()
        for response in responses
        if response.status_code != 200
    ]
    payloads = [response.json()["data"] for response in responses]
    assert len(
        {
            payload["explanation"]["explanation_id"]
            for payload in payloads
        }
    ) == 1
    assert sum(not payload["idempotency_replayed"] for payload in payloads) == 1
    assert runtime.calls == 1
    assert storage.put_calls == puts_before_retry
    assert explanation.call_count == 2
    with api_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count()).select_from(NaturalLanguageExplanationRow)
        ) == 2
        assert connection.scalar(
            select(func.count())
            .select_from(IdempotencyRecordRow)
            .where(IdempotencyRecordRow.operation == "EXPLANATION_RETRY")
        ) == 1


def test_explanation_retry_own_reservation_commit_uncertainty_continues_once(
    api_harness,
) -> None:
    actor_id = "actor_m8_explanation_reservation_uncertainty"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _MemoryStorage()
    explanation = MockExplanationAdapter(mode="failure")
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
        ),
    ) as client:
        initial = _create_explanation_failure(client)
    result_id = initial["result_summary"]["result_id"]
    explanation.mode = "success"
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=1,
    )
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            unit_of_work_factory=factory,
        ),
    ) as client:
        first = client.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": "uncertain-explanation-reservation"},
            json={},
        )
        replay = client.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": "uncertain-explanation-reservation"},
            json={},
        )

    assert first.status_code == replay.status_code == 200, (
        first.json(),
        replay.json(),
    )
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert first.json()["data"]["explanation"]["status"] == "SUCCEEDED"
    assert first.json()["data"]["task_status"] == "SUCCEEDED"
    assert explanation.call_count == 2


def test_explanation_retry_start_commit_uncertainty_calls_provider_once(
    api_harness,
) -> None:
    actor_id = "actor_m8_explanation_start_uncertainty"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _MemoryStorage()
    explanation = MockExplanationAdapter(mode="failure")
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
        ),
    ) as client:
        initial = _create_explanation_failure(client)
    result_id = initial["result_summary"]["result_id"]
    explanation.mode = "success"
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=2,
    )
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            unit_of_work_factory=factory,
        ),
    ) as client:
        response = client.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": "uncertain-explanation-start"},
            json={},
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["idempotency_replayed"] is False
    assert response.json()["data"]["explanation"]["status"] == "SUCCEEDED"
    assert response.json()["data"]["task_status"] == "SUCCEEDED"
    assert explanation.call_count == 2


def test_explanation_retry_finalize_commit_uncertainty_completes_once(
    api_harness,
) -> None:
    actor_id = "actor_m8_explanation_finalize_uncertainty"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _MemoryStorage()
    explanation = MockExplanationAdapter(mode="failure")
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
        ),
    ) as client:
        initial = _create_explanation_failure(client)
    result_id = initial["result_summary"]["result_id"]
    explanation.mode = "success"
    factory = _NthCommitUncertainFactory(
        api_harness.engine,
        fail_on_commit=3,
    )
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            explanation,
            unit_of_work_factory=factory,
        ),
    ) as client:
        response = client.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": "uncertain-explanation-finalize"},
            json={},
        )

    assert response.status_code == 200, response.json()
    assert response.json()["data"]["idempotency_replayed"] is False
    assert response.json()["data"]["explanation"]["status"] == "SUCCEEDED"
    assert response.json()["data"]["task_status"] == "SUCCEEDED"
    assert runtime.calls == 1
    assert explanation.call_count == 2


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    [
        ("timeout", "EXPLANATION_TIMEOUT"),
        ("provider_unavailable", "EXPLANATION_PROVIDER_UNAVAILABLE"),
        ("protocol_error", "EXPLANATION_PROTOCOL_ERROR"),
    ],
)
def test_failed_explanation_retry_is_stable_and_replay_has_no_new_call(
    api_harness,
    mode: str,
    expected_code: str,
) -> None:
    actor_id = f"actor_m8_explanation_{mode}"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    explanation = MockExplanationAdapter(mode="failure")
    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            _MemoryStorage(),
            explanation,
        ),
    ) as client:
        initial = _create_explanation_failure(client)
        result_id = initial["result_summary"]["result_id"]
        explanation.mode = mode
        first = client.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": f"failed-{mode}"},
            json={},
        )
        replay = client.post(
            f"/api/v1/tool-results/{result_id}/explanations",
            headers={"Idempotency-Key": f"failed-{mode}"},
            json={},
        )

    assert first.status_code == replay.status_code == 200
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert first.json()["data"]["explanation"]["status"] == "FAILED"
    assert first.json()["data"]["explanation"]["error_code"] == expected_code
    assert (
        replay.json()["data"]["explanation"]["explanation_id"]
        == first.json()["data"]["explanation"]["explanation_id"]
    )
    assert first.json()["data"]["task_status"] == "PARTIALLY_SUCCEEDED"
    assert runtime.calls == 1
    assert explanation.call_count == 2


def test_successful_explanation_retry_cannot_promote_failed_result_task(
    api_harness,
) -> None:
    actor_id = "actor_m8_failed_result_explanation_retry"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(failed=True)
    explanation = MockExplanationAdapter(mode="failure")
    options = _client_options(
        api_harness,
        runtime,
        _MemoryStorage(),
        explanation,
    )
    options["chat_orchestration_port"] = _chat_port_for_outputs(
        ["mechanical_properties"]
    )
    with api_harness.create_client(
        actor_id,
        **options,
    ) as client:
        initial = _create_explanation_failure(
            client,
            expected_result_status="FAILED",
        )
        assert initial["result_summary"]["status"] == "FAILED"
        assert initial["task"]["status"] == "FAILED"
        explanation.mode = "success"
        retried = client.post(
            "/api/v1/tool-results/"
            f"{initial['result_summary']['result_id']}/explanations",
            headers={"Idempotency-Key": "failed-result-explanation"},
            json={},
        )

    assert retried.status_code == 200, retried.json()
    assert retried.json()["data"]["explanation"]["status"] == "SUCCEEDED"
    assert retried.json()["data"]["task_status"] == "FAILED"
    assert runtime.calls == 1
    assert explanation.call_count == 2
