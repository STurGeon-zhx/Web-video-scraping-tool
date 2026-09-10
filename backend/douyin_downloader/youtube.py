from __future__ import annotations

import importlib.metadata
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal, Protocol
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from .page_collector import CollectionOutcome

if TYPE_CHECKING:
    from .page_collector import CollectionControl
    from .platforms import ExpandedVideo


YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be"}
YOUTUBE_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
MAX_YOUTUBE_CANDIDATES = 5_000
MAX_YOUTUBE_COLLECTION_SECONDS = 15 * 60


class YoutubeDLSession(Protocol):
    def __enter__(self) -> "YoutubeDLSession": ...
    def __exit__(self, *args: object) -> None: ...
    def extract_info(self, url: str, download: bool) -> dict[str, Any]: ...


class YoutubeRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class YoutubeUrl:
    kind: Literal["single", "page", "unsupported"]
    canonical_url: str
    page_kind: Literal["playlist", "channel"] | None = None


def is_youtube_host(url: str) -> bool:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".") in YOUTUBE_HOSTS
    except ValueError:
        return False


def classify_youtube_url(url: str) -> YoutubeUrl | None:
    """Classify and canonicalize the explicitly supported YouTube URL shapes."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in YOUTUBE_HOSTS:
        return None
    path = parsed.path.rstrip("/") or "/"
    query = parse_qs(parsed.query)

    if host == "youtu.be":
        video_id = path.strip("/").split("/", 1)[0]
        if YOUTUBE_VIDEO_ID.fullmatch(video_id):
            return YoutubeUrl("single", f"https://www.youtube.com/watch?v={video_id}")
        return YoutubeUrl("unsupported", url)

    if path == "/watch":
        video_id = (query.get("v") or [""])[0]
        if YOUTUBE_VIDEO_ID.fullmatch(video_id):
            # list/start/time parameters intentionally do not turn a watch URL into a page batch.
            return YoutubeUrl("single", f"https://www.youtube.com/watch?v={video_id}")
        return YoutubeUrl("unsupported", url)

    parts = [part for part in path.split("/") if part]
    if len(parts) == 2 and parts[0] == "shorts" and YOUTUBE_VIDEO_ID.fullmatch(parts[1]):
        return YoutubeUrl("single", f"https://www.youtube.com/shorts/{parts[1]}")

    if path == "/playlist":
        playlist_id = (query.get("list") or [""])[0].strip()
        if playlist_id:
            return YoutubeUrl(
                "page",
                urlunsplit(("https", "www.youtube.com", "/playlist", urlencode({"list": playlist_id}), "")),
                "playlist",
            )
        return YoutubeUrl("unsupported", url)

    is_handle = bool(parts and parts[0].startswith("@") and len(parts[0]) > 1)
    is_channel = len(parts) >= 2 and parts[0] == "channel" and bool(parts[1])
    if is_handle or is_channel:
        root_length = 1 if is_handle else 2
        suffix = parts[root_length:]
        if not suffix:
            suffix = ["videos"]
        if suffix not in (["videos"], ["shorts"]):
            return YoutubeUrl("unsupported", url)
        root = parts[:root_length]
        canonical_path = "/" + "/".join([*root, *suffix])
        return YoutubeUrl(
            "page",
            urlunsplit(("https", "www.youtube.com", canonical_path, "", "")),
            "channel",
        )

    return YoutubeUrl("unsupported", url)


def is_youtube_single_url(url: str) -> bool:
    classified = classify_youtube_url(url)
    return classified is not None and classified.kind == "single"


def youtube_video_id(url: str) -> str | None:
    classified = classify_youtube_url(url)
    if classified is None or classified.kind != "single":
        return None
    parsed = urlsplit(classified.canonical_url)
    if parsed.path == "/watch":
        return (parse_qs(parsed.query).get("v") or [None])[0]
    parts = [part for part in parsed.path.split("/") if part]
    return parts[1] if len(parts) == 2 and parts[0] == "shorts" else None


def is_youtube_page_url(url: str) -> bool:
    classified = classify_youtube_url(url)
    return classified is not None and classified.kind == "page"


def is_youtube_access_restriction(message: str) -> bool:
    lowered = message.lower()
    return (
        "po token" in lowered
        or ("sign in" in lowered and "bot" in lowered)
        or ("confirm" in lowered and "bot" in lowered)
    )


def youtube_failure_message(message: str) -> str:
    from .youtube_network import is_proxy_connection_error

    if is_proxy_connection_error(message):
        return "YouTube 本地代理不可用，请检查代理软件、地址和端口"
    if is_youtube_access_restriction(message):
        return "YouTube 要求登录确认非机器人，当前匿名模式无法下载；可更换低风险网络出口后重试"
    return message


def _runtime_root() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "runtime"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[2] / ".runtime"


def find_deno() -> Path | None:
    override = os.environ.get("VIDEO_DOWNLOADER_DENO")
    candidates = [Path(override)] if override else []
    candidates.extend(
        [
            _runtime_root() / "deno" / ("deno.exe" if os.name == "nt" else "deno"),
            _runtime_root() / ("deno.exe" if os.name == "nt" else "deno"),
        ]
    )
    return next((path.resolve() for path in candidates if path.is_file()), None)


def youtube_runtime_options() -> dict[str, Any]:
    deno = find_deno()
    if deno is None:
        raise YoutubeRuntimeError(
            "缺少 YouTube Deno 运行组件，请在项目目录运行 scripts\\setup-youtube-runtime.ps1"
        )
    try:
        importlib.metadata.version("yt-dlp-ejs")
    except importlib.metadata.PackageNotFoundError as exc:
        raise YoutubeRuntimeError(
            "缺少 YouTube EJS 组件，请安装项目 download 依赖后重试"
        ) from exc
    return {
        "js_runtimes": {"deno": {"path": str(deno)}},
        "remote_components": set(),
    }


def _default_ydl_factory(options: dict[str, Any]) -> YoutubeDLSession:
    from .downloader import _default_ydl_factory

    return _default_ydl_factory(options)


def _unavailable_entry(entry: dict[str, Any]) -> bool:
    availability = str(entry.get("availability") or "").lower()
    title = str(entry.get("title") or "").lower()
    try:
        age_restricted = int(entry.get("age_limit") or 0) >= 18
    except (TypeError, ValueError):
        age_restricted = False
    return (
        availability in {"private", "premium_only", "subscriber_only", "needs_auth"}
        or title in {"[private video]", "[deleted video]"}
        or age_restricted
        or bool(entry.get("is_live"))
        or entry.get("live_status") in {"is_live", "is_upcoming"}
        or str(entry.get("vcodec") or "").lower() == "none"
        or str(entry.get("resolution") or "").lower() == "audio only"
    )


class YoutubePageCollector:
    def __init__(
        self,
        ydl_factory: Callable[[dict[str, Any]], YoutubeDLSession] = _default_ydl_factory,
        runtime_options_provider: Callable[[], dict[str, Any]] = youtube_runtime_options,
        network_options_provider: Callable[[], dict[str, Any]] | None = None,
        auth_options_provider: Callable[[], dict[str, Any]] | None = None,
        candidate_limit: int = MAX_YOUTUBE_CANDIDATES,
        time_limit_seconds: float = MAX_YOUTUBE_COLLECTION_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ydl_factory = ydl_factory
        self._runtime_options_provider = runtime_options_provider
        self._network_options_provider = network_options_provider or (lambda: {})
        self._auth_options_provider = auth_options_provider or (lambda: {})
        self._candidate_limit = candidate_limit
        self._time_limit_seconds = time_limit_seconds
        self._clock = clock
        self._manual_proxy_active = False

    def collect(
        self,
        source_url: str,
        max_items: int,
        on_video: Callable[["ExpandedVideo"], bool],
        on_status: Callable[[str, str | None], None],
        control: "CollectionControl",
    ) -> CollectionOutcome:
        from .downloader import ErrorCode, classify_download_error
        from .youtube_network import is_proxy_connection_error

        self._manual_proxy_active = False
        try:
            return self._collect(source_url, max_items, on_video, on_status, control)
        except Exception as exc:
            detail = str(exc)
            if self._manual_proxy_active and (
                is_proxy_connection_error(detail)
                or classify_download_error(detail) == ErrorCode.NETWORK
            ):
                raise RuntimeError(
                    "YouTube 本地代理不可用，请检查代理软件、地址和端口"
                ) from exc
            raise

    def _collect(
        self,
        source_url: str,
        max_items: int,
        on_video: Callable[["ExpandedVideo"], bool],
        on_status: Callable[[str, str | None], None],
        control: "CollectionControl",
    ) -> CollectionOutcome:
        from .platforms import ExpandedVideo

        classified = classify_youtube_url(source_url)
        if classified is None or classified.kind != "page":
            raise ValueError("YouTube 页面采集仅支持播放列表或频道视频页")
        network_options = self._network_options_provider()
        self._manual_proxy_active = bool(network_options.get("proxy"))
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "extract_flat": "in_playlist",
            "lazy_playlist": True,
            "ignoreerrors": True,
            "playlistend": self._candidate_limit,
            "socket_timeout": 30,
            "retries": 3,
            **self._runtime_options_provider(),
            **network_options,
            **self._auth_options_provider(),
        }
        on_status("collecting", None)
        started = self._clock()
        accepted = 0
        examined = 0
        eligible = 0
        seen: set[str] = set()
        hit_safety_limit = False
        with self._ydl_factory(options) as ydl:
            info = ydl.extract_info(classified.canonical_url, download=False)
            if not isinstance(info, dict):
                return CollectionOutcome("no_videos", "YouTube 页面没有返回视频信息")
            entries = info.get("entries") or []
            for entry in entries:
                control.wait_if_paused()
                if control.is_cancelled():
                    return CollectionOutcome("stopped", "采集已取消")
                if examined >= self._candidate_limit or self._clock() - started >= self._time_limit_seconds:
                    hit_safety_limit = True
                    break
                examined += 1
                if not isinstance(entry, dict) or _unavailable_entry(entry):
                    continue
                video_id = str(entry.get("id") or "").strip()
                if not YOUTUBE_VIDEO_ID.fullmatch(video_id) or video_id in seen:
                    continue
                seen.add(video_id)
                eligible += 1
                video = ExpandedVideo(
                    platform="youtube",
                    video_id=video_id,
                    title=str(entry.get("title") or "").strip(),
                    canonical_url=f"https://www.youtube.com/watch?v={video_id}",
                    original_url=source_url,
                )
                if on_video(video):
                    accepted += 1
                    on_status("collecting", f"已采集 {accepted}/{max_items} 条")
                    if accepted >= max_items:
                        return CollectionOutcome("target_reached", None)

        if eligible == 0:
            return CollectionOutcome("no_videos", "没有发现新的匿名可访问公开视频")
        if hit_safety_limit:
            reason = f"仅采集到 {accepted}/{max_items} 条，已达到候选数量或运行时间安全上限"
        else:
            reason = f"仅采集到 {accepted}/{max_items} 条，页面没有更多可下载的公开视频"
        return CollectionOutcome("insufficient", reason)
