from __future__ import annotations

from uuid import uuid4

from dataclasses import replace

from sqlalchemy import text

from backend.tests.api.conftest import BASE_TIME
from backend.tests.api.test_assets import _MemoryStorage, _valid_image
from backend.tests.integration.db.test_explanation_persistence import (
    _DetailedFailureExplanationAdapter,
    _FailingFinalizeFactory,
    _UnexpectedExplanationAdapter,
)
from backend.tests.integration.db.test_result_commit import (
    _FailFirstCommitFactory,
)
from materialsagent.application.explanation_service import ExplanationService
from materialsagent.application.result_service import ResultService
from materialsagent.application.tool_execution import ToolExecutionService
from materialsagent.application.tools import build_tool_registry
from materialsagent.domain.ports.tool_execution import (
    ToolClientUnavailableError,
    ToolExecutionOutput,
)
from materialsagent.infrastructure.llm.mock import (
    MockChatOrchestrationAdapter,
)
from materialsagent.infrastructure.llm.mock_explanation import (
    MockExplanationAdapter,
)


class _Runtime:
    def __init__(
        self,
        *,
        partial: bool = False,
        failed: bool = False,
    ) -> None:
        self.partial = partial
        self.failed = failed
        self.calls = 0

    def execute(self, _metadata, validated_input, _context):
        self.calls += 1
        requested = tuple(validated_input.requested_outputs)
        image = replace(
            _valid_image(),
            requested_output="sem_image" in requested,
            image_role=(
                "generated_sem"
                if "sem_image" in requested
                else "intermediate_sem"
            ),
        )
        if self.failed:
            return ToolExecutionOutput(
                status="FAILED",
                requested_outputs=requested,
                completed_outputs=(),
                failed_outputs=requested,
                data={},
                images=(image,),
                warnings=(),
                diagnostics=(),
                actual_runtime_parameters=dict(
                    validated_input.runtime_parameters
                ),
                model_bundle_id="private-model-bundle",
                error={
                    "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
                    "safe_message": "Private Runtime wording.",
                    "retryable": False,
                },
            )
        if self.partial:
            return ToolExecutionOutput(
                status="PARTIALLY_SUCCEEDED",
                requested_outputs=tuple(validated_input.requested_outputs),
                completed_outputs=("sem_image",),
                failed_outputs=("mechanical_properties",),
                data={},
                images=(image,),
                warnings=(),
                diagnostics=(),
                actual_runtime_parameters=dict(
                    validated_input.runtime_parameters
                ),
                model_bundle_id="private-model-bundle",
                error={
                    "code": "MECHANICAL_PROPERTY_PREDICTION_FAILED",
                    "safe_message": "Private Runtime wording.",
                    "retryable": False,
                },
            )
        return ToolExecutionOutput(
            status="SUCCEEDED",
            requested_outputs=tuple(validated_input.requested_outputs),
            completed_outputs=tuple(validated_input.requested_outputs),
            failed_outputs=(),
            data=(
                {
                    "yield_strength": {
                        "value": 1000.0,
                        "unit": "MPa",
                    },
                    "elongation": {"value": 8.2, "unit": "%"},
                }
                if "mechanical_properties" in requested
                else {}
            ),
            images=(image,),
            warnings=(),
            diagnostics=(),
            actual_runtime_parameters=dict(
                validated_input.runtime_parameters
            ),
            model_bundle_id="private-model-bundle",
            error=None,
        )

    def readiness(self, _metadata):
        return "AVAILABLE"


class _UnavailableRuntime:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, _metadata, _validated_input, _context):
        self.calls += 1
        raise ToolClientUnavailableError()

    def readiness(self, _metadata):
        return "AVAILABLE"


class _CountingMemoryStorage(_MemoryStorage):
    def __init__(self) -> None:
        super().__init__()
        self.put_calls = 0

    def put(self, object_key, payload, content_type, metadata=None):
        self.put_calls += 1
        return super().put(
            object_key,
            payload,
            content_type,
            metadata,
        )


def _submit(client, conversation_id: str):
    return client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": f"explanation-{uuid4().hex}"},
        json={
            "submission_mode": "NEW_TASK",
            "content_text": "完整合法 Tool 请求",
        },
    )


def _client_options(api_harness, runtime, storage, explanation_mode):
    execution = ToolExecutionService(
        api_harness.unit_of_work_factory,
        build_tool_registry(runtime),
        clock=lambda: BASE_TIME.replace(hour=1),
        seed_factory=lambda: 101,
    )
    return {
        "tool_execution_service": execution,
        "storage_service": storage,
        "m7_tool_chain_enabled": True,
        "explanation_port": MockExplanationAdapter(
            mode=explanation_mode
        ),
    }


