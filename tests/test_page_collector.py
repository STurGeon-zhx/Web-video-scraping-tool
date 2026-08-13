import time
from pathlib import Path

from douyin_downloader.page_collector import (
    BrowserPageCollector,
    CollectionOutcome,
    CollectionControl,
    PageCollectionManager,
    PlaywrightBrowserSession,
    douyin_search_mode,
    extract_douyin_card_videos,
    extract_douyin_response_cards,
    extract_generic_media_videos,
)
from douyin_downloader.platforms import ExpandedVideo
from douyin_downloader.store import Database


class WakeQueue:
    def __init__(self) -> None:
        self.wake_calls = 0

    def wake(self) -> None:
        self.wake_calls += 1


class FakeCollector:
    def __init__(self, videos: list[ExpandedVideo], outcome: CollectionOutcome) -> None:
        self.videos = videos
        self.outcome = outcome
        self.calls: list[tuple[str, int]] = []

    def collect(self, source_url, max_items, on_video, on_status, control):
        self.calls.append((source_url, max_items))
        on_status("collecting", None)
        for video in self.videos:
            if control.is_cancelled():
                return CollectionOutcome("stopped", "采集已停止")
            control.wait_if_paused()
            on_video(video)
        return self.outcome


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "page.db")
    database.initialize()
    return database


