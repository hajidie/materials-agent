from __future__ import annotations

import asyncio
import json
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator, create_model

from materialsagent.api.dependencies import get_actor_context
from materialsagent.application.context import ActorContext
from materialsagent.application.idempotency import validate_idempotency_key
from materialsagent.domain.models.agent import AgentRun
from materialsagent.domain.models.attachment import Attachment
from materialsagent.domain.ports.agent import AgentConflictError, AgentFailure
from materialsagent.application.reliability import recovery_action
from materialsagent.domain.models.agent import now

router = APIRouter(tags=["agent-runs"])



class AgentRunView(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agent_run_id: str
    conversation_id: str
    source_message_id: str
    source_answer_message_id: str | None
    answer_root_message_id: str | None
    goal: str
    status: str
    version: int
    waiting_version: int
    submission_id: str | None
    question_message_id: str | None
    final_message_id: str | None
    stopped: bool
    waiting: dict | None
    pending_execution: dict | None
    executions: list[dict]
    observations: list[dict]
    error_message: str | None
    outcome_unknown: bool
    can_resume: bool = False
    resume_after: str | None = None
    recovery_action: Literal["CONTINUE", "RECONCILE", "FIX_CONFIGURATION", "NONE"] = "NONE"
    attachments: list[dict]
    result_attachments: list[dict]
    segmentation_items: list[dict] = Field(default_factory=list)
    created_at: str


class RunData(BaseModel):
    agent_run: AgentRunView
    idempotency_replayed: bool
    submission_id: str

class SubmissionResponse(BaseModel):
    request_id: str
    data: RunData

class RunResponse(BaseModel):
    request_id: str
    data: AgentRunView


class ReplyTo(BaseModel):
    model_config = ConfigDict(extra="forbid")
    question_message_id: str = Field(min_length=1)
    waiting_version: int = Field(ge=1)


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content_text: str = Field(min_length=1, max_length=32768)
    attachments: list[Attachment] = Field(default_factory=list, max_length=10)
    reply_to: ReplyTo | None = None

    @model_validator(mode="after")
    def valid_text(self):
        if not self.content_text.strip():
            raise ValueError("Message cannot be blank.")
        return self


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission_id: str = Field(min_length=1)


class ResumeRequest(TurnRequest):
    version: int = Field(ge=0)


class Confirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    waiting_version: int = Field(ge=1)
    confirmation_version: str = Field(min_length=1)


class RetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_type: Literal["TOOL_RETRY"]
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
        "waiting_version", "submission_id", "question_message_id", "final_message_id", "source_answer_message_id", "answer_root_message_id", "attachments", "result_attachments")}
    value.update(created_at=run.created_at.isoformat(),
        waiting={"question": protect_text(run.waiting.question, private)} if run.waiting else None,
        stopped=run.error_code == "USER_STOPPED",
        pending_execution=execution(run.pending_execution), executions=[execution(e) for e in run.executions],
        observations=[{"observation_id": o.observation_id, "kind": o.kind, "tool_name": o.tool_name,
            "status": o.status, "presentation": protect_text(project_result(o), private),
            "artifacts": [{"attachment_id": a["asset_id"], "kind": "image", "name": "结果图片"} for a in o.artifacts if a.get("asset_id")]
            } for o in run.observations if o.kind == "TOOL_RESULT"],
        outcome_unknown=run.error_code in {"MCP_OUTCOME_UNKNOWN", "TOOL_OUTCOME_UNKNOWN"} or any(
            e.status == "OUTCOME_UNKNOWN" for e in [*run.executions, *([run.pending_execution] if run.pending_execution else [])]),
        recovery_action=recovery_action(run),
        resume_after=run.retry_not_before.isoformat() if run.retry_not_before else None,
        can_resume=recovery_action(run) in {"CONTINUE", "FIX_CONFIGURATION"} and (
            run.retry_not_before is None or now() >= run.retry_not_before),
        error_message={
            "PROCESS_INTERRUPTED": "服务重启后任务已暂停。可恢复处理，已保存的过程仍保留。",
            "USER_STOPPED": "已停止生成，已保存的内容仍保留。",
            "LLM_TEMPORARILY_UNAVAILABLE": "模型服务暂时不可用，自动重试已结束。可继续处理，已完成的结果会复用。",
            "LLM_RECOVERY_WINDOW_EXCEEDED": "模型请求等待超时，任务已暂停。可继续处理，累计额度不会重置。",
            "LLM_CONFIGURATION_REQUIRED": "模型鉴权、额度或配置需要修正。修正后可继续处理。",
            "LLM_RETRY_AFTER": "模型服务要求稍后再试，任务已暂停。等待结束后可继续处理。",
            "DEPENDENCY_UNAVAILABLE": "保存服务暂时不可用，任务已暂停。服务恢复后可继续处理。",
            "CONTEXT_BUDGET_EXCEEDED": "本次请求所需的上下文超出处理上限，未能继续。已上传的附件和已保存的结果仍保留。",
            "LLM_TOKEN_BUDGET_EXCEEDED": "本次处理已达到推理额度上限，未能继续。已上传的附件和已保存的结果仍保留。",
        }.get(run.error_code, "本次处理未完成，已保存的结果仍可查看。") if run.error_code else None)
    if run.error_code == "TOOL_EXECUTION_FAILED" and any(o.kind == "TOOL_RESULT" and o.status == "FAILED" for o in run.observations):
        # The projected failed result already explains this same failure.
        value["error_message"] = None
    value["segmentation_items"] = run.segmentation_items
    if run.status == "INTERRUPTED" and run.pending_execution and run.pending_execution.tool_name == "tc4_primary_alpha_segmentation":
        value["recovery_action"] = "CONTINUE"
        value["can_resume"] = run.active_seconds < run.budget.max_active_seconds and run.llm_tokens < run.budget.max_llm_tokens
        if run.segmentation_items and not any(item["status"] in {"DISPATCHED", "OUTCOME_UNKNOWN"} for item in run.segmentation_items):
            value["outcome_unknown"] = False
            value["error_message"] = "本批图片处理已暂停，已完成结果仍保留。可以继续处理。"
    return value


