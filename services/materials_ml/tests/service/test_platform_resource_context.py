"""P6 real infrastructure, with deterministic and separately opted-in real LLM paths."""
import os
import json

import httpx
import numpy as np
import pytest
from dotenv import dotenv_values

from materials_ml import load_package, predict
from test_platform_mcp import platform, conversation, confirm, result, ROOT, diagnostic_run
from test_platform_resources import resources_platform, upload
from test_end_to_end import worker_process


@pytest.fixture
def context_platform(resources_platform):
    backend, ml = resources_platform
    backend.stop()
    backend.settings["enable_materials_ml_resource_context"] = True
    backend.start()
    return backend, ml


def message(http, scope, text, key, waiting=None):
    body = {"mode": "RESUME_RUN" if waiting else "NEW_RUN", "content_text": text}
    if waiting:
        body.update(agent_run_id=waiting["agent_run_id"], waiting_version=waiting["waiting_version"])
    response = http.post(f"/api/v1/conversations/{scope}/messages", json=body, headers={"Idempotency-Key": key})
    assert response.status_code == 200, response.text
    value = diagnostic_run(http, response.json()["data"]["agent_run"])
    if value["error_code"] == "CONTEXT_BUDGET_EXCEEDED":
        print(json.dumps(http.get("/acceptance/p6-budget").json()))
    assert value["status"] != "TERMINATED", json.dumps({"error_code": value["error_code"], "draft": value["draft"],
        "budget": http.get("/acceptance/p6-budget").json() if os.environ.get("P6_REAL_LLM") == "1" else None,
        "actions": [s["action"] for s in value["steps"]], "observation_errors": [o.get("error") for o in value["observations"]]})
    return value


def language_cycle(backend, ml, csv_payload, table, tmp_path, algorithm):
    with httpx.Client(base_url=backend.url, timeout=120, trust_env=False) as http:
        scope = conversation(http, "p6")
        prefix = f"/api/v1/conversations/{scope}/ml"
        dataset = upload(http, scope, csv_payload, "upload")
        analysis = message(http, scope, "请分析刚才上传的数据。", "analyze")
        assert analysis["status"] == "SUCCEEDED", {k: analysis[k] for k in ("status", "waiting", "draft", "error_code")}
        assert result(analysis)["id"] == dataset["resource_id"]
        waiting = message(http, scope, f"请用刚才上传的数据训练 {algorithm} 单目标回归，特征 x、z，目标 strength_MPa。", "train")
        submitted = confirm(http, waiting)
        assert submitted["status"] == "SUCCEEDED", json.dumps({"waiting": submitted["waiting"], "draft": submitted["draft"],
            "actions": [s["action"] for s in submitted["steps"]], "error": submitted["error_code"]})
        training = result(submitted)
        assert training["status"] == "PENDING"
        refs = http.get(prefix + "/resources?limit=100").json()["data"]["items"]
        assert any(r["resource_type"] == "training_run" and r["resource_id"] == training["id"] for r in refs)
        worker = worker_process(ml)
        try:
            assert worker.wait(timeout=60) == 0
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=10)
        queried = result(message(http, scope, "查询刚才的训练。", "query"))
        assert queried["status"] == "SUCCEEDED"
        incoming = table[["z", "x"]].iloc[:10]
        upload(http, scope, incoming.to_csv(index=False).encode(), "incoming")
        waiting = message(http, scope, "请用刚才训练好的模型对第二个数据集执行预测。", "predict")
        assert waiting["status"] == "WAITING_FOR_CONFIRMATION", json.dumps({"waiting": waiting["waiting"], "draft": waiting["draft"],
            "actions": [s["action"] for s in waiting["steps"]]})
        completed = confirm(http, waiting)
        assert completed["status"] == "SUCCEEDED", {"error": completed["error_code"], "observations": [o.get("error") for o in completed["observations"]]}
        prediction = result(completed)
        refs = http.get(prefix + "/resources?limit=100").json()["data"]["items"]
        model = next(r for r in refs if r["resource_type"] == "model")
        predicted = next(r for r in refs if r["resource_type"] == "prediction")
        actual = http.get(prefix + f"/resources/{predicted['reference_id']}/files/predictions.json").json()
        assert prediction["status"] == "SUCCEEDED"
        for member in ("manifest.json", "pipeline.joblib", "evaluation.json", "splits.json"):
            response = http.get(prefix + f"/resources/{model['reference_id']}/files/{member}")
            assert response.status_code == 200
            (tmp_path / member).write_bytes(response.content)
        np.testing.assert_allclose(actual["values"], predict(load_package(tmp_path, trusted=True), incoming).values, rtol=1e-10, atol=1e-12)
        ambiguous = message(http, scope, "两个数据集都可能是目标，我还没有确定要分析哪一个，请让我选择。", "ambiguous")
        assert ambiguous["status"] == "WAITING_FOR_USER", ambiguous
        if ambiguous["waiting"]["reason"] == "TOOL_ARGUMENT_CLARIFICATION":
            assert "选项" in ambiguous["waiting"]["question"]
        assert ambiguous["tool_executions"] == 0
        original_question = ambiguous["steps"][-1]["action"]
        chosen = message(http, scope, "第二个数据集", "choose", ambiguous)
        assert chosen["status"] == "SUCCEEDED", json.dumps({"waiting": chosen["waiting"], "draft": chosen["draft"],
            "actions": [s["action"] for s in chosen["steps"]]})
        assert result(chosen)["id"] != dataset["resource_id"]
        assert chosen["steps"][len(ambiguous["steps"])-1]["action"] == original_question
        assert "resource_snapshot" not in chosen


