from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from urllib.parse import urlparse
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from materialsagent.infrastructure.db.actor import ActorRow
from materialsagent.infrastructure.db.asset import AssetRow
from materialsagent.infrastructure.db.conversation_task import (
    ConversationRow,
    MessageRow,
    TaskInputRevisionRow,
    TaskRow,
)
from materialsagent.infrastructure.db.explanation import (
    NaturalLanguageExplanationRow,
)
from materialsagent.infrastructure.db.idempotency_record import (
    IdempotencyRecordRow,
)
from materialsagent.infrastructure.db.llm_call import LLMCallRow
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.tool_result import (
    ResultAssetLinkRow,
    ToolResultRow,
)
from materialsagent.infrastructure.db.tool_run import ToolRunRow
from materialsagent.infrastructure.llm.mock_explanation import (
    MockExplanationAdapter,
)


RESOURCE_ROWS = {
    "actors": ActorRow,
    "conversations": ConversationRow,
    "messages": MessageRow,
    "tasks": TaskRow,
    "revisions": TaskInputRevisionRow,
    "tool_runs": ToolRunRow,
    "tool_results": ToolResultRow,
    "assets": AssetRow,
    "asset_links": ResultAssetLinkRow,
    "explanations": NaturalLanguageExplanationRow,
    "llm_calls": LLMCallRow,
    "idempotency_records": IdempotencyRecordRow,
}


def _counts(e2e_app_factory) -> dict[str, int]:
    with e2e_app_factory.engine.connect() as connection:
        return {
            name: connection.scalar(select(func.count()).select_from(row))
            for name, row in RESOURCE_ROWS.items()
        } | {"minio_objects": e2e_app_factory.minio_object_count()}


def _create_conversation(client) -> str:
    response = client.post(
        "/api/v1/conversations",
        headers={"Idempotency-Key": f"e2e-conversation-{uuid4().hex}"},
        json={},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["conversation_id"]


def _submit(
    client,
    conversation_id: str,
    *,
    key: str,
    content: str = "完整合法 Tool 请求",
    mode: str = "NEW_TASK",
    target_task_id: str | None = None,
):
    body: dict[str, object] = {
        "submission_mode": mode,
        "content_text": content,
    }
    if target_task_id is not None:
        body["target_task_id"] = target_task_id
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": key},
        json=body,
    )


