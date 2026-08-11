import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from douyin_downloader.api import create_app, stream_batch_events
from douyin_downloader.platforms import ExpandedVideo, ExpansionResult
from douyin_downloader.store import Database, TaskStatus


class IdleQueue:
    pause_reason = None

    def __init__(self, database: Database | None = None) -> None:
        self.database = database
        self.wake_calls = 0
        self.cancel_calls: list[int] = []
        self.pause_calls: list[int] = []
        self.resume_calls: list[int] = []

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def wake(self) -> None:
        self.wake_calls += 1

    def cancel_batch(self, batch_id: int) -> None:
        self.cancel_calls.append(batch_id)

    def pause_batch(self, batch_id: int) -> None:
        self.pause_calls.append(batch_id)
        if self.database is not None:
            self.database.set_batch_paused(batch_id, True)

    def resume_batch(self, batch_id: int) -> None:
        self.resume_calls.append(batch_id)
        if self.database is not None:
            self.database.set_batch_paused(batch_id, False)


class StubExpander:
    def expand(self, urls: list[str], original_urls: list[str] | None = None) -> ExpansionResult:
        originals = original_urls or urls
        videos = []
        for url, original in zip(urls, originals, strict=True):
            video_id = url.rstrip("/").rsplit("/", 1)[-1]
            platform = "bilibili" if "bilibili.com" in url else "douyin"
            videos.append(ExpandedVideo(platform, video_id, "", url, original))
        return ExpansionResult(videos, len(urls), 0, {videos[0].platform: len(videos)})


async def identity_resolver(url: str) -> str:
    return url


def make_client(tmp_path: Path) -> tuple[TestClient, Database, IdleQueue]:
    database = Database(tmp_path / "api.db")
    database.initialize()
    queue = IdleQueue(database)
    app = create_app(
        database,
        queue,
        default_download_dir=tmp_path / "downloads",
        pick_directory=lambda: tmp_path / "picked",
        open_directory=lambda _path: None,
        shutdown_callback=lambda: None,
        short_link_resolver=identity_resolver,
        batch_expander=StubExpander(),
    )
    return TestClient(app), database, queue


def test_page_session_grace_covers_slow_browser_reconnect(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)

    assert client.app.state.page_sessions.grace_seconds >= 30


@pytest.mark.asyncio
async def test_desktop_app_ignores_page_session_disconnect(tmp_path: Path) -> None:
    shutdown_calls: list[bool] = []
    database = Database(tmp_path / "desktop.db")
    database.initialize()
    queue = IdleQueue(database)
    app = create_app(
        database,
        queue,
        default_download_dir=tmp_path,
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
        shutdown_callback=lambda: shutdown_calls.append(True),
        shutdown_on_page_disconnect=False,
    )
    tracker = app.state.page_sessions

    await tracker.connect()
    await tracker.disconnect()
    await asyncio.sleep(0.02)

    assert shutdown_calls == []


def test_create_batch_resolves_short_link_before_persisting(tmp_path: Path) -> None:
    database = Database(tmp_path / "short.db")
    database.initialize()
    queue = IdleQueue()

    async def resolve_short(_url: str) -> str:
        return "https://www.douyin.com/video/1234567890123456789"

    app = create_app(
        database,
        queue,
        default_download_dir=tmp_path,
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
        shutdown_callback=lambda: None,
        short_link_resolver=resolve_short,
        batch_expander=StubExpander(),
    )
    client = TestClient(app)

    response = client.post(
        "/api/batches",
        json={"text": "https://v.douyin.com/ABC123/", "output_dir": str(tmp_path)},
    )

    assert response.status_code == 201
    task = response.json()["tasks"][0]
    assert task["original_url"] == "https://v.douyin.com/ABC123/"
    assert task["video_id"] == "1234567890123456789"


