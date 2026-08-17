"""FastAPI application for the relay server (Phase 1)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import json
import logging
import os
import sqlite3
from collections import OrderedDict, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .availability import (
    advance_active,
    describe_active_status,
    format_nudge_schedule,
    format_windows,
    is_active,
    near_tz_matches,
    parse_nudge_schedule,
    parse_tz,
    parse_windows,
)
from .binding_codes import generate_code, normalise_code
from .callback_data import decode as decode_callback_data
from .config import RelayConfig, load_config
from .db import connect, init_schema, load_recipient, run_in_thread
from .reaper import (
    delete_nudge,
    next_nudge_due,
    reaper_loop,
    recipient_windows,
)
from .models import (
    AnswerResponse,
    BindingRequestResponse,
    BindingStatusResponse,
    CreateMessageRequest,
    CreateMessageResponse,
    InstallationMeResponse,
    PatchMessageRequest,
)
from .render import (
    awaits_human,
    payload_for as _payload_for,
    render_body,
    render_body_row,
    strip_tag,
)
from .telegram_backend import (
    FakeTelegramBackend,
    HttpTelegramBackend,
    TelegramApiError,
    TelegramBackend,
    TelegramForbidden,
    is_not_modified as _is_not_modified,
)
from .tokens import hash_token
from .waiters import WaiterRegistry

# Binding code TTL in minutes.
_BINDING_TTL_MINUTES = 10

logger = logging.getLogger(__name__)


def secrets_compare(a: str, b: str) -> bool:
    return hmac.compare_digest(a, b)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _canonical_body_hash(body: Any) -> str:
    """SHA-256 over a canonical JSON encoding (sorted keys, no whitespace)."""
    encoded = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def create_app(
    db_path: str | None = None,
    backend: TelegramBackend | None = None,
    *,
    config: RelayConfig | None = None,
) -> FastAPI:
    # Config precedence: explicit args > config object > env/TOML defaults.
    if config is None:
        overrides: dict[str, Any] = {}
        if db_path is not None:
            overrides["db_path"] = db_path
        config = load_config(overrides=overrides)
    elif db_path is not None:
        config.db_path = db_path

    resolved_db_path = config.db_path

    # If no backend supplied, prefer the real HTTP one when a bot token is
    # configured; otherwise fall back to the recording fake (tests, dev).
    if backend is None:
        if config.bot_token:
            backend = HttpTelegramBackend(config.bot_token)
        else:
            backend = FakeTelegramBackend()
    webhook_secret = config.webhook_secret

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN001
        conn = connect(resolved_db_path)
        init_schema(conn)
        app.state.db = conn
        app.state.backend = backend
        app.state.config = config
        app.state.waiters = WaiterRegistry()
        # Per-installation async lock guarding the idempotent create path so
        # two concurrent POSTs with the same Idempotency-Key in this process
        # serialize through the "claim pending row -> backend call -> store
        # response" sequence. Cross-process serialization is provided by the
        # PRIMARY KEY on idempotency_keys.
        app.state.idem_locks = defaultdict(asyncio.Lock)
        # In-process LRU of recently-seen Telegram update_ids for webhook
        # dedup. Telegram retries on non-2xx and sometimes on slow 2xx, so we
        # bounce duplicates with a fixed-size insertion-ordered set.
        app.state.seen_updates = OrderedDict()
        app.state.seen_updates_max = 1024

        # Install the webhook with Telegram if fully configured and not opted
        # out (tests skip this). Errors are logged but non-fatal — we'd rather
        # serve traffic and let an operator retry than refuse to start.
        if (
            config.set_webhook_on_startup
            and isinstance(backend, HttpTelegramBackend)
            and config.public_url
            and config.webhook_secret
        ):
            url = f"{config.public_url.rstrip('/')}/telegram/webhook/{config.webhook_secret}"
            try:
                await backend.set_webhook(url)
                logger.info("setWebhook OK for host %s", urlparse(url).netloc)
            except Exception:  # noqa: BLE001
                logger.exception("setWebhook failed for %s", url)

        # Start the background reaper task.
        reaper_task = asyncio.create_task(reaper_loop(app), name="reaper")

        try:
            yield
        finally:
            reaper_task.cancel()
            try:
                await reaper_task
            except asyncio.CancelledError:
                pass
            conn.close()
            if isinstance(backend, HttpTelegramBackend):
                await backend.aclose()

    app = FastAPI(title="Telegram Relay (Phase 1)", lifespan=lifespan)

    # ---- Health check (unauthenticated) ------------------------------------

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    # ---- Auth dependency ---------------------------------------------------

    async def require_installation(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> sqlite3.Row:
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=401, detail="missing_bearer_token")
        token = authorization[len("Bearer ") :].strip()
        if not token:
            raise HTTPException(status_code=401, detail="empty_token")
        th = hash_token(token)
        conn: sqlite3.Connection = request.app.state.db

        def _lookup() -> sqlite3.Row | None:
            return conn.execute(
                "SELECT * FROM installations WHERE token_hash = ?",
                (th,),
            ).fetchone()

        row = await run_in_thread(_lookup)
        if row is None:
            raise HTTPException(status_code=401, detail="invalid_token")
        if row["revoked_at"] is not None:
            raise HTTPException(status_code=401, detail="revoked_token")

        def _touch() -> None:
            with conn:
                conn.execute(
                    "UPDATE installations SET last_seen_at = ? WHERE id = ?",
                    (_utcnow_iso(), row["id"]),
                )

        await run_in_thread(_touch)
        request.state.installation_id = row["id"]
        return row

    # ---- Auto-unbind helper -----------------------------------------------

    async def _unbind_installation(
        conn: sqlite3.Connection, installation_id: int
    ) -> None:
        """Clear the chat binding after Telegram says we're not welcome.

        We deliberately keep ``bound_user_id`` for audit history — the chat id
        is the only field that controls whether we can talk; the user id is
        record-keeping for who last bound the install.
        """

        def _w() -> None:
            with conn:
                conn.execute(
                    "UPDATE installations SET telegram_chat_id = NULL"
                    " WHERE id = ?",
                    (installation_id,),
                )

        await run_in_thread(_w)
        logger.warning(
            "auto-unbound installation %s after Telegram Forbidden",
            installation_id,
        )

    # ---- Idempotency helpers ----------------------------------------------

    async def _idem_lookup(
        conn: sqlite3.Connection, installation_id: int, key: str
    ) -> sqlite3.Row | None:
        def _q() -> sqlite3.Row | None:
            return conn.execute(
                "SELECT request_hash, response_json FROM idempotency_keys "
                "WHERE installation_id = ? AND key = ?",
                (installation_id, key),
            ).fetchone()

        return await run_in_thread(_q)

    async def _idem_claim_pending(
        conn: sqlite3.Connection,
        installation_id: int,
        key: str,
        request_hash: str,
    ) -> bool:
        """Insert a 'pending' sentinel; return True if this caller claimed it.

        On primary-key conflict returns False — another caller is in flight
        for the same (installation, key).
        """

        def _w() -> bool:
            try:
                with conn:
                    conn.execute(
                        "INSERT INTO idempotency_keys"
                        "(installation_id, key, request_hash,"
                        " response_json, created_at)"
                        " VALUES (?, ?, ?, NULL, ?)",
                        (
                            installation_id,
                            key,
                            request_hash,
                            _utcnow_iso(),
                        ),
                    )
                return True
            except sqlite3.IntegrityError:
                return False

        return await run_in_thread(_w)

    async def _idem_finalize(
        conn: sqlite3.Connection,
        installation_id: int,
        key: str,
        response: dict[str, Any],
    ) -> None:
        def _w() -> None:
            with conn:
                conn.execute(
                    "UPDATE idempotency_keys SET response_json = ?"
                    " WHERE installation_id = ? AND key = ?",
                    (json.dumps(response), installation_id, key),
                )

        await run_in_thread(_w)

    async def _idem_abandon(
        conn: sqlite3.Connection, installation_id: int, key: str
    ) -> None:
        """Drop a pending sentinel so the client can retry from scratch."""

        def _w() -> None:
            with conn:
                conn.execute(
                    "DELETE FROM idempotency_keys"
                    " WHERE installation_id = ? AND key = ? AND response_json IS NULL",
                    (installation_id, key),
                )

        await run_in_thread(_w)

    # ---- Routes ------------------------------------------------------------

    @app.get("/v1/installations/me", response_model=InstallationMeResponse)
    async def me(
        request: Request,
        installation=Depends(require_installation),
    ) -> InstallationMeResponse:
        conn: sqlite3.Connection = request.app.state.db
        chat_id_raw = installation["telegram_chat_id"]
        base = InstallationMeResponse(
            id=installation["id"],
            label=installation["label"],
            chat_bound=chat_id_raw is not None,
            last_seen_at=installation["last_seen_at"],
        )
        if chat_id_raw is not None:
            chat_id = int(chat_id_raw)
            recipient = await run_in_thread(load_recipient, conn, chat_id)
            windows = (
                parse_windows(recipient.windows_json)
                if recipient.windows_json
                else None
            )
            now = _utcnow()
            base.tz = recipient.tz
            base.windows = (
                format_windows(windows) if windows is not None else None
            )
            base.active_now = is_active(now, recipient.tz, windows)
            base.nudge_enabled = recipient.nudge_enabled
        return base

    # ---- Binding routes -------------------------------------------------------

    @app.post("/v1/bindings/request")
    async def request_binding(
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        installation=Depends(require_installation),
    ) -> Response:
        conn: sqlite3.Connection = request.app.state.db
        installation_id = installation["id"]

        # POST /v1/bindings/request has no body; use a stable hash of an empty
        # dict so retries with the same key always match.
        request_hash = _canonical_body_hash({}) if idempotency_key else ""

        idem_locks: dict[tuple[int, str], asyncio.Lock] = (
            request.app.state.idem_locks
        )
        lock_key = (installation_id, idempotency_key) if idempotency_key else None
        lock = idem_locks[lock_key] if lock_key is not None else None

        async def _do_request() -> Response:
            if idempotency_key:
                existing = await _idem_lookup(conn, installation_id, idempotency_key)
                if existing is not None:
                    # Body hash is always "" for this endpoint; a mismatch would
                    # only happen if the stored hash was somehow different (guard
                    # for future-proofing, mirrors create_message pattern).
                    if (
                        existing["request_hash"] is not None
                        and existing["request_hash"] != request_hash
                    ):
                        raise HTTPException(
                            status_code=422,
                            detail="idempotency_key_reused_with_different_body",
                        )
                    if existing["response_json"] is not None:
                        # Completed previously — replay the stored response.
                        return JSONResponse(json.loads(existing["response_json"]))
                    # In-flight sentinel (response_json IS NULL): another worker
                    # is generating the code right now.
                    raise HTTPException(
                        status_code=409,
                        detail="idempotency_key_in_flight",
                    )

                claimed = await _idem_claim_pending(
                    conn, installation_id, idempotency_key, request_hash
                )
                if not claimed:
                    # A concurrent caller raced us to INSERT the sentinel.
                    existing = await _idem_lookup(
                        conn, installation_id, idempotency_key
                    )
                    if existing is not None and existing["response_json"] is not None:
                        return JSONResponse(json.loads(existing["response_json"]))
                    raise HTTPException(
                        status_code=409, detail="idempotency_key_in_flight"
                    )

            now = _utcnow()
            expires_at = now + timedelta(minutes=_BINDING_TTL_MINUTES)
            inserted_code: str | None = None

            def _insert_code() -> str:
                """Generate a unique code, retrying on collision (extremely rare)."""
                for _ in range(10):
                    code = generate_code()
                    try:
                        with conn:
                            conn.execute(
                                "INSERT INTO binding_codes"
                                "(code, installation_id, created_at, expires_at)"
                                " VALUES (?, ?, ?, ?)",
                                (
                                    code,
                                    installation_id,
                                    now.isoformat(),
                                    expires_at.isoformat(),
                                ),
                            )
                        return code
                    except sqlite3.IntegrityError:
                        continue  # collision, try again
                raise RuntimeError("binding code generation failed after 10 retries")

            try:
                code = await run_in_thread(_insert_code)
                inserted_code = code
            except Exception:
                if idempotency_key:
                    await _idem_abandon(conn, installation_id, idempotency_key)
                raise

            response_body = BindingRequestResponse(
                code=code, expires_at=expires_at.isoformat()
            ).model_dump()

            if idempotency_key:
                try:
                    await _idem_finalize(
                        conn, installation_id, idempotency_key, response_body
                    )
                except Exception:
                    # Roll back the binding_codes row and sentinel so the
                    # caller can retry from scratch.
                    if inserted_code is not None:
                        def _rollback_code() -> None:
                            with conn:
                                conn.execute(
                                    "DELETE FROM binding_codes WHERE code = ?",
                                    (inserted_code,),
                                )
                        await run_in_thread(_rollback_code)
                    await _idem_abandon(conn, installation_id, idempotency_key)
                    raise

            return JSONResponse(response_body)

        if lock is not None:
            async with lock:
                return await _do_request()
        return await _do_request()

    @app.get("/v1/bindings/{code}")
    async def get_binding(
        code: str,
        request: Request,
        installation=Depends(require_installation),
    ) -> Response:
        conn: sqlite3.Connection = request.app.state.db
        installation_id = installation["id"]

        normalised = normalise_code(code)
        if normalised is None:
            raise HTTPException(status_code=404, detail="code_not_found")
        code = normalised

        def _lookup() -> sqlite3.Row | None:
            return conn.execute(
                "SELECT * FROM binding_codes WHERE code = ?", (code,)
            ).fetchone()

        row = await run_in_thread(_lookup)
        if row is None:
            raise HTTPException(status_code=404, detail="code_not_found")

        # Security: don't reveal codes that belong to other installations.
        if int(row["installation_id"]) != installation_id:
            raise HTTPException(status_code=404, detail="code_not_found")

        now = _utcnow()
        if row["consumed_at"] is not None:
            return JSONResponse(
                BindingStatusResponse(
                    state="bound",
                    chat_id=row["bound_chat_id"],
                    telegram_user_id=row["bound_user_id"],
                ).model_dump(exclude_none=True)
            )

        # Parse expires_at (stored as ISO string).
        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            expires_at = now  # treat unparseable as expired

        if now > expires_at:
            return JSONResponse(
                BindingStatusResponse(state="expired").model_dump(exclude_none=True),
                status_code=410,
            )

        return JSONResponse(
            BindingStatusResponse(state="pending").model_dump(exclude_none=True)
        )

    @app.post("/v1/messages")
    async def create_message(
        body: CreateMessageRequest,
        request: Request,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        installation=Depends(require_installation),
    ) -> Response:
        conn: sqlite3.Connection = request.app.state.db
        backend: TelegramBackend = request.app.state.backend
        installation_id = installation["id"]

        # Canonicalize on ingest: whatever the client typed, the stored body is
        # untagged (invariant 1). The tag is the render layer's alone, so a
        # client echoing it back cannot pin it onto a resolved message.
        body.text = strip_tag(body.text)

        request_hash = (
            _canonical_body_hash(body.model_dump()) if idempotency_key else ""
        )

        # Serialize concurrent in-process requests for the same
        # (installation, key) so the "claim pending row -> backend call ->
        # store response" sequence is atomic from the client's POV.
        idem_locks: dict[tuple[int, str], asyncio.Lock] = (
            request.app.state.idem_locks
        )
        lock_key = (installation_id, idempotency_key) if idempotency_key else None
        lock = idem_locks[lock_key] if lock_key is not None else None

        async def _do_create() -> Response:
            if idempotency_key:
                existing = await _idem_lookup(
                    conn, installation_id, idempotency_key
                )
                if existing is not None:
                    # Body-hash mismatch is always an error, whether the prior
                    # request completed or is still pending.
                    if (
                        existing["request_hash"] is not None
                        and existing["request_hash"] != request_hash
                    ):
                        raise HTTPException(
                            status_code=422,
                            detail="idempotency_key_reused_with_different_body",
                        )
                    if existing["response_json"] is not None:
                        return JSONResponse(json.loads(existing["response_json"]))
                    # In-flight sentinel from another worker (cross-process).
                    # In Phase 1 we don't have a shared waker, so surface a
                    # retryable conflict and let the caller back off.
                    raise HTTPException(
                        status_code=409,
                        detail="idempotency_key_in_flight",
                    )

                claimed = await _idem_claim_pending(
                    conn, installation_id, idempotency_key, request_hash
                )
                if not claimed:
                    # A concurrent caller raced us to INSERT. Re-read; treat
                    # the same as a regular conflict — re-checks body hash
                    # and either replays the completed response or 409s.
                    existing = await _idem_lookup(
                        conn, installation_id, idempotency_key
                    )
                    if existing is not None:
                        if (
                            existing["request_hash"] is not None
                            and existing["request_hash"] != request_hash
                        ):
                            raise HTTPException(
                                status_code=422,
                                detail="idempotency_key_reused_with_different_body",
                            )
                        if existing["response_json"] is not None:
                            return JSONResponse(
                                json.loads(existing["response_json"])
                            )
                    raise HTTPException(
                        status_code=409, detail="idempotency_key_in_flight"
                    )

            chat_id = installation["telegram_chat_id"]
            if chat_id is None:
                # Drop the pending sentinel so the caller may retry once bound.
                if idempotency_key:
                    await _idem_abandon(conn, installation_id, idempotency_key)
                raise HTTPException(status_code=409, detail="not_bound")

            now = _utcnow()
            expires_at = now + timedelta(seconds=body.ttl_sec)
            payload_json = body.model_dump_json()

            def _insert() -> int:
                with conn:
                    cur = conn.execute(
                        "INSERT INTO messages("
                        "installation_id, telegram_chat_id, telegram_message_id,"
                        " kind, payload_json, state, created_at, expires_at)"
                        " VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                        (
                            installation_id,
                            chat_id,
                            0,  # placeholder; updated after backend call
                            body.kind,
                            payload_json,
                            now.isoformat(),
                            expires_at.isoformat(),
                        ),
                    )
                    return int(cur.lastrowid)

            message_id = await run_in_thread(_insert)

            keyboard_dump = (
                [[btn.model_dump() for btn in row] for row in body.keyboard]
                if body.keyboard
                else None
            )

            try:
                tg_message_id = await backend.send_message(
                    chat_id=chat_id,
                    # The row is 'open' by construction and has no usable id
                    # yet (telegram_message_id is still the 0 placeholder), so
                    # render from the payload we are about to store.
                    text=render_body(body.model_dump(), "open"),
                    keyboard=keyboard_dump,
                    reply_required=body.reply_required,
                    message_id=message_id,
                    # Idle notifications stay answerable but WITHOUT a force_reply
                    # prompt: a chat-wide force_reply auto-targets the newest such
                    # message, so with several idle sessions a reply meant for one
                    # threads to another. Require an explicit Reply instead.
                    force_reply=body.kind != "notification",
                )
            except TelegramForbidden:
                # Bot was blocked or removed from the chat. Clear the binding
                # and surface 409 not_bound to the client (same shape as the
                # never-bound case) so it can re-run `relay-client bind`.
                def _rollback_message() -> None:
                    with conn:
                        conn.execute(
                            "DELETE FROM messages WHERE id = ?", (message_id,)
                        )

                await run_in_thread(_rollback_message)
                await _unbind_installation(conn, installation_id)
                if idempotency_key:
                    await _idem_abandon(conn, installation_id, idempotency_key)
                raise HTTPException(status_code=409, detail="not_bound")
            except Exception:
                # Roll back: remove the placeholder message row and the
                # pending idempotency sentinel so the client can retry from
                # scratch. We intentionally do NOT cache the failure.
                def _rollback() -> None:
                    with conn:
                        conn.execute(
                            "DELETE FROM messages WHERE id = ?", (message_id,)
                        )

                await run_in_thread(_rollback)
                if idempotency_key:
                    await _idem_abandon(conn, installation_id, idempotency_key)
                raise

            # Seed the nudge ladder (epic 19-04) alongside the Telegram id, in
            # the one UPDATE that already runs here. Two reasons it belongs
            # here and not in the INSERT: a row is only ever nudgeable once it
            # has a real ``telegram_message_id`` to reply to, and with nudges
            # off (the default) this writes NULL to a column whose default is
            # NULL — no extra statement, no behaviour change (invariant 4).
            next_nudge_iso = await _seed_next_nudge_at(
                conn, config, chat_id, body, now
            )

            def _update_tg_id() -> None:
                with conn:
                    conn.execute(
                        "UPDATE messages SET telegram_message_id = ?,"
                        " next_nudge_at = ? WHERE id = ?",
                        (tg_message_id, next_nudge_iso, message_id),
                    )

            await run_in_thread(_update_tg_id)

            response_body = CreateMessageResponse(
                message_id=message_id,
                telegram_message_id=tg_message_id,
            ).model_dump()

            if idempotency_key:
                await _idem_finalize(
                    conn, installation_id, idempotency_key, response_body
                )

            return JSONResponse(response_body)

        if lock is not None:
            async with lock:
                return await _do_create()
        return await _do_create()

    @app.patch("/v1/messages/{message_id}")
    async def patch_message(
        message_id: int,
        body: PatchMessageRequest,
        request: Request,
        installation=Depends(require_installation),
    ) -> dict[str, Any]:
        if body.text is None and body.keyboard is None:
            raise HTTPException(
                status_code=400, detail="at_least_one_field_required"
            )
        # Phase 2 deliberately refuses keyboard replacement via PATCH: the
        # current Bot-API edit path can't see the originating message_id, so
        # `_inline_keyboard_payload` would fall back to label/value strings
        # for callback_data and the webhook decoder would reject taps. Clients
        # that need to change buttons should cancel + send a new message.
        if body.keyboard is not None:
            raise HTTPException(
                status_code=400, detail="keyboard_replace_not_supported"
            )
        conn: sqlite3.Connection = request.app.state.db
        backend: TelegramBackend = request.app.state.backend
        installation_id = installation["id"]
        row = await _load_message(conn, message_id, installation_id)
        # The client's text becomes the new canonical body: stored untagged,
        # displayed through the one renderer. Before this, payload_json kept the
        # create-time text forever, so every later re-render (cancel, expiry,
        # group finalize) would resurrect it over whatever the client had
        # baked in — brd §2.8.
        payload = _payload_for(row)
        payload["text"] = strip_tag(body.text or "")
        try:
            await backend.edit_message(
                chat_id=row["telegram_chat_id"],
                telegram_message_id=row["telegram_message_id"],
                text=render_body(payload, row["state"]),
                keyboard=None,
            )
        except TelegramForbidden:
            await _unbind_installation(conn, installation_id)
            raise HTTPException(status_code=409, detail="not_bound")
        except TelegramApiError as exc:
            # Re-patching a message with the text it already carries is a no-op,
            # not a failure — the caller's intent (the body reads like this) is
            # satisfied. Anything else is a genuine upstream fault and says so
            # with a 502 rather than an anonymous 500.
            if not _is_not_modified(exc):
                logger.warning(
                    "patch_message: telegram rejected edit for %s: %s",
                    message_id,
                    exc.description,
                )
                raise HTTPException(status_code=502, detail="telegram_error")

        # Write back only once Telegram has accepted (or already agreed with)
        # the body, so the payload never claims a text the chat never showed.
        payload_json = json.dumps(payload)

        # ``render_dirty`` is cleared here because this *is* the render the flag
        # asks for: the body Telegram just accepted was produced by the one
        # renderer from the canonical payload, so whatever tag a preceding
        # ``_record_answer`` left behind is gone. In the normal case this lands
        # within a second of the flip and the reaper's cleanup sweep finds
        # nothing to do — which is what keeps the sweep a net rather than a
        # second cost on the hot path (state.md 2026-08-16).
        def _persist_text() -> None:
            with conn:
                conn.execute(
                    "UPDATE messages SET payload_json = ?, render_dirty = 0"
                    " WHERE id = ? AND installation_id = ?",
                    (payload_json, message_id, installation_id),
                )

        await run_in_thread(_persist_text)
        return {}

    @app.delete("/v1/messages/{message_id}", status_code=204)
    async def delete_message(
        message_id: int,
        request: Request,
        installation=Depends(require_installation),
    ) -> Response:
        conn: sqlite3.Connection = request.app.state.db
        backend: TelegramBackend = request.app.state.backend
        installation_id = installation["id"]
        row = await _load_message(conn, message_id, installation_id)
        try:
            await backend.delete_message(
                chat_id=row["telegram_chat_id"],
                telegram_message_id=row["telegram_message_id"],
            )
        except TelegramForbidden:
            await _unbind_installation(conn, installation_id)
            raise HTTPException(status_code=409, detail="not_bound")
        return Response(status_code=204)

    @app.post("/v1/messages/{message_id}/cancel")
    async def cancel_message(
        message_id: int,
        request: Request,
        installation=Depends(require_installation),
    ) -> dict[str, Any]:
        conn: sqlite3.Connection = request.app.state.db
        backend: TelegramBackend = request.app.state.backend
        waiters: WaiterRegistry = request.app.state.waiters
        installation_id = installation["id"]
        row = await _load_message(conn, message_id, installation_id)

        # The flip retires the nudge ladder and clears ``render_dirty`` in the
        # same transaction: a terminal row has nothing left to schedule, and the
        # re-render below is the render the flag would have asked for.
        def _cancel() -> int:
            with conn:
                cur = conn.execute(
                    "UPDATE messages SET state='cancelled',"
                    " next_nudge_at = NULL, render_dirty = 0"
                    " WHERE id=? AND state='open'",
                    (message_id,),
                )
                return cur.rowcount

        flipped = await run_in_thread(_cancel)
        if flipped:
            # Drop the tag by re-rendering the canonical body. Re-read *after*
            # the flip: the row loaded above still says 'open' and would render
            # the tag straight back on. Best-effort — a failed edit must never
            # block the transition. Skipped when the row was already terminal so
            # a late cancel cannot overwrite an answer another path baked in
            # (group finalization writes text the payload does not carry).
            fresh = await _load_message(conn, message_id, installation_id)
            final_text = render_body_row(fresh)
            if final_text.strip():
                try:
                    await backend.edit_message(
                        chat_id=fresh["telegram_chat_id"],
                        telegram_message_id=fresh["telegram_message_id"],
                        text=final_text,
                        keyboard=None,
                    )
                except TelegramApiError as exc:
                    # An already-untagged body is the common case, not a fault.
                    if not _is_not_modified(exc):
                        logger.warning(
                            "cancel_message: telegram rejected text edit for"
                            " %s: %s",
                            message_id,
                            exc.description,
                        )
                except Exception:  # noqa: BLE001
                    # Includes TelegramForbidden — the keyboard strip below
                    # raises it too and owns the unbind/409 response.
                    logger.warning(
                        "cancel_message: text edit failed for %s"
                        " (best-effort, continuing)",
                        message_id,
                        exc_info=True,
                    )
        # Cleanup hangs off the transition, not off the flip: a client that
        # cancels a row some other path already answered (the usual
        # PATCH-then-cancel finalize) must still take the nudge with it. The id
        # is read from the pre-flip row — only the reaper writes it, and it
        # cannot change under a cancel.
        await delete_nudge(
            conn,
            backend,
            message_id=message_id,
            chat_id=int(row["telegram_chat_id"]),
            nudge_tg_message_id=row["nudge_tg_message_id"],
        )
        try:
            # Strip the inline keyboard via the dedicated Bot-API-shaped method.
            await backend.edit_reply_markup(
                chat_id=row["telegram_chat_id"],
                telegram_message_id=row["telegram_message_id"],
                keyboard=None,
            )
        except TelegramForbidden:
            waiters.notify(message_id)
            await _unbind_installation(conn, installation_id)
            raise HTTPException(status_code=409, detail="not_bound")
        except TelegramApiError as exc:
            # Cancel is idempotent by contract, and Telegram reports "no
            # keyboard to remove" as a 400 rather than a success. The common
            # caller is a client-side finalize: PATCH the body (``editMessageText``
            # with no ``reply_markup`` already drops the keyboard) and then
            # cancel to flip the state. That second call *always* lands here on
            # the happy path, so treating it as an error made every finalize
            # log a 500 while behaving perfectly — see 15-07's error-log delta.
            if not _is_not_modified(exc):
                waiters.notify(message_id)
                logger.warning(
                    "cancel_message: telegram rejected keyboard strip for %s: %s",
                    message_id,
                    exc.description,
                )
                raise HTTPException(status_code=502, detail="telegram_error")
        waiters.notify(message_id)
        return {}

    @app.get("/v1/messages/{message_id}/answer")
    async def get_answer(
        message_id: int,
        request: Request,
        wait: int = 0,
        installation=Depends(require_installation),
    ) -> Response:
        conn: sqlite3.Connection = request.app.state.db
        waiters: WaiterRegistry = request.app.state.waiters

        row = await _load_message(conn, message_id, installation["id"])

        # Already terminal?
        terminal = _terminal_response(row)
        if terminal is not None:
            return JSONResponse(terminal.model_dump(exclude_none=True))

        if wait <= 0:
            return Response(status_code=204)

        # Park on the event up to `wait` seconds.
        notified = await waiters.wait(message_id, timeout=float(wait))
        if not notified:
            # Timed out — but re-read the DB to guard against a TOCTOU race
            # where the reaper (or any other writer) transitioned the message
            # to a terminal state between our initial read and park.  If the
            # message is now terminal we return it immediately; otherwise the
            # client should retry (204 as before).
            row = await _load_message(conn, message_id, installation["id"])
            terminal = _terminal_response(row)
            if terminal is not None:
                return JSONResponse(terminal.model_dump(exclude_none=True))
            return Response(status_code=204)

        # Re-load and respond.
        row = await _load_message(conn, message_id, installation["id"])
        terminal = _terminal_response(row)
        if terminal is None:
            # Spurious wake or still open; tell client to retry.
            return Response(status_code=204)
        return JSONResponse(terminal.model_dump(exclude_none=True))

    # ---- Telegram webhook --------------------------------------------------

    @app.post("/telegram/webhook/{secret}")
    async def telegram_webhook(secret: str, request: Request) -> Response:
        # Constant-time compare to keep the secret-as-URL-path approach honest.
        if (
            not webhook_secret
            or not secrets_compare(secret, webhook_secret)
        ):
            # 404, not 401: we don't want to advertise that the endpoint exists
            # to anything probing.
            raise HTTPException(status_code=404, detail="not_found")
        try:
            update = await request.json()
        except Exception:  # noqa: BLE001
            raise HTTPException(status_code=400, detail="invalid_json")

        # Update_id dedup. Telegram retries on any non-2xx and sometimes on
        # slow 2xx, so duplicates show up regularly. Keep an in-process LRU
        # of recently-seen update_ids; persistence across restarts isn't
        # worth the complexity since the duplicate window is short.
        update_id = update.get("update_id")
        if update_id is not None:
            seen: OrderedDict = request.app.state.seen_updates
            cap: int = request.app.state.seen_updates_max
            if update_id in seen:
                seen.move_to_end(update_id)
                return JSONResponse({"ok": True})
            seen[update_id] = None
            while len(seen) > cap:
                seen.popitem(last=False)

        # Wrap routing so a bug in a handler can't make Telegram retry the
        # same update forever. We log and still return 2xx.
        try:
            await _handle_update(request.app, update)
        except Exception:  # noqa: BLE001
            logger.exception("webhook handler failed for update %s", update_id)
        # Telegram retries on non-2xx; always 200 once we've persisted
        # whatever the update produced.
        return Response(status_code=200)

    return app


# --- helpers shared by route handlers ---------------------------------------


async def _load_message(
    conn: sqlite3.Connection, message_id: int, installation_id: int
) -> sqlite3.Row:
    def _q() -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM messages WHERE id = ? AND installation_id = ?",
            (message_id, installation_id),
        ).fetchone()

    row = await run_in_thread(_q)
    if row is None:
        raise HTTPException(status_code=404, detail="message_not_found")
    return row


async def _seed_next_nudge_at(
    conn: sqlite3.Connection,
    config: RelayConfig,
    chat_id: int,
    body: CreateMessageRequest,
    now: datetime,
) -> str | None:
    """When this new message's first nudge falls due (ISO), or ``None``.

    ``None`` — i.e. the reaper's nudge pass never looks at this row — for:

    * a chat with **nudges off**, which is the default and is what keeps an
      unconfigured relay byte-for-byte as it is today (invariant 4);
    * a message that is not waiting on a human, decided by 19-03's
      :func:`render.awaits_human` and therefore excluding
      ``kind='notification'`` (invariant 7: an idle session must never produce
      a 09:00 ping, and there must not be a second definition of "waiting on a
      human");
    * a never-active window, where 19-01's ``advance_active`` returns None.

    Deliberately fail-open: a message must still send if the nudge lookup goes
    wrong, so any error here costs the row its ladder and nothing else.
    """
    try:
        if not awaits_human(body.model_dump(), "open"):
            return None
        recipient = await run_in_thread(load_recipient, conn, int(chat_id))
        if not recipient.nudge_enabled:
            return None
        due = next_nudge_due(
            now,
            recipient,
            config,
            nudge_count=0,
            windows=recipient_windows(recipient),
        )
        return due.isoformat() if due is not None else None
    except Exception:  # noqa: BLE001
        logger.exception(
            "failed to seed next_nudge_at for a new message in chat %s", chat_id
        )
        return None


def _terminal_response(row: sqlite3.Row) -> AnswerResponse | None:
    state = row["state"]
    if state == "open":
        return None
    if state == "answered":
        return AnswerResponse(
            state="answered",
            answer=json.loads(row["answer_json"]) if row["answer_json"] else None,
        )
    # expired or cancelled
    return AnswerResponse(state=state)


# ---- Telegram update routing ----------------------------------------------


async def _record_answer(
    conn: sqlite3.Connection,
    waiters: WaiterRegistry,
    message_id: int,
    answer: dict[str, Any],
    *,
    render_dirty: bool,
) -> bool:
    """Atomically transition an open message to ``answered`` and wake waiters.

    Returns True if the row moved to ``answered`` (we did the write), False if
    it was no longer open (already answered, expired, cancelled, or missing).

    This is brd §2.2's fifth terminal path and the only one that makes **no
    Telegram call at all** — it flips ``state`` in SQLite and returns. So it
    cannot remove the ``#unanswered`` tag itself; it records that the tag needs
    removing (``render_dirty``) in the very same ``UPDATE`` as the state flip,
    so the flag and the state cannot diverge, and the reaper's cleanup sweep
    finishes the job if the hook's PATCH never arrives (state.md 2026-08-16,
    invariant 10).

    ``render_dirty`` comes from the **caller**, which has the row and computes
    it with 19-03's ``awaits_human``: a row that carried no tag is never
    flagged, so the sweep never edits a message that needs no edit. Deliberately
    keyword-only and required — the value is not this function's to guess.

    An eager render-after-flip ``editMessageText`` here was considered and
    rejected: it costs an edit on the hottest path in the system for every
    ungrouped answer, against brd §2.7's ~1 msg/s per chat, and buys nothing the
    sweep does not (state.md 2026-08-16). Do not reintroduce it.
    """
    now_iso = _utcnow_iso()

    def _w() -> int:
        with conn:
            cur = conn.execute(
                "UPDATE messages SET state='answered',"
                " answer_json=?, answered_at=?, render_dirty=?,"
                " next_nudge_at=NULL"
                " WHERE id=? AND state='open'",
                (
                    json.dumps(answer),
                    now_iso,
                    1 if render_dirty else 0,
                    message_id,
                ),
            )
            return cur.rowcount

    updated = await run_in_thread(_w)
    if updated:
        waiters.notify(message_id)
        return True
    return False


async def _load_open_message_any(
    conn: sqlite3.Connection, message_id: int
) -> sqlite3.Row | None:
    """Load a message by id without an installation filter (webhook path)."""

    def _q() -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM messages WHERE id = ?", (message_id,)
        ).fetchone()

    return await run_in_thread(_q)


async def _load_message_by_tg_id(
    conn: sqlite3.Connection, chat_id: int, telegram_message_id: int
) -> sqlite3.Row | None:
    def _q() -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM messages"
            " WHERE telegram_chat_id = ? AND telegram_message_id = ?"
            " AND state = 'open'"
            " ORDER BY id DESC LIMIT 1",
            (chat_id, telegram_message_id),
        ).fetchone()

    return await run_in_thread(_q)


async def _load_message_by_nudge_tg_id(
    conn: sqlite3.Connection, chat_id: int, nudge_tg_message_id: int
) -> sqlite3.Row | None:
    """Look up a message by its nudge's Telegram id in a specific chat.

    No state filter — deliberate.  This function is called after the direct
    ``telegram_message_id`` lookup (``_load_message_by_tg_id``) has already
    missed, to resolve a reply aimed at a nudge back to the row the nudge
    belongs to (brd §5.6).  Including non-open rows lets the caller detect
    already-resolved targets and send an informative reply instead of silence.

    **Lookup precedence** in ``_handle_update``:
    1. ``telegram_message_id`` (open rows only) — ``_load_message_by_tg_id``
    2. ``nudge_tg_message_id`` (all states) — this function

    A collision between the two id spaces within the same chat is implausible
    in practice, but the ordering is explicit rather than incidental so it
    cannot become a surprise in the future.
    """

    def _q() -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM messages"
            " WHERE telegram_chat_id = ? AND nudge_tg_message_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (chat_id, nudge_tg_message_id),
        ).fetchone()

    return await run_in_thread(_q)


async def _load_open_in_chat(
    conn: sqlite3.Connection, chat_id: int
) -> list[sqlite3.Row]:
    """All open messages in a chat, most-recently-created first.

    Used to decide whether a loose (non-threaded) reply is unambiguous: we
    attribute it only when there is a single open target.
    """

    def _q() -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM messages WHERE telegram_chat_id = ? AND state = 'open'"
            " ORDER BY created_at DESC, id DESC",
            (chat_id,),
        ).fetchall()

    return await run_in_thread(_q)


def _distinct_open_targets(rows: list[sqlite3.Row]) -> int:
    """Count distinct answer targets among open messages.

    A question group (shared ``group_id``) is one logical target — its sibling
    child messages must not read as "multiple sessions". Every ungrouped message
    is its own target.
    """
    targets: set[object] = set()
    for row in rows:
        group_id, _ = _group_info(row)
        targets.add(group_id if group_id is not None else ("msg", row["id"]))
    return len(targets)


async def _load_installation_for_chat(
    conn: sqlite3.Connection, chat_id: int
) -> sqlite3.Row | None:
    def _q() -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM installations WHERE telegram_chat_id = ?"
            " AND revoked_at IS NULL"
            " ORDER BY id LIMIT 1",
            (chat_id,),
        ).fetchone()

    return await run_in_thread(_q)


def _payload_keyboard_for(row: sqlite3.Row) -> list[list[dict[str, Any]]] | None:
    return _payload_for(row).get("keyboard")


# ---- Re-answerable message groups -----------------------------------------
#
# AskUserQuestion fans a single prompt out into N sibling messages tagged with
# a shared ``group_id``. Each stays editable — taps update a provisional choice
# and re-render the keyboard with the selection marked — until every sibling
# has an answer, at which point the relay finalizes the whole group: strips all
# keyboards and bakes each choice into the message text. See models.py.

_SELECTED_PREFIX = "✅ "
_REPLY_PREFIX = "✍️ "
# Empty-checkbox prefix on unticked multi-select options (must match
# ``QUESTION_UNCHECKED_PREFIX`` in the hook's telegram_permission_router.py).
_UNSELECTED_PREFIX = "⬜ "

# Sentinel button ``value`` for the multi-select Submit button (must match
# ``QUESTION_SUBMIT_VALUE`` in the hook's telegram_permission_router.py).
_QUESTION_SUBMIT_VALUE = "qa_submit"
# ``via`` marker stored on a multi-select message while the user is still
# toggling options — it holds the running selection but does NOT count as an
# answer for group finalization until the Submit button flips it to
# ``button_multi``.
_MULTI_PENDING_VIA = "multi_pending"


def _esc(s: str) -> str:
    """Escape user/option text baked into a message body. All outbound text is
    sent with ``parse_mode=HTML`` (see telegram_backend.PARSE_MODE), so a stray
    ``<``/``>``/``&`` in an option label or a typed reply would otherwise make
    the edit fail as malformed HTML and leave the keyboard un-stripped."""
    return html.escape(s or "", quote=False)


def _is_multi_select(row: sqlite3.Row) -> bool:
    """True if this grouped question accumulates a multi-option selection."""
    return bool(_payload_for(row).get("multi_select"))


def _group_info(row: sqlite3.Row) -> tuple[str | None, int | None]:
    """Return ``(group_id, group_total)`` for a re-answerable message.

    ``(None, None)`` for ordinary one-shot messages (permissions, single
    notifications) so callers can branch on ``group_id is not None``.
    """
    payload = _payload_for(row)
    gid = payload.get("group_id")
    if gid is None:
        return None, None
    return gid, payload.get("group_total")


def _highlighted_keyboard(
    keyboard: list[list[dict[str, Any]]], selected_idx: int
) -> list[list[dict[str, Any]]]:
    """Copy ``keyboard`` (relay ``[[{label,value}]]`` shape), marking the button
    at flat row-major index ``selected_idx`` as selected.

    Always built from the pristine stored keyboard, so switching selections is
    idempotent (the marker never stacks) and ``selected_idx < 0`` yields an
    unmarked copy (used when the user answered with custom text instead).
    """
    out: list[list[dict[str, Any]]] = []
    flat = 0
    for kb_row in keyboard:
        out_row: list[dict[str, Any]] = []
        for btn in kb_row:
            label = btn.get("label", "")
            if flat == selected_idx:
                label = _SELECTED_PREFIX + label
            out_row.append({"label": label, "value": btn.get("value", "")})
            flat += 1
        out.append(out_row)
    return out


def _strip_checkbox(label: str) -> str:
    """Drop a leading ✅/⬜ checkbox prefix so re-rendering from a stored keyboard
    never stacks markers, regardless of which state the label came in as."""
    for prefix in (_SELECTED_PREFIX, _UNSELECTED_PREFIX):
        if label.startswith(prefix):
            return label[len(prefix):]
    return label


def _multi_highlighted_keyboard(
    keyboard: list[list[dict[str, Any]]], selected_idxs: set[int]
) -> list[list[dict[str, Any]]]:
    """Like ``_highlighted_keyboard`` but renders every option button with a
    checkbox — ✅ for selected, ⬜ for unselected — since a multi-select can have
    several options live at once. The Submit button (identified by its sentinel
    value) is left untouched. Always built from the pristine stored keyboard
    (stripping any existing checkbox first) so toggling stays idempotent."""
    out: list[list[dict[str, Any]]] = []
    flat = 0
    for kb_row in keyboard:
        out_row: list[dict[str, Any]] = []
        for btn in kb_row:
            label = btn.get("label", "")
            value = btn.get("value", "")
            if value != _QUESTION_SUBMIT_VALUE:
                base = _strip_checkbox(label)
                prefix = _SELECTED_PREFIX if flat in selected_idxs else _UNSELECTED_PREFIX
                label = prefix + base
            out_row.append({"label": label, "value": value})
            flat += 1
        out.append(out_row)
    return out


def _answer_line(answer: dict[str, Any]) -> str:
    """The trailing line baked into a message body when its group finalizes."""
    if answer.get("via") == "button_multi":
        labels = answer.get("labels") or []
        joined = ", ".join(_esc(str(label)) for label in labels)
        return f"\n\n{_SELECTED_PREFIX}{joined}"
    if answer.get("via") == "button":
        return f"\n\n{_SELECTED_PREFIX}{_esc(answer.get('label', ''))}"
    return f"\n\n{_REPLY_PREFIX}{_esc(answer.get('text', ''))}"


async def _record_provisional(
    conn: sqlite3.Connection, message_id: int, answer: dict[str, Any]
) -> bool:
    """Store/replace the provisional answer on an *open* message without making
    it terminal. Returns True iff the row was open and we wrote it."""
    now_iso = _utcnow_iso()

    def _w() -> int:
        with conn:
            cur = conn.execute(
                "UPDATE messages SET answer_json=?, answered_at=?"
                " WHERE id=? AND state='open'",
                (json.dumps(answer), now_iso, message_id),
            )
            return cur.rowcount

    return bool(await run_in_thread(_w))


async def _toggle_multi_selection(
    conn: sqlite3.Connection, message_id: int, option_idx: int
) -> list[int] | None:
    """Flip ``option_idx`` in/out of an *open* multi-select message's running
    selection (read-modify-write in one transaction so concurrent taps don't
    clobber each other). Returns the new sorted index list, or None if the
    message is no longer open."""
    now_iso = _utcnow_iso()

    def _w() -> list[int] | None:
        with conn:
            row = conn.execute(
                "SELECT answer_json, state FROM messages WHERE id=?", (message_id,)
            ).fetchone()
            if row is None or row["state"] != "open":
                return None
            current: set[int] = set()
            if row["answer_json"]:
                try:
                    data = json.loads(row["answer_json"])
                    if data.get("via") == _MULTI_PENDING_VIA:
                        current = {int(i) for i in data.get("option_idxs", [])}
                except (json.JSONDecodeError, ValueError, TypeError):
                    current = set()
            if option_idx in current:
                current.discard(option_idx)
            else:
                current.add(option_idx)
            new_idxs = sorted(current)
            conn.execute(
                "UPDATE messages SET answer_json=?, answered_at=?"
                " WHERE id=? AND state='open'",
                (
                    json.dumps({"via": _MULTI_PENDING_VIA, "option_idxs": new_idxs}),
                    now_iso,
                    message_id,
                ),
            )
            return new_idxs

    return await run_in_thread(_w)


def _member_answered(row: sqlite3.Row) -> bool:
    """True if a group member holds a *final* answer. A multi-select message
    carrying only a still-being-toggled selection (``via == multi_pending``)
    does not count — the group must wait for its Submit tap."""
    raw = row["answer_json"]
    if raw is None:
        return False
    try:
        return json.loads(raw).get("via") != _MULTI_PENDING_VIA
    except (json.JSONDecodeError, AttributeError):
        return True


async def _load_group_members(
    conn: sqlite3.Connection, chat_id: int, group_id: str
) -> list[sqlite3.Row]:
    def _q() -> list[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM messages WHERE telegram_chat_id = ?"
            " AND json_extract(payload_json, '$.group_id') = ?"
            " ORDER BY id",
            (chat_id, group_id),
        ).fetchall()

    return await run_in_thread(_q)


async def _finalize_group_if_complete(
    conn: sqlite3.Connection,
    backend: TelegramBackend,
    waiters: WaiterRegistry,
    chat_id: int,
    group_id: str,
    group_total: int | None,
) -> bool:
    """If every message in ``group_id`` now has a provisional answer, flip the
    whole group to ``answered``, strip all keyboards, bake each choice into the
    message body, and wake the long-pollers. Returns True iff *this* call did
    the finalizing (so the caller can pick the right callback toast)."""
    members = await _load_group_members(conn, chat_id, group_id)
    if not members:
        return False
    # Guard against finalizing before all siblings have even been created.
    if group_total is not None and len(members) < group_total:
        return False
    if any(not _member_answered(m) for m in members):
        return False

    ids = [int(m["id"]) for m in members]

    # Terminal for the whole group: the ladder is retired and ``render_dirty``
    # cleared in the same transaction as the flip, because every member is
    # re-rendered a few lines below.
    def _flip() -> int:
        placeholders = ",".join("?" * len(ids))
        with conn:
            cur = conn.execute(
                f"UPDATE messages SET state='answered',"
                f" next_nudge_at = NULL, render_dirty = 0"
                f" WHERE id IN ({placeholders}) AND state='open'",
                ids,
            )
            return cur.rowcount

    flipped = await run_in_thread(_flip)
    if not flipped:
        # A concurrent tap already claimed finalization for this group.
        return False

    # Re-load to capture the latest provisional answer for each member, then
    # render the terminal view: answer baked into the text, keyboard removed.
    members = await _load_group_members(conn, chat_id, group_id)
    for m in members:
        answer = json.loads(m["answer_json"]) if m["answer_json"] else {}
        # Members were re-loaded after the flip, so they render untagged.
        body = render_body_row(m) + _answer_line(answer)
        try:
            await backend.edit_message(
                chat_id=chat_id,
                telegram_message_id=int(m["telegram_message_id"]),
                text=body,
                keyboard=None,
            )
        except Exception:  # noqa: BLE001
            logger.exception("group finalize edit failed for message %s", m["id"])
        # One nudge spoke for the whole group (brd §5.3) and it hangs off
        # whichever member owns it, so every member is checked. Best-effort.
        await delete_nudge(
            conn,
            backend,
            message_id=int(m["id"]),
            chat_id=chat_id,
            nudge_tg_message_id=m["nudge_tg_message_id"],
        )
        waiters.notify(int(m["id"]))
    return True


# ---------------------------------------------------------------------------
# Preference commands: /tz, /hours, /nudge, /me (epic 19-02)
# ---------------------------------------------------------------------------

# Commands that are handled as preference settings rather than answers.
_PREF_COMMANDS = frozenset({"/tz", "/hours", "/nudge", "/me"})


def _is_preference_command(text: str) -> bool:
    """Return True if *text* starts with a known preference command."""
    if not text.startswith("/"):
        return False
    # Strip "/cmd@botname" form.
    cmd = text.split(None, 1)[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    return cmd in _PREF_COMMANDS


def _cmd_word(text: str) -> tuple[str, str]:
    """Return (lowercased command, rest-of-line stripped) from a message text."""
    parts = text.split(None, 1)
    cmd = parts[0].lower()
    if "@" in cmd:
        cmd = cmd.split("@", 1)[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    return cmd, arg


async def _handle_preference_command(
    app: FastAPI, msg: dict[str, Any], chat_id: int
) -> None:
    """Handle /tz, /hours, /nudge, and /me preference commands.

    Authorization: only the bound user of this chat may issue these.  An
    unbound chat is told to /bind first.  Anyone else is silently ignored so
    that a group chat with non-bound members does not spam error replies.

    This function always returns without writing any answer to the messages
    table — callers guarantee that by returning immediately after this call.
    """
    conn: sqlite3.Connection = app.state.db
    backend: TelegramBackend = app.state.backend
    cfg: RelayConfig = app.state.config

    text = (msg.get("text") or "").strip()
    sender_id = (msg.get("from") or {}).get("id")

    install = await _load_installation_for_chat(conn, chat_id)
    if install is None or install["bound_user_id"] is None:
        try:
            await backend.send_text(
                chat_id=chat_id,
                text=(
                    "This chat is not yet linked to an installation. "
                    "Send /bind <code> first."
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("preference command send to unbound chat failed")
        return

    # Only the bound user may configure preference commands.
    if sender_id is None or int(sender_id) != int(install["bound_user_id"]):
        return  # silently ignore non-bound sender

    now = _utcnow()
    now_iso = now.isoformat()
    cmd, arg = _cmd_word(text)

    try:
        if cmd == "/tz":
            await _pref_tz(conn, backend, chat_id, arg, now, now_iso)
        elif cmd == "/hours":
            await _pref_hours(conn, backend, chat_id, arg, now, now_iso)
        elif cmd == "/nudge":
            await _pref_nudge(conn, backend, cfg, chat_id, arg, now, now_iso)
        elif cmd == "/me":
            await _pref_me(conn, backend, cfg, chat_id, now)
    except Exception:  # noqa: BLE001
        logger.exception("preference command %r in chat %s failed", cmd, chat_id)


async def _pref_tz(
    conn: sqlite3.Connection,
    backend: TelegramBackend,
    chat_id: int,
    arg: str,
    now: datetime,
    now_iso: str,
) -> None:
    if not arg:
        await backend.send_text(
            chat_id=chat_id,
            text="Usage: /tz <IANA timezone>  (e.g. /tz Europe/Berlin)",
        )
        return

    tz_name = parse_tz(arg)
    if tz_name is None:
        matches = near_tz_matches(arg)
        if matches:
            suggestions = ", ".join(matches)
            reply = (
                f"Unknown timezone {arg!r}. Did you mean: {suggestions}?"
            )
        else:
            reply = (
                f"Unknown timezone {arg!r}. "
                "Use an IANA name like Europe/Berlin or America/New_York."
            )
        await backend.send_text(chat_id=chat_id, text=reply)
        return

    def _upsert() -> None:
        with conn:
            conn.execute(
                "INSERT INTO recipients"
                " (telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule, updated_at)"
                " VALUES (?, ?, NULL, 0, NULL, ?)"
                " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
                "   tz = excluded.tz,"
                "   updated_at = excluded.updated_at",
                (chat_id, tz_name, now_iso),
            )

    await run_in_thread(_upsert)

    recipient = await run_in_thread(load_recipient, conn, chat_id)
    windows = parse_windows(recipient.windows_json) if recipient.windows_json else None
    status = describe_active_status(now, tz_name, windows)
    await backend.send_text(
        chat_id=chat_id,
        text=f"Timezone set to {tz_name}. {status.capitalize()}.",
    )


async def _pref_hours(
    conn: sqlite3.Connection,
    backend: TelegramBackend,
    chat_id: int,
    arg: str,
    now: datetime,
    now_iso: str,
) -> None:
    if not arg:
        await backend.send_text(
            chat_id=chat_id,
            text=(
                "Usage: /hours <spec>  "
                "(e.g. /hours mon-fri 09:00-19:00, sat 11:00-15:00)\n"
                "Use /hours off to clear (always available)."
            ),
        )
        return

    if arg.lower() == "off":
        def _upsert_off() -> None:
            with conn:
                conn.execute(
                    "INSERT INTO recipients"
                    " (telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule, updated_at)"
                    " VALUES (?, NULL, NULL, 0, NULL, ?)"
                    " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
                    "   windows_json = NULL,"
                    "   updated_at = excluded.updated_at",
                    (chat_id, now_iso),
                )

        await run_in_thread(_upsert_off)
        recipient = await run_in_thread(load_recipient, conn, chat_id)
        status = describe_active_status(now, recipient.tz, None)
        await backend.send_text(
            chat_id=chat_id,
            text=f"Hours cleared — always available. {status.capitalize()}.",
        )
        return

    # Validate ALL clauses before writing anything (never partially apply).
    try:
        windows = parse_windows(arg)
    except ValueError as exc:
        await backend.send_text(
            chat_id=chat_id,
            text=(
                f"Bad window spec: {exc}.\n"
                "Example: /hours mon-fri 09:00-19:00, sat 11:00-15:00"
            ),
        )
        return

    if windows is None:
        await backend.send_text(
            chat_id=chat_id,
            text=(
                "Empty hours spec. Use /hours off to clear, "
                "or e.g. /hours mon-fri 09:00-19:00."
            ),
        )
        return

    canonical = format_windows(windows)

    def _upsert_hours() -> None:
        with conn:
            conn.execute(
                "INSERT INTO recipients"
                " (telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule, updated_at)"
                " VALUES (?, NULL, ?, 0, NULL, ?)"
                " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
                "   windows_json = excluded.windows_json,"
                "   updated_at = excluded.updated_at",
                (chat_id, canonical, now_iso),
            )

    await run_in_thread(_upsert_hours)
    recipient = await run_in_thread(load_recipient, conn, chat_id)
    status = describe_active_status(now, recipient.tz, windows)
    await backend.send_text(
        chat_id=chat_id,
        text=f"Hours: {canonical}. {status.capitalize()}.",
    )


async def _pref_nudge(
    conn: sqlite3.Connection,
    backend: TelegramBackend,
    cfg: RelayConfig,
    chat_id: int,
    arg: str,
    now: datetime,
    now_iso: str,
) -> None:
    if not arg:
        await backend.send_text(
            chat_id=chat_id,
            text=(
                "Usage: /nudge on  — enable nudges\n"
                "       /nudge off — disable nudges\n"
                "       /nudge 15m,45m,3h — enable with custom schedule"
            ),
        )
        return

    arg_lower = arg.lower()

    if arg_lower == "off":
        def _disable() -> None:
            with conn:
                conn.execute(
                    "INSERT INTO recipients"
                    " (telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule, updated_at)"
                    " VALUES (?, NULL, NULL, 0, NULL, ?)"
                    " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
                    "   nudge_enabled = 0,"
                    "   updated_at = excluded.updated_at",
                    (chat_id, now_iso),
                )
                # Clear next_nudge_at on open rows so the reaper won't act.
                conn.execute(
                    "UPDATE messages SET next_nudge_at = NULL"
                    " WHERE telegram_chat_id = ? AND state = 'open'",
                    (chat_id,),
                )

        await run_in_thread(_disable)
        recipient = await run_in_thread(load_recipient, conn, chat_id)
        _off_windows = (
            parse_windows(recipient.windows_json) if recipient.windows_json else None
        )
        status = describe_active_status(now, recipient.tz, _off_windows)
        await backend.send_text(
            chat_id=chat_id, text=f"Nudges off. {status.capitalize()}."
        )
        return

    # Determine the schedule.
    if arg_lower == "on":
        schedule_str: str | None = None  # will use per-chat or default
    else:
        # Treat the argument as an explicit schedule.
        try:
            schedule_tds = parse_nudge_schedule(arg, cfg.nudge_max)
        except ValueError as exc:
            await backend.send_text(
                chat_id=chat_id,
                text=(
                    f"Bad nudge schedule: {exc}.\n"
                    "Example: /nudge 15m,45m,3h"
                ),
            )
            return
        schedule_str = format_nudge_schedule(schedule_tds)

    # Load existing recipient to know whether nudges were off before (backfill
    # only applies when transitioning from off → on).
    recipient_before = await run_in_thread(load_recipient, conn, chat_id)
    was_off = not recipient_before.nudge_enabled

    # Determine effective schedule for the echo and for backfill.
    effective_schedule_str = (
        schedule_str
        or recipient_before.nudge_schedule
        or cfg.nudge_default_schedule
    )
    try:
        effective_schedule_tds = parse_nudge_schedule(
            effective_schedule_str, cfg.nudge_max
        )
    except ValueError:
        effective_schedule_tds = parse_nudge_schedule(
            cfg.nudge_default_schedule, cfg.nudge_max
        )
        effective_schedule_str = cfg.nudge_default_schedule

    def _enable(new_sched: str | None) -> None:
        with conn:
            conn.execute(
                "INSERT INTO recipients"
                " (telegram_chat_id, tz, windows_json, nudge_enabled, nudge_schedule, updated_at)"
                " VALUES (?, NULL, NULL, 1, ?, ?)"
                " ON CONFLICT(telegram_chat_id) DO UPDATE SET"
                "   nudge_enabled = 1,"
                "   nudge_schedule = COALESCE(?, nudge_schedule),"
                "   updated_at = excluded.updated_at",
                (chat_id, new_sched, now_iso, new_sched),
            )

    await run_in_thread(_enable, schedule_str)

    # Compute next nudge time (always, for the echo; also used for backfill).
    recipient_after = await run_in_thread(load_recipient, conn, chat_id)
    windows = (
        parse_windows(recipient_after.windows_json)
        if recipient_after.windows_json
        else None
    )
    first_interval = effective_schedule_tds[0]
    nudge_at = advance_active(now, first_interval, recipient_after.tz, windows)
    nudge_at_iso = nudge_at.isoformat() if nudge_at else None

    # Backfill open rows when transitioning off → on.
    if was_off:
        def _backfill() -> None:
            with conn:
                conn.execute(
                    "UPDATE messages SET next_nudge_at = ?"
                    " WHERE telegram_chat_id = ? AND state = 'open'"
                    " AND next_nudge_at IS NULL",
                    (nudge_at_iso, chat_id),
                )

        await run_in_thread(_backfill)

    canonical_sched = format_nudge_schedule(effective_schedule_tds)
    if nudge_at is not None:
        tz_zone = ZoneInfo(recipient_after.tz) if recipient_after.tz else ZoneInfo("UTC")
        now_local = now.astimezone(tz_zone)
        nudge_local = nudge_at.astimezone(tz_zone)
        today = now_local.date()
        tomorrow = today + timedelta(days=1)
        if nudge_local.date() == today:
            nudge_str = f"today {nudge_local.strftime('%H:%M')}"
        elif nudge_local.date() == tomorrow:
            nudge_str = f"tomorrow {nudge_local.strftime('%H:%M')}"
        else:
            nudge_str = nudge_local.strftime("%a %H:%M")
        echo_text = f"Nudges on. Schedule: {canonical_sched}. First nudge: {nudge_str}."
    else:
        echo_text = f"Nudges on. Schedule: {canonical_sched}."
    await backend.send_text(chat_id=chat_id, text=echo_text)


async def _pref_me(
    conn: sqlite3.Connection,
    backend: TelegramBackend,
    cfg: RelayConfig,
    chat_id: int,
    now: datetime,
) -> None:
    recipient = await run_in_thread(load_recipient, conn, chat_id)
    windows = (
        parse_windows(recipient.windows_json) if recipient.windows_json else None
    )

    tz_line = f"Timezone: {recipient.tz}" if recipient.tz else "Timezone: not set (UTC assumed)"
    hours_line = (
        f"Hours: {recipient.windows_json}"
        if recipient.windows_json
        else "Hours: always available (default)"
    )
    status = describe_active_status(now, recipient.tz, windows)

    if recipient.nudge_enabled:
        sched_str = recipient.nudge_schedule or cfg.nudge_default_schedule
        nudge_line = f"Nudges: on  |  Schedule: {sched_str}"
    else:
        nudge_line = "Nudges: off (default)"

    lines = [tz_line, hours_line, f"Status: {status}", nudge_line]
    await backend.send_text(chat_id=chat_id, text="\n".join(lines))


async def _handle_update(app: FastAPI, update: dict[str, Any]) -> None:
    """Route a single Telegram ``Update`` to the right handler.

    Three flavors matter to us:

    * ``callback_query`` — inline button taps. ``data`` contains the encoded
      message_id / option_idx. We answer the callback and record the answer.
    * ``message`` with ``reply_to_message_id`` matching a known open message
      → treat as a free-text answer.
    * ``message`` from the bound user without a reply pointer → fallback to
      "last awaiting open message in this chat", same as the legacy daemon.

    ``/bind`` commands are explicitly ignored in Phase 2; Phase 3 handles them.
    """
    conn: sqlite3.Connection = app.state.db
    backend: TelegramBackend = app.state.backend
    waiters: WaiterRegistry = app.state.waiters

    cbq = update.get("callback_query")
    if cbq is not None:
        await _handle_callback_query(conn, backend, waiters, cbq)
        return

    msg = update.get("message")
    if msg is None:
        return

    text = (msg.get("text") or "").strip()
    if text.lower().startswith("/bind"):
        await _handle_bind_command(app, msg)
        return

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    # Preference commands (/tz, /hours, /nudge, /me) are handled before any
    # answer path so they can never be mistakenly attributed as answers — even
    # if the message carries a reply_to_message_id.  They always return.
    if _is_preference_command(text):
        await _handle_preference_command(app, msg, int(chat_id))
        return

    # Free-text reply path: prefer explicit reply_to_message_id when present.
    # A threaded reply is the *only* unambiguous signal when several messages
    # are open in one chat (e.g. multiple idle sessions), so we honor it and
    # stop — even if it matches no open row. Falling through to the recency
    # heuristic here would silently mis-thread a reply the user aimed at a
    # since-resolved message onto whatever most recently went idle.
    reply_to = msg.get("reply_to_message_id") or (
        (msg.get("reply_to_message") or {}).get("message_id")
    )
    if reply_to is not None:
        # Precedence 1: direct telegram_message_id lookup (open rows only).
        row = await _load_message_by_tg_id(conn, int(chat_id), int(reply_to))
        if row is not None:
            await _apply_text_answer(
                conn, backend, waiters, row, msg.get("text", ""), "reply"
            )
            return
        # Precedence 2: nudge id lookup — a reply aimed at the nudge message
        # resolves to the row the nudge belongs to (brd §5.6).  No state
        # filter: we need to detect already-resolved targets so they receive
        # an informative reply rather than silence.
        nudge_row = await _load_message_by_nudge_tg_id(
            conn, int(chat_id), int(reply_to)
        )
        if nudge_row is not None:
            if nudge_row["state"] == "open":
                await _apply_text_answer(
                    conn,
                    backend,
                    waiters,
                    nudge_row,
                    msg.get("text", ""),
                    "nudge_reply",
                )
            else:
                # Target already resolved — do NOT fall through to the recency
                # heuristic; prefer a short acknowledgement over silence so
                # the operator learns the reply was received (brd §5.6).
                try:
                    await backend.send_text(
                        chat_id=int(chat_id),
                        text="That one's already been handled.",
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("already-handled nudge reply hint send failed")
        # Whether we resolved via nudge or found nothing: do NOT fall through
        # to the recency heuristic — that path caused historical mis-routing.
        return

    # Ignore other slash-commands so /start, /help, etc. don't accidentally
    # become answers.
    if text.startswith("/"):
        return

    # Loose (non-threaded) reply from the bound user. We only auto-attribute
    # when there is exactly ONE open target in the chat — otherwise the guess is
    # ambiguous (this is what mis-routed replies across concurrently idle
    # sessions). A question group counts as a single target so grouped
    # AskUserQuestion plain replies still work. When ambiguous we nudge the user
    # to use Telegram's Reply, which routes unambiguously.
    sender_id = (msg.get("from") or {}).get("id")
    install = await _load_installation_for_chat(conn, int(chat_id))
    if install is None or install["bound_user_id"] is None:
        return
    if sender_id is None or int(sender_id) != int(install["bound_user_id"]):
        return

    open_rows = await _load_open_in_chat(conn, int(chat_id))
    if not open_rows:
        return
    if _distinct_open_targets(open_rows) > 1:
        logger.info(
            "loose reply in chat %s ignored: %d open messages (ambiguous)",
            chat_id,
            len(open_rows),
        )
        try:
            await backend.send_text(
                chat_id=int(chat_id),
                text=(
                    "Multiple sessions are waiting — I can't tell which this is"
                    " for. Long-press the session's message and tap Reply."
                ),
            )
        except Exception:  # noqa: BLE001
            logger.exception("ambiguous-reply hint send failed")
        return

    # Exactly one open target: the most-recent open row is unambiguous.
    await _apply_text_answer(
        conn, backend, waiters, open_rows[0], msg.get("text", ""), "fallback"
    )


async def _handle_bind_command(app: FastAPI, msg: dict[str, Any]) -> None:
    """Handle a ``/bind <code>`` message sent by a Telegram user.

    Parses the code, looks it up, and either records the binding or sends an
    appropriate error reply in the originating chat.
    """
    conn: sqlite3.Connection = app.state.db
    backend: TelegramBackend = app.state.backend

    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    if chat_id is None:
        return

    sender = msg.get("from") or {}
    telegram_user_id = sender.get("id")

    text = (msg.get("text") or "").strip()
    # Strip the "/bind" prefix (case-insensitive) to extract the code token.
    # Accept "/bind BIND-XXXX-XXXX" or "/bind@botname BIND-XXXX-XXXX".
    parts = text.split(None, 1)
    if len(parts) < 2:
        await backend.send_text(chat_id=int(chat_id), text="Usage: /bind <code>")
        return

    raw_code = parts[1].strip()
    code = normalise_code(raw_code)
    if code is None:
        await backend.send_text(chat_id=int(chat_id), text="Unknown bind code.")
        return

    def _lookup_code() -> sqlite3.Row | None:
        return conn.execute(
            "SELECT bc.*, i.label AS installation_label,"
            " i.telegram_chat_id AS prev_chat_id"
            " FROM binding_codes bc"
            " JOIN installations i ON i.id = bc.installation_id"
            " WHERE bc.code = ?",
            (code,),
        ).fetchone()

    row = await run_in_thread(_lookup_code)
    if row is None:
        await backend.send_text(chat_id=int(chat_id), text="Unknown bind code.")
        return

    if row["consumed_at"] is not None:
        await backend.send_text(
            chat_id=int(chat_id), text="This bind code was already used."
        )
        return

    # Check expiry.
    now = _utcnow()
    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        expires_at = now

    if now > expires_at:
        await backend.send_text(
            chat_id=int(chat_id), text="This bind code has expired."
        )
        return

    # Valid pending code — consume it and update the installation.
    installation_id = int(row["installation_id"])
    prev_chat_id = row["prev_chat_id"]
    label = row["installation_label"]
    now_iso = now.isoformat()

    def _consume() -> bool:
        """Atomically consume the code and update the installation.

        Returns True if this caller won the consume race, False if a concurrent
        webhook delivery already consumed the code (rowcount == 0).

        Cross-thread atomicity of the SELECT/UPDATE/UPDATE sequence is
        guaranteed by ``run_in_thread`` serialising access to the shared
        sqlite connection. The ``consumed_at IS NULL`` guard on the first
        UPDATE handles the consume race: the second caller observes
        ``rowcount == 0`` and bails out without sending a reply.
        """
        with conn:
            cur = conn.execute(
                "UPDATE binding_codes"
                " SET consumed_at = ?, bound_chat_id = ?, bound_user_id = ?"
                " WHERE code = ? AND consumed_at IS NULL",
                (now_iso, int(chat_id), telegram_user_id, code),
            )
            if cur.rowcount == 0:
                # Already consumed by a concurrent delivery — abort silently;
                # the winning delivery already sent a confirmation reply.
                return False
            conn.execute(
                "UPDATE installations"
                " SET telegram_chat_id = ?, bound_user_id = ?"
                " WHERE id = ?",
                (int(chat_id), telegram_user_id, installation_id),
            )
            return True

    consumed = await run_in_thread(_consume)
    if not consumed:
        # Another concurrent /bind webhook for the same code already handled it.
        return

    if prev_chat_id is not None and int(prev_chat_id) != int(chat_id):
        reply = (
            f"Bound. Notifications for installation {label} will go to this chat."
            " (previous binding overwritten)"
        )
    else:
        reply = f"Bound. Notifications for installation {label} will go to this chat."

    await backend.send_text(chat_id=int(chat_id), text=reply)


async def _safe_answer_cb(
    backend: TelegramBackend, cb_id: str, text: str | None = None
) -> None:
    if not cb_id:
        return
    try:
        await backend.answer_callback_query(callback_query_id=cb_id, text=text)
    except Exception:  # noqa: BLE001
        logger.exception("answer_callback_query failed")


async def _handle_multi_select_button(
    conn: sqlite3.Connection,
    backend: TelegramBackend,
    waiters: WaiterRegistry,
    row: sqlite3.Row,
    option_idx: int,
    chosen: dict[str, Any],
    cb_id: str,
    group_id: str,
    group_total: int | None,
) -> None:
    """A tap on a multi-select grouped message. Option taps toggle the running
    selection and re-render with every chosen option marked; the dedicated
    Submit button bakes the selection into the message's final answer and may
    finalize the whole group."""
    chat_id = int(row["telegram_chat_id"])
    keyboard = _payload_keyboard_for(row) or []
    flat = [btn for r in keyboard for btn in r]

    if chosen.get("value") == _QUESTION_SUBMIT_VALUE:
        selected = await _current_multi_selection(conn, int(row["id"]))
        if selected is None:
            await _safe_answer_cb(backend, cb_id, "No longer waiting for an answer")
            return
        if not selected:
            await _safe_answer_cb(backend, cb_id, "Tap at least one option first")
            return
        labels = [
            _strip_checkbox(flat[i].get("label", ""))
            for i in selected
            if 0 <= i < len(flat)
        ]
        answer = {"via": "button_multi", "option_idxs": selected, "labels": labels}
        wrote = await _record_provisional(conn, int(row["id"]), answer)
        if not wrote:
            await _safe_answer_cb(backend, cb_id, "Already submitted")
            return
        if await _finalize_group_if_complete(
            conn, backend, waiters, chat_id, group_id, group_total
        ):
            await _safe_answer_cb(backend, cb_id, "Submitted ✓")
        else:
            await _safe_answer_cb(
                backend, cb_id, "Saved ✓ — waiting on the other questions"
            )
        return

    # An option tap: toggle it in the running selection.
    new_selected = await _toggle_multi_selection(conn, int(row["id"]), option_idx)
    if new_selected is None:
        fresh = await _load_open_message_any(conn, int(row["id"]))
        state = fresh["state"] if fresh is not None else None
        ack = {
            "answered": "Already submitted",
            "expired": "⏱ Expired — no longer waiting",
            "cancelled": "Cancelled — handled in the terminal",
        }.get(state, "No longer waiting for an answer")
        await _safe_answer_cb(backend, cb_id, ack)
        return

    highlighted = _multi_highlighted_keyboard(keyboard, set(new_selected))
    try:
        await backend.edit_message(
            chat_id=chat_id,
            telegram_message_id=int(row["telegram_message_id"]),
            # Still open and still awaiting the Submit tap — keeps the tag.
            text=render_body_row(row),
            keyboard=highlighted,
            relay_message_id=int(row["id"]),
        )
    except Exception:  # noqa: BLE001
        logger.exception("multi-select toggle edit failed for message %s", row["id"])
    on = option_idx in set(new_selected)
    await _safe_answer_cb(
        backend,
        cb_id,
        f"{'Selected' if on else 'Unselected'}: {chosen.get('label', '')}",
    )


async def _current_multi_selection(
    conn: sqlite3.Connection, message_id: int
) -> list[int] | None:
    """Read the running multi-select option indices for an *open* message.
    Returns ``[]`` when nothing is selected yet, or None if no longer open."""
    def _q() -> list[int] | None:
        row = conn.execute(
            "SELECT answer_json, state FROM messages WHERE id=?", (message_id,)
        ).fetchone()
        if row is None or row["state"] != "open":
            return None
        if not row["answer_json"]:
            return []
        try:
            data = json.loads(row["answer_json"])
        except json.JSONDecodeError:
            return []
        if data.get("via") != _MULTI_PENDING_VIA:
            return []
        return [int(i) for i in data.get("option_idxs", [])]

    return await run_in_thread(_q)


async def _handle_grouped_button(
    conn: sqlite3.Connection,
    backend: TelegramBackend,
    waiters: WaiterRegistry,
    row: sqlite3.Row,
    option_idx: int,
    chosen: dict[str, Any],
    cb_id: str,
    group_id: str,
    group_total: int | None,
) -> None:
    """A tap on a re-answerable grouped message: record the provisional choice,
    re-render with it highlighted, and finalize the group once all are in."""
    if _is_multi_select(row):
        await _handle_multi_select_button(
            conn, backend, waiters, row, option_idx, chosen, cb_id,
            group_id, group_total,
        )
        return
    chat_id = int(row["telegram_chat_id"])
    answer = {
        "option_idx": option_idx,
        "label": chosen.get("label"),
        "value": chosen.get("value"),
        "via": "button",
    }
    wrote = await _record_provisional(conn, int(row["id"]), answer)
    if not wrote:
        # No longer open — the group finalized, expired, or was cancelled.
        fresh = await _load_open_message_any(conn, int(row["id"]))
        state = fresh["state"] if fresh is not None else None
        ack = {
            "answered": "Already submitted",
            "expired": "⏱ Expired — no longer waiting",
            "cancelled": "Cancelled — handled in the terminal",
        }.get(state, "No longer waiting for an answer")
        await _safe_answer_cb(backend, cb_id, ack)
        return

    if await _finalize_group_if_complete(
        conn, backend, waiters, chat_id, group_id, group_total
    ):
        await _safe_answer_cb(backend, cb_id, "Submitted ✓")
        return

    # Group still incomplete — just highlight this message's selection. Render
    # from the pristine stored keyboard so re-selecting doesn't stack markers,
    # threading the relay message id so re-encoded taps still route back here.
    keyboard = _payload_keyboard_for(row) or []
    highlighted = _highlighted_keyboard(keyboard, option_idx)
    try:
        await backend.edit_message(
            chat_id=chat_id,
            telegram_message_id=int(row["telegram_message_id"]),
            # The group is still incomplete, so this member stays tagged.
            text=render_body_row(row),
            keyboard=highlighted,
            relay_message_id=int(row["id"]),
        )
    except Exception:  # noqa: BLE001
        logger.exception("grouped highlight edit failed for message %s", row["id"])
    await _safe_answer_cb(
        backend, cb_id, f"Selected: {chosen.get('label', '')}"
    )


async def _handle_grouped_reply(
    conn: sqlite3.Connection,
    backend: TelegramBackend,
    waiters: WaiterRegistry,
    row: sqlite3.Row,
    text: str,
) -> None:
    """A custom text answer to a re-answerable grouped message: record it,
    reflect it in the body (keyboard kept, un-highlighted), and finalize the
    group once all siblings are answered."""
    chat_id = int(row["telegram_chat_id"])
    group_id, group_total = _group_info(row)
    if group_id is None:
        return
    wrote = await _record_provisional(
        conn, int(row["id"]), {"text": text, "via": "reply"}
    )
    if not wrote:
        return
    if await _finalize_group_if_complete(
        conn, backend, waiters, chat_id, group_id, group_total
    ):
        return
    # Still collecting — show the typed answer and leave the buttons available
    # (un-highlighted) so the user can still switch to a preset option.
    body = render_body_row(row) + f"\n\n{_REPLY_PREFIX}{_esc(text)}"
    try:
        await backend.edit_message(
            chat_id=chat_id,
            telegram_message_id=int(row["telegram_message_id"]),
            text=body,
            keyboard=_payload_keyboard_for(row),
            relay_message_id=int(row["id"]),
        )
    except Exception:  # noqa: BLE001
        logger.exception("grouped reply reflect failed for message %s", row["id"])


async def _apply_text_answer(
    conn: sqlite3.Connection,
    backend: TelegramBackend,
    waiters: WaiterRegistry,
    row: sqlite3.Row,
    text: str,
    via: str,
) -> None:
    """Route a free-text answer: provisional+grouped flow for re-answerable
    messages, the legacy terminal ``_record_answer`` otherwise."""
    group_id, _ = _group_info(row)
    if group_id is not None:
        await _handle_grouped_reply(conn, backend, waiters, row, text)
    else:
        # The caller owns the tag question (invariant 7): this row is the one
        # that will need re-rendering iff it is carrying a tag right now.
        await _record_answer(
            conn,
            waiters,
            int(row["id"]),
            {"text": text, "via": via},
            render_dirty=awaits_human(_payload_for(row), row["state"]),
        )


async def _handle_callback_query(
    conn: sqlite3.Connection,
    backend: TelegramBackend,
    waiters: WaiterRegistry,
    cbq: dict[str, Any],
) -> None:
    data = cbq.get("data") or ""
    cb_id = cbq.get("id") or ""
    parsed = decode_callback_data(data)
    if parsed is None:
        # Foreign or malformed callback data; ack so Telegram stops the
        # client-side spinner but record nothing.
        if cb_id:
            try:
                await backend.answer_callback_query(callback_query_id=cb_id)
            except Exception:  # noqa: BLE001
                logger.exception("answer_callback_query failed")
        return

    row = await _load_open_message_any(conn, parsed.message_id)
    if row is None:
        if cb_id:
            try:
                await backend.answer_callback_query(
                    callback_query_id=cb_id, text="Message no longer active"
                )
            except Exception:  # noqa: BLE001
                logger.exception("answer_callback_query failed")
        return

    # Trust boundary: the callback message must originate from the chat that
    # currently owns this message. Anyone in that chat may tap (per spec).
    msg_obj = cbq.get("message") or {}
    msg_chat = (msg_obj.get("chat") or {}).get("id")
    if msg_chat is not None and int(msg_chat) != int(row["telegram_chat_id"]):
        logger.warning(
            "callback chat mismatch: cbq chat=%s message row chat=%s",
            msg_chat,
            row["telegram_chat_id"],
        )
        if cb_id:
            try:
                await backend.answer_callback_query(callback_query_id=cb_id)
            except Exception:  # noqa: BLE001
                logger.exception("answer_callback_query failed")
        return

    keyboard = _payload_keyboard_for(row) or []
    flat = [btn for r in keyboard for btn in r]
    if not (0 <= parsed.option_idx < len(flat)):
        if cb_id:
            try:
                await backend.answer_callback_query(
                    callback_query_id=cb_id, text="Invalid option"
                )
            except Exception:  # noqa: BLE001
                logger.exception("answer_callback_query failed")
        return

    chosen = flat[parsed.option_idx]

    # Re-answerable grouped messages (AskUserQuestion) take the provisional
    # path: highlight the selection, keep buttons live, finalize as a group.
    group_id, group_total = _group_info(row)
    if group_id is not None:
        await _handle_grouped_button(
            conn,
            backend,
            waiters,
            row,
            parsed.option_idx,
            chosen,
            cb_id,
            group_id,
            group_total,
        )
        return

    answer = {
        "option_idx": parsed.option_idx,
        "label": chosen.get("label"),
        "value": chosen.get("value"),
        "via": "button",
    }

    # Same as the plain-text path: the row is here, so the tag question is
    # answered here (invariant 7). ``row`` may already be terminal — then
    # ``awaits_human`` is False and ``_record_answer`` writes nothing anyway.
    wrote = await _record_answer(
        conn,
        waiters,
        parsed.message_id,
        answer,
        render_dirty=awaits_human(_payload_for(row), row["state"]),
    )

    if cb_id:
        try:
            if wrote:
                ack = f"Answered: {chosen.get('label', '')}"
            else:
                # The row was no longer ``open`` — distinguish *why* so a late
                # tap doesn't read as a duplicate ("Already answered") when it
                # actually expired or was cancelled.
                fresh = await _load_open_message_any(conn, parsed.message_id)
                state = fresh["state"] if fresh is not None else None
                ack = {
                    "answered": "Already answered",
                    "expired": "⏱ Expired — no longer waiting",
                    "cancelled": "Cancelled — handled in the terminal",
                }.get(state, "No longer waiting for an answer")
            await backend.answer_callback_query(
                callback_query_id=cb_id, text=ack
            )
        except Exception:  # noqa: BLE001
            logger.exception("answer_callback_query failed")
