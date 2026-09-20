"""Short transactions for platform references and deletion coordination."""
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Table, Column, Text, Integer, ForeignKey, UniqueConstraint, select, update, insert, tuple_
from sqlalchemy.dialects.postgresql import JSONB

from .base import Base
from .conversation_task import ConversationRow
from materialsagent.application.errors import ApplicationConflictError, ResourceNotFoundError, ConversationBusyError


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def identifier():
    return uuid4().hex


def platform_table(name, *, persistent=False, extra=()):
    return Table(name, Base.metadata, Column("id", Text, primary_key=True), Column("actor_id", Text, nullable=False),
        Column("conversation_id", Text, *(() if persistent else (ForeignKey("conversation.conversation_id", ondelete="CASCADE"),)), nullable=False),
        Column("version", Integer, nullable=False), Column("document", JSONB, nullable=False), *extra)


bindings = platform_table("ml_scope_binding", extra=(Column("service_id", Text, nullable=False),
    UniqueConstraint("conversation_id", "service_id", name="uq_ml_scope_binding")))
references = platform_table("ml_resource_ref", extra=(Column("service_id", Text, nullable=False),
    Column("dataset_ordinal", Integer, nullable=True),
    UniqueConstraint("conversation_id", "dataset_ordinal", name="uq_ml_dataset_ordinal"),
    Column("resource_type", Text, nullable=False), Column("resource_id", Text, nullable=False),
    UniqueConstraint("service_id", "conversation_id", "resource_type", "resource_id", name="uq_ml_resource_ref")))
uploads = platform_table("ml_resource_operation", persistent=True, extra=(Column("operation_key", Text, nullable=False),
    Column("status", Text, nullable=False), UniqueConstraint("actor_id", "conversation_id", "operation_key", name="uq_ml_upload_key")))
deletions = platform_table("conversation_deletion", persistent=True, extra=(Column("status", Text, nullable=False),))
TABLES = {"binding": bindings, "reference": references, "upload": uploads, "deletion": deletions}


def lock_conversation(session, actor, identity, *, writable=False, expected_version=None):
    # A newly created parent can be pending in this same transaction (autoflush=False).
    pending = next((r for r in session.new if isinstance(r, ConversationRow) and r.conversation_id == identity), None)
    if pending is not None:
        session.flush([pending])
    row = session.scalar(select(ConversationRow).where(ConversationRow.conversation_id == identity,
        ConversationRow.actor_id == actor).with_for_update().execution_options(populate_existing=True))
    if row is None:
        raise ResourceNotFoundError()
    if writable and (row.deletion_fence_operation_id is not None or
            (expected_version is not None and row.deletion_fence_version != expected_version)):
        raise ApplicationConflictError(code="CONVERSATION_DELETE_PENDING", conversation_id=identity)
    return row


