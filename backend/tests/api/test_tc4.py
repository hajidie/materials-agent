from io import BytesIO
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import zlib
import pytest
from PIL import Image
from fastapi.testclient import TestClient
from sqlalchemy import text
from backend.tests.agent_fakes import _MemoryStorage
from backend.tests.agent_state import post_message, post_operation, wait_run
from backend.tests.agent_inspection import stored_run
from materialsagent.application.tools import build_tool_registry
from materialsagent.infrastructure.llm.agent_model import MockAgentModel
from materialsagent.infrastructure.tool_clients.local_tc4 import LocalTC4Client
from materialsagent.domain.models.tc4 import TOOL_ID, MOCK_MODEL_VERSION

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "mock-runtime" / "src"))
from materialsagent_mock_runtime.main import create_app, MockRuntimeSettings


def image_bytes(index=0):
    stream = BytesIO()
    Image.new("L" if index % 2 else "RGB", (128 + index, 160), color=128 + index if index % 2 else (128 + index,) * 3).save(stream, format="PNG")
    return stream.getvalue()


def rgb16_bytes():
    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff)
    header = struct.pack("!IIBBBBB", 128, 160, 16, 2, 0, 0, 0)
    row = b"\x00" + struct.pack("!HHH", 12345, 23456, 34567) * 128
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(row * 160)) + chunk(b"IEND", b""))


@pytest.fixture
def tc4(api_harness):
    api_harness.persist_actor("tc4-test")
    conversation = api_harness.persist_conversation("tc4-test").conversation_id
    storage = _MemoryStorage()
    app = create_app(MockRuntimeSettings(token="test"))
    with TestClient(app) as runtime:
        class Pool:
            requests = []
            hook = None
            def request(self, method, url, body=None, headers=None, **kwargs):
                path = url.removeprefix("http://127.0.0.1:8100")
                self.requests.append((method, path))
                response = runtime.request(method, path, content=body, headers=headers)
                if self.hook:
                    self.hook(method, path, response)
                return SimpleNamespace(status=response.status_code, data=response.content,
                    read=lambda n: response.content[:n], close=lambda: None, release_conn=lambda: None)
        adapter = LocalTC4Client(base_url="http://127.0.0.1:8100", token="test", timeout_seconds=60,
            pool=Pool(), model_version=MOCK_MODEL_VERSION, uow_factory=api_harness.unit_of_work_factory)
        client = api_harness.create_client("tc4-test", storage_service=storage,
            tool_registry=build_tool_registry(tc4_client=adapter, enable_tc4_segmentation=True), agent_model=MockAgentModel())
        adapter.asset_service, adapter.agent_store = client.app.state.asset_service, client.app.state.agent_runtime.store
        client.app.state.conversation_cleanup_service.tc4_client = adapter
        with client:
            yield SimpleNamespace(client=client, conversation=conversation, adapter=adapter, storage=storage,
                runtime=app.state.runtime_state, harness=api_harness)


def upload_all(t, count=3):
    attachments = []
    for index in range(count):
        response = t.client.post(f"/api/v1/conversations/{t.conversation}/attachments",
            files={"file": (f"sample-{index}.png", image_bytes(index), "image/png")}, headers={"Idempotency-Key": f"image-{index}"})
        assert response.status_code == 200, response.text
        attachments.append(response.json()["data"]["attachment"])
    return attachments


def submit(t, attachments):
    response = post_message(t.client, f"/api/v1/conversations/{t.conversation}/messages", json={
        "mode": "NEW_RUN", "content_text": "批量分割 TC4 初生 α 相并统计面积占比", "attachments": attachments},
        headers={"Idempotency-Key": "batch"})
    assert response.status_code == 200, response.text
    return stored_run(t.client, response.json()["data"]["agent_run"])


