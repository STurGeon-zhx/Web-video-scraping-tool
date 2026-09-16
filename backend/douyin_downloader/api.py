from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .links import preview_links
from .page_collector import douyin_search_mode, is_douyin_search_url
from .page_sessions import PageSessionTracker
from .platforms import BatchExpander, BatchExpansionError, extractor_supports_url
from .resolver import LinkResolutionError, resolve_public_url
from .store import Database
from .youtube import classify_youtube_url, is_youtube_host, youtube_failure_message
from .youtube_network import (
    YoutubeNetworkSettings,
    YoutubeNetworkSettingsProvider,
    test_youtube_connection,
    validate_youtube_network_settings,
)


class QueueController(Protocol):
    pause_reason: str | None

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def wake(self) -> None: ...

    def cancel_batch(self, batch_id: int) -> None: ...

    def pause_batch(self, batch_id: int) -> None: ...

    def resume_batch(self, batch_id: int) -> None: ...


class PageCollectionController(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def submit(self, batch_id: int) -> None: ...
    def cancel_batch(self, batch_id: int) -> None: ...
    def resume_batch(self, batch_id: int) -> None: ...


class YoutubeAuthController(Protocol):
    def status(self) -> dict: ...
    def start(self, target_url: str | None = None) -> dict: ...
    def complete(self, timeout: float = 8.0) -> dict: ...
    def stop(self) -> None: ...


class PreviewRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500_000)


class CreateBatchRequest(PreviewRequest):
    output_dir: str | None = None
    source_mode: Literal["links", "page"] = "links"
    max_items: int = Field(default=50, ge=1, le=500)


class YoutubeNetworkRequest(BaseModel):
    mode: Literal["system", "manual"] = "system"
    proxy_url: str = Field(default="", max_length=500)


class YoutubeAuthRequest(BaseModel):
    target_url: str | None = Field(default=None, max_length=2_000)


def _batch_or_404(database: Database, batch_id: int) -> dict:
    try:
        return database.get_batch(batch_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="批次不存在") from exc


async def stream_batch_events(
    database: Database,
    batch_id: int,
    poll_interval: float = 0.5,
    max_events: int | None = None,
) -> AsyncIterator[str]:
    emitted = 0
    previous = ""
    while max_events is None or emitted < max_events:
        try:
            batch = database.get_batch(batch_id)
        except KeyError:
            yield 'event: error\ndata: {"detail":"批次不存在"}\n\n'
            return
        payload = json.dumps(batch, ensure_ascii=False, separators=(",", ":"))
        if payload != previous:
            previous = payload
            emitted += 1
            yield f"event: batch\ndata: {payload}\n\n"
        else:
            yield ": keep-alive\n\n"
        await asyncio.sleep(poll_interval)


async def stream_page_session(
    tracker: PageSessionTracker,
    keepalive_interval: float = 15.0,
    max_events: int | None = None,
) -> AsyncIterator[str]:
    emitted = 0
    await tracker.connect()
    try:
        while max_events is None or emitted < max_events:
            emitted += 1
            yield ": keep-alive\n\n"
            await asyncio.sleep(keepalive_interval)
    finally:
        await tracker.disconnect()


