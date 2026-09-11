[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ReleaseDirectory,
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-NativeChecked {
    param([string]$FilePath, [string[]]$Arguments, [string]$Failure)
    & $FilePath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Failure (exit $LASTEXITCODE)"
    }
}

$releaseFull = [System.IO.Path]::GetFullPath($ReleaseDirectory)
$zips = @(Get-ChildItem -LiteralPath $releaseFull -Filter "workbuddy-qmt-bridge-*.zip" -File)
if ($zips.Count -ne 1) {
    throw "Expected exactly one release ZIP in $releaseFull; found $($zips.Count)"
}

$tempRoot = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath()).TrimEnd("\", "/")
$smokeRoot = Join-Path $tempRoot ("workbuddy-qmt-smoke-" + [guid]::NewGuid().ToString("N"))
$smokeFull = [System.IO.Path]::GetFullPath($smokeRoot)
$safePrefix = $tempRoot + [System.IO.Path]::DirectorySeparatorChar
if (-not $smokeFull.StartsWith($safePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Unsafe smoke-test path: $smokeFull"
}

$previousPythonPath = $env:PYTHONPATH
try {
    $extract = Join-Path $smokeFull "release"
    $site = Join-Path $smokeFull "site"
    $runtime = Join-Path $smokeFull "runtime-用户-🚀"
    $mcp = Join-Path $smokeFull "mcp.json"
    $launcher = Join-Path $smokeFull "launcher.json"
    $support = Join-Path $smokeFull "support.zip"
    New-Item -ItemType Directory -Path $extract -Force | Out-Null
    Expand-Archive -LiteralPath $zips[0].FullName -DestinationPath $extract

    Push-Location $extract
    try {
        Invoke-NativeChecked cmd.exe @("/d", "/c", '"安装、升级或修复.cmd" --verify-only') "Canonical installer hash verification failed"
        Invoke-NativeChecked cmd.exe @("/d", "/c", '"首次安装与配置.cmd" --verify-only') "Legacy installer compatibility failed"
        $wheels = @(Get-ChildItem -LiteralPath $extract -Filter "*.whl" -File)
        if ($wheels.Count -ne 1) { throw "Expected one wheel in the release ZIP" }
        Invoke-NativeChecked $Python @("-m", "pip", "install", "--no-deps", "--target", $site, $wheels[0].FullName) "Isolated wheel install failed"
        $env:PYTHONPATH = $site
        [System.IO.File]::WriteAllText($mcp, '{"mcpServers":{}}', [System.Text.UTF8Encoding]::new($false))

        $setupArgs = @(
            "-m", "workbuddy_qmt.manager", "--launcher-config", $launcher,
            "setup", "--root", $runtime, "--mcp-config", $mcp,
            "--accounts", "STOCK", "--stock-account-id", "ci-stock-001",
            "--non-interactive", "--no-shortcuts", "--no-open"
        )
        Invoke-NativeChecked $Python $setupArgs "Fresh setup from Release ZIP failed"
        $profile = Join-Path $runtime "qmt_ready\main_stock\qmt_profile.json"
        $profileBefore = (Get-FileHash -LiteralPath $profile -Algorithm SHA256).Hash
        $repeatArgs = @(
            "-m", "workbuddy_qmt.manager", "--launcher-config", $launcher,
            "setup", "--root", $runtime, "--mcp-config", $mcp,
            "--stock-account-id", "ci-stock-001", "--non-interactive",
            "--no-shortcuts", "--no-open", "--force"
        )
        Invoke-NativeChecked $Python $repeatArgs "Repeat upgrade setup failed"
        $profileAfter = (Get-FileHash -LiteralPath $profile -Algorithm SHA256).Hash
        if ($profileBefore -ne $profileAfter) { throw "Repeat setup changed the Profile" }

        $config = Join-Path $runtime "config\bridge.json"
        Invoke-NativeChecked $Python @(
            "-m", "workbuddy_qmt.manager", "--launcher-config", $launcher,
            "doctor", "--config", $config
        ) "Release doctor check failed"
        Invoke-NativeChecked $Python @(
            "-m", "workbuddy_qmt.manager", "--launcher-config", $launcher,
            "account", "--config", $config, "enable", "main_credit",
            "--qmt-account-id", "ci-credit-001"
        ) "Existing-runtime account enable failed"
        Invoke-NativeChecked $Python @(
            "-m", "workbuddy_qmt.manager", "--launcher-config", $launcher,
            "support-bundle", "--config", $config, "--redact", "--output", $support
        ) "Redacted support bundle failed"

        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $archive = [System.IO.Compression.ZipFile]::OpenRead($support)
        try {
            $entryNames = @($archive.Entries | ForEach-Object { $_.FullName })
            if ($entryNames -contains "qmt_profile.json") { throw "Support bundle contains a raw Profile" }
            $text = ""
            foreach ($entry in $archive.Entries) {
                $reader = [System.IO.StreamReader]::new($entry.Open(), [System.Text.Encoding]::UTF8)
                try { $text += $reader.ReadToEnd() } finally { $reader.Dispose() }
            }
            if ($text.Contains("ci-stock-001") -or $text.Contains("ci-credit-001")) {
                throw "Support bundle leaked a QMT account ID"
            }
        }
        finally {
            $archive.Dispose()
        }
    }
    finally {
        Pop-Location
    }
    Write-Host "RELEASE_SMOKE_OK"
}
finally {
    $env:PYTHONPATH = $previousPythonPath
    if (Test-Path -LiteralPath $smokeFull -PathType Container) {
        $resolved = [System.IO.Path]::GetFullPath($smokeFull)
        if (-not $resolved.StartsWith($safePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "Refusing to remove unsafe smoke-test path: $resolved"
        }
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}
