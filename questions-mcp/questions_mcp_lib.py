#!/usr/bin/env python3
"""Logic behind the ``questions`` MCP server (epic 23, task 23-04).

Everything that does not need the ``mcp`` package lives here so the behaviour
can be unit-tested with the repo's plain-``python3`` suite while ``server.py``
stays a thin tool-registration shell (it is a ``uv`` single-file script and
its dependency is only installed inside uv's environment).

Two tools:

* ``ask`` — write a question entry into the workspace queue file, send it to
  Telegram as a ``kind=question`` message (``never_expires=True``), record the
  relay message id in the shared routing index, stamp ``**Dispatched:**`` and
  return the allocated id.  The file write precedes the relay call (invariant 1).

* ``notify`` — send an async statement.  With ``ack=True`` it rides
  ``kind=question`` with a single ``[ Acknowledge ]`` button so the nudge
  ladder has something to chase (invariant 10 / brd D7).  With ``ack=False``
  it is fire-and-forget and records nothing.

Identity and config are resolved **per call**, not at startup (architecture
§4): the server is long-lived and may serve sessions in different workspaces.
``CLAUDE_PROJECT_DIR``/``PWD`` is read per invocation, and the workspace's
``roles.toml`` is loaded through the existing loader with a short TTL cache
keyed by workspace-dir path.

Ordering guarantee (invariant 1):
  1. resolve + refuse (no write yet)
  2. ``create_entry`` — append + fsync (durable write)
  3. ``send_message``  — relay call (best-effort)
  4. ``add_message_entry`` — index write
  5. ``mark_dispatched`` — stamp entry
  6. return

A crash anywhere in steps 3-5 leaves a written entry and returns
``dispatched: false`` with the allocated id.  The reverse (Telegram message
with no queue entry) cannot happen.
"""

from __future__ import annotations

import html
import importlib.util
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Hook imports
#
# The server runs from the checkout (the installer registers ``server.py`` in
# place, like context-mcp and permissions-mcp), so importing hook modules from
# the repo keeps them in lockstep with the repo version.
#
# ``CLAUDE_HOOKS_REPO`` is exported by the installer's MCP registration; it
# wins when present so a relocated checkout still resolves, with the path
# relative to this file as the fallback.
# ---------------------------------------------------------------------------


def _resolve_hooks_dir() -> Path:
    env_repo = os.environ.get("CLAUDE_HOOKS_REPO")
    if env_repo:
        candidate = Path(env_repo).expanduser() / ".claude" / "hooks"
        if candidate.is_dir():
            return candidate
    return Path(__file__).resolve().parent.parent / ".claude" / "hooks"


HOOKS_DIR = _resolve_hooks_dir()
if str(HOOKS_DIR) not in sys.path:
    sys.path.insert(0, str(HOOKS_DIR))

import questions_store as qs  # noqa: E402
import questions_listen_lib as qll  # noqa: E402
import roles_config  # noqa: E402


# ---------------------------------------------------------------------------
# Relay client bootstrap — same pattern as telegram_permission_router.py
# ---------------------------------------------------------------------------


def _ensure_relay_server_on_path() -> None:
    try:
        if importlib.util.find_spec("relay_server") is not None:
            return
    except (ImportError, ValueError):
        pass

    candidates: list[Path] = []
    env_path = os.environ.get("CLAUDE_RELAY_SERVER_PATH")
    if env_path:
        candidates.append(Path(env_path).expanduser())
    here = Path(__file__).resolve()
    candidates.extend(parent / "relay-server" for parent in here.parents)

    for cand in candidates:
        if (cand / "relay_server" / "__init__.py").is_file():
            if str(cand) not in sys.path:
                sys.path.insert(0, str(cand))
            return


_ensure_relay_server_on_path()

try:
    from relay_server.client import (  # type: ignore[import-not-found]
        RelayClient,
        NotBoundError,
        RelayError,
    )
except Exception:  # noqa: BLE001
    RelayClient = None  # type: ignore[assignment]
    NotBoundError = Exception  # type: ignore[assignment,misc]
    RelayError = Exception  # type: ignore[assignment,misc]

_RELAY_CONFIG_FILE = Path.home() / ".config" / "claude-tg-relay" / "config.toml"

