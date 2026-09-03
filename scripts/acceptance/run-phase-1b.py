"""Offline-safe helpers for the P1B2 Gate 4 acceptance executor.

Importing this module has no external side effects.  In particular, it does
not inspect the repository ``.env``, start a process, open a socket, or import
provider/model packages.  The real acceptance orchestration is driven by the
PowerShell runner; this module supplies its fail-closed primitives and an
offline self-test.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import ctypes
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import math
import ntpath
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence
import unicodedata
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


GATE_KEY_ERROR = "P1B2_GATE4_DEEPSEEK_KEY_NOT_AVAILABLE"
SECRET_LEAK_MARKER = "P1B2_GATE4_SECRET_LEAK_DETECTED"
RUNTIME_BUDGET_ERROR = "P1B2_GATE4_RUNTIME_BUDGET_EXCEEDED"
PROVIDER_BUDGET_ERROR = "P1B2_GATE4_PROVIDER_BUDGET_EXCEEDED"
MOCK_PROVIDER_ISOLATION_FAILED = (
    "P1B2_GATE4_MOCK_PROVIDER_ISOLATION_FAILED"
)
PROVIDER_CALL_COUNT_UNKNOWN = "P1B2_GATE4_PROVIDER_CALL_COUNT_UNKNOWN"
PROVIDER_LEDGER_INJECTION_BLOCKED = (
    "P1B2_GATE4_PROVIDER_LEDGER_INJECTION_BLOCKED"
)
NEW_STAGE_C_NOT_AUTHORIZED = "P1B2_GATE4_NEW_STAGE_C_NOT_AUTHORIZED"
STAGE_B_FINAL_ATTEMPT_BLOCKED = (
    "P1B2_GATE4_STAGE_B_FINAL_ATTEMPT_BLOCKED"
)
CLEANUP_INCOMPLETE = "P1B2_GATE4_CLEANUP_INCOMPLETE"
BLOCKED_MARKER = "P1B2_GATE4_BLOCKED"
COMPLETE_MARKER = "P1B2_GATE4_COMPLETE_AWAITING_PROJECT_OWNER_REVIEW"
EXECUTOR_SELF_TEST_MARKER = "P1B2_GATE4_EXECUTOR_SELF_TEST_OK"
MOCK_HELPER_TESTS_MARKER = "P1B2_GATE4_MOCK_HELPER_TESTS_OK"
MOCK_FULL_REGRESSION_MARKER = "P1B2_GATE4_MOCK_FULL_REGRESSION_OK"
EXECUTOR_FAILED_MARKER = "P1B2_GATE4_EXECUTOR_FAILED"
SECRET_IN_CHILD_COMMAND = "P1B2_GATE4_SECRET_IN_CHILD_COMMAND"
INVALID_STATE = "P1B2_GATE4_INVALID_STATE"
INVALID_STATE_TRANSITION = "P1B2_GATE4_INVALID_STATE_TRANSITION"
INVALID_COORDINATOR_PHASE = "P1B2_GATE4_INVALID_COORDINATOR_PHASE"
PHASE_NOT_IMPLEMENTED = "P1B2_GATE4_PHASE_NOT_IMPLEMENTED"
STAGE_B_COMPLETE_MARKER = "P1B2_GATE4_STAGE_B_COMPLETE"
BROWSER_READY_MARKER = "P1B2_GATE4_BROWSER_ACCEPTANCE_READY"
RESOURCE_GATE_FAILED = "P1B2_GATE4_RESOURCE_GATE_FAILED"
STAGE_B_MEMORY_PREFLIGHT_FAILED = (
    "P1B2_GATE4_STAGE_B_MEMORY_PREFLIGHT_FAILED"
)
PROCESS_IDENTITY_FAILED = "P1B2_GATE4_PROCESS_IDENTITY_FAILED"
HTTP_CONTRACT_FAILED = "P1B2_GATE4_HTTP_CONTRACT_FAILED"
DOCKER_GATE_FAILED = "P1B2_GATE4_DOCKER_GATE_FAILED"
PHASE1_FAILED = "P1B2_GATE4_PHASE1_FAILED"
REPOSITORY_GATE_FAILED = "P1B2_GATE4_REPOSITORY_GATE_FAILED"
HOST_MEMORY_LOW_WARNING = "HOST_MEMORY_LOW_WARNING"
HOST_MEMORY_CRITICAL_LOW = "HOST_MEMORY_CRITICAL_LOW"
STAGE_C_RUNTIME_START_MEMORY_FAILED = (
    "P1B2_GATE4_STAGE_C_RUNTIME_START_MEMORY_FAILED"
)
STAGE_C_TOOL_RETRY_MEMORY_FAILED = (
    "P1B2_GATE4_STAGE_C_TOOL_RETRY_MEMORY_FAILED"
)
TOOL_RETRY_RESOURCE_READY = "P1B2_GATE4_TOOL_RETRY_RESOURCE_READY"
_SAFE_CLI_FAILURE_MARKERS = {
    GATE_KEY_ERROR,
    SECRET_LEAK_MARKER,
    RUNTIME_BUDGET_ERROR,
    PROVIDER_BUDGET_ERROR,
    MOCK_PROVIDER_ISOLATION_FAILED,
    PROVIDER_CALL_COUNT_UNKNOWN,
    PROVIDER_LEDGER_INJECTION_BLOCKED,
    NEW_STAGE_C_NOT_AUTHORIZED,
    STAGE_B_FINAL_ATTEMPT_BLOCKED,
    CLEANUP_INCOMPLETE,
    BLOCKED_MARKER,
    RESOURCE_GATE_FAILED,
    STAGE_B_MEMORY_PREFLIGHT_FAILED,
    PROCESS_IDENTITY_FAILED,
    HTTP_CONTRACT_FAILED,
    REPOSITORY_GATE_FAILED,
    INVALID_STATE,
    PHASE1_FAILED,
    DOCKER_GATE_FAILED,
    HOST_MEMORY_CRITICAL_LOW,
    STAGE_C_RUNTIME_START_MEMORY_FAILED,
    STAGE_C_TOOL_RETRY_MEMORY_FAILED,
    "P1B2_GATE4_BACKEND_ADAPTER_CANARY_FAILED",
}

EXPECTED_BRANCH = "main"
EXPECTED_HEAD = "0db1c29f661313b10dac81c807f2649210046c1f"
EXPECTED_SUBJECT = "fix: allow zta35g runtime timeout up to 900s"
EXPECTED_PARENT = "5735384430a7556682993de8861d2a7d28889156"
EXPECTED_GPU_NAME = "NVIDIA GeForce RTX 3060 Laptop GPU"
EXPECTED_GPU_MEMORY_MIB = 6144
EXPECTED_GPU_COMPUTE_CAPABILITY = "8.6"
MINIMUM_GPU_FREE_MIB = 5500
STAGE_B_MINIMUM_HOST_AVAILABLE_BYTES = 5_905_580_032
HOST_MEMORY_LOW_WARNING_BYTES = 2_147_483_648
HOST_MEMORY_CRITICAL_LOW_BYTES = 1_610_612_736
STAGE_C_RUNTIME_START_BASE_BYTES = 4_831_838_208
STAGE_C_TOOL_RETRY_BASE_BYTES = 3_758_096_384
STAGE_C_INCREMENT_RESERVE_BYTES = 1_610_612_736
MINIMUM_ACTIVE_GPU_FREE_MIB = 400
RUNTIME_URL = "http://127.0.0.1:8100"
BACKEND_URL = "http://127.0.0.1:8000"
FRONTEND_URL = "http://127.0.0.1:3000"
MODEL_BUNDLE_ID = "zta35g-sem-original-bundle"

_DOTENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\Z")
_SAFE_RESOURCE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_SAFE_LOG_PATH = re.compile(r"logs/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_PROCESS_ROLE_PORTS = {
    "runtime": 8100,
    "backend": 8000,
    "backend-real": 8000,
    "backend-invalid": 8000,
    "frontend": 3000,
    "coordinator": 1,
}
_CONTROLLED_NAMES = (
    "real_provider_key",
    "invalid_provider_key",
    "runtime_token",
    "database_password",
    "minio_secret",
    "timeline_signing_key",
)
_AUTHORIZATION_NAMES = (
    "P1B2_GATE4_REAL_PROVIDER_AUTHORIZED",
    "P1B2_GATE4_REAL_RUNTIME_AUTHORIZED",
    "P1B2_GATE4_BROWSER_AUTHORIZED",
    "P1B2_GATE4_PROJECT_OWNER_AUTHORIZED",
)
_MOCK_FORBIDDEN_AUTHORIZATION_NAMES = (
    "M12B_REAL_CALLS_AUTHORIZED",
    "P1B2_GATE4_AUTHORIZED",
    *_AUTHORIZATION_NAMES,
)
_MOCK_INFRASTRUCTURE_NAMES = (
    "APP_ENV",
    "LOG_LEVEL",
    "LOCAL_ACTOR_ID",
    "POSTGRES_HOST",
    "POSTGRES_PORT",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "MINIO_API_PORT",
    "MINIO_CONSOLE_PORT",
    "MINIO_ENDPOINT",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "MINIO_BUCKET",
    "MINIO_SECURE",
    "ZTA35G_RUNTIME_MODE",
    "ZTA35G_RUNTIME_URL",
    "ZTA35G_RUNTIME_TOKEN",
    "ZTA35G_RUNTIME_TIMEOUT_SECONDS",
    "M5_DEV_ROUTES_ENABLED",
    "TIMELINE_CURSOR_SIGNING_KEY",
)
_MOCK_REQUIRED_INFRASTRUCTURE_NAMES = (
    "LOCAL_ACTOR_ID",
    "POSTGRES_HOST",
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "MINIO_API_PORT",
    "MINIO_CONSOLE_PORT",
    "MINIO_ENDPOINT",
    "MINIO_ACCESS_KEY",
    "MINIO_SECRET_KEY",
    "MINIO_BUCKET",
    "MINIO_SECURE",
    "ZTA35G_RUNTIME_URL",
    "ZTA35G_RUNTIME_TOKEN",
    "TIMELINE_CURSOR_SIGNING_KEY",
)
_FORWARD_STATE_PHASES = (
    "STAGEB_STARTING",
    "STAGEB_COMPLETE",
    "PHASE1_STARTING",
    "PHASE1_READY",
    "PHASE2_STARTING",
    "PHASE2_READY",
    "PHASE3_STARTING",
    "PHASE3_READY",
    "AUDIT_STARTING",
    "AUDIT_COMPLETE",
    "CLEANUP_STARTING",
)
_STATE_PHASES = (*_FORWARD_STATE_PHASES, "INVALIDATED")
_STATE_TRANSITIONS = {
    current: following
    for current, following in zip(
        _FORWARD_STATE_PHASES, _FORWARD_STATE_PHASES[1:]
    )
}
_SYSTEM_ENVIRONMENT_NAMES = (
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "WINDIR",
    "COMSPEC",
    "TEMP",
    "TMP",
    "PROGRAMDATA",
    "LOCALAPPDATA",
    "PROGRAMFILES",
    "PROGRAMW6432",
    "USERPROFILE",
)


class Gate4Error(RuntimeError):
    """A fixed-marker failure that is safe to include in evidence."""


class DotenvRecord:
    """In-memory dotenv inspection record.

    The key is deliberately excluded from ``repr`` and ``safe_snapshot``.
    """

    def __init__(self, *, path: Path, start_sha256: str, key: str) -> None:
        self.path = path
        self.exists = True
        self.start_sha256 = start_sha256
        self.key = key

    def __repr__(self) -> str:
        return (
            "DotenvRecord(exists=True, start_sha256="
            f"{self.start_sha256!r}, key='<redacted>')"
        )

    def safe_snapshot(self) -> dict[str, object]:
        return {
            "exists": self.exists,
            "start_sha256": self.start_sha256,
            "modified": False,
            "key_available": True,
        }


class Gate4Secrets:
    """Role-scoped values loaded literally from the controlled root dotenv."""

    def __init__(
        self,
        *,
        real_provider_key: str,
        runtime_token: str,
        database_password: str,
        minio_secret: str,
        timeline_signing_key: str,
        postgres_user: str,
        postgres_db: str,
        minio_access_key: str,
    ) -> None:
        self.real_provider_key = real_provider_key
        self.runtime_token = runtime_token
        self.database_password = database_password
        self.minio_secret = minio_secret
        self.timeline_signing_key = timeline_signing_key
        self.postgres_user = postgres_user
        self.postgres_db = postgres_db
        self.minio_access_key = minio_access_key

    def controlled_mapping(self) -> dict[str, str]:
        return {
            "real_provider_key": self.real_provider_key,
            "runtime_token": self.runtime_token,
            "database_password": self.database_password,
            "minio_secret": self.minio_secret,
            "timeline_signing_key": self.timeline_signing_key,
        }

    def __repr__(self) -> str:
        return "Gate4Secrets(<redacted>)"


class BackendRestartPlan:
    def __init__(
        self,
        *,
        mode: str,
        database: str,
        bucket: str,
        actor_id: str,
        conversation_id: str,
        argv: Sequence[str],
        environment: Mapping[str, str],
        process: object,
    ) -> None:
        self.mode = mode
        self.database = database
        self.bucket = bucket
        self.actor_id = actor_id
        self.conversation_id = conversation_id
        self.argv = list(argv)
        self.environment = dict(environment)
        self.process = process


def _fixed_key_failure() -> Gate4Error:
    return Gate4Error(GATE_KEY_ERROR)


def _has_forbidden_control(value: str) -> bool:
    return any(unicodedata.category(character) == "Cc" for character in value)


def _valid_required_key(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(character.isspace() for character in value)
        and not _has_forbidden_control(value)
    )


def parse_dotenv_text(text: str) -> dict[str, str]:
    """Parse literal KEY=VALUE lines without interpolation or evaluation.

    The function intentionally supports only the small dotenv subset needed by
    the gate.  Any malformed input, duplicate key, or unusable provider key
    fails with one fixed marker.
    """

    if not isinstance(text, str):
        raise _fixed_key_failure()
    if any(
        unicodedata.category(character) == "Cc"
        and character not in ("\r", "\n")
        for character in text
    ):
        raise _fixed_key_failure()

    parsed: dict[str, str] = {}
    try:
        lines = text.splitlines()
        for line in lines:
            if line == "" or line.startswith("#"):
                continue
            if _has_forbidden_control(line) or "=" not in line:
                raise _fixed_key_failure()
            name, value = line.split("=", 1)
            if (
                not _DOTENV_KEY.fullmatch(name)
                or name in parsed
                or _has_forbidden_control(value)
            ):
                raise _fixed_key_failure()
            parsed[name] = value
    except Gate4Error:
        raise
    except Exception as exc:
        raise _fixed_key_failure() from exc

    if not _valid_required_key(parsed.get("DEEPSEEK_API_KEY")):
        raise _fixed_key_failure()
    return parsed


def inspect_dotenv(path: os.PathLike[str] | str) -> DotenvRecord:
    """Read and validate an explicitly supplied dotenv file without changing it."""

    candidate = Path(path)
    try:
        if not candidate.is_file():
            raise _fixed_key_failure()
        payload = candidate.read_bytes()
        digest = sha256(payload).hexdigest()
        text = payload.decode("utf-8", errors="strict")
        parsed = parse_dotenv_text(text)
        return DotenvRecord(
            path=candidate,
            start_sha256=digest,
            key=parsed["DEEPSEEK_API_KEY"],
        )
    except Gate4Error:
        raise
    except Exception as exc:
        raise _fixed_key_failure() from exc


def load_gate4_secrets(path: os.PathLike[str] | str) -> Gate4Secrets:
    """Load the exact required values without interpolation or derivation."""

    required = {
        "real_provider_key": "DEEPSEEK_API_KEY",
        "runtime_token": "ZTA35G_RUNTIME_TOKEN",
        "database_password": "POSTGRES_PASSWORD",
        "minio_secret": "MINIO_SECRET_KEY",
        "timeline_signing_key": "TIMELINE_CURSOR_SIGNING_KEY",
        "postgres_user": "POSTGRES_USER",
        "postgres_db": "POSTGRES_DB",
        "minio_access_key": "MINIO_ACCESS_KEY",
    }
    try:
        candidate = Path(path)
        if not candidate.is_file():
            raise _fixed_key_failure()
        parsed = parse_dotenv_text(
            candidate.read_bytes().decode("utf-8", errors="strict")
        )
        values = {field: parsed.get(name) for field, name in required.items()}
        if not all(_valid_required_key(value) for value in values.values()):
            raise _fixed_key_failure()
        return Gate4Secrets(**values)
    except Gate4Error:
        raise
    except Exception as exc:
        raise _fixed_key_failure() from exc


def verify_dotenv_unchanged(
    record: DotenvRecord, path: os.PathLike[str] | str | None = None
) -> bool:
    """Fail closed unless the originally inspected file remains byte-identical."""

    candidate = Path(path) if path is not None else record.path
    try:
        unchanged = candidate.is_file() and (
            sha256(candidate.read_bytes()).hexdigest() == record.start_sha256
        )
    except Exception:
        unchanged = False
    if not unchanged:
        raise _fixed_key_failure()
    return True


def _controlled_value(controlled: Mapping[str, str], name: str) -> str:
    value = controlled.get(name)
    if not _valid_required_key(value):
        raise Gate4Error("P1B2_GATE4_INVALID_CONTROLLED_VALUE")
    return value


def build_child_environment(
    role: str,
    base_environment: Mapping[str, str],
    controlled: Mapping[str, str],
) -> dict[str, str]:
    """Build a new role-scoped environment without mutating the parent mapping."""

    environment: dict[str, str] = {}
    path_value = base_environment.get("PATH")
    if isinstance(path_value, str):
        environment["PATH"] = path_value

    if role == "runtime":
        environment["ZTA35G_RUNTIME_TOKEN"] = _controlled_value(
            controlled, "runtime_token"
        )
    elif role in ("backend-real", "backend-invalid"):
        key_name = (
            "real_provider_key" if role == "backend-real" else "invalid_provider_key"
        )
        environment.update(
            {
                "DEEPSEEK_API_KEY": _controlled_value(controlled, key_name),
                "ZTA35G_RUNTIME_TOKEN": _controlled_value(
                    controlled, "runtime_token"
                ),
                "DATABASE_PASSWORD": _controlled_value(
                    controlled, "database_password"
                ),
                "MINIO_SECRET_KEY": _controlled_value(controlled, "minio_secret"),
                "TIMELINE_CURSOR_SIGNING_KEY": _controlled_value(
                    controlled, "timeline_signing_key"
                ),
            }
        )
    elif role == "database":
        environment["DATABASE_PASSWORD"] = _controlled_value(
            controlled, "database_password"
        )
    elif role == "minio":
        environment["MINIO_SECRET_KEY"] = _controlled_value(
            controlled, "minio_secret"
        )
    elif role in ("frontend", "compose", "log"):
        pass
    else:
        raise Gate4Error("P1B2_GATE4_UNKNOWN_CHILD_ROLE")
    return environment


def _system_child_environment(
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    """Copy only ordinary operating-system variables into a child."""

    return {
        name: value
        for name in _SYSTEM_ENVIRONMENT_NAMES
        if isinstance((value := base_environment.get(name)), str) and value
    }


def _required_safe_value(
    values: Mapping[str, str], name: str
) -> str:
    value = values.get(name)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or _has_forbidden_control(value)
    ):
        raise Gate4Error("P1B2_GATE4_INVALID_SAFE_CHILD_VALUE")
    return value


def build_real_role_environment(
    role: str,
    base_environment: Mapping[str, str],
    controlled: Mapping[str, str],
    *,
    safe_values: Mapping[str, str],
) -> dict[str, str]:
    """Build the exact real-run environment for one child role.

    Safe connection names and endpoints are supplied separately from
    controlled values so a caller cannot accidentally put a credential in a
    command argument.  The parent mapping is never changed.
    """

    environment = _system_child_environment(base_environment)
    if role == "runtime":
        environment.update(
            {
                "ZTA35G_RUNTIME_TOKEN": _controlled_value(
                    controlled, "runtime_token"
                ),
                "ZTA35G_MODEL_ROOT": _required_safe_value(
                    safe_values, "model_root"
                ),
                "ZTA35G_RUNTIME_PORT": "8100",
                "ZTA35G_REAL_MODEL_ACCEPTANCE": (
                    "P1B2_PROJECT_OWNER_AUTHORIZED"
                ),
                "ZTA35G_GPU_ACCEPTANCE": "P1B2_GPU_AUTHORIZED",
                "PYTHONPATH": _required_safe_value(
                    safe_values, "runtime_pythonpath"
                ),
            }
        )
    elif role in {"backend-real", "backend-invalid"}:
        key_name = (
            "real_provider_key"
            if role == "backend-real"
            else "invalid_provider_key"
        )
        environment.update(
            {
                "APP_ENV": "local",
                "LOG_LEVEL": "INFO",
                "LOCAL_ACTOR_ID": _required_safe_value(
                    safe_values, "actor_id"
                ),
                "POSTGRES_HOST": "127.0.0.1",
                "POSTGRES_PORT": "5432",
                "POSTGRES_DB": _required_safe_value(
                    safe_values, "database"
                ),
                "POSTGRES_USER": _required_safe_value(
                    safe_values, "database_user"
                ),
                "POSTGRES_PASSWORD": _controlled_value(
                    controlled, "database_password"
                ),
                "MINIO_ENDPOINT": "http://127.0.0.1:9000",
                "MINIO_ACCESS_KEY": _required_safe_value(
                    safe_values, "minio_access_key"
                ),
                "MINIO_SECRET_KEY": _controlled_value(
                    controlled, "minio_secret"
                ),
                "MINIO_BUCKET": _required_safe_value(
                    safe_values, "bucket"
                ),
                "MINIO_SECURE": "false",
                "LLM_ADAPTER": "provider",
                "DEEPSEEK_API_KEY": _controlled_value(
                    controlled, key_name
                ),
                "ZTA35G_RUNTIME_URL": "http://127.0.0.1:8100",
                "ZTA35G_RUNTIME_TOKEN": _controlled_value(
                    controlled, "runtime_token"
                ),
                "ZTA35G_RUNTIME_TIMEOUT_SECONDS": "900",
                "M5_DEV_ROUTES_ENABLED": "false",
                "TIMELINE_CURSOR_SIGNING_KEY": _controlled_value(
                    controlled, "timeline_signing_key"
                ),
                "PYTHONPATH": _required_safe_value(
                    safe_values, "backend_pythonpath"
                ),
                "P1B2_GATE4_PROVIDER_LEDGER_REQUIRED": "1",
                "P1B2_GATE4_PROVIDER_LEDGER_PATH": _required_safe_value(
                    safe_values, "provider_ledger_path"
                ),
                "P1B2_GATE4_PROVIDER_LEDGER_RUN_ID": _required_safe_value(
                    safe_values, "provider_ledger_run_id"
                ),
                "P1B2_GATE4_FRESH_STAGE_C_AUTHORIZED": _required_safe_value(
                    safe_values, "fresh_stage_c_authorization"
                ),
            }
        )
    elif role == "frontend":
        environment["MATERIALSAGENT_BACKEND_ORIGIN"] = (
            _required_safe_value(safe_values, "backend_origin")
        )
    elif role == "compose":
        environment.update(
            {
                "COMPOSE_DISABLE_ENV_FILE": "1",
                "POSTGRES_DB": _required_safe_value(
                    safe_values, "admin_database"
                ),
                "POSTGRES_USER": _required_safe_value(
                    safe_values, "database_user"
                ),
                "POSTGRES_PASSWORD": _controlled_value(
                    controlled, "database_password"
                ),
                "POSTGRES_PORT": "5432",
                "MINIO_ACCESS_KEY": _required_safe_value(
                    safe_values, "minio_access_key"
                ),
                "MINIO_SECRET_KEY": _controlled_value(
                    controlled, "minio_secret"
                ),
                "MINIO_API_PORT": "9000",
                "MINIO_CONSOLE_PORT": "9001",
            }
        )
    elif role in {"database", "migration", "bootstrap"}:
        environment.update(
            {
                "POSTGRES_HOST": "127.0.0.1",
                "POSTGRES_PORT": "5432",
                "POSTGRES_DB": _required_safe_value(
                    safe_values, "database"
                ),
                "POSTGRES_USER": _required_safe_value(
                    safe_values, "database_user"
                ),
                "POSTGRES_PASSWORD": _controlled_value(
                    controlled, "database_password"
                ),
            }
        )
        if role in {"migration", "bootstrap"}:
            environment["PYTHONPATH"] = _required_safe_value(
                safe_values, "backend_pythonpath"
            )
        if role == "bootstrap":
            environment.update(
                {
                    "APP_ENV": "local",
                    "LOCAL_ACTOR_ID": _required_safe_value(
                        safe_values, "actor_id"
                    ),
                    "MINIO_ENDPOINT": "http://127.0.0.1:9000",
                    "MINIO_ACCESS_KEY": _required_safe_value(
                        safe_values, "minio_access_key"
                    ),
                    "MINIO_SECRET_KEY": _controlled_value(
                        controlled, "minio_secret"
                    ),
                    "MINIO_BUCKET": _required_safe_value(
                        safe_values, "bucket"
                    ),
                    "MINIO_SECURE": "false",
                }
            )
    elif role == "minio":
        environment.update(
            {
                "MINIO_ENDPOINT": "http://127.0.0.1:9000",
                "MINIO_ACCESS_KEY": _required_safe_value(
                    safe_values, "minio_access_key"
                ),
                "MINIO_SECRET_KEY": _controlled_value(
                    controlled, "minio_secret"
                ),
                "MINIO_BUCKET": _required_safe_value(
                    safe_values, "bucket"
                ),
                "MINIO_SECURE": "false",
            }
        )
    elif role in {"coordinator", "log"}:
        pass
    else:
        raise Gate4Error("P1B2_GATE4_UNKNOWN_CHILD_ROLE")
    return environment


def build_mock_subprocess_environment(
    base_environment: Mapping[str, str],
    *,
    backend_pythonpath: str,
) -> dict[str, str]:
    """Build a provider-free Mock child environment from a strict allowlist."""

    pythonpath = _required_safe_value(
        {"backend_pythonpath": backend_pythonpath},
        "backend_pythonpath",
    )
    environment = _system_child_environment(base_environment)
    environment.update(
        {
            "APP_ENV": "local",
            "LLM_ADAPTER": "mock",
            # An explicit blank wins over any repository dotenv value when
            # load_settings receives the exact child mapping.
            "DEEPSEEK_API_KEY": "",
            "DASHSCOPE_API_KEY": "",
            # Captured pytest failures can include the non-ASCII workspace
            # path.  Force one deterministic encoding at both ends.
            "PYTHONUTF8": "1",
            "PYTHONPATH": pythonpath,
        }
    )
    for name in _MOCK_FORBIDDEN_AUTHORIZATION_NAMES:
        environment.pop(name, None)
    return environment


def read_mock_infrastructure_dotenv(
    path: os.PathLike[str] | str,
) -> dict[str, str]:
    """Read only Phase 1A infrastructure values, never a Provider value."""

    candidate = Path(path)
    allowed = set(_MOCK_INFRASTRUCTURE_NAMES)
    values: dict[str, str] = {}
    try:
        with candidate.open("rb") as stream:
            while True:
                prefix = bytearray()
                delimiter = b""
                while True:
                    character = stream.read(1)
                    if character in {b"", b"\n", b"="}:
                        delimiter = character
                        break
                    if len(prefix) < 256:
                        prefix.extend(character)
                if not prefix and delimiter == b"":
                    break
                try:
                    name = bytes(prefix).decode("utf-8").strip()
                except UnicodeError as exc:
                    raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc
                if delimiter != b"=" or name not in allowed:
                    while delimiter not in {b"", b"\n"}:
                        delimiter = stream.read(1)
                    if delimiter == b"":
                        break
                    continue
                raw_value = stream.readline()
                try:
                    value = raw_value.rstrip(b"\r\n").decode("utf-8").strip()
                except UnicodeError as exc:
                    raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc
                if name in values:
                    raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in {'"', "'"}
                ):
                    value = value[1:-1]
                if (
                    not value
                    or value != value.strip()
                    or _has_forbidden_control(value)
                ):
                    raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
                values[name] = value
    except Gate4Error:
        raise
    except OSError as exc:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc
    if any(name not in values for name in _MOCK_REQUIRED_INFRASTRUCTURE_NAMES):
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    return values


def write_mock_snapshot_dotenv(
    path: os.PathLike[str] | str,
    infrastructure: Mapping[str, str],
) -> None:
    """Write one ignored snapshot-only dotenv with Provider forced off."""

    unexpected = set(infrastructure) - set(_MOCK_INFRASTRUCTURE_NAMES)
    if unexpected or any(
        name not in infrastructure
        for name in _MOCK_REQUIRED_INFRASTRUCTURE_NAMES
    ):
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    fixed = {
        "APP_ENV": "local",
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": "5432",
        "MINIO_API_PORT": "9000",
        "MINIO_CONSOLE_PORT": "9001",
        "MINIO_ENDPOINT": "http://127.0.0.1:9000",
        "MINIO_SECURE": "false",
        "ZTA35G_RUNTIME_MODE": "mock",
        "ZTA35G_RUNTIME_URL": "http://127.0.0.1:8100",
        "M5_DEV_ROUTES_ENABLED": "false",
    }
    merged = dict(infrastructure)
    merged.update(fixed)
    merged.setdefault("LOG_LEVEL", "INFO")
    merged.setdefault("ZTA35G_RUNTIME_TIMEOUT_SECONDS", "10")
    lines: list[str] = []
    for name in _MOCK_INFRASTRUCTURE_NAMES:
        value = merged.get(name)
        if value is None:
            continue
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or "\n" in value
            or "\r" in value
            or _has_forbidden_control(value)
        ):
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
        lines.append(f"{name}={value}")
    lines.extend(
        (
            "LLM_ADAPTER=mock",
            "DEEPSEEK_API_KEY=",
            "DASHSCOPE_API_KEY=",
            "M12B_REAL_CALLS_AUTHORIZED=",
            "P1B2_GATE4_AUTHORIZED=",
            "P1B2_GATE4_REAL_PROVIDER_AUTHORIZED=",
            "P1B2_GATE4_REAL_RUNTIME_AUTHORIZED=",
            "P1B2_GATE4_BROWSER_AUTHORIZED=",
            "P1B2_GATE4_PROJECT_OWNER_AUTHORIZED=",
        )
    )
    try:
        Path(path).write_text(
            "\n".join(lines) + "\n",
            encoding="utf-8",
            newline="\n",
        )
    except OSError as exc:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc


def _load_mock_settings(environment: Mapping[str, str]) -> object:
    backend_source = environment.get("PYTHONPATH")
    if not isinstance(backend_source, str) or not backend_source:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    inserted = False
    if backend_source not in sys.path:
        sys.path.insert(0, backend_source)
        inserted = True
    try:
        from materialsagent.infrastructure.config import load_settings

        return load_settings(dict(environment))
    except Exception as exc:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc
    finally:
        if inserted:
            sys.path.remove(backend_source)


def _probe_mock_application(settings: object) -> dict[str, int]:
    provider_modules = {
        "materialsagent.infrastructure.llm.factory",
        "materialsagent.infrastructure.llm.langchain_chat",
        "materialsagent.infrastructure.llm.langchain_tool_input",
        "materialsagent.infrastructure.llm.langchain_explanation",
    }
    if any(name in sys.modules for name in provider_modules):
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    try:
        from materialsagent.main import create_app

        create_app(settings=settings)
    except Exception as exc:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc
    if any(name in sys.modules for name in provider_modules):
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    return {
        "provider_adapter_constructions": 0,
        "provider_delegate_calls": 0,
    }


def mock_provider_preflight(
    environment: Mapping[str, str],
    *,
    settings_loader: Callable[[Mapping[str, str]], object] | None = None,
    app_probe: Callable[[object], Mapping[str, object]] | None = None,
) -> dict[str, object]:
    """Fail before collection unless the exact child is provider-free."""

    if (
        environment.get("LLM_ADAPTER") != "mock"
        or environment.get("DEEPSEEK_API_KEY") != ""
        or environment.get("DASHSCOPE_API_KEY") != ""
        or any(name in environment for name in _MOCK_FORBIDDEN_AUTHORIZATION_NAMES)
    ):
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    loader = settings_loader or _load_mock_settings
    probe = app_probe or _probe_mock_application
    try:
        settings = loader(environment)
        if (
            getattr(settings, "llm_adapter", None) != "mock"
            or getattr(settings, "deepseek_api_key", None) is not None
            or getattr(settings, "dashscope_api_key", None) is not None
        ):
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
        facts = dict(probe(settings))
    except Gate4Error:
        raise
    except Exception as exc:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc
    expected = {
        "provider_adapter_constructions": 0,
        "provider_delegate_calls": 0,
    }
    if facts != expected:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    return {"llm_adapter": "mock", **expected}


def run_controlled_mock_command(
    command: Sequence[str],
    *,
    base_environment: Mapping[str, str],
    backend_pythonpath: str,
    preflight: Callable[[Mapping[str, str]], object],
    executor: Callable[[Sequence[str], Mapping[str, str]], int],
) -> int:
    """Run preflight and a Mock command with the same immutable env values."""

    environment = build_mock_subprocess_environment(
        base_environment,
        backend_pythonpath=backend_pythonpath,
    )
    preflight_environment = dict(environment)
    if preflight(preflight_environment) is not True:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    if preflight_environment != environment:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    return int(executor(tuple(command), dict(environment)))


def _run_controlled_mock_helper_tests(repo_root: Path) -> None:
    backend_source = repo_root / "backend" / "src"
    helper = (
        repo_root
        / "backend"
        / "tests"
        / "e2e"
        / "test_real_zta35g_journey.py"
    )
    if not backend_source.is_dir() or not helper.is_file():
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)

    def preflight(environment: Mapping[str, str]) -> bool:
        result = mock_provider_preflight(environment)
        return result == {
            "llm_adapter": "mock",
            "provider_adapter_constructions": 0,
            "provider_delegate_calls": 0,
        }

    def execute(
        command: Sequence[str],
        environment: Mapping[str, str],
    ) -> int:
        try:
            completed = subprocess.run(
                list(command),
                cwd=repo_root,
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                timeout=300,
                check=False,
            )
        except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc
        if completed.stdout:
            sys.stdout.write(completed.stdout)
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        return completed.returncode

    exit_code = run_controlled_mock_command(
        [sys.executable, "-m", "pytest", os.fspath(helper), "-q"],
        base_environment=os.environ,
        backend_pythonpath=os.fspath(backend_source),
        preflight=preflight,
        executor=execute,
    )
    if exit_code != 0:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    print(MOCK_HELPER_TESTS_MARKER)


def _run_captured_command(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    timeout: float,
    failure_marker: str = MOCK_PROVIDER_ISOLATION_FAILED,
) -> tuple[int, str, str]:
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=False,
            timeout=timeout,
            check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
        stdout_bytes = completed.stdout or b""
        stderr_bytes = completed.stderr or b""
        try:
            stdout = stdout_bytes.decode("utf-8")
            stderr = stderr_bytes.decode("utf-8")
        except UnicodeError:
            if os.name != "nt":
                raise
            stdout = stdout_bytes.decode("mbcs")
            stderr = stderr_bytes.decode("mbcs")
        return (
            completed.returncode,
            _normalize_captured_text(stdout),
            _normalize_captured_text(stderr),
        )
    except (
        OSError,
        UnicodeError,
        subprocess.SubprocessError,
    ) as exc:
        raise Gate4Error(failure_marker) from exc


def _normalize_captured_text(value: str) -> str:
    return value.replace("\r\n", "\n").replace("\r", "\n")


def _stream_file_sha256(path: Path) -> str:
    digest = sha256()
    try:
        with path.open("rb") as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                digest.update(block)
    except OSError as exc:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc
    return digest.hexdigest()


def _provider_ledger_fingerprint(repo_root: Path) -> str:
    records: list[str] = []
    temporary = repo_root / "tmp"
    if temporary.is_dir():
        for pattern in ("provider-call-ledger.json", "provider-ledger.json"):
            for path in sorted(temporary.rglob(pattern)):
                if path.is_file():
                    relative = path.relative_to(repo_root).as_posix()
                    records.append(
                        f"{relative}\t{_stream_file_sha256(path)}"
                    )
    return sha256("\n".join(records).encode("utf-8")).hexdigest()


def _extract_head_archive(
    repo_root: Path,
    snapshot_root: Path,
    archive_path: Path,
) -> None:
    system_environment = _system_child_environment(os.environ)
    code, _stdout, _stderr = _run_captured_command(
        [
            "git",
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            EXPECTED_HEAD,
        ],
        cwd=repo_root,
        environment=system_environment,
        timeout=120,
    )
    if code != 0:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    snapshot_root.mkdir(parents=True, exist_ok=False)
    try:
        with tarfile.open(archive_path, mode="r") as archive:
            for member in archive.getmembers():
                parts = member.name.replace("\\", "/").split("/")
                if (
                    not parts
                    or any(part in {"", ".", ".."} for part in parts)
                    or Path(member.name).is_absolute()
                ):
                    raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
                target = snapshot_root.joinpath(*parts)
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if not member.isfile():
                    raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
                with source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
    except (OSError, tarfile.TarError) as exc:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc


def _initialize_snapshot_git(
    repo_root: Path,
    snapshot_root: Path,
) -> None:
    environment = _system_child_environment(os.environ)
    commands = (
        ["git", "init", "--quiet", "--initial-branch=main"],
        ["git", "update-ref", "refs/heads/main", EXPECTED_HEAD],
        ["git", "read-tree", "HEAD"],
    )
    code, objects, _stderr = _run_captured_command(
        ["git", "rev-parse", "--path-format=absolute", "--git-path", "objects"],
        cwd=repo_root,
        environment=environment,
        timeout=30,
    )
    if code != 0:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    object_path = Path(objects.strip())
    if not object_path.is_dir():
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    code, _stdout, _stderr = _run_captured_command(
        commands[0],
        cwd=snapshot_root,
        environment=environment,
        timeout=30,
    )
    if code != 0:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    alternates = snapshot_root / ".git" / "objects" / "info" / "alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text(
        os.fspath(object_path) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for command in commands[1:]:
        code, _stdout, _stderr = _run_captured_command(
            command,
            cwd=snapshot_root,
            environment=environment,
            timeout=30,
        )
        if code != 0:
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    code, head, _stderr = _run_captured_command(
        ["git", "rev-parse", "HEAD"],
        cwd=snapshot_root,
        environment=environment,
        timeout=30,
    )
    if code != 0 or head.strip() != EXPECTED_HEAD:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    code, status, _stderr = _run_captured_command(
        ["git", "status", "--porcelain"],
        cwd=snapshot_root,
        environment=environment,
        timeout=30,
    )
    if code != 0 or status:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)


def _create_directory_junction(link: Path, target: Path) -> None:
    if os.name != "nt" or os.path.lexists(link) or not target.is_dir():
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    link.parent.mkdir(parents=True, exist_ok=True)
    code, _stdout, _stderr = _run_captured_command(
        ["cmd.exe", "/d", "/c", "mklink", "/J", os.fspath(link), os.fspath(target)],
        cwd=link.parent,
        environment=_system_child_environment(os.environ),
        timeout=30,
    )
    if code != 0 or not link.is_dir():
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)


def _remove_directory_junction(link: Path, *, snapshot_root: Path) -> None:
    absolute_link = Path(os.path.abspath(link))
    absolute_snapshot = Path(os.path.abspath(snapshot_root))
    try:
        if os.path.commonpath((absolute_link, absolute_snapshot)) != os.fspath(
            absolute_snapshot
        ):
            raise Gate4Error(CLEANUP_INCOMPLETE)
        if os.path.lexists(absolute_link):
            os.rmdir(absolute_link)
    except (OSError, ValueError) as exc:
        raise Gate4Error(CLEANUP_INCOMPLETE) from exc


def _mock_full_environment(
    infrastructure: Mapping[str, str],
    *,
    backend_source: Path,
) -> dict[str, str]:
    environment = _system_child_environment(os.environ)
    environment.update(dict(infrastructure))
    environment.update(
        {
            "APP_ENV": "local",
            "LLM_ADAPTER": "mock",
            "DEEPSEEK_API_KEY": "",
            "DASHSCOPE_API_KEY": "",
            "PYTHONUTF8": "1",
            "PYTHONPATH": os.fspath(backend_source),
        }
    )
    for name in _MOCK_FORBIDDEN_AUTHORIZATION_NAMES:
        environment.pop(name, None)
    return environment


def _mock_phase1a_environment(
    base_environment: Mapping[str, str],
) -> dict[str, str]:
    """Keep infrastructure in snapshot dotenv, not pytest's parent env."""

    environment = _system_child_environment(base_environment)
    environment.update(
        {
            "LLM_ADAPTER": "mock",
            "DEEPSEEK_API_KEY": "",
            "DASHSCOPE_API_KEY": "",
            "PYTHONUTF8": "1",
        }
    )
    for name in _MOCK_FORBIDDEN_AUTHORIZATION_NAMES:
        environment.pop(name, None)
    return environment


