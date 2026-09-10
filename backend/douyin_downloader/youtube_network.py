from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit, urlunsplit


YOUTUBE_NETWORK_MODE_KEY = "youtube_network_mode"
YOUTUBE_PROXY_URL_KEY = "youtube_proxy_url"
YOUTUBE_CONNECTIVITY_URLS = (
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://www.youtube.com/youtubei/v1/player",
)
ALLOWED_PROXY_SCHEMES = {"http", "https", "socks5"}
ALLOWED_PROXY_HOSTS = {"127.0.0.1", "localhost", "::1"}


class SettingsStore(Protocol):
    def get_setting(self, key: str) -> str | None: ...

    def set_setting(self, key: str, value: str) -> None: ...

    def get_settings(self, keys: tuple[str, ...]) -> dict[str, str]: ...

    def set_settings(self, values: dict[str, str]) -> None: ...


@dataclass(frozen=True, slots=True)
class YoutubeNetworkSettings:
    mode: Literal["system", "manual"]
    proxy_url: str = ""

    def ydl_options(self) -> dict[str, Any]:
        if self.mode == "manual":
            return {"proxy": self.proxy_url}
        return {}


def validate_youtube_network_settings(
    mode: str,
    proxy_url: str | None = None,
) -> YoutubeNetworkSettings:
    normalized_mode = mode.strip().lower()
    if normalized_mode not in {"system", "manual"}:
        raise ValueError("YouTube 网络模式必须为跟随本机网络或本地代理")
    if normalized_mode == "system":
        return YoutubeNetworkSettings("system", "")

    raw_proxy = (proxy_url or "").strip()
    if not raw_proxy:
        raise ValueError("本地代理模式必须填写代理地址")
    try:
        parsed = urlsplit(raw_proxy)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("代理地址或端口无效") from exc
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if scheme not in ALLOWED_PROXY_SCHEMES:
        raise ValueError("代理仅支持 http、https 或 socks5 协议")
    if host not in ALLOWED_PROXY_HOSTS:
        raise ValueError("代理地址只能使用本机 127.0.0.1、localhost 或 ::1")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("暂不支持带用户名或密码的代理")
    if port is None or not 1 <= port <= 65535:
        raise ValueError("代理地址必须包含 1–65535 的有效端口")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("代理地址不能包含路径、查询参数或片段")

    display_host = f"[{host}]" if ":" in host else host
    canonical = urlunsplit((scheme, f"{display_host}:{port}", "", "", ""))
    return YoutubeNetworkSettings("manual", canonical)


class YoutubeNetworkSettingsProvider:
    """Read settings for every operation so the next retry sees saved changes."""

    def __init__(self, store: SettingsStore) -> None:
        self._store = store

    def get(self) -> YoutubeNetworkSettings:
        values = self._store.get_settings(
            (YOUTUBE_NETWORK_MODE_KEY, YOUTUBE_PROXY_URL_KEY)
        )
        mode = values.get(YOUTUBE_NETWORK_MODE_KEY) or "system"
        proxy_url = values.get(YOUTUBE_PROXY_URL_KEY) or ""
        try:
            return validate_youtube_network_settings(mode, proxy_url)
        except ValueError:
            # Damaged manual settings must fail closed instead of silently going direct.
            if mode.strip().lower() == "manual":
                return YoutubeNetworkSettings("manual", "http://127.0.0.1:1")
            return YoutubeNetworkSettings("system", "")

    def ydl_options(self) -> dict[str, Any]:
        return self.get().ydl_options()

    def save(self, settings: YoutubeNetworkSettings) -> None:
        self._store.set_settings(
            {
                YOUTUBE_NETWORK_MODE_KEY: settings.mode,
                YOUTUBE_PROXY_URL_KEY: settings.proxy_url,
            }
        )


def is_proxy_connection_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        marker in lowered
        for marker in (
            "proxyerror",
            "proxy error",
            "unable to connect to proxy",
            "cannot connect to proxy",
            "failed to connect to proxy",
            "proxy connection",
            "proxy tunnel",
            "socks connection",
            "socks5 connection",
        )
    )


def _test_connection_sync(settings: YoutubeNetworkSettings) -> tuple[bool, str]:
    from yt_dlp import YoutubeDL
    from yt_dlp.networking.exceptions import HTTPError

    options: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "socket_timeout": 9,
        **settings.ydl_options(),
    }
    try:
        with YoutubeDL(options) as ydl:
            statuses: list[int] = []
            for url in YOUTUBE_CONNECTIVITY_URLS:
                try:
                    response = ydl.urlopen(url)
                except HTTPError as exc:
                    statuses.append(int(exc.status))
                    continue
                try:
                    response_status = getattr(response, "status", None)
                    statuses.append(
                        int(response_status if response_status is not None else response.getcode())
                    )
                finally:
                    response.close()
    except Exception as exc:
        if settings.mode == "manual" or is_proxy_connection_error(str(exc)):
            return False, "无法通过本地代理连接 YouTube，请检查代理软件、地址和端口"
        return False, "当前本机网络无法连接 YouTube，请检查 VPN、网络或地区限制"
    watch_status, api_status = statuses
    if watch_status in {200, 204} and api_status in {200, 204, 400, 404, 405}:
        return True, "YouTube 网络通道正常（不代表所有视频均可匿名访问）"
    return False, (
        "网络可以连接 YouTube，但视频接口受到限制"
        f"（HTTP {watch_status}/{api_status}）"
    )


async def test_youtube_connection(
    settings: YoutubeNetworkSettings,
) -> tuple[bool, str]:
    return await asyncio.wait_for(
        asyncio.to_thread(_test_connection_sync, settings),
        timeout=10,
    )
