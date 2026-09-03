"""Offline contract tests for the P1B2 Gate 4 acceptance executor.

These tests deliberately exercise only the runner's in-memory helpers.  The
real-runtime journey is separately opt-in, so importing this module must not
open Docker, CUDA, MinIO, PostgreSQL, or a provider connection.
"""

from __future__ import annotations

import importlib.util
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
import urllib3


REPO_ROOT = Path(__file__).resolve().parents[3]
RUNNER_PATH = REPO_ROOT / "scripts" / "acceptance" / "run-phase-1b.py"
POWERSHELL_PATH = REPO_ROOT / "scripts" / "acceptance" / "run-phase-1b.ps1"
GATE_KEY_ERROR = "P1B2_GATE4_DEEPSEEK_KEY_NOT_AVAILABLE"
SECRET_LEAK_MARKER = "P1B2_GATE4_SECRET_LEAK_DETECTED"
REAL_AUTHORIZATION_ENV = (
    "P1B2_GATE4_REAL_PROVIDER_AUTHORIZED",
    "P1B2_GATE4_REAL_RUNTIME_AUTHORIZED",
    "P1B2_GATE4_BROWSER_AUTHORIZED",
    "P1B2_GATE4_PROJECT_OWNER_AUTHORIZED",
)


class FakeChatAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.outcomes = [
            {"task_type": "KNOWLEDGE_QA", "status": "SUCCEEDED"},
            {"task_type": "TOOL_EXECUTION", "status": "NEEDS_INPUT"},
            {"task_type": "TOOL_EXECUTION", "status": "FAILED"},
        ]

    def delegate(self, *_args, **_kwargs):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        return outcome


class FakeRuntimeAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.ddpm_calls = 0

    def delegate(self, *_args, **_kwargs):
        self.calls += 1
        self.ddpm_calls += 1
        return {
            "status": "SUCCEEDED",
            "result": {"result_id": "result-attempt-2"},
            "asset": {"asset_id": "asset-attempt-2", "status": "AVAILABLE"},
        }


class FakeExplanationAdapter:
    def __init__(self) -> None:
        self.calls = 0
        self.outcomes = ["AUTHENTICATION_FAILED", "SUCCEEDED"]

    def delegate(self, *_args, **_kwargs):
        outcome = self.outcomes[self.calls]
        self.calls += 1
        if outcome == "AUTHENTICATION_FAILED":
            raise RuntimeError("AUTHENTICATION_FAILED")
        return {"status": outcome, "text": "offline explanation"}


class FakeProcessLauncher:
    """Records child specs while making process startup entirely offline."""

    def __init__(self) -> None:
        self.started: list[object] = []
        self.handles: list[FakeHandle] = []
        self.events: list[tuple[str, object]] = []

    def start(self, role, argv, env):
        mode = (
            "invalid"
            if env.get("DEEPSEEK_API_KEY", "").startswith("sk-invalid-p1b2-")
            else role
        )
        if role == "backend" and mode != "invalid":
            mode = "real"
        handle = FakeHandle(role=role, mode=mode)
        captured = {
            "role": role,
            "argv": tuple(argv),
            "env": dict(env),
            "handle": handle,
        }
        self.started.append(captured)
        self.handles.append(handle)
        self.events.append(("start", mode))
        return handle

    def stop(self, handle) -> None:
        assert handle in self.handles
        assert handle.alive is True
        handle.alive = False
        self.events.append(("stop", handle.mode))

    def port_is_clear(self, port: int) -> bool:
        assert port == 8000
        clear = not any(
            handle.role == "backend" and handle.alive for handle in self.handles
        )
        self.events.append(("port-clear", port))
        return clear


class FakeHandle:
    def __init__(self, *, role: str, mode: str) -> None:
        self.role = role
        self.mode = mode
        self.alive = True


class FakeTimeout:
    def __init__(self, total: int) -> None:
        self.total = total


class FakePool:
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return {"status": 200}


class FakeRuntimeHttpAdapter:
    def __init__(self, *, total: int = 900, retries: bool = False) -> None:
        self._timeout = FakeTimeout(total)
        self._retries = retries
        self._pool = FakePool()

    def ready(self):
        return self._pool.request(
            "GET",
            "http://127.0.0.1:8100/internal/v1/health/ready",
            timeout=self._timeout,
            retries=self._retries,
        )


class FakeProcess:
    def __init__(self, *, ignore_terminate: bool = False) -> None:
        self.ignore_terminate = ignore_terminate
        self.terminate_calls = 0
        self.kill_calls = 0
        self.wait_calls = 0
        self.returncode: int | None = None
        self.events: list[str] = []

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminate_calls += 1
        self.events.append("terminate")
        if not self.ignore_terminate:
            self.returncode = 0

    def kill(self) -> None:
        self.kill_calls += 1
        self.events.append("kill")
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.events.append("wait")
        if self.returncode is None:
            raise TimeoutError("controlled process ignored terminate")
        return self.returncode


class FakeOwnedTreeRunner:
    def __init__(self, *, identity_matches: bool) -> None:
        self.identity_matches = identity_matches
        self.events: list[object] = []
        self.commands: list[list[str]] = []

    def verify_identity(self, record) -> bool:
        self.events.append("verify-identity")
        return self.identity_matches

    def run(self, argv) -> int:
        command = list(argv)
        self.events.append(("run", command))
        self.commands.append(command)
        return 0

    def owned_tree_gone(self, record) -> bool:
        self.events.append(("owned-tree-gone", tuple(record["owned_tree_pids"])))
        return True


class FakeSequence:
    def __init__(self, values) -> None:
        self._values = iter(values)

    def __call__(self):
        return next(self._values)


class FakePhase1Stores:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.deleted: list[str] = []

    def create_database(self, *_args, **_kwargs) -> str:
        self.created.append("database")
        return "phase1-unit-database"

    def create_bucket(self, *_args, **_kwargs) -> str:
        self.created.append("bucket")
        return "phase1-unit-bucket"

    def create_actor(self, *_args, **_kwargs) -> str:
        self.created.append("actor")
        return "phase1-unit-actor"

    def create_conversation(self, *_args, **_kwargs) -> str:
        self.created.append("conversation")
        return "phase1-unit-conversation"

    def delete_database(self, *_args, **_kwargs) -> None:
        self.deleted.append("database")

    def delete_bucket(self, *_args, **_kwargs) -> None:
        self.deleted.append("bucket")


class FakePhase1Processes:
    def __init__(
        self,
        *,
        ports: dict[int, bool] | None = None,
        fail_role: str | None = None,
    ) -> None:
        self.ports = ports or {8000: True, 3000: True, 8100: False}
        self.fail_role = fail_role
        self.events: list[object] = []
        self.handles: list[dict[str, object]] = []

    def start(self, role: str, environment) -> dict[str, object]:
        self.events.append(("start", role))
        if role == self.fail_role:
            raise RuntimeError("controlled phase1 start failure")
        handle = {
            "role": role,
            "environment": dict(environment),
            "owned": True,
            "alive": True,
        }
        self.handles.append(handle)
        return handle

    def stop(self, handle) -> None:
        assert handle in self.handles
        assert handle["owned"] is True
        if handle["alive"]:
            handle["alive"] = False
            self.events.append(("stop", handle["role"]))

    def port_is_listening(self, port: int) -> bool:
        self.events.append(("port", port, self.ports[port]))
        return self.ports[port]


class FakeSafeStateWriter:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.payloads: list[str] = []

    def write(self, state) -> None:
        payload = json.dumps(state, sort_keys=True)
        self.payloads.append(payload)
        self.events.append("state-written")


class FakeEventWriter(io.StringIO):
    def __init__(self, events: list[object]) -> None:
        super().__init__()
        self.events = events

    def write(self, text: str) -> int:
        self.events.append(("output", text))
        return super().write(text)


class FakeBackendAdapter:
    def __init__(self, *, total: int = 900, retries: bool = False) -> None:
        self._timeout = urllib3.Timeout(total=total)
        self._retries = retries
        self._pool = FakePool()

    def canary(self):
        return self._pool.request(
            "POST",
            "https://provider.invalid/offline-canary",
            timeout=self._timeout,
            retries=self._retries,
        )


