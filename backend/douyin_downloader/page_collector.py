from __future__ import annotations

import re
import hashlib
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import parse_qs, urljoin, urlsplit

from .cookies import write_netscape_cookie_file
from .links import is_public_http_url
from .platforms import ExpandedVideo
from .store import Database


DOUYIN_VIDEO_PATTERN = re.compile(r"/video/(\d+)")
MEDIA_PATH_PATTERN = re.compile(r"\.(?:mp4|webm|mov|mkv|avi|m3u8|mpd)(?:$|[?#])", re.I)
MANIFEST_CONTENT_TYPES = {
    "application/vnd.apple.mpegurl",
    "application/x-mpegurl",
    "application/dash+xml",
}


@dataclass(frozen=True, slots=True)
class CollectionOutcome:
    status: str
    reason: str | None = None


class CollectionControl:
    def __init__(
        self,
        database: Database,
        batch_id: int,
        cancel_event: threading.Event,
        application_stop: threading.Event,
    ) -> None:
        self._database = database
        self._batch_id = batch_id
        self._cancel_event = cancel_event
        self._application_stop = application_stop

    def is_cancelled(self) -> bool:
        return self._cancel_event.is_set() or self._application_stop.is_set()

    def wait_if_paused(self) -> None:
        while not self.is_cancelled():
            try:
                paused = bool(self._database.get_batch(self._batch_id)["paused"])
            except KeyError:
                return
            if not paused:
                return
            self._cancel_event.wait(0.1)


class PageCollector(Protocol):
    def collect(
        self,
        source_url: str,
        max_items: int,
        on_video: Callable[[ExpandedVideo], bool],
        on_status: Callable[[str, str | None], None],
        control: CollectionControl,
    ) -> CollectionOutcome: ...


class QueueWakeable(Protocol):
    def wake(self) -> None: ...


def extract_douyin_card_videos(
    cards: list[dict[str, str]],
    source_url: str,
) -> list[ExpandedVideo]:
    videos: list[ExpandedVideo] = []
    seen: set[str] = set()
    for card in cards:
        href = urljoin(source_url, str(card.get("href") or ""))
        match = DOUYIN_VIDEO_PATTERN.search(href)
        title = str(card.get("title") or "").strip()
        if match is None or any(word in title for word in ("直播", "广告", "推广")):
            continue
        video_id = match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        videos.append(
            ExpandedVideo(
                platform="douyin",
                video_id=video_id,
                title=title,
                canonical_url=f"https://www.douyin.com/video/{video_id}",
                original_url=source_url,
            )
        )
    return videos


def extract_douyin_response_cards(payload: object) -> list[dict[str, str]]:
    cards: list[dict[str, str]] = []
    seen: set[str] = set()

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            nested = value.get("aweme_info")
            info = nested if isinstance(nested, Mapping) else value
            video_id = str(info.get("aweme_id") or "")
            has_video = isinstance(info.get("video"), Mapping)
            has_images = bool(info.get("images") or info.get("image_post_info"))
            is_live = bool(
                info.get("is_live")
                or info.get("live_room")
                or info.get("room_id")
            )
            is_ad = bool(
                info.get("is_ads")
                or info.get("is_ad")
                or info.get("raw_ad_data")
            )
            if video_id and has_video and not has_images and not is_live and not is_ad:
                if video_id not in seen:
                    seen.add(video_id)
                    cards.append(
                        {
                            "href": f"https://www.douyin.com/video/{video_id}",
                            "title": str(info.get("desc") or "").strip(),
                        }
                    )
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return cards


def extract_generic_media_videos(
    candidates: list[dict[str, str]],
    source_url: str,
) -> list[ExpandedVideo]:
    videos: list[ExpandedVideo] = []
    seen: set[str] = set()
    for candidate in candidates:
        url = urljoin(source_url, str(candidate.get("url") or ""))
        content_type = str(candidate.get("content_type") or "").partition(";")[0].lower()
        if not is_public_http_url(url):
            continue
        if not (
            content_type.startswith("video/")
            or content_type in MANIFEST_CONTENT_TYPES
            or MEDIA_PATH_PATTERN.search(url)
        ):
            continue
        if url in seen:
            continue
        seen.add(url)
        video_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        title = str(candidate.get("title") or "").strip() or f"页面视频_{video_id}"
        videos.append(
            ExpandedVideo(
                platform="direct",
                video_id=video_id,
                title=title,
                canonical_url=url,
                original_url=source_url,
            )
        )
    return videos


