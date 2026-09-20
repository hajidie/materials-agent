from contextlib import contextmanager
from dataclasses import fields
from datetime import datetime

from sqlalchemy import (MetaData, Table, Column, Text, Integer, Boolean, DateTime, JSON,
                        CheckConstraint, UniqueConstraint, ForeignKeyConstraint, select, insert, update,
                        create_engine, text, Index)

from ..domain import DatasetAsset, MLArtifact, TrainingRun, ModelAsset, Prediction, ServiceError, now

metadata = MetaData()


def resource_table(name, states, *extra):
    return Table(name, metadata, Column("id", Text, primary_key=True), Column("scope_id", Text, nullable=False),
                 Column("status", Text, nullable=False), Column("version", Integer, nullable=False),
                 Column("created_at", DateTime(timezone=True), nullable=False),
                 Column("updated_at", DateTime(timezone=True), nullable=False), Column("data", JSON, nullable=False),
                 UniqueConstraint("id", "scope_id"), CheckConstraint("version >= 0"),
                 CheckConstraint("length(scope_id) BETWEEN 1 AND 128"),
                 CheckConstraint("status IN (" + ",".join("'" + s + "'" for s in states) + ")"), *extra)


RESOURCE_STATES = ("PENDING", "AVAILABLE", "FAILED", "DELETING", "DELETED")
datasets = resource_table("ml_dataset", RESOURCE_STATES, Column("artifact_id", Text, nullable=False))
runs = resource_table("ml_training_run", ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"),
    Column("dataset_id", Text, nullable=False), Column("model_id", Text), Column("worker_session", Text),
    Column("claim_id", Text, unique=True), Column("heartbeat_at", DateTime(timezone=True)),
    Column("attempt", Integer, nullable=False), Column("cancel_requested", Boolean, nullable=False),
    Column("recovery_required", Boolean, nullable=False), Column("process_stopped", Boolean, nullable=False),
    ForeignKeyConstraint(["dataset_id", "scope_id"], ["ml_dataset.id", "ml_dataset.scope_id"]),
    CheckConstraint("(status = 'SUCCEEDED') = (model_id IS NOT NULL)"),
    CheckConstraint("status != 'RUNNING' OR claim_id IS NOT NULL"))
artifacts = resource_table("ml_artifact", RESOURCE_STATES,
    Column("dataset_id", Text), Column("run_id", Text), Column("prediction_id", Text), Column("member", Text, nullable=False),
    Column("object_ref", JSON, nullable=False), Column("checked_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("run_id", "member"),
    UniqueConstraint("prediction_id", "member"),
    UniqueConstraint("id", "prediction_id", "scope_id"),
    CheckConstraint("num_nonnulls(dataset_id, run_id, prediction_id) = 1", name="ck_artifact_owner"),
    ForeignKeyConstraint(["prediction_id", "scope_id"], ["ml_prediction.id", "ml_prediction.scope_id"]),
    ForeignKeyConstraint(["dataset_id", "scope_id"], ["ml_dataset.id", "ml_dataset.scope_id"]),
    ForeignKeyConstraint(["run_id", "scope_id"], ["ml_training_run.id", "ml_training_run.scope_id"]))
models = resource_table("ml_model", ("AVAILABLE",), Column("run_id", Text, nullable=False, unique=True),
    UniqueConstraint("id", "run_id", "scope_id"),
    ForeignKeyConstraint(["run_id", "scope_id"], ["ml_training_run.id", "ml_training_run.scope_id"]))
predictions = resource_table("ml_prediction", ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"),
    Column("model_id", Text, nullable=False), Column("input_dataset_id", Text, nullable=False),
    Column("artifact_id", Text), Column("cancel_requested", Boolean, nullable=False),
    Column("process_stopped", Boolean, nullable=False),
    ForeignKeyConstraint(["model_id", "scope_id"], ["ml_model.id", "ml_model.scope_id"]),
    ForeignKeyConstraint(["input_dataset_id", "scope_id"], ["ml_dataset.id", "ml_dataset.scope_id"]),
    CheckConstraint("(status = 'SUCCEEDED') = (artifact_id IS NOT NULL)"),
    CheckConstraint("status NOT IN ('SUCCEEDED','FAILED','CANCELLED') OR process_stopped"))
ForeignKeyConstraint([predictions.c.artifact_id, predictions.c.id, predictions.c.scope_id],
    [artifacts.c.id, artifacts.c.prediction_id, artifacts.c.scope_id], name="fk_prediction_result", use_alter=True)
ForeignKeyConstraint([runs.c.model_id, runs.c.id, runs.c.scope_id],
                     [models.c.id, models.c.run_id, models.c.scope_id],
                     name="fk_run_published_model", use_alter=True)
Index("uq_ml_active_claim", runs.c.process_stopped, unique=True,
      postgresql_where=runs.c.claim_id.is_not(None) & runs.c.process_stopped.is_(False))
Index("ix_ml_queue", runs.c.status, runs.c.created_at)
Index("ix_ml_artifact_reconcile", artifacts.c.status, artifacts.c.checked_at)
operations = Table("ml_operation", metadata, Column("auth_domain", Text, primary_key=True),
    Column("scope_id", Text, primary_key=True), Column("operation", Text, primary_key=True),
    Column("key", Text, primary_key=True), Column("digest", Text, nullable=False),
    Column("resource_id", Text, nullable=False), Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False), CheckConstraint("version = 0"))
