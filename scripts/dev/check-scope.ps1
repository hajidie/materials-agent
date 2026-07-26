[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Milestone
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Stable exit codes:
# 0 = SCOPE_OK
# 2 = UNKNOWN_MILESTONE
# 3 = GIT_UNAVAILABLE
# 4 = NOT_A_GIT_REPOSITORY
# 5 = OUT_OF_SCOPE_CHANGES
# 6 = SCRIPT_CONFIGURATION_ERROR
$ExitCodes = @{
    SCOPE_OK = 0
    UNKNOWN_MILESTONE = 2
    GIT_UNAVAILABLE = 3
    NOT_A_GIT_REPOSITORY = 4
    OUT_OF_SCOPE_CHANGES = 5
    SCRIPT_CONFIGURATION_ERROR = 6
}

function Complete-ScopeCheck {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,

        [Parameter(Mandatory = $true)]
        [string]$Summary,

        [string[]]$Paths = @()
    )

    Write-Output ("{0} {1}" -f $Code, $Summary)
    foreach ($path in $Paths) {
        Write-Output $path
    }
    exit $ExitCodes[$Code]
}

function ConvertTo-RepoPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $normalized = $Path.Replace([char]92, [char]47)
    while ($normalized.StartsWith('./', [StringComparison]::Ordinal)) {
        $normalized = $normalized.Substring(2)
    }
    return $normalized
}

$normalizedMilestone = $Milestone.Trim().ToUpperInvariant()
$recognizedMilestones = @('G0', 'M0', 'M4A', 'M4B', 'M4PLAN') + @(1..16 | ForEach-Object { "M$_" })
if ($recognizedMilestones -notcontains $normalizedMilestone) {
    Complete-ScopeCheck -Code 'UNKNOWN_MILESTONE' -Summary $normalizedMilestone
}

$globalAllowedPaths = @(
    'docs/progress/phase-1-current-status.md'
)

