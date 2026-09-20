param([string]$Python = '')
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
if (-not $Python) { $Python = Join-Path $repoRoot 'services/materials_ml/.venv/Scripts/python.exe' }
$Python = (Resolve-Path -LiteralPath $Python).Path
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Windows Job Object acceptance requires Windows.' }
& $Python -c 'import sys; assert sys.version_info[:2] == (3, 11), "Python 3.11 required"; assert sys.prefix != sys.base_prefix, "Isolated environment required"'
if ($LASTEXITCODE -ne 0) { throw 'ML interpreter validation failed.' }
$previousIntegration = $env:ML_INTEGRATION
Push-Location $repoRoot
try {
    & $Python -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'ML dependency check failed.' }
    $env:ML_INTEGRATION = '1'
    & $Python -m pytest services/materials_ml/tests packages/materials_storage/tests -q
    if ($LASTEXITCODE -ne 0) { throw 'ML acceptance failed; the requested phase is not complete.' }
} finally {
    $env:ML_INTEGRATION = $previousIntegration
    Pop-Location
}
