[CmdletBinding()]
param(
    [switch]$SelfTest,

    [ValidateSet(
        "StageB",
        "Phase1",
        "Phase2",
        "Phase3",
        "Audit",
        "Cleanup",
        "MockHelperTests",
        "MockFullRegression",
        "MockRegression"
    )]
    [string]$Action
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

$script:PowerShellSelfTestOk = "P1B2_GATE4_POWERSHELL_SELF_TEST_OK"
$script:PowerShellSelfTestFailed = "P1B2_GATE4_POWERSHELL_SELF_TEST_FAILED"
$script:NotImplemented = "P1B2_GATE4_NOT_IMPLEMENTED"
$script:KeyNotAvailable = "P1B2_GATE4_DEEPSEEK_KEY_NOT_AVAILABLE"
$script:RuntimeBudgetExceeded = "P1B2_GATE4_RUNTIME_BUDGET_EXCEEDED"
$script:ProviderBudgetExceeded = "P1B2_GATE4_PROVIDER_BUDGET_EXCEEDED"
$script:MockProviderIsolationFailed =
    "P1B2_GATE4_MOCK_PROVIDER_ISOLATION_FAILED"
$script:StageBFinalAttemptBlocked =
    "P1B2_GATE4_STAGE_B_FINAL_ATTEMPT_BLOCKED"
$script:NewStageCNotAuthorized =
    "P1B2_GATE4_NEW_STAGE_C_NOT_AUTHORIZED"
$script:SecretInCommand = "P1B2_GATE4_SECRET_IN_CHILD_COMMAND"
$script:DriverStage = "ENTRY"

function Assert-SelfTest {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition
    )

    if (-not $Condition) {
        throw "P1B2_GATE4_SELF_TEST_ASSERTION_FAILED"
    }
}

function Test-ContainsControlCharacter {
    param(
        [AllowEmptyString()]
        [string]$Value
    )

    foreach ($character in $Value.ToCharArray()) {
        $codePoint = [int][char]$character
        if (($codePoint -ge 0 -and $codePoint -le 31) -or
            ($codePoint -ge 127 -and $codePoint -le 159)) {
            return $true
        }
    }
    return $false
}

function ConvertFrom-ControlledEnvText {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Text
    )

    foreach ($character in $Text.ToCharArray()) {
        $codePoint = [int][char]$character
        if ((($codePoint -ge 0 -and $codePoint -le 31) -and
             $character -ne "`r" -and $character -ne "`n") -or
            ($codePoint -ge 127 -and $codePoint -le 159)) {
            throw $script:KeyNotAvailable
        }
    }

    $parsed = @{}
    foreach ($line in ($Text -split "\r?\n")) {
        if ($line.Length -eq 0 -or $line.StartsWith("#")) {
            continue
        }
        if ((Test-ContainsControlCharacter -Value $line) -or
            -not $line.Contains("=")) {
            throw $script:KeyNotAvailable
        }

        $separator = $line.IndexOf("=")
        $name = $line.Substring(0, $separator)
        $value = $line.Substring($separator + 1)
        if ($name -notmatch "\A[A-Za-z_][A-Za-z0-9_]*\z" -or
            $parsed.ContainsKey($name) -or
            (Test-ContainsControlCharacter -Value $value)) {
            throw $script:KeyNotAvailable
        }
        $parsed[$name] = $value
    }

    if (-not $parsed.ContainsKey("DEEPSEEK_API_KEY")) {
        throw $script:KeyNotAvailable
    }
    $providerKey = [string]$parsed["DEEPSEEK_API_KEY"]
    if ($providerKey.Length -eq 0 -or
        $providerKey -ne $providerKey.Trim() -or
        (Test-ContainsControlCharacter -Value $providerKey)) {
        throw $script:KeyNotAvailable
    }
    return $parsed
}

function Assert-FixedFailure {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Marker,

        [Parameter(Mandatory = $true)]
        [scriptblock]$Operation
    )

    $caught = $false
    try {
        & $Operation | Out-Null
    }
    catch {
        $caught = $true
        Assert-SelfTest -Condition ($_.Exception.Message -eq $Marker)
    }
    Assert-SelfTest -Condition $caught
}

function New-InvalidProviderKey {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RunId,

        [Parameter(Mandatory = $true)]
        [string]$RealProviderKey
    )

    if ($RealProviderKey.Length -eq 0 -or
        $RealProviderKey -ne $RealProviderKey.Trim() -or
        (Test-ContainsControlCharacter -Value $RealProviderKey)) {
        throw $script:KeyNotAvailable
    }

    $safeRunId = [regex]::Replace($RunId, "[^A-Za-z0-9_-]", "-").Trim("-")
    if ($safeRunId.Length -eq 0) {
        $safeRunId = "run"
    }

    do {
        $bytes = New-Object byte[] 16
        $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $random.GetBytes($bytes)
        }
        finally {
            $random.Dispose()
        }
        $suffix = ([System.BitConverter]::ToString($bytes)).Replace("-", "").ToLowerInvariant()
        $candidate = "sk-invalid-p1b2-{0}-{1}" -f $safeRunId, $suffix
    } while ($candidate -eq $RealProviderKey -or $candidate.Contains($RealProviderKey))

    return $candidate
}

function Get-ControlledValue {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Controlled,

        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if (-not $Controlled.ContainsKey($Name)) {
        throw "P1B2_GATE4_INVALID_CONTROLLED_VALUE"
    }
    $value = [string]$Controlled[$Name]
    if ($value.Length -eq 0 -or
        $value -ne $value.Trim() -or
        (Test-ContainsControlCharacter -Value $value)) {
        throw "P1B2_GATE4_INVALID_CONTROLLED_VALUE"
    }
    return $value
}

