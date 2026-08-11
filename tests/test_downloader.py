from pathlib import Path
import threading
from dataclasses import replace

import pytest

from douyin_downloader.downloader import (
    DownloadCancelled,
    DownloadError,
    DownloadPaused,
    ErrorCode,
    YtDlpDownloader,
    classify_download_error,
)
from douyin_downloader.store import TaskRecord, TaskStatus


class FakeYoutubeDL:
    def __init__(self, options: dict, title: str = "测试:标题") -> None:
        self.options = options
        self.title = title

    def __enter__(self) -> "FakeYoutubeDL":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def extract_info(self, url: str, download: bool) -> dict:
        assert download is True
        progress_hook = self.options["progress_hooks"][0]
        progress_hook(
            {
                "status": "downloading",
                "downloaded_bytes": 25,
                "total_bytes": 100,
                "speed": 50.0,
                "eta": 2.0,
            }
        )
        temp_path = Path(self.options["outtmpl"]["default"].replace("%(ext)s", "mp4"))
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_bytes(b"video")
        return {
            "id": "1234567890123456789",
            "title": self.title,
            "ext": "mp4",
            "requested_downloads": [{"filepath": str(temp_path)}],
            "webpage_url": url,
        }


class FakeMediaProcessor:
    def __init__(self) -> None:
        self.calls: list[tuple[Path, Path]] = []

    def ensure_compatible(
        self,
        source: Path,
        output: Path,
        on_progress,
    ) -> Path:
        self.calls.append((source, output))
        on_progress({"status": "transcoding", "progress": 50.0})
        output.write_bytes(b"compatible")
        return output


def make_task(tmp_path: Path) -> TaskRecord:
    return TaskRecord(
        id=1,
        batch_id=1,
        original_url="https://www.douyin.com/video/1234567890123456789",
        canonical_url="https://www.douyin.com/video/1234567890123456789",
        video_id="1234567890123456789",
        title=None,
        status=TaskStatus.RESOLVING,
        progress=0,
        output_dir=tmp_path,
        attempts=0,
    )


def test_download_reports_progress_and_moves_to_safe_title(tmp_path: Path) -> None:
    events: list[dict] = []
    downloader = YtDlpDownloader(
        ydl_factory=lambda options: FakeYoutubeDL(options),
        ffmpeg_location=None,
    )

    result = downloader.download(make_task(tmp_path), events.append)

    assert events == [
        {
            "status": "downloading",
            "progress": 25.0,
            "downloaded_bytes": 25,
            "total_bytes": 100,
            "speed": 50.0,
            "eta": 2.0,
        },
        {"status": "merging", "progress": 100.0},
    ]
    assert result.video_id == "1234567890123456789"
    assert result.title == "测试:标题"
    assert result.output_path == tmp_path / "测试_标题.mp4"
    assert result.output_path.read_bytes() == b"video"


def test_download_prefers_h264_aac_but_keeps_best_format_fallback(tmp_path: Path) -> None:
    captured: dict = {}

    def factory(options: dict):
        captured.update(options)
        return FakeYoutubeDL(options)

    YtDlpDownloader(ydl_factory=factory).download(
        make_task(tmp_path),
        lambda _event: None,
    )

    selector = captured["format"]
    assert selector.index("bestvideo[vcodec^=avc1]") < selector.index("bestvideo*+bestaudio/best")
    assert selector.index("bestaudio[acodec=mp4a.40.2]") < selector.index("bestvideo*+bestaudio/best")


def test_download_uses_compatible_mp4_returned_by_media_processor(tmp_path: Path) -> None:
    events: list[dict] = []
    processor = FakeMediaProcessor()
    downloader = YtDlpDownloader(
        ydl_factory=lambda options: FakeYoutubeDL(options),
        media_processor=processor,
    )

    result = downloader.download(make_task(tmp_path), events.append)

    assert processor.calls == [
        (
            tmp_path / ".douyin-part" / "task-1.mp4",
            tmp_path / ".douyin-part" / "task-1.compat.mp4",
        )
    ]
    assert {"status": "transcoding", "progress": 50.0} in events
    assert result.output_path.suffix == ".mp4"
    assert result.output_path.read_bytes() == b"compatible"
    assert not (tmp_path / ".douyin-part" / "task-1.mp4").exists()