@pytest.mark.parametrize("count", [1, 5, 10])
def test_ordered_batch_one_invocation_and_lossless_downloads(tc4, count):
    t = tc4
    attachments = upload_all(t, count)
    run = submit(t, attachments)
    assert run["status"] == "SUCCEEDED", run
    assert t.runtime.execution_count == count
    assert len(run["executions"]) == 1
    assert len(run["observations"]) == 1
    result = run["observations"][0]["result_summary"]
    assert result["provenance"]["model_version"] == MOCK_MODEL_VERSION
    assert len(result["data"]["items"]) == count
    public = t.client.get(f"/api/v1/agent-runs/{run['agent_run_id']}").json()["data"]
    assert [i["name"] for i in public["segmentation_items"]] == [a["name"] for a in attachments]
    for index, item in enumerate(result["data"]["items"]):
        assert item["foreground_pixels"] == item["total_pixels"] == (128 + index) * 160
        assert item["area_fraction"] == 1.0
        for role, identity in item["artifacts"].items():
            content = t.client.get(f"/api/v1/assets/{identity}/content")
            assert content.status_code == 200, content.text
            with Image.open(BytesIO(content.content)) as image:
                assert image.size == (128 + index, 160)
                assert image.mode == ("L" if role == "mask" else "RGB")
                if role == "mask":
                    assert image.histogram()[255] == image.width * image.height
    with t.harness.engine.connect() as connection:
        for table in ("invocation_run", "tool_run", "task_input_revision"):
            assert connection.scalar(text(f"SELECT count(*) FROM {table}")) == 1
        assert connection.scalar(text("SELECT count(*) FROM tool_run_item")) == count


def test_opaque_rgba_upload_preserves_original_and_segments(tc4):
    stream = BytesIO()
    Image.new("RGBA", (128, 160), (128, 128, 128, 255)).save(stream, "PNG")
    payload = stream.getvalue()
    response = tc4.client.post(f"/api/v1/conversations/{tc4.conversation}/attachments",
        files={"file": ("opaque-rgba.png", payload, "image/png")},
        headers={"Idempotency-Key": "opaque-rgba"})
    assert response.status_code == 200, response.text
    attachment = response.json()["data"]["attachment"]
    original = tc4.client.get(f"/api/v1/assets/{attachment['attachment_id']}/content")
    assert original.status_code == 200
    assert original.content == payload
    run = submit(tc4, [attachment])
    assert run["status"] == "SUCCEEDED", run
    assert tc4.runtime.execution_count == 1
    item = run["observations"][0]["result_summary"]["data"]["items"][0]
    assert item["foreground_pixels"] == item["total_pixels"] == 128 * 160
    assert item["area_fraction"] == 1.0


@pytest.mark.parametrize("alpha", [0, 254])
def test_upload_rejects_even_one_transparent_pixel_with_specific_reason(tc4, alpha):
    image = Image.new("RGBA", (128, 160), (128, 128, 128, 255))
    image.putpixel((127, 159), (128, 128, 128, alpha))
    stream = BytesIO()
    image.save(stream, "PNG")
    response = tc4.client.post(f"/api/v1/conversations/{tc4.conversation}/attachments",
        files={"file": ("transparent.png", stream.getvalue(), "image/png")},
        headers={"Idempotency-Key": "transparent"})
    assert response.status_code == 422, response.text
    assert "透明或半透明" in response.json()["error"]["message"]
    assert tc4.runtime.execution_count == 0


@pytest.mark.parametrize("mode,background,transparent", [("RGB", (17, 83, 201), (0, 0, 0)), ("L", 83, 0)])
@pytest.mark.parametrize("has_transparent_pixel", [False, True])
def test_upload_checks_actual_trns_pixels_and_preserves_opaque_original(tc4, mode, background, transparent, has_transparent_pixel):
    image = Image.new(mode, (128, 160), background)
    if has_transparent_pixel:
        image.putpixel((127, 159), transparent)
    stream = BytesIO()
    image.save(stream, "PNG", transparency=transparent)
    payload = stream.getvalue()
    response = tc4.client.post(f"/api/v1/conversations/{tc4.conversation}/attachments",
        files={"file": ("trns.png", payload, "image/png")}, headers={"Idempotency-Key": "trns"})
    if has_transparent_pixel:
        assert response.status_code == 422, response.text
        assert "透明或半透明" in response.json()["error"]["message"]
        assert not tc4.storage.objects
        assert tc4.runtime.execution_count == 0
    else:
        assert response.status_code == 200, response.text
        attachment = response.json()["data"]["attachment"]
        assert tc4.client.get(f"/api/v1/assets/{attachment['attachment_id']}/content").content == payload
        assert submit(tc4, [attachment])["status"] == "SUCCEEDED"


def test_upload_rejects_encoded_16_bit_rgb_before_creating_an_asset(tc4):
    payload = rgb16_bytes()
    with Image.open(BytesIO(payload)) as image:
        assert image.mode == "RGB"  # Pillow's decoded mode does not preserve encoded bit depth.
    response = tc4.client.post(f"/api/v1/conversations/{tc4.conversation}/attachments",
        files={"file": ("rgb16.png", payload, "image/png")}, headers={"Idempotency-Key": "rgb16"})
    assert response.status_code == 422, response.text
    assert "8 位" in response.json()["error"]["message"]
    assert not tc4.storage.objects
    assert tc4.runtime.execution_count == 0