@pytest.mark.parametrize("algorithm", ["LR", "RF"])
def test_deterministic_language_real_infrastructure(context_platform, csv_payload, table, tmp_path, algorithm):
    language_cycle(*context_platform, csv_payload, table, tmp_path, algorithm)


@pytest.mark.skipif(os.environ.get("P6_REAL_LLM") != "1", reason="Explicit real-provider P6 acceptance is opt-in")
def test_opt_in_real_llm_language_cycle(context_platform, csv_payload, table, tmp_path):
    backend, ml = context_platform
    values = dotenv_values(ROOT / ".env")
    assert values.get("DEEPSEEK_API_KEY"), "Real Provider credential is required for opt-in acceptance"
    backend.stop()
    backend.settings.update(llm_adapter="provider", deepseek_api_key=values["DEEPSEEK_API_KEY"])
    # Keep the real-provider acceptance on the bounded 24K deployment profile.
    # The committed decision default now uses the same allowance; never auto-grow.
    backend.decision_prompt_limit_tokens = 24576
    backend.start()
    language_cycle(backend, ml, csv_payload, table, tmp_path, "LR")


@pytest.mark.skipif(os.environ.get("P6_REAL_LLM") != "1", reason="Explicit real-provider semantic acceptance is opt-in")
def test_opt_in_real_llm_pronouns_context_and_clarification(context_platform, csv_payload):
    backend, _ = context_platform
    values = dotenv_values(ROOT / ".env")
    assert values.get("DEEPSEEK_API_KEY")
    backend.stop()
    backend.settings.update(llm_adapter="provider", deepseek_api_key=values["DEEPSEEK_API_KEY"])
    backend.decision_prompt_limit_tokens = 24576
    backend.start()
    with httpx.Client(base_url=backend.url, timeout=120, trust_env=False) as http:
        for i, text in enumerate(("请分析该数据集", "请分析这个文件", "请分析刚上传的数据", "继续分析它",
                                  "麻烦检查一下我放进来的那份表")):
            # Each first request must actually inspect the uploaded Dataset.
            # Repeated requests in one conversation may legitimately explain
            # an earlier analysis without executing the read-only Tool again.
            scope = conversation(http, f"semantics-{i}")
            first = upload(http, scope, csv_payload, "first")
            run = message(http, scope, text, f"pronoun-{i}")
            assert run["status"] == "SUCCEEDED", {"text": text, "waiting": run["waiting"], "draft": run["draft"]}
            assert result(run)["id"] == first["resource_id"]
            assert run["tool_executions"] == 1
        denied = message(http, scope, "请不要分析或调用工具，只回复收到。", "no-action")
        assert denied["tool_executions"] == 0
        second = upload(http, scope, csv_payload, "second")
        selected = message(http, scope, "帮我看看后传上来的那份表。", "context-ranked")
        assert selected["status"] == "SUCCEEDED", json.dumps({"waiting": selected["waiting"], "draft": selected["draft"],
            "actions": [s["action"] for s in selected["steps"]]})
        assert result(selected)["id"] == second["resource_id"]
        ambiguous = message(http, scope, "我想分析一个数据集，不过还没决定用哪份，先问我再执行分析。", "ambiguous")
        assert ambiguous["status"] == "WAITING_FOR_USER"
        chosen = message(http, scope, "就用先上传的那份", "semantic-choice", ambiguous)
        assert chosen["status"] == "SUCCEEDED", json.dumps({"waiting": chosen["waiting"], "draft": chosen["draft"],
            "actions": [s["action"] for s in chosen["steps"]]})
        assert result(chosen)["id"] == first["resource_id"]


