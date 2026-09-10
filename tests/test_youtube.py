from __future__ import annotations

from dataclasses import dataclass

import pytest

from douyin_downloader.youtube import (
    YoutubePageCollector,
    classify_youtube_url,
)


@pytest.mark.parametrize(
    ("url", "kind", "canonical"),
    [
        (
            "https://www.youtube.com/watch?v=BaW_jenozKc&list=PL123&t=4",
            "single",
            "https://www.youtube.com/watch?v=BaW_jenozKc",
        ),
        (
            "https://youtu.be/BaW_jenozKc?si=tracking",
            "single",
            "https://www.youtube.com/watch?v=BaW_jenozKc",
        ),
        (
            "https://www.youtube.com/shorts/BaW_jenozKc?feature=share",
            "single",
            "https://www.youtube.com/shorts/BaW_jenozKc",
        ),
        (
            "https://www.youtube.com/playlist?list=PL123&si=tracking",
            "page",
            "https://www.youtube.com/playlist?list=PL123",
        ),
        (
            "https://youtube.com/@example",
            "page",
            "https://www.youtube.com/@example/videos",
        ),
        (
            "https://youtube.com/channel/UC123/videos?view=0",
            "page",
            "https://www.youtube.com/channel/UC123/videos",
        ),
    ],
)
def test_classifies_and_canonicalizes_supported_urls(url: str, kind: str, canonical: str) -> None:
    result = classify_youtube_url(url)

    assert result is not None
    assert result.kind == kind
    assert result.canonical_url == canonical


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/results?search_query=test",
        "https://www.youtube.com/",
        "https://www.youtube.com/@example/streams",
        "https://www.youtube.com/@example/playlists",
    ],
)
def test_rejects_unsupported_youtube_pages(url: str) -> None:
    assert classify_youtube_url(url).kind == "unsupported"  # type: ignore[union-attr]


class FakeYoutubeDL:
    def __init__(self, entries: list[dict | None]) -> None:
        self.entries = entries
        self.options: dict = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def extract_info(self, url: str, download: bool) -> dict:
        return {"_type": "playlist", "entries": iter(self.entries)}


@dataclass
class FakeControl:
    cancelled: bool = False
    pause_checks: int = 0

    def wait_if_paused(self) -> None:
        self.pause_checks += 1

    def is_cancelled(self) -> bool:
        return self.cancelled


def make_collector(entries: list[dict | None], **kwargs) -> YoutubePageCollector:
    session = FakeYoutubeDL(entries)

    def factory(options: dict):
        session.options = options
        return session

    return YoutubePageCollector(
        ydl_factory=factory,
        runtime_options_provider=lambda: {
            "js_runtimes": {"deno": {"path": "C:/runtime/deno.exe"}},
            "remote_components": set(),
        },
        **kwargs,
    )


def test_page_collector_keeps_order_filters_entries_and_reaches_target() -> None:
    entries = [
        {"id": "AAAAAAAAAAA", "title": "一"},
        {"id": "BBBBBBBBBBB", "title": "直播", "live_status": "is_live"},
        {"id": "CCCCCCCCCCC", "title": "三"},
        {"id": "AAAAAAAAAAA", "title": "重复"},
        {"id": "DDDDDDDDDDD", "title": "四"},
    ]
    accepted = []
    statuses = []

    result = make_collector(entries).collect(
        "https://www.youtube.com/@example/videos",
        3,
        lambda video: accepted.append(video) or True,
        lambda status, reason: statuses.append((status, reason)),
        FakeControl(),
    )

    assert result.status == "target_reached"
    assert [video.video_id for video in accepted] == ["AAAAAAAAAAA", "CCCCCCCCCCC", "DDDDDDDDDDD"]
    assert all(video.original_url.endswith("/@example/videos") for video in accepted)
    assert statuses[0] == ("collecting", None)


