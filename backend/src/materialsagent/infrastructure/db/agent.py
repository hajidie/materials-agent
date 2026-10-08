"""Short-transaction Agent persistence. External work never receives a Session."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, and_, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, delete, select, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, mapped_column

from materialsagent.domain.models.agent import AgentRun, RunBudget, TokenUsage, Observation, fingerprint, identifier, now, tool_call_key
from materialsagent.domain.ports.agent import AgentConflictError, AgentFailure
from materialsagent.infrastructure.db.base import Base
from materialsagent.infrastructure.db.conversation_task import ConversationRow, MessageRow

JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class AgentRunRow(Base):
    __tablename__ = "agent_run"
    __table_args__ = (
        CheckConstraint("version >= 0", name="ck_agent_run_version"),
        CheckConstraint("status IN ('PENDING','RUNNING','WAITING_FOR_USER','WAITING_FOR_CONFIRMATION','INTERRUPTED','SUCCEEDED','TERMINATED')", name="ck_agent_run_status"),
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


class AgentExecutionRow(Base):
    __tablename__ = "agent_execution"
    tool_call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), primary_key=True)
    invocation_run_id: Mapped[str] = mapped_column(ForeignKey("invocation_run.invocation_run_id", ondelete="CASCADE"), unique=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


class AgentObservationRow(Base):
    __tablename__ = "agent_observation"
    __table_args__ = (UniqueConstraint("agent_run_id", "invocation_run_id", name="uq_agent_observation_invocation"),)
    observation_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"))
    tool_call_id: Mapped[str] = mapped_column(Text)
    invocation_run_id: Mapped[str | None] = mapped_column(ForeignKey("invocation_run.invocation_run_id", ondelete="CASCADE"), nullable=True)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


class AgentModelCallRow(Base):
    __tablename__ = "agent_model_call"
    call_id: Mapped[str] = mapped_column(Text, primary_key=True)
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"))
    document: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


class AgentCheckpointCleanupRow(Base):
    __tablename__ = "agent_checkpoint_cleanup"
    agent_run_id: Mapped[str] = mapped_column(Text, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)


class AgentProcessRow(Base):
    __tablename__ = "agent_process"
    agent_run_id: Mapped[str] = mapped_column(ForeignKey("agent_run.agent_run_id", ondelete="CASCADE"), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer)
    document: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE)


AGENT_TABLES = [AgentRunRow.__table__, AgentSubmissionRow.__table__,
                AgentObservationRow.__table__, AgentModelCallRow.__table__, AgentExecutionRow.__table__,
                AgentCheckpointCleanupRow.__table__, AgentProcessRow.__table__]


class SQLAlchemyAgentStore:
    def __init__(self, session_factory):
        self.sessions = session_factory

    def load_process(self, run_id, actor_id):
        with self.sessions() as session:
            run = session.get(AgentRunRow, run_id)
            if run is None or run.actor_id != actor_id:
                raise AgentFailure("AGENT_RUN_NOT_FOUND")
            row = session.get(AgentProcessRow, run_id)
            return row.document if row else {"revision": 0, "segments": []}

    def save_process(self, run_id, actor_id, document):
        with self.sessions.begin() as session:
            run = session.scalar(select(AgentRunRow).where(AgentRunRow.agent_run_id == run_id,
                AgentRunRow.actor_id == actor_id).with_for_update())
            if run is None:
                raise AgentFailure("AGENT_RUN_NOT_FOUND")
            row = session.get(AgentProcessRow, run_id)
            if row is None:
                session.add(AgentProcessRow(agent_run_id=run_id, revision=document["revision"], document=document))
            elif document["revision"] > row.revision:
                row.revision, row.document = document["revision"], document

    def resume(self, run_id, actor_id, submission_id, version, *, requested_version=None):
        requested_version = version if requested_version is None else requested_version
        from .ml_resources import lock_conversation
        with self.sessions.begin() as session:
            row = session.get(AgentRunRow, run_id)
            if row is None or row.actor_id != actor_id:
                raise AgentFailure("AGENT_RUN_NOT_FOUND")
            lock_conversation(session, actor_id, row.conversation_id, writable=True)
            row = session.scalar(select(AgentRunRow).where(AgentRunRow.agent_run_id == run_id)
                .with_for_update().execution_options(populate_existing=True))
            run = self._run(row.document)
            if run.submission_id != submission_id:
                raise AgentConflictError("Submission is stale.")
            if run.resumed_version == requested_version:
                return self._hydrate(session, run)
            if run.status != "INTERRUPTED" or run.version != version:
                raise AgentConflictError("Recovery version is stale.")
            run.status, run.error_code, run.resumed_version = "PENDING", None, requested_version
            run.recovery_replay = bool(run.calls or run.pending_tool_call_id)
            run.version += 1
            row.version, row.status, row.document = run.version, run.status, run.model_dump(mode="json")
            return self._hydrate(session, run)

    def accept_confirmation(self, run_id, actor_id, waiting_version, confirmation_version, approved):
        from .ml_resources import lock_conversation
        with self.sessions.begin() as session:
            row = session.get(AgentRunRow, run_id)
            if row is None or row.actor_id != actor_id:
                raise AgentFailure("AGENT_RUN_NOT_FOUND")
            lock_conversation(session, actor_id, row.conversation_id, writable=True)
            row = session.scalar(select(AgentRunRow).where(AgentRunRow.agent_run_id == run_id)
                .with_for_update().execution_options(populate_existing=True))
            run = self._run(row.document)
            record = run.pending_execution
            if (run.waiting_version != waiting_version or record is None
                    or record.confirmation_version != confirmation_version):
                raise AgentConflictError("Confirmation target changed.")
            decision = {"tool_call_id": record.tool_call_id,
                        "waiting_version": waiting_version, "approved": approved}
            # Applying approval clears confirmation_response before execution ends.
            if run.confirmation_response == decision or (approved and record.confirmed):
                return self._hydrate(session, run)
            if run.status != "WAITING_FOR_CONFIRMATION":
                raise AgentConflictError("Confirmation target changed.")
            run.confirmation_response, run.status = decision, "PENDING"
            run.version += 1
            row.version, row.status, row.document = run.version, run.status, run.model_dump(mode="json")
            return self._hydrate(session, run)

    def checkpoint_cleanup_ids(self, *, limit: int = 100) -> list[str]:
        with self.sessions() as session:
            return list(session.scalars(select(AgentCheckpointCleanupRow.agent_run_id)
                .order_by(AgentCheckpointCleanupRow.created_at).limit(limit)).all())

    def complete_checkpoint_cleanup(self, run_id: str) -> None:
        with self.sessions.begin() as session:
            session.execute(delete(AgentCheckpointCleanupRow).where(
                AgentCheckpointCleanupRow.agent_run_id == run_id))

    def checkpoint_cleanup_failed(self, run_id: str) -> None:
        with self.sessions.begin() as session:
            session.execute(update(AgentCheckpointCleanupRow).where(
                AgentCheckpointCleanupRow.agent_run_id == run_id).values(
                    attempts=AgentCheckpointCleanupRow.attempts + 1))

    @staticmethod
    def _run(document):
        if document.get("resource_protocol_version") != "resource-ref-v1":
            raise AgentFailure("CONVERSATION_UPGRADE_REQUIRED")
        return AgentRun.model_validate(document)

    def get(self, run_id: str, actor_id: str) -> AgentRun:
        with self.sessions() as session:
            row = session.get(AgentRunRow, run_id)
            if row is None or row.actor_id != actor_id:
                raise AgentFailure("AGENT_RUN_NOT_FOUND")
            return self._hydrate(session, self._run(row.document))

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
            return [self._hydrate(session, self._run(row.document)) for row in rows]

    @staticmethod
    def message_view(row):
        data = row.structured_content or {}
        return {"message_id": row.message_id, "agent_run_id": row.agent_run_id, "role": row.role,
                "phase": row.phase, "content_status": "complete", "text": row.content_text,
                "sequence": row.sequence, "created_at": row.created_at.isoformat(),
                "attachments": data.get("attachments", []), "artifacts": data.get("artifacts", []),
                "sources": data.get("sources", []), "answer_root_message_id": row.answer_root_message_id,
                "answer_version": row.answer_version, "version_count": row.answer_version or 0}

    def _hydrate(self, session, run):
        identities = set(run.user_message_ids + run.context_message_ids +
                         [run.source_message_id, run.question_message_id, run.final_message_id]) - {None}
        run.messages = [self.message_view(row) for row in session.scalars(select(MessageRow).where(
            MessageRow.message_id.in_(identities), MessageRow.actor_id == run.actor_id,
            MessageRow.conversation_id == run.conversation_id)).all()]
        final = next((m for m in run.messages if m["message_id"] == run.final_message_id), None)
        run.result_attachments = list(final["artifacts"]) if final else []
        return run

    def submit(self, conversation_id, actor_id, content, key, *, run_id=None, waiting_version=None,
               question_message_id=None, budget=None, retry_source=None, retry_type=None,
               retry_invocation_id=None, source_answer_message_id=None, attachments=None):
        from materialsagent.domain.models.attachment import Attachment
        attachments = [Attachment.model_validate(a).model_dump() for a in (attachments or [])]
        if len(attachments) > 1:
            raise AgentFailure("ATTACHMENT_LIMIT_EXCEEDED")
        digest = fingerprint([content, run_id, waiting_version, question_message_id, attachments,
                              retry_source.agent_run_id if retry_source else None, retry_type,
                              retry_invocation_id, source_answer_message_id])
        with self.sessions.begin() as session:
            from .ml_resources import lock_conversation
            conversation = lock_conversation(session, actor_id, conversation_id, writable=True)
            existing = session.scalar(select(AgentSubmissionRow).where(
                AgentSubmissionRow.conversation_id == conversation_id, AgentSubmissionRow.idempotency_key == key))
            if existing:
                if existing.payload_hash != digest:
                    raise AgentConflictError("Idempotency payload differs.")
                replay = self._hydrate(session, self._run(session.get(AgentRunRow, existing.agent_run_id).document))
                replay.accepted_submission_id = existing.submission_id
                return replay, True
            contracts = session.scalars(select(MessageRow.structured_content).where(MessageRow.conversation_id == conversation_id)).all()
            if any(not value or value.get("contract") != "chat-v2" for value in contracts):
                raise AgentFailure("CONVERSATION_UPGRADE_REQUIRED")
            active = session.scalar(select(AgentRunRow.agent_run_id).where(AgentRunRow.conversation_id == conversation_id,
                AgentRunRow.status.in_(["PENDING", "RUNNING", "WAITING_FOR_CONFIRMATION", "INTERRUPTED"])))
            if active:
                raise AgentConflictError("A submission is already active.")
            for attachment in attachments:
                if attachment["kind"] == "ebsd_image":
                    from .asset import AssetRow
                    asset = session.get(AssetRow, attachment["attachment_id"])
                    if not asset or asset.actor_id != actor_id or asset.conversation_id != conversation_id or asset.current_status != "AVAILABLE":
                        raise AgentFailure("EBSD_ASSET_NOT_FOUND")
                else:
                    from .ml_resources import references
                    ref = session.execute(select(references).where(references.c.id == attachment["attachment_id"],
                        references.c.actor_id == actor_id, references.c.conversation_id == conversation_id,
                        references.c.resource_type == "dataset")).first()
                    if ref is None:
                        raise AgentFailure("ATTACHMENT_NOT_FOUND")
            submission_id = identifier()
            if run_id:
                row = session.get(AgentRunRow, run_id)
                if not row or row.actor_id != actor_id or row.conversation_id != conversation_id:
                    raise AgentFailure("AGENT_RUN_NOT_FOUND")
                run = self._run(row.document)
                if (run.status != "WAITING_FOR_USER" or run.waiting_version != waiting_version
                        or run.question_message_id != question_message_id):
                    raise AgentConflictError("Question is no longer waiting at this version.")
            else:
                waiting = session.scalar(select(AgentRunRow.agent_run_id).where(AgentRunRow.conversation_id == conversation_id,
                    AgentRunRow.status == "WAITING_FOR_USER"))
                if waiting and not retry_source:
                    raise AgentConflictError("Reply to the current question is required.")
            if retry_source:
                # Regeneration/retry is an operation, never a fabricated second user message.
                message_id = retry_source.source_message_id
                run = AgentRun(conversation_id=conversation_id, actor_id=actor_id, source_message_id=message_id,
                    user_message_ids=list(retry_source.user_message_ids), context_message_ids=list(retry_source.context_message_ids),
                    attachments=list(retry_source.attachments), source_agent_run_id=retry_source.agent_run_id,
                    retry_type=retry_type, budget=budget or RunBudget())
                if retry_type == "ANSWER_REGENERATION":
                    target = session.get(MessageRow, source_answer_message_id)
                    if (not target or target.actor_id != actor_id or target.conversation_id != conversation_id
                            or target.phase != "answer" or target.agent_run_id != retry_source.agent_run_id):
                        raise AgentFailure("ANSWER_REGENERATION_NOT_ALLOWED")
                    run.tool_execution_disabled = True
                    run.source_answer_message_id = target.message_id
                    run.answer_root_message_id = target.answer_root_message_id
                    allowed = (target.structured_content or {}).get("sources", [])
                    run.observations = [o.model_copy(deep=True) for o in retry_source.observations if o.observation_id in allowed]
                    for observation in run.observations:
                        observation.source_agent_run_id = observation.source_agent_run_id or retry_source.agent_run_id
                else:
                    target = next((e for e in retry_source.executions if e.invocation_run_id == retry_invocation_id), None)
                    if not target or target.status != "FAILED" or not target.retryable:
                        raise AgentFailure("TOOL_RETRY_NOT_ALLOWED")
                    run.retry_execution = target.model_copy(deep=True)
            else:
                message_id = identifier()
                session.add(MessageRow(message_id=message_id, conversation_id=conversation_id, actor_id=actor_id,
                    task_id=None, request_id=submission_id, role="USER", generation_source="USER", content_text=content,
                    structured_content={"contract": "chat-v2", "attachments": attachments,
                        "reply_to": question_message_id}, created_at=now()))
                session.flush()
                if not run_id:
                    if conversation.title is None:
                        conversation.title = content.strip().splitlines()[0][:60]
                    history = session.scalars(select(MessageRow).where(MessageRow.conversation_id == conversation_id,
                        MessageRow.actor_id == actor_id, MessageRow.message_id != message_id,
                        or_(MessageRow.answer_root_message_id.is_(None), MessageRow.answer_root_message_id == MessageRow.message_id))
                        .order_by(MessageRow.sequence.desc()).limit(20)).all()
                    # Versions replace the answer at its original logical position;
                    # viewing/regenerating an old answer does not move it after later turns.
                    history_ids = []
                    for item in history:
                        latest = session.scalar(select(MessageRow).where(MessageRow.answer_root_message_id == item.message_id)
                            .order_by(MessageRow.answer_version.desc()).limit(1)) if item.answer_root_message_id else None
                        history_ids.append((latest or item).message_id)
                    run = AgentRun(conversation_id=conversation_id, actor_id=actor_id, source_message_id=message_id,
                        user_message_ids=[], context_message_ids=list(reversed(history_ids)), budget=budget or RunBudget())
                run.user_message_ids.append(message_id)
                if attachments:
                    run.attachments = attachments
            run.submission_id = submission_id
            run.status = "PENDING"
            run.version += 1 if run_id else 0
            if run_id:
                row.status, row.version, row.document = run.status, run.version, run.model_dump(mode="json")
            else:
                session.add(AgentRunRow(agent_run_id=run.agent_run_id, conversation_id=conversation_id, actor_id=actor_id,
                    source_message_id=run.source_message_id, status=run.status, version=run.version,
                    document=run.model_dump(mode="json"), created_at=run.created_at))
            session.flush()
            if not retry_source:
                session.get(MessageRow, message_id).agent_run_id = run.agent_run_id
            session.add(AgentSubmissionRow(submission_id=submission_id, conversation_id=conversation_id,
                agent_run_id=run.agent_run_id, message_id=message_id, idempotency_key=key, payload_hash=digest))
            return self._hydrate(session, run), False

    def save(self, run: AgentRun) -> None:
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
            previous = self._run(row.document)
            if run.status in ("PENDING", "RUNNING", "WAITING_FOR_CONFIRMATION", "INTERRUPTED"):
                from .ml_resources import lock_conversation
                lock_conversation(session, run.actor_id, run.conversation_id, writable=True)
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
            self._audit(session, run)
            if run.terminal and session.get(AgentCheckpointCleanupRow, run.agent_run_id) is None:
                session.add(AgentCheckpointCleanupRow(agent_run_id=run.agent_run_id, created_at=now(), attempts=0))
            if run.pending_message:
                from materialsagent.application.chat_artifacts import run_artifacts
                value = run.pending_message
                root = run.answer_root_message_id or value["message_id"] if value["phase"] == "answer" else None
                answer_version = None
                if root:
                    from sqlalchemy import func
                    answer_version = (session.scalar(select(func.max(MessageRow.answer_version)).where(
                        MessageRow.answer_root_message_id == root)) or 0) + 1
                session.add(MessageRow(message_id=value["message_id"], conversation_id=run.conversation_id,
                    agent_run_id=run.agent_run_id, actor_id=run.actor_id, task_id=None, request_id=run.submission_id or run.agent_run_id,
                    role="ASSISTANT", generation_source="AGENT", phase=value["phase"], content_text=value["text"],
                    answer_root_message_id=root, answer_version=answer_version,
                    structured_content={"contract": "chat-v2", "sources": value["sources"],
                        "artifacts": run_artifacts(session, run, value["sources"]) if value["phase"] == "answer" else []}, created_at=now()))
            owner.updated_at = max(owner.updated_at, run.updated_at)
        run.version = version + 1
        run.pending_message = None

    def recover_interrupted(self, process_id: str, *, repair=None) -> list[tuple[str, str, str]]:
        """Release stale claims and meter interrupted model calls before SDK replay."""
        with self.sessions() as session:
            rows = session.scalars(select(AgentRunRow).where(or_(
                AgentRunRow.status.in_(["PENDING", "RUNNING", "INTERRUPTED", "WAITING_FOR_USER", "WAITING_FOR_CONFIRMATION"]),
                and_(AgentRunRow.status == "TERMINATED",
                     AgentRunRow.document["error_code"].as_string() == "USER_STOPPED"),
            ))).all()
            candidates = [(row.agent_run_id, row.actor_id, row.status) for row in rows]
        result = []
        for run_id, actor_id, status in candidates:
            if status == "TERMINATED":
                run = self.get(run_id, actor_id)
                record = run.pending_execution
                if repair is not None and record and record.dispatched:
                    try:
                        observation = repair(run, record)
                        if observation is not None:
                            self.receipt(run_id, actor_id, record, observation)
                    except (AgentFailure, AgentConflictError):
                        pass
                result.append((run_id, actor_id, status))
                continue
            if status in {"PENDING", "RUNNING"}:
                with self.sessions.begin() as session:
                    row = session.scalar(select(AgentRunRow).where(
                        AgentRunRow.agent_run_id == run_id,
                        AgentRunRow.actor_id == actor_id).with_for_update())
                    if row is None or row.status not in {"PENDING", "RUNNING"}:
                        continue
                    run = self._run(row.document)
                    if run.process_id == process_id:
                        continue
                    # Downtime is not active execution time. The previous
                    # process persisted its metered active time at each fence.
                    for call in run.calls:
                        if call.status == "RUNNING":
                            call.status, call.error_code = "FAILED", "PROCESS_INTERRUPTED"
                            call.usage = TokenUsage(input_tokens=call.input_reserved,
                                output_tokens=call.output_limit,
                                total_tokens=call.input_reserved + call.output_limit,
                                source="estimated", estimator_version="cl100k-x2-or-utf8-framing-v1")
                            run.llm_tokens += call.usage.total_tokens
                    run.status, run.claim, run.process_id = "INTERRUPTED", None, None
                    run.error_code = "PROCESS_INTERRUPTED"
                    run.recovery_replay = True
                    run.version += 1
                    run.updated_at = now()
                    self._audit(session, run)
                    row.version, row.status, row.document = run.version, run.status, run.model_dump(mode="json")
                result.append((run_id, actor_id, "INTERRUPTED"))
            else:
                result.append((run_id, actor_id, status))
        return result

    def _audit(self, session, run):
        for execution in run.executions + ([run.pending_execution] if run.pending_execution else []):
            if not execution.invocation_run_id:
                continue
            stored = session.get(AgentExecutionRow, (execution.tool_call_id, run.agent_run_id))
            if stored:
                if stored.invocation_run_id != execution.invocation_run_id:
                    raise AgentConflictError("Tool-call Invocation cannot change.")
                stored.document = execution.model_dump(mode="json")
            else:
                session.add(AgentExecutionRow(tool_call_id=execution.tool_call_id, agent_run_id=run.agent_run_id,
                    invocation_run_id=execution.invocation_run_id, document=execution.model_dump(mode="json")))
        for observation in run.observations:
            if observation.source_agent_run_id:
                continue  # Imported references remain owned by the original Run.
            if session.get(AgentObservationRow, observation.observation_id) is None:
                session.add(AgentObservationRow(observation_id=observation.observation_id, agent_run_id=run.agent_run_id,
                    tool_call_id=observation.tool_call_id, invocation_run_id=observation.invocation_run_id, document=observation.model_dump(mode="json")))
        for call in run.calls:
            stored = session.get(AgentModelCallRow, call.call_id)
            if stored:
                stored.document = call.model_dump(mode="json")
            else:
                session.add(AgentModelCallRow(call_id=call.call_id, agent_run_id=run.agent_run_id,
                    document=call.model_dump(mode="json")))

    def stop(self, run_id, actor_id, submission_id):
        run, stopped = self._stop(run_id, actor_id, submission_id)
        record = run.pending_execution
        if stopped and record and not record.dispatched:
            # The preparation may have committed just before the stop, before its
            # Invocation ID was copied into the Run. The action key is immutable.
            from .tool_invocation import SQLAlchemyInvocationRunRepository, InvocationRunRow, _run_from_row
            from materialsagent.domain.models.tool_invocation import InvocationStatus
            with self.sessions.begin() as session:
                invocation = session.scalar(select(InvocationRunRow).where(
                    InvocationRunRow.actor_id == actor_id, InvocationRunRow.conversation_id == run.conversation_id,
                    InvocationRunRow.idempotency_key == tool_call_key(run.agent_run_id, record.tool_call_id)).with_for_update())
                if invocation and invocation.status == "PENDING":
                    current = _run_from_row(invocation)
                    failed = current.transition(InvocationStatus.FAILED, now=now(), completed_at=now(),
                        error_code="USER_STOPPED", safe_error_message="Stopped before dispatch.")
                    SQLAlchemyInvocationRunRepository(session).update(failed, expected_status=current.status)
                elif invocation and invocation.status == "PENDING_CONFIRMATION":
                    current = _run_from_row(invocation)
                    rejected = current.transition(InvocationStatus.REJECTED, now=now(), completed_at=now(),
                        rejected_at=now(), rejected_by=actor_id)
                    SQLAlchemyInvocationRunRepository(session).update(rejected, expected_status=current.status)
        return run, stopped

    def _stop(self, run_id, actor_id, submission_id):
        with self.sessions.begin() as session:
            from .ml_resources import lock_conversation
            original = session.get(AgentRunRow, run_id)
            if not original or original.actor_id != actor_id:
                raise AgentFailure("AGENT_RUN_NOT_FOUND")
            lock_conversation(session, actor_id, original.conversation_id)
            row = session.scalar(select(AgentRunRow).where(AgentRunRow.agent_run_id == run_id).with_for_update())
            run = self._run(row.document)
            if run.submission_id != submission_id:
                raise AgentConflictError("Submission is stale.")
            if run.status not in ("PENDING", "RUNNING", "INTERRUPTED"):
                return self._hydrate(session, run), run.error_code == "USER_STOPPED"
            run.status, run.error_code, run.stop_requested_at = "TERMINATED", "USER_STOPPED", now()
            run.updated_at = now()
            for call in run.calls:
                if call.status == "RUNNING":
                    call.status, call.error_code = "FAILED", "USER_STOPPED"
                    call.usage = TokenUsage(input_tokens=call.input_reserved, output_tokens=call.output_limit,
                        total_tokens=call.input_reserved + call.output_limit, source="estimated",
                        estimator_version="cl100k-x2-or-utf8-framing-v1")
                    run.llm_tokens += call.usage.total_tokens
            self._audit(session, run)
            if session.get(AgentCheckpointCleanupRow, run.agent_run_id) is None:
                session.add(AgentCheckpointCleanupRow(agent_run_id=run.agent_run_id, created_at=now(), attempts=0))
            run.version += 1
            row.version, row.status, row.document = run.version, run.status, run.model_dump(mode="json")
            return self._hydrate(session, run), True

    def receipt(self, run_id, actor_id, record, observation):
        with self.sessions.begin() as session:
            from .ml_resources import lock_conversation
            owner = session.get(AgentRunRow, run_id)
            if not owner or owner.actor_id != actor_id:
                raise AgentFailure("AGENT_RUN_NOT_FOUND")
            conversation = lock_conversation(session, actor_id, owner.conversation_id, writable=True)
            row = session.scalar(select(AgentRunRow).where(AgentRunRow.agent_run_id == run_id).with_for_update())
            run = self._run(row.document)
            pending = run.pending_execution
            if any(o.invocation_run_id == record.invocation_run_id for o in run.observations):
                return self._hydrate(session, run)
            if (not pending or not pending.dispatched or pending.invocation_run_id != record.invocation_run_id
                    or pending.tool_call_id != record.tool_call_id or pending.execution_fingerprint != record.execution_fingerprint
                    or observation.invocation_run_id != record.invocation_run_id
                    or run.status not in ("RUNNING", "INTERRUPTED", "TERMINATED")):
                raise AgentConflictError("Receipt does not own this dispatched execution.")
            if run.status == "TERMINATED" and run.error_code != "USER_STOPPED":
                raise AgentConflictError("Only a stopped run accepts a late receipt.")
            observation.tool_call_id = record.tool_call_id
            record.status = observation.status
            record.task_id, record.tool_run_id = observation.task_id, observation.tool_run_id
            record.retryable = bool(observation.error and observation.error.get("retryable"))
            record.observation_id = observation.observation_id
            run.observations.append(observation)
            run.executions.append(record.model_copy(deep=True))
            run.pending_execution, run.draft, run.retry_execution = None, None, None
            self._audit(session, run)
            run.version += 1
            run.updated_at = now()
            row.version, row.document = run.version, run.model_dump(mode="json")
            if run.error_code == "USER_STOPPED":
                from materialsagent.application.result_projection import project_result
                from materialsagent.application.chat_artifacts import run_artifacts
                presentation = project_result(observation)
                session.add(MessageRow(message_id=identifier(), event_key="stopped:" + record.invocation_run_id,
                    conversation_id=run.conversation_id, actor_id=actor_id, agent_run_id=run_id,
                    task_id=None, request_id=run.submission_id, role="ASSISTANT", generation_source="TEMPLATE",
                    phase="notification", content_text="此前已提交的计算已结束。\n" + "\n".join([presentation["summary"], *presentation["notes"]]),
                    structured_content={"contract": "chat-v2", "presentation": presentation,
                        "artifacts": run_artifacts(session, run, [observation.observation_id]), "sources": [observation.observation_id]}, created_at=now()))
            conversation.updated_at = run.updated_at
            return self._hydrate(session, run)

    def fail_execution(self, run_id, actor_id, record):
        observation = Observation(
            tool_call_id=record.tool_call_id, kind="TOOL_RESULT", status="FAILED", tool_name=record.tool_name,
            invocation_run_id=record.invocation_run_id, error={"code": "TOOL_EXECUTION_FAILED", "retryable": False})
        return self.receipt(run_id, actor_id, record, observation)

    def messages(self, conversation_id, actor_id, *, before=None, limit=50):
        from sqlalchemy import func, or_
        with self.sessions() as session:
            from .ml_resources import lock_conversation
            lock_conversation(session, actor_id, conversation_id)
            query = select(MessageRow).where(MessageRow.conversation_id == conversation_id,
                or_(MessageRow.answer_root_message_id.is_(None), MessageRow.answer_root_message_id == MessageRow.message_id))
            if before is not None:
                query = query.where(MessageRow.sequence < before)
            roots = session.scalars(query.order_by(MessageRow.sequence.desc()).limit(limit)).all()
            items = []
            for root in reversed(roots):
                latest = session.scalar(select(MessageRow).where(MessageRow.answer_root_message_id == root.message_id)
                    .order_by(MessageRow.answer_version.desc()).limit(1)) if root.answer_root_message_id else root
                item = self.message_view(latest)
                item["sequence"] = root.sequence
                items.append(item)
            return {"items": items, "next_cursor": roots[-1].sequence if len(roots) == limit else None}

    def versions(self, message_id, actor_id, *, before=None, limit=20):
        with self.sessions() as session:
            target = session.get(MessageRow, message_id)
            if not target or target.actor_id != actor_id or target.phase != "answer":
                raise AgentFailure("MESSAGE_NOT_FOUND")
            query = select(MessageRow).where(MessageRow.answer_root_message_id == target.answer_root_message_id,
                MessageRow.actor_id == actor_id, MessageRow.conversation_id == target.conversation_id)
            if before is not None:
                query = query.where(MessageRow.answer_version < before)
            rows = session.scalars(query.order_by(MessageRow.answer_version.desc()).limit(limit)).all()
            return {"items": [self.message_view(row) for row in rows],
                    "next_cursor": rows[-1].answer_version if len(rows) == limit else None}

    def answer_run(self, message_id, actor_id):
        with self.sessions() as session:
            message = session.get(MessageRow, message_id)
            if not message or message.actor_id != actor_id or message.phase != "answer" or not message.agent_run_id:
                raise AgentFailure("ANSWER_REGENERATION_NOT_ALLOWED")
            row = session.get(AgentRunRow, message.agent_run_id)
            return self._hydrate(session, self._run(row.document))
