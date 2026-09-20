from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator, create_model

from materialsagent.api.dependencies import get_actor_context
from materialsagent.application.context import ActorContext
from materialsagent.application.idempotency import validate_idempotency_key
from materialsagent.domain.models.agent import AgentRun
from materialsagent.domain.models.attachment import Attachment
from materialsagent.domain.ports.agent import AgentConflictError, AgentFailure

router = APIRouter(tags=["agent-runs"])



class AgentRunView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_run_id: str
    conversation_id: str
    source_message_id: str
    goal: str
    status: str
    version: int
    waiting_version: int
    waiting: dict | None
    pending_execution: dict | None
    executions: list[dict]
    observations: list[dict]
    final_answer: dict | None
    error_message: str | None
    outcome_unknown: bool
    user_inputs: list[str]
    user_messages: list[dict]
    attachments: list[dict]
    result_attachments: list[dict]
    created_at: str


class RunData(BaseModel):
    agent_run: AgentRunView
    idempotency_replayed: bool

class SubmissionResponse(BaseModel):
    request_id: str
    data: RunData

class RunResponse(BaseModel):
    request_id: str
    data: AgentRunView


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["NEW_RUN", "RESUME_RUN"]
    content_text: str = Field(min_length=1, max_length=32768)
    attachments: list[Attachment] = Field(default_factory=list, max_length=1)
    agent_run_id: str | None = None
    waiting_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def valid_target(self):
        if not self.content_text.strip():
            raise ValueError("Message cannot be blank.")
        if self.mode == "RESUME_RUN" and (not self.agent_run_id or self.waiting_version is None):
            raise ValueError("Resume requires a Run and waiting version.")
        if self.mode == "NEW_RUN" and (self.agent_run_id is not None or self.waiting_version is not None):
            raise ValueError("New Run cannot target an existing Run.")
        return self


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    waiting_version: int = Field(ge=1)
    confirmation_version: str = Field(min_length=1)


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_type: Literal["TOOL_RETRY", "ANSWER_REGENERATION"]
    invocation_run_id: str | None = None


def runtime_for(request):
    runtime = getattr(request.app.state, "agent_runtime", None)
    if runtime is None:
        raise HTTPException(503, detail="AGENT_RUNTIME_UNAVAILABLE")
    return runtime


def public(run: AgentRun):
    from materialsagent.application.result_projection import project_result, FIELD_LABELS
    from materialsagent.application.context_framework import protect_text, internal_values
    private = internal_values(run.model_dump(mode="json"))
    def execution(record):
        if record is None:
            return None
        facts = []
        for key, value in record.arguments.items():
            if key in record.resource_bindings:
                value = record.resource_bindings[key].safe_description.get("name", "已确认的输入")
            elif key.endswith("_id"):
                value = "已上传的附件"
            if key == "algorithm":
                value = {"LR": "线性回归", "RF": "随机森林"}.get(value, value)
            facts.append({"label": FIELD_LABELS.get(key, key), "value": protect_text(value, private)})
        from materialsagent.application.unit_resolution import project_units
        unit_notes = project_units({"notes": []}, record.unit_annotations)["notes"]
        if unit_notes:
            facts.append({"label": "单位说明（执行确认不等于单位确认）", "value": protect_text(unit_notes, private)})
        return {"invocation_run_id": record.invocation_run_id, "tool_name": record.tool_name,
            "status": record.status, "retryable": record.retryable, "confirmation_version": record.confirmation_version,
            "confirmation_expires_at": record.confirmation_expires_at.isoformat() if record.confirmation_expires_at else None,
            "confirmation": facts}
    value = {key: getattr(run, key) for key in ("agent_run_id", "conversation_id", "source_message_id", "goal", "status", "version",
        "waiting_version", "user_inputs", "user_messages", "attachments", "result_attachments")}
    value.update(created_at=run.created_at.isoformat(),
        waiting={"reason": run.waiting.reason, "question": protect_text(run.waiting.question, private)} if run.waiting else None,
        pending_execution=execution(run.pending_execution), executions=[execution(e) for e in run.executions],
        observations=[{"observation_id": o.observation_id, "kind": o.kind, "tool_name": o.tool_name,
            "status": o.status, "presentation": protect_text(project_result(o), private),
            "artifacts": [{"attachment_id": a["asset_id"], "kind": "image", "name": "结果图片"} for a in o.artifacts if a.get("asset_id")]
            } for o in run.observations if o.kind == "TOOL_RESULT"],
        final_answer={"text": protect_text(run.final_answer.text, private), "answer_id": run.final_answer.answer_id} if run.final_answer else None,
        outcome_unknown=run.error_code == "MCP_OUTCOME_UNKNOWN" or any(e.status == "OUTCOME_UNKNOWN" for e in run.executions),
        error_message={
            "CONTEXT_BUDGET_EXCEEDED": "本次请求所需的上下文超出处理上限，未能继续。已上传的附件和已保存的结果仍保留。",
            "LLM_TOKEN_BUDGET_EXCEEDED": "本次处理已达到推理额度上限，未能继续。已上传的附件和已保存的结果仍保留。",
        }.get(run.error_code, "本次处理未完成，已保存的结果仍可查看。") if run.error_code else None)
    if run.error_code == "TOOL_EXECUTION_FAILED" and any(o.kind == "TOOL_RESULT" and o.status == "FAILED" for o in run.observations):
        # The projected failed result already explains this same failure.
        value["error_message"] = None
    return value