@pytest.fixture(scope="module")
def runner():
    """Load the executor by path without placing secrets in process state."""
    assert RUNNER_PATH.is_file(), "P1B2 Gate 4 executor has not been created."
    spec = importlib.util.spec_from_file_location("p1b2_gate4_runner", RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def controlled() -> dict[str, str]:
    return {
        "real_provider_key": "unit-real-provider-key-not-a-secret",
        "invalid_provider_key": "sk-invalid-p1b2-unit-not-a-secret",
        "runtime_token": "unit-runtime-token-not-a-secret",
        "database_password": "unit-db-password-not-a-secret",
        "minio_secret": "unit-minio-secret-not-a-secret",
        "timeline_signing_key": "unit-timeline-signing-key-not-a-secret",
    }


def _assert_gate_key_error(exc: pytest.ExceptionInfo[Exception]) -> None:
    assert GATE_KEY_ERROR in str(exc.value)


def test_00_executor_is_present() -> None:
    """The intended first RED is a normal missing-executor assertion."""
    assert RUNNER_PATH.is_file(), "P1B2 Gate 4 executor has not been created."


def test_parse_dotenv_text_is_controlled_and_fails_closed(runner) -> None:
    # A regression that evaluated dotenv text, accepted duplicate keys, or
    # normalized whitespace would make one of these cases fail.
    parsed = runner.parse_dotenv_text(
        "# comment\nUNRELATED=value\nDEEPSEEK_API_KEY=$(never-execute)\n"
    )
    assert parsed == {
        "UNRELATED": "value",
        "DEEPSEEK_API_KEY": "$(never-execute)",
    }

    invalid_texts = [
        "OTHER=value\n",
        "DEEPSEEK_API_KEY=\n",
        "DEEPSEEK_API_KEY= value\n",
        "DEEPSEEK_API_KEY=value \n",
        "DEEPSEEK_API_KEY=bad\x01value\n",
        "DEEPSEEK_API_KEY=one\nDEEPSEEK_API_KEY=two\n",
        "not-a-key-value-line\n",
    ]
    for text in invalid_texts:
        with pytest.raises(Exception) as exc:
            runner.parse_dotenv_text(text)
        _assert_gate_key_error(exc)


def test_dotenv_file_validation_is_fail_closed_and_never_changes_the_file(
    runner, tmp_path
) -> None:
    # This catches a runner that silently repairs an invalid file, accepts a
    # control byte, or rewrites .env while parsing it.
    missing = tmp_path / ".env.missing"
    with pytest.raises(Exception) as exc:
        runner.inspect_dotenv(missing)
    _assert_gate_key_error(exc)

    valid = tmp_path / ".env"
    valid.write_text("DEEPSEEK_API_KEY=unit-real-provider-key-not-a-secret\n", encoding="utf-8")
    before = sha256(valid.read_bytes()).hexdigest()
    record = runner.inspect_dotenv(valid)
    assert record.exists is True
    assert record.start_sha256 == before
    assert record.key == "unit-real-provider-key-not-a-secret"
    assert "unit-real-provider-key-not-a-secret" not in str(record.safe_snapshot())
    runner.verify_dotenv_unchanged(record, valid)
    assert sha256(valid.read_bytes()).hexdigest() == before

    invalid_values = ["", " value", "value ", "one\nDEEPSEEK_API_KEY=two"]
    invalid_values.extend(f"value{chr(code)}tail" for code in range(0, 32))
    invalid_values.append(f"value{chr(127)}tail")
    invalid_values.extend(
        f"value{chr(code)}tail" for code in range(0x80, 0xA0)
    )
    for index, value in enumerate(invalid_values):
        candidate = tmp_path / f"invalid-{index}.env"
        candidate.write_text(f"DEEPSEEK_API_KEY={value}\n", encoding="utf-8")
        initial_hash = sha256(candidate.read_bytes()).hexdigest()
        with pytest.raises(Exception) as exc:
            runner.inspect_dotenv(candidate)
        _assert_gate_key_error(exc)
        assert sha256(candidate.read_bytes()).hexdigest() == initial_hash


def _stage_b_dotenv_values() -> dict[str, str]:
    return {
        "DEEPSEEK_API_KEY": "unit-provider-value",
        "ZTA35G_RUNTIME_TOKEN": "unit-runtime-value",
        "POSTGRES_PASSWORD": "unit-postgres-password",
        "MINIO_SECRET_KEY": "unit-minio-secret",
        "TIMELINE_CURSOR_SIGNING_KEY": "unit-timeline-key",
        "POSTGRES_USER": "unit_pg_user",
        "POSTGRES_DB": "unit_pg_db",
        "MINIO_ACCESS_KEY": "unit_minio_access",
    }


def _write_stage_b_dotenv(path: Path, values: dict[str, str]) -> None:
    path.write_text(
        "".join(f"{name}={value}\n" for name, value in values.items()),
        encoding="utf-8",
    )


def test_stage_b_load_gate4_secrets_maps_exact_controlled_values(
    runner, tmp_path
) -> None:
    dotenv = tmp_path / ".env"
    values = _stage_b_dotenv_values()
    _write_stage_b_dotenv(dotenv, values)

    loaded = runner.load_gate4_secrets(dotenv)
    assert loaded.real_provider_key == values["DEEPSEEK_API_KEY"]
    assert loaded.runtime_token == values["ZTA35G_RUNTIME_TOKEN"]
    assert loaded.database_password == values["POSTGRES_PASSWORD"]
    assert loaded.minio_secret == values["MINIO_SECRET_KEY"]
    assert (
        loaded.timeline_signing_key
        == values["TIMELINE_CURSOR_SIGNING_KEY"]
    )
    assert loaded.postgres_user == values["POSTGRES_USER"]
    assert loaded.postgres_db == values["POSTGRES_DB"]
    assert loaded.minio_access_key == values["MINIO_ACCESS_KEY"]

    secrets_loaded = [
        loaded.real_provider_key,
        loaded.runtime_token,
        loaded.database_password,
        loaded.minio_secret,
        loaded.timeline_signing_key,
    ]
    assert len(secrets_loaded) == len(set(secrets_loaded))
    for other in secrets_loaded[1:]:
        assert loaded.real_provider_key not in other
        assert other not in loaded.real_provider_key


def test_stage_b_load_gate4_secrets_fails_closed_for_every_invalid_field(
    runner, tmp_path
) -> None:
    baseline = _stage_b_dotenv_values()
    invalid_values = ["", " leading", "trailing "]
    invalid_values.extend(chr(code) for code in range(0x00, 0x20))
    invalid_values.append(chr(0x7F))
    invalid_values.extend(chr(code) for code in range(0x80, 0xA0))

    for missing_name in baseline:
        missing = dict(baseline)
        del missing[missing_name]
        candidate = tmp_path / f"missing-{missing_name}.env"
        _write_stage_b_dotenv(candidate, missing)
        with pytest.raises(runner.Gate4Error):
            runner.load_gate4_secrets(candidate)

    for field_name in baseline:
        for index, invalid in enumerate(invalid_values):
            malformed = dict(baseline)
            malformed[field_name] = f"value{invalid}tail" if invalid else ""
            candidate = tmp_path / f"invalid-{field_name}-{index}.env"
            _write_stage_b_dotenv(candidate, malformed)
            with pytest.raises(runner.Gate4Error):
                runner.load_gate4_secrets(candidate)


def test_child_roles_isolate_secrets_and_argv(runner, controlled) -> None:
    # A future role that inherits a provider/database secret accidentally must
    # be caught here, rather than only by a post-run artifact scan.
    base = {"PATH": "unit-path", "DEEPSEEK_API_KEY": "parent-must-not-leak"}
    forbidden_everywhere = {
        controlled["real_provider_key"],
        controlled["invalid_provider_key"],
        controlled["runtime_token"],
        controlled["database_password"],
        controlled["minio_secret"],
        controlled["timeline_signing_key"],
        base["DEEPSEEK_API_KEY"],
    }
    role_envs = {
        role: runner.build_child_environment(role, base, controlled)
        for role in ("runtime", "backend-real", "backend-invalid", "frontend")
    }
    assert role_envs["runtime"].get("ZTA35G_RUNTIME_TOKEN") == controlled["runtime_token"]
    assert role_envs["frontend"] == {"PATH": "unit-path"}
    assert role_envs["backend-real"]["DEEPSEEK_API_KEY"] == controlled["real_provider_key"]
    assert role_envs["backend-invalid"]["DEEPSEEK_API_KEY"] == controlled["invalid_provider_key"]
    for role in ("runtime", "frontend"):
        assert "DEEPSEEK_API_KEY" not in role_envs[role]
    for role, environment in role_envs.items():
        values = set(environment.values())
        if role == "runtime":
            assert controlled["runtime_token"] in values
            values.remove(controlled["runtime_token"])
        if role.startswith("backend-"):
            allowed = {
                controlled["real_provider_key"]
                if role == "backend-real"
                else controlled["invalid_provider_key"],
                controlled["runtime_token"],
                controlled["database_password"],
                controlled["minio_secret"],
                controlled["timeline_signing_key"],
            }
            assert allowed <= values
            values -= allowed
        assert not (values & forbidden_everywhere), role

        argv = runner.build_child_command(
            role=role,
            executable=sys.executable,
            arguments=["-c", "print('offline')"],
            secret_values=forbidden_everywhere,
        )
        argv_text = "\0".join(argv)
        assert not any(secret in argv_text for secret in forbidden_everywhere)

    for secret in forbidden_everywhere:
        with pytest.raises(
            Exception, match="P1B2_GATE4_SECRET_IN_CHILD_COMMAND"
        ):
            runner.build_child_command(
                role="backend",
                executable=sys.executable,
                arguments=["--unsafe-value", secret],
                secret_values=forbidden_everywhere,
            )


def test_repository_gate_uses_scope_allowlist_without_historical_status_snapshot(
    runner, monkeypatch
) -> None:
    def fake_git_output(_repo_root, *arguments):
        if arguments == ("branch", "--show-current"):
            return runner.EXPECTED_BRANCH
        if arguments == ("rev-parse", "HEAD"):
            return runner.EXPECTED_HEAD
        if arguments == ("log", "-1", "--format=%H%n%s%n%P"):
            return "\n".join(
                [
                    runner.EXPECTED_HEAD,
                    runner.EXPECTED_SUBJECT,
                    runner.EXPECTED_PARENT,
                ]
            )
        raise AssertionError(f"unexpected dynamic Git-state probe: {arguments!r}")

    commands: list[list[str]] = []

    def fake_read_text_command(command, **_kwargs):
        commands.append(command)
        return ""

    monkeypatch.setattr(runner, "_git_output", fake_git_output)
    monkeypatch.setattr(runner, "_read_text_command", fake_read_text_command)
    monkeypatch.setattr(
        runner.shutil,
        "which",
        lambda name: f"C:/tools/{name}.exe",
    )

    runner._validate_repository_gate(REPO_ROOT)

    scope_command = next(
        command for command in commands if "check-scope.ps1" in " ".join(command)
    )
    assert "-Milestone" not in scope_command
    assert scope_command[-4:] == [
        "-Label",
        "P1B2",
        "-AllowlistFile",
        os.fspath(REPO_ROOT / "scripts" / "acceptance" / "allowlists" / "p1b2.txt"),
    ]


def test_invalid_key_is_random_safe_and_not_derived_from_real_key(runner, controlled) -> None:
    first = runner.generate_invalid_provider_key("unit-run", controlled["real_provider_key"])
    second = runner.generate_invalid_provider_key("unit-run", controlled["real_provider_key"])
    assert first.startswith("sk-invalid-p1b2-unit-run-")
    assert first != second
    assert first != controlled["real_provider_key"]
    assert first == first.strip()
    assert not any(ord(character) < 32 or ord(character) == 127 for character in first)
    assert controlled["real_provider_key"] not in first


def test_runtime_budget_ledger_refuses_third_runtime_and_ddpm_before_delegate(
    runner,
) -> None:
    ledger = runner.BudgetLedger("unit-run")
    runtime_calls: list[str] = []

    assert ledger.delegate_runtime(lambda: runtime_calls.append("one") or "one") == "one"
    assert ledger.delegate_runtime(lambda: runtime_calls.append("two") or "two") == "two"
    with pytest.raises(Exception, match="P1B2_GATE4_RUNTIME_BUDGET_EXCEEDED"):
        ledger.delegate_runtime(lambda: runtime_calls.append("three"))
    assert runtime_calls == ["one", "two"]

    ddpm_calls: list[str] = []
    ledger.delegate_ddpm(lambda: ddpm_calls.append("one"))
    ledger.delegate_ddpm(lambda: ddpm_calls.append("two"))
    with pytest.raises(Exception, match="P1B2_GATE4_RUNTIME_BUDGET_EXCEEDED"):
        ledger.delegate_ddpm(lambda: ddpm_calls.append("three"))
    assert ddpm_calls == ["one", "two"]

    snapshot = ledger.safe_snapshot()
    assert snapshot == {
        "run_id": "unit-run",
        "runtime_delegates": 2,
        "ddpm_delegates": 2,
    }


def test_stage_c_resume_state_seeds_project_owner_accepted_stage_b_budgets(
    runner,
) -> None:
    state = runner.new_stage_c_resume_state(
        run_id="stage-c-unit-run",
        dotenv_sha256="a" * 64,
    )

    assert state["phase"] == "STAGEB_COMPLETE"
    assert state["budgets"] == {
        "runtime": 1,
        "ddpm": 1,
    }
    assert state["resources"] == {}
    assert state["processes"] == {}
    assert runner.validate_safe_state(state) is True


def test_phase_1_creates_one_stage_c_resume_after_stage_b_cleanup_and_reuses_it(
    runner,
    tmp_path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run_ids = iter(["stage-c-first", "must-not-be-used"])

    created = runner.load_or_create_stage_c_resume_state(
        repo_root=repo,
        dotenv_sha256="a" * 64,
        run_id_factory=lambda: next(run_ids),
        controlled_values=("unit-secret",),
    )
    created["budgets"]["runtime"] = 2
    runner.write_safe_state(
        runner._state_path(repo),
        created,
        controlled_values=("unit-secret",),
        allowed_parent=runner._run_root(repo),
    )

    resumed = runner.load_or_create_stage_c_resume_state(
        repo_root=repo,
        dotenv_sha256="a" * 64,
        run_id_factory=lambda: next(run_ids),
        controlled_values=("unit-secret",),
    )

    assert resumed["run_id"] == "stage-c-first"
    assert resumed["budgets"] == {
        "runtime": 2,
        "ddpm": 1,
    }


def test_invalidated_stage_c_state_is_removed_before_a_fresh_run_id(
    runner,
    tmp_path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    invalidated = runner.new_stage_c_resume_state(
        run_id="invalidated-stage-c-run",
        dotenv_sha256="a" * 64,
    )
    invalidated["phase"] = "INVALIDATED"
    runner.write_safe_state(
        runner._state_path(repo),
        invalidated,
        controlled_values=("unit-secret",),
        allowed_parent=runner._run_root(repo),
    )

    fresh = runner.load_or_create_stage_c_resume_state(
        repo_root=repo,
        dotenv_sha256="a" * 64,
        controlled_values=("unit-secret",),
        run_id_factory=lambda: "fresh-stage-c-run",
    )

    assert fresh["run_id"] == "fresh-stage-c-run"
    assert fresh["phase"] == "STAGEB_COMPLETE"
    assert fresh["budgets"] == {"runtime": 1, "ddpm": 1}


def test_runtime_budget_resume_contains_no_provider_snapshot(
    runner,
) -> None:
    first = runner.BudgetLedger.from_persisted(
        "stage-c-unit-run",
        {
            "runtime": 1,
            "ddpm": 1,
        },
    )
    first.delegate_runtime(lambda: None)
    first.delegate_ddpm(lambda: None)

    persisted = first.persisted_budgets()
    restarted = runner.BudgetLedger.from_persisted(
        "stage-c-unit-run",
        persisted,
    )
    assert restarted.persisted_budgets() == {
        "runtime": 2,
        "ddpm": 2,
    }
    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_RUNTIME_BUDGET_EXCEEDED",
    ):
        restarted.delegate_runtime(lambda: pytest.fail("delegate ran"))
    assert "chat" not in persisted
    assert "explanation" not in persisted


def test_secret_scan_detects_every_controlled_value_without_disclosing_it(runner, controlled) -> None:
    # These are fixed non-secret fixtures; the production scanner receives the
    # true values only in memory.
    all_values = list(controlled.values())
    clean = "run=unit status=FAILED safe_error=P1B2_GATE4_BLOCKED"
    assert runner.scan_artifact_text(clean, all_values) == []
    for value in all_values:
        hits = runner.scan_artifact_text(f"prefix {value} suffix", all_values)
        assert hits, "exact secret matching must find every controlled value"
        assert value not in str(hits), "scanner result must not echo a secret"


def test_offline_journey_uses_real_control_flow_without_external_fixtures(runner) -> None:
    """Exercise the Gate's own orchestration using counters, not self-reports."""
    chat = FakeChatAdapter()
    runtime = FakeRuntimeAdapter()
    explanation = FakeExplanationAdapter()
    journey = runner.OfflineJourneyHarness(
        chat_adapter=chat,
        runtime_adapter=runtime,
        explanation_adapter=explanation,
    )

    journey.runtime_direct()
    knowledge = journey.knowledge()
    needs_input = journey.needs_input()
    unavailable = journey.runtime_unavailable()
    retried = journey.tool_retry(unavailable["task_id"])
    assert knowledge["task"]["status"] == "SUCCEEDED"
    assert needs_input["task"]["status"] == "NEEDS_INPUT"
    assert unavailable["tool_run"]["attempt_no"] == 1
    assert unavailable["tool_run"]["status"] == "FAILED"
    assert unavailable["result"] is None
    assert unavailable["asset"] is None
    assert retried["task"]["status"] == "PARTIALLY_SUCCEEDED"
    assert retried["tool_run"]["attempt_no"] == 2
    assert retried["tool_run"]["status"] == "SUCCEEDED"
    assert retried["result"]["result_id"] == "result-attempt-2"
    assert retried["asset"]["asset_id"] == "asset-attempt-2"
    assert retried["explanation"]["status"] == "FAILED"
    assert retried["explanation"]["safe_error"] == "AUTHENTICATION_FAILED"
    assert chat.calls == 3
    assert runtime.calls == runtime.ddpm_calls == 2  # direct + Tool retry only
    assert explanation.calls == 1  # authentication failure is never auto-retried

    completed = journey.explanation_retry(retried["result"]["result_id"])
    assert completed["task"]["status"] == "SUCCEEDED"
    assert completed["result"] == retried["result"]
    assert completed["asset"] == retried["asset"]
    assert completed["selected_tool_run_id"] == retried["tool_run"]["tool_run_id"]
    assert completed["selected_result_id"] == retried["result"]["result_id"]
    assert completed["explanation"]["status"] == "SUCCEEDED"
    assert chat.calls == 3
    assert runtime.calls == runtime.ddpm_calls == 2
    assert explanation.calls == 2
    assert journey.provider_ledger.read()[
        "total_delegate_attempts"
    ] == 5


def test_backend_restart_plans_keep_resources_and_secrets_role_scoped(runner, controlled) -> None:
    parent_environment = {"PATH": "unit-path", "DEEPSEEK_API_KEY": "parent-value"}
    parent_before = dict(parent_environment)
    launcher = FakeProcessLauncher()
    supervisor = runner.ProcessSupervisor(launcher)
    plans = runner.plan_backend_restart_sequence(
        supervisor=supervisor,
        base_environment=parent_environment,
        controlled=controlled,
        database="gate4_unit_db",
        bucket="gate4-unit-bucket",
        actor_id="actor-unit",
        conversation_id="conversation-unit",
    )
    assert len(plans) == len(launcher.started) == 3
    assert [plan.mode for plan in plans] == ["real", "invalid", "real"]
    assert launcher.events == [
        ("start", "real"),
        ("stop", "real"),
        ("port-clear", 8000),
        ("start", "invalid"),
        ("stop", "invalid"),
        ("port-clear", 8000),
        ("start", "real"),
    ]
    assert sum(
        handle.role == "backend" and handle.alive
        for handle in launcher.handles
    ) == 1
    for plan, captured in zip(plans, launcher.started, strict=True):
        assert plan.database == "gate4_unit_db"
        assert plan.bucket == "gate4-unit-bucket"
        assert plan.actor_id == "actor-unit"
        assert plan.conversation_id == "conversation-unit"
        assert plan.environment["ZTA35G_RUNTIME_TOKEN"] == controlled["runtime_token"]
        assert plan.environment["TIMELINE_CURSOR_SIGNING_KEY"] == controlled["timeline_signing_key"]
        assert not any(secret in "\0".join(plan.argv) for secret in controlled.values())
        assert captured["role"] == "backend"
        assert captured["argv"] == tuple(plan.argv)
        assert captured["env"] == plan.environment
        assert captured["handle"] is plan.process
    assert parent_environment == parent_before

    provider_key = controlled["real_provider_key"]
    for role in ("runtime", "frontend", "compose", "database", "minio", "log"):
        environment = runner.build_child_environment(role, parent_environment, controlled)
        assert provider_key not in environment.values()
        assert controlled["invalid_provider_key"] not in environment.values()
        supervisor.launch(role, [sys.executable, "-c", "pass"], environment)
    assert [item["role"] for item in launcher.started[3:]] == [
        "runtime",
        "frontend",
        "compose",
        "database",
        "minio",
        "log",
    ]


def test_runtime_adapter_audit_observes_900_second_timeout_and_no_retries(runner) -> None:
    adapter = FakeRuntimeHttpAdapter()
    runner.audit_runtime_adapter(adapter)
    assert len(adapter._pool.requests) == 1
    request = adapter._pool.requests[0]
    assert request["timeout"] is adapter._timeout
    assert adapter._timeout.total == 900
    assert request["retries"] is False

    with pytest.raises(Exception):
        runner.audit_runtime_adapter(FakeRuntimeHttpAdapter(total=899))
    with pytest.raises(Exception):
        runner.audit_runtime_adapter(FakeRuntimeHttpAdapter(retries=True))


def test_process_watchdog_waits_after_successful_terminate(runner) -> None:
    process = FakeProcess()
    completed = runner.wait_for_process(
        process,
        watchdog_seconds=1200,
        monotonic_values=iter((0.0, 1200.0)),
    )
    assert completed is False
    assert process.terminate_calls == 1
    assert process.kill_calls == 0
    assert process.wait_calls == 1
    assert process.events == ["terminate", "wait"]
    assert process.poll() is not None


def test_process_watchdog_escalates_to_kill_when_terminate_is_ignored(
    runner,
) -> None:
    process = FakeProcess(ignore_terminate=True)
    completed = runner.wait_for_process(
        process,
        watchdog_seconds=1200,
        monotonic_values=iter((0.0, 1200.0)),
    )
    assert completed is False
    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert process.wait_calls == 2
    assert process.events == ["terminate", "wait", "kill", "wait"]
    assert process.poll() is not None


def test_explanation_retry_visibility_matches_existing_product_semantics(runner) -> None:
    succeeded = {"status": "SUCCEEDED"}
    failed = {"status": "FAILED"}
    assert runner.explanation_retry_visible(result_exists=True, explanation=succeeded, latest_failure=None) is False
    assert runner.explanation_retry_visible(result_exists=True, explanation=failed, latest_failure=None) is True
    assert runner.explanation_retry_visible(result_exists=True, explanation=None, latest_failure=failed) is True
    assert runner.explanation_retry_visible(result_exists=True, explanation=succeeded, latest_failure=failed) is True
    assert runner.explanation_retry_visible(result_exists=False, explanation=failed, latest_failure=None) is False


def test_cleanup_is_idempotent_and_failed_summary_never_claims_passed(runner) -> None:
    cleaned: list[str] = []
    cleanup = runner.OwnedCleanup()
    cleanup.add("temporary-db", lambda: cleaned.append("database"))
    cleanup.add("temporary-bucket", lambda: cleaned.append("bucket"))
    cleanup.run()
    cleanup.run()
    assert cleaned == ["bucket", "database"]

    failed = runner.build_summary(passed=False, failures=["controlled failure"], cleanup_complete=True)
    assert "PASSED" not in str(failed)
    assert "P1B2_GATE4_BLOCKED" in str(failed)
    incomplete = runner.build_summary(passed=True, failures=[], cleanup_complete=False)
    assert "PASSED" not in str(incomplete)
    assert "P1B2_GATE4_CLEANUP_INCOMPLETE" in str(incomplete)


def test_artifact_scan_quarantines_entire_run_and_keeps_only_external_marker(
    runner, controlled, tmp_path
) -> None:
    secret_values = list(controlled.values())
    run_dir = tmp_path / "p1b2-gate4-unit-run"
    run_dir.mkdir()
    for kind in ("stdout", "stderr", "json", "junit", "summary", "report"):
        (run_dir / f"{kind}.txt").write_text("safe offline output", encoding="utf-8")
    marker = tmp_path / "quarantine-marker.txt"
    assert runner.scan_and_quarantine_artifacts(
        run_dir,
        secret_values,
        marker,
        allowed_parent=tmp_path,
        expected_run_id="unit-run",
    ) is True
    assert run_dir.is_dir()
    (run_dir / "stderr.txt").write_text(
        f"leak {controlled['real_provider_key']}", encoding="utf-8"
    )
    assert runner.scan_and_quarantine_artifacts(
        run_dir,
        secret_values,
        marker,
        allowed_parent=tmp_path,
        expected_run_id="unit-run",
    ) is False
    assert not run_dir.exists()
    content = marker.read_text(encoding="utf-8")
    assert content == SECRET_LEAK_MARKER
    assert not any(secret in content for secret in secret_values)


def test_artifact_quarantine_rejects_parent_sibling_repository_and_bad_marker(
    runner, controlled, tmp_path
) -> None:
    secrets = list(controlled.values())
    marker = tmp_path / "marker.txt"
    sibling = tmp_path / "not-the-owned-run"
    sibling.mkdir()
    with pytest.raises(Exception, match="P1B2_GATE4_INVALID_QUARANTINE_SCOPE"):
        runner.scan_and_quarantine_artifacts(
            tmp_path,
            secrets,
            marker,
            allowed_parent=tmp_path,
            expected_run_id="unit-run",
        )
    with pytest.raises(Exception, match="P1B2_GATE4_INVALID_QUARANTINE_SCOPE"):
        runner.scan_and_quarantine_artifacts(
            sibling,
            secrets,
            marker,
            allowed_parent=tmp_path,
            expected_run_id="unit-run",
        )
    with pytest.raises(Exception, match="P1B2_GATE4_INVALID_QUARANTINE_SCOPE"):
        runner.scan_and_quarantine_artifacts(
            REPO_ROOT,
            secrets,
            marker,
            allowed_parent=tmp_path,
            expected_run_id="unit-run",
        )

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    exact = allowed / "p1b2-gate4-unit-run"
    exact.mkdir()
    (exact / "stderr.txt").write_text(
        controlled["runtime_token"], encoding="utf-8"
    )
    with pytest.raises(Exception, match="P1B2_GATE4_INVALID_QUARANTINE_SCOPE"):
        runner.scan_and_quarantine_artifacts(
            exact,
            secrets,
            exact / "marker.txt",
            allowed_parent=allowed,
            expected_run_id="unit-run",
        )
    with pytest.raises(Exception, match="P1B2_GATE4_INVALID_QUARANTINE_SCOPE"):
        runner.scan_and_quarantine_artifacts(
            exact,
            secrets,
            tmp_path / "outside-allowed-marker.txt",
            allowed_parent=allowed,
            expected_run_id="unit-run",
        )


def test_artifact_quarantine_rejects_symlinked_run_directory(
    runner, controlled, tmp_path
) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    target = tmp_path / "actual-run"
    target.mkdir()
    linked_run = allowed / "p1b2-gate4-unit-run"
    try:
        linked_run.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("local Windows policy does not permit symlink creation")
    with pytest.raises(Exception, match="P1B2_GATE4_INVALID_QUARANTINE_SCOPE"):
        runner.scan_and_quarantine_artifacts(
            linked_run,
            list(controlled.values()),
            allowed / "marker.txt",
            allowed_parent=allowed,
            expected_run_id="unit-run",
        )


def test_artifact_quarantine_rejects_nested_windows_junction_without_deleting_target(
    runner, controlled, tmp_path
) -> None:
    if os.name != "nt":
        pytest.skip("Windows junction regression")

    allowed_parent = tmp_path / "allowed"
    allowed_parent.mkdir()
    run_dir = allowed_parent / "p1b2-gate4-unit-run"
    run_dir.mkdir()
    external_target = tmp_path / "external-target"
    external_target.mkdir()
    target_file = external_target / "must-survive.txt"
    target_file.write_text(controlled["runtime_token"], encoding="utf-8")
    junction = run_dir / "nested-junction"
    subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "mklink",
            "/J",
            str(junction),
            str(external_target),
        ],
        check=True,
        capture_output=True,
    )

    try:
        caught: Exception | None = None
        try:
            runner.scan_and_quarantine_artifacts(
                run_dir,
                list(controlled.values()),
                allowed_parent / "marker.txt",
                allowed_parent=allowed_parent,
                expected_run_id="unit-run",
            )
        except Exception as exc:
            caught = exc
        assert run_dir.is_dir()
        assert external_target.is_dir()
        assert target_file.is_file()
        assert caught is not None
        assert "P1B2_GATE4_ARTIFACT_SCAN_FAILED" in str(caught)
    finally:
        if junction.exists():
            os.rmdir(junction)


