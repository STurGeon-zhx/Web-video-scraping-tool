from __future__ import annotations

import json
import re
import time
import threading
from copy import deepcopy
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError


APOLLO_MARKER = "window.__APOLLO_STATE__="
MOBILE_STATE_MARKER = "window.INIT_STATE = "
SHORT_VIDEO_PATTERN = re.compile(r"/short-video/([^/?#]+)")
MOBILE_PHOTO_PATTERN = re.compile(r"/fw/photo/([^/?#]+)")
_CACHE_TTL_SECONDS = 600.0
_INFO_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_LOCK = threading.Lock()


class KuaishouExtractionError(ValueError):
    pass


def cache_kuaishou_info(photo_id: str, info: dict[str, Any]) -> None:
    with _CACHE_LOCK:
        _INFO_CACHE[photo_id] = (time.monotonic() + _CACHE_TTL_SECONDS, deepcopy(info))


def _cached_kuaishou_info(photo_id: str) -> dict[str, Any] | None:
    with _CACHE_LOCK:
        item = _INFO_CACHE.get(photo_id)
        if item is None:
            return None
        expires_at, info = item
        if expires_at <= time.monotonic():
            _INFO_CACHE.pop(photo_id, None)
            return None
        return deepcopy(info)


def anonymous_device_id_from_url(url: str) -> str | None:
    value = (parse_qs(urlsplit(url).query).get("ztDid") or [""])[0]
    if re.fullmatch(r"web_[A-Za-z0-9_-]{24,80}", value):
        return value
    return None


def _format_from_representation(codec: str, item: dict[str, Any]) -> dict[str, Any] | None:
    url = str(item.get("url") or "")
    if not url.startswith("https://"):
        return None
    bitrate = item.get("avgBitrate")
    return {
        "format_id": f"{codec}-{item.get('id', 'default')}",
        "url": url,
        "ext": "mp4",
        "vcodec": "h265" if codec == "h265" else "h264",
        "acodec": "aac",
        "width": item.get("width"),
        "height": item.get("height"),
        "tbr": float(bitrate) / 1000 if bitrate else None,
        "fps": item.get("frameRate"),
    }


def parse_kuaishou_page(html: str, photo_id: str, webpage_url: str) -> dict[str, Any]:
    if APOLLO_MARKER not in html:
        raise KuaishouExtractionError("快手匿名访问未返回公开视频信息")
    raw_state = html.split(APOLLO_MARKER, 1)[1]
    try:
        state, _end = json.JSONDecoder().raw_decode(raw_state)
    except (json.JSONDecodeError, TypeError) as exc:
        raise KuaishouExtractionError("快手公开视频信息格式异常") from exc
    cache = state.get("defaultClient") if isinstance(state, dict) else None
    if not isinstance(cache, dict):
        raise KuaishouExtractionError("快手匿名访问未返回公开视频信息")
    photo = cache.get(f"VisionVideoDetailPhoto:{photo_id}")
    if not isinstance(photo, dict):
        photo = next(
            (
                value
                for key, value in cache.items()
                if str(key).startswith("VisionVideoDetailPhoto:")
                and isinstance(value, dict)
                and str(value.get("id") or "") == photo_id
            ),
            None,
        )
    if not isinstance(photo, dict):
        raise KuaishouExtractionError("快手匿名访问未返回公开视频信息")
    formats: list[dict[str, Any]] = []
    resource = photo.get("videoResource")
    resource_json = resource.get("json") if isinstance(resource, dict) else None
    if isinstance(resource_json, dict):
        for codec in ("h264", "h265"):
            codec_resource = resource_json.get(codec)
            if not isinstance(codec_resource, dict):
                continue
            for adaptation in codec_resource.get("adaptationSet") or []:
                if not isinstance(adaptation, dict):
                    continue
                for representation in adaptation.get("representation") or []:
                    if isinstance(representation, dict):
                        parsed = _format_from_representation(codec, representation)
                        if parsed:
                            formats.append(parsed)
    if not formats:
        photo_url = str(photo.get("photoUrl") or "")
        if photo_url.startswith("https://"):
            formats.append(
                {
                    "format_id": "h264-default",
                    "url": photo_url,
                    "ext": "mp4",
                    "vcodec": "h264",
                    "acodec": "aac",
                }
            )
    if not formats:
        raise KuaishouExtractionError("该快手链接中没有可下载的视频")
    duration_ms = photo.get("duration")
    return {
        "id": str(photo.get("id") or photo_id),
        "title": str(photo.get("caption") or f"快手视频_{photo_id}"),
        "duration": float(duration_ms) / 1000 if duration_ms else None,
        "thumbnail": photo.get("coverUrl"),
        "formats": formats,
        "webpage_url": webpage_url,
        "http_headers": {"Referer": "https://www.kuaishou.com/"},
    }