def _chat_port_for_outputs(outputs: list[str]):
    def responder(_input):
        return {
            "route": "TOOL_EXECUTION",
            "tool_id": "zta35g_sem_virtual_lab",
            "material": "ZTA35G",
            "candidate_parameters": {
                "solution_temperature": {"value": 1000, "unit": "°C"},
                "solution_time": {"value": 3, "unit": "h"},
                "aging_temperature": {"value": 730, "unit": "°C"},
                "aging_time": {"value": 3, "unit": "h"},
            },
            "requested_outputs": outputs,
        }

    return MockChatOrchestrationAdapter(responder)


def test_public_message_executes_one_full_committed_tool_chain(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _MemoryStorage()

    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            "success",
        ),
        raise_server_exceptions=True,
    ) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={},
        ).json()["data"]["conversation_id"]
        response = _submit(client, conversation)
        task_response = client.get(
            "/api/v1/tasks/" + response.json()["data"]["task"]["task_id"]
        )
        result_response = client.get(
            "/api/v1/tool-results/"
            + response.json()["data"]["result_summary"]["result_id"]
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["task"]["status"] == "SUCCEEDED"
    assert data["task"]["selected_tool_run_id"]
    assert data["task"]["selected_result_id"]
    assert data["assistant_message"] is None
    assert data["result_summary"]["status"] == "SUCCEEDED"
    assert data["result_summary"]["completed_outputs"] == [
        "sem_image",
        "mechanical_properties",
    ]
    assert data["result_summary"]["data"]["yield_strength"]["value"] == 1000.0
    expected_provenance = {
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
    assert data["result_summary"]["provenance"] == expected_provenance
    assert data["explanation"]["status"] == "SUCCEEDED"
    assert data["explanation"]["text"]
    assert result_response.status_code == 200
    assert result_response.json()["data"]["provenance"] == expected_provenance
    assert task_response.json()["data"]["selected_result_summary"][
        "status"
    ] == "SUCCEEDED"
    assert runtime.calls == 1
    assert len(storage.objects) == 1
    assert "task_input_revision_id" not in response.text
    assert "task_input_revision_id" not in result_response.text
    assert "private-model-bundle" not in response.text
    with api_harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tool_run")) == 1
        assert connection.scalar(text("SELECT count(*) FROM asset")) == 1
        assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM result_asset_link")
        ) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM natural_language_explanation")
        ) == 1
        assert connection.scalar(
            text(
                "SELECT count(*) FROM message "
                "WHERE role='ASSISTANT'"
            )
        ) == 0


def test_partial_result_and_explanation_timeout_still_return_committed_200(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime(partial=True)
    storage = _MemoryStorage()

    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            "timeout",
        ),
        raise_server_exceptions=True,
    ) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={},
        ).json()["data"]["conversation_id"]
        response = _submit(client, conversation)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["task"]["status"] == "PARTIALLY_SUCCEEDED"
    assert data["result_summary"]["status"] == "PARTIALLY_SUCCEEDED"
    assert data["result_summary"]["completed_outputs"] == ["sem_image"]
    assert data["result_summary"]["failed_outputs"] == [
        "mechanical_properties"
    ]
    assert data["result_summary"]["data"] == {}
    assert data["explanation"]["status"] == "FAILED"
    assert data["explanation"]["text"] is None
    assert data["explanation"]["error_code"] == "EXPLANATION_TIMEOUT"
    assert runtime.calls == 1
    assert len(storage.objects) == 1


def test_unexpected_explanation_adapter_failure_returns_committed_result(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _CountingMemoryStorage()
    adapter = _UnexpectedExplanationAdapter("runtime_error")
    options = _client_options(
        api_harness,
        runtime,
        storage,
        "success",
    )
    options["explanation_port"] = adapter

    with api_harness.create_client(
        actor_id,
        **options,
        raise_server_exceptions=True,
    ) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={},
        ).json()["data"]["conversation_id"]
        response = _submit(client, conversation)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["task"]["status"] == "PARTIALLY_SUCCEEDED"
    assert data["result_summary"]["status"] == "SUCCEEDED"
    assert data["result_summary"]["data"]["yield_strength"] == {
        "value": 1000.0,
        "unit": "MPa",
    }
    assert len(data["result_summary"]["artifacts"]) == 1
    assert data["result_summary"]["artifacts"][0]["status"] == "AVAILABLE"
    assert data["explanation"]["status"] == "FAILED"
    assert data["explanation"]["text"] is None
    assert data["explanation"]["error_code"] == "EXPLANATION_FAILED"
    assert adapter.call_count == 1
    assert runtime.calls == 1
    assert storage.put_calls == 1
    assert len(storage.objects) == 1
    assert "private provider failure" not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text
    with api_harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM result_asset_link")
        ) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM asset WHERE current_status='AVAILABLE'")
        ) == 1
        assert connection.scalar(
            text(
                "SELECT count(*) FROM natural_language_explanation "
                "WHERE status='FAILED' AND text IS NULL"
            )
        ) == 1
        assert connection.scalar(
            text(
                "SELECT count(*) FROM llm_call "
                "WHERE purpose='TOOL_RESULT_EXPLANATION' "
                "AND status='FAILED'"
            )
        ) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM tool_run WHERE current_status='SUCCEEDED'")
        ) == 1


