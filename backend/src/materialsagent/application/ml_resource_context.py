"""Call-scoped resource projection, deterministic binding, and ML result adoption."""
from __future__ import annotations

from collections import defaultdict, deque
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from time import monotonic
from uuid import uuid4

from materialsagent.domain.models.agent import ResourceBinding, canonical
from materialsagent.domain.models.ml_resource_context import BINDING_VERSION
from materialsagent.domain.ports.agent import AgentFailure
from materialsagent.domain.ports.tool_registry import ResourceProvider
from .errors import ApplicationError


resource_read_deadline = ContextVar("ml_resource_read_deadline", default=None)


@contextmanager
def reads(seconds=10):
    token = resource_read_deadline.set(monotonic() + max(0, seconds))
    try:
        yield
    finally:
        resource_read_deadline.reset(token)


def description(ref):
    names = {"dataset": "数据集", "training_run": "模型训练", "model": "预测模型", "prediction": "预测结果"}
    value = deepcopy(ref.get("description", {}))
    value.setdefault("name", names[ref["resource_type"]] + (f" {ref['dataset_ordinal']}" if ref.get("dataset_ordinal") else ""))
    return value


def _source(value: str, *, current: bool) -> str:
    if current:
        return "current_message_attachment"
    if value.startswith("upload:") or value in {"DATASET_UPLOAD", "RESOURCE_API_VERIFIED"}:
        return "uploaded"
    if value.startswith("invocation:"):
        return "tool_result"
    if value.startswith("lineage:"):
        return "derived"
    return "registered"


def _safe_details(ref: dict) -> dict:
    source = ref.get("description", {})
    result = {}
    if isinstance(source.get("columns"), list):
        result["columns"] = [item for item in source["columns"][:64] if isinstance(item, str)]
    if isinstance(source.get("units"), dict):
        result["declared_units"] = {str(key): value for key, value in list(source["units"].items())[:64]
                                     if isinstance(value, str)}
    for key in ("row_count", "algorithm", "target"):
        if isinstance(source.get(key), (str, int, float)) and not isinstance(source.get(key), bool):
            result[key] = source[key]
    return result


