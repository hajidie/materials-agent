import asyncio
from contextlib import suppress
import time

import httpx
import pytest

from materials_ml_service.domain import Prediction, TrainingRun
from test_end_to_end import Server
from test_mcp import mcp_client, protocol_headers
from test_predictions import prediction_inputs
from test_windows import active_job_handles, assert_exited


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def fault_server(service, prediction_inputs):
    server = Server(service.settings.model_copy(update={"mcp_enabled": True}))
    # Test-only launcher injects a hang after supervised startup, and a delayed training receipt.
    # No production environment flag can activate these faults.
    server.program = '''
import sys, time, uvicorn
from materials_ml_service.api import create_app
from materials_ml_service.windows import JobProcess
class Gated(JobProcess):
    def release(self): pass
app = create_app()
app.state.service.prediction_runner = Gated
original = app.state.service.submit_training
def slow_receipt(*args, **kwargs):
    run = original(*args, **kwargs)
    time.sleep(2)
    return run
app.state.service.submit_training = slow_receipt
uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]), access_log=False, log_level='critical')
'''
    try:
        server.start()
        yield server
    finally:
        server.stop()


async def wait_prediction(service, states):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        items = await asyncio.to_thread(service.list, Prediction, "scope-a")
        if items and items[0].status in states:
            return items[0]
        await asyncio.sleep(.03)
    pytest.fail("Prediction did not reach expected state")


@pytest.mark.anyio
@pytest.mark.parametrize("fault", ["notification", "disconnect", "service-death"])
async def test_real_mcp_cancel_disconnect_and_server_death_stop_process_tree(fault_server, service, prediction_inputs, fault):
    server = fault_server
    model, dataset, _ = prediction_inputs
    async with mcp_client(server, "scope-a") as (_, session_id):
        async with httpx.AsyncClient(base_url=server.url, timeout=15, trust_env=False) as http:
            headers = protocol_headers(server, session_id(), "scope-a")
            task = asyncio.create_task(http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": "prediction",
                "method": "tools/call", "params": {"name": "predict_with_model",
                    "arguments": {"model_id": model, "input_dataset_id": dataset},
                    "_meta": {"materials-ml/idempotency-key": "prediction"}}}))
            run = await wait_prediction(service, {"RUNNING"})
            handles = active_job_handles(run.data["job_name"])
            assert handles
            try:
                notification = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "prediction"}}
                wrong = await http.post("/mcp", headers=protocol_headers(server, session_id(), "wrong-scope"), json=notification)
                assert wrong.status_code == 403
                assert service.get(Prediction, "scope-a", run.id).status == "RUNNING"
                if fault == "notification":
                    assert (await http.post("/mcp", headers=headers, json=notification)).status_code == 202
                elif fault == "disconnect":
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                else:
                    server.stop()
                for handle in handles:
                    await asyncio.to_thread(assert_exited, handle)
                if fault != "service-death":
                    result = await wait_prediction(service, {"CANCELLED"})
                    assert result.artifact_id is None and result.process_stopped
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError, httpx.HTTPError):
                    await task
    if fault == "service-death":
        server.start()
        result = await wait_prediction(service, {"FAILED"})
        assert result.data["error_code"] == "SERVICE_INTERRUPTED" and result.artifact_id is None
    assert len(service.list(Prediction, "scope-a")) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True])
async def test_training_persisted_before_cancel_or_disconnect_survives(fault_server, service, prediction_inputs, cancel):
    server = fault_server
    # Existing training fixture supplies a valid immutable dataset; this is a new training submission.
    original = service.list(TrainingRun, "scope-a")[0]
    async with mcp_client(server, "scope-a") as (_, session_id):
        async with httpx.AsyncClient(base_url=server.url, timeout=10, trust_env=False) as http:
            headers = protocol_headers(server, session_id(), "scope-a")
            task = asyncio.create_task(http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": "training",
                "method": "tools/call", "params": {"name": "train_tabular_regression",
                    "arguments": {"dataset_id": original.dataset_id, "features": ["x", "z"], "target": "strength_MPa"},
                    "_meta": {"materials-ml/idempotency-key": "new-training"}}}))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                rows = service.list(TrainingRun, "scope-a")
                if len(rows) == 2:
                    break
                await asyncio.sleep(.03)
            assert len(rows) == 2
            if cancel:
                response = await http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "method": "notifications/cancelled",
                    "params": {"requestId": "training"}})
                assert response.status_code == 202
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            await asyncio.sleep(2.2)
            new = [r for r in service.list(TrainingRun, "scope-a") if r.id != original.id][0]
            assert new.status == "PENDING" and not new.cancel_requested


