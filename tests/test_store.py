from pathlib import Path
import sqlite3

import pytest

from douyin_downloader.store import Database, TaskStatus


def make_database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "tasks.db")
    database.initialize()
    return database


def test_creates_batch_with_five_hundred_queued_tasks(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    urls = [f"https://www.douyin.com/video/{10_000_000_000_000_000_000 + index}" for index in range(500)]

    batch_id = database.create_batch(urls, tmp_path / "downloads")
    batch = database.get_batch(batch_id)

    assert batch["total"] == 500
    assert batch["counts"] == {TaskStatus.QUEUED.value: 500}
    assert batch["tasks"][0]["original_url"] == urls[0]


def test_claim_next_task_is_atomic_and_marks_it_resolving(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    batch_id = database.create_batch(
        ["https://www.douyin.com/video/1234567890123456789"], tmp_path
    )

    claimed = database.claim_next_task()

    assert claimed is not None
    assert claimed.batch_id == batch_id
    assert claimed.status == TaskStatus.RESOLVING
    assert database.claim_next_task() is None


def test_paused_batch_is_not_claimed_until_resumed(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    batch_id = database.create_batch(
        ["https://www.douyin.com/video/1234567890123456789"], tmp_path
    )

    database.set_batch_paused(batch_id, True)

    assert database.get_batch(batch_id)["paused"] is True
    assert database.claim_next_task() is None

    database.set_batch_paused(batch_id, False)

    assert database.get_batch(batch_id)["paused"] is False
    assert database.claim_next_task() is not None


def test_initialize_adds_pause_state_to_existing_database(tmp_path: Path) -> None:
    path = tmp_path / "existing.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                output_dir TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

    database = Database(path)
    database.initialize()
    batch_id = database.create_batch(
        ["https://www.douyin.com/video/1234567890123456789"], tmp_path
    )

    assert database.get_batch(batch_id)["paused"] is False


def test_recovers_interrupted_tasks_to_queue_on_startup(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    database.create_batch(
        [
            "https://www.douyin.com/video/1234567890123456789",
            "https://www.douyin.com/video/2234567890123456789",
        ],
        tmp_path,
    )
    first = database.claim_next_task()
    second = database.claim_next_task()
    assert first and second
    database.update_task(first.id, status=TaskStatus.DOWNLOADING, progress=55.5)
    database.update_task(second.id, status=TaskStatus.MERGING, progress=100.0)

    recovered = database.recover_interrupted()
    tasks = database.get_batch(first.batch_id)["tasks"]

    assert recovered == 2
    assert [task["status"] for task in tasks] == ["queued", "queued"]
    assert [task["progress"] for task in tasks] == [0.0, 0.0]


def test_recovers_interrupted_transcoding_task_to_queue_on_startup(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    batch_id = database.create_batch(
        ["https://www.douyin.com/video/1234567890123456789"],
        tmp_path,
    )
    task = database.claim_next_task()
    assert task
    database.update_task(task.id, status=TaskStatus.TRANSCODING, progress=47.0)

    assert database.recover_interrupted() == 1

    recovered = database.get_batch(batch_id)["tasks"][0]
    assert recovered["status"] == "queued"
    assert recovered["progress"] == 0.0


def test_retry_failed_only_requeues_failed_tasks(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    batch_id = database.create_batch(
        [
            "https://www.douyin.com/video/1234567890123456789",
            "https://www.douyin.com/video/2234567890123456789",
        ],
        tmp_path,
    )
    first = database.claim_next_task()
    second = database.claim_next_task()
    assert first and second
    database.update_task(first.id, status=TaskStatus.FAILED, error_code="network")
    database.update_task(second.id, status=TaskStatus.COMPLETED, progress=100.0)

    count = database.retry_failed(batch_id)
    tasks = database.get_batch(batch_id)["tasks"]

    assert count == 1
    assert tasks[0]["status"] == "queued"
    assert tasks[0]["error_code"] is None
    assert tasks[1]["status"] == "completed"


def test_lists_batches_newest_first_with_pagination(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    batch_ids = [
        database.create_batch(
            [f"https://www.douyin.com/video/{1234567890123456789 + index}"],
            tmp_path / f"downloads-{index}",
        )
        for index in range(3)
    ]

    first_page = database.list_batches(page=1, page_size=2)
    second_page = database.list_batches(page=2, page_size=2)

    assert [item["id"] for item in first_page["items"]] == [batch_ids[2], batch_ids[1]]
    assert first_page["total"] == 3
    assert first_page["total_pages"] == 2
    assert first_page["items"][0]["counts"] == {"queued": 1}
    assert [item["id"] for item in second_page["items"]] == [batch_ids[0]]


def test_lists_all_batch_ids_newest_first(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    batch_ids = [
        database.create_batch(
            [f"https://www.douyin.com/video/{1234567890123456789 + index}"],
            tmp_path,
        )
        for index in range(3)
    ]

    assert database.list_batch_ids() == list(reversed(batch_ids))


def test_delete_batch_cascades_tasks_without_deleting_output_file(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    output = tmp_path / "completed.mp4"
    output.write_bytes(b"video")
    batch_id = database.create_batch(
        ["https://www.douyin.com/video/1234567890123456789"], tmp_path
    )
    task = database.claim_next_task()
    assert task
    database.update_task(task.id, status=TaskStatus.COMPLETED, output_path=str(output))

    assert database.delete_batch(batch_id) is True

    with pytest.raises(KeyError):
        database.get_batch(batch_id)
    assert output.read_bytes() == b"video"
    assert database.delete_batch(batch_id) is False


def test_persists_settings(tmp_path: Path) -> None:
    database = make_database(tmp_path)

    database.set_setting("download_directory", "D:/Videos/抖音批量下载")

    assert database.get_setting("download_directory") == "D:/Videos/抖音批量下载"
