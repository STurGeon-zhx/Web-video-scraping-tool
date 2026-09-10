from __future__ import annotations

from pathlib import Path

import pytest

from douyin_downloader.platforms import (
    BatchExpansionError,
    BatchExpander,
    ExpandedVideo,
    extractor_supports_url,
)


class FakeYoutubeDL:
    def __init__(self, info: dict) -> None:
        self.info = info

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool) -> dict:
        assert download is False
        return self.info


def test_expands_single_bilibili_video_to_platform_task() -> None:
    info = {
        "id": "BV1kdKr6qEMF",
        "title": "测试视频",
        "extractor_key": "BiliBili",
        "webpage_url": "https://www.bilibili.com/video/BV1kdKr6qEMF/",
        "formats": [{"url": "https://cdn.example/video.mp4", "vcodec": "avc1"}],
    }
    expander = BatchExpander(ydl_factory=lambda _options: FakeYoutubeDL(info))

    result = expander.expand(
        ["https://www.bilibili.com/video/BV1kdKr6qEMF/"],
        original_urls=["https://www.bilibili.com/video/BV1kdKr6qEMF/?spm_id_from=1"],
    )

    assert result.videos == [
        ExpandedVideo(
            platform="bilibili",
            video_id="BV1kdKr6qEMF",
            title="测试视频",
            canonical_url="https://www.bilibili.com/video/BV1kdKr6qEMF/",
            original_url="https://www.bilibili.com/video/BV1kdKr6qEMF/?spm_id_from=1",
        )
    ]
    assert result.platform_counts == {"bilibili": 1}


def test_expands_canonical_douyin_video_without_cookie_preflight() -> None:
    url = "https://www.douyin.com/video/7671100071668413681?modeFrom="

    class FreshCookieYoutubeDL:
        def __enter__(self):
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def extract_info(self, _url: str, download: bool) -> dict:
            assert download is False
            raise RuntimeError("Fresh cookies are needed")

    expander = BatchExpander(ydl_factory=lambda _options: FreshCookieYoutubeDL())

    result = expander.expand([url])

    assert result.videos == [
        ExpandedVideo(
            platform="douyin",
            video_id="7671100071668413681",
            title="",
            canonical_url=url,
            original_url=url,
        )
    ]
    assert result.platform_counts == {"douyin": 1}


def test_expands_youtube_single_without_network_preflight() -> None:
    def unexpected_factory(_options):
        raise AssertionError("YouTube 单视频不应在创建批次时联网预解析")

    expander = BatchExpander(ydl_factory=unexpected_factory)

    result = expander.expand(
        ["https://www.youtube.com/watch?v=BaW_jenozKc"],
        ["https://youtu.be/BaW_jenozKc?si=tracking"],
    )

    assert result.videos == [
        ExpandedVideo(
            platform="youtube",
            video_id="BaW_jenozKc",
            title="",
            canonical_url="https://www.youtube.com/watch?v=BaW_jenozKc",
            original_url="https://youtu.be/BaW_jenozKc?si=tracking",
        )
    ]


def test_accepts_direct_media_candidates_but_rejects_unsupported_kuaishou_pages() -> None:
    assert extractor_supports_url("https://example.com/video/1") is True
    assert extractor_supports_url("http://cdn.example/video.mp4") is True
    assert extractor_supports_url("https://www.kuaishou.com/profile/author-id") is False
    assert extractor_supports_url("https://www.kuaishou.com/f/X109BY66mOaj1nh") is True


def test_accepts_vipshop_product_detail_but_rejects_other_vipshop_pages() -> None:
    assert extractor_supports_url(
        "https://detail.vip.com/detail-10007920-6921967044893585744.html"
    ) is True
    assert extractor_supports_url("https://detail.vip.com/comment/123") is False