# Per-token client registry.  keyed by installation token, so two roles bound
# to the same human share one client and one connection pool.
_clients: Dict[str, Any] = {}
_clients_lock = threading.Lock()


def _client(token: str) -> Any:
    """Return a ``RelayClient`` for *token*, creating it lazily."""
    with _clients_lock:
        if token not in _clients:
            if RelayClient is None:
                raise RelayError("relay_server package not importable")
            bindings = roles_config.load_bindings(_RELAY_CONFIG_FILE)
            if not bindings.server_url:
                raise RelayError("server_url not configured in config.toml")
            _clients[token] = RelayClient(bindings.server_url, token)
        return _clients[token]


# ---------------------------------------------------------------------------
# Per-call workspace TTL cache
#
# Keyed by workspace_dir string.  Stores a tuple
# (timestamp, QuestionsConfig|None, RoleCatalog|None, Bindings) so that a
# long-lived server serving different workspaces never uses one workspace's
# config for another — and picks up config changes within TTL seconds.
# ---------------------------------------------------------------------------

_CACHE_TTL = 5.0  # seconds

@dataclass
class _WorkspaceCache:
    ts: float
    qs_config: "qs.QuestionsConfig | None"
    catalog: "roles_config.RoleCatalog | None"
    bindings: "roles_config.Bindings"


_ws_cache: Dict[str, _WorkspaceCache] = {}
_ws_lock = threading.Lock()


def _load_workspace(workspace_dir: str) -> _WorkspaceCache:
    """Load (or return cached) workspace config for *workspace_dir*."""
    now = time.monotonic()
    with _ws_lock:
        cached = _ws_cache.get(workspace_dir)
        if cached is not None and now - cached.ts < _CACHE_TTL:
            return cached

        qs_config = qs.load_questions_config(workspace_dir)
        catalog = roles_config.load_catalog(workspace_dir)
        bindings = roles_config.load_bindings(_RELAY_CONFIG_FILE)

        entry = _WorkspaceCache(
            ts=now,
            qs_config=qs_config,
            catalog=catalog,
            bindings=bindings,
        )
        _ws_cache[workspace_dir] = entry
        return entry


def _resolve_workspace_dir() -> str:
    """Read per-call identity: ``CLAUDE_PROJECT_DIR`` → ``PWD`` → cwd."""
    return (
        os.environ.get("CLAUDE_PROJECT_DIR")
        or os.environ.get("PWD")
        or os.getcwd()
    )


# ---------------------------------------------------------------------------
# Rate-limiting state (min_interval_s) — in-memory per workspace_id.
# The brd D12 count for max_open uses the queue file (durable); min_interval
# is a short per-process guard (30 s default) where a restart reset is fine.
# ---------------------------------------------------------------------------

_last_ask_time: Dict[str, float] = {}
_rate_lock = threading.Lock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Keyboard building
# ---------------------------------------------------------------------------

# Button value prefix for question options — matches existing relay convention.
_QA_PREFIX = "qa"
# Acknowledge-notification sentinel button.
_ACK_BUTTON: list[list[dict[str, str]]] = [[{"label": "Acknowledge", "value": "qa0"}]]


def _build_keyboard(
    options: List[str],
) -> Optional[List[List[Dict[str, str]]]]:
    """One button per option, value ``qa<N>``."""
    if not options:
        return None
    rows: list[list[dict[str, str]]] = []
    for i, opt in enumerate(options):
        label = str(opt)[:64]  # Telegram button text cap
        rows.append([{"label": f"{i + 1}. {label}", "value": f"{_QA_PREFIX}{i}"}])
    return rows


# ---------------------------------------------------------------------------
# Relay send helpers
# ---------------------------------------------------------------------------


def _send_async_question(
    *,
    text: str,
    keyboard: Optional[List[List[Dict[str, str]]]],
    token: str,
    nudge_schedule: str,
    escalate_after_sec: Optional[int],
    escalate_to_token: Optional[str],
    idempotency_key: str,
) -> int:
    """Send a ``kind=question`` message with ``never_expires=True``.

    Returns the relay message_id.  Raises ``RelayError`` / ``NotBoundError``
    on failure; the caller catches and returns ``dispatched: false``.
    """
    handle = _client(token).send_message(
        text=text,
        keyboard=keyboard,
        kind="question",
        reply_required=True,
        ttl_sec=86400,         # ignored by relay when never_expires=True
        idempotency_key=idempotency_key,
        never_expires=True,
        nudge_schedule=nudge_schedule,
        escalate_after_sec=escalate_after_sec,
        escalate_to_token=escalate_to_token,
    )
    return handle.message_id


