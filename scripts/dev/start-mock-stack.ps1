[CmdletBinding()]
param(
    [string]$CondaEnvironment = 'materialsagent-backend',
    [string]$PythonExecutable,
    [switch]$LoadFunctionsOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$StateRoot = Join-Path $RepoRoot 'tmp\m11-mock-stack'
$StatePath = Join-Path $StateRoot 'state.json'
$ComposeFile = Join-Path $RepoRoot 'docker-compose.yml'
$ComposeArguments = @(
    '--project-name'
    'materialsagent'
    '--file'
    $ComposeFile
)
$ControlledEnvironmentDefaults = [ordered]@{
    APP_ENV = 'local'
    LOG_LEVEL = 'INFO'
    POSTGRES_PORT = '5432'
    ZTA35G_RUNTIME_TIMEOUT_SECONDS = '10'
    M5_DEV_ROUTES_ENABLED = 'false'
}
$ControlledEnvironmentKeys = @(
    'APP_ENV'
    'LOG_LEVEL'
    'LOCAL_ACTOR_ID'
    'POSTGRES_HOST'
    'POSTGRES_PORT'
    'POSTGRES_DB'
    'POSTGRES_USER'
    'POSTGRES_PASSWORD'
    'MINIO_API_PORT'
    'MINIO_CONSOLE_PORT'
    'MINIO_ENDPOINT'
    'MINIO_ACCESS_KEY'
    'MINIO_SECRET_KEY'
    'MINIO_BUCKET'
    'MINIO_SECURE'
    'ZTA35G_RUNTIME_URL'
    'ZTA35G_RUNTIME_TOKEN'
    'ZTA35G_RUNTIME_TIMEOUT_SECONDS'
    'M5_DEV_ROUTES_ENABLED'
    # Retired setting: strip any legacy caller secret from unrelated children.
    'TIMELINE_CURSOR_SIGNING_KEY'
)
$RequiredEnvKeys = @(
    'LOCAL_ACTOR_ID'
    'POSTGRES_HOST'
    'POSTGRES_DB'
    'POSTGRES_USER'
    'POSTGRES_PASSWORD'
    'MINIO_API_PORT'
    'MINIO_CONSOLE_PORT'
    'MINIO_ENDPOINT'
    'MINIO_ACCESS_KEY'
    'MINIO_SECRET_KEY'
    'MINIO_BUCKET'
    'MINIO_SECURE'
    'ZTA35G_RUNTIME_URL'
    'ZTA35G_RUNTIME_TOKEN'
)
$ExpectedProcesses = @{
    runtime = @{
        marker = 'materialsagent_mock_runtime\main.py'
        port = 8100
    }
    backend = @{
        marker = 'materialsagent.main:create_app'
        port = 8000
    }
    frontend = @{
        marker = '--strictPort'
        port = 3000
    }
}
$ExpectedDockerServices = @('postgresql', 'minio')
$ExpectedWrapperExecutable = [IO.Path]::GetFullPath(
    (Join-Path ([Environment]::SystemDirectory) 'cmd.exe')
)

function New-RoleSpecificChildEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('runtime', 'backend', 'frontend')]
        [string]$Role,
        [Parameter(Mandatory = $true)]
        [hashtable]$ControlledEnvironment
    )

    $controlledKeys = New-Object `
        'System.Collections.Generic.HashSet[string]' `
        ([StringComparer]::OrdinalIgnoreCase)
    foreach ($key in $ControlledEnvironmentKeys) {
        [void]$controlledKeys.Add($key)
    }

    $childEnvironment = New-Object `
        'System.Collections.Generic.Dictionary[string,string]' `
        ([StringComparer]::OrdinalIgnoreCase)
    foreach (
        $entry in [Environment]::GetEnvironmentVariables(
            [EnvironmentVariableTarget]::Process
        ).GetEnumerator()
    ) {
        $key = [string]$entry.Key
        if (
            $controlledKeys.Contains($key) -or
            $key.Equals('ComSpec', [StringComparison]::OrdinalIgnoreCase) -or
            $key.Equals(
                'ZTA35G_RUNTIME_PORT',
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            continue
        }
        $childEnvironment[$key] = [string]$entry.Value
    }
    $childEnvironment['ComSpec'] = $ExpectedWrapperExecutable

    if ($Role -eq 'backend') {
        foreach ($key in $ControlledEnvironmentKeys) {
            if (-not $ControlledEnvironment.ContainsKey($key)) {
                throw "Controlled child environment key is missing: $key"
            }
            $childEnvironment[$key] = [string]$ControlledEnvironment[$key]
        }
    }
    elseif ($Role -eq 'runtime') {
        if (-not $ControlledEnvironment.ContainsKey('ZTA35G_RUNTIME_TOKEN')) {
            throw 'Controlled child environment key is missing: ZTA35G_RUNTIME_TOKEN'
        }
        $childEnvironment['ZTA35G_RUNTIME_TOKEN'] = [string](
            $ControlledEnvironment['ZTA35G_RUNTIME_TOKEN']
        )
        $childEnvironment['ZTA35G_RUNTIME_PORT'] = '8100'
    }

    return [string[]]@(
        $childEnvironment.GetEnumerator() |
            Sort-Object -Property Key |
            ForEach-Object {
                '{0}={1}' -f [string]$_.Key, [string]$_.Value
            }
    )
}

function Resolve-ExecutablePath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $candidate = $Path
    if (-not [IO.Path]::IsPathRooted($candidate)) {
        $command = Get-Command $candidate -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -eq $command) {
            throw "Executable was not found: $candidate"
        }
        $candidate = $command.Source
    }
    $resolved = Resolve-Path -LiteralPath $candidate -ErrorAction Stop
    if (-not (Test-Path -LiteralPath $resolved.Path -PathType Leaf)) {
        throw "Executable is not a file: $candidate"
    }
    return [IO.Path]::GetFullPath($resolved.Path)
}

