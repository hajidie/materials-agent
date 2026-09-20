param(
    [Parameter(Mandatory = $true)][string]$BackendPython,
    [string]$MLPython = ''
)
$ErrorActionPreference = 'Stop'
# P4's entrypoint runs the complete Backend and ML trees, including the P5
# PostgreSQL fences and real Resource API/restart scenarios. No separate stack.
& (Join-Path $PSScriptRoot 'acceptance-p4.ps1') -BackendPython $BackendPython -MLPython $MLPython
if ($LASTEXITCODE -ne 0) { throw 'P5 acceptance failed; do not declare completion.' }