def test_sem_only_and_mechanical_only_failure_are_stable_business_results(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)

    for outputs, runtime, expected_status, expected_data in (
        (["sem_image"], _Runtime(), "SUCCEEDED", {}),
        (
            ["mechanical_properties"],
            _Runtime(failed=True),
            "FAILED",
            {},
        ),
    ):
        storage = _MemoryStorage()
        with api_harness.create_client(
            actor_id,
            **_client_options(
                api_harness,
                runtime,
                storage,
                "success",
            ),
            chat_orchestration_port=_chat_port_for_outputs(outputs),
            raise_server_exceptions=True,
        ) as client:
            conversation = client.post(
                "/api/v1/conversations",
                json={},
            ).json()["data"]["conversation_id"]
            response = _submit(client, conversation)

        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["task"]["status"] == expected_status
        assert data["result_summary"]["status"] == expected_status
        assert data["result_summary"]["data"] == expected_data
        assert data["result_summary"]["requested_outputs"] == outputs
        assert runtime.calls == 1
        assert len(storage.objects) == 1
        if outputs == ["mechanical_properties"]:
            assert data["result_summary"]["artifacts"][0]["role"] == (
                "intermediate"
            )
            assert data["result_summary"]["completed_outputs"] == []


def test_asset_failure_does_not_publish_memory_result_or_tool_unavailable(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _MemoryStorage()
    storage.unavailable = True

    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            "success",
        ),
        clock=lambda: BASE_TIME.replace(hour=2),
    ) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={},
        ).json()["data"]["conversation_id"]
        response = _submit(client, conversation)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert response.json()["error"]["code"] != "TOOL_UNAVAILABLE"
    assert runtime.calls == 1
    with api_harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM natural_language_explanation")
        ) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM tool_run WHERE current_status='FAILED'")
        ) == 1
        assert connection.scalar(
            text("SELECT count(*) FROM task WHERE current_status='FAILED'")
        ) == 1
        assert connection.scalar(
            text("SELECT duration_ms FROM tool_run")
        ) == 3_600_000


def test_runtime_transport_failure_selects_persisted_failed_tool_run(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    runtime = _UnavailableRuntime()
    storage = _MemoryStorage()

    with api_harness.create_client(
        actor_id,
        **_client_options(
            api_harness,
            runtime,
            storage,
            "success",
        ),
        raise_server_exceptions=True,
    ) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={},
        ).json()["data"]["conversation_id"]
        response = _submit(client, conversation)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "RUNTIME_UNAVAILABLE"
    assert runtime.calls == 1
    assert storage.objects == {}
    with api_harness.engine.connect() as connection:
        task_row = connection.execute(
            text(
                "SELECT current_status, selected_tool_run_id, "
                "selected_result_id FROM task"
            )
        ).mappings().one()
        run_id = connection.scalar(
            text(
                "SELECT tool_run_id FROM tool_run "
                "WHERE current_status='FAILED'"
            )
        )
        assert task_row["current_status"] == "FAILED"
        assert task_row["selected_tool_run_id"] == run_id
        assert task_row["selected_result_id"] is None
        assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM natural_language_explanation")
        ) == 0


