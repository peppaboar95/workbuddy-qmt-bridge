[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$Version = "0.3.0",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$initPath = Join-Path $repoRoot "src\workbuddy_qmt\__init__.py"
$expectedWheel = "workbuddy_qmt_bridge-$Version-py3-none-any.whl"
$zipName = "workbuddy-qmt-bridge-$Version.zip"

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot "dist"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

if (-not (Test-Path -LiteralPath $initPath -PathType Leaf)) {
    throw "Package version file not found: $initPath"
}

$initText = Get-Content -LiteralPath $initPath -Raw
if ($initText -notmatch ('__version__\s*=\s*"' + [regex]::Escape($Version) + '"')) {
    throw "Requested version $Version does not match src\workbuddy_qmt\__init__.py"
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$knownOutputs = @(
    $expectedWheel,
    $zipName,
    "$zipName.sha256",
    "SHA256SUMS.txt"
)
foreach ($name in $knownOutputs) {
    $path = Join-Path $OutputDirectory $name
    if (Test-Path -LiteralPath $path -PathType Leaf) {
        Remove-Item -LiteralPath $path -Force
    }
}

& $Python -m pip wheel $repoRoot --no-deps --no-build-isolation --no-cache-dir --wheel-dir $OutputDirectory
if ($LASTEXITCODE -ne 0) {
    throw "Wheel build failed with exit code $LASTEXITCODE"
}

$wheelPath = Join-Path $OutputDirectory $expectedWheel
if (-not (Test-Path -LiteralPath $wheelPath -PathType Leaf)) {
    throw "Expected wheel was not created: $wheelPath"
}

$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$stage = Join-Path $tempRoot ("workbuddy-qmt-release-" + [guid]::NewGuid().ToString("N"))
$stageFull = [System.IO.Path]::GetFullPath($stage)
if (-not $stageFull.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe temporary staging path: $stageFull"
}

try {
    New-Item -ItemType Directory -Path $stageFull | Out-Null

    Copy-Item -LiteralPath $wheelPath -Destination $stageFull
    $installerCandidates = @(Get-ChildItem -LiteralPath $repoRoot -File -Filter "*.cmd")
    if ($installerCandidates.Count -ne 1) {
        throw "Expected exactly one CMD installer in the repository root; found $($installerCandidates.Count)"
    }
    Copy-Item -LiteralPath $installerCandidates[0].FullName -Destination $stageFull
    Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination $stageFull
    Copy-Item -LiteralPath (Join-Path $repoRoot "docs\README-RELEASE.zh-CN.md") -Destination (Join-Path $stageFull "README-RELEASE.zh-CN.md")
    Copy-Item -LiteralPath (Join-Path $repoRoot "docs\P1-VALIDATION.zh-CN.md") -Destination (Join-Path $stageFull "P1-VALIDATION.zh-CN.md")
    Copy-Item -LiteralPath (Join-Path $repoRoot "examples") -Destination (Join-Path $stageFull "examples") -Recurse

    $wheelHash = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    "$wheelHash  $expectedWheel" | Set-Content -LiteralPath (Join-Path $stageFull "SHA256SUMS.txt") -Encoding ascii

    $zipPath = Join-Path $OutputDirectory $zipName
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    [System.IO.Compression.ZipFile]::CreateFromDirectory(
        $stageFull,
        $zipPath,
        [System.IO.Compression.CompressionLevel]::Optimal,
        $false
    )

    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    @(
        "$wheelHash  $expectedWheel"
        "$zipHash  $zipName"
    ) | Set-Content -LiteralPath (Join-Path $OutputDirectory "SHA256SUMS.txt") -Encoding ascii
    "$zipHash  $zipName" | Set-Content -LiteralPath (Join-Path $OutputDirectory "$zipName.sha256") -Encoding ascii

    Write-Host "Release build completed:"
    Get-ChildItem -LiteralPath $OutputDirectory -File |
        Where-Object { $_.Name -in $knownOutputs } |
        Sort-Object Name |
        Select-Object Name, Length, LastWriteTime |
        Format-Table -AutoSize
}
finally {
    if (Test-Path -LiteralPath $stageFull -PathType Container) {
        $resolvedStage = [System.IO.Path]::GetFullPath($stageFull)
        if (-not $resolvedStage.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove unsafe temporary path: $resolvedStage"
        }
        Remove-Item -LiteralPath $resolvedStage -Recurse -Force
    }
}