def test_preview_returns_counts_and_normalized_urls(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)

    response = client.post(
        "/api/batches/preview",
        json={
            "text": "\n".join(
                [
                    "https://www.douyin.com/video/1234567890123456789",
                    "https://www.douyin.com/video/1234567890123456789",
                    "https://example.com/video/1",
                ]
            )
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "valid_count": 2,
        "duplicate_count": 1,
        "invalid_count": 0,
        "valid_urls": [
            "https://www.douyin.com/video/1234567890123456789",
            "https://example.com/video/1",
        ],
    }


def test_create_batch_invalid_input_mentions_http_and_https(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)

    response = client.post(
        "/api/batches",
        json={"text": "http://127.0.0.1/private.mp4"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "没有识别到有效的公网 HTTP/HTTPS 链接"


def test_create_batch_persists_tasks_and_wakes_queue(tmp_path: Path) -> None:
    client, _, queue = make_client(tmp_path)

    response = client.post(
        "/api/batches",
        json={
            "text": "https://www.douyin.com/video/1234567890123456789",
            "output_dir": str(tmp_path / "videos"),
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["total"] == 1
    assert payload["counts"] == {"queued": 1}
    assert payload["import_summary"] == {
        "input_count": 1,
        "expanded_count": 1,
        "duplicate_count": 0,
        "platform_counts": {"douyin": 1},
    }
    assert queue.wake_calls == 1


def test_create_batch_persists_bilibili_platform(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)

    response = client.post(
        "/api/batches",
        json={"text": "https://www.bilibili.com/video/BV1kdKr6qEMF/?spm_id_from=1"},
    )

    assert response.status_code == 201
    task = response.json()["tasks"][0]
    assert task["platform"] == "bilibili"
    assert task["video_id"] == "BV1kdKr6qEMF"


def test_create_batch_skips_completed_video_when_file_still_exists(tmp_path: Path) -> None:
    client, database, _ = make_client(tmp_path)
    url = "https://www.douyin.com/video/1234567890123456789"
    old_batch = database.create_batch([url], tmp_path)
    old_task = database.claim_next_task()
    assert old_task and old_task.batch_id == old_batch
    output = tmp_path / "已下载.mp4"
    output.write_bytes(b"video")
    database.update_task(
        old_task.id,
        status=TaskStatus.COMPLETED,
        progress=100,
        output_path=str(output),
    )

    response = client.post(
        "/api/batches",
        json={"text": url, "output_dir": str(tmp_path)},
    )

    assert response.status_code == 201
    assert response.json()["counts"] == {"skipped": 1}
    assert response.json()["tasks"][0]["error_message"] == "历史记录中已下载"


def test_get_unknown_batch_returns_404(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)

    response = client.get("/api/batches/999")

    assert response.status_code == 404
    assert response.json()["detail"] == "批次不存在"


def test_retry_failed_requeues_only_failed_tasks(tmp_path: Path) -> None:
    client, database, queue = make_client(tmp_path)
    batch_id = database.create_batch(
        ["https://www.douyin.com/video/1234567890123456789"], tmp_path
    )
    task = database.claim_next_task()
    assert task
    database.update_task(task.id, status=TaskStatus.FAILED, error_code="network")

    response = client.post(f"/api/batches/{batch_id}/retry-failed")

    assert response.status_code == 200
    assert response.json() == {"retried": 1}
    assert queue.wake_calls == 1


def test_pause_and_resume_batch_return_updated_state(tmp_path: Path) -> None:
    client, database, queue = make_client(tmp_path)
    batch_id = database.create_batch(
        ["https://www.douyin.com/video/1234567890123456789"], tmp_path
    )

    paused = client.post(f"/api/batches/{batch_id}/pause")
    resumed = client.post(f"/api/batches/{batch_id}/resume")

    assert paused.status_code == 200
    assert paused.json()["paused"] is True
    assert resumed.status_code == 200
    assert resumed.json()["paused"] is False
    assert queue.pause_calls == [batch_id]
    assert queue.resume_calls == [batch_id]


def test_list_batches_returns_paginated_summaries(tmp_path: Path) -> None:
    client, database, _ = make_client(tmp_path)
    batch_ids = [
        database.create_batch(
            [f"https://www.douyin.com/video/{1234567890123456789 + index}"],
            tmp_path,
        )
        for index in range(3)
    ]

    response = client.get("/api/batches?page=2&page_size=2")

    assert response.status_code == 200
    payload = response.json()
    assert [item["id"] for item in payload["items"]] == [batch_ids[0]]
    assert payload["page"] == 2
    assert payload["page_size"] == 2
    assert payload["total"] == 3
    assert payload["total_pages"] == 2


def test_delete_batch_cancels_queue_before_removing_records(tmp_path: Path) -> None:
    client, database, queue = make_client(tmp_path)
    batch_id = database.create_batch(
        ["https://www.douyin.com/video/1234567890123456789"], tmp_path
    )

    response = client.delete(f"/api/batches/{batch_id}")

    assert response.status_code == 200
    assert response.json() == {"deleted": True, "batch_id": batch_id}
    assert queue.cancel_calls == [batch_id]
    with pytest.raises(KeyError):
        database.get_batch(batch_id)


def test_delete_all_batches_cancels_snapshot_before_removing_records(tmp_path: Path) -> None:
    client, database, queue = make_client(tmp_path)
    batch_ids = [
        database.create_batch(
            [f"https://www.douyin.com/video/{1234567890123456789 + index}"],
            tmp_path,
        )
        for index in range(2)
    ]

    response = client.delete("/api/batches")

    assert response.status_code == 200
    assert response.json() == {"deleted": True, "count": 2}
    assert queue.cancel_calls == list(reversed(batch_ids))
    assert database.list_batches(page=1, page_size=20)["total"] == 0


def test_delete_all_batches_is_successful_when_history_is_empty(tmp_path: Path) -> None:
    client, _, queue = make_client(tmp_path)

    response = client.delete("/api/batches")

    assert response.status_code == 200
    assert response.json() == {"deleted": True, "count": 0}
    assert queue.cancel_calls == []


def test_delete_unknown_batch_returns_404(tmp_path: Path) -> None:
    client, _, queue = make_client(tmp_path)

    response = client.delete("/api/batches/999")

    assert response.status_code == 404
    assert queue.cancel_calls == []


def test_delete_batch_returns_404_if_concurrent_delete_wins(monkeypatch, tmp_path: Path) -> None:
    client, database, queue = make_client(tmp_path)
    batch_id = database.create_batch(
        ["https://www.douyin.com/video/1234567890123456789"], tmp_path
    )
    monkeypatch.setattr(database, "delete_batch", lambda _batch_id: False)

    response = client.delete(f"/api/batches/{batch_id}")

    assert response.status_code == 404
    assert queue.cancel_calls == [batch_id]


def test_pick_directory_persists_selection(tmp_path: Path) -> None:
    client, database, _ = make_client(tmp_path)

    response = client.post("/api/settings/pick-directory")

    assert response.status_code == 200
    assert response.json() == {"download_directory": str(tmp_path / "picked")}
    assert database.get_setting("download_directory") == str(tmp_path / "picked")


def test_open_directory_uses_current_download_directory(tmp_path: Path) -> None:
    database = Database(tmp_path / "open.db")
    database.initialize()
    queue = IdleQueue()
    opened: list[Path] = []
    target = tmp_path / "videos"
    app = create_app(
        database,
        queue,
        default_download_dir=target,
        pick_directory=lambda: None,
        open_directory=opened.append,
        shutdown_callback=lambda: None,
    )
    client = TestClient(app)

    response = client.post("/api/settings/open-directory")

    assert response.status_code == 200
    assert opened == [target.resolve()]


@pytest.mark.asyncio
async def test_sse_stream_emits_batch_snapshot(tmp_path: Path) -> None:
    database = Database(tmp_path / "events.db")
    database.initialize()
    batch_id = database.create_batch(
        ["https://www.douyin.com/video/1234567890123456789"], tmp_path
    )

    events = stream_batch_events(database, batch_id, poll_interval=0, max_events=1)
    event = await anext(events)

    assert event.startswith("event: batch\ndata: ")
    payload = json.loads(event.split("data: ", 1)[1])
    assert payload["id"] == batch_id
    assert payload["counts"] == {"queued": 1}
