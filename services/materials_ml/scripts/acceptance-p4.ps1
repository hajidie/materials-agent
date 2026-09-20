param(
    [Parameter(Mandatory = $true)][string]$BackendPython,
    [string]$MLPython = ''
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$BackendPython = (Resolve-Path -LiteralPath $BackendPython).Path
if (-not $MLPython) { $MLPython = Join-Path $repoRoot 'services/materials_ml/.venv/Scripts/python.exe' }
$MLPython = (Resolve-Path -LiteralPath $MLPython).Path
if ($BackendPython -eq $MLPython) { throw 'Backend and ML must use separate interpreters.' }
$previousBackend = $env:P4_BACKEND_PYTHON
Push-Location $repoRoot
try {
    & $BackendPython -c 'import sys, importlib.util; from importlib.metadata import version; assert sys.version_info[:2] == (3,11); assert version("mcp") == "1.30.0"; assert version("sse-starlette") == "3.0.3"; assert all(importlib.util.find_spec(n) is None for n in ("materials_ml", "materials_ml_service", "sklearn", "joblib")), "Backend must not contain ML dependencies"'
    if ($LASTEXITCODE -ne 0) { throw 'Backend isolation or optional MCP dependency check failed.' }
    & $BackendPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Backend dependencies are inconsistent.' }
    & $BackendPython -m pytest backend/tests -q
    if ($LASTEXITCODE -ne 0) { throw 'Backend regression failed; P4 is not complete.' }
    $env:P4_BACKEND_PYTHON = $BackendPython
    & (Join-Path $PSScriptRoot 'acceptance.ps1') -Python $MLPython
    if ($LASTEXITCODE -ne 0) { throw 'P4 real integration acceptance failed.' }
} finally {
    $env:P4_BACKEND_PYTHON = $previousBackend
    Pop-Location
}