def is_douyin_search_url(url: str) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    return (
        (hostname == "douyin.com" or hostname.endswith(".douyin.com"))
        and parsed.path.startswith(("/search/", "/root/search/"))
    )


def douyin_search_mode(url: str) -> str:
    if not is_douyin_search_url(url):
        raise ValueError("不是抖音搜索页面")
    parsed = urlsplit(url)
    mode = (parse_qs(parsed.query).get("type") or ["general"])[0].lower()
    if mode not in {"general", "video"}:
        raise ValueError(f"不支持的抖音搜索类型: {mode}")
    return mode


class BrowserSession(Protocol):
    def open(self, url: str) -> None: ...
    def login_required(self) -> bool: ...
    def risk_controlled(self) -> bool: ...
    def douyin_cards(self) -> list[dict[str, str]]: ...
    def douyin_response_cards(self) -> list[dict[str, str]]: ...
    def generic_candidates(self) -> list[dict[str, str]]: ...
    def sync_cookies(self) -> None: ...
    def reload(self) -> None: ...
    def scroll(self) -> None: ...
    def wait(self, milliseconds: int) -> None: ...
    def close(self) -> None: ...


class BrowserPageCollector:
    def __init__(
        self,
        session_factory: Callable[[], BrowserSession],
        wait_milliseconds: int = 1_000,
        max_login_rounds: int = 600,
        initial_reload_rounds: int = 10,
        first_video_rounds: int = 30,
        max_idle_rounds: int = 5,
    ) -> None:
        self._session_factory = session_factory
        self._wait_milliseconds = wait_milliseconds
        self._max_login_rounds = max_login_rounds
        self._initial_reload_rounds = initial_reload_rounds
        self._first_video_rounds = first_video_rounds
        self._max_idle_rounds = max_idle_rounds

    def collect(
        self,
        source_url: str,
        max_items: int,
        on_video: Callable[[ExpandedVideo], bool],
        on_status: Callable[[str, str | None], None],
        control: CollectionControl,
    ) -> CollectionOutcome:
        session = self._session_factory()
        douyin_search = is_douyin_search_url(source_url)
        search_mode = douyin_search_mode(source_url) if douyin_search else None
        discovered: set[tuple[str, str]] = set()
        idle_rounds = 0

        def wait_for_verification() -> CollectionOutcome | None:
            on_status("waiting_verification", "请在验证页面中完成操作")
            for _ in range(self._max_login_rounds):
                if control.is_cancelled():
                    return CollectionOutcome("stopped", "采集已停止")
                if not session.risk_controlled():
                    return None
                session.wait(self._wait_milliseconds)
            return CollectionOutcome("risk_controlled", "等待页面验证超时")

        try:
            session.open(source_url)
            if douyin_search:
                on_status("waiting_login", None)
                saw_login_prompt = False
                reloaded_initial_page = False
                for round_index in range(self._max_login_rounds):
                    if control.is_cancelled():
                        return CollectionOutcome("stopped", "采集已停止")
                    if session.risk_controlled():
                        verification_outcome = wait_for_verification()
                        if verification_outcome is not None:
                            return verification_outcome
                        continue
                    login_required = session.login_required()
                    if login_required:
                        saw_login_prompt = True
                    has_video_cards = bool(session.douyin_cards()) or bool(
                        search_mode == "general" and session.douyin_response_cards()
                    )
                    if has_video_cards or (saw_login_prompt and not login_required):
                        break
                    if (
                        not reloaded_initial_page
                        and not saw_login_prompt
                        and not login_required
                        and round_index + 1 >= self._initial_reload_rounds
                    ):
                        session.reload()
                        reloaded_initial_page = True
                    session.wait(self._wait_milliseconds)
                else:
                    return CollectionOutcome("login_expired", "等待网页登录超时")
                session.sync_cookies()
            on_status("collecting", None)
            while not control.is_cancelled():
                control.wait_if_paused()
                if control.is_cancelled():
                    return CollectionOutcome("stopped", "采集已停止")
                if session.risk_controlled():
                    verification_outcome = wait_for_verification()
                    if verification_outcome is not None:
                        return verification_outcome
                    on_status("collecting", None)
                    continue
                if douyin_search and session.login_required():
                    on_status("waiting_login", None)
                    for _ in range(self._max_login_rounds):
                        if control.is_cancelled():
                            return CollectionOutcome("stopped", "采集已停止")
                        if session.risk_controlled():
                            verification_outcome = wait_for_verification()
                            if verification_outcome is not None:
                                return verification_outcome
                            continue
                        if not session.login_required():
                            session.sync_cookies()
                            on_status("collecting", None)
                            break
                        session.wait(self._wait_milliseconds)
                    else:
                        return CollectionOutcome("login_expired", "等待网页登录超时")
                    continue
                if douyin_search:
                    cards = list(session.douyin_cards())
                    if search_mode == "general":
                        cards.extend(session.douyin_response_cards())
                    raw_videos = extract_douyin_card_videos(cards, source_url)
                else:
                    raw_videos = extract_generic_media_videos(
                        session.generic_candidates(), source_url
                    )
                added = 0
                for video in raw_videos:
                    key = (video.platform, video.video_id)
                    if key in discovered:
                        continue
                    discovered.add(key)
                    if on_video(video):
                        added += 1
                    if len(discovered) >= max_items:
                        return CollectionOutcome("target_reached", None)
                idle_rounds = 0 if added else idle_rounds + 1
                idle_limit = self._first_video_rounds if douyin_search and not discovered else self._max_idle_rounds
                if idle_rounds >= idle_limit:
                    if discovered:
                        return CollectionOutcome("page_ended", "页面已结束或没有更多视频")
                    reason = (
                        "综合页未发现视频，页面结构可能已变化"
                        if search_mode == "general"
                        else "页面中没有发现可下载视频"
                    )
                    return CollectionOutcome("no_videos", reason)
                session.scroll()
                session.wait(self._wait_milliseconds)
            return CollectionOutcome("stopped", "采集已停止")
        finally:
            session.close()


