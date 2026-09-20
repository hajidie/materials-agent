"""Prediction application lifecycle; request-owned computation, never a job queue."""
from hashlib import sha256
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time

from materials_ml import read_csv, EngineError
from materials_ml.tabular import numeric_frame
from .domain import DatasetAsset, ModelAsset, MLArtifact, Prediction, ServiceError, MEMBERS, TERMINAL, canonical, digest, new_id, text_id
from .windows import JobProcess, SingleWorker, recover_job, RunnerError

MAX_PREDICTION_BYTES = 2 * 1024**2
MAX_RESULT_BYTES = 256 * 1024


class CallCancellation:
    """One originating call owns cancellation; an idempotent replay owns nothing."""
    def __init__(self):
        self.event = threading.Event()
        self.owner = None


class PredictionDeadline:
    """Request-local process supervision keeps working while its caller waits on IO."""
    def __init__(self, process, call, deadline):
        self.done, self.expired = threading.Event(), threading.Event()
        def watch():
            while not self.done.wait(.01):
                timed_out = time.monotonic() >= deadline
                if timed_out or call.event.is_set():
                    if timed_out:
                        self.expired.set()
                    try:
                        process.stop()
                    except RunnerError:
                        pass  # Main execution must independently confirm stop before any terminal transition.
                    return
        self.thread = threading.Thread(target=watch, name="ml-prediction-deadline", daemon=True)
        self.thread.start()

    def close(self):
        self.done.set()
        self.thread.join(timeout=6)
        if self.thread.is_alive():
            raise ServiceError("PREDICTION_STOP_UNCONFIRMED", 503)


def validate_result(payload, prediction):
    try:
        if len(payload) > MAX_RESULT_BYTES:
            raise ValueError()
        value = json.loads(payload)
        spec = prediction.data["spec"]
        fixed = {"format": "predictions-v1", "model_id": prediction.model_id,
                 "model_manifest_sha256": spec["model_manifest_sha256"],
                 "input_dataset_id": prediction.input_dataset_id, "input_identity": spec["input_identity"],
                 "target": spec["target"], "target_unit": spec["target_unit"]}
        if set(value) != {*fixed, "values", "row_positions"} or any(value[k] != v for k, v in fixed.items()):
            raise ValueError()
        count = spec["input_identity"]["row_count"]
        if (value["row_positions"] != list(range(count)) or any(type(v) is not int for v in value["row_positions"])
                or len(value["values"]) != count
                or any(type(v) not in (int, float) or not math.isfinite(v) for v in value["values"])):
            raise ValueError()
        return value
    except (ValueError, KeyError, TypeError, OverflowError):
        raise ServiceError("INVALID_PREDICTION_RESULT") from None


