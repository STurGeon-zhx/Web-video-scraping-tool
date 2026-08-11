import os
import time
from collections.abc import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-gpu --no-sandbox")

import pytest
from PySide6.QtWidgets import QApplication


@pytest.fixture(scope="session")
def qt_app() -> QApplication:
    app = QApplication.instance() or QApplication([])
    return app


def wait_for_qt(
    predicate: Callable[[], bool],
    app: QApplication,
    timeout: float = 3.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("等待 Qt 条件超时")
