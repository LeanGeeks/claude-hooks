"""In-process long-poll waiter registry keyed by message_id."""

from __future__ import annotations

import asyncio
from collections import defaultdict


class WaiterRegistry:
    """A set of asyncio.Events keyed by message_id.

    Multiple coroutines may wait on the same message_id; ``notify`` wakes them
    all. Events are kept until explicitly cleared so that an answer arriving
    before any waiter parks is still observable: ``wait`` checks the flag
    immediately and returns without blocking if already set.
    """

    def __init__(self) -> None:
        self._events: dict[int, asyncio.Event] = defaultdict(asyncio.Event)

    def _event(self, message_id: int) -> asyncio.Event:
        return self._events[message_id]

    async def wait(self, message_id: int, timeout: float) -> bool:
        """Wait up to ``timeout`` seconds. Returns True if notified."""
        event = self._event(message_id)
        if event.is_set():
            return True
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def notify(self, message_id: int) -> None:
        """Wake any waiters for this message_id."""
        self._event(message_id).set()

    def clear(self, message_id: int) -> None:
        self._events.pop(message_id, None)