function Resolve-BackendPython {
    if (-not [string]::IsNullOrWhiteSpace($PythonExecutable)) {
        return Resolve-ExecutablePath -Path $PythonExecutable
    }

    $condaCandidates = New-Object 'System.Collections.Generic.List[string]'
    $pathConda = Get-Command 'conda' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $pathConda) {
        $condaCandidates.Add($pathConda.Source)
    }
    if (
        -not [string]::IsNullOrWhiteSpace($env:CONDA_EXE) -and
        (Test-Path -LiteralPath $env:CONDA_EXE -PathType Leaf)
    ) {
        $condaCandidates.Add($env:CONDA_EXE)
    }
    foreach ($driveRoot in Get-PSDrive -PSProvider FileSystem | Select-Object -ExpandProperty Root) {
        foreach (
            $relativePath in @(
                'ProgramData\Anaconda3\Scripts\conda.exe'
                'ProgramData\Miniconda3\Scripts\conda.exe'
                'Anaconda3\Scripts\conda.exe'
                'Miniconda3\Scripts\conda.exe'
            )
        ) {
            $candidate = Join-Path $driveRoot $relativePath
            if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                $condaCandidates.Add($candidate)
            }
        }
    }
    $resolvedConda = @(
        $condaCandidates |
            ForEach-Object { [IO.Path]::GetFullPath($_) } |
            Sort-Object -Unique
    )
    if ($resolvedConda.Count -ne 1) {
        throw 'Conda was not found and -PythonExecutable was not supplied.'
    }

    $rawEnvironmentList = & $resolvedConda[0] env list --json 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'Conda environment discovery failed.'
    }
    try {
        $environmentList = ($rawEnvironmentList -join [Environment]::NewLine) |
            ConvertFrom-Json
    }
    catch {
        throw 'Conda environment discovery returned invalid JSON.'
    }

    $matches = @(
        $environmentList.envs |
            Where-Object {
                (Split-Path -Leaf ([string]$_)).Equals(
                    $CondaEnvironment,
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
    )
    if ($matches.Count -ne 1) {
        throw "Expected exactly one Conda environment named $CondaEnvironment."
    }
    return Resolve-ExecutablePath -Path (Join-Path $matches[0] 'python.exe')
}

function Assert-PythonRuntime {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Python
    )

    $version = & $Python -c 'import sys; print(sys.version_info.major, sys.version_info.minor)' 2>$null
    if ($LASTEXITCODE -ne 0 -or ($version -join '').Trim() -ne '3 11') {
        throw 'Backend Python must be Python 3.11.'
    }

    & $Python -c 'import alembic, fastapi, httpx, minio, numpy, psycopg, sqlalchemy, uvicorn' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'Backend Python is missing an existing startup dependency.'
    }
}

function Read-SafeDotEnv {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'The repository root .env file is required.'
    }

    $allowed = @{}
    foreach ($key in $ControlledEnvironmentKeys) {
        if ($key -ne 'TIMELINE_CURSOR_SIGNING_KEY') {
            $allowed[$key] = $true
        }
    }
    $values = @{}
    foreach ($entry in $ControlledEnvironmentDefaults.GetEnumerator()) {
        $values[$entry.Key] = [string]$entry.Value
    }
    $seen = @{}

    foreach ($line in Get-Content -LiteralPath $Path -Encoding utf8) {
        $trimmed = $line.Trim()
        if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith('#')) {
            continue
        }
        $separator = $trimmed.IndexOf('=')
        if ($separator -le 0) {
            continue
        }
        $key = $trimmed.Substring(0, $separator).Trim()
        if (-not $allowed.ContainsKey($key)) {
            continue
        }
        if ($seen.ContainsKey($key)) {
            throw "Controlled .env key is duplicated: $key"
        }
        $seen[$key] = $true
        $value = $trimmed.Substring($separator + 1).Trim()
        if (
            $value.Length -ge 2 -and
            (
                ($value.StartsWith('"') -and $value.EndsWith('"')) -or
                ($value.StartsWith("'") -and $value.EndsWith("'"))
            )
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ([string]::IsNullOrWhiteSpace($value)) {
            throw "Controlled .env key is empty: $key"
        }
        $values[$key] = $value
    }

    foreach ($key in $RequiredEnvKeys) {
        if (-not $values.ContainsKey($key)) {
            throw "Required .env key is missing: $key"
        }
    }
    return $values
}

function Enter-ControlledEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Environment
    )

    $backup = @{}
    foreach ($key in $ControlledEnvironmentKeys) {
        $existing = [Environment]::GetEnvironmentVariable(
            $key,
            [EnvironmentVariableTarget]::Process
        )
        $backup[$key] = [PSCustomObject]@{
            existed = $null -ne $existing
            value = if ($null -ne $existing) { [string]$existing } else { $null }
        }
    }
    try {
        foreach ($key in $ControlledEnvironmentKeys) {
            [Environment]::SetEnvironmentVariable(
                $key,
                [string]$Environment[$key],
                [EnvironmentVariableTarget]::Process
            )
        }
    }
    catch {
        Restore-ControlledEnvironment -Backup $backup
        throw
    }
    return $backup
}

function Restore-ControlledEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Backup
    )

    foreach ($key in $ControlledEnvironmentKeys) {
        if (-not $Backup.ContainsKey($key)) {
            throw "Controlled environment backup is missing: $key"
        }
        $entry = $Backup[$key]
        if ($entry.existed) {
            [Environment]::SetEnvironmentVariable(
                $key,
                [string]$entry.value,
                [EnvironmentVariableTarget]::Process
            )
        }
        else {
            [Environment]::SetEnvironmentVariable(
                $key,
                $null,
                [EnvironmentVariableTarget]::Process
            )
        }
    }
}

