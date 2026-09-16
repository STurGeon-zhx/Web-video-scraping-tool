from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QUrl

from douyin_downloader.desktop import DesktopWindow, _configure_logging, activate_window


def test_desktop_window_loads_local_app_and_closes_backend(qt_app) -> None:
    stopped: list[bool] = []
    window = DesktopWindow(
        "http://127.0.0.1:8765",
        on_close=lambda: stopped.append(True),
    )

    assert window.windowTitle() == "视频批量下载工具"
    assert window.minimumWidth() >= 960
    assert window.minimumHeight() >= 640
    assert window.web_view.url() == QUrl("http://127.0.0.1:8765")
    window.close()
    window.close()
    qt_app.processEvents()

    assert stopped == [True]


def test_activate_window_restores_minimized_window(qt_app) -> None:
    window = DesktopWindow("about:blank", on_close=lambda: None)
    try:
        window.showMinimized()
        qt_app.processEvents()

        activate_window(window)
        qt_app.processEvents()

        assert window.isVisible()
        assert not bool(window.windowState() & Qt.WindowState.WindowMinimized)
    finally:
        window.close()


def test_logging_uses_process_log_when_main_log_is_locked(
    monkeypatch, tmp_path: Path
) -> None:
    filenames: list[Path] = []

    def fake_basic_config(**options) -> None:
        filename = Path(options["filename"])
        filenames.append(filename)
        if filename.name == "application.log":
            raise PermissionError("locked")

    monkeypatch.setattr(logging, "basicConfig", fake_basic_config)

    _configure_logging(tmp_path)

    assert filenames[0] == tmp_path / "application.log"
    assert filenames[1].parent == tmp_path
    assert filenames[1].name.startswith("application-")
    assert filenames[1].suffix == ".log"
