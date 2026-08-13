from __future__ import annotations

import hashlib

import pytest
from yt_dlp.utils import ExtractorError

from douyin_downloader.direct_media import DirectMediaIE
from douyin_downloader.resolver import DirectMediaProbe, LinkResolutionError


def test_extracts_direct_video_with_stable_id_and_disposition_title() -> None:
    final_url = "http://cdn.example/path/original.mp4?token=abc"
    probe = DirectMediaProbe(final_url, "video/mp4", 12345, "下载名称.mp4")

    info = DirectMediaIE(probe=lambda _url: probe)._real_extract(
        "http://cdn.example/redirect"
    )

    assert info["id"] == hashlib.sha256(final_url.encode("utf-8")).hexdigest()[:16]
    assert info["title"] == "下载名称"
    assert info["webpage_url"] == final_url
    assert info["formats"] == [
        {
            "url": final_url,
            "format_id": "direct",
            "ext": "mp4",
            "vcodec": "unknown",
            "filesize": 12345,
        }
    ]


def test_uses_decoded_url_filename_when_disposition_is_missing() -> None:
    probe = DirectMediaProbe(
        "https://cdn.example/media/my%20video.webm",
        "video/webm",
        None,
        None,
    )

    info = DirectMediaIE(probe=lambda _url: probe)._real_extract(probe.final_url)

    assert info["title"] == "my video"
    assert info["formats"][0]["ext"] == "webm"
    assert "filesize" not in info["formats"][0]


def test_reports_probe_failure_as_extraction_error() -> None:
    def fail(_url: str) -> DirectMediaProbe:
        raise LinkResolutionError("链接响应不是可下载的视频直链")

    with pytest.raises(ExtractorError, match="不是可下载的视频直链"):
        DirectMediaIE(probe=fail)._real_extract("https://example.com/page")


def test_direct_extractor_does_not_shadow_dedicated_platforms() -> None:
    assert DirectMediaIE.suitable("https://cdn.example/video.mp4") is True
    assert DirectMediaIE.suitable("https://www.bilibili.com/video/BV1kdKr6qEMF/") is False
    assert DirectMediaIE.suitable("https://www.kuaishou.com/f/X109BY66mOaj1nh") is False
    assert DirectMediaIE.suitable(
        "https://detail.vip.com/detail-10007920-6921967044893585744.html"
    ) is False


def test_extracts_hls_manifest_formats() -> None:
    probe = DirectMediaProbe(
        "https://cdn.example/master.m3u8",
        "application/vnd.apple.mpegurl",
        None,
        None,
    )

    class HlsIE(DirectMediaIE):
        def _extract_m3u8_formats(self, url, video_id, ext="mp4", **_kwargs):
            assert url == probe.final_url
            assert ext == "mp4"
            return [{"url": url, "format_id": "hls", "ext": "mp4"}]

    info = HlsIE(probe=lambda _url: probe)._real_extract(probe.final_url)

    assert info["formats"] == [
        {"url": probe.final_url, "format_id": "hls", "ext": "mp4"}
    ]
