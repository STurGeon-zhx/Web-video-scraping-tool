import threading
import time
from pathlib import Path

from douyin_downloader.downloader import DownloadCancelled, DownloadError, DownloadResult, ErrorCode
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
