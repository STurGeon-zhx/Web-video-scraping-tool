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
    position: int = 0


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
                    pause_reason TEXT,
                    source_mode TEXT NOT NULL DEFAULT 'links',
                    source_url TEXT,
                    requested_count INTEGER,
                    collected_count INTEGER NOT NULL DEFAULT 0,
                    collection_status TEXT,
                    collection_stop_reason TEXT,
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
            if "pause_reason" not in batch_columns:
                connection.execute("ALTER TABLE batches ADD COLUMN pause_reason TEXT")
            batch_migrations = {
                "source_mode": "TEXT NOT NULL DEFAULT 'links'",
                "source_url": "TEXT",
                "requested_count": "INTEGER",
                "collected_count": "INTEGER NOT NULL DEFAULT 0",
                "collection_status": "TEXT",
                "collection_stop_reason": "TEXT",
            }
            for column, definition in batch_migrations.items():
                if column not in batch_columns:
                    connection.execute(
                        f"ALTER TABLE batches ADD COLUMN {column} {definition}"
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
            rows = []
            for video in videos:
                previous = connection.execute(
                    """
                    SELECT title, output_path FROM tasks
                    WHERE platform = ? AND video_id = ? AND status = ?
                        AND output_path IS NOT NULL
                    ORDER BY id DESC LIMIT 1
                    """,
                    (
                        video.platform,
                        video.video_id,
                        TaskStatus.COMPLETED.value,
                    ),
                ).fetchone()
                existing_file = (
                    previous
                    if previous is not None and Path(previous["output_path"]).is_file()
                    else None
                )
                rows.append(
                    (
                        batch_id,
                        video.original_url,
                        video.canonical_url,
                        video.platform,
                        video.video_id,
                        (
                            existing_file["title"]
                            if existing_file is not None
                            else (video.title or None)
                        ),
                        (
                            TaskStatus.SKIPPED.value
                            if existing_file is not None
                            else TaskStatus.QUEUED.value
                        ),
                        100 if existing_file is not None else 0,
                        (
                            existing_file["output_path"]
                            if existing_file is not None
                            else None
                        ),
                        "历史记录中已下载" if existing_file is not None else None,
                        now,
                        now,
                    )
                )
            connection.executemany(
                """
                INSERT INTO tasks(
                    batch_id, original_url, canonical_url, platform, video_id,
                    title, status, progress, output_path, error_message,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return batch_id

    def create_page_batch(
        self,
        source_url: str,
        requested_count: int,
        output_dir: Path,
    ) -> int:
        if not 1 <= requested_count <= 500:
            raise ValueError("requested_count must be between 1 and 500")
        now = self._now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO batches(
                    output_dir, source_mode, source_url, requested_count,
                    collected_count, collection_status, created_at
                ) VALUES (?, 'page', ?, ?, 0, 'pending', ?)
                """,
                (str(output_dir), source_url, requested_count, now),
            )
            return int(cursor.lastrowid)

    def append_page_video(
        self,
        batch_id: int,
        video: Any,
        *,
        skip_history_duplicate: bool = False,
    ) -> bool:
        now = self._now()
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            batch = connection.execute(
                """
                SELECT source_mode, requested_count, collected_count
                FROM batches WHERE id = ?
                """,
                (batch_id,),
            ).fetchone()
            if batch is None:
                connection.rollback()
                raise KeyError(batch_id)
            if batch["source_mode"] != "page":
                connection.rollback()
                raise ValueError("batch is not a page collection")
            if int(batch["collected_count"]) >= int(batch["requested_count"]):
                connection.rollback()
                return False
            duplicate = connection.execute(
                """
                SELECT 1 FROM tasks
                WHERE batch_id = ? AND platform = ? AND video_id = ?
                """,
                (batch_id, video.platform, video.video_id),
            ).fetchone()
            if duplicate is not None:
                connection.rollback()
                return False
            previous = connection.execute(
                """
                SELECT title, output_path FROM tasks
                WHERE batch_id != ? AND platform = ? AND video_id = ? AND status = ?
                    AND output_path IS NOT NULL
                ORDER BY id DESC LIMIT 1
                """,
                (
                    batch_id,
                    video.platform,
                    video.video_id,
                    TaskStatus.COMPLETED.value,
                ),
            ).fetchone()
            existing_file = (
                previous
                if previous is not None and Path(previous["output_path"]).is_file()
                else None
            )
            if existing_file is not None and skip_history_duplicate:
                connection.rollback()
                return False
            initial_status = (
                TaskStatus.SKIPPED.value
                if existing_file is not None
                else TaskStatus.QUEUED.value
            )
            initial_title = (
                existing_file["title"]
                if existing_file is not None
                else (video.title or None)
            )
            initial_output = (
                existing_file["output_path"] if existing_file is not None else None
            )
            initial_progress = 100 if existing_file is not None else 0
            initial_message = "历史记录中已下载" if existing_file is not None else None
            connection.execute(
                """
                INSERT INTO tasks(
                    batch_id, original_url, canonical_url, platform, video_id,
                    title, status, progress, output_path, error_message,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    video.original_url,
                    video.canonical_url,
                    video.platform,
                    video.video_id,
                    initial_title,
                    initial_status,
                    initial_progress,
                    initial_output,
                    initial_message,
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE batches SET collected_count = collected_count + 1 WHERE id = ?",
                (batch_id,),
            )
            connection.commit()
            return True

    def get_batch(self, batch_id: int) -> dict[str, Any]:
        with self._connect() as connection:
            batch = connection.execute(
                """
                SELECT id, output_dir, paused, pause_reason, source_mode, source_url,
                    requested_count, collected_count, collection_status,
                    collection_stop_reason, created_at
                FROM batches WHERE id = ?
                """,
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
            "pause_reason": batch["pause_reason"],
            "source_mode": str(batch["source_mode"]),
            "source_url": batch["source_url"],
            "requested_count": batch["requested_count"],
            "collected_count": int(batch["collected_count"]),
            "collection_status": batch["collection_status"],
            "collection_stop_reason": batch["collection_stop_reason"],
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
                SELECT id, output_dir, paused, pause_reason, source_mode, source_url,
                    requested_count, collected_count, collection_status,
                    collection_stop_reason, created_at FROM batches
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
                        "pause_reason": batch["pause_reason"],
                        "source_mode": str(batch["source_mode"]),
                        "source_url": batch["source_url"],
                        "requested_count": batch["requested_count"],
                        "collected_count": int(batch["collected_count"]),
                        "collection_status": batch["collection_status"],
                        "collection_stop_reason": batch["collection_stop_reason"],
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

    def list_resumable_page_batches(self) -> list[int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id FROM batches
                WHERE source_mode = 'page'
                    AND collection_status IN ('pending', 'waiting_login', 'collecting')
                ORDER BY id
                """
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def update_collection(
        self,
        batch_id: int,
        status: str,
        stop_reason: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE batches
                SET collection_status = ?, collection_stop_reason = ?
                WHERE id = ? AND source_mode = 'page'
                """,
                (status, stop_reason, batch_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(batch_id)

    def delete_batch(self, batch_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM batches WHERE id = ?", (batch_id,))
            return cursor.rowcount == 1

    def set_batch_paused(
        self,
        batch_id: int,
        paused: bool,
        pause_reason: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE batches SET paused = ?, pause_reason = ? WHERE id = ?",
                (int(paused), pause_reason if paused else None, batch_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(batch_id)

    def claim_next_task(self) -> TaskRecord | None:
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT tasks.*, batches.output_dir,
                    (
                        SELECT COUNT(*) FROM tasks AS ordered_tasks
                        WHERE ordered_tasks.batch_id = tasks.batch_id
                            AND ordered_tasks.id <= tasks.id
                    ) AS batch_position
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
                position=int(row["batch_position"]),
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
                """
                SELECT id, platform, video_id FROM tasks
                WHERE batch_id = ? AND video_id IS NOT NULL AND status = ?
                """,
                (batch_id, TaskStatus.QUEUED.value),
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
                changed = connection.execute(
                    """
                    UPDATE tasks SET status = ?, progress = 100, title = ?, output_path = ?,
                        error_message = ?, updated_at = ? WHERE id = ? AND status = ?
                    """,
                    (
                        TaskStatus.SKIPPED.value,
                        previous["title"],
                        previous["output_path"],
                        "历史记录中已下载",
                        self._now(),
                        task["id"],
                        TaskStatus.QUEUED.value,
                    ),
                ).rowcount
                if changed == 1:
                    skipped += 1
            return skipped

    def get_setting(self, key: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None

    def get_settings(self, keys: tuple[str, ...]) -> dict[str, str]:
        if not keys:
            return {}
        placeholders = ",".join("?" for _key in keys)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
                keys,
            ).fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def set_settings(self, values: dict[str, str]) -> None:
        if not values:
            return
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                tuple(values.items()),
            )
