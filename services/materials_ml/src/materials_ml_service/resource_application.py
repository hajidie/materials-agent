"""Authority for resource identity and neutral scope lifecycle."""
from .domain import DatasetAsset, TrainingRun, ModelAsset, Prediction, MLArtifact, ServiceError, digest, text_id

IDENTITY_VERSION = "ml-resource-identity-v1"
RESOURCE_TYPES = {"dataset": DatasetAsset, "training_run": TrainingRun, "model": ModelAsset, "prediction": Prediction}


class ResourceApplication:
    def resource_identity(self, scope, kind, identity, version=IDENTITY_VERSION):
        if version != IDENTITY_VERSION:
            raise ServiceError("IDENTITY_CONTRACT_UNSUPPORTED", 409)
        if kind not in RESOURCE_TYPES:
            raise ServiceError("RESOURCE_NOT_FOUND", 404)
        with self.repo.transaction() as tx:
            tx.require_open(scope)
            resource = self._required(tx, RESOURCE_TYPES[kind], identity, scope)
            data = resource.data
            if kind == "dataset":
                immutable = {k: data[k] for k in ("identity", "raw_sha256", "parser_contract", "schema_version", "units", "request_digest")}
                members = {"dataset.csv": resource.artifact_id}
            elif kind == "training_run":
                immutable = {"dataset_id": resource.dataset_id, "request_digest": data["request_digest"]}
                members = {}
            elif kind == "model":
                run = self._required(tx, TrainingRun, resource.run_id, scope)
                manifest = self._required(tx, MLArtifact, data["members"]["manifest.json"], scope)
                immutable = {"training_run_id": resource.run_id, "dataset_id": run.dataset_id,
                             "manifest_sha256": manifest.object_ref["sha256"]}
                members = data["members"]
            else:
                immutable = {"model_id": resource.model_id, "input_dataset_id": resource.input_dataset_id,
                             "request_digest": data["request_digest"]}
                members = {"predictions.json": resource.artifact_id} if resource.artifact_id else {}
            envelope = {"identity_contract_version": version, "resource_type": kind,
                        "resource_id": identity, "scope_id": scope, "identity": immutable}
            files = {}
            for member, artifact_id in members.items():
                artifact = self._required(tx, MLArtifact, artifact_id, scope)
                if artifact.status == "AVAILABLE":
                    files[member] = {k: artifact.object_ref[k] for k in ("sha256", "size_bytes", "media_type")}
            return {**envelope, "remote_identity_digest": digest(envelope), "status": resource.status, "files": files}

    def close_scope(self, scope, operation_id):
        text_id(scope); text_id(operation_id)
        with self.repo.transaction() as tx:
            previous = tx.scope_close(scope, operation_id)
            if previous:
                return previous
            tx.ensure_scope(scope)
            closed = tx.scope_state(scope)["status"] == "CLOSED"
            busy = False if closed else tx.scope_busy(scope)
            tx.record_scope_close(scope, operation_id, "BUSY" if busy else "CLOSED")
            return tx.scope_close(scope, operation_id)

    def lookup_scope_close(self, scope, operation_id):
        text_id(scope); text_id(operation_id)
        with self.repo.transaction() as tx:
            return tx.scope_close(scope, operation_id) or {"scope_id": scope, "operation_id": operation_id, "status": "NOT_FOUND"}

    def maintain_scopes(self, limit):
        # Closing only records ownership. Work is paged in short transactions.
        with self.repo.transaction() as tx:
            tx.clean_closed_scopes(limit)
