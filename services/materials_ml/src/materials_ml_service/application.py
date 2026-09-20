"""The only writer of ML lifecycles. All external IO occurs outside transactions."""
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from materials_storage import ObjectStorageRef
from materials_ml import (EngineError, TrainingSpec, read_csv, analyze_table, table_identity,
                          validate_training_input, load_package, evaluate_test)
from materials_ml.regression import build_pipeline, parameter_json, preprocessing_parameters, environment_versions
from .domain import (DatasetAsset, MLArtifact, TrainingRun, ModelAsset, Prediction, ServiceError, MEMBERS,
                     MAX_DATASET_BYTES, TERMINAL, digest, new_id, now, text_id)
from .prediction_application import PredictionApplication

LEASE_SECONDS = 30


def inspect_dataset(payload):
    if not payload or len(payload) > MAX_DATASET_BYTES:
        raise ServiceError("DATASET_SIZE_LIMIT", 413)
    table = read_csv(payload)
    if len(table) > 100_000 or len(table.columns) > 256 or table.size > 2_000_000:
        raise ServiceError("DATASET_SHAPE_LIMIT", 413)
    return table, analyze_table(table)


def normalized_units(table, units):
    if not isinstance(units, dict) or set(units) - set(table.columns):
        raise ServiceError("INVALID_UNITS", 422)
    for value in units.values():
        if value is not None and (not isinstance(value, str) or not value.strip() or len(value) > 128):
            raise ServiceError("INVALID_UNITS", 422)
    return {column: units.get(column) for column in table.columns}


from .resource_application import ResourceApplication