def _provider_llm_call_count(
    *,
    repo_root: Path,
    environment: Mapping[str, str],
) -> int:
    code = (
        "import os,psycopg;"
        "c=psycopg.connect("
        "host=os.environ['POSTGRES_HOST'],"
        "port=os.environ.get('POSTGRES_PORT','5432'),"
        "dbname=os.environ['POSTGRES_DB'],"
        "user=os.environ['POSTGRES_USER'],"
        "password=os.environ['POSTGRES_PASSWORD']);"
        "q=c.cursor();"
        "q.execute(\"SELECT count(*) FROM llm_call "
        "WHERE provider IN ('deepseek','qwen')\");"
        "print(q.fetchone()[0]);"
        "c.close()"
    )
    exit_code, stdout, _stderr = _run_captured_command(
        [sys.executable, "-c", code],
        cwd=repo_root,
        environment=environment,
        timeout=30,
    )
    if exit_code != 0 or not stdout.strip().isdigit():
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    return int(stdout.strip())


def _read_regression_log(snapshot_root: Path, relative: str) -> str:
    path = snapshot_root / relative
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc


def _pytest_passed_count(text: str) -> int:
    matches = re.findall(r"(?m)(\d+) passed(?:, \d+ skipped)?", text)
    if not matches:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    return int(matches[-1])


def _frontend_passed_count(text: str) -> int:
    # Vitest may retain ANSI styling even when its output is redirected.
    plain = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    matches = re.findall(r"(?m)^\s*Tests\s+(\d+) passed(?:\s|$)", plain)
    if not matches:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    return int(matches[-1])


def _materialsagent_compose_container_ids(
    *,
    cwd: Path,
    environment: Mapping[str, str],
) -> tuple[str, ...]:
    """Observe the fixed Mock Compose project without exposing metadata."""

    docker = shutil.which("docker")
    if docker is None:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    code, stdout, _stderr = _run_captured_command(
        [
            docker,
            "ps",
            "--filter",
            "label=com.docker.compose.project=materialsagent",
            "--format",
            "{{.ID}}",
        ],
        cwd=cwd,
        environment=environment,
        timeout=30,
    )
    identifiers = tuple(
        sorted(line.strip() for line in stdout.splitlines() if line.strip())
    )
    if (
        code != 0
        or len(set(identifiers)) != len(identifiers)
        or any(
            re.fullmatch(r"[0-9a-f]{12,64}", identifier) is None
            for identifier in identifiers
        )
    ):
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    return identifiers


def _scan_snapshot_outputs(
    snapshot_root: Path,
    *,
    secret_values: Iterable[str],
    captured_text: Iterable[str],
) -> None:
    encoded = [
        value.encode("utf-8")
        for value in secret_values
        if isinstance(value, str) and len(value) >= 4
    ]
    tracked_code, tracked_output, _stderr = _run_captured_command(
        ["git", "ls-files"],
        cwd=snapshot_root,
        environment=_system_child_environment(os.environ),
        timeout=30,
    )
    if tracked_code != 0:
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
    tracked = set(tracked_output.splitlines())
    for root, directories, files in os.walk(snapshot_root):
        root_path = Path(root)
        relative_root = root_path.relative_to(snapshot_root)
        directories[:] = [
            name
            for name in directories
            if (
                name != ".git"
                and not (
                    relative_root == Path(".") and name == "SEM"
                )
                and not (
                    relative_root == Path("frontend")
                    and name == "node_modules"
                )
            )
        ]
        for name in files:
            path = root_path / name
            relative = path.relative_to(snapshot_root).as_posix()
            if relative in tracked or relative == ".env":
                continue
            try:
                data = path.read_bytes()
            except OSError as exc:
                raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from exc
            if any(secret in data for secret in encoded):
                raise Gate4Error(SECRET_LEAK_MARKER)
            if re.search(
                rb"(?:DEEPSEEK_API_KEY|DASHSCOPE_API_KEY)\s*=\s*[^\s\r\n]",
                data,
            ):
                raise Gate4Error(SECRET_LEAK_MARKER)
    combined = "\n".join(captured_text).encode("utf-8")
    if any(secret in combined for secret in encoded):
        raise Gate4Error(SECRET_LEAK_MARKER)


def _run_controlled_mock_full_regression(repo_root: Path) -> None:
    """Run the existing Phase 1A matrix in a clean, provider-free snapshot."""

    root_dotenv = repo_root / ".env"
    start_hash = _stream_file_sha256(root_dotenv)
    infrastructure = read_mock_infrastructure_dotenv(root_dotenv)
    ledger_before = _provider_ledger_fingerprint(repo_root)
    temporary_root = Path(
        tempfile.mkdtemp(prefix="p1b2-gate4-mock-full-")
    )
    snapshot_root = temporary_root / "repo"
    archive_path = temporary_root / "head.tar"
    sem_link = snapshot_root / "SEM"
    modules_link = snapshot_root / "frontend" / "node_modules"
    stack_attempted = False
    captured: list[str] = []
    evidence: dict[str, object] = {}
    body_error: BaseException | None = None
    failure_stage = "SNAPSHOT"
    failure_commands: list[str] = []
    failure_logs: list[str] = []
    failure_tests: list[str] = []
    try:
        failure_stage = "SNAPSHOT_ARCHIVE"
        _extract_head_archive(
            repo_root,
            snapshot_root,
            archive_path,
        )
        archive_path.unlink()
        failure_stage = "SNAPSHOT_GIT"
        _initialize_snapshot_git(repo_root, snapshot_root)
        failure_stage = "SNAPSHOT_CONFIG"
        write_mock_snapshot_dotenv(
            snapshot_root / ".env",
            infrastructure,
        )
        _create_directory_junction(sem_link, repo_root / "SEM")
        _create_directory_junction(
            modules_link,
            repo_root / "frontend" / "node_modules",
        )
        environment = _mock_full_environment(
            infrastructure,
            backend_source=snapshot_root / "backend" / "src",
        )
        failure_stage = "PROVIDER_PREFLIGHT"
        preflight = mock_provider_preflight(environment)
        if preflight != {
            "llm_adapter": "mock",
            "provider_adapter_constructions": 0,
            "provider_delegate_calls": 0,
        }:
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if powershell is None:
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
        phase_environment = _mock_phase1a_environment(os.environ)
        if _materialsagent_compose_container_ids(
            cwd=snapshot_root,
            environment=phase_environment,
        ):
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
        if any(
            _port_owner_pid(port) is not None
            for port in (3000, 8000, 8100, 5432, 9000, 9001)
        ):
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
        start_command = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            os.fspath(
                snapshot_root / "scripts" / "dev" / "start-mock-stack.ps1"
            ),
        ]
        failure_stage = "MOCK_STACK_START"
        stack_attempted = True
        code, stdout, stderr = _run_captured_command(
            start_command,
            cwd=snapshot_root,
            environment=phase_environment,
            timeout=300,
        )
        captured.extend((stdout, stderr))
        if code != 0 or "MOCK_STACK_STARTED" not in stdout:
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
        provider_before = _provider_llm_call_count(
            repo_root=snapshot_root,
            environment=environment,
        )
        phase_command = [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            os.fspath(
                snapshot_root
                / "scripts"
                / "acceptance"
                / "run-phase-1a.ps1"
            ),
            "-ScopeMilestone",
            "M12A",
        ]
        failure_stage = "PHASE1A"
        code, stdout, stderr = _run_captured_command(
            phase_command,
            cwd=snapshot_root,
            environment=phase_environment,
            timeout=1800,
        )
        captured.extend((stdout, stderr))
        if code != 0 or "PHASE_1A_AUTOMATION_PASSED" not in stdout:
            command_files = list(
                (
                    snapshot_root
                    / "tmp"
                    / "phase-1a-acceptance"
                ).glob("*/commands.json")
            )
            if len(command_files) == 1:
                commands_path = command_files[0]
                try:
                    command_rows = json.loads(
                        commands_path.read_text(encoding="utf-8")
                    )
                    failure_commands = [
                        str(row["name"])
                        for row in command_rows
                        if (
                            isinstance(row, dict)
                            and isinstance(row.get("name"), str)
                            and re.fullmatch(
                                r"[A-Za-z0-9_-]+",
                                str(row["name"]),
                            )
                            and int(row.get("exit_code", 1)) != 0
                        )
                    ]
                    for row in command_rows:
                        if (
                            not isinstance(row, dict)
                            or row.get("name") != "backend_full"
                            or int(row.get("exit_code", 1)) == 0
                        ):
                            continue
                        relative_log = row.get("log")
                        if (
                            not isinstance(relative_log, str)
                            or re.fullmatch(
                                r"tmp/phase-1a-acceptance/"
                                r"[A-Za-z0-9_.:-]+/logs/"
                                r"[A-Za-z0-9_.-]+",
                                relative_log,
                            )
                            is None
                        ):
                            continue
                        failure_text = (
                            snapshot_root / relative_log
                        ).read_text(encoding="utf-8")
                        failure_tests = re.findall(
                            r"(?m)^FAILED "
                            r"([A-Za-z0-9_./\\:-]+)"
                            r"(?: -|$)",
                            failure_text,
                        )
                except (
                    OSError,
                    UnicodeError,
                    ValueError,
                    TypeError,
                    json.JSONDecodeError,
                ):
                    failure_commands = []
                    failure_tests = []
                log_root = commands_path.parent / "logs"
                if log_root.is_dir():
                    failure_logs = [
                        path.name
                        for path in log_root.iterdir()
                        if (
                            path.is_file()
                            and path.stat().st_size > 0
                            and (
                                "fail" in path.name.lower()
                                or "error" in path.name.lower()
                                or "unhandled" in path.name.lower()
                            )
                            and re.fullmatch(
                                r"[A-Za-z0-9_.-]+",
                                path.name,
                            )
                        )
                    ]
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
        run_match = re.search(
            r"(?m)^run_id=([A-Za-z0-9_.:-]+)$",
            stdout,
        )
        summary_match = re.search(
            r"(?m)^summary=(tmp/phase-1a-acceptance/"
            r"[A-Za-z0-9_.:-]+/summary\.json)$",
            stdout,
        )
        if run_match is None or summary_match is None:
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
        run_relative = (
            f"tmp/phase-1a-acceptance/{run_match.group(1)}"
        )
        provider_after = _provider_llm_call_count(
            repo_root=snapshot_root,
            environment=environment,
        )
        if provider_after != provider_before:
            raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED)
        evidence = {"run_id": run_match.group(1)}
        failure_stage = "PARSE_BACKEND_E2E"
        evidence["backend_e2e"] = _pytest_passed_count(
            _read_regression_log(
                snapshot_root,
                f"{run_relative}/logs/10-backend-m11-e2e.log",
            )
        )
        failure_stage = "PARSE_BACKEND_FULL"
        evidence["backend_full"] = _pytest_passed_count(
            _read_regression_log(
                snapshot_root,
                f"{run_relative}/logs/11-backend-test.log",
            )
        )
        failure_stage = "PARSE_MOCK_RUNTIME"
        evidence["mock_runtime"] = _pytest_passed_count(
            _read_regression_log(
                snapshot_root,
                f"{run_relative}/logs/12-runtime-test.log",
            )
        )
        failure_stage = "PARSE_FRONTEND"
        evidence["frontend"] = _read_regression_log(
            snapshot_root,
            f"{run_relative}/logs/15-frontend-test.log",
        )
        evidence["provider_delta"] = provider_after - provider_before
        evidence["frontend"] = _frontend_passed_count(
            str(evidence["frontend"])
        )
        failure_stage = "SECRET_SCAN"
        _scan_snapshot_outputs(
            snapshot_root,
            secret_values=(
                infrastructure[name]
                for name in (
                    "POSTGRES_PASSWORD",
                    "MINIO_ACCESS_KEY",
                    "MINIO_SECRET_KEY",
                    "ZTA35G_RUNTIME_TOKEN",
                    "TIMELINE_CURSOR_SIGNING_KEY",
                )
            ),
            captured_text=captured,
        )
    except BaseException as exc:
        body_error = exc
    cleanup_failed = False
    try:
        if stack_attempted:
            powershell = shutil.which("powershell") or shutil.which("pwsh")
            if powershell is None:
                raise Gate4Error(CLEANUP_INCOMPLETE)
            phase_environment = _mock_phase1a_environment(os.environ)
            code, stdout, stderr = _run_captured_command(
                [
                    powershell,
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    os.fspath(
                        snapshot_root
                        / "scripts"
                        / "dev"
                        / "stop-mock-stack.ps1"
                    ),
                ],
                cwd=snapshot_root,
                environment=phase_environment,
                timeout=300,
                failure_marker=CLEANUP_INCOMPLETE,
            )
            captured.extend((stdout, stderr))
            if code != 0:
                raise Gate4Error(CLEANUP_INCOMPLETE)
        for port in (3000, 8000, 8100, 5432, 9000, 9001):
            _wait_port(port, listening=False, timeout=30.0)
        if stack_attempted and _materialsagent_compose_container_ids(
            cwd=snapshot_root,
            environment=_mock_phase1a_environment(os.environ),
        ):
            raise Gate4Error(CLEANUP_INCOMPLETE)
        if os.path.lexists(modules_link):
            _remove_directory_junction(
                modules_link,
                snapshot_root=snapshot_root,
            )
        if os.path.lexists(sem_link):
            _remove_directory_junction(
                sem_link,
                snapshot_root=snapshot_root,
            )
        expected_parent = Path(tempfile.gettempdir()).resolve()
        absolute_temporary = Path(os.path.abspath(temporary_root))
        if (
            os.path.commonpath((absolute_temporary, expected_parent))
            != os.fspath(expected_parent)
            or not absolute_temporary.name.startswith(
                "p1b2-gate4-mock-full-"
            )
        ):
            raise Gate4Error(CLEANUP_INCOMPLETE)
        shutil.rmtree(absolute_temporary)
    except BaseException:
        cleanup_failed = True
    if (
        cleanup_failed
        or temporary_root.exists()
        or _stream_file_sha256(root_dotenv) != start_hash
        or _provider_ledger_fingerprint(repo_root) != ledger_before
    ):
        raise Gate4Error(CLEANUP_INCOMPLETE) from body_error
    if body_error is not None:
        print(
            f"P1B2_GATE4_MOCK_FULL_FAILURE_STAGE={failure_stage}",
            file=sys.stderr,
        )
        if failure_commands:
            print(
                "P1B2_GATE4_MOCK_FULL_FAILURE_COMMANDS="
                + ",".join(sorted(set(failure_commands))),
                file=sys.stderr,
            )
        if failure_logs:
            print(
                "P1B2_GATE4_MOCK_FULL_FAILURE_LOGS="
                + ",".join(sorted(set(failure_logs))),
                file=sys.stderr,
            )
        if failure_tests:
            print(
                "P1B2_GATE4_MOCK_FULL_FAILURE_TESTS="
                + ",".join(sorted(set(failure_tests))),
                file=sys.stderr,
            )
        if isinstance(body_error, Gate4Error):
            raise body_error
        raise Gate4Error(MOCK_PROVIDER_ISOLATION_FAILED) from body_error
    print(f"P1B2_GATE4_PHASE1A_RUN_ID={evidence['run_id']}")
    print(f"P1B2_GATE4_BACKEND_E2E_PASSED={evidence['backend_e2e']}")
    print(f"P1B2_GATE4_BACKEND_FULL_PASSED={evidence['backend_full']}")
    print(f"P1B2_GATE4_MOCK_RUNTIME_PASSED={evidence['mock_runtime']}")
    print(f"P1B2_GATE4_FRONTEND_PASSED={evidence['frontend']}")
    print("P1B2_GATE4_ALEMBIC_PASSED=1")
    print("P1B2_GATE4_PIP_CHECK_PASSED=1")
    print("P1B2_GATE4_COMPILEALL_PASSED=1")
    print("P1B2_GATE4_FRONTEND_TYPECHECK_PASSED=1")
    print("P1B2_GATE4_FRONTEND_BUILD_PASSED=1")
    print("P1B2_GATE4_PROVIDER_LLM_CALL_DELTA=0")
    print("P1B2_GATE4_PROVIDER_ADAPTER_CONSTRUCTIONS=0")
    print("P1B2_GATE4_SECRET_SCAN_PASSED=1")
    print("P1B2_GATE4_MOCK_FULL_CLEANUP_PASSED=1")
    print(MOCK_FULL_REGRESSION_MARKER)


def build_child_command(
    *,
    role: str,
    executable: os.PathLike[str] | str,
    arguments: Sequence[str],
    secret_values: Iterable[str] = (),
) -> list[str]:
    """Build argv from non-secret executable arguments only."""

    if role not in {
        "runtime",
        "backend-real",
        "backend-invalid",
        "backend",
        "frontend",
        "compose",
        "database",
        "bootstrap",
        "minio",
        "log",
    }:
        raise Gate4Error("P1B2_GATE4_UNKNOWN_CHILD_ROLE")
    executable_text = os.fspath(executable)
    if not executable_text or "\x00" in executable_text:
        raise Gate4Error("P1B2_GATE4_INVALID_CHILD_COMMAND")
    command_parts = [executable_text]
    for argument in arguments:
        argument_text = os.fspath(argument)
        if "\x00" in argument_text:
            raise Gate4Error("P1B2_GATE4_INVALID_CHILD_COMMAND")
        command_parts.append(argument_text)
    protected = [
        value for value in secret_values if isinstance(value, str) and value
    ]
    if any(
        value in command_part
        for command_part in command_parts
        for value in protected
    ):
        raise Gate4Error(SECRET_IN_CHILD_COMMAND)
    return command_parts


def generate_invalid_provider_key(run_id: str, real_provider_key: str) -> str:
    """Generate an in-memory invalid credential unrelated to the real key."""

    if not _valid_required_key(real_provider_key):
        raise _fixed_key_failure()
    safe_run_id = re.sub(r"[^A-Za-z0-9_-]", "-", run_id).strip("-")
    if not safe_run_id:
        safe_run_id = "run"
    while True:
        candidate = f"sk-invalid-p1b2-{safe_run_id}-{secrets.token_hex(16)}"
        if candidate != real_provider_key and real_provider_key not in candidate:
            return candidate


