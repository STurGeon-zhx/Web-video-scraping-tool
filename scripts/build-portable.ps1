param(
    [switch]$SkipChecks,
    [switch]$SkipDesktopBuild
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Split-Path -Parent $PSScriptRoot)).Path
$portableRoot = Join-Path $projectRoot "release\portable"
$desktopAppRoot = Join-Path $projectRoot "release\desktop\app"
$buildScript = Join-Path $PSScriptRoot "build-desktop-app.ps1"
$instructionsSource = Join-Path $projectRoot "docs\portable-usage.txt"
$date = Get-Date -Format "yyyy-MM-dd"

function Assert-ProjectChild {
    param([Parameter(Mandatory = $true)][string]$Path)
    $fullPath = [IO.Path]::GetFullPath($Path)
    if (-not $fullPath.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the project: $fullPath"
    }
}

Push-Location $projectRoot
try {
    if (-not $SkipDesktopBuild) {
        if ($SkipChecks) {
            & $buildScript -SkipChecks
        }
        else {
            & $buildScript
        }
        if ($LASTEXITCODE -ne 0) {
            throw "Desktop build failed with exit code $LASTEXITCODE"
        }
    }
    $desktopExe = Get-ChildItem -LiteralPath $desktopAppRoot -Directory |
        ForEach-Object { Get-ChildItem -LiteralPath $_.FullName -File -Filter "*.exe" } |
        Select-Object -First 1
    if (-not $desktopExe) {
        throw "Desktop build output was not found under: $desktopAppRoot"
    }
    $sourceRoot = $desktopExe.Directory.FullName
    $appName = $desktopExe.Directory.Name
    $packageRoot = Join-Path $portableRoot $appName
    $zipPath = Join-Path $portableRoot "$appName-Portable-$date-Windows-x64.zip"
    $checksumPath = Join-Path $portableRoot "$appName-Portable-$date-Windows-x64.sha256.txt"

    New-Item -ItemType Directory -Force -Path $portableRoot | Out-Null
    Assert-ProjectChild $packageRoot
    if (Test-Path -LiteralPath $packageRoot) {
        Remove-Item -LiteralPath $packageRoot -Recurse -Force
    }
    Copy-Item -LiteralPath $sourceRoot -Destination $packageRoot -Recurse

    Get-ChildItem -LiteralPath $packageRoot -Recurse -File -Filter "debug.log" |
        Remove-Item -Force

    if (-not (Test-Path -LiteralPath $instructionsSource)) {
        throw "Portable usage instructions were not found: $instructionsSource"
    }
    Copy-Item -LiteralPath $instructionsSource -Destination (Join-Path $packageRoot "README.txt")

    $commit = (& git rev-parse --short HEAD).Trim()
    $dirty = & git status --porcelain
    if ($dirty) {
        $commit = "$commit+working-tree"
    }
    [IO.File]::AppendAllText(
        (Join-Path $packageRoot "README.txt"),
        "`r`nBuild date: $date`r`nSource revision: $commit`r`n",
        [Text.UTF8Encoding]::new($false)
    )

    foreach ($required in @(
        $desktopExe.Name,
        "_internal\runtime\deno\deno.exe",
        "_internal\yt_dlp_ejs",
        "_internal\imageio_ffmpeg"
    )) {
        if (-not (Test-Path -LiteralPath (Join-Path $packageRoot $required))) {
            throw "Portable package is missing: $required"
        }
    }

    foreach ($target in @($zipPath, $checksumPath)) {
        Assert-ProjectChild $target
        if (Test-Path -LiteralPath $target) {
            Remove-Item -LiteralPath $target -Force
        }
    }
    Compress-Archive -LiteralPath $packageRoot -DestinationPath $zipPath -CompressionLevel Optimal

    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [IO.Compression.ZipFile]::OpenRead($zipPath)
    try {
        $names = @($archive.Entries | ForEach-Object FullName)
        $forbidden = @($names | Where-Object {
            $_ -match '(^|[\\/])(tasks\.db|runtime\.json|youtube-cookies\.txt|application.*\.log|debug\.log)$'
        })
        if ($forbidden) {
            throw "Portable ZIP contains local runtime data: $($forbidden -join ', ')"
        }
    }
    finally {
        $archive.Dispose()
    }

    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash
    $checksumLine = "$hash *$([IO.Path]::GetFileName($zipPath))`r`n"
    [IO.File]::WriteAllText($checksumPath, $checksumLine, [Text.UTF8Encoding]::new($false))
    Write-Host "Portable ZIP: $zipPath"
    Write-Host "SHA-256: $hash"
}
finally {
    Pop-Location
}
