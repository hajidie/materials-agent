from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from materialsagent.domain.models import tool_run as tool_run_module
from materialsagent.domain.ports.tool_registry import ExecutionPolicy


BASE = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)
SCHEMA_HASH = "f821240f782ce788bc723fd1acd02a2e58cedbf68b70b1414e2accd16d989d07"


def _pending(**overrides: object) -> tool_run_module.ToolRun:
    values: dict[str, object] = {
        "tool_run_id": "tool_run_domain",
        "task_id": "task_domain",
        "request_id": "request_domain",
        "task_input_revision_id": "revision_domain",
        "input_revision_no": 3,
        "attempt_no": 1,
        "tool_id": "zta35g_sem_virtual_lab",
        "tool_version": "1",
        "schema_hash": SCHEMA_HASH,
        "normalized_input_snapshot": {
            "material": "ZTA35G",
            "solution_temperature": 1000,
        },
        "execution_input": {
            "process_parameters": {"solution_temperature": 1000},
            "requested_outputs": ["sem_image"],
            "runtime_parameters": {"seed": 101},
        },
        "execution_policy_snapshot": ExecutionPolicy.ANY_TASK,
        "requested_outputs": ["sem_image"],
        "created_at": BASE,
    }
    values.update(overrides)
    return tool_run_module.ToolRun.pending(**values)  # type: ignore[arg-type]


def test_pending_factory_records_authorized_execution_provenance() -> None:
    run = _pending()

    assert run.status is tool_run_module.ToolRunStatus.PENDING
    assert run.current_status is tool_run_module.ToolRunStatus.PENDING
    assert run.tool_id == "zta35g_sem_virtual_lab"
    assert run.tool_version == "1"
    assert run.schema_hash == SCHEMA_HASH
    assert run.normalized_input_snapshot == {
        "material": "ZTA35G",
        "solution_temperature": 1000,
    }
    assert run.execution_input["runtime_parameters"] == {"seed": 101}
    assert run.execution_policy_snapshot is ExecutionPolicy.ANY_TASK
    assert run.input_revision_no == 3


def test_pending_factory_detaches_provenance_from_mutable_source_payloads() -> None:
    normalized = {
        "material": "ZTA35G",
        "nested": {"solution_temperature": 1000},
    }
    execution = {
        "process_parameters": {"solution_temperature": 1000},
        "requested_outputs": ["sem_image"],
        "runtime_parameters": {"seed": 101},
    }

    run = _pending(
        normalized_input_snapshot=normalized,
        execution_input=execution,
    )
    normalized["nested"]["solution_temperature"] = 1100  # type: ignore[index]
    execution["runtime_parameters"]["seed"] = 202  # type: ignore[index]
    execution["requested_outputs"].append("mechanical_properties")  # type: ignore[union-attr]

    assert run.normalized_input_snapshot["nested"] == {
        "solution_temperature": 1000
    }
    assert run.execution_input["runtime_parameters"] == {"seed": 101}
    assert run.execution_input["requested_outputs"] == ["sem_image"]


@pytest.mark.parametrize(
    "terminal",
    ["SUCCEEDED", "PARTIALLY_SUCCEEDED", "FAILED"],
)
def test_tool_run_legal_flow_requires_pending_then_running_then_terminal(
    terminal: str,
) -> None:
    pending = _pending(requested_outputs=["sem_image", "mechanical_properties"])
    running = pending.start(started_at=BASE + timedelta(seconds=1))

    if terminal == "SUCCEEDED":
        completed = running.complete_from_result(
            completed_outputs=["sem_image", "mechanical_properties"],
            failed_outputs=[],
            completed_at=BASE + timedelta(seconds=2),
            error_code=None,
            safe_error_message=None,
        )
    elif terminal == "PARTIALLY_SUCCEEDED":
        completed = running.complete_from_result(
            completed_outputs=["sem_image"],
            failed_outputs=["mechanical_properties"],
            completed_at=BASE + timedelta(seconds=2),
            error_code="OUTPUT_PARTIAL",
            safe_error_message="One requested output failed.",
        )
    else:
        completed = running.fail(
            failed_at=BASE + timedelta(seconds=2),
            error_code="RUNTIME_TIMEOUT",
            safe_error_message="Tool Runtime timed out.",
        )

    assert completed.status is tool_run_module.ToolRunStatus(terminal)
    with pytest.raises(ValueError, match="Only PENDING"):
        completed.start(started_at=BASE + timedelta(seconds=3))
    with pytest.raises(ValueError, match="Only RUNNING"):
        completed.fail(
            failed_at=BASE + timedelta(seconds=3),
            error_code="LATE_FAILURE",
            safe_error_message="Late failure.",
        )
    with pytest.raises(ValueError, match="Only RUNNING"):
        completed.complete_from_result(
            completed_outputs=[],
            failed_outputs=["sem_image", "mechanical_properties"],
            completed_at=BASE + timedelta(seconds=3),
            error_code="LATE_FAILURE",
            safe_error_message="Late failure.",
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema_hash", "1.0", "schema_hash"),
        ("input_revision_no", 0, "input_revision_no"),
        (
            "execution_policy_snapshot",
            "UNCONTROLLED",
            "execution_policy_snapshot",
        ),
    ],
)
def test_pending_factory_rejects_invalid_provenance(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        _pending(**{field: value})