def create_app(
    database: Database,
    queue: QueueController,
    default_download_dir: Path,
    pick_directory: Callable[[], Path | None],
    open_directory: Callable[[Path], None],
    shutdown_callback: Callable[[], None],
    static_dir: Path | None = None,
    short_link_resolver: Callable[[str], Awaitable[str]] | None = None,
    batch_expander: BatchExpander | None = None,
    page_collection_manager: PageCollectionController | None = None,
    shutdown_on_page_disconnect: bool = True,
    youtube_network_tester: Callable[
        [YoutubeNetworkSettings], Awaitable[tuple[bool, str]]
    ] = test_youtube_connection,
    youtube_auth_manager: YoutubeAuthController | None = None,
) -> FastAPI:
    page_disconnect_callback = shutdown_callback if shutdown_on_page_disconnect else lambda: None
    page_sessions = PageSessionTracker(page_disconnect_callback, grace_seconds=30.0)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        database.initialize()
        database.recover_interrupted()
        queue.start()
        if page_collection_manager is not None:
            page_collection_manager.start()
        try:
            yield
        finally:
            await page_sessions.close()
            if page_collection_manager is not None:
                page_collection_manager.stop()
            if youtube_auth_manager is not None:
                youtube_auth_manager.stop()
            queue.stop()

    app = FastAPI(title="视频批量下载工具", version="0.2.0", lifespan=lifespan)
    app.state.page_sessions = page_sessions
    expander = batch_expander or BatchExpander()
    youtube_network = YoutubeNetworkSettingsProvider(database)

    async def resolve_short_link(url: str) -> str:
        if short_link_resolver:
            return await short_link_resolver(url)
        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 VideoBatchDownloader/0.2"},
        ) as client:
            return await resolve_public_url(url, client)

    @app.post("/api/batches/preview")
    async def preview_batch(request: PreviewRequest) -> dict:
        preview = preview_links(request.text)
        return {
            "valid_count": len(preview.valid_urls),
            "duplicate_count": preview.duplicate_count,
            "invalid_count": preview.invalid_count,
            "valid_urls": preview.valid_urls,
        }

    @app.post("/api/batches", status_code=status.HTTP_201_CREATED)
    async def create_batch(request: CreateBatchRequest) -> dict:
        preview = preview_links(request.text)
        if not preview.valid_urls:
            raise HTTPException(status_code=422, detail="没有识别到有效的公网 HTTP/HTTPS 链接")
        if request.source_mode == "page":
            if len(preview.valid_urls) != 1 or preview.invalid_count:
                raise HTTPException(status_code=422, detail="页面批量下载每次只能导入一个页面链接")
            if page_collection_manager is None:
                raise HTTPException(status_code=503, detail="页面采集功能当前不可用")
            candidate_url = preview.valid_urls[0]
            youtube = classify_youtube_url(candidate_url)
            if youtube is not None:
                if youtube.kind == "single":
                    raise HTTPException(
                        status_code=422,
                        detail="YouTube 单视频请使用链接批量下载",
                    )
                if youtube.kind != "page":
                    raise HTTPException(
                        status_code=422,
                        detail="YouTube 页面批量下载仅支持播放列表或频道视频页",
                    )
                source_url = youtube.canonical_url
            else:
                try:
                    source_url = await resolve_short_link(candidate_url)
                except LinkResolutionError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            if is_douyin_search_url(source_url):
                try:
                    douyin_search_mode(source_url)
                except ValueError as exc:
                    raise HTTPException(status_code=422, detail=str(exc)) from exc
            configured = database.get_setting("download_directory")
            output_dir = Path(request.output_dir or configured or default_download_dir).expanduser().resolve()
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                raise HTTPException(status_code=422, detail=f"无法创建下载目录: {exc}") from exc
            database.set_setting("download_directory", str(output_dir))
            batch_id = database.create_page_batch(source_url, request.max_items, output_dir)
            page_collection_manager.submit(batch_id)
            return database.get_batch(batch_id)
        resolved_urls: list[str] = []
        original_urls: list[str] = []
        seen_urls: set[str] = set()
        resolution_duplicates = 0
        try:
            for url in preview.valid_urls:
                youtube = classify_youtube_url(url)
                if youtube is not None:
                    if youtube.kind == "page":
                        raise BatchExpansionError(
                            "YouTube 播放列表或频道页请使用页面批量下载"
                        )
                    if youtube.kind != "single":
                        raise BatchExpansionError(
                            "该 YouTube 地址暂不支持；链接下载仅支持单视频、youtu.be 和 Shorts"
                        )
                    resolved = youtube.canonical_url
                else:
                    resolved = await resolve_short_link(url)
                if not extractor_supports_url(resolved):
                    raise BatchExpansionError("该链接没有匹配到受支持的平台专用解析器")
                if resolved in seen_urls:
                    resolution_duplicates += 1
                    continue
                seen_urls.add(resolved)
                resolved_urls.append(resolved)
                original_urls.append(url)
            expansion = await asyncio.to_thread(
                expander.expand,
                resolved_urls,
                original_urls,
            )
        except (LinkResolutionError, BatchExpansionError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:
            detail = youtube_failure_message(str(exc)) if any(
                classify_youtube_url(url) is not None for url in resolved_urls
            ) else str(exc)
            raise HTTPException(status_code=422, detail=f"平台解析失败: {detail}") from exc
        configured = database.get_setting("download_directory")
        output_dir = Path(request.output_dir or configured or default_download_dir).expanduser().resolve()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=422, detail=f"无法创建下载目录: {exc}") from exc
        database.set_setting("download_directory", str(output_dir))
        batch_id = database.create_expanded_batch(expansion.videos, output_dir)
        queue.wake()
        payload = database.get_batch(batch_id)
        payload["import_summary"] = {
            "input_count": expansion.input_count,
            "expanded_count": len(expansion.videos),
            "duplicate_count": (
                preview.duplicate_count
                + resolution_duplicates
                + expansion.duplicate_count
            ),
            "platform_counts": expansion.platform_counts,
        }
        return payload

    @app.get("/api/batches")
    async def list_batches(
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=20, ge=1, le=100),
    ) -> dict:
        return database.list_batches(page=page, page_size=page_size)

    @app.delete("/api/batches")
    async def delete_all_batches() -> dict:
        batch_ids = database.list_batch_ids()
        for batch_id in batch_ids:
            if page_collection_manager is not None:
                page_collection_manager.cancel_batch(batch_id)
            queue.cancel_batch(batch_id)
        deleted = sum(database.delete_batch(batch_id) for batch_id in batch_ids)
        return {"deleted": True, "count": deleted}

    @app.get("/api/batches/{batch_id}")
    async def get_batch(batch_id: int) -> dict:
        batch = _batch_or_404(database, batch_id)
        batch["pause_reason"] = batch.get("pause_reason") or queue.pause_reason
        return batch

    @app.delete("/api/batches/{batch_id}")
    async def delete_batch(batch_id: int) -> dict:
        _batch_or_404(database, batch_id)
        if page_collection_manager is not None:
            page_collection_manager.cancel_batch(batch_id)
        queue.cancel_batch(batch_id)
        if not database.delete_batch(batch_id):
            raise HTTPException(status_code=404, detail="批次不存在")
        return {"deleted": True, "batch_id": batch_id}

    @app.get("/api/events")
    async def batch_events(batchId: int) -> StreamingResponse:  # noqa: N803 - public API name
        _batch_or_404(database, batchId)
        return StreamingResponse(
            stream_batch_events(database, batchId),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/batches/{batch_id}/retry-failed")
    async def retry_failed(batch_id: int) -> dict:
        _batch_or_404(database, batch_id)
        retried = database.retry_failed(batch_id)
        if retried:
            queue.wake()
        return {"retried": retried}

    @app.post("/api/batches/{batch_id}/pause")
    async def pause_batch(batch_id: int) -> dict:
        _batch_or_404(database, batch_id)
        queue.pause_batch(batch_id)
        batch = database.get_batch(batch_id)
        batch["pause_reason"] = batch.get("pause_reason") or queue.pause_reason
        return batch

    @app.post("/api/batches/{batch_id}/resume")
    async def resume_batch(batch_id: int) -> dict:
        _batch_or_404(database, batch_id)
        queue.resume_batch(batch_id)
        if page_collection_manager is not None:
            page_collection_manager.resume_batch(batch_id)
        batch = database.get_batch(batch_id)
        batch["pause_reason"] = batch.get("pause_reason") or queue.pause_reason
        return batch

    @app.get("/api/settings")
    async def get_settings() -> dict:
        directory = database.get_setting("download_directory") or str(default_download_dir)
        network = youtube_network.get()
        return {
            "download_directory": directory,
            "youtube_network_mode": network.mode,
            "youtube_proxy_url": network.proxy_url,
        }

    def validated_youtube_network(request: YoutubeNetworkRequest) -> YoutubeNetworkSettings:
        try:
            return validate_youtube_network_settings(request.mode, request.proxy_url)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/settings/youtube-network")
    async def save_youtube_network(request: YoutubeNetworkRequest) -> dict:
        network = validated_youtube_network(request)
        youtube_network.save(network)
        return {
            "youtube_network_mode": network.mode,
            "youtube_proxy_url": network.proxy_url,
            "message": "YouTube 网络设置已保存",
        }

    @app.post("/api/settings/youtube-network/test")
    async def test_youtube_network(request: YoutubeNetworkRequest) -> dict:
        network = validated_youtube_network(request)
        try:
            ok, message = await youtube_network_tester(network)
        except TimeoutError:
            ok = False
            message = "YouTube 连接测试超时，请检查网络或本地代理"
        return {"ok": ok, "message": message}

    @app.get("/api/settings/youtube-auth")
    async def get_youtube_auth() -> dict:
        if youtube_auth_manager is None:
            return {
                "status": "unavailable",
                "message": "当前版本未启用 YouTube 登录验证",
                "running": False,
                "has_saved_state": False,
            }
        return youtube_auth_manager.status()

    @app.post("/api/settings/youtube-auth/start")
    async def start_youtube_auth(request: YoutubeAuthRequest) -> dict:
        if youtube_auth_manager is None:
            raise HTTPException(status_code=503, detail="YouTube 登录验证功能当前不可用")
        target_url = (request.target_url or "").strip() or None
        if target_url is not None:
            try:
                parsed = urlsplit(target_url)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="YouTube 验证目标链接无效") from exc
            if parsed.scheme.lower() != "https" or not is_youtube_host(target_url):
                raise HTTPException(status_code=422, detail="验证窗口只能打开 HTTPS YouTube 链接")
        return youtube_auth_manager.start(target_url)

    @app.post("/api/settings/youtube-auth/complete")
    async def complete_youtube_auth() -> dict:
        if youtube_auth_manager is None:
            raise HTTPException(status_code=503, detail="YouTube 登录验证功能当前不可用")
        return await asyncio.to_thread(youtube_auth_manager.complete)

    @app.post("/api/settings/pick-directory")
    def choose_directory() -> dict:
        selected = pick_directory()
        if selected is not None:
            selected = selected.resolve()
            selected.mkdir(parents=True, exist_ok=True)
            database.set_setting("download_directory", str(selected))
        directory = database.get_setting("download_directory") or str(default_download_dir)
        return {"download_directory": directory}

    @app.post("/api/settings/open-directory")
    def show_directory() -> dict:
        directory = Path(
            database.get_setting("download_directory") or default_download_dir
        ).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        try:
            open_directory(directory)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"无法打开下载目录: {exc}") from exc
        return {"download_directory": str(directory)}

    @app.post("/api/app/shutdown")
    async def shutdown(background_tasks: BackgroundTasks) -> dict:
        background_tasks.add_task(shutdown_callback)
        return {"message": "程序正在退出"}

    @app.get("/api/app/session")
    async def page_session() -> StreamingResponse:
        return StreamingResponse(
            stream_page_session(page_sessions),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    if static_dir and static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="frontend")
    else:
        @app.get("/", response_class=HTMLResponse)
        async def placeholder() -> str:
            return "<h1>视频批量下载工具</h1><p>前端资源尚未构建。</p>"

    return app