class PlaywrightBrowserSession:
    def __init__(self, browser_data_dir: Path) -> None:
        self.browser_data_dir = Path(browser_data_dir)
        self.profile_dir = self.browser_data_dir / "page-edge-profile"
        self.cookie_file = self.browser_data_dir / "anonymous-cookies.txt"
        self._playwright = None
        self._context = None
        self._page = None
        self._network_candidates: dict[str, dict[str, str]] = {}
        self._douyin_response_candidates: dict[str, dict[str, str]] = {}

    def open(self, url: str) -> None:
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self._context = self._playwright.chromium.launch_persistent_context(
            str(self.profile_dir),
            channel="msedge",
            headless=False,
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.on("response", self._capture_response)
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)

    def _capture_response(self, response) -> None:
        try:
            content_type = str(response.headers.get("content-type") or "").partition(";")[0].lower()
            url = str(response.url)
            if (
                urlsplit(url).path == "/aweme/v1/web/general/search/single/"
                and "json" in content_type
            ):
                for card in extract_douyin_response_cards(response.json()):
                    match = DOUYIN_VIDEO_PATTERN.search(card["href"])
                    if match is not None:
                        self._douyin_response_candidates.setdefault(match.group(1), card)
            if content_type.startswith("video/") or content_type in MANIFEST_CONTENT_TYPES:
                self._network_candidates[url] = {
                    "url": url,
                    "title": "",
                    "content_type": content_type,
                }
        except Exception:
            return

    def _body_text(self) -> str:
        if self._page is None:
            return ""
        try:
            return self._page.locator("body").inner_text(timeout=2_000)
        except Exception:
            return ""

    def login_required(self) -> bool:
        text = self._body_text()
        return any(marker in text for marker in ("登录后即可搜索", "扫码登录", "验证码登录"))

    def risk_controlled(self) -> bool:
        text = self._body_text()
        return any(marker in text for marker in ("安全验证", "访问过于频繁", "请完成验证", "验证码")) and "验证码登录" not in text

    def douyin_cards(self) -> list[dict[str, str]]:
        if self._page is None:
            return []
        try:
            return list(
                self._page.locator('a[href*="/video/"]').evaluate_all(
                    """
                    elements => elements.map(element => ({
                        href: element.href || element.getAttribute('href') || '',
                        title: (element.innerText || element.getAttribute('aria-label') || '').trim()
                    }))
                    """
                )
            )
        except Exception as exc:
            message = str(exc).lower()
            if "execution context was destroyed" in message and "navigation" in message:
                return []
            raise

    def douyin_response_cards(self) -> list[dict[str, str]]:
        return list(self._douyin_response_candidates.values())

    def generic_candidates(self) -> list[dict[str, str]]:
        if self._page is None:
            return list(self._network_candidates.values())
        dom_candidates = self._page.locator("video[src], video source[src], a[href]").evaluate_all(
            """
            elements => elements.map(element => ({
                url: element.currentSrc || element.src || element.href || '',
                title: (element.title || element.getAttribute('aria-label') || document.title || '').trim(),
                content_type: element.type || ''
            }))
            """
        )
        return [*list(dom_candidates), *self._network_candidates.values()]

    def sync_cookies(self) -> None:
        if self._context is None:
            return
        cookies = self._context.cookies(["https://www.douyin.com/"])
        if cookies:
            write_netscape_cookie_file(cookies, self.cookie_file)

    def reload(self) -> None:
        if self._page is not None:
            self._page.reload(wait_until="domcontentloaded", timeout=60_000)

    def scroll(self) -> None:
        if self._page is not None:
            self._page.evaluate("window.scrollBy(0, Math.max(window.innerHeight * 0.85, 600))")

    def wait(self, milliseconds: int) -> None:
        if self._page is not None:
            self._page.wait_for_timeout(milliseconds)

    def close(self) -> None:
        try:
            if self._context is not None:
                self.sync_cookies()
                self._context.close()
        finally:
            if self._playwright is not None:
                self._playwright.stop()
            self._page = None
            self._context = None
            self._playwright = None


