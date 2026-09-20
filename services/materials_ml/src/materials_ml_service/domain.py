"""Service domain values and ports; no HTTP, SQL or storage SDK imports."""
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
from typing import Protocol
from uuid import uuid4

ARTIFACT_STATES = ("PENDING", "AVAILABLE", "FAILED", "DELETING", "DELETED")
RUN_STATES = ("PENDING", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED")
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")
MEMBERS = {"manifest.json": (32 * 1024**2, "application/json"),
           "evaluation.json": (32 * 1024**2, "application/json"),
           "splits.json": (32 * 1024**2, "application/json"),
           "pipeline.joblib": (256 * 1024**2, "application/octet-stream")}
MAX_DATASET_BYTES = 20 * 1024**2


def now():
    return datetime.now(timezone.utc)


def new_id():
    return uuid4().hex


class ServiceError(Exception):
    def __init__(self, code: str, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


def text_id(value: str, maximum=128):
    if (not isinstance(value, str) or not value or len(value) > maximum or value != value.strip()
            or any(ord(c) < 32 for c in value)):
        raise ServiceError("INVALID_IDENTIFIER", 422)
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":")).encode("utf-8")


def digest(value):
    return sha256(canonical(value)).hexdigest()


@dataclass
class Resource:
    id: str
    scope_id: str
    status: str = "PENDING"
    version: int = 0
    created_at: datetime = field(default_factory=now)
    updated_at: datetime = field(default_factory=now)
    data: dict = field(default_factory=dict)

    def transition(self, destination):
        allowed = {"PENDING": {"AVAILABLE", "FAILED", "DELETING"},
                   "AVAILABLE": {"DELETING"}, "FAILED": {"DELETING"}, "DELETING": {"DELETED"}}
        if destination not in allowed.get(self.status, set()):
            raise ServiceError("INVALID_TRANSITION")
        self.status = destination
        self.touch()

    def touch(self):
        self.version += 1
        self.updated_at = now()


@dataclass
class DatasetAsset(Resource):
    artifact_id: str = ""


@dataclass
class MLArtifact(Resource):
    dataset_id: str | None = None
    run_id: str | None = None
    prediction_id: str | None = None
    member: str = "dataset.csv"
    object_ref: dict = field(default_factory=dict)
    checked_at: datetime = field(default_factory=now)


@dataclass
class TrainingRun(Resource):
    dataset_id: str = ""
    model_id: str | None = None
    worker_session: str | None = None
    claim_id: str | None = None
    heartbeat_at: datetime | None = None
    attempt: int = 0
    cancel_requested: bool = False
    recovery_required: bool = False
    process_stopped: bool = False

    def finish(self, destination, error=None):
        if self.status not in ("PENDING", "RUNNING") or destination not in TERMINAL:
            raise ServiceError("INVALID_TRANSITION")
        if destination == "SUCCEEDED" and (self.status != "RUNNING" or self.cancel_requested):
            raise ServiceError("INVALID_TRANSITION")
        self.status = destination
        self.process_stopped = True
        self.recovery_required = False
        if error:
            self.data = {**self.data, "error_code": error}
        self.touch()


@dataclass
class ModelAsset(Resource):
    run_id: str = ""


@dataclass
class Prediction(Resource):
    model_id: str = ""
    input_dataset_id: str = ""
    artifact_id: str | None = None
    cancel_requested: bool = False
    process_stopped: bool = False

    def finish(self, destination, error=None):
        if self.status not in ("PENDING", "RUNNING") or destination not in TERMINAL:
            raise ServiceError("INVALID_TRANSITION")
        if not self.process_stopped or (destination == "SUCCEEDED" and
                (self.status != "RUNNING" or self.cancel_requested)):
            raise ServiceError("INVALID_TRANSITION")
        self.status = destination
        if error:
            self.data = {**self.data, "error_code": error}
        self.touch()


class Repository(Protocol):
    def transaction(self) -> AbstractContextManager: ...


class StoragePort(Protocol):
    def put(self, ref: dict, payload: bytes) -> None: ...
    def get(self, ref: dict) -> bytes: ...
    def exists(self, ref: dict) -> bool: ...
    def delete(self, ref: dict) -> None: ...
