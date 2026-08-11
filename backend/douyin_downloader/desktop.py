from __future__ import annotations

import logging
import os
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt, QUrl
from PySide6.QtGui import QCloseEvent
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox

from .desktop_bridge import DirectoryPickerBridge
from .desktop_instance import DesktopInstance
from .launcher import application_data_directory, default_download_directory
from .local_backend import LocalBackend


APP_TITLE = "视频批量下载工具"
INSTANCE_NAME = "DouyinBatchDownloader.Desktop"


class DesktopWindow(QMainWindow):
    def __init__(self, base_url: str, on_close: Callable[[], None]) -> None:
        super().__init__()
        self._on_close = on_close
        self._close_notified = False
        self._load_error_shown = False
        self.setWindowTitle(APP_TITLE)
        self.resize(1200, 800)
        self.setMinimumSize(960, 640)

        self.web_view = QWebEngineView(self)
        self.web_view.loadFinished.connect(self._handle_load_finished)
        self.setCentralWidget(self.web_view)
        self.web_view.setUrl(QUrl(base_url))

    def _handle_load_finished(self, succeeded: bool) -> None:
        if succeeded or self._load_error_shown or not self.isVisible():
            return
        self._load_error_shown = True
        logging.error("桌面页面加载失败：%s", self.web_view.url().toString())
        QMessageBox.critical(
            self,
            APP_TITLE,
            "本地页面加载失败，请关闭软件后重新启动。\n"
            "如仍然失败，请查看 application.log。",
        )

    def closeEvent(self, event: QCloseEvent) -> None:
        if not self._close_notified:
            self._close_notified = True
            self._on_close()
        event.accept()


def activate_window(window: QMainWindow) -> None:
    if window.windowState() & Qt.WindowState.WindowMinimized:
        window.showNormal()
    elif not window.isVisible():
        window.show()
    window.raise_()
    window.activateWindow()


def _open_directory(path: Path) -> None:
    if os.name != "nt":
        raise OSError("桌面版本仅支持 Windows")
    os.startfile(path)  # type: ignore[attr-defined]


def _configure_application(application: QApplication) -> None:
    QCoreApplication.setOrganizationName("DouyinBatchDownloader")
    QCoreApplication.setApplicationName(APP_TITLE)
    QCoreApplication.setApplicationVersion("1.0.0")
    application.setQuitOnLastWindowClosed(True)


def _configure_logging(data_dir: Path) -> None:
    logging.basicConfig(
        filename=data_dir / "application.log",
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        encoding="utf-8",
    )


def main() -> int:
    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    application = QApplication.instance() or QApplication(sys.argv)
    _configure_application(application)

    instance = DesktopInstance(INSTANCE_NAME)
    if not instance.acquire():
        return 0

    data_dir = application_data_directory()
    _configure_logging(data_dir)
    bridge = DirectoryPickerBridge()
    backend = LocalBackend(
        data_dir=data_dir,
        default_download_dir=default_download_directory(),
        pick_directory=bridge.pick_directory,
        open_directory=_open_directory,
    )
    window: DesktopWindow | None = None
    exit_code = 1
    try:
        logging.info("正在启动桌面本地服务")
        base_url = backend.start()
        window = DesktopWindow(base_url, on_close=application.quit)
        instance.activation_requested.connect(lambda: activate_window(window))
        window.show()
        exit_code = application.exec()
    except BaseException:
        logging.critical("桌面程序启动或运行失败\n%s", traceback.format_exc())
        QMessageBox.critical(
            window,
            APP_TITLE,
            "软件启动失败，请重试。\n如仍然失败，请查看 application.log。",
        )
        exit_code = 1
    finally:
        try:
            backend.stop()
        except BaseException:
            logging.exception("停止本地服务失败")
            exit_code = 1
        instance.close()
    return exit_code