def test_tc4_assets_use_real_minio_read_boundary(tc4):
    from materialsagent.infrastructure.storage.minio import create_minio_storage
    t = tc4
    storage = create_minio_storage(t.harness.settings)
    replacement = t.harness.create_client("tc4-test", storage_service=storage,
        tool_registry=build_tool_registry(tc4_client=t.adapter, enable_tc4_segmentation=True), agent_model=MockAgentModel())
    old_client = t.client
    with replacement:
        t.client = replacement
        t.adapter.asset_service, t.adapter.agent_store = replacement.app.state.asset_service, replacement.app.state.agent_runtime.store
        replacement.app.state.conversation_cleanup_service.tc4_client = t.adapter
        run = submit(t, upload_all(t, 1))
        assert run["status"] == "SUCCEEDED", run
        for item in run["observations"][0]["data"]["items"]:
            for identity in item["artifacts"].values():
                assert replacement.get(f"/api/v1/assets/{identity}/content").status_code == 200
        assert replacement.delete(f"/api/v1/conversations/{t.conversation}").status_code == 200
    storage.close()
    t.client = old_client


def test_answer_regeneration_keeps_grouped_statistics_without_inference(tc4):
    t = tc4
    original = submit(t, upload_all(t))
    response = post_operation(t.client, f"/api/v1/messages/{original['final_message_id']}/regenerate",
        headers={"Idempotency-Key":"regenerate"})
    assert response.status_code == 200, response.text
    run = response.json()["data"]["agent_run"]
    view = t.client.get(f"/api/v1/agent-runs/{run['agent_run_id']}").json()["data"]
    assert view["status"] == "SUCCEEDED", view
    assert len(view["segmentation_items"]) == 3
    assert all(item["area_fraction"] == 1.0 for item in view["segmentation_items"])
    assert t.runtime.execution_count == 3


def test_response_lost_reconciles_original_receipt(tc4):
    t = tc4
    attachments = upload_all(t)
    def lose(method, path, response):
        if method == "POST" and path.endswith("/execute"):
            raise OSError("lost after commit")
    t.adapter._pool.hook = lose
    run = submit(t, attachments)
    assert run["status"] == "SUCCEEDED", run
    assert t.runtime.execution_count == 3
    assert len([r for r in t.adapter._pool.requests if r[0] == "POST"]) == 3
    assert len([r for r in t.adapter._pool.requests if r[0] == "GET" and "/receipts/" in r[1] and r[1].endswith("mask") is False and r[1].endswith("overlay") is False]) == 3


def test_runtime_failure_pauses_then_continues_remaining_images(tc4):
    t = tc4
    def fail_first(method, path, response):
        if method == "POST" and path.endswith("/execute"):
            receipt = next(iter(t.runtime.tc4_receipts.values()))
            receipt.update(status="FAILED", data=None, artifacts={})
            raise OSError("failed inference receipt response lost")
    t.adapter._pool.hook = fail_first
    original = submit(t, upload_all(t))
    assert original["status"] == "INTERRUPTED"
    assert t.runtime.execution_count == 1
    view = t.client.get(f"/api/v1/agent-runs/{original['agent_run_id']}").json()["data"]
    assert not view["outcome_unknown"]
    t.adapter._pool.hook = None
    result = resume(t, original)
    assert result["status"] == "SUCCEEDED", result
    assert t.runtime.execution_count == 3
    assert [i["status"] for i in result["observations"][0]["data"]["items"]] == ["FAILED", "SUCCEEDED", "SUCCEEDED"]
    assert result["observations"][0]["status"] == "PARTIALLY_SUCCEEDED"


@pytest.mark.parametrize("all_failed", [False, True])
def test_input_failures_continue_and_count_by_image(tc4, all_failed):
    t = tc4
    attachments = upload_all(t)
    for key, (payload, metadata) in list(t.storage.objects.items()):
        if all_failed or metadata.metadata["asset-id"] == attachments[1]["attachment_id"]:
            t.storage.objects[key] = (b"corrupt", metadata)
    run = submit(t, attachments)
    assert run["status"] == "SUCCEEDED", {"error": run.get("error_code"), "observations": run["observations"], "pending": run.get("pending_execution"), "executions": run["executions"]}
    result = run["observations"][0]["result_summary"]
    assert result["status"] == ("FAILED" if all_failed else "PARTIALLY_SUCCEEDED")
    assert t.runtime.execution_count == (0 if all_failed else 2)
    assert [i["status"] for i in result["data"]["items"]] == (["FAILED"] * 3 if all_failed else ["SUCCEEDED", "FAILED", "SUCCEEDED"])


