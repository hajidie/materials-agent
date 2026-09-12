"""Short-transaction Agent persistence. External work never receives a Session."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from materialsagent.domain.models.agent import AgentRun, RunBudget, TokenUsage, fingerprint, identifier, now
from materialsagent.domain.ports.agent import AgentConflictError, AgentFailure
from materialsagent.infrastructure.db.base import Base
from materialsagent.infrastructure.db.conversation_task import ConversationRow, MessageRow

JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class AgentRunRow(Base):
    __tablename__ = "agent_run"
    __table_args__ = (
        CheckConstraint("version >= 0", name="ck_agent_run_version"),
        CheckConstraint("status IN ('PENDING','RUNNING','WAITING_FOR_USER','WAITING_FOR_CONFIRMATION','SUCCEEDED','TERMINATED')", name="ck_agent_run_status"),
        Index("ix_agent_run_conversation_created", "conversation_id", "created_at", "agent_run_id"),
    )
    agent_run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversation.conversation_id", ondelete="CASCADE"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("actor.actor_id", ondelete="RESTRICT"))
    source_message_id: Mapped[str] = mapped_column(ForeignKey("message.message_id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AgentSubmissionRow(Base):
    __tablename__ = "agent_submission"
    __table_args__ = (UniqueConstraint("conversation_id", "idempotency_key", name="uq_agent_submission_key"),)
    submission_id: Mapped[str] = mapped_column(Text, primary_key=True)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversation.conversation_id", ondelete="CASCADE"))
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"))
    message_id: Mapped[str] = mapped_column(ForeignKey("message.message_id", ondelete="CASCADE"))
    idempotency_key: Mapped[str] = mapped_column(Text)
    payload_hash: Mapped[str] = mapped_column(String(64))


class AgentStepRow(Base):
    __tablename__ = "agent_step"
    __table_args__ = (UniqueConstraint("agent_run_id", "number", name="uq_agent_step_number"),)
    step_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"))
    number: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


class AgentExecutionRow(Base):
    __tablename__ = "agent_execution"
    action_id: Mapped[str] = mapped_column(ForeignKey("agent_step.step_id", ondelete="CASCADE"), primary_key=True)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"))
    invocation_run_id: Mapped[str] = mapped_column(ForeignKey("invocation_run.invocation_run_id", ondelete="CASCADE"), unique=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


class AgentObservationRow(Base):
    __tablename__ = "agent_observation"
    __table_args__ = (UniqueConstraint("agent_run_id", "invocation_run_id", name="uq_agent_observation_invocation"),)
    observation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"))
    step_id: Mapped[str] = mapped_column(ForeignKey("agent_step.step_id", ondelete="CASCADE"))
    invocation_run_id: Mapped[str | None] = mapped_column(ForeignKey("invocation_run.invocation_run_id", ondelete="CASCADE"), nullable=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


class AgentModelCallRow(Base):
    __tablename__ = "agent_model_call"
    call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"))
    step_id: Mapped[str] = mapped_column(ForeignKey("agent_step.step_id", ondelete="CASCADE"))
    document: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


class FinalAnswerRow(Base):
    __tablename__ = "agent_final_answer"
    answer_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), unique=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("agent_step.step_id", ondelete="CASCADE"))
    document: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


AGENT_TABLES = [AgentRunRow.__table__, AgentSubmissionRow.__table__, AgentStepRow.__table__,
                AgentObservationRow.__table__, AgentModelCallRow.__table__, FinalAnswerRow.__table__, AgentExecutionRow.__table__]


class SQLAlchemyAgentStore:
    def __init__(self, session_factory):
        self.sessions = session_factory

    def get(self, run_id: str, actor_id: str) -> AgentRun:
        with self.sessions() as session:
            row = session.get(AgentRunRow, run_id)
            if row is None or row.actor_id != actor_id:
                raise AgentFailure("AGENT_RUN_NOT_FOUND")
            return AgentRun.model_validate(row.document)

    def list(self, conversation_id: str, actor_id: str, *, limit: int = 20, before: str | None = None) -> list[AgentRun]:
        with self.sessions() as session:
            conversation = session.get(ConversationRow, conversation_id)
            if conversation is None or conversation.actor_id != actor_id:
                raise AgentFailure("CONVERSATION_NOT_FOUND")
            query = select(AgentRunRow).where(AgentRunRow.conversation_id == conversation_id, AgentRunRow.actor_id == actor_id)
            if before:
                boundary = session.get(AgentRunRow, before)
                if boundary is None or boundary.conversation_id != conversation_id or boundary.actor_id != actor_id:
                    raise AgentFailure("AGENT_RUN_NOT_FOUND")
                from sqlalchemy import tuple_
                query = query.where(tuple_(AgentRunRow.created_at, AgentRunRow.agent_run_id) < (boundary.created_at, before))
            rows = session.scalars(query.order_by(AgentRunRow.created_at.desc(), AgentRunRow.agent_run_id.desc()).limit(limit)).all()
            return [AgentRun.model_validate(row.document) for row in rows]

    def submit(self, conversation_id: str, actor_id: str, content: str, key: str, *,
               run_id: str | None = None, waiting_version: int | None = None,
               budget: RunBudget | None = None, retry_source: AgentRun | None = None,
               retry_type: str | None = None, retry_invocation_id: str | None = None) -> tuple[AgentRun, bool]:
        digest = fingerprint([content, run_id, waiting_version, retry_source.agent_run_id if retry_source else None, retry_type, retry_invocation_id])
        try:
            with self.sessions.begin() as session:
                conversation = session.scalar(select(ConversationRow).where(ConversationRow.conversation_id == conversation_id).with_for_update())
                if conversation is None or conversation.actor_id != actor_id:
                    raise AgentFailure("CONVERSATION_NOT_FOUND")
                existing = session.scalar(select(AgentSubmissionRow).where(AgentSubmissionRow.conversation_id == conversation_id, AgentSubmissionRow.idempotency_key == key))
                if existing:
                    if existing.payload_hash != digest:
                        raise AgentConflictError("Idempotency payload differs.")
                    row = session.get(AgentRunRow, existing.agent_run_id)
                    return AgentRun.model_validate(row.document), True
                if run_id:
                    row = session.get(AgentRunRow, run_id)
                    if row is None or row.actor_id != actor_id or row.conversation_id != conversation_id:
                        raise AgentFailure("AGENT_RUN_NOT_FOUND")
                    run = AgentRun.model_validate(row.document)
                    if run.status != "WAITING_FOR_USER" or run.waiting_version != waiting_version:
                        raise AgentConflictError("Run is not waiting at this version.")
                    if run.accepted_waiting_version == waiting_version:
                        if run.accepted_input_hash == digest:
                            return run, True
                        raise AgentConflictError("A response already owns this waiting version.")
                    # Serialize input acceptance with the message, before request-hosted advancement.
                    run.accepted_waiting_version = waiting_version
                    run.accepted_input_hash = digest
                    run.version += 1
                    row.version = run.version
                    row.document = run.model_dump(mode="json")
                message_id = identifier()
                session.add(MessageRow(message_id=message_id, conversation_id=conversation_id, actor_id=actor_id,
                    task_id=None, request_id=identifier(), role="USER", generation_source="USER", content_text=content,
                    structured_content=None, llm_call_id=None, created_at=now()))
                session.flush()
                if not run_id:
                    if conversation.title is None:
                        conversation.title = content.strip().splitlines()[0][:60]
                    history = session.scalars(select(AgentRunRow).where(AgentRunRow.conversation_id == conversation_id,
                        AgentRunRow.actor_id == actor_id, AgentRunRow.status == "SUCCEEDED")
                        .order_by(AgentRunRow.created_at.desc(), AgentRunRow.agent_run_id.desc()).limit(10)).all()
                    context = []
                    for item in reversed(history):
                        previous = AgentRun.model_validate(item.document)
                        if previous.final_answer:
                            context.extend([{"role": "user", "content": previous.goal,
                                "additional_inputs": previous.user_inputs, "agent_run_id": previous.agent_run_id},
                                {"role": "assistant", "content": previous.final_answer.text,
                                "agent_run_id": previous.agent_run_id}])
                    run = AgentRun(conversation_id=conversation_id, actor_id=actor_id, source_message_id=message_id,
                        goal=content, budget=budget or RunBudget(), context=context)
                    if retry_source:
                        run.user_inputs = list(retry_source.user_inputs)
                        run.source_agent_run_id = retry_source.agent_run_id
                        run.retry_type = retry_type
                        if retry_type == "TOOL_RETRY":
                            target = next((e for e in retry_source.executions if e.invocation_run_id == retry_invocation_id), None)
                            if target is None or target.status != "FAILED" or not target.retryable:
                                raise AgentFailure("TOOL_RETRY_NOT_ALLOWED")
                            run.retry_execution = target.model_copy(deep=True)
                        if retry_type == "ANSWER_REGENERATION":
                            run.tool_execution_disabled = True
                            # Copy references as explicit imported observations; no fabricated tool execution.
                            run.observations = [o.model_copy(deep=True) for o in retry_source.observations if o.kind == "TOOL_RESULT" and o.status == "SUCCEEDED"]
                            for observation in run.observations:
                                observation.source_agent_run_id = retry_source.agent_run_id
                    session.add(AgentRunRow(agent_run_id=run.agent_run_id, conversation_id=conversation_id, actor_id=actor_id,
                        source_message_id=message_id, status=run.status, version=0, document=run.model_dump(mode="json"), created_at=run.created_at))
                    session.flush()
                session.add(AgentSubmissionRow(submission_id=identifier(), conversation_id=conversation_id,
                    agent_run_id=run.agent_run_id, message_id=message_id, idempotency_key=key, payload_hash=digest))
                conversation.updated_at = max(conversation.updated_at, now())
                return run, False
        except IntegrityError:
            with self.sessions() as session:
                existing = session.scalar(select(AgentSubmissionRow).where(AgentSubmissionRow.conversation_id == conversation_id, AgentSubmissionRow.idempotency_key == key))
                if existing and existing.payload_hash == digest:
                    return self.get(existing.agent_run_id, actor_id), True
            raise AgentConflictError("Concurrent submission conflict.") from None

    def save(self, run: AgentRun) -> None:
        run.last_completed_step = max((s.number for s in run.steps if s.status == "COMPLETED"), default=0)
        version = run.version
        with self.sessions.begin() as session:
            # Serialize deletion versus acquisition, only for this short commit.
            owner = session.scalar(select(ConversationRow).where(ConversationRow.conversation_id == run.conversation_id,
                ConversationRow.actor_id == run.actor_id).with_for_update())
            if owner is None:
                raise AgentConflictError("Conversation was deleted.")
            row = session.get(AgentRunRow, run.agent_run_id)
            if row is None or row.actor_id != run.actor_id or row.version != version:
                raise AgentConflictError("AgentRun version changed.")
            previous = AgentRun.model_validate(row.document)
            if previous.status == "RUNNING" and previous.claim != run.claim:
                raise AgentConflictError("AgentRun execution claim changed.")
            run.validate_transition(previous)
            document = run.model_dump(mode="json")
            document["version"] = version + 1
            count = session.execute(update(AgentRunRow).where(AgentRunRow.agent_run_id == run.agent_run_id,
                AgentRunRow.version == version, AgentRunRow.status == previous.status).values(
                    version=version + 1, status=run.status, document=document), execution_options={"synchronize_session": False}).rowcount
            if count != 1:
                raise AgentConflictError("AgentRun advance already claimed.")
            for step in run.steps:
                stored = session.get(AgentStepRow, step.step_id)
                if stored:
                    stored.document = step.model_dump(mode="json")
                else:
                    session.add(AgentStepRow(step_id=step.step_id, agent_run_id=run.agent_run_id, number=step.number, document=step.model_dump(mode="json")))
            session.flush()
            for execution in run.executions + ([run.pending_execution] if run.pending_execution else []):
                if not execution.invocation_run_id:
                    continue
                stored = session.get(AgentExecutionRow, execution.action_id)
                if stored:
                    if stored.invocation_run_id != execution.invocation_run_id:
                        raise AgentConflictError("Action Invocation cannot change.")
                    stored.document = execution.model_dump(mode="json")
                else:
                    session.add(AgentExecutionRow(action_id=execution.action_id, agent_run_id=run.agent_run_id,
                        invocation_run_id=execution.invocation_run_id, document=execution.model_dump(mode="json")))
            for observation in run.observations:
                if observation.source_agent_run_id:
                    continue  # Imported references remain owned by the original Run.
                if session.get(AgentObservationRow, observation.observation_id) is None:
                    session.add(AgentObservationRow(observation_id=observation.observation_id, agent_run_id=run.agent_run_id,
                        step_id=observation.step_id, invocation_run_id=observation.invocation_run_id, document=observation.model_dump(mode="json")))
            for call in run.calls:
                stored = session.get(AgentModelCallRow, call.call_id)
                if stored:
                    stored.document = call.model_dump(mode="json")
                else:
                    session.add(AgentModelCallRow(call_id=call.call_id, agent_run_id=run.agent_run_id,
                        step_id=call.step_id, document=call.model_dump(mode="json")))
            if run.final_answer and previous.final_answer is None:
                answer = run.final_answer
                session.add(FinalAnswerRow(answer_id=answer.answer_id, agent_run_id=run.agent_run_id,
                    step_id=answer.step_id, document=answer.model_dump(mode="json")))
                session.add(MessageRow(message_id=answer.answer_id, conversation_id=run.conversation_id, actor_id=run.actor_id,
                    task_id=None, request_id=run.agent_run_id, role="ASSISTANT", generation_source="AGENT",
                    content_text=answer.text, structured_content={"agent_run_id": run.agent_run_id}, llm_call_id=None, created_at=answer.created_at))
            owner.updated_at = max(owner.updated_at, run.updated_at)
        run.version = version + 1

    def recover_interrupted(self, process_id: str, *, repair=None) -> None:
        with self.sessions() as session:
            rows = session.scalars(select(AgentRunRow).where(AgentRunRow.status.in_(["PENDING", "RUNNING"]))).all()
            runs = [AgentRun.model_validate(row.document) for row in rows]
        for run in runs:
            if run.process_id != process_id:
                if run.status == "RUNNING":
                    run.active_seconds += max(0, (now() - run.updated_at).total_seconds())
                record = run.pending_execution
                if repair is not None and record and record.dispatched:
                    try:
                        observation = repair(run, record)
                        if observation is not None:
                            if observation.invocation_run_id != record.invocation_run_id:
                                raise AgentFailure("OBSERVATION_SOURCE_MISMATCH")
                            observation.step_id = record.action_id
                            if not any(o.invocation_run_id == record.invocation_run_id for o in run.observations):
                                run.observations.append(observation)
                            record.status = observation.status
                            record.task_id, record.tool_run_id = observation.task_id, observation.tool_run_id
                            record.observation_id = observation.observation_id
                            record.retryable = bool(observation.error and observation.error.get("retryable"))
                            run.executions.append(record.model_copy(deep=True))
                            run.pending_execution = None
                            for step in run.steps:
                                if step.step_id == record.action_id:
                                    step.status = "COMPLETED"
                    except Exception:
                        run.error_code = "OBSERVATION_INCONSISTENT"
                run.status, run.error_code = "TERMINATED", run.error_code or "PROCESS_INTERRUPTED"
                for call in run.calls:
                    if call.status == "RUNNING":
                        call.status, call.error_code = "FAILED", "PROCESS_INTERRUPTED"
                        call.usage = TokenUsage(input_tokens=call.input_reserved, output_tokens=call.output_limit,
                            total_tokens=call.input_reserved + call.output_limit, source="estimated",
                            estimator_version="cl100k-x2-or-utf8-framing-v1")
                        run.llm_tokens += call.usage.total_tokens
                for step in run.steps:
                    if step.status == "RUNNING":
                        step.status = "FAILED"
                try:
                    self.save(run)
                except AgentConflictError:
                    pass