def key_value(value):
    try:
        return validate_idempotency_key(value)
    except (TypeError, ValueError):
        raise HTTPException(422, detail="INVALID_IDEMPOTENCY_KEY") from None


@router.post("/api/v1/conversations/{conversation_id}/messages", response_model=SubmissionResponse)
def submit(conversation_id: str, body: Submission, request: Request,
           actor: Annotated[ActorContext, Depends(get_actor_context)],
           idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    runtime = runtime_for(request)
    run, replayed = runtime.store.submit(conversation_id, actor.actor_id, body.content_text,
        key_value(idempotency_key), run_id=body.agent_run_id, waiting_version=body.waiting_version,
        budget=request.app.state.agent_budget, attachments=[a.model_dump() for a in body.attachments])
    if run.status == "PENDING" or (body.mode == "RESUME_RUN" and run.status == "WAITING_FOR_USER" and run.waiting_version == body.waiting_version):
        run = runtime.advance(run.agent_run_id, actor.actor_id, waiting_version=body.waiting_version,
            user_input=body.content_text if body.mode == "RESUME_RUN" else None)
    return {"request_id": request.state.request_id, "data": {"agent_run": public(run), "idempotency_replayed": replayed}}


@router.get("/api/v1/agent-runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return {"request_id": request.state.request_id, "data": public(runtime_for(request).store.get(run_id, actor.actor_id))}


@router.get("/api/v1/conversations/{conversation_id}/agent-runs")
def list_runs(conversation_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)],
              limit: Annotated[int, Query(ge=1, le=100)] = 20, before: str | None = None):
    runs = runtime_for(request).store.list(conversation_id, actor.actor_id, limit=limit, before=before)
    return {"request_id": request.state.request_id, "data": {"items": [public(r) for r in runs],
        "next_cursor": runs[-1].agent_run_id if len(runs) == limit else None}}


@router.get("/api/v1/agent-runs/{run_id}/trace")
def trace(run_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)],
          offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=100)] = 20):
    if not getattr(request.app.state, "m5_dev_routes_enabled", False):
        raise HTTPException(404, detail="NOT_FOUND")
    run = runtime_for(request).store.get(run_id, actor.actor_id)
    steps = run.steps[offset:offset + limit]
    ids = {s.step_id for s in steps}
    return {"request_id": request.state.request_id, "data": {
        "steps": [s.model_dump(mode="json") for s in steps],
        "observations": [o.model_dump(mode="json") for o in run.observations if o.step_id in ids],
        "model_calls": [c.model_dump(mode="json") for c in run.calls if c.step_id in ids],
        "next_offset": offset + limit if offset + limit < len(run.steps) else None}}


def _confirmation(run_id, invocation_id, body, request, actor, approved):
    runtime = runtime_for(request)
    run = runtime.store.get(run_id, actor.actor_id)
    pending = run.pending_execution
    if pending is None or pending.invocation_run_id != invocation_id or pending.confirmation_version != body.confirmation_version:
        # Same immutable invocation remains identifiable after a successful confirmation.
        recorded = next((e for e in run.executions if e.invocation_run_id == invocation_id and e.confirmation_version == body.confirmation_version), None)
        if recorded and approved:
            return {"request_id": request.state.request_id, "data": public(run)}
        raise AgentConflictError("Confirmation target changed.")
    run = runtime.advance(run_id, actor.actor_id, waiting_version=body.waiting_version, confirmation=approved)
    return {"request_id": request.state.request_id, "data": public(run)}