def _send_notification(
    *,
    text: str,
    keyboard: Optional[List[List[Dict[str, str]]]],
    token: str,
    nudge_schedule: Optional[str],
    kind: str,
    idempotency_key: str,
) -> int:
    """Send a ``kind=question`` or ``kind=notification`` message."""
    kw: dict[str, Any] = dict(
        text=text,
        keyboard=keyboard,
        kind=kind,
        reply_required=(keyboard is not None),
        ttl_sec=86400,
        idempotency_key=idempotency_key,
    )
    if kind == "question" and nudge_schedule is not None:
        kw["never_expires"] = True
        kw["nudge_schedule"] = nudge_schedule
    handle = _client(token).send_message(**kw)
    return handle.message_id


# ---------------------------------------------------------------------------
# Body rendering
# ---------------------------------------------------------------------------


def _render_ask_body(
    title: str,
    body: str,
    options: List[str],
    workspace_id: str,
    role_title: Optional[str],
    notes: List[str],
) -> str:
    """Render the Telegram HTML body for an ``ask`` message."""

    def esc(s: str) -> str:
        return html.escape(s or "", quote=False)

    lines: list[str] = [
        f"<b>Question</b> — {esc(workspace_id)}",
    ]
    if role_title:
        lines.append(f"<i>for: {esc(role_title)}</i>")
    for note in notes:
        if note:
            lines.append(f"⚠️ {esc(note)}")
    lines.append("")
    lines.append(f"<b>{esc(title)}</b>")
    if body:
        lines.append("")
        lines.append(esc(body))
    if options:
        lines.append("")
        lines.append("<i>Tap a number below, or reply with text:</i>")
        lines.append("")
        for i, opt in enumerate(options, start=1):
            lines.append(f"<b>{i}. {esc(opt)}</b>")
    else:
        lines.append("")
        lines.append("<i>Reply to this message with your answer.</i>")
    return "\n".join(lines)


