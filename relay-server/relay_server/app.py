"""FastAPI application for the relay server (Phase 1)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
from collections import OrderedDict, defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .binding_codes import generate_code, normalise_code
from .callback_data import decode as decode_callback_data
from .config import RelayConfig, load_config
from .db import connect, init_schema, run_in_thread
from .reaper import reaper_loop
from .models import (
    AnswerResponse,
    BindingRequestResponse,
    BindingStatusResponse,
    CreateMessageRequest,
    CreateMessageResponse,
    InstallationMeResponse,
    PatchMessageRequest,
)
from .telegram_backend import (
    FakeTelegramBackend,
    HttpTelegramBackend,
    TelegramBackend,
    TelegramForbidden,
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
    async def me(installation=Depends(require_installation)) -> InstallationMeResponse:
        return InstallationMeResponse(
            id=installation["id"],
            label=installation["label"],
            chat_bound=installation["telegram_chat_id"] is not None,
            last_seen_at=installation["last_seen_at"],
        )

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
                    text=body.text,
                    keyboard=keyboard_dump,
                    reply_required=body.reply_required,
                    message_id=message_id,
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

            def _update_tg_id() -> None:
                with conn:
                    conn.execute(
                        "UPDATE messages SET telegram_message_id = ? WHERE id = ?",
                        (tg_message_id, message_id),
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
        try:
            await backend.edit_message(
                chat_id=row["telegram_chat_id"],
                telegram_message_id=row["telegram_message_id"],
                text=body.text,
                keyboard=None,
            )
        except TelegramForbidden:
            await _unbind_installation(conn, installation_id)
            raise HTTPException(status_code=409, detail="not_bound")
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

        def _cancel() -> None:
            with conn:
                conn.execute(
                    "UPDATE messages SET state='cancelled' WHERE id=? AND state='open'",
                    (message_id,),
                )

        await run_in_thread(_cancel)
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
) -> bool:
    """Atomically transition an open message to ``answered`` and wake waiters.

    Returns True if the row moved to ``answered`` (we did the write), False if
    it was no longer open (already answered, expired, cancelled, or missing).
    """
    now_iso = _utcnow_iso()

    def _w() -> int:
        with conn:
            cur = conn.execute(
                "UPDATE messages SET state='answered',"
                " answer_json=?, answered_at=? WHERE id=? AND state='open'",
                (json.dumps(answer), now_iso, message_id),
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


async def _load_last_open_in_chat(
    conn: sqlite3.Connection, chat_id: int
) -> sqlite3.Row | None:
    """Fallback heuristic: most recently created open message in this chat.

    Mirrors the per-chat "last awaiting" tracking the legacy daemon used so a
    bound user can type a plain reply without quoting the prompt.
    """

    def _q() -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM messages WHERE telegram_chat_id = ? AND state = 'open'"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()

    return await run_in_thread(_q)


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
    try:
        payload = json.loads(row["payload_json"])
    except Exception:  # noqa: BLE001
        return None
    return payload.get("keyboard")


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

    # Free-text reply path: prefer explicit reply_to_message_id when present.
    reply_to = msg.get("reply_to_message_id") or (
        (msg.get("reply_to_message") or {}).get("message_id")
    )
    if reply_to is not None:
        row = await _load_message_by_tg_id(conn, int(chat_id), int(reply_to))
        if row is not None:
            await _record_answer(
                conn,
                waiters,
                row["id"],
                {"text": msg.get("text", ""), "via": "reply"},
            )
            return

    # Ignore other slash-commands so /start, /help, etc. don't accidentally
    # become answers.
    if text.startswith("/"):
        return

    # Fallback: attribute to the most recently created open message in this
    # chat *iff* the sender is the user that bound the chat.
    # This path also compensates for the send-time trade-off where a message
    # with both `keyboard` and `reply_required` ends up without a force_reply
    # prompt — bound users can still answer by typing.
    sender_id = (msg.get("from") or {}).get("id")
    install = await _load_installation_for_chat(conn, int(chat_id))
    if install is None or install["bound_user_id"] is None:
        return
    if sender_id is None or int(sender_id) != int(install["bound_user_id"]):
        return

    row = await _load_last_open_in_chat(conn, int(chat_id))
    if row is None:
        return
    await _record_answer(
        conn,
        waiters,
        row["id"],
        {"text": msg.get("text", ""), "via": "fallback"},
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
    answer = {
        "option_idx": parsed.option_idx,
        "label": chosen.get("label"),
        "value": chosen.get("value"),
        "via": "button",
    }

    wrote = await _record_answer(conn, waiters, parsed.message_id, answer)

    if cb_id:
        try:
            ack = f"Answered: {chosen.get('label', '')}" if wrote else "Already answered"
            await backend.answer_callback_query(
                callback_query_id=cb_id, text=ack
            )
        except Exception:  # noqa: BLE001
            logger.exception("answer_callback_query failed")
