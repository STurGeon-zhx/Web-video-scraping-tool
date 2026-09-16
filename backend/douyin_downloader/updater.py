from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from packaging.version import InvalidVersion, Version

from . import __version__


GITHUB_REPOSITORY = "STurGeon-zhx/Web-video-scraping-tool"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
ALLOWED_DOWNLOAD_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
MAX_ARCHIVE_BYTES = 1_000_000_000
MAX_EXTRACTED_BYTES = 3_000_000_000
MAX_ARCHIVE_ENTRIES = 20_000


class UpdateError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseAsset:
    name: str
    url: str
    size: int


@dataclass(frozen=True, slots=True)
class ReleaseInfo:
    version: str
    tag: str
    title: str
    notes: str
    page_url: str
    archive: ReleaseAsset
    checksum: ReleaseAsset


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    version: str
    source_root: str
    destination_root: str
    executable_name: str
    archive_path: str
    sha256: str


ClientFactory = Callable[[], httpx.Client]
ProcessLauncher = Callable[[list[str]], Any]


def _default_client_factory() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(45.0, connect=15.0),
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"VideoBatchDownloader/{__version__}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )


def _default_process_launcher(command: list[str]) -> subprocess.Popen[bytes]:
    creation_flags = 0
    if os.name == "nt":
        creation_flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    return subprocess.Popen(
        command,
        close_fds=True,
        creationflags=creation_flags,
    )


def _normalized_version(raw: str) -> Version:
    try:
        return Version(raw.strip().lstrip("vV"))
    except InvalidVersion as exc:
        raise UpdateError(f"发布版本号无效：{raw}") from exc


def _validate_github_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
    except ValueError as exc:
        raise UpdateError("更新地址格式无效") from exc
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in ALLOWED_DOWNLOAD_HOSTS:
        raise UpdateError("更新地址不是受信任的 GitHub HTTPS 地址")
    if parsed.username or parsed.password or parsed.port not in {None, 443}:
        raise UpdateError("更新地址包含不允许的认证信息或端口")


def _asset(payload: dict[str, Any]) -> ReleaseAsset:
    name = str(payload.get("name") or "").strip()
    url = str(payload.get("browser_download_url") or "").strip()
    try:
        size = int(payload.get("size") or 0)
    except (TypeError, ValueError) as exc:
        raise UpdateError("GitHub 更新附件大小无效") from exc
    if not name or not url or size <= 0:
        raise UpdateError("GitHub 更新附件信息不完整")
    _validate_github_url(url)
    return ReleaseAsset(name=name, url=url, size=size)


def parse_release(payload: dict[str, Any]) -> ReleaseInfo:
    if payload.get("draft") or payload.get("prerelease"):
        raise UpdateError("GitHub 最新发布不是正式版本")
    tag = str(payload.get("tag_name") or "").strip()
    version = str(_normalized_version(tag))
    assets = [_asset(item) for item in payload.get("assets") or []]
    archives = [item for item in assets if item.name.lower().endswith(".zip")]
    if not archives:
        raise UpdateError("GitHub 最新发布缺少 Windows 便携 ZIP")
    archive = next(
        (item for item in archives if "portable" in item.name.lower()),
        archives[0],
    )
    checksum_names = {
        f"{archive.name}.sha256".lower(),
        f"{archive.name}.sha256.txt".lower(),
        f"{Path(archive.name).stem}.sha256.txt".lower(),
    }
    checksum = next(
        (item for item in assets if item.name.lower() in checksum_names),
        None,
    )
    if checksum is None:
        raise UpdateError("GitHub 最新发布缺少对应的 SHA-256 校验文件")
    page_url = str(payload.get("html_url") or "").strip()
    _validate_github_url(page_url)
    return ReleaseInfo(
        version=version,
        tag=tag,
        title=str(payload.get("name") or tag),
        notes=str(payload.get("body") or ""),
        page_url=page_url,
        archive=archive,
        checksum=checksum,
    )