class BudgetLedger:
    """Runtime/DDPM counters; Provider attempts use ProviderDelegateLedger."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self.runtime_delegates = 0
        self.ddpm_delegates = 0

    @classmethod
    def from_persisted(
        cls,
        run_id: str,
        budgets: Mapping[str, object],
    ) -> BudgetLedger:
        if (
            not _is_safe_identifier(run_id)
            or not isinstance(budgets, Mapping)
            or set(budgets) != {"runtime", "ddpm"}
        ):
            raise Gate4Error(INVALID_STATE)
        limits = {"runtime": 2, "ddpm": 2}
        for name, limit in limits.items():
            value = budgets[name]
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= limit
            ):
                raise Gate4Error(INVALID_STATE)
        ledger = cls(run_id)
        ledger.runtime_delegates = int(budgets["runtime"])
        ledger.ddpm_delegates = int(budgets["ddpm"])
        return ledger

    def delegate_runtime(self, delegate: Callable[[], Any]) -> Any:
        if self.runtime_delegates >= 2:
            raise Gate4Error(RUNTIME_BUDGET_ERROR)
        self.runtime_delegates += 1
        return delegate()

    def delegate_ddpm(self, delegate: Callable[[], Any]) -> Any:
        if self.ddpm_delegates >= 2:
            raise Gate4Error(RUNTIME_BUDGET_ERROR)
        self.ddpm_delegates += 1
        return delegate()

    def safe_snapshot(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "runtime_delegates": self.runtime_delegates,
            "ddpm_delegates": self.ddpm_delegates,
        }

    def persisted_budgets(self) -> dict[str, int]:
        return {
            "runtime": self.runtime_delegates,
            "ddpm": self.ddpm_delegates,
        }


_PROVIDER_LEDGER_FIELDS = {
    "schema_version",
    "run_id",
    "chat_delegate_attempts",
    "explanation_delegate_attempts",
    "total_delegate_attempts",
    "maximum_delegate_attempts",
    "state",
    "updated_at",
}
_PROVIDER_LEDGER_STATES = {"EXACT", "UNKNOWN", "INVALIDATED"}


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_provider_ledger_payload(
    payload: object,
) -> dict[str, object]:
    if not isinstance(payload, dict) or set(payload) != _PROVIDER_LEDGER_FIELDS:
        raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
    if (
        payload["schema_version"] != "1.0"
        or not _is_safe_identifier(payload["run_id"])
        or payload["state"] not in _PROVIDER_LEDGER_STATES
        or payload["maximum_delegate_attempts"] != 5
    ):
        raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
    counters = (
        "chat_delegate_attempts",
        "explanation_delegate_attempts",
        "total_delegate_attempts",
    )
    if any(
        isinstance(payload[name], bool)
        or not isinstance(payload[name], int)
        or payload[name] < 0
        for name in counters
    ):
        raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
    if payload["total_delegate_attempts"] != (
        payload["chat_delegate_attempts"]
        + payload["explanation_delegate_attempts"]
    ):
        raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
    if payload["state"] == "EXACT" and (
        payload["chat_delegate_attempts"] > 3
        or payload["explanation_delegate_attempts"] > 2
        or payload["total_delegate_attempts"] > 5
    ):
        raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
    updated_at = payload["updated_at"]
    if not isinstance(updated_at, str) or not updated_at.endswith("Z"):
        raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
    try:
        datetime.fromisoformat(updated_at[:-1] + "+00:00")
    except ValueError as exc:
        raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN) from exc
    return dict(payload)


def _new_provider_ledger_payload(
    *,
    run_id: str,
    chat: int = 0,
    explanation: int = 0,
    state: str = "EXACT",
) -> dict[str, object]:
    payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "chat_delegate_attempts": chat,
        "explanation_delegate_attempts": explanation,
        "total_delegate_attempts": chat + explanation,
        "maximum_delegate_attempts": 5,
        "state": state,
        "updated_at": _utc_timestamp(),
    }
    return _validate_provider_ledger_payload(payload)


def write_provider_ledger_atomic(
    path: os.PathLike[str] | str,
    payload: Mapping[str, object],
) -> None:
    """Write the exact safe ledger schema through fsync + atomic replace."""

    candidate = Path(path)
    validated = _validate_provider_ledger_payload(dict(payload))
    serialized = json.dumps(
        validated,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    candidate.parent.mkdir(parents=True, exist_ok=True)
    _assert_path_has_no_reparse(
        candidate.parent,
        failure_marker=PROVIDER_CALL_COUNT_UNKNOWN,
    )
    _atomic_write_bytes(
        candidate,
        serialized,
        failure_marker=PROVIDER_CALL_COUNT_UNKNOWN,
    )


class _ProviderLedgerLock:
    def __init__(self, path: Path, *, timeout: float = 5.0) -> None:
        self._path = path.with_name(f"{path.name}.lock")
        self._timeout = timeout
        self._descriptor: int | None = None

    def __enter__(self) -> _ProviderLedgerLock:
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                self._descriptor = os.open(
                    self._path,
                    os.O_WRONLY
                    | os.O_CREAT
                    | os.O_EXCL
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                os.write(self._descriptor, b"locked")
                os.fsync(self._descriptor)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                time.sleep(0.01)
            except OSError as exc:
                raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN) from exc

    def __exit__(self, *_args: object) -> None:
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN) from exc


class ProviderDelegateLedger:
    """Authoritative conservative counter at the Provider delegate boundary."""

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        run_id: str,
        expected_minimum_total: int = 0,
        atomic_writer: Callable[
            [os.PathLike[str] | str, Mapping[str, object]], None
        ] = write_provider_ledger_atomic,
    ) -> None:
        if (
            not _is_safe_identifier(run_id)
            or isinstance(expected_minimum_total, bool)
            or not isinstance(expected_minimum_total, int)
            or expected_minimum_total < 0
        ):
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
        self.path = Path(path)
        self.run_id = run_id
        self._minimum_total = expected_minimum_total
        self._atomic_writer = atomic_writer

    @classmethod
    def initialize(
        cls,
        path: os.PathLike[str] | str,
        *,
        run_id: str,
    ) -> dict[str, object]:
        candidate = Path(path)
        if os.path.lexists(candidate):
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
        payload = _new_provider_ledger_payload(run_id=run_id)
        write_provider_ledger_atomic(candidate, payload)
        reread = cls(candidate, run_id=run_id).read()
        if reread != payload:
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
        return reread

    def _read_raw(self) -> dict[str, object]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return _validate_provider_ledger_payload(payload)
        except Gate4Error:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN) from exc

    def _mark_unknown(
        self,
        last_known: Mapping[str, object] | None = None,
    ) -> None:
        run_id = self.run_id
        chat = 0
        explanation = 0
        if isinstance(last_known, Mapping):
            candidate_run_id = last_known.get("run_id")
            if _is_safe_identifier(candidate_run_id):
                run_id = str(candidate_run_id)
            candidate_chat = last_known.get("chat_delegate_attempts")
            candidate_explanation = last_known.get(
                "explanation_delegate_attempts"
            )
            if type(candidate_chat) is int and candidate_chat >= 0:
                chat = candidate_chat
            if type(candidate_explanation) is int and candidate_explanation >= 0:
                explanation = candidate_explanation
        payload = _new_provider_ledger_payload(
            run_id=run_id,
            chat=chat,
            explanation=explanation,
            state="UNKNOWN",
        )
        try:
            self._atomic_writer(self.path, payload)
        except BaseException:
            pass

    def read(self, *, allow_non_exact: bool = False) -> dict[str, object]:
        try:
            payload = self._read_raw()
        except Gate4Error:
            self._mark_unknown()
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
        if payload["run_id"] != self.run_id:
            self._mark_unknown(payload)
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
        if not allow_non_exact and payload["state"] != "EXACT":
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
        if payload["total_delegate_attempts"] < self._minimum_total:
            self._mark_unknown(payload)
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
        return payload

    def delegate(
        self,
        kind: str,
        delegate: Callable[[], Any],
        *,
        fact_reader: Callable[[], Mapping[str, int]],
    ) -> Any:
        normalized = kind.upper()
        if normalized not in {"CHAT", "EXPLANATION"}:
            raise Gate4Error("P1B2_GATE4_INVALID_PROVIDER_KIND")
        try:
            with _ProviderLedgerLock(self.path):
                try:
                    current = self._read_raw()
                except Gate4Error:
                    self._mark_unknown()
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                if (
                    current["run_id"] != self.run_id
                    or current["total_delegate_attempts"] < self._minimum_total
                ):
                    self._mark_unknown(current)
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                if current["state"] == "INVALIDATED":
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                if current["state"] != "EXACT":
                    self._mark_unknown(current)
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                per_kind_limit = {"CHAT": 3, "EXPLANATION": 2}[normalized]
                counter_name = (
                    "chat_delegate_attempts"
                    if normalized == "CHAT"
                    else "explanation_delegate_attempts"
                )
                if (
                    current["total_delegate_attempts"]
                    >= current["maximum_delegate_attempts"]
                    or current[counter_name] >= per_kind_limit
                ):
                    raise Gate4Error(PROVIDER_BUDGET_ERROR)
                reserved = dict(current)
                reserved[counter_name] = int(reserved[counter_name]) + 1
                reserved["total_delegate_attempts"] = (
                    int(reserved["chat_delegate_attempts"])
                    + int(reserved["explanation_delegate_attempts"])
                )
                # UNKNOWN is an in-flight reservation.  A hard process exit
                # from this point onward can conservatively over-count but
                # can never reopen the budget as an EXACT run.
                reserved["state"] = "UNKNOWN"
                reserved["updated_at"] = _utc_timestamp()
                try:
                    self._atomic_writer(self.path, reserved)
                    confirmed = self._read_raw()
                except BaseException:
                    self._mark_unknown(current)
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                if confirmed != _validate_provider_ledger_payload(reserved):
                    self._mark_unknown(confirmed)
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                try:
                    facts = dict(fact_reader())
                except BaseException:
                    self._mark_unknown(confirmed)
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                if (
                    set(facts) != {"CHAT", "EXPLANATION"}
                    or any(type(value) is not int or value < 0 for value in facts.values())
                    or facts["CHAT"] != confirmed["chat_delegate_attempts"]
                    or facts["EXPLANATION"]
                    != confirmed["explanation_delegate_attempts"]
                ):
                    self._mark_unknown(confirmed)
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                self._minimum_total = int(confirmed["total_delegate_attempts"])
        except Gate4Error:
            raise
        except BaseException as exc:
            self._mark_unknown()
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN) from exc
        try:
            result = delegate()
        except Exception:
            self._complete_reserved_attempt(
                reserved,
                fact_reader=fact_reader,
            )
            raise
        except BaseException:
            # SystemExit/KeyboardInterrupt model an abnormal process boundary.
            # Leave the durable reservation UNKNOWN for the next Backend.
            raise
        self._complete_reserved_attempt(
            reserved,
            fact_reader=fact_reader,
        )
        return result

    def _complete_reserved_attempt(
        self,
        reserved: Mapping[str, object],
        *,
        fact_reader: Callable[[], Mapping[str, int]],
    ) -> None:
        """Finalize one returned Adapter attempt without weakening UNKNOWN."""

        try:
            with _ProviderLedgerLock(self.path):
                current = self._read_raw()
                if current != _validate_provider_ledger_payload(
                    dict(reserved)
                ):
                    self._mark_unknown(current)
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                try:
                    facts = dict(fact_reader())
                except BaseException:
                    self._mark_unknown(current)
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                if (
                    set(facts) != {"CHAT", "EXPLANATION"}
                    or any(
                        type(value) is not int or value < 0
                        for value in facts.values()
                    )
                    or facts["CHAT"] != current["chat_delegate_attempts"]
                    or facts["EXPLANATION"]
                    != current["explanation_delegate_attempts"]
                ):
                    self._mark_unknown(current)
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                completed = dict(current)
                completed["state"] = "EXACT"
                completed["updated_at"] = _utc_timestamp()
                self._atomic_writer(self.path, completed)
                confirmed = self._read_raw()
                if confirmed != _validate_provider_ledger_payload(completed):
                    self._mark_unknown(confirmed)
                    raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
                self._minimum_total = int(
                    confirmed["total_delegate_attempts"]
                )
        except Gate4Error:
            raise
        except BaseException as exc:
            self._mark_unknown(reserved)
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN) from exc


def write_invalidated_provider_ledger(
    path: os.PathLike[str] | str,
    *,
    run_id: str,
    minimum_chat_delegate_attempts: int,
) -> None:
    if (
        isinstance(minimum_chat_delegate_attempts, bool)
        or not isinstance(minimum_chat_delegate_attempts, int)
        or minimum_chat_delegate_attempts < 1
    ):
        raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
    write_provider_ledger_atomic(
        path,
        _new_provider_ledger_payload(
            run_id=run_id,
            chat=minimum_chat_delegate_attempts,
            state="INVALIDATED",
        ),
    )


def invalidate_provider_ledger(
    path: Path,
    *,
    run_id: str,
) -> dict[str, object]:
    """Preserve all observed attempts while permanently invalidating a run."""

    try:
        payload = _validate_provider_ledger_payload(
            json.loads(path.read_text(encoding="utf-8"))
        )
        if payload["run_id"] != run_id:
            raise ValueError("run mismatch")
        payload["state"] = "INVALIDATED"
        payload["updated_at"] = _utc_timestamp()
        write_provider_ledger_atomic(path, payload)
        confirmed = _validate_provider_ledger_payload(
            json.loads(path.read_text(encoding="utf-8"))
        )
        if confirmed != payload:
            raise ValueError("invalidation verification failed")
        return confirmed
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN) from exc


class BudgetedChatOrchestrationPort:
    def __init__(
        self,
        adapter: object,
        ledger: ProviderDelegateLedger,
        fact_reader: Callable[[], Mapping[str, int]],
    ) -> None:
        self._adapter = adapter
        self._ledger = ledger
        self._fact_reader = fact_reader
        self.provider = getattr(adapter, "provider")
        self.model_name = getattr(adapter, "model_name")

    def request_metadata(self, value: object) -> Any:
        return self._adapter.request_metadata(value)

    def orchestrate(self, value: object) -> Any:
        return self._ledger.delegate(
            "CHAT",
            lambda: self._adapter.orchestrate(value),
            fact_reader=self._fact_reader,
        )


class BudgetedExplanationPort:
    def __init__(
        self,
        adapter: object,
        ledger: ProviderDelegateLedger,
        fact_reader: Callable[[], Mapping[str, int]],
    ) -> None:
        self._adapter = adapter
        self._ledger = ledger
        self._fact_reader = fact_reader
        for name in (
            "provider",
            "model_name",
            "prompt_template_id",
            "prompt_template_version",
        ):
            setattr(self, name, getattr(adapter, name))

    def request_metadata(self, value: object) -> Any:
        return self._adapter.request_metadata(value)

    def explain(self, value: object) -> Any:
        return self._ledger.delegate(
            "EXPLANATION",
            lambda: self._adapter.explain(value),
            fact_reader=self._fact_reader,
        )


def build_budgeted_backend_application(
    *,
    settings: object,
    chat_adapter: object,
    explanation_adapter: object,
    tool_input_adapter: object | None = None,
    ledger: ProviderDelegateLedger,
    fact_reader: Callable[[], Mapping[str, int]],
    app_factory: Callable[..., Any],
) -> Any:
    """Inject both budget wrappers through the existing application factory."""

    try:
        options: dict[str, object] = {
            "settings": settings,
            "chat_orchestration_port": BudgetedChatOrchestrationPort(
                chat_adapter,
                ledger,
                fact_reader,
            ),
            "explanation_port": BudgetedExplanationPort(
                explanation_adapter,
                ledger,
                fact_reader,
            ),
        }
        if tool_input_adapter is not None:
            options["tool_input_extraction_port"] = tool_input_adapter
        return app_factory(
            **options,
        )
    except TypeError as exc:
        raise Gate4Error(PROVIDER_LEDGER_INJECTION_BLOCKED) from exc


def _provider_facts_from_rows(
    rows: Iterable[Sequence[object]],
) -> dict[str, int]:
    facts = {"CHAT": 0, "EXPLANATION": 0}
    purposes = {
        "CHAT_ORCHESTRATION": "CHAT",
        "TOOL_RESULT_EXPLANATION": "EXPLANATION",
    }
    for row in rows:
        try:
            purpose = str(row[0])
            count = int(row[1])
            kind = purposes[purpose]
        except (IndexError, KeyError, TypeError, ValueError) as exc:
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN) from exc
        if count < 0:
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
        facts[kind] = count
    return facts


def _build_provider_database_fact_reader(
    settings: object,
) -> tuple[Callable[[], Mapping[str, int]], object]:
    try:
        from sqlalchemy import text
        from materialsagent.infrastructure.db.session import (
            create_engine_from_settings,
        )

        engine = create_engine_from_settings(settings)
    except Exception as exc:
        raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN) from exc

    def read() -> Mapping[str, int]:
        try:
            with engine.connect() as connection:
                rows = connection.execute(
                    text(
                        """
                        SELECT purpose, COUNT(*)
                        FROM llm_call
                        WHERE provider IN ('deepseek', 'qwen')
                        GROUP BY purpose
                        """
                    )
                ).all()
            return _provider_facts_from_rows(rows)
        except Gate4Error:
            raise
        except Exception as exc:
            raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN) from exc

    return read, engine


def build_real_budgeted_backend_from_environment(
    environment: Mapping[str, str],
) -> object:
    """Build the real Backend only through explicitly injected budget wrappers."""

    if (
        environment.get("P1B2_GATE4_FRESH_STAGE_C_AUTHORIZED")
        != "PROJECT_OWNER_AUTHORIZED"
        or environment.get("P1B2_GATE4_PROVIDER_LEDGER_REQUIRED") != "1"
    ):
        raise Gate4Error(NEW_STAGE_C_NOT_AUTHORIZED)
    try:
        from materialsagent.infrastructure.config import load_settings
        from materialsagent.infrastructure.llm.configuration import (
            load_llm_configuration,
        )
        from materialsagent.infrastructure.llm.langchain_chat import (
            LangChainChatOrchestrationAdapter,
        )
        from materialsagent.infrastructure.llm.langchain_explanation import (
            LangChainExplanationAdapter,
        )
        from materialsagent.infrastructure.llm.langchain_tool_input import (
            LangChainToolInputExtractionAdapter,
        )
        from materialsagent.main import create_app

        settings = load_settings(dict(environment))
        if settings.llm_adapter != "provider":
            raise Gate4Error(PROVIDER_LEDGER_INJECTION_BLOCKED)
        configuration = load_llm_configuration(settings)
        run_id = environment["P1B2_GATE4_PROVIDER_LEDGER_RUN_ID"]
        ledger_path = Path(
            environment["P1B2_GATE4_PROVIDER_LEDGER_PATH"]
        ).resolve()
        expected_root = _run_root(_repo_root()).resolve()
        if (
            not _is_safe_identifier(run_id)
            or not _is_relative_to(ledger_path, expected_root)
        ):
            raise Gate4Error(PROVIDER_LEDGER_INJECTION_BLOCKED)
        ledger = ProviderDelegateLedger(ledger_path, run_id=run_id)
        ledger.read()
        fact_reader, fact_engine = _build_provider_database_fact_reader(
            settings
        )
        app = build_budgeted_backend_application(
            settings=settings,
            chat_adapter=LangChainChatOrchestrationAdapter(
                configuration.for_role("chat_orchestration")
            ),
            explanation_adapter=LangChainExplanationAdapter(
                configuration.for_role("tool_result_explanation")
            ),
            tool_input_adapter=LangChainToolInputExtractionAdapter(
                configuration.for_role("tool_input_extraction")
            ),
            ledger=ledger,
            fact_reader=fact_reader,
            app_factory=create_app,
        )
        app.state.p1b2_provider_fact_engine = fact_engine
        return app
    except Gate4Error:
        raise
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise Gate4Error(PROVIDER_LEDGER_INJECTION_BLOCKED) from exc


def serve_real_budgeted_backend_from_environment(
    environment: Mapping[str, str],
) -> None:
    """Start the already-authorized local Backend on its fixed loopback port."""

    try:
        import uvicorn

        app = build_real_budgeted_backend_from_environment(environment)
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=8000,
            log_level="info",
        )
    except Gate4Error:
        raise
    except Exception as exc:
        raise Gate4Error(PROVIDER_LEDGER_INJECTION_BLOCKED) from exc


class Phase1Coordinator:
    """Pure coordinator used to prove the Phase 1 pause protocol offline."""

    def __init__(
        self,
        *,
        state: Mapping[str, object],
        stores: object,
        processes: object,
        state_writer: object,
        controlled: Mapping[str, str],
    ) -> None:
        if state.get("phase") != "STAGEB_COMPLETE":
            raise Gate4Error("P1B2_GATE4_PHASE1_INVALID_RESUME")
        budgets = state.get("budgets")
        if budgets != {
            "runtime": 1,
            "ddpm": 1,
        }:
            raise Gate4Error("P1B2_GATE4_PHASE1_INVALID_RESUME")
        run_id = state.get("run_id")
        if not isinstance(run_id, str) or not _is_safe_identifier(run_id):
            raise Gate4Error("P1B2_GATE4_PHASE1_INVALID_RESUME")
        self.run_id = run_id
        self.budgets = dict(budgets)
        self._stores = stores
        self._processes = processes
        self._state_writer = state_writer
        self._controlled = dict(controlled)
        self._started = False

    def _environment(
        self, role: str, resources: Mapping[str, str]
    ) -> dict[str, str]:
        if role == "backend-real":
            return {
                "DEEPSEEK_API_KEY": _controlled_value(
                    self._controlled, "real_provider_key"
                ),
                "ZTA35G_RUNTIME_TOKEN": _controlled_value(
                    self._controlled, "runtime_token"
                ),
                "ZTA35G_RUNTIME_URL": RUNTIME_URL,
                "ZTA35G_RUNTIME_TIMEOUT_SECONDS": "900",
                "POSTGRES_DB": resources["database"],
                "POSTGRES_PASSWORD": _controlled_value(
                    self._controlled, "database_password"
                ),
                "MINIO_BUCKET": resources["bucket"],
                "MINIO_SECRET_KEY": _controlled_value(
                    self._controlled, "minio_secret"
                ),
                "LOCAL_ACTOR_ID": resources["actor"],
                "TIMELINE_CURSOR_SIGNING_KEY": _controlled_value(
                    self._controlled, "timeline_signing_key"
                ),
            }
        if role in {"compose", "postgres"}:
            return {
                "POSTGRES_PASSWORD": _controlled_value(
                    self._controlled, "database_password"
                )
            }
        if role == "minio":
            return {
                "MINIO_SECRET_KEY": _controlled_value(
                    self._controlled, "minio_secret"
                )
            }
        if role == "frontend":
            return {"MATERIALSAGENT_BACKEND_ORIGIN": BACKEND_URL}
        raise Gate4Error("P1B2_GATE4_UNKNOWN_CHILD_ROLE")

    def prepare_browser_pause(self, *, output: Any) -> dict[str, object]:
        if self._started:
            raise Gate4Error("P1B2_GATE4_PHASE1_ALREADY_STARTED")
        self._started = True
        resources: dict[str, str] = {}
        handles: list[object] = []
        try:
            resources["database"] = self._stores.create_database(self.run_id)
            resources["bucket"] = self._stores.create_bucket(self.run_id)
            resources["actor"] = self._stores.create_actor(self.run_id)
            resources["conversation"] = self._stores.create_conversation(
                self.run_id
            )
            for role in (
                "compose",
                "postgres",
                "minio",
                "backend-real",
                "frontend",
            ):
                handles.append(
                    self._processes.start(
                        role, self._environment(role, resources)
                    )
                )
            pause = {
                "run_id": self.run_id,
                "phase": "PHASE1_READY",
                "budgets": dict(self.budgets),
                "resources": resources,
            }
            payload = json.dumps(
                pause, ensure_ascii=False, allow_nan=False, sort_keys=True
            )
            if scan_artifact_text(
                payload, _all_controlled_values(self._controlled)
            ):
                raise Gate4Error(SECRET_LEAK_MARKER)
            self._state_writer.write(pause)
            expected_ports = ((8000, True), (3000, True), (8100, False))
            for port, expected in expected_ports:
                if self._processes.port_is_listening(port) is not expected:
                    raise Gate4Error(PHASE1_FAILED)
            output.write(f"{BROWSER_READY_MARKER}\n")
            return pause
        except BaseException as exc:
            cleanup_failed = False
            for handle in reversed(handles):
                try:
                    self._processes.stop(handle)
                except BaseException:
                    cleanup_failed = True
            if "bucket" in resources:
                try:
                    self._stores.delete_bucket(resources["bucket"])
                except BaseException:
                    cleanup_failed = True
            if "database" in resources:
                try:
                    self._stores.delete_database(resources["database"])
                except BaseException:
                    cleanup_failed = True
            invalidated = {
                "run_id": self.run_id,
                "phase": "INVALIDATED",
                "budgets": dict(self.budgets),
                "resources": {},
            }
            try:
                self._state_writer.write(invalidated)
            except BaseException:
                cleanup_failed = True
            if cleanup_failed:
                raise Gate4Error(CLEANUP_INCOMPLETE) from exc
            raise Gate4Error(PHASE1_FAILED) from exc


def audit_backend_adapter(adapter: object) -> bool:
    """Offline canary: prove one injected pool call uses total=900/no retries."""

    timeout = getattr(adapter, "_timeout", None)
    if (
        timeout is None
        or getattr(timeout, "total", None) != 900
        or getattr(adapter, "_retries", None) is not False
    ):
        raise Gate4Error("P1B2_GATE4_BACKEND_ADAPTER_CANARY_FAILED")
    canary = getattr(adapter, "canary", None)
    if not callable(canary):
        raise Gate4Error("P1B2_GATE4_BACKEND_ADAPTER_CANARY_FAILED")
    canary()
    pool = getattr(adapter, "_pool", None)
    requests = getattr(pool, "requests", None)
    if not isinstance(requests, list) or len(requests) != 1:
        raise Gate4Error("P1B2_GATE4_BACKEND_ADAPTER_CANARY_FAILED")
    captured = requests[0]
    if (
        captured.get("timeout") is not timeout
        or captured.get("retries") is not False
    ):
        raise Gate4Error("P1B2_GATE4_BACKEND_ADAPTER_CANARY_FAILED")
    return True


def new_safe_state(*, run_id: str, dotenv_sha256: str) -> dict[str, object]:
    """Create the only state shape permitted for a real run."""

    state: dict[str, object] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "phase": "STAGEB_STARTING",
        "dotenv_sha256": dotenv_sha256,
        "resources": {},
        "budgets": {
            "runtime": 0,
            "ddpm": 0,
        },
        "docker": {
            "postgresql": {
                "owned": False,
                "preexisting": False,
                "container_id": None,
            },
            "minio": {
                "owned": False,
                "preexisting": False,
                "container_id": None,
            },
        },
        "processes": {},
        "artifacts": {"audit_json": None},
    }
    validate_safe_state(state)
    return state


def new_stage_c_resume_state(
    *,
    run_id: str,
    dotenv_sha256: str,
) -> dict[str, object]:
    """Seed a new Stage C run from the accepted Stage B budget facts."""

    state = new_safe_state(
        run_id=run_id,
        dotenv_sha256=dotenv_sha256,
    )
    state["phase"] = "STAGEB_COMPLETE"
    state["budgets"] = {
        "runtime": 1,
        "ddpm": 1,
    }
    validate_safe_state(state)
    return state


def _is_safe_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SAFE_ID.fullmatch(value) is not None
    )


def _executable_path_digest(executable_path: str | os.PathLike[str]) -> str:
    value = os.fspath(executable_path)
    if re.match(r"^[A-Za-z]:[\\/]", value) or "\\" in value:
        normalized = ntpath.normcase(ntpath.normpath(value))
    else:
        normalized = os.path.normcase(os.path.abspath(value))
    return sha256(normalized.encode("utf-8")).hexdigest()


def _command_arguments_digest(
    arguments: Sequence[str],
) -> tuple[int, str]:
    """Hash the exact ordered argv tokens without persisting their text."""

    if isinstance(arguments, (str, bytes)) or any(
        not isinstance(argument, str) for argument in arguments
    ):
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    values = list(arguments)
    encoded = json.dumps(
        values,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(values), sha256(encoded).hexdigest()


def make_process_identity_record(
    *,
    role: str,
    port: int,
    pid: int,
    start_time: int,
    executable_path: str | os.PathLike[str],
    arguments: Sequence[str],
    marker: str,
    stdout_log: str,
    stderr_log: str,
) -> dict[str, object]:
    """Build a secret-free process identity record from exact launch facts."""

    executable = ntpath.basename(os.fspath(executable_path).replace("/", "\\"))
    argument_count, arguments_sha256 = _command_arguments_digest(arguments)
    record: dict[str, object] = {
        "role": role,
        "port": port,
        "pid": pid,
        "start_time": start_time,
        "executable": executable,
        "executable_path_sha256": _executable_path_digest(executable_path),
        "command_argument_count": argument_count,
        "command_arguments_sha256": arguments_sha256,
        "marker": marker,
        "stdout_log": stdout_log,
        "stderr_log": stderr_log,
    }
    _validate_processes({role if role in {"runtime", "backend", "frontend"} else "coordinator": record})
    return record


def evaluate_process_identity(
    record: Mapping[str, object],
    observation: Mapping[str, object],
    *,
    require_port_owner: bool = True,
) -> tuple[bool, dict[str, object]]:
    """Compare exact identity facts and return only a safe diagnostic."""

    try:
        role = str(record["role"])
        pid = int(record["pid"])
    except (KeyError, TypeError, ValueError):
        role = "unknown"
        pid = 0

    diagnostic: dict[str, object] = {
        "role": role,
        "pid": pid,
        "expected": True,
        "actual": False,
        "failure_field": "record",
    }
    try:
        checks = (
            ("active", observation["active"] is True),
            ("pid", int(observation["pid"]) == pid),
            ("pid_reuse", observation.get("pid_reused") is False),
            (
                "start_time",
                int(observation["start_time"]) == int(record["start_time"]),
            ),
            (
                "executable",
                _executable_path_digest(
                    str(observation["executable_path"])
                )
                == record["executable_path_sha256"],
            ),
            (
                "command_arguments",
                _command_arguments_digest(observation["command_arguments"])
                == (
                    record["command_argument_count"],
                    record["command_arguments_sha256"],
                ),
            ),
            ("command_marker", record["marker"] in observation["command_arguments"]),
        )
        for field, matches in checks:
            if not matches:
                diagnostic["failure_field"] = field
                return False, diagnostic
        if require_port_owner and int(observation["port_owner_pid"]) != pid:
            diagnostic["failure_field"] = "port_owner"
            return False, diagnostic
    except (Gate4Error, KeyError, TypeError, ValueError):
        return False, diagnostic

    diagnostic["actual"] = True
    diagnostic["failure_field"] = None
    return True, diagnostic


def _validate_resources(resources: object) -> None:
    allowed_text = {
        "database",
        "bucket",
        "actor_id",
        "conversation_id",
        "tool_task_id",
        "database_baseline_sha256",
        "bucket_baseline_sha256",
        "volume_baseline_sha256",
    }
    allowed_counts = {
        "database_baseline_count",
        "bucket_baseline_count",
        "volume_baseline_count",
    }
    if not isinstance(resources, dict) or not set(resources).issubset(
        allowed_text | allowed_counts
    ):
        raise Gate4Error(INVALID_STATE)
    for name in allowed_text:
        value = resources.get(name)
        if value is None:
            continue
        if name.endswith("_sha256"):
            if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
                raise Gate4Error(INVALID_STATE)
        elif (
            not isinstance(value, str)
            or _SAFE_RESOURCE_NAME.fullmatch(value) is None
        ):
            raise Gate4Error(INVALID_STATE)
    for name in allowed_counts:
        value = resources.get(name)
        if value is not None and (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise Gate4Error(INVALID_STATE)


def _validate_processes(processes: object) -> None:
    if not isinstance(processes, dict):
        raise Gate4Error(INVALID_STATE)
    allowed_roles = {
        "runtime",
        "backend",
        "frontend",
        "coordinator",
    }
    if not set(processes).issubset(allowed_roles):
        raise Gate4Error(INVALID_STATE)
    expected = {
        "role",
        "port",
        "pid",
        "start_time",
        "executable",
        "executable_path_sha256",
        "command_argument_count",
        "command_arguments_sha256",
        "marker",
        "stdout_log",
        "stderr_log",
    }
    for record in processes.values():
        if not isinstance(record, dict) or set(record) != expected:
            raise Gate4Error(INVALID_STATE)
        if (
            isinstance(record["pid"], bool)
            or not isinstance(record["pid"], int)
            or record["pid"] <= 0
            or isinstance(record["start_time"], bool)
            or not isinstance(record["start_time"], int)
            or record["start_time"] <= 0
            or not _is_safe_identifier(record["role"])
            or isinstance(record["port"], bool)
            or not isinstance(record["port"], int)
            or not 1 <= record["port"] <= 65535
            or not _is_safe_identifier(record["executable"])
            or not isinstance(record["executable_path_sha256"], str)
            or _SHA256.fullmatch(record["executable_path_sha256"]) is None
            or isinstance(record["command_argument_count"], bool)
            or not isinstance(record["command_argument_count"], int)
            or record["command_argument_count"] < 1
            or not isinstance(record["command_arguments_sha256"], str)
            or _SHA256.fullmatch(record["command_arguments_sha256"]) is None
            or not _is_safe_identifier(record["marker"])
            or _PROCESS_ROLE_PORTS.get(str(record["role"]))
            != record["port"]
        ):
            raise Gate4Error(INVALID_STATE)
        for name in ("stdout_log", "stderr_log"):
            value = record[name]
            if (
                not isinstance(value, str)
                or _SAFE_LOG_PATH.fullmatch(value) is None
            ):
                raise Gate4Error(INVALID_STATE)


def validate_safe_state(state: object) -> bool:
    """Validate state by an exact schema, not by a blacklist."""

    expected = {
        "schema_version",
        "run_id",
        "phase",
        "dotenv_sha256",
        "resources",
        "budgets",
        "docker",
        "processes",
        "artifacts",
    }
    if not isinstance(state, dict) or set(state) != expected:
        raise Gate4Error(INVALID_STATE)
    if (
        state["schema_version"] != "1.0"
        or not _is_safe_identifier(state["run_id"])
        or state["phase"] not in _STATE_PHASES
        or not isinstance(state["dotenv_sha256"], str)
        or _SHA256.fullmatch(state["dotenv_sha256"]) is None
    ):
        raise Gate4Error(INVALID_STATE)
    _validate_resources(state["resources"])

    budgets = state["budgets"]
    if not isinstance(budgets, dict) or set(budgets) != {
        "runtime",
        "ddpm",
    }:
        raise Gate4Error(INVALID_STATE)
    limits = {"runtime": 2, "ddpm": 2}
    for name, limit in limits.items():
        value = budgets[name]
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= limit
        ):
            raise Gate4Error(INVALID_STATE)
    docker = state["docker"]
    if not isinstance(docker, dict) or set(docker) != {
        "postgresql",
        "minio",
    }:
        raise Gate4Error(INVALID_STATE)
    for service in docker.values():
        if not isinstance(service, dict) or set(service) != {
            "owned",
            "preexisting",
            "container_id",
        }:
            raise Gate4Error(INVALID_STATE)
        if (
            type(service["owned"]) is not bool
            or type(service["preexisting"]) is not bool
            or (
                service["container_id"] is not None
                and (
                    not isinstance(service["container_id"], str)
                    or re.fullmatch(
                        r"[0-9a-f]{12,64}", service["container_id"]
                    )
                    is None
                )
            )
            or (service["owned"] and service["preexisting"])
        ):
            raise Gate4Error(INVALID_STATE)

    _validate_processes(state["processes"])
    artifacts = state["artifacts"]
    if not isinstance(artifacts, dict) or set(artifacts) != {"audit_json"}:
        raise Gate4Error(INVALID_STATE)
    audit_json = artifacts["audit_json"]
    if audit_json is not None and audit_json != "audit.json":
        raise Gate4Error(INVALID_STATE)
    return True


def transition_safe_state(
    state: Mapping[str, object], next_phase: str
) -> dict[str, object]:
    """Return a validated copy advanced by exactly one approved edge."""

    current = json.loads(json.dumps(state))
    validate_safe_state(current)
    if _STATE_TRANSITIONS.get(current["phase"]) != next_phase:
        raise Gate4Error(INVALID_STATE_TRANSITION)
    current["phase"] = next_phase
    validate_safe_state(current)
    return current


def rollback_phase1_start_resources(
    *,
    state: dict[str, object],
    expected_database: str,
    expected_bucket: str,
    delete_database: Callable[[str], Any],
    delete_bucket: Callable[[str], Any],
) -> bool:
    """Delete only exact Phase 1 resources and invalidate the failed run."""

    validate_safe_state(state)
    if state["phase"] != "PHASE1_STARTING":
        return False
    resources = state["resources"]
    assert isinstance(resources, dict)
    database = resources.get("database")
    bucket = resources.get("bucket")
    if (
        database not in (None, expected_database)
        or bucket not in (None, expected_bucket)
    ):
        return False

    failed = False
    if bucket is not None:
        try:
            delete_bucket(bucket)
        except BaseException:
            failed = True
        else:
            resources["bucket"] = None
    if database is not None:
        try:
            delete_database(database)
        except BaseException:
            failed = True
        else:
            resources["database"] = None
    if failed:
        validate_safe_state(state)
        return False

    state["phase"] = "INVALIDATED"
    state["resources"] = {}
    state["processes"] = {}
    state["docker"] = {
        "postgresql": {
            "owned": False,
            "preexisting": False,
            "container_id": None,
        },
        "minio": {
            "owned": False,
            "preexisting": False,
            "container_id": None,
        },
    }
    validate_safe_state(state)
    return True


def next_fresh_stage_c_run_id(
    *,
    previous_run_id: str,
    factory: Callable[[], str],
) -> str:
    """Return a validated run id that cannot reuse an invalidated run."""

    candidate = factory()
    if (
        not _is_safe_identifier(previous_run_id)
        or not _is_safe_identifier(candidate)
        or candidate == previous_run_id
    ):
        raise Gate4Error(INVALID_STATE)
    return candidate


def serialize_safe_state(
    state: Mapping[str, object],
    *,
    controlled_values: Iterable[str],
) -> str:
    validate_safe_state(state)
    payload = json.dumps(
        state,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    if scan_artifact_text(payload, controlled_values):
        raise Gate4Error(SECRET_LEAK_MARKER)
    return payload


def _validated_owned_file_path(
    path: os.PathLike[str] | str,
    *,
    allowed_parent: os.PathLike[str] | str,
) -> tuple[Path, Path]:
    parent = _lexical_absolute(allowed_parent)
    candidate = _lexical_absolute(path)
    if candidate == parent or not _is_within(candidate, parent):
        raise Gate4Error(INVALID_STATE)
    try:
        parent.mkdir(parents=True, exist_ok=True)
        candidate.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise Gate4Error(INVALID_STATE) from exc
    _assert_path_has_no_reparse(parent, failure_marker=INVALID_STATE)
    _assert_path_has_no_reparse(candidate.parent, failure_marker=INVALID_STATE)
    return candidate, parent


def _atomic_write_bytes(
    path: Path, payload: bytes, *, failure_marker: str
) -> None:
    temporary = _unique_sibling(path.parent, f".{path.name}.tmp-")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0),
            0o600,
        )
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
    except OSError as exc:
        raise Gate4Error(failure_marker) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if os.path.lexists(temporary):
            _safe_remove_tree(temporary)


def write_safe_state(
    path: os.PathLike[str] | str,
    state: Mapping[str, object],
    *,
    controlled_values: Iterable[str],
    allowed_parent: os.PathLike[str] | str,
) -> None:
    candidate, _parent = _validated_owned_file_path(
        path, allowed_parent=allowed_parent
    )
    payload = serialize_safe_state(
        state, controlled_values=controlled_values
    ).encode("utf-8")
    _atomic_write_bytes(candidate, payload, failure_marker=INVALID_STATE)


def read_safe_state(
    path: os.PathLike[str] | str,
    *,
    allowed_parent: os.PathLike[str] | str,
) -> dict[str, object]:
    candidate, _parent = _validated_owned_file_path(
        path, allowed_parent=allowed_parent
    )
    try:
        metadata = os.lstat(candidate)
        if _metadata_is_reparse(metadata) or not stat.S_ISREG(
            metadata.st_mode
        ):
            raise Gate4Error(INVALID_STATE)
        payload = _read_safe_artifact_file(candidate, metadata)
        if len(payload) > 64 * 1024:
            raise Gate4Error(INVALID_STATE)
        value = json.loads(payload.decode("utf-8"))
    except Gate4Error:
        raise
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise Gate4Error(INVALID_STATE) from exc
    validate_safe_state(value)
    return value


def write_control_marker(
    path: os.PathLike[str] | str,
    marker: str,
    *,
    allowed_parent: os.PathLike[str] | str,
) -> None:
    if re.fullmatch(r"P1B2_GATE4_[A-Z0-9_]{1,96}", marker) is None:
        raise Gate4Error("P1B2_GATE4_INVALID_CONTROL_MARKER")
    candidate, _parent = _validated_owned_file_path(
        path, allowed_parent=allowed_parent
    )
    if os.path.lexists(candidate):
        raise Gate4Error("P1B2_GATE4_CONTROL_MARKER_ALREADY_EXISTS")
    _atomic_write_bytes(
        candidate,
        marker.encode("ascii"),
        failure_marker="P1B2_GATE4_CONTROL_WRITE_FAILED",
    )


def read_control_marker(
    path: os.PathLike[str] | str,
    marker: str,
    *,
    allowed_parent: os.PathLike[str] | str,
) -> bool:
    candidate, _parent = _validated_owned_file_path(
        path, allowed_parent=allowed_parent
    )
    try:
        metadata = os.lstat(candidate)
        if _metadata_is_reparse(metadata) or not stat.S_ISREG(
            metadata.st_mode
        ):
            return False
        payload = _read_safe_artifact_file(candidate, metadata)
    except OSError:
        return False
    return payload == marker.encode("ascii")


def coordinator_command_name(phase: str) -> str:
    command = {
        "PHASE2_READY": "phase3",
        "PHASE3_READY": "audit",
        "AUDIT_COMPLETE": "cleanup",
    }.get(phase)
    if command is None:
        raise Gate4Error(INVALID_COORDINATOR_PHASE)
    return command


def scan_artifact_text(text: str, controlled_values: Iterable[str]) -> list[dict[str, object]]:
    """Return secret indices and fixed metadata, never matched values or excerpts."""

    hits: list[dict[str, object]] = []
    for index, value in enumerate(controlled_values):
        if isinstance(value, str) and value and value in text:
            hits.append({"kind": "EXACT_VALUE", "secret_index": index})
    return hits


class OfflineJourneyHarness:
    """Exercise the approved journey with injected, entirely offline adapters."""

    def __init__(
        self,
        *,
        chat_adapter: object,
        runtime_adapter: object,
        explanation_adapter: object,
    ) -> None:
        self.chat_adapter = chat_adapter
        self.runtime_adapter = runtime_adapter
        self.explanation_adapter = explanation_adapter
        self.ledger = BudgetLedger("offline-journey")
        self._provider_directory = tempfile.TemporaryDirectory(
            prefix="p1b2-offline-provider-ledger-"
        )
        provider_path = (
            Path(self._provider_directory.name) / "provider-ledger.json"
        )
        ProviderDelegateLedger.initialize(
            provider_path,
            run_id="offline-journey",
        )
        self.provider_ledger = ProviderDelegateLedger(
            provider_path,
            run_id="offline-journey",
        )
        self._provider_facts = {"CHAT": 0, "EXPLANATION": 0}
        self._task_id = "tool-task-stable"
        self._retried: dict[str, object] | None = None
        self._explanation_attempts: list[dict[str, object]] = []

    def _runtime_delegate(self, source: str) -> object:
        return self.ledger.delegate_runtime(
            lambda: self.ledger.delegate_ddpm(
                lambda: self.runtime_adapter.delegate(source=source)
            )
        )

    def _chat_delegate(self, scenario: str) -> object:
        return self._provider_delegate(
            "CHAT",
            lambda: self.chat_adapter.delegate(scenario=scenario),
        )

    def _provider_delegate(
        self,
        kind: str,
        delegate: Callable[[], Any],
    ) -> Any:
        self._provider_facts[kind] += 1
        return self.provider_ledger.delegate(
            kind,
            delegate,
            fact_reader=lambda: dict(self._provider_facts),
        )

    def runtime_direct(self) -> object:
        return self._runtime_delegate("direct")

    def knowledge(self) -> dict[str, object]:
        outcome = self._chat_delegate("knowledge")
        return {
            "task": {
                "task_id": "knowledge-task",
                "task_type": outcome["task_type"],
                "status": outcome["status"],
            }
        }

    def needs_input(self) -> dict[str, object]:
        outcome = self._chat_delegate("needs-input")
        return {
            "task": {
                "task_id": "needs-input-task",
                "task_type": outcome["task_type"],
                "status": outcome["status"],
                "missing_fields": ["solution_temperature", "aging_temperature"],
            }
        }

    def runtime_unavailable(self) -> dict[str, object]:
        self._chat_delegate("runtime-unavailable")
        return {
            "task_id": self._task_id,
            "task": {"task_id": self._task_id, "status": "FAILED"},
            "tool_run": {
                "tool_run_id": "tool-run-attempt-1",
                "attempt_no": 1,
                "status": "FAILED",
                "safe_error": "RUNTIME_UNAVAILABLE",
            },
            "result": None,
            "asset": None,
        }

    def tool_retry(self, task_id: str) -> dict[str, object]:
        if task_id != self._task_id:
            raise Gate4Error("P1B2_GATE4_UNKNOWN_TASK")
        runtime_outcome = self._runtime_delegate("tool-retry")
        tool_run = {
            "tool_run_id": "tool-run-attempt-2",
            "attempt_no": 2,
            "status": "SUCCEEDED",
        }
        result = runtime_outcome["result"]
        asset = runtime_outcome["asset"]

        try:
            explanation_outcome = self._provider_delegate(
                "EXPLANATION",
                lambda: self.explanation_adapter.delegate(
                    result_id=result["result_id"], automatic=True
                ),
            )
            explanation = {
                "attempt_no": 1,
                "status": explanation_outcome["status"],
                "text": explanation_outcome.get("text"),
            }
        except Exception as exc:
            safe_error = (
                "AUTHENTICATION_FAILED"
                if "AUTHENTICATION_FAILED" in str(exc)
                else "PROVIDER_ERROR"
            )
            explanation = {
                "attempt_no": 1,
                "status": "FAILED",
                "safe_error": safe_error,
            }

        self._explanation_attempts.append(explanation)
        task_status = (
            "SUCCEEDED" if explanation["status"] == "SUCCEEDED" else "PARTIALLY_SUCCEEDED"
        )
        self._retried = {
            "task": {"task_id": self._task_id, "status": task_status},
            "tool_run": tool_run,
            "result": result,
            "asset": asset,
            "selected_tool_run_id": tool_run["tool_run_id"],
            "selected_result_id": result["result_id"],
            "explanation": explanation,
            "explanation_attempts": list(self._explanation_attempts),
        }
        return self._retried

    def explanation_retry(self, result_id: str) -> dict[str, object]:
        if self._retried is None or result_id != self._retried["selected_result_id"]:
            raise Gate4Error("P1B2_GATE4_UNKNOWN_RESULT")
        current_explanation = self._retried.get("explanation")
        if (
            isinstance(current_explanation, Mapping)
            and current_explanation.get("status") == "SUCCEEDED"
        ):
            raise Gate4Error("P1B2_GATE4_EXPLANATION_RETRY_NOT_ALLOWED")
        try:
            outcome = self._provider_delegate(
                "EXPLANATION",
                lambda: self.explanation_adapter.delegate(
                    result_id=result_id, automatic=False
                ),
            )
            explanation = {
                "attempt_no": len(self._explanation_attempts) + 1,
                "status": outcome["status"],
                "text": outcome.get("text"),
            }
        except Gate4Error:
            raise
        except Exception as exc:
            safe_error = (
                "AUTHENTICATION_FAILED"
                if "AUTHENTICATION_FAILED" in str(exc)
                else "PROVIDER_ERROR"
            )
            explanation = {
                "attempt_no": len(self._explanation_attempts) + 1,
                "status": "FAILED",
                "safe_error": safe_error,
            }
        self._explanation_attempts.append(explanation)
        completed = dict(self._retried)
        completed["task"] = {
            "task_id": self._task_id,
            "status": (
                "SUCCEEDED"
                if explanation["status"] == "SUCCEEDED"
                else "PARTIALLY_SUCCEEDED"
            ),
        }
        completed["explanation"] = explanation
        completed["explanation_attempts"] = list(self._explanation_attempts)
        self._retried = completed
        return completed


class ProcessSupervisor:
    """Thin injectable supervisor; launcher owns the actual start primitive."""

    def __init__(self, launcher: object) -> None:
        self.launcher = launcher

    def launch(
        self, role: str, argv: Sequence[str], environment: Mapping[str, str]
    ) -> object:
        return self.launcher.start(role, list(argv), dict(environment))

    def stop(self, handle: object) -> None:
        self.launcher.stop(handle)

    def confirm_port_clear(self, port: int) -> None:
        if self.launcher.port_is_clear(port) is not True:
            raise Gate4Error("P1B2_GATE4_BACKEND_PORT_NOT_CLEAR")


def plan_backend_restart_sequence(
    *,
    supervisor: ProcessSupervisor,
    base_environment: Mapping[str, str],
    controlled: Mapping[str, str],
    database: str,
    bucket: str,
    actor_id: str,
    conversation_id: str,
) -> list[BackendRestartPlan]:
    """Launch real-invalid-real backend plans while retaining stable resources."""

    plans: list[BackendRestartPlan] = []
    for mode, environment_role in (
        ("real", "backend-real"),
        ("invalid", "backend-invalid"),
        ("real", "backend-real"),
    ):
        if plans:
            supervisor.stop(plans[-1].process)
            supervisor.confirm_port_clear(8000)
        environment = build_child_environment(
            environment_role, base_environment, controlled
        )
        environment.update(
            {
                "DATABASE_NAME": database,
                "MINIO_BUCKET": bucket,
                "ACTOR_ID": actor_id,
                "CONVERSATION_ID": conversation_id,
                "ZTA35G_RUNTIME_TIMEOUT_SECONDS": "900",
                "ZTA35G_RUNTIME_URL": "http://127.0.0.1:8100",
            }
        )
        argv = build_child_command(
            role="backend",
            executable=sys.executable,
            arguments=["-m", "materials_agent_backend"],
            secret_values=controlled.values(),
        )
        process = supervisor.launch("backend", argv, environment)
        plans.append(
            BackendRestartPlan(
                mode=mode,
                database=database,
                bucket=bucket,
                actor_id=actor_id,
                conversation_id=conversation_id,
                argv=argv,
                environment=environment,
                process=process,
            )
        )
    return plans


def audit_runtime_adapter(adapter: object) -> bool:
    """Call ready and verify the timeout/retry values actually sent to the pool."""

    pool = getattr(adapter, "_pool", None)
    requests = getattr(pool, "requests", None)
    if not isinstance(requests, list):
        raise Gate4Error("P1B2_GATE4_RUNTIME_ADAPTER_AUDIT_FAILED")
    before = len(requests)
    adapter.ready()
    if len(requests) != before + 1:
        raise Gate4Error("P1B2_GATE4_RUNTIME_ADAPTER_AUDIT_FAILED")
    captured = requests[-1]
    if not isinstance(captured, Mapping):
        raise Gate4Error("P1B2_GATE4_RUNTIME_ADAPTER_AUDIT_FAILED")
    timeout = captured.get("timeout")
    if (
        captured.get("method") != "GET"
        or captured.get("url")
        != "http://127.0.0.1:8100/internal/v1/health/ready"
        or getattr(timeout, "total", None) != 900
        or captured.get("retries") is not False
    ):
        raise Gate4Error("P1B2_GATE4_RUNTIME_ADAPTER_AUDIT_FAILED")
    return True


def wait_for_process(
    process: object,
    *,
    watchdog_seconds: float,
    monotonic_values: Iterable[float] | None = None,
) -> bool:
    """Wait for exit; deterministic injected clocks never sleep."""

    if watchdog_seconds <= 0:
        raise ValueError("watchdog_seconds must be positive")
    injected = monotonic_values is not None
    clock: Iterator[float] | None = iter(monotonic_values) if injected else None
    start = next(clock) if clock is not None else time.monotonic()

    while process.poll() is None:
        now = next(clock) if clock is not None else time.monotonic()
        if now - start >= watchdog_seconds:
            try:
                process.terminate()
            except Exception:
                pass
            try:
                process.wait(timeout=5.0)
            except Exception:
                pass
            if process.poll() is None:
                process.kill()
                try:
                    process.wait(timeout=5.0)
                except Exception:
                    pass
            if process.poll() is None:
                raise Gate4Error("P1B2_GATE4_PROCESS_WATCHDOG_CLEANUP_FAILED")
            return False
        if not injected:
            time.sleep(0.05)
    return True


def explanation_retry_visible(
    *,
    result_exists: bool,
    explanation: Mapping[str, object] | None,
    latest_failure: Mapping[str, object] | None,
) -> bool:
    if not result_exists:
        return False
    if latest_failure is not None and latest_failure.get("status") == "FAILED":
        return True
    return explanation is not None and explanation.get("status") == "FAILED"


class OwnedCleanup:
    """LIFO, run-once cleanup registry."""

    def __init__(self) -> None:
        self._actions: list[tuple[str, Callable[[], Any]]] = []
        self._has_run = False
        self._result: bool | None = None

    def add(self, name: str, action: Callable[[], Any]) -> None:
        if self._has_run:
            raise Gate4Error("P1B2_GATE4_CLEANUP_ALREADY_RAN")
        self._actions.append((name, action))

    def run(self) -> bool:
        if self._has_run:
            assert self._result is not None
            return self._result
        self._has_run = True
        failed = False
        for _name, action in reversed(self._actions):
            try:
                action()
            except Exception:
                failed = True
        self._result = not failed
        return self._result


def build_summary(
    *, passed: bool, failures: Sequence[str], cleanup_complete: bool
) -> dict[str, object]:
    if not cleanup_complete:
        return {
            "status": CLEANUP_INCOMPLETE,
            "failures": list(failures),
            "cleanup_complete": False,
        }
    if not passed or failures:
        return {
            "status": BLOCKED_MARKER,
            "failures": list(failures),
            "cleanup_complete": True,
        }
    return {
        "status": COMPLETE_MARKER,
        "failures": [],
        "cleanup_complete": True,
    }


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _is_reparse_point(
    path: os.PathLike[str] | str,
    *,
    lstat_func: Callable[[os.PathLike[str] | str], Any] = os.lstat,
    platform_name: str = os.name,
) -> bool:
    """Identify symlinks and Windows reparse points without following them."""

    metadata = lstat_func(path)
    return _metadata_is_reparse(metadata, platform_name=platform_name)


def _metadata_is_reparse(metadata: Any, *, platform_name: str = os.name) -> bool:
    if platform_name == "nt":
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(getattr(metadata, "st_file_attributes", 0) & reparse_flag)
    return stat.S_ISLNK(metadata.st_mode)


def _lexical_absolute(path: os.PathLike[str] | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _path_components(path: Path) -> list[Path]:
    components: list[Path] = []
    current = path
    while True:
        components.append(current)
        if current.parent == current:
            break
        current = current.parent
    components.reverse()
    return components


def _assert_path_has_no_reparse(
    path: Path, *, failure_marker: str
) -> None:
    try:
        for component in _path_components(path):
            if os.path.lexists(component) and _is_reparse_point(component):
                raise Gate4Error(failure_marker)
    except Gate4Error:
        raise
    except OSError as exc:
        raise Gate4Error(failure_marker) from exc


def _same_file_identity(first: Any, second: Any) -> bool:
    return (
        first.st_dev,
        first.st_ino,
        stat.S_IFMT(first.st_mode),
    ) == (
        second.st_dev,
        second.st_ino,
        stat.S_IFMT(second.st_mode),
    )


def _scan_safe_directory(
    directory: Path, expected_metadata: Any | None = None
) -> tuple[Any, list[tuple[Path, Any]]]:
    """Bind scandir to a directory whose lstat identity stays unchanged."""

    try:
        before = os.lstat(directory)
        if (
            _metadata_is_reparse(before)
            or not stat.S_ISDIR(before.st_mode)
            or (
                expected_metadata is not None
                and not _same_file_identity(expected_metadata, before)
            )
        ):
            raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED")
        iterator = os.scandir(directory)
        try:
            after_open = os.lstat(directory)
            if (
                _metadata_is_reparse(after_open)
                or not _same_file_identity(before, after_open)
            ):
                raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED")
            entries: list[tuple[Path, Any]] = []
            for entry in iterator:
                directory_metadata = entry.stat(follow_symlinks=False)
                candidate = Path(entry.path)
                metadata = os.lstat(candidate)
                if (
                    _metadata_is_reparse(directory_metadata)
                    or _metadata_is_reparse(metadata)
                    or stat.S_IFMT(directory_metadata.st_mode)
                    != stat.S_IFMT(metadata.st_mode)
                ):
                    raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED")
                entries.append((candidate, metadata))
        finally:
            iterator.close()
        return before, entries
    except Gate4Error:
        raise
    except OSError as exc:
        raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED") from exc


def _collect_safe_artifact_tree(
    run_root: Path,
) -> tuple[list[Path], list[tuple[Path, Any]]]:
    """Validate an entire tree with lstat semantics before any content read."""

    all_entries: list[Path] = []
    files: list[tuple[Path, Any]] = []
    pending: list[tuple[Path, Any | None]] = [(run_root, None)]
    while pending:
        current, expected_metadata = pending.pop()
        _directory_metadata, entries = _scan_safe_directory(
            current, expected_metadata
        )
        for candidate, metadata in entries:
            all_entries.append(candidate)
            if stat.S_ISDIR(metadata.st_mode):
                pending.append((candidate, metadata))
            elif stat.S_ISREG(metadata.st_mode):
                files.append((candidate, metadata))
            else:
                raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED")
    return all_entries, files


def _read_safe_artifact_file(candidate: Path, expected_metadata: Any) -> bytes:
    """Read only after an opened descriptor matches the validated lstat object."""

    descriptor: int | None = None
    try:
        before = os.lstat(candidate)
        if (
            _metadata_is_reparse(before)
            or not stat.S_ISREG(before.st_mode)
            or not _same_file_identity(expected_metadata, before)
        ):
            raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(
            os, "O_NOFOLLOW", 0
        )
        descriptor = os.open(candidate, flags)
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or not _same_file_identity(before, opened)
        ):
            raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except Gate4Error:
        raise
    except OSError as exc:
        raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _safe_remove_tree(path: Path) -> None:
    """Remove an isolated tree, unlinking reparse points without traversing them."""

    try:
        metadata = os.lstat(path)
        if _metadata_is_reparse(metadata):
            if stat.S_ISDIR(metadata.st_mode):
                os.rmdir(path)
            else:
                os.unlink(path)
            return
        if stat.S_ISREG(metadata.st_mode):
            os.unlink(path)
            return
        if not stat.S_ISDIR(metadata.st_mode):
            raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED")
        with os.scandir(path) as iterator:
            children = [Path(entry.path) for entry in iterator]
        for child in children:
            _safe_remove_tree(child)
        current = os.lstat(path)
        if _metadata_is_reparse(current) or not stat.S_ISDIR(current.st_mode):
            raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED")
        os.rmdir(path)
    except Gate4Error:
        raise
    except OSError as exc:
        raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED") from exc


def _write_fixed_file_exclusive(path: Path) -> None:
    descriptor: int | None = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
        )
        descriptor = os.open(path, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED")
        payload = SECRET_LEAK_MARKER.encode("utf-8")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])
    except Gate4Error:
        raise
    except OSError as exc:
        raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _unique_sibling(parent: Path, prefix: str) -> Path:
    for _attempt in range(128):
        candidate = parent / f"{prefix}{secrets.token_hex(16)}"
        if not os.path.lexists(candidate):
            return candidate
    raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED")


def _quarantine_owned_run(
    run_root: Path, marker: Path, allowed_root: Path
) -> None:
    """Atomically isolate the owned name before any destructive traversal."""

    tombstone = _unique_sibling(
        allowed_root, ".p1b2-gate4-quarantine-tombstone-"
    )
    marker_temporary = _unique_sibling(
        marker.parent, ".p1b2-gate4-secret-marker-"
    )
    marker_temporary_created = False
    try:
        _assert_path_has_no_reparse(
            run_root, failure_marker="P1B2_GATE4_ARTIFACT_SCAN_FAILED"
        )
        _assert_path_has_no_reparse(
            marker, failure_marker="P1B2_GATE4_ARTIFACT_SCAN_FAILED"
        )
        _write_fixed_file_exclusive(marker_temporary)
        marker_temporary_created = True
        os.replace(run_root, tombstone)
        _safe_remove_tree(tombstone)
        os.replace(marker_temporary, marker)
        marker_temporary_created = False
    except Gate4Error:
        raise
    except OSError as exc:
        raise Gate4Error("P1B2_GATE4_ARTIFACT_SCAN_FAILED") from exc
    finally:
        if marker_temporary_created and os.path.lexists(marker_temporary):
            _safe_remove_tree(marker_temporary)


def scan_and_quarantine_artifacts(
    run_dir: os.PathLike[str] | str,
    controlled_values: Iterable[str],
    marker_path: os.PathLike[str] | str,
    *,
    allowed_parent: os.PathLike[str] | str,
    expected_run_id: str,
) -> bool:
    """Delete an explicitly scoped run directory on any exact-value leak."""

    run_lexical = _lexical_absolute(run_dir)
    parent_lexical = _lexical_absolute(allowed_parent)
    marker_lexical = _lexical_absolute(marker_path)
    expected_lexical = parent_lexical / f"p1b2-gate4-{expected_run_id}"
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", expected_run_id)
        or run_lexical != expected_lexical
        or run_lexical == parent_lexical
        or not _is_within(marker_lexical, parent_lexical)
        or marker_lexical == parent_lexical
        or _is_within(marker_lexical, run_lexical)
    ):
        raise Gate4Error("P1B2_GATE4_INVALID_QUARANTINE_SCOPE")
    _assert_path_has_no_reparse(
        parent_lexical, failure_marker="P1B2_GATE4_INVALID_QUARANTINE_SCOPE"
    )
    _assert_path_has_no_reparse(
        run_lexical, failure_marker="P1B2_GATE4_INVALID_QUARANTINE_SCOPE"
    )
    _assert_path_has_no_reparse(
        marker_lexical, failure_marker="P1B2_GATE4_INVALID_QUARANTINE_SCOPE"
    )
    try:
        allowed_root = parent_lexical.resolve(strict=True)
        run_root = run_lexical.resolve(strict=True)
        marker = marker_lexical.resolve(strict=False)
    except OSError as exc:
        raise Gate4Error("P1B2_GATE4_INVALID_QUARANTINE_SCOPE") from exc
    expected_run_root = allowed_root / f"p1b2-gate4-{expected_run_id}"
    if (
        not allowed_root.is_dir()
        or not run_root.is_dir()
        or run_root != expected_run_root
        or run_root == allowed_root
        or not _is_within(marker, allowed_root)
        or marker == allowed_root
        or _is_within(marker, run_root)
        or not marker.parent.is_dir()
    ):
        raise Gate4Error("P1B2_GATE4_INVALID_QUARANTINE_SCOPE")

    byte_values = [
        value.encode("utf-8")
        for value in controlled_values
        if isinstance(value, str) and value
    ]
    leaked = False
    all_entries, files = _collect_safe_artifact_tree(run_root)
    for candidate in all_entries:
        relative_name = os.fspath(candidate.relative_to(run_root))
        encoded_name = relative_name.encode("utf-8", errors="surrogatepass")
        if any(value in encoded_name for value in byte_values):
            leaked = True
    for candidate, expected_metadata in files:
        payload = _read_safe_artifact_file(candidate, expected_metadata)
        if any(value in payload for value in byte_values):
            leaked = True

    if not leaked:
        return True

    _collect_safe_artifact_tree(run_root)
    _quarantine_owned_run(run_root, marker, allowed_root)
    return False


def assert_real_authorized(environment: Mapping[str, str]) -> bool:
    if not all(environment.get(name) == "1" for name in _AUTHORIZATION_NAMES):
        raise Gate4Error("P1B2_GATE4_REAL_EXECUTION_NOT_AUTHORIZED")
    return True


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _run_root(repo_root: Path) -> Path:
    return repo_root / "tmp" / "p1b2-gate4"


def _state_path(repo_root: Path) -> Path:
    return _run_root(repo_root) / "state.json"


def load_or_create_stage_c_resume_state(
    *,
    repo_root: Path,
    dotenv_sha256: str,
    controlled_values: Iterable[str],
    run_id_factory: Callable[[], str] | None = None,
) -> dict[str, object]:
    """Create one Stage C run after Stage B cleanup, or resume it verbatim."""

    run_root = _run_root(repo_root)
    state_path = _state_path(repo_root)
    previous_run_id: str | None = None
    if os.path.lexists(run_root):
        state = read_safe_state(state_path, allowed_parent=run_root)
        if state["dotenv_sha256"] != dotenv_sha256:
            raise Gate4Error(INVALID_STATE)
        if state["phase"] != "INVALIDATED":
            return state
        docker = state["docker"]
        if (
            state["resources"]
            or state["processes"]
            or not isinstance(docker, dict)
            or any(
                item.get("owned") is not False
                or item.get("preexisting") is not False
                or item.get("container_id") is not None
                for item in docker.values()
                if isinstance(item, dict)
            )
        ):
            raise Gate4Error(CLEANUP_INCOMPLETE)
        previous_run_id = str(state["run_id"])
        _remove_failed_real_run(repo_root)
    factory = run_id_factory or (lambda: f"g4{secrets.token_hex(12)}")
    run_id = (
        next_fresh_stage_c_run_id(
            previous_run_id=previous_run_id,
            factory=factory,
        )
        if previous_run_id is not None
        else factory()
    )
    state = new_stage_c_resume_state(
        run_id=run_id,
        dotenv_sha256=dotenv_sha256,
    )
    write_safe_state(
        state_path,
        state,
        controlled_values=controlled_values,
        allowed_parent=run_root,
    )
    return state


def _all_controlled_values(
    controlled: Mapping[str, str],
) -> tuple[str, ...]:
    return tuple(
        value
        for value in controlled.values()
        if isinstance(value, str) and value
    )


def _read_text_command(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    controlled_values: Iterable[str],
    failure_marker: str,
    timeout: float = 60.0,
) -> str:
    argv = build_child_command(
        role="log",
        executable=command[0],
        arguments=command[1:],
        secret_values=controlled_values,
    )
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(environment),
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Gate4Error(failure_marker) from exc
    combined = completed.stdout + completed.stderr
    byte_values = [
        value.encode("utf-8")
        for value in controlled_values
        if isinstance(value, str) and value
    ]
    if any(value in combined for value in byte_values):
        raise Gate4Error(SECRET_LEAK_MARKER)
    if completed.returncode != 0:
        raise Gate4Error(failure_marker)
    try:
        return completed.stdout.decode("utf-8", errors="strict")
    except UnicodeError as exc:
        raise Gate4Error(failure_marker) from exc


def _git_output(repo_root: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise Gate4Error(REPOSITORY_GATE_FAILED)
    return _read_text_command(
        [git, *arguments],
        cwd=repo_root,
        environment=_system_child_environment(os.environ),
        controlled_values=(),
        failure_marker=REPOSITORY_GATE_FAILED,
    )


def _validate_repository_gate(repo_root: Path) -> None:
    if _git_output(repo_root, "branch", "--show-current").strip() != EXPECTED_BRANCH:
        raise Gate4Error(REPOSITORY_GATE_FAILED)
    if _git_output(repo_root, "rev-parse", "HEAD").strip() != EXPECTED_HEAD:
        raise Gate4Error(REPOSITORY_GATE_FAILED)
    log_lines = _git_output(
        repo_root, "log", "-1", "--format=%H%n%s%n%P"
    ).splitlines()
    if log_lines != [EXPECTED_HEAD, EXPECTED_SUBJECT, EXPECTED_PARENT]:
        raise Gate4Error(REPOSITORY_GATE_FAILED)

    git = shutil.which("git")
    assert git is not None
    environment = _system_child_environment(os.environ)
    for arguments in (
        ("diff", "--check"),
        ("diff", "--cached", "--check"),
    ):
        _read_text_command(
            [git, *arguments],
            cwd=repo_root,
            environment=environment,
            controlled_values=(),
            failure_marker=REPOSITORY_GATE_FAILED,
        )
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        raise Gate4Error(REPOSITORY_GATE_FAILED)
    _read_text_command(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            os.fspath(repo_root / "scripts" / "dev" / "check-scope.ps1"),
            "-Label",
            "P1B2",
            "-AllowlistFile",
            os.fspath(
                repo_root
                / "scripts"
                / "acceptance"
                / "allowlists"
                / "p1b2.txt"
            ),
        ],
        cwd=repo_root,
        environment=environment,
        controlled_values=(),
        failure_marker=REPOSITORY_GATE_FAILED,
    )
    _read_text_command(
        [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            os.fspath(
                repo_root / "scripts" / "dev" / "check-sem-integrity.ps1"
            ),
        ],
        cwd=repo_root,
        environment=environment,
        controlled_values=(),
        failure_marker=REPOSITORY_GATE_FAILED,
        timeout=300.0,
    )


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _host_available_memory_bytes() -> int:
    if os.name == "nt":
        status = _MemoryStatusEx()
        status.dwLength = ctypes.sizeof(_MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(
            ctypes.byref(status)
        ) == 0:
            raise Gate4Error(RESOURCE_GATE_FAILED)
        return int(status.ullAvailPhys)
    try:
        for line in Path("/proc/meminfo").read_text(
            encoding="ascii"
        ).splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError) as exc:
        raise Gate4Error(RESOURCE_GATE_FAILED) from exc
    raise Gate4Error(RESOURCE_GATE_FAILED)


def _nvidia_smi(
    arguments: Sequence[str], *, timeout: float = 30.0
) -> str:
    executable = shutil.which("nvidia-smi")
    if executable is None:
        raise Gate4Error(RESOURCE_GATE_FAILED)
    return _read_text_command(
        [executable, *arguments],
        cwd=_repo_root(),
        environment=_system_child_environment(os.environ),
        controlled_values=(),
        failure_marker=RESOURCE_GATE_FAILED,
        timeout=timeout,
    )


def _gpu_facts() -> dict[str, object]:
    rows = _nvidia_smi(
        [
            "--query-gpu=name,memory.total,memory.used,memory.free,"
            "compute_cap",
            "--format=csv,noheader,nounits",
        ]
    ).strip().splitlines()
    if len(rows) != 1:
        raise Gate4Error(RESOURCE_GATE_FAILED)
    fields = [field.strip() for field in rows[0].split(",")]
    if len(fields) != 5:
        raise Gate4Error(RESOURCE_GATE_FAILED)
    try:
        total, used, free = (int(fields[index]) for index in (1, 2, 3))
    except ValueError as exc:
        raise Gate4Error(RESOURCE_GATE_FAILED) from exc
    applications = _nvidia_smi(
        [
            "--query-compute-apps=pid",
            "--format=csv,noheader,nounits",
        ]
    )
    pids = [
        int(line.strip())
        for line in applications.splitlines()
        if line.strip() and line.strip().isdigit()
    ]
    return {
        "name": fields[0],
        "total": total,
        "used": used,
        "free": free,
        "compute_capability": fields[4],
        "compute_processes": len(pids),
        "compute_pids": pids,
    }


def _validated_memory_bytes(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise Gate4Error(RESOURCE_GATE_FAILED)
    return value


def evaluate_stage_b_memory_preflight(
    free_bytes: int,
) -> dict[str, object]:
    """Cover Runtime load, resident model, and the first real inference."""

    measured = _validated_memory_bytes(free_bytes)
    if measured < STAGE_B_MINIMUM_HOST_AVAILABLE_BYTES:
        raise Gate4Error(STAGE_B_MEMORY_PREFLIGHT_FAILED)
    return {"free_bytes": measured}


def stage_b_model_root(repo_root: Path) -> Path:
    """Return the confirmed root containing all four bundle files."""

    return repo_root / "SEM" / "ZTA35G_lab"


def start_stage_b_runtime_after_memory_gate(
    *,
    before_runtime_start_free_bytes: int,
    start_runtime: Callable[[], Any],
) -> Any:
    """Recheck the fresh pre-Popen reading before weights can be opened."""

    evaluate_stage_b_memory_preflight(
        before_runtime_start_free_bytes
    )
    return start_runtime()


def measure_stable_free_memory_bytes(
    *,
    host_reader: Callable[[], Mapping[str, object]],
    settle_seconds: float = 2.0,
    sample_interval_seconds: float = 0.25,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Wait for residency to settle, then return three-sample median bytes."""

    if settle_seconds < 0 or sample_interval_seconds < 0:
        raise Gate4Error(INVALID_STATE)
    sleep(settle_seconds)
    samples: list[int] = []
    for index in range(3):
        samples.append(
            _validated_memory_bytes(host_reader()["available_bytes"])
        )
        if index < 2:
            sleep(sample_interval_seconds)
    return sorted(samples)[1]