def _render_notify_body(text: str, workspace_id: str, ack: bool) -> str:
    """Render the Telegram HTML body for a ``notify`` message."""

    def esc(s: str) -> str:
        return html.escape(s or "", quote=False)

    lines = [
        f"<b>Notification</b> — {esc(workspace_id)}",
        "",
        esc(text),
    ]
    if ack:
        lines += ["", "<i>Tap Acknowledge to confirm receipt.</i>"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def ask(
    title: str,
    body: str = "",
    options: Optional[List[str]] = None,
    role: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Write a question to the workspace queue, send it to Telegram, return id.

    Parameters
    ----------
    title:
        Short question title (becomes the Markdown heading suffix and the
        Telegram bold line).
    body:
        The question body — passed through verbatim into the queue entry and
        the Telegram message.  This is the agent's domain; the tool only
        owns the frame (invariant 6 / brd D3).
    options:
        Optional list of answer options.  Single-select only in v1 — the
        caller must not pass ``multi_select`` semantics.  Each option gets
        one Telegram button (``qa<N>`` value) and a numbered entry in the
        message body.
    role:
        Target role alias (lowercased).  ``None`` → workspace default role.
        Unresolvable (no token reachable) → refused before any write.
    tags:
        Optional list of tags appended to the heading (e.g. ``["blocks:
        phase-0.2"]``).

    Returns
    -------
    dict with keys:
        id          — allocated question id (``Q-NNN``)
        file        — relative path of the queue file inside the workspace
        dispatched  — ``True`` when the relay call succeeded
        message_id  — relay message id (``None`` when ``dispatched=False``)
        pending_answers — count of unapplied answers in the index
        error       — present (with a string) only when something went wrong
    """
    workspace_dir = _resolve_workspace_dir()
    wc = _load_workspace(workspace_dir)

    # ── 0. No [questions] section → clear error ──────────────────────────────
    if wc.qs_config is None:
        return {
            "error": (
                "This workspace has no [questions] section in .claude/roles.toml. "
                "Add one with at least `dir = \"docs/questions\"` to enable async questions."
            )
        }

    # ── 0b. No roles catalog → no token resolution ───────────────────────────
    if wc.catalog is None:
        return {
            "error": (
                "This workspace has no role catalog (roles.toml is missing or has no "
                "`default` key).  Add a [role.*] section and a `default` key to enable "
                "role-based routing."
            )
        }

    # ── 1. Resolve destination (role → token).  Refuse if unreachable. ───────
    dest = roles_config.resolve_destination(wc.catalog, wc.bindings, role)
    if dest.token is None:
        role_desc = f"@{role}" if role else "the default role"
        return {
            "error": (
                f"Role {role_desc} is not reachable from this machine — no installation "
                f"token found for role '{dest.role_id}'.  "
                "Check your ~/.config/claude-tg-relay/config.toml bindings."
            )
        }

    # ── 1b. Single-select only in v1 ─────────────────────────────────────────
    # (The relay's multi_select is for grouped messages; an async ask is
    # deliberately ungrouped — one Q-NNN is one entry.)
    # The caller simply passes options as a list.  We accept it as-is.
    opts: List[str] = list(options) if options else []

    # ── 1c. Open store — builds against resolved anchor root ─────────────────
    store = qs.open_store(workspace_dir)
    if store is None:
        # Should not happen (qs_config is not None above), but guard anyway.
        return {
            "error": (
                "Could not open the questions store for this workspace "
                "(roles.toml has a [questions] section but the store could not be "
                "constructed — check [questions].dir and [questions].anchor)."
            )
        }

    # ── 1d. Resolve queue file for this role ─────────────────────────────────
    # If a role was explicitly requested, check that role's own queue file
    # FIRST.  An explicitly requested chat-only role is refused even when the
    # destination resolved by fallback to a role that does have a queue (brd
    # §5; state.md invariant 1 — "an unresolvable role refuses here, before
    # anything is written").
    if role is not None:
        req_role_id = wc.catalog.alias_index.get(role.lower(), role)
        queue_file = store.queue_file_for_role(req_role_id)
        if queue_file is None:
            return {
                "error": (
                    f"Role '{role}' has no queue file in [questions.queue]; "
                    "it is chat-only.  To route questions here, add an entry like "
                    f"`{req_role_id} = \"for-{req_role_id}.md\"` under [questions.queue]."
                )
            }
    else:
        queue_file = store.queue_file_for_role(dest.role_id)
        if queue_file is None:
            return {
                "error": (
                    f"Default role '{dest.role_id}' has no queue file in "
                    "[questions.queue]; it is chat-only.  Add an entry like "
                    f"`{dest.role_id} = \"for-{dest.role_id}.md\"` under [questions.queue]."
                )
            }

    # ── 1e. Caps (brd D12) ───────────────────────────────────────────────────
    open_count = store.count_open()
    if open_count >= wc.qs_config.max_open:
        return {
            "error": (
                f"Too many open questions in this workspace ({open_count} >= "
                f"{wc.qs_config.max_open} max_open).  "
                "Wait for some to be answered before asking more."
            )
        }

    ws_id = store.workspace_id
    min_interval = wc.qs_config.min_interval_s
    with _rate_lock:
        last = _last_ask_time.get(ws_id, 0.0)
        elapsed = time.monotonic() - last
        if elapsed < min_interval:
            remaining = min_interval - elapsed
            return {
                "error": (
                    f"Rate limit: asks must be at least {min_interval:.0f}s apart; "
                    f"try again in {remaining:.1f}s."
                )
            }

    # ── 2. Write entry (invariant 1 — write before send) ─────────────────────
    routed_alias = dest.role_id
    tags_list: List[str] = list(tags) if tags else []
    try:
        new_entry = store.create_entry(
            title=title,
            body=body,
            role=dest.role_id,
            options=opts,
            tags=tags_list,
            routed_to=routed_alias,
            path=queue_file,
        )
    except qs.QuestionsStoreError as exc:
        # Nothing was written — the error happened before or during the
        # atomic write.  Surface it directly so the agent can diagnose.
        return {"error": f"Could not write question entry: {exc}"}

    # Update rate-limit timestamp only after a successful write.
    with _rate_lock:
        _last_ask_time[ws_id] = time.monotonic()

    qid = new_entry.qid
    rel_path = new_entry.rel_path

    # ── 3. Resolve escalation ─────────────────────────────────────────────────
    escalate_after_sec: Optional[int] = None
    escalate_to_token: Optional[str] = None
    if wc.qs_config.escalate_after is not None:
        escalate_after_sec = int(wc.qs_config.escalate_after)
    if escalate_after_sec is not None and not dest.is_default:
        # Escalation goes to the default role.
        default_dest = roles_config.resolve_destination(wc.catalog, wc.bindings, None)
        escalate_to_token = default_dest.token  # may still be None (fine, skip)

    # ── 4. Build Telegram body + keyboard ────────────────────────────────────
    notes_list = list(dest.notes)
    body_html = _render_ask_body(
        title=title,
        body=body,
        options=opts,
        workspace_id=ws_id,
        role_title=dest.title if not dest.is_default else None,
        notes=notes_list,
    )
    keyboard = _build_keyboard(opts)
    idempotency_key = f"ask:{ws_id}:{qid}:send"

    # ── 5. Send — relay failure returns dispatched: false with the id ─────────
    message_id: Optional[int] = None
    dispatched = False
    send_error: Optional[str] = None

    try:
        message_id = _send_async_question(
            text=body_html,
            keyboard=keyboard,
            token=dest.token,
            nudge_schedule=wc.qs_config.nudge,
            escalate_after_sec=escalate_after_sec,
            escalate_to_token=escalate_to_token,
            idempotency_key=idempotency_key,
        )
        dispatched = True
    except NotBoundError as exc:
        send_error = f"Relay refused: installation not bound ({exc}).  Run relay-client bind."
    except RelayError as exc:
        send_error = f"Relay send failed: {exc}"
    except Exception as exc:  # noqa: BLE001
        send_error = f"Unexpected error during relay send: {exc}"

    # ── 6. Record in index (even if send failed, for future diagnostics) ──────
    index_routing_failed = False
    if dispatched and message_id is not None:
        index_entry = qll.IndexEntry(
            workspace_id=ws_id,
            anchor=store.config.anchor,
            root=str(store.root),
            rel_path=rel_path,
            qid=qid,
            role=dest.role_id,
            created_at=_utc_now(),
        )
        _idx_exc: Exception | None = None
        for _attempt in range(3):
            try:
                qll.add_message_entry(message_id, index_entry)
                _idx_exc = None
                break
            except Exception as exc:  # noqa: BLE001
                _idx_exc = exc
                if _attempt < 2:
                    time.sleep(0.1 * (2 ** _attempt))  # 0.1 s, 0.2 s
        if _idx_exc is not None:
            # All retries exhausted — the answer for this message_id cannot be
            # routed automatically.  The queue entry and Telegram message both
            # exist; the operator must inspect the queue file to apply the answer.
            index_routing_failed = True
            logging.getLogger(__name__).warning(
                "index write failed after 3 attempts — message_id=%s qid=%s "
                "will not be routed automatically; check the queue file at %s. "
                "Error: %s",
                message_id, qid, rel_path, _idx_exc,
            )
            send_error = (
                (send_error + "; " if send_error else "")
                + f"index write failed after retries: {_idx_exc}. "
                  f"The answer for message_id={message_id} cannot be routed "
                  f"automatically — check the queue file."
            )

    # ── 7. mark_dispatched (best-effort — entry and message already exist) ────
    if dispatched and message_id is not None:
        try:
            store.mark_dispatched(qid, message_id, rel_path=rel_path)
        except qs.QuestionsStoreError as exc:
            # Stamp failed — notable but not fatal; entry and message still exist.
            send_error = (
                (send_error + "; " if send_error else "")
                + f"mark_dispatched failed: {exc}"
            )

    # ── 8. Return ─────────────────────────────────────────────────────────────
    pending_count = qll.count_pending()
    result: Dict[str, Any] = {
        "id": qid,
        "file": rel_path,
        "dispatched": dispatched,
        "message_id": message_id,
        "pending_answers": pending_count,
    }
    if index_routing_failed:
        result["index_routing_failed"] = True
    if send_error:
        result["error"] = send_error
    return result


def notify(
    text: str,
    role: Optional[str] = None,
    ack: bool = True,
) -> Dict[str, Any]:
    """Send an async notification to a human role.

    Parameters
    ----------
    text:
        The notification text (plain prose; HTML will be escaped before send).
    role:
        Target role alias.  ``None`` → workspace default role.
    ack:
        When ``True`` (the default), sends as ``kind=question`` with a single
        ``[ Acknowledge ]`` button so the nudge ladder has something to chase
        (invariant 10 / brd D7).  The message is recorded in the routing index
        with no ``qid``, so the listener finalises the Telegram message without
        touching any file.

        When ``False``, sends as ``kind=notification`` (fire-and-forget) and
        records nothing.  Idle-session notifications are byte-identical to the
        pre-epic behaviour (invariant 9).

    Returns
    -------
    dict with keys:
        dispatched  — ``True`` when the relay call succeeded
        message_id  — relay message id (``None`` when ``dispatched=False``)
        error       — present only when something went wrong
    """
    workspace_dir = _resolve_workspace_dir()
    wc = _load_workspace(workspace_dir)

    # For notify we only need a token — [questions] section is not required.
    if wc.catalog is None:
        return {
            "dispatched": False,
            "message_id": None,
            "error": (
                "This workspace has no role catalog (roles.toml missing or no "
                "`default` key).  Cannot resolve a destination token."
            ),
        }

    dest = roles_config.resolve_destination(wc.catalog, wc.bindings, role)
    if dest.token is None:
        role_desc = f"@{role}" if role else "the default role"
        return {
            "dispatched": False,
            "message_id": None,
            "error": (
                f"Role {role_desc} is not reachable from this machine — "
                f"no installation token found for role '{dest.role_id}'."
            ),
        }

    ws_id = wc.catalog.workspace_id
    body_html = _render_notify_body(text, ws_id, ack)

    idempotency_key = f"notify:{ws_id}:{time.time_ns()}:send"

    if ack:
        # kind=question with one Acknowledge button — nudges apply (brd D7)
        nudge_schedule: Optional[str] = None
        if wc.qs_config is not None:
            nudge_schedule = wc.qs_config.nudge
        message_id: Optional[int] = None
        dispatched = False
        send_error: Optional[str] = None
        try:
            message_id = _send_notification(
                text=body_html,
                keyboard=_ACK_BUTTON,
                token=dest.token,
                nudge_schedule=nudge_schedule,
                kind="question",
                idempotency_key=idempotency_key,
            )
            dispatched = True
        except NotBoundError as exc:
            send_error = f"Relay refused: installation not bound ({exc})."
        except RelayError as exc:
            send_error = f"Relay send failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            send_error = f"Unexpected error during relay send: {exc}"

        # Record in index (no qid — listener finalises without file write)
        if dispatched and message_id is not None:
            _notify_store = qs.open_store(workspace_dir) if wc.qs_config is not None else None
            index_entry = qll.IndexEntry(
                workspace_id=ws_id,
                anchor=(wc.qs_config.anchor if wc.qs_config is not None else "repo"),
                root=(str(_notify_store.root) if _notify_store is not None else ""),
                rel_path="",
                qid=None,   # ack-notification — no queue file involved
                role=dest.role_id,
                created_at=_utc_now(),
            )
            try:
                qll.add_message_entry(message_id, index_entry)
            except Exception as exc:  # noqa: BLE001
                send_error = (
                    (send_error + "; " if send_error else "")
                    + f"index write failed: {exc}"
                )

        result: Dict[str, Any] = {"dispatched": dispatched, "message_id": message_id}
        if send_error:
            result["error"] = send_error
        return result

    else:
        # ack=False: plain notification, fire-and-forget, no record
        message_id = None
        dispatched = False
        send_error = None
        try:
            message_id = _send_notification(
                text=body_html,
                keyboard=None,
                token=dest.token,
                nudge_schedule=None,
                kind="notification",
                idempotency_key=idempotency_key,
            )
            dispatched = True
        except NotBoundError as exc:
            send_error = f"Relay refused: installation not bound ({exc})."
        except RelayError as exc:
            send_error = f"Relay send failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            send_error = f"Unexpected error during relay send: {exc}"

        result = {"dispatched": dispatched, "message_id": message_id}
        if send_error:
            result["error"] = send_error
        return result