# M0-M10 configured milestones/work units have reviewed exact allowlists.
# G0 and otherwise unconfigured recognized milestones are retained so
# callers receive a stable configuration error until their exact plan allowlist
# is reviewed and added; this deliberately avoids inventing future scope.
$milestoneAllowlists = @{
    M0 = @(
        '.editorconfig'
        '.gitattributes'
        '.env.example'
        'README.md'
        'environments/README.md'
        'scripts/dev/check-scope.ps1'
        'docs/acceptance/phase-1-checklist.md'
    )
    M1 = @(
        'scripts/dev/check-scope.ps1'
        'environments/materialsagent-backend.yml'
        'backend/pyproject.toml'
        'backend/src/materialsagent/__init__.py'
        'backend/src/materialsagent/main.py'
        'backend/src/materialsagent/api/__init__.py'
        'backend/src/materialsagent/api/routes/__init__.py'
        'backend/src/materialsagent/api/routes/health.py'
        'backend/src/materialsagent/infrastructure/__init__.py'
        'backend/src/materialsagent/infrastructure/config.py'
        'backend/src/materialsagent/infrastructure/logging.py'
        'backend/tests/conftest.py'
        'backend/tests/unit/test_config.py'
        'backend/tests/api/test_health.py'
    )
    M2 = @(
        'scripts/dev/check-scope.ps1'
        'backend/pyproject.toml'
        'backend/src/materialsagent/main.py'
        'backend/src/materialsagent/api/routes/health.py'
        'backend/src/materialsagent/application/bootstrap.py'
        'backend/src/materialsagent/application/readiness.py'
        'backend/src/materialsagent/domain/ports/storage.py'
        'backend/src/materialsagent/infrastructure/config.py'
        'backend/src/materialsagent/infrastructure/storage/minio.py'
        'backend/tests/unit/test_config.py'
        'backend/tests/unit/test_readiness.py'
        'backend/tests/unit/test_storage_contract.py'
        'backend/tests/integration/storage/conftest.py'
        'backend/tests/integration/storage/test_minio_storage.py'
        'backend/tests/api/test_health.py'
    )
    M3 = @(
        'scripts/dev/check-scope.ps1'
        'backend/src/materialsagent/main.py'
        'backend/src/materialsagent/application/context.py'
        'backend/src/materialsagent/application/errors.py'
        'backend/src/materialsagent/application/conversations.py'
        'backend/src/materialsagent/application/messages.py'
        'backend/src/materialsagent/application/tasks.py'
        'backend/src/materialsagent/api/dependencies.py'
        'backend/src/materialsagent/api/routes/conversations.py'
        'backend/src/materialsagent/api/routes/tasks.py'
        'backend/src/materialsagent/domain/ports/unit_of_work.py'
        'backend/src/materialsagent/infrastructure/db/conversation_task.py'
        'backend/tests/unit/test_conversation_title.py'
        'backend/tests/api/conftest.py'
        'backend/tests/api/test_conversations.py'
        'backend/tests/api/test_tasks.py'
    )
    M4A = @(
        'scripts/dev/check-scope.ps1'
        'docs/progress/phase-1-current-status.md'
        'backend/src/materialsagent/domain/ports/chat_orchestration.py'
        'backend/src/materialsagent/application/zta35g_input.py'
        'backend/src/materialsagent/infrastructure/llm/mock.py'
        'backend/src/materialsagent/domain/models/llm_call.py'
        'backend/src/materialsagent/domain/ports/unit_of_work.py'
        'backend/src/materialsagent/infrastructure/db/llm_call.py'
        'backend/src/materialsagent/infrastructure/db/conversation_task.py'
        'backend/src/materialsagent/infrastructure/db/unit_of_work.py'
        'backend/alembic/env.py'
        'backend/alembic/versions/0004_create_llm_call.py'
        'backend/tests/contract/test_chat_orchestration.py'
        'backend/tests/unit/test_zta35g_input.py'
        'backend/tests/unit/test_llm_call_domain.py'
        'backend/tests/integration/db/test_llm_call.py'
        'backend/tests/integration/db/test_migrations.py'
    )
    M4PLAN = @(
        'scripts/dev/check-scope.ps1'
        'docs/acceptance/phase-1-checklist.md'
        'docs/progress/phase-1-current-status.md'
        'docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md'
    )
    M4B = @(
        'scripts/dev/check-scope.ps1'
        'docs/progress/phase-1-current-status.md'
        'backend/src/materialsagent/application/chat_orchestration.py'
        'backend/src/materialsagent/application/errors.py'
        'backend/src/materialsagent/domain/ports/chat_orchestration.py'
        'backend/src/materialsagent/domain/ports/unit_of_work.py'
        'backend/src/materialsagent/infrastructure/llm/mock.py'
        'backend/src/materialsagent/infrastructure/db/conversation_task.py'
        'backend/src/materialsagent/api/dependencies.py'
        'backend/src/materialsagent/api/routes/conversations.py'
        'backend/src/materialsagent/main.py'
        'backend/tests/unit/test_chat_orchestration_service.py'
        'backend/tests/contract/test_chat_orchestration.py'
        'backend/tests/integration/db/test_chat_orchestration_persistence.py'
        'backend/tests/api/test_conversations.py'
        'backend/tests/api/test_message_orchestration.py'
        'backend/tests/api/test_tasks.py'
    )
    M5 = @(
        '.env.example'
        'scripts/dev/check-scope.ps1'
        'backend/alembic/env.py'
        'backend/alembic/versions/0005_create_tool_run.py'
        'backend/src/materialsagent/main.py'
        'backend/src/materialsagent/api/dependencies.py'
        'backend/src/materialsagent/api/routes/tools.py'
        'backend/src/materialsagent/application/tool_execution.py'
        'backend/src/materialsagent/application/tools.py'
        'backend/src/materialsagent/domain/models/tool_run.py'
        'backend/src/materialsagent/domain/ports/tool_execution.py'
        'backend/src/materialsagent/domain/ports/unit_of_work.py'
        'backend/src/materialsagent/infrastructure/config.py'
        'backend/src/materialsagent/infrastructure/db/conversation_task.py'
        'backend/src/materialsagent/infrastructure/db/tool_run.py'
        'backend/src/materialsagent/infrastructure/db/unit_of_work.py'
        'backend/src/materialsagent/infrastructure/tool_clients/local_zta35g.py'
        'backend/tests/api/test_message_orchestration.py'
        'backend/tests/api/test_tools.py'
        'backend/tests/contract/test_runtime_contract.py'
        'backend/tests/integration/db/test_chat_orchestration_persistence.py'
        'backend/tests/integration/db/test_migrations.py'
        'backend/tests/integration/db/test_tool_run.py'
        'backend/tests/unit/test_config.py'
        'backend/tests/unit/test_tool_execution_service.py'
        'mock-runtime/pyproject.toml'
        'mock-runtime/src/materialsagent_mock_runtime/main.py'
        'mock-runtime/tests/conftest.py'
        'mock-runtime/tests/test_runtime.py'
    )
    M6 = @(
        'scripts/dev/check-scope.ps1'
        'backend/pyproject.toml'
        'backend/alembic/env.py'
        'backend/alembic/versions/0006_create_asset.py'
        'backend/src/materialsagent/main.py'
        'backend/src/materialsagent/api/dependencies.py'
        'backend/src/materialsagent/api/routes/assets.py'
        'backend/src/materialsagent/api/routes/tools.py'
        'backend/src/materialsagent/application/asset_service.py'
        'backend/src/materialsagent/application/image_payload.py'
        'backend/src/materialsagent/application/png_encoder.py'
        'backend/src/materialsagent/application/tool_execution.py'
        'backend/src/materialsagent/domain/models/asset.py'
        'backend/src/materialsagent/domain/ports/storage.py'
        'backend/src/materialsagent/domain/ports/tool_execution.py'
        'backend/src/materialsagent/domain/ports/unit_of_work.py'
        'backend/src/materialsagent/infrastructure/db/asset.py'
        'backend/src/materialsagent/infrastructure/db/unit_of_work.py'
        'backend/src/materialsagent/infrastructure/storage/minio.py'
        'backend/tests/unit/test_asset_domain.py'
        'backend/tests/unit/test_image_payload.py'
        'backend/tests/unit/test_png_encoder.py'
        'backend/tests/unit/test_storage_contract.py'
        'backend/tests/unit/test_tool_execution_service.py'
        'backend/tests/integration/db/test_asset.py'
        'backend/tests/integration/db/test_chat_orchestration_persistence.py'
        'backend/tests/integration/db/test_migrations.py'
        'backend/tests/integration/storage/test_asset_lifecycle.py'
        'backend/tests/integration/storage/test_minio_storage.py'
        'backend/tests/api/conftest.py'
        'backend/tests/api/test_assets.py'
        'backend/tests/api/test_message_orchestration.py'
        'backend/tests/api/test_tools.py'
    )
    M7 = @(
        'scripts/dev/check-scope.ps1'
        'backend/alembic/env.py'
        'backend/alembic/versions/0007_create_tool_result_explanation.py'
        'backend/src/materialsagent/main.py'
        'backend/src/materialsagent/api/dependencies.py'
        'backend/src/materialsagent/api/routes/conversations.py'
        'backend/src/materialsagent/api/routes/tasks.py'
        'backend/src/materialsagent/api/routes/tool_results.py'
        'backend/src/materialsagent/application/chat_orchestration.py'
        'backend/src/materialsagent/application/errors.py'
        'backend/src/materialsagent/application/explanation_service.py'
        'backend/src/materialsagent/application/result_service.py'
        'backend/src/materialsagent/application/tasks.py'
        'backend/src/materialsagent/application/tool_execution.py'
        'backend/src/materialsagent/application/tool_workflow.py'
        'backend/src/materialsagent/domain/models/explanation.py'
        'backend/src/materialsagent/domain/models/llm_call.py'
        'backend/src/materialsagent/domain/models/result_asset_link.py'
        'backend/src/materialsagent/domain/models/task.py'
        'backend/src/materialsagent/domain/models/tool_result.py'
        'backend/src/materialsagent/domain/models/tool_run.py'
        'backend/src/materialsagent/domain/ports/explanation.py'
        'backend/src/materialsagent/domain/ports/unit_of_work.py'
        'backend/src/materialsagent/infrastructure/db/asset.py'
        'backend/src/materialsagent/infrastructure/db/conversation_task.py'
        'backend/src/materialsagent/infrastructure/db/explanation.py'
        'backend/src/materialsagent/infrastructure/db/llm_call.py'
        'backend/src/materialsagent/infrastructure/db/tool_result.py'
        'backend/src/materialsagent/infrastructure/db/tool_run.py'
        'backend/src/materialsagent/infrastructure/db/unit_of_work.py'
        'backend/src/materialsagent/infrastructure/llm/mock_explanation.py'
        'backend/tests/api/conftest.py'
        'backend/tests/api/test_assets.py'
        'backend/tests/api/test_explanation_outcomes.py'
        'backend/tests/api/test_message_orchestration.py'
        'backend/tests/api/test_tasks.py'
        'backend/tests/api/test_tool_results.py'
        'backend/tests/contract/test_explanation.py'
        'backend/tests/contract/test_tool_execution_output.py'
        'backend/tests/integration/db/test_chat_orchestration_persistence.py'
        'backend/tests/integration/db/test_conversation_task.py'
        'backend/tests/integration/db/test_explanation_persistence.py'
        'backend/tests/integration/db/test_llm_call.py'
        'backend/tests/integration/db/test_migrations.py'
        'backend/tests/integration/db/test_result_commit.py'
        'backend/tests/integration/db/test_tool_run.py'
        'backend/tests/unit/test_chat_orchestration_service.py'
        'backend/tests/unit/test_explanation_domain.py'
        'backend/tests/unit/test_explanation_service.py'
        'backend/tests/unit/test_llm_call_domain.py'
        'backend/tests/unit/test_result_public_projection.py'
        'backend/tests/unit/test_result_service.py'
        'backend/tests/unit/test_tool_execution_service.py'
        'backend/tests/unit/test_tool_result_domain.py'
    )
    M8 = @(
        'scripts/dev/check-scope.ps1'
        'backend/alembic/env.py'
        'backend/alembic/versions/0008_create_idempotency_record.py'
        'backend/src/materialsagent/main.py'
        'backend/src/materialsagent/api/dependencies.py'
        'backend/src/materialsagent/api/routes/conversations.py'
        'backend/src/materialsagent/api/routes/tasks.py'
        'backend/src/materialsagent/api/routes/tool_results.py'
        'backend/src/materialsagent/application/chat_orchestration.py'
        'backend/src/materialsagent/application/errors.py'
        'backend/src/materialsagent/application/explanation_service.py'
        'backend/src/materialsagent/application/idempotency.py'
        'backend/src/materialsagent/application/messages.py'
        'backend/src/materialsagent/application/result_service.py'
        'backend/src/materialsagent/application/retries.py'
        'backend/src/materialsagent/application/tool_execution.py'
        'backend/src/materialsagent/application/tool_workflow.py'
        'backend/src/materialsagent/domain/models/idempotency_record.py'
        'backend/src/materialsagent/domain/ports/unit_of_work.py'
        'backend/src/materialsagent/infrastructure/db/actor.py'
        'backend/src/materialsagent/infrastructure/db/conversation_task.py'
        'backend/src/materialsagent/infrastructure/db/idempotency_record.py'
        'backend/src/materialsagent/infrastructure/db/tool_result.py'
        'backend/src/materialsagent/infrastructure/db/unit_of_work.py'
        'backend/tests/api/test_assets.py'
        'backend/tests/api/test_conversations.py'
        'backend/tests/api/test_explanation_outcomes.py'
        'backend/tests/api/test_m8_explanation_retry.py'
        'backend/tests/api/test_m8_message_idempotency.py'
        'backend/tests/api/test_m8_tool_retry.py'
        'backend/tests/api/test_message_orchestration.py'
        'backend/tests/api/test_tasks.py'
        'backend/tests/api/test_tools.py'
        'backend/tests/integration/db/test_idempotency_persistence.py'
        'backend/tests/integration/db/test_migrations.py'
        'backend/tests/integration/db/test_result_commit.py'
        'backend/tests/unit/test_explanation_service.py'
        'backend/tests/unit/test_idempotency.py'
    )
    M9 = @(
        'docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md'
        'scripts/dev/check-scope.ps1'
        '.env.example'
        'backend/alembic/versions/0009_add_timeline_query_indexes.py'
        'backend/src/materialsagent/application/timeline.py'
        'backend/src/materialsagent/application/timeline_cursor.py'
        'backend/src/materialsagent/application/tasks.py'
        'backend/src/materialsagent/domain/ports/timeline_query.py'
        'backend/src/materialsagent/infrastructure/config.py'
        'backend/src/materialsagent/infrastructure/db/conversation_task.py'
        'backend/src/materialsagent/infrastructure/db/timeline_query.py'
        'backend/src/materialsagent/api/dependencies.py'
        'backend/src/materialsagent/api/routes/timeline.py'
        'backend/src/materialsagent/api/routes/tasks.py'
        'backend/src/materialsagent/main.py'
        'backend/tests/unit/test_config.py'
        'backend/tests/unit/test_timeline_cursor.py'
        'backend/tests/unit/test_timeline_sort.py'
        'backend/tests/unit/test_task_query.py'
        'backend/tests/contract/test_timeline_contract.py'
        'backend/tests/integration/db/test_migrations.py'
        'backend/tests/integration/db/test_timeline_query.py'
        'backend/tests/api/conftest.py'
        'backend/tests/api/test_timeline.py'
        'backend/tests/api/test_tasks.py'
        'backend/tests/conftest.py'
        'backend/tests/integration/db/conftest.py'
    )
    M10 = @(
        'docs/superpowers/plans/2026-07-17-sem-mvp-phase-1-implementation-plan.md'
        'scripts/dev/check-scope.ps1'
        '.env.example'
        'frontend/.env.example'
        'frontend/.gitignore'
        'frontend/.node-version'
        'frontend/index.html'
        'frontend/package.json'
        'frontend/package-lock.json'
        'frontend/tsconfig.json'
        'frontend/tsconfig.app.json'
        'frontend/tsconfig.node.json'
        'frontend/vite.config.ts'
        'frontend/src/env.d.ts'
        'frontend/src/main.ts'
        'frontend/src/App.vue'
        'frontend/src/styles.css'
        'frontend/src/api/types.ts'
        'frontend/src/api/errors.ts'
        'frontend/src/api/client.ts'
        'frontend/src/composables/useIdempotentRequest.ts'
        'frontend/src/composables/usePolling.ts'
        'frontend/src/composables/useMaterialsAgent.ts'
        'frontend/src/components/ConversationSidebar.vue'
        'frontend/src/components/ConversationView.vue'
        'frontend/src/components/TimelineList.vue'
        'frontend/src/components/UserMessageItem.vue'
        'frontend/src/components/AssistantMessageItem.vue'
        'frontend/src/components/ToolTaskCard.vue'
        'frontend/src/components/StructuredResult.vue'
        'frontend/src/components/AssetGallery.vue'
        'frontend/src/components/TaskHistory.vue'
        'frontend/src/components/ChatComposer.vue'
        'frontend/src/components/GlobalErrorNotice.vue'
        'frontend/src/test/setup.ts'
        'frontend/tests/api/client.test.ts'
        'frontend/tests/composables/idempotent-request.test.ts'
        'frontend/tests/composables/polling.test.ts'
        'frontend/tests/composables/materials-agent.test.ts'
        'frontend/tests/components/conversation-sidebar.test.ts'
        'frontend/tests/components/timeline-list.test.ts'
        'frontend/tests/components/tool-task-card.test.ts'
        'frontend/tests/components/chat-composer.test.ts'
        'frontend/tests/components/app-flow.test.ts'
    )
}

