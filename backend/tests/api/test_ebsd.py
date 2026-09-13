from hashlib import sha256
from io import BytesIO
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from backend.tests.agent_fakes import _MemoryStorage
from materialsagent.application.tools import build_tool_registry
from materialsagent.infrastructure.llm.agent_model import MockAgentModel
from materialsagent.infrastructure.tool_clients.local_ebsd import LocalEBSDToolClientAdapter

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "mock-runtime" / "src"))
from materialsagent_mock_runtime.main import create_app, MockRuntimeSettings


def image_bytes(mode="RGB", size=(200, 200), color=100):
    buffer = BytesIO()
    Image.new(mode, size, color=color).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def ebsd(api_harness):
    api_harness.persist_actor("ebsd-test")
    conversation = api_harness.persist_conversation("ebsd-test").conversation_id
    storage = _MemoryStorage()
    runtime_app = create_app(MockRuntimeSettings(token="ebsd-test-token"))
    with TestClient(runtime_app) as runtime:
        class Pool:
            def request(self, method, url, body=None, headers=None, **kwargs):
                response = runtime.request(method, url.removeprefix("http://127.0.0.1:8100"), content=body, headers=headers)
                return SimpleNamespace(status=response.status_code, data=response.content,
                    read=lambda n: response.content[:n], close=lambda: None, release_conn=lambda: None)
        adapter = LocalEBSDToolClientAdapter(base_url="http://127.0.0.1:8100", token="ebsd-test-token", timeout_seconds=60, pool=Pool())
        client = api_harness.create_client("ebsd-test", storage_service=storage,
            tool_registry=build_tool_registry(ebsd_client=adapter), agent_model=MockAgentModel())
        adapter.asset_service = client.app.state.asset_service
        with client:
            yield client, conversation, storage, runtime_app.state.runtime_state


def upload(client, conversation, payload=None, key="image"):
    return client.post(f"/api/v1/conversations/{conversation}/ebsd-images",
        content=image_bytes() if payload is None else payload,
        headers={"Idempotency-Key": key, "Content-Type": "image/png"})


def submit(client, conversation, asset=None, key="run", **extra):
    return client.post(f"/api/v1/conversations/{conversation}/messages",
        json={"mode": "NEW_RUN", "content_text": "预测 EBSD 屈服强度", **({"ebsd_asset_id": asset} if asset else {}), **extra},
        headers={"Idempotency-Key": key})


def test_ebsd_upload_managed_prediction_replay_history_and_cleanup(ebsd):
    client, conversation, storage, runtime = ebsd
    response = upload(client, conversation)
    assert response.status_code == 200, response.text
    asset = response.json()["data"]["asset_id"]
    assert upload(client, conversation).json()["data"]["asset_id"] == asset
    assert upload(client, conversation, image_bytes(color=101)).status_code == 409
    response = submit(client, conversation, asset)
    assert response.status_code == 200, response.text
    run = response.json()["data"]["agent_run"]
    assert run["status"] == "SUCCEEDED", run
    result = run["observations"][0]["result_summary"]
    assert result["data"] == {"yield_strength": {"value": 400.0, "unit": "MPa"}}
    assert result["provenance"]["input_asset"] == {"asset_id": asset, "sha256": sha256(image_bytes()).hexdigest()}
    assert result["provenance"]["model_version"] == "mock-ebsd-bundle"
    assert run["observations"][0]["artifacts"] == []
    assert client.get(f"/api/v1/assets/{asset}/content").content == image_bytes()
    assert submit(client, conversation, asset).json()["data"]["idempotency_replayed"]
    assert runtime.execution_count == 1
    reloaded = client.get(f"/api/v1/agent-runs/{run['agent_run_id']}").json()["data"]
    assert reloaded["ebsd_asset_id"] == asset
    assert reloaded["observations"] == run["observations"]
    regenerated = client.post(f"/api/v1/agent-runs/{run['agent_run_id']}/retry",
        json={"retry_type": "ANSWER_REGENERATION"}, headers={"Idempotency-Key": "answer"})
    assert regenerated.json()["data"]["agent_run"]["status"] == "SUCCEEDED", regenerated.text
    assert runtime.execution_count == 1
    deleted = client.delete(f"/api/v1/conversations/{conversation}")
    assert deleted.status_code == 200, deleted.text
    assert storage.objects == {}
    assert client.get(f"/api/v1/assets/{asset}/content").status_code == 404


