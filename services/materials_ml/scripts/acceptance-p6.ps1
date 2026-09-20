param(
    [Parameter(Mandatory = $true)][string]$BackendPython,
    [string]$MLPython = '',
    [switch]$RealLLM
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
if (-not $MLPython) { $MLPython = Join-Path $repoRoot 'services/materials_ml/.venv/Scripts/python.exe' }
$previousReal = $env:P6_REAL_LLM
$previousBackend = $env:P4_BACKEND_PYTHON
$previousIntegration = $env:ML_INTEGRATION
Push-Location $repoRoot
try {
    $env:P6_REAL_LLM = '0'
    & (Join-Path $PSScriptRoot 'acceptance-p5.ps1') -BackendPython $BackendPython -MLPython $MLPython
    if ($LASTEXITCODE -ne 0) { throw 'P6 foundation acceptance failed.' }
    if ($RealLLM) {
        $env:P6_REAL_LLM = '1'
        $env:ML_INTEGRATION = '1'
        $env:P4_BACKEND_PYTHON = (Resolve-Path -LiteralPath $BackendPython).Path
        & $MLPython -m pytest services/materials_ml/tests/service/test_platform_resource_context.py -k opt_in -q
        if ($LASTEXITCODE -ne 0) { throw 'Real language acceptance failed; P6 is not complete.' }
    } else {
        Write-Output 'Foundation checked. Real LLM acceptance was not requested; do not declare full P6 acceptance.'
    }
} finally {
    $env:P6_REAL_LLM = $previousReal
    $env:P4_BACKEND_PYTHON = $previousBackend
    $env:ML_INTEGRATION = $previousIntegration
    Pop-Location
}
