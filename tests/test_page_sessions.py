import asyncio

import pytest

from douyin_downloader.api import stream_page_session
from douyin_downloader.page_sessions import PageSessionTracker


@pytest.mark.asyncio
async def test_last_page_disconnects_then_shutdown_runs_after_grace() -> None:
    shutdown_called = asyncio.Event()
    tracker = PageSessionTracker(shutdown_called.set, grace_seconds=0.01)

    await tracker.connect()
    await tracker.disconnect()

    await asyncio.wait_for(shutdown_called.wait(), timeout=0.2)


@pytest.mark.asyncio
async def test_reconnect_during_grace_cancels_shutdown() -> None:
    shutdown_called = asyncio.Event()
    tracker = PageSessionTracker(shutdown_called.set, grace_seconds=0.03)

    await tracker.connect()
    await tracker.disconnect()
    await tracker.connect()
    await asyncio.sleep(0.05)

    assert shutdown_called.is_set() is False
    await tracker.close()


@pytest.mark.asyncio
async def test_one_of_multiple_pages_disconnects_without_shutdown() -> None:
    shutdown_called = asyncio.Event()
    tracker = PageSessionTracker(shutdown_called.set, grace_seconds=0.01)

    await tracker.connect()
    await tracker.connect()
    await tracker.disconnect()
    await asyncio.sleep(0.03)

    assert tracker.active_sessions == 1
    assert shutdown_called.is_set() is False
    await tracker.close()


@pytest.mark.asyncio
async def test_disconnect_before_first_connection_does_not_shutdown() -> None:
    shutdown_called = asyncio.Event()
    tracker = PageSessionTracker(shutdown_called.set, grace_seconds=0.01)

    await tracker.disconnect()
    await asyncio.sleep(0.03)

    assert shutdown_called.is_set() is False


@pytest.mark.asyncio
async def test_page_session_stream_registers_until_closed() -> None:
    shutdown_called = asyncio.Event()
    tracker = PageSessionTracker(shutdown_called.set, grace_seconds=0.01)
    events = stream_page_session(tracker, keepalive_interval=0, max_events=1)

    assert await anext(events) == ": keep-alive\n\n"
    assert tracker.active_sessions == 1

    await events.aclose()
    await asyncio.wait_for(shutdown_called.wait(), timeout=0.2)

    assert tracker.active_sessions == 0
