[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$entryPath = Join-Path $PSScriptRoot 'local-dev.ps1'
$script:Passed = 0

function Assert-True {
    param(
        [Parameter(Mandatory = $true)]
        [bool]$Condition,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Assert-Equal {
    param(
        [AllowNull()]
        [object]$Actual,
        [AllowNull()]
        [object]$Expected,
        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if ([string]$Actual -cne [string]$Expected) {
        throw (
            '{0} expected=<{1}> actual=<{2}>' -f
            $Message,
            [string]$Expected,
            [string]$Actual
        )
    }
}

function Invoke-Test {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Body
    )

    try {
        & $Body
        $script:Passed++
        Write-Output "PASS $Name"
    }
    catch {
        $failureMessage = 'FAIL {0}: {1}' -f @($Name, $_.Exception.Message)
        [Console]::Error.WriteLine($failureMessage)
        throw
    }
}

if (-not (Test-Path -LiteralPath $entryPath -PathType Leaf)) {
    throw 'local-dev.ps1 is missing.'
}

. $entryPath -Action Start -LoadFunctionsOnly

Invoke-Test 'profiles keep secrets out of arguments and state metadata' {
    $runtimeToken = 'offline-runtime-token-canary'
    $mock = New-LaunchProfile `
        -Runtime Mock `
        -Llm Mock `
        -RuntimeToken $runtimeToken `
        -BackendPython 'C:\tools\backend-python.exe' `
        -RuntimePython 'C:\tools\backend-python.exe'
    Assert-Equal $mock.backend_environment['LLM_ADAPTER'] 'mock' 'Mock LLM adapter'
    Assert-Equal $mock.backend_environment['DEEPSEEK_API_KEY'] '' 'Mock key override'
    Assert-Equal $mock.backend_environment['ZTA35G_RUNTIME_TOKEN'] $runtimeToken 'Backend Runtime token'
    Assert-Equal $mock.runtime_environment['ZTA35G_RUNTIME_TOKEN'] $runtimeToken 'Runtime token'
    Assert-Equal $mock.runtime_role 'mock' 'Mock Runtime role'
    Assert-True `
        (-not (($mock.runtime_arguments -join ' ') -match $runtimeToken)) `
        'Runtime arguments contain a secret canary.'

    $real = New-LaunchProfile `
        -Runtime Real `
        -Llm DeepSeek `
        -RuntimeToken $runtimeToken `
        -BackendPython 'C:\tools\backend-python.exe' `
        -RuntimePython 'C:\tools\runtime-python.exe'
    Assert-Equal $real.backend_environment['LLM_ADAPTER'] 'deepseek' 'DeepSeek adapter'
    Assert-True `
        ($real.backend_environment.ContainsKey('DEEPSEEK_API_KEY')) `
        'DeepSeek key isolation is missing.'
    Assert-True `
        ($null -eq $real.backend_environment['DEEPSEEK_API_KEY']) `
        'DeepSeek key must come from the Backend root dotenv loader.'
    Assert-Equal $real.backend_environment['ZTA35G_RUNTIME_TOKEN'] $runtimeToken 'Real Backend token'
    Assert-Equal $real.runtime_environment['ZTA35G_RUNTIME_TOKEN'] $runtimeToken 'Real Runtime token'
    Assert-Equal `
        $real.runtime_environment['ZTA35G_MODEL_ROOT'] `
        ([IO.Path]::GetFullPath((Join-Path $repoRoot 'SEM\ZTA35G_lab'))) `
        'Real Runtime model root'
    Assert-Equal $real.runtime_role 'real' 'Real Runtime role'
    Assert-True `
        (($real.runtime_arguments -join ' ') -match 'materialsagent_zta35g_runtime.main') `
        'Real Runtime entry point is missing.'
}

Invoke-Test 'non-Backend children remove unrelated caller secrets' {
    $profile = New-LaunchProfile `
        -Runtime Real `
        -Llm DeepSeek `
        -RuntimeToken 'offline-runtime-token-canary' `
        -BackendPython 'C:\tools\backend-python.exe' `
        -RuntimePython 'C:\tools\runtime-python.exe'
    foreach ($name in @(
        'DEEPSEEK_API_KEY',
        'POSTGRES_PASSWORD',
        'MINIO_ACCESS_KEY',
        'MINIO_SECRET_KEY',
        'TIMELINE_CURSOR_SIGNING_KEY'
    )) {
        Assert-True `
            ($profile.runtime_environment.ContainsKey($name)) `
            "Runtime isolation does not control $name."
        Assert-True `
            ($null -eq $profile.runtime_environment[$name]) `
            "Runtime isolation retains $name."
        Assert-True `
            ($profile.frontend_environment.ContainsKey($name)) `
            "Frontend isolation does not control $name."
        Assert-True `
            ($null -eq $profile.frontend_environment[$name]) `
            "Frontend isolation retains $name."
    }
    foreach ($name in @(
        'DEEPSEEK_API_KEY',
        'ZTA35G_RUNTIME_TOKEN',
        'ZTA35G_MODEL_ROOT',
        'TIMELINE_CURSOR_SIGNING_KEY'
    )) {
        Assert-True `
            ($profile.compose_environment.ContainsKey($name)) `
            "Compose isolation does not control $name."
        Assert-True `
            ($null -eq $profile.compose_environment[$name]) `
            "Compose isolation retains $name."
    }
}

Invoke-Test 'ephemeral Runtime tokens are strong nonblank per-run values' {
    $first = New-EphemeralRuntimeToken
    $second = New-EphemeralRuntimeToken
    Assert-True ($first -cmatch '^[A-Za-z0-9_-]{43}$') 'First Runtime token shape is invalid.'
    Assert-True ($second -cmatch '^[A-Za-z0-9_-]{43}$') 'Second Runtime token shape is invalid.'
    Assert-True ($first -cne $second) 'Two Runtime starts reused one token.'
}

Invoke-Test 'DeepSeek root dotenv preflight accepts a valid silent probe' {
    Assert-DeepSeekRootConfiguration `
        -BackendPython 'C:\tools\backend-python.exe' `
        -Probe { param($python) return 0 }
}

Invoke-Test 'DeepSeek root dotenv preflight reports a missing key without values' {
    $message = $null
    try {
        Assert-DeepSeekRootConfiguration `
            -BackendPython 'C:\tools\backend-python.exe' `
            -Probe { param($python) return 3 }
    }
    catch {
        $message = $_.Exception.Message
    }
    Assert-Equal `
        $message `
        'LOCAL_DEV_CONFIGURATION_MISSING name=DEEPSEEK_API_KEY source=root_dotenv' `
        'Missing root dotenv key result'
}

Invoke-Test 'DeepSeek root dotenv preflight reports invalid configuration safely' {
    $message = $null
    try {
        Assert-DeepSeekRootConfiguration `
            -BackendPython 'C:\tools\backend-python.exe' `
            -Probe { param($python) return 2 }
    }
    catch {
        $message = $_.Exception.Message
    }
    Assert-Equal `
        $message `
        'LOCAL_DEV_CONFIGURATION_INVALID source=root_dotenv mode=deepseek' `
        'Invalid root dotenv result'
}

Invoke-Test 'repeated-start Runtime readiness uses the Backend-owned token' {
    $available = Invoke-RuntimeReadyViaBackendProbe -Request {
        return [PSCustomObject]@{
            StatusCode = 200
            Content = '{"request_id":"offline-request","data":{"tool_id":"zta35g_sem_virtual_lab","availability":"AVAILABLE"}}'
        }
    }
    $unavailable = Invoke-RuntimeReadyViaBackendProbe -Request {
        return [PSCustomObject]@{
            StatusCode = 200
            Content = '{"request_id":"offline-request","data":{"tool_id":"zta35g_sem_virtual_lab","availability":"UNAVAILABLE"}}'
        }
    }
    Assert-True $available 'Backend Runtime proxy rejected AVAILABLE.'
    Assert-True (-not $unavailable) 'Backend Runtime proxy accepted UNAVAILABLE.'
}

Invoke-Test 'required ports and conflicts cover the complete local stack' {
    $ports = @(Get-RequiredPorts | Sort-Object)
    Assert-Equal ($ports -join ',') '3000,5432,8000,8100,9000,9001' 'Required ports'

    $occupied = @(
        Get-OccupiedPorts `
            -Ports $ports `
            -Probe { param($port) return $port -in @(3000, 9001) }
    )
    Assert-Equal ($occupied -join ',') '3000,9001' 'Occupied ports'
}

Invoke-Test 'configured Backend interpreter passes the offline version preflight' {
    $python = Resolve-CondaEnvironmentPython `
        -EnvironmentName 'materialsagent-backend'
    Assert-PythonVersion -Python $python -ExpectedMajorMinor '3.11'
}

Invoke-Test 'managed native arguments preserve spaces and quotes without a shell' {
    $python = Resolve-CondaEnvironmentPython `
        -EnvironmentName 'materialsagent-backend'
    $testRoot = Join-Path ([IO.Path]::GetTempPath()) (
        'materialsagent-local-dev-argv-{0}' -f [Guid]::NewGuid().ToString('N')
    )
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    try {
        $stdout = Join-Path $testRoot 'stdout.log'
        $stderr = Join-Path $testRoot 'stderr.log'
        $value = 'value with spaces and "quotes"'
        $exitCode = Invoke-QuietProcess `
            -FilePath $python `
            -Arguments @('-c', 'import sys; print(sys.argv[1])', $value) `
            -WorkingDirectory $repoRoot `
            -Environment @{} `
            -StdoutPath $stdout `
            -StderrPath $stderr
        Assert-Equal $exitCode 0 'Native argument probe exit code'
        Assert-Equal `
            ((Get-Content -Raw -Encoding UTF8 $stdout).Trim()) `
            $value `
            'Native argument round trip'
    }
    finally {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}

Invoke-Test 'state serialization contains ownership metadata but no secrets' {
    $processes = @(
        [PSCustomObject]@{
            role = 'runtime'
            pid = 101
            process_start_time = '2026-08-04T01:02:03.0000000Z'
            executable = 'C:\tools\python.exe'
            command_marker = 'materialsagent_mock_runtime.main'
            port = 8100
            stdout_log = 'tmp/local-dev/run/runtime.stdout.log'
            stderr_log = 'tmp/local-dev/run/runtime.stderr.log'
        }
    )
    $docker = @(
        [PSCustomObject]@{
            service = 'postgresql'
            started_by_this_run = $true
        },
        [PSCustomObject]@{
            service = 'minio'
            started_by_this_run = $false
        }
    )
    $state = New-LocalDevState `
        -RunId '11111111111111111111111111111111' `
        -Phase running `
        -Runtime Mock `
        -Llm Mock `
        -DockerContext 'desktop-linux' `
        -DockerEngineId 'engine-id' `
        -Processes $processes `
        -DockerServices $docker
    $json = ConvertTo-LocalDevStateJson -State $state
    Assert-True (Test-LocalDevState -State $state) 'Valid state was rejected.'
    foreach ($forbidden in @(
        'DEEPSEEK_API_KEY',
        'ZTA35G_RUNTIME_TOKEN',
        'POSTGRES_PASSWORD',
        'MINIO_SECRET_KEY',
        'test-secret-canary'
    )) {
        Assert-True (-not $json.Contains($forbidden)) "State contains $forbidden."
    }
}

Invoke-Test 'process ownership rejects PID reuse metadata changes' {
    $record = [PSCustomObject]@{
        role = 'backend'
        pid = 202
        process_start_time = '2026-08-04T01:02:03.0000000Z'
        executable = 'C:\tools\python.exe'
        command_marker = 'materialsagent.main:create_app'
        port = 8000
        stdout_log = 'tmp/local-dev/run/backend.stdout.log'
        stderr_log = 'tmp/local-dev/run/backend.stderr.log'
    }
    $matching = [PSCustomObject]@{
        pid = 202
        start_time = [DateTime]::Parse('2026-08-04T01:02:03Z').ToUniversalTime()
        executable = 'C:\tools\python.exe'
        command_line = (
            'python -m uvicorn materialsagent.main:create_app --factory --app-dir "{0}\backend\src"' -f
            $repoRoot
        )
    }
    Assert-True `
        (Test-ProcessSnapshotMatch -Record $record -Snapshot $matching) `
        'Matching process snapshot was rejected.'

    $reused = $matching.PSObject.Copy()
    $reused.start_time = [DateTime]::Parse('2026-08-04T01:12:03Z').ToUniversalTime()
    Assert-True `
        (-not (Test-ProcessSnapshotMatch -Record $record -Snapshot $reused)) `
        'A reused PID with a different start time was accepted.'
}

Invoke-Test 'state validation rejects broadened process markers' {
    $processes = @(
        [PSCustomObject]@{
            role = 'backend'
            pid = 303
            process_start_time = '2026-08-04T01:02:03.0000000Z'
            executable = 'C:\tools\python.exe'
            command_marker = $repoRoot
            port = 8000
            stdout_log = 'tmp/local-dev/run/backend.stdout.log'
            stderr_log = 'tmp/local-dev/run/backend.stderr.log'
        }
    )
    $docker = @(
        [PSCustomObject]@{ service = 'postgresql'; started_by_this_run = $true },
        [PSCustomObject]@{ service = 'minio'; started_by_this_run = $true }
    )
    $state = New-LocalDevState `
        -RunId '22222222222222222222222222222222' `
        -Phase cleanup_required `
        -Runtime Mock `
        -Llm Mock `
        -DockerContext 'desktop-linux' `
        -DockerEngineId 'engine-id' `
        -Processes $processes `
        -DockerServices $docker
    Assert-True `
        (-not (Test-LocalDevState -State $state)) `
        'State accepted a broadened command marker.'
}

Invoke-Test 'stop targets only recorded processes and run-owned Compose services' {
    $processes = @(
        [PSCustomObject]@{ role = 'runtime' },
        [PSCustomObject]@{ role = 'frontend' },
        [PSCustomObject]@{ role = 'backend' }
    )
    $processOrder = @(Get-StopProcessRecords -Processes $processes)
    Assert-Equal (($processOrder | ForEach-Object role) -join ',') 'frontend,backend,runtime' 'Process stop order'

    $docker = @(
        [PSCustomObject]@{ service = 'postgresql'; started_by_this_run = $false },
        [PSCustomObject]@{ service = 'minio'; started_by_this_run = $true }
    )
    $owned = @(Get-OwnedComposeRecords -DockerServices $docker)
    Assert-Equal (($owned | ForEach-Object service) -join ',') 'minio' 'Owned Compose services'
}

Invoke-Test 'native stderr progress does not abort successful stop handling' {
    $originalPreference = $ErrorActionPreference
    $exitCode = Invoke-NativeCommandExitCode -Operation {
        & cmd.exe /d /c 'echo native-progress 1>&2 & exit /b 0'
    }
    Assert-Equal $exitCode 0 'Native success exit code'
    Assert-Equal `
        $ErrorActionPreference `
        $originalPreference `
        'ErrorActionPreference restoration'
}

Invoke-Test 'Compose start accepts native stderr progress when exit code is zero' {
    Invoke-ComposeStartSafely -Operation {
        & cmd.exe /d /c 'echo compose-start-progress 1>&2 & exit /b 0'
    }
}

Invoke-Test 'repeated stop with no state is a clear no-op' {
    $missing = Join-Path ([IO.Path]::GetTempPath()) (
        'materialsagent-local-dev-missing-{0}.json' -f [Guid]::NewGuid().ToString('N')
    )
    $result = @(Invoke-LocalDevStop -Path $missing)
    Assert-Equal $result[0] 'LOCAL_DEV_NOT_RUNNING' 'Repeated stop output'
    Assert-Equal $result[-1] 0 'Repeated stop exit code'
}

Invoke-Test 'status summary never labels an unready service READY' {
    $docker = @(
        [PSCustomObject]@{ service = 'postgresql'; started_by_this_run = $true },
        [PSCustomObject]@{ service = 'minio'; started_by_this_run = $true }
    )
    $state = New-LocalDevState `
        -RunId '33333333333333333333333333333333' `
        -Phase running `
        -Runtime Mock `
        -Llm Mock `
        -DockerContext 'desktop-linux' `
        -DockerEngineId 'engine-id' `
        -Processes @() `
        -DockerServices $docker
    $lines = @(
        Write-LocalDevSummary `
            -State $state `
            -Marker 'LOCAL_DEV_ALREADY_RUNNING' `
            -RuntimeReady $false `
            -BackendReady $true `
            -FrontendReady $false
    )
    Assert-True ($lines -contains 'Runtime: NOT_READY_OR_UNVERIFIED http://127.0.0.1:8100') 'Runtime status was not honest.'
    Assert-True ($lines -contains 'Frontend: NOT_READY_OR_UNVERIFIED http://127.0.0.1:3000') 'Frontend status was not honest.'
    Assert-True ($lines -contains 'Backend: READY http://127.0.0.1:8000') 'Backend READY status was lost.'
}

Write-Output ("LOCAL_DEV_OFFLINE_TESTS_OK tests={0}" -f $script:Passed)
