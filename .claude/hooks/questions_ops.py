#!/usr/bin/env python3
"""Question operations shared by the MCP server and ``claude-questions`` (epic 46).

``withdraw``, ``claim`` and ``sync`` have two front ends — an MCP tool for a
session and a CLI flag for a human at a shell — and must behave identically
from both.  They live here, in an installed hook module, so neither front end
reimplements them (brd D1).  ``ask`` stays in ``questions_mcp_lib``, its only
caller.

Each function returns a plain ``dict`` (the tool result / what the CLI prints)
and never raises for an operational failure: a refused withdrawal, an
unreachable relay or a push that did not happen is reported in the result.

Order of work in :func:`withdraw` — the file first, the relay second:

1. ``questions_store.withdraw`` through ``questions_git.run``.  The file is the
   durable act and the one that decides a race with an answer (brd D5).
2. ``cancel`` the Telegram message on the relay, with the withdrawn text —
   which strips the keyboard, retires the nudge ladder, prevents escalation and
   (relay ≥ task 46-01) does the same to an escalated copy.  The cancel is the
   act; the text is a courtesy Telegram may refuse (it never lets a bot edit a
   question sent without buttons), so a refused edit never fails a withdrawal
   (task 49, see ``_relay_withdraw``).

A relay failure leaves a withdrawn entry whose message is still live; calling
``withdraw`` again is a no-op on the file and retries the relay (brd D4).
"""

from __future__ import annotations

import html
import sys
from pathlib import Path
from typing import Any, Callable

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import questions_git as qg  # noqa: E402
import questions_listen_lib as qll  # noqa: E402
import questions_store as qs  # noqa: E402
import roles_config  # noqa: E402

#: Telegram's text limit is 4096; leave room for the frame.
_TEXT_BUDGET = 3000

ClientFor = Callable[[str], Any]

