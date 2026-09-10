import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
            if "youtube.com" in url:
                platform = "youtube"
                video_id = (parse_qs(urlsplit(url).query).get("v") or [video_id])[0]
            else:
                platform = "bilibili" if "bilibili.com" in url else "douyin"
            videos.append(ExpandedVideo(platform, video_id, "", url, original))
        return ExpansionResult(videos, len(urls), 0, {videos[0].platform: len(videos)})


class StubPageManager:
    def __init__(self) -> None:
        self.submitted: list[int] = []
        self.cancelled: list[int] = []
        self.resumed: list[int] = []

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def submit(self, batch_id: int) -> None:
        self.submitted.append(batch_id)

    def cancel_batch(self, batch_id: int) -> None:
        self.cancelled.append(batch_id)

    def resume_batch(self, batch_id: int) -> None:
        self.resumed.append(batch_id)


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


def test_create_page_batch_returns_immediately_and_starts_collection(tmp_path: Path) -> None:
    database = Database(tmp_path / "page-api.db")
    database.initialize()
    queue = IdleQueue(database)
    page_manager = StubPageManager()
    app = create_app(
        database,
        queue,
        default_download_dir=tmp_path / "downloads",
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
        shutdown_callback=lambda: None,
        short_link_resolver=identity_resolver,
        page_collection_manager=page_manager,
    )
    client = TestClient(app)

    response = client.post(
        "/api/batches",
        json={
            "text": "https://www.douyin.com/search/food",
            "output_dir": str(tmp_path / "videos"),
            "source_mode": "page",
            "max_items": 50,
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["source_mode"] == "page"
    assert payload["requested_count"] == 50
    assert payload["collection_status"] == "pending"
    assert payload["total"] == 0
    assert page_manager.submitted == [payload["id"]]
    assert queue.wake_calls == 0


def test_page_batch_requires_exactly_one_public_page_url(tmp_path: Path) -> None:
    database = Database(tmp_path / "page-validation.db")
    database.initialize()
    page_manager = StubPageManager()
    app = create_app(
        database,
        IdleQueue(database),
        default_download_dir=tmp_path,
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
        shutdown_callback=lambda: None,
        short_link_resolver=identity_resolver,
        page_collection_manager=page_manager,
    )
    client = TestClient(app)

    response = client.post(
        "/api/batches",
        json={
            "text": "https://example.com/one\nhttps://example.com/two",
            "source_mode": "page",
            "max_items": 10,
        },
    )

    assert response.status_code == 422
    assert "一个页面链接" in response.json()["detail"]
    assert page_manager.submitted == []


def test_page_batch_rejects_unsupported_douyin_search_tab(tmp_path: Path) -> None:
    database = Database(tmp_path / "page-search-type.db")
    database.initialize()
    page_manager = StubPageManager()
    app = create_app(
        database,
        IdleQueue(database),
        default_download_dir=tmp_path,
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
        shutdown_callback=lambda: None,
        short_link_resolver=identity_resolver,
        page_collection_manager=page_manager,
    )
    client = TestClient(app)

    response = client.post(
        "/api/batches",
        json={
            "text": "https://www.douyin.com/search/风景?type=user",
            "source_mode": "page",
            "max_items": 10,
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "不支持的抖音搜索类型: user"
    assert page_manager.submitted == []


def test_youtube_watch_with_playlist_parameter_remains_single_video(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)

    response = client.post(
        "/api/batches",
        json={"text": "https://www.youtube.com/watch?v=BaW_jenozKc&list=PL123&t=2"},
    )

    assert response.status_code == 201
    task = response.json()["tasks"][0]
    assert task["platform"] == "youtube"
    assert task["video_id"] == "BaW_jenozKc"
    assert task["canonical_url"] == "https://www.youtube.com/watch?v=BaW_jenozKc"


def test_youtube_page_batch_normalizes_channel_root(tmp_path: Path) -> None:
    database = Database(tmp_path / "youtube-page.db")
    database.initialize()
    page_manager = StubPageManager()
    app = create_app(
        database,
        IdleQueue(database),
        default_download_dir=tmp_path,
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
        shutdown_callback=lambda: None,
        short_link_resolver=identity_resolver,
        page_collection_manager=page_manager,
    )
    client = TestClient(app)

    response = client.post(
        "/api/batches",
        json={"text": "https://youtube.com/@example", "source_mode": "page", "max_items": 5},
    )

    assert response.status_code == 201
    assert response.json()["source_url"] == "https://www.youtube.com/@example/videos"


def test_youtube_page_and_single_urls_are_rejected_in_wrong_modes(tmp_path: Path) -> None:
    database = Database(tmp_path / "youtube-modes.db")
    database.initialize()
    page_manager = StubPageManager()
    app = create_app(
        database,
        IdleQueue(database),
        default_download_dir=tmp_path,
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
        shutdown_callback=lambda: None,
        short_link_resolver=identity_resolver,
        batch_expander=StubExpander(),
        page_collection_manager=page_manager,
    )
    client = TestClient(app)

    playlist = client.post(
        "/api/batches",
        json={"text": "https://youtube.com/playlist?list=PL123"},
    )
    single = client.post(
        "/api/batches",
        json={
            "text": "https://youtu.be/BaW_jenozKc",
            "source_mode": "page",
            "max_items": 5,
        },
    )

    assert playlist.status_code == 422
    assert "页面批量下载" in playlist.json()["detail"]
    assert single.status_code == 422
    assert "单视频" in single.json()["detail"]


def test_page_batch_resume_and_delete_control_page_collector(tmp_path: Path) -> None:
    database = Database(tmp_path / "page-control.db")
    database.initialize()
    queue = IdleQueue(database)
    page_manager = StubPageManager()
    app = create_app(
        database,
        queue,
        default_download_dir=tmp_path,
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
        shutdown_callback=lambda: None,
        short_link_resolver=identity_resolver,
        page_collection_manager=page_manager,
    )
    client = TestClient(app)
    batch_id = database.create_page_batch("https://example.com/videos", 10, tmp_path)

    paused = client.post(f"/api/batches/{batch_id}/pause")
    resumed = client.post(f"/api/batches/{batch_id}/resume")
    deleted = client.delete(f"/api/batches/{batch_id}")

    assert paused.status_code == 200
    assert resumed.status_code == 200
    assert page_manager.resumed == [batch_id]
    assert deleted.status_code == 200
    assert page_manager.cancelled == [batch_id]


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


def test_youtube_network_settings_default_and_persistence(tmp_path: Path) -> None:
    client, database, _ = make_client(tmp_path)

    defaults = client.get("/api/settings")
    assert defaults.status_code == 200
    assert defaults.json()["youtube_network_mode"] == "system"
    assert defaults.json()["youtube_proxy_url"] == ""

    saved = client.post(
        "/api/settings/youtube-network",
        json={"mode": "manual", "proxy_url": "http://127.0.0.1:7890/"},
    )

    assert saved.status_code == 200
    assert saved.json()["youtube_proxy_url"] == "http://127.0.0.1:7890"
    assert database.get_setting("youtube_network_mode") == "manual"
    assert database.get_setting("youtube_proxy_url") == "http://127.0.0.1:7890"


def test_youtube_network_settings_reject_remote_or_authenticated_proxy(tmp_path: Path) -> None:
    client, _, _ = make_client(tmp_path)

    remote = client.post(
        "/api/settings/youtube-network",
        json={"mode": "manual", "proxy_url": "http://192.168.1.2:7890"},
    )
    authenticated = client.post(
        "/api/settings/youtube-network",
        json={"mode": "manual", "proxy_url": "http://user:secret@127.0.0.1:7890"},
    )

    assert remote.status_code == 422
    assert "只能使用本机" in remote.json()["detail"]
    assert authenticated.status_code == 422
    assert "用户名或密码" in authenticated.json()["detail"]


def test_youtube_network_test_uses_form_values_without_saving(tmp_path: Path) -> None:
    database = Database(tmp_path / "network-test.db")
    database.initialize()
    seen = []

    async def tester(settings):
        seen.append(settings)
        return True, "测试通过"

    app = create_app(
        database,
        IdleQueue(database),
        default_download_dir=tmp_path,
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
        shutdown_callback=lambda: None,
        youtube_network_tester=tester,
    )
    client = TestClient(app)

    response = client.post(
        "/api/settings/youtube-network/test",
        json={"mode": "manual", "proxy_url": "socks5://localhost:7891"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "message": "测试通过"}
    assert seen[0].ydl_options() == {"proxy": "socks5://localhost:7891"}
    assert database.get_setting("youtube_network_mode") is None


def test_youtube_auth_endpoints_validate_target_and_control_dedicated_window(tmp_path: Path) -> None:
    database = Database(tmp_path / "youtube-auth.db")
    database.initialize()

    class AuthManager:
        def __init__(self) -> None:
            self.targets: list[str | None] = []
            self.completed = 0

        def status(self) -> dict:
            return {"status": "idle", "message": "尚未验证", "running": False, "has_saved_state": False}

        def start(self, target_url: str | None = None) -> dict:
            self.targets.append(target_url)
            return {"status": "waiting", "message": "等待验证", "running": True, "has_saved_state": False}

        def complete(self, timeout: float = 8.0) -> dict:
            self.completed += 1
            return {"status": "saved", "message": "已保存", "running": False, "has_saved_state": True}

        def stop(self) -> None:
            return None

    manager = AuthManager()
    app = create_app(
        database,
        IdleQueue(database),
        default_download_dir=tmp_path,
        pick_directory=lambda: None,
        open_directory=lambda _path: None,
        shutdown_callback=lambda: None,
        youtube_auth_manager=manager,
    )
    client = TestClient(app)

    assert client.get("/api/settings/youtube-auth").json()["status"] == "idle"
    started = client.post(
        "/api/settings/youtube-auth/start",
        json={"target_url": "https://www.youtube.com/shorts/UrgqdJ6vtoY"},
    )
    rejected = client.post(
        "/api/settings/youtube-auth/start",
        json={"target_url": "https://example.com/video"},
    )
    completed = client.post("/api/settings/youtube-auth/complete")

    assert started.status_code == 200
    assert manager.targets == ["https://www.youtube.com/shorts/UrgqdJ6vtoY"]
    assert rejected.status_code == 422
    assert completed.json()["has_saved_state"] is True
    assert manager.completed == 1


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
