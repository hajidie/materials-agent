from datetime import datetime, timezone
import json

from fastapi.testclient import TestClient


EXTERNAL_DEPENDENCY_VARIABLES = (
    "POSTGRES_HOST",
    "POSTGRES_PASSWORD",
    "MINIO_ENDPOINT",
    "MINIO_SECRET_KEY",
    "LLM_API_KEY",
    "ZTA35G_RUNTIME_URL",
    "ZTA35G_RUNTIME_TOKEN",
)


def _create_client() -> TestClient:
    from materialsagent.main import create_app

    return TestClient(create_app())


def _assert_utc_timestamp(value: str) -> None:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)


def test_live_returns_exact_safe_response() -> None:
    with _create_client() as client:
        response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert set(response.json()) == {"status", "checked_at", "request_id"}
    assert response.json()["status"] == "LIVE"
    assert response.json()["request_id"].startswith("req_")
    _assert_utc_timestamp(response.json()["checked_at"])


def test_ready_is_not_ready_without_breaking_live() -> None:
    with _create_client() as client:
        ready_response = client.get("/api/v1/health/ready")
        live_response = client.get("/api/v1/health/live")

    assert ready_response.status_code == 503
    assert set(ready_response.json()) == {"status", "checked_at", "request_id"}
    assert ready_response.json()["status"] == "NOT_READY"
    assert live_response.status_code == 200
    assert live_response.json()["status"] == "LIVE"


def test_repeated_health_reads_have_independent_request_ids() -> None:
    with _create_client() as client:
        responses = [
            client.get("/api/v1/health/live"),
            client.get("/api/v1/health/live"),
            client.get("/api/v1/health/ready"),
            client.get("/api/v1/health/ready"),
        ]

    request_ids = [response.json()["request_id"] for response in responses]
    assert len(set(request_ids)) == len(request_ids)
    assert [response.status_code for response in responses] == [200, 200, 503, 503]


def test_missing_future_dependency_configuration_does_not_block_live(
    monkeypatch,
) -> None:
    for variable_name in EXTERNAL_DEPENDENCY_VARIABLES:
        monkeypatch.delenv(variable_name, raising=False)

    with _create_client() as client:
        response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json()["status"] == "LIVE"


def test_unknown_path_returns_controlled_404_without_sensitive_data() -> None:
    secret_value = "secret-header-value"

    with _create_client() as client:
        response = client.get(
            "/does-not-exist",
            headers={"X-Secret-Token": secret_value},
        )

    body = response.text
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}
    assert secret_value not in body
    assert "traceback" not in body.lower()
    assert "D:\\ProgramData" not in body
    assert "C:\\Users" not in body


def test_request_log_is_json_and_correlates_request_id(capsys) -> None:
    secret_value = "secret-log-value"

    with _create_client() as client:
        response = client.get(
            "/api/v1/health/live",
            headers={"Authorization": f"Bearer {secret_value}"},
        )

    captured = capsys.readouterr().out
    log_entries = []
    for line in captured.splitlines():
        try:
            log_entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    request_log = next(
        entry
        for entry in log_entries
        if entry.get("event") == "http_request_completed"
    )
    assert {
        "timestamp",
        "level",
        "event",
        "request_id",
        "method",
        "path",
        "status_code",
        "duration_ms",
    } <= set(request_log)
    assert request_log["request_id"] == response.json()["request_id"]
    assert request_log["method"] == "GET"
    assert request_log["path"] == "/api/v1/health/live"
    assert request_log["status_code"] == 200
    assert request_log["duration_ms"] >= 0
    assert secret_value not in captured
    assert "Authorization" not in captured
    assert "D:\\ProgramData" not in captured
    assert "C:\\Users" not in captured