class MLService(PredictionApplication, ResourceApplication):
    def __init__(self, repository, storage, settings):
        self.repo, self.storage, self.settings = repository, storage, settings
        self.initialize_predictions()

    def _required(self, tx, cls, identity, scope=None):
        resource = tx.get(cls, identity, scope)
        if resource is None:
            raise ServiceError("RESOURCE_NOT_FOUND", 404)
        return resource

    def get(self, cls, scope, identity):
        text_id(scope)
        with self.repo.transaction() as tx:
            tx.require_open(scope)
            return self._required(tx, cls, identity, scope)

    def list(self, cls, scope, limit=20, after=None):
        text_id(scope)
        with self.repo.transaction() as tx:
            tx.require_open(scope)
            return tx.list(cls, scope=scope, limit=limit, after=after)

    def _artifact(self, scope, payload_sha, size, media, *, dataset=None, run=None, prediction=None, member="dataset.csv"):
        artifact_id = new_id()
        ref = ObjectStorageRef(artifact_id, self.settings.store_id, self.settings.minio_bucket,
            f"ml/v1/{sha256(scope.encode()).hexdigest()}/{artifact_id}", payload_sha, size, media)
        return MLArtifact(id=artifact_id, scope_id=scope, dataset_id=dataset, run_id=run,
                          prediction_id=prediction, member=member, object_ref=ref.to_dict())

    def dataset_upload_identity(self, scope, payload, units, display_name=None):
        text_id(scope)
        table, analysis = inspect_dataset(payload)
        units = normalized_units(table, units)
        if display_name is not None and (not isinstance(display_name, str) or len(display_name) > 200):
            raise ServiceError("INVALID_DISPLAY_NAME", 422)
        name = (display_name.strip() or None) if display_name is not None else None
        raw_sha = sha256(payload).hexdigest()
        request = {"digest_version": "dataset-upload-v1", "operation": "dataset.upload", "scope_id": scope,
                   "raw_file": {"sha256": raw_sha, "size_bytes": len(payload), "media_type": "text/csv"},
                   "parser_contract": "csv-utf8-v1", "display_name": name, "units": units}
        return analysis, units, name, raw_sha, digest(request)

    def prepare_dataset_upload(self, scope, payload, units, display_name=None):
        with self.repo.transaction() as tx:
            tx.require_open(scope)
        *_, fingerprint = self.dataset_upload_identity(scope, payload, units, display_name)
        return {"scope_id": scope, "operation": "dataset.upload", "digest_version": "dataset-upload-v1",
                "request_digest": fingerprint}

    def upload_dataset(self, scope, key, payload, units, display_name=None, *, expected_digest=None):
        text_id(scope); text_id(key, 255)
        analysis, units, name, raw_sha, request_digest = self.dataset_upload_identity(scope, payload, units, display_name)
        if expected_digest is not None and expected_digest != request_digest:
            raise ServiceError("REQUEST_DIGEST_MISMATCH")
        with self.repo.transaction() as tx:
            tx.require_open(scope)
            existing = tx.operation("resource", scope, "dataset.upload", key, request_digest)
            if existing:
                dataset = self._required(tx, DatasetAsset, existing, scope)
                artifact = self._required(tx, MLArtifact, dataset.artifact_id, scope)
            else:
                identity = new_id()
                artifact = self._artifact(scope, raw_sha, len(payload), "text/csv", dataset=identity)
                dataset = DatasetAsset(id=identity, scope_id=scope, artifact_id=artifact.id,
                    data={"identity": analysis["dataset"], "raw_sha256": raw_sha, "parser_contract": "csv-utf8-v1",
                          "schema_version": "tabular-schema-v1", "analysis": analysis, "units": units,
                          "display_name": name, "request_digest": request_digest})
                tx.add(dataset); tx.add(artifact)
                tx.record("resource", scope, "dataset.upload", key, request_digest, identity)
        if dataset.status != "PENDING":
            return dataset
        self.storage.put(artifact.object_ref, payload)
        self.reconcile_artifact(artifact.id)
        return self.get(DatasetAsset, scope, dataset.id)

    def dataset_content(self, scope, identity):
        dataset = self.get(DatasetAsset, scope, identity)
        if dataset.status != "AVAILABLE":
            raise ServiceError("DATASET_NOT_AVAILABLE")
        artifact = self.get(MLArtifact, scope, dataset.artifact_id)
        if artifact.status != "AVAILABLE":
            raise ServiceError("ARTIFACT_NOT_AVAILABLE")
        payload = self.storage.get(artifact.object_ref)
        if table_identity(read_csv(payload)).to_dict() != dataset.data["identity"]:
            raise ServiceError("DATASET_IDENTITY_MISMATCH")
        return payload

    def delete_dataset(self, scope, identity):
        with self.repo.transaction() as tx:
            dataset = self._required(tx, DatasetAsset, identity, scope)
            if tx.find(TrainingRun, dataset_id=identity) or tx.find(Prediction, input_dataset_id=identity):
                raise ServiceError("DATASET_HAS_DEPENDENCIES")
            if dataset.status in ("DELETING", "DELETED"):
                return dataset
            dataset.transition("DELETING"); tx.save(dataset)
            artifact = self._required(tx, MLArtifact, dataset.artifact_id, scope)
            if artifact.status not in ("DELETING", "DELETED"):
                artifact.transition("DELETING"); tx.save(artifact)
        return dataset

    def training_identity(self, scope, dataset_id, values):
        """Shared, non-mutating identity construction; never reads CSV or fits a model."""
        text_id(scope)
        dataset = self.get(DatasetAsset, scope, dataset_id)
        # Compute replay identity from immutable metadata even if data has since become unavailable.
        supplied_units = values.get("units", {})
        if not isinstance(supplied_units, dict):
            raise ServiceError("INVALID_UNITS", 422)
        spec = TrainingSpec(**{**values, "units": {}})
        selected = (*spec.features, spec.target)
        if not set(selected) <= set(dataset.data["units"]):
            raise ServiceError("MISSING_COLUMNS", 422)
        units = {c: dataset.data["units"][c] for c in selected}
        if any(c not in units or value != units[c] for c, value in supplied_units.items()):
            raise ServiceError("UNIT_CONFLICT", 422)
        spec = TrainingSpec(**{**spec.to_dict(), "units": {k: v for k, v in units.items() if v is not None}})
        pipeline = build_pipeline(spec)
        frozen = {"engine_spec": spec.to_dict(), "units": units, "dataset_id": dataset_id,
                  "dataset_identity": dataset.data["identity"], "raw_sha256": dataset.data["raw_sha256"],
                  "parser_contract": dataset.data["parser_contract"], "schema_version": dataset.data["schema_version"],
                  "algorithm_contract": "lr-rf-v1", "cv": {"folds": 5, "shuffle": True},
                  "estimator_parameters": parameter_json(pipeline.named_steps["estimator"].get_params(deep=True)),
                  "preprocessing": preprocessing_parameters(pipeline), "environment": environment_versions()}
        request_digest = digest({"digest_version": "training-submit-v1", "operation": "training.submit",
                                 "scope_id": scope, "spec": frozen})
        return dataset, spec, frozen, request_digest

    def prepare_operation(self, scope, operation, arguments):
        from .schemas import TrainingRequest, PredictionRequest
        if operation == "training.submit":
            body = TrainingRequest.model_validate(arguments)
            _, _, _, fingerprint = self.training_identity(scope, body.dataset_id,
                body.model_dump(exclude={"dataset_id"}))
            version = "training-submit-v1"
        elif operation == "prediction.submit":
            body = PredictionRequest.model_validate(arguments)
            _, _, _, fingerprint = self.prediction_identity(scope, body.model_id, body.input_dataset_id)
            version = "prediction-request-v1"
        else:
            raise ServiceError("INVALID_OPERATION", 422)
        return {"scope_id": scope, "operation": operation, "digest_version": version, "request_digest": fingerprint}

    def lookup_operation(self, scope, operation, key, fingerprint, version):
        # Historical equality uses only persisted digests, never the current normalizer/environment.
        text_id(scope); text_id(key, 255)
        contracts = {"dataset.upload": (DatasetAsset, "dataset-upload-v1"), "training.submit": (TrainingRun, "training-submit-v1"),
                     "prediction.submit": (Prediction, "prediction-request-v1")}
        if operation not in contracts or contracts[operation][1] != version:
            raise ServiceError("INVALID_OPERATION_CONTRACT", 422)
        from .schemas import resource_view
        with self.repo.transaction() as tx:
            identity = tx.operation("resource", scope, operation, key, fingerprint)
            resource = self._required(tx, contracts[operation][0], identity, scope) if identity else None
            if resource and resource.data["request_digest"] != fingerprint:
                raise ServiceError("RECEIPT_INCONSISTENT", 503)
        return {"scope_id": scope, "operation": operation, "digest_version": version,
                "request_digest": fingerprint, "lookup_status": "FOUND" if resource else "NOT_FOUND",
                "resource": resource_view(resource) if resource else None}

    def submit_training(self, scope, key, dataset_id, values, *, expected_digest=None):
        text_id(scope); text_id(key, 255)
        dataset, spec, frozen, request_digest = self.training_identity(scope, dataset_id, values)
        if expected_digest is not None and expected_digest != request_digest:
            raise ServiceError("REQUEST_DIGEST_MISMATCH")
        with self.repo.transaction() as tx:
            previous = tx.operation("resource", scope, "training.submit", key, request_digest)
            if previous:
                return self._required(tx, TrainingRun, previous, scope)
        table = read_csv(self.dataset_content(scope, dataset_id))
        validate_training_input(table, spec)
        with self.repo.transaction() as tx:
            previous = tx.operation("resource", scope, "training.submit", key, request_digest)
            if previous:
                return self._required(tx, TrainingRun, previous, scope)
            current = self._required(tx, DatasetAsset, dataset_id, scope)
            if current.status != "AVAILABLE":
                raise ServiceError("DATASET_NOT_AVAILABLE")
            run = TrainingRun(id=new_id(), scope_id=scope, dataset_id=dataset_id,
                data={"spec": frozen, "request_digest": request_digest,
                      "warnings": ["UNIT_UNKNOWN"] if None in frozen["units"].values() else []})
            tx.add(run)
            tx.record("resource", scope, "training.submit", key, request_digest, run.id)
        return run

    def cancel(self, scope, identity):
        with self.repo.transaction() as tx:
            run = self._required(tx, TrainingRun, identity, scope)
            if run.status == "CANCELLED":
                return run
            if run.status in TERMINAL:
                raise ServiceError("RUN_ALREADY_TERMINAL")
            run.cancel_requested = True
            if run.status == "PENDING":
                stopped = run.claim_id is None
                run.finish("CANCELLED")
                run.process_stopped = stopped  # A claimed pre-start process must still be stopped.
            else:
                run.touch()
            tx.save(run)
        return run

    def claim(self, session, key):
        text_id(session); text_id(key)
        request_digest = digest({"session": session, "key": key, "contract": "claim-v1"})
        with self.repo.transaction() as tx:
            old = tx.operation("worker", "_worker", "claim", key, request_digest)
            if old:
                return self._required(tx, TrainingRun, old)
            if tx.active_claims():
                return None
            candidates = tx.list(TrainingRun, statuses=("PENDING",), limit=1)
            if not candidates:
                return None
            run = candidates[0]
            run.worker_session, run.claim_id = session, new_id()
            run.heartbeat_at, run.attempt = now(), 1
            run.touch(); tx.save(run)
            tx.record("worker", "_worker", "claim", key, request_digest, run.id)
            return run

    def _owned(self, tx, identity, scope, session, claim, *, live=True):
        tx.require_open(scope)
        run = self._required(tx, TrainingRun, identity, scope)
        if run.worker_session != session or run.claim_id != claim:
            raise ServiceError("STALE_CLAIM")
        if live and (run.recovery_required or run.heartbeat_at is None
                     or now() - run.heartbeat_at > timedelta(seconds=LEASE_SECONDS)):
            raise ServiceError("STALE_CLAIM")
        return run

    def owned(self, identity, scope, session, claim):
        with self.repo.transaction() as tx:
            return self._owned(tx, identity, scope, session, claim, live=False)

    def start(self, identity, scope, session, claim, job_name):
        with self.repo.transaction() as tx:
            run = self._owned(tx, identity, scope, session, claim)
            if job_name != self.job_name(run):
                raise ServiceError("INVALID_JOB_IDENTITY")
            if run.status == "RUNNING":
                return run
            if run.status != "PENDING" or run.cancel_requested:
                raise ServiceError("RUN_NOT_STARTABLE")
            run.status = "RUNNING"
            run.data = {**run.data, "job_name": job_name}
            run.touch(); tx.save(run)
            return run

    @staticmethod
    def job_name(run):
        return "Local\\MaterialsML-" + run.id + "-" + run.claim_id

    def heartbeat(self, identity, scope, session, claim):
        with self.repo.transaction() as tx:
            run = self._owned(tx, identity, scope, session, claim)
            if not run.process_stopped:
                run.heartbeat_at = now(); run.touch(); tx.save(run)
            return run

    def stopped(self, identity, scope, session, claim, reason):
        allowed = {"JOB_SETUP_FAILED", "TRAINING_FAILED", "TRAINING_TIMEOUT", "WORKER_INTERRUPTED",
                   "CANCELLED", "LEASE_LOST", "INVALID_PACKAGE"}
        if reason not in allowed:
            raise ServiceError("INVALID_FAILURE_CODE", 422)
        with self.repo.transaction() as tx:
            run = self._owned(tx, identity, scope, session, claim, live=False)
            if run.status not in TERMINAL:
                run.finish("CANCELLED" if run.cancel_requested else "FAILED", reason)
                tx.save(run)
            elif not run.process_stopped:
                run.process_stopped = True; run.touch(); tx.save(run)
            self._discard_artifacts(tx, run)
            return run

    def recoverable(self):
        with self.repo.transaction() as tx:
            return tx.active_claims()

    def recover(self, identity, scope, previous_claim, new_session):
        text_id(new_session)
        # Called only after the new sole local worker confirms the old named job is empty.
        with self.repo.transaction() as tx:
            run = self._required(tx, TrainingRun, identity, scope)
            if run.claim_id != previous_claim:
                raise ServiceError("STALE_CLAIM")
            if not run.process_stopped:
                if run.status not in TERMINAL:
                    run.finish("CANCELLED" if run.cancel_requested else "FAILED", "WORKER_INTERRUPTED")
                else:
                    run.process_stopped = True; run.touch()
                tx.save(run)
                self._discard_artifacts(tx, run)
            return run

    def _discard_artifacts(self, tx, run):
        if run.status == "SUCCEEDED":
            return
        for artifact in tx.find(MLArtifact, run_id=run.id):
            if artifact.status not in ("DELETING", "DELETED"):
                artifact.transition("DELETING"); tx.save(artifact)

    def upload_member(self, identity, scope, session, claim, member, payload):
        if member not in MEMBERS or len(payload) > MEMBERS[member][0]:
            raise ServiceError("INVALID_PACKAGE_MEMBER", 413)
        with self.repo.transaction() as tx:
            run = self._owned(tx, identity, scope, session, claim)
            if run.status != "RUNNING" or run.cancel_requested:
                raise ServiceError("RUN_NOT_PUBLISHABLE")
            matches = tx.find(MLArtifact, run_id=identity, member=member)
            if matches:
                artifact = matches[0]
                if artifact.object_ref["sha256"] != sha256(payload).hexdigest():
                    raise ServiceError("ARTIFACT_CONFLICT")
                if artifact.status not in ("PENDING", "AVAILABLE"):
                    raise ServiceError("ARTIFACT_NOT_AVAILABLE")
            else:
                artifact = self._artifact(scope, sha256(payload).hexdigest(), len(payload),
                    MEMBERS[member][1], run=identity, member=member)
                artifact.data = {"claim_id": claim}
                tx.add(artifact)
        self.storage.put(artifact.object_ref, payload)
        self.reconcile_artifact(artifact.id)
        return self.get(MLArtifact, scope, artifact.id)

    def complete(self, identity, scope, session, claim):
        with self.repo.transaction() as tx:
            run = self._owned(tx, identity, scope, session, claim, live=False)
            if run.status == "SUCCEEDED":
                return run
            self._owned(tx, identity, scope, session, claim)
            if run.status != "RUNNING" or run.cancel_requested:
                raise ServiceError("RUN_NOT_PUBLISHABLE")
            artifacts = tx.find(MLArtifact, run_id=identity)
        if {a.member for a in artifacts} != set(MEMBERS) or any(a.status != "AVAILABLE" for a in artifacts):
            raise ServiceError("PACKAGE_INCOMPLETE")
        table = read_csv(self.dataset_content(scope, run.dataset_id))
        with TemporaryDirectory(prefix="ml-verify-") as folder:
            for artifact in artifacts:
                (Path(folder) / artifact.member).write_bytes(self.storage.get(artifact.object_ref))
            package = load_package(folder, trusted=True)
            frozen = run.data["spec"]
            if (package.manifest["spec"] != frozen["engine_spec"]
                    or package.manifest["dataset"] != frozen["dataset_identity"]
                    or package.manifest["environment"] != frozen["environment"]
                    or package.manifest["estimator_parameters"] != frozen["estimator_parameters"]
                    or package.manifest["preprocessing"] != frozen["preprocessing"]
                    or evaluate_test(package, table) != package.report["test"]):
                raise ServiceError("PACKAGE_CONTRACT_MISMATCH")
            for record in [package.report["train"], package.report["test"], *package.report["cv"]["folds"]]:
                values = table.iloc[record["row_positions"]][frozen["engine_spec"]["target"]].astype(float).tolist()
                if record["y_true"] != values:
                    raise ServiceError("PACKAGE_TARGET_MISMATCH")
            model_data = {"manifest": package.manifest, "units": frozen["units"],
                          "warnings": list(dict.fromkeys([*run.data["warnings"], *package.report["warnings"]])),
                          "metrics": package.report["test"]["metrics"],
                          "members": {a.member: a.id for a in artifacts}}
        with self.repo.transaction() as tx:
            current = self._owned(tx, identity, scope, session, claim, live=False)
            if current.status == "SUCCEEDED":
                return current
            self._owned(tx, identity, scope, session, claim)
            if current.status != "RUNNING" or current.cancel_requested:
                raise ServiceError("RUN_NOT_PUBLISHABLE")
            for artifact in artifacts:
                checked = self._required(tx, MLArtifact, artifact.id, scope)
                if checked.status != "AVAILABLE" or checked.object_ref != artifact.object_ref:
                    raise ServiceError("ARTIFACT_CONFLICT")
            model = ModelAsset(id=new_id(), scope_id=scope, status="AVAILABLE", run_id=identity, data=model_data)
            tx.add(model)
            current.model_id = model.id
            current.finish("SUCCEEDED"); tx.save(current)
            return current

    def model_content(self, scope, identity, member):
        if member not in MEMBERS:
            raise ServiceError("RESOURCE_NOT_FOUND", 404)
        model = self.get(ModelAsset, scope, identity)
        artifact = self.get(MLArtifact, scope, model.data["members"][member])
        if artifact.status != "AVAILABLE":
            raise ServiceError("ARTIFACT_NOT_AVAILABLE")
        return self.storage.get(artifact.object_ref)

    def reconcile_artifact(self, identity):
        with self.repo.transaction() as tx:
            artifact = self._required(tx, MLArtifact, identity)
        if artifact.status == "AVAILABLE":
            return
        if artifact.status in ("FAILED", "DELETING", "DELETED"):
            if artifact.status == "FAILED":
                with self.repo.transaction() as tx:
                    artifact = self._required(tx, MLArtifact, identity)
                    artifact.transition("DELETING"); tx.save(artifact)
            self.storage.delete(artifact.object_ref)
            with self.repo.transaction() as tx:
                current = self._required(tx, MLArtifact, identity)
                current.data = {k: v for k, v in current.data.items() if k != "maintenance_error"}
                if current.status == "DELETING":
                    current.transition("DELETED"); tx.save(current)
                else:
                    current.touch(); tx.save(current)
                if current.dataset_id:
                    dataset = self._required(tx, DatasetAsset, current.dataset_id)
                    if dataset.status == "DELETING":
                        dataset.transition("DELETED"); tx.save(dataset)
            return
        if not self.storage.exists(artifact.object_ref):
            return  # Unknown upload; retain the operation identity for replay or later reconciliation.
        payload = self.storage.get(artifact.object_ref)
        if artifact.prediction_id:
            self.reconcile_prediction(artifact, payload)
            return
        if artifact.dataset_id:
            _, analysis = inspect_dataset(payload)
        with self.repo.transaction() as tx:
            current = self._required(tx, MLArtifact, identity)
            if current.status != "PENDING":
                return
            if current.dataset_id:
                dataset = self._required(tx, DatasetAsset, current.dataset_id)
                if dataset.status != "PENDING":
                    return
                if dataset.data["identity"] != analysis["dataset"]:
                    raise ServiceError("DATASET_IDENTITY_MISMATCH")
                current.transition("AVAILABLE"); tx.save(current)
                dataset.transition("AVAILABLE"); tx.save(dataset)
            else:
                run = self._required(tx, TrainingRun, current.run_id)
                if run.status != "RUNNING" or run.cancel_requested or run.recovery_required:
                    current.transition("DELETING"); tx.save(current)
                else:
                    current.transition("AVAILABLE"); tx.save(current)

    def maintain(self, limit=50):
        self.maintain_scopes(limit)
        with self.repo.transaction() as tx:
            for run in tx.active_claims():
                if not run.recovery_required and now() - run.heartbeat_at > timedelta(seconds=LEASE_SECONDS):
                    run.recovery_required = True; run.touch(); tx.save(run)
            artifacts = tx.due_artifacts(limit)
            for artifact in artifacts:
                artifact.checked_at = now(); artifact.touch(); tx.save(artifact)
        for artifact in artifacts:
            try:
                self.reconcile_artifact(artifact.id)
            except (ServiceError, EngineError) as error:
                with self.repo.transaction() as tx:
                    current = self._required(tx, MLArtifact, artifact.id)
                    current.data = {**current.data, "maintenance_error": error.code}
                    tx.mark_scope_cleanup_pending(current.scope_id)
                    if current.status == "PENDING" and getattr(error, "status", 409) != 503:
                        current.transition("FAILED")
                        if current.dataset_id:
                            dataset = self._required(tx, DatasetAsset, current.dataset_id)
                            if dataset.status == "PENDING":
                                dataset.transition("FAILED"); tx.save(dataset)
                        elif current.prediction_id:
                            prediction = self._required(tx, Prediction, current.prediction_id)
                            if prediction.status not in TERMINAL and prediction.process_stopped:
                                prediction.finish("CANCELLED" if prediction.cancel_requested else "FAILED", error.code)
                                tx.save(prediction)
                    else:
                        current.touch()
                    tx.save(current)
