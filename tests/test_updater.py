from __future__ import annotations

import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

import httpx
import pytest

from douyin_downloader.updater import AppUpdater, UpdateError, parse_release


def make_archive(executable_name: str, *, unsafe: bool = False) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(
            "../outside.txt" if unsafe else f"VideoTool/{executable_name}",
            b"new-executable",
        )
        archive.writestr("VideoTool/_internal/runtime.txt", b"runtime")
    return stream.getvalue()


def release_payload(archive: bytes, version: str = "1.3.0") -> dict:
    archive_name = f"Web-video-scraping-tool-portable-v{version}.zip"
    return {
        "tag_name": f"v{version}",
        "name": f"Version {version}",
        "body": "Release notes",
        "html_url": f"https://github.com/example/repo/releases/tag/v{version}",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": archive_name,
                "browser_download_url": f"https://github.com/example/repo/releases/download/v{version}/{archive_name}",
                "size": len(archive),
            },
            {
                "name": f"{archive_name}.sha256",
                "browser_download_url": f"https://github.com/example/repo/releases/download/v{version}/{archive_name}.sha256",
                "size": 111,
            },
        ],
    }


def updater_with_transport(
    tmp_path: Path,
    archive: bytes,
    *,
    checksum: str | None = None,
    process_launcher=None,
) -> AppUpdater:
    executable = tmp_path / "installed" / "VideoTool.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"old-executable")
    (executable.parent / "README.txt").write_text("portable", encoding="utf-8")
    payload = release_payload(archive)
    archive_name = payload["assets"][0]["name"]
    expected = checksum or hashlib.sha256(archive).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/releases/latest"):
            return httpx.Response(200, json=payload)
        if request.url.path.endswith(".sha256"):
            return httpx.Response(200, text=f"{expected} *{archive_name}\n")
        if request.url.path.endswith(".zip"):
            return httpx.Response(200, content=archive)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    return AppUpdater(
        tmp_path / "updates",
        current_version="1.2.0",
        executable_path=executable,
        frozen=True,
        client_factory=lambda: httpx.Client(transport=transport),
        process_launcher=process_launcher,
    )


def test_parse_release_requires_trusted_github_assets() -> None:
    archive = make_archive("VideoTool.exe")
    payload = release_payload(archive)
    payload["assets"][0]["browser_download_url"] = "https://example.com/update.zip"

    with pytest.raises(UpdateError, match="受信任"):
        parse_release(payload)


def test_check_reports_newer_release_without_downloading(tmp_path: Path) -> None:
    updater = updater_with_transport(tmp_path, make_archive("VideoTool.exe"))

    result = updater.check()

    assert result["current_version"] == "1.2.0"
    assert result["latest_version"] == "1.3.0"
    assert result["update_available"] is True
    assert result["can_self_update"] is (os.name == "nt")


@pytest.mark.skipif(os.name != "nt", reason="桌面自动更新仅支持 Windows")
def test_prepare_verifies_and_extracts_portable_release(tmp_path: Path) -> None:
    archive = make_archive("VideoTool.exe")
    updater = updater_with_transport(tmp_path, archive)

    result = updater.prepare()

    assert result["prepared"] is True
    assert result["prepared_version"] == "1.3.0"
    manifest = json.loads((tmp_path / "updates" / "prepared-update.json").read_text(encoding="utf-8"))
    assert Path(manifest["source_root"], "VideoTool.exe").read_bytes() == b"new-executable"
    assert manifest["sha256"] == hashlib.sha256(archive).hexdigest().upper()


@pytest.mark.skipif(os.name != "nt", reason="桌面自动更新仅支持 Windows")
def test_prepare_rejects_checksum_mismatch(tmp_path: Path) -> None:
    updater = updater_with_transport(
        tmp_path,
        make_archive("VideoTool.exe"),
        checksum="0" * 64,
    )

    with pytest.raises(UpdateError, match="SHA-256"):
        updater.prepare()

    assert not (tmp_path / "updates" / "prepared-update.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="桌面自动更新仅支持 Windows")
def test_prepare_rejects_zip_path_traversal(tmp_path: Path) -> None:
    updater = updater_with_transport(
        tmp_path,
        make_archive("VideoTool.exe", unsafe=True),
    )

    with pytest.raises(UpdateError, match="不安全"):
        updater.prepare()

    assert not (tmp_path / "outside.txt").exists()


@pytest.mark.skipif(os.name != "nt", reason="桌面自动更新仅支持 Windows")
def test_apply_launches_external_updater_with_exact_paths(tmp_path: Path) -> None:
    launched: list[list[str]] = []
    updater = updater_with_transport(
        tmp_path,
        make_archive("VideoTool.exe"),
        process_launcher=lambda command: launched.append(command),
    )
    updater.prepare()

    result = updater.apply()

    assert result["applying"] is True
    assert len(launched) == 1
    command = launched[0]
    assert command[0] == "powershell.exe"
    assert "-TargetProcessId" in command
    assert "-Destination" in command
    assert str(tmp_path / "installed") in command
    script = (tmp_path / "updates" / "apply-update.ps1").read_text(encoding="ascii")
    assert "Copy-Item -Destination $Destination" in script
    assert "Start-Process -FilePath" in script


def test_source_environment_cannot_prepare_self_update(tmp_path: Path) -> None:
    executable = tmp_path / "python.exe"
    executable.write_bytes(b"python")
    updater = AppUpdater(
        tmp_path / "updates",
        current_version="1.2.0",
        executable_path=executable,
        frozen=False,
    )

    with pytest.raises(UpdateError, match="不支持自动覆盖"):
        updater.prepare()
