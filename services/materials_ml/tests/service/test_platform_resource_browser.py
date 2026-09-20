"""Opt-in supervised browser acceptance with real infrastructure.

Run with P7_BROWSER_ACCEPTANCE=1 after building the frontend. The temporary
ready.json contains only a local URL and synthetic file paths. A browser operator
must exercise the UI before writing finish.json with the observed checks.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from dotenv import dotenv_values

from test_platform_resource_context import context_platform
from test_platform_resources import resources_platform
from test_platform_mcp import platform, ROOT
from test_end_to_end import process_environment


@pytest.mark.skipif(os.environ.get("P7_BROWSER_ACCEPTANCE") != "1", reason="Supervised browser acceptance is opt-in")
def test_real_browser_resource_workflow(context_platform, table):
    backend, ml = context_platform
    backend.stop()
    backend.frontend = True
    real_llm = os.environ.get("P7_BROWSER_REAL_LLM") == "1"
    if real_llm:
        values = dotenv_values(ROOT / ".env")
        assert values.get("DEEPSEEK_API_KEY"), "Real Provider credential required for explicit opt-in"
        backend.settings.update(llm_adapter="provider", deepseek_api_key=values["DEEPSEEK_API_KEY"])
    backend.decision_prompt_limit_tokens = 24576
    backend.start()
    folder = ROOT / "tmp" / "p7-browser" / str(backend.port)
    folder.mkdir(parents=True, exist_ok=False)
    dataset = folder / "synthetic.csv"; incoming = folder / "prediction-input.csv"
    table.to_csv(dataset, index=False); table[["z", "x"]].iloc[:10].to_csv(incoming, index=False)
    env = process_environment()
    env.update(ML_SERVICE_URL=ml.url, ML_WORKER_TOKEN=ml.settings.worker_token.get_secret_value())
    worker = subprocess.Popen([sys.executable, "-I", "-m", "materials_ml_service.worker"], env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        ready = {"url": backend.url, "dataset": str(dataset), "input": str(incoming), "completion": str(folder / "finish.json"),
            "model": "provider" if real_llm else "scripted-ui-fixture"}
        (folder / "ready.json").write_text(json.dumps(ready), encoding="utf8")
        print("P7_BROWSER_READY " + json.dumps(ready), flush=True)
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline and not (folder / "finish.json").exists():
            assert backend.process.poll() is None and worker.poll() is None
            time.sleep(1)
        assert (folder / "finish.json").exists(), "Browser acceptance did not finish"
        checks = json.loads((folder / "finish.json").read_text(encoding="utf8"))
        assert all(checks.get(name) for name in ("upload", "chat", "viewer_read_only", "append_once", "prediction_download", "dataset_download", "keyboard", "narrow", "recovery"))
        if real_llm:
            assert checks.get("real_language")
        with httpx.Client(base_url=backend.url, timeout=30, trust_env=False) as http:
            scopes = http.get("/api/v1/conversations").json()["data"]["items"]
            refs = [r for scope in scopes for r in http.get(f"/api/v1/conversations/{scope['conversation_id']}/ml/resources?limit=100").json()["data"]["items"]]
            assert {r["resource_type"] for r in refs} == {"dataset", "training_run", "model", "prediction"}
            assert any(http.get(f"/api/v1/conversations/{r['conversation_id']}/ml/resources/{r['reference_id']}/remote").json()["data"]["status"] == "SUCCEEDED"
                       for r in refs if r["resource_type"] == "prediction")
    finally:
        if worker.poll() is None:
            worker.kill(); worker.wait(timeout=10)