def test_any_media_processing_failure_has_specific_user_facing_error(tmp_path: Path) -> None:
    class FailingMediaProcessor:
        def ensure_compatible(self, source: Path, output: Path, on_progress) -> Path:
            raise OSError("encoder failed")

    downloader = YtDlpDownloader(
        ydl_factory=lambda options: FakeYoutubeDL(options),
        media_processor=FailingMediaProcessor(),
    )

    with pytest.raises(DownloadError) as caught:
        downloader.download(make_task(tmp_path), lambda _event: None)

    assert caught.value.code == ErrorCode.TRANSCODE_ERROR
    assert caught.value.user_message == "兼容格式转换失败"
    assert not list(tmp_path.glob("*.mp4"))


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("HTTP Error 429: Too Many Requests", ErrorCode.RATE_LIMITED),
        ("Fresh cookies are needed", ErrorCode.FRESH_COOKIE),
        ("This video is private; login required", ErrorCode.LOGIN_REQUIRED),
        ("Video not available, status code 10204", ErrorCode.ACCESS_RESTRICTED),
        ("No space left on device", ErrorCode.DISK_ERROR),
        ("ffmpeg exited with code 1", ErrorCode.MERGE_ERROR),
        ("Connection timed out", ErrorCode.NETWORK),
        ("快手匿名访问失败: captcha", ErrorCode.KUAISHOU_ACCESS),
        ("该链接没有匹配到受支持的平台专用解析器", ErrorCode.UNSUPPORTED_PLATFORM),
        ("unexpected extractor response", ErrorCode.UNKNOWN),
    ],
)
def test_classifies_download_failures(message: str, expected: ErrorCode) -> None:
    assert classify_download_error(message) == expected


def test_adapter_wraps_external_failure_with_chinese_message(tmp_path: Path) -> None:
    class FailingYoutubeDL(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool) -> dict:
            raise RuntimeError("HTTP Error 429: Too Many Requests")

    downloader = YtDlpDownloader(
        ydl_factory=lambda options: FailingYoutubeDL(options),
        ffmpeg_location=None,
    )

    with pytest.raises(DownloadError) as caught:
        downloader.download(make_task(tmp_path), lambda _event: None)

    assert caught.value.code == ErrorCode.RATE_LIMITED
    assert caught.value.user_message == "访问过于频繁，任务将在冷却后重试"


def test_empty_title_uses_platform_specific_fallback(tmp_path: Path) -> None:
    task = replace(make_task(tmp_path), platform="bilibili", video_id="BV1kdKr6qEMF")
    downloader = YtDlpDownloader(
        ydl_factory=lambda options: FakeYoutubeDL(options, title=""),
        ffmpeg_location=None,
    )

    result = downloader.download(task, lambda _event: None)

    assert result.output_path.name == "B站视频_1234567890123456789.mp4"


def test_refreshes_anonymous_cookies_once_when_required(tmp_path: Path) -> None:
    cookie_file = tmp_path / "anonymous-cookies.txt"

    class CookieProvider:
        calls = 0

        urls: list[str] = []

        def refresh(self, url: str) -> Path:
            self.calls += 1
            self.urls.append(url)
            cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            return cookie_file

    calls = 0

    def factory(options: dict):
        nonlocal calls
        calls += 1
        if calls == 1:
            class FreshCookieFailure(FakeYoutubeDL):
                def extract_info(self, url: str, download: bool) -> dict:
                    raise RuntimeError("Fresh cookies are needed")

            return FreshCookieFailure(options)
        assert options["cookiefile"] == str(cookie_file)
        return FakeYoutubeDL(options)

    provider = CookieProvider()
    downloader = YtDlpDownloader(
        ydl_factory=factory,
        ffmpeg_location=None,
        cookie_provider=provider,
    )

    result = downloader.download(make_task(tmp_path), lambda _event: None)

    assert result.output_path.exists()
    assert provider.calls == 1
    assert provider.urls == [make_task(tmp_path).canonical_url]
    assert calls == 2


