"""Tiny publish/subscribe hub used to push live updates to web clients."""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger("events")


class EventHub:
    def __init__(self, queue_size: int = 256) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._queue_size = queue_size
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Remember the event loop so publish() also works from worker threads."""
        self._loop = loop

    @contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(self._queue_size)
        self._subscribers.add(queue)
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)

    def publish(self, kind: str, data: Any) -> None:
        event = {"type": kind, "data": data}
        if self._loop is not None:
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not self._loop:
                # asyncio queues are not thread safe: hand over to the event loop
                self._loop.call_soon_threadsafe(self._deliver, event)
                return
        self._deliver(event)

    def _deliver(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A stuck client must not block everybody else; it resyncs on reconnect.
                log.debug("Event-Queue voll, Client wird übersprungen")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)
