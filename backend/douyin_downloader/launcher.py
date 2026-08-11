from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import traceback
import webbrowser
from pathlib import Path
from typing import Any

import uvicorn
from platformdirs import user_data_path

from .api import create_app
from .cookies import AnonymousCookieProvider
from .downloader import YtDlpDownloader
from .queue import TaskQueue
from .store import Database


APP_NAME = "DouyinBatchDownloader"
MUTEX_NAME = r"Local\DouyinBatchDownloader.SingleInstance"


def application_data_directory() -> Path:
    override = os.environ.get("DOUYIN_DOWNLOADER_DATA_DIR")
    if override:
        directory = Path(override).expanduser().resolve()
        directory.mkdir(parents=True, exist_ok=True)
        return directory
    return Path(user_data_path(APP_NAME, appauthor=False, ensure_exists=True))


def should_open_browser() -> bool:
    return os.environ.get("DOUYIN_DOWNLOADER_NO_BROWSER") != "1"


def default_download_directory() -> Path:
    return Path.home() / "Videos" / "抖音批量下载"


def select_available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def find_frontend_directory() -> Path:
    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "frontend"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


def find_ffmpeg() -> Path | None:
    try:
        import imageio_ffmpeg

        return Path(imageio_ffmpeg.get_ffmpeg_exe())
    except (ImportError, RuntimeError, OSError):
        return None


def create_downloader(
    data_dir: Path,
    ffmpeg_location: Path | None = None,
) -> YtDlpDownloader:
    cookie_provider = AnonymousCookieProvider(data_dir / "browser")
    return YtDlpDownloader(
        ffmpeg_location=ffmpeg_location,
        cookie_file=cookie_provider.cookie_file,
        cookie_provider=cookie_provider,
    )


def create_server_config(app: Any, port: int) -> uvicorn.Config:
    return uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        access_log=False,
        log_config=None,
        timeout_graceful_shutdown=5,
    )


class SingleInstance:
    def __init__(self) -> None:
        self.handle: Any = None
        self.already_running = False

    def acquire(self) -> bool:
        if os.name != "nt":
            return True
        import ctypes

        kernel32 = ctypes.windll.kernel32
        self.handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        self.already_running = kernel32.GetLastError() == 183
        return not self.already_running

    def close(self) -> None:
        if self.handle and os.name == "nt":
            import ctypes

            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


def _pick_directory() -> Path | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        selected = filedialog.askdirectory(title="选择视频保存目录", mustexist=False)
    finally:
        root.destroy()
    return Path(selected) if selected else None


def _open_directory(path: Path) -> None:
    if os.name == "nt":
        os.startfile(path)  # type: ignore[attr-defined]
    else:
        webbrowser.open(path.as_uri())


def _open_existing_instance(runtime_file: Path) -> None:
    try:
        runtime = json.loads(runtime_file.read_text(encoding="utf-8"))
        port = int(runtime["port"])
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return
    webbrowser.open(f"http://127.0.0.1:{port}/")


def main() -> None:
    data_dir = application_data_directory()
    runtime_file = data_dir / "runtime.json"
    instance = SingleInstance()
    if not instance.acquire():
        _open_existing_instance(runtime_file)
        return

    logging.basicConfig(
        filename=data_dir / "application.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
    )
    logging.info("启动阶段: 日志已初始化")
    try:
        database = Database(data_dir / "tasks.db")
        database.initialize()
        logging.info("启动阶段: SQLite 已初始化")
        ffmpeg_path = find_ffmpeg()
        logging.info("启动阶段: FFmpeg 路径=%s", ffmpeg_path)
        downloader = create_downloader(data_dir, ffmpeg_path)
        queue = TaskQueue(database, downloader, worker_count=2)
        port = select_available_port()
        logging.info("启动阶段: 已选择端口 %s", port)
        server_holder: dict[str, uvicorn.Server] = {}
    except Exception:
        logging.critical("启动组件初始化失败\n%s", traceback.format_exc())
        instance.close()
        raise

    def request_shutdown() -> None:
        server = server_holder.get("server")
        if server:
            server.should_exit = True

    try:
        frontend_dir = find_frontend_directory()
        logging.info("启动阶段: 前端目录=%s", frontend_dir)
        app = create_app(
            database,
            queue,
            default_download_dir=default_download_directory(),
            pick_directory=_pick_directory,
            open_directory=_open_directory,
            shutdown_callback=request_shutdown,
            static_dir=frontend_dir,
        )
        config = create_server_config(app, port)
        server = uvicorn.Server(config)
        server_holder["server"] = server
        runtime_file.write_text(
            json.dumps({"port": port, "pid": os.getpid()}, ensure_ascii=False),
            encoding="utf-8",
        )
        logging.info("启动阶段: runtime.json 已写入")
    except Exception:
        logging.critical("Web 服务初始化失败\n%s", traceback.format_exc())
        instance.close()
        raise
    if should_open_browser():
        browser_timer = threading.Timer(
            0.6, webbrowser.open, args=(f"http://127.0.0.1:{port}/",)
        )
        browser_timer.daemon = True
        browser_timer.start()
    try:
        server.run()
    finally:
        runtime_file.unlink(missing_ok=True)
        instance.close()


if __name__ == "__main__":
    main()
