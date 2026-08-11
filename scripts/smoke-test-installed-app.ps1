$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$setupExe = Get-ChildItem -LiteralPath (Join-Path $projectRoot "release\installer") -Filter "*.exe" -File |
    Sort-Object LastWriteTime -Descending |
    Select-Object -First 1
if (-not $setupExe) {
    throw "Installer EXE was not found"
}

$runId = Get-Date -Format "yyyyMMdd-HHmmss-fff"
$testRoot = Join-Path $projectRoot "release\installed-smoke\$runId"
$installDir = Join-Path $testRoot "app"
$dataDir = Join-Path $testRoot "data"
$installLog = Join-Path $testRoot "install.log"
$uninstallLog = Join-Path $testRoot "uninstall.log"
New-Item -ItemType Directory -Force -Path $testRoot, $dataDir | Out-Null

Add-Type @"
using System;
using System.Runtime.InteropServices;
public static class SmokeWindow {
    [DllImport("user32.dll", SetLastError = true)]
    public static extern bool PostMessage(IntPtr hWnd, uint msg, IntPtr wParam, IntPtr lParam);
}
"@

function Wait-ForApplication {
    param(
        [Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory = $true)][string]$RuntimeFile
    )
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) {
            throw "Installed application exited during startup"
        }
        $Process.Refresh()
        if ($Process.MainWindowHandle -ne 0 -and (Test-Path -LiteralPath $RuntimeFile)) {
            try {
                $runtime = Get-Content -LiteralPath $RuntimeFile -Raw -Encoding UTF8 | ConvertFrom-Json
                $settings = Invoke-RestMethod -Uri ("http://127.0.0.1:{0}/api/settings" -f $runtime.port) -TimeoutSec 2
                if ($settings.download_directory) {
                    return $runtime
                }
            }
            catch {
            }
        }
        Start-Sleep -Milliseconds 200
    }
    throw "Installed application did not become ready"
}

function Close-Application {
    param([Parameter(Mandatory = $true)][System.Diagnostics.Process]$Process)
    $Process.Refresh()
    if ($Process.MainWindowHandle -eq 0) {
        throw "Application main window handle is missing"
    }
    [SmokeWindow]::PostMessage($Process.MainWindowHandle, 0x0010, [IntPtr]::Zero, [IntPtr]::Zero) | Out-Null
    if (-not $Process.WaitForExit(10000)) {
        throw "Application did not exit within 10 seconds after WM_CLOSE"
    }
}

$mainProcess = $null
$installed = $false
$uninstalled = $false
try {
    $installer = Start-Process -FilePath $setupExe.FullName -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/DIR=$installDir",
        "/LOG=$installLog"
    ) -PassThru -Wait
    if ($installer.ExitCode -ne 0) {
        throw "Silent installation failed with exit code $($installer.ExitCode)"
    }
    $installed = $true

    $appExe = Get-ChildItem -LiteralPath $installDir -Filter "*.exe" -File |
        Where-Object { $_.Name -notlike "unins*.exe" } |
        Select-Object -First 1
    if (-not $appExe) {
        throw "Installed application EXE was not found"
    }

    $browserNames = @("chrome", "msedge", "firefox", "brave")
    $browserIdsBefore = @(Get-Process -Name $browserNames -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id)
    $env:DOUYIN_DOWNLOADER_DATA_DIR = $dataDir
    $runtimeFile = Join-Path $dataDir "runtime.json"
    $mainProcess = Start-Process -FilePath $appExe.FullName -PassThru
    $runtime = Wait-ForApplication -Process $mainProcess -RuntimeFile $runtimeFile

    $rootResponse = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/" -f $runtime.port) -UseBasicParsing -TimeoutSec 10
    if ($rootResponse.StatusCode -ne 200) {
        throw "Installed application root returned HTTP $($rootResponse.StatusCode)"
    }
    $newBrowserProcesses = @(Get-Process -Name $browserNames -ErrorAction SilentlyContinue |
        Where-Object { $_.Id -notin $browserIdsBefore })
    if ($newBrowserProcesses) {
        throw "Application unexpectedly opened an external browser"
    }

    $secondProcess = Start-Process -FilePath $appExe.FullName -PassThru
    if (-not $secondProcess.WaitForExit(10000)) {
        Stop-Process -Id $secondProcess.Id -Force
        throw "Second application instance did not exit"
    }
    if ($mainProcess.HasExited) {
        throw "Primary application exited after second launch"
    }

    Close-Application -Process $mainProcess
    $mainProcess = $null
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (Test-Path -LiteralPath $runtimeFile)) {
        Start-Sleep -Milliseconds 100
    }
    if (Test-Path -LiteralPath $runtimeFile) {
        throw "runtime.json remained after graceful exit"
    }

    $mainProcess = Start-Process -FilePath $appExe.FullName -PassThru
    Wait-ForApplication -Process $mainProcess -RuntimeFile $runtimeFile | Out-Null
    Close-Application -Process $mainProcess
    $mainProcess = $null

    $uninstaller = Get-ChildItem -LiteralPath $installDir -Filter "unins*.exe" -File | Select-Object -First 1
    if (-not $uninstaller) {
        throw "Uninstaller was not found"
    }
    $uninstallProcess = Start-Process -FilePath $uninstaller.FullName -ArgumentList @(
        "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/LOG=$uninstallLog"
    ) -PassThru -Wait
    if ($uninstallProcess.ExitCode -ne 0) {
        throw "Silent uninstall failed with exit code $($uninstallProcess.ExitCode)"
    }
    $uninstalled = $true
    $deadline = (Get-Date).AddSeconds(20)
    while ((Get-Date) -lt $deadline -and (Test-Path -LiteralPath $installDir)) {
        Start-Sleep -Milliseconds 200
    }
    if (Test-Path -LiteralPath $installDir) {
        throw "Install directory remained after uninstall"
    }
    if (-not (Test-Path -LiteralPath $dataDir)) {
        throw "Application data was removed by uninstall"
    }

    [pscustomobject]@{
        status = "ok"
        installer = $setupExe.FullName
        test_root = $testRoot
        http_root = 200
        single_instance = $true
        graceful_exit = $true
        restart = $true
        uninstall_preserved_data = $true
    } | ConvertTo-Json -Compress
}
finally {
    if ($mainProcess -and -not $mainProcess.HasExited) {
        Stop-Process -Id $mainProcess.Id -Force
    }
    if ($installed -and -not $uninstalled -and (Test-Path -LiteralPath $installDir)) {
        $uninstaller = Get-ChildItem -LiteralPath $installDir -Filter "unins*.exe" -File -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($uninstaller) {
            Start-Process -FilePath $uninstaller.FullName -ArgumentList @(
                "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"
            ) -Wait | Out-Null
        }
    }
}