def test_artifact_quarantine_rejects_windows_reparse_allowed_parent(
    runner, controlled, tmp_path
) -> None:
    if os.name != "nt":
        pytest.skip("Windows reparse-point regression")

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    real_run_dir = real_parent / "p1b2-gate4-unit-run"
    real_run_dir.mkdir()
    target_file = real_run_dir / "must-survive.txt"
    target_file.write_text(controlled["minio_secret"], encoding="utf-8")
    allowed_parent = tmp_path / "allowed-parent-junction"
    subprocess.run(
        [
            "cmd.exe",
            "/d",
            "/c",
            "mklink",
            "/J",
            str(allowed_parent),
            str(real_parent),
        ],
        check=True,
        capture_output=True,
    )

    try:
        caught: Exception | None = None
        try:
            runner.scan_and_quarantine_artifacts(
                allowed_parent / "p1b2-gate4-unit-run",
                list(controlled.values()),
                allowed_parent / "marker.txt",
                allowed_parent=allowed_parent,
                expected_run_id="unit-run",
            )
        except Exception as exc:
            caught = exc
        assert allowed_parent.exists()
        assert real_parent.is_dir()
        assert real_run_dir.is_dir()
        assert target_file.is_file()
        assert caught is not None
        assert "P1B2_GATE4_INVALID_QUARANTINE_SCOPE" in str(caught)
    finally:
        if allowed_parent.exists():
            os.rmdir(allowed_parent)


def test_stage_b_stop_owned_process_tree_verifies_then_uses_exact_taskkill(
    runner,
) -> None:
    record = {
        "pid": 4242,
        "start_time": 100,
        "executable": "python.exe",
        "marker": "p1b2-owned-stage-b",
        "owned_tree_pids": [4242, 4343],
    }
    command_runner = FakeOwnedTreeRunner(identity_matches=True)
    assert (
        runner.stop_owned_process_tree(record, runner=command_runner) is True
    )
    assert command_runner.commands == [
        ["taskkill.exe", "/PID", "4242", "/T", "/F"]
    ]
    assert command_runner.events == [
        "verify-identity",
        (
            "run",
            ["taskkill.exe", "/PID", "4242", "/T", "/F"],
        ),
        ("owned-tree-gone", (4242, 4343)),
    ]

    identity_mismatch = FakeOwnedTreeRunner(identity_matches=False)
    with pytest.raises(runner.Gate4Error):
        runner.stop_owned_process_tree(record, runner=identity_mismatch)
    assert identity_mismatch.events == ["verify-identity"]
    assert identity_mismatch.commands == []


def test_windows_process_active_distinguishes_terminated_retained_handle(
    runner,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows retained process-handle regression")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time;time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        assert runner._process_is_active(process.pid) is True
        completed = subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=30,
        )
        assert completed.returncode == 0
        process.wait(timeout=30)
        assert runner._process_start_time(process.pid) is not None
        assert runner._process_is_active(process.pid) is False
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)


