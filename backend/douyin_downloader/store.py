from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


VIDEO_ID_PATTERN = re.compile(r"/video/(\d+)")


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RESOLVING = "resolving"
    DOWNLOADING = "downloading"
    MERGING = "merging"
    TRANSCODING = "transcoding"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


ACTIVE_STATUSES = (
    TaskStatus.RESOLVING,
    TaskStatus.DOWNLOADING,
    TaskStatus.MERGING,
    TaskStatus.TRANSCODING,
)


@dataclass(slots=True)
class TaskRecord:
    id: int
    batch_id: int
    original_url: str
    canonical_url: str
    video_id: str | None
    title: str | None
    status: TaskStatus
    progress: float
    output_dir: Path
    attempts: int
    platform: str = "douyin"


class Database:
    _UPDATABLE_COLUMNS = {
        "canonical_url",
        "video_id",
        "title",
        "status",
        "progress",
        "downloaded_bytes",
        "total_bytes",
        "speed",
        "eta",
        "output_path",
        "attempts",
        "error_code",
        "error_message",
        "platform",
    }

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    output_dir TEXT NOT NULL,
                    paused INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES batches(id) ON DELETE CASCADE,
                    original_url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'douyin',
                    video_id TEXT,
                    title TEXT,
                    status TEXT NOT NULL DEFAULT 'queued',
                    progress REAL NOT NULL DEFAULT 0,
                    downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                    total_bytes INTEGER,
                    speed REAL,
                    eta REAL,
                    output_path TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, id);
                CREATE INDEX IF NOT EXISTS idx_tasks_batch ON tasks(batch_id, id);
                CREATE INDEX IF NOT EXISTS idx_tasks_video ON tasks(video_id);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            batch_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(batches)")
            }
            if "paused" not in batch_columns:
                connection.execute(
                    "ALTER TABLE batches ADD COLUMN paused INTEGER NOT NULL DEFAULT 0"
                )
            task_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(tasks)")
            }
            if "platform" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN platform TEXT NOT NULL DEFAULT 'douyin'"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tasks_platform_video ON tasks(platform, video_id)"
            )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_batch(self, urls: list[str], output_dir: Path) -> int:
        now = self._now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO batches(output_dir, created_at) VALUES (?, ?)",
                (str(output_dir), now),
            )
            batch_id = int(cursor.lastrowid)
            rows = []
            for url in urls:
                match = VIDEO_ID_PATTERN.search(url)
                rows.append(
                    (
                        batch_id,
                        url,
                        url,
                        match.group(1) if match else None,
                        TaskStatus.QUEUED.value,
                        now,
                        now,
                    )
                )
            connection.executemany(
                """
                INSERT INTO tasks(
                    batch_id, original_url, canonical_url, video_id, status,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return batch_id

    def create_expanded_batch(self, videos: list[Any], output_dir: Path) -> int:
        now = self._now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "INSERT INTO batches(output_dir, created_at) VALUES (?, ?)",
                (str(output_dir), now),
            )
            batch_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO tasks(
                    batch_id, original_url, canonical_url, platform, video_id,
                    title, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        batch_id,
                        video.original_url,
                        video.canonical_url,
                        video.platform,
                        video.video_id,
                        video.title or None,
                        TaskStatus.QUEUED.value,
                        now,
                        now,
                    )
                    for video in videos
                ],
            )
            return batch_id

    def get_batch(self, batch_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            batch = connection.execute(
                "SELECT id, output_dir, paused, created_at FROM batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            tasks = [dict(row) for row in connection.execute(
                "SELECT * FROM tasks WHERE batch_id = ? ORDER BY id", (batch_id,)
            )]
        counts: dict[str, int] = {}
        for task in tasks:
            status = str(task["status"])
            counts[status] = counts.get(status, 0) + 1
        return {
            "id": int(batch["id"]),
            "output_dir": batch["output_dir"],
            "paused": bool(batch["paused"]),
            "created_at": batch["created_at"],
            "total": len(tasks),
            "counts": counts,
            "tasks": tasks,
        }

    def list_batches(self, page: int, page_size: int) -> dict[str, Any]:
        offset = (page - 1) * page_size
        with self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM batches").fetchone()[0])
            batches = connection.execute(
                """
                SELECT id, output_dir, paused, created_at FROM batches
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ).fetchall()
            items: list[dict[str, Any]] = []
            for batch in batches:
                status_rows = connection.execute(
                    """
                    SELECT status, COUNT(*) AS count FROM tasks
                    WHERE batch_id = ? GROUP BY status
                    """,
                    (batch["id"],),
                ).fetchall()
                counts = {str(row["status"]): int(row["count"]) for row in status_rows}
                items.append(
                    {
                        "id": int(batch["id"]),
                        "output_dir": str(batch["output_dir"]),
                        "paused": bool(batch["paused"]),
                        "created_at": str(batch["created_at"]),
                        "total": sum(counts.values()),
                        "counts": counts,
                    }
                )
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": (total + page_size - 1) // page_size,
        }

    def list_batch_ids(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM batches ORDER BY id DESC"
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def delete_batch(self, batch_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
            return cursor.rowcount == 1

    def set_batch_paused(self, batch_id: int, paused: bool) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE batches SET paused = ? WHERE id = ?",
                (int(paused), batch_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(batch_id)

    def claim_next_task(self) -> TaskRecord | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT tasks.*, batches.output_dir
                FROM tasks JOIN batches ON batches.id = tasks.batch_id
                WHERE tasks.status = ? AND batches.paused = 0
                ORDER BY tasks.id LIMIT 1
                """,
                (TaskStatus.QUEUED.value,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            changed = connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (
                    TaskStatus.RESOLVING.value,
                    self._now(),
                    row["id"],
                    TaskStatus.QUEUED.value,
                ),
            ).rowcount
            connection.commit()
            if changed != 1:
                return None
            return TaskRecord(
                id=int(row["id"]),
                batch_id=int(row["batch_id"]),
                original_url=str(row["original_url"]),
                canonical_url=str(row["canonical_url"]),
                video_id=row["video_id"],
                title=row["title"],
                status=TaskStatus.RESOLVING,
                progress=float(row["progress"]),
                output_dir=Path(row["output_dir"]),
                attempts=int(row["attempts"]),
                platform=str(row["platform"]),
            )

    def update_task(self, task_id: int, **fields: Any) -> None:
        unknown = set(fields) - self._UPDATABLE_COLUMNS
        if unknown:
            raise ValueError(f"不允许更新字段: {', '.join(sorted(unknown))}")
        if not fields:
            return
        normalized = {
            key: value.value if isinstance(value, TaskStatus) else value
            for key, value in fields.items()
        }
        normalized["updated_at"] = self._now()
        assignments = ", ".join(f"{key} = ?" for key in normalized)
        with self._lock, self._connect() as connection:
            connection.execute(
                f"UPDATE tasks SET {assignments} WHERE id = ?",
                (*normalized.values(), task_id),
            )

    def recover_interrupted(self) -> int:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE tasks SET status = ?, progress = 0, speed = NULL, eta = NULL,
                    error_code = NULL, error_message = NULL, updated_at = ?
                WHERE status IN ({placeholders})
                """,
                (
                    TaskStatus.QUEUED.value,
                    self._now(),
                    *(status.value for status in ACTIVE_STATUSES),
                ),
            )
            return cursor.rowcount

    def retry_failed(self, batch_id: int) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks SET status = ?, progress = 0, downloaded_bytes = 0,
                    speed = NULL, eta = NULL, error_code = NULL, error_message = NULL,
                    attempts = 0, updated_at = ?
                WHERE batch_id = ? AND status = ?
                """,
                (
                    TaskStatus.QUEUED.value,
                    self._now(),
                    batch_id,
                    TaskStatus.FAILED.value,
                ),
            )
            return cursor.rowcount

    def skip_existing_completed(self, batch_id: int) -> int:
        with self._lock, self._connect() as connection:
            new_tasks = connection.execute(
                "SELECT id, platform, video_id FROM tasks WHERE batch_id = ? AND video_id IS NOT NULL",
                (batch_id,),
            ).fetchall()
            skipped = 0
            for task in new_tasks:
                previous = connection.execute(
                    """
                    SELECT title, output_path FROM tasks
                    WHERE batch_id != ? AND platform = ? AND video_id = ? AND status = ?
                        AND output_path IS NOT NULL
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        batch_id,
                        task["platform"],
                        task["video_id"],
                        TaskStatus.COMPLETED.value,
                    ),
                ).fetchone()
                if previous is None or not Path(previous["output_path"]).is_file():
                    continue
                connection.execute(
                    """
                    UPDATE tasks SET status = ?, progress = 100, title = ?, output_path = ?,
                        error_message = ?, updated_at = ? WHERE id = ?
                    """,
                    (
                        TaskStatus.SKIPPED.value,
                        previous["title"],
                        previous["output_path"],
                        "历史记录中已下载",
                        self._now(),
                        task["id"],
                    ),
                )
                skipped += 1
            return skipped

    def get_setting(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
