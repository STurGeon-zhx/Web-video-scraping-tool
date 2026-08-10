from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Protocol

from .filenames import build_unique_output_path
from .store import TaskRecord


ProgressCallback = Callable[[dict[str, Any]], None]


class YoutubeDLSession(Protocol):
    def __enter__(self) -> "YoutubeDLSession": ...

    def __exit__(self, *args: object) -> None: ...

    def extract_info(self, url: str, download: bool) -> dict[str, Any]: ...


class CookieProvider(Protocol):
    def refresh(self, url: str) -> Path: ...


class ErrorCode(StrEnum):
    INVALID_URL = "invalid_url"
    UNAVAILABLE = "unavailable"
    LOGIN_REQUIRED = "login_required"
    ACCESS_RESTRICTED = "access_restricted"
    RATE_LIMITED = "rate_limited"
    FRESH_COOKIE = "fresh_cookie"
    NETWORK = "network"
    DISK_ERROR = "disk_error"
    MERGE_ERROR = "merge_error"
    UNKNOWN = "unknown"


ERROR_MESSAGES = {
    ErrorCode.INVALID_URL: "链接格式无效",
    ErrorCode.UNAVAILABLE: "视频不存在或已被删除",
    ErrorCode.LOGIN_REQUIRED: "该视频需要登录或无权访问",
    ErrorCode.ACCESS_RESTRICTED: "当前网络无法访问该视频",
    ErrorCode.RATE_LIMITED: "访问过于频繁，任务将在冷却后重试",
    ErrorCode.FRESH_COOKIE: "需要刷新匿名访问状态",
    ErrorCode.NETWORK: "网络连接失败",
    ErrorCode.DISK_ERROR: "下载目录无权限或磁盘空间不足",
    ErrorCode.MERGE_ERROR: "音视频合并失败",
    ErrorCode.UNKNOWN: "解析或下载失败",
}


class DownloadError(RuntimeError):
    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.user_message = ERROR_MESSAGES[code]


class DownloadCancelled(RuntimeError):
    pass


@dataclass(slots=True)
class DownloadResult:
    video_id: str
    title: str
    output_path: Path


def task_temp_key(task: TaskRecord) -> str:
    return f"task-{task.id}"


def classify_download_error(message: str) -> ErrorCode:
    lowered = message.lower()
    if "429" in lowered or "too many requests" in lowered or "rate limit" in lowered:
        return ErrorCode.RATE_LIMITED
    if "fresh cookie" in lowered:
        return ErrorCode.FRESH_COOKIE
    if "private" in lowered or "login required" in lowered or "sign in" in lowered:
        return ErrorCode.LOGIN_REQUIRED
    if "10204" in lowered or "ip address is blocked" in lowered or "access denied" in lowered:
        return ErrorCode.ACCESS_RESTRICTED
    if "not available" in lowered or "unavailable" in lowered or "404" in lowered:
        return ErrorCode.UNAVAILABLE
    if "no space" in lowered or "permission denied" in lowered or "disk" in lowered:
        return ErrorCode.DISK_ERROR
    if "ffmpeg" in lowered or "postprocessing" in lowered or "merge" in lowered:
        return ErrorCode.MERGE_ERROR
    if any(word in lowered for word in ("connection", "timed out", "timeout", "network", "dns")):
        return ErrorCode.NETWORK
    return ErrorCode.UNKNOWN


def _default_ydl_factory(options: dict[str, Any]) -> YoutubeDLSession:
    from yt_dlp import YoutubeDL

    return YoutubeDL(options)


