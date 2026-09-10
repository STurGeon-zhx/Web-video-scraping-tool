import threading
import time
from pathlib import Path

from douyin_downloader.downloader import (
    DownloadCancelled,
    DownloadError,
    DownloadPaused,
    DownloadResult,
    ErrorCode,
)
from douyin_downloader.queue import TaskQueue
from douyin_downloader.store import Database


def wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待队列状态超时")


def make_database(tmp_path: Path, count: int) -> tuple[Database, int]:
    database = Database(tmp_path / "queue.db")
    database.initialize()
    batch_id = database.create_batch(
        [f"https://www.douyin.com/video/{1234567890123456789 + index}" for index in range(count)],
        tmp_path,
    )
    return database, batch_id


def test_queue_never_runs_more_than_two_downloads(tmp_path: Path) -> None:
    database, batch_id = make_database(tmp_path, 4)

    class CountingDownloader:
        def __init__(self) -> None:
            self.active = 0
            self.maximum = 0
            self.lock = threading.Lock()

        def download(self, task, on_progress):
            with self.lock:
                self.active += 1
                self.maximum = max(self.maximum, self.active)
            try:
                time.sleep(0.04)
                output = tmp_path / f"{task.id}.mp4"
                output.write_bytes(b"video")
                return DownloadResult(str(task.id), f"视频{task.id}", output)
            finally:
                with self.lock:
                    self.active -= 1

    downloader = CountingDownloader()
    queue = TaskQueue(database, downloader, worker_count=2, retry_delays=(0, 0))
    queue.start()
    try:
        wait_until(lambda: database.get_batch(batch_id)["counts"].get("completed") == 4)
    finally:
        queue.stop()

    assert downloader.maximum == 2


def test_network_failure_retries_three_times_then_succeeds(tmp_path: Path) -> None:
    database, batch_id = make_database(tmp_path, 1)

    class FlakyDownloader:
        def __init__(self) -> None:
            self.calls = 0

        def download(self, task, on_progress):
            self.calls += 1
            if self.calls < 3:
                raise DownloadError(ErrorCode.NETWORK, "Connection timed out")
            output = tmp_path / "完成.mp4"
            output.write_bytes(b"video")
            return DownloadResult(task.video_id or "unknown", "完成", output)

    downloader = FlakyDownloader()
    queue = TaskQueue(database, downloader, worker_count=1, retry_delays=(0, 0))
    queue.start()
    try:
        wait_until(lambda: database.get_batch(batch_id)["counts"].get("completed") == 1)
    finally:
        queue.stop()

    task = database.get_batch(batch_id)["tasks"][0]
    assert downloader.calls == 3
    assert task["attempts"] == 3
    assert task["output_path"] == str(tmp_path / "完成.mp4")


def test_proxy_failure_retries_three_times_then_continues_next_task(tmp_path: Path) -> None:
    database, batch_id = make_database(tmp_path, 2)

    class ProxyFailureThenSuccess:
        def __init__(self) -> None:
            self.calls: dict[int, int] = {}

        def download(self, task, on_progress):
            self.calls[task.id] = self.calls.get(task.id, 0) + 1
            if task.position == 1:
                raise DownloadError(ErrorCode.PROXY_UNAVAILABLE, "proxy connection refused")
            output = tmp_path / f"{task.id}.mp4"
            output.write_bytes(b"video")
            return DownloadResult(task.video_id or "unknown", "完成", output)

    downloader = ProxyFailureThenSuccess()
    queue = TaskQueue(database, downloader, worker_count=1, retry_delays=(0, 0))
    queue.start()
    try:
        wait_until(lambda: database.get_batch(batch_id)["counts"] == {"failed": 1, "completed": 1})
    finally:
        queue.stop()

    tasks = database.get_batch(batch_id)["tasks"]
    assert downloader.calls[tasks[0]["id"]] == 3
    assert downloader.calls[tasks[1]["id"]] == 1
    assert tasks[0]["error_message"] == "YouTube 本地代理不可用，请检查代理软件、地址和端口"