function New-RoleEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet(
            "runtime",
            "backend-real",
            "backend-invalid",
            "frontend",
            "compose",
            "database",
            "minio",
            "log"
        )]
        [string]$Role,

        [Parameter(Mandatory = $true)]
        [hashtable]$BaseEnvironment,

        [Parameter(Mandatory = $true)]
        [hashtable]$Controlled
    )

    $child = @{}
    if ($BaseEnvironment.ContainsKey("PATH")) {
        $child["PATH"] = [string]$BaseEnvironment["PATH"]
    }

    switch ($Role) {
        "runtime" {
            $child["ZTA35G_RUNTIME_TOKEN"] =
                Get-ControlledValue -Controlled $Controlled -Name "runtime_token"
        }
        "backend-real" {
            $child["DEEPSEEK_API_KEY"] =
                Get-ControlledValue -Controlled $Controlled -Name "real_provider_key"
            $child["ZTA35G_RUNTIME_TOKEN"] =
                Get-ControlledValue -Controlled $Controlled -Name "runtime_token"
            $child["DATABASE_PASSWORD"] =
                Get-ControlledValue -Controlled $Controlled -Name "database_password"
            $child["MINIO_SECRET_KEY"] =
                Get-ControlledValue -Controlled $Controlled -Name "minio_secret"
            $child["TIMELINE_CURSOR_SIGNING_KEY"] =
                Get-ControlledValue -Controlled $Controlled -Name "timeline_signing_key"
        }
        "backend-invalid" {
            $child["DEEPSEEK_API_KEY"] =
                Get-ControlledValue -Controlled $Controlled -Name "invalid_provider_key"
            $child["ZTA35G_RUNTIME_TOKEN"] =
                Get-ControlledValue -Controlled $Controlled -Name "runtime_token"
            $child["DATABASE_PASSWORD"] =
                Get-ControlledValue -Controlled $Controlled -Name "database_password"
            $child["MINIO_SECRET_KEY"] =
                Get-ControlledValue -Controlled $Controlled -Name "minio_secret"
            $child["TIMELINE_CURSOR_SIGNING_KEY"] =
                Get-ControlledValue -Controlled $Controlled -Name "timeline_signing_key"
        }
        "database" {
            $child["DATABASE_PASSWORD"] =
                Get-ControlledValue -Controlled $Controlled -Name "database_password"
        }
        "minio" {
            $child["MINIO_SECRET_KEY"] =
                Get-ControlledValue -Controlled $Controlled -Name "minio_secret"
        }
    }
    return $child
}

function New-MockChildEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$BaseEnvironment
    )

    $child = @{}
    foreach ($name in (Get-Gate4SystemEnvironmentNames)) {
        if ($BaseEnvironment.ContainsKey($name)) {
            $child[$name] = [string]$BaseEnvironment[$name]
        }
    }
    $child["APP_ENV"] = "local"
    $child["LLM_ADAPTER"] = "mock"
    $child["DEEPSEEK_API_KEY"] = ""
    $child["PYTHONUTF8"] = "1"
    foreach ($name in @(
        "M12B_REAL_CALLS_AUTHORIZED",
        "P1B2_GATE4_AUTHORIZED",
        "P1B2_GATE4_REAL_PROVIDER_AUTHORIZED",
        "P1B2_GATE4_REAL_RUNTIME_AUTHORIZED",
        "P1B2_GATE4_BROWSER_AUTHORIZED",
        "P1B2_GATE4_PROJECT_OWNER_AUTHORIZED"
    )) {
        [void]$child.Remove($name)
    }
    return $child
}

function Assert-NoSecretsInArguments {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,

        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [string[]]$SecretValues
    )

    foreach ($argument in $Arguments) {
        foreach ($secret in $SecretValues) {
            if ($secret.Length -gt 0 -and $argument.Contains($secret)) {
                throw $script:SecretInCommand
            }
        }
    }
}

function Find-ExactSecret {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Text,

        [Parameter(Mandatory = $true)]
        [string[]]$SecretValues
    )

    $hits = New-Object System.Collections.ArrayList
    for ($index = 0; $index -lt $SecretValues.Count; $index++) {
        $secret = $SecretValues[$index]
        if ($secret.Length -gt 0 -and $Text.Contains($secret)) {
            [void]$hits.Add(@{
                kind = "EXACT_VALUE"
                secret_index = $index
            })
        }
    }
    return ,$hits
}

function Invoke-RuntimeBudgeted {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Ledger,

        [Parameter(Mandatory = $true)]
        [ValidateSet("runtime", "ddpm")]
        [string]$Kind,

        [Parameter(Mandatory = $true)]
        [scriptblock]$Delegate
    )

    if ([int]$Ledger[$Kind] -ge 2) {
        throw $script:RuntimeBudgetExceeded
    }
    $Ledger[$Kind] = [int]$Ledger[$Kind] + 1
    & $Delegate | Out-Null
}

function Invoke-ProviderBudgeted {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Ledger,

        [Parameter(Mandatory = $true)]
        [ValidateSet("chat", "explanation")]
        [string]$Kind,

        [Parameter(Mandatory = $true)]
        [scriptblock]$Delegate
    )

    $kindLimit = if ($Kind -eq "chat") { 3 } else { 2 }
    $total = [int]$Ledger["chat"] + [int]$Ledger["explanation"]
    if ($total -ge 5 -or [int]$Ledger[$Kind] -ge $kindLimit) {
        throw $script:ProviderBudgetExceeded
    }
    $Ledger[$Kind] = [int]$Ledger[$Kind] + 1
    & $Delegate | Out-Null
}