_NO_QUESTIONS = (
    "This workspace has no [questions] section in .claude/roles.toml."
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _default_client_for(token: str) -> Any:
    bindings = roles_config.load_bindings(qll.RELAY_CONFIG_PATH)
    if not bindings.server_url:
        raise qll.RelayError(f"server_url is not configured in {qll.RELAY_CONFIG_PATH}")
    return qll._default_client_factory(bindings.server_url, token)


def _clip(text: str, budget: int = _TEXT_BUDGET) -> str:
    text = text.strip()
    return text if len(text) <= budget else text[:budget] + "…"


def withdrawn_text(title: str, reason: str, qid: str | None, workspace_id: str) -> str:
    """The body a withdrawn Telegram message ends on (HTML, escaped)."""

    def esc(value: str) -> str:
        return html.escape(value or "", quote=False)

    head = "🚫 <b>Withdrawn</b>"
    if title:
        head += f" — {esc(_clip(title, 300))}"
    lines = [head]
    if reason.strip():
        lines += ["", esc(_clip(reason))]
    footer = " · ".join(p for p in (qid or "", workspace_id or "") if p)
    if footer:
        lines += ["", f"<i>{esc(footer)}</i>"]
    return "\n".join(lines)


def _index_lookup(
    store: qs.QuestionsStore | None,
    workspace_id: str,
    *,
    qid: str | None = None,
    message_id: int | None = None,
) -> tuple[int | None, "qll.IndexEntry | None"]:
    """Find the routing record for a question id or a relay message id."""
    index = qll.read_index()
    if message_id is not None:
        return int(message_id), index.messages.get(str(int(message_id)))
    if qid is None or store is None:
        return None, None
    key = qs.parse_id(store.fmt, qid)
    best: tuple[int | None, Any] = (None, None)
    for mid, entry in index.messages.items():
        if entry.workspace_id != workspace_id or not entry.qid:
            continue
        if qs.parse_id(store.fmt, entry.qid) == key:
            try:
                number = int(mid)
            except ValueError:
                continue
            if best[0] is None or number > best[0]:
                best = (number, entry)
    return best


def _token_for(workspace_dir: str, store: qs.QuestionsStore | None, role: str | None) -> str | None:
    """The installation token a message to *role* was sent with."""
    catalog = roles_config.load_catalog(workspace_dir)
    if catalog is None:
        return None
    bindings = roles_config.load_bindings(qll.RELAY_CONFIG_PATH)
    return roles_config.resolve_destination(catalog, bindings, role).token


def _role_of_file(store: qs.QuestionsStore, rel_path: str) -> str | None:
    """Reverse ``[questions.queue]``: which role's queue is this file?"""
    name = Path(rel_path).name
    for role, filename in store.config.queue.items():
        if filename == name:
            return role
    return None


_NOTE_REPLIED = (
    "Telegram does not let a bot edit a question that was sent without buttons, "
    "so the withdrawal was posted as a reply under it."
)
_CLOSED = "the relay closed the message, so its reminders and escalation are cancelled, "
_NOTE_REFUSED = _CLOSED + (
    "but the chat still shows the original text: Telegram does not let a bot "
    "edit a question that was sent without buttons, and the reply that should "
    "have said so under it could not be sent."
)
_NOTE_ALREADY_CLOSED = (
    "the message was already closed on the relay (cancelled, answered or expired) "
    "before this call, so no reminder or escalation is pending and its text was "
    "left as it was."
)
_NOTE_502 = _CLOSED + (
    "but Telegram refused to change the message itself (the relay answered 502), "
    "so the chat may still show the original text.  A question sent without "
    "buttons can never be edited; this relay does not yet post the withdrawal "
    "as a reply instead."
)


def _closed_on_relay(client: Any, message_id: int) -> bool:
    """True when the relay says the message can no longer be answered.

    A closed message (cancelled, answered or expired) has no reminder and no
    escalation left, whatever the last call's status was.  Any doubt is False.
    """
    try:
        answer = client.wait_for_answer(message_id, timeout=1.0, long_poll_chunk=1)
    except Exception:  # noqa: BLE001 — unknown is "not known to be closed"
        return False
    return getattr(answer, "state", None) in ("cancelled", "answered", "expired")


def _send_text(client: Any, message_id: int, text: str) -> str:
    """PATCH the withdrawn text onto a message the relay has already closed.

    Returns a note; "" when the message now reads withdrawn.  Never raises: the
    cancel stands whatever becomes of the text.
    """
    try:
        client.edit_message(message_id, text=text)
    except Exception as exc:  # noqa: BLE001
        return _CLOSED + f"but its text could not be updated: {exc}"
    return ""


def _relay_withdraw(client: Any, message_id: int, text: str) -> str:
    """Cancel the message on the relay and make the chat say so.

    Returns a note for the caller ("" when the message itself now reads
    withdrawn).  Raises what the client raises when the message is **not**
    closed on the relay: that, and only that, is a failed cancel.

    The cancel goes first and carries the text.  It is the act that matters:
    it takes the reminders, the escalation and any escalated copy with it.  The
    text is a courtesy on top, and Telegram can refuse it: a question with no
    buttons is sent with a reply prompt, and a bot may never edit such a
    message.  So a refused edit never decides the outcome.

    Three relays are possible:

    * current: says what became of the text (``final_text_shown``, see
      ``RelayClient.cancel_message``).  Only ``"failed"`` (an edit that failed
      for a passing reason) is followed by an edit of our own.
    * from before that reply existed: answers ``{}``, or 502 on a message it
      cannot edit although it closed it.  Ask whether the message is closed.
    * from before task 46-01: ignores the text (or its client has no
      ``final_text``), so the text is sent as a separate edit afterwards.
    """
    reply: Any = None
    try:
        try:
            reply = client.cancel_message(message_id, final_text=text)
        except TypeError:
            # A relay client from before task 46-01 has no ``final_text``.
            reply = client.cancel_message(message_id)
    except Exception as exc:  # noqa: BLE001 — re-raised unless the relay closed it
        if getattr(exc, "status_code", None) != 502 or not _closed_on_relay(client, message_id):
            raise
        # No edit of our own: Telegram has just refused one.
        return _NOTE_502

    shown = reply.get("final_text_shown") if isinstance(reply, dict) else None
    if shown == "edit":
        return ""
    if shown == "reply":
        return _NOTE_REPLIED
    if shown == "refused":
        return _NOTE_REFUSED
    if shown == "already_closed":
        # Its text may be an answer someone gave; it is not ours to replace.
        return _NOTE_ALREADY_CLOSED
    # "failed", or no word on the text at all (an older relay): send it.
    return _send_text(client, message_id, text)


def _relay_failure(exc: BaseException, role: str | None) -> str:
    """Explain a failed relay call; a 404 gets the one explanation it can have.

    A message belongs to the installation that sent it.  The relay answers 404
    when asked about it with any other token — which is what happens if the
    role's binding in config.toml was added, removed or changed after the
    question was asked.  Retrying cannot fix that, so do not suggest it.
    """
    if getattr(exc, "status_code", None) == 404:
        return (
            f"{exc}.  The relay does not know this message under the token now bound "
            f"to role '{role}' — the binding has probably changed since the question "
            "was asked.  Cancel it from a machine (or a config.toml) that still has "
            "the original token."
        )
    return f"{exc}.  Run the withdrawal again to retry."


# ── withdraw ──────────────────────────────────────────────────────────────────


def withdraw(
    workspace_dir: str,
    *,
    qid: str | None = None,
    message_id: int | None = None,
    reason: str = "",
    by: str = "",
    client_for: ClientFor | None = None,
) -> dict[str, Any]:
    """Withdraw a question (by id) or an ack-notification (by relay message id).

    Returns the brd §4 shape::

        { id, withdrawn, status, file, message_id, relay_cancelled, sync?, error? }
    """
    client_for = client_for or _default_client_for
    reason = (reason or "").strip()
    if not reason:
        return {"withdrawn": False, "error": "A reason is required: say why the question is moot."}
    if not qid and message_id is None:
        return {"withdrawn": False, "error": "Pass the question id (e.g. Q-042), or message_id for a notification."}

    store = qs.open_store(workspace_dir)
    catalog = roles_config.load_catalog(workspace_dir)
    workspace_id = store.workspace_id if store is not None else (
        catalog.workspace_id if catalog is not None else Path(workspace_dir).name
    )

    # ── an ack-notification: nothing in any file, the relay only ──
    if not qid:
        mid, entry = _index_lookup(store, workspace_id, message_id=message_id)
        if entry is None:
            return {
                "withdrawn": False, "message_id": mid,
                "error": f"relay #{mid} is not a message this machine sent through the questions tools.",
            }
        if entry.qid:
            return {
                "withdrawn": False, "message_id": mid, "id": entry.qid,
                "error": f"relay #{mid} is question {entry.qid}; withdraw it by id.",
            }
        if entry.workspace_id and entry.workspace_id != workspace_id:
            # The role in the record means something in *that* workspace's
            # catalog, not this one's.
            return {
                "withdrawn": False, "message_id": mid,
                "error": (
                    f"relay #{mid} was sent from workspace '{entry.workspace_id}', "
                    f"not '{workspace_id}'; run the withdrawal from there."
                ),
            }
        out: dict[str, Any] = {"withdrawn": False, "message_id": mid, "relay_cancelled": False}
        token = _token_for(workspace_dir, store, entry.role)
        if token is None:
            out["error"] = f"no installation token for role '{entry.role}' on this machine."
            return out
        try:
            note = _relay_withdraw(client_for(token), int(mid), withdrawn_text("", reason, None, workspace_id))
        except Exception as exc:  # noqa: BLE001 — reported, retryable
            out["error"] = f"relay cancel failed: {_relay_failure(exc, entry.role)}"
            return out
        if note:
            out["note"] = note
        out["withdrawn"] = True
        out["relay_cancelled"] = True
        return out

    # ── a question ──
    if store is None:
        return {"withdrawn": False, "id": qid, "error": _NO_QUESTIONS}

    try:
        result, report = qg.run(
            store,
            lambda s: s.withdraw(qid, reason, withdrawn_by=by),
            summary=f"withdraw {qid}",
        )
    except qs.QuestionsStoreError as exc:
        return {"withdrawn": False, "id": qid, "error": f"Could not write the withdrawal: {exc}"}

    out = {
        "id": result.qid or qid,
        "withdrawn": result.ok,
        "status": result.status,
        "file": result.rel_path,
        "message_id": result.message_id,
        "relay_cancelled": False,
    }
    if report is not None:
        out["sync"] = report.to_dict()

    if result.outcome is qs.WithdrawOutcome.NOT_FOUND:
        out["error"] = f"No entry for {qid} in this workspace's queue files."
        return out
    if result.outcome is qs.WithdrawOutcome.CONFLICT:
        out["error"] = f"More than one entry is headed {qid}; resolve the duplicate by hand first."
        return out
    if result.outcome is qs.WithdrawOutcome.NOT_OPEN:
        found = f"[{result.status}]" if result.status else "no status bracket"
        out["error"] = (
            f"{out['id']} is not open ({found}) — only an open question can be withdrawn. "
            "If it was answered, the answer stands."
        )
        return out
    if result.outcome is qs.WithdrawOutcome.ALREADY_WITHDRAWN:
        out["note"] = "already withdrawn — the file was left as it is; the relay cancel is retried."

    # ── the relay ──
    mid, entry = _index_lookup(store, workspace_id, qid=result.qid)
    if result.message_id is not None:
        mid = result.message_id
        entry = qll.read_index().messages.get(str(mid)) or entry
    if mid is None:
        out["note"] = (
            (out.get("note", "") + "  " if out.get("note") else "")
            + "the entry was never dispatched to Telegram, so there is nothing to cancel on the relay."
        )
        return out
    out["message_id"] = mid

    role = entry.role if entry is not None and entry.role else _role_of_file(store, result.rel_path)
    token = _token_for(workspace_dir, store, role)
    if token is None:
        out["error"] = (
            f"withdrawn in the file, but no installation token for role '{role}' is "
            "configured on this machine — the Telegram buttons are still live."
        )
        return out
    text = withdrawn_text(result.title, reason, result.qid, workspace_id)
    try:
        note = _relay_withdraw(client_for(token), int(mid), text)
    except Exception as exc:  # noqa: BLE001 — reported, retryable
        out["error"] = (
            "withdrawn in the file, but the relay cancel failed — the Telegram "
            "message is still live (buttons, reminders and escalation).  "
            f"{_relay_failure(exc, role)}"
        )
        return out
    if note:
        out["note"] = (out["note"] + "  " if out.get("note") else "") + note
    out["relay_cancelled"] = True
    return out


# ── claim ─────────────────────────────────────────────────────────────────────


def claim(
    workspace_dir: str,
    *,
    count: int = 1,
    note: str = "",
    by: str = "",
) -> dict[str, Any]:
    """Reserve the next id(s) without asking anything.

    Returns ``{ ids, file, claimed_on_remote, sync?, error? }``.
    ``claimed_on_remote`` is the one to read: ``False`` means the reservation
    exists only in this checkout and another machine can still take the id.
    """
    store = qs.open_store(workspace_dir)
    if store is None:
        return {"ids": [], "claimed_on_remote": False, "error": _NO_QUESTIONS}
    try:
        ids, report = qg.run(
            store,
            lambda s: s.claim(count, note=note, claimed_by=by),
            summary=lambda got: (
                f"claim {got[0]}" if len(got) == 1 else f"claim {got[0]}–{got[-1]}"
            ),
        )
    except (qs.QuestionsStoreError, ValueError, TypeError) as exc:
        return {"ids": [], "claimed_on_remote": False, "error": f"Could not claim: {exc}"}

    out: dict[str, Any] = {
        "ids": ids,
        "file": store.rel_path(store.claims_path),
        "claimed_on_remote": bool(report is not None and report.pushed),
    }
    if report is not None:
        out["sync"] = report.to_dict()
        if not report.pushed:
            out["note"] = (
                "reserved in this checkout only"
                + (f" ({report.reason})" if report.reason else "")
                + " — another checkout can still allocate the same id until it is pushed."
            )
    else:
        out["note"] = (
            "[questions] sync is off for this workspace: the id is reserved in this "
            "checkout only.  Set sync = \"push\" to make claims reliable across machines."
        )
    return out


# ── sync ──────────────────────────────────────────────────────────────────────


def sync(workspace_dir: str) -> dict[str, Any]:
    """Commit and push whatever the queue files owe.  Returns ``{ sync }``."""
    store = qs.open_store(workspace_dir)
    if store is None:
        return {"error": _NO_QUESTIONS}
    if store.config.sync not in ("commit", "push"):
        return {
            "error": (
                "[questions] sync is off for this workspace; there is nothing to sync.  "
                "Set sync = \"commit\" or \"push\" in .claude/roles.toml."
            )
        }
    report = qg.sync(store)
    return {"sync": report.to_dict() if report is not None else {}}