def resume(t, run):
    response = t.client.post(f"/api/v1/agent-runs/{run['agent_run_id']}/resume",
        json={"submission_id": run["submission_id"], "version": run["version"]}, headers={"Idempotency-Key": "resume"})
    assert response.status_code == 202, response.text
    response = wait_run(t.client, run["agent_run_id"])
    return stored_run(t.client, response.json()["data"])


def test_asset_save_failure_resume_uses_receipt_not_inference(tc4):
    t = tc4
    attachments = upload_all(t)
    def storage_outage(method, path, response):
        if method == "POST":
            t.storage.unavailable = True
    t.adapter._pool.hook = storage_outage
    run = submit(t, attachments)
    assert run["status"] == "INTERRUPTED", run
    assert t.runtime.execution_count == 1
    assert t.client.delete(f"/api/v1/conversations/{t.conversation}").status_code == 409
    t.storage.unavailable = False
    t.adapter._pool.hook = None
    result = resume(t, run)
    assert result["status"] == "SUCCEEDED", result
    assert t.runtime.execution_count == 3
    assert len(result["executions"]) == 1
    assert len([r for r in t.adapter._pool.requests if r[0] == "POST"]) == 3


def test_unknown_receipt_never_resends_and_blocks_deletion(tc4):
    t = tc4
    attachments = upload_all(t)
    def lose_runtime(method, path, response):
        if method == "POST":
            t.runtime.tc4_receipts.clear()
            t.runtime.tc4_artifacts.clear()
            raise OSError("runtime restarted before receipt")
    t.adapter._pool.hook = lose_runtime
    run = submit(t, attachments)
    assert run["status"] == "INTERRUPTED", run
    t.adapter._pool.hook = None
    again = resume(t, run)
    assert again["status"] == "INTERRUPTED"
    assert t.client.get(f"/api/v1/agent-runs/{again['agent_run_id']}").json()["data"]["outcome_unknown"]
    assert t.runtime.execution_count == 1
    assert len([r for r in t.adapter._pool.requests if r[0] == "POST"]) == 1
    assert t.client.delete(f"/api/v1/conversations/{t.conversation}").status_code == 409


def test_owned_conversation_deletion_cleans_runtime_with_outbox(tc4):
    t = tc4
    run = submit(t, upload_all(t))
    assert run["status"] == "SUCCEEDED"
    assert t.runtime.tc4_artifacts
    t.adapter._pool.hook = lambda *args: (_ for _ in ()).throw(OSError("cleanup unavailable"))
    response = t.client.delete(f"/api/v1/conversations/{t.conversation}")
    assert response.status_code == 200, response.text
    with t.harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runtime_receipt_cleanup WHERE status='PENDING'")) > 0
    t.adapter._pool.hook = None
    t.client.app.state.conversation_cleanup_service.drain()
    assert not t.runtime.tc4_artifacts and not t.storage.objects
    with t.harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM runtime_receipt_cleanup WHERE status='PENDING'")) == 0


def test_stop_during_dispatch_saves_late_result_and_skips_rest(tc4):
    t = tc4
    def stop_after_compute(method, path, response):
        if method == "POST" and path.endswith("/execute"):
            active = t.adapter.agent_store.list(t.conversation, "tc4-test")[0]
            t.adapter.agent_store.stop(active.agent_run_id, active.actor_id, active.submission_id)
    t.adapter._pool.hook = stop_after_compute
    run = submit(t, upload_all(t))
    assert run["status"] == "TERMINATED", run
    assert t.runtime.execution_count == 1
    public = t.client.get(f"/api/v1/agent-runs/{run['agent_run_id']}").json()["data"]
    assert [i["status"] for i in public["segmentation_items"]] == ["SUCCEEDED", "SKIPPED", "SKIPPED"]
    assert len(public["segmentation_items"][0]["artifacts"]) == 2