function Assert-M11ALocalConfiguration {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Environment,
        [Parameter(Mandatory = $true)]
        [string]$Python
    )

    $fixedValues = [ordered]@{
        APP_ENV = 'local'
        POSTGRES_HOST = '127.0.0.1'
        POSTGRES_PORT = '5432'
        MINIO_ENDPOINT = 'http://127.0.0.1:9000'
        MINIO_API_PORT = '9000'
        MINIO_CONSOLE_PORT = '9001'
        ZTA35G_RUNTIME_URL = 'http://127.0.0.1:8100'
        M5_DEV_ROUTES_ENABLED = 'false'
    }
    foreach ($entry in $fixedValues.GetEnumerator()) {
        if (
            -not ([string]$Environment[$entry.Key]).Equals(
                [string]$entry.Value,
                [StringComparison]::Ordinal
            )
        ) {
            throw "$($entry.Key) does not satisfy the M11-A local boundary."
        }
    }
    if (
        -not ([string]$Environment['MINIO_SECURE']).Equals(
            'false',
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        throw 'MINIO_SECURE does not satisfy the M11-A local boundary.'
    }

    $previousPythonPath = [Environment]::GetEnvironmentVariable(
        'PYTHONPATH',
        [EnvironmentVariableTarget]::Process
    )
    $hadPythonPath = $null -ne $previousPythonPath
    try {
        [Environment]::SetEnvironmentVariable(
            'PYTHONPATH',
            (Join-Path $RepoRoot 'backend\src'),
            [EnvironmentVariableTarget]::Process
        )
        $validationCode = @'
import os
import sys

from materialsagent.infrastructure.config import (
    ConfigurationError,
    load_settings,
    parse_minio_config,
    parse_zta35g_runtime_config,
)
from materialsagent.infrastructure.db.session import build_postgres_url

try:
    settings = load_settings(os.environ)
except ConfigurationError:
    sys.exit(32)

try:
    build_postgres_url(settings)
except ConfigurationError:
    sys.exit(33)
try:
    parse_minio_config(settings)
except ConfigurationError:
    sys.exit(34)
try:
    runtime = parse_zta35g_runtime_config(settings)
except ConfigurationError:
    sys.exit(35)
if runtime is None:
    sys.exit(35)
if settings.local_actor_id is None or not settings.local_actor_id.strip():
    sys.exit(36)
'@
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            & $Python -c $validationCode 2>$null | Out-Null
            $validationExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
    }
    finally {
        if ($hadPythonPath) {
            [Environment]::SetEnvironmentVariable(
                'PYTHONPATH',
                $previousPythonPath,
                [EnvironmentVariableTarget]::Process
            )
        }
        else {
            [Environment]::SetEnvironmentVariable(
                'PYTHONPATH',
                $null,
                [EnvironmentVariableTarget]::Process
            )
        }
    }

    switch ($validationExitCode) {
        0 { return }
        32 {
            throw 'AppSettings configuration does not satisfy M11-A preflight validation.'
        }
        33 {
            throw 'PostgreSQL configuration does not satisfy AppSettings validation.'
        }
        34 {
            throw 'MinIO configuration does not satisfy AppSettings validation.'
        }
        35 {
            throw 'ZTA35G_RUNTIME_URL/TOKEN does not satisfy AppSettings validation.'
        }
        36 { throw 'LOCAL_ACTOR_ID must be non-empty for M11-A.' }
        default {
            throw 'M11-A AppSettings preflight validation could not run.'
        }
    }
}

function Test-PortInUse {
    param(
        [Parameter(Mandatory = $true)]
        [int]$Port
    )

    $client = New-Object Net.Sockets.TcpClient
    $async = $null
    try {
        $async = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(250)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $async) {
            $async.AsyncWaitHandle.Dispose()
        }
        $client.Dispose()
    }
}

function Test-SafeDockerIdentityValue {
    param(
        [AllowEmptyString()]
        [string]$Value
    )

    return (
        -not [string]::IsNullOrWhiteSpace($Value) -and
        $Value.Length -le 256 -and
        $Value -notmatch '[\x00-\x1f\x7f]'
    )
}

function Get-LocalDockerIdentity {
    $dockerCommand = Get-Command `
        'docker' `
        -CommandType Application `
        -ErrorAction SilentlyContinue |
            Select-Object -First 1
    if ($null -eq $dockerCommand) {
        throw 'Docker was not found.'
    }

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $contextOutput = @(& docker context show 2>&1)
        $contextExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $contextLines = @(
        $contextOutput |
            ForEach-Object { [string]$_ } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if (
        $contextExitCode -ne 0 -or
        $contextLines.Count -ne 1 -or
        -not (Test-SafeDockerIdentityValue -Value $contextLines[0].Trim())
    ) {
        throw 'Docker context could not be determined safely.'
    }
    $context = $contextLines[0].Trim()

    $contextOverride = [Environment]::GetEnvironmentVariable(
        'DOCKER_CONTEXT',
        [EnvironmentVariableTarget]::Process
    )
    $hostOverride = [Environment]::GetEnvironmentVariable(
        'DOCKER_HOST',
        [EnvironmentVariableTarget]::Process
    )
    if (
        [string]::IsNullOrWhiteSpace($contextOverride) -and
        -not [string]::IsNullOrWhiteSpace($hostOverride)
    ) {
        $endpoint = [string]$hostOverride
    }
    else {
        $previousErrorActionPreference = $ErrorActionPreference
        try {
            $ErrorActionPreference = 'Continue'
            $endpointOutput = @(
                & docker context inspect $context `
                    --format '{{json .Endpoints.docker.Host}}' 2>&1
            )
            $endpointExitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
        if ($endpointExitCode -ne 0) {
            throw 'Docker context endpoint could not be determined safely.'
        }
        try {
            $endpoint = [string](
                (($endpointOutput | ForEach-Object { [string]$_ }) -join '') |
                    ConvertFrom-Json
            )
        }
        catch {
            throw 'Docker context endpoint could not be determined safely.'
        }
    }
    if (
        -not [regex]::IsMatch(
            $endpoint,
            '\Anpipe:////\./pipe/[A-Za-z0-9_.-]+\z',
            [Text.RegularExpressions.RegexOptions]::IgnoreCase
        )
    ) {
        throw 'Docker endpoint does not satisfy the M11-A local boundary.'
    }

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $engineOutput = @(& docker info --format '{{.ID}}' 2>&1)
        $engineExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $engineLines = @(
        $engineOutput |
            ForEach-Object { [string]$_ } |
            Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
    )
    if (
        $engineExitCode -ne 0 -or
        $engineLines.Count -ne 1 -or
        -not (Test-SafeDockerIdentityValue -Value $engineLines[0].Trim())
    ) {
        throw 'Docker engine identity could not be determined safely.'
    }
    return [PSCustomObject]@{
        context = $context
        endpoint = $endpoint
        engine_id = $engineLines[0].Trim()
    }
}

function Test-DockerIdentityMatch {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Expected,
        [Parameter(Mandatory = $true)]
        [object]$Actual
    )

    return (
        (Test-SafeDockerIdentityValue -Value ([string]$Expected.context)) -and
        (Test-SafeDockerIdentityValue -Value ([string]$Expected.engine_id)) -and
        ([string]$Expected.context).Equals(
            [string]$Actual.context,
            [StringComparison]::Ordinal
        ) -and
        ([string]$Expected.engine_id).Equals(
            [string]$Actual.engine_id,
            [StringComparison]::Ordinal
        )
    )
}

function Test-RequiredCollectionProperty {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State,
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    if ($State -is [Collections.IDictionary]) {
        if (-not $State.Contains($Name)) {
            return $false
        }
        $value = $State[$Name]
    }
    else {
        $property = $State.PSObject.Properties[$Name]
        if ($null -eq $property) {
            return $false
        }
        $value = $property.Value
    }
    if ($null -eq $value) {
        return $false
    }
    return (
        $value -is [Array] -or
        $value -is [Collections.IList]
    )
}

function Test-ComposeServiceRunning {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Service
    )

    $output = @(
        & docker compose @ComposeArguments ps --status running --services $Service 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to inspect Compose service: $Service"
    }
    return @($output | Where-Object { $_.Trim() -eq $Service }).Count -eq 1
}

