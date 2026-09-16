from __future__ import annotations

import shutil
import logging
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from .downloader import (
    DownloadCancelled,
    DownloadError,
    DownloadPaused,
    DownloadResult,
    ErrorCode,
    ERROR_MESSAGES,
)
from .store import Database, TaskRecord, TaskStatus


class Downloader(Protocol):
    def download(self, task: TaskRecord, on_progress) -> DownloadResult: ...


RETRYABLE_ERRORS = {
    ErrorCode.NETWORK,
    ErrorCode.PROXY_UNAVAILABLE,
    ErrorCode.RATE_LIMITED,
    ErrorCode.FRESH_COOKIE,
}
YOUTUBE_CIRCUIT_ERRORS = {
    ErrorCode.ACCESS_RESTRICTED,
    ErrorCode.LOGIN_REQUIRED,
    ErrorCode.YOUTUBE_UNAVAILABLE,
}
YOUTUBE_CIRCUIT_THRESHOLD = 3
YOUTUBE_CIRCUIT_MESSAGE = "连续多个 YouTube 视频在当前网络或账号下不可用，批次已暂停；请测试网络或重新完成 YouTube 登录验证后继续"


class TaskQueue:
    def __init__(
        self,
        database: Database,
        downloader: Downloader,
        worker_count: int = 2,
        retry_delays: tuple[float, ...] = (2.0, 5.0, 15.0, 30.0),
        minimum_free_bytes: int = 1024**3,
    ) -> None:
        self.database = database
        self.downloader = downloader
        self.worker_count = worker_count
        self.retry_delays = retry_delays
        self.minimum_free_bytes = minimum_free_bytes
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._cancel_lock = threading.Lock()
        self._cancelled_batches: set[int] = set()
        self._paused_batches: set[int] = set()
        self._youtube_failure_lock = threading.Lock()
        self._youtube_access_failures: dict[int, int] = {}
        self.pause_reason: str | None = None

    def start(self) -> None:
        if self._threads:
            return
        self._stop_event.clear()
        for index in range(self.worker_count):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"download-worker-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._threads.append(thread)

    def stop(self, timeout: float = 10.0) -> None:
        self._stop_event.set()
        self._wake_event.set()
        for thread in self._threads:
            thread.join(timeout=timeout)
        self._threads.clear()

    def wake(self) -> None:
        self._wake_event.set()

    def cancel_batch(self, batch_id: int) -> None:
        with self._cancel_lock:
            self._cancelled_batches.add(batch_id)
            self._paused_batches.discard(batch_id)
        try:
            batch = self.database.get_batch(batch_id)
        except KeyError:
            batch = None
        if batch is not None:
            output_dir = Path(batch["output_dir"])
            pending: list[tuple[Path, int]] = []
            for task in batch["tasks"]:
                task_id = int(task["id"])
                if not self._cleanup_task_partials(output_dir, task_id):
                    pending.append((output_dir, task_id))
            if pending:
                self._schedule_cleanup_retry(pending)
        self._wake_event.set()

    def pause_batch(self, batch_id: int) -> None:
        self.database.set_batch_paused(batch_id, True)
        with self._cancel_lock:
            self._paused_batches.add(batch_id)
        self._wake_event.set()

    def resume_batch(self, batch_id: int) -> None:
        self.database.set_batch_paused(batch_id, False, pause_reason=None)
        with self._cancel_lock:
            self._paused_batches.discard(batch_id)
        with self._youtube_failure_lock:
            self._youtube_access_failures.pop(batch_id, None)
        self._wake_event.set()

    def _is_cancelled(self, batch_id: int) -> bool:
        with self._cancel_lock:
            return batch_id in self._cancelled_batches

    def _is_paused(self, batch_id: int) -> bool:
        with self._cancel_lock:
            return batch_id in self._paused_batches

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            task: TaskRecord | None = None
            try:
                task = self.database.claim_next_task()
                if task is None:
                    self._wake_event.wait(0.2)
                    self._wake_event.clear()
                    continue
                if self._is_cancelled(task.batch_id):
                    self._cleanup_partial_files(task)
                    continue
                if self._is_paused(task.batch_id):
                    self.database.update_task(task.id, status=TaskStatus.QUEUED)
                    continue
                if not self._has_disk_space(task.output_dir):
                    self.pause_reason = "磁盘剩余空间不足 1 GB，队列已暂停"
                    self.database.update_task(
                        task.id,
                        status=TaskStatus.QUEUED,
                        error_code=ErrorCode.DISK_ERROR.value,
                        error_message=self.pause_reason,
                    )
                    self._stop_event.wait(1.0)
                    continue
                self.pause_reason = None
                self._process_task(task)
            except BaseException as exc:
                if task is not None:
                    try:
                        self.database.update_task(
                            task.id,
                            status=TaskStatus.FAILED,
                            error_code=ErrorCode.UNKNOWN.value,
                            error_message=f"{ERROR_MESSAGES[ErrorCode.UNKNOWN]}: {exc}",
                        )
                    except Exception:
                        logging.exception("工作器无法记录任务 %s 的异常状态", task.id)
                logging.exception("下载工作器捕获到未处理异常")

    def _has_disk_space(self, directory: Path) -> bool:
        directory.mkdir(parents=True, exist_ok=True)
        return shutil.disk_usage(directory).free >= self.minimum_free_bytes

    def _process_task(self, task: TaskRecord) -> None:
        attempts = len(self.retry_delays) + 1
        for attempt in range(1, attempts + 1):
            if self._is_cancelled(task.batch_id):
                self._cleanup_partial_files(task)
                return
            if self._is_paused(task.batch_id):
                self.database.update_task(task.id, status=TaskStatus.QUEUED)
                return
            if self._stop_event.is_set():
                self.database.update_task(task.id, status=TaskStatus.QUEUED)
                return
            self.database.update_task(
                task.id,
                attempts=attempt,
                status=TaskStatus.RESOLVING,
                error_code=None,
                error_message=None,
            )
            try:
                result = self.downloader.download(
                    task,
                    lambda event: self._on_progress(task, event),
                )
            except DownloadCancelled:
                self._cleanup_partial_files(task)
                return
            except DownloadPaused:
                self.database.update_task(
                    task.id,
                    status=TaskStatus.QUEUED,
                    speed=None,
                    eta=None,
                    error_code=None,
                    error_message=None,
                )
                return
            except DownloadError as exc:
                if exc.code in RETRYABLE_ERRORS and attempt < attempts:
                    self.database.update_task(
                        task.id,
                        error_code=exc.code.value,
                        error_message=exc.user_message,
                    )
                    if self._wait_for_retry(task, self.retry_delays[attempt - 1]):
                        if self._is_paused(task.batch_id):
                            self.database.update_task(
                                task.id,
                                status=TaskStatus.QUEUED,
                                speed=None,
                                eta=None,
                                error_code=None,
                                error_message=None,
                            )
                        else:
                            self._cleanup_partial_files(task)
                        return
                    continue
                self.database.update_task(
                    task.id,
                    status=TaskStatus.FAILED,
                    error_code=exc.code.value,
                    error_message=exc.user_message,
                )
                self._record_youtube_failure(task, exc.code)
                return
            except Exception as exc:
                self.database.update_task(
                    task.id,
                    status=TaskStatus.FAILED,
                    error_code=ErrorCode.UNKNOWN.value,
                    error_message=f"{ERROR_MESSAGES[ErrorCode.UNKNOWN]}: {exc}",
                )
                return
            if self._is_cancelled(task.batch_id):
                return
            self.database.update_task(
                task.id,
                video_id=result.video_id,
                title=result.title,
                status=TaskStatus.COMPLETED,
                progress=100.0,
                output_path=str(result.output_path),
                speed=None,
                eta=None,
                error_code=None,
                error_message=None,
            )
            if task.platform == "youtube":
                with self._youtube_failure_lock:
                    self._youtube_access_failures.pop(task.batch_id, None)
            return

    def _record_youtube_failure(self, task: TaskRecord, code: ErrorCode) -> None:
        if task.platform != "youtube" or code not in YOUTUBE_CIRCUIT_ERRORS:
            return
        with self._youtube_failure_lock:
            failures = self._youtube_access_failures.get(task.batch_id, 0) + 1
            self._youtube_access_failures[task.batch_id] = failures
        if failures < YOUTUBE_CIRCUIT_THRESHOLD:
            return
        self.database.set_batch_paused(
            task.batch_id,
            True,
            pause_reason=YOUTUBE_CIRCUIT_MESSAGE,
        )
        with self._cancel_lock:
            self._paused_batches.add(task.batch_id)
        self._wake_event.set()

    def _wait_for_retry(self, task: TaskRecord, delay: float) -> bool:
        deadline = time.monotonic() + delay
        while True:
            if (
                self._stop_event.is_set()
                or self._is_cancelled(task.batch_id)
                or self._is_paused(task.batch_id)
            ):
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            self._stop_event.wait(min(0.1, remaining))

    def _on_progress(self, task: TaskRecord, event: dict[str, Any]) -> None:
        if self._is_cancelled(task.batch_id):
            raise DownloadCancelled()
        if self._stop_event.is_set():
            raise DownloadCancelled()
        if self._is_paused(task.batch_id):
            raise DownloadPaused()
        fields = {key: value for key, value in event.items() if key != "status"}
        status = event.get("status")
        if status == "downloading":
            fields["status"] = TaskStatus.DOWNLOADING
        elif status == "merging":
            fields["status"] = TaskStatus.MERGING
        elif status == "transcoding":
            fields["status"] = TaskStatus.TRANSCODING
        self.database.update_task(task.id, **fields)

    def _cleanup_partial_files(self, task: TaskRecord) -> None:
        if not self._cleanup_task_partials(task.output_dir, task.id):
            self._schedule_cleanup_retry([(task.output_dir, task.id)])

    @staticmethod
    def _cleanup_task_partials(output_dir: Path, task_id: int) -> bool:
        partial_dir = output_dir / ".douyin-part"
        temp_key = f"task-{task_id}"
        if not partial_dir.is_dir():
            return True
        cleaned = True
        for candidate in partial_dir.glob(f"{temp_key}.*"):
            if candidate.is_file():
                try:
                    candidate.unlink(missing_ok=True)
                except OSError:
                    cleaned = False
        return cleaned

    def _schedule_cleanup_retry(self, targets: list[tuple[Path, int]]) -> None:
        thread = threading.Thread(
            target=self._retry_cleanup,
            args=(targets,),
            name="partial-cleanup",
            daemon=True,
        )
        thread.start()

    def _retry_cleanup(self, targets: list[tuple[Path, int]]) -> None:
        pending = targets
        for _ in range(20):
            pending = [
                target
                for target in pending
                if not self._cleanup_task_partials(*target)
            ]
            if not pending or self._stop_event.wait(0.25):
                return