function Invoke-IdempotentCleanup {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Cleanup
    )

    if ([bool]$Cleanup["complete"]) {
        return
    }
    $actions = [System.Collections.ArrayList]$Cleanup["actions"]
    for ($index = $actions.Count - 1; $index -ge 0; $index--) {
        & ([hashtable]$actions[$index])["action"] | Out-Null
    }
    $Cleanup["complete"] = $true
}

function New-SafeSummary {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Passed,

        [Parameter(Mandatory = $true)]
        [bool]$CleanupComplete
    )

    if (-not $CleanupComplete) {
        return @{ status = "P1B2_GATE4_CLEANUP_INCOMPLETE" }
    }
    if (-not $Passed) {
        return @{ status = "P1B2_GATE4_BLOCKED" }
    }
    return @{ status = "P1B2_GATE4_COMPLETE_AWAITING_PROJECT_OWNER_REVIEW" }
}

function ConvertTo-QuotedProcessArgument {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    return '"{0}"' -f $Value.Replace("\", "\\").Replace('"', '\"')
}

function Invoke-PythonExecutorSelfTest {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$SecretValues
    )

    $python = Get-Command python -CommandType Application -ErrorAction Stop |
        Select-Object -First 1
    $executorPath = Join-Path -Path $PSScriptRoot -ChildPath "run-phase-1b.py"
    if (-not [System.IO.File]::Exists($executorPath)) {
        throw "P1B2_GATE4_EXECUTOR_NOT_AVAILABLE"
    }

    $logicalArguments = @($executorPath, "--self-test")
    Assert-NoSecretsInArguments -Arguments $logicalArguments -SecretValues $SecretValues

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $python.Source
    $startInfo.Arguments =
        (ConvertTo-QuotedProcessArgument -Value $executorPath) + " --self-test"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables.Clear()
    if ($null -ne $env:PATH) {
        $startInfo.EnvironmentVariables["PATH"] = $env:PATH
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "P1B2_GATE4_EXECUTOR_START_FAILED"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(1200000)) {
            $killInfo = New-Object System.Diagnostics.ProcessStartInfo
            $killInfo.FileName = "taskkill.exe"
            $killInfo.Arguments = "/PID {0} /T /F" -f $process.Id
            $killInfo.UseShellExecute = $false
            $killInfo.CreateNoWindow = $true
            $killInfo.RedirectStandardOutput = $true
            $killInfo.RedirectStandardError = $true
            $killer = New-Object System.Diagnostics.Process
            $killer.StartInfo = $killInfo
            try {
                if (-not $killer.Start()) {
                    throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
                }
                [void]$killer.StandardOutput.ReadToEndAsync()
                [void]$killer.StandardError.ReadToEndAsync()
                if (-not $killer.WaitForExit(30000)) {
                    throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
                }
                if (
                    $killer.ExitCode -ne 0 -and
                    -not $process.HasExited
                ) {
                    throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
                }
            }
            finally {
                $killer.Dispose()
            }
            if (-not $process.WaitForExit(30000)) {
                throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
            }
            throw "P1B2_GATE4_STAGE_B_WATCHDOG_EXCEEDED"
        }
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0 -or
            $stderr.Length -ne 0 -or
            $stdout.Trim() -ne "P1B2_GATE4_EXECUTOR_SELF_TEST_OK") {
            throw "P1B2_GATE4_EXECUTOR_SELF_TEST_FAILED"
        }
        Assert-SelfTest -Condition (
            (Find-ExactSecret -Text ($stdout + $stderr) -SecretValues $SecretValues).Count -eq 0
        )
        [Console]::Out.WriteLine("P1B2_GATE4_EXECUTOR_SELF_TEST_OK")
    }
    finally {
        $process.Dispose()
    }
}

function Get-Gate4ExecutorPython {
    $candidates = New-Object System.Collections.ArrayList
    if (-not [string]::IsNullOrWhiteSpace($env:CONDA_PREFIX)) {
        [void]$candidates.Add((Join-Path $env:CONDA_PREFIX "python.exe"))
    }
    [void]$candidates.Add(
        "D:\ProgramData\Anaconda3\envs\materialsagent-backend\python.exe"
    )
    $discovered = Get-Command python -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $discovered) {
        [void]$candidates.Add($discovered.Source)
    }
    foreach ($candidate in $candidates) {
        if ([System.IO.File]::Exists($candidate)) {
            return [System.IO.Path]::GetFullPath($candidate)
        }
    }
    throw "P1B2_GATE4_EXECUTOR_NOT_AVAILABLE"
}

