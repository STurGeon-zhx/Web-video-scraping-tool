from __future__ import annotations

import uuid

from conftest import wait_for_qt
from douyin_downloader.desktop_instance import DesktopInstance


def test_second_instance_activates_first(qt_app) -> None:
    name = f"DouyinBatchDownloader.Test.{uuid.uuid4().hex}"
    primary = DesktopInstance(name)
    secondary = DesktopInstance(name)
    replacement: DesktopInstance | None = None
    activated: list[bool] = []
    primary.activation_requested.connect(lambda: activated.append(True))

    try:
        assert primary.acquire() is True
        assert secondary.acquire() is False
        wait_for_qt(lambda: activated == [True], qt_app)

        primary.close()
        replacement = DesktopInstance(name)
        assert replacement.acquire() is True
    finally:
        secondary.close()
        primary.close()
        if replacement is not None:
            replacement.close()


def test_acquire_is_idempotent(qt_app) -> None:
    name = f"DouyinBatchDownloader.Test.{uuid.uuid4().hex}"
    instance = DesktopInstance(name)
    try:
        assert instance.acquire() is True
        assert instance.acquire() is True
    finally:
        instance.close()
