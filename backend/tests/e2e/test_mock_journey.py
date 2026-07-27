from __future__ import annotations

import json
import re

import pytest
from sqlalchemy import func, select

from materialsagent.infrastructure.db.asset import AssetRow
from materialsagent.infrastructure.db.conversation_task import (
    MessageRow,
    TaskInputRevisionRow,
)
from materialsagent.infrastructure.db.session import create_session_factory
from materialsagent.infrastructure.db.tool_result import (
    ResultAssetLinkRow,
    ToolResultRow,
)
from materialsagent.infrastructure.db.tool_run import ToolRunRow


FORBIDDEN_PUBLIC_TEXT = (
    "object_key",
    "model_bundle",
    "runtime_url",
    "weight_path",
    "traceback",
    "token",
    "bucket",
    ":\\",
)
FORBIDDEN_HEADER_TEXT = (
    "object_key",
    "bucket",
    "token",
    "runtime_url",
    "weight_path",
    "traceback",
)
CONTROLLED_PNG_DISPOSITION = re.compile(
    r'inline; filename="([A-Za-z0-9_-]{1,96}\.png)"\Z'
)


class _PreexistingBucketClient:
    def __init__(self, bucket: str) -> None:
        self.bucket = bucket
        self.objects = ["sentinel-object"]

    def bucket_exists(self, bucket: str) -> bool:
        assert bucket == self.bucket
        return True

    def make_bucket(self, bucket: str) -> None:
        raise AssertionError(f"must not reuse pre-existing bucket: {bucket}")

    def list_objects(self, bucket: str, *, recursive: bool):
        raise AssertionError(f"must not list pre-existing bucket: {bucket}")

    def remove_object(self, bucket: str, object_name: str) -> None:
        raise AssertionError(
            f"must not remove pre-existing object: {bucket}/{object_name}"
        )

    def remove_bucket(self, bucket: str) -> None:
        raise AssertionError(f"must not remove pre-existing bucket: {bucket}")


def test_preexisting_temporary_bucket_is_rejected_without_deletion(
    temporary_bucket_creator,
) -> None:
    bucket = "materialsagent-e2e-0123456789abcdef"
    client = _PreexistingBucketClient(bucket)

    with pytest.raises(pytest.fail.Exception, match="already exists"):
        temporary_bucket_creator(client, bucket)

    assert client.objects == ["sentinel-object"]


def _assert_safe_json(response, *, repo_root: str) -> None:
    assert response.headers["content-type"].startswith("application/json")
    public_text = response.text.lower()
    for forbidden in FORBIDDEN_PUBLIC_TEXT:
        assert forbidden not in public_text
    assert repo_root.lower() not in public_text


def _assert_safe_png_response(
    response,
    *,
    repo_root: str,
    object_key: str,
) -> None:
    assert response.headers["content-type"] == "image/png"
    disposition = response.headers["content-disposition"]
    match = CONTROLLED_PNG_DISPOSITION.fullmatch(disposition)
    assert match is not None
    filename = match.group(1)
    assert "/" not in filename
    assert "\\" not in filename
    assert re.search(r"[A-Za-z]:", filename) is None
    assert repo_root.lower() not in filename.lower()

    public_headers = json.dumps(
        dict(response.headers),
        ensure_ascii=False,
    ).lower()
    for forbidden in FORBIDDEN_HEADER_TEXT:
        assert forbidden not in public_headers
    assert repo_root.lower() not in public_headers
    assert object_key.lower() not in public_headers


def _create_conversation(e2e_harness) -> str:
    response = e2e_harness.client.post("/api/v1/conversations", json={})
    assert response.status_code == 201, response.text
    _assert_safe_json(response, repo_root=e2e_harness.repo_root)
    return response.json()["data"]["conversation_id"]


def _submit(
    e2e_harness,
    conversation_id: str,
    *,
    key: str,
    content: str,
    mode: str = "NEW_TASK",
    target_task_id: str | None = None,
):
    body: dict[str, object] = {
        "submission_mode": mode,
        "content_text": content,
    }
    if target_task_id is not None:
        body["target_task_id"] = target_task_id
    return e2e_harness.client.post(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Idempotency-Key": key},
        json=body,
    )


def _timeline(e2e_harness, conversation_id: str):
    response = e2e_harness.client.get(
        f"/api/v1/conversations/{conversation_id}/timeline"
    )
    assert response.status_code == 200, response.text
    _assert_safe_json(response, repo_root=e2e_harness.repo_root)
    return response