class PredictionApplication:
    def initialize_predictions(self):
        self.prediction_capacity = threading.Lock()
        self.prediction_guard = threading.RLock()
        self.prediction_calls = {}
        self.prediction_instance = None
        self.prediction_runner = JobProcess
        self.prediction_timeout = 30

    def start_predictions(self):
        identity = sha256(self.settings.minio_bucket.encode()).hexdigest()
        self.prediction_instance = SingleWorker(identity, service=True)
        try:
            self.recover_predictions()
        except BaseException:
            self.prediction_instance.__exit__()
            self.prediction_instance = None
            raise

    def close_predictions(self):
        with self.prediction_guard:
            calls = list(self.prediction_calls.values())
            for call in calls:
                call.event.set()
        # Shutdown never detaches request threads from their supervised children.
        with self.prediction_capacity:
            self.recover_predictions()
        if self.prediction_instance:
            self.prediction_instance.__exit__()
            self.prediction_instance = None

    def prediction_spec(self, scope, model_id, dataset_id):
        model = self.get(ModelAsset, scope, model_id)
        dataset = self.get(DatasetAsset, scope, dataset_id)
        features = model.data["manifest"]["spec"]["features"]
        target = model.data["manifest"]["spec"]["target"]
        model_units = {c: model.data["units"].get(c) for c in features}
        input_units = dict(dataset.data["units"])
        for feature, unit in model_units.items():
            if unit is not None and input_units.get(feature) != unit:
                raise ServiceError("UNIT_CONFLICT", 422)
        manifest = self.get(MLArtifact, scope, model.data["members"]["manifest.json"])
        spec = {"contract": "prediction-v1", "model_id": model_id,
                "model_manifest_sha256": manifest.object_ref["sha256"],
                "input_dataset_id": dataset_id, "input_identity": dataset.data["identity"],
                "raw_sha256": dataset.data["raw_sha256"], "schema_version": dataset.data["schema_version"],
                "parser_contract": dataset.data["parser_contract"], "features": features,
                "model_units": model_units, "input_units": input_units,
                "unit_verification": {c: "UNVERIFIED" if u is None else "MATCHED" for c, u in model_units.items()},
                "target": target, "target_unit": model.data["units"].get(target)}
        return model, dataset, spec

    def prediction_identity(self, scope, model_id, dataset_id):
        model, dataset, spec = self.prediction_spec(scope, model_id, dataset_id)
        fingerprint = digest({"digest_version": "prediction-request-v1", "operation": "prediction.submit",
                              "scope_id": scope, "spec": spec})
        return model, dataset, spec, fingerprint

    def predict_model(self, scope, key, model_id, dataset_id, cancellation=None, *, expected_digest=None):
        text_id(scope); text_id(key, 255)
        call = cancellation or CallCancellation()
        model, dataset, spec, fingerprint = self.prediction_identity(scope, model_id, dataset_id)
        if expected_digest is not None and fingerprint != expected_digest:
            raise ServiceError("REQUEST_DIGEST_MISMATCH")
        with self.repo.transaction() as tx:
            previous = tx.operation("resource", scope, "prediction.submit", key, fingerprint)
            if previous:
                return self._required(tx, Prediction, previous, scope)
        if model.status != "AVAILABLE" or dataset.status != "AVAILABLE":
            raise ServiceError("PREDICTION_RESOURCE_UNAVAILABLE")
        source = self.get(MLArtifact, scope, dataset.artifact_id)
        count = spec["input_identity"]["row_count"]
        if source.object_ref["size_bytes"] > MAX_PREDICTION_BYTES or count > 1000 or count * len(spec["input_units"]) > 100_000:
            raise ServiceError("PREDICTION_SIZE_LIMIT", 413)
        payload = self.dataset_content(scope, dataset_id)
        table = read_csv(payload)
        if set(table.columns) != set(spec["features"]):
            raise ServiceError("PREDICTION_COLUMNS_MISMATCH", 422)
        numeric_frame(table, spec["features"])
        if not self.prediction_capacity.acquire(blocking=False):
            # The first request may have committed while preflight was in progress.
            with self.repo.transaction() as tx:
                previous = tx.operation("resource", scope, "prediction.submit", key, fingerprint)
                if previous:
                    return self._required(tx, Prediction, previous, scope)
            raise ServiceError("PREDICTION_BUSY", 409)
        prediction = None
        try:
            with self.repo.transaction() as tx:
                previous = tx.operation("resource", scope, "prediction.submit", key, fingerprint)
                if previous:
                    return self._required(tx, Prediction, previous, scope)
                if tx.find(Prediction, process_stopped=False):
                    raise ServiceError("PREDICTION_RECOVERY_REQUIRED", 503)
                if self._required(tx, DatasetAsset, dataset_id, scope).status != "AVAILABLE":
                    raise ServiceError("DATASET_NOT_AVAILABLE")
                identity = new_id()
                prediction = Prediction(id=identity, scope_id=scope, model_id=model_id, input_dataset_id=dataset_id,
                    data={"spec": spec, "request_digest": fingerprint,
                          "job_name": "Local\\MaterialsMLPrediction-" + identity + "-" + new_id(),
                          "phase": "CREATED", "warnings": ["UNIT_UNVERIFIED"] if None in spec["model_units"].values() else []})
                tx.add(prediction)
                tx.record("resource", scope, "prediction.submit", key, fingerprint, identity)
                call.owner = (scope, prediction.id)  # Also identifies a commit whose receipt is lost.
            with self.prediction_guard:
                self.prediction_calls[prediction.id] = call
            return self._execute_prediction(prediction, model, payload, call)
        finally:
            if prediction:
                with self.prediction_guard:
                    self.prediction_calls.pop(prediction.id, None)
            self.prediction_capacity.release()

    def _prediction_cancelled(self, prediction, call):
        return call.event.is_set() or self.get(Prediction, prediction.scope_id, prediction.id).cancel_requested

    def _start_prediction(self, prediction, process, deadline):
        with self.repo.transaction() as tx:
            current = self._required(tx, Prediction, prediction.id, prediction.scope_id)
            if time.monotonic() >= deadline:
                raise ServiceError("PREDICTION_TIMEOUT")
            if process.poll() is not None:
                raise ServiceError("PREDICTION_FAILED")
            if current.status != "PENDING" or current.cancel_requested:
                raise ServiceError("PREDICTION_CANCELLED")
            current.status = "RUNNING"
            current.data = {**current.data, "phase": "COMPUTING"}
            current.touch(); tx.save(current)

    def _execute_prediction(self, prediction, model, payload, call):
        process = None
        supervisor = None
        stopped = False
        publishing = False
        try:
            with TemporaryDirectory(prefix="ml-predict-") as folder:
                root = Path(folder)
                (root / "model").mkdir()
                for member in MEMBERS:
                    if self._prediction_cancelled(prediction, call):
                        raise ServiceError("PREDICTION_CANCELLED")
                    (root / "model" / member).write_bytes(self.model_content(prediction.scope_id, model.id, member))
                (root / "input.csv").write_bytes(payload)
                (root / "request.json").write_bytes(canonical(prediction.data["spec"]))
                if self._prediction_cancelled(prediction, call):
                    raise ServiceError("PREDICTION_CANCELLED")
                created = time.monotonic()
                process = self.prediction_runner(prediction.data["job_name"], root, prediction=True)
                supervisor = PredictionDeadline(process, call, created + self.prediction_timeout)
                try:
                    self._start_prediction(prediction, process, created + self.prediction_timeout)
                except Exception:
                    current = self.get(Prediction, prediction.scope_id, prediction.id)
                    if current.status != "RUNNING" or current.cancel_requested:
                        raise
                if self._prediction_cancelled(prediction, call):
                    raise ServiceError("PREDICTION_CANCELLED")
                process.release()
                while process.poll() is None:
                    if self._prediction_cancelled(prediction, call):
                        raise ServiceError("PREDICTION_CANCELLED")
                    if time.monotonic() - created >= self.prediction_timeout:
                        raise ServiceError("PREDICTION_TIMEOUT")
                    call.event.wait(.05)
                code = process.poll()
                process.stop(); stopped = True  # Includes descendants, even after the main child exits.
                supervisor.close(); supervisor = None  # Publication IO has its own timeout, no live prediction tree.
                if time.monotonic() - created >= self.prediction_timeout:
                    raise ServiceError("PREDICTION_TIMEOUT")
                if code != 0:
                    raise ServiceError("PREDICTION_FAILED")
                result = (root / "predictions.json").read_bytes()
                validate_result(result, prediction)
                if self._prediction_cancelled(prediction, call):
                    raise ServiceError("PREDICTION_CANCELLED")
                artifact = self._prediction_publication_intent(prediction, result)
                publishing = True
                self.storage.put(artifact.object_ref, result)
                if call.event.is_set():
                    self._finish_prediction(prediction, "PREDICTION_CANCELLED", cancelled=True)
                else:
                    self.reconcile_prediction(artifact, result)
        except Exception as error:
            if isinstance(error, RunnerError) and str(error) in ("PROCESS_STOP_UNCONFIRMED", "JOB_STATE_UNKNOWN"):
                raise ServiceError("PREDICTION_STOP_UNCONFIRMED", 503) from None
            if process and not stopped:
                process.stop(); stopped = True  # Failure propagates: do not claim terminal if stop is unconfirmed.
            reason = error.code if isinstance(error, ServiceError) else "JOB_SETUP_FAILED" if process is None else "PREDICTION_FAILED"
            if supervisor and supervisor.expired.is_set():
                reason = "PREDICTION_TIMEOUT"
            cancelled = call.event.is_set() or reason == "PREDICTION_CANCELLED"
            if not publishing or cancelled:
                self._finish_prediction(prediction, reason, cancelled=cancelled)
            elif isinstance(error, (ServiceError, EngineError)) and getattr(error, "status", 409) != 503:
                self._finish_prediction(prediction, "INVALID_PREDICTION_RESULT")
            # Unknown upload/commit retains the original publication intent; maintenance reconciles it.
        finally:
            try:
                if supervisor:
                    supervisor.close()
            finally:
                if process:
                    process.close()
        return self.get(Prediction, prediction.scope_id, prediction.id)

    def _prediction_publication_intent(self, prediction, result):
        artifact = self._artifact(prediction.scope_id, sha256(result).hexdigest(), len(result), "application/json",
                                  prediction=prediction.id, member="predictions.json")
        try:
            with self.repo.transaction() as tx:
                current = self._required(tx, Prediction, prediction.id, prediction.scope_id)
                if current.cancel_requested or current.status != "RUNNING":
                    raise ServiceError("PREDICTION_CANCELLED")
                current.process_stopped = True
                current.data = {**current.data, "phase": "PUBLISHING"}
                current.touch(); tx.save(current); tx.add(artifact)
        except Exception:
            with self.repo.transaction() as tx:
                recorded = tx.get(MLArtifact, artifact.id, artifact.scope_id)
            if recorded is None:
                raise
            return recorded
        return artifact

    def reconcile_prediction(self, artifact, payload):
        current = self.get(Prediction, artifact.scope_id, artifact.prediction_id)
        validate_result(payload, current)
        with self.repo.transaction() as tx:
            current = self._required(tx, Prediction, current.id, current.scope_id)
            item = self._required(tx, MLArtifact, artifact.id, artifact.scope_id)
            if current.status == "SUCCEEDED":
                return
            if item.status != "PENDING":
                return
            if current.cancel_requested or current.status in TERMINAL:
                item.transition("DELETING"); tx.save(item)
                if current.status not in TERMINAL and current.process_stopped:
                    current.finish("CANCELLED", "PREDICTION_CANCELLED"); tx.save(current)
                return
            if current.status != "RUNNING" or not current.process_stopped or current.data["phase"] != "PUBLISHING":
                raise ServiceError("PREDICTION_NOT_PUBLISHABLE")
            item.transition("AVAILABLE"); tx.save(item)
            current.artifact_id = item.id
            current.finish("SUCCEEDED"); tx.save(current)

    def _finish_prediction(self, prediction, reason, *, cancelled=False):
        with self.repo.transaction() as tx:
            current = self._required(tx, Prediction, prediction.id, prediction.scope_id)
            if current.status not in TERMINAL:
                current.process_stopped = True
                current.cancel_requested = current.cancel_requested or cancelled
                current.finish("CANCELLED" if current.cancel_requested else "FAILED", reason)
                tx.save(current)
            if current.status != "SUCCEEDED":
                for artifact in tx.find(MLArtifact, prediction_id=current.id):
                    if artifact.status not in ("DELETING", "DELETED"):
                        artifact.transition("DELETING"); tx.save(artifact)
            return current

    def request_prediction_cancel(self, scope, identity):
        with self.repo.transaction() as tx:
            prediction = self._required(tx, Prediction, identity, scope)
            if prediction.status in TERMINAL:
                return prediction
            if not prediction.cancel_requested:
                prediction.cancel_requested = True; prediction.touch(); tx.save(prediction)
        return prediction

    def cancel_prediction(self, scope, identity):
        prediction = self.request_prediction_cancel(scope, identity)
        if prediction.status in TERMINAL:
            return prediction
        with self.prediction_guard:
            call = self.prediction_calls.get(identity)
            if call:
                call.event.set()
        if call:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                current = self.get(Prediction, scope, identity)
                if current.status in TERMINAL:
                    return current
                time.sleep(.05)
            raise ServiceError("PREDICTION_STOP_UNCONFIRMED", 503)
        recover_job(prediction.data["job_name"])
        return self._finish_prediction(prediction, "PREDICTION_CANCELLED", cancelled=True)

    def recover_predictions(self):
        after = None
        while True:
            with self.repo.transaction() as tx:
                batch = tx.list(Prediction, statuses=("PENDING", "RUNNING"), after=after, limit=50)
            if not batch:
                return
            for prediction in batch:
                recover_job(prediction.data["job_name"])
                if prediction.process_stopped and prediction.data["phase"] == "PUBLISHING" and not prediction.cancel_requested:
                    with self.repo.transaction() as tx:
                        artifacts = tx.find(MLArtifact, prediction_id=prediction.id)
                    if artifacts:
                        self.reconcile_artifact(artifacts[0].id)
                        continue
                self._finish_prediction(prediction, "SERVICE_INTERRUPTED")
            after = batch[-1].id

    def prediction_content(self, scope, identity):
        prediction = self.get(Prediction, scope, identity)
        if prediction.status != "SUCCEEDED":
            raise ServiceError("PREDICTION_NOT_AVAILABLE")
        artifact = self.get(MLArtifact, scope, prediction.artifact_id)
        if artifact.status != "AVAILABLE":
            raise ServiceError("ARTIFACT_NOT_AVAILABLE")
        payload = self.storage.get(artifact.object_ref)
        validate_result(payload, prediction)
        return payload
