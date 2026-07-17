[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$ExitCodes = @{
    SEM_INTEGRITY_OK = 0
    MANIFEST_NOT_FOUND = 2
    SEM_ROOT_NOT_FOUND = 3
    FILE_SET_MISMATCH = 4
    FILE_SIZE_MISMATCH = 5
    FILE_HASH_MISMATCH = 6
    MANIFEST_INVALID = 7
}

function Complete-Check {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Code,

        [Parameter(Mandatory = $true)]
        [string]$Summary
    )

    Write-Output ("{0}: {1}" -f $Code, $Summary)
    exit $ExitCodes[$Code]
}

function Get-AggregateFingerprint {
    param(
        [Parameter(Mandatory = $true)]
        [object[]]$Records
    )

    $canonical = New-Object System.Text.StringBuilder
    foreach ($record in $Records) {
        [void]$canonical.Append([string]$record.relative_path)
        [void]$canonical.Append([char]9)
        [void]$canonical.Append([string]([int64]$record.size_bytes))
        [void]$canonical.Append([char]9)
        [void]$canonical.Append(([string]$record.sha256).ToLowerInvariant())
        [void]$canonical.Append([char]10)
    }

    $encoding = New-Object System.Text.UTF8Encoding($false)
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = $encoding.GetBytes($canonical.ToString())
        return ([BitConverter]::ToString($sha256.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $sha256.Dispose()
    }
}

$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
$manifestPath = Join-Path $repoRoot 'docs\acceptance\sem-package-manifest.json'
$semRoot = Join-Path $repoRoot 'SEM'

if (-not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    Complete-Check -Code 'MANIFEST_NOT_FOUND' -Summary 'docs/acceptance/sem-package-manifest.json was not found.'
}

if (-not (Test-Path -LiteralPath $semRoot -PathType Container)) {
    Complete-Check -Code 'SEM_ROOT_NOT_FOUND' -Summary 'SEM/ was not found.'
}

try {
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $requiredProperties = @('manifest_schema_version', 'generated_at', 'source_root', 'file_count', 'total_size_bytes', 'aggregate_fingerprint', 'aggregate_fingerprint_algorithm', 'files')
    foreach ($property in $requiredProperties) {
        if ($null -eq $manifest.PSObject.Properties[$property]) {
            throw "Missing top-level property: $property"
        }
    }
    if ([string]$manifest.manifest_schema_version -ne '1.0') { throw 'Unsupported manifest_schema_version.' }
    if ([string]$manifest.source_root -ne 'SEM/') { throw 'source_root must be SEM/.' }
    if ([string]$manifest.aggregate_fingerprint_algorithm -ne 'SHA-256 over UTF-8 (no BOM) canonical lines sorted by ordinal relative_path: relative_path<TAB>size_bytes<TAB>lowercase_sha256<LF>. Paths are relative to SEM/.') { throw 'Unsupported aggregate_fingerprint_algorithm.' }
    if ([string]$manifest.aggregate_fingerprint -notmatch '^[0-9a-f]{64}$') { throw 'aggregate_fingerprint must be a lowercase SHA-256 value.' }
    $generatedAt = [DateTimeOffset]::Parse([string]$manifest.generated_at, [Globalization.CultureInfo]::InvariantCulture)
    if ($generatedAt.Offset -ne [TimeSpan]::Zero) { throw 'generated_at must be UTC.' }
}
catch {
    Complete-Check -Code 'MANIFEST_INVALID' -Summary ("Manifest parsing or top-level validation failed: {0}" -f $_.Exception.Message)
}

try {
    $manifestRecords = @($manifest.files)
    if ([int64]$manifest.file_count -ne $manifestRecords.Count) { throw 'file_count does not match files length.' }
    $manifestPaths = New-Object System.Collections.Generic.List[string]
    $normalizedRecords = New-Object System.Collections.Generic.List[object]
    $manifestTotal = [int64]0

    foreach ($record in $manifestRecords) {
        foreach ($property in @('relative_path', 'size_bytes', 'sha256')) {
            if ($null -eq $record.PSObject.Properties[$property]) { throw "Manifest file record is missing $property." }
        }

        $relativePath = [string]$record.relative_path
        if ([string]::IsNullOrWhiteSpace($relativePath) -or $relativePath.Contains('\') -or $relativePath.StartsWith('/') -or [IO.Path]::IsPathRooted($relativePath) -or $relativePath -match '(^|/)\.\.(/|$)') {
            throw "Unsafe or non-canonical relative_path: $relativePath"
        }

        $size = [int64]$record.size_bytes
        if ($size -lt 0) { throw "Negative size for $relativePath." }
        $hash = ([string]$record.sha256).ToLowerInvariant()
        if ($hash -notmatch '^[0-9a-f]{64}$') { throw "Invalid SHA-256 for $relativePath." }

        $manifestPaths.Add($relativePath)
        $manifestTotal += $size
        $normalizedRecords.Add([pscustomobject]@{
            relative_path = $relativePath
            size_bytes = $size
            sha256 = $hash
        })
    }

    $sortedManifestPaths = $manifestPaths.ToArray()
    [Array]::Sort($sortedManifestPaths, [StringComparer]::Ordinal)
    for ($index = 0; $index -lt $manifestPaths.Count; $index++) {
        if ($manifestPaths[$index] -cne $sortedManifestPaths[$index]) { throw 'files must be sorted by ordinal relative_path.' }
        if ($index -gt 0 -and $manifestPaths[$index] -ceq $manifestPaths[$index - 1]) { throw "Duplicate relative_path: $($manifestPaths[$index])" }
    }

    if ([int64]$manifest.total_size_bytes -ne $manifestTotal) { throw 'total_size_bytes does not match the sum of file records.' }
    $manifestAggregate = Get-AggregateFingerprint -Records $normalizedRecords.ToArray()
    if ($manifestAggregate -cne [string]$manifest.aggregate_fingerprint) { throw 'aggregate_fingerprint does not match the manifest file records.' }
}
catch {
    Complete-Check -Code 'MANIFEST_INVALID' -Summary ("Manifest file-record validation failed: {0}" -f $_.Exception.Message)
}

try {
    $actualPaths = @(Get-ChildItem -LiteralPath $semRoot -Recurse -Force -File | ForEach-Object {
        $_.FullName.Substring($semRoot.Length + 1).Replace([char]92, [char]47)
    })
    [Array]::Sort($actualPaths, [StringComparer]::Ordinal)
}
catch {
    Complete-Check -Code 'FILE_SET_MISMATCH' -Summary ("Could not enumerate SEM/: {0}" -f $_.Exception.Message)
}

if ($actualPaths.Count -ne [int64]$manifest.file_count) {
    Complete-Check -Code 'FILE_SET_MISMATCH' -Summary ("Expected {0} files but found {1}." -f $manifest.file_count, $actualPaths.Count)
}

for ($index = 0; $index -lt $actualPaths.Count; $index++) {
    if ($actualPaths[$index] -cne $manifestPaths[$index]) {
        Complete-Check -Code 'FILE_SET_MISMATCH' -Summary ("Path mismatch at index {0}: expected '{1}', found '{2}'." -f $index, $manifestPaths[$index], $actualPaths[$index])
    }
}

$actualRecords = New-Object System.Collections.Generic.List[object]
$actualTotal = [int64]0
for ($index = 0; $index -lt $actualPaths.Count; $index++) {
    $relativePath = $actualPaths[$index]
    $fullPath = Join-Path $semRoot $relativePath.Replace([char]47, [char]92)
    $item = Get-Item -LiteralPath $fullPath
    $expected = $normalizedRecords[$index]

    if ([int64]$item.Length -ne [int64]$expected.size_bytes) {
        Complete-Check -Code 'FILE_SIZE_MISMATCH' -Summary ("{0}: expected {1} bytes, found {2}." -f $relativePath, $expected.size_bytes, $item.Length)
    }

    try {
        $actualHash = (Get-FileHash -LiteralPath $fullPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    catch {
        Complete-Check -Code 'FILE_HASH_MISMATCH' -Summary ("Could not hash {0}: {1}" -f $relativePath, $_.Exception.Message)
    }

    if ($actualHash -cne [string]$expected.sha256) {
        Complete-Check -Code 'FILE_HASH_MISMATCH' -Summary ("{0}: expected {1}, found {2}." -f $relativePath, $expected.sha256, $actualHash)
    }

    $actualTotal += [int64]$item.Length
    $actualRecords.Add([pscustomobject]@{
        relative_path = $relativePath
        size_bytes = [int64]$item.Length
        sha256 = $actualHash
    })
}

if ($actualTotal -ne [int64]$manifest.total_size_bytes) {
    Complete-Check -Code 'FILE_SIZE_MISMATCH' -Summary ("Expected total_size_bytes {0}, found {1}." -f $manifest.total_size_bytes, $actualTotal)
}

$actualAggregate = Get-AggregateFingerprint -Records $actualRecords.ToArray()
if ($actualAggregate -cne [string]$manifest.aggregate_fingerprint) {
    Complete-Check -Code 'FILE_HASH_MISMATCH' -Summary ("Aggregate fingerprint mismatch: expected {0}, found {1}." -f $manifest.aggregate_fingerprint, $actualAggregate)
}

Complete-Check -Code 'SEM_INTEGRITY_OK' -Summary ("files={0} total_size_bytes={1} aggregate_fingerprint={2}" -f $actualPaths.Count, $actualTotal, $actualAggregate)
