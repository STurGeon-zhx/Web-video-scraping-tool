from __future__ import annotations

from PySide6.QtCore import Qt, QUrl

from douyin_downloader.desktop import DesktopWindow, activate_window


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
