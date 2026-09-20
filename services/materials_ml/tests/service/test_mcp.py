import asyncio
from contextlib import asynccontextmanager
import json
import time

import httpx
import numpy as np
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from materials_ml import read_csv, load_package, predict
from materials_ml_service.domain import Prediction
from materials_ml_service.mcp_adapter import IDEMPOTENCY_META
from test_end_to_end import Server, client, worker_process, wait_status


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def mcp_server(service):
    settings = service.settings.model_copy(update={"mcp_enabled": True})
    server = Server(settings)
    try:
        server.start()
        yield server
    finally:
        server.stop()


@asynccontextmanager
async def mcp_client(server, scope="independent"):
    headers = {"Authorization": "Bearer " + server.settings.mcp_token.get_secret_value(), "X-ML-Scope-ID": scope}
    async with httpx.AsyncClient(headers=headers, timeout=45, trust_env=False) as http:
        async with streamable_http_client(server.url + "/mcp", http_client=http, terminate_on_close=False) as (read, write, session_id):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.protocolVersion == "2025-11-25"
                yield session, session_id


@pytest.mark.anyio
@pytest.mark.parametrize("algorithm", ["LR", "RF"])
async def test_real_mcp_training_worker_prediction_and_rest_replay(mcp_server, csv_payload, tmp_path, algorithm):
    server = mcp_server
    with client(server) as http:
        root = "/api/v1/scopes/independent"
        dataset = http.post(root + "/datasets", headers={"Idempotency-Key": "upload"},
                            files={"file": ("data.csv", csv_payload, "text/csv")}).json()
        table = read_csv(csv_payload)
        inputs = http.post(root + "/datasets", headers={"Idempotency-Key": "features"},
            files={"file": ("features.csv", table[["z", "x"]].to_csv(index=False).encode(), "text/csv")}).json()
        async with mcp_client(server) as (session, _):
            catalog = await session.list_tools()
            assert {t.name for t in catalog.tools} == {"analyze_tabular_dataset", "train_tabular_regression", "get_training_run", "predict_with_model"}
            for tool in catalog.tools:
                assert "scope_id" not in tool.inputSchema["properties"]
                assert tool.outputSchema and tool.meta["schema_sha256"]
            analysis = await session.call_tool("analyze_tabular_dataset", {"dataset_id": dataset["id"]})
            assert not analysis.isError and analysis.structuredContent["resource"]["identity"]["row_count"] == len(table)
            spec = {"dataset_id": dataset["id"], "features": ["x", "z"], "target": "strength_MPa", "algorithm": algorithm}
            response = await session.call_tool("train_tabular_regression", spec, meta={IDEMPOTENCY_META: "train"})
            assert not response.isError, response
            run = response.structuredContent["resource"]
            assert run["status"] == "PENDING"
            replay = http.post(root + "/training-runs", headers={"Idempotency-Key": "train"}, json=spec)
            assert replay.json()["id"] == run["id"]
        # Closing a real MCP client after persistence cannot cancel the TrainingRun.
        worker = worker_process(server)
        try:
            run = await asyncio.to_thread(wait_status, http, run["id"], {"SUCCEEDED", "FAILED"})
            assert run["status"] == "SUCCEEDED", run
            assert await asyncio.to_thread(worker.wait, 15) == 0
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=5)
        async with mcp_client(server) as (session, _):
            status = await session.call_tool("get_training_run", {"training_run_id": run["id"]})
            assert status.structuredContent["resource"]["metrics"]["r2"] > .5
            args = {"model_id": run["model_id"], "input_dataset_id": inputs["id"]}
            output = await session.call_tool("predict_with_model", args, meta={IDEMPOTENCY_META: "predict"})
            assert not output.isError, output
            prediction = output.structuredContent["resource"]
            assert prediction["status"] == "SUCCEEDED", prediction
            replay = http.post(root + "/predictions", headers={"Idempotency-Key": "predict"}, json=args)
            assert replay.json()["id"] == prediction["id"]
            values = http.get(root + f"/predictions/{prediction['id']}/content").json()
            for member in ("manifest.json", "pipeline.joblib", "evaluation.json", "splits.json"):
                (tmp_path / member).write_bytes(http.get(root + f"/models/{run['model_id']}/files/{member}").content)
            expected = predict(load_package(tmp_path, trusted=True), table[["x", "z"]]).values
            np.testing.assert_allclose(values["values"], expected, rtol=1e-10, atol=1e-12)
            # Opposite entry direction: Resource request first, then same MCP idempotency key.
            first = await asyncio.to_thread(http.post, root + "/predictions", headers={"Idempotency-Key": "rest-first"}, json=args)
            again = await session.call_tool("predict_with_model", args, meta={IDEMPOTENCY_META: "rest-first"})
            assert again.structuredContent["resource"]["id"] == first.json()["id"]


def protocol_headers(server, session, scope):
    return {"Authorization": "Bearer " + server.settings.mcp_token.get_secret_value(),
            "MCP-Session-Id": session, "MCP-Protocol-Version": "2025-11-25",
            "X-ML-Scope-ID": scope, "Accept": "application/json, text/event-stream"}