@pytest.mark.parametrize("fault", ["p6-registration-unavailable", "commit-before"])
def test_registration_recovery_and_unknown_explicit_reconcile_preserve_history(context_platform, csv_payload, fault):
    backend, ml = context_platform
    with httpx.Client(base_url=backend.url, timeout=90, trust_env=False) as http:
        scope = conversation(http, "recover")
        prefix = f"/api/v1/conversations/{scope}/ml"
        upload(http, scope, csv_payload, "data")
        backend.stop(); backend.fault = fault; backend.start()
        waiting = message(http, scope, "用刚才上传的数据训练 LR，特征 x、z，目标 strength_MPa", "train")
        completed = confirm(http, waiting)
        expected = "TERMINATED" if fault == "commit-before" else "SUCCEEDED"
        assert completed["status"] == expected
        if fault == "commit-before":
            assert completed["observations"][-1]["error"]["code"] == "MCP_OUTCOME_UNKNOWN"
        assert len(http.get(prefix + "/resources").json()["data"]["items"]) == 1
        backend.stop(); backend.fault = None; backend.start()
        # Startup receipt checking is not an explicit registration authorization.
        assert len(http.get(prefix + "/resources").json()["data"]["items"]) == 1
        base = f"/api/v1/agent-runs/{completed['agent_run_id']}"
        invocation = completed["executions"][0]["invocation_run_id"]
        endpoint = base + f"/invocations/{invocation}/reconcile" if fault == "commit-before" else base + "/resources/reconcile"
        assert http.post(endpoint).status_code == 200
        assert http.post(endpoint).status_code == 200
        refs = http.get(prefix + "/resources").json()["data"]["items"]
        assert len(refs) == 2
        original = diagnostic_run(http, http.get(base).json()["data"])
        assert original["status"] == completed["status"]
        assert original["observations"] == completed["observations"]
        assert original["final_answer"] == completed["final_answer"]
        if fault == "commit-before":
            registered = next(r for r in refs if r["resource_type"] == "training_run")
            assert ":reconcile:" in registered["source"]


@pytest.mark.parametrize("fault", ["deleted", "offline", "disabled"])
def test_confirmation_freezes_resource_and_authority_failure_stops_dispatch(context_platform, csv_payload, fault):
    backend, ml = context_platform
    with httpx.Client(base_url=backend.url, timeout=90, trust_env=False) as http:
        scope = conversation(http, "frozen")
        prefix = f"/api/v1/conversations/{scope}/ml"
        dataset = upload(http, scope, csv_payload, "data")
        waiting = message(http, scope, "用刚才上传的数据训练 LR，特征 x、z，目标 strength_MPa", "train")
        if fault == "deleted":
            assert http.delete(prefix + f"/resources/{dataset['reference_id']}").status_code == 200
        elif fault == "offline":
            ml.stop()
        else:
            backend.stop(); backend.settings["enable_materials_ml_resource_context"] = False; backend.start()
        completed = confirm(http, waiting)
        assert completed["status"] == "TERMINATED", completed
        assert completed["observations"][-1]["status"] == "FAILED"
        assert completed["final_answer"] is None
        assert len(completed["calls"]) == len(waiting["calls"])
