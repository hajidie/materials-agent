# Local Dev One-Command Real Stack Implementation Plan

> **For agentic workers:** Execute this single task inline with test-driven development. Do not dispatch subagents, stage, commit, push, amend, read the real root `.env` during tests, or start real DeepSeek, Runtime, or GPU resources.

**Goal:** Make real Runtime plus DeepSeek local development start with one PowerShell command while keeping secrets out of arguments, output, logs, and state.

**Architecture:** Reuse Backend's existing root `.env` settings loader for DeepSeek and validate it through a silent Backend-Python subprocess before resources start. Generate a per-run Runtime token in memory, inject it only into Backend and Runtime child environments, and use the repository's verified `SEM\ZTA35G_lab` directory as the real Runtime model-root default.

**Tech Stack:** Windows PowerShell, Backend Python 3.11 configuration loader, existing local-dev process launcher, offline PowerShell self-tests.

## Global Constraints

- Do not change production Backend or Frontend business logic, dependencies, migrations, models, or weights.
- Do not read or modify the real root `.env` during implementation or offline verification.
- Do not invoke DeepSeek, the real Runtime, GPU, or model loading.
- Do not persist or print the DeepSeek API key or generated Runtime token.
- Do not stage, commit, push, or amend.

---

### Task 1: One-command real-stack configuration

**Files:**
- Modify: `scripts/dev/test-local-dev.ps1`
- Modify: `scripts/dev/local-dev.ps1`
- Modify: `docs/local-development.md`
- Modify: `docs/progress/phase-1-current-status.md`

**Interfaces:**
- Consumes: `materialsagent.infrastructure.config.load_settings()` and `parse_deepseek_config()` in an isolated preflight subprocess.
- Produces: `New-EphemeralRuntimeToken`, `Assert-DeepSeekRootConfiguration`, token-aware `New-LaunchProfile`, and the command `scripts/dev/local-dev.ps1 Start -Runtime Real -Llm DeepSeek` without caller-supplied variables.

- [x] **Step 1: Add failing offline tests**

  Extend `scripts/dev/test-local-dev.ps1` to assert that the real launch profile injects one supplied Runtime token into Backend and Runtime only, clears caller `DEEPSEEK_API_KEY` so Backend uses root `.env`, defaults the model root to `SEM\ZTA35G_lab`, generates nonblank distinct tokens, and maps stubbed DeepSeek preflight exit codes to secret-free errors.

- [x] **Step 2: Run the focused tests and record RED**

  Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/dev/test-local-dev.ps1`; expect failures because the token generator, root-configuration preflight, and launch-profile inputs do not exist yet.

- [x] **Step 3: Implement the minimum entry-point changes**

  Add the token generator and silent Backend-Python preflight, update the launch profile to inject the ephemeral token and fixed model root, pass the token explicitly to the initial Runtime ready probe, and use Backend's Tool catalog for repeated-start Runtime readiness because the token is intentionally not persisted.

- [x] **Step 4: Update concise usage and failure guidance**

  Document the one-command real-stack start, root `.env` DeepSeek loading, fixed model-root default, in-memory Runtime token, repeated-start readiness boundary, and safe missing/invalid configuration messages.

- [x] **Step 5: Run offline verification and record GREEN**

  Run the focused PowerShell suite, PowerShell parser checks for both scripts, `git diff --check`, `scripts/dev/check-sem-integrity.ps1`, and read-only Git status/diff checks. Confirm the tests did not invoke real resources; if an independently started state/listener exists, report it without stopping, deleting, or claiming it came from the offline suite.

### Task 2: Windows PowerShell Compose-stop stderr compatibility

**Files:**
- Modify: `scripts/dev/test-local-dev.ps1`
- Modify: `scripts/dev/local-dev.ps1`
- Modify: `docs/local-development.md`
- Modify: `docs/progress/phase-1-current-status.md`

**Interfaces:**
- Consumes: Windows PowerShell 5.1 native-command stderr and `$LASTEXITCODE` behavior.
- Produces: `Invoke-NativeCommandExitCode`, which suppresses native progress streams, returns the real exit code, and restores the caller's error preference.

- [x] **Step 1: Reproduce the reported failure without Docker**

  Run a harmless `cmd.exe` command that writes one progress line to stderr and exits zero under `$ErrorActionPreference='Stop'`; confirm `NativeCommandError` occurs before subsequent statements.

- [x] **Step 2: Add and observe the failing regression test**

  Add a test that invokes the same real native behavior through `Invoke-NativeCommandExitCode`, expects exit code `0`, and expects the caller's error preference to be restored. Record RED because the helper is absent.

- [x] **Step 3: Implement the minimum fix**

  Temporarily set `$ErrorActionPreference='Continue'` only around the native operation, discard progress streams, capture `$LASTEXITCODE`, restore the original preference in `finally`, and use the helper at the single Compose-stop call site.

- [x] **Step 4: Verify offline and leave live cleanup to the project owner**

  Run the complete local-dev offline suite and static checks without invoking Docker stop. Record the partially stopped external state and require the project owner to rerun Stop before Start.

### Task 3: Windows PowerShell Compose-start stderr compatibility

**Files:**
- Modify: `scripts/dev/test-local-dev.ps1`
- Modify: `scripts/dev/local-dev.ps1`
- Modify: `docs/local-development.md`
- Modify: `docs/progress/phase-1-current-status.md`

**Interfaces:**
- Consumes: `Invoke-NativeCommandExitCode` from Task 2.
- Produces: `Invoke-ComposeStartSafely`, the only local-dev Compose-start exit-code gate.

- [x] **Step 1: Confirm the failure boundary read-only**

  Verify state is absent, both Compose containers are `Exited (0)`, the newest run directory has no files, and the direct Compose-start call still runs under global `$ErrorActionPreference='Stop'`.

- [x] **Step 2: Add and observe the failing call-site regression test**

  Run real `cmd.exe` stderr/exit-0 behavior through `Invoke-ComposeStartSafely`; record RED because the call-site function is absent.

- [x] **Step 3: Route Compose start through the safe native wrapper**

  Capture the real exit code through `Invoke-NativeCommandExitCode`; throw `LOCAL_DEV_COMPOSE_START_FAILED` only when it is nonzero.

- [x] **Step 4: Verify without starting Docker or App resources**

  Run the full local-dev offline suite and static checks, then confirm state remains absent and both Compose services remain stopped.