@pytest.mark.anyio
async def test_same_session_parallel_scopes_authentication_and_errors(mcp_server, csv_payload):
    server = mcp_server
    with client(server) as resource:
        datasets = {scope: resource.post(f"/api/v1/scopes/{scope}/datasets", headers={"Idempotency-Key": "upload"},
            files={"file": ("data.csv", csv_payload, "text/csv")}).json()["id"] for scope in ("a", "b")}
        huge = resource.post('/api/v1/scopes/a/datasets', headers={'Idempotency-Key': 'large-metadata'},
            files={'file': ('large.csv', ('column-' + 'x'*40000 + '\n1\n2\n').encode(), 'text/csv')}).json()['id']
    async with mcp_client(server, "a") as (session, session_id):
        large_result = await session.call_tool('analyze_tabular_dataset', {'dataset_id': huge})
        assert large_result.isError and large_result.structuredContent['error']['code'] == 'TOOL_RESULT_TOO_LARGE'
        assert large_result.structuredContent['resource']['id'] == huge
        assert len(json.dumps(large_result.structuredContent).encode()) < 65536
        async with httpx.AsyncClient(base_url=server.url, trust_env=False, timeout=20) as http:
            async def query(scope, identity, rid):
                return await http.post("/mcp", headers=protocol_headers(server, session_id(), scope),
                    json={"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                          "params": {"name": "analyze_tabular_dataset", "arguments": {"dataset_id": identity}}})
            responses = await asyncio.gather(query("a", datasets["a"], "a1"), query("b", datasets["b"], "b1"))
            assert [r.json()["result"]["structuredContent"]["resource"]["scope_id"] for r in responses] == ["a", "b"]
            wrong_scope = await query("a", datasets["b"], "wrong")
            assert wrong_scope.json()["result"]["structuredContent"]["error"]["code"] == "RESOURCE_NOT_FOUND"
            missing = protocol_headers(server, session_id(), "a"); missing.pop("X-ML-Scope-ID")
            response = await http.post("/mcp", headers=missing,
                json={"jsonrpc": "2.0", "id": "missing", "method": "tools/call", "params": {"name": "analyze_tabular_dataset", "arguments": {"dataset_id": datasets['a']}}})
            assert response.status_code == 400
            malformed = await http.post("/mcp", headers=protocol_headers(server, session_id(), "a"),
                json={"jsonrpc": "2.0", "id": "bad", "method": "tools/call", "params": {"name": 123, "arguments": {"private": "secret-marker"}}})
            assert "secret-marker" not in malformed.text and malformed.json()["error"]["code"] == -32602
            for name, arguments, expected in (("unknown_tool", {}, -32602),
                ("analyze_tabular_dataset", {"dataset_id": datasets['a'], "scope_id": "b"}, -32602)):
                bad = await http.post('/mcp', headers=protocol_headers(server, session_id(), 'a'),
                    json={'jsonrpc': '2.0', 'id': name, 'method': 'tools/call', 'params': {'name': name, 'arguments': arguments}})
                assert bad.json()['error']['code'] == expected
            oversize = await http.post('/mcp', headers={**protocol_headers(server, session_id(), 'a'), 'Content-Type': 'application/json'}, content=b' ' * 65537)
            assert oversize.status_code == 413
            invalid_origin = {**protocol_headers(server, session_id(), 'a'), 'Origin': 'http://invalid.example'}
            assert (await http.post('/mcp', headers=invalid_origin, json={'jsonrpc':'2.0','id':'origin','method':'ping'})).status_code == 403
            for path, method, token in (("/mcp", "post", server.settings.resource_token),
                ("/mcp", "post", server.settings.worker_token),
                ("/api/v1/scopes/a/datasets", "get", server.settings.mcp_token),
                ("/api/v1/scopes/a/datasets", "get", server.settings.worker_token),
                ("/internal/v1/recovery", "get", server.settings.mcp_token),
                ("/internal/v1/recovery", "get", server.settings.resource_token)):
                r = await http.request(method, path, headers={"Authorization": "Bearer " + token.get_secret_value()})
                assert r.status_code == 401
            assert (await http.get("/internal/v1/recovery", headers={"Authorization": "Bearer " + server.settings.worker_token.get_secret_value()})).status_code == 200
            assert (await http.post("/mcp", headers={"MCP-Session-Id": session_id()})).status_code == 401
            # Session is protocol state only; restart invalidates it without changing domain resources.
            old_session = session_id()
        # Do not stop the server while the client still owns its transport tasks.
    server.stop(); server.start()
    async with httpx.AsyncClient(trust_env=False) as http:
        response = await http.post(server.url + "/mcp", headers=protocol_headers(server, old_session, "a"),
            json={"jsonrpc": "2.0", "id": "old", "method": "ping"})
        assert response.status_code == 404
    async with mcp_client(server, "b") as (session, _):
        result = await session.call_tool("analyze_tabular_dataset", {"dataset_id": datasets["b"]})
        assert not result.isError
