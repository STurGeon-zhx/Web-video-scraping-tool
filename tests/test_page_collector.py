import time
from pathlib import Path

import pytest

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
    is_douyin_search_url,
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


def test_jingxuan_search_redirect_is_still_a_douyin_search_page() -> None:
    url = "https://www.douyin.com/jingxuan/search/landscape?type=general"

    assert is_douyin_search_url(url) is True
    assert douyin_search_mode(url) == "general"


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


def test_manager_uses_one_controlled_collection_worker_for_pending_batches(
    tmp_path: Path,
) -> None:
    import threading

    database = make_database(tmp_path)
    batch_ids = [
        database.create_page_batch(f"https://example.com/{index}", 1, tmp_path)
        for index in range(3)
    ]
    first_started = threading.Event()
    release_first = threading.Event()

    class SequentialCollector:
        def __init__(self) -> None:
            self.calls = 0

        def collect(self, source_url, max_items, on_video, on_status, control):
            self.calls += 1
            on_status("collecting", None)
            if self.calls == 1:
                first_started.set()
                assert release_first.wait(2)
            return CollectionOutcome("page_ended", "done")

    collector = SequentialCollector()
    manager = PageCollectionManager(database, WakeQueue(), lambda _url: collector)
    for batch_id in batch_ids:
        manager.submit(batch_id)

    assert first_started.wait(1)
    collector_threads = [
        thread
        for thread in threading.enumerate()
        if thread.name.startswith("page-collector-")
    ]
    assert len(collector_threads) == 1
    release_first.set()
    wait_until(
        lambda: all(
            database.get_batch(batch_id)["collection_status"] == "page_ended"
            for batch_id in batch_ids
        )
    )
    manager.stop()


