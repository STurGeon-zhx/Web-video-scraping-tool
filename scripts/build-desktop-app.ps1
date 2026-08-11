param(
    [switch]$SkipChecks
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$desktopRoot = Join-Path $projectRoot "release\desktop"
$appRoot = Join-Path $desktopRoot "app"
$workRoot = Join-Path $desktopRoot "build"

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)][string]$Program,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    & $Program @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Program failed with exit code $LASTEXITCODE"
    }
}

Push-Location $projectRoot
try {
    if (-not $SkipChecks) {
        Invoke-Checked npm @("--prefix", "frontend", "ci")
        Invoke-Checked npm @("--prefix", "frontend", "test", "--", "--run")
        Invoke-Checked npm @("--prefix", "frontend", "run", "build")
        Invoke-Checked python @("-m", "pytest", "-q")
    }

    Invoke-Checked python @(
        "-m", "pip", "install",
        "--timeout", "300",
        "setuptools>=75", "wheel"
    )
    Invoke-Checked python @(
        "-m", "pip", "install",
        "--timeout", "300",
        "--no-build-isolation",
        "-e", ".[download,desktop,build]"
    )
    Invoke-Checked python @(
        "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--distpath", $appRoot,
        "--workpath", $workRoot,
        "douyin_downloader_desktop.spec"
    )

    $desktopExe = Get-ChildItem -LiteralPath $appRoot -Directory |
        ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -File -Filter "*.exe" } |
        Select-Object -First 1
    if (-not $desktopExe) {
        throw "Desktop EXE was not generated under $appRoot"
    }
    $desktopDirectory = $desktopExe.DirectoryName
    $qtWebEngine = Get-ChildItem -LiteralPath $desktopDirectory -Recurse -Filter "QtWebEngineProcess.exe" -File
    if (-not $qtWebEngine) {
        throw "QtWebEngineProcess.exe was not bundled"
    }
    $ffmpeg = Get-ChildItem -LiteralPath $desktopDirectory -Recurse -Filter "ffmpeg*.exe" -File
    if (-not $ffmpeg) {
        throw "FFmpeg was not bundled"
    }

    Write-Host "Desktop application built: $($desktopExe.FullName)"
}
finally {
    Pop-Location
}
