"""FastAPI application for the relay server (Phase 1)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from .db import connect, init_schema, run_in_thread
from .models import (
    AnswerResponse,
    CreateMessageRequest,
    CreateMessageResponse,
    InstallationMeResponse,
    PatchMessageRequest,
)
from .telegram_backend import FakeTelegramBackend, TelegramBackend
from .tokens import hash_token
from .waiters import WaiterRegistry


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
) -> FastAPI:
    db_path = db_path or os.environ.get("RELAY_DB_PATH", "relay.db")
    backend = backend or FakeTelegramBackend()
    enable_internal = (
        os.environ.get("RELAY_ENABLE_INTERNAL_ENDPOINTS", "0").lower()
        in {"1", "true", "yes", "on"}
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN001
        conn = connect(db_path)
        init_schema(conn)
        app.state.db = conn
        app.state.backend = backend
        app.state.waiters = WaiterRegistry()
        # Per-installation async lock guarding the idempotent create path so
        # two concurrent POSTs with the same Idempotency-Key in this process
        # serialize through the "claim pending row -> backend call -> store
        # response" sequence. Cross-process serialization is provided by the
        # PRIMARY KEY on idempotency_keys.
        app.state.idem_locks = defaultdict(asyncio.Lock)
        try:
            yield
        finally:
            conn.close()

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
        conn: sqlite3.Connection = request.app.state.db
        backend: TelegramBackend = request.app.state.backend
        row = await _load_message(conn, message_id, installation["id"])
        keyboard_dump = (
            [[btn.model_dump() for btn in r] for r in body.keyboard]
            if body.keyboard
            else None
        )
        await backend.edit_message(
            chat_id=row["telegram_chat_id"],
            telegram_message_id=row["telegram_message_id"],
            text=body.text,
            keyboard=keyboard_dump,
        )
        return {}

    @app.delete("/v1/messages/{message_id}", status_code=204)
    async def delete_message(
        message_id: int,
        request: Request,
        installation=Depends(require_installation),
    ) -> Response:
        conn: sqlite3.Connection = request.app.state.db
        backend: TelegramBackend = request.app.state.backend
        row = await _load_message(conn, message_id, installation["id"])
        await backend.delete_message(
            chat_id=row["telegram_chat_id"],
            telegram_message_id=row["telegram_message_id"],
        )
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
        row = await _load_message(conn, message_id, installation["id"])

        def _cancel() -> None:
            with conn:
                conn.execute(
                    "UPDATE messages SET state='cancelled' WHERE id=? AND state='open'",
                    (message_id,),
                )

        await run_in_thread(_cancel)
        # Strip the inline keyboard via the dedicated Bot-API-shaped method.
        await backend.edit_reply_markup(
            chat_id=row["telegram_chat_id"],
            telegram_message_id=row["telegram_message_id"],
            keyboard=None,
        )
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
            return Response(status_code=204)

        # Re-load and respond.
        row = await _load_message(conn, message_id, installation["id"])
        terminal = _terminal_response(row)
        if terminal is None:
            # Spurious wake or still open; tell client to retry.
            return Response(status_code=204)
        return JSONResponse(terminal.model_dump(exclude_none=True))

    # ---- Internal test/utility endpoint -----------------------------------
    # Phase 2's webhook will call into the same code path. Exposing it here
    # (auth'd) keeps Phase 1 end-to-end testable without inventing a webhook.
    # GATED: only registered when RELAY_ENABLE_INTERNAL_ENDPOINTS is truthy,
    # because an installation token alone must not be able to fabricate
    # answers in production.

    if enable_internal:

        @app.post("/v1/_internal/record_answer/{message_id}")
        async def record_answer(
            message_id: int,
            request: Request,
            installation=Depends(require_installation),
        ) -> dict[str, Any]:
            conn: sqlite3.Connection = request.app.state.db
            waiters: WaiterRegistry = request.app.state.waiters
            body = await request.json()
            row = await _load_message(conn, message_id, installation["id"])
            if row["state"] != "open":
                raise HTTPException(status_code=409, detail="not_open")

            def _record() -> None:
                with conn:
                    conn.execute(
                        "UPDATE messages SET state='answered',"
                        " answer_json=?, answered_at=? WHERE id=?",
                        (json.dumps(body), _utcnow_iso(), message_id),
                    )

            await run_in_thread(_record)
            waiters.notify(message_id)
            return {}

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
