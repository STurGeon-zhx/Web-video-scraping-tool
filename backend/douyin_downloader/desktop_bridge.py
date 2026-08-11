from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtWidgets import QFileDialog


@dataclass
class _DirectoryRequest:
    completed: threading.Event = field(default_factory=threading.Event)
    result: Path | None = None
    error: BaseException | None = None
    cancelled: bool = False


class DirectoryPickerBridge(QObject):
    """Marshal a synchronous backend directory request onto the Qt GUI thread."""

    _requested = Signal(object)

    def __init__(
        self,
        dialog: Callable[[], str] | None = None,
        timeout: float = 60.0,
    ) -> None:
        super().__init__()
        self._dialog = dialog or self._show_dialog
        self._timeout = timeout
        self._requested.connect(
            self._handle_request,
            Qt.ConnectionType.QueuedConnection,
        )

    @staticmethod
    def _show_dialog() -> str:
        return QFileDialog.getExistingDirectory(None, "选择视频保存目录")

    def pick_directory(self) -> Path | None:
        if QThread.currentThread() == self.thread():
            return self._selected_path(self._dialog())

        request = _DirectoryRequest()
        self._requested.emit(request)
        if not request.completed.wait(self._timeout):
            request.cancelled = True
            raise TimeoutError("等待目录选择超时")
        if request.error is not None:
            raise request.error
        return request.result

    @staticmethod
    def _selected_path(selected: str) -> Path | None:
        return Path(selected) if selected else None

    @Slot(object)
    def _handle_request(self, request: _DirectoryRequest) -> None:
        if request.cancelled:
            return
        try:
            request.result = self._selected_path(self._dialog())
        except BaseException as exc:
            request.error = exc
        finally:
            request.completed.set()