def test_unexpected_task_preparation_error_fails_only_that_task_and_queue_continues(tmp_path: Path) -> None:
    database, batch_id = make_database(tmp_path, 2)

    class SuccessfulDownloader:
        calls = 0

        def download(self, task, on_progress):
            self.calls += 1
            output = tmp_path / f"{task.id}.mp4"
            output.write_bytes(b"video")
            return DownloadResult(task.video_id or "unknown", "完成", output)

    downloader = SuccessfulDownloader()
    queue = TaskQueue(database, downloader, worker_count=1, retry_delays=(0, 0))
    original_disk_check = queue._has_disk_space
    disk_checks = 0

    def fail_first_disk_check(directory):
        nonlocal disk_checks
        disk_checks += 1
        if disk_checks == 1:
            raise OSError("temporary task path error")
        return original_disk_check(directory)

    queue._has_disk_space = fail_first_disk_check  # type: ignore[method-assign]
    queue.start()
    try:
        wait_until(
            lambda: database.get_batch(batch_id)["counts"]
            == {"failed": 1, "completed": 1}
        )
    finally:
        queue.stop()

    assert downloader.calls == 1


def test_worker_survives_base_exception_and_processes_remaining_tasks(tmp_path: Path) -> None:
    database, batch_id = make_database(tmp_path, 2)

    class FatalOnceDownloader:
        def __init__(self) -> None:
            self.calls = 0

        def download(self, task, on_progress):
            self.calls += 1
            if self.calls == 1:
                raise SystemExit("simulated worker termination")
            output = tmp_path / f"{task.id}.mp4"
            output.write_bytes(b"video")
            return DownloadResult(task.video_id or "unknown", "completed", output)

    downloader = FatalOnceDownloader()
    queue = TaskQueue(database, downloader, worker_count=1, retry_delays=(0, 0))
    queue.start()
    try:
        wait_until(
            lambda: database.get_batch(batch_id)["counts"]
            == {"failed": 1, "completed": 1}
        )
    finally:
        queue.stop()

    assert downloader.calls == 2


def test_cancel_active_batch_removes_only_its_partial_files(tmp_path: Path) -> None:
    database, batch_id = make_database(tmp_path, 1)
    started = threading.Event()
    stopped = threading.Event()
    completed_file = tmp_path / "keep.mp4"
    completed_file.write_bytes(b"completed")

    class CancellableDownloader:
        def download(self, task, on_progress):
            partial_dir = task.output_dir / ".douyin-part"
            partial_dir.mkdir(parents=True, exist_ok=True)
            (partial_dir / "task-1.mp4.part").write_bytes(b"partial")
            (partial_dir / "task-999.mp4.part").write_bytes(b"other")
            started.set()
            try:
                while True:
                    on_progress({"status": "downloading", "progress": 10.0})
                    time.sleep(0.01)
            except DownloadCancelled:
                stopped.set()
                raise

    queue = TaskQueue(database, CancellableDownloader(), worker_count=1)
    queue.start()
    try:
        assert started.wait(timeout=2)
        queue.cancel_batch(batch_id)
        assert database.delete_batch(batch_id) is True
        assert stopped.wait(timeout=2)
    finally:
        queue.stop()

    partial_dir = tmp_path / ".douyin-part"
    assert not (partial_dir / "task-1.mp4.part").exists()
    assert (partial_dir / "task-999.mp4.part").exists()
    assert completed_file.read_bytes() == b"completed"


