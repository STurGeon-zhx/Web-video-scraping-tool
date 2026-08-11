from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Protocol

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .links import preview_links
from .page_sessions import PageSessionTracker
from .resolver import LinkResolutionError, resolve_douyin_url
from .store import Database


class QueueController(Protocol):
    pause_reason: str | None

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def wake(self) -> None: ...

    def cancel_batch(self, batch_id: int) -> None: ...

    def pause_batch(self, batch_id: int) -> None: ...

    def resume_batch(self, batch_id: int) -> None: ...


class PreviewRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500_000)


class CreateBatchRequest(PreviewRequest):
    output_dir: str | None = None


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
    shutdown_on_page_disconnect: bool = True,
) -> FastAPI:
    page_disconnect_callback = shutdown_callback if shutdown_on_page_disconnect else lambda: None
    page_sessions = PageSessionTracker(page_disconnect_callback, grace_seconds=30.0)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        database.initialize()
        database.recover_interrupted()
        queue.start()
        try:
            yield
        finally:
            await page_sessions.close()
            queue.stop()

    app = FastAPI(title="抖音批量下载工具", version="0.1.0", lifespan=lifespan)
    app.state.page_sessions = page_sessions

    async def resolve_short_link(url: str) -> str:
        if short_link_resolver:
            return await short_link_resolver(url)
        async with httpx.AsyncClient(
            timeout=15,
            headers={"User-Agent": "Mozilla/5.0 DouyinBatchDownloader/0.1"},
        ) as client:
            return await resolve_douyin_url(url, client)

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
            raise HTTPException(status_code=422, detail="没有识别到有效的抖音链接")
        resolved_urls: list[str] = []
        seen_urls: set[str] = set()
        try:
            for url in preview.valid_urls:
                resolved = url
                if not re.search(r"/video/\d+", url):
                    resolved = await resolve_short_link(url)
                if resolved not in seen_urls:
                    seen_urls.add(resolved)
                    resolved_urls.append(resolved)
        except LinkResolutionError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        configured = database.get_setting("download_directory")
        output_dir = Path(request.output_dir or configured or default_download_dir).expanduser().resolve()
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=422, detail=f"无法创建下载目录: {exc}") from exc
        database.set_setting("download_directory", str(output_dir))
        batch_id = database.create_batch(resolved_urls, output_dir)
        database.skip_existing_completed(batch_id)
        queue.wake()
        return database.get_batch(batch_id)

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
            queue.cancel_batch(batch_id)
        deleted = sum(database.delete_batch(batch_id) for batch_id in batch_ids)
        return {"deleted": True, "count": deleted}

    @app.get("/api/batches/{batch_id}")
    async def get_batch(batch_id: int) -> dict:
        batch = _batch_or_404(database, batch_id)
        batch["pause_reason"] = queue.pause_reason
        return batch

    @app.delete("/api/batches/{batch_id}")
    async def delete_batch(batch_id: int) -> dict:
        _batch_or_404(database, batch_id)
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
        batch["pause_reason"] = queue.pause_reason
        return batch

    @app.post("/api/batches/{batch_id}/resume")
    async def resume_batch(batch_id: int) -> dict:
        _batch_or_404(database, batch_id)
        queue.resume_batch(batch_id)
        batch = database.get_batch(batch_id)
        batch["pause_reason"] = queue.pause_reason
        return batch

    @app.get("/api/settings")
    async def get_settings() -> dict:
        directory = database.get_setting("download_directory") or str(default_download_dir)
        return {"download_directory": directory}

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
            return "<h1>抖音批量下载工具</h1><p>前端资源尚未构建。</p>"

    return app