@pytest.mark.parametrize("payload", [b"broken", image_bytes("L"), image_bytes(size=(200, 201)), image_bytes(size=(64, 64)), image_bytes("RGBA")])
def test_invalid_upload_never_creates_object(ebsd, payload):
    client, conversation, storage, runtime = ebsd
    assert upload(client, conversation, payload).status_code == 422
    assert not storage.objects and runtime.execution_count == 0


def test_missing_image_resume_and_cross_conversation_rejection(ebsd, api_harness):
    client, conversation, storage, runtime = ebsd
    def responder(role, payload):
        draft = payload.get("draft")
        if role == "agent_decision" and draft and not draft["issues"]:
            return {"type": "CallTool", "tool_name": draft["tool_name"], "arguments": draft["normalized"]}
        return MockAgentModel._respond(role, payload)
    client.app.state.agent_runtime.model = MockAgentModel(responder)
    run = submit(client, conversation).json()["data"]["agent_run"]
    assert run["status"] == "WAITING_FOR_USER", run
    asset = upload(client, conversation).json()["data"]["asset_id"]
    other = api_harness.persist_conversation("ebsd-test").conversation_id
    rejected = submit(client, other, asset)
    assert rejected.status_code != 200
    response = submit(client, conversation, asset, key="resume", mode="RESUME_RUN",
        agent_run_id=run["agent_run_id"], waiting_version=run["waiting_version"])
    assert response.json()["data"]["agent_run"]["status"] == "SUCCEEDED", response.text
    assert runtime.execution_count == 1


def test_storage_failure_replays_original_upload_and_busy_is_explicit_retry(ebsd):
    client, conversation, storage, runtime = ebsd
    storage.unavailable = True
    assert upload(client, conversation).status_code == 503
    storage.unavailable = False
    asset = upload(client, conversation).json()["data"]["asset_id"]
    with runtime.execution_lock:
        response = submit(client, conversation, asset)
    run = response.json()["data"]["agent_run"]
    failed = next(e for e in run["executions"] if e["status"] == "FAILED")
    assert runtime.execution_count == 0
    response = client.post(f"/api/v1/agent-runs/{run['agent_run_id']}/retry",
        json={"retry_type": "TOOL_RETRY", "invocation_run_id": failed["invocation_run_id"]}, headers={"Idempotency-Key": "retry"})
    assert response.json()["data"]["agent_run"]["status"] == "SUCCEEDED", response.text
    assert runtime.execution_count == 1


def test_upload_delete_race_is_busy_until_storage_finishes(ebsd):
    client, conversation, storage, runtime = ebsd
    original_put = storage.put
    deletions = []
    def put(*args, **kwargs):
        deletions.append(client.delete(f"/api/v1/conversations/{conversation}").status_code)
        return original_put(*args, **kwargs)
    storage.put = put
    assert upload(client, conversation).status_code == 200
    assert deletions == [409]
    assert client.delete(f"/api/v1/conversations/{conversation}").status_code == 200
    assert not storage.objects


def test_invalid_upload_key_and_size_are_rejected(ebsd):
    client, conversation, storage, runtime = ebsd
    assert upload(client, conversation, key=" " * 3).status_code == 422
    assert upload(client, conversation, b"x" * (10 * 1024 * 1024 + 1)).status_code == 413
    assert not storage.objects and runtime.execution_count == 0


