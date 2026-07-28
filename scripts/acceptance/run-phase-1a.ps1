[CmdletBinding()]
param(
    [switch]$ManualBrowserAccepted
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..\..')).Path
$runId = (
    [DateTime]::UtcNow.ToString('yyyyMMddTHHmmssZ') +
    '-' +
    [Guid]::NewGuid().ToString('N').Substring(0, 12)
)
$runRelative = "tmp/phase-1a-acceptance/$runId"
$runRoot = Join-Path $repoRoot ($runRelative.Replace('/', '\'))
$logsRoot = Join-Path $runRoot 'logs'
$junitRoot = Join-Path $runRoot 'junit'
$summaryPath = Join-Path $runRoot 'summary.json'
$commandsPath = Join-Path $runRoot 'commands.json'
$statePath = Join-Path $repoRoot 'tmp\m11-mock-stack\state.json'
$startScript = Join-Path $repoRoot 'scripts\dev\start-mock-stack.ps1'
$stopScript = Join-Path $repoRoot 'scripts\dev\stop-mock-stack.ps1'
$scopeScript = Join-Path $repoRoot 'scripts\dev\check-scope.ps1'
$semScript = Join-Path $repoRoot 'scripts\dev\check-sem-integrity.ps1'
$powershellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
$m11aBaseline = '4ed740222238433541fb31c993dd75610634d157'
$expectedComposeServices = @('minio', 'postgresql')
$expectedComposeImages = @(
    (
        'minio/minio:RELEASE.2025-09-07T16-13-09Z@sha256:' +
        '14cea493d9a34af32f524e538b8346cf79f3321eff8e708c1e2960462bd8936e'
    ),
    (
        'postgres:17.10-bookworm@sha256:' +
        '4f736ae292687621d4dbe0d499ffd024a36bd2ee7d8ca6f2ccd4c800f047b394'
    )
)

$expectedM11BPaths = @(
    'docs/progress/phase-1-current-status.md',
    'scripts/dev/check-scope.ps1',
    'scripts/dev/start-mock-stack.ps1',
    'scripts/dev/stop-mock-stack.ps1',
    'scripts/acceptance/run-phase-1a.ps1',
    'backend/src/materialsagent/domain/ports/storage.py',
    'backend/src/materialsagent/infrastructure/storage/minio.py',
    'backend/src/materialsagent/application/asset_service.py',
    'backend/src/materialsagent/infrastructure/llm/mock.py',
    'backend/tests/unit/test_storage_contract.py',
    'backend/tests/contract/test_chat_orchestration.py',
    'backend/tests/integration/storage/test_minio_storage.py',
    'backend/tests/integration/storage/test_asset_lifecycle.py',
    'backend/tests/e2e/conftest.py',
    'backend/tests/e2e/test_mock_journey.py',
    'backend/tests/e2e/test_mock_acceptance_matrix.py',
    'docs/acceptance/phase-1a-report.md'
)

$allowedPaths = @(
    'docs/progress/phase-1-current-status.md',
    'scripts/dev/check-scope.ps1',
    'scripts/dev/start-mock-stack.ps1',
    'scripts/dev/stop-mock-stack.ps1',
    'scripts/acceptance/run-phase-1a.ps1',
    'backend/src/materialsagent/domain/ports/storage.py',
    'backend/src/materialsagent/infrastructure/storage/minio.py',
    'backend/src/materialsagent/application/asset_service.py',
    'backend/src/materialsagent/infrastructure/llm/mock.py',
    'backend/tests/unit/test_storage_contract.py',
    'backend/tests/contract/test_chat_orchestration.py',
    'backend/tests/integration/storage/test_minio_storage.py',
    'backend/tests/integration/storage/test_asset_lifecycle.py',
    'backend/tests/e2e/conftest.py',
    'backend/tests/e2e/test_mock_journey.py',
    'backend/tests/e2e/test_mock_acceptance_matrix.py',
    'docs/acceptance/phase-1a-report.md'
)

$script:commandRecords = [System.Collections.Generic.List[object]]::new()
$script:failed = 0
$script:startedByRunner = $false
$script:stackAvailable = $false
$script:pythonExe = $null
$script:gitEvidence = [ordered]@{
    branch = $null
    head = $null
    subject = $null
    parent = $null
    m11a_baseline = $m11aBaseline
    m11a_is_ancestor = $false
    staging_empty = $false
    baseline_valid = $false
}
$script:environmentEvidence = [ordered]@{
    python = $null
    node = $null
    npm = $null
    docker_compose = $null
    compose_services = @()
    compose_images = @()
}
$script:versionEvidenceProbeCompleted = $false
$script:securityScanEvidence = [ordered]@{
    allowlist_path_count = $allowedPaths.Count
    allowlist_coverage_valid = $false
    dynamic_artifacts_scanned = $false
}
$script:sensitiveValues = [System.Collections.Generic.List[string]]::new()
$script:scenarioCounts = [ordered]@{
    success_scenarios = 0
    input_error_scenarios = 0
    dependency_failure_scenarios = 0
    idempotency_scenarios = 0
    retry_scenarios = 0
    asset_security_scenarios = 0
    browser_manual_scenarios = 0
}

function Get-AcceptanceMarker {
    param(
        [bool]$AutomationPassed,
        [bool]$ManualAccepted
    )

    if (-not $AutomationPassed) {
        return 'PHASE_1A_AUTOMATION_FAILED'
    }
    if ($ManualAccepted) {
        return 'PHASE_1A_ACCEPTANCE_PASSED'
    }
    return 'PHASE_1A_AUTOMATION_PASSED'
}

if ($env:MATERIALSAGENT_PHASE1A_MARKER_PROBE -eq '1') {
    $automation = Get-AcceptanceMarker `
        -AutomationPassed $true `
        -ManualAccepted $false
    $acceptance = Get-AcceptanceMarker `
        -AutomationPassed $true `
        -ManualAccepted $true
    $failure = Get-AcceptanceMarker `
        -AutomationPassed $false `
        -ManualAccepted $false
    if (
        $automation -ne 'PHASE_1A_AUTOMATION_PASSED' -or
        $acceptance -ne 'PHASE_1A_ACCEPTANCE_PASSED' -or
        $failure -ne 'PHASE_1A_AUTOMATION_FAILED'
    ) {
        Write-Output 'MANUAL_MARKER_LOGIC_FAILED'
        exit 1
    }
    Write-Output 'MANUAL_MARKER_LOGIC_OK'
    exit 0
}

New-Item -ItemType Directory -Path $logsRoot -Force | Out-Null
New-Item -ItemType Directory -Path $junitRoot -Force | Out-Null
Set-Location -LiteralPath $repoRoot

function Read-DotEnv {
    param([string]$Path)

    $values = @{}
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $values
    }
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        if ($line -notmatch '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
            continue
        }
        $name = $Matches[1]
        $value = $Matches[2].Trim()
        if (
            $value.Length -ge 2 -and
            (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            )
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        $values[$name] = $value
    }
    return $values
}

$dotEnv = Read-DotEnv -Path (Join-Path $repoRoot '.env')
$sensitiveNames = @(
    'POSTGRES_PASSWORD',
    'MINIO_ACCESS_KEY',
    'MINIO_SECRET_KEY',
    'ZTA35G_RUNTIME_TOKEN',
    'TIMELINE_CURSOR_SIGNING_KEY',
    'LLM_API_KEY'
)
foreach ($name in $sensitiveNames) {
    if ($dotEnv.ContainsKey($name)) {
        $value = [string]$dotEnv[$name]
        if ($value.Length -ge 4) {
            [void]$script:sensitiveValues.Add($value)
        }
    }
}

function ConvertTo-SafeText {
    param([AllowNull()][object]$Value)

    $safe = ''
    if ($null -ne $Value) {
        $safe = [string]$Value
    }
    foreach ($secret in $script:sensitiveValues) {
        $safe = $safe.Replace($secret, '<redacted>')
    }
    $safe = $safe.Replace($repoRoot, '<repo>')
    $safe = $safe.Replace($repoRoot.Replace('\', '/'), '<repo>')
    $absolutePathPattern = '(?i)\b[A-Z]:[\\/][^\s"''<>|]+'
    $safe = [regex]::Replace($safe, $absolutePathPattern, '<absolute-path>')
    $safe = [regex]::Replace(
        $safe,
        (
            '(?i)(\b(?:pid(?:s|_id)?|process_?id|processid)' +
            '\s*[:=]\s*)\d+'
        ),
        '$1<redacted>'
    )
    return $safe
}

function Write-SafeLog {
    param(
        [string]$RelativePath,
        [AllowNull()][object]$Content
    )

    $absolutePath = Join-Path $runRoot ($RelativePath.Replace('/', '\'))
    $parent = Split-Path -Parent $absolutePath
    New-Item -ItemType Directory -Path $parent -Force | Out-Null
    $safe = ConvertTo-SafeText -Value $Content
    [IO.File]::WriteAllText(
        $absolutePath,
        $safe,
        [Text.UTF8Encoding]::new($false)
    )
}

function Add-CommandRecord {
    param(
        [string]$Name,
        [DateTime]$StartedAt,
        [DateTime]$EndedAt,
        [int]$ExitCode,
        [string]$LogRelative
    )

    [void]$script:commandRecords.Add(
        [ordered]@{
            name = $Name
            started_at = $StartedAt.ToUniversalTime().ToString('o')
            ended_at = $EndedAt.ToUniversalTime().ToString('o')
            exit_code = $ExitCode
            log = "$runRelative/$LogRelative"
        }
    )
}

function Invoke-RecordedCommand {
    param(
        [string]$Name,
        [string]$FilePath,
        [string[]]$Arguments,
        [string]$LogRelative,
        [switch]$Required
    )

    $startedAt = [DateTime]::UtcNow
    $output = ''
    $exitCode = 1
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $captured = & $FilePath @Arguments 2>&1
        if ($null -eq $LASTEXITCODE) {
            $exitCode = 0
        }
        else {
            $exitCode = [int]$LASTEXITCODE
        }
        $output = $captured | Out-String
    }
    catch {
        $output = $_ | Out-String
        $exitCode = 1
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $endedAt = [DateTime]::UtcNow
    Write-SafeLog -RelativePath $LogRelative -Content $output
    Add-CommandRecord `
        -Name $Name `
        -StartedAt $startedAt `
        -EndedAt $endedAt `
        -ExitCode $exitCode `
        -LogRelative $LogRelative
    if ($Required -and $exitCode -ne 0) {
        $script:failed++
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
    }
}

function Invoke-RecordedAction {
    param(
        [string]$Name,
        [scriptblock]$Action,
        [string]$LogRelative,
        [switch]$Required
    )

    $startedAt = [DateTime]::UtcNow
    $output = ''
    $exitCode = 0
    try {
        $output = (& $Action 2>&1 | Out-String)
    }
    catch {
        $output = $_ | Out-String
        $exitCode = 1
    }
    $endedAt = [DateTime]::UtcNow
    Write-SafeLog -RelativePath $LogRelative -Content $output
    Add-CommandRecord `
        -Name $Name `
        -StartedAt $startedAt `
        -EndedAt $endedAt `
        -ExitCode $exitCode `
        -LogRelative $LogRelative
    if ($Required -and $exitCode -ne 0) {
        $script:failed++
    }
    return [pscustomobject]@{
        ExitCode = $exitCode
        Output = $output
    }
}

function Assert-Result {
    param(
        [bool]$Condition,
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Get-SafeSingleLine {
    param([AllowNull()][object]$Value)

    $lines = @(
        ((ConvertTo-SafeText -Value $Value) -split '\r?\n') |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_.Length -gt 0 }
    )
    if ($lines.Count -ne 1) {
        throw 'Expected exactly one safe output line.'
    }
    return $lines[0]
}

function Get-SafeOutputLines {
    param([AllowNull()][object]$Value)

    return @(
        ((ConvertTo-SafeText -Value $Value) -split '\r?\n') |
            ForEach-Object { $_.Trim() } |
            Where-Object { $_.Length -gt 0 }
    )
}

function Test-ExactSet {
    param(
        [string[]]$Actual,
        [string[]]$Expected
    )

    $actualSorted = @($Actual | Sort-Object)
    $expectedSorted = @($Expected | Sort-Object)
    if ($actualSorted.Count -ne $expectedSorted.Count) {
        return $false
    }
    for ($index = 0; $index -lt $actualSorted.Count; $index++) {
        if ($actualSorted[$index] -cne $expectedSorted[$index]) {
            return $false
        }
    }
    return $true
}

function Test-AcceptanceAllowlistCoverage {
    $duplicates = @(
        $allowedPaths |
            Group-Object -CaseSensitive |
            Where-Object { $_.Count -gt 1 }
    )
    Assert-Result ($duplicates.Count -eq 0) `
        'Acceptance scan allowlist contains duplicate paths.'
    Assert-Result ($allowedPaths.Count -eq 17) `
        'Acceptance scan allowlist must contain exactly 17 paths.'
    Assert-Result (
        Test-ExactSet `
            -Actual $allowedPaths `
            -Expected $expectedM11BPaths
    ) 'Acceptance scan allowlist does not match the M11-B scope.'
    foreach ($relative in $allowedPaths) {
        $path = Join-Path $repoRoot ($relative.Replace('/', '\'))
        Assert-Result (
            Test-Path -LiteralPath $path -PathType Leaf
        ) 'An acceptance scan allowlist file is missing.'
    }
    $script:securityScanEvidence.allowlist_path_count = $allowedPaths.Count
    $script:securityScanEvidence.allowlist_coverage_valid = $true
    return "ACCEPTANCE_SCAN_COVERAGE_OK count=$($allowedPaths.Count)"
}

function Get-ScenarioCounts {
    param([string]$JunitPath)

    if (-not (Test-Path -LiteralPath $JunitPath -PathType Leaf)) {
        throw 'M11 E2E JUnit XML is missing.'
    }
    [xml]$document = Get-Content -LiteralPath $JunitPath -Raw -Encoding UTF8
    $names = @(
        $document.SelectNodes('//testcase') |
            ForEach-Object {
                '{0}::{1}' -f $_.classname, $_.name
            }
    )
    $matrixNames = @(
        $names |
            Where-Object {
                $_ -match 'test_mock_acceptance_matrix'
            }
    )
    $counts = [ordered]@{
        success_scenarios = @(
            $matrixNames | Where-Object { $_ -match '::test_success_' }
        ).Count
        input_error_scenarios = @(
            $matrixNames | Where-Object { $_ -match '::test_input_error_' }
        ).Count
        dependency_failure_scenarios = @(
            $matrixNames |
                Where-Object { $_ -match '::test_dependency_failure_' }
        ).Count
        idempotency_scenarios = @(
            $matrixNames |
                Where-Object {
                    $_ -match '::test_idempotency_' -or
                    (
                        $_ -match '::test_retry_' -and
                        $_ -match 'replay'
                    )
                }
        ).Count
        retry_scenarios = @(
            $matrixNames | Where-Object { $_ -match '::test_retry_' }
        ).Count
        asset_security_scenarios = @(
            $matrixNames |
                Where-Object {
                    $_ -match '::test_asset_' -or
                    $_ -match '::test_security_'
                }
        ).Count
    }
    Assert-Result ($matrixNames.Count -eq 20) `
        'Expected exactly 20 M11-B matrix cases.'
    Assert-Result ($counts.success_scenarios -eq 1) `
        'Success scenario count mismatch.'
    Assert-Result ($counts.input_error_scenarios -eq 5) `
        'Input-error scenario count mismatch.'
    Assert-Result ($counts.dependency_failure_scenarios -eq 7) `
        'Dependency-failure scenario count mismatch.'
    Assert-Result ($counts.idempotency_scenarios -eq 5) `
        'Idempotency scenario count mismatch.'
    Assert-Result ($counts.retry_scenarios -eq 2) `
        'Retry scenario count mismatch.'
    Assert-Result ($counts.asset_security_scenarios -eq 2) `
        'Asset/security scenario count mismatch.'
    return $counts
}

function Test-AcceptanceArtifacts {
    $null = Test-AcceptanceAllowlistCoverage
    $findings = [System.Collections.Generic.List[string]]::new()
    $textExtensions = @('.json', '.log', '.xml', '.txt')
    $artifactFiles = @(
        Get-ChildItem -LiteralPath $runRoot -Recurse -File |
            Where-Object { $textExtensions -contains $_.Extension.ToLowerInvariant() }
    )
    foreach ($file in $artifactFiles) {
        $contentValue = (
            Get-Content -LiteralPath $file.FullName -Raw -Encoding UTF8
        )
        if ($null -eq $contentValue) {
            $content = ''
        }
        else {
            $content = [string]$contentValue
        }
        foreach ($secret in $script:sensitiveValues) {
            if ($content.Contains($secret)) {
                [void]$findings.Add('actual secret value')
            }
        }
        if (
            $content.Contains($repoRoot) -or
            $content.Contains($repoRoot.Replace('\', '/'))
        ) {
            [void]$findings.Add('repository absolute path')
        }
        if ($content -match '(?i)\b[A-Z]:[\\/]') {
            [void]$findings.Add('Windows absolute path')
        }
        if (
            $content -match (
                '(?i)\b(?:pid(?:s|_id)?|process_?id|processid)' +
                '\s*[:=]\s*\d+'
            )
        ) {
            [void]$findings.Add('process identifier')
        }
        if ($content -match '(?i)\bobject_key\s*[:=]\s*["''][^"'']+') {
            [void]$findings.Add('object key')
        }
        if ($content -match '[A-Za-z0-9+/]{256,}={0,2}') {
            [void]$findings.Add('large Base64 payload')
        }
        if ($content -match '(?m)^Traceback \(most recent call last\):') {
            [void]$findings.Add('Python traceback')
        }
        if (
            $content -match (
                '(?i)\b(?:full_?prompt|raw_?prompt|prompt_text)' +
                '\s*[:=]\s*["''][^"'']+'
            )
        ) {
            [void]$findings.Add('full prompt')
        }
    }

    foreach ($relative in $allowedPaths) {
        $path = Join-Path $repoRoot ($relative.Replace('/', '\'))
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            continue
        }
        $contentValue = (
            Get-Content -LiteralPath $path -Raw -Encoding UTF8
        )
        if ($null -eq $contentValue) {
            $content = ''
        }
        else {
            $content = [string]$contentValue
        }
        foreach ($secret in $script:sensitiveValues) {
            if ($content.Contains($secret)) {
                [void]$findings.Add('actual secret value in allowlist')
            }
        }
        if (
            $content.Contains($repoRoot) -or
            $content.Contains($repoRoot.Replace('\', '/'))
        ) {
            [void]$findings.Add('repository absolute path in allowlist')
        }
        if (
            $content -match (
                '(?i)\b[A-Z]:[\\/]' +
                '(?:Users|ProgramData|Windows|CodexData)[\\/]'
            )
        ) {
            [void]$findings.Add('Windows absolute path in allowlist')
        }
        if (
            $content -match (
                '(?i)\b(?:pid(?:s|_id)?|process_?id|processid)' +
                '\s*[:=]\s*[1-9]\d{2,}'
            )
        ) {
            [void]$findings.Add('process identifier in allowlist')
        }
        $objectKeyMatches = [regex]::Matches(
            $content,
            '(?i)\bobject_key\s*[:=]\s*["''](?<value>[^"'']+)'
        )
        foreach ($objectKeyMatch in $objectKeyMatches) {
            $isControlledTestFixture = (
                $relative.StartsWith(
                    'backend/tests/',
                    [StringComparison]::Ordinal
                ) -and
                $objectKeyMatch.Groups['value'].Value -match (
                    '^assets/test/[A-Za-z0-9._/-]+$'
                )
            )
            if (-not $isControlledTestFixture) {
                [void]$findings.Add('object key in allowlist')
            }
        }
        if ($content -match '[A-Za-z0-9+/]{256,}={0,2}') {
            [void]$findings.Add('large Base64 payload in allowlist')
        }
        if ($content -match '(?m)^Traceback \(most recent call last\):') {
            [void]$findings.Add('Python traceback in allowlist')
        }
        if (
            $content -match (
                '(?i)\b(?:full_?prompt|raw_?prompt|prompt_text)' +
                '\s*[:=]\s*["''][^"'']+'
            )
        ) {
            [void]$findings.Add('full prompt in allowlist')
        }
    }

    if ($findings.Count -gt 0) {
        $unique = @($findings | Sort-Object -Unique)
        throw ('Acceptance security scan failed: ' + ($unique -join ', '))
    }
    $requiredDynamicArtifacts = @(
        $commandsPath,
        $summaryPath,
        $junitPath
    )
    $requiredDynamicArtifactsPresent = @(
        $requiredDynamicArtifacts |
            Where-Object {
                -not (Test-Path -LiteralPath $_ -PathType Leaf)
            }
    ).Count -eq 0
    $dynamicLogsPresent = @(
        Get-ChildItem -LiteralPath $logsRoot -File -ErrorAction SilentlyContinue
    ).Count -gt 0
    if ($requiredDynamicArtifactsPresent -and $dynamicLogsPresent) {
        $script:securityScanEvidence.dynamic_artifacts_scanned = $true
    }
    'ACCEPTANCE_SECURITY_SCAN_OK'
}

function Write-AcceptanceMetadata {
    param([string]$Marker)

    $commandsJson = @($script:commandRecords) | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText(
        $commandsPath,
        $commandsJson,
        [Text.UTF8Encoding]::new($false)
    )

    $summary = [ordered]@{
        run_id = $runId
        status = $Marker
        started_by_runner = $script:startedByRunner
        manual_browser_accepted = [bool]$ManualBrowserAccepted
        git = $script:gitEvidence
        environment = $script:environmentEvidence
        security_scan = $script:securityScanEvidence
        success_scenarios = $script:scenarioCounts.success_scenarios
        input_error_scenarios = $script:scenarioCounts.input_error_scenarios
        dependency_failure_scenarios = (
            $script:scenarioCounts.dependency_failure_scenarios
        )
        idempotency_scenarios = $script:scenarioCounts.idempotency_scenarios
        retry_scenarios = $script:scenarioCounts.retry_scenarios
        asset_security_scenarios = (
            $script:scenarioCounts.asset_security_scenarios
        )
        browser_manual_scenarios = (
            $script:scenarioCounts.browser_manual_scenarios
        )
        failed = $script:failed
        commands = "$runRelative/commands.json"
        junit = "$runRelative/junit/m11-e2e.xml"
    }
    $summaryJson = $summary | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText(
        $summaryPath,
        $summaryJson,
        [Text.UTF8Encoding]::new($false)
    )
}

$abortHighCost = $false
$junitPath = Join-Path $junitRoot 'm11-e2e.xml'

if ($env:MATERIALSAGENT_PHASE1A_SCAN_COVERAGE_PROBE -eq '1') {
    try {
        Write-Output (Test-AcceptanceAllowlistCoverage)
        exit 0
    }
    catch {
        $script:failed++
        Write-SafeLog `
            -RelativePath 'logs/scan-coverage-probe.log' `
            -Content ($_ | Out-String)
        Write-AcceptanceMetadata `
            -Marker 'PHASE_1A_SCAN_COVERAGE_PROBE_FAILED'
        Write-Output 'ACCEPTANCE_SCAN_COVERAGE_FAILED'
        Write-Output "summary=$runRelative/summary.json"
        exit 1
    }
}

try {
    $coverageValidation = Invoke-RecordedAction `
        -Name 'acceptance_scan_coverage_validation' `
        -LogRelative 'logs/00-scan-coverage-validation.log' `
        -Required `
        -Action {
            Test-AcceptanceAllowlistCoverage
        }
    if ($coverageValidation.ExitCode -ne 0) {
        $abortHighCost = $true
    }

    $gitBranchResult = Invoke-RecordedCommand `
        -Name 'git_branch' `
        -FilePath 'git' `
        -Arguments @('branch', '--show-current') `
        -LogRelative 'logs/01-git-branch.log' `
        -Required
    $gitHeadResult = Invoke-RecordedCommand `
        -Name 'git_head' `
        -FilePath 'git' `
        -Arguments @('rev-parse', 'HEAD') `
        -LogRelative 'logs/01-git-head.log' `
        -Required
    $gitSubjectResult = Invoke-RecordedCommand `
        -Name 'git_subject' `
        -FilePath 'git' `
        -Arguments @('log', '-1', '--format=%s') `
        -LogRelative 'logs/01-git-subject.log' `
        -Required
    $gitParentResult = Invoke-RecordedCommand `
        -Name 'git_parent' `
        -FilePath 'git' `
        -Arguments @('rev-parse', 'HEAD^') `
        -LogRelative 'logs/01-git-parent.log' `
        -Required
    $null = Invoke-RecordedCommand `
        -Name 'git_status' `
        -FilePath 'git' `
        -Arguments @('status', '--short', '--branch') `
        -LogRelative 'logs/01-git-status.log' `
        -Required
    $gitStagingResult = Invoke-RecordedCommand `
        -Name 'git_staging_empty' `
        -FilePath 'git' `
        -Arguments @('diff', '--cached', '--quiet') `
        -LogRelative 'logs/01-git-staging-empty.log' `
        -Required
    $gitAncestorResult = Invoke-RecordedCommand `
        -Name 'git_m11a_ancestor' `
        -FilePath 'git' `
        -Arguments @(
            'merge-base',
            '--is-ancestor',
            $m11aBaseline,
            'HEAD'
        ) `
        -LogRelative 'logs/01-git-m11a-ancestor.log' `
        -Required
    $gitBaselineValidation = Invoke-RecordedAction `
        -Name 'git_baseline_validation' `
        -LogRelative 'logs/01-git-baseline-validation.log' `
        -Required `
        -Action {
            Assert-Result ($gitBranchResult.ExitCode -eq 0) `
                'Git branch lookup failed.'
            Assert-Result ($gitHeadResult.ExitCode -eq 0) `
                'Git HEAD lookup failed.'
            Assert-Result ($gitSubjectResult.ExitCode -eq 0) `
                'Git subject lookup failed.'
            Assert-Result ($gitParentResult.ExitCode -eq 0) `
                'Git parent lookup failed.'
            $head = Get-SafeSingleLine -Value $gitHeadResult.Output
            $subject = Get-SafeSingleLine -Value $gitSubjectResult.Output
            $parent = Get-SafeSingleLine -Value $gitParentResult.Output
            $branchLines = @(
                Get-SafeOutputLines -Value $gitBranchResult.Output
            )
            Assert-Result ($branchLines.Count -le 1) `
                'Git branch output is invalid.'
            $branch = ''
            if ($branchLines.Count -eq 1) {
                $branch = $branchLines[0]
            }
            $script:gitEvidence.branch = $branch
            $script:gitEvidence.head = $head
            $script:gitEvidence.subject = $subject
            $script:gitEvidence.parent = $parent
            $script:gitEvidence.staging_empty = (
                $gitStagingResult.ExitCode -eq 0
            )
            $script:gitEvidence.m11a_is_ancestor = (
                $gitAncestorResult.ExitCode -eq 0
            )
            $branchForValidation = $branch
            if (
                $env:MATERIALSAGENT_PHASE1A_GIT_BASELINE_PROBE -eq
                'wrong_branch'
            ) {
                $branchForValidation = 'controlled-probe-branch'
            }
            Assert-Result ($branchForValidation -ceq 'main') `
                'Acceptance runner requires branch main.'
            Assert-Result ($gitStagingResult.ExitCode -eq 0) `
                'Acceptance runner requires an empty staging area.'
            Assert-Result ($gitAncestorResult.ExitCode -eq 0) `
                'M11-A acceptance commit is not an ancestor of HEAD.'
            $script:gitEvidence.baseline_valid = $true
            'GIT_BASELINE_OK'
        }
    if ($gitBaselineValidation.ExitCode -ne 0) {
        $abortHighCost = $true
    }

    if (-not $abortHighCost) {
        $nodeVersionResult = Invoke-RecordedCommand `
            -Name 'node_version' `
            -FilePath 'node' `
            -Arguments @('--version') `
            -LogRelative 'logs/02-node-version.log' `
            -Required
        $npmVersionResult = Invoke-RecordedCommand `
            -Name 'npm_version' `
            -FilePath 'npm.cmd' `
            -Arguments @('--version') `
            -LogRelative 'logs/02-npm-version.log' `
            -Required
        $composeVersionResult = Invoke-RecordedCommand `
            -Name 'docker_compose_version' `
            -FilePath 'docker' `
            -Arguments @('compose', 'version') `
            -LogRelative 'logs/02-docker-compose-version.log' `
            -Required
        $composeServicesResult = Invoke-RecordedCommand `
            -Name 'docker_compose_config_services' `
            -FilePath 'docker' `
            -Arguments @('compose', 'config', '--services') `
            -LogRelative 'logs/02-docker-compose-services.log' `
            -Required
        $composeImagesResult = Invoke-RecordedCommand `
            -Name 'docker_compose_config_images' `
            -FilePath 'docker' `
            -Arguments @('compose', 'config', '--images') `
            -LogRelative 'logs/02-docker-compose-images.log' `
            -Required
        $environmentValidation = Invoke-RecordedAction `
            -Name 'environment_evidence_validation' `
            -LogRelative 'logs/02-environment-evidence-validation.log' `
            -Required `
            -Action {
                Assert-Result ($nodeVersionResult.ExitCode -eq 0) `
                    'Node version command failed.'
                Assert-Result ($npmVersionResult.ExitCode -eq 0) `
                    'npm version command failed.'
                Assert-Result ($composeVersionResult.ExitCode -eq 0) `
                    'Docker Compose version command failed.'
                Assert-Result ($composeServicesResult.ExitCode -eq 0) `
                    'Docker Compose service config command failed.'
                Assert-Result ($composeImagesResult.ExitCode -eq 0) `
                    'Docker Compose image config command failed.'
                $nodeLine = Get-SafeSingleLine -Value (
                    $nodeVersionResult.Output
                )
                $npmLine = Get-SafeSingleLine -Value (
                    $npmVersionResult.Output
                )
                $composeLine = Get-SafeSingleLine -Value (
                    $composeVersionResult.Output
                )
                Assert-Result ($nodeLine -match '^v(\d+\.\d+\.\d+)$') `
                    'Node version output is invalid.'
                $script:environmentEvidence.node = $Matches[1]
                Assert-Result ($npmLine -match '^(\d+\.\d+\.\d+)$') `
                    'npm version output is invalid.'
                $script:environmentEvidence.npm = $Matches[1]
                Assert-Result (
                    $composeLine -match (
                        '^Docker Compose version v?(\d+\.\d+\.\d+)$'
                    )
                ) 'Docker Compose version output is invalid.'
                $script:environmentEvidence.docker_compose = $Matches[1]
                $services = @(
                    Get-SafeOutputLines -Value $composeServicesResult.Output
                )
                $images = @(
                    Get-SafeOutputLines -Value $composeImagesResult.Output
                )
                Assert-Result (
                    Test-ExactSet `
                        -Actual $services `
                        -Expected $expectedComposeServices
                ) 'Docker Compose services do not match the fixed config.'
                Assert-Result (
                    Test-ExactSet `
                        -Actual $images `
                        -Expected $expectedComposeImages
                ) 'Docker Compose images do not match the fixed config.'
                $script:environmentEvidence.compose_services = @(
                    $services | Sort-Object
                )
                $script:environmentEvidence.compose_images = @(
                    $images | Sort-Object
                )
                'ENVIRONMENT_EVIDENCE_PRESTART_OK'
            }
        if ($environmentValidation.ExitCode -ne 0) {
            $abortHighCost = $true
        }
    }

    if (-not $abortHighCost) {
        $null = Invoke-RecordedCommand `
            -Name 'scope_m11_pre' `
            -FilePath $powershellExe `
            -Arguments @(
                '-NoProfile',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                $scopeScript,
                '-Milestone',
                'M11'
            ) `
            -LogRelative 'logs/03-scope-m11-pre.log' `
            -Required
        $null = Invoke-RecordedCommand `
            -Name 'sem_integrity_pre' `
            -FilePath $powershellExe `
            -Arguments @(
                '-NoProfile',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                $semScript
            ) `
            -LogRelative 'logs/03-sem-integrity-pre.log' `
            -Required

        $stateExistedBeforeStart = Test-Path -LiteralPath $statePath
        $startResult = Invoke-RecordedCommand `
            -Name 'mock_stack_start_or_reuse' `
            -FilePath $powershellExe `
            -Arguments @(
                '-NoProfile',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                $startScript
            ) `
            -LogRelative 'logs/04-mock-stack-start.log' `
            -Required
        if (
            $startResult.ExitCode -eq 0 -and
            $startResult.Output -match '(?m)^MOCK_STACK_ALREADY_RUNNING\s*$'
        ) {
            $script:stackAvailable = $true
        }
        elseif (
            $startResult.ExitCode -eq 0 -and
            $startResult.Output -match '(?m)^MOCK_STACK_STARTED\s*$'
        ) {
            $script:startedByRunner = $true
            $script:stackAvailable = $true
        }
        else {
            $script:startedByRunner = -not $stateExistedBeforeStart
            if ($startResult.ExitCode -eq 0) {
                $script:failed++
            }
            $abortHighCost = $true
        }
    }

    if ($script:stackAvailable) {
        try {
            $state = Get-Content `
                -LiteralPath $statePath `
                -Raw `
                -Encoding UTF8 |
                    ConvertFrom-Json
            $candidatePython = [string]$state.python_executable
            Assert-Result (
                Test-Path -LiteralPath $candidatePython -PathType Leaf
            ) 'Mock Stack state has no valid Python executable.'
            $script:pythonExe = $candidatePython
        }
        catch {
            $script:failed++
            $abortHighCost = $true
            Write-SafeLog `
                -RelativePath 'logs/04-state-validation.log' `
                -Content ($_ | Out-String)
        }
    }
    else {
        $abortHighCost = $true
    }

    if ($script:stackAvailable -and $null -ne $script:pythonExe) {
        $pythonVersionResult = Invoke-RecordedCommand `
            -Name 'python_version' `
            -FilePath $script:pythonExe `
            -Arguments @('--version') `
            -LogRelative 'logs/04-python-version.log' `
            -Required
        $pythonVersionValidation = Invoke-RecordedAction `
            -Name 'python_version_validation' `
            -LogRelative 'logs/04-python-version-validation.log' `
            -Required `
            -Action {
                Assert-Result ($pythonVersionResult.ExitCode -eq 0) `
                    'Python version command failed.'
                $pythonLine = Get-SafeSingleLine -Value (
                    $pythonVersionResult.Output
                )
                Assert-Result (
                    $pythonLine -match '^Python (\d+\.\d+\.\d+)$'
                ) 'Python version output is invalid.'
                $script:environmentEvidence.python = $Matches[1]
                'PYTHON_VERSION_EVIDENCE_OK'
            }
        if ($pythonVersionValidation.ExitCode -ne 0) {
            $abortHighCost = $true
        }
        elseif (
            $env:MATERIALSAGENT_PHASE1A_VERSION_EVIDENCE_PROBE -eq '1'
        ) {
            $script:versionEvidenceProbeCompleted = $true
            $abortHighCost = $true
        }
    }

    if ($env:MATERIALSAGENT_PHASE1A_FAILURE_PROBE -eq '1') {
        $failureProbe = Invoke-RecordedAction `
            -Name 'controlled_failure_probe' `
            -LogRelative 'logs/04-controlled-failure-probe.log' `
            -Required `
            -Action {
                throw 'Controlled acceptance-runner failure probe.'
            }
        $null = $failureProbe
        $abortHighCost = $true
    }

    if (-not $abortHighCost) {
        $runtimeToken = ''
        if ($dotEnv.ContainsKey('ZTA35G_RUNTIME_TOKEN')) {
            $runtimeToken = [string]$dotEnv['ZTA35G_RUNTIME_TOKEN']
        }
        $null = Invoke-RecordedAction `
            -Name 'runtime_health' `
            -LogRelative 'logs/05-runtime-health.log' `
            -Required `
            -Action {
                Assert-Result ($runtimeToken.Length -gt 0) `
                    'Runtime token is not configured.'
                $headers = @{ 'X-ZTA35G-Runtime-Token' = $runtimeToken }
                $live = Invoke-RestMethod `
                    -Uri 'http://127.0.0.1:8100/internal/v1/health/live' `
                    -Headers $headers `
                    -TimeoutSec 5
                $ready = Invoke-RestMethod `
                    -Uri 'http://127.0.0.1:8100/internal/v1/health/ready' `
                    -Headers $headers `
                    -TimeoutSec 5
                Assert-Result ($live.status -eq 'LIVE') `
                    'Runtime live status mismatch.'
                Assert-Result ($ready.status -eq 'READY') `
                    'Runtime ready status mismatch.'
                Assert-Result ($ready.model_bundle_id -eq 'mock-zta35g-bundle') `
                    'Runtime bundle identity mismatch.'
                'RUNTIME_HEALTH_OK bundle=mock-zta35g-bundle'
            }
        $null = Invoke-RecordedAction `
            -Name 'backend_health' `
            -LogRelative 'logs/06-backend-health.log' `
            -Required `
            -Action {
                $live = Invoke-RestMethod `
                    -Uri 'http://127.0.0.1:8000/api/v1/health/live' `
                    -TimeoutSec 5
                $ready = Invoke-RestMethod `
                    -Uri 'http://127.0.0.1:8000/api/v1/health/ready' `
                    -TimeoutSec 5
                $components = @{}
                foreach ($component in @($ready.components)) {
                    $components[[string]$component.name] = (
                        [string]$component.status
                    )
                }
                Assert-Result ($live.status -eq 'LIVE') `
                    'Backend live status mismatch.'
                Assert-Result ($ready.status -eq 'READY') `
                    'Backend ready status mismatch.'
                Assert-Result ($components['postgresql'] -eq 'AVAILABLE') `
                    'PostgreSQL readiness mismatch.'
                Assert-Result ($components['object_storage'] -eq 'AVAILABLE') `
                    'Object-storage readiness mismatch.'
                'BACKEND_HEALTH_OK'
            }
        $null = Invoke-RecordedAction `
            -Name 'frontend_root' `
            -LogRelative 'logs/07-frontend-root.log' `
            -Required `
            -Action {
                $response = Invoke-WebRequest `
                    -UseBasicParsing `
                    -Uri 'http://127.0.0.1:3000/' `
                    -TimeoutSec 5
                Assert-Result ($response.StatusCode -eq 200) `
                    'Frontend root status mismatch.'
                'FRONTEND_ROOT_OK'
            }
        $null = Invoke-RecordedAction `
            -Name 'frontend_proxy' `
            -LogRelative 'logs/08-frontend-proxy.log' `
            -Required `
            -Action {
                $response = Invoke-RestMethod `
                    -Uri 'http://127.0.0.1:3000/api/v1/health/live' `
                    -TimeoutSec 5
                Assert-Result ($response.status -eq 'LIVE') `
                    'Frontend proxy status mismatch.'
                'FRONTEND_PROXY_OK'
            }

        $headsResult = Invoke-RecordedCommand `
            -Name 'alembic_heads' `
            -FilePath $script:pythonExe `
            -Arguments @('-m', 'alembic', '-c', 'backend/alembic.ini', 'heads') `
            -LogRelative 'logs/09-alembic-heads.log' `
            -Required
        $currentResult = Invoke-RecordedCommand `
            -Name 'alembic_current' `
            -FilePath $script:pythonExe `
            -Arguments @('-m', 'alembic', '-c', 'backend/alembic.ini', 'current') `
            -LogRelative 'logs/09-alembic-current.log' `
            -Required
        $checkResult = Invoke-RecordedCommand `
            -Name 'alembic_check' `
            -FilePath $script:pythonExe `
            -Arguments @('-m', 'alembic', '-c', 'backend/alembic.ini', 'check') `
            -LogRelative 'logs/09-alembic-check.log' `
            -Required
        $null = Invoke-RecordedAction `
            -Name 'alembic_revision_consistency' `
            -LogRelative 'logs/09-alembic-revision-consistency.log' `
            -Required `
            -Action {
                Assert-Result ($headsResult.ExitCode -eq 0) `
                    'Alembic heads command failed.'
                Assert-Result ($currentResult.ExitCode -eq 0) `
                    'Alembic current command failed.'
                Assert-Result ($checkResult.ExitCode -eq 0) `
                    'Alembic check command failed.'
                $revisionPattern = (
                    '(?m)^([0-9a-z_]+) \(head\)\s*$'
                )
                $headMatches = [regex]::Matches(
                    $headsResult.Output,
                    $revisionPattern
                )
                $currentMatches = [regex]::Matches(
                    $currentResult.Output,
                    $revisionPattern
                )
                Assert-Result ($headMatches.Count -eq 1) `
                    'Alembic must have exactly one head.'
                Assert-Result ($currentMatches.Count -eq 1) `
                    'Database must have exactly one current head.'
                $headRevision = $headMatches[0].Groups[1].Value
                $currentRevision = $currentMatches[0].Groups[1].Value
                Assert-Result (
                    $headRevision -eq '0009_timeline_query_indexes'
                ) 'Unexpected Alembic head.'
                Assert-Result ($currentRevision -eq $headRevision) `
                    'Database current revision does not match the head.'
                Assert-Result (
                    $checkResult.Output -match (
                        'No new upgrade operations detected\.'
                    )
                ) 'Alembic check did not confirm a clean model.'
                'ALEMBIC_REVISION_CONSISTENCY_OK'
            }

        $e2eResult = Invoke-RecordedCommand `
            -Name 'backend_m11_e2e' `
            -FilePath $script:pythonExe `
            -Arguments @(
                '-m',
                'pytest',
                'backend/tests/e2e/test_mock_journey.py',
                'backend/tests/e2e/test_mock_acceptance_matrix.py',
                "--junitxml=$junitPath",
                '-q'
            ) `
            -LogRelative 'logs/10-backend-m11-e2e.log' `
            -Required
        if ($e2eResult.ExitCode -eq 0) {
            $countResult = Invoke-RecordedAction `
                -Name 'm11_scenario_count' `
                -LogRelative 'logs/10-m11-scenario-count.log' `
                -Required `
                -Action {
                    $counts = Get-ScenarioCounts -JunitPath $junitPath
                    foreach ($key in $counts.Keys) {
                        $script:scenarioCounts[$key] = [int]$counts[$key]
                    }
                    'M11_SCENARIO_COUNT_OK'
                }
            if ($countResult.ExitCode -ne 0) {
                $abortHighCost = $true
            }
        }
        else {
            $abortHighCost = $true
        }

        if (-not $abortHighCost) {
            $null = Invoke-RecordedCommand `
                -Name 'backend_full' `
                -FilePath $script:pythonExe `
                -Arguments @('-m', 'pytest', 'backend/tests', '-q') `
                -LogRelative 'logs/11-backend-test.log' `
                -Required
            $null = Invoke-RecordedCommand `
                -Name 'mock_runtime_tests' `
                -FilePath $script:pythonExe `
                -Arguments @('-m', 'pytest', 'mock-runtime/tests', '-q') `
                -LogRelative 'logs/12-runtime-test.log' `
                -Required
            $null = Invoke-RecordedCommand `
                -Name 'pip_check' `
                -FilePath $script:pythonExe `
                -Arguments @('-m', 'pip', 'check') `
                -LogRelative 'logs/13-pip-check.log' `
                -Required
            $null = Invoke-RecordedCommand `
                -Name 'compileall' `
                -FilePath $script:pythonExe `
                -Arguments @(
                    '-m',
                    'compileall',
                    '-q',
                    'backend/src',
                    'mock-runtime/src'
                ) `
                -LogRelative 'logs/14-compileall.log' `
                -Required
            $null = Invoke-RecordedCommand `
                -Name 'frontend_vitest' `
                -FilePath 'npm.cmd' `
                -Arguments @(
                    '--prefix',
                    'frontend',
                    'run',
                    'test',
                    '--',
                    '--run'
                ) `
                -LogRelative 'logs/15-frontend-test.log' `
                -Required
            $null = Invoke-RecordedCommand `
                -Name 'frontend_typecheck' `
                -FilePath 'npm.cmd' `
                -Arguments @('--prefix', 'frontend', 'run', 'typecheck') `
                -LogRelative 'logs/16-frontend-typecheck.log' `
                -Required
            $null = Invoke-RecordedCommand `
                -Name 'frontend_build' `
                -FilePath 'npm.cmd' `
                -Arguments @('--prefix', 'frontend', 'run', 'build') `
                -LogRelative 'logs/17-frontend-build.log' `
                -Required
        }
    }
}
catch {
    $script:failed++
    Write-SafeLog `
        -RelativePath 'logs/runner-unhandled-error.log' `
        -Content ($_ | Out-String)
}
finally {
    $null = Invoke-RecordedCommand `
        -Name 'scope_m11_post' `
        -FilePath $powershellExe `
        -Arguments @(
            '-NoProfile',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            $scopeScript,
            '-Milestone',
            'M11'
        ) `
        -LogRelative 'logs/18-scope-m11-post.log' `
        -Required
    $null = Invoke-RecordedCommand `
        -Name 'scope_m11b_post' `
        -FilePath $powershellExe `
        -Arguments @(
            '-NoProfile',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            $scopeScript,
            '-Milestone',
            'M11B'
        ) `
        -LogRelative 'logs/18-scope-m11b-post.log' `
        -Required
    $null = Invoke-RecordedCommand `
        -Name 'sem_integrity_post' `
        -FilePath $powershellExe `
        -Arguments @(
            '-NoProfile',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            $semScript
        ) `
        -LogRelative 'logs/19-sem-integrity-post.log' `
        -Required
    $null = Invoke-RecordedCommand `
        -Name 'git_diff_check' `
        -FilePath 'git' `
        -Arguments @('diff', '--check') `
        -LogRelative 'logs/20-git-diff-check.log' `
        -Required
    $null = Invoke-RecordedCommand `
        -Name 'git_cached_diff_check' `
        -FilePath 'git' `
        -Arguments @('diff', '--cached', '--check') `
        -LogRelative 'logs/20-git-cached-diff-check.log' `
        -Required
    $null = Invoke-RecordedCommand `
        -Name 'git_staging_empty_post' `
        -FilePath 'git' `
        -Arguments @('diff', '--cached', '--quiet') `
        -LogRelative 'logs/20-git-staging-empty-post.log' `
        -Required
    $null = Invoke-RecordedCommand `
        -Name 'git_status_post' `
        -FilePath 'git' `
        -Arguments @('status', '--short') `
        -LogRelative 'logs/20-git-status-post.log' `
        -Required

    if ($script:startedByRunner) {
        $null = Invoke-RecordedCommand `
            -Name 'mock_stack_stop' `
            -FilePath $powershellExe `
            -Arguments @(
                '-NoProfile',
                '-ExecutionPolicy',
                'Bypass',
                '-File',
                $stopScript
            ) `
            -LogRelative 'logs/23-mock-stack-stop.log' `
            -Required
    }

    $null = Invoke-RecordedAction `
        -Name 'acceptance_security_scan' `
        -LogRelative 'logs/21-security-scan.log' `
        -Required `
        -Action {
            Test-AcceptanceArtifacts
        }

    if ($ManualBrowserAccepted) {
        $script:scenarioCounts.browser_manual_scenarios = 11
    }
    $marker = Get-AcceptanceMarker `
        -AutomationPassed ($script:failed -eq 0) `
        -ManualAccepted ([bool]$ManualBrowserAccepted)
    if (
        $script:versionEvidenceProbeCompleted -and
        $script:failed -eq 0
    ) {
        $marker = 'PHASE_1A_VERSION_EVIDENCE_PROBE_PASSED'
    }
    Write-AcceptanceMetadata -Marker $marker
    try {
        $null = Test-AcceptanceArtifacts
        Assert-Result (
            $script:securityScanEvidence.dynamic_artifacts_scanned
        ) 'Acceptance dynamic artifacts were not fully scanned.'
        Write-AcceptanceMetadata -Marker $marker
        $null = Test-AcceptanceArtifacts
    }
    catch {
        $script:failed++
        Write-SafeLog `
            -RelativePath 'logs/21-final-artifact-scan.log' `
            -Content ($_ | Out-String)
        $marker = Get-AcceptanceMarker `
            -AutomationPassed $false `
            -ManualAccepted ([bool]$ManualBrowserAccepted)
        Write-AcceptanceMetadata -Marker $marker
    }

    Write-Output $marker
    Write-Output "run_id=$runId"
    Write-Output (
        'success_scenarios={0}' -f
        $script:scenarioCounts.success_scenarios
    )
    Write-Output (
        'input_error_scenarios={0}' -f
        $script:scenarioCounts.input_error_scenarios
    )
    Write-Output (
        'dependency_failure_scenarios={0}' -f
        $script:scenarioCounts.dependency_failure_scenarios
    )
    Write-Output (
        'idempotency_scenarios={0}' -f
        $script:scenarioCounts.idempotency_scenarios
    )
    Write-Output (
        'retry_scenarios={0}' -f
        $script:scenarioCounts.retry_scenarios
    )
    Write-Output (
        'asset_security_scenarios={0}' -f
        $script:scenarioCounts.asset_security_scenarios
    )
    Write-Output (
        'browser_manual_scenarios={0}' -f
        $script:scenarioCounts.browser_manual_scenarios
    )
    Write-Output "failed=$($script:failed)"
    Write-Output "summary=$runRelative/summary.json"

    if ($script:failed -eq 0) {
        exit 0
    }
    exit 1
}