def _timeline(client, conversation_id: str) -> dict[str, object]:
    response = client.get(
        f"/api/v1/conversations/{conversation_id}/timeline"
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def _controlled_tool_payload(
    *,
    material: object = "ZTA35G",
    solution_temperature: object = 1000,
    solution_temperature_unit: object = "°C",
    solution_time: object = 3,
    solution_time_unit: object = "h",
    aging_temperature: object = 730,
    aging_temperature_unit: object = "°C",
    aging_time: object = 3,
    aging_time_unit: object = "h",
    requested_outputs: object = (
        "sem_image",
        "mechanical_properties",
    ),
) -> dict[str, object]:
    return {
        "route": "TOOL_CANDIDATES",
        "candidates": [
            {
                "tool_id": "zta35g_sem_virtual_lab",
                "proposed_arguments": {
                    "material": material,
                    "solution_temperature": {
                        "value": solution_temperature,
                        "unit": solution_temperature_unit,
                    },
                    "solution_time": {
                        "value": solution_time,
                        "unit": solution_time_unit,
                    },
                    "aging_temperature": {
                        "value": aging_temperature,
                        "unit": aging_temperature_unit,
                    },
                    "aging_time": {
                        "value": aging_time,
                        "unit": aging_time_unit,
                    },
                    "requested_outputs": list(requested_outputs),
                },
            }
        ],
    }


def _unused_port(runtime) -> int:
    parsed = urlparse(runtime.base_url)
    assert parsed.hostname == "127.0.0.1"
    assert parsed.port is not None
    return parsed.port


def _secret_text(value: object) -> str:
    getter = getattr(value, "get_secret_value", None)
    if callable(getter):
        return str(getter())
    return str(value)


def _internal_values(e2e_app_factory, runtime=None) -> list[str]:
    settings = e2e_app_factory.settings
    values = [
        _secret_text(settings.postgres_password),
        _secret_text(settings.minio_access_key),
        _secret_text(settings.minio_secret_key),
        str(settings.minio_endpoint),
        str(settings.minio_bucket),
        e2e_app_factory.repo_root,
    ]
    timeline_key = settings.timeline_cursor_signing_key
    if timeline_key is not None:
        values.append(_secret_text(timeline_key))
    if runtime is not None:
        values.extend([runtime.token, runtime.base_url])
    return [value for value in values if value]


def _assert_value_absent(value: object, public: str) -> None:
    text = _secret_text(value)
    variants = {
        text,
        text.replace("\\", "/"),
        json.dumps(text, ensure_ascii=False)[1:-1],
    }
    for variant in variants:
        if variant:
            assert variant not in public


def _assert_safe_error(
    response,
    *,
    internal_values: tuple[str, ...] | list[str] = (),
) -> None:
    assert response.headers["content-type"].startswith("application/json")
    for value in internal_values:
        _assert_value_absent(value, response.text)
    lowered = response.text.lower()
    for forbidden in (
        "object_key",
        "traceback",
        "sqlalchemy",
        "psycopg",
        "urllib3",
        "connectionrefusederror",
        "c:\\",
    ):
        assert forbidden not in lowered


def _assert_failed_task_and_run(
    e2e_app_factory,
    response,
    *,
    error_code: str,
) -> None:
    task_id = response.json()["resource"]["task_id"]
    with create_session_factory(e2e_app_factory.engine)() as session:
        task = session.get(TaskRow, task_id)
        runs = list(
            session.scalars(
                select(ToolRunRow)
                .where(ToolRunRow.task_id == task_id)
                .order_by(ToolRunRow.attempt_no)
            ).all()
        )
        assert task is not None
        assert task.current_status == "FAILED"
        assert task.error_code == error_code
        assert len(runs) == 1
        assert runs[0].current_status == "FAILED"
        assert runs[0].error_code == error_code
        assert runs[0].safe_error_message


def test_success_partial_result_uses_loopback_http_and_keeps_safe_timeline(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("partial_success") as runtime:
        with e2e_app_factory.client(runtime=runtime) as client:
            conversation_id = _create_conversation(client)
            response = _submit(
                client,
                conversation_id,
                key="m11b-partial-success",
            )
            timeline = _timeline(client, conversation_id)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    task_id = data["task"]["task_id"]
    result_id = data["result_summary"]["result_id"]
    asset_id = data["result_summary"]["artifacts"][0]["asset_id"]
    assert data["task"]["status"] == "PARTIALLY_SUCCEEDED"
    assert data["result_summary"]["status"] == "PARTIALLY_SUCCEEDED"
    assert data["result_summary"]["completed_outputs"] == ["sem_image"]
    assert data["result_summary"]["failed_outputs"] == [
        "mechanical_properties"
    ]
    assert data["result_summary"]["data"] == {}
    assert data["result_summary"]["artifacts"][0]["status"] == "AVAILABLE"
    assert "未获得力学性能结果" in data["explanation"]["text"]
    assert "MPa" not in data["explanation"]["text"]
    assert len(timeline["items"]) == 1
    card = timeline["items"][0]
    assert card["task"]["status"] == "PARTIALLY_SUCCEEDED"
    selected_run = card["tool_runs"]["selected_tool_run"]
    assert selected_run["status"] == "PARTIALLY_SUCCEEDED"
    assert selected_run["completed_outputs"] == ["sem_image"]
    assert selected_run["failed_outputs"] == ["mechanical_properties"]
    assert selected_run["error"] == {
        "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
        "message": "Mechanical property prediction failed.",
    }
    assert selected_run["diagnostics_summary"][-1] == {
        "step": "mechanical_property_prediction",
        "status": "FAILED",
        "duration_ms": 0,
        "error_code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
        "safe_error_message": "Mechanical property prediction failed.",
    }
    assert card["result"]["status"] == "PARTIALLY_SUCCEEDED"
    assert card["result"]["completed_outputs"] == ["sem_image"]
    assert card["result"]["failed_outputs"] == ["mechanical_properties"]
    assert card["assets"][0]["status"] == "AVAILABLE"
    assert "object_key" not in response.text
    assert "object_key" not in str(timeline)
    assert runtime.execution_count == 1
    with create_session_factory(e2e_app_factory.engine)() as session:
        task = session.get(TaskRow, task_id)
        run = session.get(ToolRunRow, data["task"]["selected_tool_run_id"])
        stored_result = session.get(ToolResultRow, result_id)
        asset = session.get(AssetRow, asset_id)
        assert task is not None
        assert run is not None
        assert stored_result is not None
        assert asset is not None
        assert task.current_status == "PARTIALLY_SUCCEEDED"
        assert run.current_status == "PARTIALLY_SUCCEEDED"
        assert stored_result.status == "PARTIALLY_SUCCEEDED"
        assert asset.current_status == "AVAILABLE"


def test_input_error_empty_content_stops_before_task_and_runtime(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("success") as runtime:
        with e2e_app_factory.client(runtime=runtime) as client:
            conversation_id = _create_conversation(client)
            before = _counts(e2e_app_factory)
            response = _submit(
                client,
                conversation_id,
                key="m11b-empty-content",
                content="",
            )
            after = _counts(e2e_app_factory)

    assert response.status_code == 422
    assert after == before
    assert runtime.execution_count == 0
    _assert_safe_error(
        response,
        internal_values=_internal_values(e2e_app_factory, runtime),
    )


@pytest.mark.parametrize(
    ("case_name", "payload"),
    [
        (
            "illegal_unit",
            _controlled_tool_payload(solution_time_unit="day"),
        ),
        (
            "out_of_range",
            _controlled_tool_payload(solution_temperature=1200),
        ),
        (
            "unsupported_material",
            _controlled_tool_payload(material="OTHER"),
        ),
        (
            "invalid_requested_outputs",
            _controlled_tool_payload(
                requested_outputs=("sem_image", "unknown_output")
            ),
        ),
    ],
)
def test_input_error_controlled_candidate_never_calls_runtime_or_minio(
    e2e_app_factory,
    runtime_http_factory,
    case_name: str,
    payload: dict[str, object],
) -> None:
    def responder(_orchestration_input):
        return payload

    with runtime_http_factory("success") as runtime:
        with e2e_app_factory.client(
            runtime=runtime,
            chat_responder=responder,
        ) as client:
            conversation_id = _create_conversation(client)
            before = _counts(e2e_app_factory)
            response = _submit(
                client,
                conversation_id,
                key=f"m11b-input-{case_name}",
                content=f"controlled {case_name}",
            )
            after = _counts(e2e_app_factory)

    assert response.status_code == 422, response.text
    body = response.json()
    task_id = body["resource"]["task_id"]
    assert body["resource"]["conversation_id"] == conversation_id
    assert runtime.execution_count == 0
    assert after["tasks"] == before["tasks"] + 1
    assert after["messages"] == before["messages"] + 1
    assert after["revisions"] == before["revisions"] + 1
    assert after["tool_runs"] == before["tool_runs"]
    assert after["tool_results"] == before["tool_results"]
    assert after["assets"] == before["assets"]
    assert after["asset_links"] == before["asset_links"]
    assert after["minio_objects"] == before["minio_objects"]
    with create_session_factory(e2e_app_factory.engine)() as session:
        task = session.get(TaskRow, task_id)
        assert task is not None
        assert task.current_status == "FAILED"
        assert task.error_code == "VALIDATION_FAILED"
        assert session.scalar(
            select(func.count())
            .select_from(ToolRunRow)
            .where(ToolRunRow.task_id == task_id)
        ) == 0
    _assert_safe_error(
        response,
        internal_values=_internal_values(e2e_app_factory, runtime),
    )


def test_dependency_failure_postgresql_unavailable_is_safe_and_isolated(
    e2e_app_factory,
    runtime_http_factory,
    monkeypatch,
) -> None:
    monkeypatch.setenv("PGCONNECT_TIMEOUT", "1")
    before = _counts(e2e_app_factory)
    with runtime_http_factory("unavailable") as unused:
        with e2e_app_factory.client(
            settings_overrides={
                "postgres_port": _unused_port(unused),
            },
        ) as client:
            response = client.post(
                "/api/v1/conversations",
                headers={"Idempotency-Key": "postgres-unavailable-conversation"},
                json={},
            )
    after = _counts(e2e_app_factory)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert after == before
    _assert_safe_error(
        response,
        internal_values=_internal_values(e2e_app_factory, unused),
    )


def test_dependency_failure_minio_unavailable_never_publishes_result(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("success") as runtime:
        with runtime_http_factory("unavailable") as unavailable_storage:
            endpoint = (
                "http://127.0.0.1:"
                f"{_unused_port(unavailable_storage)}"
            )
            with e2e_app_factory.client(
                runtime=runtime,
                settings_overrides={
                    "minio_endpoint": endpoint,
                    "minio_secure": False,
                },
            ) as client:
                conversation_id = _create_conversation(client)
                before = _counts(e2e_app_factory)
                response = _submit(
                    client,
                    conversation_id,
                    key="m11b-minio-unavailable",
                )
                after = _counts(e2e_app_factory)

    assert response.status_code == 503, response.text
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert runtime.execution_count == 1
    assert after["tool_runs"] == before["tool_runs"] + 1
    assert after["tool_results"] == before["tool_results"]
    assert after["assets"] == before["assets"] + 1
    assert after["asset_links"] == before["asset_links"]
    assert after["minio_objects"] == before["minio_objects"]
    _assert_failed_task_and_run(
        e2e_app_factory,
        response,
        error_code="DEPENDENCY_UNAVAILABLE",
    )
    _assert_safe_error(
        response,
        internal_values=(
            _internal_values(e2e_app_factory, runtime) + [endpoint]
        ),
    )
    task_id = response.json()["resource"]["task_id"]
    with create_session_factory(e2e_app_factory.engine)() as session:
        assets = list(
            session.scalars(
                select(AssetRow).where(AssetRow.task_id == task_id)
            ).all()
        )
        assert len(assets) == 1
        assert assets[0].current_status == "FAILED"
        assert assets[0].error_code is not None
        assert assets[0].safe_error_message is not None


def test_dependency_failure_runtime_unavailable_has_one_failed_run(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("unavailable") as runtime:
        with e2e_app_factory.client(runtime=runtime) as client:
            conversation_id = _create_conversation(client)
            before = _counts(e2e_app_factory)
            response = _submit(
                client,
                conversation_id,
                key="m11b-runtime-unavailable",
            )
            after = _counts(e2e_app_factory)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RUNTIME_UNAVAILABLE"
    assert runtime.execution_count == 0
    assert after["tool_runs"] == before["tool_runs"] + 1
    assert after["tool_results"] == before["tool_results"]
    assert after["assets"] == before["assets"]
    assert after["minio_objects"] == before["minio_objects"]
    _assert_failed_task_and_run(
        e2e_app_factory,
        response,
        error_code="RUNTIME_UNAVAILABLE",
    )
    _assert_safe_error(
        response,
        internal_values=_internal_values(e2e_app_factory, runtime),
    )


def test_dependency_failure_runtime_timeout_rejects_late_result(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("timeout") as runtime:
        with e2e_app_factory.client(
            runtime=runtime,
            runtime_timeout_seconds=0.05,
        ) as client:
            conversation_id = _create_conversation(client)
            before = _counts(e2e_app_factory)
            try:
                response = _submit(
                    client,
                    conversation_id,
                    key="m11b-runtime-timeout",
                )
            finally:
                runtime.release.set()
            after = _counts(e2e_app_factory)

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "RUNTIME_TIMEOUT"
    assert runtime.execution_count == 1
    assert after["tool_runs"] == before["tool_runs"] + 1
    assert after["tool_results"] == before["tool_results"]
    assert after["assets"] == before["assets"]
    assert after["minio_objects"] == before["minio_objects"]
    _assert_failed_task_and_run(
        e2e_app_factory,
        response,
        error_code="RUNTIME_TIMEOUT",
    )
    _assert_safe_error(
        response,
        internal_values=_internal_values(e2e_app_factory, runtime),
    )


def test_dependency_failure_runtime_busy_is_bounded_without_auto_retry(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("busy") as runtime:
        with e2e_app_factory.client(runtime=runtime) as client:
            first_conversation = _create_conversation(client)
            second_conversation = _create_conversation(client)

            def submit_first():
                return _submit(
                    client,
                    first_conversation,
                    key="m11b-runtime-busy-first",
                )

            with ThreadPoolExecutor(max_workers=1) as executor:
                first_future = executor.submit(submit_first)
                assert runtime.entered.wait(timeout=5)
                busy = _submit(
                    client,
                    second_conversation,
                    key="m11b-runtime-busy-second",
                )
                runtime.release.set()
                first = first_future.result(timeout=10)

    assert first.status_code == 200, first.text
    assert busy.status_code == 503, busy.text
    assert busy.json()["error"]["code"] == "RUNTIME_BUSY"
    assert runtime.execution_count == 1
    _assert_failed_task_and_run(
        e2e_app_factory,
        busy,
        error_code="RUNTIME_BUSY",
    )
    _assert_safe_error(
        busy,
        internal_values=_internal_values(e2e_app_factory, runtime),
    )


@pytest.mark.parametrize("mode", ["failure", "timeout"])
def test_dependency_failure_explanation_preserves_committed_result(
    e2e_app_factory,
    runtime_http_factory,
    mode: str,
) -> None:
    explanation = MockExplanationAdapter(mode=mode)
    with runtime_http_factory("success") as runtime:
        with e2e_app_factory.client(
            runtime=runtime,
            explanation=explanation,
        ) as client:
            conversation_id = _create_conversation(client)
            response = _submit(
                client,
                conversation_id,
                key=f"m11b-explanation-{mode}",
            )
            timeline = _timeline(client, conversation_id)

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    task_id = data["task"]["task_id"]
    assert data["task"]["status"] == "PARTIALLY_SUCCEEDED"
    assert data["result_summary"]["status"] == "SUCCEEDED"
    assert data["result_summary"]["artifacts"][0]["status"] == "AVAILABLE"
    assert data["explanation"]["status"] == "FAILED"
    assert timeline["items"][0]["result"]["status"] == "SUCCEEDED"
    assert timeline["items"][0]["explanation"]["status"] == "FAILED"
    assert runtime.execution_count == 1
    assert explanation.call_count == 1
    with create_session_factory(e2e_app_factory.engine)() as session:
        task = session.get(TaskRow, task_id)
        run = session.get(ToolRunRow, data["task"]["selected_tool_run_id"])
        stored_result = session.get(
            ToolResultRow,
            data["result_summary"]["result_id"],
        )
        asset = session.get(
            AssetRow,
            data["result_summary"]["artifacts"][0]["asset_id"],
        )
        assert task is not None
        assert run is not None
        assert stored_result is not None
        assert asset is not None
        assert task.current_status == "PARTIALLY_SUCCEEDED"
        assert run.current_status == "SUCCEEDED"
        assert stored_result.status == "SUCCEEDED"
        assert asset.current_status == "AVAILABLE"


def test_idempotency_task_create_replay_has_zero_resource_increment(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("success") as runtime:
        with e2e_app_factory.client(runtime=runtime) as client:
            conversation_id = _create_conversation(client)
            first = _submit(
                client,
                conversation_id,
                key="m11b-task-create-replay",
            )
            after_first = _counts(e2e_app_factory)
            replay = _submit(
                client,
                conversation_id,
                key="m11b-task-create-replay",
            )
            after_replay = _counts(e2e_app_factory)

    assert first.status_code == replay.status_code == 200
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert replay.json()["data"]["task"]["task_id"] == (
        first.json()["data"]["task"]["task_id"]
    )
    assert after_replay == after_first
    assert runtime.execution_count == 1


def test_idempotency_supplement_replay_does_not_duplicate_resources(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("success") as runtime:
        with e2e_app_factory.client(runtime=runtime) as client:
            conversation_id = _create_conversation(client)
            initial = _submit(
                client,
                conversation_id,
                key="m11b-supplement-initial",
                content="缺 aging_temperature",
            )
            task_id = initial.json()["data"]["task"]["task_id"]
            before = _counts(e2e_app_factory)
            first = _submit(
                client,
                conversation_id,
                key="m11b-supplement-replay",
                content="730 °C",
                mode="SUPPLEMENT_TASK",
                target_task_id=task_id,
            )
            after_first = _counts(e2e_app_factory)
            replay = _submit(
                client,
                conversation_id,
                key="m11b-supplement-replay",
                content="730 °C",
                mode="SUPPLEMENT_TASK",
                target_task_id=task_id,
            )
            after_replay = _counts(e2e_app_factory)

    assert initial.status_code == first.status_code == replay.status_code == 200
    assert first.json()["data"]["task"]["task_id"] == task_id
    assert replay.json()["data"]["task"]["task_id"] == task_id
    assert first.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert after_first["messages"] == before["messages"] + 1
    assert after_first["revisions"] == before["revisions"] + 1
    assert after_first["tool_runs"] == before["tool_runs"] + 1
    assert after_first["tool_results"] == before["tool_results"] + 1
    assert after_replay == after_first
    assert runtime.execution_count == 1


def test_idempotency_same_key_different_digest_is_conflict_without_side_effect(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("success") as runtime:
        with e2e_app_factory.client(runtime=runtime) as client:
            conversation_id = _create_conversation(client)
            first = _submit(
                client,
                conversation_id,
                key="m11b-digest-conflict",
            )
            before_conflict = _counts(e2e_app_factory)
            conflict = _submit(
                client,
                conversation_id,
                key="m11b-digest-conflict",
                content="不同请求摘要",
            )
            after_conflict = _counts(e2e_app_factory)

    assert first.status_code == 200
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert after_conflict == before_conflict
    assert runtime.execution_count == 1
    _assert_safe_error(
        conflict,
        internal_values=_internal_values(e2e_app_factory, runtime),
    )


def test_retry_tool_replay_executes_exactly_one_new_http_attempt(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("fail_once_then_success") as runtime:
        with e2e_app_factory.client(runtime=runtime) as client:
            conversation_id = _create_conversation(client)
            initial = _submit(
                client,
                conversation_id,
                key="m11b-tool-retry-initial",
            )
            task_id = initial.json()["resource"]["task_id"]
            before_timeline = _timeline(client, conversation_id)
            before_retry = _counts(e2e_app_factory)
            retried = client.post(
                f"/api/v1/tasks/{task_id}/tool-runs",
                headers={"Idempotency-Key": "m11b-tool-retry"},
                json={},
            )
            after_retry = _counts(e2e_app_factory)
            replay = client.post(
                f"/api/v1/tasks/{task_id}/tool-runs",
                headers={"Idempotency-Key": "m11b-tool-retry"},
                json={},
            )
            after_replay = _counts(e2e_app_factory)
            after_timeline = _timeline(client, conversation_id)

    with create_session_factory(e2e_app_factory.engine)() as session:
        runs = list(
            session.scalars(
                select(ToolRunRow)
                .where(ToolRunRow.task_id == task_id)
                .order_by(ToolRunRow.attempt_no)
            ).all()
        )
        task = session.get(TaskRow, task_id)
        stored_results = list(
            session.scalars(
                select(ToolResultRow).where(
                    ToolResultRow.task_id == task_id
                )
            ).all()
        )
        stored_assets = list(
            session.scalars(
                select(AssetRow).where(AssetRow.task_id == task_id)
            ).all()
        )

    assert initial.status_code == 502, initial.text
    assert retried.status_code == replay.status_code == 200
    assert retried.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert retried.json()["data"]["task_id"] == task_id
    assert replay.json()["data"]["task_id"] == task_id
    assert retried.json()["data"]["task_status"] == "SUCCEEDED"
    assert replay.json()["data"]["task_status"] == "SUCCEEDED"
    assert retried.json()["data"]["tool_run"]["attempt_no"] == 2
    assert after_retry["tool_runs"] == before_retry["tool_runs"] + 1
    assert after_retry["tool_results"] == before_retry["tool_results"] + 1
    assert after_retry["assets"] == before_retry["assets"] + 1
    assert after_replay == after_retry
    assert runtime.execution_count == 2
    before_card = before_timeline["items"][0]
    after_card = after_timeline["items"][0]
    assert before_card["item_id"] == after_card["item_id"] == task_id
    assert before_card["anchor_at"] == after_card["anchor_at"]
    assert after_card["tool_runs"]["attempt_count"] == 2
    assert after_card["tool_runs"]["selected_tool_run"]["attempt_no"] == 2
    assert after_card["task"]["status"] == "SUCCEEDED"
    assert [run.current_status for run in runs] == ["FAILED", "SUCCEEDED"]
    assert runs[0].error_code == "INTERNAL_RUNTIME_ERROR"
    assert runs[0].execution_input["runtime_parameters"]["seed"] != (
        runs[1].execution_input["runtime_parameters"]["seed"]
    )
    assert task is not None
    assert task.current_status == "SUCCEEDED"
    assert task.selected_tool_run_id == runs[1].tool_run_id
    assert len(stored_results) == len(stored_assets) == 1
    assert task.selected_result_id == stored_results[0].result_id
    assert stored_results[0].tool_run_id == runs[1].tool_run_id
    assert stored_assets[0].producer_tool_run_id == runs[1].tool_run_id


def test_retry_explanation_replay_never_calls_runtime_or_storage(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    explanation = MockExplanationAdapter(mode="failure")
    with runtime_http_factory("success") as runtime:
        with e2e_app_factory.client(
            runtime=runtime,
            explanation=explanation,
        ) as client:
            conversation_id = _create_conversation(client)
            initial = _submit(
                client,
                conversation_id,
                key="m11b-explanation-retry-initial",
            )
            result_id = initial.json()["data"]["result_summary"]["result_id"]
            before_timeline = _timeline(client, conversation_id)
            before_retry = _counts(e2e_app_factory)
            explanation.mode = "success"
            retried = client.post(
                f"/api/v1/tool-results/{result_id}/explanations",
                headers={"Idempotency-Key": "m11b-explanation-retry"},
                json={},
            )
            after_retry = _counts(e2e_app_factory)
            replay = client.post(
                f"/api/v1/tool-results/{result_id}/explanations",
                headers={"Idempotency-Key": "m11b-explanation-retry"},
                json={},
            )
            after_replay = _counts(e2e_app_factory)
            after_timeline = _timeline(client, conversation_id)

    task_id = initial.json()["data"]["task"]["task_id"]
    with create_session_factory(e2e_app_factory.engine)() as session:
        task = session.get(TaskRow, task_id)
        explanations = list(
            session.scalars(
                select(NaturalLanguageExplanationRow)
                .where(
                    NaturalLanguageExplanationRow.result_id == result_id
                )
                .order_by(NaturalLanguageExplanationRow.attempt_no)
            ).all()
        )
        llm_calls = list(
            session.scalars(
                select(LLMCallRow).where(
                    LLMCallRow.input_result_id == result_id,
                    LLMCallRow.purpose == "TOOL_RESULT_EXPLANATION",
                )
            ).all()
        )

    assert initial.status_code == 200, initial.text
    assert initial.json()["data"]["task"]["status"] == "PARTIALLY_SUCCEEDED"
    assert retried.status_code == replay.status_code == 200
    assert retried.json()["data"]["idempotency_replayed"] is False
    assert replay.json()["data"]["idempotency_replayed"] is True
    assert retried.json()["data"]["task_id"] == task_id
    assert replay.json()["data"]["task_id"] == task_id
    assert retried.json()["data"]["task_status"] == "SUCCEEDED"
    assert replay.json()["data"]["task_status"] == "SUCCEEDED"
    assert retried.json()["data"]["explanation"]["attempt_no"] == 2
    assert retried.json()["data"]["explanation"]["status"] == "SUCCEEDED"
    assert after_retry["explanations"] == before_retry["explanations"] + 1
    assert after_retry["llm_calls"] == before_retry["llm_calls"] + 1
    for name in ("tool_runs", "tool_results", "assets", "asset_links"):
        assert after_retry[name] == before_retry[name]
    assert after_retry["minio_objects"] == before_retry["minio_objects"]
    assert after_replay == after_retry
    assert runtime.execution_count == 1
    assert before_timeline["items"][0]["anchor_at"] == (
        after_timeline["items"][0]["anchor_at"]
    )
    assert after_timeline["items"][0]["task"]["status"] == "SUCCEEDED"
    assert after_timeline["items"][0]["explanation"]["status"] == "SUCCEEDED"
    assert task is not None
    assert task.current_status == "SUCCEEDED"
    assert [item.status for item in explanations] == [
        "FAILED",
        "SUCCEEDED",
    ]
    assert [item.attempt_no for item in explanations] == [1, 2]
    assert all(item.result_id == result_id for item in explanations)
    assert len(llm_calls) == 2


def test_asset_inline_attachment_headers_are_safe_and_stable(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("success") as runtime:
        with e2e_app_factory.client(runtime=runtime) as client:
            conversation_id = _create_conversation(client)
            submitted = _submit(
                client,
                conversation_id,
                key="m11b-asset-content",
            )
            artifact = submitted.json()["data"]["result_summary"][
                "artifacts"
            ][0]
            asset_id = artifact["asset_id"]
            inline = client.get(f"/api/v1/assets/{asset_id}/content")
            attachment = client.get(
                f"/api/v1/assets/{asset_id}/content",
                params={"disposition": "attachment"},
            )

    assert submitted.status_code == 200
    assert inline.status_code == attachment.status_code == 200
    assert inline.headers["content-type"] == "image/png"
    assert attachment.headers["content-type"] == "image/png"
    assert inline.headers["content-disposition"].startswith("inline;")
    assert attachment.headers["content-disposition"].startswith(
        "attachment;"
    )
    assert inline.content == attachment.content
    assert inline.content.startswith(b"\x89PNG\r\n\x1a\n")
    for response in (inline, attachment):
        headers = json.dumps(dict(response.headers)).lower()
        for forbidden in (
            "object_key",
            "bucket",
            "minio",
            "127.0.0.1:9000",
            "token",
            "c:\\",
        ):
            assert forbidden not in headers


def test_security_public_json_and_headers_filter_internal_values(
    e2e_app_factory,
    runtime_http_factory,
) -> None:
    with runtime_http_factory("success") as runtime:
        with e2e_app_factory.client(runtime=runtime) as client:
            conversation_id = _create_conversation(client)
            submitted = _submit(
                client,
                conversation_id,
                key="m11b-security-filter",
            )
            data = submitted.json()["data"]
            task = client.get(
                f"/api/v1/tasks/{data['task']['task_id']}"
            )
            result = client.get(
                "/api/v1/tool-results/"
                f"{data['result_summary']['result_id']}"
            )
            timeline = client.get(
                f"/api/v1/conversations/{conversation_id}/timeline"
            )
            asset_id = data["result_summary"]["artifacts"][0]["asset_id"]
            content = client.get(
                f"/api/v1/assets/{asset_id}/content"
            )
            with e2e_app_factory.engine.connect() as connection:
                object_key = connection.scalar(
                    select(AssetRow.object_key).where(
                        AssetRow.asset_id == asset_id
                    )
                )

    actual_values = _internal_values(e2e_app_factory, runtime) + [
        object_key
    ]
    public = "\n".join(
        [
            submitted.text,
            task.text,
            result.text,
            timeline.text,
            json.dumps(dict(content.headers), ensure_ascii=False),
        ]
    )
    for value in actual_values:
        if value:
            _assert_value_absent(value, public)
    lowered = public.lower()
    for forbidden in (
        "data_base64",
        "traceback",
        "python exception",
        "select ",
        "insert ",
        "full prompt",
        "weight_path",
        "model_bundle",
    ):
        assert forbidden not in lowered
