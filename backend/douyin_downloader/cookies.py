from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit


@dataclass(slots=True, frozen=True)
class BrowserMedia:
    url: str
    title: str
    http_headers: dict[str, str]


def write_netscape_cookie_file(
    cookies: Iterable[dict[str, Any]],
    destination: Path,
    allowed_domains: tuple[str, ...] = ("douyin.com",),
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Netscape HTTP Cookie File"]
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lower()
        if not any(
            domain == allowed or domain.endswith(f".{allowed}")
            for allowed in allowed_domains
        ):
            continue
        include_subdomains = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if cookie.get("secure") else "FALSE"
        expires = max(0, int(cookie.get("expires") or 0))
        lines.append(
            "\t".join(
                [
                    domain,
                    include_subdomains,
                    str(cookie.get("path") or "/"),
                    secure,
                    str(expires),
                    str(cookie.get("name") or ""),
                    str(cookie.get("value") or ""),
                ]
            )
        )
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


class AnonymousCookieProvider:
    def __init__(
        self,
        app_data_dir: Path,
        playwright_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.app_data_dir = app_data_dir
        self.cookie_file = app_data_dir / "anonymous-cookies.txt"
        self.profile_dir = app_data_dir / "anonymous-edge-profile"
        self._playwright_factory = playwright_factory
        self._browser_lock = threading.Lock()
        self._media_cache: dict[str, BrowserMedia] = {}

    @staticmethod
    def _is_media_url(url: str) -> bool:
        parsed = urlsplit(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname)

    @staticmethod
    def _is_douyin_video_cdn(url: str) -> bool:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").lower()
        return hostname.endswith(".douyinvod.com") or "/video/tos/" in parsed.path

    @classmethod
    def _page_media(
        cls,
        page: Any,
        target_url: str,
        response_urls: list[str],
    ) -> BrowserMedia | None:
        payload: dict[str, Any] = {}
        try:
            payload = page.evaluate(
                """
                () => ({
                    sources: Array.from(document.querySelectorAll('video'))
                        .flatMap(video => [
                            video.currentSrc,
                            video.src,
                            ...Array.from(video.querySelectorAll('source'))
                                .map(source => source.src)
                        ])
                        .filter(Boolean),
                    title: (
                        document.querySelector('meta[property="og:title"]')?.content ||
                        document.title || ''
                    ).trim(),
                    userAgent: navigator.userAgent
                })
                """
            )
        except Exception:
            payload = {}

        sources = [str(value) for value in payload.get("sources") or []]
        captured = list(reversed(response_urls))
        preferred_responses = [
            value for value in captured if cls._is_douyin_video_cdn(value)
        ]
        candidates = [*sources, *preferred_responses]
        media_url = next((value for value in candidates if cls._is_media_url(value)), None)
        if media_url is None:
            return None
        headers = {"Referer": target_url}
        user_agent = str(payload.get("userAgent") or "").strip()
        if user_agent:
            headers["User-Agent"] = user_agent
        return BrowserMedia(
            url=media_url,
            title=str(payload.get("title") or "").strip(),
            http_headers=headers,
        )

    @staticmethod
    def _capture_media_responses(page: Any, response_urls: list[str]) -> None:
        def capture(response: Any) -> None:
            try:
                content_type = str(
                    response.headers.get("content-type") or ""
                ).partition(";")[0].lower()
                if content_type.startswith("video/") or content_type in {
                    "application/vnd.apple.mpegurl",
                    "application/x-mpegurl",
                    "application/dash+xml",
                }:
                    response_urls.append(str(response.url))
            except Exception:
                return

        try:
            page.on("response", capture)
        except (AttributeError, TypeError):
            return

    def refresh(self, url: str) -> Path:
        if self._playwright_factory is None:
            from playwright.sync_api import sync_playwright

            playwright_factory = sync_playwright
        else:
            playwright_factory = self._playwright_factory

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        response_urls: list[str] = []
        with self._browser_lock, playwright_factory() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel="msedge",
                headless=True,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._capture_media_responses(page, response_urls)
                page.goto(url, wait_until="domcontentloaded", timeout=30_000)
                cookies: list[dict[str, Any]] = []
                for _ in range(32):
                    cookies = context.cookies(["https://www.douyin.com/"])
                    names = {str(cookie.get("name") or "") for cookie in cookies}
                    if "s_v_web_id" in names and "ttwid" in names:
                        break
                    page.wait_for_timeout(250)
                else:
                    raise RuntimeError("Fresh cookies are needed after video page verification")
                page.wait_for_timeout(5_000)
                cookies = context.cookies(["https://www.douyin.com/"])
                media = self._page_media(page, url, response_urls)
                if media is not None:
                    self._media_cache[url] = media
            finally:
                context.close()
        write_netscape_cookie_file(cookies, self.cookie_file)
        return self.cookie_file

    def resolve_media(self, url: str) -> BrowserMedia:
        cached = self._media_cache.get(url)
        if cached is not None:
            return cached

        if self._playwright_factory is None:
            from playwright.sync_api import sync_playwright

            playwright_factory = sync_playwright
        else:
            playwright_factory = self._playwright_factory

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        response_urls: list[str] = []
        with self._browser_lock, playwright_factory() as playwright:
            cached = self._media_cache.get(url)
            if cached is not None:
                return cached
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel="msedge",
                headless=True,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                self._capture_media_responses(page, response_urls)
                page.goto(url, wait_until="domcontentloaded", timeout=60_000)
                media = None
                for _ in range(60):
                    media = self._page_media(page, url, response_urls)
                    if media is not None:
                        break
                    page.wait_for_timeout(250)
                if media is None:
                    raise RuntimeError("抖音页面未返回可下载的视频地址")
                self._media_cache[url] = media
                cookies = context.cookies(["https://www.douyin.com/"])
                if cookies:
                    write_netscape_cookie_file(cookies, self.cookie_file)
                return media
            finally:
                context.close()
