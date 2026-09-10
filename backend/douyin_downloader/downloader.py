from __future__ import annotations

import shutil
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Protocol

from .filenames import build_unique_output_path
from .media import (
    FFmpegMediaProcessor,
    MediaProcessingError,
    MediaProcessingInterrupted,
)
from .store import TaskRecord


ProgressCallback = Callable[[dict[str, Any]], None]


class YoutubeDLSession(Protocol):
    def __enter__(self) -> "YoutubeDLSession": ...

    def __exit__(self, *args: object) -> None: ...

    def extract_info(self, url: str, download: bool) -> dict[str, Any]: ...


class CookieProvider(Protocol):
    def refresh(self, url: str) -> Path: ...


class MediaProcessor(Protocol):
    def ensure_compatible(
        self,
        source: Path,
        output: Path,
        on_progress: ProgressCallback,
    ) -> Path: ...


H264_PREFERRED_FORMAT = "/".join(
    (
        "bestvideo[vcodec^=avc1]+bestaudio[acodec=mp4a.40.2]",
        "bestvideo[vcodec^=h264]+bestaudio[acodec=mp4a.40.2]",
        "best[vcodec^=avc1][acodec=mp4a.40.2]",
        "best[vcodec^=h264][acodec=mp4a.40.2]",
        "bestvideo[vcodec^=avc1]+bestaudio",
        "bestvideo[vcodec^=h264]+bestaudio",
        "best[vcodec^=avc1]",
        "best[vcodec^=h264]",
        "bestvideo*+bestaudio/best",
    )
)


class ErrorCode(StrEnum):
    INVALID_URL = "invalid_url"
    UNSUPPORTED_PLATFORM = "unsupported_platform"
    KUAISHOU_ACCESS = "kuaishou_access"
    VIPSHOP_ACCESS = "vipshop_access"
    YOUTUBE_RUNTIME = "youtube_runtime"
    PROXY_UNAVAILABLE = "proxy_unavailable"
    UNAVAILABLE = "unavailable"
    LOGIN_REQUIRED = "login_required"
    ACCESS_RESTRICTED = "access_restricted"
    RATE_LIMITED = "rate_limited"
    FRESH_COOKIE = "fresh_cookie"
    NETWORK = "network"
    DISK_ERROR = "disk_error"
    MERGE_ERROR = "merge_error"
    TRANSCODE_ERROR = "transcode_error"
    UNKNOWN = "unknown"


ERROR_MESSAGES = {
    ErrorCode.INVALID_URL: "链接格式无效",
    ErrorCode.UNSUPPORTED_PLATFORM: "该平台暂不支持",
    ErrorCode.KUAISHOU_ACCESS: "快手匿名访问失败，请稍后重试",
    ErrorCode.VIPSHOP_ACCESS: "唯品会匿名访问失败，请稍后重试",
    ErrorCode.YOUTUBE_RUNTIME: "YouTube 运行组件未安装，请运行 scripts\\setup-youtube-runtime.ps1",
    ErrorCode.PROXY_UNAVAILABLE: "YouTube 本地代理不可用，请检查代理软件、地址和端口",
    ErrorCode.UNAVAILABLE: "视频不存在或已被删除",
    ErrorCode.LOGIN_REQUIRED: "该视频需要登录或无权访问",
    ErrorCode.ACCESS_RESTRICTED: "当前网络无法访问该视频",
    ErrorCode.RATE_LIMITED: "访问过于频繁，任务将在冷却后重试",
    ErrorCode.FRESH_COOKIE: "需要刷新匿名访问状态",
    ErrorCode.NETWORK: "网络连接失败",
    ErrorCode.DISK_ERROR: "下载目录无权限或磁盘空间不足",
    ErrorCode.MERGE_ERROR: "音视频合并失败",
    ErrorCode.TRANSCODE_ERROR: "兼容格式转换失败",
    ErrorCode.UNKNOWN: "解析或下载失败",
}