class ResourceTransaction:
    def __init__(self, session, actor, conversation):
        self.session, self.actor, self.conversation = session, actor, conversation

    @property
    def fence_version(self):
        return lock_conversation(self.session, self.actor, self.conversation).deletion_fence_version

    def rows(self, table, **filters):
        table = TABLES[table] if isinstance(table, str) else table
        q = select(table).where(table.c.actor_id == self.actor, table.c.conversation_id == self.conversation)
        for key, value in filters.items():
            q = q.where(table.c[key] == value)
        return [dict(r) for r in self.session.execute(q).mappings()]

    def reference_page(self, after, limit, resource_type=None, resource_id=None):
        query = select(references.c.document).where(references.c.actor_id == self.actor,
            references.c.conversation_id == self.conversation)
        if resource_type:
            query = query.where(references.c.resource_type == resource_type)
            if resource_id is not None:
                query = query.where(references.c.resource_id == resource_id)
            order = references.c.dataset_ordinal if resource_type == "dataset" else references.c.document["created_at"].as_string()
            if after:
                anchor = self.ref(after)
                if anchor["resource_type"] != resource_type:
                    raise ApplicationConflictError(code="ML_CURSOR_MISMATCH")
                key = anchor["dataset_ordinal"] if resource_type == "dataset" else anchor["created_at"]
                comparison = tuple_(order, references.c.id)
                query = query.where(comparison > (key, after) if resource_type == "dataset" else comparison < (key, after))
            ordering = (order.asc(), references.c.id.asc()) if resource_type == "dataset" else (order.desc(), references.c.id.desc())
            return list(self.session.scalars(query.order_by(*ordering).limit(limit + 1)))
        if after is not None:
            query = query.where(references.c.id > after)
        return list(self.session.scalars(query.order_by(references.c.id).limit(limit + 1)))

    def upload_page(self, after=None, limit=20, key=None):
        query = select(uploads.c.document, uploads.c.operation_key).where(uploads.c.actor_id == self.actor,
            uploads.c.conversation_id == self.conversation)
        if key is not None:
            query = query.where(uploads.c.operation_key == key)
        if after:
            query = query.where(uploads.c.id > after)
        return [{**row.document, "client_idempotency_key": row.operation_key}
                for row in self.session.execute(query.order_by(uploads.c.id).limit(limit + 1))]

    def ui_fence(self):
        row = lock_conversation(self.session, self.actor, self.conversation)
        return {"operation_id": row.deletion_fence_operation_id, "version": row.deletion_fence_version}

    def put(self, table, identity, document, **columns):
        table = TABLES[table] if isinstance(table, str) else table
        existing = self.session.execute(select(table).where(table.c.id == identity)).mappings().first()
        if existing:
            if existing["actor_id"] != self.actor or existing["conversation_id"] != self.conversation:
                raise ResourceNotFoundError()
            count = self.session.execute(update(table).where(table.c.id == identity,
                table.c.version == existing["version"]).values(document=document,
                    version=existing["version"] + 1, **columns)).rowcount
            if count != 1:
                raise ApplicationConflictError()
        else:
            self.session.execute(insert(table).values(id=identity, actor_id=self.actor,
                conversation_id=self.conversation, version=0, document=document, **columns))

    def bind(self, service, source):
        old = self.rows(bindings, service_id=service["service_id"])
        if old:
            if old[0]["document"]["service"] != service:
                raise ApplicationConflictError(code="ML_SERVICE_BINDING_MISMATCH")
            return
        lock_conversation(self.session, self.actor, self.conversation, writable=True)
        self.put(bindings, identifier(), {"scope_id": self.conversation, "service": service, "source": source},
                 service_id=service["service_id"])

    def ref(self, identity):
        rows = self.rows(references, id=identity)
        if not rows:
            raise ResourceNotFoundError()
        return rows[0]["document"]

    def register(self, service, descriptor, source):
        lock_conversation(self.session, self.actor, self.conversation, writable=True)
        self.bind(service, source)
        old = self.rows(references, service_id=service["service_id"], resource_type=descriptor["resource_type"], resource_id=descriptor["resource_id"])
        if old:
            from materialsagent.domain.models.ml_resource import descriptor_matches
            if not descriptor_matches(old[0]["document"], descriptor):
                raise ApplicationConflictError(code="ML_RESOURCE_IDENTITY_MISMATCH")
            return old[0]["document"]
        value = {k: descriptor[k] for k in ("resource_type", "resource_id", "scope_id", "identity_contract_version", "remote_identity_digest", "identity")}
        if descriptor.get("description"):
            value["description"] = descriptor["description"]
        value.update(reference_id=identifier(), actor_id=self.actor, conversation_id=self.conversation,
            service_id=service["service_id"], source=source, created_at=timestamp())
        ordinal = None
        if value["resource_type"] == "dataset":
            ordinal = self.session.scalar(update(ConversationRow).where(ConversationRow.conversation_id == self.conversation)
                .values(ml_dataset_ordinal=ConversationRow.ml_dataset_ordinal + 1).returning(ConversationRow.ml_dataset_ordinal))
            value.update(dataset_ordinal=ordinal, ordinal_source="registration")
        self.put(references, value["reference_id"], value, service_id=service["service_id"],
            resource_type=value["resource_type"], resource_id=value["resource_id"], dataset_ordinal=ordinal)
        return value

    def assert_idle(self):
        from .agent import AgentRunRow
        from .asset import AssetRow
        from .tool_invocation import InvocationRunRow
        for table, condition in ((AgentRunRow, AgentRunRow.status.in_(("PENDING", "RUNNING"))),
                (AssetRow, (AssetRow.source_type == "UPLOADED") & (AssetRow.current_status == "PENDING")),
                (InvocationRunRow, InvocationRunRow.status.in_(("PENDING", "RUNNING")))):
            if self.session.scalar(select(table).where(table.conversation_id == self.conversation, condition).limit(1)):
                raise ConversationBusyError(conversation_id=self.conversation)
        if any(r["status"] not in ("RESOLVED", "REJECTED") for r in self.rows(uploads)):
            raise ConversationBusyError(conversation_id=self.conversation)
        unknowns = self.session.scalars(select(InvocationRunRow).where(InvocationRunRow.conversation_id == self.conversation,
            InvocationRunRow.status == "OUTCOME_UNKNOWN")).all()
        for row in unknowns:
            receipt = row.remote_receipt or {}
            resource = receipt.get("resource") or {}
            if (receipt.get("lookup_status") != "FOUND" or resource.get("scope_id") != self.conversation
                    or resource.get("status") not in ("SUCCEEDED", "FAILED", "CANCELLED")
                    or not row.remote_operation or any(receipt.get(k) != row.remote_operation.get(k)
                        for k in ("operation", "request_digest", "digest_version"))
                    or not self.rows(bindings)):
                raise ConversationBusyError(conversation_id=self.conversation)