def test_reuses_refreshed_cookie_for_later_tasks(tmp_path: Path) -> None:
    cookie_file = tmp_path / "anonymous-cookies.txt"

    class CookieProvider:
        calls = 0

        def refresh(self, _url: str) -> Path:
            self.calls += 1
            cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            return cookie_file

    calls_without_cookie = 0

    def factory(options: dict):
        nonlocal calls_without_cookie
        if "cookiefile" not in options:
            calls_without_cookie += 1

            class FreshCookieFailure(FakeYoutubeDL):
                def extract_info(self, url: str, download: bool) -> dict:
                    raise RuntimeError("Fresh cookies are needed")

            return FreshCookieFailure(options)
        return FakeYoutubeDL(options)

    provider = CookieProvider()
    downloader = YtDlpDownloader(ydl_factory=factory, cookie_provider=provider)

    downloader.download(make_task(tmp_path / "first"), lambda _event: None)
    downloader.download(make_task(tmp_path / "second"), lambda _event: None)

    assert provider.calls == 1
    assert calls_without_cookie == 1


def test_cookie_retry_does_not_mutate_active_ydl_options(tmp_path: Path) -> None:
    cookie_file = tmp_path / "anonymous-cookies.txt"

    class CookieProvider:
        def refresh(self, _url: str) -> Path:
            cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            return cookie_file

    factory_calls = 0

    class FirstSession(FakeYoutubeDL):
        def __exit__(self, *_args: object) -> None:
            if "cookiefile" in self.options:
                raise RuntimeError(
                    "a filename was not supplied (nor was the CookieJar instance initialised with one)"
                )

        def extract_info(self, url: str, download: bool) -> dict:
            raise RuntimeError("Fresh cookies are needed")

    def factory(options: dict):
        nonlocal factory_calls
        factory_calls += 1
        return FirstSession(options) if factory_calls == 1 else FakeYoutubeDL(options)

    downloader = YtDlpDownloader(ydl_factory=factory, cookie_provider=CookieProvider())

    result = downloader.download(make_task(tmp_path), lambda _event: None)

    assert result.output_path.exists()


def test_cancellation_after_parse_failure_skips_cookie_refresh(tmp_path: Path) -> None:
    class CookieProvider:
        calls = 0

        def refresh(self, _url: str) -> Path:
            self.calls += 1
            return tmp_path / "unused.txt"

    class FreshCookieFailure(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool) -> dict:
            raise RuntimeError("Fresh cookies are needed")

    provider = CookieProvider()
    downloader = YtDlpDownloader(
        ydl_factory=lambda options: FreshCookieFailure(options),
        cookie_provider=provider,
    )

    def cancel(event: dict) -> None:
        if event["status"] == "resolving":
            raise DownloadCancelled()

    with pytest.raises(DownloadCancelled):
        downloader.download(make_task(tmp_path), cancel)
    assert provider.calls == 0


def test_cancellation_after_successful_parse_stops_before_download(tmp_path: Path) -> None:
    class FilterAwareSession(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool) -> dict:
            self.options["match_filter"]({}, incomplete=False)
            return super().extract_info(url, download)

    downloader = YtDlpDownloader(
        ydl_factory=lambda options: FilterAwareSession(options)
    )

    def cancel(event: dict) -> None:
        if event["status"] == "resolving":
            raise DownloadCancelled()

    with pytest.raises(DownloadCancelled):
        downloader.download(make_task(tmp_path), cancel)