function Get-ProcessSnapshot {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    $process = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $null
    }
    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
        if ($null -eq $cim) {
            return $null
        }
        return [PSCustomObject]@{
            start_time = $process.StartTime.ToUniversalTime()
            executable = [IO.Path]::GetFullPath([string]$cim.ExecutablePath)
            command_line = [string]$cim.CommandLine
        }
    }
    catch {
        return $null
    }
    finally {
        $process.Dispose()
    }
}

function Test-StateStructure {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State,
        [switch]$RequireComplete
    )

    try {
        foreach (
            $propertyName in @(
                'run_id'
                'created_at'
                'python_executable'
                'docker_engine_id'
                'docker_context'
            )
        ) {
            if ($State -is [Collections.IDictionary]) {
                if (-not $State.Contains($propertyName)) {
                    return $false
                }
                $propertyValue = $State[$propertyName]
            }
            else {
                $property = $State.PSObject.Properties[$propertyName]
                if ($null -eq $property) {
                    return $false
                }
                $propertyValue = $property.Value
            }
            if ([string]::IsNullOrWhiteSpace([string]$propertyValue)) {
                return $false
            }
        }
        $parsedRunId = [Guid]::Empty
        if (-not [Guid]::TryParse([string]$State.run_id, [ref]$parsedRunId)) {
            return $false
        }
        [void][DateTimeOffset]::Parse(
            [string]$State.created_at,
            [Globalization.CultureInfo]::InvariantCulture
        )
        if (
            -not [IO.Path]::IsPathRooted([string]$State.python_executable) -or
            -not (
                Test-SafeDockerIdentityValue `
                    -Value ([string]$State.docker_engine_id)
            ) -or
            -not (
                Test-SafeDockerIdentityValue `
                    -Value ([string]$State.docker_context)
            ) -or
            -not (Test-RequiredCollectionProperty -State $State -Name 'process') -or
            -not (Test-RequiredCollectionProperty -State $State -Name 'docker')
        ) {
            return $false
        }
        if (
            [string]$State.schema_version -ne '1' -or
            -not ([IO.Path]::GetFullPath([string]$State.repo_root)).Equals(
                $RepoRoot,
                [StringComparison]::OrdinalIgnoreCase
            )
        ) {
            return $false
        }

        $seenRoles = @{}
        $processes = @($State.process)
        foreach ($record in $processes) {
            $role = [string]$record.role
            if (
                -not $ExpectedProcesses.ContainsKey($role) -or
                $seenRoles.ContainsKey($role)
            ) {
                return $false
            }
            $seenRoles[$role] = $true
            $expected = $ExpectedProcesses[$role]
            if (
                [string]$record.expected_command_marker -ne
                    [string]$expected.marker -or
                [int]$record.port -ne [int]$expected.port -or
                [int]$record.pid -le 0 -or
                [string]::IsNullOrWhiteSpace([string]$record.process_start_time) -or
                [string]::IsNullOrWhiteSpace([string]$record.executable)
            ) {
                return $false
            }
            [void][DateTimeOffset]::Parse(
                [string]$record.process_start_time,
                [Globalization.CultureInfo]::InvariantCulture
            )
            if (
                -not ([IO.Path]::GetFullPath(
                    [string]$record.executable
                )).Equals(
                    $ExpectedWrapperExecutable,
                    [StringComparison]::OrdinalIgnoreCase
                )
            ) {
                return $false
            }
        }

        $seenServices = @{}
        $dockerRecords = @($State.docker)
        foreach ($record in $dockerRecords) {
            $service = [string]$record.service
            if (
                $ExpectedDockerServices -notcontains $service -or
                $seenServices.ContainsKey($service) -or
                -not ($record.preexisting -is [bool]) -or
                -not ($record.started_by_this_run -is [bool]) -or
                ($record.preexisting -and $record.started_by_this_run)
            ) {
                return $false
            }
            $seenServices[$service] = $true
        }

        if (
            $RequireComplete -and
            (
                $processes.Count -ne $ExpectedProcesses.Count -or
                $dockerRecords.Count -ne $ExpectedDockerServices.Count
            )
        ) {
            return $false
        }
        return $true
    }
    catch {
        return $false
    }
}

function Test-ManagedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Record
    )

    $role = [string]$Record.role
    if (-not $ExpectedProcesses.ContainsKey($role)) {
        return $false
    }
    $expected = $ExpectedProcesses[$role]
    if (
        [string]$Record.expected_command_marker -ne [string]$expected.marker -or
        [int]$Record.port -ne [int]$expected.port
    ) {
        return $false
    }

    $snapshot = Get-ProcessSnapshot -ProcessId ([int]$Record.pid)
    if ($null -eq $snapshot) {
        return $false
    }
    try {
        $expectedStart = [DateTimeOffset]::Parse(
            [string]$Record.process_start_time,
            [Globalization.CultureInfo]::InvariantCulture
        ).UtcDateTime
    }
    catch {
        return $false
    }
    if ([Math]::Abs(($snapshot.start_time - $expectedStart).TotalSeconds) -gt 1) {
        return $false
    }
    try {
        $recordedExecutable = [IO.Path]::GetFullPath(
            [string]$Record.executable
        )
    }
    catch {
        return $false
    }
    if (
        -not $recordedExecutable.Equals(
            $ExpectedWrapperExecutable,
            [StringComparison]::OrdinalIgnoreCase
        ) -or
        -not $snapshot.executable.Equals(
            $ExpectedWrapperExecutable,
            [StringComparison]::OrdinalIgnoreCase
        )
    ) {
        return $false
    }
    if (
        $snapshot.command_line.IndexOf(
            $RepoRoot,
            [StringComparison]::OrdinalIgnoreCase
        ) -lt 0 -or
        $snapshot.command_line.IndexOf(
            [string]$expected.marker,
            [StringComparison]::OrdinalIgnoreCase
        ) -lt 0
    ) {
        return $false
    }
    return $true
}

function Invoke-RuntimeProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Token
    )

    try {
        $headers = @{ 'X-ZTA35G-Runtime-Token' = $Token }
        $live = Invoke-RestMethod `
            -Uri 'http://127.0.0.1:8100/internal/v1/health/live' `
            -Headers $headers `
            -TimeoutSec 2
        $ready = Invoke-RestMethod `
            -Uri 'http://127.0.0.1:8100/internal/v1/health/ready' `
            -Headers $headers `
            -TimeoutSec 2
        return (
            $live.runtime_contract_version -eq '1.0' -and
            $live.status -eq 'LIVE' -and
            $ready.runtime_contract_version -eq '1.0' -and
            $ready.status -eq 'READY' -and
            $ready.model_loaded -eq $true -and
            $ready.device.kind -eq 'cpu' -and
            $ready.supported_tool.tool_id -eq 'zta35g_sem_virtual_lab' -and
            $ready.supported_tool.tool_version -eq '0.1.0' -and
            $ready.supported_tool.schema_version -eq '1.0' -and
            $ready.model_bundle_id -eq 'mock-zta35g-bundle'
        )
    }
    catch {
        return $false
    }
}

