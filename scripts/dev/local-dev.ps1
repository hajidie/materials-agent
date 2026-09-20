[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('Start', 'Stop')]
    [string]$Action = 'Start',
    [ValidateSet('Mock', 'Real')]
    [string]$Runtime = 'Mock',
    [ValidateSet('Mock', 'Provider')]
    [string]$Llm = 'Mock',
    [string]$BackendPython,
    [string]$RuntimePython,
    [string]$MaterialsMlPython,
    [string]$EbsdModelRoot = $env:EBSD_MODEL_ROOT,
    [ValidateRange(10, 900)]
    [int]$ReadyTimeoutSeconds = 300,
    [switch]$LoadFunctionsOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$RepoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$StateRoot = Join-Path $RepoRoot 'tmp\local-dev'
$StatePath = Join-Path $StateRoot 'state.json'
$ComposeFile = Join-Path $RepoRoot 'docker-compose.yml'
$ComposeArguments = @(
    'compose',
    '--project-name',
    'materialsagent',
    '--file',
    $ComposeFile
)
$ExpectedRoles = @{
    runtime = @{ port = 8100 }
    ml_service = @{ port = 8200; marker = 'materials_ml_service.api' }
    ml_worker = @{ port = 0; marker = 'materials_ml_service.worker' }
    backend = @{ port = 8000; marker = 'materialsagent.main:create_app' }
    frontend = @{ port = 3000; marker = '--strictPort' }
}
$ForbiddenStateNames = @(
    'DEEPSEEK_API_KEY',
    'DASHSCOPE_API_KEY',
    'ZTA35G_RUNTIME_TOKEN',
    'POSTGRES_PASSWORD',
    'MINIO_SECRET_KEY',
    'ML_DATABASE_URL',
    'ML_MINIO_SECRET_KEY',
    'ML_RESOURCE_TOKEN',
    'ML_WORKER_TOKEN',
    'ML_MCP_TOKEN',
    'TIMELINE_CURSOR_SIGNING_KEY'
)

function Get-RequiredPorts {
    param(
        [bool]$MaterialsMlEnabled = $false
    )

    $ports = @(5432, 9000, 9001, 8100, 8000, 3000)
    if ($MaterialsMlEnabled) {
        $ports += 8200
    }
    return $ports
}

function Get-ExpectedCommandMarker {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('runtime', 'ml_service', 'ml_worker', 'backend', 'frontend')]
        [string]$Role,
        [Parameter(Mandatory = $true)]
        [ValidateSet('mock', 'real')]
        [string]$RuntimeMode
    )

    if ($Role -eq 'runtime') {
        return $(if ($RuntimeMode -eq 'real') {
            'materialsagent_zta35g_runtime.main'
        }
        else {
            'materialsagent_mock_runtime.main'
        })
    }
    return [string]$ExpectedRoles[$Role].marker
}

function Get-ConfiguredMaterialsMlEnabled {
    param(
        [string]$Path = (Join-Path $RepoRoot '.env')
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $false
    }
    $featureNames = @(
        'ENABLE_DEV_MATERIALS_ML_TOOLS',
        'ENABLE_MATERIALS_ML_RESOURCES',
        'ENABLE_MATERIALS_ML_RESOURCE_CONTEXT'
    )
    $values = @{}
    foreach ($name in $featureNames) {
        $values[$name] = $false
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
        $name = $trimmed.Substring(0, $separator).Trim()
        if ($featureNames -notcontains $name) {
            continue
        }
        if ($seen.ContainsKey($name)) {
            throw "LOCAL_DEV_CONFIGURATION_INVALID duplicate=$name source=root_dotenv"
        }
        $seen[$name] = $true
        $value = $trimmed.Substring($separator + 1).Trim()
        if ($value -notmatch '^(?i:true|false)$') {
            throw "LOCAL_DEV_CONFIGURATION_INVALID name=$name source=root_dotenv"
        }
        $values[$name] = $value.Equals('true', [StringComparison]::OrdinalIgnoreCase)
    }
    foreach ($name in $featureNames) {
        $environmentValue = [Environment]::GetEnvironmentVariable(
            $name,
            [EnvironmentVariableTarget]::Process
        )
        if ($null -eq $environmentValue) {
            continue
        }
        if ($environmentValue -notmatch '^(?i:true|false)$') {
            throw "LOCAL_DEV_CONFIGURATION_INVALID name=$name source=process_environment"
        }
        $values[$name] = $environmentValue.Equals(
            'true',
            [StringComparison]::OrdinalIgnoreCase
        )
    }
    return $values.Values -contains $true
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

function Get-OccupiedPorts {
    param(
        [Parameter(Mandatory = $true)]
        [int[]]$Ports,
        [scriptblock]$Probe = { param($port) Test-PortInUse -Port $port }
    )

    foreach ($port in $Ports) {
        if (& $Probe $port) {
            Write-Output $port
        }
    }
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
            throw 'LOCAL_DEV_EXECUTABLE_NOT_FOUND'
        }
        $candidate = $command.Source
    }
    $resolved = Resolve-Path -LiteralPath $candidate -ErrorAction SilentlyContinue
    if ($null -eq $resolved -or -not (Test-Path -LiteralPath $resolved.Path -PathType Leaf)) {
        throw 'LOCAL_DEV_EXECUTABLE_NOT_FOUND'
    }
    return [IO.Path]::GetFullPath($resolved.Path)
}

function Resolve-CondaExecutable {
    $candidates = New-Object 'System.Collections.Generic.List[string]'
    $pathCommand = Get-Command 'conda.exe' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -ne $pathCommand) {
        $candidates.Add($pathCommand.Source)
    }
    if (-not [string]::IsNullOrWhiteSpace($env:CONDA_EXE)) {
        $candidates.Add($env:CONDA_EXE)
    }
    foreach ($candidate in @(
        'D:\ProgramData\Anaconda3\Scripts\conda.exe',
        (Join-Path $env:ProgramData 'Anaconda3\Scripts\conda.exe'),
        (Join-Path $env:ProgramData 'Miniconda3\Scripts\conda.exe')
    )) {
        if (-not [string]::IsNullOrWhiteSpace($candidate)) {
            $candidates.Add($candidate)
        }
    }
    $resolved = @(
        $candidates |
            Select-Object -Unique |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
            ForEach-Object { [IO.Path]::GetFullPath($_) }
    )
    if ($resolved.Count -eq 0) {
        throw 'LOCAL_DEV_CONDA_NOT_FOUND'
    }
    return $resolved[0]
}

function Resolve-CondaEnvironmentPython {
    param(
        [Parameter(Mandatory = $true)]
        [string]$EnvironmentName,
        [string]$ExplicitPath
    )

    if (-not [string]::IsNullOrWhiteSpace($ExplicitPath)) {
        return Resolve-ExecutablePath -Path $ExplicitPath
    }

    $conda = Resolve-CondaExecutable
    try {
        $raw = @(& $conda env list --json 2>$null)
        if ($LASTEXITCODE -ne 0) {
            throw 'query failed'
        }
        $environmentList = (($raw | ForEach-Object { [string]$_ }) -join [Environment]::NewLine) |
            ConvertFrom-Json
    }
    catch {
        throw 'LOCAL_DEV_CONDA_ENVIRONMENT_QUERY_FAILED'
    }
    $matches = @(
        $environmentList.envs |
            Where-Object {
                [IO.Path]::GetFileName([string]$_).Equals(
                    $EnvironmentName,
                    [StringComparison]::OrdinalIgnoreCase
                )
            }
    )
    if ($matches.Count -ne 1) {
        throw "LOCAL_DEV_CONDA_ENVIRONMENT_NOT_FOUND name=$EnvironmentName"
    }
    $python = Join-Path ([string]$matches[0]) 'python.exe'
    return Resolve-ExecutablePath -Path $python
}

function Assert-PythonVersion {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Python,
        [Parameter(Mandatory = $true)]
        [string]$ExpectedMajorMinor
    )

    try {
        $version = @(
            & $Python -c `
                'import sys; print(sys.version_info[0], sys.version_info[1], sep=chr(46))' `
                2>$null
        )
    }
    catch {
        throw 'LOCAL_DEV_PYTHON_NOT_RUNNABLE'
    }
    if ($LASTEXITCODE -ne 0 -or ($version -join '').Trim() -ne $ExpectedMajorMinor) {
        throw "LOCAL_DEV_PYTHON_VERSION_MISMATCH expected=$ExpectedMajorMinor"
    }
}