@router.post("/api/v1/agent-runs/{run_id}/invocations/{invocation_id}/confirm")
def confirm(run_id: str, invocation_id: str, body: Confirmation, request: Request,
            actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return _confirmation(run_id, invocation_id, body, request, actor, True)


@router.post("/api/v1/agent-runs/{run_id}/invocations/{invocation_id}/reject")
def reject(run_id: str, invocation_id: str, body: Confirmation, request: Request,
           actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return _confirmation(run_id, invocation_id, body, request, actor, False)


def receipt_target(run_id, invocation_id, request, actor):
    run = runtime_for(request).store.get(run_id, actor.actor_id)
    records = [*run.executions, *([run.pending_execution] if run.pending_execution else [])]
    if not any(r.invocation_run_id == invocation_id for r in records):
        raise HTTPException(404, detail="INVOCATION_NOT_FOUND")
    service = request.app.state.invocation_service
    value = service.get(actor, invocation_id)
    if value.run.conversation_id != run.conversation_id or value.run.executor_id != "mcp":
        raise HTTPException(404, detail="INVOCATION_NOT_FOUND")
    return service, value


@router.get("/api/v1/agent-runs/{run_id}/invocations/{invocation_id}/receipt")
def get_receipt(run_id: str, invocation_id: str, request: Request,
                actor: Annotated[ActorContext, Depends(get_actor_context)]):
    _, value = receipt_target(run_id, invocation_id, request, actor)
    return {"request_id": request.state.request_id, "data": {"invocation_run_id": invocation_id,
        "status": value.run.status.value, "remote_receipt": _receipt_json(value.run.remote_receipt)}}


def _receipt_json(value):
    from materialsagent.application.tool_invocations import _plain_json
    return _plain_json(value)


@router.post("/api/v1/agent-runs/{run_id}/invocations/{invocation_id}/reconcile")
def reconcile_receipt(run_id: str, invocation_id: str, request: Request,
                      actor: Annotated[ActorContext, Depends(get_actor_context)]):
    service, _ = receipt_target(run_id, invocation_id, request, actor)
    value = service.reconcile_mcp(actor, invocation_id)
    registration = None
    registrar = getattr(runtime_for(request).tools, "resource_registrar", None)
    if registrar:
        run = runtime_for(request).store.get(run_id, actor.actor_id)
        record = next(r for r in [*run.executions, *([run.pending_execution] if run.pending_execution else [])]
                      if r.invocation_run_id == invocation_id)
        registration = registrar.explicit_reconcile(run, record)
    return {"request_id": request.state.request_id, "data": {"invocation_run_id": invocation_id,
        "status": value.run.status.value, "remote_receipt": _receipt_json(value.run.remote_receipt),
        **({"registration": registration} if registrar else {})}}


@router.post("/api/v1/agent-runs/{run_id}/resources/reconcile")
def reconcile_resources(run_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    runtime = runtime_for(request)
    registrar = getattr(runtime.tools, "resource_registrar", None)
    if registrar is None:
        raise HTTPException(404, detail="RESOURCE_CONTEXT_DISABLED")
    run = runtime.store.get(run_id, actor.actor_id)
    known = [record for record in run.executions if request.app.state.invocation_service.get(
        actor, record.invocation_run_id).run.status.value in ("SUCCEEDED", "FAILED")]
    return {"data": [registrar.explicit_reconcile(run, record) for record in known]}


@router.post("/api/v1/agent-runs/{run_id}/retry", response_model=SubmissionResponse)
def retry(run_id: str, body: RetryRequest, request: Request,
          actor: Annotated[ActorContext, Depends(get_actor_context)],
          idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    runtime = runtime_for(request)
    source = runtime.store.get(run_id, actor.actor_id)
    if not source.terminal:
        raise AgentConflictError("Only terminal Runs can be retried.")
    if body.retry_type == "ANSWER_REGENERATION":
        if body.invocation_run_id or not any(o.kind == "TOOL_RESULT" and o.status == "SUCCEEDED" for o in source.observations):
            raise AgentFailure("ANSWER_REGENERATION_NOT_ALLOWED")
    else:
        execution = next((e for e in source.executions if e.invocation_run_id == body.invocation_run_id), None)
        if execution is None or execution.status != "FAILED" or not execution.retryable:
            raise AgentFailure("TOOL_RETRY_NOT_ALLOWED")
    run, replayed = runtime.store.submit(source.conversation_id, actor.actor_id, source.goal, key_value(idempotency_key),
        budget=request.app.state.agent_budget, retry_source=source, retry_type=body.retry_type,
        retry_invocation_id=body.invocation_run_id)
    if run.status == "PENDING":
        run = runtime.advance(run.agent_run_id, actor.actor_id)
    return {"request_id": request.state.request_id, "data": {"agent_run": public(run), "idempotency_replayed": replayed}}