@pytest.mark.anyio
async def test_mcp_replay_wait_timeout_never_returns_running_or_cancels_original(fault_server, service, prediction_inputs):
    server = fault_server
    server.stop()
    server.program = server.program.replace("original = app.state.service.submit_training", '''
next(route.app for route in app.routes if route.path == "/mcp").prediction_wait_seconds = .4
original = app.state.service.submit_training''')
    server.start()
    model, dataset, _ = prediction_inputs
    args = {"model_id": model, "input_dataset_id": dataset}
    async with httpx.AsyncClient(base_url=server.url, timeout=15, trust_env=False) as http:
        owner = asyncio.create_task(http.post("/api/v1/scopes/scope-a/predictions",
            headers={"Authorization": "Bearer " + server.settings.resource_token.get_secret_value(), "Idempotency-Key": "shared"}, json=args))
        try:
            run = await wait_prediction(service, {"RUNNING"})
            async with mcp_client(server, "scope-a") as (session, _):
                replay = await session.call_tool("predict_with_model", args, meta={"materials-ml/idempotency-key": "shared"})
                assert replay.isError and replay.structuredContent["error"]["code"] == "PREDICTION_OUTCOME_UNKNOWN"
                assert replay.structuredContent["resource"] is None
            original = service.get(Prediction, "scope-a", run.id)
            assert original.status == "RUNNING" and not original.cancel_requested
            cancelled = await http.post(f"/api/v1/scopes/scope-a/predictions/{run.id}/cancel",
                headers={"Authorization": "Bearer " + server.settings.resource_token.get_secret_value()})
            assert cancelled.status_code == 200
            assert (await owner).json()["status"] == "CANCELLED"
            async with mcp_client(server, "scope-a") as (session, _):
                terminal = await session.call_tool("predict_with_model", args, meta={"materials-ml/idempotency-key": "shared"})
                assert terminal.isError and terminal.structuredContent["resource"]["status"] == "CANCELLED"
            assert len(service.list(Prediction, "scope-a")) == 1
        finally:
            owner.cancel()
            with suppress(asyncio.CancelledError, httpx.HTTPError):
                await owner


@pytest.mark.anyio
async def test_mcp_first_prediction_waits_for_unknown_upload_reconciliation(service, prediction_inputs):
    model, dataset, _ = prediction_inputs
    server = Server(service.settings.model_copy(update={"mcp_enabled": True}))
    server.program = '''
import sys, uvicorn
from materials_ml_service.api import create_app
from materials_ml_service.domain import ServiceError
app = create_app()
put = app.state.service.storage.put
def uncertain(ref, payload):
    put(ref, payload)
    raise ServiceError("UPLOAD_OUTCOME_UNKNOWN", 503)
app.state.service.storage.put = uncertain
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), access_log=False, log_level="critical")
'''
    try:
        server.start()
        async with mcp_client(server, "scope-a") as (session, _):
            args = {"model_id": model, "input_dataset_id": dataset}
            output = await session.call_tool("predict_with_model", args, meta={"materials-ml/idempotency-key": "publication"})
            assert not output.isError and output.structuredContent["resource"]["status"] == "SUCCEEDED"
            again = await session.call_tool("predict_with_model", args, meta={"materials-ml/idempotency-key": "publication"})
            assert again.structuredContent["resource"]["id"] == output.structuredContent["resource"]["id"]
        assert len(service.list(Prediction, "scope-a")) == 1
    finally:
        server.stop()
