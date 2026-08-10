from __future__ import annotations

import asyncio
from collections.abc import Callable


class PageSessionTracker:
    def __init__(
        self,
        shutdown_callback: Callable[[], None],
        grace_seconds: float = 3.0,
    ) -> None:
        self.shutdown_callback = shutdown_callback
        self.grace_seconds = grace_seconds
        self.active_sessions = 0
        self._ever_connected = False
        self._lock = asyncio.Lock()
        self._shutdown_task: asyncio.Task[None] | None = None

    async def connect(self) -> None:
        async with self._lock:
            self.active_sessions += 1
            self._ever_connected = True
            if self._shutdown_task is not None:
                self._shutdown_task.cancel()
                self._shutdown_task = None

    async def disconnect(self) -> None:
        async with self._lock:
            if self.active_sessions == 0:
                return
            self.active_sessions -= 1
            if self._ever_connected and self.active_sessions == 0:
                if self._shutdown_task is not None:
                    self._shutdown_task.cancel()
                self._shutdown_task = asyncio.create_task(self._shutdown_after_grace())

    async def _shutdown_after_grace(self) -> None:
        try:
            await asyncio.sleep(self.grace_seconds)
            async with self._lock:
                if self.active_sessions != 0:
                    return
                self._shutdown_task = None
            self.shutdown_callback()
        except asyncio.CancelledError:
            return

    async def close(self) -> None:
        async with self._lock:
            if self._shutdown_task is not None:
                self._shutdown_task.cancel()
                self._shutdown_task = None
