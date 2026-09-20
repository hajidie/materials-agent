"""Controlled ML references. The remote Application remains the lifecycle owner."""
from uuid import uuid4
from datetime import datetime, timezone

from .errors import ApplicationConflictError, DependencyUnavailableError, ResourceNotFoundError, ConversationBusyError, ApplicationError
from .idempotency import validate_idempotency_key
from materialsagent.domain.models.ml_resource import descriptor_matches, IDENTITY_VERSION


class MLResources:
    def __init__(self, repository, client):
        self.repo, self.client = repository, client

    def _client(self):
        if self.client is None:
            raise DependencyUnavailableError(code="ML_RESOURCE_UNAVAILABLE")
        return self.client

    def register(self, actor, conversation, kind, identity):
        with self.repo.transaction(actor, conversation, writable=True) as tx:
            epoch = tx.fence_version
        client = self._client()
        descriptor = client.descriptor(conversation, kind, identity)
        with self.repo.transaction(actor, conversation, writable=True, expected_version=epoch) as tx:
            return tx.register(client.service, descriptor, "RESOURCE_API_VERIFIED")

    def list(self, actor, conversation, *, after=None, limit=20, resource_type=None, resource_id=None):
        from .ml_resource_views import KINDS
        if (resource_type is not None and resource_type not in KINDS) or (resource_id is not None and resource_type is None):
            from .errors import ApplicationValidationError
            raise ApplicationValidationError(code="INVALID_RESOURCE_TYPE")
        with self.repo.transaction(actor, conversation) as tx:
            values = tx.reference_page(after, limit, resource_type, resource_id) if resource_type else tx.reference_page(after, limit)
            return {"items": values[:limit], "next_cursor": values[limit-1]["reference_id"] if len(values) > limit else None}

    def ui_state(self, actor, conversation):
        with self.repo.transaction(actor, conversation) as tx:
            return {"conversation_id": conversation, "fence": tx.ui_fence()}

    def uploads(self, actor, conversation, *, after=None, limit=20, key=None):
        from .ml_resource_views import upload_view
        if key is not None:
            try:
                validate_idempotency_key(key)
            except ValueError:
                from .errors import ApplicationValidationError
                raise ApplicationValidationError(code="INVALID_IDEMPOTENCY_KEY") from None
        with self.repo.transaction(actor, conversation) as tx:
            values = tx.upload_page(after, limit, key)
            return {"items": [upload_view(v) for v in values[:limit]],
                "next_cursor": values[limit-1]["operation_id"] if len(values) > limit else None}

    def discover_model(self, actor, conversation, identity):
        """An explicit, idempotent registration command anchored to a registered run."""
        ref = self.reference(actor, conversation, identity)
        if ref["resource_type"] != "training_run":
            raise ResourceNotFoundError()
        with self.repo.transaction(actor, conversation, writable=True) as tx:
            epoch = tx.fence_version
        run = self.resource(actor, conversation, identity)
        if run.get("status") != "SUCCEEDED" or not run.get("model_id"):
            raise ApplicationConflictError(code="ML_MODEL_NOT_READY")
        client = self._client()
        descriptor = client.descriptor(conversation, "model", run["model_id"])
        if (descriptor["identity"].get("training_run_id") != ref["resource_id"] or
                descriptor["identity"].get("dataset_id") != ref["identity"].get("dataset_id")):
            raise ApplicationConflictError(code="ML_RESOURCE_LINEAGE_MISMATCH")
        with self.repo.transaction(actor, conversation, writable=True, expected_version=epoch) as tx:
            return tx.register(client.service, descriptor, "lineage:" + identity)

    def reference(self, actor, conversation, identity):
        with self.repo.transaction(actor, conversation) as tx:
            return tx.ref(identity)

    def verified(self, actor, conversation, identity):
        ref = self.reference(actor, conversation, identity)
        client = self._client()
        with self.repo.transaction(actor, conversation) as tx:
            bound = tx.rows("binding", service_id=ref["service_id"])
            if not bound or bound[0]["document"]["service"] != client.service:
                raise ApplicationConflictError(code="ML_SERVICE_BINDING_MISMATCH")
        descriptor = client.descriptor(conversation, ref["resource_type"], ref["resource_id"], ref["identity_contract_version"])
        if not descriptor_matches(ref, descriptor):
            raise ApplicationConflictError(code="ML_RESOURCE_IDENTITY_MISMATCH")
        return descriptor

    def resource(self, actor, conversation, identity, action=None):
        descriptor = self.verified(actor, conversation, identity)
        result = self._client().resource(conversation, descriptor["resource_type"], descriptor["resource_id"], action=action)
        if result.get("scope_id") != conversation or result.get("id") != descriptor["resource_id"]:
            raise DependencyUnavailableError(code="ML_RESOURCE_IDENTITY_MISMATCH")
        return result

    def download(self, actor, conversation, identity, member):
        descriptor = self.verified(actor, conversation, identity)
        expected = "SUCCEEDED" if descriptor["resource_type"] == "prediction" else "AVAILABLE"
        if descriptor["status"] != expected:
            raise ApplicationConflictError(code="ML_RESOURCE_NOT_AVAILABLE")
        return self._client().download(conversation, descriptor, member)

    def upload(self, actor, conversation, key, payload, metadata):
        try:
            validate_idempotency_key(key)
        except ValueError:
            from .errors import ApplicationValidationError
            raise ApplicationValidationError(code="INVALID_IDEMPOTENCY_KEY") from None
        with self.repo.transaction(actor, conversation, writable=True) as tx:
            epoch = tx.fence_version
        client = self._client()
        prepared = client.prepare_upload(conversation, payload, metadata)
        with self.repo.transaction(actor, conversation, writable=True, expected_version=epoch) as tx:
            tx.bind(client.service, "DATASET_UPLOAD")
            rows = tx.rows("upload", operation_key=key)
            fresh = not rows
            if rows:
                operation = rows[0]["document"]
                if operation["request_digest"] != prepared["request_digest"]:
                    raise ApplicationConflictError(code="IDEMPOTENCY_CONFLICT")
            else:
                identity = uuid4().hex
                operation = {**prepared, "operation_id": identity, "idempotency_key": "platform-upload:" + identity,
                    "conversation_id": conversation, "service": client.service, "status": "DISPATCHED",
                    "created_at": datetime.now(timezone.utc).isoformat()}
                tx.put("upload", identity, operation, operation_key=key, status="DISPATCHED")
        if not fresh:
            result = self.reconcile_upload(actor, operation["operation_id"])
            if result.get("remote_status") != "PENDING":
                return result
            # Explicit same-file replay resumes only an already confirmed original upload.
            with self.repo.transaction(actor, conversation, writable=True, expected_version=epoch) as tx:
                current = tx.rows("upload", id=operation["operation_id"])[0]
                if current["status"] == "RESOLVED":
                    return current["document"]
                operation = {**current["document"], "status": "DISPATCHED"}
                tx.put("upload", operation["operation_id"], operation, operation_key=key, status="DISPATCHED")
        try:
            client.upload(conversation, operation, payload, metadata)
        except ApplicationError:
            # Even an error response may follow intent creation. Resolve original history.
            pass
        return self.reconcile_upload(actor, operation["operation_id"])

    def upload_operation(self, actor, identity):
        return self.repo.operation(actor, identity)["document"]

    def recover_uploads(self, actor):
        for identity in self.repo.pending_uploads(actor):
            try:
                self.reconcile_upload(actor, identity)
            except ApplicationError:
                pass

    def reconcile_upload(self, actor, identity):
        row = self.repo.operation(actor, identity)
        operation, conversation = row["document"], row["conversation_id"]
        if operation["status"] == "RESOLVED":
            return operation
        client = self._client()
        if operation["service"] != client.service:
            raise ApplicationConflictError(code="ML_SERVICE_BINDING_MISMATCH")
        try:
            receipt = client.lookup(conversation, operation)
            if any(receipt.get(k) != operation[k] for k in ("scope_id", "operation", "request_digest", "digest_version")):
                raise DependencyUnavailableError(code="ML_UPLOAD_RECEIPT_INVALID")
            resource = receipt.get("resource") or {}
            descriptor = None
            if receipt.get("lookup_status") == "FOUND":
                if resource.get("scope_id") != conversation or not resource.get("id"):
                    raise DependencyUnavailableError(code="ML_UPLOAD_RECEIPT_INVALID")
                descriptor = client.descriptor(conversation, "dataset", resource["id"])
                # Descriptive metadata is frozen at upload; availability remains remote.
                detail = client.resource(conversation, "dataset", resource["id"])
                if detail.get("id") != resource["id"] or detail.get("scope_id") != conversation:
                    raise DependencyUnavailableError(code="ML_RESOURCE_IDENTITY_MISMATCH")
                from .result_projection import project_resource
                facts = project_resource(detail, kind="dataset")["facts"]
                descriptor["description"] = {"name": facts.get("display_name") or "CSV 实验数据",
                    **{key: facts[key] for key in ("row_count", "columns", "units") if key in facts}}
        except ApplicationError:
            receipt, descriptor, resource = {"lookup_status": "UNAVAILABLE"}, None, {}
        with self.repo.transaction(actor, conversation) as tx:
            latest = tx.rows("upload", id=identity)[0]
            current = latest["document"]
            if latest["version"] != row["version"]:
                return current
            if current["status"] == "RESOLVED":
                return current
            value = {**current, "status": "OUTCOME_UNKNOWN", "lookup_status": receipt.get("lookup_status")}
            if current.get("resource_id") and descriptor is None:
                value = {**current, "last_check": receipt.get("lookup_status")}
            if descriptor:
                ref = tx.register(client.service, descriptor, "upload:" + identity)
                value.update(reference_id=ref["reference_id"], remote_status=descriptor["status"], resource_id=descriptor["resource_id"],
                    status="RESOLVED" if descriptor["status"] != "PENDING" else "REMOTE_PENDING")
            tx.put("upload", identity, value, operation_key=row["operation_key"], status=value["status"])
            return value


