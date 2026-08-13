$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location $projectRoot
try {
    npm --prefix frontend ci
    npm --prefix frontend run build
    python -m pip install -e ".[download,build]"
    python -m PyInstaller --noconfirm --clean douyin_downloader.spec
    Write-Output "构建完成: $projectRoot\dist\视频批量下载工具.exe"
}
finally {
    Pop-Location
}