def test_ebsd_provider_boundary_contains_text_and_references_only(ebsd):
    import base64
    import json
    from backend.tests.unit.test_llm_provider_factory import _role
    from materialsagent.infrastructure.llm.agent_model import AgentModelAdapter
    client, conversation, storage, runtime = ebsd
    captured = []
    def factory(config):
        class Model:
            def bind(self, **kwargs): return self
            def invoke(self, messages):
                captured.extend(messages)
                payload = json.loads(messages[-1]["content"])
                value = MockAgentModel._respond(config.role, payload)
                return SimpleNamespace(content=json.dumps(value) if not isinstance(value, str) else value,
                    usage_metadata={"input_tokens": 100, "output_tokens": 100, "total_tokens": 200})
        return Model()
    client.app.state.agent_runtime.model = AgentModelAdapter({role: _role("deepseek", role)
        for role in ("agent_decision", "tool_arg_resolution", "final_answer")}, factory=factory)
    asset = upload(client, conversation).json()["data"]["asset_id"]
    result = submit(client, conversation, asset).json()["data"]["agent_run"]
    assert result["status"] == "SUCCEEDED", result
    assert captured and all(type(message["content"]) is str for message in captured)
    wire = json.dumps(captured)
    assert asset in wire
    for forbidden in (base64.b64encode(image_bytes()).decode(), "data:image/", "image_url", "object_key", "storage_namespace"):
        assert forbidden not in wire
    assert runtime.execution_count == 1


def test_uploaded_asset_database_constraints_and_digest_guard(ebsd, api_harness):
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError
    client, conversation, storage, runtime = ebsd
    asset = upload(client, conversation).json()["data"]["asset_id"]
    for mutation in ("source_type = 'GENERATED'", "conversation_id = NULL", "role = 'generated_sem'", "asset_type = 'sem_image'"):
        with pytest.raises(IntegrityError), api_harness.engine.begin() as connection:
            connection.execute(text(f"UPDATE asset SET {mutation} WHERE asset_id = :asset"), {"asset": asset})
    object_key, (payload, metadata) = next(iter(storage.objects.items()))
    storage.objects[object_key] = (b"corrupted", metadata)
    run = submit(client, conversation, asset).json()["data"]["agent_run"]
    assert runtime.execution_count == 0
    assert any(item["status"] == "FAILED" for item in run["executions"])
    assert not any((item.get("result_summary") or {}).get("data") for item in run["observations"])
    assert client.get(f"/api/v1/assets/{asset}/content").status_code == 409


@pytest.mark.parametrize("connection_refused", [True, False])
def test_unavailable_ebsd_runtime_requires_explicit_retry(ebsd, connection_refused):
    client, conversation, storage, runtime = ebsd
    asset = upload(client, conversation).json()["data"]["asset_id"]
    adapter = client.app.state.agent_runtime.tools.registry.resolve("ebsd_yield_strength_predictor").binding.execution_target.client
    pool = adapter._pool
    class OfflinePool:
        def request(self, *args, **kwargs):
            if connection_refused:
                raise ConnectionRefusedError("runtime unavailable")
            raise OSError("transport outcome uncertain")
    adapter._pool = OfflinePool()
    try:
        run = submit(client, conversation, asset).json()["data"]["agent_run"]
    finally:
        adapter._pool = pool
    failed = next(e for e in run["executions"] if e["status"] == "FAILED")
    assert failed["retryable"] is connection_refused and runtime.execution_count == 0
    assert not any((o.get("result_summary") or {}).get("data") for o in run["observations"])
    if not connection_refused:
        return
    response = client.post(f"/api/v1/agent-runs/{run['agent_run_id']}/retry",
        json={"retry_type": "TOOL_RETRY", "invocation_run_id": failed["invocation_run_id"]},
        headers={"Idempotency-Key": "runtime-reconnected"})
    assert response.json()["data"]["agent_run"]["status"] == "SUCCEEDED", response.text
    assert runtime.execution_count == 1
