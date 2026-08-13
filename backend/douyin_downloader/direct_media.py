from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

from yt_dlp.extractor import gen_extractor_classes
from yt_dlp.extractor.common import InfoExtractor
from yt_dlp.utils import ExtractorError

from .links import is_public_http_url
from .resolver import DirectMediaProbe, LinkResolutionError, probe_public_video_url


Probe = Callable[[str], DirectMediaProbe]


def _is_kuaishou_domain(url: str) -> bool:
    hostname = (urlsplit(url).hostname or "").lower()
    return (
        hostname == "kuaishou.com"
        or hostname.endswith(".kuaishou.com")
        or hostname == "kuaishou.cn"
        or hostname.endswith(".kuaishou.cn")
    )


def _is_vipshop_detail_domain(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.hostname or "").lower() == "detail.vip.com"


def _dedicated_extractor_supports(url: str) -> bool:
    if _is_kuaishou_domain(url) or _is_vipshop_detail_domain(url):
        return True
    for extractor in gen_extractor_classes():
        if extractor.__name__ == "GenericIE" or getattr(extractor, "IE_NAME", "") == "generic":
            continue
        if extractor.suitable(url):
            return True
    return False


def _extension(probe: DirectMediaProbe) -> str:
    known_types = {
        "video/mp4": "mp4",
        "video/webm": "webm",
        "video/quicktime": "mov",
        "video/x-matroska": "mkv",
        "video/x-msvideo": "avi",
        "video/mpeg": "mpeg",
    }
    if probe.content_type in known_types:
        return known_types[probe.content_type]
    suffix = PurePosixPath(unquote(urlsplit(probe.final_url).path)).suffix.lstrip(".").lower()
    return suffix or "mp4"


def _title(probe: DirectMediaProbe, video_id: str) -> str:
    filename = probe.filename or PurePosixPath(
        unquote(urlsplit(probe.final_url).path)
    ).name
    if filename:
        stem = PurePosixPath(filename).stem.strip()
        if stem:
            return stem
    return f"视频直链_{video_id}"


class DirectMediaIE(InfoExtractor):
    IE_NAME = "direct-media"
    _VALID_URL = r"https?://.+"

    def __init__(self, downloader: Any = None, probe: Probe = probe_public_video_url) -> None:
        super().__init__(downloader)
        self._probe = probe

    @classmethod
    def suitable(cls, url: str) -> bool:
        return (
            is_public_http_url(url)
            and not _dedicated_extractor_supports(url)
        )

    def _real_extract(self, url: str) -> dict[str, Any]:
        try:
            probe = self._probe(url)
        except LinkResolutionError as exc:
            raise ExtractorError(str(exc), expected=True) from exc
        video_id = hashlib.sha256(probe.final_url.encode("utf-8")).hexdigest()[:16]
        extension = _extension(probe)
        if probe.content_type in {
            "application/vnd.apple.mpegurl",
            "application/x-mpegurl",
        } or extension == "m3u8":
            formats = self._extract_m3u8_formats(
                probe.final_url,
                video_id,
                ext="mp4",
            )
            return {
                "id": video_id,
                "title": _title(probe, video_id),
                "webpage_url": probe.final_url,
                "extractor_key": "DirectMedia",
                "ext": "mp4",
                "formats": formats,
            }
        if probe.content_type == "application/dash+xml" or extension == "mpd":
            formats = self._extract_mpd_formats(probe.final_url, video_id)
            return {
                "id": video_id,
                "title": _title(probe, video_id),
                "webpage_url": probe.final_url,
                "extractor_key": "DirectMedia",
                "ext": "mp4",
                "formats": formats,
            }
        media_format: dict[str, Any] = {
            "url": probe.final_url,
            "format_id": "direct",
            "ext": extension,
            "vcodec": "unknown",
        }
        if probe.content_length is not None:
            media_format["filesize"] = probe.content_length
        return {
            "id": video_id,
            "title": _title(probe, video_id),
            "webpage_url": probe.final_url,
            "extractor_key": "DirectMedia",
            "ext": extension,
            "formats": [media_format],
        }
