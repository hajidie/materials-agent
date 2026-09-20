param(
    [Parameter(Mandatory = $true)][string]$BackendPython,
    [string]$MLPython = '',
    [switch]$RealLLM
)
$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
Push-Location $repoRoot
try {
    npm --prefix frontend test -- --run
    if ($LASTEXITCODE -ne 0) { throw 'P7 frontend tests failed.' }
    npm --prefix frontend run typecheck
    if ($LASTEXITCODE -ne 0) { throw 'P7 frontend typecheck failed.' }
    npm --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { throw 'P7 frontend build failed.' }
    if ($RealLLM) { Write-Output 'Real ML language acceptance uses an explicit 24576-token decision prompt limit; repository defaults remain unchanged.' }
    & (Join-Path $PSScriptRoot 'acceptance-p6.ps1') -BackendPython $BackendPython -MLPython $MLPython -RealLLM:$RealLLM
    if ($LASTEXITCODE -ne 0) { throw 'P7 foundation or language acceptance failed.' }
    Write-Output 'Automated checks finished. Separate real browser acceptance is required before declaring P7 complete.'
} finally { Pop-Location }