def test_knowledge_journey_commits_messages_without_tool_resources(
    e2e_harness,
) -> None:
    conversation_id = _create_conversation(e2e_harness)
    submitted = _submit(
        e2e_harness,
        conversation_id,
        key="m11a-knowledge-journey",
        content="什么是 ZTA35G？",
    )

    assert submitted.status_code == 200, submitted.text
    _assert_safe_json(submitted, repo_root=e2e_harness.repo_root)
    data = submitted.json()["data"]
    task_id = data["task"]["task_id"]
    assert data["task"]["task_id"] == task_id
    assert data["task"]["task_type"] == "KNOWLEDGE_QA"
    assert data["task"]["status"] == "SUCCEEDED"
    assert data["task"]["selected_tool_run_id"] is None
    assert data["task"]["selected_result_id"] is None
    assert data["task"]["created_at"]
    assert data["task"]["updated_at"]
    assert data["assistant_message"]["role"] == "ASSISTANT"
    assert data["assistant_message"]["content_text"]
    assert data["needs_input"] is None
    assert data["result_summary"] is None
    assert data["explanation"] is None

    timeline = _timeline(e2e_harness, conversation_id)
    items = timeline.json()["data"]["items"]
    assert [item["item_type"] for item in items] == [
        "USER_MESSAGE",
        "ASSISTANT_MESSAGE",
    ]
    assert items[0]["message"]["message_id"] == data["user_message"]["message_id"]
    assert items[1]["message"]["message_id"] == (
        data["assistant_message"]["message_id"]
    )

    with e2e_harness.engine.connect() as connection:
        assert connection.scalar(
            select(func.count())
            .select_from(ToolRunRow)
            .where(ToolRunRow.task_id == task_id)
        ) == 0
        assert connection.scalar(
            select(func.count())
            .select_from(ToolResultRow)
            .where(ToolResultRow.task_id == task_id)
        ) == 0
        assert connection.scalar(
            select(func.count())
            .select_from(AssetRow)
            .where(AssetRow.task_id == task_id)
        ) == 0


def test_complete_tool_journey_uses_mock_runtime_and_persists_one_chain(
    e2e_harness,
) -> None:
    conversation_id = _create_conversation(e2e_harness)
    submitted = _submit(
        e2e_harness,
        conversation_id,
        key="m11a-complete-tool-journey",
        content="完整合法 Tool 请求",
    )

    assert submitted.status_code == 200, submitted.text
    _assert_safe_json(submitted, repo_root=e2e_harness.repo_root)
    data = submitted.json()["data"]
    task = data["task"]
    result = data["result_summary"]
    assert task["task_type"] == "TOOL_EXECUTION"
    assert task["status"] == "SUCCEEDED"
    assert task["selected_tool_run_id"]
    assert task["selected_result_id"] == result["result_id"]
    assert result["status"] == "SUCCEEDED"
    assert result["requested_outputs"] == [
        "sem_image",
        "mechanical_properties",
    ]
    assert result["completed_outputs"] == [
        "sem_image",
        "mechanical_properties",
    ]
    assert result["failed_outputs"] == []
    assert len(result["artifacts"]) == 1
    artifact = result["artifacts"][0]
    assert artifact["status"] == "AVAILABLE"
    assert artifact["media_type"] == "image/png"
    assert data["explanation"]["status"] == "SUCCEEDED"
    assert data["explanation"]["text"]

    task_response = e2e_harness.client.get(f"/api/v1/tasks/{task['task_id']}")
    result_response = e2e_harness.client.get(
        f"/api/v1/tool-results/{result['result_id']}"
    )
    asset_response = e2e_harness.client.get(
        f"/api/v1/assets/{artifact['asset_id']}/content"
    )
    assert task_response.status_code == result_response.status_code == 200
    assert asset_response.status_code == 200
    _assert_safe_json(task_response, repo_root=e2e_harness.repo_root)
    _assert_safe_json(result_response, repo_root=e2e_harness.repo_root)
    assert task_response.json()["data"]["tool_run_count"] == 1
    assert asset_response.content.startswith(b"\x89PNG\r\n\x1a\n")

    timeline = _timeline(e2e_harness, conversation_id)
    items = timeline.json()["data"]["items"]
    assert len(items) == 1
    card = items[0]
    assert card["item_type"] == "TOOL_TASK"
    assert card["item_id"] == task["task_id"]
    assert card["anchor_at"] == data["user_message"]["created_at"]
    assert card["initial_user_message"]["message_id"] == (
        data["user_message"]["message_id"]
    )
    assert card["tool_runs"]["attempt_count"] == 1
    assert card["tool_runs"]["selected_tool_run"]["tool_run_id"] == (
        task["selected_tool_run_id"]
    )
    assert card["result"]["result_id"] == result["result_id"]
    assert len(card["assets"]) == 1
    assert card["assets"][0]["asset_id"] == artifact["asset_id"]
    assert card["explanation"]["status"] == "SUCCEEDED"

    session_factory = create_session_factory(e2e_harness.engine)
    with session_factory() as session:
        run = session.scalar(
            select(ToolRunRow).where(ToolRunRow.task_id == task["task_id"])
        )
        stored_result = session.get(ToolResultRow, result["result_id"])
        asset = session.get(AssetRow, artifact["asset_id"])
        links = list(
            session.scalars(
                select(ResultAssetLinkRow).where(
                    ResultAssetLinkRow.result_id == result["result_id"]
                )
            ).all()
        )
        assert run is not None
        assert stored_result is not None
        assert asset is not None
        assert run.model_bundle_id == "mock-zta35g-bundle"
        assert run.tool_run_id == task["selected_tool_run_id"]
        assert stored_result.tool_run_id == run.tool_run_id
        assert asset.producer_tool_run_id == run.tool_run_id
        object_key = asset.object_key
        assert [(link.result_id, link.asset_id) for link in links] == [
            (result["result_id"], artifact["asset_id"])
        ]
    _assert_safe_png_response(
        asset_response,
        repo_root=e2e_harness.repo_root,
        object_key=object_key,
    )


