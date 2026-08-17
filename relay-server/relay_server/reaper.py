"""Background reaper: expire stale messages, nudge, sweep, purge idem keys.

Phase 5 background task — runs on a configurable interval (default 30 s).

Epic 19-04 added two passes beside expiry (brd §6). Both are wrapped so that no
nudge failure can reach the expiry pass, which runs first and is unchanged in
shape: **the tick must never die** and a chat the bot cannot talk to must not
cost anyone their TTL.

What it does each tick
----------------------
1. **Expired messages** — rows with ``state = 'open' AND expires_at < now``:
   - Transitions state to ``'expired'`` in the DB.
   - Best-effort ``edit_message`` re-rendering the canonical body, followed by
     the ``edit_reply_markup(..., keyboard=None)`` keyboard strip.  **This
     reverses task 05's "Option A"** (keyboard-only strip, no text edit), which
     rejected the extra ``editMessageText`` as unjustified rate-limit surface.
     Epic 19 is the justification: an expired message must lose its
     ``#unanswered`` tag or Telegram's hashtag search — the pending-work index —
     starts listing work nobody is waiting on (brd §4.3).  The reversal is to be
     written up in ``tasks/05_telegram_prompt_lifecycle_management.md`` by
     task 19-06.
   - Notifies the WaiterRegistry so any parked long-polls wake up immediately
     and return ``state=expired`` instead of waiting for their timeout.
   - Evicts the WaiterRegistry event after notification (memory hygiene — the
     reaper is the natural cleanup point for terminal states; no further
     notifications are expected once a message is terminal).

   - Deletes any live nudge the row owns and clears the ladder columns, in the
     same place the tag is stripped (brd §5.7): expiry is one of the four
     server-side terminal transitions that must not leak a nudge.

2. **Cleanup sweep** (19-04) — rows that have **left** ``open`` still carrying a
   tag (``render_dirty = 1``) or a nudge (``nudge_tg_message_id IS NOT NULL``).
   No other sweep in the codebase looks at non-open rows, and ``_record_answer``
   — the fifth terminal path (brd §2.2) — reaches Telegram not at all: it flips
   ``state`` in SQLite and returns, leaving the tag and the nudge to the hook's
   PATCH, which never arrives if the machine sleeps mid-request. This pass is
   the backstop (state.md 2026-08-16, invariant 10). In the normal case the
   PATCH or cancel render has already cleared the flag and this pass costs
   nothing.

3. **Nudge pass** (19-04) — rows ``state = 'open' AND next_nudge_at < now``,
   coalesced to **one nudge per group and one per chat per tick** (brd §5.3),
   with availability resolved **once per chat** (brd §6). ``next_nudge_at`` is
   NULL unless a chat ran ``/nudge on``, so an unconfigured relay never reaches
   past the first SELECT (invariant 4).

4. **Binding codes** — left untouched.  The ``GET /v1/bindings/{code}``
   endpoint already returns HTTP 410 for expired codes.  No-op; documented here
   for completeness.

5. **Idempotency keys** — rows whose ``created_at`` is older than 24 h are
   deleted.  These are safe to purge because the 24 h window is generous
   relative to any realistic retry horizon; clients that retried within the
   window already have a stored response.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone

from .availability import (
    Window,
    advance_active,
    is_active,
    next_active_start,
    parse_nudge_schedule,
    parse_windows,
)
from .config import RelayConfig
from .db import RecipientRow, load_recipient, run_in_thread
from .render import TAG, awaits_human, payload_for, render_body, strip_tag
from .telegram_backend import (
    TelegramApiError,
    TelegramBackend,
    TelegramForbidden,
    is_not_modified,
)
from .waiters import WaiterRegistry

logger = logging.getLogger(__name__)

# How long idempotency keys are retained before deletion.
_IDEM_RETENTION_HOURS = 24

# Upper bound on nudge sends in a single tick. One send per chat per tick means
# the *per-chat* rate is bounded by the tick interval (30 s) and never
# approaches brd §2.7's ~1 msg/s, but a relay serving very many chats could
# still burst against Telegram's global ceiling. Chats over the bound keep their
# due times and are picked up by the next tick, oldest work first.
_MAX_NUDGE_SENDS_PER_TICK = 20

# How much of the target's body the nudge quotes. Deliberately short: the
# reply-quote already shows the message (brd §5.2), so this line only has to be
# recognisable in a notification preview.
_NUDGE_SNIPPET_LIMIT = 160
_NUDGE_HEAD = "⏳ still waiting"
_NUDGE_SEP = " — "

# The body is HTML (``PARSE_MODE``), so a raw first line can carry an opening
# tag whose close lives further down — sending that fragment would fail to
# parse and cost us the whole nudge. Tags are dropped; entity references are
# already escaped and are kept as they are.
_HTML_TAG_RE = re.compile(r"<[^>]*>")
_PARTIAL_ENTITY_RE = re.compile(r"&[#A-Za-z0-9]*$")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Nudge scheduling helpers — shared with ``app.py`` (create-time seeding and    #
# the terminal transitions that own a nudge). They live here because the reaper #
# is the nudge engine and ``app.py`` already imports this module; the reverse   #
# import would be a cycle.                                                     #
# --------------------------------------------------------------------------- #


def recipient_windows(recipient: RecipientRow) -> list[Window] | None:
    """Availability windows for a recipient, or ``None`` for always-available.

    ``recipients.windows_json`` holds a canonical **spec string**, not JSON,
    despite the column name — every reader parses it with
    :func:`availability.parse_windows` (state.md, 19-02). An unparseable spec
    degrades to always-available rather than silencing a chat's nudges.
    """
    if not recipient.windows_json:
        return None
    try:
        return parse_windows(recipient.windows_json)
    except ValueError:
        logger.warning(
            "reaper: unparseable windows %r for chat %s; treating as"
            " always available",
            recipient.windows_json,
            recipient.telegram_chat_id,
        )
        return None


def nudge_ladder(recipient: RecipientRow, config: RelayConfig) -> list[timedelta]:
    """The chat's nudge ladder, capped at ``config.nudge_max`` rungs.

    Per-chat schedule wins over ``config.nudge_default_schedule``; the cap is
    applied here so a schedule stored before the cap was lowered cannot buy a
    fourth 03:00 notification.
    """
    spec = recipient.nudge_schedule or config.nudge_default_schedule
    ladder: list[timedelta]
    try:
        ladder = parse_nudge_schedule(spec, max(1, int(config.nudge_max)))
    except ValueError:
        logger.warning(
            "reaper: unparseable nudge schedule %r for chat %s; falling back to"
            " the server default",
            spec,
            recipient.telegram_chat_id,
        )
        try:
            ladder = parse_nudge_schedule(
                config.nudge_default_schedule, max(1, int(config.nudge_max))
            )
        except ValueError:
            logger.error(
                "reaper: server default nudge schedule %r is unparseable;"
                " nudges disabled for chat %s",
                config.nudge_default_schedule,
                recipient.telegram_chat_id,
            )
            return []
    return ladder[: max(0, int(config.nudge_max))]


def next_nudge_due(
    now: datetime,
    recipient: RecipientRow,
    config: RelayConfig,
    *,
    nudge_count: int,
    windows: list[Window] | None,
) -> datetime | None:
    """When the ``nudge_count``-th nudge of this row falls due, or ``None``.

    ``None`` means *never again*: the ladder is spent (the cap is reached — and
    it is checked here, before any send, so an off-by-one cannot become a fourth
    notification) or the chat's windows are never active (19-01's
    ``advance_active`` contract).

    Each rung is measured from the previous event — creation for the first,
    the last nudge for the rest — in **active** time (brd §3.4), which is why
    ``advance_active`` and not plain addition. ``windows`` is passed in rather
    than resolved here so a tick can resolve availability once per chat.
    """
    ladder = nudge_ladder(recipient, config)
    if nudge_count < 0 or nudge_count >= len(ladder):
        return None
    return advance_active(now, ladder[nudge_count], recipient.tz, windows)


def nudge_text(payload: dict, *, extra: int) -> str:
    """The nudge body: a short recall line, plus ``+N more`` when it speaks for
    several rows (brd §5.3). Never a copy of the prompt — the reply-quote
    already shows it — and never a keyboard: the buttons live on the original,
    one tap away through the quote.
    """
    snippet = ""
    for line in strip_tag(str(payload.get("text") or "")).splitlines():
        cleaned = _HTML_TAG_RE.sub("", line).strip()
        if cleaned:
            snippet = cleaned
            break
    if len(snippet) > _NUDGE_SNIPPET_LIMIT:
        snippet = (
            _PARTIAL_ENTITY_RE.sub("", snippet[:_NUDGE_SNIPPET_LIMIT].rstrip())
            + "…"
        )
    body = f"{_NUDGE_HEAD}{_NUDGE_SEP}{snippet}" if snippet else _NUDGE_HEAD
    if extra > 0:
        body += f"\n\n+{extra} more {TAG}"
    return body


async def delete_nudge(
    conn,  # sqlite3.Connection — typed loosely to avoid a circular import
    backend: TelegramBackend,
    *,
    message_id: int,
    chat_id: int,
    nudge_tg_message_id: int | None,
) -> None:
    """Delete a row's live nudge and clear the column **regardless**.

    Best-effort by contract (brd §5.7): a failed delete logs and continues. The
    column is cleared either way, so a stale id cannot pin a row into the
    cleanup sweep forever — the visible cost of a lost delete is one orphaned
    reminder in the chat, and retrying it every 30 s forever is worse.
    """
    if nudge_tg_message_id is None:
        return
    try:
        await backend.delete_message(
            chat_id=int(chat_id), telegram_message_id=int(nudge_tg_message_id)
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            "nudge delete failed for message %s (nudge=%s, best-effort,"
            " continuing)",
            message_id,
            nudge_tg_message_id,
            exc_info=True,
        )

    def _clear() -> None:
        with conn:
            conn.execute(
                "UPDATE messages SET nudge_tg_message_id = NULL WHERE id = ?",
                (message_id,),
            )

    try:
        await run_in_thread(_clear)
    except Exception:
        logger.exception(
            "failed to clear nudge id for message %s (will retry next tick)",
            message_id,
        )


async def reaper_tick(
    conn,  # sqlite3.Connection — typed loosely to avoid a circular import
    backend: TelegramBackend,
    waiters: WaiterRegistry,
    config: RelayConfig | None = None,
    *,
    now: datetime | None = None,
) -> None:
    """Run one reaper pass.

    Separated from the loop so tests can call it directly (inline tick pattern)
    without needing to manage asyncio tasks or sleep intervals.

    ``config`` supplies the nudge knobs and comes from ``app.state.config`` —
    the single source of truth for ``nudge_default_schedule`` and ``nudge_max``
    (state.md, 19-02). Without it the **nudge pass is skipped**: inventing the
    server's defaults locally is exactly the duplication that note forbids, and
    a caller that wants nudges has a config to hand. Expiry and the cleanup
    sweep need no config and always run.

    ``now`` exists for tests that need a specific wall-clock instant (an
    availability window that has closed); production passes nothing.
    """
    now_dt = now or _utcnow()
    now_iso = now_dt.isoformat()

    # ------------------------------------------------------------------ #
    # 1. Expire open messages whose expires_at is in the past.            #
    # ------------------------------------------------------------------ #

    def _fetch_expired() -> list:
        """Return all open messages that have passed their expiry deadline.

        ``payload_json`` rides along in the same query because the text edit
        below needs the canonical body: this pass holds the shared connection
        lock and can cover dozens of rows, so a per-row re-read would be exactly
        the wrong shape.
        """
        return conn.execute(
            "SELECT id, telegram_chat_id, telegram_message_id, payload_json,"
            " nudge_tg_message_id"
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
        # ``next_nudge_at`` and ``render_dirty`` are cleared with the flip:
        # every terminal transition ends the ladder, and the text edit below
        # renders the row untagged, so nothing is left for the cleanup sweep.
        def _mark_expired(mid=message_id) -> int:
            with conn:
                cur = conn.execute(
                    "UPDATE messages SET state = 'expired',"
                    " next_nudge_at = NULL, render_dirty = 0"
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

        # Best-effort text re-render: the row is now 'expired', so the body
        # renders without the ``#unanswered`` tag. The state we pass is the one
        # we just wrote — rendering from a row that still said 'open' is how a
        # tag survives its own expiry. Rows with no stored body (legacy or
        # unreadable payloads) get the keyboard strip alone rather than an empty
        # edit Telegram would reject.
        body = render_body(payload_for(row), "expired")
        if body.strip():
            try:
                await backend.edit_message(
                    chat_id=chat_id,
                    telegram_message_id=tg_message_id,
                    text=body,
                    keyboard=None,
                )
            except TelegramApiError as exc:
                # A body that already reads that way is the common correct case.
                if not is_not_modified(exc):
                    logger.warning(
                        "reaper: text edit failed for message %s (%s)"
                        " (best-effort, continuing)",
                        message_id,
                        exc.description,
                    )
            except Exception:
                logger.warning(
                    "reaper: text edit failed for message %s"
                    " (best-effort, continuing)",
                    message_id,
                    exc_info=True,
                )

        # Best-effort keyboard strip.
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

        # The row is terminal, so any nudge speaking for it must go with it
        # (brd §5.7). Fully best-effort — ``delete_nudge`` raises nothing, so an
        # unreachable chat cannot cost this row its expiry or hold up the next.
        await delete_nudge(
            conn,
            backend,
            message_id=message_id,
            chat_id=chat_id,
            nudge_tg_message_id=row["nudge_tg_message_id"],
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
    # 2. Cleanup sweep: rows that have LEFT 'open' still carrying a tag    #
    #    (render_dirty) or a nudge (nudge_tg_message_id).                  #
    # ------------------------------------------------------------------ #
    try:
        await _cleanup_sweep(conn, backend)
    except Exception:
        # Never let the sweep take the tick — the idempotency purge below and
        # the nudge pass are independent of it.
        logger.exception("reaper: cleanup sweep failed, continuing")

    # ------------------------------------------------------------------ #
    # 3. Nudge pass: open rows whose next_nudge_at has come due.           #
    # ------------------------------------------------------------------ #
    if config is not None:
        try:
            await _nudge_pass(conn, backend, config, now_dt, now_iso)
        except Exception:
            logger.exception("reaper: nudge pass failed, continuing")
    else:
        logger.debug("reaper: no config supplied, skipping the nudge pass")

    # ------------------------------------------------------------------ #
    # 4. Binding codes — no-op (see module docstring).                    #
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # 5. Delete idempotency keys older than 24 h.                         #
    # ------------------------------------------------------------------ #
    cutoff_iso = (now_dt - timedelta(hours=_IDEM_RETENTION_HOURS)).isoformat()

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


async def _cleanup_sweep(conn, backend: TelegramBackend) -> None:  # noqa: ANN001
    """Pass 2 — the backstop for the terminal path that reaches Telegram not at
    all (``_record_answer``, brd §2.2's fifth row; state.md 2026-08-16).

    Both halves select on rows that have **left** ``open``, which no other sweep
    in the codebase looks at:

    * ``render_dirty = 1`` → re-render from the canonical payload and edit. The
      row is terminal, so :func:`render.render_body` emits no tag. The edit also
      drops the keyboard, which is correct for a row nobody can answer any more
      and matches what expiry and cancel do.
    * ``nudge_tg_message_id IS NOT NULL`` → delete the nudge. No flag needed;
      the id is its own predicate.

    In the normal case this pass finds nothing: the hook's PATCH (or a cancel)
    lands within a second of the flip and clears the flag first. **The sweep is
    a net, not the mechanism** — its only job is the case where the hook never
    arrives: machine sleeps, process dies, network drops.
    """

    def _fetch_stale() -> list:
        return conn.execute(
            "SELECT id, telegram_chat_id, telegram_message_id, payload_json,"
            " state, render_dirty, nudge_tg_message_id"
            " FROM messages"
            " WHERE state != 'open'"
            "   AND (render_dirty = 1 OR nudge_tg_message_id IS NOT NULL)"
        ).fetchall()

    rows = await run_in_thread(_fetch_stale)
    if not rows:
        return

    for row in rows:
        message_id = int(row["id"])
        chat_id = int(row["telegram_chat_id"])

        if row["render_dirty"]:
            body = render_body(payload_for(row), row["state"])
            # A row with no readable body gets no edit — an empty
            # ``editMessageText`` is a guaranteed Telegram rejection.
            if body.strip():
                try:
                    await backend.edit_message(
                        chat_id=chat_id,
                        telegram_message_id=int(row["telegram_message_id"]),
                        text=body,
                        keyboard=None,
                    )
                except TelegramApiError as exc:
                    # A late PATCH racing the sweep leaves the body already
                    # reading this way — the expected outcome, not a fault.
                    if not is_not_modified(exc):
                        logger.warning(
                            "reaper: cleanup edit failed for message %s (%s)"
                            " (best-effort, continuing)",
                            message_id,
                            exc.description,
                        )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "reaper: cleanup edit failed for message %s"
                        " (best-effort, continuing)",
                        message_id,
                        exc_info=True,
                    )

            # Cleared regardless of the edit's outcome: a message that can never
            # be edited again (deleted by the user, say) must not be retried
            # every 30 s for the rest of the process's life.
            def _clear_flag(mid: int = message_id) -> None:
                with conn:
                    conn.execute(
                        "UPDATE messages SET render_dirty = 0 WHERE id = ?",
                        (mid,),
                    )

            try:
                await run_in_thread(_clear_flag)
            except Exception:
                logger.exception(
                    "reaper: failed to clear render_dirty for message %s",
                    message_id,
                )

        await delete_nudge(
            conn,
            backend,
            message_id=message_id,
            chat_id=chat_id,
            nudge_tg_message_id=row["nudge_tg_message_id"],
        )


def _coalesce_chat(rows: list) -> tuple[object, int, list]:
    """Pick one nudge target for a chat and fold the rest into a count.

    ``rows`` are that chat's due rows, oldest first. Two keys (brd §5.3):

    * **within a ``group_id``** — an AskUserQuestion spanning several messages
      is one target, represented by its **first** member, never one nudge per
      question;
    * **within the chat** — the oldest target wins the nudge and every other
      target becomes part of ``+N more``.

    ``N`` counts distinct *targets*, not rows, for the same reason
    ``_distinct_open_targets`` does in ``app.py``: a group of four is one thing
    waiting on you. Returns ``(target_row, extra_targets, folded_rows)`` where
    ``folded_rows`` is every row but the target — including the target's own
    group siblings, which own nothing and only get their due time pushed
    (invariant 6).
    """
    representatives: dict[object, object] = {}
    for row in rows:
        gid = payload_for(row).get("group_id")
        key = gid if gid is not None else ("msg", int(row["id"]))
        # Rows arrive oldest-first, so the first one seen for a key is both the
        # group's first member and the oldest of its targets.
        representatives.setdefault(key, row)
    reps = list(representatives.values())
    target = reps[0]
    folded = [r for r in rows if int(r["id"]) != int(target["id"])]
    return target, len(reps) - 1, folded


async def _set_due(conn, ids: list[int], when: datetime | None) -> None:  # noqa: ANN001
    """Set ``next_nudge_at`` on ``ids`` to ``when`` (NULL when ``when`` is None).

    A row that left ``open`` while this tick was working is forced to NULL
    whatever ``when`` says: every terminal transition retires the ladder, and a
    reschedule racing an answer must not put it back.
    """
    if not ids:
        return
    value = when.isoformat() if when is not None else None
    placeholders = ",".join("?" * len(ids))

    def _w() -> None:
        with conn:
            conn.execute(
                f"UPDATE messages SET next_nudge_at ="
                f"   CASE WHEN state = 'open' THEN ? ELSE NULL END"
                f" WHERE id IN ({placeholders})",
                [value, *ids],
            )

    try:
        await run_in_thread(_w)
    except Exception:
        logger.exception("reaper: failed to reschedule nudges for %s", ids)


async def _nudge_pass(
    conn,  # noqa: ANN001
    backend: TelegramBackend,
    config: RelayConfig,
    now_dt: datetime,
    now_iso: str,
) -> None:
    """Pass 3 — emit at most one nudge per chat for the rows that have come due.

    ``next_nudge_at IS NOT NULL`` is the gate that keeps an unconfigured relay
    exactly as it is today (invariant 4): with nudges off nothing is ever seeded,
    the SELECT below returns nothing, and the pass returns before it reads a
    recipient, resolves a window or writes a column.
    """

    def _fetch_due() -> list:
        return conn.execute(
            "SELECT id, telegram_chat_id, telegram_message_id, payload_json,"
            " created_at, nudge_count, nudge_tg_message_id"
            " FROM messages"
            " WHERE state = 'open' AND next_nudge_at IS NOT NULL"
            "   AND next_nudge_at < ?"
            " ORDER BY created_at, id",
            (now_iso,),
        ).fetchall()

    due = await run_in_thread(_fetch_due)
    if not due:
        return

    # Group by chat, preserving the oldest-first order inside each chat and
    # visiting chats in the order their oldest due row appears.
    by_chat: dict[int, list] = {}
    for row in due:
        by_chat.setdefault(int(row["telegram_chat_id"]), []).append(row)

    sends = 0
    for visited, (chat_id, chat_rows) in enumerate(by_chat.items()):
        if sends >= _MAX_NUDGE_SENDS_PER_TICK:
            logger.warning(
                "reaper: hit the per-tick nudge ceiling (%d); %d chat(s) keep"
                " their due times and are picked up next tick",
                _MAX_NUDGE_SENDS_PER_TICK,
                len(by_chat) - visited,
            )
            break

        # Availability is resolved once per chat, not once per row (brd §6).
        recipient = await run_in_thread(load_recipient, conn, chat_id)

        if not recipient.nudge_enabled:
            # ``/nudge off`` clears next_nudge_at, so a due row here means a
            # write was lost somewhere. The recipient row is authoritative.
            logger.info(
                "reaper: chat %s has nudges off but %d due row(s); clearing",
                chat_id,
                len(chat_rows),
            )
            await _set_due(conn, [int(r["id"]) for r in chat_rows], None)
            continue

        windows = recipient_windows(recipient)
        ladder_len = len(nudge_ladder(recipient, config))

        # The cap is checked **before** the send, not after — an off-by-one here
        # is a fourth 03:00 notification. A capped row should already carry a
        # NULL due time; if one is due anyway it stops here.
        capped = [r for r in chat_rows if int(r["nudge_count"]) >= ladder_len]
        if capped:
            await _set_due(conn, [int(r["id"]) for r in capped], None)
        chat_rows = [r for r in chat_rows if int(r["nudge_count"]) < ladder_len]
        if not chat_rows:
            continue

        # Defensive eligibility re-check (brd §4.1, invariant 7 — secondary fix
        # 19-08).  The create-time seed path filters through awaits_human; the
        # backfill now does too; but a future writer that bypasses both could
        # still set next_nudge_at on an ineligible row.  Re-checking here is
        # nearly free (payload_json is already fetched) and keeps the ladder
        # from emitting a spurious nudge at 03:00.  A bad seed is visible in
        # the logs rather than silent.  This reuses render.awaits_human — it is
        # not a second definition (invariant 7).
        # _fetch_due's WHERE clause guarantees state='open'; pass it directly
        # rather than reading it from the row (it is not in the SELECT list).
        ineligible = [r for r in chat_rows if not awaits_human(payload_for(r), "open")]
        if ineligible:
            for r in ineligible:
                logger.warning(
                    "reaper: row %s is seeded (next_nudge_at set) but does not"
                    " await a human (kind=%r state='open') — clearing"
                    " next_nudge_at to prevent a spurious nudge",
                    int(r["id"]),
                    payload_for(r).get("kind"),
                )
            await _set_due(conn, [int(r["id"]) for r in ineligible], None)
            chat_rows = [r for r in chat_rows if awaits_human(payload_for(r), "open")]
            if not chat_rows:
                continue

        if not is_active(now_dt, recipient.tz, windows):
            # Outside the recipient's hours: emit nothing and push every due row
            # to the next window start. ``None`` means the windows can never be
            # active (19-01), so the ladder is retired rather than retried.
            resume = next_active_start(now_dt, recipient.tz, windows)
            await _set_due(conn, [int(r["id"]) for r in chat_rows], resume)
            logger.info(
                "reaper: chat %s is outside its hours; %d due row(s) pushed to %s",
                chat_id,
                len(chat_rows),
                resume.isoformat() if resume else "never (no active window)",
            )
            continue

        target, extra, folded = _coalesce_chat(chat_rows)
        target_id = int(target["id"])

        # One live nudge per target (brd §5.4): the predecessor goes before the
        # replacement is sent, so a long-pending prompt never grows a column of
        # identical reminders.
        await delete_nudge(
            conn,
            backend,
            message_id=target_id,
            chat_id=chat_id,
            nudge_tg_message_id=target["nudge_tg_message_id"],
        )

        new_nudge_id: int | None = None
        try:
            new_nudge_id = await backend.send_reply(
                chat_id=chat_id,
                text=nudge_text(payload_for(target), extra=extra),
                reply_to_message_id=int(target["telegram_message_id"]),
            )
            sends += 1
        except TelegramForbidden as exc:
            # Terminal, not transient: the bot was blocked or removed. Every
            # other path in app.py answers this by unbinding, but this one has
            # no request to fail and deliberately does **not** unbind — a
            # background sweep silently disconnecting a machine whose user
            # merely archived the chat is the worse failure. Stop nudging that
            # chat and log loudly instead (19-04 implementation notes).
            logger.error(
                "reaper: Telegram forbids messages to chat %s (%s) — clearing"
                " next_nudge_at for its open rows; the binding is left intact"
                " deliberately, re-run /nudge on once the chat is reachable",
                chat_id,
                exc.description or "forbidden",
            )
            await _clear_due_for_chat(conn, chat_id)
            continue
        except Exception:  # noqa: BLE001
            # Best-effort: log and carry on to the schedule bump below, so a
            # chat that keeps failing walks its ladder to the cap and falls
            # silent rather than being retried every 30 s forever.
            logger.warning(
                "reaper: nudge send failed for message %s (best-effort,"
                " continuing)",
                target_id,
                exc_info=True,
            )

        new_count = int(target["nudge_count"]) + 1
        target_due = next_nudge_due(
            now_dt, recipient, config, nudge_count=new_count, windows=windows
        )

        # The nudge id is stored **unconditionally** while the ladder only
        # advances on a still-open row: if the target was answered while we were
        # sending, a guarded UPDATE would drop the id on the floor and leave the
        # nudge in the chat with nothing pointing at it. Recorded, the cleanup
        # sweep deletes it on the very next tick.
        def _record(
            mid: int = target_id,
            count: int = new_count,
            due_iso: str | None = (
                target_due.isoformat() if target_due is not None else None
            ),
            nudge_id: int | None = new_nudge_id,
        ) -> None:
            with conn:
                conn.execute(
                    "UPDATE messages SET nudge_tg_message_id = ?,"
                    " nudge_count ="
                    "   CASE WHEN state = 'open' THEN ? ELSE nudge_count END,"
                    " next_nudge_at ="
                    "   CASE WHEN state = 'open' THEN ? ELSE NULL END"
                    " WHERE id = ?",
                    (nudge_id, count, due_iso, mid),
                )

        try:
            await run_in_thread(_record)
        except Exception:
            logger.exception(
                "reaper: failed to record nudge state for message %s", target_id
            )

        # Folded rows own nothing (invariant 6) — they keep their nudge_count and
        # simply re-arm the same rung, which is what holds the chat to one nudge
        # per rung instead of one per row on the following tick.
        for row in folded:
            row_due = next_nudge_due(
                now_dt,
                recipient,
                config,
                nudge_count=int(row["nudge_count"]),
                windows=windows,
            )
            await _set_due(conn, [int(row["id"])], row_due)

        logger.info(
            "reaper: nudged message %s in chat %s (nudge %d/%d, +%d more)",
            target_id,
            chat_id,
            new_count,
            ladder_len,
            extra,
        )


async def _clear_due_for_chat(conn, chat_id: int) -> None:  # noqa: ANN001
    """Retire the ladder for every open row in a chat we cannot talk to."""

    def _w() -> int:
        with conn:
            cur = conn.execute(
                "UPDATE messages SET next_nudge_at = NULL"
                " WHERE telegram_chat_id = ? AND state = 'open'"
                " AND next_nudge_at IS NOT NULL",
                (chat_id,),
            )
            return cur.rowcount

    try:
        cleared = await run_in_thread(_w)
        logger.warning(
            "reaper: cleared next_nudge_at on %d open row(s) in chat %s",
            cleared,
            chat_id,
        )
    except Exception:
        logger.exception(
            "reaper: failed to clear next_nudge_at for chat %s", chat_id
        )


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
                    # The nudge knobs live on RelayConfig and nowhere else
                    # (state.md, 19-02) — read the app's config, never a fresh
                    # one, or an operator's overrides are silently ignored.
                    config=getattr(app.state, "config", None),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # An unexpected error in a tick must not crash the loop.
                logger.exception("reaper: unexpected error in tick, continuing")
    except asyncio.CancelledError:
        logger.info("reaper: loop cancelled (shutdown)")
        raise
