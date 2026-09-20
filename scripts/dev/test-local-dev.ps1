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
    Assert-Equal $mock.backend_environment['DASHSCOPE_API_KEY'] '' 'Mock Qwen key override'
    Assert-Equal $mock.backend_environment['ZTA35G_RUNTIME_TOKEN'] $runtimeToken 'Backend Runtime token'
    Assert-Equal $mock.runtime_environment['ZTA35G_RUNTIME_TOKEN'] $runtimeToken 'Runtime token'
    Assert-Equal $mock.runtime_role 'mock' 'Mock Runtime role'
    Assert-True `
        (-not (($mock.runtime_arguments -join ' ') -match $runtimeToken)) `
        'Runtime arguments contain a secret canary.'

    $provider = New-LaunchProfile `
        -Runtime Mock `
        -Llm Provider `
        -RuntimeToken $runtimeToken `
        -BackendPython 'C:\tools\backend-python.exe' `
        -RuntimePython 'C:\tools\backend-python.exe'
    Assert-Equal $provider.backend_environment['LLM_ADAPTER'] 'provider' 'Provider adapter'
    Assert-True `
        ($provider.backend_environment.ContainsKey('DEEPSEEK_API_KEY')) `
        'DeepSeek key isolation is missing.'
    Assert-True `
        ($null -eq $provider.backend_environment['DEEPSEEK_API_KEY']) `
        'DeepSeek key must come from the Backend root dotenv loader.'
    Assert-True `
        ($null -eq $provider.backend_environment['DASHSCOPE_API_KEY']) `
        'Qwen key must come from the Backend root dotenv loader.'
    Assert-Equal $provider.backend_environment['ZTA35G_RUNTIME_TOKEN'] $runtimeToken 'Provider Backend token'
    Assert-Equal $provider.runtime_environment['ZTA35G_RUNTIME_TOKEN'] $runtimeToken 'Mock Runtime token'
    Assert-Equal $provider.runtime_role 'mock' 'Mock Runtime role'
    Assert-True `
        (($provider.runtime_arguments -join ' ') -match 'materialsagent_mock_runtime.main') `
        'Mock Runtime entry point is missing.'
}

Invoke-Test 'real profile resolves EBSD root from root dotenv and isolates it to Runtime' {
    $testRoot = Join-Path ([IO.Path]::GetTempPath()) (
        'materialsagent-local-dev-ebsd-{0}' -f [Guid]::NewGuid().ToString('N')
    )
    $modelRoot = Join-Path $testRoot 'research root'
    $weights = Join-Path $modelRoot 'model\save\CNN_1.pt'
    $dotenv = Join-Path $testRoot '.env'
    New-Item -ItemType Directory -Path (Split-Path -Parent $weights) -Force | Out-Null
    [IO.File]::WriteAllText($weights, 'offline-fixture')
    [IO.File]::WriteAllText($dotenv, "EBSD_MODEL_ROOT=`"$modelRoot`"")
    try {
        $resolved = Resolve-EbsdModelRoot -ConfiguredPath $null -DotenvPath $dotenv
        Assert-Equal $resolved ([IO.Path]::GetFullPath($modelRoot)) 'Resolved EBSD model root'

        $profile = New-LaunchProfile `
            -Runtime Real `
            -Llm Mock `
            -RuntimeToken 'offline-runtime-token-canary' `
            -BackendPython 'C:\tools\backend-python.exe' `
            -RuntimePython 'C:\tools\runtime-python.exe' `
            -ResolvedEbsdModelRoot $resolved
        Assert-Equal $profile.runtime_environment['EBSD_MODEL_ROOT'] $resolved 'Runtime EBSD root'
        Assert-True `
            ($null -eq $profile.backend_environment['EBSD_MODEL_ROOT']) `
            'Backend inherited the EBSD model root.'
        Assert-True `
            ($null -eq $profile.frontend_environment['EBSD_MODEL_ROOT']) `
            'Frontend inherited the EBSD model root.'
        Assert-True `
            ($null -eq $profile.compose_environment['EBSD_MODEL_ROOT']) `
            'Compose inherited the EBSD model root.'
    }
    finally {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}

