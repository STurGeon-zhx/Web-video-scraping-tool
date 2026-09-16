param(
    [string]$ZipPath
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
if (-not $ZipPath) {
    $ZipPath = Get-ChildItem -LiteralPath (Join-Path $projectRoot "release\portable") -Filter "*.zip" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $ZipPath -or -not (Test-Path -LiteralPath $ZipPath)) {
    throw "Portable ZIP was not found"
}

$runId = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$testRoot = Join-Path $projectRoot "release\portable-smoke\$runId"
$extractRoot = Join-Path $testRoot "package"
$dataRoot = Join-Path $testRoot "data"
New-Item -ItemType Directory -Force -Path $extractRoot, $dataRoot | Out-Null
Expand-Archive -LiteralPath $ZipPath -DestinationPath $extractRoot

$appExe = Get-ChildItem -LiteralPath $extractRoot -Directory |
    ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -File -Filter "*.exe" } |
    Select-Object -First 1
if (-not $appExe) {
    throw "Portable EXE was not found after extraction"
}

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class PortableSmokeWindow {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
}
"@

$oldDataDir = $env:DOUYIN_DOWNLOADER_DATA_DIR
$process = $null
try {
    $env:DOUYIN_DOWNLOADER_DATA_DIR = $dataRoot
    $process = Start-Process -FilePath $appExe.FullName -PassThru
    $runtimeFile = Join-Path $dataRoot "runtime.json"
    $deadline = (Get-Date).AddSeconds(60)
    $runtime = $null
    while ((Get-Date) -lt $deadline) {
        if ($process.HasExited) {
            throw "Portable application exited during startup"
        }
        $process.Refresh()
        if ($process.MainWindowHandle -ne 0 -and (Test-Path -LiteralPath $runtimeFile)) {
            try {
                $runtime = Get-Content -LiteralPath $runtimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
                $settings = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/settings" -f $runtime.port) -TimeoutSec 2
                $auth = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/settings/youtube-auth" -f $runtime.port) -TimeoutSec 2
                if ($settings.download_directory -and $auth.status) {
                    break
                }
            }
            catch {
            }
        }
        Start-Sleep -Milliseconds 200
    }
    if (-not $runtime) {
        throw "Portable application did not become ready"
    }
    if ($auth.has_saved_state) {
        throw "Fresh portable smoke data unexpectedly contained YouTube login state"
    }

    [PortableSmokeWindow]::PostMessage(
        $process.MainWindowHandle,
        0x0010,
        [IntPtr]::Zero,
        [IntPtr]::Zero
    ) | Out-Null
    if (-not $process.WaitForExit(15000)) {
        throw "Portable application did not exit after WM_CLOSE"
    }
    $process = $null

    [pscustomobject]@{
        status = "ok"
        zip = (Resolve-Path $ZipPath).Path
        isolated_data = $dataRoot
        api_ready = $true
        youtube_auth_clean = $true
        graceful_exit = $true
    } | ConvertTo-Json -Compress
}
finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force
    }
    if ($null -eq $oldDataDir) {
        Remove-Item Env:DOUYIN_DOWNLOADER_DATA_DIR -ErrorAction SilentlyContinue
    }
    else {
        $env:DOUYIN_DOWNLOADER_DATA_DIR = $oldDataDir
    }
}
