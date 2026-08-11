param(
    [switch]$SkipAppBuild
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot
$installerScript = Join-Path $projectRoot "installer\douyin-downloader.iss"
$outputDirectory = Join-Path $projectRoot "release\installer"

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
    if (-not $SkipAppBuild) {
        & (Join-Path $PSScriptRoot "build-desktop-app.ps1")
        if ($LASTEXITCODE -ne 0) {
            throw "Desktop application build failed"
        }
    }

    $isccCandidates = @(
        (Get-Command "ISCC.exe" -ErrorAction SilentlyContinue).Source,
        (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    )
    $isccPath = $isccCandidates |
        Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
        Select-Object -First 1
    if (-not $isccPath) {
        throw "Inno Setup 6 ISCC.exe was not found"
    }

    New-Item -ItemType Directory -Force -Path $outputDirectory | Out-Null
    Invoke-Checked $isccPath @("/Qp", $installerScript)

    $setupExe = Get-ChildItem -LiteralPath $outputDirectory -Filter "*.exe" -File |
        Sort-Object LastWriteTime -Descending |
        Select-Object -First 1
    if (-not $setupExe) {
        throw "Installer EXE was not generated"
    }
    $hash = Get-FileHash -LiteralPath $setupExe.FullName -Algorithm SHA256
    Write-Host "Installer built: $($setupExe.FullName)"
    Write-Host "SHA256: $($hash.Hash)"
}
finally {
    Pop-Location
}
