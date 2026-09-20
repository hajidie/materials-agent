"""Real HTTP Backend/Invocation/SDK/ML/Worker acceptance, in separate Python environments."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import secrets
import socket
import subprocess
import time

from dotenv import dotenv_values
import httpx
import numpy as np
import psycopg
from psycopg import sql
import pytest

from materials_ml import read_csv, load_package, predict
from test_end_to_end import Server, client, worker_process, process_environment

ROOT = Path(__file__).resolve().parents[4]


class Backend:
    def __init__(self, settings, python):
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); self.port = sock.getsockname()[1]
        self.settings, self.python, self.process = settings, python, None
        self.url = f"http://127.0.0.1:{self.port}"

    def start(self):
        self.process = subprocess.Popen([self.python, "-I", str(ROOT / "backend/tests/support/ml_platform_server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env=process_environment(), creationflags=subprocess.CREATE_NO_WINDOW)
        self.process.stdin.write(json.dumps({"settings": self.settings, "port": self.port, "fault": getattr(self, "fault", None),
            "frontend": getattr(self, "frontend", False),
            "decision_prompt_limit_tokens": getattr(self, "decision_prompt_limit_tokens", None)}).encode() + b"\n")
        self.process.stdin.flush(); self.process.stdin.close()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                # Tracebacks contain no command-line or environment credentials.
                pytest.fail("Acceptance Backend stopped: " + self.process.stderr.read().decode(errors="replace"))
            try:
                if httpx.get(self.url + "/api/v1/health/live", trust_env=False, timeout=1).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(.1)
        pytest.fail("Acceptance Backend startup timed out")

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.kill(); self.process.wait(timeout=10)


@pytest.fixture
def platform(service):
    python = os.environ.get("P4_BACKEND_PYTHON")
    if not python:
        pytest.skip("P4_BACKEND_PYTHON must name the isolated Backend Python 3.11 interpreter")
    assert Path(python).is_file()
    values = dotenv_values(ROOT / ".env")
    name = "materialsagent_test_" + secrets.token_hex(8)
    assert re.fullmatch(r"materialsagent_test_[0-9a-f]{16}", name)
    pg = {"host": values.get("POSTGRES_HOST", "127.0.0.1"), "port": int(values.get("POSTGRES_PORT", 5432)),
          "user": values["POSTGRES_USER"], "password": values["POSTGRES_PASSWORD"], "dbname": "postgres"}
    ml = Server(service.settings.model_copy(update={"mcp_enabled": True}))
    backend = None
    with psycopg.connect(**pg, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        ml.start()
        settings = {"app_env": "test", "log_level": "CRITICAL", "postgres_host": pg["host"], "postgres_port": pg["port"],
            "postgres_user": pg["user"], "postgres_password": pg["password"], "postgres_db": name,
            "enable_dev_materials_ml_tools": True, "enable_materials_ml_resources": True, "enable_materials_ml_resource_context": True, "materials_ml_mcp_url": ml.url + "/mcp",
            "materials_ml_mcp_token": service.settings.mcp_token.get_secret_value(),
            "materials_ml_resource_token": service.settings.resource_token.get_secret_value(),
            "agent_standard_timeout_seconds": 60, "agent_max_llm_tokens": 200000}
        backend = Backend(settings, python)
        backend.start()
        yield backend, ml
    finally:
        if backend:
            backend.stop()
        ml.stop()
        with psycopg.connect(**pg, autocommit=True) as connection:
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def conversation(http, key):
    response = http.post("/api/v1/conversations", json={"title": "P4 synthetic acceptance"}, headers={"Idempotency-Key": key})
    assert response.status_code == 201, response.text
    return response.json()["data"]["conversation_id"]


def diagnostic_run(http, view):
    return http.get("/acceptance/runs/" + view["agent_run_id"]).json()["data"]


def invoke(http, scope, tool, arguments, key):
    fields = {"dataset_id": ("dataset", "dataset_reference"), "input_dataset_id": ("dataset", "input_dataset_reference"),
        "model_id": ("model", "model_reference"), "training_run_id": ("training_run", "training_reference")}
    prefix = f"/api/v1/conversations/{scope}/ml/resources"
    bound = {}
    for field, identity in arguments.items():
        if field not in fields:
            continue
        response = http.post(prefix, json={"resource_type": fields[field][0], "resource_id": identity})
        assert response.status_code == 200, response.text
        ref = response.json()["data"]
        bound[fields[field][1]] = {"resource_type": fields[field][0],
            **({"dataset_ordinal": ref["dataset_ordinal"]} if ref.get("dataset_ordinal") is not None else {})}
    response = http.post(f"/api/v1/conversations/{scope}/messages", headers={"Idempotency-Key": key},
        json={"mode": "NEW_RUN", "content_text": json.dumps({"acceptance_tool": "materials_ml_" + tool,
            "arguments": {k: v for k, v in arguments.items() if k not in fields}, "reference_selectors": bound})})
    assert response.status_code == 200, response.text
    return diagnostic_run(http, response.json()["data"]["agent_run"])


def supplement(http, scope, run, text, key):
    response = http.post(f"/api/v1/conversations/{scope}/messages", headers={"Idempotency-Key": key}, json={
        "mode": "RESUME_RUN", "agent_run_id": run["agent_run_id"], "waiting_version": run["waiting_version"],
        "content_text": text})
    assert response.status_code == 200, response.text
    return diagnostic_run(http, response.json()["data"]["agent_run"])


def confirm(http, run):
    assert run["status"] == "WAITING_FOR_CONFIRMATION", run
    pending = run["pending_execution"]
    response = http.post(f"/api/v1/agent-runs/{run['agent_run_id']}/invocations/{pending['invocation_run_id']}/confirm",
        json={"waiting_version": run["waiting_version"], "confirmation_version": pending["confirmation_version"]})
    assert response.status_code == 200, response.text
    return diagnostic_run(http, response.json()["data"])


def result(run):
    assert run["status"] == "SUCCEEDED", run
    return next(o for o in run["observations"] if o["kind"] == "TOOL_RESULT")["data"]["resource"]


@pytest.mark.parametrize("algorithm", ["LR", "RF"])
def test_platform_real_agent_invocation_mcp_worker_prediction(platform, csv_payload, tmp_path, algorithm):
    backend, ml = platform
    with httpx.Client(base_url=backend.url, trust_env=False, timeout=75) as http, client(ml) as resource:
        scope = conversation(http, "conversation")
        root = f"/api/v1/scopes/{scope}"
        dataset = resource.post(root + "/datasets", headers={"Idempotency-Key": "dataset"},
            files={"file": ("synthetic.csv", csv_payload, "text/csv")}).json()
        analysis = result(invoke(http, scope, "analyze_tabular_dataset", {"dataset_id": dataset["id"]}, "analyze"))
        assert analysis["id"] == dataset["id"]
        args = {"dataset_id": dataset["id"], "features": ["x", "z"], "target": "strength_MPa", "algorithm": algorithm}
        # The model may infer a unit, separately from canonical Service metadata.
        assert dataset["units"]["strength_MPa"] is None
        rejected = resource.post(root + "/operation-identities/prepare", json={
            "operation": "training.submit", "arguments": {**args, "units": {"strength_MPa": "MPa"}}})
        assert rejected.status_code == 422 and rejected.json()["error"]["code"] == "UNIT_CONFLICT"
        annotation = {"resource_parameter": "dataset_reference", "column": "strength_MPa", "unit": "MPa",
            "source": "model_inference", "evidence": "strength_MPa", "usage": "interpretation"}
        needs_unit = invoke(http, scope, "train_tabular_regression", {**args, "semantic_annotations": [annotation]}, "train")
        assert needs_unit["status"] == "WAITING_FOR_USER"
        waiting = supplement(http, scope, needs_unit, "确认 strength_MPa 的单位是 MPa", "confirm-unit")
        assert waiting["pending_execution"]["unit_annotations"][0]["provenance"] == "confirmed"
        assert not {"units", "semantic_annotations"} & waiting["pending_execution"]["arguments"].keys()
        assert resource.get(root + "/training-runs").json()["items"] == []
        # Populate every pool slot, then invalidate the sessions while the user
        # reviews confirmation. Confirmation must renew preflight, not fail or
        # resubmit a tools/call. Both LR and RF still create exactly one run.
        result(invoke(http, scope, "analyze_tabular_dataset", {"dataset_id": dataset["id"]}, "warm-pool"))
        ml.stop(); ml.start()
        submitted = confirm(http, waiting)
        run = result(submitted)
        assert submitted["observations"][0]["unit_annotations"][0]["provenance"] == "confirmed"
        assert "用户确认" in submitted["final_answer"]["text"]
        assert run["status"] == "PENDING"
        assert result(confirm(http, waiting))["id"] == run["id"]
        assert len(resource.get(root + "/training-runs").json()["items"]) == 1
        worker = worker_process(ml)
        try:
            assert worker.wait(timeout=45) == 0, "Worker failed"
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=5)
        trained = result(invoke(http, scope, "get_training_run", {"training_run_id": run["id"]}, "query"))
        assert trained["status"] == "SUCCEEDED"
        assert trained["units"]["strength_MPa"] is None
        observed = http.post(f"/api/v1/conversations/{scope}/results/reconcile", json={}).json()["data"]
        assert len(observed["messages"]) == 1 and "用户确认" in observed["messages"][0]["text"]
        assert observed["messages"][0]["presentation"]["unit_annotations"][0]["provenance"] == "confirmed"
        table = read_csv(csv_payload)
        inputs = resource.post(root + "/datasets", headers={"Idempotency-Key": "features"},
            files={"file": ("features.csv", table[["z", "x"]].to_csv(index=False).encode(), "text/csv")}).json()
        prediction_unit = invoke(http, scope, "predict_with_model",
            {"model_id": trained["model_id"], "input_dataset_id": inputs["id"],
             "semantic_annotations": [{**annotation, "resource_parameter": "model_reference"}]}, "predict")
        predicted = confirm(http, supplement(http, scope, prediction_unit,
            "确认模型目标 strength_MPa 的单位是 MPa", "confirm-prediction-unit"))
        prediction = result(predicted)
        assert prediction["status"] == "SUCCEEDED"
        assert predicted["observations"][0]["unit_annotations"][0]["provenance"] == "confirmed"
        assert "用户确认" in predicted["final_answer"]["text"]
        values = resource.get(root + f"/predictions/{prediction['id']}/content").json()
        for member in ("manifest.json", "pipeline.joblib", "evaluation.json", "splits.json"):
            (tmp_path / member).write_bytes(resource.get(root + f"/models/{trained['model_id']}/files/{member}").content)
        np.testing.assert_allclose(values["values"], predict(load_package(tmp_path, trusted=True), table[["x", "z"]]).values,
                                   rtol=1e-10, atol=1e-12)


def test_official_client_pool_releases_slots_without_scope_leak_or_session_growth(platform, csv_payload):
    backend, ml = platform
    datasets = []
    with httpx.Client(base_url=backend.url, trust_env=False, timeout=20) as http, client(ml) as resource:
        for i in range(2):
            scope = conversation(http, "scope-" + str(i))
            uploaded = resource.post(f"/api/v1/scopes/{scope}/datasets", headers={"Idempotency-Key": "dataset"},
                files={"file": ("synthetic.csv", csv_payload, "text/csv")}).json()
            datasets.append([scope, uploaded["id"]])
        process = subprocess.run([backend.python, "-I", str(ROOT / "backend/tests/support/mcp_client_probe.py")],
            input=json.dumps({"url": ml.url + "/mcp", "token": ml.settings.mcp_token.get_secret_value(),
                "resource_token": ml.settings.resource_token.get_secret_value(), "datasets": datasets}).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=process_environment(), timeout=60,
            creationflags=subprocess.CREATE_NO_WINDOW)
        assert process.returncode == 0, process.stderr.decode(errors="replace")
        assert json.loads(process.stdout)["calls"] == 40
        scope, dataset = datasets[0]
        for index in range(2):  # prepare and execute each lease one slot: initialize all four.
            assert result(invoke(http, scope, "analyze_tabular_dataset", {"dataset_id": dataset}, "before-restart-" + str(index)))["id"] == dataset
        ml.stop(); ml.start()
        for index in range(4):
            restored = invoke(http, scope, "analyze_tabular_dataset", {"dataset_id": dataset}, "stale-session-" + str(index))
            assert result(restored)["id"] == dataset
        assert result(invoke(http, scope, "analyze_tabular_dataset", {"dataset_id": dataset}, "new-session"))["id"] == dataset


@pytest.mark.parametrize("fault", ["commit-before", "commit-after"])
def test_real_local_commit_uncertainty_never_overwrites_success_or_resubmits(platform, csv_payload, fault):
    backend, ml = platform
    backend.stop(); backend.fault = fault; backend.start()
    with httpx.Client(base_url=backend.url, trust_env=False, timeout=75) as http, client(ml) as resource:
        scope = conversation(http, "scope")
        root = f"/api/v1/scopes/{scope}"
        dataset = resource.post(root + "/datasets", headers={"Idempotency-Key": "data"},
            files={"file": ("synthetic.csv", csv_payload, "text/csv")}).json()
        waiting = invoke(http, scope, "train_tabular_regression",
            {"dataset_id": dataset["id"], "features": ["x", "z"], "target": "strength_MPa"}, "train")
        completed = confirm(http, waiting)
        runs = resource.get(root + "/training-runs").json()["items"]
        assert len(runs) == 1
        if fault == "commit-after":
            assert result(completed)["id"] == runs[0]["id"]
            assert result(confirm(http, waiting))["id"] == runs[0]["id"]
        else:
            assert completed["status"] == "TERMINATED" and completed["final_answer"] is None
            observation = next(o for o in completed["observations"] if o["kind"] == "TOOL_RESULT")
            assert observation["error"]["code"] == "MCP_OUTCOME_UNKNOWN"
            prefix = f"/api/v1/agent-runs/{waiting['agent_run_id']}/invocations/{observation['invocation_run_id']}"
            reconciled = http.post(prefix + "/reconcile").json()["data"]
            assert reconciled["status"] == "OUTCOME_UNKNOWN" and reconciled["remote_receipt"]["resource"]["id"] == runs[0]["id"]


DELAY_TRAIN = '''
import sys, time, uvicorn
from materials_ml_service.api import create_app
app = create_app()
original = app.state.service.submit_training
def delayed(*args, **kwargs):
    run = original(*args, **kwargs)
    time.sleep(6)
    return run
app.state.service.submit_training = delayed
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), access_log=False, log_level="critical")
'''


@pytest.mark.parametrize("fault", ["timeout", "backend-death", "backend-disabled"])
def test_unknown_training_safe_observation_historical_receipt_and_no_reexecution(platform, csv_payload, fault):
    backend, ml = platform
    ml.stop(); ml.program = DELAY_TRAIN; ml.start()
    backend.stop()
    backend.settings["agent_standard_timeout_seconds"] = 3 if fault == "timeout" else 60
    backend.start()
    with httpx.Client(base_url=backend.url, trust_env=False, timeout=75) as http, client(ml) as resource:
        scope = conversation(http, "scope")
        root = f"/api/v1/scopes/{scope}"
        dataset = resource.post(root + "/datasets", headers={"Idempotency-Key": "data"},
            files={"file": ("synthetic.csv", csv_payload, "text/csv")}).json()
        waiting = invoke(http, scope, "train_tabular_regression",
            {"dataset_id": dataset["id"], "features": ["x", "z"], "target": "strength_MPa"}, "train")
        if fault == "timeout":
            stopped = confirm(http, waiting)
        else:
            with ThreadPoolExecutor(1) as pool:
                future = pool.submit(confirm, http, waiting)
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if resource.get(root + "/training-runs").json()["items"]:
                        break
                    time.sleep(.05)
                else:
                    pytest.fail("Training submission was not persisted")
                backend.stop()
                try:
                    future.result(timeout=10)
                except httpx.HTTPError:
                    pass
            if fault == "backend-disabled":
                backend.settings["enable_dev_materials_ml_tools"] = False
            backend.settings["enable_materials_ml_resource_context"] = False
            backend.start()
            stopped = diagnostic_run(http, http.get(f"/api/v1/agent-runs/{waiting['agent_run_id']}").json()["data"])
        assert stopped["status"] == "TERMINATED" and stopped["final_answer"] is None
        assert len(stopped["calls"]) == len(waiting["calls"]), "Model continued after unknown"
        observation = next(o for o in stopped["observations"] if o["kind"] == "TOOL_RESULT")
        assert observation["error"]["code"] == "MCP_OUTCOME_UNKNOWN"
        assert observation["error"]["outcome"] == "UNKNOWN" and observation["error"]["retryable"] is False
        invocation = observation["invocation_run_id"]
        prefix = f"/api/v1/agent-runs/{waiting['agent_run_id']}/invocations/{invocation}"
        if fault == "backend-disabled":
            assert http.get(prefix + "/receipt").json()["data"]["status"] == "OUTCOME_UNKNOWN"
            backend.stop(); backend.settings["enable_dev_materials_ml_tools"] = True; backend.start()
        reconciled = http.post(prefix + "/reconcile").json()["data"]
        assert reconciled["status"] == "OUTCOME_UNKNOWN"
        assert reconciled["remote_receipt"]["lookup_status"] == "FOUND"
        remote = reconciled["remote_receipt"]["resource"]
        assert remote["status"] == "PENDING"
        with ThreadPoolExecutor(4) as pool:
            checks = list(pool.map(lambda _: http.post(prefix + "/reconcile"), range(4)))
        assert all(r.status_code == 200 and r.json()["data"]["status"] == "OUTCOME_UNKNOWN"
                   and r.json()["data"]["remote_receipt"]["resource"]["id"] == remote["id"] for r in checks)
        retry = http.post(f"/api/v1/agent-runs/{waiting['agent_run_id']}/retry", headers={"Idempotency-Key": "forbidden-retry"},
            json={"retry_type": "TOOL_RETRY", "invocation_run_id": invocation})
        assert retry.status_code >= 400
        assert len(resource.get(root + "/training-runs").json()["items"]) == 1
        worker = worker_process(ml)
        try:
            assert worker.wait(timeout=45) == 0
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=5)
        assert resource.get(root + "/training-runs/" + remote["id"]).json()["status"] == "SUCCEEDED"


GATED_PREDICTION = '''
import sys, uvicorn
from materials_ml_service.api import create_app
from materials_ml_service.windows import JobProcess
class Gated(JobProcess):
    def release(self): pass
app = create_app()
app.state.service.prediction_runner = Gated
uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), access_log=False, log_level="critical")
'''


@pytest.mark.parametrize("fault", ["timeout", "backend-death"])
def test_platform_prediction_interruption_stops_tree_and_blocks_agent(platform, service, csv_payload, fault):
    from materials_ml_service.domain import Prediction
    from test_windows import active_job_handles, assert_exited
    backend, ml = platform
    with httpx.Client(base_url=backend.url, trust_env=False, timeout=75) as http, client(ml) as resource:
        scope = conversation(http, "scope")
        root = f"/api/v1/scopes/{scope}"
        dataset = resource.post(root + "/datasets", headers={"Idempotency-Key": "data"},
            files={"file": ("synthetic.csv", csv_payload, "text/csv")}).json()
        training = result(confirm(http, invoke(http, scope, "train_tabular_regression",
            {"dataset_id": dataset["id"], "features": ["x", "z"], "target": "strength_MPa"}, "train")))
        worker = worker_process(ml)
        try:
            assert worker.wait(timeout=45) == 0
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=5)
        training = resource.get(root + "/training-runs/" + training["id"]).json()
        assert training["status"] == "SUCCEEDED"
        inputs = resource.post(root + "/datasets", headers={"Idempotency-Key": "features"},
            files={"file": ("features.csv", read_csv(csv_payload)[["x", "z"]].to_csv(index=False).encode(), "text/csv")}).json()
        ml.stop(); ml.program = GATED_PREDICTION; ml.start()
        backend.stop(); backend.settings["agent_standard_timeout_seconds"] = 3 if fault == "timeout" else 60
        backend.start()
        waiting = invoke(http, scope, "predict_with_model", {"model_id": training["model_id"], "input_dataset_id": inputs["id"]}, "predict")
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(confirm, http, waiting)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                rows = service.list(Prediction, scope)
                if rows and rows[0].status == "RUNNING":
                    break
                time.sleep(.03)
            else:
                pytest.fail("Prediction was not supervised")
            handles = active_job_handles(rows[0].data["job_name"])
            assert handles
            if fault == "backend-death":
                backend.stop()
                try:
                    pending.result(timeout=10)
                except httpx.HTTPError:
                    pass
                backend.start()
                stopped = diagnostic_run(http, http.get(f"/api/v1/agent-runs/{waiting['agent_run_id']}").json()["data"])
            else:
                stopped = pending.result(timeout=15)
            for handle in handles:
                assert_exited(handle)
        assert stopped["status"] == "TERMINATED" and stopped["final_answer"] is None
        assert len(stopped["calls"]) == len(waiting["calls"])
        observation = next(o for o in stopped["observations"] if o["kind"] == "TOOL_RESULT")
        assert observation["error"]["code"] == "MCP_OUTCOME_UNKNOWN"
        assert observation["error"]["retryable"] is False and observation["error"]["outcome"] == "UNKNOWN"
        prefix = f"/api/v1/agent-runs/{waiting['agent_run_id']}/invocations/{observation['invocation_run_id']}"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            remote = http.post(prefix + "/reconcile").json()["data"]
            if remote["remote_receipt"].get("resource", {}).get("status") == "CANCELLED":
                break
            time.sleep(.1)
        assert remote["status"] == "OUTCOME_UNKNOWN"
        assert remote["remote_receipt"]["resource"]["status"] == "CANCELLED"
        assert len(service.list(Prediction, scope)) == 1
