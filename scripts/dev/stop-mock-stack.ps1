[CmdletBinding()]
param()

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
        [object]$State,
        [Parameter(Mandatory = $true)]
        [object]$Actual
    )

    return (
        (Test-SafeDockerIdentityValue -Value ([string]$State.docker_context)) -and
        (
            Test-SafeDockerIdentityValue `
                -Value ([string]$State.docker_engine_id)
        ) -and
        ([string]$State.docker_context).Equals(
            [string]$Actual.context,
            [StringComparison]::Ordinal
        ) -and
        ([string]$State.docker_engine_id).Equals(
            [string]$Actual.engine_id,
            [StringComparison]::Ordinal
        )
    )
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

function Test-StateStructure {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State
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
        foreach ($record in @($State.process)) {
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
        foreach ($record in @($State.docker)) {
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
        $expectedExecutable = [IO.Path]::GetFullPath([string]$Record.executable)
    }
    catch {
        return $false
    }
    if (
        -not $expectedExecutable.Equals(
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

function Stop-RecordedProcessSafely {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Record
    )

    $process = Get-Process -Id ([int]$Record.pid) -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        Write-Host ("PROCESS_ALREADY_STOPPED role={0} pid={1}" -f $Record.role, $Record.pid)
        return $true
    }
    $process.Dispose()

    if (-not (Test-ManagedProcess -Record $Record)) {
        Write-Host ("PROCESS_OWNERSHIP_NOT_VERIFIED role={0} pid={1}" -f $Record.role, $Record.pid)
        return $false
    }

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & taskkill.exe /PID ([string]$Record.pid) /T /F 2>&1 | Out-Null
        $taskkillExitCode = $LASTEXITCODE
    }
    catch {
        $taskkillExitCode = -1
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($taskkillExitCode -ne 0) {
        Write-Host ("PROCESS_STOP_FAILED role={0} pid={1}" -f $Record.role, $Record.pid)
        return $false
    }

    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while ([DateTime]::UtcNow -lt $deadline) {
        $remaining = Get-Process -Id ([int]$Record.pid) -ErrorAction SilentlyContinue
        if ($null -eq $remaining) {
            Write-Host ("PROCESS_STOPPED role={0} pid={1}" -f $Record.role, $Record.pid)
            return $true
        }
        $remaining.Dispose()
        Start-Sleep -Milliseconds 200
    }
    Write-Host ("PROCESS_STOP_FAILED role={0} pid={1}" -f $Record.role, $Record.pid)
    return $false
}

Set-Location -LiteralPath $RepoRoot

if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
    $occupiedPorts = @(
        3000, 8000, 8100 |
            Where-Object { Test-PortInUse -Port $_ }
    )
    if ($occupiedPorts.Count -gt 0) {
        Write-Output ("UNKNOWN_MOCK_STACK_PORT_OCCUPANCY ports={0}" -f ($occupiedPorts -join ','))
        exit 2
    }
    Write-Output 'MOCK_STACK_NOT_RUNNING'
    exit 0
}

try {
    $state = Get-Content -Raw -Encoding utf8 -LiteralPath $StatePath |
        ConvertFrom-Json
}
catch {
    Write-Output 'PROCESS_OWNERSHIP_NOT_VERIFIED state=unreadable'
    exit 2
}

if (-not (Test-StateStructure -State $state)) {
    Write-Output 'PROCESS_OWNERSHIP_NOT_VERIFIED state=invalid_structure'
    exit 2
}

$allSafe = $true
$processes = @($state.process)
foreach ($role in @('frontend', 'backend', 'runtime')) {
    $records = @($processes | Where-Object { $_.role -eq $role })
    if ($records.Count -eq 0) {
        continue
    }
    if (-not (Stop-RecordedProcessSafely -Record $records[0])) {
        $allSafe = $false
    }
}

$ownedDockerRecords = @(
    @($state.docker) |
        Where-Object { $_.started_by_this_run -eq $true }
)
$dockerIdentityVerified = $ownedDockerRecords.Count -eq 0
if (-not $dockerIdentityVerified) {
    try {
        $currentDockerIdentity = Get-LocalDockerIdentity
        $dockerIdentityVerified = Test-DockerIdentityMatch `
            -State $state `
            -Actual $currentDockerIdentity
    }
    catch {
        $dockerIdentityVerified = $false
    }
}
foreach ($service in @('minio', 'postgresql')) {
    $records = @(
        $ownedDockerRecords |
            Where-Object { $_.service -eq $service }
    )
    if ($records.Count -eq 0) {
        continue
    }
    if ($records.Count -ne 1 -or -not $dockerIdentityVerified) {
        Write-Output ("DOCKER_OWNERSHIP_NOT_VERIFIED service={0}" -f $service)
        $allSafe = $false
        continue
    }
    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & docker compose @ComposeArguments stop $service 2>&1 | Out-Null
        $composeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($composeExitCode -ne 0) {
        Write-Output ("DOCKER_STOP_FAILED service={0}" -f $service)
        $allSafe = $false
    }
    else {
        Write-Output ("DOCKER_SERVICE_STOPPED service={0}" -f $service)
    }
}

if (-not $allSafe) {
    Write-Output 'MOCK_STACK_STOP_INCOMPLETE'
    Write-Output 'state=tmp/m11-mock-stack/state.json'
    exit 2
}

Remove-Item -LiteralPath $StatePath -Force
Write-Output 'MOCK_STACK_STOPPED'
exit 0