if (-not $milestoneAllowlists.ContainsKey($normalizedMilestone)) {
    Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary ("{0} allowlist is not configured yet." -f $normalizedMilestone)
}

$allowedPaths = @{}
foreach ($path in @($globalAllowedPaths + $milestoneAllowlists[$normalizedMilestone])) {
    $allowedPaths[(ConvertTo-RepoPath -Path $path)] = $true
}

$gitExecutable = $null
if (-not [string]::IsNullOrWhiteSpace($env:PATH)) {
    foreach ($directory in @($env:PATH -split ';')) {
        $normalizedDirectory = $directory.Trim().Trim('"')
        if ([string]::IsNullOrWhiteSpace($normalizedDirectory)) {
            continue
        }
        $candidate = Join-Path $normalizedDirectory 'git.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $gitExecutable = [IO.Path]::GetFullPath($candidate)
            break
        }
    }
}
if ($null -eq $gitExecutable) {
    Complete-ScopeCheck -Code 'GIT_UNAVAILABLE' -Summary 'git executable was not found.'
}

try {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $gitExecutable
    $startInfo.Arguments = '-c core.quotepath=false status --porcelain=v1 -z --untracked-files=all'
    $startInfo.WorkingDirectory = (Get-Location).Path
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $statusOutput = $process.StandardOutput.ReadToEnd()
    $statusError = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $gitExitCode = $process.ExitCode
    $process.Dispose()
}
catch {
    Complete-ScopeCheck -Code 'GIT_UNAVAILABLE' -Summary 'git could not be executed.'
}