function Invoke-LocalDevPreflightStep {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Component,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Operation
    )

    try {
        return & $Operation
    }
    catch {
        $message = [string]$_.Exception.Message
        if ($message -match '^LOCAL_DEV_[A-Z0-9_]+(?: .*)?$') {
            throw $message
        }
        throw "LOCAL_DEV_PREFLIGHT_FAILED component=$Component"
    }
}

function New-EphemeralRuntimeToken {
    $bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    }
    finally {
        $generator.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-LaunchProfile {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('Mock', 'Real')]
        [string]$Runtime,
        [Parameter(Mandatory = $true)]
        [ValidateSet('Mock', 'Provider')]
        [string]$Llm,
        [Parameter(Mandatory = $true)]
        [string]$RuntimeToken,
        [Parameter(Mandatory = $true)]
        [string]$BackendPython,
        [Parameter(Mandatory = $true)]
        [string]$RuntimePython
    )

    $backendEnvironment = @{
        APP_ENV = 'local'
        POSTGRES_HOST = '127.0.0.1'
        POSTGRES_PORT = '5432'
        MINIO_API_PORT = '9000'
        MINIO_CONSOLE_PORT = '9001'
        MINIO_ENDPOINT = 'http://127.0.0.1:9000'
        LLM_ADAPTER = $Llm.ToLowerInvariant()
        ZTA35G_RUNTIME_URL = 'http://127.0.0.1:8100'
        ZTA35G_RUNTIME_TIMEOUT_SECONDS = '1200'
        ZTA35G_RUNTIME_TOKEN = $RuntimeToken
    }
    if ($Llm -eq 'Mock') {
        $backendEnvironment['DEEPSEEK_API_KEY'] = ''
        $backendEnvironment['DASHSCOPE_API_KEY'] = ''
        $backendEnvironment['M12B_REAL_CALLS_AUTHORIZED'] = ''
        $backendEnvironment['P1B2_GATE4_AUTHORIZED'] = ''
    }
    else {
        $backendEnvironment['DEEPSEEK_API_KEY'] = $null
        $backendEnvironment['DASHSCOPE_API_KEY'] = $null
    }

    $runtimeModule = if ($Runtime -eq 'Real') {
        'materialsagent_zta35g_runtime.main'
    }
    else {
        'materialsagent_mock_runtime.main'
    }
    $runtimeSource = if ($Runtime -eq 'Real') {
        Join-Path $RepoRoot 'zta35g-runtime\src'
    }
    else {
        Join-Path $RepoRoot 'mock-runtime\src'
    }
    $modelRoot = Join-Path $RepoRoot 'SEM\ZTA35G_lab'
    if ($Runtime -eq 'Real' -and -not (Test-Path -LiteralPath $modelRoot -PathType Container)) {
        throw 'LOCAL_DEV_CONFIGURATION_MISSING name=ZTA35G_MODEL_ROOT source=repository_default'
    }
    $runtimeEnvironment = @{
        ZTA35G_RUNTIME_PORT = '8100'
        PYTHONPATH = $runtimeSource
        ZTA35G_RUNTIME_TOKEN = $RuntimeToken
        ZTA35G_MODEL_ROOT = $(if ($Runtime -eq 'Real') { [IO.Path]::GetFullPath($modelRoot) } else { $null })
        EBSD_MODEL_ROOT = $(if ($Runtime -eq 'Real' -and $EbsdModelRoot) { [IO.Path]::GetFullPath($EbsdModelRoot) } else { $null })
        DEEPSEEK_API_KEY = $null
        DASHSCOPE_API_KEY = $null
        POSTGRES_PASSWORD = $null
        MINIO_ACCESS_KEY = $null
        MINIO_SECRET_KEY = $null
        TIMELINE_CURSOR_SIGNING_KEY = $null
    }
    return [PSCustomObject]@{
        runtime_role = $Runtime.ToLowerInvariant()
        llm_role = $Llm.ToLowerInvariant()
        runtime_python = $RuntimePython
        runtime_arguments = @(
            '-m',
            $runtimeModule,
            '--local-dev-root',
            $RepoRoot
        )
        runtime_working_directory = $RepoRoot
        runtime_marker = $runtimeModule
        runtime_environment = $runtimeEnvironment
        backend_python = $BackendPython
        backend_arguments = @(
            '-m',
            'uvicorn',
            'materialsagent.main:create_app',
            '--factory',
            '--app-dir',
            (Join-Path $RepoRoot 'backend\src'),
            '--host',
            '127.0.0.1',
            '--port',
            '8000'
        )
        backend_working_directory = $RepoRoot
        backend_environment = $backendEnvironment
        frontend_environment = @{
            MATERIALSAGENT_BACKEND_ORIGIN = 'http://127.0.0.1:8000'
            DEEPSEEK_API_KEY = $null
            DASHSCOPE_API_KEY = $null
            ZTA35G_RUNTIME_TOKEN = $null
            ZTA35G_MODEL_ROOT = $null
            POSTGRES_PASSWORD = $null
            MINIO_ACCESS_KEY = $null
            MINIO_SECRET_KEY = $null
            TIMELINE_CURSOR_SIGNING_KEY = $null
        }
        compose_environment = @{
            POSTGRES_PORT = '5432'
            MINIO_API_PORT = '9000'
            MINIO_CONSOLE_PORT = '9001'
            DEEPSEEK_API_KEY = $null
            DASHSCOPE_API_KEY = $null
            ZTA35G_RUNTIME_TOKEN = $null
            ZTA35G_MODEL_ROOT = $null
            TIMELINE_CURSOR_SIGNING_KEY = $null
        }
    }
}

function New-MaterialsMlLaunchProfile {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Python,
        [Parameter(Mandatory = $true)]
        [string]$EnvironmentFile,
        [Parameter(Mandatory = $true)]
        [ValidatePattern('^[A-Za-z0-9_-]{32,256}$')]
        [string]$WorkerToken
    )

    $isolatedNames = @(
        'ML_DATABASE_URL',
        'ML_MINIO_ENDPOINT',
        'ML_MINIO_BUCKET',
        'ML_MINIO_ACCESS_KEY',
        'ML_MINIO_SECRET_KEY',
        'ML_MINIO_SECURE',
        'ML_RESOURCE_TOKEN',
        'ML_WORKER_TOKEN',
        'ML_MCP_ENABLED',
        'ML_MCP_TOKEN',
        'ML_NAMESPACE',
        'ML_STORE_ID',
        'ML_ENV_FILE',
        'ML_SERVICE_URL',
        'ML_ADMIN_DATABASE_URL',
        'ML_ADMIN_MINIO_ACCESS_KEY',
        'ML_ADMIN_MINIO_SECRET_KEY',
        'DEEPSEEK_API_KEY',
        'DASHSCOPE_API_KEY',
        'ZTA35G_RUNTIME_TOKEN',
        'POSTGRES_PASSWORD',
        'MINIO_ACCESS_KEY',
        'MINIO_SECRET_KEY'
    )
    $serviceEnvironment = @{}
    $workerEnvironment = @{}
    foreach ($name in $isolatedNames) {
        $serviceEnvironment[$name] = $null
        $workerEnvironment[$name] = $null
    }
    $serviceEnvironment['ML_ENV_FILE'] = $EnvironmentFile
    $workerEnvironment['ML_SERVICE_URL'] = 'http://127.0.0.1:8200'
    $workerEnvironment['ML_WORKER_TOKEN'] = $WorkerToken
    $mlPythonPath = @(
        (Join-Path $RepoRoot 'services\materials_ml\src'),
        (Join-Path $RepoRoot 'packages\materials_storage\src')
    ) -join [IO.Path]::PathSeparator
    $serviceEnvironment['PYTHONPATH'] = $mlPythonPath
    $workerEnvironment['PYTHONPATH'] = $mlPythonPath

    return [PSCustomObject]@{
        python = $Python
        service_arguments = @('-m', 'materials_ml_service.api')
        service_marker = 'materials_ml_service.api'
        service_environment = $serviceEnvironment
        worker_arguments = @('-m', 'materials_ml_service.worker')
        worker_marker = 'materials_ml_service.worker'
        worker_environment = $workerEnvironment
        working_directory = $RepoRoot
    }
}

