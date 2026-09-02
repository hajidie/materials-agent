from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest

from materialsagent.domain.models.task import (
    READY,
    NEEDS_INPUT,
    TASK_STATUSES,
    Task,
    TaskRoutingStateError,
    TaskToolBindingConflictError,
)
from materialsagent.domain.models.task_input_revision import TaskInputRevision
from materialsagent.domain.ports.tool_registry import ToolRef


BASE_TIME = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
ZTA_REF = ToolRef(
    tool_id="zta35g_sem_virtual_lab",
    version="1",
    schema_hash="a" * 64,
)


def _task(**overrides: object) -> Task:
    values: dict[str, object] = {
        "task_id": "task_domain",
        "conversation_id": "conversation_domain",
        "actor_id": "actor_domain",
        "task_type": "TOOL_EXECUTION",
        "current_status": "PENDING",
        "selected_tool_run_id": None,
        "selected_result_id": None,
        "created_at": BASE_TIME,
        "started_at": None,
        "updated_at": BASE_TIME,
        "completed_at": None,
        "error_code": None,
        "safe_error_message": None,
    }
    values.update(overrides)
    return Task(**values)  # type: ignore[arg-type]


def _revision(**overrides: object) -> TaskInputRevision:
    values: dict[str, object] = {
        "task_input_revision_id": "revision_domain",
        "task_id": "task_domain",
        "request_id": "request_domain",
        "source_llm_call_id": None,
        "source_message_ids": ["message_domain"],
        "revision": 1,
        "raw_input": {"material": "ZTA35G"},
        "normalized_input": {"material": "ZTA35G"},
        "missing_fields": [],
        "ambiguous_fields": [],
        "validation_errors": [],
        "created_at": BASE_TIME,
    }
    values.update(overrides)
    return TaskInputRevision(**values)  # type: ignore[arg-type]


def test_binding_requires_all_three_values_or_none() -> None:
    with pytest.raises(ValueError, match="Tool binding"):
        _task(tool_id=ZTA_REF.tool_id)

    task = _task()
    task.bind_tool(ZTA_REF)

    assert task.bound_tool_ref == ZTA_REF


def test_ready_is_a_declared_task_status_and_requires_a_binding_at_construction() -> None:
    assert READY in TASK_STATUSES

    with pytest.raises(ValueError, match="READY requires a bound Tool"):
        _task(current_status=READY)


def test_binding_is_immutable_including_a_version_only_change() -> None:
    task = _task()
    task.bind_tool(ZTA_REF)

    with pytest.raises(TaskToolBindingConflictError):
        task.bind_tool(replace(ZTA_REF, version="2"))


def test_ready_requires_a_bound_tool_and_complete_latest_revision() -> None:
    task = _task()
    complete = _revision()
    task.bind_tool(ZTA_REF)
    task.current_status = READY
    task.validate_routing_state(complete)

    incomplete = _revision(missing_fields=["aging_time"])
    with pytest.raises(TaskRoutingStateError, match="complete"):
        task.validate_routing_state(incomplete)


def test_needs_input_allows_bound_incomplete_or_unbound_multi_candidate_clarification() -> None:
    bound = _task(current_status=NEEDS_INPUT)
    bound.bind_tool(ZTA_REF)
    bound.validate_routing_state(_revision(missing_fields=["aging_time"]))
    bound.validate_routing_state(
        _revision(
            ambiguous_fields=[
                {"field": "aging_time", "message": "Please confirm the unit."}
            ]
        )
    )

    unbound = _task(current_status=NEEDS_INPUT)
    unbound.validate_routing_state(
        _revision(
            normalized_input=None,
            missing_fields=[],
            candidate_tool_refs=(
                ZTA_REF,
                ToolRef("training", "1", "b" * 64),
            ),
        )
    )

    with pytest.raises(ValueError, match="candidate_tool_refs"):
        _revision(candidate_tool_refs=(ZTA_REF,))


@pytest.mark.parametrize(
    "revision_overrides",
    [
        {},
        {"validation_errors": [{"field": "aging_time", "code": "invalid"}]},
    ],
    ids=("complete", "validation-errors-only"),
)
def test_bound_needs_input_requires_a_missing_or_ambiguous_field(
    revision_overrides: dict[str, object],
) -> None:
    task = _task(current_status=NEEDS_INPUT)
    task.bind_tool(ZTA_REF)

    with pytest.raises(TaskRoutingStateError, match="unresolved input field"):
        task.validate_routing_state(_revision(**revision_overrides))