scopes = Table("ml_scope", metadata, Column("scope_id", Text, primary_key=True),
    Column("status", Text, nullable=False), Column("version", Integer, nullable=False), Column("closed_by", Text),
    Column("cleanup_pending", Boolean, nullable=False), Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False))
scope_closes = Table("ml_scope_close", metadata, Column("scope_id", Text, primary_key=True),
    Column("operation_id", Text, primary_key=True), Column("status", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    ForeignKeyConstraint(["scope_id"], ["ml_scope.scope_id"]))
TABLES = {DatasetAsset: datasets, MLArtifact: artifacts, TrainingRun: runs, ModelAsset: models, Prediction: predictions}


def _values(resource):
    return {f.name: getattr(resource, f.name) for f in fields(resource)}


class Transaction:
    def __init__(self, connection):
        self.connection = connection
        # Single local service: serialize short transactions. No external IO here.
        connection.execute(text("SELECT pg_advisory_xact_lock(734921006)"))

    def scope_state(self, scope):
        return self.connection.execute(select(scopes).where(scopes.c.scope_id == scope)).mappings().first()

    def ensure_scope(self, scope):
        if self.scope_state(scope) is None:
            self.connection.execute(insert(scopes).values(scope_id=scope, status="OPEN", version=0,
                closed_by=None, cleanup_pending=False, created_at=now(), updated_at=now()))

    def require_open(self, scope):
        state = self.scope_state(scope)
        if state and state["status"] == "CLOSED":
            raise ServiceError("SCOPE_CLOSED", 410)

    def scope_close(self, scope, operation):
        row = self.connection.execute(select(scope_closes).where(scope_closes.c.scope_id == scope,
            scope_closes.c.operation_id == operation)).mappings().first()
        if row:
            state = self.scope_state(scope)
            return {"scope_id": scope, "operation_id": operation, "status": row["status"],
                    "cleanup_accepted": row["status"] == "CLOSED", "cleanup_pending": state["cleanup_pending"]}

    def record_scope_close(self, scope, operation, status):
        self.connection.execute(insert(scope_closes).values(scope_id=scope, operation_id=operation,
            status=status, created_at=now()))
        if status == "CLOSED" and self.scope_state(scope)["status"] != "CLOSED":
            self.connection.execute(update(scopes).where(scopes.c.scope_id == scope).values(status="CLOSED",
                closed_by=operation, cleanup_pending=True, updated_at=now(), version=scopes.c.version + 1))

    def scope_busy(self, scope):
        for table, condition in ((datasets, datasets.c.status == "PENDING"),
            (runs, runs.c.status.in_(("PENDING", "RUNNING")) | runs.c.recovery_required | ~runs.c.process_stopped),
            (predictions, predictions.c.status.in_(("PENDING", "RUNNING")) | ~predictions.c.process_stopped)):
            if self.connection.execute(select(table.c.id).where(table.c.scope_id == scope, condition).limit(1)).first():
                return True
        return False

    def mark_scope_cleanup_pending(self, scope):
        self.connection.execute(update(scopes).where(scopes.c.scope_id == scope,
            scopes.c.status == "CLOSED").values(cleanup_pending=True,
                updated_at=now(), version=scopes.c.version + 1))

    def clean_closed_scopes(self, limit):
        closed = self.connection.execute(select(scopes.c.scope_id).where(scopes.c.status == "CLOSED",
            scopes.c.cleanup_pending.is_(True)).limit(limit)).scalars().all()
        remaining = limit
        for scope in closed:
            for cls in (MLArtifact, DatasetAsset):
                for item in self.list(cls, scope=scope, statuses=("PENDING", "AVAILABLE", "FAILED"), limit=remaining):
                    item.transition("DELETING"); self.save(item); remaining -= 1
                if remaining == 0:
                    return
            pending = self.connection.execute(select(artifacts.c.id).where(artifacts.c.scope_id == scope,
                (artifacts.c.status != "DELETED") |
                artifacts.c.data["maintenance_error"].as_string().is_not(None)).limit(1)).first()
            if not pending:
                self.connection.execute(update(scopes).where(scopes.c.scope_id == scope).values(
                    cleanup_pending=False, updated_at=now(), version=scopes.c.version + 1))

    def get(self, cls, resource_id, scope=None):
        table = TABLES[cls]
        query = select(table).where(table.c.id == resource_id)
        if scope is not None:
            query = query.where(table.c.scope_id == scope)
        row = self.connection.execute(query).mappings().first()
        return cls(**dict(row)) if row else None

    def list(self, cls, *, scope=None, statuses=None, limit=100, after=None):
        table = TABLES[cls]
        query = select(table)
        if scope is not None:
            query = query.where(table.c.scope_id == scope)
        if statuses is not None:
            query = query.where(table.c.status.in_(statuses))
        if after:
            query = query.where(table.c.id > after)
        query = query.order_by(table.c.id).limit(limit)
        return [cls(**dict(r)) for r in self.connection.execute(query).mappings()]

    def add(self, resource):
        self.require_open(resource.scope_id)
        self.ensure_scope(resource.scope_id)
        self.connection.execute(insert(TABLES[type(resource)]).values(**_values(resource)))

    def find(self, cls, **conditions):
        table = TABLES[cls]
        q = select(table).where(*(table.c[k] == v for k, v in conditions.items())).order_by(table.c.id)
        return [cls(**dict(r)) for r in self.connection.execute(q).mappings()]

    def active_claims(self):
        q = select(runs).where(runs.c.claim_id.is_not(None), runs.c.process_stopped.is_(False))
        return [TrainingRun(**dict(r)) for r in self.connection.execute(q).mappings()]

    def due_artifacts(self, limit):
        q = select(artifacts).where(artifacts.c.status.in_(("PENDING", "FAILED", "DELETING", "DELETED")))
        q = q.order_by(artifacts.c.checked_at, artifacts.c.id).limit(limit)
        return [MLArtifact(**dict(r)) for r in self.connection.execute(q).mappings()]

    def save(self, resource):
        # Defense in depth for late publication; cleanup and stop evidence remain legal.
        if resource.status in ("AVAILABLE", "RUNNING", "PENDING", "SUCCEEDED"):
            self.require_open(resource.scope_id)
        table = TABLES[type(resource)]
        result = self.connection.execute(update(table).where(table.c.id == resource.id,
            table.c.version == resource.version - 1).values(**_values(resource)))
        if result.rowcount != 1:
            raise ServiceError("VERSION_CONFLICT")

    def operation(self, domain, scope, operation, key, request_digest):
        q = select(operations).where(operations.c.auth_domain == domain, operations.c.scope_id == scope,
            operations.c.operation == operation, operations.c.key == key)
        row = self.connection.execute(q).mappings().first()
        if row and row["digest"] != request_digest:
            raise ServiceError("IDEMPOTENCY_CONFLICT")
        return row["resource_id"] if row else None

    def record(self, domain, scope, operation, key, request_digest, resource_id):
        timestamp = now()
        self.connection.execute(insert(operations).values(auth_domain=domain, scope_id=scope,
            operation=operation, key=key, digest=request_digest, resource_id=resource_id,
            version=0, created_at=timestamp, updated_at=timestamp))


class PostgresRepository:
    def __init__(self, url):
        self.engine = create_engine(url, hide_parameters=True, pool_pre_ping=True)
        if self.engine.dialect.name != "postgresql":
            raise ValueError("ML persistence requires PostgreSQL")

    @contextmanager
    def transaction(self):
        with self.engine.begin() as connection:
            yield Transaction(connection)

    def close(self):
        self.engine.dispose()
