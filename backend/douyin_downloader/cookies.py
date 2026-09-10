from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable


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

    def refresh(self, url: str) -> Path:
        if self._playwright_factory is None:
            from playwright.sync_api import sync_playwright

            playwright_factory = sync_playwright
        else:
            playwright_factory = self._playwright_factory

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with playwright_factory() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(self.profile_dir),
                channel="msedge",
                headless=True,
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
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
            finally:
                context.close()
        write_netscape_cookie_file(cookies, self.cookie_file)
        return self.cookie_file
