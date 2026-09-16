from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, get_package_paths


datas = [
    ("frontend/dist", "frontend"),
    (".runtime/deno/deno.exe", "runtime/deno"),
]
binaries = []
hiddenimports = collect_submodules("yt_dlp")

for package in ("imageio_ffmpeg", "playwright", "certifi", "curl_cffi", "yt_dlp_ejs"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

a = Analysis(
    ["run_desktop.py"],
    pathex=["backend"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "vitest"],
    noarchive=False,
    optimize=1,
)

# Codex's host PATH exposes Poppler and media-runtime DLLs that PyInstaller can
# mistake for application dependencies. They are not project files and can
# override Windows/Qt libraries in the frozen application's child processes.
a.binaries = [
    entry
    for entry in a.binaries
    if "codex-runtimes" not in str(entry[1]).lower()
]

# Keep the Visual C++ runtime beside the executable at the version shipped with
# the active PySide6 wheel. An older Python runtime copy can otherwise win DLL
# resolution and break newer Qt builds on machines without a newer redist.
_, pyside6_path = get_package_paths("PySide6")
qt_runtime_names = {
    "msvcp140.dll",
    "msvcp140_1.dll",
    "msvcp140_2.dll",
    "vcruntime140.dll",
    "vcruntime140_1.dll",
}
a.binaries = [entry for entry in a.binaries if entry[0].lower() not in qt_runtime_names]
for runtime_name in sorted(qt_runtime_names):
    runtime_path = Path(pyside6_path, runtime_name)
    if runtime_path.is_file():
        a.binaries.append((runtime_name, str(runtime_path), "BINARY"))
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="视频批量下载工具",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="视频批量下载工具",
)