class DownloadError(RuntimeError):
    def __init__(
        self,
        code: ErrorCode,
        detail: str,
        user_message: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.user_message = user_message or ERROR_MESSAGES[code]


class DownloadCancelled(MediaProcessingInterrupted):
    pass


class DownloadPaused(MediaProcessingInterrupted):
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
    from .youtube import is_youtube_access_restriction
    from .youtube_network import is_proxy_connection_error

    if "快手匿名访问失败" in message:
        return ErrorCode.KUAISHOU_ACCESS
    if "唯品会匿名访问失败" in message:
        return ErrorCode.VIPSHOP_ACCESS
    if "受支持的平台专用解析器" in message:
        return ErrorCode.UNSUPPORTED_PLATFORM
    if "429" in lowered or "too many requests" in lowered or "rate limit" in lowered:
        return ErrorCode.RATE_LIMITED
    if "fresh cookie" in lowered:
        return ErrorCode.FRESH_COOKIE
    if is_proxy_connection_error(message):
        return ErrorCode.PROXY_UNAVAILABLE
    if is_youtube_access_restriction(message) or any(
        phrase in lowered
        for phrase in (
            "po token",
            "sign in to confirm you're not a bot",
            "sign in to confirm you’re not a bot",
            "confirm you are not a bot",
            "youtube said: sign in",
        )
    ):
        return ErrorCode.ACCESS_RESTRICTED
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
    from .direct_media import DirectMediaIE
    from .kuaishou import KuaishouIE
    from .vipshop import VipshopIE

    downloader = YoutubeDL(options, auto_init=False)
    downloader.add_info_extractor(KuaishouIE())
    downloader.add_info_extractor(VipshopIE())
    downloader.add_info_extractor(DirectMediaIE())
    downloader.add_default_info_extractors()
    return downloader


class YtDlpDownloader:
    def __init__(
        self,
        ydl_factory: Callable[[dict[str, Any]], YoutubeDLSession] = _default_ydl_factory,
        ffmpeg_location: Path | None = None,
        cookie_file: Path | None = None,
        cookie_provider: CookieProvider | None = None,
        media_processor: MediaProcessor | None = None,
        youtube_runtime_options_provider: Callable[[], dict[str, Any]] | None = None,
        youtube_network_options_provider: Callable[[], dict[str, Any]] | None = None,
        youtube_auth_options_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        self._ydl_factory = ydl_factory
        self._ffmpeg_location = ffmpeg_location
        self._cookie_file = cookie_file
        self._cookie_provider = cookie_provider
        self._media_processor = media_processor
        if youtube_runtime_options_provider is None:
            from .youtube import youtube_runtime_options

            youtube_runtime_options_provider = youtube_runtime_options
        self._youtube_runtime_options_provider = youtube_runtime_options_provider
        self._youtube_network_options_provider = youtube_network_options_provider or (lambda: {})
        self._youtube_auth_options_provider = youtube_auth_options_provider or (lambda: {})
        if self._media_processor is None and ffmpeg_location is not None:
            self._media_processor = FFmpegMediaProcessor(ffmpeg_location)
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
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": 2,
            "format": H264_PREFERRED_FORMAT,
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
        if task.platform == "youtube":
            try:
                options.update(self._youtube_runtime_options_provider())
                options.update(self._youtube_network_options_provider())
                options.update(self._youtube_auth_options_provider())
            except RuntimeError as exc:
                raise DownloadError(ErrorCode.YOUTUBE_RUNTIME, str(exc)) from exc
        elif cookie_file and cookie_file.exists():
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
                        or task.platform == "youtube"
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
            if self._media_processor is not None:
                compatible_output = temp_dir / f"{temp_key}.compat.mp4"
                try:
                    processed = self._media_processor.ensure_compatible(
                        source,
                        compatible_output,
                        on_progress,
                    )
                except (DownloadCancelled, DownloadPaused, MediaProcessingError):
                    raise
                except Exception as exc:
                    raise MediaProcessingError(str(exc)) from exc
                if processed != source:
                    source.unlink(missing_ok=True)
                source = processed
            on_progress({"status": "merging", "progress": 100.0})
            platform_prefix = {
                "douyin": "抖音视频",
                "kuaishou": "快手视频",
                "bilibili": "B站视频",
                "youtube": "YouTube视频",
                "direct": "视频直链",
                "vipshop": "唯品会视频",
            }.get(task.platform, "视频")
            output_title = title or f"{platform_prefix}_{video_id}"
            if task.position > 0:
                output_title = f"{task.position:03d}_{output_title}"
            destination = build_unique_output_path(
                task.output_dir,
                output_title,
                "mp4",
            )
            shutil.move(str(source), str(destination))
            return DownloadResult(video_id=video_id, title=title, output_path=destination)
        except (DownloadCancelled, DownloadPaused):
            raise
        except MediaProcessingError as exc:
            raise DownloadError(ErrorCode.TRANSCODE_ERROR, str(exc)) from exc
        except DownloadError:
            raise
        except Exception as exc:
            detail = str(exc)
            code = classify_download_error(detail)
            if task.platform == "youtube" and options.get("proxy") and code == ErrorCode.NETWORK:
                code = ErrorCode.PROXY_UNAVAILABLE
            youtube_message = None
            if task.platform == "youtube" and code == ErrorCode.ACCESS_RESTRICTED:
                from .youtube import youtube_failure_message

                youtube_message = youtube_failure_message(detail)
            raise DownloadError(code, detail, youtube_message) from exc

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
