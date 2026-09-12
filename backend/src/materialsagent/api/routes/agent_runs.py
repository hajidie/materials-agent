from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator, create_model

from materialsagent.api.dependencies import get_actor_context
from materialsagent.application.context import ActorContext
from materialsagent.application.idempotency import validate_idempotency_key
from materialsagent.domain.models.agent import AgentRun
from materialsagent.domain.ports.agent import AgentConflictError, AgentFailure

router = APIRouter(tags=["agent-runs"])



AgentRunView = create_model("AgentRunView", __config__=ConfigDict(extra="forbid"), **{
    name: (field.annotation, field) for name, field in AgentRun.model_fields.items()
    if name not in {"actor_id", "claim", "process_id", "context", "accepted_waiting_version", "accepted_input_hash"}
})

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
    return run.model_dump(mode="json", exclude={"actor_id", "claim", "process_id", "context", "accepted_waiting_version", "accepted_input_hash"})


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
        budget=request.app.state.agent_budget)
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
