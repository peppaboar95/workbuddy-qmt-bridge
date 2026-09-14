[CmdletBinding()]
param(
    [string]$Python = "python",
    [string]$Version = "",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$initPath = Join-Path $repoRoot "src\workbuddy_qmt\__init__.py"
$projectPath = Join-Path $repoRoot "pyproject.toml"

if (-not (Test-Path -LiteralPath $projectPath -PathType Leaf)) {
    throw "Project metadata not found: $projectPath"
}

$projectText = Get-Content -LiteralPath $projectPath -Raw
$projectVersionMatch = [regex]::Match(
    $projectText,
    '(?m)^version\s*=\s*"(?<version>[^"]+)"\s*$'
)
if (-not $projectVersionMatch.Success) {
    throw "Project version not found in pyproject.toml"
}
$projectVersion = $projectVersionMatch.Groups["version"].Value
if ([string]::IsNullOrWhiteSpace($Version)) {
    $Version = $projectVersion
}
if ($Version -ne $projectVersion) {
    throw "Requested version $Version does not match pyproject.toml version $projectVersion"
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:[A-Za-z0-9._-]+)?$') {
    throw "Unsupported release version: $Version"
}

$expectedWheel = "workbuddy_qmt_bridge-$Version-py3-none-any.whl"
$zipName = "workbuddy-qmt-bridge-$Version.zip"
$releaseNotesName = "RELEASE-v$Version.md"
$releaseNotesPath = Join-Path $repoRoot "docs\$releaseNotesName"
$installerName = "安装、升级或修复.cmd"
$legacyInstallerName = "首次安装与配置.cmd"
$setupName = "setup.cmd"
$installerPath = Join-Path $repoRoot $installerName
$legacyInstallerPath = Join-Path $repoRoot $legacyInstallerName
$setupPath = Join-Path $repoRoot $setupName
$quickStartPath = Join-Path $repoRoot "docs\QUICKSTART.zh-CN.md"

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot "dist"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)

if (-not (Test-Path -LiteralPath $initPath -PathType Leaf)) {
    throw "Package version file not found: $initPath"
}
if (-not (Test-Path -LiteralPath $releaseNotesPath -PathType Leaf)) {
    throw "Release notes not found: $releaseNotesPath"
}
foreach ($requiredSource in @($installerPath, $legacyInstallerPath, $setupPath, $quickStartPath)) {
    if (-not (Test-Path -LiteralPath $requiredSource -PathType Leaf)) {
        throw "Release source file not found: $requiredSource"
    }
}

$initText = Get-Content -LiteralPath $initPath -Raw
if ($initText -notmatch ('__version__\s*=\s*"' + [regex]::Escape($Version) + '"')) {
    throw "Requested version $Version does not match src\workbuddy_qmt\__init__.py"
}

New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null

$releaseOutputs = @(
    "workbuddy_qmt_bridge-*-py3-none-any.whl",
    "workbuddy-qmt-bridge-*.zip",
    "workbuddy-qmt-bridge-*.zip.sha256",
    "SHA256SUMS.txt"
)
Get-ChildItem -LiteralPath $OutputDirectory -File | Where-Object {
    $name = $_.Name
    @($releaseOutputs | Where-Object { $name -like $_ }).Count -gt 0
} | Remove-Item -Force

$expectedOutputs = @(
    $expectedWheel,
    $zipName,
    "$zipName.sha256",
    "SHA256SUMS.txt"
)

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
    Copy-Item -LiteralPath $installerPath -Destination $stageFull
    Copy-Item -LiteralPath $legacyInstallerPath -Destination $stageFull
    Copy-Item -LiteralPath $setupPath -Destination $stageFull
    Copy-Item -LiteralPath (Join-Path $repoRoot "LICENSE") -Destination $stageFull
    Copy-Item -LiteralPath $releaseNotesPath -Destination $stageFull
    Copy-Item -LiteralPath (Join-Path $repoRoot "docs\README-RELEASE.zh-CN.md") -Destination (Join-Path $stageFull "README-RELEASE.zh-CN.md")
    Copy-Item -LiteralPath (Join-Path $repoRoot "docs\P1-VALIDATION.zh-CN.md") -Destination (Join-Path $stageFull "P1-VALIDATION.zh-CN.md")
    Copy-Item -LiteralPath $quickStartPath -Destination (Join-Path $stageFull "快速开始.md")
    $taskDocs = Join-Path $stageFull "docs"
    New-Item -ItemType Directory -Path $taskDocs | Out-Null
    foreach ($taskDoc in @(
        "DAILY-USE.zh-CN.md",
        "UPGRADE-RECOVERY.zh-CN.md",
        "P0-P1-ADVANCED.zh-CN.md",
        "TROUBLESHOOTING.zh-CN.md",
        "COMPATIBILITY.zh-CN.md"
    )) {
        Copy-Item -LiteralPath (Join-Path $repoRoot "docs\$taskDoc") -Destination $taskDocs
    }
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

    $requiredEntries = @(
        $installerName,
        $legacyInstallerName,
        $setupName,
        "LICENSE",
        "快速开始.md",
        "README-RELEASE.zh-CN.md",
        "P1-VALIDATION.zh-CN.md",
        $releaseNotesName,
        "SHA256SUMS.txt",
        $expectedWheel,
        "examples/README.md",
        "docs/COMPATIBILITY.zh-CN.md"
    )
    $archive = [System.IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $archiveEntries = @($archive.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
        $missingEntries = @($requiredEntries | Where-Object { $_ -notin $archiveEntries })
        if ($missingEntries.Count -gt 0) {
            throw "Release ZIP is missing required entries: $($missingEntries -join ', ')"
        }
    }
    finally {
        $archive.Dispose()
    }

    $zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
    @(
        "$wheelHash  $expectedWheel"
        "$zipHash  $zipName"
    ) | Set-Content -LiteralPath (Join-Path $OutputDirectory "SHA256SUMS.txt") -Encoding ascii
    "$zipHash  $zipName" | Set-Content -LiteralPath (Join-Path $OutputDirectory "$zipName.sha256") -Encoding ascii

    Write-Host "Release build completed:"
    Get-ChildItem -LiteralPath $OutputDirectory -File |
        Where-Object { $_.Name -in $expectedOutputs } |
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