def stage_b_memory_metrics(
    *,
    before_runtime_start_free_bytes: int,
    after_runtime_ready_free_bytes: int,
    pre_execute_free_bytes: int,
    minimum_during_execute_free_bytes: int,
    post_execute_free_bytes: int,
) -> dict[str, int]:
    before_start = _validated_memory_bytes(
        before_runtime_start_free_bytes
    )
    after_ready = _validated_memory_bytes(
        after_runtime_ready_free_bytes
    )
    pre_execute = _validated_memory_bytes(pre_execute_free_bytes)
    minimum = _validated_memory_bytes(
        minimum_during_execute_free_bytes
    )
    post_execute = _validated_memory_bytes(post_execute_free_bytes)
    return {
        "stage_b_before_runtime_start_free_bytes": before_start,
        "stage_b_after_runtime_ready_free_bytes": after_ready,
        "stage_b_pre_execute_free_bytes": pre_execute,
        "stage_b_minimum_during_execute_free_bytes": minimum,
        "stage_b_post_execute_free_bytes": post_execute,
        "stage_b_model_load_drop_bytes": max(
            0, before_start - after_ready
        ),
        "stage_b_execute_drop_bytes": max(0, pre_execute - minimum),
        "stage_b_total_drop_bytes": max(0, before_start - minimum),
    }


