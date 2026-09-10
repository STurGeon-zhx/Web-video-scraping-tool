from __future__ import annotations

import time
from pathlib import Path

from douyin_downloader.youtube_auth import YoutubeAuthManager


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待 YouTube 验证状态超时")


def test_auth_window_stays_open_until_user_completes_and_exports_only_allowed_cookies(
    tmp_path: Path,
) -> None:
    visited: list[str] = []
    launch_options: list[dict] = []
    closed: list[bool] = []
    cookies = [
        {
            "name": "LOGIN_INFO",
            "value": "youtube-secret",
            "domain": ".youtube.com",
            "path": "/",
            "expires": 2_000_000_000,
            "secure": True,
        },
        {
            "name": "SAPISID",
            "value": "google-secret",
            "domain": ".google.com",
            "path": "/",
            "expires": 2_000_000_000,
            "secure": True,
        },
        {
            "name": "unrelated",
            "value": "must-not-appear",
            "domain": ".example.com",
            "path": "/",
        },
    ]

    class Page:
        def goto(self, url: str, **_kwargs) -> None:
            visited.append(url)

        def wait_for_timeout(self, milliseconds: int) -> None:
            time.sleep(milliseconds / 1000)

    class Context:
        pages = [Page()]

        def cookies(self):
            return cookies

        def close(self) -> None:
            closed.append(True)

    class Chromium:
        def launch_persistent_context(self, *_args, **kwargs):
            launch_options.append(kwargs)
            return Context()

    class Playwright:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    target = "https://www.youtube.com/shorts/UrgqdJ6vtoY"
    manager = YoutubeAuthManager(
        tmp_path / "youtube",
        lambda: {"proxy": "http://127.0.0.1:7890"},
        playwright_factory=lambda: Playwright(),
        poll_milliseconds=10,
    )

    manager.start(target)
    wait_until(lambda: manager.status()["status"] == "authenticated")

    assert manager.status()["running"] is True
    assert closed == []
    assert visited == [target]
    assert launch_options[0]["proxy"] == {"server": "http://127.0.0.1:7890"}
    assert launch_options[0]["ignore_default_args"] == ["--enable-automation"]
    assert "--disable-blink-features=AutomationControlled" in launch_options[0]["args"]
    assert "--window-position=100,100" in launch_options[0]["args"]

    result = manager.complete()

    assert result["running"] is False
    assert result["has_saved_state"] is True
    assert closed == [True]
    content = manager.cookie_file.read_text(encoding="utf-8")
    assert "youtube-secret" in content
    assert "google-secret" in content
    assert "must-not-appear" not in content
    assert manager.cookie_options() == {"cookiefile": str(manager.cookie_file)}


def test_auth_manager_without_saved_login_reports_closed(tmp_path: Path) -> None:
    closed: list[bool] = []

    class Page:
        def goto(self, _url: str, **_kwargs) -> None:
            return None

        def wait_for_timeout(self, milliseconds: int) -> None:
            time.sleep(milliseconds / 1000)

    class Context:
        pages = [Page()]

        def cookies(self):
            return []

        def close(self) -> None:
            closed.append(True)

    class Chromium:
        def launch_persistent_context(self, *_args, **_kwargs):
            return Context()

    class Playwright:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    manager = YoutubeAuthManager(
        tmp_path / "youtube",
        lambda: {},
        playwright_factory=lambda: Playwright(),
        poll_milliseconds=10,
    )
    manager.start()
    wait_until(lambda: manager.status()["status"] == "waiting")

    result = manager.complete()

    assert result["status"] == "closed"
    assert result["has_saved_state"] is False
    assert closed == [True]