def test_manager_starts_next_batch_after_previous_collector_fails(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    first_batch = database.create_page_batch("https://example.com/first", 1, tmp_path)
    second_batch = database.create_page_batch("https://example.com/second", 1, tmp_path)

    class FailThenSucceedCollector:
        def collect(self, source_url, max_items, on_video, on_status, control):
            on_status("collecting", None)
            if source_url.endswith("/first"):
                raise RuntimeError("first batch failed")
            return CollectionOutcome("page_ended", "done")

    manager = PageCollectionManager(
        database,
        WakeQueue(),
        lambda _url: FailThenSucceedCollector(),
    )
    manager.submit(first_batch)
    manager.submit(second_batch)

    wait_until(lambda: database.get_batch(first_batch)["collection_status"] == "stopped")
    wait_until(lambda: database.get_batch(second_batch)["collection_status"] == "page_ended")
    manager.stop()


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
        self.persist_calls = 0
        self.elapsed_seconds = 0.0
        self.bottom = True
        self.exhausted = False

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
        self.elapsed_seconds += _milliseconds / 1000
        if self.index < self.login_rounds:
            self.index += 1

    def reload(self) -> None:
        self.reload_calls += 1

    def close(self) -> None:
        self.closed = True

    def persist_state(self) -> None:
        self.persist_calls += 1
        self.sync_cookies()

    def at_page_bottom(self) -> bool:
        return self.bottom

    def page_exhausted(self) -> bool:
        return self.exhausted

    def loaded_saved_state(self) -> bool:
        return False


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


class CleanRecoverySession(FakeBrowserSession):
    def __init__(self) -> None:
        super().__init__(rounds=[[]])
        self.reopen_calls: list[tuple[str, bool, bool]] = []
        self.recovered = False

    def douyin_cards(self):
        if self.recovered:
            return [{"href": "/video/777", "title": "recovered"}]
        return []

    def reopen(
        self,
        url: str,
        *,
        clean_state: bool,
        software_rendering: bool,
    ) -> None:
        self.reopen_calls.append((url, clean_state, software_rendering))
        self.recovered = True


class StaleSavedStateSession(FakeBrowserSession):
    def __init__(self) -> None:
        super().__init__(rounds=[[]])
        self.login_checks = iter([True, False, False, False, False])
        self.reopen_calls: list[tuple[str, bool, bool]] = []
        self.recovered = False

    def login_required(self) -> bool:
        if self.recovered:
            return False
        return next(self.login_checks, False)

    def douyin_cards(self):
        if self.recovered:
            return [{"href": "/video/888", "title": "fresh session video"}]
        return []

    def loaded_saved_state(self) -> bool:
        return not self.recovered

    def reopen(
        self,
        url: str,
        *,
        clean_state: bool,
        software_rendering: bool,
    ) -> None:
        self.reopen_calls.append((url, clean_state, software_rendering))
        self.recovered = True


class LoggedOutSavedStateSession(FakeBrowserSession):
    def __init__(self) -> None:
        super().__init__(rounds=[[]])
        self.recovered = False
        self.reopen_calls: list[tuple[str, bool, bool]] = []

    def login_required(self) -> bool:
        return not self.recovered

    def douyin_cards(self):
        if self.recovered:
            return [{"href": "/video/999", "title": "clean login video"}]
        return []

    def loaded_saved_state(self) -> bool:
        return not self.recovered

    def reopen(
        self,
        url: str,
        *,
        clean_state: bool,
        software_rendering: bool,
    ) -> None:
        self.reopen_calls.append((url, clean_state, software_rendering))
        self.recovered = True


class ProgressiveVideoSession(FakeBrowserSession):
    def __init__(self, total: int, page_size: int = 25) -> None:
        super().__init__(rounds=[[]])
        self.total = total
        self.page_size = page_size

    def douyin_cards(self):
        visible = min(self.total, (self.index + 1) * self.page_size)
        return [
            {"href": f"/video/{index + 1}", "title": f"video {index + 1}"}
            for index in range(visible)
        ]

    def page_exhausted(self) -> bool:
        return (self.index + 1) * self.page_size >= self.total


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
    assert session.persist_calls == 2
    assert session.closed is True


def test_browser_collector_persists_latest_state_when_collection_finishes(
    tmp_path: Path,
) -> None:
    session = FakeBrowserSession(
        rounds=[[{"href": "/video/111", "title": "video"}]],
    )
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food?type=general",
        1,
        lambda _video: True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert session.persist_calls == 2


def test_browser_collector_reopens_clean_session_when_saved_state_stays_empty(
    tmp_path: Path,
) -> None:
    session = StaleSavedStateSession()
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_login_rounds=5,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food?type=general",
        1,
        lambda video: videos.append(video) is None or True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [video.video_id for video in videos] == ["888"]
    assert session.reopen_calls == [
        ("https://www.douyin.com/search/food?type=general", True, False)
    ]


def test_browser_collector_reopens_clean_session_when_saved_state_is_logged_out(
    tmp_path: Path,
) -> None:
    session = LoggedOutSavedStateSession()
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_login_rounds=5,
        first_video_rounds=2,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food?type=general",
        1,
        lambda video: videos.append(video) is None or True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [video.video_id for video in videos] == ["999"]
    assert session.reopen_calls == [
        ("https://www.douyin.com/search/food?type=general", True, False)
    ]


@pytest.mark.parametrize("requested_count", [50, 100, 500])
def test_browser_collector_continues_scrolling_for_large_batches(
    tmp_path: Path,
    requested_count: int,
) -> None:
    session = ProgressiveVideoSession(requested_count)
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/landscape?type=video",
        requested_count,
        lambda video: videos.append(video) is None or True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert len(videos) == requested_count
    assert len({video.video_id for video in videos}) == requested_count


def test_browser_collector_writes_terminal_status_before_browser_cleanup(
    tmp_path: Path,
) -> None:
    events: list[str] = []

    class OrderedCloseSession(FakeBrowserSession):
        def close(self) -> None:
            events.append("close")
            super().close()

    session = OrderedCloseSession(
        rounds=[[{"href": "/video/111", "title": "video"}]],
    )
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_idle_rounds=2,
    )

    collector.collect(
        "https://www.douyin.com/search/food?type=video",
        1,
        lambda _video: True,
        lambda status, _reason: events.append(status),
        make_control(tmp_path),
    )

    assert events[-2:] == ["target_reached", "close"]


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


