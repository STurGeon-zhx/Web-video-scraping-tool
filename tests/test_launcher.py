import socket
import sys
from pathlib import Path

from fastapi import FastAPI

from douyin_downloader.launcher import (
    application_data_directory,
    create_server_config,
    create_downloader,
    default_download_directory,
    find_frontend_directory,
    select_available_port,
    should_open_browser,
)


def test_select_available_port_is_bindable() -> None:
    port = select_available_port()

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", port))


def test_finds_built_frontend_assets() -> None:
    frontend = find_frontend_directory()

    assert frontend.name == "dist"
    assert (frontend / "index.html").exists()


def test_default_download_directory_uses_windows_videos_folder(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    result = default_download_directory()

    assert result == tmp_path / "Videos" / "抖音批量下载"


def test_automation_can_override_data_directory_and_browser(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DOUYIN_DOWNLOADER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DOUYIN_DOWNLOADER_NO_BROWSER", "1")

    assert application_data_directory() == tmp_path
    assert should_open_browser() is False


def test_server_config_works_when_windowed_exe_has_no_stderr(monkeypatch) -> None:
    monkeypatch.setattr(sys, "stderr", None)

    config = create_server_config(FastAPI(), 8765)

    assert config.host == "127.0.0.1"
    assert config.port == 8765
    assert config.log_config is None
    assert config.timeout_graceful_shutdown == 5


def test_create_downloader_reuses_anonymous_cookie_file(monkeypatch, tmp_path: Path) -> None:
    captured: dict = {}

    class FakeDownloader:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    monkeypatch.setattr("douyin_downloader.launcher.YtDlpDownloader", FakeDownloader)
    monkeypatch.setattr("douyin_downloader.launcher.find_ffmpeg", lambda: None)

    downloader = create_downloader(tmp_path)

    assert isinstance(downloader, FakeDownloader)
    assert captured["cookie_file"] == tmp_path / "browser" / "anonymous-cookies.txt"
    assert captured["cookie_provider"].cookie_file == captured["cookie_file"]