class YtDlpDownloader:
    def __init__(
        self,
        ydl_factory: Callable[[dict[str, Any]], YoutubeDLSession] = _default_ydl_factory,
        ffmpeg_location: Path | None = None,
        cookie_file: Path | None = None,
        cookie_provider: CookieProvider | None = None,
    ) -> None:
        self._ydl_factory = ydl_factory
        self._ffmpeg_location = ffmpeg_location
        self._cookie_file = cookie_file
        self._cookie_provider = cookie_provider
        self._cookie_lock = threading.Lock()
        self._cookie_generation = 0
        self._cookie_refresh_error: tuple[int, Exception] | None = None

    def download(self, task: TaskRecord, on_progress: ProgressCallback) -> DownloadResult:
        task.output_dir.mkdir(parents=True, exist_ok=True)
        temp_dir = task.output_dir / ".douyin-part"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_key = task_temp_key(task)
        output_template = str(temp_dir / f"{temp_key}.%(ext)s")

        def progress_hook(data: dict[str, Any]) -> None:
            if data.get("status") != "downloading":
                return
            downloaded = int(data.get("downloaded_bytes") or 0)
            total = int(data.get("total_bytes") or data.get("total_bytes_estimate") or 0)
            progress = round(downloaded * 100 / total, 2) if total else 0.0
            on_progress(
                {
                    "status": "downloading",
                    "progress": progress,
                    "downloaded_bytes": downloaded,
                    "total_bytes": total or None,
                    "speed": data.get("speed"),
                    "eta": data.get("eta"),
                }
            )

        def match_filter(_info: dict[str, Any], *, incomplete: bool) -> None:
            on_progress({"status": "resolving"})
            return None

        def postprocessor_hook(data: dict[str, Any]) -> None:
            if data.get("status") == "started":
                on_progress({"status": "merging", "progress": 100.0})

        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "continuedl": True,
            "retries": 3,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": 2,
            "format": "bestvideo*+bestaudio/best",
            "merge_output_format": "mp4",
            "outtmpl": {"default": output_template},
            "progress_hooks": [progress_hook],
            "postprocessor_hooks": [postprocessor_hook],
            "match_filter": match_filter,
            "windowsfilenames": True,
        }
        if self._ffmpeg_location:
            options["ffmpeg_location"] = str(self._ffmpeg_location)
        with self._cookie_lock:
            cookie_file = self._cookie_file
            cookie_generation = self._cookie_generation
        if cookie_file and cookie_file.exists():
            options["cookiefile"] = str(cookie_file)

        try:
            info: dict[str, Any]
            with self._ydl_factory(options) as ydl:
                try:
                    info = ydl.extract_info(task.canonical_url, download=True)
                except Exception as exc:
                    if (
                        classify_download_error(str(exc)) != ErrorCode.FRESH_COOKIE
                        or self._cookie_provider is None
                    ):
                        raise
                    on_progress({"status": "resolving"})
                    cookie_file = self._refresh_cookie(
                        task.canonical_url,
                        failed_generation=cookie_generation,
                    )
                    on_progress({"status": "resolving"})
                    retry_options = {**options, "cookiefile": str(cookie_file)}
                    with self._ydl_factory(retry_options) as retry_ydl:
                        info = retry_ydl.extract_info(task.canonical_url, download=True)
            source = self._downloaded_path(info)
            if source is None or not source.exists():
                raise DownloadError(ErrorCode.UNKNOWN, "下载器未返回可用的输出文件")
            title = str(info.get("title") or "")
            video_id = str(info.get("id") or task.video_id or task.id)
            extension = source.suffix.lstrip(".") or str(info.get("ext") or "mp4")
            on_progress({"status": "merging", "progress": 100.0})
            destination = build_unique_output_path(task.output_dir, title or f"抖音视频_{video_id}", extension)
            shutil.move(str(source), str(destination))
            return DownloadResult(video_id=video_id, title=title, output_path=destination)
        except DownloadCancelled:
            raise
        except DownloadError:
            raise
        except Exception as exc:
            detail = str(exc)
            raise DownloadError(classify_download_error(detail), detail) from exc

    def _refresh_cookie(self, url: str, failed_generation: int) -> Path:
        if self._cookie_provider is None:
            raise RuntimeError("Fresh cookies are needed")
        with self._cookie_lock:
            if self._cookie_generation == failed_generation:
                try:
                    self._cookie_file = self._cookie_provider.refresh(url)
                except Exception as exc:
                    self._cookie_generation += 1
                    self._cookie_refresh_error = (self._cookie_generation, exc)
                    raise
                else:
                    self._cookie_generation += 1
                    self._cookie_refresh_error = None
            elif (
                self._cookie_refresh_error is not None
                and self._cookie_refresh_error[0] == self._cookie_generation
            ):
                error = self._cookie_refresh_error[1]
                raise RuntimeError(str(error)) from error
            if self._cookie_file is None:
                raise RuntimeError("Fresh cookies are needed")
            return self._cookie_file

    @staticmethod
    def _downloaded_path(info: dict[str, Any]) -> Path | None:
        requested = info.get("requested_downloads") or []
        for item in reversed(requested):
            filepath = item.get("filepath") if isinstance(item, dict) else None
            if filepath:
                return Path(filepath)
        for key in ("filepath", "_filename"):
            if info.get(key):
                return Path(info[key])
        return None