if ($gitExitCode -ne 0) {
    if ($statusError -match '(?i)not a git repository') {
        Complete-ScopeCheck -Code 'NOT_A_GIT_REPOSITORY' -Summary 'current directory is not inside a Git work tree.'
    }
    Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary 'git status failed unexpectedly.'
}

$changedPaths = New-Object 'System.Collections.Generic.List[string]'
$records = $statusOutput.Split([char]0)
for ($index = 0; $index -lt $records.Length; $index++) {
    $record = $records[$index]
    if ([string]::IsNullOrEmpty($record)) {
        continue
    }
    if ($record.Length -lt 4 -or $record[2] -ne ' ') {
        Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary 'git status returned an unsupported record.'
    }

    $status = $record.Substring(0, 2)
    $primaryPath = ConvertTo-RepoPath -Path $record.Substring(3)
    $changedPaths.Add($primaryPath)

    # With porcelain v1 -z, rename/copy records store destination first and
    # source in the following NUL-delimited field. Check both paths.
    if ($status.Contains('R') -or $status.Contains('C')) {
        $index++
        if ($index -ge $records.Length -or [string]::IsNullOrEmpty($records[$index])) {
            Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary 'git status returned an incomplete rename or copy record.'
        }
        $sourcePath = ConvertTo-RepoPath -Path $records[$index]
        $changedPaths.Add($sourcePath)
    }
}

$outOfScope = @(
    $changedPaths |
        Where-Object { -not $allowedPaths.ContainsKey($_) } |
        Sort-Object -Unique
)

if ($outOfScope.Count -gt 0) {
    Complete-ScopeCheck -Code 'OUT_OF_SCOPE_CHANGES' -Summary $normalizedMilestone -Paths $outOfScope
}

Complete-ScopeCheck -Code 'SCOPE_OK' -Summary $normalizedMilestone