class ResourceContextResolver:
    """Projects local candidates and turns selected temporary refs into frozen facts."""

    def __init__(self, resources, registry, uow_factory):
        self.resources = resources
        self.registry = registry
        self.uow_factory = uow_factory

    def supports(self, registration):
        for item in registration.resource_parameters:
            if item.provider is ResourceProvider.ML_RESOURCE and (
                self.resources is None or self.resources.client is None
            ):
                return False
            if item.provider is ResourceProvider.ASSET and self.uow_factory is None:
                return False
        return True

    def _ml_references(self, run):
        if self.resources is None or self.resources.client is None:
            return []
        with self.resources.repo.transaction(run.actor_id, run.conversation_id) as tx:
            return [row["document"] for row in tx.rows(
                "reference", service_id=self.resources._client().service["service_id"]
            )]

    @staticmethod
    def _fair(values, limit):
        buckets = defaultdict(deque)
        for value in values:
            buckets[value["resource_type"]].append(value)
        result = []
        kinds = sorted(buckets)
        while kinds and len(result) < limit:
            remaining = []
            for kind in kinds:
                if buckets[kind] and len(result) < limit:
                    result.append(buckets[kind].popleft())
                if buckets[kind]:
                    remaining.append(kind)
            kinds = remaining
        return result

    def context(self, run):
        current = {item["attachment_id"]: item for item in run.attachments}
        refs = self._ml_references(run)
        refs.sort(key=lambda item: (item.get("created_at", ""), item["reference_id"]), reverse=True)
        current_refs = [item for item in refs if item["reference_id"] in current]
        other_refs = [item for item in refs if item["reference_id"] not in current]
        candidates = current_refs + self._fair(other_refs, 20 - len(current_refs))
        handles, projected = {}, []

        def append(item, handle):
            ref = f"r{len(projected) + 1}"
            candidate = {"resource_ref": ref, **item}
            projected.append(candidate)
            handles[ref] = handle
            if len(canonical({"resources": projected, "complete": True, "omitted_count": 0}).encode("utf-8")) > 16_000:
                projected.pop()
                handles.pop(ref)
                return False
            return True

        for ref in candidates[:20]:
            detail = description(ref)
            item = {
                "resource_type": ref["resource_type"],
                "name": detail["name"],
                "source": _source(ref.get("source", ""), current=ref["reference_id"] in current),
                "created_at": ref.get("created_at"),
            }
            if ref.get("dataset_ordinal") is not None:
                item["dataset_ordinal"] = ref["dataset_ordinal"]
            details = _safe_details(ref)
            if details:
                item["details"] = details
            if not append(item, {"provider": "ml_resource", "resource_type": ref["resource_type"],
                                 "platform_resource_id": ref["reference_id"]}):
                break

        for attachment in run.attachments:
            if len(projected) >= 20 or attachment["kind"] != "ebsd_image":
                continue
            append({"resource_type": "ebsd_image", "name": attachment["name"],
                    "source": "current_message_attachment"},
                   {"provider": "asset", "resource_type": "ebsd_image",
                    "platform_resource_id": attachment["attachment_id"]})

        total = len(refs) + sum(1 for item in run.attachments if item["kind"] == "ebsd_image")
        return {"view": {"resources": projected, "complete": len(projected) == total,
                         "omitted_count": max(0, total - len(projected))}, "mapping": handles}

    def _asset(self, run, identity):
        with self.uow_factory() as uow:
            asset = uow.assets.get_owned(identity, run.actor_id)
            if (asset is None or asset.conversation_id != run.conversation_id
                    or asset.asset_type != "ebsd_image" or asset.source_type != "UPLOADED"):
                raise AgentFailure("RESOURCE_ASSET_IDENTITY_MISMATCH")
            if asset.current_status != "AVAILABLE" or not asset.sha256:
                raise AgentFailure("RESOURCE_NOT_AVAILABLE")
            return asset

    def _ml_binding(self, run, spec, identity):
        ref = self.resources.reference(run.actor_id, run.conversation_id, identity)
        if ref["resource_type"] != spec.expected_resource_type.value:
            raise AgentFailure("RESOURCE_TYPE_MISMATCH")
        descriptor = self.resources.verified(run.actor_id, run.conversation_id, identity)
        if descriptor["resource_type"] != "training_run" and descriptor.get("status") != "AVAILABLE":
            raise AgentFailure("RESOURCE_NOT_AVAILABLE")
        return ResourceBinding(provider="ml_resource", resource_type=ref["resource_type"],
            model_argument=spec.model_argument, execution_argument=spec.execution_argument,
            platform_resource_id=ref["reference_id"], execution_value=ref["resource_id"],
            identity_digest=ref["remote_identity_digest"], safe_description=description(ref))

    def _asset_binding(self, run, spec, identity):
        asset = self._asset(run, identity)
        if spec.expected_resource_type.value != "ebsd_image":
            raise AgentFailure("RESOURCE_TYPE_MISMATCH")
        name = next((item["name"] for item in run.attachments if item["attachment_id"] == asset.asset_id), "已上传 EBSD 图片")
        return ResourceBinding(provider="asset", resource_type="ebsd_image",
            model_argument=spec.model_argument, execution_argument=spec.execution_argument,
            platform_resource_id=asset.asset_id, execution_value=asset.asset_id,
            content_digest=asset.sha256, safe_description={"name": name})

    def bind(self, run, spec, handle):
        if not isinstance(handle, dict) or set(handle) != {"provider", "resource_type", "platform_resource_id"}:
            raise AgentFailure("RESOURCE_REFERENCE_INVALID")
        if handle["provider"] != spec.provider.value or handle["resource_type"] != spec.expected_resource_type.value:
            raise AgentFailure("RESOURCE_TYPE_MISMATCH")
        if spec.provider is ResourceProvider.ML_RESOURCE:
            return self._ml_binding(run, spec, handle["platform_resource_id"])
        return self._asset_binding(run, spec, handle["platform_resource_id"])

    def verify(self, run, binding: ResourceBinding):
        if binding.version != BINDING_VERSION:
            raise AgentFailure("RESOURCE_BINDING_VERSION_MISMATCH")
        if binding.provider == "ml_resource":
            ref = self.resources.reference(run.actor_id, run.conversation_id, binding.platform_resource_id)
            descriptor = self.resources.verified(run.actor_id, run.conversation_id, binding.platform_resource_id)
            if (ref["resource_id"] != binding.execution_value or ref["resource_type"] != binding.resource_type
                    or ref["remote_identity_digest"] != binding.identity_digest):
                raise AgentFailure("RESOURCE_BINDING_CHANGED")
            if descriptor["resource_type"] != "training_run" and descriptor.get("status") != "AVAILABLE":
                raise AgentFailure("RESOURCE_NOT_AVAILABLE")
            return descriptor
        asset = self._asset(run, binding.platform_resource_id)
        if (asset.asset_id != binding.execution_value or asset.asset_type != binding.resource_type
                or asset.sha256 != binding.content_digest):
            raise AgentFailure("RESOURCE_BINDING_CHANGED")
        return asset

    def resolve(self, run, registration, merged, previous):
        values = dict(merged)
        issues, bindings = {}, {}
        waiting_fields = set(run.waiting.fields if run.waiting else ())
        for spec in registration.resource_parameters:
            field = spec.execution_argument
            old = previous.resource_bindings.get(field) if previous else None
            supplied = values.pop(field, None)
            if old is not None and field not in waiting_fields and supplied is None:
                try:
                    self.verify(run, old)
                    bindings[field] = old
                    values[field] = old.execution_value
                except (AgentFailure, ApplicationError):
                    issues[field] = "Conflict"
                continue
            if isinstance(supplied, dict) and supplied.get("_resource_unresolved") is True:
                issues[field] = "Ambiguous"
                continue
            if isinstance(supplied, dict) and supplied.get("_resource_invalid") is True:
                issues[field] = "Invalid"
                continue
            if supplied is None:
                issues[field] = "Missing"
                continue
            if (not isinstance(supplied, dict)
                    or set(supplied) != {"provider", "resource_type", "platform_resource_id"}
                    or supplied.get("provider") != spec.provider.value
                    or supplied.get("resource_type") != spec.expected_resource_type.value):
                issues[field] = "Invalid"
                continue
            try:
                binding = self.bind(run, spec, supplied)
            except (AgentFailure, ApplicationError):
                issues[field] = "Conflict"
                continue
            bindings[field] = binding
            values[field] = binding.execution_value
        return values, issues, bindings

    def dispatch(self, run, record, seconds):
        registration = self.registry.resolve(record.tool_name)
        required = {item.execution_argument for item in registration.resource_parameters if item.required}
        if set(record.resource_bindings) != required:
            raise AgentFailure("RESOURCE_BINDING_REQUIRED")
        with reads(min(10, seconds)):
            for field, binding in record.resource_bindings.items():
                if record.arguments.get(field) != binding.execution_value:
                    raise AgentFailure("RESOURCE_BINDING_CHANGED")
                self.verify(run, binding)


