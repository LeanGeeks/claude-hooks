"""In-process long-poll waiter registries.

WaiterRegistry   — latching Event-based, keyed by message_id.  One answer,
                   one wake; the latch is harmless because messages are
                   answered exactly once.

ConditionWaiterRegistry — multi-fire Condition-based, keyed by installation_id.
                          Used for the answer feed (GET /v1/answers): each
                          long-poll parks independently; repeated notifies must
                          each unblock a fresh wait().
"""

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


class ConditionWaiterRegistry:
    """Multi-fire wake registry keyed by installation_id using asyncio.Condition.

    Unlike ``WaiterRegistry`` (latching Event), each ``get_condition()`` /
    ``cond.wait()`` call blocks until the *next* ``notify()``, regardless of
    past notifications.  This is required for repeated long-polling where the
    same installation id may park many times over its lifetime.

    Usage pattern in the endpoint (race-free)::

        cond = answer_waiters.get_condition(installation_id)
        async with cond:
            rows = await run_in_thread(_fetch)   # second DB check under lock
            if not rows:
                try:
                    await asyncio.wait_for(cond.wait(), timeout=float(wait))
                except asyncio.TimeoutError:
                    pass
        # final fetch after wake/timeout
        rows = await run_in_thread(_fetch)

    Holding the condition lock during the second DB check guarantees that any
    concurrent ``notify()`` either fires before we hold the lock (and we see the
    row in the second check) or blocks on the lock until after ``cond.wait()``
    registers our future, so the wake is never missed.

    Key lifecycle: conditions are kept for the process lifetime.  Cleanup is
    deliberately omitted: removing a condition between a caller's check and its
    park would leave the caller waiting on a new object that ``notify()`` will
    never fire.  With bounded installations the steady-state size is negligible.
    """

    def __init__(self) -> None:
        self._conditions: dict[int, asyncio.Condition] = defaultdict(
            asyncio.Condition
        )

    def get_condition(self, key: int) -> asyncio.Condition:
        """Return the asyncio.Condition for *key*, creating it on first access."""
        return self._conditions[key]

    async def notify(self, key: int) -> None:
        """Wake all waiters for *key*.

        Must be awaited; acquires the condition lock before notifying so that
        a concurrent waiter that is between its DB check and its ``cond.wait()``
        call cannot miss the wake.
        """
        cond = self._conditions[key]
        async with cond:
            cond.notify_all()