def test_stopped_batch_retries_only_original_artifact_read(tc4):
    t = tc4
    lost = False
    def stop_and_disconnect(method, path, response):
        nonlocal lost
        if method == "POST" and path.endswith("/execute"):
            active = t.adapter.agent_store.list(t.conversation, "tc4-test")[0]
            t.adapter.agent_store.stop(active.agent_run_id, active.actor_id, active.submission_id)
        if method == "GET" and path.endswith("/overlay") and not lost:
            lost = True
            raise OSError("idle connection closed")
    t.adapter._pool.hook = stop_and_disconnect
    run = submit(t, upload_all(t))
    assert run["status"] == "TERMINATED"
    assert t.runtime.execution_count == 1
    public = t.client.get(f"/api/v1/agent-runs/{run['agent_run_id']}").json()["data"]
    assert [i["status"] for i in public["segmentation_items"]] == ["SUCCEEDED", "SKIPPED", "SKIPPED"]
    reads = [path for method, path in t.adapter._pool.requests if method == "GET" and path.endswith("/overlay")]
    assert len(reads) == 2 and reads[0] == reads[1]


def test_stopped_batch_explicit_reconcile_saves_receipt_without_new_inference(tc4):
    t = tc4
    def stop_then_outage(method, path, response):
        if method == "POST" and path.endswith("/execute"):
            active = t.adapter.agent_store.list(t.conversation, "tc4-test")[0]
            t.adapter.agent_store.stop(active.agent_run_id, active.actor_id, active.submission_id)
        if method == "GET" and path.endswith("/overlay"):
            raise OSError("artifact read unavailable")
    t.adapter._pool.hook = stop_then_outage
    run = submit(t, upload_all(t))
    assert run["status"] == "TERMINATED"
    invocation = run["pending_execution"]["invocation_run_id"]
    t.adapter._pool.hook = None
    response = t.client.post(f"/api/v1/agent-runs/{run['agent_run_id']}/invocations/{invocation}/reconcile")
    assert response.status_code == 200, response.text
    public = t.client.get(f"/api/v1/agent-runs/{run['agent_run_id']}").json()["data"]
    assert public["status"] == "TERMINATED"
    assert [i["status"] for i in public["segmentation_items"]] == ["SUCCEEDED", "SKIPPED", "SKIPPED"]
    assert t.runtime.execution_count == 1


def test_backend_restart_resumes_original_batch(tc4):
    t = tc4
    def busy(method, path, response):
        if method == "POST":
            t.storage.unavailable = True
    t.adapter._pool.hook = busy
    original = submit(t, upload_all(t))
    assert original["status"] == "INTERRUPTED"
    t.storage.unavailable, t.adapter._pool.hook = False, None
    replacement = t.harness.create_client("tc4-test", storage_service=t.storage,
        tool_registry=build_tool_registry(tc4_client=t.adapter, enable_tc4_segmentation=True), agent_model=MockAgentModel())
    old_client = t.client
    with replacement:
        t.client = replacement
        t.adapter.asset_service, t.adapter.agent_store = replacement.app.state.asset_service, replacement.app.state.agent_runtime.store
        from datetime import datetime, timedelta, timezone
        from materialsagent.application.context import ActorContext
        cleanup = replacement.app.state.conversation_cleanup_service
        cleanup._process_cutoff = datetime.now(timezone.utc) + timedelta(days=1)
        assert cleanup.recover_stale(ActorContext("tc4-test", None)) == {"tool_runs":0,"assets":0,"tasks":0}
        result = resume(t, original)
        assert result["status"] == "SUCCEEDED", result
        assert t.runtime.execution_count == 3
        assert len(result["executions"]) == 1
    t.client = old_client


@pytest.mark.parametrize("phase", ["_create_initial_pending_attempt", "_start_pending_attempt"])
def test_restart_before_parent_starts_keeps_one_attempt(tc4, monkeypatch, phase):
    from materialsagent.application.errors import DependencyUnavailableError
    t = tc4
    execution = t.client.app.state.agent_runtime.tools.workflow.execution
    original = getattr(execution, phase)
    def crash(*args, **kwargs):
        raise DependencyUnavailableError()
    monkeypatch.setattr(execution, phase, crash)
    run = submit(t, upload_all(t))
    assert run["status"] == "INTERRUPTED", run
    assert t.runtime.execution_count == 0
    monkeypatch.setattr(execution, phase, original)
    result = resume(t, run)
    assert result["status"] == "SUCCEEDED", {"error": result.get("error_code"), "observations": result["observations"], "draft": result.get("draft"), "pending": result.get("pending_execution")}
    assert t.runtime.execution_count == 3
    assert len(result["executions"]) == 1
    with t.harness.engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM tool_run")) == 1
