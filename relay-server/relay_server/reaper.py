"""Background reaper: expire stale messages and purge old idempotency keys.

Phase 5 background task — runs on a configurable interval (default 30 s).

What it does each tick
----------------------
1. **Expired messages** — rows with ``state = 'open' AND expires_at < now``:
   - Transitions state to ``'expired'`` in the DB.
   - Best-effort ``edit_reply_markup(..., keyboard=None)`` to strip the
     Telegram inline keyboard.  We chose Option A from the spec: keyboard-only
     strip, no text edit.  Rationale: avoids the extra ``editMessageText`` call
     (rate-limit surface, extra failure point) and the user can already see the
     question is gone when they open the chat.
   - Notifies the WaiterRegistry so any parked long-polls wake up immediately
     and return ``state=expired`` instead of waiting for their timeout.
   - Evicts the WaiterRegistry event after notification (memory hygiene — the
     reaper is the natural cleanup point for terminal states; no further
     notifications are expected once a message is terminal).

2. **Binding codes** — left untouched.  The ``GET /v1/bindings/{code}``
   endpoint already returns HTTP 410 for expired codes.  No-op; documented here
   for completeness.

3. **Idempotency keys** — rows whose ``created_at`` is older than 24 h are
   deleted.  These are safe to purge because the 24 h window is generous
   relative to any realistic retry horizon; clients that retried within the
   window already have a stored response.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from .db import run_in_thread
from .telegram_backend import TelegramBackend
from .waiters import WaiterRegistry

logger = logging.getLogger(__name__)

# How long idempotency keys are retained before deletion.
_IDEM_RETENTION_HOURS = 24


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def reaper_tick(
    conn,  # sqlite3.Connection — typed loosely to avoid a circular import
    backend: TelegramBackend,
    waiters: WaiterRegistry,
) -> None:
    """Run one reaper pass.

    Separated from the loop so tests can call it directly (inline tick pattern)
    without needing to manage asyncio tasks or sleep intervals.
    """
    now_iso = _utcnow().isoformat()

    # ------------------------------------------------------------------ #
    # 1. Expire open messages whose expires_at is in the past.            #
    # ------------------------------------------------------------------ #

    def _fetch_expired() -> list:
        """Return all open messages that have passed their expiry deadline."""
        return conn.execute(
            "SELECT id, telegram_chat_id, telegram_message_id"
            " FROM messages"
            " WHERE state = 'open' AND expires_at < ?",
            (now_iso,),
        ).fetchall()

    try:
        expired_rows = await run_in_thread(_fetch_expired)
    except Exception:
        logger.exception("reaper: failed to query expired messages")
        return  # don't proceed if we can't even read the DB

    for row in expired_rows:
        message_id = row["id"]
        chat_id = row["telegram_chat_id"]
        tg_message_id = row["telegram_message_id"]

        # Transition to 'expired' in the DB first so that the state is
        # durable even if the Telegram call fails.
        def _mark_expired(mid=message_id) -> int:
            with conn:
                cur = conn.execute(
                    "UPDATE messages SET state = 'expired'"
                    " WHERE id = ? AND state = 'open'",
                    (mid,),
                )
                return cur.rowcount

        try:
            updated = await run_in_thread(_mark_expired)
        except Exception:
            logger.exception(
                "reaper: failed to mark message %s as expired", message_id
            )
            continue  # try the next message

        if updated == 0:
            # Already transitioned (answered / cancelled / expired by another
            # concurrent path — very unlikely in a single-process setup but
            # handled for correctness).
            logger.debug(
                "reaper: message %s was already in a terminal state, skipping",
                message_id,
            )
            continue

        # Best-effort keyboard strip (Option A: reply_markup only, no text edit).
        try:
            await backend.edit_reply_markup(
                chat_id=chat_id,
                telegram_message_id=tg_message_id,
                keyboard=None,
            )
        except Exception:
            # Log and continue — keyboard strip is best-effort.  The message
            # is already expired in the DB; not stripping the keyboard is a
            # cosmetic issue only.
            logger.warning(
                "reaper: edit_reply_markup failed for message %s (best-effort, continuing)",
                message_id,
                exc_info=True,
            )

        # Wake any long-polling waiters so they can return state=expired
        # immediately instead of waiting for their poll timeout.
        waiters.notify(message_id)

        # Evict the event from the registry: the message is terminal and no
        # further notifications are expected.  Without eviction the defaultdict
        # would accumulate one Event per expired message indefinitely.
        waiters.clear(message_id)

        logger.info("reaper: expired message %s (chat=%s)", message_id, chat_id)

    # ------------------------------------------------------------------ #
    # 2. Binding codes — no-op (see module docstring).                    #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # 3. Delete idempotency keys older than 24 h.                         #
    # ------------------------------------------------------------------ #
    cutoff_iso = (_utcnow() - timedelta(hours=_IDEM_RETENTION_HOURS)).isoformat()

    def _purge_idem() -> int:
        with conn:
            cur = conn.execute(
                "DELETE FROM idempotency_keys WHERE created_at < ?",
                (cutoff_iso,),
            )
            return cur.rowcount

    try:
        deleted = await run_in_thread(_purge_idem)
        if deleted:
            logger.info("reaper: purged %d old idempotency key(s)", deleted)
    except Exception:
        logger.exception("reaper: failed to purge old idempotency keys")


async def reaper_loop(app, interval: float = 30.0) -> None:  # noqa: ANN001
    """Async background task — run ``reaper_tick`` every ``interval`` seconds.

    Designed to be started as an ``asyncio.Task`` from the FastAPI lifespan and
    cancelled on shutdown (``asyncio.CancelledError`` propagates cleanly out of
    ``asyncio.sleep``).

    ``interval`` defaults to 30 s and can be overridden via
    ``app.state.config.reaper_interval`` if that attribute exists (added to
    ``RelayConfig`` as an optional field; falls back to the argument default so
    callers that don't set it are unaffected).
    """
    effective_interval = interval
    try:
        cfg_interval = getattr(app.state.config, "reaper_interval", None)
        if cfg_interval is not None:
            effective_interval = float(cfg_interval)
    except Exception:  # noqa: BLE001
        logger.warning("reaper: failed to read reaper_interval from config; using default", exc_info=True)

    logger.info("reaper: loop started (interval=%.1fs)", effective_interval)
    try:
        while True:
            # Sleep first so that a very short-lived app (tests, quick restart)
            # doesn't pay the cost of a tick before the event loop has fully
            # warmed up and also so cancellation during the sleep is clean —
            # no in-flight DB or backend calls need to be aborted.
            await asyncio.sleep(effective_interval)
            try:
                await reaper_tick(
                    conn=app.state.db,
                    backend=app.state.backend,
                    waiters=app.state.waiters,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # An unexpected error in a tick must not crash the loop.
                logger.exception("reaper: unexpected error in tick, continuing")
    except asyncio.CancelledError:
        logger.info("reaper: loop cancelled (shutdown)")
        raise
