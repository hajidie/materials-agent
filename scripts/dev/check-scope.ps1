[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$Label,

    [string[]]$AllowedPath = @(),

    [string]$AllowlistFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

# Stable exit codes:
# 0 = SCOPE_OK
# 3 = GIT_UNAVAILABLE
# 4 = NOT_A_GIT_REPOSITORY
# 5 = OUT_OF_SCOPE_CHANGES
# 6 = SCRIPT_CONFIGURATION_ERROR
$ExitCodes = @{
    SCOPE_OK = 0
    GIT_UNAVAILABLE = 3
    NOT_A_GIT_REPOSITORY = 4
    OUT_OF_SCOPE_CHANGES = 5
    SCRIPT_CONFIGURATION_ERROR = 6
}

function Complete-ScopeCheck {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,

        [Parameter(Mandatory = $true)]
        [string]$Summary,

        [string[]]$Paths = @()
    )

    Write-Output ("{0} {1}" -f $Code, $Summary)
    foreach ($path in $Paths) {
        Write-Output $path
    }
    exit $ExitCodes[$Code]
}

$normalizedLabel = $Label.Trim().ToUpperInvariant()
if ($normalizedLabel -notmatch '^[A-Z0-9][A-Z0-9._-]{0,63}$') {
    Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary 'INVALID_LABEL'
}

function ConvertTo-RepoPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $normalized = $Path.Trim().Replace([char]92, [char]47)
    while ($normalized.StartsWith('./', [StringComparison]::Ordinal)) {
        $normalized = $normalized.Substring(2)
    }
    return $normalized
}

function ConvertTo-AllowedRepoPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path
    )

    $normalized = ConvertTo-RepoPath -Path $Path
    $segments = @($normalized.Split([char]47))
    $invalid = (
        [string]::IsNullOrWhiteSpace($normalized) -or
        [IO.Path]::IsPathRooted($normalized) -or
        $normalized -match '^[A-Za-z]:' -or
        $normalized.EndsWith('/', [StringComparison]::Ordinal) -or
        [System.Management.Automation.WildcardPattern]::ContainsWildcardCharacters($normalized) -or
        $segments -contains '' -or
        $segments -contains '.' -or
        $segments -contains '..'
    )
    if ($invalid) {
        Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary $normalizedLabel
    }
    $workingTreePath = Join-Path (Get-Location).Path $normalized
    if (Test-Path -LiteralPath $workingTreePath -PathType Container) {
        Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary $normalizedLabel
    }
    return $normalized
}

$configuredPaths = New-Object 'System.Collections.Generic.List[string]'
foreach ($path in @($AllowedPath)) {
    if ($null -eq $path) {
        Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary $normalizedLabel
    }
    $configuredPaths.Add((ConvertTo-AllowedRepoPath -Path $path))
}

if (-not [string]::IsNullOrWhiteSpace($AllowlistFile)) {
    try {
        $resolvedAllowlistFile = [IO.Path]::GetFullPath($AllowlistFile)
        if (-not (Test-Path -LiteralPath $resolvedAllowlistFile -PathType Leaf)) {
            Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary $normalizedLabel
        }
        foreach ($line in @(Get-Content -LiteralPath $resolvedAllowlistFile -Encoding UTF8)) {
            $trimmed = $line.Trim()
            if ([string]::IsNullOrWhiteSpace($trimmed) -or $trimmed.StartsWith('#')) {
                continue
            }
            $configuredPaths.Add((ConvertTo-AllowedRepoPath -Path $trimmed))
        }
    }
    catch {
        Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary $normalizedLabel
    }
}

if ($configuredPaths.Count -eq 0) {
    Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary $normalizedLabel
}

$allowedPaths = @{}
foreach ($path in $configuredPaths) {
    $allowedPaths[$path] = $true
}

$gitExecutable = $null
if (-not [string]::IsNullOrWhiteSpace($env:PATH)) {
    foreach ($directory in @($env:PATH -split ';')) {
        $normalizedDirectory = $directory.Trim().Trim('"')
        if ([string]::IsNullOrWhiteSpace($normalizedDirectory)) {
            continue
        }
        $candidate = Join-Path $normalizedDirectory 'git.exe'
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            $gitExecutable = [IO.Path]::GetFullPath($candidate)
            break
        }
    }
}
if ($null -eq $gitExecutable) {
    Complete-ScopeCheck -Code 'GIT_UNAVAILABLE' -Summary 'git executable was not found.'
}

try {
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $gitExecutable
    $startInfo.Arguments = '-c core.quotepath=false status --porcelain=v1 -z --untracked-files=all'
    $startInfo.WorkingDirectory = (Get-Location).Path
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    [void]$process.Start()
    $statusOutput = $process.StandardOutput.ReadToEnd()
    $statusError = $process.StandardError.ReadToEnd()
    $process.WaitForExit()
    $gitExitCode = $process.ExitCode
    $process.Dispose()
}
catch {
    Complete-ScopeCheck -Code 'GIT_UNAVAILABLE' -Summary 'git could not be executed.'
}

if ($gitExitCode -ne 0) {
    if ($statusError -match '(?i)not a git repository') {
        Complete-ScopeCheck -Code 'NOT_A_GIT_REPOSITORY' -Summary 'current directory is not inside a Git work tree.'
    }
    Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary 'git status failed unexpectedly.'
}

$changedPaths = New-Object 'System.Collections.Generic.List[string]'
$records = $statusOutput.Split([char]0)
for ($index = 0; $index -lt $records.Length; $index++) {
    $record = $records[$index]
    if ([string]::IsNullOrEmpty($record)) {
        continue
    }
    if ($record.Length -lt 4 -or $record[2] -ne ' ') {
        Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary 'git status returned an unsupported record.'
    }

    $status = $record.Substring(0, 2)
    $primaryPath = ConvertTo-RepoPath -Path $record.Substring(3)
    $changedPaths.Add($primaryPath)

    # With porcelain v1 -z, rename/copy records store destination first and
    # source in the following NUL-delimited field. Check both paths.
    if ($status.Contains('R') -or $status.Contains('C')) {
        $index++
        if ($index -ge $records.Length -or [string]::IsNullOrEmpty($records[$index])) {
            Complete-ScopeCheck -Code 'SCRIPT_CONFIGURATION_ERROR' -Summary 'git status returned an incomplete rename or copy record.'
        }
        $sourcePath = ConvertTo-RepoPath -Path $records[$index]
        $changedPaths.Add($sourcePath)
    }
}

$outOfScope = @(
    $changedPaths |
        Where-Object { -not $allowedPaths.ContainsKey($_) } |
        Sort-Object -Unique
)

if ($outOfScope.Count -gt 0) {
    Complete-ScopeCheck -Code 'OUT_OF_SCOPE_CHANGES' -Summary $normalizedLabel -Paths $outOfScope
}

Complete-ScopeCheck -Code 'SCOPE_OK' -Summary $normalizedLabel
