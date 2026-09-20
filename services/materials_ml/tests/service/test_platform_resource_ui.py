"""Chat attachment contracts against real isolated services, worker and storage."""
import csv
from io import StringIO
import httpx
import numpy as np
import pytest

from materials_ml import load_package, predict
from test_platform_resource_context import context_platform
from test_platform_resources import resources_platform
from test_platform_mcp import platform, conversation, confirm, diagnostic_run
from test_end_to_end import worker_process


@pytest.mark.parametrize("algorithm", ["LR", "RF"])
def test_chat_attachment_view_and_immutable_completion(context_platform, csv_payload, table, tmp_path, algorithm):
    backend, ml = context_platform
    with httpx.Client(base_url=backend.url, timeout=90, trust_env=False) as http:
        scope = conversation(http, "chat-artifacts")
        root = f"/api/v1/conversations/{scope}"
        def upload(payload, name, key):
            response = http.post(root + "/attachments", files={"file": (name, payload, "text/csv")}, headers={"Idempotency-Key": key})
            assert response.status_code == 200, response.text
            return response.json()["data"]["attachment"]
        def send(text, attachment, key):
            response = http.post(root + "/messages", json={"mode": "NEW_RUN", "content_text": text, "attachments": [attachment]},
                headers={"Idempotency-Key": key})
            assert response.status_code == 200, response.text
            return diagnostic_run(http, response.json()["data"]["agent_run"])
        def state():
            return http.get(root + "/ml/ui-state").json()["data"]
        def observe():
            response = http.post(root + "/results/reconcile", json={})
            assert response.status_code == 200, response.text
            return response.json()["data"]
        dataset = upload(csv_payload, "synthetic.csv", "upload")
        waiting = send(f"请用刚才上传的数据训练 {algorithm}，特征 x、z，目标 strength_MPa。", dataset, "train")
        finished = confirm(http, waiting)
        run_url = f"/api/v1/agent-runs/{finished['agent_run_id']}"
        original = http.get(run_url).json()["data"]
        viewer = root + f"/messages/{finished['source_message_id']}/artifacts/{dataset['attachment_id']}"
        before = state()
        overview = http.get(viewer).json()["data"]
        assert overview["presentation"]["facts"]["row_count"] == len(table)
        assert http.get(overview["downloads"][0]["url"]).content == csv_payload
        assert state() == before
        assert http.get(root + "/result-messages").json()["data"]["items"] == []
        assert observe()["pending"]
        worker = worker_process(ml)
        try:
            assert worker.wait(timeout=60) == 0
        finally:
            if worker.poll() is None:
                worker.kill(); worker.wait(timeout=10)
        training = original["result_attachments"][0]
        answer_message = original["final_answer"]["answer_id"]
        before = state()
        assert http.get(root + f"/messages/{answer_message}/artifacts/{training['attachment_id']}").status_code == 200
        assert state() == before  # Viewing never discovers the model or selects inputs.
        assert http.get(root + "/result-messages").json()["data"]["items"] == []
        completed = observe()
        assert not completed["pending"] and len(completed["messages"]) == 1
        terminal = completed["messages"][0]
        assert terminal["text"].startswith("模型训练完成") and terminal["presentation"]["metrics"]
        assert observe()["messages"] == completed["messages"]
        assert http.get(run_url).json()["data"] == original
        assert state()["selections"] == before["selections"]
        model = next(a for a in terminal["artifacts"] if a["kind"] == "model")
        incoming = table[["z", "x"]].iloc[:10]
        attachment = upload(incoming.to_csv(index=False).encode(), "input.csv", "input")
        prediction = confirm(http, send("用刚才训练好的模型对第二个数据集执行预测。", attachment, "predict"))
        public = http.get(f"/api/v1/agent-runs/{prediction['agent_run_id']}").json()["data"]
        output = next(a for a in public["result_attachments"] if a["kind"] == "prediction")
        viewer = root + f"/messages/{public['final_answer']['answer_id']}/artifacts/{output['attachment_id']}"
        details = http.get(viewer).json()["data"]
        response = http.get(details["downloads"][0]["url"])
        assert response.status_code == 200 and "text/csv" in response.headers["content-type"]
        values = [float(row["预测值"]) for row in csv.DictReader(StringIO(response.content.decode("utf-8-sig")))]
        for member in ("manifest.json", "pipeline.joblib", "evaluation.json", "splits.json"):
            content = http.get(root + f"/ml/resources/{model['attachment_id']}/files/{member}")
            assert content.status_code == 200
            (tmp_path / member).write_bytes(content.content)
        np.testing.assert_allclose(values, predict(load_package(tmp_path, trusted=True), incoming).values, rtol=1e-10, atol=1e-12)
        assert len(observe()["messages"]) == 1  # Synchronous prediction already has its final answer.
        ml.stop()
        assert http.get(viewer).status_code == 503
        assert http.get(root + "/result-messages").json()["data"]["items"] == completed["messages"]