@pytest.mark.parametrize(
    ("role", "port", "executable_path", "arguments"),
    [
        (
            "backend",
            8000,
            r"C:\Program Files\Python 3.11\python.exe",
            ["-m", "p1b2.backend", "--marker", 'quoted "argument"'],
        ),
        (
            "frontend",
            3000,
            r"C:\Program Files\nodejs\node.exe",
            ["--title=p1b2-owned-frontend", r"C:\repo with spaces\vite.js"],
        ),
    ],
)
def test_process_identity_accepts_paths_spaces_quotes_and_live_retained_handle(
    runner,
    role,
    port,
    executable_path,
    arguments,
) -> None:
    marker = f"p1b2-owned-{role}"
    record = runner.make_process_identity_record(
        role=role,
        port=port,
        pid=4242,
        start_time=100,
        executable_path=executable_path,
        arguments=[*arguments, marker],
        marker=marker,
        stdout_log=f"logs/{role}.stdout.log",
        stderr_log=f"logs/{role}.stderr.log",
    )
    observation = {
        "active": True,
        "retained_handle": True,
        "pid": 4242,
        "pid_reused": False,
        "start_time": 100,
        "executable_path": executable_path,
        "command_arguments": [*arguments, marker],
        "port_owner_pid": 4242,
    }

    matches, diagnostic = runner.evaluate_process_identity(
        record, observation
    )

    assert matches is True
    assert diagnostic == {
        "role": role,
        "pid": 4242,
        "expected": True,
        "actual": True,
        "failure_field": None,
    }
    assert set(record) == {
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
    assert executable_path not in json.dumps(record)


def test_windows_command_line_tokenization_preserves_python_node_paths_and_quotes(
    runner,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows command-line ownership regression")
    arguments = [
        r"C:\Program Files\Python 3.11\python.exe",
        r"C:\repo with spaces\runner.py",
        "--ownership-marker",
        "p1b2-owned-backend",
        'quoted "argument"',
        r"C:\Program Files\nodejs\node.exe",
    ]
    command_line = subprocess.list2cmdline(arguments)

    assert runner._windows_command_line_arguments(command_line) == arguments


def _expected_command_arguments_digest(arguments: list[str]) -> tuple[int, str]:
    encoded = json.dumps(
        arguments,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return len(arguments), sha256(encoded).hexdigest()


@pytest.mark.parametrize(
    "observed_arguments",
    [
        [
            r"C:\repo with spaces\run-phase-1b.py",
            "--ownership-marker",
            "p1b2-owned-backend",
        ],
        [
            r"C:\repo with spaces\run-phase-1b.py",
            "--serve-budgeted-backend",
            "--ownership-marker",
            "p1b2-owned-backend",
            "--extra",
        ],
        [
            "--serve-budgeted-backend",
            r"C:\repo with spaces\run-phase-1b.py",
            "--ownership-marker",
            "p1b2-owned-backend",
        ],
        [
            r"C:\other script\run-phase-1b.py",
            "--serve-budgeted-backend",
            "--ownership-marker",
            "p1b2-owned-backend",
        ],
        [
            r"C:\repo with spaces\run-phase-1b.py",
            "--stage-b",
            "--ownership-marker",
            "p1b2-owned-backend",
        ],
        [
            r"C:\repo with spaces\run-phase-1b.py",
            "--serve-budgeted-backend",
            "--different-marker-option",
            "p1b2-owned-backend",
        ],
        [
            r"C:\repo with spaces\run-phase-1b.py",
            "--serve-budgeted-backend",
            "--ownership-marker",
            "p1b2-other-backend",
        ],
    ],
)
def test_process_identity_rejects_any_argument_change_even_with_marker_present(
    runner,
    observed_arguments,
) -> None:
    """Marker presence may not hide a different script, action, or argv."""

    expected_arguments = [
        r"C:\repo with spaces\run-phase-1b.py",
        "--serve-budgeted-backend",
        "--ownership-marker",
        "p1b2-owned-backend",
    ]
    count, digest = _expected_command_arguments_digest(expected_arguments)
    record = runner.make_process_identity_record(
        role="backend",
        port=8000,
        pid=4242,
        start_time=100,
        executable_path=r"C:\Program Files\Python 3.11\python.exe",
        arguments=expected_arguments,
        marker="p1b2-owned-backend",
        stdout_log="logs/backend.stdout.log",
        stderr_log="logs/backend.stderr.log",
    )
    assert record["command_argument_count"] == count
    assert record["command_arguments_sha256"] == digest
    observation = {
        "active": True,
        "pid": 4242,
        "pid_reused": False,
        "start_time": 100,
        "executable_path": r"C:\Program Files\Python 3.11\python.exe",
        "command_arguments": observed_arguments,
        "port_owner_pid": 4242,
    }

    matches, diagnostic = runner.evaluate_process_identity(
        record,
        observation,
    )

    assert matches is False
    assert diagnostic["failure_field"] == "command_arguments"


@pytest.mark.parametrize(
    ("change", "failure_field"),
    [
        ({"active": False}, "active"),
        ({"pid_reused": True}, "pid_reuse"),
        ({"start_time": 101}, "start_time"),
        (
            {"executable_path": r"C:\Other Python\python.exe"},
            "executable",
        ),
        ({"command_arguments": ["not-the-marker"]}, "command_arguments"),
        ({"port_owner_pid": 9999}, "port_owner"),
    ],
)
def test_process_identity_rejects_exit_reuse_and_all_identity_mismatches(
    runner,
    change,
    failure_field,
) -> None:
    record = runner.make_process_identity_record(
        role="backend",
        port=8000,
        pid=4242,
        start_time=100,
        executable_path=r"C:\Program Files\Python 3.11\python.exe",
        arguments=[
            "-m",
            "p1b2.backend",
            "p1b2-owned-backend",
        ],
        marker="p1b2-owned-backend",
        stdout_log="logs/backend.stdout.log",
        stderr_log="logs/backend.stderr.log",
    )
    observation = {
        "active": True,
        "retained_handle": False,
        "pid": 4242,
        "pid_reused": False,
        "start_time": 100,
        "executable_path": r"C:\Program Files\Python 3.11\python.exe",
        "command_arguments": [
            "-m",
            "p1b2.backend",
            "p1b2-owned-backend",
        ],
        "port_owner_pid": 4242,
    }
    observation.update(change)

    matches, diagnostic = runner.evaluate_process_identity(
        record, observation
    )

    assert matches is False
    assert diagnostic == {
        "role": "backend",
        "pid": 4242,
        "expected": True,
        "actual": False,
        "failure_field": failure_field,
    }
    assert set(diagnostic) == {
        "role",
        "pid",
        "expected",
        "actual",
        "failure_field",
    }
    diagnostic_text = json.dumps(diagnostic, sort_keys=True)
    assert "Program Files" not in diagnostic_text
    assert "p1b2-owned-backend" not in diagnostic_text


def test_process_role_port_pair_is_constrained_by_record_schema(runner) -> None:
    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_INVALID_STATE",
    ):
        runner.make_process_identity_record(
            role="backend",
            port=3000,
            pid=4242,
            start_time=100,
            executable_path=r"C:\Program Files\Python 3.11\python.exe",
            arguments=[
                r"C:\repo with spaces\run-phase-1b.py",
                "--serve-budgeted-backend",
                "--ownership-marker",
                "p1b2-owned-backend",
            ],
            marker="p1b2-owned-backend",
            stdout_log="logs/backend.stdout.log",
            stderr_log="logs/backend.stderr.log",
        )


def test_windows_owned_loopback_process_uses_exact_argv_and_port_owner(
    runner,
    tmp_path,
) -> None:
    """Exercise the real Windows process/argv/port ownership path offline."""

    if os.name != "nt":
        pytest.skip("Windows local process ownership regression")
    port = next(
        (
            candidate
            for candidate in (8000, 3000)
            if runner._port_owner_pid(candidate) is None
        ),
        None,
    )
    if port is None:
        pytest.skip("approved local ownership test ports are occupied")

    script_root = tmp_path / "loopback process with spaces"
    script_root.mkdir()
    script_path = script_root / "listener script.py"
    script_path.write_text(
        "import socket, sys, time\n"
        "port = int(sys.argv[sys.argv.index('--port') + 1])\n"
        "sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
        "sock.bind(('127.0.0.1', port))\n"
        "sock.listen(1)\n"
        "try:\n"
        "    time.sleep(60)\n"
        "finally:\n"
        "    sock.close()\n",
        encoding="utf-8",
    )
    run_root = tmp_path / "owned process evidence"
    marker = "p1b2-owned-backend"
    command = [
        sys.executable,
        os.fspath(script_path),
        "--ownership-marker",
        marker,
        "--port",
        str(port),
        "--quoted",
        'value "with quote"',
    ]
    process = None
    record = None
    try:
        process, record = runner._start_owned_process(
            role="backend" if port == 8000 else "frontend",
            port=port,
            command=command,
            environment=runner._system_child_environment(os.environ),
            cwd=tmp_path,
            run_root=run_root,
            marker=marker,
            stdout_name="loopback.stdout.log",
            stderr_name="loopback.stderr.log",
            controlled_values=(),
        )
        runner._wait_port(port, listening=True, timeout=15.0)

        assert runner.verify_owned_process_port(record) is True
        count, digest = runner._command_arguments_digest(command[1:])
        assert record["command_argument_count"] == count
        assert record["command_arguments_sha256"] == digest
        assert runner._port_owner_pid(port) == process.pid
    finally:
        if record is not None and process is not None and process.poll() is None:
            runner.stop_owned_process_tree(record)
            process.wait(timeout=30)
        elif process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=30)
        runner._wait_port(port, listening=False, timeout=15.0)

    assert process is not None
    assert process.poll() is not None
    assert runner._port_owner_pid(port) is None


def test_windows_process_active_fails_closed_on_open_process_query_errors(
    runner,
) -> None:
    if os.name != "nt":
        pytest.skip("Windows process-query regression")

    class OpenProcessFailure:
        def __init__(self, error_code: int) -> None:
            self.error_code = error_code

        def OpenProcess(self, *_args) -> int:
            return 0

        def GetLastError(self) -> int:
            return self.error_code

    assert (
        runner._process_is_active(
            4242, kernel32=OpenProcessFailure(87)
        )
        is False
    )
    assert (
        runner._process_is_active(
            4242, kernel32=OpenProcessFailure(5)
        )
        is True
    )


def test_stage_b_finalizer_runs_all_checks_and_cleanup_failure_wins(
    runner,
) -> None:
    calls: list[str] = []
    ledger = runner.BudgetLedger("stage-b-finalizer")
    ledger.delegate_runtime(lambda: None)
    ledger.delegate_ddpm(lambda: None)
    before = ledger.safe_snapshot()

    def stop_owned() -> None:
        calls.append("stop")
        raise RuntimeError("controlled cleanup failure")

    def check_port_gpu() -> bool:
        calls.append("port-gpu")
        return True

    def verify_dotenv() -> bool:
        calls.append("dotenv")
        return True

    def scan_artifacts() -> bool:
        calls.append("artifact-scan")
        return True

    with pytest.raises(
        runner.Gate4Error, match="P1B2_GATE4_CLEANUP_INCOMPLETE"
    ):
        runner.finalize_stage_b(
            stop_owned=stop_owned,
            check_port_gpu=check_port_gpu,
            verify_dotenv=verify_dotenv,
            scan_artifacts=scan_artifacts,
            ledger=ledger,
        )
    assert calls == ["stop", "port-gpu", "dotenv", "artifact-scan"]
    assert ledger.safe_snapshot() == before
    assert before["runtime_delegates"] == 1
    assert before["ddpm_delegates"] == 1


def test_stage_b_memory_preflight_accepts_exact_binary_5_5_gib(
    runner,
) -> None:
    result = runner.evaluate_stage_b_memory_preflight(5_905_580_032)
    assert result == {"free_bytes": 5_905_580_032}


def test_stage_b_memory_preflight_rejects_one_byte_below_5_5_gib(
    runner,
) -> None:
    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_STAGE_B_MEMORY_PREFLIGHT_FAILED",
    ):
        runner.evaluate_stage_b_memory_preflight(5_905_580_031)


def test_final_stage_b_pre_start_gate_blocks_process_and_preserves_budgets(
    runner,
) -> None:
    started: list[str] = []
    ledger = runner.BudgetLedger("stage-b-final-preflight-unit")
    before = ledger.safe_snapshot()

    def start_runtime() -> None:
        started.append("runtime-started")
        ledger.delegate_runtime(lambda: None)
        ledger.delegate_ddpm(lambda: None)

    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_STAGE_B_MEMORY_PREFLIGHT_FAILED",
    ):
        runner.start_stage_b_runtime_after_memory_gate(
            before_runtime_start_free_bytes=5_905_580_031,
            start_runtime=start_runtime,
        )
    assert started == []
    assert ledger.safe_snapshot() == before


def test_stage_b_runtime_uses_confirmed_model_bundle_root(runner) -> None:
    assert runner.stage_b_model_root(REPO_ROOT) == (
        REPO_ROOT / "SEM" / "ZTA35G_lab"
    )


def test_stable_free_memory_measurement_waits_then_uses_three_sample_median(
    runner,
) -> None:
    sleeps: list[float] = []
    reader = FakeSequence(
        [
            {"available_bytes": 6_400_000_000},
            {"available_bytes": 6_000_000_000},
            {"available_bytes": 6_200_000_000},
        ]
    )
    measured = runner.measure_stable_free_memory_bytes(
        host_reader=reader,
        settle_seconds=2.0,
        sample_interval_seconds=0.25,
        sleep=sleeps.append,
    )
    assert measured == 6_200_000_000
    assert sleeps == [2.0, 0.25, 0.25]


def test_host_memory_sampler_records_2_gib_warning_without_stopping(
    runner,
) -> None:
    normal = 4 * 1024**3
    warning = (2 * 1024**3) - 1
    sampler = runner.ResourceSampler(
        host_reader=FakeSequence(
            [
                {"available_bytes": normal},
                {"available_bytes": warning},
            ]
        ),
        gpu_reader=FakeSequence(
            [
                {"used_mib": 2000, "free_mib": 4200, "compute_pids": [4242]},
                {"used_mib": 2100, "free_mib": 4100, "compute_pids": [4242]},
            ]
        ),
        owned_compute_pids={4242},
        interval_seconds=60.0,
    )
    sampler.start()
    report = sampler.stop()
    assert report["host_memory_low_warning"] is True
    assert report["host_memory_low_warning_marker"] == "HOST_MEMORY_LOW_WARNING"
    assert isinstance(
        report["host_memory_low_warning_first_at_utc"], str
    )
    assert report["minimum_host_available_bytes"] == warning
    assert report["host_memory_critical_low"] is False
    assert report["host_memory_low_warning_duration_seconds"] >= 0


def test_host_memory_sampler_defers_1_5_gib_critical_until_call_returns(
    runner,
) -> None:
    normal = 4 * 1024**3
    one_point_five_gib = 1_610_612_736
    critical = one_point_five_gib - 1
    sampler = runner.ResourceSampler(
        host_reader=FakeSequence(
            [
                {"available_bytes": normal},
                {"available_bytes": normal},
                {"available_bytes": critical},
                {"available_bytes": critical},
            ]
        ),
        gpu_reader=FakeSequence(
            [
                {"used_mib": 2000, "free_mib": 4200, "compute_pids": [4242]},
                {"used_mib": 2100, "free_mib": 4100, "compute_pids": [4242]},
                {"used_mib": 2200, "free_mib": 4000, "compute_pids": [4242]},
                {"used_mib": 2200, "free_mib": 4000, "compute_pids": [4242]},
            ]
        ),
        owned_compute_pids={4242},
        interval_seconds=60.0,
    )
    sampler.start()
    assert sampler.begin_execute() == normal
    metrics = sampler.finish_execute()
    assert metrics["stage_b_post_execute_free_bytes"] == critical
    with pytest.raises(
        runner.Gate4Error, match="HOST_MEMORY_CRITICAL_LOW"
    ):
        sampler.raise_if_critical()
    report = sampler.stop()
    assert report["host_memory_critical_low"] is True
    assert report["minimum_host_available_bytes"] == critical
    assert one_point_five_gib == 1_610_612_736


def test_host_memory_sampler_rejects_call_start_below_1_5_gib(
    runner,
) -> None:
    critical = 1_610_612_735
    sampler = runner.ResourceSampler(
        host_reader=FakeSequence(
            [
                {"available_bytes": critical},
                {"available_bytes": critical},
                {"available_bytes": critical},
            ]
        ),
        gpu_reader=FakeSequence(
            [
                {"used_mib": 2000, "free_mib": 4200, "compute_pids": [4242]},
                {"used_mib": 2000, "free_mib": 4200, "compute_pids": [4242]},
                {"used_mib": 2000, "free_mib": 4200, "compute_pids": [4242]},
            ]
        ),
        owned_compute_pids={4242},
        interval_seconds=60.0,
    )
    sampler.start()
    with pytest.raises(
        runner.Gate4Error, match="HOST_MEMORY_CRITICAL_LOW"
    ):
        sampler.begin_execute()
    sampler.stop()


def test_host_memory_sampler_holds_lock_through_final_start_check(
    runner,
) -> None:
    normal = 4 * 1024**3
    sampler = runner.ResourceSampler(
        host_reader=FakeSequence(
            [{"available_bytes": normal} for _index in range(6)]
        ),
        gpu_reader=FakeSequence(
            [
                {
                    "used_mib": 2000,
                    "free_mib": 4200,
                    "compute_pids": [4242],
                }
                for _index in range(6)
            ]
        ),
        owned_compute_pids={4242},
        interval_seconds=60.0,
    )
    sampler.start()

    class CriticalCheckProbe:
        def __init__(self) -> None:
            self.lock_observations: list[bool] = []

        def __bool__(self) -> bool:
            self.lock_observations.append(sampler._sample_lock.locked())
            return False

    probe = CriticalCheckProbe()
    sampler._critical_host_memory = probe
    assert sampler.begin_execute() == normal
    assert probe.lock_observations == [True]
    sampler._critical_host_memory = False
    sampler.finish_execute()
    sampler.stop()