function Invoke-BackendProbe {
    try {
        $live = Invoke-RestMethod `
            -Uri 'http://127.0.0.1:8000/api/v1/health/live' `
            -TimeoutSec 2
        $ready = Invoke-RestMethod `
            -Uri 'http://127.0.0.1:8000/api/v1/health/ready' `
            -TimeoutSec 2
        $components = @{}
        foreach ($component in @($ready.components)) {
            $components[[string]$component.name] = [string]$component.status
        }
        return (
            $live.status -eq 'LIVE' -and
            $ready.status -eq 'READY' -and
            $components['postgresql'] -eq 'AVAILABLE' -and
            $components['object_storage'] -eq 'AVAILABLE'
        )
    }
    catch {
        return $false
    }
}

function Invoke-FrontendProbe {
    try {
        $root = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri 'http://127.0.0.1:3000/' `
            -TimeoutSec 2
        $proxy = Invoke-RestMethod `
            -Uri 'http://127.0.0.1:3000/api/v1/health/live' `
            -TimeoutSec 2
        return $root.StatusCode -eq 200 -and $proxy.status -eq 'LIVE'
    }
    catch {
        return $false
    }
}

function Wait-ForProbe {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Probe,
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [int]$TimeoutSeconds = 60
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if (& $Probe) {
            return
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "$Name health check timed out."
}

function Test-RecordedStack {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State,
        [Parameter(Mandatory = $true)]
        [string]$RuntimeToken,
        [Parameter(Mandatory = $true)]
        [object]$DockerIdentity
    )

    try {
        if (-not (Test-StateStructure -State $State -RequireComplete)) {
            return $false
        }
        if (
            -not (
                Test-DockerIdentityMatch `
                    -Expected ([PSCustomObject]@{
                        context = [string]$State.docker_context
                        engine_id = [string]$State.docker_engine_id
                    }) `
                    -Actual $DockerIdentity
            )
        ) {
            return $false
        }
        $processes = @($State.process)
        foreach ($role in @('runtime', 'backend', 'frontend')) {
            $records = @($processes | Where-Object { $_.role -eq $role })
            if ($records.Count -ne 1 -or -not (Test-ManagedProcess -Record $records[0])) {
                return $false
            }
        }
        foreach ($service in @('postgresql', 'minio')) {
            if (-not (Test-ComposeServiceRunning -Service $service)) {
                return $false
            }
        }
        return (
            (Invoke-RuntimeProbe -Token $RuntimeToken) -and
            (Invoke-BackendProbe) -and
            (Invoke-FrontendProbe)
        )
    }
    catch {
        return $false
    }
}

function Invoke-AlembicReadiness {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Python
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        # Alembic writes normal INFO records to stderr. Windows PowerShell 5.1
        # must judge these native calls by exit code, not by the stderr stream.
        $ErrorActionPreference = 'Continue'
        $headsOutput = @(& $Python -m alembic -c backend/alembic.ini heads 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw 'Alembic heads failed.'
        }
        $heads = @(
            $headsOutput |
                ForEach-Object { [string]$_ } |
                Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
        )
        if ($heads.Count -ne 1 -or $heads[0] -notmatch '^(\S+) \(head\)$') {
            throw 'Alembic must have exactly one head.'
        }
        $headRevision = $Matches[1]

        & $Python -m alembic -c backend/alembic.ini upgrade head 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Alembic upgrade head failed.'
        }

        $currentOutput = @(& $Python -m alembic -c backend/alembic.ini current 2>&1)
        if ($LASTEXITCODE -ne 0) {
            throw 'Alembic current failed.'
        }
        $current = @(
            $currentOutput |
                ForEach-Object { [string]$_ } |
                Where-Object { $_ -match '^(\S+) \(head\)$' }
        )
        if ($current.Count -ne 1 -or $current[0] -notmatch '^(\S+) \(head\)$') {
            throw 'Alembic current did not report one head.'
        }
        if ($Matches[1] -ne $headRevision) {
            throw 'Alembic current is not the repository head.'
        }

        & $Python -m alembic -c backend/alembic.ini check 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Alembic check failed.'
        }
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Ensure-ConfiguredBucket {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Python
    )

    $previousPythonPath = [Environment]::GetEnvironmentVariable(
        'PYTHONPATH',
        [EnvironmentVariableTarget]::Process
    )
    $hadPythonPath = $null -ne $previousPythonPath
    try {
        [Environment]::SetEnvironmentVariable(
            'PYTHONPATH',
            (Join-Path $RepoRoot 'backend\src'),
            [EnvironmentVariableTarget]::Process
        )
        $code = @'
from materialsagent.application.bootstrap import ensure_object_storage_bucket
from materialsagent.infrastructure.config import load_settings
from materialsagent.infrastructure.storage.minio import create_minio_storage

storage = create_minio_storage(load_settings())
try:
    ensure_object_storage_bucket(storage)
finally:
    storage.close()
'@
        & $Python -c $code 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw 'Configured MinIO bucket bootstrap failed.'
        }
    }
    finally {
        if ($hadPythonPath) {
            [Environment]::SetEnvironmentVariable(
                'PYTHONPATH',
                $previousPythonPath,
                [EnvironmentVariableTarget]::Process
            )
        }
        else {
            [Environment]::SetEnvironmentVariable(
                'PYTHONPATH',
                $null,
                [EnvironmentVariableTarget]::Process
            )
        }
    }
}

function Invoke-NativeTaskKill {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    $existing = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        return $true
    }
    $existing.Dispose()

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & taskkill.exe /PID ([string]$ProcessId) /T /F 2>&1 | Out-Null
        $taskkillExitCode = $LASTEXITCODE
    }
    catch {
        $taskkillExitCode = -1
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($taskkillExitCode -ne 0) {
        return $false
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ([DateTime]::UtcNow -lt $deadline) {
        $remaining = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
        if ($null -eq $remaining) {
            return $true
        }
        $remaining.Dispose()
        Start-Sleep -Milliseconds 200
    }
    $remaining = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $remaining) {
        return $true
    }
    $remaining.Dispose()
    return $false
}

function New-ManagedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Role,
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList,
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedCommandMarker,
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [Parameter(Mandatory = $true)]
        [string]$StdoutRelative,
        [Parameter(Mandatory = $true)]
        [string]$StderrRelative,
        [Parameter(Mandatory = $true)]
        [hashtable]$ControlledEnvironment,
        [Parameter(Mandatory = $true)]
        [AllowEmptyCollection()]
        [Collections.Generic.List[object]]$OwnershipRecords
    )

    if (
        -not $ExpectedProcesses.ContainsKey($Role) -or
        [string]$ExpectedCommandMarker -ne
            [string]$ExpectedProcesses[$Role].marker -or
        $Port -ne [int]$ExpectedProcesses[$Role].port
    ) {
        throw "$Role process metadata does not match its fixed role definition."
    }

    $stdout = Join-Path $RepoRoot $StdoutRelative
    $stderr = Join-Path $RepoRoot $StderrRelative

    $commandParts = New-Object 'System.Collections.Generic.List[string]'
    foreach ($value in @($FilePath) + $ArgumentList) {
        if (
            $value.Contains('"') -or
            $value.Contains("`r") -or
            $value.Contains("`n")
        ) {
            throw "$Role process argument contains an unsupported character."
        }
        $commandParts.Add('"{0}"' -f $value)
    }
    $targetCommand = $commandParts -join ' '
    $commandLine = (
        '"{0}" /d /s /c "{1} 1>"{2}" 2>"{3}""' -f
        $ExpectedWrapperExecutable,
        $targetCommand,
        $stdout,
        $stderr
    )

    $processEnvironment = @(
        New-RoleSpecificChildEnvironment `
            -Role $Role `
            -ControlledEnvironment $ControlledEnvironment
    )
    $startup = New-CimInstance `
        -ClassName Win32_ProcessStartup `
        -ClientOnly `
        -Property @{
            EnvironmentVariables = [string[]]$processEnvironment
            ShowWindow = [uint16]0
        }
    $created = Invoke-CimMethod `
        -ClassName Win32_Process `
        -MethodName Create `
        -Arguments @{
            CommandLine = $commandLine
            CurrentDirectory = $WorkingDirectory
            ProcessStartupInformation = $startup
        }
    if ([uint32]$created.ReturnValue -ne 0 -or [uint32]$created.ProcessId -eq 0) {
        throw (
            "{0} process could not be created (win32_code={1})." -f
            $Role,
            $created.ReturnValue
        )
    }

    $processId = [int]$created.ProcessId
    $startedAt = [DateTime]::UtcNow.ToString('o')
    $createdProcess = Get-Process -Id $processId -ErrorAction SilentlyContinue
    if ($null -ne $createdProcess) {
        try {
            $startedAt = $createdProcess.StartTime.ToUniversalTime().ToString('o')
        }
        finally {
            $createdProcess.Dispose()
        }
    }
    $provisionalRecord = [PSCustomObject]@{
        role = $Role
        pid = $processId
        process_start_time = $startedAt
        executable = $ExpectedWrapperExecutable
        expected_command_marker = [string]$ExpectedProcesses[$Role].marker
        port = [int]$ExpectedProcesses[$Role].port
        stdout_log = $StdoutRelative.Replace([char]92, [char]47)
        stderr_log = $StderrRelative.Replace([char]92, [char]47)
    }
    $OwnershipRecords.Add($provisionalRecord)
    try {
        Start-Sleep -Milliseconds 200
        $snapshot = Get-ProcessSnapshot -ProcessId $processId
        if ($null -eq $snapshot) {
            throw "$Role process metadata could not be collected."
        }
        if (
            -not $snapshot.executable.Equals(
                $ExpectedWrapperExecutable,
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            $snapshot.command_line.IndexOf(
                $RepoRoot,
                [StringComparison]::OrdinalIgnoreCase
            ) -lt 0 -or
            $snapshot.command_line.IndexOf(
                $ExpectedProcesses[$Role].marker,
                [StringComparison]::OrdinalIgnoreCase
            ) -lt 0
        ) {
            throw "$Role process metadata did not match the managed wrapper."
        }
        $provisionalRecord.executable = $snapshot.executable
        $provisionalRecord.process_start_time = $snapshot.start_time.ToString('o')
    }
    catch {
        $rolledBack = Invoke-NativeTaskKill -ProcessId $processId
        if ($rolledBack) {
            [void]$OwnershipRecords.Remove($provisionalRecord)
            throw
        }
        throw "$Role process metadata failed and exact-PID rollback failed."
    }
    return $provisionalRecord
}