class AppUpdater:
    def __init__(
        self,
        data_dir: Path,
        *,
        current_version: str = __version__,
        executable_path: Path | None = None,
        frozen: bool | None = None,
        client_factory: ClientFactory | None = None,
        process_launcher: ProcessLauncher | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.current_version = str(_normalized_version(current_version))
        self.executable_path = Path(executable_path or sys.executable).resolve()
        self.frozen = bool(getattr(sys, "frozen", False)) if frozen is None else frozen
        self._client_factory = client_factory or _default_client_factory
        self._process_launcher = process_launcher or _default_process_launcher
        self._lock = threading.Lock()
        self._release: ReleaseInfo | None = None
        self._applying = False
        self._prepared_file = self.data_dir / "prepared-update.json"

    @property
    def can_self_update(self) -> bool:
        return (
            self.frozen
            and os.name == "nt"
            and self.executable_path.is_file()
            and (self.executable_path.parent / "README.txt").is_file()
        )

    def status(self) -> dict[str, Any]:
        prepared = self._load_prepared(required=False)
        return {
            "current_version": self.current_version,
            "packaged": self.frozen,
            "can_self_update": self.can_self_update,
            "prepared_version": prepared.version if prepared else None,
        }

    def check(self) -> dict[str, Any]:
        with self._client_factory() as client:
            response = self._request(client, LATEST_RELEASE_API)
            try:
                payload = response.json()
            except ValueError as exc:
                raise UpdateError("GitHub 返回了无法识别的版本信息") from exc
            finally:
                response.close()
        if not isinstance(payload, dict):
            raise UpdateError("GitHub 返回了无法识别的版本信息")
        release = parse_release(payload)
        self._release = release
        update_available = _normalized_version(release.version) > _normalized_version(
            self.current_version
        )
        return {
            **self.status(),
            "latest_version": release.version,
            "update_available": update_available,
            "release_title": release.title,
            "release_notes": release.notes,
            "release_url": release.page_url,
            "download_size": release.archive.size,
            "message": (
                f"发现新版本 {release.version}"
                if update_available
                else "当前已经是最新版本"
            ),
        }

    def prepare(self) -> dict[str, Any]:
        if not self.can_self_update:
            raise UpdateError("当前运行方式不支持自动覆盖，请从 GitHub 发布页手动下载")
        with self._lock:
            check = self.check()
            if not check["update_available"] or self._release is None:
                return {**check, "prepared": False}
            release = self._release
            if release.archive.size > MAX_ARCHIVE_BYTES:
                raise UpdateError("更新包超过允许的最大大小")

            version_root = self.data_dir / f"v{release.version}"
            download_root = version_root / "download"
            extract_root = version_root / "extracted"
            if version_root.exists():
                shutil.rmtree(version_root)
            download_root.mkdir(parents=True, exist_ok=True)

            archive_path = download_root / release.archive.name
            with self._client_factory() as client:
                checksum_response = self._request(client, release.checksum.url)
                try:
                    if len(checksum_response.content) > 16_384:
                        raise UpdateError("SHA-256 校验文件过大")
                    checksum_text = checksum_response.text
                finally:
                    checksum_response.close()
                expected_hash = self._expected_hash(checksum_text, release.archive.name)
                actual_hash = self._download_archive(client, release.archive, archive_path)

            if actual_hash.lower() != expected_hash.lower():
                archive_path.unlink(missing_ok=True)
                raise UpdateError("更新包 SHA-256 校验失败，已取消更新")

            source_root = self._extract_archive(archive_path, extract_root)
            prepared = PreparedUpdate(
                version=release.version,
                source_root=str(source_root),
                destination_root=str(self.executable_path.parent),
                executable_name=self.executable_path.name,
                archive_path=str(archive_path),
                sha256=actual_hash.upper(),
            )
            self._write_prepared(prepared)
            return {
                **check,
                "prepared": True,
                "prepared_version": prepared.version,
                "message": f"版本 {prepared.version} 已下载并通过校验，可以重启更新",
            }

    def apply(self) -> dict[str, Any]:
        with self._lock:
            if self._applying:
                raise UpdateError("更新程序已经启动，请等待应用重启")
            result = self._apply_prepared()
            self._applying = True
            return result

    def _apply_prepared(self) -> dict[str, Any]:
        if not self.can_self_update:
            raise UpdateError("当前运行环境不支持自动更新")
        prepared = self._load_prepared(required=True)
        assert prepared is not None
        source_root = Path(prepared.source_root).resolve()
        destination_root = Path(prepared.destination_root).resolve()
        executable = source_root / prepared.executable_name
        if not source_root.is_dir() or not executable.is_file():
            raise UpdateError("已下载的更新文件不完整，请重新检查更新")
        if destination_root != self.executable_path.parent:
            raise UpdateError("更新目标目录与当前程序目录不一致")

        script_path = self.data_dir / "apply-update.ps1"
        log_path = self.data_dir / "update.log"
        script_path.write_text(self._powershell_script(), encoding="ascii")
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-WindowStyle",
            "Hidden",
            "-File",
            str(script_path),
            "-TargetProcessId",
            str(os.getpid()),
            "-Source",
            str(source_root),
            "-Destination",
            str(destination_root),
            "-ExecutableName",
            prepared.executable_name,
            "-LogPath",
            str(log_path),
            "-ManifestPath",
            str(self._prepared_file),
            "-CleanupRoot",
            str(source_root.parent.parent),
        ]
        self._process_launcher(command)
        return {
            "applying": True,
            "version": prepared.version,
            "message": "程序即将退出，更新完成后会自动重新启动",
        }

    def _request(
        self,
        client: httpx.Client,
        url: str,
        *,
        stream: bool = False,
    ) -> httpx.Response:
        current = url
        for _ in range(6):
            _validate_github_url(current)
            request = client.build_request("GET", current)
            response = client.send(request, stream=stream)
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                response.close()
                if not location:
                    raise UpdateError("GitHub 更新地址重定向不完整")
                current = urljoin(current, location)
                continue
            try:
                response.raise_for_status()
            except httpx.HTTPError as exc:
                response.close()
                raise UpdateError("无法连接 GitHub 检查更新") from exc
            return response
        raise UpdateError("GitHub 更新地址重定向次数过多")

    def _download_archive(
        self,
        client: httpx.Client,
        asset: ReleaseAsset,
        destination: Path,
    ) -> str:
        response = self._request(client, asset.url, stream=True)
        digest = hashlib.sha256()
        written = 0
        temporary = destination.with_suffix(destination.suffix + ".part")
        try:
            with temporary.open("wb") as output:
                for chunk in response.iter_bytes(1024 * 1024):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if written > MAX_ARCHIVE_BYTES:
                        raise UpdateError("更新包超过允许的最大大小")
                    digest.update(chunk)
                    output.write(chunk)
            if written != asset.size:
                raise UpdateError("更新包下载不完整，请重试")
            temporary.replace(destination)
            return digest.hexdigest()
        finally:
            response.close()
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _expected_hash(text: str, archive_name: str) -> str:
        for line in text.splitlines():
            match = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+?)\s*$", line)
            if match and Path(match.group(2)).name == archive_name:
                return match.group(1)
        raise UpdateError("SHA-256 校验文件未包含当前更新包")

    def _extract_archive(self, archive_path: Path, destination: Path) -> Path:
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive_path) as archive:
            members = archive.infolist()
            if len(members) > MAX_ARCHIVE_ENTRIES:
                raise UpdateError("更新包文件数量异常")
            total_size = 0
            for member in members:
                normalized = member.filename.replace("\\", "/")
                path = PurePosixPath(normalized)
                if path.is_absolute() or ".." in path.parts:
                    raise UpdateError("更新包包含不安全的文件路径")
                total_size += max(0, member.file_size)
                if total_size > MAX_EXTRACTED_BYTES:
                    raise UpdateError("更新包解压后大小异常")
            archive.extractall(destination)

        candidates = [
            path
            for path in destination.rglob(self.executable_path.name)
            if path.is_file() and (path.parent / "_internal").is_dir()
        ]
        if len(candidates) != 1:
            raise UpdateError("更新包未找到唯一的桌面程序")
        return candidates[0].parent.resolve()

    def _write_prepared(self, prepared: PreparedUpdate) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._prepared_file.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(asdict(prepared), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(self._prepared_file)

    def _load_prepared(self, *, required: bool) -> PreparedUpdate | None:
        try:
            payload = json.loads(self._prepared_file.read_text(encoding="utf-8"))
            prepared = PreparedUpdate(**payload)
        except (OSError, ValueError, TypeError, KeyError):
            if required:
                raise UpdateError("尚未下载可安装的更新")
            return None
        if _normalized_version(prepared.version) <= _normalized_version(self.current_version):
            self._prepared_file.unlink(missing_ok=True)
            if required:
                raise UpdateError("准备好的更新版本不高于当前版本")
            return None
        return prepared

    @staticmethod
    def _powershell_script() -> str:
        return r'''param(
    [int]$TargetProcessId,
    [string]$Source,
    [string]$Destination,
    [string]$ExecutableName,
    [string]$LogPath,
    [string]$ManifestPath,
    [string]$CleanupRoot
)
$ErrorActionPreference = "Stop"
try {
    $deadline = (Get-Date).AddSeconds(120)
    while ((Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $TargetProcessId -ErrorAction SilentlyContinue) {
        throw "Application did not exit before update timeout"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Source $ExecutableName))) {
        throw "Staged executable is missing"
    }
    $copyDeadline = (Get-Date).AddSeconds(30)
    while ($true) {
        try {
            Get-ChildItem -LiteralPath $Source -Force | Copy-Item -Destination $Destination -Recurse -Force
            break
        }
        catch {
            if ((Get-Date) -ge $copyDeadline) { throw }
            Start-Sleep -Seconds 1
        }
    }
    Remove-Item -LiteralPath $ManifestPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $CleanupRoot -Recurse -Force -ErrorAction SilentlyContinue
    Start-Process -FilePath (Join-Path $Destination $ExecutableName) -WorkingDirectory $Destination
    [IO.File]::WriteAllText($LogPath, "Update completed successfully`r`n", [Text.Encoding]::UTF8)
}
catch {
    [IO.File]::WriteAllText($LogPath, ("Update failed: " + $_.Exception.Message + "`r`n"), [Text.Encoding]::UTF8)
    exit 1
}
'''