def test_host_memory_sampler_starts_delegate_inside_final_preflight_lock(
    runner,
) -> None:
    normal = 4 * 1024**3
    sampler = runner.ResourceSampler(
        host_reader=FakeSequence(
            [{"available_bytes": normal} for _index in range(6)]
        ),
        gpu_reader=FakeSequence(
            [
                {
                    "used_mib": 2000,
                    "free_mib": 4200,
                    "compute_pids": [4242],
                }
                for _index in range(6)
            ]
        ),
        owned_compute_pids={4242},
        interval_seconds=60.0,
    )
    sampler.start()

    class DelegateStartProbe:
        def __init__(self) -> None:
            self.started = False
            self.lock_was_held = False

        def start(self) -> None:
            self.lock_was_held = sampler._sample_lock.locked()
            self.started = True

    delegate = DelegateStartProbe()
    assert sampler.start_execute_thread(delegate) == normal
    assert delegate.started is True
    assert delegate.lock_was_held is True
    sampler.finish_execute()
    sampler.stop()


def test_host_memory_sampler_uses_three_sample_median_before_delegate(
    runner,
) -> None:
    samples = [
        7_000_000_000,
        6_400_000_000,
        6_000_000_000,
        6_200_000_000,
        5_900_000_000,
        5_800_000_000,
    ]
    sampler = runner.ResourceSampler(
        host_reader=FakeSequence(
            [{"available_bytes": value} for value in samples]
        ),
        gpu_reader=FakeSequence(
            [
                {
                    "used_mib": 2000,
                    "free_mib": 4200,
                    "compute_pids": [4242],
                }
                for _value in samples
            ]
        ),
        owned_compute_pids={4242},
        interval_seconds=60.0,
    )
    sampler.start()

    class Delegate:
        def start(self) -> None:
            return None

    assert (
        sampler.start_execute_thread(
            Delegate(), sample_interval_seconds=0
        )
        == 6_200_000_000
    )
    sampler.finish_execute()
    report = sampler.stop()
    assert report["stage_b_pre_execute_free_bytes"] == 6_200_000_000


def test_resource_sampler_records_separate_runtime_load_and_execute_increments(
    runner,
) -> None:
    samples = [
        7_000_000_000,
        6_500_000_000,
        6_100_000_000,
        6_300_000_000,
        6_200_000_000,
        6_000_000_000,
        6_100_000_000,
        4_500_000_000,
        5_000_000_000,
    ]
    sampler = runner.ResourceSampler(
        host_reader=FakeSequence(
            [{"available_bytes": value} for value in samples]
        ),
        gpu_reader=FakeSequence(
            [
                {
                    "used_mib": 2000,
                    "free_mib": 4200,
                    "compute_pids": [4242],
                }
                for _value in samples
            ]
        ),
        owned_compute_pids={4242},
        before_runtime_start_free_bytes=7_000_000_000,
        interval_seconds=60.0,
    )
    sampler.start()
    assert (
        sampler.capture_after_runtime_ready(
            settle_seconds=0,
            sample_interval_seconds=0,
        )
        == 6_300_000_000
    )

    class Delegate:
        def start(self) -> None:
            return None

    sampler.start_execute_thread(
        Delegate(), sample_interval_seconds=0
    )
    sampler.finish_execute()
    report = sampler.stop()
    assert report["stage_b_before_runtime_start_free_bytes"] == 7_000_000_000
    assert report["stage_b_after_runtime_ready_free_bytes"] == 6_300_000_000
    assert report["stage_b_model_load_drop_bytes"] == 700_000_000
    assert report["stage_b_execute_drop_bytes"] == 1_600_000_000
    assert report["stage_b_total_drop_bytes"] == 2_500_000_000


def test_stage_b_memory_metrics_separate_model_load_execute_and_total_drop(
    runner,
) -> None:
    metrics = runner.stage_b_memory_metrics(
        before_runtime_start_free_bytes=8_000_000_000,
        after_runtime_ready_free_bytes=6_500_000_000,
        pre_execute_free_bytes=6_400_000_000,
        minimum_during_execute_free_bytes=4_000_000_000,
        post_execute_free_bytes=5_000_000_000,
    )
    assert metrics == {
        "stage_b_before_runtime_start_free_bytes": 8_000_000_000,
        "stage_b_after_runtime_ready_free_bytes": 6_500_000_000,
        "stage_b_pre_execute_free_bytes": 6_400_000_000,
        "stage_b_minimum_during_execute_free_bytes": 4_000_000_000,
        "stage_b_post_execute_free_bytes": 5_000_000_000,
        "stage_b_model_load_drop_bytes": 1_500_000_000,
        "stage_b_execute_drop_bytes": 2_400_000_000,
        "stage_b_total_drop_bytes": 4_000_000_000,
    }


def test_stage_b_critical_failure_persists_exact_memory_evidence(
    runner, tmp_path
) -> None:
    run_root = tmp_path / "p1b2-gate4"
    run_root.mkdir()
    resources = {
        "minimum_host_available_bytes": 1_500_000_000,
        "max_gpu_used_mib": 3100,
        "min_gpu_free_mib": 3000,
        "host_memory_low_warning": True,
        "host_memory_low_warning_marker": "HOST_MEMORY_LOW_WARNING",
        "host_memory_low_warning_first_at_utc": (
            "2026-07-31T00:00:00+00:00"
        ),
        "host_memory_critical_low": True,
        "host_memory_low_warning_duration_seconds": 1.25,
        "stage_b_before_runtime_start_free_bytes": 7_500_000_000,
        "stage_b_after_runtime_ready_free_bytes": 5_500_000_000,
        "stage_b_pre_execute_free_bytes": 5_400_000_000,
        "stage_b_minimum_during_execute_free_bytes": 1_500_000_000,
        "stage_b_post_execute_free_bytes": 2_000_000_000,
        "stage_b_model_load_drop_bytes": 2_000_000_000,
        "stage_b_execute_drop_bytes": 3_900_000_000,
        "stage_b_total_drop_bytes": 6_000_000_000,
    }
    record_path = runner.write_stage_b_critical_memory_record(
        run_root=run_root,
        run_id="unit-critical-memory",
        stage_b_preflight={
            "free_bytes": 7_500_000_000,
        },
        resources=resources,
        budgets={
            "run_id": "unit-critical-memory",
            "runtime_delegates": 1,
            "ddpm_delegates": 1,
        },
        controlled_values=["must-not-appear"],
    )
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    assert payload == {
        "marker": "HOST_MEMORY_CRITICAL_LOW",
        "run_id": "unit-critical-memory",
        "stage_b_preflight": {
            "free_bytes": 7_500_000_000,
        },
        "resources": resources,
        "budgets": {
            "run_id": "unit-critical-memory",
            "runtime_delegates": 1,
            "ddpm_delegates": 1,
        },
    }
    assert "must-not-appear" not in record_path.read_text(encoding="utf-8")


def _stage_b_memory_evidence(
    *,
    model_load_drop_bytes: int = 2 * 1024**3,
    execute_drop_bytes: int = 1 * 1024**3,
    total_drop_bytes: int = 7 * 1024**3,
) -> dict[str, int]:
    return {
        "stage_b_before_runtime_start_free_bytes": 12 * 1024**3,
        "stage_b_after_runtime_ready_free_bytes": (
            (12 * 1024**3) - model_load_drop_bytes
        ),
        "stage_b_pre_execute_free_bytes": 5 * 1024**3,
        "stage_b_minimum_during_execute_free_bytes": (
            (5 * 1024**3) - execute_drop_bytes
        ),
        "stage_b_post_execute_free_bytes": 4 * 1024**3,
        "stage_b_model_load_drop_bytes": model_load_drop_bytes,
        "stage_b_execute_drop_bytes": execute_drop_bytes,
        "stage_b_total_drop_bytes": total_drop_bytes,
    }


def test_stage_c_runtime_start_gate_uses_maximum_of_4_5_gib_and_load_plus_1_5(
    runner,
) -> None:
    assert (
        runner.required_stage_c_runtime_start_free_bytes(2 * 1024**3)
        == 4_831_838_208
    )
    assert (
        runner.required_stage_c_runtime_start_free_bytes(4 * 1024**3)
        == 5_905_580_032
    )
    result = runner.evaluate_stage_c_runtime_start_memory_preflight(
        stage_b_memory=_stage_b_memory_evidence(
            model_load_drop_bytes=4 * 1024**3
        ),
        stage_c_before_runtime_start_free_bytes=5_905_580_032,
    )
    assert result == {
        "stage_b_model_load_drop_bytes": 4_294_967_296,
        "stage_c_before_runtime_start_free_bytes": 5_905_580_032,
        "required_stage_c_runtime_start_free_bytes": 5_905_580_032,
    }


def test_stage_c_runtime_start_gate_failure_never_opens_weights(
    runner,
) -> None:
    opened: list[str] = []
    ledger = runner.BudgetLedger("stage-c-runtime-start-unit")
    before = ledger.safe_snapshot()

    def start_runtime() -> None:
        opened.append("weights-opened")
        ledger.delegate_runtime(lambda: None)

    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_STAGE_C_RUNTIME_START_MEMORY_FAILED",
    ):
        runner.start_stage_c_runtime_after_memory_gate(
            stage_b_memory=_stage_b_memory_evidence(
                model_load_drop_bytes=4 * 1024**3
            ),
            stage_c_before_runtime_start_free_bytes=5_905_580_031,
            start_runtime=start_runtime,
        )
    assert opened == []
    assert ledger.safe_snapshot() == before


def test_stage_c_tool_retry_gate_uses_execute_drop_not_total_drop(
    runner,
) -> None:
    assert (
        runner.required_stage_c_tool_retry_free_bytes(1 * 1024**3)
        == 3_758_096_384
    )
    assert (
        runner.required_stage_c_tool_retry_free_bytes(3 * 1024**3)
        == 4_831_838_208
    )
    result = runner.evaluate_stage_c_tool_retry_memory_preflight(
        stage_b_memory=_stage_b_memory_evidence(
            execute_drop_bytes=1 * 1024**3,
            total_drop_bytes=9 * 1024**3,
        ),
        stage_c_pre_tool_retry_free_bytes=3_758_096_384,
    )
    assert result == {
        "marker": "P1B2_GATE4_TOOL_RETRY_RESOURCE_READY",
        "stage_b_execute_drop_bytes": 1_073_741_824,
        "stage_c_pre_tool_retry_free_bytes": 3_758_096_384,
        "required_stage_c_tool_retry_free_bytes": 3_758_096_384,
    }


def test_stage_c_tool_retry_failure_keeps_runtime_and_provider_budgets_unchanged(
    runner,
    tmp_path,
) -> None:
    ledger = runner.BudgetLedger("stage-c-unit")
    ledger.delegate_runtime(lambda: None)
    ledger.delegate_ddpm(lambda: None)
    provider_path = tmp_path / "provider-ledger.json"
    runner.ProviderDelegateLedger.initialize(
        provider_path, run_id="stage-c-unit"
    )
    provider = runner.ProviderDelegateLedger(
        provider_path, run_id="stage-c-unit"
    )
    facts = {"CHAT": 0, "EXPLANATION": 0}
    for _index in range(3):
        facts["CHAT"] += 1
        provider.delegate(
            "CHAT",
            lambda: None,
            fact_reader=lambda: dict(facts),
        )
    runtime_before = ledger.safe_snapshot()
    provider_before = provider.read()
    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_STAGE_C_TOOL_RETRY_MEMORY_FAILED",
    ):
        runner.evaluate_stage_c_tool_retry_memory_preflight(
            stage_b_memory=_stage_b_memory_evidence(
                execute_drop_bytes=3 * 1024**3
            ),
            stage_c_pre_tool_retry_free_bytes=4_831_838_207,
        )
    assert ledger.safe_snapshot() == runtime_before
    assert provider.read() == provider_before


def test_stage_c_live_stack_readings_drive_both_gates_without_extra_estimate(
    runner,
) -> None:
    stage_b_memory = _stage_b_memory_evidence(
        model_load_drop_bytes=0,
        execute_drop_bytes=0,
    )
    runtime_gate = runner.evaluate_stage_c_runtime_start_memory_preflight(
        stage_b_memory=stage_b_memory,
        stage_c_before_runtime_start_free_bytes=4_831_838_208,
    )
    retry_gate = runner.evaluate_stage_c_tool_retry_memory_preflight(
        stage_b_memory=stage_b_memory,
        stage_c_pre_tool_retry_free_bytes=3_758_096_384,
    )
    assert runtime_gate[
        "stage_c_before_runtime_start_free_bytes"
    ] == 4_831_838_208
    assert retry_gate[
        "stage_c_pre_tool_retry_free_bytes"
    ] == 3_758_096_384


def test_stage_b_resource_sampler_reports_extrema_and_rejects_foreign_gpu_pid(
    runner,
) -> None:
    sampler = runner.ResourceSampler(
        host_reader=FakeSequence(
            [
                {"available_bytes": 32000 * 1024**2},
                {"available_bytes": 28000 * 1024**2},
            ]
        ),
        gpu_reader=FakeSequence(
            [
                {"used_mib": 2000, "free_mib": 22000, "compute_pids": [4242]},
                {"used_mib": 5000, "free_mib": 19000, "compute_pids": [4242]},
            ]
        ),
        owned_compute_pids={4242},
    )
    sampler.start()
    report = sampler.stop()
    assert report["minimum_host_available_bytes"] == 28000 * 1024**2
    assert report["max_gpu_used_mib"] == 5000
    assert report["min_gpu_free_mib"] == 19000

    foreign = runner.ResourceSampler(
        host_reader=FakeSequence(
            [
                {"available_bytes": 32000 * 1024**2},
                {"available_bytes": 31000 * 1024**2},
            ]
        ),
        gpu_reader=FakeSequence(
            [
                {"used_mib": 2000, "free_mib": 22000, "compute_pids": [4242]},
                {
                    "used_mib": 3000,
                    "free_mib": 21000,
                    "compute_pids": [4242, 9999],
                },
            ]
        ),
        owned_compute_pids={4242},
    )
    foreign.start()
    with pytest.raises(
        runner.Gate4Error, match="P1B2_GATE4_UNEXPECTED_GPU_PROCESS"
    ):
        foreign.stop()


def _phase_1_stage_b_state() -> dict[str, object]:
    return {
        "run_id": "phase1-unit-run",
        "phase": "STAGEB_COMPLETE",
        "budgets": {
            "runtime": 1,
            "ddpm": 1,
        },
    }


def _phase_1_starting_safe_state(runner) -> dict[str, object]:
    state = runner.new_safe_state(
        run_id="phase1-unit-run",
        dotenv_sha256="a" * 64,
    )
    state["phase"] = "PHASE1_STARTING"
    state["budgets"] = {
        "runtime": 1,
        "ddpm": 1,
    }
    state["resources"] = {
        "database": "phase1-unit-database",
        "bucket": "phase1-unit-bucket",
    }
    runner.validate_safe_state(state)
    return state