function Invoke-PythonStdinProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Python,
        [Parameter(Mandatory = $true)]
        [string]$Code,
        [Parameter(Mandatory = $true)]
        [hashtable]$Environment
    )

    return Invoke-WithProcessEnvironment -Values $Environment -Operation {
        # Windows PowerShell 5.1 rewrites quotes in multiline native -c
        # arguments. Stdin keeps the probe identical across 5.1 and 7.x.
        $output = @($Code | & $Python - 2>$null)
        return [PSCustomObject]@{
            exit_code = [int]$LASTEXITCODE
            output = @($output)
        }
    }
}

function Get-MaterialsMlWorkerToken {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Python,
        [Parameter(Mandatory = $true)]
        [string]$EnvironmentFile,
        [Parameter(Mandatory = $true)]
        [hashtable]$Environment,
        [scriptblock]$Probe
    )

    if (
        -not (Test-Path -LiteralPath $EnvironmentFile -PathType Leaf) -or
        [string]$Environment['ML_ENV_FILE'] -cne [string]$EnvironmentFile
    ) {
        throw 'LOCAL_DEV_ML_CONFIGURATION_INVALID'
    }

    if ($null -eq $Probe) {
        $Probe = {
            param($python, $environment)
            $probeCode = @'
from materialsagent.infrastructure.config import load_settings as load_backend_settings
from materials_ml_service.config import load_settings as load_ml_settings

backend = load_backend_settings()
service = load_ml_settings()
if backend.materials_ml_mcp_url != "http://127.0.0.1:8200/mcp":
    raise SystemExit(5)
if backend.enable_materials_ml_resources:
    if backend.materials_ml_resource_token is None or backend.materials_ml_resource_token.get_secret_value() != service.resource_token.get_secret_value():
        raise SystemExit(3)
if backend.enable_dev_materials_ml_tools:
    if not service.mcp_enabled or backend.materials_ml_mcp_token is None or service.mcp_token is None:
        raise SystemExit(4)
    if backend.materials_ml_mcp_token.get_secret_value() != service.mcp_token.get_secret_value():
        raise SystemExit(4)
print(service.worker_token.get_secret_value())
'@
            return Invoke-PythonStdinProbe `
                -Python $python `
                -Code $probeCode `
                -Environment $environment
        }
    }
    $result = & $Probe $Python $Environment
    if ([int]$result.exit_code -ne 0) {
        $message = switch ([int]$result.exit_code) {
            3 { 'LOCAL_DEV_ML_RESOURCE_CREDENTIAL_MISMATCH' }
            4 { 'LOCAL_DEV_ML_MCP_CONFIGURATION_MISMATCH' }
            5 { 'LOCAL_DEV_ML_ENDPOINT_MUST_BE_LOCAL' }
            default { 'LOCAL_DEV_ML_CONFIGURATION_INVALID' }
        }
        throw $message
    }
    $lines = @($result.output | Where-Object { -not [string]::IsNullOrWhiteSpace([string]$_) })
    if ($lines.Count -ne 1 -or [string]$lines[0] -notmatch '^[A-Za-z0-9_-]{32,256}$') {
        throw 'LOCAL_DEV_ML_CONFIGURATION_INVALID'
    }
    return [string]$lines[0]
}

function Invoke-WithProcessEnvironment {
    param(
        [Parameter(Mandatory = $true)]
        [hashtable]$Values,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Operation
    )

    $backup = @{}
    foreach ($name in $Values.Keys) {
        $backup[$name] = [PSCustomObject]@{
            existed = Test-Path -LiteralPath "Env:$name"
            value = [Environment]::GetEnvironmentVariable(
                $name,
                [EnvironmentVariableTarget]::Process
            )
        }
    }
    try {
        foreach ($name in $Values.Keys) {
            if ($null -eq $Values[$name]) {
                Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
            }
            else {
                Set-Item -LiteralPath "Env:$name" -Value ([string]$Values[$name])
            }
        }
        return & $Operation
    }
    finally {
        foreach ($name in $Values.Keys) {
            $item = $backup[$name]
            if ($item.existed) {
                Set-Item -LiteralPath "Env:$name" -Value ([string]$item.value)
            }
            else {
                Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
            }
        }
    }
}

function Invoke-NativeCommandExitCode {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Operation
    )

    $previousErrorActionPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $Operation 2>&1 | Out-Null
        return [int]$LASTEXITCODE
    }
    catch {
        return -1
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
}

function Invoke-ComposeStartSafely {
    param(
        [Parameter(Mandatory = $true)]
        [scriptblock]$Operation
    )

    $composeExitCode = Invoke-NativeCommandExitCode -Operation $Operation
    if ($composeExitCode -ne 0) {
        throw 'LOCAL_DEV_COMPOSE_START_FAILED'
    }
}

function Assert-ProviderRootConfiguration {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BackendPython,
        [scriptblock]$Probe
    )

    if ($null -eq $Probe) {
        $Probe = {
            param($python)
            $probeCode = @'
from materialsagent.infrastructure.config import ConfigurationError, load_settings
from materialsagent.infrastructure.llm.configuration import load_llm_configuration
try:
    settings = load_settings()
except ConfigurationError:
    raise SystemExit(2)
try:
    load_llm_configuration(settings)
except ConfigurationError as error:
    message = str(error)
    if message == "Missing provider credential 'DEEPSEEK_API_KEY'.":
        raise SystemExit(3)
    if message == "Missing provider credential 'DASHSCOPE_API_KEY'.":
        raise SystemExit(4)
    raise SystemExit(2)
'@
            return Invoke-WithProcessEnvironment `
                -Values @{
                    PYTHONPATH = (Join-Path $RepoRoot 'backend\src')
                    LLM_ADAPTER = 'provider'
                    DEEPSEEK_API_KEY = $null
                    DASHSCOPE_API_KEY = $null
                } `
                -Operation {
                    $probeCode | & $python - *> $null
                    return [int]$LASTEXITCODE
                }
        }
    }
    $exitCode = [int](& $Probe $BackendPython)
    if ($exitCode -eq 0) {
        return
    }
    if ($exitCode -eq 2) {
        throw 'LOCAL_DEV_CONFIGURATION_INVALID source=root_dotenv mode=provider'
    }
    if ($exitCode -eq 3) {
        throw 'LOCAL_DEV_CONFIGURATION_MISSING name=DEEPSEEK_API_KEY source=root_dotenv'
    }
    if ($exitCode -eq 4) {
        throw 'LOCAL_DEV_CONFIGURATION_MISSING name=DASHSCOPE_API_KEY source=root_dotenv'
    }
    throw 'LOCAL_DEV_CONFIGURATION_PROBE_FAILED mode=provider'
}

function Test-SafeIdentityValue {
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

function Get-DockerIdentity {
    $docker = Get-Command 'docker.exe' -CommandType Application -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $docker) {
        $docker = Get-Command 'docker' -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
    }
    if ($null -eq $docker) {
        throw 'LOCAL_DEV_DOCKER_NOT_FOUND'
    }

    $contextOutput = @(& $docker.Source context show 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw 'LOCAL_DEV_DOCKER_UNAVAILABLE'
    }
    $context = ($contextOutput -join '').Trim()
    $engineOutput = @(& $docker.Source info --format '{{.ID}}' 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw 'LOCAL_DEV_DOCKER_UNAVAILABLE'
    }
    $engineId = ($engineOutput -join '').Trim()
    if (-not (Test-SafeIdentityValue -Value $context) -or -not (Test-SafeIdentityValue -Value $engineId)) {
        throw 'LOCAL_DEV_DOCKER_IDENTITY_UNAVAILABLE'
    }
    & $docker.Source compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw 'LOCAL_DEV_DOCKER_COMPOSE_UNAVAILABLE'
    }
    return [PSCustomObject]@{
        executable = $docker.Source
        context = $context
        engine_id = $engineId
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
        [string]$State.docker_context -ceq [string]$Actual.context -and
        [string]$State.docker_engine_id -ceq [string]$Actual.engine_id
    )
}

function Test-ComposeServiceRunning {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Docker,
        [Parameter(Mandatory = $true)]
        [ValidateSet('postgresql', 'minio')]
        [string]$Service
    )

    $output = @(& $Docker @ComposeArguments ps --status running --services $Service 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw 'LOCAL_DEV_DOCKER_STATUS_FAILED'
    }
    return @($output | ForEach-Object { ([string]$_).Trim() }) -contains $Service
}

function New-LocalDevState {
    param(
        [Parameter(Mandatory = $true)]
        [string]$RunId,
        [Parameter(Mandatory = $true)]
        [ValidateSet('starting', 'running', 'cleanup_required')]
        [string]$Phase,
        [Parameter(Mandatory = $true)]
        [ValidateSet('Mock', 'Real')]
        [string]$Runtime,
        [Parameter(Mandatory = $true)]
        [ValidateSet('Mock', 'Provider')]
        [string]$Llm,
        [Parameter(Mandatory = $true)]
        [string]$DockerContext,
        [Parameter(Mandatory = $true)]
        [string]$DockerEngineId,
        [AllowEmptyCollection()]
        [object[]]$Processes = @(),
        [AllowEmptyCollection()]
        [object[]]$DockerServices = @()
    )

    return [PSCustomObject][ordered]@{
        schema_version = '1'
        run_id = $RunId
        repo_root = $RepoRoot
        created_at = [DateTime]::UtcNow.ToString('o')
        phase = $Phase
        runtime = $Runtime.ToLowerInvariant()
        llm = $Llm.ToLowerInvariant()
        docker_context = $DockerContext
        docker_engine_id = $DockerEngineId
        process = @($Processes)
        docker = @($DockerServices)
    }
}

function Test-ObjectProperties {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Value,
        [Parameter(Mandatory = $true)]
        [string[]]$Required,
        [Parameter(Mandatory = $true)]
        [string[]]$Allowed
    )

    $names = @($Value.PSObject.Properties.Name)
    foreach ($name in $Required) {
        if ($names -notcontains $name) {
            return $false
        }
    }
    foreach ($name in $names) {
        if ($Allowed -notcontains $name) {
            return $false
        }
    }
    return $true
}

function Test-LocalDevState {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State
    )

    try {
        $top = @(
            'schema_version', 'run_id', 'repo_root', 'created_at', 'phase',
            'runtime', 'llm', 'docker_context', 'docker_engine_id', 'process', 'docker'
        )
        if (-not (Test-ObjectProperties -Value $State -Required $top -Allowed $top)) {
            return $false
        }
        $parsedRunId = [Guid]::Empty
        $createdAt = [DateTime]::MinValue
        if (
            [string]$State.schema_version -ne '1' -or
            -not [Guid]::TryParseExact([string]$State.run_id, 'N', [ref]$parsedRunId) -or
            -not [DateTime]::TryParse([string]$State.created_at, [ref]$createdAt) -or
            -not ([IO.Path]::GetFullPath([string]$State.repo_root)).Equals(
                $RepoRoot,
                [StringComparison]::OrdinalIgnoreCase
            ) -or
            [string]$State.phase -notin @('starting', 'running', 'cleanup_required') -or
            [string]$State.runtime -notin @('mock', 'real') -or
            [string]$State.llm -notin @('mock', 'provider') -or
            -not (Test-SafeIdentityValue -Value ([string]$State.docker_context)) -or
            -not (Test-SafeIdentityValue -Value ([string]$State.docker_engine_id))
        ) {
            return $false
        }

        $processFields = @(
            'role', 'pid', 'process_start_time', 'executable', 'command_marker',
            'port', 'stdout_log', 'stderr_log'
        )
        $roles = New-Object 'System.Collections.Generic.HashSet[string]'
        foreach ($record in @($State.process)) {
            if (-not (Test-ObjectProperties -Value $record -Required $processFields -Allowed $processFields)) {
                return $false
            }
            $role = [string]$record.role
            $expectedMarker = Get-ExpectedCommandMarker `
                -Role $role `
                -RuntimeMode ([string]$State.runtime)
            $start = [DateTime]::MinValue
            if (
                $role -notin @('runtime', 'ml_service', 'ml_worker', 'backend', 'frontend') -or
                -not $roles.Add($role) -or
                [int]$record.pid -le 0 -or
                -not [DateTime]::TryParse([string]$record.process_start_time, [ref]$start) -or
                -not [IO.Path]::IsPathRooted([string]$record.executable) -or
                [string]$record.command_marker -cne $expectedMarker -or
                [int]$record.port -ne [int]$ExpectedRoles[$role].port -or
                [string]::IsNullOrWhiteSpace([string]$record.stdout_log) -or
                [string]::IsNullOrWhiteSpace([string]$record.stderr_log)
            ) {
                return $false
            }
        }

        $dockerFields = @('service', 'started_by_this_run')
        $services = New-Object 'System.Collections.Generic.HashSet[string]'
        foreach ($record in @($State.docker)) {
            if (-not (Test-ObjectProperties -Value $record -Required $dockerFields -Allowed $dockerFields)) {
                return $false
            }
            if (
                [string]$record.service -notin @('postgresql', 'minio') -or
                -not $services.Add([string]$record.service) -or
                $record.started_by_this_run -isnot [bool]
            ) {
                return $false
            }
        }
        return $services.Count -eq 2
    }
    catch {
        return $false
    }
}

function ConvertTo-LocalDevStateJson {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State
    )

    if (-not (Test-LocalDevState -State $State)) {
        throw 'LOCAL_DEV_STATE_INVALID'
    }
    $json = $State | ConvertTo-Json -Depth 8
    foreach ($name in $ForbiddenStateNames) {
        if ($json.Contains($name)) {
            throw 'LOCAL_DEV_STATE_CONTAINS_FORBIDDEN_FIELD'
        }
    }
    return $json
}

function Write-LocalDevState {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State,
        [string]$Path = $StatePath
    )

    $json = ConvertTo-LocalDevStateJson -State $State
    $directory = Split-Path -Parent $Path
    New-Item -ItemType Directory -Path $directory -Force | Out-Null
    $temporary = Join-Path $directory ('.state-{0}.tmp' -f [Guid]::NewGuid().ToString('N'))
    $encoding = New-Object Text.UTF8Encoding($false)
    [IO.File]::WriteAllText($temporary, $json, $encoding)
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Read-LocalDevState {
    param(
        [string]$Path = $StatePath
    )

    try {
        $state = Get-Content -Raw -Encoding UTF8 -LiteralPath $Path | ConvertFrom-Json
    }
    catch {
        throw 'LOCAL_DEV_STATE_UNREADABLE'
    }
    if (-not (Test-LocalDevState -State $state)) {
        throw 'LOCAL_DEV_STATE_INVALID'
    }
    return $state
}

function ConvertTo-NativeArgument {
    param(
        [AllowEmptyString()]
        [string]$Value
    )

    if ($Value -notmatch '[\s"]' -and $Value.Length -gt 0) {
        return $Value
    }
    $builder = New-Object Text.StringBuilder
    [void]$builder.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
            continue
        }
        if ($character -eq '"') {
            [void]$builder.Append(('\' * ($backslashes * 2 + 1)))
            [void]$builder.Append('"')
            $backslashes = 0
            continue
        }
        if ($backslashes -gt 0) {
            [void]$builder.Append(('\' * $backslashes))
            $backslashes = 0
        }
        [void]$builder.Append($character)
    }
    if ($backslashes -gt 0) {
        [void]$builder.Append(('\' * ($backslashes * 2)))
    }
    [void]$builder.Append('"')
    return $builder.ToString()
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
            pid = $ProcessId
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

function Test-ProcessSnapshotMatch {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Record,
        [Parameter(Mandatory = $true)]
        [object]$Snapshot
    )

    try {
        # PowerShell 7 may deserialize ISO JSON timestamps into UTC DateTime values.
        # String-casting them discards the zone and can shift ownership checks by hours.
        $expectedStart = if ($Record.process_start_time -is [DateTime]) {
            $Record.process_start_time.ToUniversalTime()
        } else {
            [DateTime]::Parse([string]$Record.process_start_time).ToUniversalTime()
        }
        $actualStart = ([DateTime]$Snapshot.start_time).ToUniversalTime()
        return (
            [int]$Record.pid -eq [int]$Snapshot.pid -and
            [Math]::Abs(($actualStart - $expectedStart).TotalSeconds) -le 2 -and
            ([IO.Path]::GetFullPath([string]$Record.executable)).Equals(
                [IO.Path]::GetFullPath([string]$Snapshot.executable),
                [StringComparison]::OrdinalIgnoreCase
            ) -and
            ([string]$Snapshot.command_line).IndexOf(
                [string]$Record.command_marker,
                [StringComparison]::OrdinalIgnoreCase
            ) -ge 0 -and
            ([string]$Snapshot.command_line).IndexOf(
                $RepoRoot,
                [StringComparison]::OrdinalIgnoreCase
            ) -ge 0
        )
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

    $snapshot = Get-ProcessSnapshot -ProcessId ([int]$Record.pid)
    if ($null -eq $snapshot) {
        return $false
    }
    return Test-ProcessSnapshotMatch -Record $Record -Snapshot $snapshot
}

function Invoke-TaskKill {
    param(
        [Parameter(Mandatory = $true)]
        [int]$ProcessId
    )

    $existing = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        return $true
    }
    $existing.Dispose()
    & taskkill.exe /PID ([string]$ProcessId) /T /F *> $null
    if ($LASTEXITCODE -ne 0) {
        return $false
    }
    for ($index = 0; $index -lt 30; $index++) {
        if ($null -eq (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            return $true
        }
        Start-Sleep -Milliseconds 100
    }
    return $false
}

function Start-ManagedProcess {
    param(
        [Parameter(Mandatory = $true)]
        [ValidateSet('runtime', 'ml_service', 'ml_worker', 'backend', 'frontend')]
        [string]$Role,
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]
        [string]$CommandMarker,
        [Parameter(Mandatory = $true)]
        [int]$Port,
        [Parameter(Mandatory = $true)]
        [string]$RunRelative,
        [Parameter(Mandatory = $true)]
        [hashtable]$Environment
    )

    if ($Port -ne [int]$ExpectedRoles[$Role].port) {
        throw 'LOCAL_DEV_PROCESS_METADATA_INVALID'
    }
    foreach ($argument in $Arguments) {
        if ($argument -match '[\r\n]') {
            throw 'LOCAL_DEV_PROCESS_ARGUMENT_INVALID'
        }
    }
    $runPath = Join-Path $RepoRoot $RunRelative
    New-Item -ItemType Directory -Path $runPath -Force | Out-Null
    $stdoutRelative = "$RunRelative/$Role.stdout.log"
    $stderrRelative = "$RunRelative/$Role.stderr.log"
    $stdout = Join-Path $RepoRoot $stdoutRelative
    $stderr = Join-Path $RepoRoot $stderrRelative
    $argumentText = ($Arguments | ForEach-Object { ConvertTo-NativeArgument -Value $_ }) -join ' '

    $process = Invoke-WithProcessEnvironment -Values $Environment -Operation {
        Start-Process `
            -FilePath $FilePath `
            -ArgumentList $argumentText `
            -WorkingDirectory $WorkingDirectory `
            -WindowStyle Hidden `
            -RedirectStandardOutput $stdout `
            -RedirectStandardError $stderr `
            -PassThru
    }
    try {
        Start-Sleep -Milliseconds 250
        $snapshot = Get-ProcessSnapshot -ProcessId ([int]$process.Id)
        if ($null -eq $snapshot) {
            throw 'LOCAL_DEV_PROCESS_EXITED_EARLY'
        }
        $record = [PSCustomObject]@{
            role = $Role
            pid = [int]$snapshot.pid
            process_start_time = $snapshot.start_time.ToString('o')
            executable = [string]$snapshot.executable
            command_marker = $CommandMarker
            port = $Port
            stdout_log = $stdoutRelative.Replace([char]92, [char]47)
            stderr_log = $stderrRelative.Replace([char]92, [char]47)
        }
        if (-not (Test-ProcessSnapshotMatch -Record $record -Snapshot $snapshot)) {
            throw 'LOCAL_DEV_PROCESS_OWNERSHIP_NOT_VERIFIED'
        }
        return $record
    }
    catch {
        [void](Invoke-TaskKill -ProcessId ([int]$process.Id))
        throw
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-RuntimeReadyProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Token
    )

    if ([string]::IsNullOrWhiteSpace($Token)) {
        return $false
    }
    try {
        $headers = @{ 'X-ZTA35G-Runtime-Token' = $Token }
        $live = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri 'http://127.0.0.1:8100/internal/v1/health/live' `
            -Headers $headers `
            -TimeoutSec 2
        $ready = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri 'http://127.0.0.1:8100/internal/v1/health/ready' `
            -Headers $headers `
            -TimeoutSec 2
        return $live.StatusCode -eq 200 -and $ready.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Invoke-RuntimeReadyViaBackendProbe {
    param(
        [scriptblock]$Request
    )

    if ($null -eq $Request) {
        $Request = {
            Invoke-WebRequest `
                -UseBasicParsing `
                -Uri 'http://127.0.0.1:8000/api/v1/tools/zta35g_sem_virtual_lab' `
                -TimeoutSec 4
        }
    }
    try {
        $response = & $Request
        if ([int]$response.StatusCode -ne 200) {
            return $false
        }
        $payload = [string]$response.Content | ConvertFrom-Json
        return (
            [string]$payload.data.tool_id -ceq 'zta35g_sem_virtual_lab' -and
            [string]$payload.data.availability -ceq 'AVAILABLE'
        )
    }
    catch {
        return $false
    }
}

function Invoke-BackendReadyProbe {
    try {
        $live = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri 'http://127.0.0.1:8000/api/v1/health/live' `
            -TimeoutSec 2
        $ready = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri 'http://127.0.0.1:8000/api/v1/health/ready' `
            -TimeoutSec 2
        $payload = $ready.Content | ConvertFrom-Json
        return (
            $live.StatusCode -eq 200 -and
            $ready.StatusCode -eq 200 -and
            [string]$payload.status -eq 'READY'
        )
    }
    catch {
        return $false
    }
}

function Invoke-FrontendReadyProbe {
    try {
        $root = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri 'http://127.0.0.1:3000/' `
            -TimeoutSec 2
        $proxy = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri 'http://127.0.0.1:3000/api/v1/health/live' `
            -TimeoutSec 2
        return $root.StatusCode -eq 200 -and $proxy.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Invoke-MaterialsMlServiceReadyProbe {
    try {
        $live = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri 'http://127.0.0.1:8200/health/live' `
            -TimeoutSec 2
        $ready = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri 'http://127.0.0.1:8200/health/ready' `
            -TimeoutSec 2
        return $live.StatusCode -eq 200 -and $ready.StatusCode -eq 200
    }
    catch {
        return $false
    }
}

function Wait-ForReady {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Probe,
        [Parameter(Mandatory = $true)]
        [object]$ProcessRecord,
        [Parameter(Mandatory = $true)]
        [int]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (-not (Test-ManagedProcess -Record $ProcessRecord)) {
            throw "LOCAL_DEV_SERVICE_EXITED name=$Name"
        }
        if (& $Probe) {
            return
        }
        Start-Sleep -Seconds 1
    }
    throw "LOCAL_DEV_READY_TIMEOUT name=$Name seconds=$TimeoutSeconds"
}

function Invoke-QuietProcess {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments,
        [Parameter(Mandatory = $true)]
        [string]$WorkingDirectory,
        [Parameter(Mandatory = $true)]
        [hashtable]$Environment,
        [Parameter(Mandatory = $true)]
        [string]$StdoutPath,
        [Parameter(Mandatory = $true)]
        [string]$StderrPath
    )

    $argumentText = ($Arguments | ForEach-Object { ConvertTo-NativeArgument -Value $_ }) -join ' '
    $process = Invoke-WithProcessEnvironment -Values $Environment -Operation {
        Start-Process `
            -FilePath $FilePath `
            -ArgumentList $argumentText `
            -WorkingDirectory $WorkingDirectory `
            -WindowStyle Hidden `
            -RedirectStandardOutput $StdoutPath `
            -RedirectStandardError $StderrPath `
            -Wait `
            -PassThru
    }
    try {
        return [int]$process.ExitCode
    }
    finally {
        $process.Dispose()
    }
}

function Invoke-DatabaseAndBucketPreparation {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Profile,
        [Parameter(Mandatory = $true)]
        [string]$RunRelative
    )

    $runPath = Join-Path $RepoRoot $RunRelative
    $alembicExit = Invoke-QuietProcess `
        -FilePath $Profile.backend_python `
        -Arguments @(
            '-m', 'alembic', '-c', (Join-Path $RepoRoot 'backend\alembic.ini'),
            'upgrade', 'head'
        ) `
        -WorkingDirectory $RepoRoot `
        -Environment $Profile.backend_environment `
        -StdoutPath (Join-Path $runPath 'alembic.stdout.log') `
        -StderrPath (Join-Path $runPath 'alembic.stderr.log')
    if ($alembicExit -ne 0) {
        throw 'LOCAL_DEV_ALEMBIC_FAILED'
    }

    $bootstrapCode = @'
from materialsagent.application.bootstrap import ensure_object_storage_bucket
from materialsagent.infrastructure.config import load_settings
from materialsagent.infrastructure.storage.minio import create_minio_storage
storage = create_minio_storage(load_settings())
try:
    ensure_object_storage_bucket(storage)
finally:
    storage.close()
'@
    $bucketExit = Invoke-QuietProcess `
        -FilePath $Profile.backend_python `
        -Arguments @('-c', $bootstrapCode) `
        -WorkingDirectory $RepoRoot `
        -Environment $Profile.backend_environment `
        -StdoutPath (Join-Path $runPath 'bucket.stdout.log') `
        -StderrPath (Join-Path $runPath 'bucket.stderr.log')
    if ($bucketExit -ne 0) {
        throw 'LOCAL_DEV_BUCKET_PREPARATION_FAILED'
    }
}

function Invoke-MaterialsMlMigration {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Profile,
        [Parameter(Mandatory = $true)]
        [string]$RunRelative
    )

    $runPath = Join-Path $RepoRoot $RunRelative
    $exitCode = Invoke-QuietProcess `
        -FilePath $Profile.python `
        -Arguments @('-m', 'materials_ml_service.admin', 'migrate') `
        -WorkingDirectory $Profile.working_directory `
        -Environment $Profile.service_environment `
        -StdoutPath (Join-Path $runPath 'ml-migration.stdout.log') `
        -StderrPath (Join-Path $runPath 'ml-migration.stderr.log')
    if ($exitCode -ne 0) {
        throw 'LOCAL_DEV_ML_MIGRATION_FAILED'
    }
}

function Get-StopProcessRecords {
    param(
        [AllowEmptyCollection()]
        [object[]]$Processes
    )

    foreach ($role in @('frontend', 'backend', 'ml_worker', 'ml_service', 'runtime')) {
        foreach ($record in @($Processes | Where-Object { $_.role -eq $role })) {
            Write-Output $record
        }
    }
}

function Get-OwnedComposeRecords {
    param(
        [AllowEmptyCollection()]
        [object[]]$DockerServices
    )

    foreach ($service in @('minio', 'postgresql')) {
        foreach ($record in @(
            $DockerServices |
                Where-Object {
                    $_.service -eq $service -and $_.started_by_this_run -eq $true
                }
        )) {
            Write-Output $record
        }
    }
}

function Stop-RecordedProcess {
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
        Write-Output (
            'LOCAL_DEV_PROCESS_OWNERSHIP_NOT_VERIFIED role={0} pid={1}' -f
            $Record.role,
            $Record.pid
        )
        return $false
    }
    return Invoke-TaskKill -ProcessId ([int]$Record.pid)
}

function Invoke-RecordedCleanup {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State,
        [Parameter(Mandatory = $true)]
        [string]$Docker
    )

    $remainingProcesses = New-Object 'System.Collections.Generic.List[object]'
    foreach ($record in @(Get-StopProcessRecords -Processes @($State.process))) {
        $result = @(Stop-RecordedProcess -Record $record)
        foreach ($line in @($result | Where-Object { $_ -is [string] })) {
            Write-Output $line
        }
        $stopped = @($result | Where-Object { $_ -is [bool] })[-1]
        if (-not $stopped) {
            $remainingProcesses.Add($record)
        }
    }
    $State.process = $remainingProcesses.ToArray()

    $ownedDocker = @(Get-OwnedComposeRecords -DockerServices @($State.docker))
    if ($ownedDocker.Count -gt 0) {
        try {
            $actual = Get-DockerIdentity
            $identityMatches = Test-DockerIdentityMatch -State $State -Actual $actual
        }
        catch {
            $identityMatches = $false
        }
        if (-not $identityMatches) {
            Write-Output 'LOCAL_DEV_DOCKER_OWNERSHIP_NOT_VERIFIED'
        }
        else {
            foreach ($record in $ownedDocker) {
                $composeExitCode = Invoke-NativeCommandExitCode -Operation {
                    & $Docker @ComposeArguments stop ([string]$record.service)
                }
                if ($composeExitCode -eq 0) {
                    $record.started_by_this_run = $false
                }
                else {
                    Write-Output (
                        'LOCAL_DEV_DOCKER_STOP_FAILED service={0}' -f $record.service
                    )
                }
            }
        }
    }
    $remainingDocker = @(Get-OwnedComposeRecords -DockerServices @($State.docker))
    return (
        @($State.process).Count -eq 0 -and
        $remainingDocker.Count -eq 0
    )
}

function Test-RecordedStackRunning {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State,
        [Parameter(Mandatory = $true)]
        [string]$Docker
    )

    $roles = @($State.process | ForEach-Object { [string]$_.role })
    $hasMlService = $roles -contains 'ml_service'
    $hasMlWorker = $roles -contains 'ml_worker'
    if (
        [string]$State.phase -ne 'running' -or
        $hasMlService -ne $hasMlWorker -or
        @($State.process).Count -ne $(if ($hasMlService) { 5 } else { 3 })
    ) {
        return $false
    }
    foreach ($record in @($State.process)) {
        if (-not (Test-ManagedProcess -Record $record)) {
            return $false
        }
    }
    foreach ($service in @('postgresql', 'minio')) {
        if (-not (Test-ComposeServiceRunning -Docker $Docker -Service $service)) {
            return $false
        }
    }
    return $true
}

function Write-LocalDevSummary {
    param(
        [Parameter(Mandatory = $true)]
        [object]$State,
        [Parameter(Mandatory = $true)]
        [string]$Marker,
        [bool]$RuntimeReady = $true,
        [bool]$BackendReady = $true,
        [bool]$FrontendReady = $true,
        [bool]$MaterialsMlServiceReady = $true,
        [bool]$MaterialsMlWorkerReady = $true
    )

    $runtimeStatus = if ($RuntimeReady) { 'READY' } else { 'NOT_READY_OR_UNVERIFIED' }
    $backendStatus = if ($BackendReady) { 'READY' } else { 'NOT_READY_OR_UNVERIFIED' }
    $frontendStatus = if ($FrontendReady) { 'READY' } else { 'NOT_READY_OR_UNVERIFIED' }
    $dependencyStatus = if ($BackendReady) { 'READY' } else { 'NOT_READY_OR_UNVERIFIED' }
    $materialsMlEnabled = @($State.process | Where-Object { $_.role -eq 'ml_service' }).Count -eq 1
    $materialsMlServiceStatus = if (-not $materialsMlEnabled) {
        'DISABLED'
    }
    elseif ($MaterialsMlServiceReady) {
        'READY'
    }
    else {
        'NOT_READY_OR_UNVERIFIED'
    }
    $materialsMlWorkerStatus = if (-not $materialsMlEnabled) {
        'DISABLED'
    }
    elseif ($MaterialsMlWorkerReady) {
        'RUNNING'
    }
    else {
        'NOT_RUNNING_OR_UNVERIFIED'
    }
    Write-Output (
        '{0} run_id={1} runtime={2} llm={3}' -f
        $Marker,
        $State.run_id,
        $State.runtime,
        $State.llm
    )
    Write-Output ("Frontend: $frontendStatus http://127.0.0.1:3000")
    Write-Output ("Backend: $backendStatus http://127.0.0.1:8000")
    Write-Output ("Runtime: $runtimeStatus http://127.0.0.1:8100")
    Write-Output ("ML Service: $materialsMlServiceStatus http://127.0.0.1:8200")
    Write-Output ("ML Worker: $materialsMlWorkerStatus")
    Write-Output ("PostgreSQL: $dependencyStatus 127.0.0.1:5432")
    Write-Output ("MinIO: $dependencyStatus http://127.0.0.1:9000 console=http://127.0.0.1:9001")
    Write-Output 'state=tmp/local-dev/state.json'
    Write-Output ("logs=tmp/local-dev/{0}" -f $State.run_id)
}

function Invoke-LocalDevStop {
    param(
        [string]$Path = $StatePath
    )

    Set-Location -LiteralPath $RepoRoot
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        Write-Output 'LOCAL_DEV_NOT_RUNNING'
        return 0
    }
    try {
        $state = Read-LocalDevState -Path $Path
    }
    catch {
        Write-Output 'LOCAL_DEV_STATE_INVALID no_action_taken=true'
        return 2
    }
    try {
        $dockerIdentity = Get-DockerIdentity
        $docker = $dockerIdentity.executable
    }
    catch {
        $docker = 'docker.exe'
    }
    $cleanupResult = @(Invoke-RecordedCleanup -State $state -Docker $docker)
    foreach ($line in @($cleanupResult | Where-Object { $_ -is [string] })) {
        Write-Output $line
    }
    $complete = @($cleanupResult | Where-Object { $_ -is [bool] })[-1]
    if ($complete) {
        Remove-Item -LiteralPath $Path -Force
        Write-Output 'LOCAL_DEV_STOPPED data_preserved=true logs_preserved=true'
        return 0
    }
    $state.phase = 'cleanup_required'
    Write-LocalDevState -State $state -Path $Path
    Write-Output 'LOCAL_DEV_STOP_INCOMPLETE state_preserved=true'
    return 2
}

function Invoke-LocalDevStart {
    Set-Location -LiteralPath $RepoRoot
    $currentStage = 'initialization'
    $state = $null
    $docker = $null
    $runId = [Guid]::NewGuid().ToString('N')
    $runRelative = "tmp/local-dev/$runId"
    $materialsMlEnabled = $false

    try {
        $currentStage = 'feature_preflight'
        $materialsMlEnabled = Get-ConfiguredMaterialsMlEnabled
        if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
            try {
                $existing = Read-LocalDevState
                $identity = Get-DockerIdentity
                $docker = $identity.executable
                $running = Test-RecordedStackRunning -State $existing -Docker $docker
            }
            catch {
                Write-Output 'LOCAL_DEV_STATE_REQUIRES_STOP'
                Write-Output 'Run -Action Stop; no new process was started.'
                return 2
            }
            if ($running) {
                $existingMaterialsMlEnabled = @(
                    $existing.process | Where-Object { $_.role -eq 'ml_service' }
                ).Count -eq 1
                if (
                    [string]$existing.runtime -ne $Runtime.ToLowerInvariant() -or
                    [string]$existing.llm -ne $Llm.ToLowerInvariant() -or
                    $existingMaterialsMlEnabled -ne $materialsMlEnabled
                ) {
                    Write-Output (
                        'LOCAL_DEV_ALREADY_RUNNING runtime={0} llm={1} ml={2} requested_runtime={3} requested_llm={4} requested_ml={5}' -f
                        $existing.runtime,
                        $existing.llm,
                        $existingMaterialsMlEnabled.ToString().ToLowerInvariant(),
                        $Runtime.ToLowerInvariant(),
                        $Llm.ToLowerInvariant(),
                        $materialsMlEnabled.ToString().ToLowerInvariant()
                    )
                    Write-Output 'Run -Action Stop before changing the managed service set.'
                    return 2
                }
                $backendReady = Invoke-BackendReadyProbe
                $runtimeReady = $(if ($backendReady) {
                    Invoke-RuntimeReadyViaBackendProbe
                }
                else {
                    $false
                })
                $frontendReady = Invoke-FrontendReadyProbe
                $materialsMlServiceReady = $(if ($materialsMlEnabled) {
                    Invoke-MaterialsMlServiceReadyProbe
                }
                else {
                    $true
                })
                $materialsMlWorkerReady = $(if ($materialsMlEnabled) {
                    $workerRecord = @($existing.process | Where-Object { $_.role -eq 'ml_worker' })[0]
                    Test-ManagedProcess -Record $workerRecord
                }
                else {
                    $true
                })
                Write-LocalDevSummary `
                    -State $existing `
                    -Marker 'LOCAL_DEV_ALREADY_RUNNING' `
                    -RuntimeReady $runtimeReady `
                    -BackendReady $backendReady `
                    -FrontendReady $frontendReady `
                    -MaterialsMlServiceReady $materialsMlServiceReady `
                    -MaterialsMlWorkerReady $materialsMlWorkerReady
                return $(if (
                    $runtimeReady -and
                    $backendReady -and
                    $frontendReady -and
                    $materialsMlServiceReady -and
                    $materialsMlWorkerReady
                ) { 0 } else { 2 })
            }
            Write-Output 'LOCAL_DEV_STATE_REQUIRES_STOP'
            Write-Output 'Run -Action Stop; no new process was started.'
            return 2
        }

        $currentStage = 'port_preflight'
        $occupied = @(
            Get-OccupiedPorts `
                -Ports @(Get-RequiredPorts -MaterialsMlEnabled $materialsMlEnabled) |
                Sort-Object
        )
        if ($occupied.Count -gt 0) {
            Write-Output ('LOCAL_DEV_PORT_CONFLICT ports={0} no_action_taken=true' -f ($occupied -join ','))
            return 3
        }

        $currentStage = 'backend_python_preflight'
        $backendPythonPath = Invoke-LocalDevPreflightStep `
            -Component backend_python `
            -Operation {
                $resolved = Resolve-CondaEnvironmentPython `
                    -EnvironmentName 'materialsagent-backend' `
                    -ExplicitPath $BackendPython
                Assert-PythonVersion -Python $resolved -ExpectedMajorMinor '3.11'
                return $resolved
            }
        $currentStage = 'runtime_python_preflight'
        $runtimePythonPath = Invoke-LocalDevPreflightStep `
            -Component runtime_python `
            -Operation {
                $resolved = if ($Runtime -eq 'Real') {
                    Resolve-CondaEnvironmentPython `
                        -EnvironmentName 'materialsagent-zta35g' `
                        -ExplicitPath $RuntimePython
                }
                else {
                    $backendPythonPath
                }
                Assert-PythonVersion `
                    -Python $resolved `
                    -ExpectedMajorMinor $(if ($Runtime -eq 'Real') { '3.8' } else { '3.11' })
                return $resolved
            }
        $materialsMlProfile = $null
        if ($materialsMlEnabled) {
            $currentStage = 'materials_ml_python_preflight'
            $materialsMlPythonPath = Invoke-LocalDevPreflightStep `
                -Component materials_ml_python `
                -Operation {
                    $resolved = Resolve-ExecutablePath -Path $(if (
                        -not [string]::IsNullOrWhiteSpace($MaterialsMlPython)
                    ) {
                        $MaterialsMlPython
                    }
                    else {
                        Join-Path $RepoRoot 'services\materials_ml\.venv\Scripts\python.exe'
                    })
                    Assert-PythonVersion -Python $resolved -ExpectedMajorMinor '3.11'
                    return $resolved
                }
            $currentStage = 'materials_ml_configuration_preflight'
            $materialsMlEnvironmentFile = Join-Path $RepoRoot 'services\materials_ml\.env'
            if (-not (Test-Path -LiteralPath $materialsMlEnvironmentFile -PathType Leaf)) {
                throw 'LOCAL_DEV_CONFIGURATION_MISSING name=ML_ENV_FILE source=services_materials_ml'
            }
            $probeEnvironment = @{
                ML_ENV_FILE = $materialsMlEnvironmentFile
                PYTHONPATH = @(
                    (Join-Path $RepoRoot 'backend\src'),
                    (Join-Path $RepoRoot 'services\materials_ml\src'),
                    (Join-Path $RepoRoot 'packages\materials_storage\src')
                ) -join [IO.Path]::PathSeparator
            }
            foreach ($name in @(
                'ML_DATABASE_URL', 'ML_MINIO_ENDPOINT', 'ML_MINIO_BUCKET',
                'ML_MINIO_ACCESS_KEY', 'ML_MINIO_SECRET_KEY', 'ML_MINIO_SECURE',
                'ML_RESOURCE_TOKEN', 'ML_WORKER_TOKEN', 'ML_MCP_ENABLED',
                'ML_MCP_TOKEN', 'ML_NAMESPACE', 'ML_STORE_ID', 'ML_SERVICE_URL',
                'ML_ADMIN_DATABASE_URL', 'ML_ADMIN_MINIO_ACCESS_KEY',
                'ML_ADMIN_MINIO_SECRET_KEY'
            )) {
                $probeEnvironment[$name] = $null
            }
            $workerToken = Invoke-LocalDevPreflightStep `
                -Component materials_ml_configuration `
                -Operation {
                    Get-MaterialsMlWorkerToken `
                        -Python $materialsMlPythonPath `
                        -EnvironmentFile $materialsMlEnvironmentFile `
                        -Environment $probeEnvironment
                }
            $materialsMlProfile = New-MaterialsMlLaunchProfile `
                -Python $materialsMlPythonPath `
                -EnvironmentFile $materialsMlEnvironmentFile `
                -WorkerToken $workerToken
        }
        $currentStage = 'configuration_preflight'
        if ($Llm -eq 'Provider') {
            Assert-ProviderRootConfiguration -BackendPython $backendPythonPath
        }
        $npm = Get-Command 'npm.cmd' -CommandType Application -ErrorAction SilentlyContinue |
            Select-Object -First 1
        if ($null -eq $npm) {
            throw 'LOCAL_DEV_NPM_NOT_FOUND'
        }
        if (-not (Test-Path -LiteralPath (Join-Path $RepoRoot 'frontend\node_modules\.bin\vite.cmd') -PathType Leaf)) {
            throw 'LOCAL_DEV_FRONTEND_DEPENDENCIES_MISSING'
        }
        $runtimeToken = New-EphemeralRuntimeToken
        $profile = New-LaunchProfile `
            -Runtime $Runtime `
            -Llm $Llm `
            -RuntimeToken $runtimeToken `
            -BackendPython $backendPythonPath `
            -RuntimePython $runtimePythonPath
        $dockerIdentity = Get-DockerIdentity
        $docker = $dockerIdentity.executable

        $currentStage = 'compose_ownership'
        $dockerRecords = New-Object 'System.Collections.Generic.List[object]'
        foreach ($service in @('postgresql', 'minio')) {
            $preexisting = Test-ComposeServiceRunning -Docker $docker -Service $service
            $dockerRecords.Add([PSCustomObject]@{
                service = $service
                started_by_this_run = (-not $preexisting)
            })
        }
        $state = New-LocalDevState `
            -RunId $runId `
            -Phase starting `
            -Runtime $Runtime `
            -Llm $Llm `
            -DockerContext $dockerIdentity.context `
            -DockerEngineId $dockerIdentity.engine_id `
            -Processes @() `
            -DockerServices $dockerRecords.ToArray()
        Write-LocalDevState -State $state
        New-Item -ItemType Directory -Path (Join-Path $RepoRoot $runRelative) -Force | Out-Null

        $currentStage = 'compose_start'
        Invoke-WithProcessEnvironment -Values $profile.compose_environment -Operation {
            Invoke-ComposeStartSafely -Operation {
                & $docker @ComposeArguments up -d --wait --pull never postgresql minio
            }
        }
        foreach ($service in @('postgresql', 'minio')) {
            if (-not (Test-ComposeServiceRunning -Docker $docker -Service $service)) {
                throw "LOCAL_DEV_COMPOSE_NOT_READY service=$service"
            }
        }

        $currentStage = 'database_and_bucket'
        Invoke-DatabaseAndBucketPreparation -Profile $profile -RunRelative $runRelative

        if ($materialsMlEnabled) {
            $currentStage = 'materials_ml_migration'
            Invoke-MaterialsMlMigration `
                -Profile $materialsMlProfile `
                -RunRelative $runRelative

            $currentStage = 'materials_ml_service_start'
            $materialsMlServiceRecord = Start-ManagedProcess `
                -Role ml_service `
                -FilePath $materialsMlProfile.python `
                -Arguments $materialsMlProfile.service_arguments `
                -WorkingDirectory $materialsMlProfile.working_directory `
                -CommandMarker $materialsMlProfile.service_marker `
                -Port 8200 `
                -RunRelative $runRelative `
                -Environment $materialsMlProfile.service_environment
            $state.process = @($state.process) + @($materialsMlServiceRecord)
            Write-LocalDevState -State $state
            Wait-ForReady `
                -Name MaterialsMlService `
                -Probe { Invoke-MaterialsMlServiceReadyProbe } `
                -ProcessRecord $materialsMlServiceRecord `
                -TimeoutSeconds ([Math]::Min($ReadyTimeoutSeconds, 120))

            $currentStage = 'materials_ml_worker_start'
            $materialsMlWorkerRecord = Start-ManagedProcess `
                -Role ml_worker `
                -FilePath $materialsMlProfile.python `
                -Arguments $materialsMlProfile.worker_arguments `
                -WorkingDirectory $materialsMlProfile.working_directory `
                -CommandMarker $materialsMlProfile.worker_marker `
                -Port 0 `
                -RunRelative $runRelative `
                -Environment $materialsMlProfile.worker_environment
            $state.process = @($state.process) + @($materialsMlWorkerRecord)
            Write-LocalDevState -State $state
        }

        $currentStage = 'runtime_start'
        $runtimeRecord = Start-ManagedProcess `
            -Role runtime `
            -FilePath $profile.runtime_python `
            -Arguments $profile.runtime_arguments `
            -WorkingDirectory $profile.runtime_working_directory `
            -CommandMarker $profile.runtime_marker `
            -Port 8100 `
            -RunRelative $runRelative `
            -Environment $profile.runtime_environment
        $state.process = @($state.process) + @($runtimeRecord)
        Write-LocalDevState -State $state
        Wait-ForReady `
            -Name Runtime `
            -Probe { Invoke-RuntimeReadyProbe -Token $runtimeToken } `
            -ProcessRecord $runtimeRecord `
            -TimeoutSeconds $ReadyTimeoutSeconds

        $currentStage = 'backend_start'
        $backendRecord = Start-ManagedProcess `
            -Role backend `
            -FilePath $profile.backend_python `
            -Arguments $profile.backend_arguments `
            -WorkingDirectory $profile.backend_working_directory `
            -CommandMarker 'materialsagent.main:create_app' `
            -Port 8000 `
            -RunRelative $runRelative `
            -Environment $profile.backend_environment
        $state.process = @($state.process) + @($backendRecord)
        Write-LocalDevState -State $state
        Wait-ForReady `
            -Name Backend `
            -Probe { Invoke-BackendReadyProbe } `
            -ProcessRecord $backendRecord `
            -TimeoutSeconds ([Math]::Min($ReadyTimeoutSeconds, 120))

        $currentStage = 'frontend_start'
        $frontendRecord = Start-ManagedProcess `
            -Role frontend `
            -FilePath $npm.Source `
            -Arguments @(
                '--prefix',
                (Join-Path $RepoRoot 'frontend'),
                'run',
                'dev',
                '--',
                '--port',
                '3000',
                '--strictPort'
            ) `
            -WorkingDirectory $RepoRoot `
            -CommandMarker '--strictPort' `
            -Port 3000 `
            -RunRelative $runRelative `
            -Environment $profile.frontend_environment
        $state.process = @($state.process) + @($frontendRecord)
        Write-LocalDevState -State $state
        Wait-ForReady `
            -Name Frontend `
            -Probe { Invoke-FrontendReadyProbe } `
            -ProcessRecord $frontendRecord `
            -TimeoutSeconds ([Math]::Min($ReadyTimeoutSeconds, 120))

        $state.phase = 'running'
        Write-LocalDevState -State $state
        Write-LocalDevSummary -State $state -Marker 'LOCAL_DEV_STARTED'
        return 0
    }
    catch {
        $safeMessage = [string]$_.Exception.Message
        if ($safeMessage -match '^LOCAL_DEV_[A-Z0-9_]+(?: .*)?$') {
            Write-Output $safeMessage
        }
        Write-Output "LOCAL_DEV_START_FAILED stage=$currentStage"
        if ($null -ne $state) {
            if ([string]::IsNullOrWhiteSpace($docker)) {
                $docker = 'docker.exe'
            }
            $cleanupResult = @(Invoke-RecordedCleanup -State $state -Docker $docker)
            foreach ($line in @($cleanupResult | Where-Object { $_ -is [string] })) {
                Write-Output $line
            }
            $complete = @($cleanupResult | Where-Object { $_ -is [bool] })[-1]
            if ($complete) {
                Remove-Item -LiteralPath $StatePath -Force -ErrorAction SilentlyContinue
                Write-Output 'LOCAL_DEV_START_CLEANUP_COMPLETE data_preserved=true'
            }
            else {
                $state.phase = 'cleanup_required'
                Write-LocalDevState -State $state
                Write-Output 'LOCAL_DEV_START_CLEANUP_INCOMPLETE state_preserved=true'
            }
        }
        return 1
    }
}

if (-not $LoadFunctionsOnly) {
    $result = if ($Action -eq 'Start') {
        @(Invoke-LocalDevStart)
    }
    else {
        @(Invoke-LocalDevStop)
    }
    foreach ($line in @($result | Where-Object { $_ -isnot [int] })) {
        Write-Output $line
    }
    $exitCode = @($result | Where-Object { $_ -is [int] })[-1]
    exit $exitCode
}
