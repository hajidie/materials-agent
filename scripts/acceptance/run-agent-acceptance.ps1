[CmdletBinding()]
param([string]$BackendPython = 'python')
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
Push-Location $repoRoot
try {
    $version = & $BackendPython -c 'import sys; print("%d.%d" % sys.version_info[:2])'
    if ($LASTEXITCODE -ne 0 -or $version -ne '3.11') { throw 'Backend acceptance requires Python 3.11.' }
    & $BackendPython -m pytest backend/tests mock-runtime/tests -q
    if ($LASTEXITCODE -ne 0) { throw 'Backend or Mock Runtime tests failed.' }
    npm --prefix frontend test -- --run
    if ($LASTEXITCODE -ne 0) { throw 'Frontend tests failed.' }
    npm --prefix frontend run typecheck
    if ($LASTEXITCODE -ne 0) { throw 'Frontend typecheck failed.' }
    npm --prefix frontend run build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    & .\scripts\dev\test-local-dev.ps1
    & .\scripts\dev\test-check-scope.ps1
    git diff --check
    if ($LASTEXITCODE -ne 0) { throw 'Whitespace validation failed.' }
    Write-Output 'AGENT_OFFLINE_ACCEPTANCE_OK'
} finally { Pop-Location }