Invoke-Test 'EBSD root preflight rejects a directory without the reviewed weights' {
    $testRoot = Join-Path ([IO.Path]::GetTempPath()) (
        'materialsagent-local-dev-ebsd-missing-{0}' -f [Guid]::NewGuid().ToString('N')
    )
    New-Item -ItemType Directory -Path $testRoot | Out-Null
    $message = $null
    try {
        try {
            Resolve-EbsdModelRoot -ConfiguredPath $testRoot | Out-Null
        }
        catch {
            $message = $_.Exception.Message
        }
        Assert-Equal `
            $message `
            'LOCAL_DEV_CONFIGURATION_INVALID name=EBSD_MODEL_ROOT reason=weights_missing' `
            'Missing EBSD weights result'
    }
    finally {
        Remove-Item -LiteralPath $testRoot -Recurse -Force
    }
}

Invoke-Test 'non-Backend children remove unrelated caller secrets' {
    $profile = New-LaunchProfile `
        -Runtime Mock `
        -Llm Provider `
        -RuntimeToken 'offline-runtime-token-canary' `
        -BackendPython 'C:\tools\backend-python.exe' `
        -RuntimePython 'C:\tools\backend-python.exe'
    foreach ($name in @(
        'DEEPSEEK_API_KEY',
        'DASHSCOPE_API_KEY',
        'POSTGRES_PASSWORD',
        'MINIO_ACCESS_KEY',
        'MINIO_SECRET_KEY',
        'EBSD_MODEL_ROOT',
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
        'DASHSCOPE_API_KEY',
        'ZTA35G_RUNTIME_TOKEN',
        'ZTA35G_MODEL_ROOT',
        'EBSD_MODEL_ROOT',
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

Invoke-Test 'null process environment overrides are absent from child processes' {
    $name = 'MATERIALSAGENT_LOCAL_DEV_ENV_CANARY'
    $originalExists = Test-Path -LiteralPath "Env:$name"
    $originalValue = if ($originalExists) {
        (Get-Item -LiteralPath "Env:$name").Value
    }
    else {
        $null
    }
    try {
        Set-Item -LiteralPath "Env:$name" -Value 'caller-canary'
        $shell = (Get-Process -Id $PID).Path
        $exitCode = Invoke-WithProcessEnvironment `
            -Values @{$name = $null} `
            -Operation {
                & $shell `
                    -NoLogo `
                    -NoProfile `
                    -NonInteractive `
                    -Command "if (Test-Path -LiteralPath 'Env:$name') { exit 9 }"
                return [int]$LASTEXITCODE
            }
        Assert-Equal $exitCode 0 'Child process inherited a null environment override'
        Assert-Equal `
            (Get-Item -LiteralPath "Env:$name").Value `
            'caller-canary' `
            'Caller environment was not restored'
    }
    finally {
        if ($originalExists) {
            Set-Item -LiteralPath "Env:$name" -Value ([string]$originalValue)
        }
        else {
            Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
        }
    }
}

Invoke-Test 'Provider root dotenv preflight accepts a valid silent probe' {
    Assert-ProviderRootConfiguration `
        -BackendPython 'C:\tools\backend-python.exe' `
        -Probe { param($python) return 0 }
}

Invoke-Test 'Provider root dotenv preflight reports a missing DeepSeek key safely' {
    $message = $null
    try {
        Assert-ProviderRootConfiguration `
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

Invoke-Test 'Provider root dotenv preflight reports a missing Qwen key safely' {
    $message = $null
    try {
        Assert-ProviderRootConfiguration `
            -BackendPython 'C:\tools\backend-python.exe' `
            -Probe { param($python) return 4 }
    }
    catch {
        $message = $_.Exception.Message
    }
    Assert-Equal `
        $message `
        'LOCAL_DEV_CONFIGURATION_MISSING name=DASHSCOPE_API_KEY source=root_dotenv' `
        'Missing Qwen root dotenv key result'
}

Invoke-Test 'Provider root dotenv preflight reports invalid configuration safely' {
    $message = $null
    try {
        Assert-ProviderRootConfiguration `
            -BackendPython 'C:\tools\backend-python.exe' `
            -Probe { param($python) return 2 }
    }
    catch {
        $message = $_.Exception.Message
    }
    Assert-Equal `
        $message `
        'LOCAL_DEV_CONFIGURATION_INVALID source=root_dotenv mode=provider' `
        'Invalid root dotenv result'
}

Invoke-Test 'repeated-start Runtime readiness uses the Backend-owned token' {
    $toolIds = @('zta35g_sem_virtual_lab', 'ebsd_yield_strength_predictor')
    $available = Invoke-RuntimeReadyViaBackendProbe -ToolIds $toolIds -Request {
        param($toolId)
        return [PSCustomObject]@{
            StatusCode = 200
            Content = ('{{"request_id":"offline-request","data":{{"tool_id":"{0}","availability":"AVAILABLE"}}}}' -f $toolId)
        }
    }
    $unavailable = Invoke-RuntimeReadyViaBackendProbe -ToolIds $toolIds -Request {
        param($toolId)
        return [PSCustomObject]@{
            StatusCode = 200
            Content = ('{{"request_id":"offline-request","data":{{"tool_id":"{0}","availability":"{1}"}}}}' -f @(
                $toolId,
                $(if ($toolId -eq 'ebsd_yield_strength_predictor') { 'UNAVAILABLE' } else { 'AVAILABLE' })
            ))
        }
    }
    Assert-True $available 'Backend Runtime proxy rejected available Runtime tools.'
    Assert-True (-not $unavailable) 'Backend Runtime proxy accepted unavailable EBSD.'
}

Invoke-Test 'real Runtime readiness requires the EBSD model endpoint' {
    $visited = New-Object 'System.Collections.Generic.List[string]'
    $available = Invoke-RuntimeReadyProbe `
        -Token 'offline-runtime-token-canary' `
        -RequireEbsd $true `
        -Request {
            param($uri, $headers)
            $visited.Add([string]$uri)
            return [PSCustomObject]@{ StatusCode = 200 }
        }
    $unavailable = Invoke-RuntimeReadyProbe `
        -Token 'offline-runtime-token-canary' `
        -RequireEbsd $true `
        -Request {
            param($uri, $headers)
            return [PSCustomObject]@{
                StatusCode = $(if ($uri.EndsWith('/ebsd/health/ready')) { 503 } else { 200 })
            }
        }
    Assert-True $available 'Runtime readiness rejected READY EBSD.'
    Assert-True `
        ($visited -contains 'http://127.0.0.1:8100/internal/v1/ebsd/health/ready') `
        'Runtime readiness did not inspect EBSD.'
    Assert-True (-not $unavailable) 'Runtime readiness accepted NOT_READY EBSD.'
}

Invoke-Test 'required ports and conflicts cover the complete local stack' {
    $ports = @(Get-RequiredPorts -MaterialsMlEnabled $false | Sort-Object)
    Assert-Equal ($ports -join ',') '3000,5432,8000,8100,9000,9001' 'Required ports'

    $mlPorts = @(Get-RequiredPorts -MaterialsMlEnabled $true | Sort-Object)
    Assert-Equal ($mlPorts -join ',') '3000,5432,8000,8100,8200,9000,9001' 'ML required ports'

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
        'DASHSCOPE_API_KEY',
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

    $deserialized = $record.PSObject.Copy()
    $deserialized.process_start_time = $matching.start_time
    Assert-True (Test-ProcessSnapshotMatch -Record $deserialized -Snapshot $matching) `
        'JSON-deserialized UTC timestamp was incorrectly interpreted as local time.'

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
        [PSCustomObject]@{ role = 'ml_service' },
        [PSCustomObject]@{ role = 'ml_worker' },
        [PSCustomObject]@{ role = 'frontend' },
        [PSCustomObject]@{ role = 'backend' }
    )
    $processOrder = @(Get-StopProcessRecords -Processes $processes)
    Assert-Equal (($processOrder | ForEach-Object role) -join ',') 'frontend,backend,ml_worker,ml_service,runtime' 'Process stop order'

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
    Assert-True ($lines -contains 'ML Service: DISABLED http://127.0.0.1:8200') 'Disabled ML Service status was lost.'
    Assert-True ($lines -contains 'ML Worker: DISABLED') 'Disabled ML Worker status was lost.'
}

Invoke-Test 'legacy Mock launcher preflight does not require the retired cursor secret' {
    $python = Resolve-CondaEnvironmentPython -EnvironmentName 'materialsagent-backend'
    & {
        . (Join-Path $PSScriptRoot 'start-mock-stack.ps1') -LoadFunctionsOnly
        $fixture = [ordered]@{
            LOCAL_ACTOR_ID = 'offline-actor'
            POSTGRES_HOST = '127.0.0.1'
            POSTGRES_DB = 'offline-db'
            POSTGRES_USER = 'offline-user'
            POSTGRES_PASSWORD = 'offline-password'
            MINIO_API_PORT = '9000'
            MINIO_CONSOLE_PORT = '9001'
            MINIO_ENDPOINT = 'http://127.0.0.1:9000'
            MINIO_ACCESS_KEY = 'offline-access'
            MINIO_SECRET_KEY = 'offline-secret'
            MINIO_BUCKET = 'offline-bucket'
            MINIO_SECURE = 'false'
            ZTA35G_RUNTIME_URL = 'http://127.0.0.1:8100'
            ZTA35G_RUNTIME_TOKEN = 'offline-runtime-token'
        }
        $fixturePath = [IO.Path]::GetTempFileName()
        try {
            foreach ($legacyLine in @('', 'TIMELINE_CURSOR_SIGNING_KEY=')) {
                $lines = @($fixture.GetEnumerator() | ForEach-Object { '{0}={1}' -f $_.Key, $_.Value })
                Set-Content -LiteralPath $fixturePath -Value ($lines + $legacyLine) -Encoding UTF8
                $environment = Read-SafeDotEnv -Path $fixturePath
                Assert-True (-not $environment.ContainsKey('TIMELINE_CURSOR_SIGNING_KEY')) 'Retired secret was parsed'
                $backup = Enter-ControlledEnvironment -Environment $environment
                try { Assert-M11ALocalConfiguration -Environment $environment -Python $python }
                finally { Restore-ControlledEnvironment -Backup $backup }
            }
        }
        finally { Remove-Item -LiteralPath $fixturePath -Force }
    }
}

Invoke-Test 'ML profiles isolate Service and Worker credentials' {
    $token = 'worker-token-canary-123456789012345'
    $profile = New-MaterialsMlLaunchProfile `
        -Python 'C:\tools\ml-python.exe' `
        -EnvironmentFile 'C:\repo\services\materials_ml\.env' `
        -WorkerToken $token
    Assert-Equal $profile.service_environment['ML_ENV_FILE'] 'C:\repo\services\materials_ml\.env' 'Service env file'
    Assert-True ($null -eq $profile.service_environment['ML_WORKER_TOKEN']) 'Service received a direct Worker token.'
    Assert-Equal $profile.worker_environment['ML_WORKER_TOKEN'] $token 'Worker token'
    Assert-Equal $profile.worker_environment['ML_SERVICE_URL'] 'http://127.0.0.1:8200' 'Worker Service URL'
    foreach ($name in @(
        'ML_ENV_FILE', 'ML_DATABASE_URL', 'ML_MINIO_ACCESS_KEY',
        'ML_MINIO_SECRET_KEY', 'ML_RESOURCE_TOKEN', 'ML_MCP_TOKEN',
        'ML_ADMIN_DATABASE_URL'
    )) {
        Assert-True ($profile.worker_environment.ContainsKey($name)) "Worker isolation does not control $name."
        Assert-True ($null -eq $profile.worker_environment[$name]) "Worker retains $name."
    }
    Assert-True (-not (($profile.worker_arguments -join ' ').Contains($token))) 'Worker arguments contain its token.'
}

Invoke-Test 'ML feature detection reads only explicit boolean switches' {
    $fixturePath = [IO.Path]::GetTempFileName()
    try {
        Set-Content -LiteralPath $fixturePath -Encoding UTF8 -Value @(
            'ENABLE_DEV_MATERIALS_ML_TOOLS=false',
            'ENABLE_MATERIALS_ML_RESOURCES=true',
            'IGNORED_SECRET=must-not-be-read'
        )
        Assert-True (Get-ConfiguredMaterialsMlEnabled -Path $fixturePath) 'Enabled ML resources were ignored.'
        Set-Content -LiteralPath $fixturePath -Encoding UTF8 -Value @(
            'ENABLE_DEV_MATERIALS_ML_TOOLS=false',
            'ENABLE_MATERIALS_ML_RESOURCES=false',
            'ENABLE_MATERIALS_ML_RESOURCE_CONTEXT=false'
        )
        Assert-True (-not (Get-ConfiguredMaterialsMlEnabled -Path $fixturePath)) 'Disabled ML switches enabled the stack.'
    }
    finally {
        Remove-Item -LiteralPath $fixturePath -Force
    }
}

Invoke-Test 'toolchain preflight preserves safe codes and hides unexpected details' {
    $safeMessage = $null
    try {
        Invoke-LocalDevPreflightStep -Component backend_python -Operation {
            throw 'LOCAL_DEV_PYTHON_VERSION_MISMATCH expected=3.11'
        }
    }
    catch {
        $safeMessage = $_.Exception.Message
    }
    Assert-Equal $safeMessage 'LOCAL_DEV_PYTHON_VERSION_MISMATCH expected=3.11' 'Safe preflight code'

    $mappedMessage = $null
    try {
        Invoke-LocalDevPreflightStep -Component materials_ml_python -Operation {
            throw 'unexpected-detail-secret-canary'
        }
    }
    catch {
        $mappedMessage = $_.Exception.Message
    }
    Assert-Equal $mappedMessage 'LOCAL_DEV_PREFLIGHT_FAILED component=materials_ml_python' 'Mapped preflight code'
    Assert-True (-not $mappedMessage.Contains('secret-canary')) 'Unexpected preflight detail leaked.'
}

Invoke-Test 'multiline Python probes preserve quotes through stdin' {
    $python = Resolve-CondaEnvironmentPython -EnvironmentName 'materialsagent-backend'
    $code = @'
value = "http://127.0.0.1:8200/mcp"
print("QUOTED_PROBE_OK" if value.endswith("/mcp") else "QUOTED_PROBE_BAD")
'@
    $result = Invoke-PythonStdinProbe `
        -Python $python `
        -Code $code `
        -Environment @{}
    Assert-Equal $result.exit_code 0 'Quoted probe exit code'
    $probeOutput = @($result.output)[0]
    Assert-Equal $probeOutput 'QUOTED_PROBE_OK' 'Quoted probe output'
}

Write-Output ("LOCAL_DEV_OFFLINE_TESTS_OK tests={0}" -f $script:Passed)
