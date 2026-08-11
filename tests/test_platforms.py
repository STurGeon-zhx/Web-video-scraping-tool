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


def test_rejects_generic_only_url_without_fetching_it() -> None:
    assert extractor_supports_url("https://example.com/video/1") is False
    assert extractor_supports_url("https://www.kuaishou.com/profile/author-id") is False
    assert extractor_supports_url("https://www.kuaishou.com/f/X109BY66mOaj1nh") is True


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
