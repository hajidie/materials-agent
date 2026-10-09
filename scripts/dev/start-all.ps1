[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('Start', 'Stop')]
    [string]$Action = 'Start',
    [string]$BackendPython,
    [string]$RuntimePython,
    [string]$MaterialsMlPython,
    [string]$EbsdModelRoot,
    [string]$Tc4Weights,
    [string]$Tc4RuntimeDataDir,
    [ValidateRange(10, 900)]
    [int]$ReadyTimeoutSeconds = 300
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Keep process ownership, migrations, readiness, and cleanup in one launcher.
$launchArguments = @{
    Action = $Action
    Runtime = 'Real'
    Llm = 'Provider'
    AllFeatures = $true
    ReadyTimeoutSeconds = $ReadyTimeoutSeconds
}
foreach ($name in @(
    'BackendPython', 'RuntimePython', 'MaterialsMlPython',
    'EbsdModelRoot', 'Tc4Weights', 'Tc4RuntimeDataDir'
)) {
    if ($PSBoundParameters.ContainsKey($name)) {
        $launchArguments[$name] = $PSBoundParameters[$name]
    }
}
& (Join-Path $PSScriptRoot 'local-dev.ps1') @launchArguments
exit $LASTEXITCODE