def test_transcoding_progress_is_persisted_and_cancellation_cleans_output(tmp_path: Path) -> None:
    database, batch_id = make_database(tmp_path, 1)
    transcoding = threading.Event()
    stopped = threading.Event()

    class TranscodingDownloader:
        def download(self, task, on_progress):
            partial = task.output_dir / ".douyin-part" / f"task-{task.id}.compat.mp4"
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"partial")
            try:
                while True:
                    on_progress({"status": "transcoding", "progress": 42.0})
                    transcoding.set()
                    time.sleep(0.01)
            except DownloadCancelled:
                stopped.set()
                raise

    queue = TaskQueue(database, TranscodingDownloader(), worker_count=1)
    queue.start()
    try:
        assert transcoding.wait(timeout=2)
        wait_until(
            lambda: database.get_batch(batch_id)["counts"].get("transcoding") == 1
        )
        queue.cancel_batch(batch_id)
        assert stopped.wait(timeout=2)
    finally:
        queue.stop()

    assert not (tmp_path / ".douyin-part" / "task-1.compat.mp4").exists()


def test_queue_stop_interrupts_transcoding_and_cleans_task_partials(tmp_path: Path) -> None:
    database, _batch_id = make_database(tmp_path, 1)
    transcoding = threading.Event()
    stopped = threading.Event()
    partial = tmp_path / ".douyin-part" / "task-1.compat.mp4"

    class TranscodingDownloader:
        def download(self, task, on_progress):
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"partial")
            try:
                while True:
                    on_progress({"status": "transcoding", "progress": 10.0})
                    transcoding.set()
                    time.sleep(0.01)
            except DownloadCancelled:
                stopped.set()
                raise

    queue = TaskQueue(database, TranscodingDownloader(), worker_count=1)
    queue.start()
    assert transcoding.wait(timeout=2)

    queue.stop()

    assert stopped.wait(timeout=1)
    assert not partial.exists()


def test_cancel_batch_cleans_unclaimed_task_partials(tmp_path: Path) -> None:
    database, batch_id = make_database(tmp_path, 1)
    partial_dir = tmp_path / ".douyin-part"
    partial_dir.mkdir()
    (partial_dir / "task-1.mp4.part").write_bytes(b"partial")
    (partial_dir / "task-999.mp4.part").write_bytes(b"other")
    queue = TaskQueue(database, downloader=None, worker_count=0)  # type: ignore[arg-type]

    queue.cancel_batch(batch_id)

    assert not (partial_dir / "task-1.mp4.part").exists()
    assert (partial_dir / "task-999.mp4.part").exists()


def test_cancel_batch_tolerates_temporarily_locked_partial(monkeypatch, tmp_path: Path) -> None:
    database, batch_id = make_database(tmp_path, 1)
    partial_dir = tmp_path / ".douyin-part"
    partial_dir.mkdir()
    partial = partial_dir / "task-1.mp4.part"
    partial.write_bytes(b"partial")
    original_unlink = Path.unlink

    attempts = 0

    def locked_unlink(path: Path, *args, **kwargs):
        nonlocal attempts
        if path == partial and attempts == 0:
            attempts += 1
            raise PermissionError("file is in use")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked_unlink)
    queue = TaskQueue(database, downloader=None, worker_count=0)  # type: ignore[arg-type]

    queue.cancel_batch(batch_id)

    wait_until(lambda: not partial.exists(), timeout=1)
    assert attempts == 1


def test_cancel_batch_interrupts_retry_backoff(tmp_path: Path) -> None:
    database, first_batch = make_database(tmp_path, 1)
    first_failed = threading.Event()

    class RetryDownloader:
        def download(self, task, on_progress):
            if task.batch_id == first_batch:
                first_failed.set()
                raise DownloadError(ErrorCode.NETWORK, "Connection timed out")
            output = tmp_path / f"{task.id}.mp4"
            output.write_bytes(b"video")
            return DownloadResult(task.video_id or "unknown", "completed", output)

    queue = TaskQueue(database, RetryDownloader(), worker_count=1, retry_delays=(5.0, 5.0))
    queue.start()
    try:
        assert first_failed.wait(timeout=2)
        queue.cancel_batch(first_batch)
        assert database.delete_batch(first_batch) is True
        second_batch = database.create_batch(
            ["https://www.douyin.com/video/2234567890123456789"], tmp_path
        )
        queue.wake()
        wait_until(
            lambda: database.get_batch(second_batch)["counts"].get("completed") == 1,
            timeout=1,
        )
    finally:
        queue.stop()


