from __future__ import annotations

import re
import hashlib
import logging
import threading
import time
from collections import deque
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
        and parsed.path.startswith(
            ("/search/", "/root/search/", "/jingxuan/search/")
        )
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
    def persist_state(self) -> None: ...
    def reload(self) -> None: ...
    def reopen(
        self,
        url: str,
        *,
        clean_state: bool,
        software_rendering: bool,
    ) -> None: ...
    def scroll(self) -> None: ...
    def at_page_bottom(self) -> bool: ...
    def page_exhausted(self) -> bool: ...
    def loaded_saved_state(self) -> bool: ...
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
        max_idle_rounds: int | None = None,
        idle_timeout_seconds: float = 30.0,
        bottom_confirmations: int = 3,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._session_factory = session_factory
        self._wait_milliseconds = wait_milliseconds
        self._max_login_rounds = max_login_rounds
        self._initial_reload_rounds = initial_reload_rounds
        self._first_video_rounds = first_video_rounds
        self._max_idle_rounds = max_idle_rounds
        self._idle_timeout_seconds = idle_timeout_seconds
        self._bottom_confirmations = bottom_confirmations
        self._clock = clock

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
        bottom_idle_checks = 0
        reported_end_checks = 0
        last_progress_at = self._clock()
        state_persisted = False
        clean_recovery_attempted = False

        def persist_login_state() -> None:
            try:
                persist_state = getattr(session, "persist_state", session.sync_cookies)
                persist_state()
            except Exception as exc:
                logging.warning("保存页面采集登录状态失败: %s", exc)

        def finish(outcome: CollectionOutcome) -> CollectionOutcome:
            if discovered and outcome.status in {"target_reached", "page_ended"}:
                persist_login_state()
            on_status(outcome.status, outcome.reason)
            return outcome

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
                        return finish(CollectionOutcome("stopped", "采集已停止"))
                    if session.risk_controlled():
                        verification_outcome = wait_for_verification()
                        if verification_outcome is not None:
                            return finish(verification_outcome)
                        continue
                    login_required = session.login_required()
                    if login_required:
                        saw_login_prompt = True
                    has_video_cards = bool(session.douyin_cards()) or bool(
                        search_mode == "general" and session.douyin_response_cards()
                    )
                    if has_video_cards or (saw_login_prompt and not login_required):
                        break
                    loaded_saved_state = bool(
                        getattr(session, "loaded_saved_state", lambda: False)()
                    )
                    if (
                        not clean_recovery_attempted
                        and loaded_saved_state
                        and login_required
                        and round_index + 1 >= self._first_video_rounds
                    ):
                        logging.info("已保存的登录状态已退出，切换干净会话")
                        on_status(
                            "waiting_login",
                            "已保存的登录状态已失效，请重新登录或验证",
                        )
                        session.reopen(
                            source_url,
                            clean_state=True,
                            software_rendering=False,
                        )
                        clean_recovery_attempted = True
                        saw_login_prompt = False
                        reloaded_initial_page = False
                        continue
                    if (
                        not clean_recovery_attempted
                        and not saw_login_prompt
                        and not login_required
                        and round_index + 1 >= self._first_video_rounds
                    ):
                        reopen = getattr(session, "reopen", None)
                        if reopen is not None:
                            on_status("waiting_login", "页面未返回视频，正在重新打开干净会话")
                            reopen(
                                source_url,
                                clean_state=True,
                                software_rendering=False,
                            )
                            clean_recovery_attempted = True
                            reloaded_initial_page = False
                            continue
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
                    return finish(
                        CollectionOutcome("login_expired", "等待网页登录超时")
                    )
            on_status("collecting", None)
            while not control.is_cancelled():
                control.wait_if_paused()
                if control.is_cancelled():
                    return finish(CollectionOutcome("stopped", "采集已停止"))
                if session.risk_controlled():
                    verification_outcome = wait_for_verification()
                    if verification_outcome is not None:
                        return finish(verification_outcome)
                    on_status("collecting", None)
                    continue
                if douyin_search and session.login_required():
                    on_status("waiting_login", None)
                    for _ in range(self._max_login_rounds):
                        if control.is_cancelled():
                            return finish(CollectionOutcome("stopped", "采集已停止"))
                        if session.risk_controlled():
                            verification_outcome = wait_for_verification()
                            if verification_outcome is not None:
                                return finish(verification_outcome)
                            continue
                        if not session.login_required():
                            on_status("collecting", None)
                            break
                        session.wait(self._wait_milliseconds)
                    else:
                        return finish(
                            CollectionOutcome("login_expired", "等待网页登录超时")
                        )
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
                newly_discovered = 0
                for video in raw_videos:
                    key = (video.platform, video.video_id)
                    if key in discovered:
                        continue
                    discovered.add(key)
                    newly_discovered += 1
                    if not state_persisted:
                        persist_login_state()
                        state_persisted = True
                    if on_video(video):
                        added += 1
                    if len(discovered) >= max_items:
                        return finish(CollectionOutcome("target_reached", None))
                if newly_discovered:
                    last_progress_at = self._clock()
                    idle_rounds = 0
                    bottom_idle_checks = 0
                    reported_end_checks = 0
                else:
                    idle_rounds += 1

                reported_exhausted = bool(
                    getattr(session, "page_exhausted", lambda: False)()
                )
                if (
                    not newly_discovered
                    and reported_exhausted
                    and bool(getattr(session, "at_page_bottom", lambda: True)())
                ):
                    reported_end_checks += 1
                else:
                    reported_end_checks = 0
                exhausted = reported_end_checks >= self._bottom_confirmations
                timed_out_at_bottom = False
                if self._max_idle_rounds is not None:
                    idle_limit = (
                        self._first_video_rounds
                        if douyin_search and not discovered
                        else self._max_idle_rounds
                    )
                    timed_out_at_bottom = idle_rounds >= idle_limit
                elif self._clock() - last_progress_at >= self._idle_timeout_seconds:
                    if bool(getattr(session, "at_page_bottom", lambda: True)()):
                        bottom_idle_checks += 1
                    else:
                        bottom_idle_checks = 0
                    timed_out_at_bottom = bottom_idle_checks >= self._bottom_confirmations

                if exhausted or timed_out_at_bottom:
                    loaded_saved_state = bool(
                        getattr(session, "loaded_saved_state", lambda: False)()
                    )
                    if (
                        not discovered
                        and loaded_saved_state
                        and not clean_recovery_attempted
                    ):
                        logging.info("已保存的登录状态未返回视频，切换干净会话重试")
                        on_status(
                            "waiting_login",
                            "已保存的登录状态已失效，请重新登录或验证",
                        )
                        session.reopen(
                            source_url,
                            clean_state=True,
                            software_rendering=False,
                        )
                        clean_recovery_attempted = True
                        idle_rounds = 0
                        bottom_idle_checks = 0
                        reported_end_checks = 0
                        last_progress_at = self._clock()
                        continue
                    if discovered:
                        return finish(
                            CollectionOutcome("page_ended", "页面已结束或没有更多视频")
                        )
                    reason = (
                        "综合页未发现视频，页面结构可能已变化"
                        if search_mode == "general"
                        else "页面中没有发现可下载视频"
                    )
                    return finish(CollectionOutcome("no_videos", reason))
                session.scroll()
                session.wait(self._wait_milliseconds)
            return finish(CollectionOutcome("stopped", "采集已停止"))
        finally:
            session.close()