def _stage_b_drop(
    stage_b_memory: Mapping[str, object], field: str
) -> int:
    if field not in stage_b_memory:
        raise Gate4Error(INVALID_STATE)
    return _validated_memory_bytes(stage_b_memory[field])


def required_stage_c_runtime_start_free_bytes(
    stage_b_model_load_drop_bytes: int,
) -> int:
    load_drop = _validated_memory_bytes(stage_b_model_load_drop_bytes)
    return max(
        STAGE_C_RUNTIME_START_BASE_BYTES,
        load_drop + STAGE_C_INCREMENT_RESERVE_BYTES,
    )


def evaluate_stage_c_runtime_start_memory_preflight(
    *,
    stage_b_memory: Mapping[str, object],
    stage_c_before_runtime_start_free_bytes: int,
) -> dict[str, object]:
    """Gate model loading after browser and the integrated stack are live."""

    current = _validated_memory_bytes(
        stage_c_before_runtime_start_free_bytes
    )
    if current < HOST_MEMORY_CRITICAL_LOW_BYTES:
        raise Gate4Error(HOST_MEMORY_CRITICAL_LOW)
    load_drop = _stage_b_drop(
        stage_b_memory, "stage_b_model_load_drop_bytes"
    )
    required = required_stage_c_runtime_start_free_bytes(load_drop)
    if current < required:
        raise Gate4Error(STAGE_C_RUNTIME_START_MEMORY_FAILED)
    return {
        "stage_b_model_load_drop_bytes": load_drop,
        "stage_c_before_runtime_start_free_bytes": current,
        "required_stage_c_runtime_start_free_bytes": required,
    }


def start_stage_c_runtime_after_memory_gate(
    *,
    stage_b_memory: Mapping[str, object],
    stage_c_before_runtime_start_free_bytes: int,
    start_runtime: Callable[[], Any],
) -> Any:
    """Never open weights until the live-stack Runtime-start gate passes."""

    evaluate_stage_c_runtime_start_memory_preflight(
        stage_b_memory=stage_b_memory,
        stage_c_before_runtime_start_free_bytes=(
            stage_c_before_runtime_start_free_bytes
        ),
    )
    return start_runtime()


def required_stage_c_tool_retry_free_bytes(
    stage_b_execute_drop_bytes: int,
) -> int:
    execute_drop = _validated_memory_bytes(stage_b_execute_drop_bytes)
    return max(
        STAGE_C_TOOL_RETRY_BASE_BYTES,
        execute_drop + STAGE_C_INCREMENT_RESERVE_BYTES,
    )


def evaluate_stage_c_tool_retry_memory_preflight(
    *,
    stage_b_memory: Mapping[str, object],
    stage_c_pre_tool_retry_free_bytes: int,
) -> dict[str, object]:
    """Gate one inference; browser and stack use are in the live reading."""

    current = _validated_memory_bytes(stage_c_pre_tool_retry_free_bytes)
    if current < HOST_MEMORY_CRITICAL_LOW_BYTES:
        raise Gate4Error(HOST_MEMORY_CRITICAL_LOW)
    execute_drop = _stage_b_drop(
        stage_b_memory, "stage_b_execute_drop_bytes"
    )
    required = required_stage_c_tool_retry_free_bytes(execute_drop)
    if current < required:
        raise Gate4Error(STAGE_C_TOOL_RETRY_MEMORY_FAILED)
    return {
        "marker": TOOL_RETRY_RESOURCE_READY,
        "stage_b_execute_drop_bytes": execute_drop,
        "stage_c_pre_tool_retry_free_bytes": current,
        "required_stage_c_tool_retry_free_bytes": required,
    }


def write_stage_b_critical_memory_record(
    *,
    run_root: Path,
    run_id: str,
    stage_b_preflight: Mapping[str, object],
    resources: Mapping[str, object],
    budgets: Mapping[str, object],
    controlled_values: Iterable[str],
) -> Path:
    """Persist secret-scanned memory facts before re-raising critical stop."""

    if not _is_safe_identifier(run_id):
        raise Gate4Error(INVALID_STATE)
    if set(stage_b_preflight) != {"free_bytes"}:
        raise Gate4Error(INVALID_STATE)
    required_resource_fields = {
        "minimum_host_available_bytes",
        "host_memory_low_warning",
        "host_memory_low_warning_marker",
        "host_memory_low_warning_first_at_utc",
        "host_memory_low_warning_duration_seconds",
        "host_memory_critical_low",
    }
    if (
        not required_resource_fields.issubset(resources)
        or resources["host_memory_critical_low"] is not True
    ):
        raise Gate4Error(INVALID_STATE)
    stage_fields = {
        "stage_b_before_runtime_start_free_bytes",
        "stage_b_after_runtime_ready_free_bytes",
        "stage_b_pre_execute_free_bytes",
        "stage_b_minimum_during_execute_free_bytes",
        "stage_b_post_execute_free_bytes",
        "stage_b_model_load_drop_bytes",
        "stage_b_execute_drop_bytes",
        "stage_b_total_drop_bytes",
    }
    present_stage_fields = stage_fields.intersection(resources)
    if present_stage_fields and present_stage_fields != stage_fields:
        raise Gate4Error(INVALID_STATE)
    if present_stage_fields:
        expected_metrics = stage_b_memory_metrics(
            before_runtime_start_free_bytes=int(
                resources["stage_b_before_runtime_start_free_bytes"]
            ),
            after_runtime_ready_free_bytes=int(
                resources["stage_b_after_runtime_ready_free_bytes"]
            ),
            pre_execute_free_bytes=int(
                resources["stage_b_pre_execute_free_bytes"]
            ),
            minimum_during_execute_free_bytes=int(
                resources[
                    "stage_b_minimum_during_execute_free_bytes"
                ]
            ),
            post_execute_free_bytes=int(
                resources["stage_b_post_execute_free_bytes"]
            ),
        )
        if any(
            resources[field] != value
            for field, value in expected_metrics.items()
        ):
            raise Gate4Error(INVALID_STATE)

    payload = {
        "marker": HOST_MEMORY_CRITICAL_LOW,
        "run_id": run_id,
        "stage_b_preflight": dict(stage_b_preflight),
        "resources": dict(resources),
        "budgets": dict(budgets),
    }
    try:
        serialized = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise Gate4Error(INVALID_STATE) from exc
    if scan_artifact_text(serialized, controlled_values):
        raise Gate4Error(SECRET_LEAK_MARKER)
    path, _parent = _validated_owned_file_path(
        run_root / "stage-b-memory-blocked.json",
        allowed_parent=run_root,
    )
    _atomic_write_bytes(
        path,
        serialized.encode("utf-8"),
        failure_marker=INVALID_STATE,
    )
    return path


def _assert_resource_gate(
    *, require_free_memory: bool = True
) -> dict[str, object] | None:
    memory = None
    if require_free_memory:
        memory = evaluate_stage_b_memory_preflight(
            _host_available_memory_bytes()
        )
    facts = _gpu_facts()
    if (
        facts["name"] != EXPECTED_GPU_NAME
        or facts["total"] != EXPECTED_GPU_MEMORY_MIB
        or facts["compute_capability"] != EXPECTED_GPU_COMPUTE_CAPABILITY
        or facts["free"] < MINIMUM_GPU_FREE_MIB
        or facts["compute_processes"] != 0
    ):
        raise Gate4Error(RESOURCE_GATE_FAILED)
    return memory


