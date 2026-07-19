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
$recognizedMilestones = @('G0', 'M0') + @(1..16 | ForEach-Object { "M$_" })
if ($recognizedMilestones -notcontains $normalizedMilestone) {
    Complete-ScopeCheck -Code 'UNKNOWN_MILESTONE' -Summary $normalizedMilestone
}

$globalAllowedPaths = @(
    'docs/progress/phase-1-current-status.md'
)

# M0-M3 have reviewed exact allowlists. G0 and M4-M16 are recognized so
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
        'backend/alembic/env.py'
        'backend/alembic/versions/0002_create_conversation_message_task_revision.py'
        'backend/alembic/versions/0003_add_task_time_order_constraints.py'
        'backend/src/materialsagent/domain/models/conversation.py'
        'backend/src/materialsagent/domain/models/message.py'
        'backend/src/materialsagent/domain/models/task.py'
        'backend/src/materialsagent/domain/models/task_input_revision.py'
        'backend/src/materialsagent/domain/ports/unit_of_work.py'
        'backend/src/materialsagent/infrastructure/db/conversation_task.py'
        'backend/src/materialsagent/infrastructure/db/unit_of_work.py'
        'backend/tests/integration/db/conftest.py'
        'backend/tests/integration/db/test_migrations.py'
        'backend/tests/integration/db/test_conversation_task.py'
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