def test_browser_collector_uses_douyin_ids_for_jingxuan_search_redirect(
    tmp_path: Path,
) -> None:
    session = FakeBrowserSession(
        rounds=[
            [
                {"href": "/video/333", "title": "first"},
                {"href": "/video/333", "title": "duplicate"},
                {"href": "/video/444", "title": "second"},
            ]
        ],
    )
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/jingxuan/search/landscape?type=general",
        2,
        lambda video: videos.append(video) is None or True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [(video.platform, video.video_id) for video in videos] == [
        ("douyin", "333"),
        ("douyin", "444"),
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
    assert statuses[-1] == "target_reached"


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


def test_browser_collector_reopens_clean_context_when_saved_state_has_no_videos(
    tmp_path: Path,
) -> None:
    session = CleanRecoverySession()
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_login_rounds=8,
        initial_reload_rounds=1,
        first_video_rounds=3,
        max_idle_rounds=2,
    )
    source_url = "https://www.douyin.com/search/food?type=video"

    outcome = collector.collect(
        source_url,
        1,
        lambda video: videos.append(video) is None or True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [video.video_id for video in videos] == ["777"]
    assert session.reopen_calls == [(source_url, True, False)]


class LateSecondVideoSession(FakeBrowserSession):
    def __init__(self) -> None:
        super().__init__(rounds=[[]])

    def douyin_cards(self):
        cards = [{"href": "/video/111", "title": "first"}]
        if self.elapsed_seconds >= 29:
            cards.append({"href": "/video/222", "title": "second"})
        return cards

    def scroll(self) -> None:
        return


def test_browser_collector_does_not_end_before_thirty_seconds_without_progress(
    tmp_path: Path,
) -> None:
    session = LateSecondVideoSession()
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=1_000,
        idle_timeout_seconds=30,
        bottom_confirmations=3,
        clock=lambda: session.elapsed_seconds,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food?type=video",
        2,
        lambda video: videos.append(video) is None or True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [video.video_id for video in videos] == ["111", "222"]


def test_browser_collector_does_not_end_on_has_more_false_while_finding_videos(
    tmp_path: Path,
) -> None:
    session = FakeBrowserSession(
        rounds=[
            [{"href": "/video/111", "title": "first"}],
            [
                {"href": "/video/111", "title": "first"},
                {"href": "/video/222", "title": "second"},
            ],
        ],
    )
    session.exhausted = True
    videos: list[ExpandedVideo] = []
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=0,
        max_idle_rounds=2,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food?type=general",
        2,
        lambda video: videos.append(video) is None or True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome == CollectionOutcome("target_reached", None)
    assert [video.video_id for video in videos] == ["111", "222"]


def test_browser_collector_requires_three_bottom_checks_after_idle_timeout(
    tmp_path: Path,
) -> None:
    session = FakeBrowserSession(
        rounds=[[{"href": "/video/111", "title": "only"}]],
    )
    collector = BrowserPageCollector(
        session_factory=lambda: session,
        wait_milliseconds=10_000,
        idle_timeout_seconds=30,
        bottom_confirmations=3,
        clock=lambda: session.elapsed_seconds,
    )

    outcome = collector.collect(
        "https://www.douyin.com/search/food?type=video",
        2,
        lambda _video: True,
        lambda _status, _reason: None,
        make_control(tmp_path),
    )

    assert outcome.status == "page_ended"
    assert session.elapsed_seconds >= 50


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


class ReturningLocator:
    def __init__(self, cards: list[dict[str, str]]) -> None:
        self.cards = cards
        self.script = ""

    def evaluate_all(self, script: str):
        self.script = script
        return self.cards


class SearchCardPage:
    def __init__(self, cards: list[dict[str, str]]) -> None:
        self.selector = ""
        self.locator_result = ReturningLocator(cards)

    def locator(self, selector: str) -> ReturningLocator:
        self.selector = selector
        return self.locator_result


def test_douyin_cards_reads_new_general_search_react_cards_in_dom_order() -> None:
    page = SearchCardPage(
        [
            {
                "href": "https://www.douyin.com/video/111",
                "title": "first",
            },
            {
                "href": "https://www.douyin.com/video/222",
                "title": "second",
            },
        ]
    )
    session = PlaywrightBrowserSession(Path("browser"))
    session._page = page  # type: ignore[assignment]

    assert session.douyin_cards() == page.locator_result.cards
    assert ".search-result-card" in page.selector
    assert "__react" in page.locator_result.script
    assert "awemeInfo" in page.locator_result.script


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


def test_douyin_cards_reopens_once_with_software_rendering_after_browser_closes() -> None:
    session = PlaywrightBrowserSession(Path("browser"))
    session._page = LocatorPage(  # type: ignore[assignment]
        RuntimeError("Target page, context or browser has been closed")
    )
    session._last_url = "https://www.douyin.com/search/food?type=video"
    reopen_calls: list[tuple[str, bool, bool]] = []

    def reopen(url: str, *, clean_state: bool, software_rendering: bool) -> None:
        reopen_calls.append((url, clean_state, software_rendering))
        session._page = LocatorPage(RuntimeError("navigation in progress"))  # type: ignore[assignment]

    session.reopen = reopen  # type: ignore[method-assign]

    assert session.douyin_cards() == []
    assert reopen_calls == [
        ("https://www.douyin.com/search/food?type=video", False, True)
    ]


def test_douyin_cards_stops_after_second_browser_close() -> None:
    session = PlaywrightBrowserSession(Path("browser"))
    session._page = LocatorPage(  # type: ignore[assignment]
        RuntimeError("Target page, context or browser has been closed")
    )
    session._last_url = "https://www.douyin.com/search/food?type=video"
    session._browser_retry_used = True

    try:
        session.douyin_cards()
    except RuntimeError as exc:
        assert "验证浏览器意外退出" in str(exc)
    else:
        raise AssertionError("the second browser close must stop collection")


class JsonResponse:
    def __init__(self, url: str, payload: object) -> None:
        self.url = url
        self.headers = {"content-type": "application/json"}
        self._payload = payload

    def json(self):
        return self._payload


class FinishedRequest:
    def __init__(self, response: JsonResponse) -> None:
        self._response = response

    def response(self) -> JsonResponse:
        return self._response


def test_playwright_session_captures_video_search_responses_and_end_marker(
    tmp_path: Path,
) -> None:
    session = PlaywrightBrowserSession(tmp_path)
    session._capture_response(  # type: ignore[attr-defined]
        JsonResponse(
            "https://www.douyin.com/aweme/v1/web/search/item/?keyword=food",
            {
                "data": [
                    {
                        "aweme_info": {
                            "aweme_id": "987",
                            "desc": "video result",
                            "video": {"play_addr": {}},
                        }
                    }
                ],
                "has_more": 0,
            },
        )
    )

    assert session.douyin_response_cards() == [
        {"href": "https://www.douyin.com/video/987", "title": "video result"}
    ]
    assert session.page_exhausted() is True


def test_playwright_session_retries_search_capture_after_response_body_finishes(
    tmp_path: Path,
) -> None:
    page = BrowserPage()
    context = BrowserContext(page)
    browser = BrowserProcess(context)
    starter = PlaywrightStarter(ChromiumLauncher(browser))
    session = PlaywrightBrowserSession(tmp_path, playwright_factory=lambda: starter)
    session.open("https://www.douyin.com/search/food?type=general")
    response = JsonResponse(
        "https://www.douyin.com/aweme/v1/web/general/search/single/?keyword=food",
        {
            "data": [
                {
                    "aweme_info": {
                        "aweme_id": "654",
                        "desc": "finished response video",
                        "video": {"play_addr": {}},
                    }
                }
            ]
        },
    )

    handler = page.handlers["requestfinished"]
    handler(FinishedRequest(response))  # type: ignore[operator]

    assert session.douyin_response_cards() == [
        {
            "href": "https://www.douyin.com/video/654",
            "title": "finished response video",
        }
    ]


class BrowserPage:
    def __init__(self) -> None:
        self.goto_calls: list[str] = []
        self.handlers: dict[str, object] = {}

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    def goto(self, url: str, **_kwargs) -> None:
        self.goto_calls.append(url)


class BrowserContext:
    def __init__(self, page: BrowserPage) -> None:
        self.pages: list[BrowserPage] = []
        self.page = page
        self.closed = False

    def new_page(self) -> BrowserPage:
        self.pages.append(self.page)
        return self.page

    def cookies(self, _urls):
        return []

    def close(self) -> None:
        self.closed = True

    def storage_state(self, *, path: str) -> None:
        Path(path).write_text('{"cookies": [], "origins": []}', encoding="utf-8")


class BrowserProcess:
    def __init__(self, context: BrowserContext) -> None:
        self.context = context
        self.new_context_kwargs: dict[str, object] | None = None
        self.closed = False

    def new_context(self, **kwargs) -> BrowserContext:
        self.new_context_kwargs = kwargs
        return self.context

    def close(self) -> None:
        self.closed = True


class ChromiumLauncher:
    def __init__(self, browser: BrowserProcess) -> None:
        self.browser = browser
        self.launch_kwargs: dict[str, object] | None = None

    def launch(self, **kwargs) -> BrowserProcess:
        self.launch_kwargs = kwargs
        return self.browser

    def launch_persistent_context(self, *_args, **_kwargs):
        raise AssertionError("persistent browser profiles must not be reused")


class PlaywrightStarter:
    def __init__(self, chromium: ChromiumLauncher) -> None:
        self.chromium = chromium
        self.stopped = False

    def start(self):
        return self

    def stop(self) -> None:
        self.stopped = True


def test_playwright_session_uses_fresh_context_with_saved_login_state(tmp_path: Path) -> None:
    storage_state = tmp_path / "page-login-state.json"
    storage_state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    page = BrowserPage()
    context = BrowserContext(page)
    browser = BrowserProcess(context)
    chromium = ChromiumLauncher(browser)
    starter = PlaywrightStarter(chromium)
    session = PlaywrightBrowserSession(tmp_path, playwright_factory=lambda: starter)

    session.open("https://www.douyin.com/search/food?type=video")

    assert chromium.launch_kwargs == {"headless": False}
    assert browser.new_context_kwargs == {"storage_state": str(storage_state)}
    assert page.goto_calls == ["https://www.douyin.com/search/food?type=video"]


def test_playwright_session_persists_login_state_atomically(tmp_path: Path) -> None:
    page = BrowserPage()
    context = BrowserContext(page)
    browser = BrowserProcess(context)
    starter = PlaywrightStarter(ChromiumLauncher(browser))
    session = PlaywrightBrowserSession(tmp_path, playwright_factory=lambda: starter)
    session.open("https://www.douyin.com/search/food?type=video")

    session.persist_state()

    assert (tmp_path / "page-login-state.json").read_text(encoding="utf-8") == (
        '{"cookies": [], "origins": []}'
    )
    assert not (tmp_path / "page-login-state.json.tmp").exists()


class FailingClose:
    def close(self) -> None:
        raise RuntimeError("Target page, context or browser has been closed")


def test_playwright_session_cleanup_does_not_replace_collection_outcome(tmp_path: Path) -> None:
    starter = PlaywrightStarter(ChromiumLauncher(BrowserProcess(BrowserContext(BrowserPage()))))
    session = PlaywrightBrowserSession(tmp_path, playwright_factory=lambda: starter)
    session._context = FailingClose()  # type: ignore[assignment]
    session._browser = FailingClose()  # type: ignore[assignment]
    session._playwright = starter

    session.close()

    assert starter.stopped is True
