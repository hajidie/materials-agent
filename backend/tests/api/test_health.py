from datetime import datetime, timezone
import json

from fastapi.testclient import TestClient
import pytest


EXTERNAL_DEPENDENCY_VARIABLES = (
    "POSTGRES_HOST",
    "POSTGRES_PASSWORD",
    "MINIO_ENDPOINT",
    "MINIO_SECRET_KEY",
    "LLM_API_KEY",
    "ZTA35G_RUNTIME_URL",
    "ZTA35G_RUNTIME_TOKEN",
)


def _create_client(
    *,
    postgresql_available: bool = False,
    object_storage_available: bool = False,
) -> TestClient:
    from materialsagent.application.readiness import ReadinessService
    from materialsagent.infrastructure.config import load_settings
    from materialsagent.main import create_app

    readiness_service = ReadinessService(
        postgresql_probe=lambda: postgresql_available,
        object_storage_probe=lambda: object_storage_available,
    )
    return TestClient(
        create_app(
            settings=load_settings({}),
            readiness_service=readiness_service,
        )
    )


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


@pytest.mark.parametrize(
    (
        "postgresql_available",
        "object_storage_available",
        "expected_status",
        "expected_http_status",
        "expected_component_statuses",
    ),
    [
        (True, True, "READY", 200, ["AVAILABLE", "AVAILABLE"]),
        (True, False, "DEGRADED", 200, ["AVAILABLE", "UNAVAILABLE"]),
        (False, True, "NOT_READY", 503, ["UNAVAILABLE", "AVAILABLE"]),
        (False, False, "NOT_READY", 503, ["UNAVAILABLE", "UNAVAILABLE"]),
    ],
)
def test_ready_returns_exact_dependency_aggregation(
    postgresql_available: bool,
    object_storage_available: bool,
    expected_status: str,
    expected_http_status: int,
    expected_component_statuses: list[str],
) -> None:
    with _create_client(
        postgresql_available=postgresql_available,
        object_storage_available=object_storage_available,
    ) as client:
        response = client.get("/api/v1/health/ready")

    body = response.json()
    assert response.status_code == expected_http_status
    assert set(body) == {"status", "components", "checked_at", "request_id"}
    assert body["status"] == expected_status
    assert body["request_id"].startswith("req_")
    assert body["components"] == [
        {"name": "postgresql", "status": expected_component_statuses[0]},
        {"name": "object_storage", "status": expected_component_statuses[1]},
    ]
    _assert_utc_timestamp(body["checked_at"])


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

    from materialsagent.infrastructure.config import load_settings
    from materialsagent.main import create_app

    with TestClient(create_app(settings=load_settings({}))) as client:
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


def test_ready_response_never_exposes_probe_or_configuration_details() -> None:
    from materialsagent.application.readiness import ReadinessService
    from materialsagent.infrastructure.config import load_settings
    from materialsagent.main import create_app

    sensitive_values = [
        "127.0.0.1",
        "5432",
        "9000",
        "9001",
        "actual-database-name",
        "actual-bucket-name",
        "m2/private/object.bin",
        "actual-access-key",
        "actual-secret-key",
        ".env",
        "C:\\Users\\private",
        "Traceback",
    ]

    def failed_probe() -> bool:
        raise RuntimeError(" ".join(sensitive_values))

    app = create_app(
        settings=load_settings({}),
        readiness_service=ReadinessService(
            postgresql_probe=failed_probe,
            object_storage_probe=failed_probe,
        ),
    )
    with TestClient(app) as client:
        response = client.get("/api/v1/health/ready")

    body = response.text
    assert response.status_code == 503
    for sensitive_value in sensitive_values:
        assert sensitive_value not in body


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