class MLDeletionCoordinator:
    def __init__(self, resources, cleanup):
        self.resources, self.cleanup = resources, cleanup

    def delete(self, actor, conversation):
        try:
            operation = self.resources.repo.begin_delete(actor.actor_id, conversation, self.cleanup._process_cutoff)
        except ResourceNotFoundError:
            return self.cleanup._delete_local(actor, conversation)
        if operation is None:
            return self.cleanup._delete_local(actor, conversation)
        result = self.reconcile(actor, operation["operation_id"])
        if result["status"] == "REJECTED_BUSY":
            raise ConversationBusyError(conversation_id=conversation)
        if result["status"] != "COMPLETED":
            raise DependencyUnavailableError(code="CONVERSATION_DELETE_PENDING", conversation_id=conversation,
                details=[{"operation_id": operation["operation_id"]}])
        from .conversation_cleanup import ConversationDeletion
        return ConversationDeletion(conversation, tuple(result.get("cleanup_ids", ())))

    def reconcile(self, actor, identity):
        repo = self.resources.repo
        operation = repo.operation(actor.actor_id, identity, "deletion")["document"]
        if operation["status"] in ("COMPLETED", "REJECTED_BUSY"):
            return operation
        try:
            client = self.resources._client()
            if client.service != operation["service"]:
                raise DependencyUnavailableError()
            receipt = client.close_scope(operation["scope_id"], identity, lookup=True)
            if receipt["status"] == "NOT_FOUND":
                receipt = client.close_scope(operation["scope_id"], identity)
        except ApplicationError:
            receipt = {"status": "UNKNOWN"}
        fact = repo.deletion_fact(actor.actor_id, identity, receipt)
        if fact.get("receipt", {}).get("status") == "CLOSED":
            try:
                self.cleanup._delete_local(actor, operation["conversation_id"], closing_operation=identity)
            except ApplicationError:
                # Commit acknowledgement loss must not overwrite a durable completion.
                try:
                    return repo.operation(actor.actor_id, identity, "deletion")["document"]
                except ApplicationError:
                    return fact
            return repo.operation(actor.actor_id, identity, "deletion")["document"]
        return fact

    def recover(self, actor):
        for identity in self.resources.repo.pending_deletions(actor.actor_id):
            try:
                self.reconcile(actor, identity)
            except ApplicationError:
                pass