class MLResourceResultRegistrar:
    """Registers committed MCP results without participating in semantic selection."""

    def __init__(self, resources, invocations):
        self.resources = resources
        self.invocations = invocations

    def discover_model(self, run, training):
        value = self.resources.resource(run.actor_id, run.conversation_id, training["reference_id"])
        if value["status"] != "SUCCEEDED" or not value.get("model_id"):
            raise AgentFailure("ML_TRAINING_NOT_COMPLETE")
        descriptor = self.resources._client().descriptor(run.conversation_id, "model", value["model_id"])
        if descriptor["identity"].get("training_run_id") != training["resource_id"]:
            raise AgentFailure("ML_RESOURCE_LINEAGE_MISMATCH")
        with self.resources.repo.transaction(run.actor_id, run.conversation_id, writable=True) as tx:
            return tx.register(self.resources._client().service, descriptor, "lineage:" + training["reference_id"])

    def adopt(self, run, record, *, reconcile_operation=None):
        from .context import ActorContext
        from .agent_tools import plain
        value = self.invocations.get(ActorContext(actor_id=run.actor_id, user_id=None), record.invocation_run_id)
        invocation = value.run
        if invocation.conversation_id != run.conversation_id or invocation.executor_id != "mcp":
            return None
        self.invocations._current_registration(invocation)
        if invocation.status.value == "SUCCEEDED":
            resource = plain(value.result.data).get("resource") if value.result else None
        elif invocation.status.value == "FAILED" and record.tool_name == "materials_ml_predict_with_model":
            receipt = plain(invocation.remote_receipt or {})
            resource = receipt.get("resource")
            if receipt.get("lookup_status") != "FOUND" or not resource or resource.get("status") not in ("FAILED", "CANCELLED"):
                return None
        elif invocation.status.value == "OUTCOME_UNKNOWN" and reconcile_operation:
            receipt = plain(invocation.remote_receipt or {})
            operation = plain(invocation.remote_operation or {})
            if (receipt.get("last_check", {}).get("lookup_status", "FOUND") != "FOUND"
                    or receipt.get("lookup_status") != "FOUND" or any(receipt.get(key) != operation.get(key)
                    for key in ("operation", "request_digest", "digest_version"))):
                return None
            resource = receipt.get("resource")
        else:
            return None
        kind = {"materials_ml_train_tabular_regression": "training_run", "materials_ml_get_training_run": "training_run",
                "materials_ml_predict_with_model": "prediction"}.get(record.tool_name)
        if not kind or not resource or resource.get("scope_id") != run.conversation_id or not resource.get("id"):
            return None
        descriptor = self.resources._client().descriptor(run.conversation_id, kind, resource["id"])
        if kind == "training_run" and record.tool_name.endswith("train_tabular_regression"):
            if descriptor["identity"].get("dataset_id") != record.arguments.get("dataset_id"):
                raise AgentFailure("ML_RESOURCE_LINEAGE_MISMATCH")
        if kind == "prediction" and any(descriptor["identity"].get(key) != record.arguments.get(key)
                                         for key in ("model_id", "input_dataset_id")):
            raise AgentFailure("ML_RESOURCE_LINEAGE_MISMATCH")
        source = "invocation:" + invocation.invocation_run_id + (":reconcile:" + reconcile_operation if reconcile_operation else "")
        with self.resources.repo.transaction(run.actor_id, run.conversation_id, writable=True) as tx:
            ref = tx.register(self.resources._client().service, descriptor, source)
        if kind == "training_run" and resource.get("status") == "SUCCEEDED":
            self.discover_model(run, ref)
        return ref

    def after_result(self, run, record, seconds=10):
        try:
            with reads(min(10, seconds)):
                ref = self.adopt(run, record)
            if ref:
                artifact = {"attachment_id": ref["reference_id"], "kind": ref["resource_type"],
                            "name": description(ref)["name"]}
                if artifact not in run.result_attachments:
                    run.result_attachments.append(artifact)
        except (ApplicationError, AgentFailure):
            pass

    def explicit_reconcile(self, run, record):
        operation = uuid4().hex
        try:
            with reads(10):
                ref = self.adopt(run, record, reconcile_operation=operation)
            return {"operation_id": operation, "status": "REGISTERED" if ref else "NOT_CONFIRMED",
                    "reference_id": ref["reference_id"] if ref else None}
        except (ApplicationError, AgentFailure):
            return {"operation_id": operation, "status": "REGISTRATION_PENDING", "reference_id": None}