def test_expands_vipshop_main_video_with_platform_identity() -> None:
    url = "https://detail.vip.com/detail-10007920-6921967044893585744.html"
    info = {
        "id": "6921967044893585744",
        "title": "测试商品",
        "extractor_key": "Vipshop",
        "webpage_url": url,
        "formats": [{"url": "https://a.vpimg4.com/main.mp4", "vcodec": "h264"}],
    }
    expander = BatchExpander(ydl_factory=lambda _options: FakeYoutubeDL(info))

    result = expander.expand([url])

    assert result.videos == [
        ExpandedVideo(
            platform="vipshop",
            video_id="6921967044893585744",
            title="测试商品",
            canonical_url=url,
            original_url=url,
        )
    ]
    assert result.platform_counts == {"vipshop": 1}


def test_expands_http_direct_media_without_upgrading_canonical_url() -> None:
    url = "http://cdn.example/video.mp4"
    info = {
        "id": "0123456789abcdef",
        "title": "video",
        "extractor_key": "DirectMedia",
        "webpage_url": url,
        "formats": [{"url": url, "vcodec": "unknown"}],
    }
    expander = BatchExpander(ydl_factory=lambda _options: FakeYoutubeDL(info))

    result = expander.expand([url])

    assert result.videos == [
        ExpandedVideo(
            platform="direct",
            video_id="0123456789abcdef",
            title="video",
            canonical_url=url,
            original_url=url,
        )
    ]


def test_rejects_playlist_over_five_hundred_without_partial_result() -> None:
    entries = [
        {
            "id": f"BV{i:010d}",
            "title": f"视频{i}",
            "extractor_key": "BiliBili",
            "url": f"https://www.bilibili.com/video/BV{i:010d}/",
        }
        for i in range(501)
    ]
    info = {"_type": "playlist", "extractor_key": "BiliBili", "entries": entries}
    expander = BatchExpander(ydl_factory=lambda _options: FakeYoutubeDL(info))

    with pytest.raises(BatchExpansionError, match="500"):
        expander.expand(["https://www.bilibili.com/list/watchlater"])


def test_deduplicates_by_platform_and_video_id() -> None:
    infos = iter(
        [
            {
                "id": "same-id",
                "title": "B站",
                "extractor_key": "BiliBili",
                "webpage_url": "https://www.bilibili.com/video/same-id",
                "formats": [{"url": "https://cdn.example/b.mp4", "vcodec": "h264"}],
            },
            {
                "id": "same-id",
                "title": "抖音",
                "extractor_key": "TikTok",
                "webpage_url": "https://www.douyin.com/video/same-id",
                "formats": [{"url": "https://cdn.example/d.mp4", "vcodec": "h264"}],
            },
        ]
    )
    expander = BatchExpander(ydl_factory=lambda _options: FakeYoutubeDL(next(infos)))

    result = expander.expand(
        [
            "https://www.bilibili.com/video/same-id",
            "https://www.douyin.com/video/same-id",
        ]
    )

    assert [(video.platform, video.video_id) for video in result.videos] == [
        ("bilibili", "same-id"),
        ("douyin", "same-id"),
    ]


def test_rejects_live_and_audio_only_results() -> None:
    live = {
        "id": "live-1",
        "title": "直播",
        "extractor_key": "BiliBiliLive",
        "is_live": True,
        "formats": [{"url": "https://cdn.example/live.m3u8", "vcodec": "h264"}],
    }
    expander = BatchExpander(ydl_factory=lambda _options: FakeYoutubeDL(live))
    with pytest.raises(BatchExpansionError, match="直播"):
        expander.expand(["https://live.bilibili.com/123"])

    audio = {
        "id": "audio-1",
        "title": "音频",
        "extractor_key": "Youtube",
        "formats": [{"url": "https://cdn.example/audio.m4a", "vcodec": "none"}],
    }
    expander = BatchExpander(ydl_factory=lambda _options: FakeYoutubeDL(audio))
    with pytest.raises(BatchExpansionError, match="视频"):
        expander.expand(["https://www.youtube.com/watch?v=audio-1"])
