[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$scopeScript = Join-Path $PSScriptRoot 'check-scope.ps1'
$powershellExe = (Get-Command powershell.exe -ErrorAction Stop).Source
$gitExe = (Get-Command git.exe -ErrorAction Stop).Source
$testRoot = Join-Path ([IO.Path]::GetTempPath()) (
    'materialsagent-check-scope-' + [Guid]::NewGuid().ToString('N')
)
$script:testCount = 0

function Assert-Equal {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Expected,

        [Parameter(Mandatory = $true)]
        [object]$Actual,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if ($Expected -ne $Actual) {
        throw ("{0}: expected <{1}> but got <{2}>" -f $Message, $Expected, $Actual)
    }
}

function Assert-Contains {
    param(
        [Parameter(Mandatory = $true)]
        [string[]]$Lines,

        [Parameter(Mandatory = $true)]
        [string]$Expected,

        [Parameter(Mandatory = $true)]
        [string]$Message
    )

    if ($Lines -notcontains $Expected) {
        throw ("{0}: missing line <{1}>; output was <{2}>" -f (
            $Message,
            $Expected,
            ($Lines -join ' | ')
        ))
    }
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Repo,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    $previousErrorActionPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = @(& $gitExe -C $Repo @Arguments 2>&1 | ForEach-Object { $_.ToString() })
        $exitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousErrorActionPreference
    }
    if ($exitCode -ne 0) {
        throw ("git failed ({0}): {1}" -f $exitCode, ($output -join ' | '))
    }
    return $output
}

function New-TestRepository {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    $repo = Join-Path $testRoot $Name
    $null = New-Item -ItemType Directory -Path (Join-Path $repo 'dir') -Force
    [IO.File]::WriteAllText((Join-Path $repo 'allowed.txt'), "base`n")
    [IO.File]::WriteAllText((Join-Path $repo 'old.txt'), "base`n")
    [IO.File]::WriteAllText((Join-Path $repo 'dir\allowed.txt'), "base`n")
    $null = Invoke-Git -Repo $repo -Arguments @('init', '--quiet')
    $null = Invoke-Git -Repo $repo -Arguments @('config', 'user.name', 'Scope Test')
    $null = Invoke-Git -Repo $repo -Arguments @('config', 'user.email', 'scope@example.invalid')
    $null = Invoke-Git -Repo $repo -Arguments @('add', '--', 'allowed.txt', 'old.txt', 'dir/allowed.txt')
    $null = Invoke-Git -Repo $repo -Arguments @('commit', '--quiet', '-m', 'baseline')
    return $repo
}

function Invoke-Scope {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Repo,

        [Parameter(Mandatory = $true)]
        [string]$Label,

        [string]$AllowlistFile,

        [string]$AllowedPath,

        [switch]$NoAllowlist
    )

    $arguments = @(
        '-NoProfile',
        '-ExecutionPolicy',
        'Bypass',
        '-File',
        $scopeScript,
        '-Label',
        $Label
    )
    if (-not $NoAllowlist -and -not [string]::IsNullOrWhiteSpace($AllowlistFile)) {
        $arguments += @('-AllowlistFile', $AllowlistFile)
    }
    if (-not $NoAllowlist -and -not [string]::IsNullOrWhiteSpace($AllowedPath)) {
        $arguments += @('-AllowedPath', $AllowedPath)
    }

    Push-Location -LiteralPath $Repo
    try {
        $previousErrorActionPreference = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            $lines = @(& $powershellExe @arguments 2>&1 | ForEach-Object { $_.ToString() })
            $exitCode = $LASTEXITCODE
        }
        finally {
            $ErrorActionPreference = $previousErrorActionPreference
        }
    }
    finally {
        Pop-Location
    }

    return [pscustomobject]@{
        ExitCode = $exitCode
        Lines = $lines
    }
}

function Write-Allowlist {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name,

        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string[]]$Lines
    )

    $path = Join-Path $testRoot $Name
    [IO.File]::WriteAllLines($path, $Lines, [Text.UTF8Encoding]::new($false))
    return $path
}

function Complete-Test {
    $script:testCount++
}