def test_cancellation_stops_before_postprocessing_starts(tmp_path: Path) -> None:
    class PostProcessAwareSession(FakeYoutubeDL):
        def extract_info(self, url: str, download: bool) -> dict:
            info = super().extract_info(url, download)
            self.options["postprocessor_hooks"][0]({"status": "started"})
            return info

    downloader = YtDlpDownloader(
        ydl_factory=lambda options: PostProcessAwareSession(options)
    )

    def cancel(event: dict) -> None:
        if event["status"] == "merging":
            raise DownloadCancelled()

    with pytest.raises(DownloadCancelled):
        downloader.download(make_task(tmp_path), cancel)


def test_progress_cancellation_is_not_wrapped_as_download_failure(tmp_path: Path) -> None:
    downloader = YtDlpDownloader(ydl_factory=lambda options: FakeYoutubeDL(options))

    def cancel(_event: dict) -> None:
        raise DownloadCancelled()

    with pytest.raises(DownloadCancelled):
        downloader.download(make_task(tmp_path), cancel)


def test_progress_pause_is_not_wrapped_as_download_failure(tmp_path: Path) -> None:
    downloader = YtDlpDownloader(ydl_factory=lambda options: FakeYoutubeDL(options))

    def pause(_event: dict) -> None:
        raise DownloadPaused()

    with pytest.raises(DownloadPaused):
        downloader.download(make_task(tmp_path), pause)


def test_temporary_download_name_is_scoped_to_task_id(tmp_path: Path) -> None:
    captured: dict = {}

    def factory(options: dict):
        captured.update(options)
        return FakeYoutubeDL(options)

    downloader = YtDlpDownloader(ydl_factory=factory)

    downloader.download(make_task(tmp_path), lambda _event: None)

    assert Path(captured["outtmpl"]["default"]).name == "task-1.%(ext)s"


def test_concurrent_tasks_share_one_cookie_refresh(tmp_path: Path) -> None:
    cookie_file = tmp_path / "anonymous-cookies.txt"
    initial_failures = threading.Barrier(2)

    class CookieProvider:
        calls = 0

        def refresh(self, _url: str) -> Path:
            self.calls += 1
            cookie_file.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
            return cookie_file

    def factory(options: dict):
        if "cookiefile" not in options:
            class FreshCookieFailure(FakeYoutubeDL):
                def extract_info(self, url: str, download: bool) -> dict:
                    initial_failures.wait(timeout=2)
                    raise RuntimeError("Fresh cookies are needed")

            return FreshCookieFailure(options)
        return FakeYoutubeDL(options)

    provider = CookieProvider()
    downloader = YtDlpDownloader(ydl_factory=factory, cookie_provider=provider)
    tasks = [make_task(tmp_path / "one"), make_task(tmp_path / "two")]
    errors: list[Exception] = []

    def run(task: TaskRecord) -> None:
        try:
            downloader.download(task, lambda _event: None)
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(task,)) for task in tasks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert errors == []
    assert provider.calls == 1


def test_concurrent_tasks_share_one_failed_cookie_refresh(tmp_path: Path) -> None:
    initial_failures = threading.Barrier(2)

    class CookieProvider:
        calls = 0

        def refresh(self, _url: str) -> Path:
            self.calls += 1
            raise RuntimeError("anonymous browser failed")

    def factory(options: dict):
        class FreshCookieFailure(FakeYoutubeDL):
            def extract_info(self, url: str, download: bool) -> dict:
                initial_failures.wait(timeout=2)
                raise RuntimeError("Fresh cookies are needed")

        return FreshCookieFailure(options)

    provider = CookieProvider()
    downloader = YtDlpDownloader(ydl_factory=factory, cookie_provider=provider)
    errors: list[DownloadError] = []

    def run(path: Path) -> None:
        try:
            downloader.download(make_task(path), lambda _event: None)
        except DownloadError as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(tmp_path / "one",)),
        threading.Thread(target=run, args=(tmp_path / "two",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)

    assert len(errors) == 2
    assert provider.calls == 1
