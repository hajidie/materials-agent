"""Deterministic batch coordinator; one parent call, durable single-image requests."""
import json
from hashlib import sha256
import urllib3
from materialsagent.application.context import ActorContext
from materialsagent.application.ebsd_assets import require_asset
from materialsagent.application.errors import ApplicationError, ApplicationConflictError, DependencyUnavailableError
from materialsagent.application.execution_deadline import current_execution_owner, remaining_timeout
from materialsagent.application import tc4_assets
from materialsagent.domain.models.agent import now
from materialsagent.domain.models.tc4 import TOOL_ID, MODEL_VERSION, MOCK_MODEL_VERSION, PREPROCESSING_VERSION, OUTPUTS, ToolRunItem, PixelStatistics, batch_status
from materialsagent.domain.ports.tool_execution import ToolExecutionOutput, ToolClientError, ToolClientUnavailableError, ToolClientProtocolError, ToolClientRuntimeError
from .local_zta35g import LocalZTA35GToolClientAdapter


class LocalTC4Client(LocalZTA35GToolClientAdapter):
    def __init__(self, *, asset_service=None, uow_factory=None, agent_store=None, model_version=MODEL_VERSION, **kwargs):
        super().__init__(**kwargs)
        self.asset_service, self.uow_factory, self.agent_store = asset_service, uow_factory, agent_store
        if model_version not in {MODEL_VERSION, MOCK_MODEL_VERSION}:
            raise ValueError("Unsupported TC4 model version.")
        self.model_version = model_version

    def readiness(self, metadata):
        try:
            value = self._request_json("GET", "/internal/v1/tc4/health/ready", None)
            if (value.get("status"), value.get("tool_id"), value.get("model_version"), value.get("preprocessing_version")) != (
                    "READY", TOOL_ID, self.model_version, PREPROCESSING_VERSION):
                return "UNAVAILABLE"
            return "DEGRADED" if value.get("busy") else "AVAILABLE"
        except Exception:
            return "UNAVAILABLE"

    def items(self, tool_run_id):
        with self.uow_factory() as uow:
            return uow.tool_run_items.list_for_tool_run(tool_run_id)

    def update(self, item, **changes):
        updated = ToolRunItem.model_validate({**item.model_dump(), **changes, "version": item.version + 1, "updated_at": now()})
        with self.uow_factory() as uow:
            if uow.tool_run_items.update(updated, expected_version=item.version) is None:
                raise ApplicationConflictError()
            uow.commit()
        return updated

    def _owner(self, actor_id):
        owner = current_execution_owner()
        if owner is None or self.agent_store is None:
            return None
        return self.agent_store.get(owner[0], actor_id)

    def _can_dispatch(self, actor_id):
        remaining_timeout(1200)
        run = self._owner(actor_id)
        return run is None or (run.status == "RUNNING" and run.stop_requested_at is None)

    @staticmethod
    def request(context, item):
        return {"request_id": item.request_id, "conversation_id": context.conversation_id, "task_id": context.task_id,
            "tool_run_id": context.tool_run_id, "item_id": item.item_id, "asset_id": item.input_asset_id, "sha256": item.input_sha256}

    def _http(self, method, path, body=None, request=None, maximum=65536):
        headers = {"X-ZTA35G-Runtime-Token": self._token}
        if request is not None:
            headers.update({"Content-Type": "application/octet-stream", "X-TC4-Request": json.dumps(request)})
        # An idle keep-alive connection may be closed while inference finishes
        # or the user stops. Only repeat reads of the same receipt/artifact.
        for attempt in range(2 if method == "GET" else 1):
            try:
                response = self._pool.request(method, self._base_url + path, body=body, headers=headers,
                    timeout=self._request_timeout(), retries=False, preload_content=False)
                try:
                    payload = response.read(maximum + 1)
                    if len(payload) > maximum:
                        raise ToolClientProtocolError(outcome_unknown=method == "POST")
                    return response.status, payload
                finally:
                    response.close()
                    response.release_conn()
            except (OSError, urllib3.exceptions.HTTPError, TimeoutError):
                if method != "GET" or attempt:
                    raise ToolClientUnavailableError(outcome_unknown=True) from None

    def lookup(self, request):
        status, payload = self._http("GET", "/internal/v1/tc4/receipts/" + request["request_id"])
        if status == 404:
            return None
        if status != 200:
            raise ToolClientUnavailableError(outcome_unknown=True)
        return self.validate_receipt(json.loads(payload), request)

    def cleanup(self, request):
        body = json.dumps(request).encode("utf-8")
        status, payload = self._http("POST", "/internal/v1/tc4/cleanup", body=body)
        return status == 200 and json.loads(payload) == {"status": "DELETED"}

    def validate_receipt(self, receipt, request):
        try:
            if (receipt["request"] != request or receipt["tool_id"] != TOOL_ID
                    or receipt["model_version"] != self.model_version or receipt["preprocessing_version"] != PREPROCESSING_VERSION
                    or receipt["status"] not in {"SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN"}):
                raise ValueError()
            if receipt["status"] == "SUCCEEDED":
                PixelStatistics.model_validate(receipt["data"])
                if set(receipt["artifacts"]) != {"overlay", "mask"}:
                    raise ValueError()
                for artifact in receipt["artifacts"].values():
                    if (set(artifact) != {"sha256", "size_bytes"} or type(artifact["size_bytes"]) is not int
                            or not 0 < artifact["size_bytes"] <= tc4_assets.MAX_PNG_BYTES
                            or not isinstance(artifact["sha256"], str) or len(artifact["sha256"]) != 64
                            or any(c not in "0123456789abcdef" for c in artifact["sha256"])):
                        raise ValueError()
            return receipt
        except (KeyError, ValueError, TypeError):
            raise ToolClientProtocolError(outcome_unknown=True) from None

    def _receive(self, actor, context, item, receipt):
        if receipt is None or receipt["status"] == "OUTCOME_UNKNOWN":
            if item.status == "DISPATCHED":
                self.update(item, status="OUTCOME_UNKNOWN")
            raise ToolClientUnavailableError(outcome_unknown=True)
        if item.status != "RESULT_RECEIVED":
            item = self.update(item, status="RESULT_RECEIVED", receipt=receipt,
                statistics=PixelStatistics.model_validate(receipt["data"]) if receipt["status"] == "SUCCEEDED" else None)
        if receipt["status"] == "FAILED":
            self.update(item, status="FAILED", error={"code": "TC4_INFERENCE_FAILED", "message": "分割运行失败，后续图片已暂停。"})
            raise ToolClientUnavailableError(outcome_unknown=True)
        artifacts = {}
        for role in ("overlay", "mask"):
            status, payload = self._http("GET", "/internal/v1/tc4/receipts/" + item.request_id + "/" + role,
                maximum=tc4_assets.MAX_PNG_BYTES)
            descriptor = receipt["artifacts"][role]
            if status != 200 or len(payload) != descriptor["size_bytes"] or sha256(payload).hexdigest() != descriptor["sha256"]:
                raise ToolClientProtocolError(outcome_unknown=True)
            asset = tc4_assets.create(self.asset_service, actor, context, item, role, payload)
            artifacts[role] = asset.asset_id
        return self.update(item, status="SUCCEEDED", artifacts=artifacts)

    def execute(self, metadata, validated_input, context):
        if self.asset_service is None or self.uow_factory is None:
            raise ToolClientUnavailableError()
        actor = ActorContext(context.actor_id, context.user_id)
        items = self.items(context.tool_run_id)
        if not items:
            run = self._owner(actor.actor_id)
            names = {a["attachment_id"]: a["name"] for a in run.attachments} if run else {}
            with self.uow_factory() as uow:
                for ordinal, identity in enumerate(validated_input.input_assets["image_asset_ids"]):
                    asset = uow.assets.get_owned(identity, actor.actor_id)
                    if asset is None or asset.conversation_id != context.conversation_id or not asset.sha256:
                        raise ToolClientProtocolError()
                    stable = sha256((context.tool_run_id + ":" + str(ordinal)).encode()).hexdigest()
                    item = ToolRunItem(item_id="tc4item_" + stable[:40], request_id="tc4req_" + stable,
                        tool_run_id=context.tool_run_id, actor_id=actor.actor_id, ordinal=ordinal,
                        input_asset_id=identity, input_sha256=asset.sha256, name=names.get(identity, f"图片 {ordinal + 1}"),
                        created_at=now(), updated_at=now())
                    uow.tool_run_items.add(item)
                uow.commit()
            items = self.items(context.tool_run_id)
        if [item.input_asset_id for item in items] != validated_input.input_assets["image_asset_ids"]:
            raise ToolClientProtocolError()
        for item in items:
            if item.status in {"SUCCEEDED", "FAILED", "SKIPPED"}:
                continue
            request = self.request(context, item)
            if item.status in {"DISPATCHED", "OUTCOME_UNKNOWN", "RESULT_RECEIVED"}:
                receipt = item.receipt if item.status == "RESULT_RECEIVED" else self.lookup(request)
                self._receive(actor, context, item, receipt)
                continue
            if not self._can_dispatch(actor.actor_id):
                self.update(item, status="SKIPPED", error={"code": "USER_STOPPED", "message": "停止后未执行。"})
                continue
            try:
                asset = require_asset(self.asset_service, actor, context.conversation_id, item.input_asset_id)
                if asset.sha256 != item.input_sha256:
                    raise ApplicationConflictError()
                content = self.asset_service.get_content(actor, asset.asset_id)
            except DependencyUnavailableError:
                raise ToolClientUnavailableError(outcome_unknown=True) from None
            except ApplicationError:
                self.update(item, status="FAILED", error={"code": "INPUT_UNAVAILABLE", "message": "输入图片不可用。"})
                continue
            item = self.update(item, status="DISPATCHED")
            try:
                status, payload = self._http("POST", "/internal/v1/tc4/execute", body=content.payload, request=request)
                if status != 200:
                    # These explicit pre-inference rejections permit a later user resume.
                    error = json.loads(payload).get("error", {}).get("code")
                    if status == 503 and error in {"RUNTIME_BUSY", "RUNTIME_NOT_READY", "MODEL_LOAD_FAILED"}:
                        self.update(item, status="NOT_DISPATCHED")
                        raise ToolClientUnavailableError(outcome_unknown=True)
                    raise ToolClientProtocolError(outcome_unknown=True)
                receipt = self.validate_receipt(json.loads(payload), request)
            except (ToolClientError, ValueError, TypeError):
                if self.items(context.tool_run_id)[item.ordinal].status == "NOT_DISPATCHED":
                    raise ToolClientUnavailableError(outcome_unknown=True) from None
                receipt = self.lookup(request)
            self._receive(actor, context, item, receipt)
        items = self.items(context.tool_run_id)
        data = {"items": [item.public() for item in items]}
        status = batch_status(data["items"])
        return ToolExecutionOutput(status, OUTPUTS, OUTPUTS if status != "FAILED" else (),
            OUTPUTS if status != "SUCCEEDED" else (), data, (),
            ({"code": "MOCK_RESULT", "message": "Mock 模拟结果，不是真实分割预测。"},) if self.model_version == MOCK_MODEL_VERSION else (),
            (), dict(validated_input.runtime_parameters), self.model_version,
            None if status == "SUCCEEDED" else {"code": "TC4_BATCH_INCOMPLETE", "safe_message": "部分图片未完成，请查看逐图状态。", "retryable": False})