def test_needs_input_supplement_keeps_task_history_and_anchor(
    e2e_harness,
) -> None:
    conversation_id = _create_conversation(e2e_harness)
    initial = _submit(
        e2e_harness,
        conversation_id,
        key="m11a-needs-input-initial",
        content="缺 aging_temperature",
    )

    assert initial.status_code == 200, initial.text
    _assert_safe_json(initial, repo_root=e2e_harness.repo_root)
    initial_data = initial.json()["data"]
    task_id = initial_data["task"]["task_id"]
    initial_user = initial_data["user_message"]
    assert initial_data["task"]["status"] == "NEEDS_INPUT"
    assert initial_data["needs_input"]["missing_fields"] == [
        "aging_temperature"
    ]
    assert initial_data["task"]["selected_tool_run_id"] is None
    assert initial_data["task"]["selected_result_id"] is None
    assert initial_data["result_summary"] is None

    before_timeline = _timeline(e2e_harness, conversation_id)
    before_card = before_timeline.json()["data"]["items"][0]
    assert before_card["item_type"] == "TOOL_TASK"
    assert before_card["anchor_at"] == initial_user["created_at"]
    assert before_card["tool_runs"]["attempt_count"] == 0
    assert before_card["result"] is None
    assert before_card["assets"] == []

    supplemented = _submit(
        e2e_harness,
        conversation_id,
        key="m11a-needs-input-supplement",
        content=(
            "完整合法 Tool 请求；补充 aging_temperature = 730 °C"
        ),
        mode="SUPPLEMENT_TASK",
        target_task_id=task_id,
    )

    assert supplemented.status_code == 200, supplemented.text
    _assert_safe_json(supplemented, repo_root=e2e_harness.repo_root)
    supplemented_data = supplemented.json()["data"]
    assert supplemented_data["task"]["task_id"] == task_id
    assert supplemented_data["task"]["status"] == "SUCCEEDED"
    assert supplemented_data["task"]["selected_tool_run_id"]
    assert supplemented_data["task"]["selected_result_id"]
    assert supplemented_data["result_summary"]["status"] == "SUCCEEDED"

    task_response = e2e_harness.client.get(f"/api/v1/tasks/{task_id}")
    assert task_response.status_code == 200
    _assert_safe_json(task_response, repo_root=e2e_harness.repo_root)
    assert task_response.json()["data"]["tool_run_count"] == 1

    after_timeline = _timeline(e2e_harness, conversation_id)
    items = after_timeline.json()["data"]["items"]
    assert len(items) == 1
    after_card = items[0]
    assert after_card["item_type"] == "TOOL_TASK"
    assert after_card["item_id"] == task_id
    assert after_card["anchor_at"] == before_card["anchor_at"]
    assert after_card["initial_user_message"]["message_id"] == (
        initial_user["message_id"]
    )
    assert after_card["tool_runs"]["attempt_count"] == 1
    thread_ids = {
        message["message_id"] for message in after_card["input_thread"]
    }
    assert initial_data["assistant_message"]["message_id"] in thread_ids
    assert supplemented_data["user_message"]["message_id"] in thread_ids

    session_factory = create_session_factory(e2e_harness.engine)
    with session_factory() as session:
        messages = list(
            session.scalars(
                select(MessageRow)
                .where(MessageRow.task_id == task_id)
                .order_by(MessageRow.created_at, MessageRow.message_id)
            ).all()
        )
        revisions = list(
            session.scalars(
                select(TaskInputRevisionRow)
                .where(TaskInputRevisionRow.task_id == task_id)
                .order_by(TaskInputRevisionRow.revision)
            ).all()
        )
        runs = list(
            session.scalars(
                select(ToolRunRow).where(ToolRunRow.task_id == task_id)
            ).all()
        )
        results = list(
            session.scalars(
                select(ToolResultRow).where(ToolResultRow.task_id == task_id)
            ).all()
        )
        assets = list(
            session.scalars(
                select(AssetRow).where(AssetRow.task_id == task_id)
            ).all()
        )
        assert [message.role for message in messages] == [
            "USER",
            "ASSISTANT",
            "USER",
        ]
        assert len(revisions) == 2
        assert revisions[0].task_input_revision_id != (
            revisions[1].task_input_revision_id
        )
        assert len(runs) == len(results) == len(assets) == 1
        assert runs[0].model_bundle_id == "mock-zta35g-bundle"
