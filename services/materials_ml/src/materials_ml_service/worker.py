"""HTTP-only worker. No database, MinIO, configuration-of-Service or Engine imports."""
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from uuid import uuid4

import httpx

from .windows import JobProcess, SingleWorker, recover_job, RunnerError


class Heartbeat:
    def __init__(self, worker, run, owner):
        self.worker, self.run, self.owner = worker, run, owner
        self.stop_event = threading.Event()
        self.cancelled, self.fenced = False, False
        self.last_ok = time.monotonic()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        while not self.stop_event.is_set():
            try:
                state = self.worker.command(self.run, "heartbeat", self.owner)
                self.cancelled = state["cancel_requested"] or state["status"] == "CANCELLED"
                self.last_ok = time.monotonic()
            except httpx.HTTPError as error:
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code == 409:
                    self.fenced = True
            if self.stop_event.wait(5):
                break

    @property
    def lost(self):
        return self.fenced or time.monotonic() - self.last_ok >= 20

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop_event.set()
        self.thread.join(timeout=10)


class Worker:
    def __init__(self, base_url, token, *, timeout=1800, runner=JobProcess):
        self.base_url = base_url.rstrip("/")
        parsed = httpx.URL(self.base_url)
        if parsed.host not in ("127.0.0.1", "localhost") or parsed.scheme != "http" or parsed.path not in ("", "/"):
            raise ValueError("Worker requires a local service URL")
        self.client = httpx.Client(base_url=self.base_url, headers={"Authorization": "Bearer " + token},
                                   timeout=5, trust_env=False)
        self.session = uuid4().hex
        self.timeout, self.runner = timeout, runner

    def command(self, run, action, body, *, timeout=5):
        response = self.client.post(f"/internal/v1/runs/{run['id']}/{action}", json=body, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def recover(self):
        response = self.client.get("/internal/v1/recovery")
        response.raise_for_status()
        for run in response.json()["runs"]:
            recover_job(run["job_name"])
            self.command(run, "recover", {"scope_id": run["scope_id"], "previous_claim": run["claim"], "new_session": self.session})

    def execute(self, run):
        owner = {"scope_id": run["scope_id"], "session": self.session, "claim": run["claim"]}
        reason, process = "TRAINING_FAILED", None
        with TemporaryDirectory(prefix="ml-training-") as folder, Heartbeat(self, run, owner) as heartbeat:
            try:
                response = self.client.post(f"/internal/v1/runs/{run['id']}/input", json=owner, timeout=15)
                response.raise_for_status()
                payload = response.content
                if sha256(payload).hexdigest() != run["frozen_spec"]["raw_sha256"]:
                    raise RunnerError("DATASET_MISMATCH")
                root = Path(folder)
                (root / "input.csv").write_bytes(payload)
                (root / "request.json").write_text(json.dumps(run["frozen_spec"]), encoding="utf-8")
                reason = "JOB_SETUP_FAILED"
                process = self.runner(run["job_name"], root)
                try:
                    state = self.command(run, "start", {**owner, "job_name": run["job_name"]})
                except httpx.HTTPError:
                    state = self.command(run, "status", owner)
                if state["status"] != "RUNNING" or state["cancel_requested"] or heartbeat.lost:
                    reason = "CANCELLED" if state["cancel_requested"] else "LEASE_LOST"
                    raise RunnerError("START_NOT_CONFIRMED")
                process.release()
                reason = "TRAINING_FAILED"
                deadline = time.monotonic() + self.timeout
                while process.poll() is None:
                    if heartbeat.cancelled or heartbeat.lost:
                        reason = "CANCELLED" if heartbeat.cancelled else "LEASE_LOST"
                        raise RunnerError(reason)
                    if time.monotonic() >= deadline:
                        reason = "TRAINING_TIMEOUT"
                        raise RunnerError(reason)
                    time.sleep(.1)
                if process.poll() != 0:
                    raise RunnerError("TRAINING_FAILED")
                for member in ("manifest.json", "pipeline.joblib", "evaluation.json", "splits.json"):
                    if heartbeat.cancelled or heartbeat.lost:
                        reason = "CANCELLED" if heartbeat.cancelled else "LEASE_LOST"
                        raise RunnerError(reason)
                    response = self.client.put(f"/internal/v1/runs/{run['id']}/files/{member}",
                        headers={"X-ML-Claim": json.dumps(owner)}, content=(root / "model" / member).read_bytes(), timeout=60)
                    response.raise_for_status()
                try:
                    state = self.command(run, "complete", owner, timeout=60)
                except httpx.HTTPError:
                    state = self.command(run, "status", owner)
                    if state["status"] != "SUCCEEDED":
                        state = self.command(run, "complete", owner, timeout=60)
                if state["status"] == "SUCCEEDED":
                    return
            except (RunnerError, httpx.HTTPError, OSError, ValueError):
                pass
            finally:
                if process is not None:
                    process.stop()  # If stop cannot be confirmed, propagate and leave recovery pending.
                    process.close()
        # A failed response must not create a second execution. Retry only this terminal receipt.
        while True:
            try:
                self.command(run, "stopped", {**owner, "reason": reason})
                return
            except httpx.HTTPError as error:
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500:
                    raise  # The child is stopped; leave a fenced receipt for explicit recovery.
                time.sleep(1)

    def run(self, *, once=False):
        with SingleWorker("local-training-worker-v1"):
            self.recover()
            key = uuid4().hex
            while True:
                try:
                    response = self.client.post("/internal/v1/claim", json={"session": self.session, "key": key})
                    response.raise_for_status()
                    run = response.json()["run"]
                except httpx.HTTPError as error:
                    if isinstance(error, httpx.HTTPStatusError) and error.response.status_code < 500:
                        raise
                    time.sleep(1)
                    continue  # Preserve the claim request key after an uncertain response.
                if run is not None:
                    self.execute(run)
                    key = uuid4().hex
                    if once:
                        return
                elif once:
                    return
                time.sleep(1)

    def close(self):
        self.client.close()


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    worker = None
    try:
        if any(key in os.environ for key in ("ML_DATABASE_URL", "ML_MINIO_ACCESS_KEY", "ML_MINIO_SECRET_KEY",
                "ML_RESOURCE_TOKEN", "ML_MCP_TOKEN", "ML_ADMIN_DATABASE_URL", "ML_ADMIN_MINIO_ACCESS_KEY", "ML_ADMIN_MINIO_SECRET_KEY")):
            raise ValueError("Worker must not inherit Service or administrator credentials")
        worker = Worker(os.environ.get("ML_SERVICE_URL", "http://127.0.0.1:8200"), os.environ["ML_WORKER_TOKEN"])
        worker.run(once=args.once)
    except (KeyError, ValueError, RunnerError, httpx.HTTPError):
        raise SystemExit("ML_WORKER_STOPPED") from None
    finally:
        if worker:
            worker.close()


if __name__ == "__main__":
    main()