def test_history_duplicates_do_not_count_toward_requested_items() -> None:
    entries = [
        {"id": "AAAAAAAAAAA", "title": "历史重复"},
        {"id": "BBBBBBBBBBB", "title": "新视频一"},
        {"id": "CCCCCCCCCCC", "title": "新视频二"},
    ]
    visited = []

    def add(video) -> bool:
        visited.append(video.video_id)
        return video.video_id != "AAAAAAAAAAA"

    result = make_collector(entries).collect(
        "https://www.youtube.com/playlist?list=PL123",
        2,
        add,
        lambda _status, _reason: None,
        FakeControl(),
    )

    assert result.status == "target_reached"
    assert visited == ["AAAAAAAAAAA", "BBBBBBBBBBB", "CCCCCCCCCCC"]


def test_exhausted_page_returns_insufficient_and_keeps_collected_items() -> None:
    accepted = []
    result = make_collector([{"id": "AAAAAAAAAAA", "title": "仅一条"}]).collect(
        "https://www.youtube.com/playlist?list=PL123",
        5,
        lambda video: accepted.append(video) or True,
        lambda _status, _reason: None,
        FakeControl(),
    )

    assert result.status == "insufficient"
    assert result.reason == "仅采集到 1/5 条，页面没有更多可下载的公开视频"
    assert len(accepted) == 1


def test_candidate_safety_limit_returns_insufficient() -> None:
    result = make_collector(
        [
            {"id": "AAAAAAAAAAA", "title": "一"},
            {"id": "BBBBBBBBBBB", "title": "二"},
        ],
        candidate_limit=1,
    ).collect(
        "https://www.youtube.com/@example/videos",
        5,
        lambda _video: True,
        lambda _status, _reason: None,
        FakeControl(),
    )

    assert result.status == "insufficient"
    assert "安全上限" in (result.reason or "")


def test_page_collector_accepts_full_five_hundred_item_target() -> None:
    entries = [
        {"id": f"A{index:010d}", "title": f"视频 {index}"}
        for index in range(500)
    ]
    accepted = []

    result = make_collector(entries).collect(
        "https://www.youtube.com/@example/videos",
        500,
        lambda video: accepted.append(video) or True,
        lambda _status, _reason: None,
        FakeControl(),
    )

    assert result.status == "target_reached"
    assert len(accepted) == 500


def test_page_collector_injects_manual_proxy_option() -> None:
    captured: dict = {}
    session = FakeYoutubeDL([{"id": "AAAAAAAAAAA", "title": "一"}])

    def factory(options: dict):
        captured.update(options)
        return session

    collector = YoutubePageCollector(
        ydl_factory=factory,
        runtime_options_provider=lambda: {},
        network_options_provider=lambda: {"proxy": "socks5://127.0.0.1:7891"},
    )
    collector.collect(
        "https://www.youtube.com/@example/videos",
        1,
        lambda _video: True,
        lambda _status, _reason: None,
        FakeControl(),
    )

    assert captured["proxy"] == "socks5://127.0.0.1:7891"


def test_page_collector_injects_tool_specific_cookie_file() -> None:
    captured: dict = {}
    session = FakeYoutubeDL([{"id": "AAAAAAAAAAA", "title": "一"}])

    def factory(options: dict):
        captured.update(options)
        return session

    collector = YoutubePageCollector(
        ydl_factory=factory,
        runtime_options_provider=lambda: {},
        auth_options_provider=lambda: {"cookiefile": "C:/tool-data/youtube-cookies.txt"},
    )
    collector.collect(
        "https://www.youtube.com/@example/videos",
        1,
        lambda _video: True,
        lambda _status, _reason: None,
        FakeControl(),
    )

    assert captured["cookiefile"] == "C:/tool-data/youtube-cookies.txt"


def test_page_collector_reports_manual_proxy_connection_failure() -> None:
    class FailingSession(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool) -> dict:
            raise RuntimeError("connection refused")

    collector = YoutubePageCollector(
        ydl_factory=lambda _options: FailingSession([]),
        runtime_options_provider=lambda: {},
        network_options_provider=lambda: {"proxy": "http://127.0.0.1:7890"},
    )

    with pytest.raises(RuntimeError, match="本地代理不可用"):
        collector.collect(
            "https://www.youtube.com/@example/videos",
            1,
            lambda _video: True,
            lambda _status, _reason: None,
            FakeControl(),
        )