def test_phase_1_start_rollback_deletes_exact_resources_and_invalidates_run(
    runner,
) -> None:
    state = _phase_1_starting_safe_state(runner)
    deleted: list[tuple[str, str]] = []

    assert runner.rollback_phase1_start_resources(
        state=state,
        expected_database="phase1-unit-database",
        expected_bucket="phase1-unit-bucket",
        delete_database=lambda name: deleted.append(("database", name)),
        delete_bucket=lambda name: deleted.append(("bucket", name)),
    )
    assert deleted == [
        ("bucket", "phase1-unit-bucket"),
        ("database", "phase1-unit-database"),
    ]
    assert state["phase"] == "INVALIDATED"
    assert state["resources"] == {}
    assert state["processes"] == {}
    assert all(
        record == {
            "owned": False,
            "preexisting": False,
            "container_id": None,
        }
        for record in state["docker"].values()
    )


def test_phase_1_start_rollback_refuses_unexpected_resource_names(runner) -> None:
    state = _phase_1_starting_safe_state(runner)
    state["resources"]["bucket"] = "not-the-owned-bucket"
    deleted: list[str] = []

    assert not runner.rollback_phase1_start_resources(
        state=state,
        expected_database="phase1-unit-database",
        expected_bucket="phase1-unit-bucket",
        delete_database=deleted.append,
        delete_bucket=deleted.append,
    )
    assert deleted == []
    assert state["phase"] == "PHASE1_STARTING"


def test_phase_1_coordinator_resumes_stage_b_and_builds_one_safe_pause(
    runner, controlled
) -> None:
    state = _phase_1_stage_b_state()
    stores = FakePhase1Stores()
    processes = FakePhase1Processes()
    writer = FakeSafeStateWriter(processes.events)
    output = FakeEventWriter(processes.events)
    coordinator = runner.Phase1Coordinator(
        state=state,
        stores=stores,
        processes=processes,
        state_writer=writer,
        controlled=controlled,
    )

    pause = coordinator.prepare_browser_pause(output=output)
    assert pause["run_id"] == "phase1-unit-run"
    assert pause["resources"] == {
        "database": "phase1-unit-database",
        "bucket": "phase1-unit-bucket",
        "actor": "phase1-unit-actor",
        "conversation": "phase1-unit-conversation",
    }
    assert stores.created == ["database", "bucket", "actor", "conversation"]
    assert stores.deleted == []
    assert len(writer.payloads) == 1
    assert not any(
        value in writer.payloads[0] for value in controlled.values()
    )

    started = {
        handle["role"]: handle["environment"]
        for handle in processes.handles
        if handle["owned"]
    }
    assert set(started) == {
        "compose",
        "postgres",
        "minio",
        "backend-real",
        "frontend",
    }
    assert "runtime" not in started
    backend = started["backend-real"]
    assert backend["DEEPSEEK_API_KEY"] == controlled["real_provider_key"]
    assert backend["ZTA35G_RUNTIME_TOKEN"] == controlled["runtime_token"]
    assert backend["ZTA35G_RUNTIME_URL"] == "http://127.0.0.1:8100"
    assert backend["ZTA35G_RUNTIME_TIMEOUT_SECONDS"] == "900"
    provider_holders = [
        role
        for role, environment in started.items()
        if controlled["real_provider_key"] in environment.values()
    ]
    assert provider_holders == ["backend-real"]
    for role in ("frontend", "compose", "postgres", "minio"):
        assert "DEEPSEEK_API_KEY" not in started[role]

    assert output.getvalue().splitlines() == [
        "P1B2_GATE4_BROWSER_ACCEPTANCE_READY"
    ]
    output_index = next(
        index
        for index, event in enumerate(processes.events)
        if isinstance(event, tuple) and event[0] == "output"
    )
    assert processes.events[:output_index][-3:] == [
        ("port", 8000, True),
        ("port", 3000, True),
        ("port", 8100, False),
    ]
    assert coordinator.run_id == "phase1-unit-run"
    assert coordinator.budgets == {
        "runtime": 1,
        "ddpm": 1,
    }


def test_phase_1_double_start_is_rejected_without_duplicate_resources(
    runner, controlled
) -> None:
    stores = FakePhase1Stores()
    processes = FakePhase1Processes()
    coordinator = runner.Phase1Coordinator(
        state=_phase_1_stage_b_state(),
        stores=stores,
        processes=processes,
        state_writer=FakeSafeStateWriter(processes.events),
        controlled=controlled,
    )
    coordinator.prepare_browser_pause(output=io.StringIO())
    with pytest.raises(
        runner.Gate4Error, match="P1B2_GATE4_PHASE1_ALREADY_STARTED"
    ):
        coordinator.prepare_browser_pause(output=io.StringIO())
    assert stores.created == ["database", "bucket", "actor", "conversation"]
    assert len([event for event in processes.events if event[0] == "start"]) == 5


def test_phase_1_failed_start_stops_only_owned_and_removes_temp_stores(
    runner, controlled
) -> None:
    stores = FakePhase1Stores()
    processes = FakePhase1Processes(fail_role="frontend")
    foreign = {
        "role": "foreign",
        "environment": {},
        "owned": False,
        "alive": True,
    }
    processes.handles.append(foreign)
    writer = FakeSafeStateWriter(processes.events)
    coordinator = runner.Phase1Coordinator(
        state=_phase_1_stage_b_state(),
        stores=stores,
        processes=processes,
        state_writer=writer,
        controlled=controlled,
    )
    with pytest.raises(runner.Gate4Error):
        coordinator.prepare_browser_pause(output=io.StringIO())
    assert foreign["alive"] is True
    assert all(
        not handle["alive"]
        for handle in processes.handles
        if handle["owned"]
    )
    assert stores.created == ["database", "bucket", "actor", "conversation"]
    assert stores.deleted == ["bucket", "database"]
    assert writer.payloads
    invalidated = json.loads(writer.payloads[-1])
    assert invalidated["phase"] == "INVALIDATED"
    assert invalidated["run_id"] == "phase1-unit-run"


@pytest.mark.parametrize(
    "failure_role",
    ["backend-real", "frontend", "ownership"],
)
def test_phase_1_atomic_start_failure_cleans_resources_ports_and_environment(
    runner,
    controlled,
    failure_role,
) -> None:
    stores = FakePhase1Stores()
    processes = FakePhase1Processes(
        fail_role=(
            failure_role
            if failure_role in {"backend-real", "frontend"}
            else None
        ),
        ports=(
            {8000: False, 3000: True, 8100: False}
            if failure_role == "ownership"
            else None
        ),
    )
    original_environment = dict(os.environ)
    writer = FakeSafeStateWriter(processes.events)
    coordinator = runner.Phase1Coordinator(
        state=_phase_1_stage_b_state(),
        stores=stores,
        processes=processes,
        state_writer=writer,
        controlled=controlled,
    )

    with pytest.raises(runner.Gate4Error):
        coordinator.prepare_browser_pause(output=io.StringIO())

    assert stores.deleted == ["bucket", "database"]
    assert all(
        not handle["alive"]
        for handle in processes.handles
        if handle["owned"]
    )
    assert os.environ == original_environment
    assert writer.payloads
    assert json.loads(writer.payloads[-1])["phase"] == "INVALIDATED"
    assert all(
        event != ("output", "P1B2_GATE4_BROWSER_ACCEPTANCE_READY")
        for event in processes.events
    )


def test_invalidated_phase_1_run_cannot_be_resumed_and_retry_uses_new_run_id(
    runner,
    controlled,
) -> None:
    stores = FakePhase1Stores()
    processes = FakePhase1Processes(fail_role="backend-real")
    writer = FakeSafeStateWriter(processes.events)
    coordinator = runner.Phase1Coordinator(
        state=_phase_1_stage_b_state(),
        stores=stores,
        processes=processes,
        state_writer=writer,
        controlled=controlled,
    )
    with pytest.raises(runner.Gate4Error):
        coordinator.prepare_browser_pause(output=io.StringIO())
    invalidated = json.loads(writer.payloads[-1])

    with pytest.raises(
        runner.Gate4Error, match="P1B2_GATE4_PHASE1_INVALID_RESUME"
    ):
        runner.Phase1Coordinator(
            state=invalidated,
            stores=FakePhase1Stores(),
            processes=FakePhase1Processes(),
            state_writer=FakeSafeStateWriter([]),
            controlled=controlled,
        )

    assert runner.next_fresh_stage_c_run_id(
        previous_run_id=invalidated["run_id"],
        factory=lambda: "phase1-fresh-run",
    ) == "phase1-fresh-run"


def test_phase_1_browser_marker_requires_runtime_stopped_and_ui_ports_ready(
    runner, controlled
) -> None:
    processes = FakePhase1Processes(
        ports={8000: True, 3000: True, 8100: True}
    )
    output = io.StringIO()
    coordinator = runner.Phase1Coordinator(
        state=_phase_1_stage_b_state(),
        stores=FakePhase1Stores(),
        processes=processes,
        state_writer=FakeSafeStateWriter(processes.events),
        controlled=controlled,
    )
    with pytest.raises(runner.Gate4Error):
        coordinator.prepare_browser_pause(output=output)
    assert output.getvalue() == ""
    assert "runtime" not in [
        handle["role"] for handle in processes.handles
    ]


def test_phase_1_backend_adapter_canary_uses_urllib3_timeout_and_no_retries(
    runner,
) -> None:
    adapter = FakeBackendAdapter()
    assert runner.audit_backend_adapter(adapter) is True
    assert len(adapter._pool.requests) == 1
    captured = adapter._pool.requests[0]
    assert isinstance(adapter._timeout, urllib3.Timeout)
    assert captured["timeout"] is adapter._timeout
    assert captured["timeout"].total == 900
    assert captured["retries"] is False

    with pytest.raises(runner.Gate4Error):
        runner.audit_backend_adapter(FakeBackendAdapter(total=899))
    with pytest.raises(runner.Gate4Error):
        runner.audit_backend_adapter(FakeBackendAdapter(retries=True))


def test_frontend_command_preserves_node_path_with_spaces_and_ownership_marker(
    runner,
) -> None:
    command = runner.build_frontend_command(
        node=Path(r"C:\Program Files\nodejs\node.exe"),
        vite=Path(r"C:\repo with spaces\frontend\node_modules\vite.js"),
        marker="p1b2-stage-c-frontend",
    )

    assert command == [
        r"C:\Program Files\nodejs\node.exe",
        "--title=p1b2-stage-c-frontend",
        r"C:\repo with spaces\frontend\node_modules\vite.js",
        "--host",
        "127.0.0.1",
        "--port",
        "3000",
        "--strictPort",
    ]


def test_phase_1_failure_cleanup_removes_only_transient_logs(
    runner,
    tmp_path,
) -> None:
    run_root = tmp_path / "p1b2-gate4"
    logs = run_root / "logs"
    logs.mkdir(parents=True)
    (logs / "backend.stdout.log").write_text("safe", encoding="utf-8")
    state = run_root / "state.json"
    state.write_text("state-must-remain", encoding="utf-8")

    runner.cleanup_phase1_transient_artifacts(run_root)

    assert not logs.exists()
    assert state.read_text(encoding="utf-8") == "state-must-remain"


def test_phase_1_cleanup_does_not_reidentify_an_exited_retained_process(
    runner,
) -> None:
    record = {
        "pid": 4242,
        "start_time": 100,
        "executable": "node.exe",
        "marker": "phase1-retained-handle",
        "stdout_log": "logs/frontend.stdout.log",
        "stderr_log": "logs/frontend.stderr.log",
    }
    state = {"processes": {"frontend": dict(record)}}
    stopped: list[int] = []
    persisted: list[dict[str, object]] = []

    assert runner.stop_phase1_process_records(
        started_records={"frontend": record},
        state=state,
        process_is_active=lambda _pid: False,
        stop_process=lambda item: stopped.append(int(item["pid"])),
        persist=lambda: persisted.append(dict(state["processes"])),
    )

    assert stopped == []
    assert state["processes"] == {}
    assert persisted == [{}]


def test_secret_leak_priority_precedes_oversized_http_contract_failure(
    runner, monkeypatch
) -> None:
    controlled_value = "unit-controlled-http-value"
    payload = (
        controlled_value.encode("utf-8")
        + b"x" * (5 * 1024 * 1024 + 1)
    )

    class FakeOversizedResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, limit: int) -> bytes:
            assert limit == 5 * 1024 * 1024 + 1
            return payload

    monkeypatch.setattr(
        runner,
        "urlopen",
        lambda *_args, **_kwargs: FakeOversizedResponse(),
    )
    with pytest.raises(runner.Gate4Error) as exc:
        runner._http_json(
            "GET",
            "http://127.0.0.1:9/offline",
            controlled_values=[controlled_value],
        )
    assert str(exc.value) == "P1B2_GATE4_SECRET_LEAK_DETECTED"
    assert "P1B2_GATE4_HTTP_CONTRACT_FAILED" not in str(exc.value)


def test_secret_leak_priority_survives_stage_b_cleanup_failures(
    runner,
) -> None:
    calls: list[str] = []
    ledger = runner.BudgetLedger("secret-priority")

    def stop_owned() -> None:
        calls.append("stop")
        raise RuntimeError("controlled stop failure")

    def check_port_gpu() -> None:
        calls.append("check")
        raise RuntimeError("controlled resource cleanup failure")

    def verify_dotenv() -> bool:
        calls.append("dotenv")
        return True

    def scan_artifacts() -> bool:
        calls.append("scan")
        return True

    with pytest.raises(runner.Gate4Error) as exc:
        runner.finalize_stage_b(
            stop_owned=stop_owned,
            check_port_gpu=check_port_gpu,
            verify_dotenv=verify_dotenv,
            scan_artifacts=scan_artifacts,
            ledger=ledger,
            body_error=runner.Gate4Error(
                "P1B2_GATE4_SECRET_LEAK_DETECTED"
            ),
        )
    assert calls == ["stop", "check", "dotenv", "scan"]
    assert str(exc.value) == "P1B2_GATE4_SECRET_LEAK_DETECTED"
    assert "P1B2_GATE4_CLEANUP_INCOMPLETE" not in str(exc.value)