def _port_is_clear(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            return probe.connect_ex(("127.0.0.1", port)) != 0
    except OSError:
        return False


def _assert_ports_clear(*ports: int) -> None:
    if not all(_port_is_clear(port) for port in ports):
        raise Gate4Error(RESOURCE_GATE_FAILED)


def _wait_port(port: int, *, listening: bool, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _port_is_clear(port) is not listening:
            return
        time.sleep(0.2)
    raise Gate4Error(RESOURCE_GATE_FAILED)


def _find_conda_executable() -> Path:
    candidates: list[Path] = [
        Path("D:/ProgramData/Anaconda3/Scripts/conda.exe")
    ]
    discovered = shutil.which("conda")
    if discovered:
        candidates.append(Path(discovered))
    conda_exe = os.environ.get("CONDA_EXE")
    if conda_exe:
        candidates.append(Path(conda_exe))
    user_profile = os.environ.get("USERPROFILE")
    if user_profile:
        base = Path(user_profile)
        candidates.extend(
            (
                base / "Anaconda3" / "Scripts" / "conda.exe",
                base / "Miniconda3" / "Scripts" / "conda.exe",
            )
        )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    raise Gate4Error(RESOURCE_GATE_FAILED)


def _find_conda_python(environment_name: str) -> Path:
    approved_direct = (
        Path("D:/ProgramData/Anaconda3/envs")
        / environment_name
        / ("python.exe" if os.name == "nt" else "bin/python")
    )
    if approved_direct.is_file():
        return approved_direct.resolve()
    conda = _find_conda_executable()
    output = _read_text_command(
        [os.fspath(conda), "env", "list", "--json"],
        cwd=_repo_root(),
        environment=_system_child_environment(os.environ),
        controlled_values=(),
        failure_marker=RESOURCE_GATE_FAILED,
    )
    try:
        payload = json.loads(output)
        environments = payload["envs"]
        matches = [
            Path(value)
            for value in environments
            if Path(value).name.casefold() == environment_name.casefold()
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Gate4Error(RESOURCE_GATE_FAILED) from exc
    if len(matches) != 1:
        raise Gate4Error(RESOURCE_GATE_FAILED)
    python = matches[0] / ("python.exe" if os.name == "nt" else "bin/python")
    if not python.is_file():
        raise Gate4Error(RESOURCE_GATE_FAILED)
    return python.resolve()


def _process_start_time(pid: int) -> int | None:
    if pid <= 0:
        return None
    if os.name == "nt":
        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            creation = ctypes.c_ulonglong()
            exit_time = ctypes.c_ulonglong()
            kernel = ctypes.c_ulonglong()
            user = ctypes.c_ulonglong()
            if ctypes.windll.kernel32.GetProcessTimes(
                process,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            ) == 0:
                return None
            return int(creation.value)
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(
            encoding="ascii"
        ).split()
        return int(fields[21])
    except (OSError, ValueError, IndexError):
        return None


def _process_is_active(
    pid: int, *, kernel32: Any | None = None
) -> bool:
    """Distinguish a running Windows PID from a retained terminated handle."""

    if pid <= 0:
        return False
    if os.name == "nt":
        api = kernel32 or ctypes.windll.kernel32
        process = api.OpenProcess(0x1000, False, pid)
        if not process:
            # OpenProcess documents ERROR_INVALID_PARAMETER for a PID that
            # does not exist.  Access denied and every other query failure
            # remain active/indeterminate so cleanup fails closed.
            return int(api.GetLastError()) != 87
        try:
            exit_code = ctypes.c_ulong()
            if api.GetExitCodeProcess(
                process, ctypes.byref(exit_code)
            ) == 0:
                # Cleanup is fail closed when liveness cannot be proved.
                return True
            return int(exit_code.value) == 259
        finally:
            api.CloseHandle(process)
    return _process_start_time(pid) is not None


def _process_executable_path(pid: int) -> str | None:
    if os.name == "nt":
        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not process:
            return None
        try:
            capacity = ctypes.c_ulong(32768)
            buffer = ctypes.create_unicode_buffer(capacity.value)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(capacity)
            ) == 0:
                return None
            return buffer.value
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:
        return os.readlink(f"/proc/{pid}/exe")
    except OSError:
        return None


def _process_command_line(pid: int) -> str:
    if os.name != "nt":
        try:
            return Path(f"/proc/{pid}/cmdline").read_bytes().replace(
                b"\0", b" "
            ).decode("utf-8", errors="strict")
        except (OSError, UnicodeError) as exc:
            raise Gate4Error(PROCESS_IDENTITY_FAILED) from exc
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    script = (
        "$p=Get-CimInstance Win32_Process -Filter "
        f"'ProcessId = {pid}';"
        "if($null -eq $p){exit 2};"
        "$b=[Text.Encoding]::Unicode.GetBytes([string]$p.CommandLine);"
        "[Console]::Out.Write([Convert]::ToBase64String($b))"
    )
    encoded = _read_text_command(
        [powershell, "-NoProfile", "-Command", script],
        cwd=_repo_root(),
        environment=_system_child_environment(os.environ),
        controlled_values=(),
        failure_marker=PROCESS_IDENTITY_FAILED,
    )
    try:
        return base64.b64decode(
            encoded, validate=True
        ).decode("utf-16-le", errors="strict")
    except (binascii.Error, UnicodeError) as exc:
        raise Gate4Error(PROCESS_IDENTITY_FAILED) from exc


def _windows_command_line_arguments(command_line: str) -> list[str]:
    if os.name != "nt":
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    count = ctypes.c_int()
    command_line_to_argv = ctypes.windll.shell32.CommandLineToArgvW
    command_line_to_argv.argtypes = [
        ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    command_line_to_argv.restype = ctypes.POINTER(ctypes.c_wchar_p)
    pointer = command_line_to_argv(
        command_line, ctypes.byref(count)
    )
    if not pointer:
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    try:
        return [pointer[index] for index in range(count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(pointer)


def _process_command_arguments(pid: int) -> list[str]:
    if os.name == "nt":
        return _windows_command_line_arguments(_process_command_line(pid))[1:]
    try:
        parts = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return [
            part.decode("utf-8", errors="strict")
            for part in parts[1:]
            if part
        ]
    except (OSError, UnicodeError) as exc:
        raise Gate4Error(PROCESS_IDENTITY_FAILED) from exc


def _port_owner_pid(port: int) -> int | None:
    if not 1 <= port <= 65535:
        return None
    if os.name == "nt":
        try:
            output = subprocess.run(
                ["netstat.exe", "-ano", "-p", "tcp"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=False,
                check=True,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.decode("ascii", errors="ignore")
        except (OSError, UnicodeError, subprocess.SubprocessError) as exc:
            raise Gate4Error(PROCESS_IDENTITY_FAILED) from exc
        owners: set[int] = set()
        for line in output.splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[0].upper() != "TCP":
                continue
            local, state, pid_text = fields[1], fields[3], fields[4]
            if state.upper() != "LISTENING":
                continue
            if local.rsplit(":", 1) != ["127.0.0.1", str(port)]:
                continue
            try:
                owners.add(int(pid_text))
            except ValueError as exc:
                raise Gate4Error(PROCESS_IDENTITY_FAILED) from exc
        if len(owners) > 1:
            raise Gate4Error(PROCESS_IDENTITY_FAILED)
        return next(iter(owners), None)
    try:
        output = subprocess.run(
            ["lsof", "-nP", f"-iTCP:127.0.0.1:{port}", "-sTCP:LISTEN", "-t"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            check=False,
            timeout=15,
        ).stdout.split()
        owners = {int(value) for value in output}
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as exc:
        raise Gate4Error(PROCESS_IDENTITY_FAILED) from exc
    if len(owners) > 1:
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    return next(iter(owners), None)


def _observe_process_identity(
    record: Mapping[str, object],
    *,
    require_port_owner: bool,
) -> dict[str, object]:
    pid = int(record["pid"])
    start_time = _process_start_time(pid)
    return {
        "active": _process_is_active(pid),
        "retained_handle": start_time is not None,
        "pid": pid,
        "pid_reused": (
            start_time is not None
            and start_time != int(record["start_time"])
        ),
        "start_time": start_time,
        "executable_path": _process_executable_path(pid),
        "command_arguments": _process_command_arguments(pid),
        "port_owner_pid": (
            _port_owner_pid(int(record["port"]))
            if require_port_owner
            else pid
        ),
    }


def _capture_process_record(
    process: subprocess.Popen[bytes],
    *,
    role: str,
    port: int,
    executable: str,
    arguments: Sequence[str],
    marker: str,
    stdout_log: str,
    stderr_log: str,
) -> dict[str, object]:
    start_time = _process_start_time(process.pid)
    executable_path = _process_executable_path(process.pid)
    if start_time is None or executable_path is None:
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    record = make_process_identity_record(
        role=role,
        port=port,
        pid=process.pid,
        start_time=start_time,
        executable_path=executable,
        arguments=arguments,
        marker=marker,
        stdout_log=stdout_log,
        stderr_log=stderr_log,
    )
    observation = _observe_process_identity(
        record, require_port_owner=False
    )
    matches, _diagnostic = evaluate_process_identity(
        record, observation, require_port_owner=False
    )
    if (
        not matches
        or _executable_path_digest(executable_path)
        != record["executable_path_sha256"]
    ):
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    return record


def _start_owned_process(
    *,
    role: str,
    port: int,
    command: Sequence[str],
    environment: Mapping[str, str],
    cwd: Path,
    run_root: Path,
    marker: str,
    stdout_name: str,
    stderr_name: str,
    controlled_values: Iterable[str],
) -> tuple[subprocess.Popen[bytes], dict[str, object]]:
    log_root = run_root / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    _assert_path_has_no_reparse(
        log_root, failure_marker=PROCESS_IDENTITY_FAILED
    )
    stdout_relative = f"logs/{stdout_name}"
    stderr_relative = f"logs/{stderr_name}"
    stdout_path = run_root / stdout_relative
    stderr_path = run_root / stderr_relative
    argv = build_child_command(
        role=role,
        executable=command[0],
        arguments=command[1:],
        secret_values=controlled_values,
    )
    stdout_stream = stdout_path.open("xb")
    stderr_stream = stderr_path.open("xb")
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
    try:
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.DEVNULL,
            stdout=stdout_stream,
            stderr=stderr_stream,
            close_fds=True,
            creationflags=creationflags,
        )
    except OSError as exc:
        raise Gate4Error(PROCESS_IDENTITY_FAILED) from exc
    finally:
        stdout_stream.close()
        stderr_stream.close()
    try:
        record = _capture_process_record(
            process,
            role=role,
            port=port,
            executable=command[0],
            arguments=argv[1:],
            marker=marker,
            stdout_log=stdout_relative,
            stderr_log=stderr_relative,
        )
    except Exception as identity_error:
        cleanup_error: BaseException | None = None
        try:
            if os.name == "nt":
                completed = subprocess.run(
                    [
                        "taskkill.exe",
                        "/PID",
                        str(process.pid),
                        "/T",
                        "/F",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
                if completed.returncode != 0 and process.poll() is None:
                    raise Gate4Error(CLEANUP_INCOMPLETE)
            else:
                process.kill()
            process.wait(timeout=30)
        except BaseException as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            raise Gate4Error(CLEANUP_INCOMPLETE) from identity_error
        raise
    return process, record


def _owned_process_matches(record: Mapping[str, object]) -> bool:
    try:
        observation = _observe_process_identity(
            record, require_port_owner=False
        )
        matches, _diagnostic = evaluate_process_identity(
            record, observation, require_port_owner=False
        )
        return matches
    except (Gate4Error, KeyError, TypeError, ValueError):
        return False


def verify_owned_process_port(record: Mapping[str, object]) -> bool:
    try:
        observation = _observe_process_identity(
            record, require_port_owner=True
        )
        matches, _diagnostic = evaluate_process_identity(
            record, observation, require_port_owner=True
        )
    except (Gate4Error, KeyError, TypeError, ValueError):
        matches = False
    if not matches:
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    return True


def _capture_owned_tree_pids(root_pid: int) -> list[int]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    output = _read_text_command(
        [
            powershell,
            "-NoProfile",
            "-Command",
            (
                "Get-CimInstance Win32_Process | "
                "Select-Object ProcessId,ParentProcessId | "
                "ConvertTo-Json -Compress"
            ),
        ],
        cwd=_repo_root(),
        environment=_system_child_environment(os.environ),
        controlled_values=(),
        failure_marker=PROCESS_IDENTITY_FAILED,
    )
    try:
        decoded = json.loads(output)
        rows = decoded if isinstance(decoded, list) else [decoded]
        children: dict[int, list[int]] = {}
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid process record")
            pid = int(row["ProcessId"])
            parent = int(row["ParentProcessId"])
            children.setdefault(parent, []).append(pid)
        found: list[int] = []
        pending = [root_pid]
        while pending:
            pid = pending.pop()
            if pid in found:
                continue
            found.append(pid)
            pending.extend(children.get(pid, ()))
        return sorted(found)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise Gate4Error(PROCESS_IDENTITY_FAILED) from exc


class _WindowsOwnedTreeRunner:
    def verify_identity(self, record: Mapping[str, object]) -> bool:
        return _owned_process_matches(record)

    def run(self, argv: Sequence[str]) -> int:
        try:
            completed = subprocess.run(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise Gate4Error(CLEANUP_INCOMPLETE) from exc
        return completed.returncode

    def owned_tree_gone(self, record: Mapping[str, object]) -> bool:
        try:
            pids = [int(value) for value in record["owned_tree_pids"]]
        except (KeyError, TypeError, ValueError) as exc:
            raise Gate4Error(CLEANUP_INCOMPLETE) from exc
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if all(not _process_is_active(pid) for pid in pids):
                return True
            time.sleep(0.1)
        return False


def stop_owned_process_tree(
    record: Mapping[str, object], *, runner: Any | None = None
) -> bool:
    """Verify ownership, kill the exact Windows tree, then prove it is gone."""

    command_runner = runner or _WindowsOwnedTreeRunner()
    if not command_runner.verify_identity(record):
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    effective_record = dict(record)
    known_pids = {
        int(value)
        for value in effective_record.get("owned_tree_pids", ())
    }
    known_pids.add(int(effective_record["pid"]))
    if runner is None:
        try:
            known_pids.update(
                _capture_owned_tree_pids(int(effective_record["pid"]))
            )
        except Gate4Error:
            # The Popen PID and identity were already verified.  taskkill /T
            # remains fail-closed; port/GPU gates verify residual resources.
            pass
    effective_record["owned_tree_pids"] = sorted(known_pids)
    command = [
        "taskkill.exe",
        "/PID",
        str(int(effective_record["pid"])),
        "/T",
        "/F",
    ]
    if command_runner.run(command) != 0:
        raise Gate4Error(CLEANUP_INCOMPLETE)
    if not command_runner.owned_tree_gone(effective_record):
        raise Gate4Error(CLEANUP_INCOMPLETE)
    return True


def finalize_stage_b(
    *,
    stop_owned: Callable[[], object],
    check_port_gpu: Callable[[], object],
    verify_dotenv: Callable[[], object],
    scan_artifacts: Callable[[], object],
    ledger: "BudgetLedger",
    body_error: BaseException | None = None,
) -> bool:
    """Run every finalizer step; any cleanup defect wins over the body result."""

    del ledger  # The finalizer must never reset or mutate consumed budgets.
    failed = False
    leaked = (
        isinstance(body_error, Gate4Error)
        and SECRET_LEAK_MARKER in str(body_error)
    )
    for action in (
        stop_owned,
        check_port_gpu,
        verify_dotenv,
        scan_artifacts,
    ):
        try:
            if action() is False:
                failed = True
        except BaseException as exc:
            if isinstance(exc, Gate4Error) and SECRET_LEAK_MARKER in str(exc):
                leaked = True
            else:
                failed = True
    if leaked:
        raise Gate4Error(SECRET_LEAK_MARKER)
    if failed:
        raise Gate4Error(CLEANUP_INCOMPLETE)
    return True


class ResourceSampler:
    """Sample host/GPU extrema while rejecting foreign compute processes."""

    def __init__(
        self,
        *,
        host_reader: Callable[[], Mapping[str, object]],
        gpu_reader: Callable[[], Mapping[str, object]],
        owned_compute_pids: set[int],
        before_runtime_start_free_bytes: int | None = None,
        interval_seconds: float = 1.0,
    ) -> None:
        self._host_reader = host_reader
        self._gpu_reader = gpu_reader
        self._owned_compute_pids = set(owned_compute_pids)
        self._interval_seconds = interval_seconds
        self._minimum_host_bytes: int | None = None
        self._maximum_gpu: int | None = None
        self._minimum_gpu_free: int | None = None
        self._warning_first_at_utc: str | None = None
        self._warning_started_monotonic: float | None = None
        self._warning_duration_seconds = 0.0
        self._critical_host_memory = False
        self._before_runtime_start_bytes = (
            None
            if before_runtime_start_free_bytes is None
            else _validated_memory_bytes(
                before_runtime_start_free_bytes
            )
        )
        self._after_runtime_ready_bytes: int | None = None
        self._execute_active = False
        self._execute_pre_bytes: int | None = None
        self._execute_minimum_bytes: int | None = None
        self._execute_post_bytes: int | None = None
        self._failure: BaseException | None = None
        self._sample_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def _sample_locked(self) -> int:
        host = self._host_reader()
        gpu = self._gpu_reader()
        available = _validated_memory_bytes(host["available_bytes"])
        used = int(gpu["used_mib"])
        free = int(gpu["free_mib"])
        compute_pids = {int(value) for value in gpu["compute_pids"]}
        if compute_pids - self._owned_compute_pids:
            raise Gate4Error("P1B2_GATE4_UNEXPECTED_GPU_PROCESS")
        if free < MINIMUM_ACTIVE_GPU_FREE_MIB:
            raise Gate4Error(RESOURCE_GATE_FAILED)

        self._minimum_host_bytes = (
            available
            if self._minimum_host_bytes is None
            else min(self._minimum_host_bytes, available)
        )
        if available < HOST_MEMORY_LOW_WARNING_BYTES:
            if self._warning_first_at_utc is None:
                self._warning_first_at_utc = datetime.now(
                    timezone.utc
                ).isoformat()
            if self._warning_started_monotonic is None:
                self._warning_started_monotonic = time.monotonic()
        elif self._warning_started_monotonic is not None:
            self._warning_duration_seconds += (
                time.monotonic() - self._warning_started_monotonic
            )
            self._warning_started_monotonic = None
        if available < HOST_MEMORY_CRITICAL_LOW_BYTES:
            self._critical_host_memory = True
        if self._execute_active:
            self._execute_minimum_bytes = (
                available
                if self._execute_minimum_bytes is None
                else min(self._execute_minimum_bytes, available)
            )
        self._maximum_gpu = (
            used
            if self._maximum_gpu is None
            else max(self._maximum_gpu, used)
        )
        self._minimum_gpu_free = (
            free
            if self._minimum_gpu_free is None
            else min(self._minimum_gpu_free, free)
        )
        return available

    def _sample(self) -> int:
        with self._sample_lock:
            return self._sample_locked()

    def _monitor(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                self._sample()
            except BaseException as exc:
                self._failure = exc
                self._stop_event.set()

    def raise_if_failed(self) -> None:
        if self._failure is not None:
            raise self._failure

    def raise_if_critical(self) -> None:
        if self._critical_host_memory:
            raise Gate4Error(HOST_MEMORY_CRITICAL_LOW)

    def _activate_execute_locked(self, available: int) -> int:
        if self._thread is None or self._execute_active:
            raise Gate4Error(INVALID_STATE)
        if (
            self._critical_host_memory
            or available < HOST_MEMORY_CRITICAL_LOW_BYTES
        ):
            raise Gate4Error(HOST_MEMORY_CRITICAL_LOW)
        self._execute_pre_bytes = available
        self._execute_minimum_bytes = available
        self._execute_post_bytes = None
        self._execute_active = True
        return available

    def _begin_execute_locked(self) -> int:
        return self._activate_execute_locked(self._sample_locked())

    def begin_execute(self) -> int:
        with self._sample_lock:
            return self._begin_execute_locked()

    def capture_after_runtime_ready(
        self,
        *,
        settle_seconds: float = 2.0,
        sample_interval_seconds: float = 0.25,
    ) -> int:
        """Measure resident model load separately before any execute."""

        if self._thread is None or self._execute_active:
            raise Gate4Error(INVALID_STATE)
        if settle_seconds < 0 or sample_interval_seconds < 0:
            raise Gate4Error(INVALID_STATE)
        time.sleep(settle_seconds)
        with self._sample_lock:
            samples: list[int] = []
            for index in range(3):
                samples.append(self._sample_locked())
                if index < 2:
                    time.sleep(sample_interval_seconds)
            measured = sorted(samples)[1]
            self._after_runtime_ready_bytes = measured
            return measured

    def start_execute_thread(
        self,
        delegate_thread: object,
        *,
        sample_interval_seconds: float = 0.25,
    ) -> int:
        start = getattr(delegate_thread, "start", None)
        if not callable(start) or sample_interval_seconds < 0:
            raise Gate4Error(INVALID_STATE)
        with self._sample_lock:
            if self._thread is None or self._execute_active:
                raise Gate4Error(INVALID_STATE)
            samples: list[int] = []
            for index in range(3):
                samples.append(self._sample_locked())
                if index < 2:
                    time.sleep(sample_interval_seconds)
            available = sorted(samples)[1]
            self._activate_execute_locked(available)
            try:
                start()
            except BaseException:
                self._execute_active = False
                self._execute_pre_bytes = None
                self._execute_minimum_bytes = None
                self._execute_post_bytes = None
                raise
            return available

    def finish_execute(self) -> dict[str, int]:
        with self._sample_lock:
            if not self._execute_active:
                raise Gate4Error(INVALID_STATE)
            available = self._sample_locked()
            self._execute_post_bytes = available
            self._execute_active = False
            assert self._execute_pre_bytes is not None
            assert self._execute_minimum_bytes is not None
            result = {
                "stage_b_pre_execute_free_bytes": self._execute_pre_bytes,
                "stage_b_minimum_during_execute_free_bytes": (
                    self._execute_minimum_bytes
                ),
                "stage_b_post_execute_free_bytes": self._execute_post_bytes,
            }
            if (
                self._before_runtime_start_bytes is not None
                and self._after_runtime_ready_bytes is not None
            ):
                result.update(
                    stage_b_memory_metrics(
                        before_runtime_start_free_bytes=(
                            self._before_runtime_start_bytes
                        ),
                        after_runtime_ready_free_bytes=(
                            self._after_runtime_ready_bytes
                        ),
                        pre_execute_free_bytes=self._execute_pre_bytes,
                        minimum_during_execute_free_bytes=(
                            self._execute_minimum_bytes
                        ),
                        post_execute_free_bytes=self._execute_post_bytes,
                    )
                )
            return result

    def start(self) -> None:
        if self._thread is not None:
            raise Gate4Error(INVALID_STATE)
        self._sample()
        self._thread = threading.Thread(
            target=self._monitor,
            name="p1b2-gate4-resource-sampler",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> dict[str, object]:
        if self._thread is None:
            raise Gate4Error(INVALID_STATE)
        self._stop_event.set()
        self._thread.join(timeout=max(5.0, self._interval_seconds * 2))
        try:
            with self._sample_lock:
                available = self._sample_locked()
                if self._execute_active:
                    self._execute_post_bytes = available
                    self._execute_active = False
        except BaseException as exc:
            if self._failure is None:
                self._failure = exc
        if self._thread.is_alive():
            raise Gate4Error(CLEANUP_INCOMPLETE)
        if self._failure is not None:
            raise self._failure
        assert self._minimum_host_bytes is not None
        assert self._maximum_gpu is not None
        assert self._minimum_gpu_free is not None
        warning_duration = self._warning_duration_seconds
        if self._warning_started_monotonic is not None:
            warning_duration += (
                time.monotonic() - self._warning_started_monotonic
            )
        report: dict[str, object] = {
            "minimum_host_available_bytes": self._minimum_host_bytes,
            "max_gpu_used_mib": self._maximum_gpu,
            "min_gpu_free_mib": self._minimum_gpu_free,
            "host_memory_low_warning": (
                self._warning_first_at_utc is not None
            ),
            "host_memory_low_warning_marker": (
                HOST_MEMORY_LOW_WARNING
                if self._warning_first_at_utc is not None
                else None
            ),
            "host_memory_low_warning_first_at_utc": (
                self._warning_first_at_utc
            ),
            "host_memory_low_warning_duration_seconds": max(
                0.0, warning_duration
            ),
            "host_memory_critical_low": self._critical_host_memory,
        }
        if (
            self._execute_pre_bytes is not None
            and self._execute_minimum_bytes is not None
            and self._execute_post_bytes is not None
        ):
            report.update(
                {
                    "stage_b_pre_execute_free_bytes": (
                        self._execute_pre_bytes
                    ),
                    "stage_b_minimum_during_execute_free_bytes": (
                        self._execute_minimum_bytes
                    ),
                    "stage_b_post_execute_free_bytes": (
                        self._execute_post_bytes
                    ),
                }
            )
        if (
            self._before_runtime_start_bytes is not None
            and self._after_runtime_ready_bytes is not None
            and self._execute_pre_bytes is not None
            and self._execute_minimum_bytes is not None
            and self._execute_post_bytes is not None
        ):
            report.update(
                stage_b_memory_metrics(
                    before_runtime_start_free_bytes=(
                        self._before_runtime_start_bytes
                    ),
                    after_runtime_ready_free_bytes=(
                        self._after_runtime_ready_bytes
                    ),
                    pre_execute_free_bytes=self._execute_pre_bytes,
                    minimum_during_execute_free_bytes=(
                        self._execute_minimum_bytes
                    ),
                    post_execute_free_bytes=self._execute_post_bytes,
                )
            )
        return report


def _http_json(
    method: str,
    url: str,
    *,
    token: str | None = None,
    payload: Mapping[str, object] | None = None,
    raw_body: bytes | None = None,
    content_type: str | None = None,
    controlled_values: Iterable[str] = (),
    timeout: float = 10.0,
) -> tuple[int, dict[str, object], float]:
    headers: dict[str, str] = {"Accept": "application/json"}
    body: bytes | None = None
    if token is not None:
        headers["X-ZTA35G-Runtime-Token"] = token
    if payload is not None and raw_body is not None:
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    if payload is not None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif raw_body is not None:
        body = raw_body
        headers["Content-Type"] = content_type or "application/json"
    protected = [
        value.encode("utf-8")
        for value in controlled_values
        if isinstance(value, str) and value
    ]
    if body is not None and any(value in body for value in protected):
        raise Gate4Error(SECRET_LEAK_MARKER)
    request = Request(url, data=body, headers=headers, method=method)
    started = time.monotonic()
    status: int
    response_body: bytes
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(response.status)
            response_body = response.read(5 * 1024 * 1024 + 1)
    except HTTPError as error:
        status = int(error.code)
        response_body = error.read(5 * 1024 * 1024 + 1)
    except (OSError, URLError, TimeoutError) as exc:
        raise Gate4Error(HTTP_CONTRACT_FAILED) from exc
    elapsed = time.monotonic() - started
    if any(value in response_body for value in protected):
        raise Gate4Error(SECRET_LEAK_MARKER)
    if len(response_body) > 5 * 1024 * 1024:
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    try:
        decoded = json.loads(response_body.decode("utf-8"))
    except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
        raise Gate4Error(HTTP_CONTRACT_FAILED) from exc
    if not isinstance(decoded, dict):
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    return status, decoded, elapsed


def _wait_runtime_ready(
    process: subprocess.Popen[bytes],
    token: str,
    *,
    timeout: float,
    controlled_values: Iterable[str] = (),
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise Gate4Error(HTTP_CONTRACT_FAILED)
        try:
            status, payload, _elapsed = _http_json(
                "GET",
                f"{RUNTIME_URL}/internal/v1/health/ready",
                token=token,
                controlled_values=controlled_values,
                timeout=2.0,
            )
            if (
                status == 200
                and payload.get("status") == "READY"
                and payload.get("model_loaded") is True
            ):
                return payload
        except Gate4Error as exc:
            if SECRET_LEAK_MARKER in str(exc):
                raise
        time.sleep(0.5)
    raise Gate4Error(HTTP_CONTRACT_FAILED)


def _runtime_execute_payload(run_id: str, suffix: str) -> dict[str, object]:
    return {
        "runtime_contract_version": "1.0",
        "request_id": f"req-{run_id}-{suffix}",
        "task_id": f"task-{run_id}-{suffix}",
        "tool_run_id": f"toolrun-{run_id}-{suffix}",
        "tool_id": "zta35g_sem_virtual_lab",
        "tool_version": "0.1.0",
        "schema_version": "1.0",
        "process_parameters": {
            "solution_temperature": 1000,
            "solution_time": 3.0,
            "aging_temperature": 730,
            "aging_time": 3.0,
        },
        "requested_outputs": ["sem_image", "mechanical_properties"],
        "runtime_parameters": {
            "seed": 20260730,
            "num_samples": 1,
            "guide_scale": 2.0,
            "timesteps": 1000,
        },
    }


def _validate_runtime_result(
    payload: Mapping[str, object],
    *,
    expected_request: Mapping[str, object] | None = None,
) -> dict[str, float]:
    expected_top_level = {
        "runtime_contract_version",
        "request_id",
        "task_id",
        "tool_run_id",
        "tool_id",
        "tool_version",
        "schema_version",
        "status",
        "requested_outputs",
        "completed_outputs",
        "failed_outputs",
        "data",
        "images",
        "warnings",
        "diagnostics",
        "actual_runtime_parameters",
        "model_bundle_id",
        "error",
    }
    if set(payload) != expected_top_level:
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    if expected_request is not None:
        for name in (
            "runtime_contract_version",
            "request_id",
            "task_id",
            "tool_run_id",
            "tool_id",
            "tool_version",
            "schema_version",
        ):
            if payload.get(name) != expected_request.get(name):
                raise Gate4Error(HTTP_CONTRACT_FAILED)
        if (
            payload.get("requested_outputs")
            != expected_request.get("requested_outputs")
            or payload.get("actual_runtime_parameters")
            != expected_request.get("runtime_parameters")
        ):
            raise Gate4Error(HTTP_CONTRACT_FAILED)
    if (
        payload.get("status") != "SUCCEEDED"
        or payload.get("model_bundle_id") != MODEL_BUNDLE_ID
        or payload.get("completed_outputs")
        != ["sem_image", "mechanical_properties"]
        or payload.get("failed_outputs") != []
    ):
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    images = payload.get("images")
    data = payload.get("data")
    diagnostics = payload.get("diagnostics")
    if (
        not isinstance(images, list)
        or len(images) != 1
        or not isinstance(images[0], dict)
        or not isinstance(data, dict)
        or payload.get("warnings") != []
        or not isinstance(diagnostics, list)
        or len(diagnostics) < 2
        or payload.get("error") is not None
    ):
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    diagnostic_steps: set[str] = set()
    for diagnostic in diagnostics:
        if not isinstance(diagnostic, dict):
            raise Gate4Error(HTTP_CONTRACT_FAILED)
        duration = diagnostic.get("duration_ms")
        step = diagnostic.get("step")
        if (
            set(diagnostic)
            != {
                "step",
                "status",
                "started_at",
                "completed_at",
                "duration_ms",
                "error_code",
                "safe_error_message",
            }
            or
            step not in {
                "sem_generation",
                "mechanical_property_prediction",
            }
            or step in diagnostic_steps
            or diagnostic.get("status") != "SUCCEEDED"
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or float(duration) < 0
            or diagnostic.get("error_code") is not None
            or diagnostic.get("safe_error_message") is not None
        ):
            raise Gate4Error(HTTP_CONTRACT_FAILED)
        diagnostic_steps.add(step)
    if diagnostic_steps != {
        "sem_generation",
        "mechanical_property_prediction",
    }:
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    image = images[0]
    encoded = image.get("data_base64")
    digest = image.get("sha256")
    if (
        set(image)
        != {
            "image_role",
            "requested_output",
            "dtype",
            "numpy_dtype",
            "shape",
            "channel_layout",
            "value_range",
            "encoding",
            "byte_order",
            "array_order",
            "sha256",
            "data_base64",
        }
        or
        image.get("encoding") != "base64+npy"
        or image.get("image_role") != "generated_sem"
        or image.get("requested_output") is not True
        or image.get("dtype") != "float32"
        or image.get("shape") != [512, 512]
        or image.get("numpy_dtype") != "<f4"
        or image.get("channel_layout") != "GRAYSCALE_2D"
        or image.get("value_range") != [-1.0, 1.0]
        or image.get("byte_order") != "little"
        or image.get("array_order") != "C"
        or not isinstance(encoded, str)
        or not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
    ):
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise Gate4Error(HTTP_CONTRACT_FAILED) from exc
    if (
        len(raw) > 2 * 1024 * 1024
        or sha256(raw).hexdigest() != digest
    ):
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    import numpy as np

    try:
        array = np.load(BytesIO(raw), allow_pickle=False)
    except Exception as exc:
        raise Gate4Error(HTTP_CONTRACT_FAILED) from exc
    if (
        not isinstance(array, np.ndarray)
        or array.shape != (512, 512)
        or array.dtype.str != "<f4"
        or not array.flags.c_contiguous
        or not np.isfinite(array).all()
        or float(array.min()) < -1.0
        or float(array.max()) > 1.0
    ):
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    expected_units = {"yield_strength": "MPa", "elongation": "%"}
    metrics: dict[str, float] = {}
    for name, unit in expected_units.items():
        item = data.get(name)
        value = item.get("value") if isinstance(item, dict) else None
        if (
            not isinstance(item, dict)
            or set(item) != {"value", "unit"}
            or
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or float(value) <= 0
            or item.get("unit") != unit
        ):
            raise Gate4Error(HTTP_CONTRACT_FAILED)
        metrics[name] = float(value)
    if set(data) != set(expected_units):
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    return metrics


def _read_log_event_count(run_root: Path, relative: str, event: str) -> int:
    candidate = run_root / relative
    try:
        payload = candidate.read_bytes()
    except OSError as exc:
        raise Gate4Error(HTTP_CONTRACT_FAILED) from exc
    return payload.count(f'"event":"{event}"'.encode("ascii"))


def _scan_run_artifacts(
    run_root: Path, controlled_values: Iterable[str]
) -> bool:
    byte_values = [
        value.encode("utf-8")
        for value in controlled_values
        if isinstance(value, str) and value
    ]
    all_entries, files = _collect_safe_artifact_tree(run_root)
    for candidate in all_entries:
        relative = os.fspath(candidate.relative_to(run_root)).encode(
            "utf-8", errors="surrogatepass"
        )
        if any(value in relative for value in byte_values):
            return False
    for candidate, metadata in files:
        payload = _read_safe_artifact_file(candidate, metadata)
        if any(value in payload for value in byte_values):
            return False
    return True


def _quarantine_real_run(repo_root: Path) -> None:
    run_root = _run_root(repo_root)
    temporary_parent = repo_root / "tmp"
    marker = temporary_parent / "p1b2-gate4-secret-leak.marker"
    expected = temporary_parent / "p1b2-gate4"
    if (
        _lexical_absolute(run_root) != _lexical_absolute(expected)
        or not run_root.is_dir()
    ):
        raise Gate4Error("P1B2_GATE4_INVALID_QUARANTINE_SCOPE")
    _assert_path_has_no_reparse(
        temporary_parent,
        failure_marker="P1B2_GATE4_INVALID_QUARANTINE_SCOPE",
    )
    _assert_path_has_no_reparse(
        run_root,
        failure_marker="P1B2_GATE4_INVALID_QUARANTINE_SCOPE",
    )
    if marker.exists():
        _safe_remove_tree(marker)
    tombstone = _unique_sibling(
        temporary_parent, ".p1b2-gate4-quarantine-"
    )
    os.replace(run_root, tombstone)
    _safe_remove_tree(tombstone)
    _write_fixed_file_exclusive(marker)


def _remove_failed_real_run(repo_root: Path) -> None:
    """Remove only the verified runner-owned fixed run directory."""

    run_root = _run_root(repo_root)
    temporary_parent = repo_root / "tmp"
    expected = temporary_parent / "p1b2-gate4"
    if (
        _lexical_absolute(run_root) != _lexical_absolute(expected)
        or not run_root.is_dir()
        or not temporary_parent.is_dir()
    ):
        raise Gate4Error("P1B2_GATE4_INVALID_QUARANTINE_SCOPE")
    _assert_path_has_no_reparse(
        temporary_parent,
        failure_marker="P1B2_GATE4_INVALID_QUARANTINE_SCOPE",
    )
    _assert_path_has_no_reparse(
        run_root,
        failure_marker="P1B2_GATE4_INVALID_QUARANTINE_SCOPE",
    )
    tombstone = _unique_sibling(
        temporary_parent, ".p1b2-gate4-failed-"
    )
    os.replace(run_root, tombstone)
    _safe_remove_tree(tombstone)


def _exercise_stage_b_runtime(
    *,
    process: subprocess.Popen[bytes],
    record: Mapping[str, object],
    token: str,
    run_id: str,
    run_root: Path,
    ledger: BudgetLedger,
    on_budget_reserved: Callable[[BudgetLedger], None],
    resource_sampler: ResourceSampler,
    controlled_values: Iterable[str],
) -> dict[str, object]:
    stdout_log = str(record["stdout_log"])
    stderr_log = str(record["stderr_log"])

    def event_count(event: str) -> int:
        return _read_log_event_count(
            run_root, stderr_log, event
        ) + _read_log_event_count(run_root, stdout_log, event)

    def assert_safe_error(
        status: int,
        payload: Mapping[str, object],
        expected_status: int,
        expected_code: str,
    ) -> None:
        error = payload.get("error")
        if (
            status != expected_status
            or not isinstance(error, dict)
            or error.get("code") != expected_code
            or not isinstance(error.get("safe_message"), str)
        ):
            raise Gate4Error(HTTP_CONTRACT_FAILED)

    legal_payload = _runtime_execute_payload(run_id, "direct")
    negative_cases = (
        ("GET", "/internal/v1/health/live", None),
        ("GET", "/internal/v1/health/ready", None),
        ("POST", "/internal/v1/execute", legal_payload),
    )
    for method, path, payload in negative_cases:
        for negative_token in (
            None,
            "p1b2-deliberately-invalid-runtime-token",
        ):
            before = event_count("execution_received")
            status, response, _elapsed = _http_json(
                method,
                f"{RUNTIME_URL}{path}",
                token=negative_token,
                payload=payload,
                controlled_values=controlled_values,
            )
            assert_safe_error(
                status,
                response,
                401,
                "INVALID_RUNTIME_REQUEST",
            )
            if event_count("execution_received") != before:
                raise Gate4Error(HTTP_CONTRACT_FAILED)

    ready = _wait_runtime_ready(
        process,
        token,
        timeout=300.0,
        controlled_values=controlled_values,
    )
    verify_owned_process_port(record)
    supported = ready.get("supported_tool")
    device = ready.get("device")
    if (
        ready.get("model_bundle_id") != MODEL_BUNDLE_ID
        or not isinstance(device, dict)
        or device.get("kind") != "cuda"
        or not isinstance(supported, dict)
        or supported.get("tool_id") != "zta35g_sem_virtual_lab"
    ):
        raise Gate4Error(HTTP_CONTRACT_FAILED)

    resource_sampler.capture_after_runtime_ready()

    live_status, live_payload, _elapsed = _http_json(
        "GET",
        f"{RUNTIME_URL}/internal/v1/health/live",
        token=token,
        controlled_values=controlled_values,
    )
    if live_status != 200 or live_payload.get("status") != "LIVE":
        raise Gate4Error(HTTP_CONTRACT_FAILED)

    invalid_payloads: list[
        tuple[dict[str, object], int, str]
    ] = [({}, 422, "INVALID_RUNTIME_REQUEST")]
    for name, value, status_code, code in (
        (
            "runtime_contract_version",
            "2.0",
            409,
            "SCHEMA_VERSION_MISMATCH",
        ),
        ("tool_id", "unsupported_tool", 422, "UNSUPPORTED_TOOL"),
        ("tool_version", "9.9.9", 409, "TOOL_VERSION_MISMATCH"),
        ("schema_version", "2.0", 409, "SCHEMA_VERSION_MISMATCH"),
    ):
        candidate = dict(legal_payload)
        candidate[name] = value
        invalid_payloads.append((candidate, status_code, code))
    for field, value in (
        ("num_samples", 2),
        ("guide_scale", 2.1),
        ("timesteps", 999),
    ):
        candidate = json.loads(json.dumps(legal_payload))
        candidate["runtime_parameters"][field] = value
        invalid_payloads.append(
            (candidate, 422, "INVALID_RUNTIME_REQUEST")
        )
    invalid_process = json.loads(json.dumps(legal_payload))
    invalid_process["process_parameters"]["solution_temperature"] = 899
    invalid_payloads.append(
        (invalid_process, 422, "INVALID_RUNTIME_REQUEST")
    )
    invalid_before = event_count("execution_received")
    for payload, expected_status, expected_code in invalid_payloads:
        status, response, _elapsed = _http_json(
            "POST",
            f"{RUNTIME_URL}/internal/v1/execute",
            token=token,
            payload=payload,
            controlled_values=controlled_values,
        )
        assert_safe_error(
            status, response, expected_status, expected_code
        )
    raw_invalid_cases = (
        (b"{}", "text/plain", 415),
        (b"{", "application/json", 400),
        (b"x" * ((64 * 1024) + 1), "application/json", 413),
    )
    for raw_body, content_type, expected_status in raw_invalid_cases:
        status, response, _elapsed = _http_json(
            "POST",
            f"{RUNTIME_URL}/internal/v1/execute",
            token=token,
            raw_body=raw_body,
            content_type=content_type,
            controlled_values=controlled_values,
        )
        assert_safe_error(
            status,
            response,
            expected_status,
            "INVALID_RUNTIME_REQUEST",
        )
    if event_count("execution_received") != invalid_before:
        raise Gate4Error(HTTP_CONTRACT_FAILED)

    outcomes: queue.Queue[tuple[str, object]] = queue.Queue()

    def legal_execute() -> None:
        try:
            def delegate() -> tuple[int, dict[str, object], float]:
                on_budget_reserved(ledger)
                return _http_json(
                    "POST",
                    f"{RUNTIME_URL}/internal/v1/execute",
                    token=token,
                    payload=legal_payload,
                    controlled_values=controlled_values,
                    timeout=1190.0,
                )

            result = ledger.delegate_runtime(
                lambda: ledger.delegate_ddpm(delegate)
            )
            outcomes.put(("ok", result))
        except BaseException:
            outcomes.put(("error", HTTP_CONTRACT_FAILED))

    thread = threading.Thread(
        target=legal_execute,
        name="p1b2-gate4-stageb-execute",
        daemon=True,
    )
    started = time.monotonic()
    resource_sampler.start_execute_thread(thread)
    busy_seen = False
    while time.monotonic() - started < 120.0:
        resource_sampler.raise_if_failed()
        status, snapshot, _elapsed = _http_json(
            "GET",
            f"{RUNTIME_URL}/internal/v1/health/ready",
            token=token,
            controlled_values=controlled_values,
            timeout=3.0,
        )
        if status == 200 and snapshot.get("busy") is True:
            busy_seen = True
            break
        if not thread.is_alive():
            break
        time.sleep(0.25)
    if not busy_seen:
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    busy_status, busy_payload, busy_elapsed = _http_json(
        "POST",
        f"{RUNTIME_URL}/internal/v1/execute",
        token=token,
        payload=_runtime_execute_payload(run_id, "busy"),
        controlled_values=controlled_values,
        timeout=10.0,
    )
    busy_error = busy_payload.get("error")
    if (
        busy_status != 503
        or busy_elapsed >= 5.0
        or not isinstance(busy_error, dict)
        or busy_error.get("code") != "RUNTIME_BUSY"
        or busy_error.get("retryable") is not True
    ):
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    deadline = started + 1200.0
    while thread.is_alive() and time.monotonic() < deadline:
        resource_sampler.raise_if_failed()
        thread.join(0.25)
    if thread.is_alive():
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    resource_sampler.raise_if_failed()
    memory_metrics = resource_sampler.finish_execute()
    stage_b_memory_metrics(
        before_runtime_start_free_bytes=int(
            memory_metrics[
                "stage_b_before_runtime_start_free_bytes"
            ]
        ),
        after_runtime_ready_free_bytes=int(
            memory_metrics["stage_b_after_runtime_ready_free_bytes"]
        ),
        pre_execute_free_bytes=int(
            memory_metrics["stage_b_pre_execute_free_bytes"]
        ),
        minimum_during_execute_free_bytes=int(
            memory_metrics[
                "stage_b_minimum_during_execute_free_bytes"
            ]
        ),
        post_execute_free_bytes=int(
            memory_metrics["stage_b_post_execute_free_bytes"]
        ),
    )
    resource_sampler.raise_if_critical()
    kind, outcome = outcomes.get_nowait()
    if kind != "ok" or not isinstance(outcome, tuple):
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    status, payload, execute_elapsed = outcome
    if status != 200:
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    metrics = _validate_runtime_result(
        payload, expected_request=legal_payload
    )
    if (
        ledger.runtime_delegates != 1
        or ledger.ddpm_delegates != 1
    ):
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    expected_log_deltas = {
        "execution_received": 2,
        "runtime_busy": 1,
        "execution_completed": 1,
        "sem_generation": 1,
        "mechanical_property_prediction": 1,
    }
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        observed = {
            event: event_count(event)
            for event in expected_log_deltas
        }
        if (
            observed["execution_received"] - invalid_before == 2
            and observed["runtime_busy"] == 1
            and observed["execution_completed"] == 1
            and observed["sem_generation"] == 1
            and observed["mechanical_property_prediction"] == 1
        ):
            break
        time.sleep(0.1)
    else:
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    return {
        "ready_status": ready.get("status"),
        "device": "cuda",
        "bundle": MODEL_BUNDLE_ID,
        "execute_elapsed_seconds": round(float(execute_elapsed), 3),
        "busy_elapsed_seconds": round(float(busy_elapsed), 3),
        "image_shape": [512, 512],
        "metrics": metrics,
        "log_deltas": expected_log_deltas,
    }


def _run_stage_b_inner(repo_root: Path, dotenv: DotenvRecord) -> None:
    run_root = _run_root(repo_root)
    if os.path.lexists(run_root):
        raise Gate4Error(INVALID_STATE)
    _validate_repository_gate(repo_root)
    _assert_ports_clear(8100)
    stage_b_preflight = _assert_resource_gate()
    assert stage_b_preflight is not None
    loaded = load_gate4_secrets(dotenv.path)
    if loaded.real_provider_key != dotenv.key:
        raise _fixed_key_failure()
    run_id = f"g4{secrets.token_hex(12)}"
    controlled = loaded.controlled_mapping()
    values = _all_controlled_values(controlled)
    ledger = BudgetLedger(run_id)
    state = new_safe_state(
        run_id=run_id, dotenv_sha256=dotenv.start_sha256
    )
    process: subprocess.Popen[bytes] | None = None
    record: dict[str, object] | None = None
    owned_tree_pids: list[int] = []
    body_error: BaseException | None = None
    summary: dict[str, object] | None = None
    resources: dict[str, object] | None = None
    before_runtime_start_free_bytes: int | None = None

    def persist_budget(reserved: BudgetLedger) -> None:
        state["budgets"] = {
            "runtime": reserved.runtime_delegates,
            "ddpm": reserved.ddpm_delegates,
        }
        write_safe_state(
            _state_path(repo_root),
            state,
            controlled_values=values,
            allowed_parent=run_root,
        )

    def wait_port_gpu_clean() -> bool:
        _wait_port(8100, listening=False, timeout=30.0)
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            facts = _gpu_facts()
            if (
                facts["compute_processes"] == 0
                and facts["free"] >= MINIMUM_GPU_FREE_MIB
            ):
                return True
            time.sleep(1.0)
        return False

    def scan_artifacts() -> bool:
        if not run_root.is_dir():
            return True
        if _scan_run_artifacts(run_root, values):
            return True
        _quarantine_real_run(repo_root)
        raise Gate4Error(SECRET_LEAK_MARKER)

    def stop_runtime_tree() -> bool:
        if record is None:
            return True
        alive = [
            pid for pid in owned_tree_pids if _process_is_active(pid)
        ]
        if not alive:
            return True
        if _process_start_time(int(record["pid"])) is None:
            raise Gate4Error(CLEANUP_INCOMPLETE)
        stop_record = dict(record)
        stop_record["owned_tree_pids"] = list(owned_tree_pids)
        return stop_owned_process_tree(stop_record)

    try:
        write_safe_state(
            _state_path(repo_root),
            state,
            controlled_values=values,
            allowed_parent=run_root,
        )
        runtime_python = _find_conda_python("materialsagent-zta35g")
        runtime_source = repo_root / "zta35g-runtime" / "src"
        runtime_environment = build_real_role_environment(
            "runtime",
            os.environ,
            controlled,
            safe_values={
                "model_root": os.fspath(stage_b_model_root(repo_root)),
                "runtime_pythonpath": os.fspath(runtime_source),
            },
        )
        runtime_prefix = runtime_python.parent
        runtime_path_entries = [
            runtime_prefix,
            runtime_prefix / "Library" / "mingw-w64" / "bin",
            runtime_prefix / "Library" / "usr" / "bin",
            runtime_prefix / "Library" / "bin",
            runtime_prefix / "Scripts",
            runtime_prefix / "bin",
        ]
        existing_path = runtime_environment.get("PATH", "")
        runtime_environment["PATH"] = os.pathsep.join(
            [
                *(os.fspath(path) for path in runtime_path_entries),
                existing_path,
            ]
        )
        runtime_environment["CONDA_PREFIX"] = os.fspath(runtime_prefix)
        runtime_environment["CONDA_DEFAULT_ENV"] = "materialsagent-zta35g"
        runtime_environment["PYTHONNOUSERSITE"] = "1"
        runtime_environment["PYTHONUTF8"] = "1"
        _read_text_command(
            [
                os.fspath(runtime_python),
                "-c",
                (
                    "import sys,importlib.util;"
                    "assert sys.version_info[:2]==(3,8);"
                    "assert importlib.util.find_spec("
                    "'materialsagent_zta35g_runtime') is not None"
                ),
            ],
            cwd=repo_root / "zta35g-runtime",
            environment=runtime_environment,
            controlled_values=values,
            failure_marker=RESOURCE_GATE_FAILED,
        )
        marker = f"p1b2-{run_id}-runtime-stageb"
        before_runtime_start_free_bytes = (
            _host_available_memory_bytes()
        )
        process, record = start_stage_b_runtime_after_memory_gate(
            before_runtime_start_free_bytes=(
                before_runtime_start_free_bytes
            ),
            start_runtime=lambda: _start_owned_process(
                role="runtime",
                port=8100,
                command=[
                    os.fspath(runtime_python),
                    "-c",
                    (
                        "from materialsagent_zta35g_runtime.main "
                        "import run;run()"
                    ),
                    marker,
                ],
                environment=runtime_environment,
                cwd=repo_root / "zta35g-runtime",
                run_root=run_root,
                marker=marker,
                stdout_name="runtime-stageb.stdout.log",
                stderr_name="runtime-stageb.stderr.log",
                controlled_values=values,
            ),
        )
        owned_tree_pids = [process.pid]
        owned_tree_pids = sorted(
            set(owned_tree_pids + _capture_owned_tree_pids(process.pid))
        )
        state["processes"] = {"runtime": record}
        write_safe_state(
            _state_path(repo_root),
            state,
            controlled_values=values,
            allowed_parent=run_root,
        )
        def read_gpu_resources() -> dict[str, object]:
            facts = _gpu_facts()
            return {
                "used_mib": facts["used"],
                "free_mib": facts["free"],
                "compute_pids": facts["compute_pids"],
            }

        sampler = ResourceSampler(
            host_reader=lambda: {
                "available_bytes": _host_available_memory_bytes()
            },
            gpu_reader=read_gpu_resources,
            owned_compute_pids=set(owned_tree_pids),
            before_runtime_start_free_bytes=(
                before_runtime_start_free_bytes
            ),
        )
        sampler.start()
        try:
            summary = _exercise_stage_b_runtime(
                process=process,
                record=record,
                token=controlled["runtime_token"],
                run_id=run_id,
                run_root=run_root,
                ledger=ledger,
                on_budget_reserved=persist_budget,
                resource_sampler=sampler,
                controlled_values=values,
            )
        finally:
            resources = sampler.stop()
            if resources.get("host_memory_critical_low") is True:
                raise Gate4Error(HOST_MEMORY_CRITICAL_LOW)
    except BaseException as exc:
        body_error = exc
    try:
        finalize_stage_b(
            stop_owned=stop_runtime_tree,
            check_port_gpu=wait_port_gpu_clean,
            verify_dotenv=lambda: verify_dotenv_unchanged(dotenv),
            scan_artifacts=scan_artifacts,
            ledger=ledger,
            body_error=body_error,
        )
    except BaseException as cleanup_error:
        if (
            isinstance(body_error, Gate4Error)
            and SECRET_LEAK_MARKER in str(body_error)
        ):
            if run_root.is_dir():
                try:
                    _quarantine_real_run(repo_root)
                except BaseException:
                    pass
            raise Gate4Error(SECRET_LEAK_MARKER) from cleanup_error
        raise cleanup_error from body_error

    if body_error is not None:
        if (
            isinstance(body_error, Gate4Error)
            and SECRET_LEAK_MARKER in str(body_error)
            and run_root.is_dir()
        ):
            _quarantine_real_run(repo_root)
            raise Gate4Error(SECRET_LEAK_MARKER) from body_error
        state["processes"] = {}
        persist_budget(ledger)
        if (
            isinstance(body_error, Gate4Error)
            and str(body_error) == HOST_MEMORY_CRITICAL_LOW
            and resources is not None
        ):
            try:
                write_stage_b_critical_memory_record(
                    run_root=run_root,
                    run_id=run_id,
                    stage_b_preflight=stage_b_preflight,
                    resources=resources,
                    budgets=ledger.safe_snapshot(),
                    controlled_values=values,
                )
                if not scan_artifacts():
                    raise Gate4Error(SECRET_LEAK_MARKER)
            except Gate4Error as evidence_error:
                if str(evidence_error) == SECRET_LEAK_MARKER:
                    raise
                raise Gate4Error(CLEANUP_INCOMPLETE) from evidence_error
        raise body_error
    if summary is None or resources is None:
        raise Gate4Error(HTTP_CONTRACT_FAILED)
    state["processes"] = {}
    persist_budget(ledger)
    safe_summary = {
        "marker": STAGE_B_COMPLETE_MARKER,
        "run_id": run_id,
        "budgets": ledger.safe_snapshot(),
        "stage_b_preflight": stage_b_preflight,
        "resources": resources,
        "runtime": summary,
        "dotenv": {
            "exists": True,
            "start_sha256": dotenv.start_sha256,
            "end_sha256": dotenv.start_sha256,
            "modified": False,
            "entered_artifact": False,
        },
    }
    summary_payload = json.dumps(
        safe_summary,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    if scan_artifact_text(summary_payload, values):
        _quarantine_real_run(repo_root)
        raise Gate4Error(SECRET_LEAK_MARKER)
    _atomic_write_bytes(
        run_root / "stage-b-summary.json",
        summary_payload.encode("utf-8"),
        failure_marker=INVALID_STATE,
    )
    if not scan_artifacts():
        raise Gate4Error(SECRET_LEAK_MARKER)
    state = transition_safe_state(state, "STAGEB_COMPLETE")
    write_safe_state(
        _state_path(repo_root),
        state,
        controlled_values=values,
        allowed_parent=run_root,
    )
    output_lines = [
        STAGE_B_COMPLETE_MARKER,
        "Runtime=1/2 DDPM=1/2 Provider=NOT_APPLICABLE_STAGE_B_ONLY",
        f"run_id={run_id} device=cuda bundle={MODEL_BUNDLE_ID} "
        "image=512x512 metrics=positive",
    ]
    if scan_artifact_text("\n".join(output_lines), values):
        _quarantine_real_run(repo_root)
        raise Gate4Error(SECRET_LEAK_MARKER)
    for line in output_lines:
        print(line)


def _run_stage_b(repo_root: Path, dotenv: DotenvRecord) -> None:
    """Total Stage B controller, including preflight and safe epilogue."""

    body_error: BaseException | None = None
    try:
        _run_stage_b_inner(repo_root, dotenv)
    except BaseException as exc:
        body_error = exc
    try:
        verify_dotenv_unchanged(dotenv)
    except BaseException as dotenv_error:
        raise dotenv_error from body_error
    if body_error is not None:
        raise body_error


def _inventory_fingerprint(values: Iterable[str]) -> tuple[int, str]:
    normalized = sorted(set(values))
    return len(normalized), sha256(
        "\n".join(normalized).encode("utf-8")
    ).hexdigest()


def build_frontend_command(
    *,
    node: Path,
    vite: Path,
    marker: str,
) -> list[str]:
    """Start Vite directly so Windows paths with spaces remain one argv item."""

    if not _is_safe_identifier(marker):
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    node_text = os.fspath(node)
    vite_text = os.fspath(vite)
    if not node_text or not vite_text:
        raise Gate4Error(PROCESS_IDENTITY_FAILED)
    return [
        node_text,
        f"--title={marker}",
        vite_text,
        "--host",
        "127.0.0.1",
        "--port",
        "3000",
        "--strictPort",
    ]


def cleanup_phase1_transient_artifacts(run_root: Path) -> None:
    """Remove only failed Phase 1 process logs while preserving safe state."""

    root = _lexical_absolute(run_root)
    logs = root / "logs"
    if not os.path.lexists(logs):
        return
    if logs.parent != root:
        raise Gate4Error(CLEANUP_INCOMPLETE)
    _assert_path_has_no_reparse(root, failure_marker=CLEANUP_INCOMPLETE)
    _assert_path_has_no_reparse(logs, failure_marker=CLEANUP_INCOMPLETE)
    if not logs.is_dir():
        raise Gate4Error(CLEANUP_INCOMPLETE)
    _safe_remove_tree(logs)


def stop_phase1_process_records(
    *,
    started_records: Mapping[str, Mapping[str, object]],
    state: dict[str, object],
    process_is_active: Callable[[int], bool] = _process_is_active,
    stop_process: Callable[[Mapping[str, object]], Any] = (
        stop_owned_process_tree
    ),
    persist: Callable[[], Any],
) -> bool:
    """Stop only active owned Phase 1 processes and persist removals."""

    failed = False
    processes = state.get("processes")
    if not isinstance(processes, dict):
        return False
    for role in ("frontend", "backend"):
        record = started_records.get(role)
        if record is None:
            continue
        try:
            if process_is_active(int(record["pid"])):
                stop_process(record)
        except BaseException:
            failed = True
        else:
            processes.pop(role, None)
    try:
        persist()
    except BaseException:
        failed = True
    return not failed


def _docker_text(
    repo_root: Path,
    environment: Mapping[str, str],
    controlled_values: Iterable[str],
    *arguments: str,
    timeout: float = 120.0,
) -> str:
    docker = shutil.which("docker")
    if docker is None:
        raise Gate4Error(DOCKER_GATE_FAILED)
    return _read_text_command(
        [docker, *arguments],
        cwd=repo_root,
        environment=environment,
        controlled_values=controlled_values,
        failure_marker=DOCKER_GATE_FAILED,
        timeout=timeout,
    )


def _wait_backend_ready(
    process: subprocess.Popen[bytes],
    *,
    timeout: float,
    controlled_values: Iterable[str] = (),
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise Gate4Error(PHASE1_FAILED)
        try:
            status, payload, _elapsed = _http_json(
                "GET",
                f"{BACKEND_URL}/api/v1/health/ready",
                controlled_values=controlled_values,
                timeout=3.0,
            )
            components = payload.get("components")
            if (
                status == 200
                and payload.get("status") == "READY"
                and isinstance(components, list)
                and {
                    (item.get("name"), item.get("status"))
                    for item in components
                    if isinstance(item, dict)
                }
                == {
                    ("postgresql", "AVAILABLE"),
                    ("object_storage", "AVAILABLE"),
                }
            ):
                return payload
        except Gate4Error as exc:
            if SECRET_LEAK_MARKER in str(exc):
                raise
        time.sleep(0.25)
    raise Gate4Error(PHASE1_FAILED)


def _wait_frontend_ready(
    process: subprocess.Popen[bytes], *, timeout: float
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise Gate4Error(PHASE1_FAILED)
        try:
            request = Request(
                f"{FRONTEND_URL}/",
                method="GET",
                headers={"Accept": "text/html"},
            )
            with urlopen(request, timeout=3.0) as response:
                if response.status == 200:
                    return
        except (HTTPError, URLError, TimeoutError, OSError):
            pass
        time.sleep(0.25)
    raise Gate4Error(PHASE1_FAILED)


def _audit_real_backend_runtime_adapter(
    repo_root: Path, runtime_token: str
) -> bool:
    backend_source = repo_root / "backend" / "src"
    source_text = os.fspath(backend_source)
    inserted = False
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
        inserted = True
    try:
        import urllib3
        from materialsagent.infrastructure.tool_clients.local_zta35g import (
            LocalZTA35GToolClientAdapter,
        )

        class Response:
            status = 200
            data = b"{}"

            def release_conn(self) -> None:
                return None

        class Pool:
            def __init__(self) -> None:
                self.calls: list[dict[str, object]] = []

            def request(self, method: str, url: str, **kwargs: object) -> Response:
                self.calls.append(
                    {"method": method, "url": url, **kwargs}
                )
                return Response()

        pool = Pool()
        adapter = LocalZTA35GToolClientAdapter(
            base_url=RUNTIME_URL,
            token=runtime_token,
            timeout_seconds=900,
            pool=pool,
        )
        adapter._request_json("GET", "/internal/v1/health/ready", None)
        if (
            not isinstance(adapter._timeout, urllib3.Timeout)
            or adapter._timeout.total != 900
            or len(pool.calls) != 1
            or pool.calls[0].get("timeout") is not adapter._timeout
            or pool.calls[0].get("retries") is not False
        ):
            raise Gate4Error(
                "P1B2_GATE4_BACKEND_ADAPTER_CANARY_FAILED"
            )
        return True
    except Gate4Error:
        raise
    except Exception as exc:
        raise Gate4Error(
            "P1B2_GATE4_BACKEND_ADAPTER_CANARY_FAILED"
        ) from exc
    finally:
        if inserted:
            sys.path.remove(source_text)


def _run_phase_1(
    repo_root: Path,
    dotenv: DotenvRecord,
    *,
    fresh_stage_c_authorization: str,
) -> None:
    if fresh_stage_c_authorization != "PROJECT_OWNER_AUTHORIZED":
        raise Gate4Error(NEW_STAGE_C_NOT_AUTHORIZED)
    _validate_repository_gate(repo_root)
    _assert_ports_clear(3000, 8000, 8100)
    verify_dotenv_unchanged(dotenv)
    loaded = load_gate4_secrets(dotenv.path)
    if loaded.real_provider_key != dotenv.key:
        raise _fixed_key_failure()
    controlled = loaded.controlled_mapping()
    values = _all_controlled_values(controlled)
    run_root = _run_root(repo_root)
    state = load_or_create_stage_c_resume_state(
        repo_root=repo_root,
        dotenv_sha256=dotenv.start_sha256,
        controlled_values=values,
    )
    if (
        state["phase"] != "STAGEB_COMPLETE"
        or state["dotenv_sha256"] != dotenv.start_sha256
        or state["budgets"]
        != {
            "runtime": 1,
            "ddpm": 1,
        }
        or state["processes"]
    ):
        raise Gate4Error(INVALID_STATE)
    if not _scan_run_artifacts(run_root, values):
        raise Gate4Error(SECRET_LEAK_MARKER)
    cleanup_phase1_transient_artifacts(run_root)
    run_id = str(state["run_id"])
    provider_ledger_path = run_root / "provider-ledger.json"
    if os.path.lexists(provider_ledger_path):
        raise Gate4Error(PROVIDER_CALL_COUNT_UNKNOWN)
    database = f"p1b2_gate4_{run_id.lower()}"
    bucket = f"p1b2-gate4-{run_id.lower()}"
    actor_id = f"actor-{run_id.lower()}"
    if len(database) > 63 or len(bucket) > 63:
        raise Gate4Error(PHASE1_FAILED)
    backend_python = _find_conda_python("materialsagent-backend")
    backend_source = repo_root / "backend" / "src"
    compose_environment = build_real_role_environment(
        "compose",
        os.environ,
        controlled,
        safe_values={
            "admin_database": loaded.postgres_db,
            "database_user": loaded.postgres_user,
            "minio_access_key": loaded.minio_access_key,
        },
    )
    started_records: dict[str, dict[str, object]] = {}
    owned_services: list[str] = []

    def persist() -> None:
        write_safe_state(
            _state_path(repo_root),
            state,
            controlled_values=values,
            allowed_parent=run_root,
        )

    def stop_started_processes() -> bool:
        return stop_phase1_process_records(
            started_records=started_records,
            state=state,
            persist=persist,
        )

    def stop_owned_services() -> bool:
        failed = False
        for service in reversed(owned_services):
            try:
                _docker_text(
                    repo_root,
                    compose_environment,
                    values,
                    "compose",
                    "stop",
                    service,
                )
            except BaseException:
                failed = True
        return not failed

    def delete_owned_bucket(name: str) -> None:
        if name != bucket:
            raise Gate4Error(CLEANUP_INCOMPLETE)
        from minio import Minio

        client = Minio(
            "127.0.0.1:9000",
            access_key=loaded.minio_access_key,
            secret_key=loaded.minio_secret,
            secure=False,
        )
        if not client.bucket_exists(name):
            return
        for item in client.list_objects(name, recursive=True):
            object_name = getattr(item, "object_name", None)
            if not isinstance(object_name, str) or not object_name:
                raise Gate4Error(CLEANUP_INCOMPLETE)
            client.remove_object(name, object_name)
        client.remove_bucket(name)

    def delete_owned_database(name: str) -> None:
        if name != database:
            raise Gate4Error(CLEANUP_INCOMPLETE)
        import psycopg
        from psycopg import sql

        connection_kwargs = {
            "host": "127.0.0.1",
            "port": 5432,
            "dbname": loaded.postgres_db,
            "user": loaded.postgres_user,
            "password": loaded.database_password,
        }
        with psycopg.connect(
            **connection_kwargs, autocommit=True
        ) as connection:
            exists = connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (name,),
            ).fetchone()
            if exists is None:
                return
            connection.execute(
                """
                SELECT pg_terminate_backend(pid)
                FROM pg_stat_activity
                WHERE datname = %s AND pid <> pg_backend_pid()
                """,
                (name,),
            )
            connection.execute(
                sql.SQL("DROP DATABASE {}").format(sql.Identifier(name))
            )

    body_error: BaseException | None = None
    ledger_initialized = False
    state = transition_safe_state(state, "PHASE1_STARTING")
    try:
        ProviderDelegateLedger.initialize(
            provider_ledger_path,
            run_id=run_id,
        )
        ledger_initialized = True
        persist()
        running_before = set(
            _docker_text(
                repo_root,
                compose_environment,
                values,
                "compose",
                "ps",
                "--status",
                "running",
                "--services",
            ).split()
        )
        for service in ("postgresql", "minio"):
            if service not in running_before:
                _docker_text(
                    repo_root,
                    compose_environment,
                    values,
                    "compose",
                    "up",
                    "-d",
                    "--wait",
                    service,
                    timeout=300.0,
                )
                owned_services.append(service)
            container_id = _docker_text(
                repo_root,
                compose_environment,
                values,
                "compose",
                "ps",
                "-q",
                service,
            ).strip()
            if re.fullmatch(r"[0-9a-f]{12,64}", container_id) is None:
                raise Gate4Error(DOCKER_GATE_FAILED)
            state["docker"][service] = {
                "owned": service in owned_services,
                "preexisting": service in running_before,
                "container_id": container_id,
            }
            persist()

        volume_names = _docker_text(
            repo_root,
            _system_child_environment(os.environ),
            values,
            "volume",
            "ls",
            "--format",
            "{{.Name}}",
        ).split()
        volume_count, volume_digest = _inventory_fingerprint(volume_names)

        try:
            import psycopg
            from psycopg import sql
            from minio import Minio
        except Exception as exc:
            raise Gate4Error(PHASE1_FAILED) from exc

        connection_kwargs = {
            "host": "127.0.0.1",
            "port": 5432,
            "dbname": loaded.postgres_db,
            "user": loaded.postgres_user,
            "password": loaded.database_password,
        }
        with psycopg.connect(
            **connection_kwargs, autocommit=True
        ) as connection:
            database_names = [
                row[0]
                for row in connection.execute(
                    "SELECT datname FROM pg_database ORDER BY datname"
                ).fetchall()
            ]
        database_count, database_digest = _inventory_fingerprint(
            database_names
        )
        minio_client = Minio(
            "127.0.0.1:9000",
            access_key=loaded.minio_access_key,
            secret_key=loaded.minio_secret,
            secure=False,
        )
        bucket_names = [
            bucket.name for bucket in minio_client.list_buckets()
        ]
        bucket_count, bucket_digest = _inventory_fingerprint(bucket_names)

        if (
            minio_client.bucket_exists(bucket)
        ):
            raise Gate4Error(PHASE1_FAILED)
        state["resources"] = {
            "database": None,
            "bucket": None,
            "actor_id": actor_id,
            "conversation_id": None,
            "tool_task_id": None,
            "database_baseline_count": database_count,
            "database_baseline_sha256": database_digest,
            "bucket_baseline_count": bucket_count,
            "bucket_baseline_sha256": bucket_digest,
            "volume_baseline_count": volume_count,
            "volume_baseline_sha256": volume_digest,
        }
        persist()
        with psycopg.connect(
            **connection_kwargs, autocommit=True
        ) as connection:
            if connection.execute(
                "SELECT 1 FROM pg_database WHERE datname = %s",
                (database,),
            ).fetchone() is not None:
                raise Gate4Error(PHASE1_FAILED)
            connection.execute(
                sql.SQL("CREATE DATABASE {}").format(
                    sql.Identifier(database)
                )
            )
        state["resources"]["database"] = database
        persist()
        minio_client.make_bucket(bucket)
        state["resources"]["bucket"] = bucket
        persist()

        migration_environment = build_real_role_environment(
            "migration",
            os.environ,
            controlled,
            safe_values={
                "database": database,
                "database_user": loaded.postgres_user,
                "backend_pythonpath": os.fspath(backend_source),
            },
        )
        migration_code = (
            "import os;"
            "from alembic import command;"
            "from alembic.config import Config;"
            "from materialsagent.infrastructure.config import load_settings;"
            "c=Config('backend/alembic.ini');"
            "c.attributes['settings']=load_settings(dict(os.environ));"
            "command.upgrade(c,'head')"
        )
        _read_text_command(
            [os.fspath(backend_python), "-c", migration_code],
            cwd=repo_root,
            environment=migration_environment,
            controlled_values=values,
            failure_marker=PHASE1_FAILED,
            timeout=300.0,
        )
        bootstrap_environment = build_real_role_environment(
            "bootstrap",
            os.environ,
            controlled,
            safe_values={
                "database": database,
                "database_user": loaded.postgres_user,
                "backend_pythonpath": os.fspath(backend_source),
                "actor_id": actor_id,
                "minio_access_key": loaded.minio_access_key,
                "bucket": bucket,
            },
        )
        bootstrap_code = (
            "from materialsagent.application.bootstrap import "
            "ensure_local_actor,ensure_object_storage_bucket;"
            "from materialsagent.infrastructure.config import load_settings;"
            "from materialsagent.infrastructure.db.session import "
            "create_engine_from_settings,create_session_factory;"
            "from materialsagent.infrastructure.db.unit_of_work import "
            "SQLAlchemyUnitOfWork;"
            "from materialsagent.infrastructure.storage.minio import "
            "create_minio_storage;"
            "s=load_settings(dict(__import__('os').environ));"
            "e=create_engine_from_settings(s);"
            "f=create_session_factory(e);"
            "ensure_local_actor(s,lambda:SQLAlchemyUnitOfWork(f));"
            "o=create_minio_storage(s);"
            "ensure_object_storage_bucket(o);o.close();e.dispose()"
        )
        _read_text_command(
            [os.fspath(backend_python), "-c", bootstrap_code],
            cwd=repo_root,
            environment=bootstrap_environment,
            controlled_values=values,
            failure_marker=PHASE1_FAILED,
            timeout=120.0,
        )
        _audit_real_backend_runtime_adapter(
            repo_root, controlled["runtime_token"]
        )

        backend_environment = build_real_role_environment(
            "backend-real",
            os.environ,
            controlled,
            safe_values={
                "actor_id": actor_id,
                "database": database,
                "database_user": loaded.postgres_user,
                "minio_access_key": loaded.minio_access_key,
                "bucket": bucket,
                "backend_pythonpath": os.fspath(backend_source),
                "provider_ledger_path": os.fspath(provider_ledger_path),
                "provider_ledger_run_id": run_id,
                "fresh_stage_c_authorization": (
                    fresh_stage_c_authorization
                ),
            },
        )
        backend_marker = f"p1b2-{run_id}-backend-real"
        backend_process, backend_record = _start_owned_process(
            role="backend-real",
            port=8000,
            command=[
                os.fspath(backend_python),
                os.fspath(Path(__file__).resolve()),
                "--serve-budgeted-backend",
                "--ownership-marker",
                backend_marker,
            ],
            environment=backend_environment,
            cwd=repo_root / "backend",
            run_root=run_root,
            marker=backend_marker,
            stdout_name="backend-real.stdout.log",
            stderr_name="backend-real.stderr.log",
            controlled_values=values,
        )
        started_records["backend"] = backend_record
        state["processes"]["backend"] = backend_record
        persist()
        _wait_backend_ready(
            backend_process,
            timeout=120.0,
            controlled_values=values,
        )
        verify_owned_process_port(backend_record)

        conversation_status, conversation_payload, _elapsed = _http_json(
            "POST",
            f"{BACKEND_URL}/api/v1/conversations",
            payload={"title": f"P1B2 Gate 4 {run_id}"},
            controlled_values=values,
            timeout=30.0,
        )
        conversation_data = conversation_payload.get("data")
        conversation_id = (
            conversation_data.get("conversation_id")
            if isinstance(conversation_data, dict)
            else None
        )
        if (
            conversation_status != 201
            or not isinstance(conversation_id, str)
            or not _is_safe_identifier(conversation_id)
        ):
            raise Gate4Error(PHASE1_FAILED)
        state["resources"]["conversation_id"] = conversation_id
        persist()

        node = shutil.which("node")
        vite = repo_root / "frontend" / "node_modules" / "vite" / "bin" / "vite.js"
        if node is None or not vite.is_file():
            raise Gate4Error(PHASE1_FAILED)
        frontend_environment = build_real_role_environment(
            "frontend",
            os.environ,
            controlled,
            safe_values={"backend_origin": BACKEND_URL},
        )
        frontend_marker = f"p1b2-{run_id}-frontend"
        frontend_process, frontend_record = _start_owned_process(
            role="frontend",
            port=3000,
            command=build_frontend_command(
                node=Path(node),
                vite=vite,
                marker=frontend_marker,
            ),
            environment=frontend_environment,
            cwd=repo_root / "frontend",
            run_root=run_root,
            marker=frontend_marker,
            stdout_name="frontend.stdout.log",
            stderr_name="frontend.stderr.log",
            controlled_values=values,
        )
        started_records["frontend"] = frontend_record
        state["processes"]["frontend"] = frontend_record
        persist()
        _wait_frontend_ready(frontend_process, timeout=120.0)
        verify_owned_process_port(frontend_record)
        if (
            _port_is_clear(8000)
            or _port_is_clear(3000)
            or not _port_is_clear(8100)
        ):
            raise Gate4Error(PHASE1_FAILED)
        verify_dotenv_unchanged(dotenv)
        if not _scan_run_artifacts(run_root, values):
            raise Gate4Error(SECRET_LEAK_MARKER)
        state = transition_safe_state(state, "PHASE1_READY")
        persist()
    except BaseException as exc:
        body_error = exc

    if body_error is not None:
        process_clean = stop_started_processes()
        ledger_clean = True
        if ledger_initialized:
            try:
                invalidate_provider_ledger(
                    provider_ledger_path,
                    run_id=run_id,
                )
            except BaseException:
                ledger_clean = False
        docker_snapshot = json.loads(json.dumps(state["docker"]))
        resources_clean = rollback_phase1_start_resources(
            state=state,
            expected_database=database,
            expected_bucket=bucket,
            delete_database=delete_owned_database,
            delete_bucket=delete_owned_bucket,
        )
        service_clean = stop_owned_services()
        if not service_clean:
            state["phase"] = "PHASE1_STARTING"
            state["docker"] = docker_snapshot
        try:
            persist()
        except BaseException:
            resources_clean = False
        dotenv_clean = True
        artifact_clean = True
        transient_clean = True
        try:
            verify_dotenv_unchanged(dotenv)
        except BaseException:
            dotenv_clean = False
        try:
            artifact_clean = _scan_run_artifacts(run_root, values)
        except BaseException:
            artifact_clean = False
        if not artifact_clean:
            if run_root.is_dir():
                try:
                    _quarantine_real_run(repo_root)
                except BaseException:
                    pass
            raise Gate4Error(SECRET_LEAK_MARKER) from body_error
        try:
            cleanup_phase1_transient_artifacts(run_root)
        except BaseException:
            transient_clean = False
        if (
            not process_clean
            or not ledger_clean
            or not resources_clean
            or not service_clean
            or not dotenv_clean
            or not transient_clean
        ):
            raise Gate4Error(CLEANUP_INCOMPLETE) from body_error
        raise body_error

    output_lines = [
        BROWSER_READY_MARKER,
        "Frontend:",
        FRONTEND_URL,
        "真实 Runtime：",
        "当前故意未启动",
        "请完成场景1–3并回复结果。",
    ]
    if scan_artifact_text("\n".join(output_lines), values):
        raise Gate4Error(SECRET_LEAK_MARKER)
    for line in output_lines:
        print(line)


def run_cli_action(
    action: Callable[[], Any], *, stderr: Any = sys.stderr
) -> int:
    """Run one CLI action while emitting no exception detail on failure."""

    try:
        action()
    except SystemExit as exc:
        if exc.code in (None, 0):
            return 0
        stderr.write(f"{EXECUTOR_FAILED_MARKER}\n")
        return 1
    except BaseException as exc:
        marker = str(exc)
        if isinstance(exc, Gate4Error) and marker in _SAFE_CLI_FAILURE_MARKERS:
            stderr.write(f"{marker}\n")
        else:
            stderr.write(f"{EXECUTOR_FAILED_MARKER}\n")
        return 1
    return 0


def _assert_raises_marker(marker: str, function: Callable[[], Any]) -> None:
    try:
        function()
    except Exception as exc:
        if marker not in str(exc):
            raise AssertionError("unexpected fixed failure marker") from exc
    else:
        raise AssertionError("expected fixed failure marker")


def _run_self_test() -> None:
    """Exercise every offline primitive without repository or external access."""

    class SelfTestReparseMetadata:
        st_file_attributes = 0x400
        st_mode = stat.S_IFDIR

    class SelfTestSymlinkMetadata:
        st_mode = stat.S_IFLNK

    assert _is_reparse_point(
        "injected-windows-path",
        lstat_func=lambda _path: SelfTestReparseMetadata(),
        platform_name="nt",
    )
    assert _is_reparse_point(
        "injected-posix-path",
        lstat_func=lambda _path: SelfTestSymlinkMetadata(),
        platform_name="posix",
    )

    controlled = {
        "real_provider_key": "selftest-real-provider-fixed-nonsecret",
        "invalid_provider_key": "selftest-invalid-provider-fixed-nonsecret",
        "runtime_token": "selftest-runtime-fixed-nonsecret",
        "database_password": "selftest-database-fixed-nonsecret",
        "minio_secret": "selftest-minio-fixed-nonsecret",
        "timeline_signing_key": "selftest-timeline-fixed-nonsecret",
    }

    assert evaluate_stage_b_memory_preflight(
        STAGE_B_MINIMUM_HOST_AVAILABLE_BYTES
    ) == {"free_bytes": STAGE_B_MINIMUM_HOST_AVAILABLE_BYTES}
    _assert_raises_marker(
        STAGE_B_MEMORY_PREFLIGHT_FAILED,
        lambda: evaluate_stage_b_memory_preflight(
            STAGE_B_MINIMUM_HOST_AVAILABLE_BYTES - 1
        ),
    )
    stable_samples = iter(
        (
            {"available_bytes": 6_400_000_000},
            {"available_bytes": 6_000_000_000},
            {"available_bytes": 6_200_000_000},
        )
    )
    stable_sleeps: list[float] = []
    assert measure_stable_free_memory_bytes(
        host_reader=lambda: next(stable_samples),
        sleep=stable_sleeps.append,
    ) == 6_200_000_000
    assert stable_sleeps == [2.0, 0.25, 0.25]
    stage_b_evidence = stage_b_memory_metrics(
        before_runtime_start_free_bytes=8_000_000_000,
        after_runtime_ready_free_bytes=6_500_000_000,
        pre_execute_free_bytes=8_000_000_000,
        minimum_during_execute_free_bytes=3_500_000_000,
        post_execute_free_bytes=5_000_000_000,
    )
    assert stage_b_evidence["stage_b_model_load_drop_bytes"] == 1_500_000_000
    assert stage_b_evidence["stage_b_execute_drop_bytes"] == 4_500_000_000
    assert stage_b_evidence["stage_b_total_drop_bytes"] == 4_500_000_000
    assert (
        required_stage_c_runtime_start_free_bytes(4 * 1024**3)
        == 5_905_580_032
    )
    assert evaluate_stage_c_runtime_start_memory_preflight(
        stage_b_memory=stage_b_evidence,
        stage_c_before_runtime_start_free_bytes=4_831_838_208,
    )["required_stage_c_runtime_start_free_bytes"] == 4_831_838_208
    assert (
        required_stage_c_tool_retry_free_bytes(3 * 1024**3)
        == 4_831_838_208
    )
    assert evaluate_stage_c_tool_retry_memory_preflight(
        stage_b_memory=stage_b_evidence,
        stage_c_pre_tool_retry_free_bytes=(
            required_stage_c_tool_retry_free_bytes(
                int(stage_b_evidence["stage_b_execute_drop_bytes"])
            )
        ),
    )["marker"] == TOOL_RETRY_RESOURCE_READY
    _assert_raises_marker(
        STAGE_C_RUNTIME_START_MEMORY_FAILED,
        lambda: evaluate_stage_c_runtime_start_memory_preflight(
            stage_b_memory={
                **stage_b_evidence,
                "stage_b_model_load_drop_bytes": 4 * 1024**3,
            },
            stage_c_before_runtime_start_free_bytes=5_905_580_031,
        ),
    )
    _assert_raises_marker(
        STAGE_C_TOOL_RETRY_MEMORY_FAILED,
        lambda: evaluate_stage_c_tool_retry_memory_preflight(
            stage_b_memory={
                **stage_b_evidence,
                "stage_b_execute_drop_bytes": 3 * 1024**3,
            },
            stage_c_pre_tool_retry_free_bytes=4_831_838_207,
        ),
    )

    parsed = parse_dotenv_text(
        "# literal only\nDEEPSEEK_API_KEY=$(not-executed)\nOTHER=value\n"
    )
    assert parsed["DEEPSEEK_API_KEY"] == "$(not-executed)"
    for invalid in (
        "OTHER=value\n",
        "DEEPSEEK_API_KEY=\n",
        "DEEPSEEK_API_KEY= value\n",
        "DEEPSEEK_API_KEY=value \n",
        "DEEPSEEK_API_KEY=bad\x01value\n",
        "DEEPSEEK_API_KEY=bad\u0080value\n",
        "DEEPSEEK_API_KEY=bad\u0085\n",
        "DEEPSEEK_API_KEY=one\nDEEPSEEK_API_KEY=two\n",
    ):
        _assert_raises_marker(GATE_KEY_ERROR, lambda invalid=invalid: parse_dotenv_text(invalid))

    with tempfile.TemporaryDirectory(prefix="p1b2-gate4-selftest-") as temporary:
        temporary_root = Path(temporary)
        dotenv = temporary_root / "controlled.env"
        dotenv.write_text(
            f"DEEPSEEK_API_KEY={controlled['real_provider_key']}\n",
            encoding="utf-8",
        )
        record = inspect_dotenv(dotenv)
        assert record.key == controlled["real_provider_key"]
        assert controlled["real_provider_key"] not in repr(record)
        assert verify_dotenv_unchanged(record, dotenv)
        _assert_raises_marker(
            GATE_KEY_ERROR, lambda: inspect_dotenv(temporary_root / "missing.env")
        )

        invalid_key = generate_invalid_provider_key(
            "selftest", controlled["real_provider_key"]
        )
        assert invalid_key != controlled["real_provider_key"]
        assert controlled["real_provider_key"] not in invalid_key
        role_controlled = dict(controlled)
        role_controlled["invalid_provider_key"] = invalid_key
        base = {"PATH": "selftest-path", "DEEPSEEK_API_KEY": "must-be-removed"}
        assert build_child_environment("frontend", base, role_controlled) == {
            "PATH": "selftest-path"
        }
        assert "DEEPSEEK_API_KEY" not in build_child_environment(
            "runtime", base, role_controlled
        )
        assert (
            build_child_environment("backend-real", base, role_controlled)[
                "DEEPSEEK_API_KEY"
            ]
            == controlled["real_provider_key"]
        )
        argv = build_child_command(
            role="backend",
            executable=sys.executable,
            arguments=["-m", "offline"],
            secret_values=role_controlled.values(),
        )
        assert not any(value in "\0".join(argv) for value in role_controlled.values())
        _assert_raises_marker(
            SECRET_IN_CHILD_COMMAND,
            lambda: build_child_command(
                role="backend",
                executable=sys.executable,
                arguments=["--unsafe", controlled["runtime_token"]],
                secret_values=role_controlled.values(),
            ),
        )

        ledger = BudgetLedger("selftest")
        runtime_calls: list[int] = []
        for index in range(2):
            ledger.delegate_runtime(lambda index=index: runtime_calls.append(index))
            ledger.delegate_ddpm(lambda: None)
        _assert_raises_marker(
            RUNTIME_BUDGET_ERROR,
            lambda: ledger.delegate_runtime(lambda: runtime_calls.append(3)),
        )
        _assert_raises_marker(
            RUNTIME_BUDGET_ERROR, lambda: ledger.delegate_ddpm(lambda: None)
        )
        provider_calls: list[str] = []
        provider_facts = {"CHAT": 0, "EXPLANATION": 0}
        provider_ledger_path = temporary_root / "provider-ledger.json"
        ProviderDelegateLedger.initialize(
            provider_ledger_path,
            run_id="selftest-stage-c",
        )
        provider_ledger = ProviderDelegateLedger(
            provider_ledger_path,
            run_id="selftest-stage-c",
        )
        for kind in ("CHAT", "CHAT", "CHAT", "EXPLANATION", "EXPLANATION"):
            provider_facts[kind] += 1
            provider_ledger.delegate(
                kind,
                lambda kind=kind: provider_calls.append(kind),
                fact_reader=lambda: dict(provider_facts),
            )
        _assert_raises_marker(
            PROVIDER_BUDGET_ERROR,
            lambda: ProviderDelegateLedger(
                provider_ledger_path,
                run_id="selftest-stage-c",
            ).delegate(
                "CHAT",
                lambda: provider_calls.append("six"),
                fact_reader=lambda: dict(provider_facts),
            ),
        )
        assert len(provider_calls) == 5 and len(runtime_calls) == 2
        accepted_resume = new_stage_c_resume_state(
            run_id="selftest-stage-c",
            dotenv_sha256="a" * 64,
        )
        assert accepted_resume["budgets"] == {
            "runtime": 1,
            "ddpm": 1,
        }
        assert ProviderDelegateLedger(
            provider_ledger_path,
            run_id="selftest-stage-c",
        ).read()["total_delegate_attempts"] == 5

        all_values = list(role_controlled.values())
        assert scan_artifact_text("safe", all_values) == []
        hits = scan_artifact_text(
            f"x{controlled['runtime_token']}y", all_values
        )
        assert hits and controlled["runtime_token"] not in str(hits)

        class SelfTestChat:
            def __init__(self) -> None:
                self.calls = 0
                self.outcomes = [
                    {"task_type": "KNOWLEDGE_QA", "status": "SUCCEEDED"},
                    {"task_type": "TOOL_EXECUTION", "status": "NEEDS_INPUT"},
                    {"task_type": "TOOL_EXECUTION", "status": "FAILED"},
                ]

            def delegate(self, **_kwargs: object) -> dict[str, str]:
                outcome = self.outcomes[self.calls]
                self.calls += 1
                return outcome

        class SelfTestRuntime:
            def __init__(self) -> None:
                self.calls = 0
                self.ddpm_calls = 0

            def delegate(self, **_kwargs: object) -> dict[str, object]:
                self.calls += 1
                self.ddpm_calls += 1
                return {
                    "result": {"result_id": "selftest-result"},
                    "asset": {"asset_id": "selftest-asset", "status": "AVAILABLE"},
                }

        class SelfTestExplanation:
            def __init__(self) -> None:
                self.calls = 0

            def delegate(self, **_kwargs: object) -> dict[str, str]:
                self.calls += 1
                if self.calls == 1:
                    raise RuntimeError("AUTHENTICATION_FAILED")
                return {"status": "SUCCEEDED", "text": "safe"}

        chat = SelfTestChat()
        runtime = SelfTestRuntime()
        explanation = SelfTestExplanation()
        journey = OfflineJourneyHarness(
            chat_adapter=chat,
            runtime_adapter=runtime,
            explanation_adapter=explanation,
        )
        journey.runtime_direct()
        assert journey.knowledge()["task"]["status"] == "SUCCEEDED"
        assert journey.needs_input()["task"]["status"] == "NEEDS_INPUT"
        unavailable = journey.runtime_unavailable()
        retried = journey.tool_retry(unavailable["task_id"])
        assert retried["task"]["status"] == "PARTIALLY_SUCCEEDED"
        completed = journey.explanation_retry(retried["result"]["result_id"])
        assert completed["task"]["status"] == "SUCCEEDED"
        assert chat.calls == 3
        assert runtime.calls == runtime.ddpm_calls == 2
        assert explanation.calls == 2

        class SelfTestHandle:
            def __init__(self) -> None:
                self.alive = True

        class SelfTestLauncher:
            def __init__(self) -> None:
                self.started: list[dict[str, object]] = []
                self.handles: list[SelfTestHandle] = []

            def start(
                self, role: str, child_argv: Sequence[str], env: Mapping[str, str]
            ) -> object:
                handle = SelfTestHandle()
                self.started.append(
                    {
                        "role": role,
                        "argv": tuple(child_argv),
                        "env": dict(env),
                        "handle": handle,
                    }
                )
                self.handles.append(handle)
                return handle

            def stop(self, handle: SelfTestHandle) -> None:
                assert handle.alive
                handle.alive = False

            def port_is_clear(self, port: int) -> bool:
                assert port == 8000
                return not any(handle.alive for handle in self.handles)

        launcher = SelfTestLauncher()
        parent_environment = dict(base)
        parent_before = dict(parent_environment)
        plans = plan_backend_restart_sequence(
            supervisor=ProcessSupervisor(launcher),
            base_environment=parent_environment,
            controlled=role_controlled,
            database="selftest-db",
            bucket="selftest-bucket",
            actor_id="selftest-actor",
            conversation_id="selftest-conversation",
        )
        assert [plan.mode for plan in plans] == ["real", "invalid", "real"]
        assert parent_environment == parent_before
        assert len(launcher.started) == 3
        assert [handle.alive for handle in launcher.handles] == [False, False, True]

        class SelfTestTimeout:
            total = 900

        class SelfTestPool:
            def __init__(self) -> None:
                self.requests: list[dict[str, object]] = []

            def request(self, method: str, url: str, **kwargs: object) -> object:
                self.requests.append({"method": method, "url": url, **kwargs})
                return object()

        class SelfTestHttpAdapter:
            def __init__(self) -> None:
                self._pool = SelfTestPool()
                self._timeout = SelfTestTimeout()
                self._retries = False

            def ready(self) -> object:
                return self._pool.request(
                    "GET",
                    "http://127.0.0.1:8100/internal/v1/health/ready",
                    timeout=self._timeout,
                    retries=self._retries,
                )

        assert audit_runtime_adapter(SelfTestHttpAdapter())

        class SelfTestProcess:
            def __init__(self, *, ignore_terminate: bool = False) -> None:
                self.ignore_terminate = ignore_terminate
                self.terminated = 0
                self.killed = 0
                self.waited = 0
                self.returncode: int | None = None

            def poll(self) -> int | None:
                return self.returncode

            def terminate(self) -> None:
                self.terminated += 1
                if not self.ignore_terminate:
                    self.returncode = 0

            def kill(self) -> None:
                self.killed += 1
                self.returncode = -9

            def wait(self, timeout: float | None = None) -> int:
                assert timeout == 5.0
                self.waited += 1
                if self.returncode is None:
                    raise TimeoutError
                return self.returncode

        process = SelfTestProcess()
        assert not wait_for_process(
            process,
            watchdog_seconds=1200,
            monotonic_values=iter((0.0, 1200.0)),
        )
        assert process.terminated == process.waited == 1
        assert process.killed == 0
        ignored_process = SelfTestProcess(ignore_terminate=True)
        assert not wait_for_process(
            ignored_process,
            watchdog_seconds=1200,
            monotonic_values=iter((0.0, 1200.0)),
        )
        assert ignored_process.terminated == ignored_process.killed == 1
        assert ignored_process.waited == 2

        cleanup_order: list[str] = []
        cleanup = OwnedCleanup()
        cleanup.add("first", lambda: cleanup_order.append("first"))
        cleanup.add("second", lambda: cleanup_order.append("second"))
        assert cleanup.run() and cleanup.run()
        assert cleanup_order == ["second", "first"]
        assert "PASSED" not in str(
            build_summary(
                passed=False, failures=["fixed failure"], cleanup_complete=True
            )
        )
        assert CLEANUP_INCOMPLETE in str(
            build_summary(passed=True, failures=[], cleanup_complete=False)
        )

        clean_run = temporary_root / "p1b2-gate4-selftest"
        clean_run.mkdir()
        (clean_run / "summary.txt").write_text("safe", encoding="utf-8")
        marker = temporary_root / "external-marker.txt"
        wrong_run = temporary_root / "not-the-owned-run"
        wrong_run.mkdir()
        _assert_raises_marker(
            "P1B2_GATE4_INVALID_QUARANTINE_SCOPE",
            lambda: scan_and_quarantine_artifacts(
                wrong_run,
                all_values,
                marker,
                allowed_parent=temporary_root,
                expected_run_id="selftest",
            ),
        )
        assert scan_and_quarantine_artifacts(
            clean_run,
            all_values,
            marker,
            allowed_parent=temporary_root,
            expected_run_id="selftest",
        )
        (clean_run / "stderr.txt").write_text(
            controlled["timeline_signing_key"], encoding="utf-8"
        )
        assert not scan_and_quarantine_artifacts(
            clean_run,
            all_values,
            marker,
            allowed_parent=temporary_root,
            expected_run_id="selftest",
        )
        assert not clean_run.exists()
        assert marker.read_text(encoding="utf-8") == SECRET_LEAK_MARKER
        class SelfTestStderr:
            def __init__(self) -> None:
                self.value = ""

            def write(self, value: str) -> None:
                self.value += value

        safe_stderr = SelfTestStderr()
        assert run_cli_action(
            lambda: (_ for _ in ()).throw(Gate4Error("private-detail")),
            stderr=safe_stderr,
        ) == 1
        assert safe_stderr.value.strip() == EXECUTOR_FAILED_MARKER

        # A missing or weakened state machine could let a later real action
        # skip an audit gate or persist a controlled value.  These literals
        # deliberately exercise the production state serializer itself.
        safe_state = new_safe_state(
            run_id="selftest-state",
            dotenv_sha256="a" * 64,
        )
        assert safe_state["phase"] == "STAGEB_STARTING"
        safe_state = transition_safe_state(safe_state, "STAGEB_COMPLETE")
        safe_state = transition_safe_state(safe_state, "PHASE1_STARTING")
        assert validate_safe_state(safe_state) is True
        _assert_raises_marker(
            "P1B2_GATE4_INVALID_STATE_TRANSITION",
            lambda: transition_safe_state(safe_state, "PHASE3_STARTING"),
        )
        state_text = serialize_safe_state(
            safe_state,
            controlled_values=role_controlled.values(),
        )
        assert not any(value in state_text for value in role_controlled.values())

        # A wrong coordinator mapping could restore the real key or begin the
        # audit at the wrong browser checkpoint.
        assert coordinator_command_name("PHASE2_READY") == "phase3"
        assert coordinator_command_name("PHASE3_READY") == "audit"
        assert coordinator_command_name("AUDIT_COMPLETE") == "cleanup"
        _assert_raises_marker(
            "P1B2_GATE4_INVALID_COORDINATOR_PHASE",
            lambda: coordinator_command_name("PHASE1_READY"),
        )

        real_role_env = build_real_role_environment(
            "frontend",
            {"PATH": "selftest-path", "DEEPSEEK_API_KEY": "parent-leak"},
            role_controlled,
            safe_values={"backend_origin": "http://127.0.0.1:8000"},
        )
        assert real_role_env == {
            "PATH": "selftest-path",
            "MATERIALSAGENT_BACKEND_ORIGIN": "http://127.0.0.1:8000",
        }
        assert "DEEPSEEK_API_KEY" not in real_role_env

        state_file = temporary_root / "state" / "state.json"
        write_safe_state(
            state_file,
            safe_state,
            controlled_values=role_controlled.values(),
            allowed_parent=temporary_root,
        )
        assert read_safe_state(
            state_file, allowed_parent=temporary_root
        ) == safe_state
        assert not any(
            value in state_file.read_text(encoding="utf-8")
            for value in role_controlled.values()
        )
        signal = temporary_root / "state" / "control" / "phase3.request"
        write_control_marker(
            signal,
            "P1B2_GATE4_PHASE_3_REQUEST",
            allowed_parent=temporary_root,
        )
        assert read_control_marker(
            signal,
            "P1B2_GATE4_PHASE_3_REQUEST",
            allowed_parent=temporary_root,
        )


class _SafeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> None:
        raise Gate4Error("P1B2_GATE4_INVALID_EXECUTOR_ARGUMENTS")


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = _SafeArgumentParser(
        description="P1B2 Gate 4 offline executor helpers"
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--self-test",
        action="store_true",
        help="run the pure offline executor self-test",
    )
    actions.add_argument(
        "--stage-b",
        action="store_true",
        help="run the authorized real Runtime direct acceptance",
    )
    actions.add_argument(
        "--phase-1",
        action="store_true",
        help="prepare the real Backend/Frontend browser pause",
    )
    actions.add_argument(
        "--serve-budgeted-backend",
        action="store_true",
        help=argparse.SUPPRESS,
    )
    actions.add_argument(
        "--mock-helper-tests",
        action="store_true",
        help="run the controlled offline Mock helper regression",
    )
    actions.add_argument(
        "--mock-full-regression",
        action="store_true",
        help="run the controlled clean-snapshot Phase 1A Mock regression",
    )
    parser.add_argument(
        "--ownership-marker",
        help=argparse.SUPPRESS,
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    def action() -> None:
        arguments = _build_argument_parser().parse_args(argv)
        if arguments.self_test:
            _run_self_test()
            print(EXECUTOR_SELF_TEST_MARKER)
            return
        if arguments.mock_helper_tests:
            _run_controlled_mock_helper_tests(_repo_root())
            return
        if arguments.mock_full_regression:
            _run_controlled_mock_full_regression(_repo_root())
            return
        if arguments.stage_b:
            raise Gate4Error(STAGE_B_FINAL_ATTEMPT_BLOCKED)
        if arguments.phase_1:
            raise Gate4Error(NEW_STAGE_C_NOT_AUTHORIZED)
        if arguments.serve_budgeted_backend:
            if not _is_safe_identifier(arguments.ownership_marker):
                raise Gate4Error(PROCESS_IDENTITY_FAILED)
            serve_real_budgeted_backend_from_environment(os.environ)
            return
        raise Gate4Error("P1B2_GATE4_EXECUTOR_ACTION_REQUIRED")

    return run_cli_action(action, stderr=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
