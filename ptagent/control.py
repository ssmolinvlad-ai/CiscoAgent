from __future__ import annotations

import asyncio
import threading


class Stopped(Exception):
    pass


class RunControl:
    """Cooperative pause/cancel: never kills Packet Tracer or abandons a sent mutation."""
    def __init__(self):
        self.paused = threading.Event()
        self.stopped = threading.Event()

    async def checkpoint(self):
        while self.paused.is_set() and not self.stopped.is_set():
            await asyncio.sleep(0.1)
        if self.stopped.is_set():
            raise Stopped("Остановлено пользователем")

    def pause(self):
        self.paused.set()

    def resume(self):
        self.paused.clear()

    def stop(self):
        self.stopped.set()
        self.paused.clear()
