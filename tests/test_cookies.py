from pathlib import Path

from douyin_downloader.cookies import AnonymousCookieProvider, write_netscape_cookie_file


def test_writes_only_douyin_cookies_without_secret_logging(tmp_path: Path) -> None:
    destination = tmp_path / "cookies.txt"
    cookies = [
        {
            "name": "sessionid",
            "value": "secret-value",
            "domain": ".douyin.com",
            "path": "/",
            "expires": 2_000_000_000,
            "secure": True,
        },
        {
            "name": "unrelated",
            "value": "must-not-appear",
            "domain": ".example.com",
            "path": "/",
            "expires": -1,
            "secure": False,
        },
    ]

    write_netscape_cookie_file(cookies, destination)
    content = destination.read_text(encoding="utf-8")

    assert content.startswith("# Netscape HTTP Cookie File\n")
    assert ".douyin.com\tTRUE\t/\tTRUE\t2000000000\tsessionid\tsecret-value" in content
    assert "example.com" not in content
    assert "must-not-appear" not in content


def test_refresh_opens_target_video_and_waits_for_target_cookies(tmp_path: Path) -> None:
    visited: list[str] = []
    waits: list[int] = []
    cookie_snapshots = [
        [{"name": "s_v_web_id", "value": "first", "domain": ".douyin.com"}],
        [
            {"name": "s_v_web_id", "value": "ready", "domain": ".douyin.com"},
            {"name": "ttwid", "value": "target", "domain": ".douyin.com"},
        ],
        [
            {"name": "s_v_web_id", "value": "ready", "domain": ".douyin.com"},
            {"name": "ttwid", "value": "target", "domain": ".douyin.com"},
            {"name": "odin_tt", "value": "settled", "domain": ".douyin.com"},
        ],
    ]

    class Page:
        def goto(self, url: str, **_kwargs) -> None:
            visited.append(url)

        def wait_for_timeout(self, _milliseconds: int) -> None:
            waits.append(_milliseconds)

    class Context:
        pages = [Page()]

        def cookies(self, _urls):
            if len(cookie_snapshots) > 1:
                return cookie_snapshots.pop(0)
            return cookie_snapshots[0]

        def close(self) -> None:
            return None

    class Chromium:
        def launch_persistent_context(self, *_args, **_kwargs):
            return Context()

    class Playwright:
        chromium = Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    url = "https://www.douyin.com/video/1234567890123456789"
    provider = AnonymousCookieProvider(tmp_path, playwright_factory=lambda: Playwright())

    cookie_file = provider.refresh(url)

    assert visited == [url]
    content = cookie_file.read_text(encoding="utf-8")
    rows = [line.split("\t") for line in content.splitlines() if not line.startswith("#")]
    values = {row[5]: row[6] for row in rows}
    assert values["ttwid"] == "target"
    assert values["s_v_web_id"] == "ready"
    assert values["odin_tt"] == "settled"
    assert 5_000 in waits