def test_mock_subprocess_environment_overrides_provider_canaries_and_authorizations(
    runner,
    tmp_path,
) -> None:
    """A Mock child inheriting the root provider mode or key is a safety bug."""

    fake_dotenv = tmp_path / ".env"
    fake_dotenv.write_text(
        "LLM_ADAPTER=provider\n"
        "DEEPSEEK_API_KEY=offline-provider-canary\n"
        "DASHSCOPE_API_KEY=offline-qwen-canary\n",
        encoding="utf-8",
    )
    inherited = runner.parse_dotenv_text(
        fake_dotenv.read_text(encoding="utf-8")
    )
    inherited.update(
        {
            "PATH": "offline-path",
            "M12B_REAL_CALLS_AUTHORIZED": "YES",
            "P1B2_GATE4_AUTHORIZED": "YES",
            "P1B2_GATE4_REAL_PROVIDER_AUTHORIZED": "1",
        }
    )

    environment = runner.build_mock_subprocess_environment(
        inherited,
        backend_pythonpath="offline-backend-src",
    )

    assert environment["LLM_ADAPTER"] == "mock"
    assert environment["DEEPSEEK_API_KEY"] == ""
    assert environment["DASHSCOPE_API_KEY"] == ""
    assert environment["PYTHONUTF8"] == "1"
    assert "offline-provider-canary" not in environment.values()
    assert environment["PYTHONPATH"] == "offline-backend-src"
    for name in (
        "M12B_REAL_CALLS_AUTHORIZED",
        "P1B2_GATE4_AUTHORIZED",
        *REAL_AUTHORIZATION_ENV,
    ):
        assert name not in environment


def test_mock_provider_preflight_constructs_no_provider_adapter_or_delegate(
    runner,
) -> None:
    """Changing the preflight to construct or call a real port must fail."""

    events: list[str] = []

    class Settings:
        llm_adapter = "mock"
        deepseek_api_key = None
        dashscope_api_key = None

    result = runner.mock_provider_preflight(
        {
            "LLM_ADAPTER": "mock",
            "DEEPSEEK_API_KEY": "",
            "DASHSCOPE_API_KEY": "",
        },
        settings_loader=lambda environment: (
            events.append(f"settings:{environment['LLM_ADAPTER']}") or Settings()
        ),
        app_probe=lambda _settings: events.append("mock-app") or {
            "provider_adapter_constructions": 0,
            "provider_delegate_calls": 0,
        },
    )

    assert result == {
        "llm_adapter": "mock",
        "provider_adapter_constructions": 0,
        "provider_delegate_calls": 0,
    }
    assert events == ["settings:mock", "mock-app"]


@pytest.mark.parametrize(
    "environment",
    [
        {"DEEPSEEK_API_KEY": "", "DASHSCOPE_API_KEY": ""},
        {
            "LLM_ADAPTER": "provider",
            "DEEPSEEK_API_KEY": "",
            "DASHSCOPE_API_KEY": "",
        },
        {
            "LLM_ADAPTER": "mock",
            "DEEPSEEK_API_KEY": "canary",
            "DASHSCOPE_API_KEY": "",
        },
        {
            "LLM_ADAPTER": "mock",
            "DEEPSEEK_API_KEY": "",
            "DASHSCOPE_API_KEY": "canary",
        },
    ],
)
def test_mock_provider_preflight_rejects_before_test_collection(
    runner,
    environment,
) -> None:
    """An omitted Mock override or retained key must stop before collection."""

    collected: list[str] = []

    class Settings:
        llm_adapter = environment.get("LLM_ADAPTER", "provider")
        deepseek_api_key = environment.get("DEEPSEEK_API_KEY")
        dashscope_api_key = environment.get("DASHSCOPE_API_KEY")

    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_MOCK_PROVIDER_ISOLATION_FAILED",
    ):
        runner.mock_provider_preflight(
            environment,
            settings_loader=lambda _environment: Settings(),
            app_probe=lambda _settings: collected.append("collected") or {},
        )

    assert collected == []


def test_controlled_mock_command_runs_preflight_in_exact_child_environment(
    runner,
) -> None:
    """A future regression command may not use a different preflight env."""

    events: list[object] = []
    inherited = {
        "PATH": "offline-path",
        "LLM_ADAPTER": "provider",
        "DEEPSEEK_API_KEY": "offline-provider-canary",
        "DASHSCOPE_API_KEY": "offline-qwen-canary",
    }

    exit_code = runner.run_controlled_mock_command(
        ["python", "-m", "pytest", "offline-helper.py"],
        base_environment=inherited,
        backend_pythonpath="offline-backend-src",
        preflight=lambda environment: (
            events.append(("preflight", dict(environment))) or True
        ),
        executor=lambda command, environment: (
            events.append(("execute", tuple(command), dict(environment))) or 0
        ),
    )

    assert exit_code == 0
    assert [event[0] for event in events] == ["preflight", "execute"]
    assert events[0][1] == events[1][2]
    assert events[1][2]["LLM_ADAPTER"] == "mock"
    assert events[1][2]["DEEPSEEK_API_KEY"] == ""
    assert events[1][2]["DASHSCOPE_API_KEY"] == ""
    assert "offline-provider-canary" not in events[1][2].values()


def test_mock_regression_cli_separates_helper_from_full_regression(runner) -> None:
    """The compatibility action must never silently run helper tests only."""

    helper = runner._build_argument_parser().parse_args(
        ["--mock-helper-tests"]
    )
    full = runner._build_argument_parser().parse_args(
        ["--mock-full-regression"]
    )
    powershell = POWERSHELL_PATH.read_text(encoding="utf-8")

    assert helper.mock_helper_tests is True
    assert full.mock_full_regression is True
    assert '"MockHelperTests"' in powershell
    assert '"MockFullRegression"' in powershell
    assert (
        'if ($Action -eq "MockRegression") {\n'
        "        Invoke-MockFullRegressionExecutor"
    ) in powershell


def test_mock_full_regression_owns_cleanup_before_stack_start_returns() -> None:
    """A partial start failure must still enter the idempotent stop path."""

    source = RUNNER_PATH.read_text(encoding="utf-8")
    start = source.index('failure_stage = "MOCK_STACK_START"')
    end = source.index("provider_before =", start)
    start_block = source[start:end]

    assert "stack_attempted = True" in start_block
    assert start_block.index("stack_attempted = True") < start_block.index(
        "_run_captured_command"
    )
    assert "if stack_attempted:" in source


def test_mock_snapshot_dotenv_whitelists_infrastructure_without_provider_key(
    runner,
    tmp_path,
) -> None:
    """The real provider value may never be materialized into the snapshot."""

    source = tmp_path / "source.env"
    source.write_text(
        "APP_ENV=local\n"
        "LOCAL_ACTOR_ID=offline-actor\n"
        "POSTGRES_HOST=127.0.0.1\n"
        "POSTGRES_DB=offline-db\n"
        "POSTGRES_USER=offline-user\n"
        "POSTGRES_PASSWORD=offline-db-secret\n"
        "MINIO_API_PORT=9000\n"
        "MINIO_CONSOLE_PORT=9001\n"
        "MINIO_ENDPOINT=http://127.0.0.1:9000\n"
        "MINIO_ACCESS_KEY=offline-minio-user\n"
        "MINIO_SECRET_KEY=offline-minio-secret\n"
        "MINIO_BUCKET=offline-bucket\n"
        "MINIO_SECURE=false\n"
        "ZTA35G_RUNTIME_URL=http://127.0.0.1:8100\n"
        "ZTA35G_RUNTIME_TOKEN=offline-runtime-secret\n"
        "TIMELINE_CURSOR_SIGNING_KEY=offline-signing-key-at-least-32-bytes\n"
        "DEEPSEEK_API_KEY=provider-canary-must-not-be-read\n"
        "DASHSCOPE_API_KEY=qwen-canary-must-not-be-read\n",
        encoding="utf-8",
    )
    destination = tmp_path / "snapshot.env"

    values = runner.read_mock_infrastructure_dotenv(source)
    runner.write_mock_snapshot_dotenv(destination, values)
    rendered = destination.read_text(encoding="utf-8")

    assert "DEEPSEEK_API_KEY" not in values
    assert "DASHSCOPE_API_KEY" not in values
    assert "provider-canary-must-not-be-read" not in values.values()
    assert "provider-canary-must-not-be-read" not in rendered
    assert "qwen-canary-must-not-be-read" not in values.values()
    assert "qwen-canary-must-not-be-read" not in rendered
    for line in (
        "LLM_ADAPTER=mock",
        "DEEPSEEK_API_KEY=",
        "DASHSCOPE_API_KEY=",
        "M12B_REAL_CALLS_AUTHORIZED=",
        "P1B2_GATE4_AUTHORIZED=",
        "P1B2_GATE4_REAL_PROVIDER_AUTHORIZED=",
        "P1B2_GATE4_REAL_RUNTIME_AUTHORIZED=",
        "P1B2_GATE4_BROWSER_AUTHORIZED=",
        "P1B2_GATE4_PROJECT_OWNER_AUTHORIZED=",
    ):
        assert line in rendered.splitlines()


def test_phase1a_parent_environment_excludes_infrastructure_configuration(
    runner,
) -> None:
    """Phase 1A config tests must still observe genuinely absent settings."""

    environment = runner._mock_phase1a_environment(
        {
            "PATH": "offline-path",
            "POSTGRES_DB": "must-not-enter-tests",
            "MINIO_SECRET_KEY": "must-not-enter-tests",
            "DEEPSEEK_API_KEY": "provider-canary",
            "DASHSCOPE_API_KEY": "qwen-canary",
            "P1B2_GATE4_AUTHORIZED": "YES",
        }
    )

    assert environment["LLM_ADAPTER"] == "mock"
    assert environment["DEEPSEEK_API_KEY"] == ""
    assert environment["DASHSCOPE_API_KEY"] == ""
    assert environment["PYTHONUTF8"] == "1"
    assert "POSTGRES_DB" not in environment
    assert "MINIO_SECRET_KEY" not in environment
    assert "P1B2_GATE4_AUTHORIZED" not in environment
    assert "must-not-enter-tests" not in environment.values()
    assert "provider-canary" not in environment.values()


def test_captured_powershell_output_normalizes_crlf_before_strict_parsing(
    runner,
) -> None:
    assert runner._normalize_captured_text(
        "PHASE_1A_AUTOMATION_PASSED\r\n"
        "run_id=offline-run\r\n"
    ) == (
        "PHASE_1A_AUTOMATION_PASSED\n"
        "run_id=offline-run\n"
    )


def test_frontend_passed_count_ignores_ansi_without_relaxing_count_shape(
    runner,
) -> None:
    output = (
        "\x1b[32m Test Files  9 passed (9)\x1b[39m\n"
        "      Tests  \x1b[1m204 passed\x1b[22m (204)\n"
    )

    assert runner._frontend_passed_count(output) == 204


def _provider_facts(chat: int, explanation: int) -> dict[str, int]:
    return {"CHAT": chat, "EXPLANATION": explanation}


def test_provider_delegate_ledger_is_atomic_and_shared_across_three_backends(
    runner,
    tmp_path,
) -> None:
    """Resetting counts on any Backend restart must fail this test."""

    ledger_path = tmp_path / "provider-ledger.json"
    run_id = "fresh-stage-c-run"
    runner.ProviderDelegateLedger.initialize(ledger_path, run_id=run_id)
    delegated: list[str] = []
    database_facts = {"CHAT": 0, "EXPLANATION": 0}

    backend_1 = runner.ProviderDelegateLedger(ledger_path, run_id=run_id)
    for _index in range(3):
        database_facts["CHAT"] += 1
        backend_1.delegate(
            "CHAT",
            lambda: delegated.append("CHAT"),
            fact_reader=lambda: dict(database_facts),
        )

    backend_2 = runner.ProviderDelegateLedger(ledger_path, run_id=run_id)
    database_facts["EXPLANATION"] += 1
    backend_2.delegate(
        "EXPLANATION",
        lambda: delegated.append("EXPLANATION"),
        fact_reader=lambda: dict(database_facts),
    )

    backend_3 = runner.ProviderDelegateLedger(ledger_path, run_id=run_id)
    database_facts["EXPLANATION"] += 1
    backend_3.delegate(
        "EXPLANATION",
        lambda: delegated.append("EXPLANATION"),
        fact_reader=lambda: dict(database_facts),
    )

    snapshot = backend_3.read()
    assert set(snapshot) == {
        "schema_version",
        "run_id",
        "chat_delegate_attempts",
        "explanation_delegate_attempts",
        "total_delegate_attempts",
        "maximum_delegate_attempts",
        "state",
        "updated_at",
    }
    assert snapshot["run_id"] == run_id
    assert snapshot["chat_delegate_attempts"] == 3
    assert snapshot["explanation_delegate_attempts"] == 2
    assert snapshot["total_delegate_attempts"] == 5
    assert snapshot["maximum_delegate_attempts"] == 5
    assert snapshot["state"] == "EXACT"
    assert delegated == ["CHAT", "CHAT", "CHAT", "EXPLANATION", "EXPLANATION"]

    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_PROVIDER_BUDGET_EXCEEDED",
    ):
        backend_3.delegate(
            "CHAT",
            lambda: delegated.append("six"),
            fact_reader=lambda: dict(database_facts),
        )
    assert delegated[-1] == "EXPLANATION"
    assert backend_3.read()["total_delegate_attempts"] == 5


@pytest.mark.parametrize("damage", ["missing", "corrupt", "run-mismatch"])
def test_provider_delegate_ledger_damage_enters_unknown_before_delegate(
    runner,
    tmp_path,
    damage,
) -> None:
    """Missing, corrupt, or foreign ledgers must never reach the adapter."""

    ledger_path = tmp_path / "provider-ledger.json"
    expected_run = "expected-stage-c-run"
    if damage == "corrupt":
        ledger_path.write_text("{not-json", encoding="utf-8")
    elif damage == "run-mismatch":
        runner.ProviderDelegateLedger.initialize(
            ledger_path,
            run_id="foreign-stage-c-run",
        )
    delegated: list[str] = []

    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_PROVIDER_CALL_COUNT_UNKNOWN",
    ):
        runner.ProviderDelegateLedger(
            ledger_path,
            run_id=expected_run,
        ).delegate(
            "CHAT",
            lambda: delegated.append("CHAT"),
            fact_reader=lambda: _provider_facts(1, 0),
        )

    assert delegated == []
    snapshot = json.loads(ledger_path.read_text(encoding="utf-8"))
    assert snapshot["state"] == "UNKNOWN"