def key_value(value):
    try:
        return validate_idempotency_key(value)
    except (TypeError, ValueError):
        raise HTTPException(422, detail="INVALID_IDEMPOTENCY_KEY") from None


def accepted(run, replayed, request):
    return {"request_id": request.state.request_id, "data": {"agent_run": public(run),
        "submission_id": run.accepted_submission_id or run.submission_id, "idempotency_replayed": replayed}}


@router.post("/api/v1/conversations/{conversation_id}/messages", response_model=SubmissionResponse, status_code=202)
async def submit(conversation_id: str, body: Submission, request: Request,
           actor: Annotated[ActorContext, Depends(get_actor_context)],
           idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    runtime = runtime_for(request)
    target = None
    if body.reply_to:
        from materialsagent.infrastructure.db.conversation_task import MessageRow
        with runtime.store.sessions() as session:
            message = session.get(MessageRow, body.reply_to.question_message_id)
            if not message or message.actor_id != actor.actor_id or message.conversation_id != conversation_id or message.phase != "question":
                raise AgentFailure("MESSAGE_NOT_FOUND")
            target = message.agent_run_id
    run, replayed = await runtime.submit(conversation_id, actor.actor_id, body.content_text, key_value(idempotency_key),
        run_id=target, waiting_version=body.reply_to.waiting_version if body.reply_to else None,
        question_message_id=body.reply_to.question_message_id if body.reply_to else None,
        budget=request.app.state.agent_budget, attachments=[a.model_dump() for a in body.attachments])
    response = accepted(run, replayed, request)
    return response


@router.post("/api/v1/agent-runs/{run_id}/resume", response_model=RunResponse, status_code=202)
async def resume(run_id: str, body: ResumeRequest, request: Request,
                  actor: Annotated[ActorContext, Depends(get_actor_context)]):
    run = await runtime_for(request).resume(run_id, actor.actor_id, body.submission_id, body.version)
    return {"request_id": request.state.request_id, "data": public(run)}


@router.get("/api/v1/agent-runs/{run_id}/process")
async def process_snapshot(run_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    snapshot = await runtime_for(request).process.snapshot(run_id, actor.actor_id)
    snapshot["run"] = public(snapshot["run"])
    return {"data": snapshot}


@router.get("/api/v1/agent-runs/{run_id}/events")
async def events(run_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)]):
    runtime = runtime_for(request)
    # Authenticate before starting the response, including cached channels.
    await asyncio.to_thread(runtime.store.get, run_id, actor.actor_id)
    channel = await runtime.process.channel(run_id, actor.actor_id)

    async def stream():
        queue: asyncio.Queue = asyncio.Queue(maxsize=1)
        channel.listeners.add(queue)  # Subscribe before the initial snapshot.
        first = True
        revisions: dict[str, int] = {}
        last_version = -1
        serial = 0
        try:
            while True:
                if await request.is_disconnected():
                    return
                snapshot = await runtime.process.snapshot(run_id, actor.actor_id)
                run = snapshot.pop("run")
                changed = [s for s in snapshot["segments"] if s["revision"] > revisions.get(s["segment_id"], -1)]
                if first or changed or run.version > last_version:
                    value = {**snapshot, "segments": snapshot["segments"] if first else changed, "run": public(run)}
                    serial += 1
                    event = "snapshot" if first else "process.updated"
                    yield f"id: {channel.epoch}:{serial}\nevent: {event}\ndata: {json.dumps(value, ensure_ascii=False)}\n\n"
                    revisions.update({s["segment_id"]: s["revision"] for s in snapshot["segments"]})
                    last_version, first = run.version, False
                active = any(k[0] == run_id and not task.done() for k, task in runtime.running_tasks.items())
                if run.status not in {"PENDING", "RUNNING"} and not active:
                    yield "event: settled\ndata: {}\n\n"
                    return
                try:
                    await asyncio.wait_for(queue.get(), timeout=10)
                    await asyncio.sleep(0.04)  # Coalesce deltas; never hold the producer.
                except TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            channel.listeners.discard(queue)
            runtime.process.evict()
    return StreamingResponse(stream(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


@router.post("/api/v1/agent-runs/{run_id}/stop")
async def stop(run_id: str, body: TurnRequest, request: Request,
               actor: Annotated[ActorContext, Depends(get_actor_context)]):
    run, stopped = await runtime_for(request).stop(run_id, actor.actor_id, body.submission_id)
    return {"request_id": request.state.request_id, "data": {"agent_run": public(run),
        "outcome": "stopped" if stopped else "already_completed"}}


@router.get("/api/v1/conversations/{conversation_id}/messages")
def messages(conversation_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)],
             limit: Annotated[int, Query(ge=1, le=100)] = 50, before: Annotated[int | None, Query(ge=1)] = None):
    return {"data": runtime_for(request).store.messages(conversation_id, actor.actor_id, before=before, limit=limit)}