def wait_until(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition was not reached")


def test_extracts_douyin_cards_in_order_and_skips_non_video_cards() -> None:
    cards = [
        {"href": "https://www.douyin.com/video/111?from=search", "title": "第一条"},
        {"href": "https://live.douyin.com/222", "title": "直播"},
        {"href": "https://www.douyin.com/video/111", "title": "重复"},
        {"href": "/video/333", "title": "广告推广"},
        {"href": "/video/444", "title": "第二条"},
    ]

    videos = extract_douyin_card_videos(
        cards,
        "https://www.douyin.com/search/food",
    )

    assert [video.video_id for video in videos] == ["111", "444"]
    assert [video.title for video in videos] == ["第一条", "第二条"]
    assert videos[0].canonical_url == "https://www.douyin.com/video/111"


def test_manager_collects_incrementally_and_keeps_partial_results(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    batch_id = database.create_page_batch("https://example.com/videos", 5, tmp_path)
    videos = [
        ExpandedVideo("direct", "one", "one", "https://cdn.example.com/1.mp4", "https://example.com/videos"),
        ExpandedVideo("direct", "two", "two", "https://cdn.example.com/2.mp4", "https://example.com/videos"),
    ]
    collector = FakeCollector(videos, CollectionOutcome("page_ended", "页面已结束"))
    queue = WakeQueue()
    manager = PageCollectionManager(database, queue, lambda _url: collector)

    manager.submit(batch_id)
    wait_until(lambda: database.get_batch(batch_id)["collection_status"] == "page_ended")
    manager.stop()

    batch = database.get_batch(batch_id)
    assert batch["collected_count"] == 2
    assert [task["video_id"] for task in batch["tasks"]] == ["one", "two"]
    assert batch["collection_stop_reason"] == "页面已结束"
    assert queue.wake_calls == 2


def test_manager_resumes_pending_page_batches_on_start(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    batch_id = database.create_page_batch("https://example.com/videos", 1, tmp_path)
    video = ExpandedVideo(
        "direct", "one", "one", "https://cdn.example.com/1.mp4", "https://example.com/videos"
    )
    collector = FakeCollector([video], CollectionOutcome("target_reached", None))
    manager = PageCollectionManager(database, WakeQueue(), lambda _url: collector)

    manager.start()
    wait_until(lambda: database.get_batch(batch_id)["collection_status"] == "target_reached")
    manager.stop()

    assert collector.calls == [("https://example.com/videos", 1)]


class FakeBrowserSession:
    def __init__(
        self,
        rounds,
        login_rounds: int = 0,
        risk_round: int | None = None,
        response_rounds=None,
    ) -> None:
        self.rounds = rounds
        self.response_rounds = response_rounds or [[]]
        self.login_rounds = login_rounds
        self.risk_round = risk_round
        self.index = 0
        self.opened: list[str] = []
        self.closed = False
        self.sync_calls = 0
        self.reload_calls = 0

    def open(self, url: str) -> None:
        self.opened.append(url)

    def login_required(self) -> bool:
        return self.index < self.login_rounds

    def risk_controlled(self) -> bool:
        return self.risk_round is not None and self.index >= self.risk_round

    def douyin_cards(self):
        return self.rounds[min(self.index, len(self.rounds) - 1)]

    def douyin_response_cards(self):
        return self.response_rounds[min(self.index, len(self.response_rounds) - 1)]

    def generic_candidates(self):
        return self.rounds[min(self.index, len(self.rounds) - 1)]

    def scroll(self) -> None:
        self.index += 1

    def sync_cookies(self) -> None:
        self.sync_calls += 1

    def wait(self, _milliseconds: int) -> None:
        if self.index < self.login_rounds:
            self.index += 1

    def reload(self) -> None:
        self.reload_calls += 1

    def close(self) -> None:
        self.closed = True


class DelayedLoginSession(FakeBrowserSession):
    def __init__(self) -> None:
        super().__init__(
            rounds=[
                [],
                [],
                [{"href": "/video/111", "title": "登录后出现的视频"}],
            ]
        )
        self.login_checks = iter([False, True, True, False, False])

    def login_required(self) -> bool:
        return next(self.login_checks, False)

    def wait(self, _milliseconds: int) -> None:
        self.index = min(self.index + 1, len(self.rounds) - 1)


class SkeletonThenReloadSession(FakeBrowserSession):
    def __init__(self) -> None:
        super().__init__(rounds=[[], [{"href": "/video/111", "title": "重载后的视频"}]])

    def wait(self, _milliseconds: int) -> None:
        return

    def reload(self) -> None:
        self.reload_calls += 1
        self.index = 1


class VerificationThenVideoSession(FakeBrowserSession):
    def __init__(self) -> None:
        super().__init__(rounds=[[], [], [{"href": "/video/111", "title": "验证后的视频"}]])

    def risk_controlled(self) -> bool:
        return self.index < 2

    def wait(self, _milliseconds: int) -> None:
        self.index = min(self.index + 1, len(self.rounds) - 1)


class SlowAfterLoginSession(FakeBrowserSession):
    def __init__(self) -> None:
        super().__init__(rounds=[[], [], [], [], [{"href": "/video/111", "title": "慢加载视频"}]])

    def login_required(self) -> bool:
        return self.index == 0

    def wait(self, _milliseconds: int) -> None:
        self.index = min(self.index + 1, len(self.rounds) - 1)


def make_control(tmp_path: Path) -> CollectionControl:
    database = make_database(tmp_path)
    batch_id = database.create_page_batch("https://example.com", 5, tmp_path)
    import threading

    return CollectionControl(database, batch_id, threading.Event(), threading.Event())


def test_browser_collector_waits_for_login_then_reaches_requested_count(tmp_path: Path) -> None:
    session = FakeBrowserSession(
        rounds=[
            [],
            [{"href": "/video/111", "title": "第一条"}],
            [
                {"href": "/video/111", "title": "第一条"},
                {"href": "/video/222", "title": "第二条"},
            ],
        ],
        login_rounds=1,
    )
    statuses: list[str] = []
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_login_rounds=3,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food",
        2,
        lambda video: videos.append(video) is None or True,
        lambda status, _reason: statuses.append(status),
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [video.video_id for video in videos] == ["111", "222"]
    assert statuses[:2] == ["waiting_login", "collecting"]
    assert session.sync_calls >= 1
    assert session.closed is True


def test_browser_collector_keeps_douyin_general_search_on_general_tab(tmp_path: Path) -> None:
    session = FakeBrowserSession(
        rounds=[[{"href": "/video/111", "title": "风景视频"}]],
    )
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_login_rounds=3,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/%E9%A3%8E%E6%99%AF?type=general",
        1,
        lambda _video: True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert session.opened == [
        "https://www.douyin.com/search/%E9%A3%8E%E6%99%AF?type=general"
    ]


def test_douyin_search_mode_defaults_to_general_and_rejects_other_tabs() -> None:
    assert douyin_search_mode("https://www.douyin.com/search/风景") == "general"
    assert douyin_search_mode("https://www.douyin.com/search/风景?type=general") == "general"
    assert douyin_search_mode("https://www.douyin.com/search/风景?type=video") == "video"

    try:
        douyin_search_mode("https://www.douyin.com/search/风景?type=user")
    except ValueError as exc:
        assert str(exc) == "不支持的抖音搜索类型: user"
    else:
        raise AssertionError("type=user should be rejected")


def test_extract_douyin_general_response_filters_non_video_results_in_order() -> None:
    payload = {
        "data": [
            {
                "type": 1,
                "aweme_info": {
                    "aweme_id": "111",
                    "desc": "第一条普通视频",
                    "video": {"play_addr": {}},
                },
            },
            {
                "type": 1,
                "aweme_info": {
                    "aweme_id": "222",
                    "desc": "图集",
                    "video": {"play_addr": {}},
                    "images": [{"url_list": []}],
                },
            },
            {
                "type": 1,
                "aweme_info": {
                    "aweme_id": "333",
                    "desc": "直播",
                    "video": {"play_addr": {}},
                    "live_room": {"room_id": "3"},
                },
            },
            {
                "type": 1,
                "aweme_info": {
                    "aweme_id": "444",
                    "desc": "广告",
                    "video": {"play_addr": {}},
                    "raw_ad_data": "{}",
                },
            },
            {"type": 2, "user_info": {"uid": "user"}},
            {
                "type": 1,
                "aweme_info": {
                    "aweme_id": "555",
                    "desc": "第二条普通视频",
                    "video": {"play_addr": {}},
                },
            },
            {
                "nested": {
                    "aweme_info": {
                        "aweme_id": "111",
                        "desc": "嵌套重复",
                        "video": {"play_addr": {}},
                    }
                }
            },
        ]
    }

    cards = extract_douyin_response_cards(payload)

    assert cards == [
        {"href": "https://www.douyin.com/video/111", "title": "第一条普通视频"},
        {"href": "https://www.douyin.com/video/555", "title": "第二条普通视频"},
    ]


def test_general_search_uses_dom_before_response_fallback(tmp_path: Path) -> None:
    session = FakeBrowserSession(
        rounds=[[{"href": "/video/111", "title": "DOM 第一条"}]],
        response_rounds=[[
            {"href": "/video/111", "title": "响应重复"},
            {"href": "/video/222", "title": "响应第二条"},
        ]],
    )
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_login_rounds=3,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/风景?type=general",
        2,
        lambda video: videos.append(video) is None or True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [(video.video_id, video.title) for video in videos] == [
        ("111", "DOM 第一条"),
        ("222", "响应第二条"),
    ]


def test_browser_collector_does_not_close_when_login_prompt_appears_after_initial_render(tmp_path: Path) -> None:
    session = DelayedLoginSession()
    videos: list[ExpandedVideo] = []
    statuses: list[str] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_login_rounds=8,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food",
        1,
        lambda video: videos.append(video) is None or True,
        lambda status, _reason: statuses.append(status),
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [video.video_id for video in videos] == ["111"]
    assert statuses.count("waiting_login") >= 1
    assert statuses[-1] == "collecting"


def test_browser_collector_reloads_once_when_douyin_stays_on_skeleton_screen(tmp_path: Path) -> None:
    session = SkeletonThenReloadSession()
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_login_rounds=6,
        initial_reload_rounds=2,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food",
        1,
        lambda video: videos.append(video) is None or True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [video.video_id for video in videos] == ["111"]
    assert session.reload_calls == 1


def test_browser_collector_keeps_verification_window_open_until_video_appears(tmp_path: Path) -> None:
    session = VerificationThenVideoSession()
    videos: list[ExpandedVideo] = []
    statuses: list[str] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_login_rounds=6,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food",
        1,
        lambda video: videos.append(video) is None or True,
        lambda status, _reason: statuses.append(status),
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [video.video_id for video in videos] == ["111"]
    assert "waiting_verification" in statuses
    assert session.closed is True


def test_browser_collector_waits_for_slow_video_render_after_login(tmp_path: Path) -> None:
    session = SlowAfterLoginSession()
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_login_rounds=8,
        first_video_rounds=6,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food",
        1,
        lambda video: videos.append(video) is None or True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [video.video_id for video in videos] == ["111"]


def test_browser_collector_keeps_partial_results_when_risk_control_appears(tmp_path: Path) -> None:
    session = FakeBrowserSession(
        rounds=[[{"href": "/video/111", "title": "第一条"}]],
        risk_round=1,
    )
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_idle_rounds=3,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food",
        5,
        lambda video: videos.append(video) is None or True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome.status == "risk_controlled"
    assert [video.video_id for video in videos] == ["111"]


def test_extracts_generic_public_media_candidates_and_deduplicates() -> None:
    videos = extract_generic_media_videos(
        [
            {"url": "https://cdn.example.com/a.mp4", "title": "A"},
            {"url": "https://cdn.example.com/a.mp4", "title": "重复"},
            {"url": "https://cdn.example.com/master.m3u8", "title": "HLS"},
            {"url": "blob:https://example.com/private", "title": "blob"},
        ],
        "https://example.com/videos",
    )

    assert [video.title for video in videos] == ["A", "HLS"]
    assert all(video.platform == "direct" for video in videos)
    assert len({video.video_id for video in videos}) == 2


class RaisingLocator:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def evaluate_all(self, _script: str):
        raise self.error


class LocatorPage:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def locator(self, _selector: str) -> RaisingLocator:
        return RaisingLocator(self.error)


def test_douyin_cards_treats_navigation_context_loss_as_temporary_empty_result() -> None:
    session = PlaywrightBrowserSession(Path("browser"))
    session._page = LocatorPage(  # type: ignore[assignment]
        RuntimeError("Execution context was destroyed, most likely because of a navigation")
    )

    assert session.douyin_cards() == []


def test_douyin_cards_does_not_hide_unrelated_dom_errors() -> None:
    session = PlaywrightBrowserSession(Path("browser"))
    session._page = LocatorPage(RuntimeError("selector engine crashed"))  # type: ignore[assignment]

    try:
        session.douyin_cards()
    except RuntimeError as exc:
        assert str(exc) == "selector engine crashed"
    else:
        raise AssertionError("unrelated DOM error should be raised")