class PlaywrightBrowserSession:
    def __init__(
        self,
        browser_data_dir: Path,
        playwright_factory: Callable[[], Any] | None = None,
        *,
        clean_state: bool = False,
        software_rendering: bool = False,
    ) -> None:
        self.browser_data_dir = Path(browser_data_dir)
        self.storage_state_file = self.browser_data_dir / "page-login-state.json"
        self.cookie_file = self.browser_data_dir / "anonymous-cookies.txt"
        self._playwright_factory = playwright_factory
        self._clean_state = clean_state
        self._software_rendering = software_rendering
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._network_candidates: dict[str, dict[str, str]] = {}
        self._douyin_response_candidates: dict[str, dict[str, str]] = {}
        self._page_exhausted = False
        self._last_url: str | None = None
        self._browser_retry_used = False
        self._loaded_saved_state = False

    def open(self, url: str) -> None:
        self._last_url = url
        if self._playwright_factory is None:
            from playwright.sync_api import sync_playwright

            starter = sync_playwright()
        else:
            starter = self._playwright_factory()
        self.browser_data_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = starter.start()
        launch_options: dict[str, Any] = {"headless": False, "channel": "msedge"}
        if self._software_rendering:
            launch_options["args"] = ["--disable-gpu"]
        self._browser = self._playwright.chromium.launch(**launch_options)
        context_options: dict[str, Any] = {}
        if self.storage_state_file.is_file() and not self._clean_state:
            context_options["storage_state"] = str(self.storage_state_file)
        self._loaded_saved_state = "storage_state" in context_options
        self._context = self._browser.new_context(**context_options)
        self._page = self._context.new_page()
        self._page.on("response", self._capture_response)
        self._page.on("requestfinished", self._capture_finished_request)
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:
            if not self._recover_browser_close(exc):
                raise

    @staticmethod
    def _is_browser_closed_error(exc: Exception) -> bool:
        message = str(exc).lower()
        return "target page, context or browser has been closed" in message

    def _recover_browser_close(self, exc: Exception) -> bool:
        if not self._is_browser_closed_error(exc):
            return False
        if self._browser_retry_used or self._last_url is None:
            raise RuntimeError("验证浏览器意外退出，兼容模式重试失败") from exc
        self._browser_retry_used = True
        self.reopen(
            self._last_url,
            clean_state=False,
            software_rendering=True,
        )
        return True

    def _capture_response(self, response) -> None:
        try:
            content_type = str(response.headers.get("content-type") or "").partition(";")[0].lower()
            url = str(response.url)
            parsed = urlsplit(url)
            if (
                (parsed.hostname or "").lower().endswith("douyin.com")
                and "search" in parsed.path
                and "json" in content_type
            ):
                payload = response.json()
                response_cards = extract_douyin_response_cards(payload)
                for card in response_cards:
                    match = DOUYIN_VIDEO_PATTERN.search(card["href"])
                    if match is not None:
                        self._douyin_response_candidates.setdefault(match.group(1), card)
                if (
                    response_cards
                    and isinstance(payload, Mapping)
                    and payload.get("has_more") in (0, False)
                ):
                    self._page_exhausted = True
            if content_type.startswith("video/") or content_type in MANIFEST_CONTENT_TYPES:
                self._network_candidates[url] = {
                    "url": url,
                    "title": "",
                    "content_type": content_type,
                }
        except Exception:
            return

    def _capture_finished_request(self, request) -> None:
        try:
            response = request.response()
            if response is not None:
                self._capture_response(response)
        except Exception:
            return

    def _body_text(self) -> str:
        if self._page is None:
            return ""
        try:
            return self._page.locator("body").inner_text(timeout=2_000)
        except Exception as exc:
            self._recover_browser_close(exc)
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
                self._page.locator(
                    'a[href*="/video/"], .search-result-card'
                ).evaluate_all(
                    """
                    elements => {
                        const findAwemeInfo = element => {
                            const queue = [];
                            const seen = new Set();
                            const reactProperties = Object.getOwnPropertyNames(element);
                            const fiberProperties = reactProperties.filter(
                                name => name.startsWith('__reactFiber')
                            );
                            const roots = fiberProperties.length
                                ? fiberProperties
                                : reactProperties.filter(name => name.startsWith('__reactProps'));
                            for (const name of roots) {
                                queue.push([element[name], 0]);
                            }
                            let visited = 0;
                            while (queue.length && visited < 500) {
                                const [value, depth] = queue.pop();
                                visited += 1;
                                if (!value || typeof value !== 'object' || seen.has(value)) {
                                    continue;
                                }
                                seen.add(value);
                                const candidates = [
                                    value.awemeInfo,
                                    value.aweme_info,
                                    value.data && (value.data.awemeInfo || value.data.aweme_info),
                                    value.pendingProps && value.pendingProps.data && (
                                        value.pendingProps.data.awemeInfo ||
                                        value.pendingProps.data.aweme_info
                                    ),
                                    value.memoizedProps && value.memoizedProps.data && (
                                        value.memoizedProps.data.awemeInfo ||
                                        value.memoizedProps.data.aweme_info
                                    )
                                ];
                                for (const info of candidates) {
                                    if (!info || typeof info !== 'object') {
                                        continue;
                                    }
                                    const hasContent = value => {
                                        if (!value) {
                                            return false;
                                        }
                                        if (Array.isArray(value)) {
                                            return value.length > 0;
                                        }
                                        if (typeof value === 'object') {
                                            return Object.keys(value).length > 0;
                                        }
                                        return true;
                                    };
                                    const videoId = String(info.awemeId || info.aweme_id || '');
                                    const hasVideo = hasContent(info.video);
                                    const hasImages = hasContent(
                                        info.images || info.imagePostInfo || info.image_post_info
                                    );
                                    const isLive = hasContent(
                                        info.isLive || info.is_live || info.liveRoom ||
                                        info.live_room || info.roomId || info.room_id
                                    );
                                    const isAd = hasContent(
                                        info.isAds || info.is_ads || info.isAd ||
                                        info.is_ad || info.rawAdData || info.raw_ad_data
                                    );
                                    if (/^\\d+$/.test(videoId) && hasVideo && !hasImages && !isLive && !isAd) {
                                        return info;
                                    }
                                }
                                if (depth >= 12) {
                                    continue;
                                }
                                for (const key of [
                                    'sibling', 'memoizedProps', 'pendingProps', 'data', 'child'
                                ]) {
                                    let child;
                                    try {
                                        child = value[key];
                                    } catch (_error) {
                                        continue;
                                    }
                                    if (child && typeof child === 'object') {
                                        queue.push([child, depth + 1]);
                                    }
                                }
                            }
                            return null;
                        };

                        return elements.map(element => {
                            const directHref = element.href || element.getAttribute('href') || '';
                            if (directHref.includes('/video/')) {
                                return {
                                    href: directHref,
                                    title: (
                                        element.innerText || element.getAttribute('aria-label') || ''
                                    ).trim()
                                };
                            }
                            const info = findAwemeInfo(element);
                            if (!info) {
                                return {href: '', title: ''};
                            }
                            const videoId = String(info.awemeId || info.aweme_id || '');
                            return {
                                href: `https://www.douyin.com/video/${videoId}`,
                                title: String(info.desc || element.innerText || '').trim()
                            };
                        }).filter(card => card.href);
                    }
                    """
                )
            )
        except Exception as exc:
            message = str(exc).lower()
            if "execution context was destroyed" in message and "navigation" in message:
                return []
            if self._recover_browser_close(exc):
                return []
            raise

    def douyin_response_cards(self) -> list[dict[str, str]]:
        return list(self._douyin_response_candidates.values())

    def page_exhausted(self) -> bool:
        return self._page_exhausted

    def loaded_saved_state(self) -> bool:
        return self._loaded_saved_state

    def at_page_bottom(self) -> bool:
        if self._page is None:
            return True
        try:
            return bool(
                self._page.evaluate(
                    "window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 8"
                )
            )
        except Exception:
            return False

    def generic_candidates(self) -> list[dict[str, str]]:
        if self._page is None:
            return list(self._network_candidates.values())
        try:
            dom_candidates = self._page.locator(
                "video[src], video source[src], a[href]"
            ).evaluate_all(
                """
                elements => elements.map(element => ({
                    url: element.currentSrc || element.src || element.href || '',
                    title: (element.title || element.getAttribute('aria-label') || document.title || '').trim(),
                    content_type: element.type || ''
                }))
                """
            )
        except Exception as exc:
            if self._recover_browser_close(exc):
                return list(self._network_candidates.values())
            raise
        return [*list(dom_candidates), *self._network_candidates.values()]

    def sync_cookies(self) -> None:
        if self._context is None:
            return
        cookies = self._context.cookies(["https://www.douyin.com/"])
        if cookies:
            write_netscape_cookie_file(cookies, self.cookie_file)

    def persist_state(self) -> None:
        if self._context is None:
            return
        temporary = self.storage_state_file.with_suffix(
            self.storage_state_file.suffix + ".tmp"
        )
        try:
            self._context.storage_state(path=str(temporary))
            temporary.replace(self.storage_state_file)
            self.sync_cookies()
        finally:
            temporary.unlink(missing_ok=True)

    def reload(self) -> None:
        if self._page is not None:
            try:
                self._page.reload(wait_until="domcontentloaded", timeout=60_000)
            except Exception as exc:
                if not self._recover_browser_close(exc):
                    raise

    def reopen(
        self,
        url: str,
        *,
        clean_state: bool,
        software_rendering: bool,
    ) -> None:
        self.close()
        self._clean_state = clean_state
        self._software_rendering = software_rendering
        self._network_candidates.clear()
        self._douyin_response_candidates.clear()
        self._page_exhausted = False
        self.open(url)

    def scroll(self) -> None:
        if self._page is not None:
            try:
                self._page.evaluate(
                    "window.scrollBy(0, Math.max(window.innerHeight * 0.85, 600))"
                )
            except Exception as exc:
                if not self._recover_browser_close(exc):
                    raise

    def wait(self, milliseconds: int) -> None:
        if self._page is not None:
            try:
                self._page.wait_for_timeout(milliseconds)
            except Exception as exc:
                if not self._recover_browser_close(exc):
                    raise

    def close(self) -> None:
        for resource_name, resource in (
            ("browser context", self._context),
            ("browser", self._browser),
        ):
            if resource is None:
                continue
            try:
                resource.close()
            except Exception as exc:
                logging.warning("关闭页面采集%s失败: %s", resource_name, exc)
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                logging.warning("停止页面采集 Playwright 失败: %s", exc)
        self._page = None
        self._context = None
        self._browser = None
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
        self._condition = threading.Condition(self._lock)
        self._pending: deque[int] = deque()
        self._pending_ids: set[int] = set()
        self._worker_thread: threading.Thread | None = None
        self._current_batch_id: int | None = None
        self._cancel_events: dict[int, threading.Event] = {}

    def start(self) -> None:
        self._application_stop.clear()
        with self._condition:
            self._ensure_worker_locked()
        for batch_id in self.database.list_resumable_page_batches():
            self.submit(batch_id)

    def stop(self, timeout: float = 10.0) -> None:
        self._application_stop.set()
        with self._condition:
            for event in self._cancel_events.values():
                event.set()
            worker = self._worker_thread
            self._condition.notify_all()
        if worker is not None:
            worker.join(timeout=timeout)
        with self._condition:
            if worker is not None and self._worker_thread is worker and not worker.is_alive():
                self._worker_thread = None
            self._pending.clear()
            self._pending_ids.clear()
            self._cancel_events.clear()
            self._current_batch_id = None

    def submit(self, batch_id: int) -> None:
        with self._condition:
            if batch_id == self._current_batch_id or batch_id in self._pending_ids:
                return
            cancel_event = threading.Event()
            self._cancel_events[batch_id] = cancel_event
            self._pending.append(batch_id)
            self._pending_ids.add(batch_id)
            self._ensure_worker_locked()
            self._condition.notify()

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

    def _ensure_worker_locked(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._worker_thread = threading.Thread(
            target=self._worker_loop,
            name="page-collector-worker",
            daemon=True,
        )
        self._worker_thread.start()

    def _worker_loop(self) -> None:
        while True:
            with self._condition:
                while not self._pending and not self._application_stop.is_set():
                    self._condition.wait()
                if self._application_stop.is_set():
                    return
                batch_id = self._pending.popleft()
                self._pending_ids.discard(batch_id)
                self._current_batch_id = batch_id
                cancel_event = self._cancel_events[batch_id]
            try:
                if not cancel_event.is_set():
                    self._run_batch(batch_id, cancel_event)
            finally:
                with self._condition:
                    self._cancel_events.pop(batch_id, None)
                    if self._current_batch_id == batch_id:
                        self._current_batch_id = None
                    self._condition.notify_all()

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
                inserted = self.database.append_page_video(
                    batch_id,
                    video,
                    skip_history_duplicate=video.platform == "youtube",
                )
                if inserted:
                    self.queue.wake()
                return inserted

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
                from .youtube import is_youtube_page_url, youtube_failure_message

                reason = youtube_failure_message(str(exc)) if (
                    "batch" in locals() and is_youtube_page_url(str(batch["source_url"]))
                ) else str(exc)
                self.database.update_collection(batch_id, "stopped", reason)
            except KeyError:
                pass