@router.get("/api/v1/messages/{message_id}/versions")
def versions(message_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)],
             limit: Annotated[int, Query(ge=1, le=100)] = 20, before: Annotated[int | None, Query(ge=1)] = None):
    return {"data": runtime_for(request).store.versions(message_id, actor.actor_id, before=before, limit=limit)}


@router.post("/api/v1/messages/{message_id}/regenerate", response_model=SubmissionResponse, status_code=202)
async def regenerate(message_id: str, request: Request, actor: Annotated[ActorContext, Depends(get_actor_context)],
               idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    runtime = runtime_for(request)
    source = runtime.store.answer_run(message_id, actor.actor_id)
    run, replayed = await runtime.submit(source.conversation_id, actor.actor_id, source.goal, key_value(idempotency_key),
        budget=request.app.state.agent_budget, retry_source=source, retry_type="ANSWER_REGENERATION",
        source_answer_message_id=message_id)
    response = accepted(run, replayed, request)
    return response


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
    records = [
        {"kind": "model_call", "created_at": call.created_at.isoformat(),
         "record": {"call_id": call.call_id, "status": call.status,
                    "usage": call.usage.model_dump(mode="json") if call.usage else None,
                    "error_code": call.error_code}}
        for call in run.calls
    ] + [
        {"kind": "tool_call", "created_at": execution.created_at.isoformat(),
         "record": {"tool_call_id": execution.tool_call_id,
                    "tool_name": execution.tool_name, "status": execution.status,
                    "invocation_run_id": execution.invocation_run_id}}
        for execution in [*run.executions, *([run.pending_execution] if run.pending_execution else [])]
    ] + [
        {"kind": "observation", "created_at": observation.created_at.isoformat(),
         "record": {"observation_id": observation.observation_id,
                    "tool_call_id": observation.tool_call_id,
                    "tool_name": observation.tool_name, "status": observation.status,
                    "invocation_run_id": observation.invocation_run_id}}
        for observation in run.observations
    ]
    records.sort(key=lambda item: item["created_at"])
    return {"request_id": request.state.request_id, "data": {
        "events": records[offset:offset + limit],
        "next_offset": offset + limit if offset + limit < len(records) else None}}


async def _confirmation(run_id, invocation_id, body, request, actor, approved):
    runtime = runtime_for(request)
    run = runtime.store.get(run_id, actor.actor_id)
    pending = run.pending_execution
    if pending is None or pending.invocation_run_id != invocation_id or pending.confirmation_version != body.confirmation_version:
        # Same immutable invocation remains identifiable after a successful confirmation.
        recorded = next((e for e in run.executions if e.invocation_run_id == invocation_id and e.confirmation_version == body.confirmation_version), None)
        if recorded and approved:
            return {"request_id": request.state.request_id, "data": public(run)}
        raise AgentConflictError("Confirmation target changed.")
    run = await runtime.accept_confirmation(run_id, actor.actor_id,
        body.waiting_version, body.confirmation_version, approved)
    return {"request_id": request.state.request_id, "data": public(run)}


@router.post("/api/v1/agent-runs/{run_id}/invocations/{invocation_id}/confirm")
async def confirm(run_id: str, invocation_id: str, body: Confirmation, request: Request,
            actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return await _confirmation(run_id, invocation_id, body, request, actor, True)


@router.post("/api/v1/agent-runs/{run_id}/invocations/{invocation_id}/reject")
async def reject(run_id: str, invocation_id: str, body: Confirmation, request: Request,
           actor: Annotated[ActorContext, Depends(get_actor_context)]):
    return await _confirmation(run_id, invocation_id, body, request, actor, False)


def receipt_target(run_id, invocation_id, request, actor):
    run = runtime_for(request).store.get(run_id, actor.actor_id)
    records = [*run.executions, *([run.pending_execution] if run.pending_execution else [])]
    if not any(r.invocation_run_id == invocation_id for r in records):
        raise HTTPException(404, detail="INVOCATION_NOT_FOUND")
    service = request.app.state.invocation_service
    value = service.get(actor, invocation_id)
    if value.run.conversation_id != run.conversation_id:
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
async def reconcile_receipt(run_id: str, invocation_id: str, request: Request,
                      actor: Annotated[ActorContext, Depends(get_actor_context)]):
    service, value = await asyncio.to_thread(receipt_target, run_id, invocation_id, request, actor)
    if value.run.executor_id == "mcp" and value.run.remote_operation and value.run.status.value == "OUTCOME_UNKNOWN":
        value = await asyncio.to_thread(service.reconcile_mcp, actor, invocation_id)
    runtime = runtime_for(request)
    run = await asyncio.to_thread(runtime.store.get, run_id, actor.actor_id)
    record = next(r for r in [*run.executions, *([run.pending_execution] if run.pending_execution else [])]
                  if r.invocation_run_id == invocation_id)
    if run.pending_execution and run.pending_execution.invocation_run_id == invocation_id:
        from materialsagent.application.reliability import unknown_observation
        if (run.status == "TERMINATED" and run.error_code == "USER_STOPPED"
                and record.tool_name == "tc4_primary_alpha_segmentation" and run.segmentation_items):
            # Explicit receipt reconciliation may save the already-dispatched
            # image. The persisted stop prevents dispatching any remaining item.
            observation = await asyncio.to_thread(runtime.tools.continue_batch, run, record, 60)
        else:
            observation = await asyncio.to_thread(runtime.tools.repair, run, record)
        if not unknown_observation(observation):
            run = await asyncio.to_thread(runtime.store.receipt, run_id, actor.actor_id, record, observation)
            await runtime.process.tools(run)
            runtime.process.changed(run_id)
            value = await asyncio.to_thread(service.get, actor, invocation_id)
    registration = None
    registrar = getattr(runtime_for(request).tools, "resource_registrar", None)
    if registrar:
        run = await asyncio.to_thread(runtime.store.get, run_id, actor.actor_id)
        record = next(r for r in [*run.executions, *([run.pending_execution] if run.pending_execution else [])]
                      if r.invocation_run_id == invocation_id)
        registration = await asyncio.to_thread(registrar.explicit_reconcile, run, record)
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


@router.post("/api/v1/agent-runs/{run_id}/retry", response_model=SubmissionResponse, status_code=202)
async def retry(run_id: str, body: RetryRequest, request: Request,
          actor: Annotated[ActorContext, Depends(get_actor_context)],
          idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None):
    runtime = runtime_for(request)
    source = runtime.store.get(run_id, actor.actor_id)
    if not source.terminal:
        raise AgentConflictError("Only terminal Runs can be retried.")
    execution = next((e for e in source.executions if e.invocation_run_id == body.invocation_run_id), None)
    if execution is None or execution.status != "FAILED" or not execution.retryable:
        raise AgentFailure("TOOL_RETRY_NOT_ALLOWED")
    run, replayed = await runtime.submit(source.conversation_id, actor.actor_id, source.goal, key_value(idempotency_key),
        budget=request.app.state.agent_budget, retry_source=source, retry_type=body.retry_type,
        retry_invocation_id=body.invocation_run_id)
    response = accepted(run, replayed, request)
    return response