function Write-RuntimeLauncher {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $content = @'
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$RepoRoot,
    [Parameter(Mandatory = $true)]
    [string]$Python,
    [Parameter(Mandatory = $true)]
    [string]$Entry
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$token = $env:ZTA35G_RUNTIME_TOKEN
if ([string]::IsNullOrWhiteSpace($token)) {
    exit 41
}
$env:ZTA35G_RUNTIME_TOKEN = $token
$env:ZTA35G_RUNTIME_PORT = '8100'
& $Python $Entry
exit $LASTEXITCODE
'@
    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($Path, $content, $encoding)
}

function Stop-RecordedProcessSafely {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Record
    )

    $process = Get-Process -Id ([int]$Record.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $true
    }
    $process.Dispose()
    if (-not (Test-ManagedProcess -Record $Record)) {
        Write-Host ("PROCESS_OWNERSHIP_NOT_VERIFIED role={0} pid={1}" -f $Record.role, $Record.pid)
        return $false
    }
    return Invoke-NativeTaskKill -ProcessId ([int]$Record.pid)
}

function Stop-ComposeServiceSafely {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Service
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & docker compose @ComposeArguments stop $Service 2>&1 | Out-Null
        $composeExitCode = $LASTEXITCODE
    }
    catch {
        $composeExitCode = -1
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    return $composeExitCode -eq 0
}