function Invoke-MockExecutor {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet("--mock-helper-tests", "--mock-full-regression")]
        [string]$ExecutorAction,

        [Parameter(Mandatory = $true)]
        [int]$TimeoutMilliseconds,

        [Parameter(Mandatory = $true)]
        [string]$SuccessMarker
    )

    $python = Get-Gate4ExecutorPython
    $executorPath = Join-Path -Path $PSScriptRoot -ChildPath "run-phase-1b.py"
    if (-not [System.IO.File]::Exists($executorPath)) {
        throw "P1B2_GATE4_EXECUTOR_NOT_AVAILABLE"
    }
    $logicalArguments = @($executorPath, $ExecutorAction)
    Assert-NoSecretsInArguments -Arguments $logicalArguments -SecretValues @()

    $base = @{}
    foreach ($name in (Get-Gate4SystemEnvironmentNames)) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if ($null -ne $value) {
            $base[$name] = $value
        }
    }
    $base["LLM_ADAPTER"] = "deepseek"
    $base["DEEPSEEK_API_KEY"] = "must-not-enter-mock-child"
    $environment = New-MockChildEnvironment -BaseEnvironment $base

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $python
    $startInfo.Arguments =
        (ConvertTo-QuotedProcessArgument -Value $executorPath) +
        " " + $ExecutorAction
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables.Clear()
    foreach ($entry in $environment.GetEnumerator()) {
        $startInfo.EnvironmentVariables[[string]$entry.Key] =
            [string]$entry.Value
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "P1B2_GATE4_EXECUTOR_START_FAILED"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($TimeoutMilliseconds)) {
            [void](Start-Process -FilePath "taskkill.exe" `
                -ArgumentList @("/PID", [string]$process.Id, "/T", "/F") `
                -WindowStyle Hidden -Wait -PassThru)
            throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
        }
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            foreach ($line in ($stderr -split "\r?\n")) {
                if (
                    $line -match (
                        '^P1B2_GATE4_MOCK_FULL_FAILURE_' +
                        '[A-Z]+=[A-Za-z0-9_.,:\\-]+$'
                    )
                ) {
                    [Console]::Error.WriteLine($line)
                }
            }
            if ($stderr.Trim() -eq $script:MockProviderIsolationFailed) {
                [Console]::Error.WriteLine($script:MockProviderIsolationFailed)
            }
            throw $script:MockProviderIsolationFailed
        }
        if ($stderr.Length -ne 0) {
            throw "P1B2_GATE4_UNEXPECTED_EXECUTOR_STDERR"
        }
        if (-not $stdout.Contains($SuccessMarker)) {
            throw "P1B2_GATE4_UNEXPECTED_EXECUTOR_STDOUT"
        }
        [Console]::Out.Write($stdout)
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-MockHelperTestsExecutor {
    Invoke-MockExecutor `
        -ExecutorAction "--mock-helper-tests" `
        -TimeoutMilliseconds 600000 `
        -SuccessMarker "P1B2_GATE4_MOCK_HELPER_TESTS_OK"
}

function Invoke-MockFullRegressionExecutor {
    Invoke-MockExecutor `
        -ExecutorAction "--mock-full-regression" `
        -TimeoutMilliseconds 2400000 `
        -SuccessMarker "P1B2_GATE4_MOCK_FULL_REGRESSION_OK"
}

function Get-Gate4SystemEnvironmentNames {
    return @(
        "PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "PROGRAMDATA", "LOCALAPPDATA", "USERPROFILE",
        "PROGRAMFILES", "PROGRAMW6432"
    )
}

