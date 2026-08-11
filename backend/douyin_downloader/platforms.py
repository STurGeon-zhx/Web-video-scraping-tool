from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit


MAX_EXPANDED_VIDEOS = 500


class YoutubeDLSession(Protocol):
    def __enter__(self) -> "YoutubeDLSession": ...
    def __exit__(self, *args: object) -> None: ...
    def extract_info(self, url: str, download: bool) -> dict[str, Any]: ...


class BatchExpansionError(ValueError):
    pass


@dataclass(slots=True, frozen=True)
class ExpandedVideo:
    platform: str
    video_id: str
    title: str
    canonical_url: str
    original_url: str


@dataclass(slots=True)
class ExpansionResult:
    videos: list[ExpandedVideo]
    input_count: int
    duplicate_count: int
    platform_counts: dict[str, int]


def _default_ydl_factory(options: dict[str, Any]) -> YoutubeDLSession:
    from .downloader import _default_ydl_factory as downloader_factory

    return downloader_factory(options)


def _is_kuaishou_url(url: str) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    is_domain = (
        hostname == "kuaishou.com"
        or hostname.endswith(".kuaishou.com")
        or hostname == "kuaishou.cn"
        or hostname.endswith(".kuaishou.cn")
    )
    return is_domain and (
        parsed.path.startswith("/f/")
        or parsed.path.startswith("/short-video/")
    )


def extractor_supports_url(url: str) -> bool:
    if _is_kuaishou_url(url):
        return True
    from yt_dlp.extractor import gen_extractor_classes

    for extractor in gen_extractor_classes():
        if extractor.__name__ == "GenericIE" or getattr(extractor, "IE_NAME", "") == "generic":
            continue
        if extractor.suitable(url):
            return True
    return False


def _platform_slug(info: dict[str, Any], url: str) -> str:
    hostname = (urlsplit(url).hostname or "").lower()
    key = str(info.get("extractor_key") or info.get("extractor") or "").lower()
    if "kuaishou" in key or _is_kuaishou_url(url):
        return "kuaishou"
    if "bilibili" in key or hostname == "bilibili.com" or hostname.endswith(".bilibili.com"):
        return "bilibili"
    if "tiktok" in key or hostname == "douyin.com" or hostname.endswith(".douyin.com"):
        return "douyin"
    return key.replace(":", "-") or hostname


def _has_video(info: dict[str, Any]) -> bool:
    formats = info.get("formats") or []
    return any(str(item.get("vcodec") or "none").lower() != "none" for item in formats if isinstance(item, dict))


class BatchExpander:
    def __init__(
        self,
        ydl_factory: Callable[[dict[str, Any]], YoutubeDLSession] = _default_ydl_factory,
        limit: int = MAX_EXPANDED_VIDEOS,
    ) -> None:
        self._ydl_factory = ydl_factory
        self._limit = limit

    def expand(
        self,
        urls: list[str],
        original_urls: list[str] | None = None,
    ) -> ExpansionResult:
        originals = original_urls or urls
        if len(originals) != len(urls):
            raise ValueError("原始链接与解析链接数量不一致")
        videos: list[ExpandedVideo] = []
        seen: set[tuple[str, str]] = set()
        duplicate_count = 0
        options = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": False,
            "extract_flat": "in_playlist",
            "playlistend": self._limit + 1,
            "skip_download": True,
        }
        for url, original in zip(urls, originals, strict=True):
            with self._ydl_factory(options) as ydl:
                info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise BatchExpansionError("平台没有返回可用的视频信息")
            if info.get("_type") in {"playlist", "multi_video"}:
                entries = list(info.get("entries") or [])
                if len(entries) > self._limit:
                    raise BatchExpansionError(f"单条合集或列表最多支持 {self._limit} 个视频")
            else:
                entries = [info]
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                if entry.get("is_live") or entry.get("live_status") in {"is_live", "is_upcoming"}:
                    raise BatchExpansionError("暂不支持直播内容")
                is_flat_entry = info.get("_type") in {"playlist", "multi_video"}
                if not is_flat_entry and not _has_video(entry):
                    raise BatchExpansionError("链接中没有可下载的视频内容")
                video_id = str(entry.get("id") or "").strip()
                if not video_id:
                    raise BatchExpansionError("平台没有返回视频 ID")
                canonical_url = str(entry.get("webpage_url") or entry.get("url") or url)
                if not canonical_url.startswith("https://"):
                    canonical_url = url
                platform = _platform_slug(entry, canonical_url)
                key = (platform, video_id)
                if key in seen:
                    duplicate_count += 1
                    continue
                seen.add(key)
                videos.append(
                    ExpandedVideo(
                        platform=platform,
                        video_id=video_id,
                        title=str(entry.get("title") or ""),
                        canonical_url=canonical_url,
                        original_url=original,
                    )
                )
                if len(videos) > self._limit:
                    raise BatchExpansionError(f"每个批次最多支持 {self._limit} 个视频")
        if not videos:
            raise BatchExpansionError("没有解析到可下载的视频")
        return ExpansionResult(
            videos=videos,
            input_count=len(urls),
            duplicate_count=duplicate_count,
            platform_counts=dict(Counter(item.platform for item in videos)),
        )
