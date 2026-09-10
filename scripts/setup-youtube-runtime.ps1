[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$denoVersion = "2.9.6"
$assetName = "deno-x86_64-pc-windows-msvc.zip"
$releaseBase = "https://github.com/denoland/deno/releases/download/v$denoVersion"
$runtimeDirectory = Join-Path $PSScriptRoot "..\.runtime\deno"
$runtimeDirectory = [System.IO.Path]::GetFullPath($runtimeDirectory)
$archivePath = Join-Path $runtimeDirectory $assetName
$checksumPath = "$archivePath.sha256sum"
$denoPath = Join-Path $runtimeDirectory "deno.exe"

New-Item -ItemType Directory -Force -Path $runtimeDirectory | Out-Null
Write-Host "Downloading Deno $denoVersion for YouTube extraction..."
Invoke-WebRequest -Uri "$releaseBase/$assetName" -OutFile $archivePath
Invoke-WebRequest -Uri "$releaseBase/$assetName.sha256sum" -OutFile $checksumPath

$checksumText = Get-Content -LiteralPath $checksumPath -Raw
$hashMatch = [regex]::Match($checksumText, "(?i)\b[0-9a-f]{64}\b")
if (-not $hashMatch.Success) {
    throw "Official Deno checksum file did not contain a SHA-256 value."
}
$expectedHash = $hashMatch.Value.ToLowerInvariant()
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $archivePath).Hash.ToLowerInvariant()
if ($expectedHash -ne $actualHash) {
    Remove-Item -LiteralPath $archivePath -Force
    throw "Deno archive SHA-256 verification failed."
}

Expand-Archive -LiteralPath $archivePath -DestinationPath $runtimeDirectory -Force
Remove-Item -LiteralPath $archivePath -Force
Remove-Item -LiteralPath $checksumPath -Force
if (-not (Test-Path -LiteralPath $denoPath -PathType Leaf)) {
    throw "Deno extraction finished but deno.exe was not found."
}

& $denoPath --version
Write-Host "YouTube Deno runtime installed: $denoPath"