def test_deepseek_auth_failure_is_detailed_only_in_llm_call(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _MemoryStorage()
    adapter = _DetailedFailureExplanationAdapter(
        llm_error_code="LLM_AUTHENTICATION_FAILED",
        llm_safe_error_message="LLM provider authentication failed.",
        error_code="EXPLANATION_PROVIDER_UNAVAILABLE",
        safe_error_message="Explanation provider is unavailable.",
    )
    options = _client_options(
        api_harness,
        runtime,
        storage,
        "success",
    )
    options["explanation_port"] = adapter

    with api_harness.create_client(
        actor_id,
        **options,
        raise_server_exceptions=True,
    ) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={},
        ).json()["data"]["conversation_id"]
        response = _submit(client, conversation)

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["task"]["status"] == "PARTIALLY_SUCCEEDED"
    assert data["explanation"]["error_code"] == (
        "EXPLANATION_PROVIDER_UNAVAILABLE"
    )
    assert data["explanation"]["safe_error_message"] == (
        "Explanation provider is unavailable."
    )
    assert "LLM_AUTHENTICATION_FAILED" not in response.text
    assert "LLM provider authentication failed." not in response.text
    with api_harness.engine.connect() as connection:
        call_row = connection.execute(
            text(
                "SELECT error_code, safe_error_message FROM llm_call "
                "WHERE purpose = 'TOOL_RESULT_EXPLANATION'"
            )
        ).mappings().one()
        explanation_row = connection.execute(
            text(
                "SELECT error_code, safe_error_message "
                "FROM natural_language_explanation"
            )
        ).mappings().one()
        task_row = connection.execute(
            text(
                "SELECT error_code, safe_error_message FROM task "
                "WHERE task_type = 'TOOL_EXECUTION'"
            )
        ).mappings().one()
    assert call_row == {
        "error_code": "LLM_AUTHENTICATION_FAILED",
        "safe_error_message": "LLM provider authentication failed.",
    }
    public_error = {
        "error_code": "EXPLANATION_PROVIDER_UNAVAILABLE",
        "safe_error_message": "Explanation provider is unavailable.",
    }
    assert explanation_row == public_error
    assert task_row == public_error


def test_result_commit_failure_has_no_result_explanation_or_memory_success(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _CountingMemoryStorage()
    options = _client_options(
        api_harness,
        runtime,
        storage,
        "success",
    )
    options["result_service"] = ResultService(
        _FailFirstCommitFactory(api_harness.engine),
        clock=lambda: BASE_TIME.replace(hour=1),
    )

    with api_harness.create_client(actor_id, **options) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={},
        ).json()["data"]["conversation_id"]
        response = _submit(client, conversation)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "RESULT_PERSISTENCE_FAILED"
    assert "yield_strength" not in response.text
    assert runtime.calls == 1
    assert storage.put_calls == 1
    assert len(storage.objects) == 1
    with api_harness.engine.connect() as connection:
        task_row = connection.execute(
            text(
                "SELECT current_status, selected_tool_run_id, "
                "selected_result_id, error_code "
                "FROM task WHERE task_type = 'TOOL_EXECUTION'"
            )
        ).mappings().one()
        run_row = connection.execute(
            text(
                "SELECT tool_run_id, current_status, requested_outputs, "
                "completed_outputs, failed_outputs, error_code, "
                "diagnostics, output_summary "
                "FROM tool_run"
            )
        ).mappings().one()
        asset_row = connection.execute(
            text("SELECT current_status FROM asset")
        ).mappings().one()
        assert task_row["current_status"] == "FAILED"
        assert task_row["selected_tool_run_id"] == run_row["tool_run_id"]
        assert task_row["selected_result_id"] is None
        assert task_row["error_code"] == "RESULT_PERSISTENCE_FAILED"
        assert run_row["current_status"] == "FAILED"
        assert run_row["completed_outputs"] == []
        assert run_row["failed_outputs"] == run_row["requested_outputs"]
        assert run_row["error_code"] == "RESULT_PERSISTENCE_FAILED"
        assert run_row["diagnostics"] == []
        assert run_row["output_summary"] is not None
        assert asset_row["current_status"] == "AVAILABLE"
        assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM result_asset_link")
        ) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM natural_language_explanation")
        ) == 0
        assert connection.scalar(
            text(
                "SELECT count(*) FROM llm_call "
                "WHERE purpose = 'TOOL_RESULT_EXPLANATION'"
            )
        ) == 0


def test_explanation_finalize_commit_failure_never_returns_uncommitted_text(
    api_harness,
) -> None:
    actor_id = "actor_local"
    api_harness.persist_actor(actor_id)
    runtime = _Runtime()
    storage = _MemoryStorage()
    adapter = MockExplanationAdapter(mode="success")
    failing_explanation = ExplanationService(
        _FailingFinalizeFactory(api_harness.engine),
        adapter,
        clock=lambda: BASE_TIME.replace(hour=1),
    )
    options = _client_options(
        api_harness,
        runtime,
        storage,
        "success",
    )
    options["explanation_service"] = failing_explanation

    with api_harness.create_client(actor_id, **options) as client:
        conversation = client.post(
            "/api/v1/conversations",
            json={},
        ).json()["data"]["conversation_id"]
        response = _submit(client, conversation)

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "EXPLANATION_PERSISTENCE_FAILED"
    assert "屈服强度" not in response.text
    assert adapter.call_count == 1
    assert runtime.calls == 1
    assert len(storage.objects) == 1
    with api_harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tool_result")) == 1
        assert connection.scalar(
            text(
                "SELECT count(*) FROM natural_language_explanation "
                "WHERE status='RUNNING' AND text IS NULL"
            )
        ) == 1
