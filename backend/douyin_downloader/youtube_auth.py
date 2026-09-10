from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from .cookies import write_netscape_cookie_file


YOUTUBE_COOKIE_DOMAINS = ("youtube.com", "google.com")
YOUTUBE_AUTH_COOKIE_NAMES = {
    "LOGIN_INFO",
    "SAPISID",
    "__Secure-1PSID",
    "__Secure-3PSID",
}
DEFAULT_YOUTUBE_LOGIN_URL = "https://www.youtube.com/"


class YoutubeAuthManager:
    """Own a dedicated visible Edge profile and export only its YouTube cookies."""

    def __init__(
        self,
        data_dir: Path,
        network_options_provider: Callable[[], dict[str, Any]],
        playwright_factory: Callable[[], Any] | None = None,
        poll_milliseconds: int = 500,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.profile_dir = self.data_dir / "edge-profile"
        self.cookie_file = self.data_dir / "youtube-cookies.txt"
        self._network_options_provider = network_options_provider
        self._playwright_factory = playwright_factory
        self._poll_milliseconds = poll_milliseconds
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = "saved" if self._has_saved_cookie() else "idle"
        self._message = (
            "已保存工具专用 YouTube 验证状态"
            if self._status == "saved"
            else "尚未进行 YouTube 登录验证"
        )
        self._target_url = DEFAULT_YOUTUBE_LOGIN_URL

    def status(self) -> dict[str, Any]:
        with self._lock:
            running = self._thread is not None and self._thread.is_alive()
            return {
                "status": self._status,
                "message": self._message,
                "running": running,
                "has_saved_state": self._has_saved_cookie(),
            }

    def cookie_options(self) -> dict[str, Any]:
        if self._has_saved_cookie():
            return {"cookiefile": str(self.cookie_file)}
        return {}

    def start(self, target_url: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.status_unlocked()
            self._target_url = target_url or DEFAULT_YOUTUBE_LOGIN_URL
            self._stop_event.clear()
            self._status = "starting"
            self._message = "正在打开工具专用 Edge 验证窗口"
            self._thread = threading.Thread(
                target=self._run_browser,
                name="youtube-auth-browser",
                daemon=True,
            )
            self._thread.start()
            return self.status_unlocked()

    def complete(self, timeout: float = 8.0) -> dict[str, Any]:
        with self._lock:
            thread = self._thread
            self._stop_event.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        return self.status()

    def stop(self) -> None:
        self.complete()

    def status_unlocked(self) -> dict[str, Any]:
        running = self._thread is not None and self._thread.is_alive()
        return {
            "status": self._status,
            "message": self._message,
            "running": running,
            "has_saved_state": self._has_saved_cookie(),
        }

    def _run_browser(self) -> None:
        if self._playwright_factory is None:
            from playwright.sync_api import sync_playwright

            playwright_factory = sync_playwright
        else:
            playwright_factory = self._playwright_factory

        context = None
        authenticated = False
        try:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            network_options = self._network_options_provider()
            launch_options: dict[str, Any] = {
                "channel": "msedge",
                "headless": False,
                # Google rejects Edge when Chromium's default automation switch is
                # present.  This remains a real, visible Edge window backed by the
                # tool's isolated profile; only the automation banner/flag is
                # removed so the user can complete an ordinary interactive login.
                "ignore_default_args": ["--enable-automation"],
                "args": [
                    "--disable-blink-features=AutomationControlled",
                    "--window-position=100,100",
                    "--window-size=1100,800",
                ],
            }
            if network_options.get("proxy"):
                launch_options["proxy"] = {"server": str(network_options["proxy"])}
            with playwright_factory() as playwright:
                context = playwright.chromium.launch_persistent_context(
                    str(self.profile_dir),
                    **launch_options,
                )
                page = context.pages[0] if context.pages else context.new_page()
                page.goto(self._target_url, wait_until="domcontentloaded", timeout=60_000)
                self._set_status(
                    "waiting",
                    "请在 Edge 中完成 YouTube 登录或机器人验证；窗口不会自动关闭",
                )
                while not self._stop_event.is_set():
                    cookies = list(context.cookies())
                    if self._is_authenticated(cookies) and not authenticated:
                        self._save_cookies(cookies)
                        authenticated = True
                        self._set_status(
                            "authenticated",
                            "已保存验证状态，可关闭 Edge 或点击“完成验证”",
                        )
                    if not context.pages:
                        break
                    try:
                        context.pages[0].wait_for_timeout(self._poll_milliseconds)
                    except Exception:
                        break
                try:
                    cookies = list(context.cookies())
                except Exception:
                    cookies = []
                if self._is_authenticated(cookies):
                    self._save_cookies(cookies)
                    authenticated = True
        except Exception:
            self._set_status(
                "error",
                "YouTube 验证窗口启动失败，请确认 Edge 和 VPN/代理可用",
            )
        finally:
            if context is not None and self._stop_event.is_set():
                try:
                    context.close()
                except Exception:
                    pass
            with self._lock:
                self._thread = None
                if authenticated or self._has_saved_cookie():
                    self._status = "saved"
                    self._message = "已保存工具专用 YouTube 验证状态"
                elif self._status != "error":
                    self._status = "closed"
                    self._message = "验证窗口已关闭，但没有检测到已登录状态"

    def _save_cookies(self, cookies: list[dict[str, Any]]) -> None:
        write_netscape_cookie_file(
            cookies,
            self.cookie_file,
            allowed_domains=YOUTUBE_COOKIE_DOMAINS,
        )

    @staticmethod
    def _is_authenticated(cookies: list[dict[str, Any]]) -> bool:
        names = {str(cookie.get("name") or "") for cookie in cookies}
        return bool(names & YOUTUBE_AUTH_COOKIE_NAMES)

    def _has_saved_cookie(self) -> bool:
        try:
            return self.cookie_file.is_file() and self.cookie_file.stat().st_size > 32
        except OSError:
            return False

    def _set_status(self, status: str, message: str) -> None:
        with self._lock:
            self._status = status
            self._message = message