def test_pause_active_batch_preserves_partial_and_other_batches_continue(tmp_path: Path) -> None:
    database, first_batch = make_database(tmp_path, 1)
    second_batch = database.create_batch(
        ["https://www.douyin.com/video/2234567890123456789"], tmp_path
    )
    first_started = threading.Event()
    first_paused = threading.Event()

    class PausableDownloader:
        def __init__(self) -> None:
            self.first_calls = 0

        def download(self, task, on_progress):
            partial_dir = task.output_dir / ".douyin-part"
            partial_dir.mkdir(parents=True, exist_ok=True)
            partial = partial_dir / f"task-{task.id}.mp4.part"
            if task.batch_id == first_batch:
                self.first_calls += 1
                if self.first_calls == 1:
                    partial.write_bytes(b"partial")
                    first_started.set()
                    try:
                        while True:
                            on_progress({"status": "downloading", "progress": 25.0})
                            time.sleep(0.01)
                    except DownloadPaused:
                        first_paused.set()
                        raise
                assert partial.read_bytes() == b"partial"
            output = tmp_path / f"{task.id}.mp4"
            output.write_bytes(b"video")
            return DownloadResult(task.video_id or "unknown", "completed", output)

    downloader = PausableDownloader()
    queue = TaskQueue(database, downloader, worker_count=1)
    queue.start()
    try:
        assert first_started.wait(timeout=2)
        queue.pause_batch(first_batch)
        assert first_paused.wait(timeout=2)
        wait_until(
            lambda: database.get_batch(second_batch)["counts"].get("completed") == 1
        )

        paused = database.get_batch(first_batch)
        assert paused["paused"] is True
        assert paused["counts"] == {"queued": 1}
        assert (tmp_path / ".douyin-part" / "task-1.mp4.part").read_bytes() == b"partial"
        assert downloader.first_calls == 1

        queue.resume_batch(first_batch)
        wait_until(
            lambda: database.get_batch(first_batch)["counts"].get("completed") == 1
        )
    finally:
        queue.stop()

    assert database.get_batch(first_batch)["paused"] is False
    assert downloader.first_calls == 2


def test_pause_interrupts_retry_backoff_without_deleting_partial(tmp_path: Path) -> None:
    database, batch_id = make_database(tmp_path, 1)
    failed = threading.Event()
    partial = tmp_path / ".douyin-part" / "task-1.mp4.part"

    class RetryDownloader:
        calls = 0

        def download(self, task, on_progress):
            self.calls += 1
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"partial")
            failed.set()
            raise DownloadError(ErrorCode.NETWORK, "Connection timed out")

    downloader = RetryDownloader()
    queue = TaskQueue(database, downloader, worker_count=1, retry_delays=(5.0, 5.0))
    queue.start()
    try:
        assert failed.wait(timeout=2)
        queue.pause_batch(batch_id)
        wait_until(
            lambda: database.get_batch(batch_id)["counts"] == {"queued": 1},
            timeout=1,
        )
    finally:
        queue.stop()

    assert downloader.calls == 1
    assert partial.read_bytes() == b"partial"


def test_permanent_login_failure_is_not_retried(tmp_path: Path) -> None:
    database, batch_id = make_database(tmp_path, 1)

    class PrivateDownloader:
        calls = 0

        def download(self, task, on_progress):
            self.calls += 1
            raise DownloadError(ErrorCode.LOGIN_REQUIRED, "private")

    downloader = PrivateDownloader()
    queue = TaskQueue(database, downloader, worker_count=1, retry_delays=(0, 0))
    queue.start()
    try:
        wait_until(lambda: database.get_batch(batch_id)["counts"].get("failed") == 1)
    finally:
        queue.stop()

    task = database.get_batch(batch_id)["tasks"][0]
    assert downloader.calls == 1
    assert task["error_code"] == "login_required"
    assert task["error_message"] == "该视频需要登录或无权访问"