function Invoke-FailureCleanup {
    param(
        [object[]]$Processes,
        [object[]]$DockerServices,
        [object]$DockerIdentity
    )

    $remainingProcesses = New-Object 'System.Collections.Generic.List[object]'
    $remainingDockerServices = New-Object 'System.Collections.Generic.List[object]'
    foreach ($role in @('frontend', 'backend', 'runtime')) {
        foreach ($record in @($Processes | Where-Object { $_.role -eq $role })) {
            if (-not (Stop-RecordedProcessSafely -Record $record)) {
                $remainingProcesses.Add($record)
            }
        }
    }
    $ownedDockerServices = @(
        $DockerServices |
            Where-Object { $_.started_by_this_run -eq $true }
    )
    $dockerIdentityVerified = $ownedDockerServices.Count -eq 0
    if (-not $dockerIdentityVerified -and $null -ne $DockerIdentity) {
        try {
            $currentDockerIdentity = Get-LocalDockerIdentity
            $dockerIdentityVerified = Test-DockerIdentityMatch `
                -Expected $DockerIdentity `
                -Actual $currentDockerIdentity
        }
        catch {
            $dockerIdentityVerified = $false
        }
    }
    foreach ($service in @('minio', 'postgresql')) {
        foreach (
            $record in @(
                $ownedDockerServices |
                    Where-Object {
                        $_.service -eq $service
                    }
                )
        ) {
            if (-not $dockerIdentityVerified) {
                Write-Host (
                    "DOCKER_OWNERSHIP_NOT_VERIFIED service={0}" -f
                    $record.service
                )
                $remainingDockerServices.Add($record)
            }
            elseif (-not (Stop-ComposeServiceSafely -Service $record.service)) {
                Write-Host (
                    "DOCKER_CLEANUP_FAILED service={0}" -f
                    $record.service
                )
                $remainingDockerServices.Add($record)
            }
        }
    }
    return [PSCustomObject]@{
        success = (
            $remainingProcesses.Count -eq 0 -and
            $remainingDockerServices.Count -eq 0
        )
        process = $remainingProcesses.ToArray()
        docker = $remainingDockerServices.ToArray()
    }
}

function Write-StateAtomically {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State
    )

    New-Item -ItemType Directory -Path $StateRoot -Force | Out-Null
    $temporaryPath = Join-Path $StateRoot ("state.{0}.tmp" -f $State.run_id)
    $json = $State | ConvertTo-Json -Depth 8
    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($temporaryPath, $json, $encoding)
    Move-Item -LiteralPath $temporaryPath -Destination $StatePath -Force
}

function Write-RecoveryState {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RunId,
        [Parameter(Mandatory = $true)]
        [string]$Python,
        [Parameter(Mandatory = $true)]
        [object]$DockerIdentity,
        [object[]]$Processes,
        [object[]]$DockerServices
    )

    $state = [ordered]@{
        schema_version = '1'
        run_id = $RunId
        repo_root = $RepoRoot
        created_at = [DateTime]::UtcNow.ToString('o')
        python_executable = $Python
        docker_engine_id = [string]$DockerIdentity.engine_id
        docker_context = [string]$DockerIdentity.context
        process = @($Processes)
        docker = @($DockerServices)
    }
    if (-not (Test-StateStructure -State $state)) {
        throw 'Recovery state structure is invalid.'
    }
    Write-StateAtomically -State $state
}