function Invoke-StageBExecutor {
    throw $script:StageBFinalAttemptBlocked

    $script:DriverStage = "STAGEB_RESOLVE"
    $python = Get-Gate4ExecutorPython
    $executorPath = Join-Path -Path $PSScriptRoot -ChildPath "run-phase-1b.py"
    if (-not [System.IO.File]::Exists($executorPath)) {
        throw "P1B2_GATE4_EXECUTOR_NOT_AVAILABLE"
    }
    $logicalArguments = @($executorPath, "--stage-b")
    Assert-NoSecretsInArguments -Arguments $logicalArguments -SecretValues @()

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $python
    $startInfo.Arguments =
        (ConvertTo-QuotedProcessArgument -Value $executorPath) + " --stage-b"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables.Clear()
    foreach ($name in (Get-Gate4SystemEnvironmentNames)) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if (-not [string]::IsNullOrEmpty($value)) {
            $startInfo.EnvironmentVariables[$name] = $value
        }
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        $script:DriverStage = "STAGEB_START"
        if (-not $process.Start()) {
            throw "P1B2_GATE4_EXECUTOR_START_FAILED"
        }
        $script:DriverStage = "STAGEB_WAIT"
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(1200000)) {
            $killInfo = New-Object System.Diagnostics.ProcessStartInfo
            $killInfo.FileName = "taskkill.exe"
            $killInfo.Arguments = "/PID {0} /T /F" -f $process.Id
            $killInfo.UseShellExecute = $false
            $killInfo.CreateNoWindow = $true
            $killInfo.RedirectStandardOutput = $true
            $killInfo.RedirectStandardError = $true
            $killer = New-Object System.Diagnostics.Process
            $killer.StartInfo = $killInfo
            try {
                if (-not $killer.Start()) {
                    throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
                }
                [void]$killer.StandardOutput.ReadToEndAsync()
                [void]$killer.StandardError.ReadToEndAsync()
                if (-not $killer.WaitForExit(30000)) {
                    throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
                }
                if (
                    $killer.ExitCode -ne 0 -and
                    -not $process.HasExited
                ) {
                    throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
                }
            }
            finally {
                $killer.Dispose()
            }
            if (-not $process.WaitForExit(30000)) {
                throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
            }
            throw "P1B2_GATE4_STAGE_B_WATCHDOG_EXCEEDED"
        }
        $process.WaitForExit()
        $script:DriverStage = "STAGEB_READ"
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            $script:DriverStage = "STAGEB_CHILD_FAILED"
            $safeFailureMarkers = @(
                "P1B2_GATE4_DEEPSEEK_KEY_NOT_AVAILABLE",
                "P1B2_GATE4_SECRET_LEAK_DETECTED",
                "P1B2_GATE4_RUNTIME_BUDGET_EXCEEDED",
                "P1B2_GATE4_PROVIDER_BUDGET_EXCEEDED",
                "P1B2_GATE4_CLEANUP_INCOMPLETE",
                "P1B2_GATE4_RESOURCE_GATE_FAILED",
                "P1B2_GATE4_STAGE_B_MEMORY_PREFLIGHT_FAILED",
                "P1B2_GATE4_STAGE_C_RUNTIME_START_MEMORY_FAILED",
                "P1B2_GATE4_STAGE_C_TOOL_RETRY_MEMORY_FAILED",
                "P1B2_GATE4_PROCESS_IDENTITY_FAILED",
                "P1B2_GATE4_HTTP_CONTRACT_FAILED",
                "P1B2_GATE4_REPOSITORY_GATE_FAILED",
                "P1B2_GATE4_INVALID_STATE",
                "P1B2_GATE4_EXECUTOR_FAILED"
            )
            $failure = $stderr.Trim()
            if ($safeFailureMarkers -contains $failure) {
                [Console]::Error.WriteLine($failure)
            }
            throw "P1B2_GATE4_STAGE_B_FAILED"
        }
        if ($stderr.Length -ne 0) {
            throw "P1B2_GATE4_UNEXPECTED_EXECUTOR_STDERR"
        }
        $lines = @($stdout.TrimEnd() -split "\r?\n")
        $script:DriverStage = "STAGEB_VALIDATE"
        if (
            $lines.Count -ne 3 -or
            $lines[0] -ne "P1B2_GATE4_STAGE_B_COMPLETE" -or
            $lines[1] -ne (
                "Runtime=1/2 DDPM=1/2 " +
                "Provider=NOT_APPLICABLE_STAGE_B_ONLY"
            ) -or
            $lines[2] -notmatch (
                "^run_id=[A-Za-z0-9_.:-]+ device=cuda " +
                "bundle=zta35g-sem-original-bundle " +
                "image=512x512 metrics=positive$"
            )
        ) {
            throw "P1B2_GATE4_UNEXPECTED_EXECUTOR_STDOUT"
        }
        foreach ($line in $lines) {
            [Console]::Out.WriteLine($line)
        }
        $script:DriverStage = "STAGEB_DONE"
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-Phase1Executor {
    throw $script:NewStageCNotAuthorized

    $python = Get-Gate4ExecutorPython
    $executorPath = Join-Path -Path $PSScriptRoot -ChildPath "run-phase-1b.py"
    if (-not [System.IO.File]::Exists($executorPath)) {
        throw "P1B2_GATE4_EXECUTOR_NOT_AVAILABLE"
    }
    $logicalArguments = @($executorPath, "--phase-1")
    Assert-NoSecretsInArguments -Arguments $logicalArguments -SecretValues @()

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $python
    $startInfo.Arguments =
        (ConvertTo-QuotedProcessArgument -Value $executorPath) + " --phase-1"
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables.Clear()
    foreach ($name in (Get-Gate4SystemEnvironmentNames)) {
        $value = [Environment]::GetEnvironmentVariable($name, "Process")
        if (-not [string]::IsNullOrEmpty($value)) {
            $startInfo.EnvironmentVariables[$name] = $value
        }
    }

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) {
            throw "P1B2_GATE4_EXECUTOR_START_FAILED"
        }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(600000)) {
            $killInfo = New-Object System.Diagnostics.ProcessStartInfo
            $killInfo.FileName = "taskkill.exe"
            $killInfo.Arguments = "/PID {0} /T /F" -f $process.Id
            $killInfo.UseShellExecute = $false
            $killInfo.CreateNoWindow = $true
            $killInfo.RedirectStandardOutput = $true
            $killInfo.RedirectStandardError = $true
            $killer = New-Object System.Diagnostics.Process
            $killer.StartInfo = $killInfo
            try {
                if (-not $killer.Start()) {
                    throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
                }
                [void]$killer.StandardOutput.ReadToEndAsync()
                [void]$killer.StandardError.ReadToEndAsync()
                if (-not $killer.WaitForExit(30000)) {
                    throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
                }
                if (
                    $killer.ExitCode -ne 0 -and
                    -not $process.HasExited
                ) {
                    throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
                }
            }
            finally {
                $killer.Dispose()
            }
            if (-not $process.WaitForExit(30000)) {
                throw "P1B2_GATE4_CLEANUP_INCOMPLETE"
            }
            throw "P1B2_GATE4_PHASE1_WATCHDOG_EXCEEDED"
        }
        $process.WaitForExit()
        $stdout = $stdoutTask.GetAwaiter().GetResult()
        $stderr = $stderrTask.GetAwaiter().GetResult()
        if ($process.ExitCode -ne 0) {
            $safeFailureMarkers = @(
                "P1B2_GATE4_DEEPSEEK_KEY_NOT_AVAILABLE",
                "P1B2_GATE4_SECRET_LEAK_DETECTED",
                "P1B2_GATE4_CLEANUP_INCOMPLETE",
                "P1B2_GATE4_PHASE1_FAILED",
                "P1B2_GATE4_DOCKER_GATE_FAILED",
                "P1B2_GATE4_BACKEND_ADAPTER_CANARY_FAILED",
                "P1B2_GATE4_PROCESS_IDENTITY_FAILED",
                "P1B2_GATE4_REPOSITORY_GATE_FAILED",
                "P1B2_GATE4_INVALID_STATE",
                "P1B2_GATE4_EXECUTOR_FAILED"
            )
            $failure = $stderr.Trim()
            if ($safeFailureMarkers -contains $failure) {
                [Console]::Error.WriteLine($failure)
            }
            throw "P1B2_GATE4_PHASE1_FAILED"
        }
        if ($stderr.Length -ne 0) {
            throw "P1B2_GATE4_UNEXPECTED_EXECUTOR_STDERR"
        }
        $lines = @($stdout.TrimEnd() -split "\r?\n")
        if (
            $lines.Count -ne 6 -or
            $lines[0] -ne "P1B2_GATE4_BROWSER_ACCEPTANCE_READY" -or
            $lines[1] -ne "Frontend:" -or
            $lines[2] -ne "http://127.0.0.1:3000" -or
            [string]::IsNullOrWhiteSpace($lines[3]) -or
            [string]::IsNullOrWhiteSpace($lines[4]) -or
            [string]::IsNullOrWhiteSpace($lines[5])
        ) {
            throw "P1B2_GATE4_UNEXPECTED_EXECUTOR_STDOUT"
        }
        foreach ($line in $lines) {
            [Console]::Out.WriteLine($line)
        }
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-OfflineSelfTest {
    $realKey = "selftest-real-provider-fixed-nonsecret"
    $controlled = @{
        real_provider_key = $realKey
        invalid_provider_key = New-InvalidProviderKey -RunId "selftest" -RealProviderKey $realKey
        runtime_token = "selftest-runtime-fixed-nonsecret"
        database_password = "selftest-database-fixed-nonsecret"
        minio_secret = "selftest-minio-fixed-nonsecret"
        timeline_signing_key = "selftest-timeline-fixed-nonsecret"
    }
    $secretValues = @(
        $controlled["real_provider_key"],
        $controlled["invalid_provider_key"],
        $controlled["runtime_token"],
        $controlled["database_password"],
        $controlled["minio_secret"],
        $controlled["timeline_signing_key"]
    )
    $systemEnvironmentNames = @(Get-Gate4SystemEnvironmentNames)
    Assert-SelfTest -Condition (
        $systemEnvironmentNames -contains "PROGRAMFILES"
    )
    Assert-SelfTest -Condition (
        $systemEnvironmentNames -contains "PROGRAMW6432"
    )

    $literal = ConvertFrom-ControlledEnvText -Text (
        "# controlled literal parser`n" +
        'DEEPSEEK_API_KEY=$(not-executed)' + "`nOTHER=value`n"
    )
    Assert-SelfTest -Condition (
        $literal["DEEPSEEK_API_KEY"] -eq '$(not-executed)'
    )
    Assert-SelfTest -Condition (-not (Test-Path variable:global:not-executed))

    $invalidTexts = @(
        "OTHER=value`n",
        "DEEPSEEK_API_KEY=`n",
        "DEEPSEEK_API_KEY= value`n",
        "DEEPSEEK_API_KEY=value `n",
        "DEEPSEEK_API_KEY=one`nDEEPSEEK_API_KEY=two`n",
        ("DEEPSEEK_API_KEY=bad" + [char]1 + "value`n"),
        ("DEEPSEEK_API_KEY=bad" + [char]127 + "value`n"),
        ("DEEPSEEK_API_KEY=bad" + [char]128 + "value`n"),
        ("DEEPSEEK_API_KEY=bad" + [char]159 + "value`n")
    )
    foreach ($invalidText in $invalidTexts) {
        Assert-FixedFailure -Marker $script:KeyNotAvailable -Operation {
            ConvertFrom-ControlledEnvText -Text $invalidText
        }
    }

    $secondInvalidKey = New-InvalidProviderKey -RunId "selftest" -RealProviderKey $realKey
    Assert-SelfTest -Condition ($controlled["invalid_provider_key"] -ne $realKey)
    Assert-SelfTest -Condition ($secondInvalidKey -ne $realKey)
    Assert-SelfTest -Condition ($secondInvalidKey -ne $controlled["invalid_provider_key"])
    Assert-SelfTest -Condition (
        -not (Test-ContainsControlCharacter -Value $controlled["invalid_provider_key"])
    )
    Assert-SelfTest -Condition (
        $controlled["invalid_provider_key"] -eq $controlled["invalid_provider_key"].Trim()
    )

    $baseEnvironment = @{
        PATH = "selftest-path"
        DEEPSEEK_API_KEY = "must-not-be-copied"
        LLM_ADAPTER = "deepseek"
        M12B_REAL_CALLS_AUTHORIZED = "YES"
        P1B2_GATE4_AUTHORIZED = "YES"
    }
    $mockEnvironment = New-MockChildEnvironment `
        -BaseEnvironment $baseEnvironment
    Assert-SelfTest -Condition ($mockEnvironment["LLM_ADAPTER"] -eq "mock")
    Assert-SelfTest -Condition ($mockEnvironment["DEEPSEEK_API_KEY"] -eq "")
    Assert-SelfTest -Condition (
        -not $mockEnvironment.ContainsKey("M12B_REAL_CALLS_AUTHORIZED")
    )
    Assert-SelfTest -Condition (
        -not $mockEnvironment.ContainsKey("P1B2_GATE4_AUTHORIZED")
    )
    Assert-SelfTest -Condition (
        -not ($mockEnvironment.Values -contains "must-not-be-copied")
    )
    $runtimeEnvironment = New-RoleEnvironment `
        -Role "runtime" -BaseEnvironment $baseEnvironment -Controlled $controlled
    Assert-SelfTest -Condition ($runtimeEnvironment.Count -eq 2)
    Assert-SelfTest -Condition (
        $runtimeEnvironment["ZTA35G_RUNTIME_TOKEN"] -eq $controlled["runtime_token"]
    )
    Assert-SelfTest -Condition (-not $runtimeEnvironment.ContainsKey("DEEPSEEK_API_KEY"))

    foreach ($providerFreeRole in @("frontend", "compose", "log")) {
        $roleEnvironment = New-RoleEnvironment `
            -Role $providerFreeRole -BaseEnvironment $baseEnvironment -Controlled $controlled
        Assert-SelfTest -Condition ($roleEnvironment.Count -eq 1)
        Assert-SelfTest -Condition (-not $roleEnvironment.ContainsKey("DEEPSEEK_API_KEY"))
    }
    $databaseEnvironment = New-RoleEnvironment `
        -Role "database" -BaseEnvironment $baseEnvironment -Controlled $controlled
    Assert-SelfTest -Condition ($databaseEnvironment.Count -eq 2)
    Assert-SelfTest -Condition (-not $databaseEnvironment.ContainsKey("DEEPSEEK_API_KEY"))
    $minioEnvironment = New-RoleEnvironment `
        -Role "minio" -BaseEnvironment $baseEnvironment -Controlled $controlled
    Assert-SelfTest -Condition ($minioEnvironment.Count -eq 2)
    Assert-SelfTest -Condition (-not $minioEnvironment.ContainsKey("DEEPSEEK_API_KEY"))

    $backendReal = New-RoleEnvironment `
        -Role "backend-real" -BaseEnvironment $baseEnvironment -Controlled $controlled
    $backendInvalid = New-RoleEnvironment `
        -Role "backend-invalid" -BaseEnvironment $baseEnvironment -Controlled $controlled
    Assert-SelfTest -Condition ($backendReal["DEEPSEEK_API_KEY"] -eq $realKey)
    Assert-SelfTest -Condition (
        $backendInvalid["DEEPSEEK_API_KEY"] -eq $controlled["invalid_provider_key"]
    )
    Assert-SelfTest -Condition ($baseEnvironment["DEEPSEEK_API_KEY"] -eq "must-not-be-copied")

    $safeArguments = @("-m", "offline_executor", "--mode", "selftest")
    Assert-NoSecretsInArguments -Arguments $safeArguments -SecretValues $secretValues
    Assert-FixedFailure -Marker $script:SecretInCommand -Operation {
        Assert-NoSecretsInArguments `
            -Arguments @("--unsafe", $controlled["runtime_token"]) `
            -SecretValues $secretValues
    }

    Assert-SelfTest -Condition (
        (Find-ExactSecret -Text "safe artifact" -SecretValues $secretValues).Count -eq 0
    )
    $secretHits = Find-ExactSecret `
        -Text ("prefix" + $controlled["timeline_signing_key"] + "suffix") `
        -SecretValues $secretValues
    Assert-SelfTest -Condition ($secretHits.Count -eq 1)
    Assert-SelfTest -Condition (
        -not (($secretHits | Out-String).Contains($controlled["timeline_signing_key"]))
    )

    $runtimeLedger = @{ runtime = 0; ddpm = 0 }
    $runtimeDelegateCount = @{ runtime = 0; ddpm = 0 }
    for ($index = 0; $index -lt 2; $index++) {
        Invoke-RuntimeBudgeted -Ledger $runtimeLedger -Kind "runtime" -Delegate {
            $runtimeDelegateCount["runtime"] = [int]$runtimeDelegateCount["runtime"] + 1
        }
        Invoke-RuntimeBudgeted -Ledger $runtimeLedger -Kind "ddpm" -Delegate {
            $runtimeDelegateCount["ddpm"] = [int]$runtimeDelegateCount["ddpm"] + 1
        }
    }
    Assert-FixedFailure -Marker $script:RuntimeBudgetExceeded -Operation {
        Invoke-RuntimeBudgeted -Ledger $runtimeLedger -Kind "runtime" -Delegate {
            $runtimeDelegateCount["runtime"] = [int]$runtimeDelegateCount["runtime"] + 1
        }
    }
    Assert-FixedFailure -Marker $script:RuntimeBudgetExceeded -Operation {
        Invoke-RuntimeBudgeted -Ledger $runtimeLedger -Kind "ddpm" -Delegate {
            $runtimeDelegateCount["ddpm"] = [int]$runtimeDelegateCount["ddpm"] + 1
        }
    }
    Assert-SelfTest -Condition ($runtimeDelegateCount["runtime"] -eq 2)
    Assert-SelfTest -Condition ($runtimeDelegateCount["ddpm"] -eq 2)

    $providerLedger = @{ chat = 0; explanation = 0 }
    $providerDelegateCount = @{ chat = 0; explanation = 0 }
    foreach ($kind in @("chat", "chat", "chat", "explanation", "explanation")) {
        Invoke-ProviderBudgeted -Ledger $providerLedger -Kind $kind -Delegate {
            $providerDelegateCount[$kind] = [int]$providerDelegateCount[$kind] + 1
        }
    }
    Assert-FixedFailure -Marker $script:ProviderBudgetExceeded -Operation {
        Invoke-ProviderBudgeted -Ledger $providerLedger -Kind "chat" -Delegate {
            $providerDelegateCount["chat"] = [int]$providerDelegateCount["chat"] + 1
        }
    }
    Assert-SelfTest -Condition ($providerDelegateCount["chat"] -eq 3)
    Assert-SelfTest -Condition ($providerDelegateCount["explanation"] -eq 2)

    $resourceIdentity = @{
        database = "selftest-database"
        bucket = "selftest-bucket"
        actor = "selftest-actor"
        conversation = "selftest-conversation"
    }
    $events = New-Object System.Collections.ArrayList
    $resourceSnapshots = New-Object System.Collections.ArrayList
    foreach ($event in @(
        "start:real",
        "stop:real",
        "port-clear:8000",
        "start:invalid",
        "stop:invalid",
        "port-clear:8000",
        "start:real"
    )) {
        [void]$events.Add($event)
        if ($event.StartsWith("start:")) {
            [void]$resourceSnapshots.Add(("{0}|{1}|{2}|{3}" -f
                $resourceIdentity["database"],
                $resourceIdentity["bucket"],
                $resourceIdentity["actor"],
                $resourceIdentity["conversation"]))
        }
    }
    Assert-SelfTest -Condition (
        ($events -join ",") -eq (
            "start:real,stop:real,port-clear:8000,start:invalid," +
            "stop:invalid,port-clear:8000,start:real"
        )
    )
    Assert-SelfTest -Condition (
        @($resourceSnapshots | Select-Object -Unique).Count -eq 1
    )

    $sentinelName = "P1B2_GATE4_SELFTEST_PARENT_SENTINEL"
    $sentinelBeforeExists = Test-Path ("Env:{0}" -f $sentinelName)
    $sentinelBeforeValue = if ($sentinelBeforeExists) {
        [Environment]::GetEnvironmentVariable($sentinelName, "Process")
    }
    else {
        $null
    }
    try {
        [Environment]::SetEnvironmentVariable(
            $sentinelName,
            "selftest-temporary-value",
            "Process"
        )
        Assert-SelfTest -Condition (
            [Environment]::GetEnvironmentVariable($sentinelName, "Process") -eq
            "selftest-temporary-value"
        )
    }
    finally {
        if ($sentinelBeforeExists) {
            [Environment]::SetEnvironmentVariable(
                $sentinelName,
                $sentinelBeforeValue,
                "Process"
            )
        }
        else {
            [Environment]::SetEnvironmentVariable($sentinelName, $null, "Process")
        }
    }
    Assert-SelfTest -Condition (
        (Test-Path ("Env:{0}" -f $sentinelName)) -eq $sentinelBeforeExists
    )
    if ($sentinelBeforeExists) {
        Assert-SelfTest -Condition (
            [Environment]::GetEnvironmentVariable($sentinelName, "Process") -eq
            $sentinelBeforeValue
        )
    }

    $cleanupOrder = New-Object System.Collections.ArrayList
    $cleanupActions = New-Object System.Collections.ArrayList
    foreach ($name in @("database", "minio", "backend", "frontend")) {
        $capturedName = $name
        [void]$cleanupActions.Add(@{
            name = $capturedName
            action = {
                [void]$cleanupOrder.Add($capturedName)
            }.GetNewClosure()
        })
    }
    $cleanup = @{ actions = $cleanupActions; complete = $false }
    Invoke-IdempotentCleanup -Cleanup $cleanup
    Invoke-IdempotentCleanup -Cleanup $cleanup
    Assert-SelfTest -Condition (
        ($cleanupOrder -join ",") -eq "frontend,backend,minio,database"
    )

    $failedSummary = New-SafeSummary -Passed $false -CleanupComplete $true
    Assert-SelfTest -Condition (-not (($failedSummary | Out-String).Contains("PASSED")))
    $incompleteSummary = New-SafeSummary -Passed $true -CleanupComplete $false
    Assert-SelfTest -Condition (
        $incompleteSummary["status"] -eq "P1B2_GATE4_CLEANUP_INCOMPLETE"
    )

    Invoke-PythonExecutorSelfTest -SecretValues $secretValues
}

try {
    if ($SelfTest) {
        if ($Action) {
            throw "P1B2_GATE4_INVALID_ARGUMENTS"
        }
        Invoke-OfflineSelfTest
        [Console]::Out.WriteLine($script:PowerShellSelfTestOk)
        exit 0
    }

    if (-not $Action) {
        throw "P1B2_GATE4_ACTION_REQUIRED"
    }
    if ($Action -eq "StageB") {
        Invoke-StageBExecutor
        exit 0
    }
    if ($Action -eq "Phase1") {
        Invoke-Phase1Executor
        exit 0
    }
    if ($Action -eq "MockHelperTests") {
        Invoke-MockHelperTestsExecutor
        exit 0
    }
    if ($Action -eq "MockFullRegression") {
        Invoke-MockFullRegressionExecutor
        exit 0
    }
    if ($Action -eq "MockRegression") {
        Invoke-MockFullRegressionExecutor
        exit 0
    }
    throw $script:NotImplemented
}
catch {
    if ($SelfTest) {
        [Console]::Error.WriteLine($script:PowerShellSelfTestFailed)
    }
    else {
        $safeCatchMarkers = @(
            "P1B2_GATE4_ACTION_REQUIRED",
            "P1B2_GATE4_EXECUTOR_NOT_AVAILABLE",
            "P1B2_GATE4_EXECUTOR_START_FAILED",
            "P1B2_GATE4_UNEXPECTED_EXECUTOR_STDERR",
            "P1B2_GATE4_UNEXPECTED_EXECUTOR_STDOUT",
            "P1B2_GATE4_STAGE_B_FAILED",
            "P1B2_GATE4_STAGE_B_WATCHDOG_EXCEEDED",
            "P1B2_GATE4_PHASE1_FAILED",
            "P1B2_GATE4_PHASE1_WATCHDOG_EXCEEDED",
            "P1B2_GATE4_CLEANUP_INCOMPLETE",
            "P1B2_GATE4_MOCK_PROVIDER_ISOLATION_FAILED",
            "P1B2_GATE4_STAGE_B_FINAL_ATTEMPT_BLOCKED",
            "P1B2_GATE4_NEW_STAGE_C_NOT_AUTHORIZED"
        )
        $safeMessage = [string]$_.Exception.Message
        if ($safeCatchMarkers -contains $safeMessage) {
            [Console]::Error.WriteLine($safeMessage)
        }
        if ($Action -in @("StageB", "Phase1")) {
            [Console]::Error.WriteLine(
                "P1B2_GATE4_DRIVER_STAGE_{0}" -f $script:DriverStage
            )
        }
        if ($Action -in @("StageB", "Phase1")) {
            [Console]::Error.WriteLine("P1B2_GATE4_BLOCKED")
        }
        elseif ($Action -in @(
            "MockHelperTests",
            "MockFullRegression",
            "MockRegression"
        )) {
            [Console]::Error.WriteLine(
                "P1B2_GATE4_MOCK_PROVIDER_ISOLATION_FAILED"
            )
        }
        else {
            [Console]::Error.WriteLine($script:NotImplemented)
        }
    }
    exit 1
}
