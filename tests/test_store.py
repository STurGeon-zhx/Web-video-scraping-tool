from pathlib import Path
import sqlite3

import pytest

from douyin_downloader.platforms import ExpandedVideo
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


def test_claimed_tasks_include_their_original_batch_position(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    batch_id = database.create_batch(
        [
            "https://www.douyin.com/video/1234567890123456781",
            "https://www.douyin.com/video/1234567890123456782",
        ],
        tmp_path,
    )

    first = database.claim_next_task()
    second = database.claim_next_task()

    assert first is not None and first.batch_id == batch_id
    assert second is not None and second.batch_id == batch_id
    assert getattr(first, "position", None) == 1
    assert getattr(second, "position", None) == 2


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


def test_initialize_migrates_legacy_tasks_to_douyin_platform(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                output_dir TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id INTEGER NOT NULL,
                video_id TEXT,
                status TEXT NOT NULL
            );
            INSERT INTO batches(output_dir, created_at) VALUES ('D:/Videos', '2026-01-01');
            INSERT INTO tasks(batch_id, video_id, status) VALUES (1, '123', 'completed');
            """
        )

    Database(path).initialize()

    with sqlite3.connect(path) as connection:
        platform = connection.execute("SELECT platform FROM tasks WHERE id = 1").fetchone()[0]
    assert platform == "douyin"


def test_creates_expanded_tasks_with_platform_identity(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    videos = [
        ExpandedVideo("bilibili", "same", "B站", "https://www.bilibili.com/video/same", "https://b23.tv/a"),
        ExpandedVideo("douyin", "same", "抖音", "https://www.douyin.com/video/same", "https://v.douyin.com/a"),
    ]

    batch_id = database.create_expanded_batch(videos, tmp_path)
    tasks = database.get_batch(batch_id)["tasks"]

    assert [(task["platform"], task["video_id"], task["title"]) for task in tasks] == [
        ("bilibili", "same", "B站"),
        ("douyin", "same", "抖音"),
    ]
    assert tasks[0]["original_url"] == "https://b23.tv/a"


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


def test_creates_page_batch_and_exposes_collection_state(tmp_path: Path) -> None:
    database = make_database(tmp_path)

    batch_id = database.create_page_batch(
        "https://www.douyin.com/search/%E7%BE%8E%E9%A3%9F",
        requested_count=50,
        output_dir=tmp_path / "downloads",
    )

    batch = database.get_batch(batch_id)
    assert batch["source_mode"] == "page"
    assert batch["source_url"].startswith("https://www.douyin.com/search/")
    assert batch["requested_count"] == 50
    assert batch["collected_count"] == 0
    assert batch["collection_status"] == "pending"
    assert batch["collection_stop_reason"] is None
    assert batch["tasks"] == []


def test_appends_unique_page_videos_in_discovery_order(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    batch_id = database.create_page_batch(
        "https://www.douyin.com/search/food",
        requested_count=2,
        output_dir=tmp_path,
    )
    first = ExpandedVideo(
        "douyin",
        "111",
        "first",
        "https://www.douyin.com/video/111",
        "https://www.douyin.com/search/food",
    )
    second = ExpandedVideo(
        "douyin",
        "222",
        "second",
        "https://www.douyin.com/video/222",
        "https://www.douyin.com/search/food",
    )

    assert database.append_page_video(batch_id, first) is True
    assert database.append_page_video(batch_id, first) is False
    assert database.append_page_video(batch_id, second) is True

    batch = database.get_batch(batch_id)
    assert batch["collected_count"] == 2
    assert [task["video_id"] for task in batch["tasks"]] == ["111", "222"]


def test_page_video_is_inserted_as_skipped_before_workers_can_claim_it(
    tmp_path: Path,
) -> None:
    database = make_database(tmp_path)
    output = tmp_path / "already-downloaded.mp4"
    output.write_bytes(b"video")
    video = ExpandedVideo(
        "douyin",
        "111",
        "existing title",
        "https://www.douyin.com/video/111",
        "https://www.douyin.com/search/food",
    )
    old_batch = database.create_expanded_batch([video], tmp_path)
    old_task = database.claim_next_task()
    assert old_task is not None and old_task.batch_id == old_batch
    database.update_task(
        old_task.id,
        status=TaskStatus.COMPLETED,
        title="downloaded title",
        output_path=str(output),
        progress=100,
    )
    page_batch = database.create_page_batch(
        "https://www.douyin.com/search/food",
        requested_count=2,
        output_dir=tmp_path,
    )

    assert database.append_page_video(page_batch, video) is True

    task = database.get_batch(page_batch)["tasks"][0]
    assert task["status"] == TaskStatus.SKIPPED.value
    assert task["title"] == "downloaded title"
    assert task["output_path"] == str(output)
    assert database.claim_next_task() is None


def test_history_skip_never_overwrites_a_claimed_task(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    output = tmp_path / "already-downloaded.mp4"
    video = ExpandedVideo(
        "douyin",
        "111",
        "title",
        "https://www.douyin.com/video/111",
        "https://www.douyin.com/search/food",
    )
    old_batch = database.create_expanded_batch([video], tmp_path)
    old_task = database.claim_next_task()
    assert old_task is not None and old_task.batch_id == old_batch
    database.update_task(
        old_task.id,
        status=TaskStatus.COMPLETED,
        output_path=str(output),
    )
    new_batch = database.create_expanded_batch([video], tmp_path)
    claimed = database.claim_next_task()
    assert claimed is not None and claimed.batch_id == new_batch
    output.write_bytes(b"video")

    database.skip_existing_completed(new_batch)

    task = database.get_batch(new_batch)["tasks"][0]
    assert task["status"] == TaskStatus.RESOLVING.value


def test_expanded_batch_marks_history_duplicates_before_commit(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    output = tmp_path / "already-downloaded.mp4"
    output.write_bytes(b"video")
    video = ExpandedVideo(
        "douyin",
        "111",
        "title",
        "https://www.douyin.com/video/111",
        "https://www.douyin.com/video/111",
    )
    old_batch = database.create_expanded_batch([video], tmp_path)
    old_task = database.claim_next_task()
    assert old_task is not None and old_task.batch_id == old_batch
    database.update_task(
        old_task.id,
        status=TaskStatus.COMPLETED,
        output_path=str(output),
        progress=100,
    )

    new_batch = database.create_expanded_batch([video], tmp_path)

    task = database.get_batch(new_batch)["tasks"][0]
    assert task["status"] == TaskStatus.SKIPPED.value
    assert task["output_path"] == str(output)
    assert database.claim_next_task() is None


def test_page_batch_never_collects_more_than_requested_count(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    batch_id = database.create_page_batch(
        "https://example.com/videos",
        requested_count=1,
        output_dir=tmp_path,
    )

    assert database.append_page_video(
        batch_id,
        ExpandedVideo("direct", "one", "one", "https://cdn.example.com/1.mp4", "https://example.com/videos"),
    ) is True
    assert database.append_page_video(
        batch_id,
        ExpandedVideo("direct", "two", "two", "https://cdn.example.com/2.mp4", "https://example.com/videos"),
    ) is False

    assert database.get_batch(batch_id)["collected_count"] == 1


def test_initialize_migrates_existing_batches_to_link_mode(tmp_path: Path) -> None:
    path = tmp_path / "legacy-batches.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE batches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                output_dir TEXT NOT NULL,
                paused INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO batches(output_dir, paused, created_at) VALUES ('D:/Videos', 0, '2026-01-01')"
        )

    database = Database(path)
    database.initialize()

    batch = database.get_batch(1)
    assert batch["source_mode"] == "links"
    assert batch["source_url"] is None
    assert batch["collection_status"] is None


def test_updates_collection_status_and_lists_resumable_page_batches(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    pending_id = database.create_page_batch("https://example.com/one", 5, tmp_path)
    collecting_id = database.create_page_batch("https://example.com/two", 5, tmp_path)
    completed_id = database.create_page_batch("https://example.com/three", 5, tmp_path)

    database.update_collection(collecting_id, "collecting")
    database.update_collection(completed_id, "page_ended", "页面已结束")

    assert database.list_resumable_page_batches() == [pending_id, collecting_id]
    completed = database.get_batch(completed_id)
    assert completed["collection_status"] == "page_ended"
    assert completed["collection_stop_reason"] == "页面已结束"


def test_youtube_page_can_skip_history_duplicate_without_counting_it(tmp_path: Path) -> None:
    database = make_database(tmp_path)
    output = tmp_path / "existing.mp4"
    output.write_bytes(b"video")
    old_batch = database.create_page_batch("https://youtube.com/@old/videos", 1, tmp_path)
    video = ExpandedVideo(
        "youtube",
        "BaW_jenozKc",
        "测试视频",
        "https://www.youtube.com/watch?v=BaW_jenozKc",
        "https://youtube.com/@old/videos",
    )
    assert database.append_page_video(old_batch, video) is True
    task_id = database.get_batch(old_batch)["tasks"][0]["id"]
    database.update_task(
        task_id,
        status=TaskStatus.COMPLETED.value,
        output_path=str(output),
        progress=100,
    )
    new_batch = database.create_page_batch("https://youtube.com/@new/videos", 5, tmp_path)

    assert database.append_page_video(
        new_batch,
        video,
        skip_history_duplicate=True,
    ) is False
    assert database.get_batch(new_batch)["collected_count"] == 0
    assert database.get_batch(new_batch)["tasks"] == []
