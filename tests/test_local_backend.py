from pathlib import Path
from urllib.parse import urlsplit

import httpx

from douyin_downloader.local_backend import LocalBackend


def make_backend(tmp_path: Path) -> LocalBackend:
    return LocalBackend(
        data_dir=tmp_path / "data",
        default_download_dir=tmp_path / "downloads",
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
    )


def test_local_backend_serves_settings_and_stops(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)

    base_url = backend.start(timeout=10)
    response = httpx.get(f"{base_url}/api/settings", timeout=5)
    backend.stop(timeout=10)

    assert response.status_code == 200
    assert backend.is_running is False
    assert not (tmp_path / "data" / "runtime.json").exists()


def test_local_backend_only_binds_loopback(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)

    try:
        base_url = backend.start(timeout=10)
    finally:
        backend.stop(timeout=10)

    assert urlsplit(base_url).hostname == "127.0.0.1"


def test_local_backend_start_and_stop_are_idempotent(tmp_path: Path) -> None:
    backend = make_backend(tmp_path)

    first_url = backend.start(timeout=10)
    second_url = backend.start(timeout=10)
    backend.stop(timeout=10)
    backend.stop(timeout=10)

    assert second_url == first_url
