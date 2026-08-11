from __future__ import annotations

import threading
from pathlib import Path

from conftest import wait_for_qt
from douyin_downloader.desktop_bridge import DirectoryPickerBridge


def test_directory_picker_returns_main_thread_selection(qt_app, tmp_path: Path) -> None:
    dialog_thread_ids: list[int] = []

    def dialog() -> str:
        dialog_thread_ids.append(threading.get_ident())
        return str(tmp_path)

    bridge = DirectoryPickerBridge(dialog=dialog)
    result: list[Path | None] = []
    worker = threading.Thread(target=lambda: result.append(bridge.pick_directory()))
    worker.start()
    wait_for_qt(lambda: not worker.is_alive(), qt_app)
    worker.join()

    assert result == [tmp_path]
    assert dialog_thread_ids == [threading.get_ident()]


def test_directory_picker_returns_none_when_cancelled(qt_app) -> None:
    bridge = DirectoryPickerBridge(dialog=lambda: "")
    result: list[Path | None] = []
    worker = threading.Thread(target=lambda: result.append(bridge.pick_directory()))
    worker.start()
    wait_for_qt(lambda: not worker.is_alive(), qt_app)
    worker.join()

    assert result == [None]


def test_directory_picker_propagates_dialog_error(qt_app) -> None:
    def broken_dialog() -> str:
        raise RuntimeError("dialog failed")

    bridge = DirectoryPickerBridge(dialog=broken_dialog)
    errors: list[BaseException] = []

    def pick() -> None:
        try:
            bridge.pick_directory()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=pick)
    worker.start()
    wait_for_qt(lambda: not worker.is_alive(), qt_app)
    worker.join()

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "dialog failed"


def test_directory_picker_times_out_without_qt_event_processing(qt_app) -> None:
    bridge = DirectoryPickerBridge(dialog=lambda: "unused", timeout=0.01)
    errors: list[BaseException] = []

    def pick() -> None:
        try:
            bridge.pick_directory()
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=pick)
    worker.start()
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], TimeoutError)
    assert "目录选择" in str(errors[0])