def test_provider_delegate_ledger_detects_count_rollback_and_database_conflict(
    runner,
    tmp_path,
) -> None:
    """A lower file count or divergent LLMCall facts invalidates the run."""

    ledger_path = tmp_path / "provider-ledger.json"
    run_id = "rollback-stage-c-run"
    runner.ProviderDelegateLedger.initialize(ledger_path, run_id=run_id)
    ledger = runner.ProviderDelegateLedger(ledger_path, run_id=run_id)
    ledger.delegate(
        "CHAT",
        lambda: None,
        fact_reader=lambda: _provider_facts(1, 0),
    )
    rolled_back = dict(ledger.read())
    rolled_back.update(
        {
            "chat_delegate_attempts": 0,
            "total_delegate_attempts": 0,
        }
    )
    ledger_path.write_text(
        json.dumps(rolled_back, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    delegated: list[str] = []

    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_PROVIDER_CALL_COUNT_UNKNOWN",
    ):
        ledger.delegate(
            "CHAT",
            lambda: delegated.append("rollback"),
            fact_reader=lambda: _provider_facts(1, 0),
        )
    assert delegated == []
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["state"] == "UNKNOWN"

    conflict_path = tmp_path / "provider-conflict.json"
    runner.ProviderDelegateLedger.initialize(conflict_path, run_id=run_id)
    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_PROVIDER_CALL_COUNT_UNKNOWN",
    ):
        runner.ProviderDelegateLedger(
            conflict_path,
            run_id=run_id,
        ).delegate(
            "CHAT",
            lambda: delegated.append("conflict"),
            fact_reader=lambda: _provider_facts(0, 0),
        )
    assert delegated == []
    assert json.loads(conflict_path.read_text(encoding="utf-8"))["state"] == "UNKNOWN"


def test_provider_delegate_ledger_atomic_replace_failure_enters_unknown(
    runner,
    tmp_path,
) -> None:
    """A failed reservation write must mark UNKNOWN and skip the adapter."""

    ledger_path = tmp_path / "provider-ledger.json"
    run_id = "atomic-failure-run"
    runner.ProviderDelegateLedger.initialize(ledger_path, run_id=run_id)
    calls = 0

    def fail_first_write(path, payload) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("controlled atomic replace failure")
        runner.write_provider_ledger_atomic(path, payload)

    delegated: list[str] = []
    ledger = runner.ProviderDelegateLedger(
        ledger_path,
        run_id=run_id,
        atomic_writer=fail_first_write,
    )
    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_PROVIDER_CALL_COUNT_UNKNOWN",
    ):
        ledger.delegate(
            "CHAT",
            lambda: delegated.append("CHAT"),
            fact_reader=lambda: _provider_facts(1, 0),
        )

    assert calls >= 2
    assert delegated == []
    assert json.loads(ledger_path.read_text(encoding="utf-8"))["state"] == "UNKNOWN"


def test_provider_reservation_crash_window_cannot_resume_as_exact(
    runner,
    tmp_path,
) -> None:
    """A crash after reservation must conservatively poison the run."""

    ledger_path = tmp_path / "provider-ledger.json"
    run_id = "reservation-crash-run"
    runner.ProviderDelegateLedger.initialize(ledger_path, run_id=run_id)

    with pytest.raises(SystemExit):
        runner.ProviderDelegateLedger(
            ledger_path,
            run_id=run_id,
        ).delegate(
            "CHAT",
            lambda: (_ for _ in ()).throw(SystemExit(91)),
            fact_reader=lambda: _provider_facts(1, 0),
        )

    snapshot = runner.ProviderDelegateLedger(
        ledger_path,
        run_id=run_id,
    ).read(allow_non_exact=True)
    assert snapshot["chat_delegate_attempts"] == 1
    assert snapshot["total_delegate_attempts"] == 1
    assert snapshot["state"] in {"UNKNOWN", "INVALIDATED"}
    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_PROVIDER_CALL_COUNT_UNKNOWN",
    ):
        runner.ProviderDelegateLedger(
            ledger_path,
            run_id=run_id,
        ).read()


def test_invalidated_historical_provider_ledger_never_becomes_a_fresh_budget(
    runner,
    tmp_path,
) -> None:
    """The observed lower bound may not be rewritten as a fresh 0/5 run."""

    ledger_path = tmp_path / "provider-ledger.json"
    runner.write_invalidated_provider_ledger(
        ledger_path,
        run_id="invalidated-stage-c-run",
        minimum_chat_delegate_attempts=8,
    )
    ledger = runner.ProviderDelegateLedger(
        ledger_path,
        run_id="invalidated-stage-c-run",
    )
    snapshot = ledger.read(allow_non_exact=True)
    assert snapshot["state"] == "INVALIDATED"
    assert snapshot["chat_delegate_attempts"] == 8
    assert snapshot["total_delegate_attempts"] == 8

    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_PROVIDER_CALL_COUNT_UNKNOWN",
    ):
        ledger.delegate(
            "CHAT",
            lambda: pytest.fail("invalidated run delegated"),
            fact_reader=lambda: _provider_facts(9, 0),
        )
    assert ledger.read(allow_non_exact=True)["state"] == "INVALIDATED"


def test_budget_wrappers_are_explicitly_injected_outside_real_adapters(
    runner,
    tmp_path,
) -> None:
    """Removing create_app port injection would bypass the file ledger."""

    class Chat:
        provider = "deepseek"
        model_name = "deepseek-v4-flash"

        def request_metadata(self, value):
            return ("chat-metadata", value)

        def orchestrate(self, value):
            return ("chat-result", value)

    class Explanation:
        provider = "deepseek"
        model_name = "deepseek-v4-flash"
        prompt_template_id = "explanation"
        prompt_template_version = "1"

        def request_metadata(self, value):
            return ("explanation-metadata", value)

        def explain(self, value):
            return ("explanation-result", value)

    ledger_path = tmp_path / "provider-ledger.json"
    run_id = "injection-stage-c-run"
    runner.ProviderDelegateLedger.initialize(ledger_path, run_id=run_id)
    ledger = runner.ProviderDelegateLedger(ledger_path, run_id=run_id)
    database_facts = {"CHAT": 1, "EXPLANATION": 0}
    captured: dict[str, object] = {}

    def app_factory(**kwargs):
        captured.update(kwargs)
        return "app"

    app = runner.build_budgeted_backend_application(
        settings="settings",
        chat_adapter=Chat(),
        explanation_adapter=Explanation(),
        ledger=ledger,
        fact_reader=lambda: dict(database_facts),
        app_factory=app_factory,
    )
    assert app == "app"
    assert set(captured) == {
        "settings",
        "chat_orchestration_port",
        "explanation_port",
    }
    chat = captured["chat_orchestration_port"]
    explanation = captured["explanation_port"]
    assert chat.orchestrate("chat") == ("chat-result", "chat")
    database_facts["EXPLANATION"] = 1
    assert explanation.explain("explanation") == (
        "explanation-result",
        "explanation",
    )
    assert ledger.read()["total_delegate_attempts"] == 2


def test_backend_factory_without_explicit_port_injection_fails_closed(
    runner,
    tmp_path,
) -> None:
    class Chat:
        provider = "deepseek"
        model_name = "deepseek-v4-flash"

    class Explanation:
        provider = "deepseek"
        model_name = "deepseek-v4-flash"
        prompt_template_id = "explanation"
        prompt_template_version = "1"

    ledger_path = tmp_path / "provider-ledger.json"
    runner.ProviderDelegateLedger.initialize(
        ledger_path, run_id="injection-blocked-run"
    )

    with pytest.raises(
        runner.Gate4Error,
        match="P1B2_GATE4_PROVIDER_LEDGER_INJECTION_BLOCKED",
    ):
        runner.build_budgeted_backend_application(
            settings="settings",
            chat_adapter=Chat(),
            explanation_adapter=Explanation(),
            ledger=runner.ProviderDelegateLedger(
                ledger_path, run_id="injection-blocked-run"
            ),
            fact_reader=lambda: _provider_facts(0, 0),
            app_factory=lambda settings: settings,
        )


def test_three_backend_environments_share_one_authoritative_provider_ledger(
    runner,
    controlled,
    tmp_path,
) -> None:
    ledger_path = tmp_path / "provider-ledger.json"
    safe_values = {
        "actor_id": "actor-fresh-run",
        "database": "p1b2_fresh_run",
        "database_user": "materialsagent",
        "minio_access_key": "materialsagent",
        "bucket": "p1b2-fresh-run",
        "backend_pythonpath": "backend/src",
        "provider_ledger_path": os.fspath(ledger_path),
        "provider_ledger_run_id": "fresh-stage-c-run",
        "fresh_stage_c_authorization": "PROJECT_OWNER_AUTHORIZED",
    }
    environments = [
        runner.build_real_role_environment(
            role,
            {
                "PATH": "offline-path",
                "DEEPSEEK_API_KEY": "parent-canary",
            },
            controlled,
            safe_values=safe_values,
        )
        for role in ("backend-real", "backend-invalid", "backend-real")
    ]

    assert {
        environment["P1B2_GATE4_PROVIDER_LEDGER_PATH"]
        for environment in environments
    } == {os.fspath(ledger_path)}
    assert {
        environment["P1B2_GATE4_PROVIDER_LEDGER_RUN_ID"]
        for environment in environments
    } == {"fresh-stage-c-run"}
    assert [environment["DEEPSEEK_API_KEY"] for environment in environments] == [
        controlled["real_provider_key"],
        controlled["invalid_provider_key"],
        controlled["real_provider_key"],
    ]
    assert all(
        environment["P1B2_GATE4_PROVIDER_LEDGER_REQUIRED"] == "1"
        for environment in environments
    )
    assert all(
        environment["P1B2_GATE4_FRESH_STAGE_C_AUTHORIZED"]
        == "PROJECT_OWNER_AUTHORIZED"
        for environment in environments
    )
    assert all("parent-canary" not in environment.values() for environment in environments)


def test_runner_and_powershell_self_tests_emit_fixed_success_markers() -> None:
    # The subprocess boundary proves no Python helper imports external fixtures.
    python_result = subprocess.run(
        [sys.executable, str(RUNNER_PATH), "--self-test"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert python_result.stdout.splitlines() == [
        "P1B2_GATE4_EXECUTOR_SELF_TEST_OK",
    ]
    assert python_result.stderr == ""
    powershell_result = subprocess.run(
        ["powershell", "-NoProfile", "-File", str(POWERSHELL_PATH), "-SelfTest"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert powershell_result.stdout.splitlines() == [
        "P1B2_GATE4_EXECUTOR_SELF_TEST_OK",
        "P1B2_GATE4_POWERSHELL_SELF_TEST_OK",
    ]
    assert powershell_result.stderr == ""


def test_powershell_surfaces_final_resource_gate_failure_markers() -> None:
    source = POWERSHELL_PATH.read_text(encoding="utf-8")
    for marker in (
        "P1B2_GATE4_STAGE_B_MEMORY_PREFLIGHT_FAILED",
        "P1B2_GATE4_STAGE_C_RUNTIME_START_MEMORY_FAILED",
        "P1B2_GATE4_STAGE_C_TOOL_RETRY_MEMORY_FAILED",
    ):
        assert marker in source


def test_sanitized_system_environment_retains_runtime_dependency_paths(
    runner,
) -> None:
    source = POWERSHELL_PATH.read_text(encoding="utf-8")
    environment = runner._system_child_environment(
        {
            "PATH": "system-path",
            "PROGRAMFILES": r"C:\Program Files",
            "PROGRAMW6432": r"C:\Program Files",
            "USERPROFILE": r"C:\Users\acceptance",
            "DEEPSEEK_API_KEY": "must-not-propagate",
        }
    )
    assert environment == {
        "PATH": "system-path",
        "PROGRAMFILES": r"C:\Program Files",
        "PROGRAMW6432": r"C:\Program Files",
        "USERPROFILE": r"C:\Users\acceptance",
    }
    assert '"PROGRAMFILES", "PROGRAMW6432"' in source


def test_cli_wrapper_emits_only_fixed_failure_marker(runner) -> None:
    stderr = io.StringIO()

    def fail_with_internal_detail() -> None:
        raise runner.Gate4Error("internal-sensitive-detail")

    exit_code = runner.run_cli_action(fail_with_internal_detail, stderr=stderr)
    output = stderr.getvalue()
    assert exit_code != 0
    assert output.strip() == "P1B2_GATE4_EXECUTOR_FAILED"
    assert "internal-sensitive-detail" not in output
    assert "Traceback" not in output


def test_stage_b_cli_is_permanently_blocked_before_dotenv_or_fifth_attempt(
    runner, monkeypatch, capfd
) -> None:
    monkeypatch.setattr(
        runner,
        "inspect_dotenv",
        lambda _path: pytest.fail("blocked Stage B read .env"),
    )
    monkeypatch.setattr(
        runner,
        "_run_stage_b",
        lambda *_args: pytest.fail("fifth Stage B attempt ran"),
    )

    assert runner.main(["--stage-b"]) == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "P1B2_GATE4_STAGE_B_FINAL_ATTEMPT_BLOCKED"
    ]


def test_stage_b_self_test_dispatch_never_reads_dotenv(
    runner, monkeypatch
) -> None:
    self_test_calls: list[str] = []

    def reject_dotenv(_path):
        raise AssertionError("SelfTest must not inspect .env")

    monkeypatch.setattr(runner, "inspect_dotenv", reject_dotenv)
    monkeypatch.setattr(
        runner, "_run_self_test", lambda: self_test_calls.append("self-test")
    )
    assert runner.main(["--self-test"]) == 0
    assert self_test_calls == ["self-test"]


def test_stage_b_cli_unimplemented_stage_fails_with_fixed_marker(
    runner, capfd
) -> None:
    assert runner.main(["--stage-c"]) == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == ["P1B2_GATE4_EXECUTOR_FAILED"]


def test_phase_1_cli_blocks_new_run_before_dotenv_or_service_start(
    runner, monkeypatch, capfd
) -> None:
    monkeypatch.setattr(
        runner,
        "inspect_dotenv",
        lambda _path: pytest.fail("blocked Stage C read .env"),
    )
    monkeypatch.setattr(
        runner,
        "_run_phase_1",
        lambda *_args, **_kwargs: pytest.fail("new Stage C started"),
    )

    assert runner.main(["--phase-1"]) == 1
    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err.splitlines() == [
        "P1B2_GATE4_NEW_STAGE_C_NOT_AUTHORIZED"
    ]


def test_phase_1_self_test_does_not_inspect_dotenv(
    runner, monkeypatch
) -> None:
    calls: list[str] = []

    def reject_dotenv(_path):
        raise AssertionError("SelfTest must remain offline")

    monkeypatch.setattr(runner, "inspect_dotenv", reject_dotenv)
    monkeypatch.setattr(
        runner, "_run_self_test", lambda: calls.append("self-test")
    )
    assert runner.main(["--self-test"]) == 0
    assert calls == ["self-test"]


@pytest.mark.skipif(
    not all(os.getenv(name) == "1" for name in REAL_AUTHORIZATION_ENV),
    reason="P1B2 Gate 4 real journey requires all four explicit authorizations.",
)
def test_real_authorization_gate_requires_explicit_full_authorization() -> None:
    """Four approvals enable the gate; ordinary pytest starts no resources."""
    runner_module = importlib.util.spec_from_file_location("p1b2_gate4_real_auth", RUNNER_PATH)
    assert runner_module is not None and runner_module.loader is not None
    runner = importlib.util.module_from_spec(runner_module)
    runner_module.loader.exec_module(runner)
    assert runner.assert_real_authorized(os.environ) is True