try {
    $null = New-Item -ItemType Directory -Path $testRoot -Force

    $repo = New-TestRepository -Name 'direct-allowed'
    [IO.File]::AppendAllText((Join-Path $repo 'allowed.txt'), "change`n")
    $result = Invoke-Scope -Repo $repo -Label 'DIRECT' -AllowedPath 'allowed.txt'
    Assert-Equal -Expected 0 -Actual $result.ExitCode -Message 'direct allowlist exit code'
    Assert-Contains -Lines $result.Lines -Expected 'SCOPE_OK DIRECT' -Message 'direct allowlist marker'
    Complete-Test

    $repo = New-TestRepository -Name 'untracked-rejected'
    [IO.File]::WriteAllText((Join-Path $repo 'unexpected.txt'), "unexpected`n")
    $result = Invoke-Scope -Repo $repo -Label 'UNTRACKED' -AllowedPath 'allowed.txt'
    Assert-Equal -Expected 5 -Actual $result.ExitCode -Message 'untracked rejection exit code'
    Assert-Contains -Lines $result.Lines -Expected 'OUT_OF_SCOPE_CHANGES UNTRACKED' -Message 'untracked rejection marker'
    Assert-Contains -Lines $result.Lines -Expected 'unexpected.txt' -Message 'untracked rejection path'
    Complete-Test

    $repo = New-TestRepository -Name 'staged-allowed'
    [IO.File]::AppendAllText((Join-Path $repo 'dir\allowed.txt'), "change`n")
    $null = Invoke-Git -Repo $repo -Arguments @('add', '--', 'dir/allowed.txt')
    $allowlist = Write-Allowlist -Name 'staged.txt' -Lines @(
        '# comments and blank lines are ignored',
        '',
        '.\dir\allowed.txt'
    )
    $result = Invoke-Scope -Repo $repo -Label 'STAGED' -AllowlistFile $allowlist
    Assert-Equal -Expected 0 -Actual $result.ExitCode -Message 'staged allowlist exit code'
    Assert-Contains -Lines $result.Lines -Expected 'SCOPE_OK STAGED' -Message 'staged allowlist marker'
    Complete-Test

    $repo = New-TestRepository -Name 'rename-both-paths'
    $null = Invoke-Git -Repo $repo -Arguments @('mv', 'old.txt', 'renamed.txt')
    $destinationOnly = Write-Allowlist -Name 'rename-destination-only.txt' -Lines @('renamed.txt')
    $result = Invoke-Scope -Repo $repo -Label 'RENAME' -AllowlistFile $destinationOnly
    Assert-Equal -Expected 5 -Actual $result.ExitCode -Message 'rename source rejection exit code'
    Assert-Contains -Lines $result.Lines -Expected 'old.txt' -Message 'rename source rejection path'
    $bothPaths = Write-Allowlist -Name 'rename-both.txt' -Lines @('old.txt', 'renamed.txt')
    $result = Invoke-Scope -Repo $repo -Label 'RENAME' -AllowlistFile $bothPaths
    Assert-Equal -Expected 0 -Actual $result.ExitCode -Message 'rename both paths exit code'
    Complete-Test

    $repo = New-TestRepository -Name 'combined-sources'
    [IO.File]::AppendAllText((Join-Path $repo 'allowed.txt'), "change`n")
    [IO.File]::WriteAllText((Join-Path $repo 'extra.txt'), "extra`n")
    $allowlist = Write-Allowlist -Name 'combined.txt' -Lines @('allowed.txt')
    $result = Invoke-Scope `
        -Repo $repo `
        -Label 'COMBINED' `
        -AllowlistFile $allowlist `
        -AllowedPath 'extra.txt'
    Assert-Equal -Expected 0 -Actual $result.ExitCode -Message 'combined sources exit code'
    Complete-Test

    $repo = New-TestRepository -Name 'invalid-config'
    $result = Invoke-Scope -Repo $repo -Label 'EMPTY' -NoAllowlist
    Assert-Equal -Expected 6 -Actual $result.ExitCode -Message 'empty allowlist exit code'
    Assert-Contains -Lines $result.Lines -Expected 'SCRIPT_CONFIGURATION_ERROR EMPTY' -Message 'empty allowlist marker'
    $result = Invoke-Scope `
        -Repo $repo `
        -Label 'MISSING' `
        -AllowlistFile (Join-Path $testRoot 'missing.txt')
    Assert-Equal -Expected 6 -Actual $result.ExitCode -Message 'missing allowlist exit code'
    $invalidAllowlist = Write-Allowlist -Name 'invalid.txt' -Lines @('../outside.txt')
    $result = Invoke-Scope -Repo $repo -Label 'INVALID' -AllowlistFile $invalidAllowlist
    Assert-Equal -Expected 6 -Actual $result.ExitCode -Message 'parent traversal exit code'
    Assert-Contains -Lines $result.Lines -Expected 'SCRIPT_CONFIGURATION_ERROR INVALID' -Message 'invalid allowlist marker'
    $wildcardAllowlist = Write-Allowlist -Name 'wildcard.txt' -Lines @('backend/*.py')
    $result = Invoke-Scope -Repo $repo -Label 'WILDCARD' -AllowlistFile $wildcardAllowlist
    Assert-Equal -Expected 6 -Actual $result.ExitCode -Message 'wildcard allowlist exit code'
    $directoryAllowlist = Write-Allowlist -Name 'directory.txt' -Lines @('dir')
    $result = Invoke-Scope -Repo $repo -Label 'DIRECTORY' -AllowlistFile $directoryAllowlist
    Assert-Equal -Expected 6 -Actual $result.ExitCode -Message 'directory allowlist exit code'
    Complete-Test

    Write-Output ("CHECK_SCOPE_TESTS_OK tests={0}" -f $script:testCount)
}
finally {
    $resolvedTestRoot = [IO.Path]::GetFullPath($testRoot)
    $resolvedTempRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
    if (
        $resolvedTestRoot.StartsWith($resolvedTempRoot, [StringComparison]::OrdinalIgnoreCase) -and
        $resolvedTestRoot -ne $resolvedTempRoot -and
        (Test-Path -LiteralPath $resolvedTestRoot)
    ) {
        Remove-Item -LiteralPath $resolvedTestRoot -Recurse -Force
    }
}