class MLResourceRepository:
    def __init__(self, sessions):
        self.sessions = sessions

    @contextmanager
    def transaction(self, actor, conversation, *, writable=False, expected_version=None):
        with self.sessions.begin() as session:
            lock_conversation(session, actor, conversation, writable=writable, expected_version=expected_version)
            yield ResourceTransaction(session, actor, conversation)

    def operation(self, actor, identity, table=uploads):
        table = TABLES[table] if isinstance(table, str) else table
        with self.sessions() as session:
            row = session.execute(select(table).where(table.c.id == identity, table.c.actor_id == actor)).mappings().first()
            if not row:
                raise ResourceNotFoundError()
            return dict(row)

    def pending_deletions(self, actor, limit=20):
        with self.sessions() as session:
            return [r[0] for r in session.execute(select(deletions.c.id).where(deletions.c.actor_id == actor,
                deletions.c.status.in_(("DELETE_PENDING", "RECONCILING"))).limit(limit))]

    def pending_uploads(self, actor, limit=20):
        with self.sessions() as session:
            return list(session.execute(select(uploads.c.id).where(uploads.c.actor_id == actor,
                uploads.c.status.in_(("DISPATCHED", "OUTCOME_UNKNOWN", "REMOTE_PENDING"))).limit(limit)).scalars())

    def begin_delete(self, actor, conversation, cutoff):
        with self.transaction(actor, conversation) as tx:
            row = lock_conversation(tx.session, actor, conversation)
            if row.deletion_fence_operation_id:
                return tx.rows(deletions, id=row.deletion_fence_operation_id)[0]["document"]
            tx.assert_idle()
            from .conversation_cleanup import SQLAlchemyConversationLifecycleRepository
            lifecycle = SQLAlchemyConversationLifecycleRepository(tx.session)
            if lifecycle.current_activity_exists(actor_id=actor, conversation_id=conversation, process_cutoff=cutoff):
                raise ConversationBusyError(conversation_id=conversation)
            managed = tx.rows(bindings)
            if not managed:
                return None
            if len(managed) != 1:
                raise ApplicationConflictError(code="ML_SCOPE_BINDING_CONFLICT")
            identity = identifier()
            row.deletion_fence_operation_id = identity
            row.deletion_fence_version += 1
            value = {"operation_id": identity, "conversation_id": conversation, "actor_id": actor,
                "service": managed[0]["document"]["service"], "scope_id": conversation,
                "fence_version": row.deletion_fence_version, "status": "DELETE_PENDING", "created_at": timestamp()}
            tx.put(deletions, identity, value, status=value["status"])
            return value

    def deletion_fact(self, actor, identity, receipt):
        old = self.operation(actor, identity, deletions)["document"]
        if old["status"] in ("COMPLETED", "REJECTED_BUSY"):
            return old
        with self.transaction(actor, old["conversation_id"]) as tx:
            row = lock_conversation(tx.session, actor, old["conversation_id"])
            current = tx.rows(deletions, id=identity)[0]["document"]
            if row.deletion_fence_operation_id != identity or row.deletion_fence_version != old["fence_version"]:
                raise ApplicationConflictError(code="DELETION_FENCE_CHANGED")
            if current.get("receipt", {}).get("status") == "CLOSED":
                return current
            value = {**current, "status": "RECONCILING", "receipt": receipt}
            if receipt.get("status") == "BUSY":
                value["status"] = "REJECTED_BUSY"
                row.deletion_fence_operation_id = None
                row.deletion_fence_version += 1
            tx.put(deletions, identity, value, status=value["status"])
            return value