def parse_kuaishou_mobile_page(
    html: str,
    photo_id: str,
    webpage_url: str,
) -> dict[str, Any]:
    if MOBILE_STATE_MARKER not in html:
        raise KuaishouExtractionError("快手移动页未返回公开视频信息")
    try:
        state, _end = json.JSONDecoder().raw_decode(
            html.split(MOBILE_STATE_MARKER, 1)[1]
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise KuaishouExtractionError("快手移动页视频信息格式异常") from exc

    def find_photo(value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            photo = value.get("photo")
            if isinstance(photo, dict):
                share_info = str(photo.get("share_info") or "")
                media_urls = [
                    str(item.get("url") or "")
                    for item in photo.get("mainMvUrls") or []
                    if isinstance(item, dict)
                ]
                if (
                    str(photo.get("photoId") or "") == photo_id
                    or f"photoId={photo_id}" in share_info
                    or any(f"clientCacheKey={photo_id}_" in url for url in media_urls)
                ):
                    return photo
            for child in value.values():
                found = find_photo(child)
                if found is not None:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = find_photo(child)
                if found is not None:
                    return found
        return None

    photo = find_photo(state)
    if photo is None:
        raise KuaishouExtractionError("快手移动页未返回公开视频信息")
    if photo.get("singlePicture"):
        raise KuaishouExtractionError("该快手链接是图集，暂不支持下载")
    formats: list[dict[str, Any]] = []
    manifest = photo.get("manifest")
    if isinstance(manifest, dict):
        for adaptation in manifest.get("adaptationSet") or []:
            if not isinstance(adaptation, dict):
                continue
            for representation in adaptation.get("representation") or []:
                if not isinstance(representation, dict):
                    continue
                url = str(representation.get("url") or "")
                if not url.startswith("https://"):
                    continue
                codec = str(representation.get("videoCodec") or "avc").lower()
                bitrate = representation.get("avgBitrate")
                formats.append(
                    {
                        "format_id": f"mobile-{representation.get('id', 'default')}",
                        "url": url,
                        "ext": "mp4",
                        "vcodec": "h265" if codec in {"hevc", "h265"} else "h264",
                        "acodec": "aac",
                        "width": representation.get("width"),
                        "height": representation.get("height"),
                        "tbr": (
                            float(bitrate) / 1000
                            if bitrate and float(bitrate) > 100_000
                            else bitrate
                        ),
                        "fps": representation.get("frameRate"),
                        "filesize": representation.get("fileSize"),
                    }
                )
    if not formats:
        for index, item in enumerate(photo.get("mainMvUrls") or []):
            url = str(item.get("url") or "") if isinstance(item, dict) else ""
            if url.startswith("https://"):
                formats.append(
                    {
                        "format_id": f"mobile-default-{index + 1}",
                        "url": url,
                        "ext": "mp4",
                        "vcodec": "h264",
                        "acodec": "aac",
                        "width": photo.get("width"),
                        "height": photo.get("height"),
                    }
                )
    if not formats:
        raise KuaishouExtractionError("该快手链接中没有可下载的视频")
    covers = photo.get("coverUrls") or []
    thumbnail = covers[0].get("url") if covers and isinstance(covers[0], dict) else None
    duration_ms = photo.get("duration")
    return {
        "id": photo_id,
        "title": str(photo.get("caption") or f"快手视频_{photo_id}"),
        "duration": float(duration_ms) / 1000 if duration_ms else None,
        "thumbnail": thumbnail,
        "formats": formats,
        "webpage_url": webpage_url,
        "http_headers": {"Referer": "https://www.kuaishou.com/"},
    }


class KuaishouIE(InfoExtractor):
    IE_NAME = "kuaishou"
    _VALID_URL = r"https?://(?:(?:www|v)\.)?kuaishou\.(?:com|cn)/(?:f/[^/?#]+|short-video/(?P<id>[^/?#]+))(?:[/?#].*)?$"

    def _real_extract(self, url: str) -> dict[str, Any]:
        direct_match = SHORT_VIDEO_PATTERN.search(url)
        if direct_match:
            cached = _cached_kuaishou_info(direct_match.group(1))
            if cached is not None:
                return cached
        try:
            from curl_cffi import requests
        except ImportError as exc:
            raise ExtractorError("缺少快手解析依赖 curl-cffi", expected=True) from exc

        last_error: Exception | None = None
        for attempt in range(3):
            try:
                mobile_headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Linux; Android 14; Pixel 8) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Mobile Safari/537.36"
                    )
                }
                mobile = requests.Session(impersonate="chrome_android")
                mobile_response = mobile.get(
                    url,
                    headers=mobile_headers,
                    timeout=20,
                    allow_redirects=True,
                )
                mobile_response.raise_for_status()
                mobile_url = str(mobile_response.url)
                mobile_match = MOBILE_PHOTO_PATTERN.search(mobile_url)
                mobile_photo_id = (
                    mobile_match.group(1)
                    if mobile_match
                    else (direct_match.group(1) if direct_match else self._match_id(url))
                )
                canonical_url = f"https://www.kuaishou.com/short-video/{mobile_photo_id}"
                info = parse_kuaishou_mobile_page(
                    mobile_response.text,
                    mobile_photo_id,
                    canonical_url,
                )
                cache_kuaishou_info(mobile_photo_id, info)
                return info
            except Exception as mobile_error:
                last_error = mobile_error
            try:
                target_url = url
                if "/f/" in urlsplit(url).path:
                    bootstrap = requests.Session(impersonate="chrome")
                    redirect = bootstrap.get(url, timeout=20, allow_redirects=False)
                    location = redirect.headers.get("location")
                    if location:
                        target_url = urljoin(url, location)
                session = requests.Session(impersonate="chrome")
                device_id = anonymous_device_id_from_url(target_url)
                if device_id:
                    for name, value in (
                        ("did", device_id),
                        ("kpf", "PC_WEB"),
                        ("kpn", "KUAISHOU_VISION"),
                        ("clientid", "3"),
                    ):
                        session.cookies.set(name, value, domain=".kuaishou.com")
                else:
                    session.get("https://www.kuaishou.com/", timeout=20)
                response = session.get(target_url, timeout=20, allow_redirects=True)
                response.raise_for_status()
                final_url = str(response.url)
                match = SHORT_VIDEO_PATTERN.search(final_url)
                photo_id = match.group(1) if match else self._match_id(url)
                info = parse_kuaishou_page(response.text, photo_id, final_url)
                cache_kuaishou_info(photo_id, info)
                return info
            except Exception as exc:
                last_error = exc
                if attempt < 2:
                    time.sleep(0.5 * (2**attempt))
        detail = str(last_error or "未知错误")
        raise ExtractorError(f"快手匿名访问失败: {detail}", expected=True) from last_error
