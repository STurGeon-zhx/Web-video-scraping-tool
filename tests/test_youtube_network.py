from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

import pytest

from douyin_downloader.store import Database
from douyin_downloader.youtube_network import (
    YoutubeNetworkSettingsProvider,
    test_youtube_connection as check_youtube_connection,
    validate_youtube_network_settings,
)


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("https://localhost:7890/", "https://localhost:7890"),
        ("socks5://[::1]:7891", "socks5://[::1]:7891"),
    ],
)
def test_accepts_supported_local_proxy_urls(url: str, canonical: str) -> None:
    result = validate_youtube_network_settings("manual", url)

    assert result.mode == "manual"
    assert result.proxy_url == canonical
    assert result.ydl_options() == {"proxy": canonical}


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://127.0.0.1:7890",
        "http://192.168.1.2:7890",
        "http://user:secret@127.0.0.1:7890",
        "http://127.0.0.1",
        "http://127.0.0.1:70000",
        "http://127.0.0.1:7890/path",
        "http://127.0.0.1:7890?x=1",
    ],
)
def test_rejects_unsafe_or_invalid_proxy_urls(url: str) -> None:
    with pytest.raises(ValueError):
        validate_youtube_network_settings("manual", url)


def test_system_mode_ignores_proxy_value() -> None:
    result = validate_youtube_network_settings("system", "http://127.0.0.1:7890")

    assert result.mode == "system"
    assert result.proxy_url == ""
    assert result.ydl_options() == {}


def test_database_defaults_to_system_and_reads_new_value_each_time(tmp_path) -> None:
    database = Database(tmp_path / "settings.db")
    database.initialize()
    provider = YoutubeNetworkSettingsProvider(database)

    assert provider.get().mode == "system"
    assert provider.ydl_options() == {}

    settings = validate_youtube_network_settings("manual", "socks5://localhost:7891")
    provider.save(settings)

    assert provider.get() == settings
    assert provider.ydl_options() == {"proxy": "socks5://localhost:7891"}


def test_damaged_manual_setting_fails_closed_instead_of_direct_connection(tmp_path) -> None:
    database = Database(tmp_path / "damaged.db")
    database.initialize()
    database.set_settings(
        {"youtube_network_mode": "manual", "youtube_proxy_url": ""}
    )

    settings = YoutubeNetworkSettingsProvider(database).get()

    assert settings.mode == "manual"
    assert settings.ydl_options() == {"proxy": "http://127.0.0.1:1"}


def test_connectivity_check_really_uses_selected_http_proxy(monkeypatch) -> None:
    requested_paths: list[str] = []

    class ProxyHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            requested_paths.append(self.path)
            self.send_response(200 if "watch?v=" in self.path else 405)
            self.end_headers()

        def log_message(self, _format: str, *_args: object) -> None:
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        "douyin_downloader.youtube_network.YOUTUBE_CONNECTIVITY_URLS",
        (
            "http://connectivity.test/watch?v=test",
            "http://connectivity.test/youtubei/v1/player",
        ),
    )
    try:
        result = asyncio.run(
            check_youtube_connection(
                validate_youtube_network_settings(
                    "manual", f"http://127.0.0.1:{server.server_port}"
                )
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result == (True, "YouTube 网络通道正常（不代表所有视频均可匿名访问）")
    assert requested_paths == [
        "http://connectivity.test/watch?v=test",
        "http://connectivity.test/youtubei/v1/player",
    ]
