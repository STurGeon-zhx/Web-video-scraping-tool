from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path

import uvicorn

from .api import create_app
from .launcher import (
    create_downloader,
    create_server_config,
    find_ffmpeg,
    find_frontend_directory,
    select_available_port,
)
from .page_collector import BrowserPageCollector, PageCollectionManager, PlaywrightBrowserSession
from .queue import TaskQueue
from .store import Database


class LocalBackend:
    """Run the existing FastAPI application in a managed background thread."""

    def __init__(
        self,
        data_dir: Path,
        default_download_dir: Path,
        pick_directory: Callable[[], Path | None],
        open_directory: Callable[[Path], None],
    ) -> None:
        self.data_dir = Path(data_dir)
        self.default_download_dir = Path(default_download_dir)
        self.pick_directory = pick_directory
        self.open_directory = open_directory
        self.base_url: str | None = None
        self.port: int | None = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._runtime_file = self.data_dir / "runtime.json"

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, timeout: float = 20) -> str:
        if self.is_running and self.base_url is not None:
            return self.base_url

        self.data_dir.mkdir(parents=True, exist_ok=True)
        database = Database(self.data_dir / "tasks.db")
        downloader = create_downloader(self.data_dir, find_ffmpeg())
        queue = TaskQueue(database, downloader, worker_count=2)
        page_manager = PageCollectionManager(
            database,
            queue,
            lambda _url: BrowserPageCollector(
                lambda: PlaywrightBrowserSession(self.data_dir / "browser")
            ),
        )
        port = select_available_port()

        def request_shutdown() -> None:
            if self._server is not None:
                self._server.should_exit = True

        app = create_app(
            database,
            queue,
            default_download_dir=self.default_download_dir,
            pick_directory=self.pick_directory,
            open_directory=self.open_directory,
            shutdown_callback=request_shutdown,
            static_dir=find_frontend_directory(),
            page_collection_manager=page_manager,
            shutdown_on_page_disconnect=False,
        )
        server = uvicorn.Server(create_server_config(app, port))
        thread = threading.Thread(
            target=server.run,
            name="douyin-local-backend",
            daemon=True,
        )
        self.port = port
        self.base_url = f"http://127.0.0.1:{port}"
        self._server = server
        self._thread = thread
        thread.start()

        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                if not thread.is_alive():
                    raise RuntimeError("本地服务启动失败")
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.02)
            else:
                raise TimeoutError("等待本地服务启动超时")

            self._runtime_file.write_text(
                json.dumps({"port": port, "pid": os.getpid()}, ensure_ascii=False),
                encoding="utf-8",
            )
            return self.base_url
        except Exception:
            self.stop(timeout=min(timeout, 5))
            raise

    def stop(self, timeout: float = 10) -> None:
        thread = self._thread
        server = self._server
        if thread is None:
            self._runtime_file.unlink(missing_ok=True)
            return

        if server is not None:
            server.should_exit = True
        thread.join(timeout=timeout)
        if thread.is_alive():
            raise TimeoutError("等待本地服务停止超时")

        self._runtime_file.unlink(missing_ok=True)
        self._thread = None
        self._server = None
        self.base_url = None
        self.port = None