class PageCollectionManager:
    def __init__(
        self,
        database: Database,
        queue: QueueWakeable,
        collector_factory: Callable[[str], PageCollector],
    ) -> None:
        self.database = database
        self.queue = queue
        self.collector_factory = collector_factory
        self._application_stop = threading.Event()
        self._lock = threading.Lock()
        self._collection_lock = threading.Lock()
        self._threads: dict[int, threading.Thread] = {}
        self._cancel_events: dict[int, threading.Event] = {}

    def start(self) -> None:
        self._application_stop.clear()
        for batch_id in self.database.list_resumable_page_batches():
            self.submit(batch_id)

    def stop(self, timeout: float = 10.0) -> None:
        self._application_stop.set()
        with self._lock:
            threads = list(self._threads.values())
            events = list(self._cancel_events.values())
        for event in events:
            event.set()
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))

    def submit(self, batch_id: int) -> None:
        with self._lock:
            running = self._threads.get(batch_id)
            if running is not None and running.is_alive():
                return
            cancel_event = threading.Event()
            thread = threading.Thread(
                target=self._run_batch,
                args=(batch_id, cancel_event),
                name=f"page-collector-{batch_id}",
                daemon=True,
            )
            self._cancel_events[batch_id] = cancel_event
            self._threads[batch_id] = thread
            thread.start()

    def cancel_batch(self, batch_id: int) -> None:
        with self._lock:
            event = self._cancel_events.get(batch_id)
        if event is not None:
            event.set()

    def resume_batch(self, batch_id: int) -> None:
        try:
            batch = self.database.get_batch(batch_id)
        except KeyError:
            return
        if batch["source_mode"] == "page" and batch["collection_status"] in {
            "pending",
            "waiting_login",
            "collecting",
        }:
            self.submit(batch_id)

    def _run_batch(self, batch_id: int, cancel_event: threading.Event) -> None:
        try:
            batch = self.database.get_batch(batch_id)
            collector = self.collector_factory(str(batch["source_url"]))
            control = CollectionControl(
                self.database,
                batch_id,
                cancel_event,
                self._application_stop,
            )

            def on_status(status: str, reason: str | None) -> None:
                self.database.update_collection(batch_id, status, reason)

            def on_video(video: ExpandedVideo) -> bool:
                inserted = self.database.append_page_video(batch_id, video)
                if inserted:
                    self.database.skip_existing_completed(batch_id)
                    self.queue.wake()
                return inserted

            with self._collection_lock:
                outcome = collector.collect(
                    str(batch["source_url"]),
                    int(batch["requested_count"]),
                    on_video,
                    on_status,
                    control,
                )
            if self._application_stop.is_set():
                self.database.update_collection(batch_id, "pending", None)
            elif not cancel_event.is_set():
                self.database.update_collection(batch_id, outcome.status, outcome.reason)
        except KeyError:
            return
        except Exception as exc:
            try:
                self.database.update_collection(batch_id, "stopped", str(exc))
            except KeyError:
                pass
        finally:
            with self._lock:
                self._threads.pop(batch_id, None)
                self._cancel_events.pop(batch_id, None)