function Invoke-MockStackStart {
    Set-Location -LiteralPath $RepoRoot
    $managedProcesses = New-Object 'System.Collections.Generic.List[object]'
    $dockerServices = New-Object 'System.Collections.Generic.List[object]'
    $currentStage = 'initialization'
    $runtimeLauncherPath = $null
    $python = $null
    $dockerIdentity = $null
    $runId = [Guid]::NewGuid().ToString('N')
    $controlledEnvironmentBackup = $null
    $controlledEnvironmentEntered = $false

    try {
    $currentStage = 'python_resolution'
    $python = Resolve-BackendPython
    Assert-PythonRuntime -Python $python
    $currentStage = 'configuration'
    $environment = Read-SafeDotEnv -Path (Join-Path $RepoRoot '.env')
    $controlledEnvironmentBackup = Enter-ControlledEnvironment `
        -Environment $environment
    $controlledEnvironmentEntered = $true
    Assert-M11ALocalConfiguration `
        -Environment $environment `
        -Python $python
    $runtimeToken = $environment['ZTA35G_RUNTIME_TOKEN']

    $currentStage = 'docker_validation'
    $dockerCommand = Get-Command 'docker' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $dockerCommand) {
        throw 'Docker was not found.'
    }
    $dockerIdentity = Get-LocalDockerIdentity
    & docker version 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker daemon is unavailable.'
    }
    & docker compose version 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker Compose is unavailable.'
    }

    $currentStage = 'existing_state_validation'
    if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
        try {
            $existingState = Get-Content -Raw -Encoding utf8 -LiteralPath $StatePath |
                ConvertFrom-Json
        }
        catch {
            Write-Output 'STALE_OR_INVALID_MOCK_STACK_STATE'
            return 2
        }
        if (
            Test-RecordedStack `
                -State $existingState `
                -RuntimeToken $runtimeToken `
                -DockerIdentity $dockerIdentity
        ) {
            Write-Output 'MOCK_STACK_ALREADY_RUNNING'
            return 0
        }
        Write-Output 'STALE_OR_INVALID_MOCK_STACK_STATE'
        Write-Output 'Run scripts/dev/stop-mock-stack.ps1 for a safe ownership check.'
        return 2
    }

    $currentStage = 'port_validation'
    $occupiedPorts = @(
        3000, 8000, 8100 |
            Where-Object { Test-PortInUse -Port $_ }
    )
    if ($occupiedPorts.Count -gt 0) {
        Write-Output ("PORT_ALREADY_IN_USE ports={0}" -f ($occupiedPorts -join ','))
        return 3
    }

    $currentStage = 'frontend_dependency_validation'
    $npm = Get-Command 'npm.cmd' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $npm) {
        throw 'npm was not found.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'frontend\node_modules\.bin\vite.cmd') -PathType Leaf)) {
        throw 'Existing frontend dependencies are unavailable; dependency installation is not permitted.'
    }
    & $npm.Source --version 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw 'npm is not runnable.'
    }

    $currentStage = 'compose_ownership_snapshot'
    foreach ($service in @('postgresql', 'minio')) {
        $preexisting = Test-ComposeServiceRunning -Service $service
        $dockerServices.Add(
            [PSCustomObject]@{
                service = $service
                preexisting = $preexisting
                started_by_this_run = $false
            }
        )
    }

    $currentStage = 'compose_startup'
    foreach ($record in $dockerServices) {
        if (-not $record.preexisting) {
            # Once compose up is attempted, a previously stopped service remains
            # potential recovery ownership until a successful query proves its
            # actual post-start state.
            $record.started_by_this_run = $true
        }
    }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & docker compose @ComposeArguments up -d --wait postgresql minio 2>&1 |
            Out-Null
        $composeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    $ownershipReconciliationFailed = $false
    foreach ($record in $dockerServices) {
        try {
            $running = Test-ComposeServiceRunning -Service $record.service
            $record.started_by_this_run = (-not $record.preexisting) -and $running
        }
        catch {
            $ownershipReconciliationFailed = $true
        }
    }
    if ($ownershipReconciliationFailed) {
        throw 'Docker Compose ownership reconciliation failed.'
    }
    if ($composeExitCode -ne 0) {
        throw 'Docker Compose dependency startup failed.'
    }
    foreach ($record in $dockerServices) {
        if (-not (Test-ComposeServiceRunning -Service $record.service)) {
            throw "Compose service is not running: $($record.service)"
        }
    }

    $currentStage = 'alembic'
    Invoke-AlembicReadiness -Python $python
    $currentStage = 'bucket_bootstrap'
    Ensure-ConfiguredBucket -Python $python

    $currentStage = 'runtime_startup'
    $runRelative = "tmp/m11-mock-stack/$runId"
    $runPath = Join-Path $RepoRoot $runRelative
    New-Item -ItemType Directory -Path $runPath -Force | Out-Null

    $runtimeMain = Join-Path $RepoRoot 'mock-runtime\src\materialsagent_mock_runtime\main.py'
    $runtimeLauncherPath = Join-Path $runPath 'runtime-launcher.ps1'
    Write-RuntimeLauncher -Path $runtimeLauncherPath
    $powershellExecutable = Resolve-ExecutablePath -Path 'powershell.exe'
    $runtimeProcess = New-ManagedProcess `
        -Role 'runtime' `
        -FilePath $powershellExecutable `
        -ArgumentList @(
            '-NoProfile'
            '-NonInteractive'
            '-ExecutionPolicy'
            'Bypass'
            '-File'
            $runtimeLauncherPath
            '-RepoRoot'
            $RepoRoot
            '-Python'
            $python
            '-Entry'
            $runtimeMain
        ) `
        -WorkingDirectory $RepoRoot `
        -ExpectedCommandMarker 'materialsagent_mock_runtime\main.py' `
        -Port 8100 `
        -StdoutRelative "$runRelative/runtime.stdout.log" `
        -StderrRelative "$runRelative/runtime.stderr.log" `
        -ControlledEnvironment $environment `
        -OwnershipRecords $managedProcesses
    Wait-ForProbe `
        -Name 'Mock Runtime' `
        -Probe { Invoke-RuntimeProbe -Token $runtimeToken }
    Remove-Item -LiteralPath $runtimeLauncherPath -Force
    $runtimeLauncherPath = $null

    $currentStage = 'backend_startup'
    $backendProcess = New-ManagedProcess `
        -Role 'backend' `
        -FilePath $python `
        -ArgumentList @(
            '-m'
            'uvicorn'
            'materialsagent.main:create_app'
            '--factory'
            '--app-dir'
            (Join-Path $RepoRoot 'backend\src')
            '--host'
            '127.0.0.1'
            '--port'
            '8000'
        ) `
        -WorkingDirectory $RepoRoot `
        -ExpectedCommandMarker 'materialsagent.main:create_app' `
        -Port 8000 `
        -StdoutRelative "$runRelative/backend.stdout.log" `
        -StderrRelative "$runRelative/backend.stderr.log" `
        -ControlledEnvironment $environment `
        -OwnershipRecords $managedProcesses
    Wait-ForProbe -Name 'Backend' -Probe { Invoke-BackendProbe }

    $currentStage = 'frontend_startup'
    $frontendProcess = New-ManagedProcess `
        -Role 'frontend' `
        -FilePath $npm.Source `
        -ArgumentList @(
            '--prefix'
            (Join-Path $RepoRoot 'frontend')
            'run'
            'dev'
            '--'
            '--port'
            '3000'
            '--strictPort'
        ) `
        -WorkingDirectory $RepoRoot `
        -ExpectedCommandMarker '--strictPort' `
        -Port 3000 `
        -StdoutRelative "$runRelative/frontend.stdout.log" `
        -StderrRelative "$runRelative/frontend.stderr.log" `
        -ControlledEnvironment $environment `
        -OwnershipRecords $managedProcesses
    Wait-ForProbe -Name 'Frontend' -Probe { Invoke-FrontendProbe }

    $currentStage = 'state_persistence'
    $state = [ordered]@{
        schema_version = '1'
        run_id = $runId
        repo_root = $RepoRoot
        created_at = [DateTime]::UtcNow.ToString('o')
        python_executable = $python
        docker_engine_id = [string]$dockerIdentity.engine_id
        docker_context = [string]$dockerIdentity.context
        process = $managedProcesses.ToArray()
        docker = $dockerServices.ToArray()
    }
    if (-not (Test-StateStructure -State $state -RequireComplete)) {
        throw 'Normal running state structure is invalid.'
    }
    Write-StateAtomically -State $state

    Write-Output 'MOCK_STACK_STARTED'
    Write-Output 'frontend=http://127.0.0.1:3000'
    Write-Output 'backend=http://127.0.0.1:8000'
    Write-Output 'runtime=http://127.0.0.1:8100'
    Write-Output 'state=tmp/m11-mock-stack/state.json'
    Write-Output ("logs={0}" -f $runRelative)
    return 0
    }
    catch {
    Write-Output (
        "MOCK_STACK_START_FAILED stage={0} error={1}" -f
        $currentStage,
        $_.Exception.Message
    )
    $cleanup = Invoke-FailureCleanup `
        -Processes $managedProcesses.ToArray() `
        -DockerServices $dockerServices.ToArray() `
        -DockerIdentity $dockerIdentity
    if (
        $null -ne $runtimeLauncherPath -and
        (Test-Path -LiteralPath $runtimeLauncherPath -PathType Leaf)
    ) {
        Remove-Item `
            -LiteralPath $runtimeLauncherPath `
            -Force `
            -ErrorAction SilentlyContinue
    }
    if (-not $cleanup.success) {
        Write-RecoveryState `
            -RunId $runId `
            -Python $python `
            -DockerIdentity $dockerIdentity `
            -Processes @($cleanup.process) `
            -DockerServices @($cleanup.docker)
        Write-Output 'MOCK_STACK_START_CLEANUP_INCOMPLETE'
        Write-Output 'state=tmp/m11-mock-stack/state.json'
    }
    return 1
    }
    finally {
        if ($controlledEnvironmentEntered) {
            Restore-ControlledEnvironment -Backup $controlledEnvironmentBackup
        }
    }
}

if (-not $LoadFunctionsOnly) {
    $startResult = @(Invoke-MockStackStart)
    if ($startResult.Count -eq 0) {
        Write-Output 'MOCK_STACK_START_FAILED stage=internal error=No exit code was returned.'
        exit 1
    }
    if ($startResult.Count -gt 1) {
        $startResult[0..($startResult.Count - 2)] | Write-Output
    }
    exit ([int]$startResult[-1])
}
