#!/usr/bin/env python3
"""Tests for ``questions_ops`` (epic 46, task 46-04) — withdraw, claim, sync.

These are the operations the ``questions`` MCP server and ``claude-questions``
share.  A fake relay client records every call; the workspace is a temp
directory with ``sync = "off"`` (the git side of each operation is covered in
``test_unit_questions_git``).
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS = _REPO_ROOT / ".claude" / "hooks"
if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))

_ISOLATED = tempfile.mkdtemp(prefix="questions-ops-test-")
_ORIG_PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR")
os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATED

import questions_listen_lib as qll  # noqa: E402
import questions_ops as qops  # noqa: E402
import questions_store as qs  # noqa: E402
import roles_config  # noqa: E402


def tearDownModule():
    if _ORIG_PROJECT_DIR is None:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
    else:
        os.environ["CLAUDE_PROJECT_DIR"] = _ORIG_PROJECT_DIR
    shutil.rmtree(_ISOLATED, ignore_errors=True)


ROLES_TOML = """\
workspace_id = "test-ws"
default      = "hpl"

[role.hpl]
title = "Product lead"

[role.htl]
title = "Tech lead"

[questions]
dir    = "docs/questions"
anchor = "worktree"

[questions.queue]
hpl = "for-product-lead.md"
htl = "for-tech-lead.md"
"""


class RelayStatus(Exception):
    """What the relay client raises for an HTTP error status."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"relay HTTP {status_code}: {detail}")
        self.status_code = status_code


class FakeRelay:
    """Records the calls a withdrawal makes; can be told to fail.

    Behaves like a current relay: the cancel carries the text and the reply
    says how the chat came to show it (``shown``).  ``state`` is what the relay
    would answer when asked whether the message is still open.
    """

    def __init__(self, token: str):
        self.token = token
        self.calls: list[tuple] = []
        self.fail: Exception | None = None
        self.fail_edit: Exception | None = None
        self.fail_cancel: Exception | None = None
        self.shown: str | None = "edit"
        self.state = "open"

    def edit_message(self, message_id, text=None):
        if self.fail is not None:
            raise self.fail
        self.calls.append(("edit", int(message_id), text))
        if self.fail_edit is not None:
            raise self.fail_edit

    def cancel_message(self, message_id, final_text=None):
        if self.fail is not None:
            raise self.fail
        self.calls.append(("cancel", int(message_id), final_text))
        if self.fail_cancel is not None:
            raise self.fail_cancel
        self.state = "cancelled"
        return {} if self.shown is None else {"final_text_shown": self.shown}

    def wait_for_answer(self, message_id, timeout=300.0, long_poll_chunk=30):
        self.calls.append(("state", int(message_id)))
        if self.state == "open":
            return None
        return types.SimpleNamespace(state=self.state, answer=None)


class OldFakeRelay(FakeRelay):
    """A relay client from before task 46-01: ``cancel`` takes no text."""

    def cancel_message(self, message_id):  # type: ignore[override]
        self.calls.append(("cancel", int(message_id)))


class OpsTestCase(unittest.TestCase):

    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="qops-ws-"))
        self.addCleanup(shutil.rmtree, self.ws, ignore_errors=True)
        (self.ws / ".claude").mkdir()
        (self.ws / ".claude" / "roles.toml").write_text(ROLES_TOML, encoding="utf-8")

        # roles_config looks only at $CLAUDE_PROJECT_DIR when it is set (the
        # module-level isolation above), so point it at this test's workspace.
        patcher = patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.ws)})
        patcher.start()
        self.addCleanup(patcher.stop)

        self.state = Path(tempfile.mkdtemp(prefix="qops-state-"))
        self.addCleanup(shutil.rmtree, self.state, ignore_errors=True)
        bindings = roles_config.Bindings(
            server_url="http://relay.test",
            default_token="rly_default_token",
            roles={"hpl": "rly_hpl_token", "htl": "rly_htl_token"},
            workspace_roles={},
            escalate_after={},
            workspace_escalate_after={},
            errors=(),
        )
        for target, name, value in (
            (qll, "INDEX_PATH", self.state / "async_questions.json"),
            (qll, "_LOCK_PATH", self.state / "async_questions.json.lock"),
        ):
            patcher = patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(roles_config, "load_bindings", return_value=bindings)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.relays: dict[str, FakeRelay] = {}
        self.relay_class = FakeRelay

    # ── helpers ──

    def client_for(self, token: str) -> FakeRelay:
        if token not in self.relays:
            self.relays[token] = self.relay_class(token)
        return self.relays[token]

    def store(self) -> qs.QuestionsStore:
        store = qs.open_store(str(self.ws))
        self.assertIsNotNone(store)
        return store

    def ask(self, title: str = "Ship <it> & go?", *, role: str = "hpl", message_id: int | None = 4187) -> str:
        """Create an entry the way ``ask`` leaves it: written, dispatched, indexed."""
        store = self.store()
        entry = store.create_entry(title=title, body="Body.", role=role, routed_to=role)
        if message_id is not None:
            store.mark_dispatched(entry.qid, message_id, rel_path=entry.rel_path)
            qll.add_message_entry(message_id, qll.IndexEntry(
                workspace_id="test-ws", anchor="worktree", root=str(self.ws),
                rel_path=entry.rel_path, qid=entry.qid, role=role,
                created_at="2026-11-15T10:00:00Z",
            ))
        return entry.qid

    def withdraw(self, **kwargs) -> dict:
        kwargs.setdefault("reason", "The change went ahead by itself.")
        kwargs.setdefault("by", "tester")
        return qops.withdraw(str(self.ws), client_for=self.client_for, **kwargs)

    def queue(self, name: str = "for-product-lead.md") -> str:
        return (self.ws / "docs" / "questions" / name).read_text(encoding="utf-8")


class TestWithdraw(OpsTestCase):

    def test_withdraw_marks_the_file_and_cancels_the_relay_message(self):
        qid = self.ask()
        result = self.withdraw(qid=qid)

        self.assertEqual(result, {
            "id": "Q-001",
            "withdrawn": True,
            "status": "withdrawn",
            "file": "docs/questions/for-product-lead.md",
            "message_id": 4187,
            "relay_cancelled": True,
        })
        self.assertNotIn("sync", result, "sync is off: the key must be absent")

        text = self.queue()
        self.assertIn("## Q-001 — Ship <it> & go?  [withdrawn]", text)
        self.assertIn("**Withdrawn:** ", text)
        self.assertIn(" · tester\n", text)
        self.assertIn("    The change went ahead by itself.", text)

        relay = self.relays["rly_hpl_token"]
        self.assertEqual(
            [c[:2] for c in relay.calls], [("cancel", 4187)],
            "one call: the cancel carries the text, for the message and its escalated copy",
        )
        shown = relay.calls[0][2]
        self.assertIn("🚫 <b>Withdrawn</b> — Ship &lt;it&gt; &amp; go?", shown)
        self.assertIn("The change went ahead by itself.", shown)
        self.assertIn("<i>Q-001 · test-ws</i>", shown)

    def test_the_token_is_the_one_the_question_was_sent_with(self):
        qid = self.ask(role="htl", message_id=5001)
        result = self.withdraw(qid=qid)
        self.assertTrue(result["relay_cancelled"])
        self.assertEqual(list(self.relays), ["rly_htl_token"])
        self.assertIn("[withdrawn]", self.queue("for-tech-lead.md"))

    def test_the_relay_id_is_read_from_the_file_when_the_index_lost_it(self):
        qid = self.ask(message_id=4187)
        os.unlink(qll.INDEX_PATH)
        result = self.withdraw(qid=qid)
        self.assertTrue(result["relay_cancelled"])
        self.assertEqual(self.relays["rly_hpl_token"].calls[0][:2], ("cancel", 4187))

    def test_an_undispatched_entry_has_nothing_to_cancel(self):
        qid = self.ask(message_id=None)
        result = self.withdraw(qid=qid)
        self.assertTrue(result["withdrawn"])
        self.assertFalse(result["relay_cancelled"])
        self.assertIsNone(result["message_id"])
        self.assertIn("never dispatched", result["note"])
        self.assertNotIn("error", result)
        self.assertEqual(self.relays, {})

    def test_an_answered_question_is_refused_and_the_relay_is_not_touched(self):
        qid = self.ask()
        self.store().apply_answer(qid, "Ship it", "@hpl", 4187, role="hpl")
        before = self.queue()
        result = self.withdraw(qid=qid)
        self.assertFalse(result["withdrawn"])
        self.assertEqual(result["status"], "resolved")
        self.assertIn("only an open question can be withdrawn", result["error"])
        self.assertIn("the answer stands", result["error"])
        self.assertEqual(self.queue(), before)
        self.assertEqual(self.relays, {})

    def test_relay_failure_is_reported_and_a_second_call_retries_only_the_relay(self):
        qid = self.ask()
        self.client_for("rly_hpl_token").fail = RuntimeError("connection refused")
        first = self.withdraw(qid=qid)
        self.assertTrue(first["withdrawn"], "the file is the durable act")
        self.assertFalse(first["relay_cancelled"])
        self.assertIn("message is still live", first["error"])
        self.assertIn("connection refused", first["error"])

        after_first = self.queue()
        self.relays["rly_hpl_token"].fail = None
        second = self.withdraw(qid=qid, reason="another wording")
        self.assertTrue(second["withdrawn"])
        self.assertTrue(second["relay_cancelled"])
        self.assertIn("already withdrawn", second["note"])
        self.assertNotIn("error", second)
        self.assertEqual(self.queue(), after_first, "a retry must not write the file again")

    def test_a_404_is_explained_as_a_changed_binding_not_as_retryable(self):
        """A message belongs to the installation that sent it.  If the role's
        binding changed since, the relay answers 404 and no retry will help."""
        class NotFound(Exception):
            status_code = 404

        qid = self.ask()
        self.client_for("rly_hpl_token").fail = NotFound("relay HTTP 404: not_found")
        result = self.withdraw(qid=qid)
        self.assertTrue(result["withdrawn"])
        self.assertFalse(result["relay_cancelled"])
        self.assertIn("token now bound to role 'hpl'", result["error"])
        self.assertNotIn("again to retry", result["error"])
        self.assertNotIn("rly_", result["error"], "never token material")

    def test_a_role_that_fell_back_at_ask_time_is_cancelled_with_the_token_used(self):
        """The index records the role the message was *sent as*.  ``ask(role=htl)``
        on a machine with no htl binding goes to the default role, and that is
        the token the cancel needs — not htl's, should one be added later."""
        store = self.store()
        entry = store.create_entry(title="t", role="htl", routed_to="hpl")
        store.mark_dispatched(entry.qid, 6001, rel_path=entry.rel_path)
        qll.add_message_entry(6001, qll.IndexEntry(
            workspace_id="test-ws", anchor="worktree", root=str(self.ws),
            rel_path=entry.rel_path, qid=entry.qid, role="hpl",     # sent as the default role
            created_at="2026-11-15T10:00:00Z",
        ))
        result = self.withdraw(qid=entry.qid)
        self.assertTrue(result["relay_cancelled"])
        self.assertEqual(list(self.relays), ["rly_hpl_token"])

    def test_a_relay_client_without_final_text_still_cancels(self):
        self.relay_class = OldFakeRelay
        qid = self.ask()
        result = self.withdraw(qid=qid)
        self.assertTrue(result["relay_cancelled"])
        self.assertNotIn("note", result)
        calls = self.relays["rly_hpl_token"].calls
        self.assertEqual(
            [c[:2] for c in calls], [("cancel", 4187), ("edit", 4187)],
            "an old relay ignores the text, so it is sent as an edit after the cancel",
        )
        self.assertIn("🚫 <b>Withdrawn</b>", calls[1][2])

    # ── a question without buttons: Telegram refuses every edit of it ──

    def test_a_question_without_buttons_is_cancelled_and_told_in_a_reply(self):
        """Telegram never lets a bot edit a message sent with the reply prompt
        (what a question with no options is).  The relay closes it and posts
        the withdrawal under it; the caller is told the cancel worked."""
        qid = self.ask()
        self.client_for("rly_hpl_token").shown = "reply"
        result = self.withdraw(qid=qid)
        self.assertTrue(result["withdrawn"])
        self.assertTrue(result["relay_cancelled"])
        self.assertNotIn("error", result)
        self.assertIn("posted as a reply under it", result["note"])
        self.assertEqual(
            [c[:2] for c in self.relays["rly_hpl_token"].calls], [("cancel", 4187)],
            "no edit is attempted: Telegram would refuse it",
        )

    def test_a_cancel_whose_text_reached_nobody_is_still_a_cancel(self):
        qid = self.ask()
        self.client_for("rly_hpl_token").shown = "refused"
        result = self.withdraw(qid=qid)
        self.assertTrue(result["relay_cancelled"])
        self.assertNotIn("error", result)
        self.assertIn("reminders and escalation are cancelled", result["note"])
        self.assertIn("still shows the original text", result["note"])
        self.assertEqual(
            [c[:2] for c in self.relays["rly_hpl_token"].calls], [("cancel", 4187)],
            "no edit of our own: Telegram never allows one on this message",
        )

    def test_an_edit_that_failed_in_passing_is_sent_again(self):
        """A question with buttons whose text edit stumbled inside the relay:
        the row is closed, and the text is not given up on."""
        qid = self.ask()
        relay = self.client_for("rly_hpl_token")
        relay.shown = "failed"
        result = self.withdraw(qid=qid)
        self.assertTrue(result["relay_cancelled"])
        self.assertNotIn("note", result)
        self.assertEqual([c[:2] for c in relay.calls], [("cancel", 4187), ("edit", 4187)])
        self.assertIn("🚫 <b>Withdrawn</b>", relay.calls[1][2])

    def test_an_edit_that_failed_twice_is_a_note_not_a_failed_cancel(self):
        qid = self.ask()
        relay = self.client_for("rly_hpl_token")
        relay.shown = "failed"
        relay.fail_edit = RuntimeError("connection reset")
        result = self.withdraw(qid=qid)
        self.assertTrue(result["relay_cancelled"])
        self.assertNotIn("error", result)
        self.assertIn("its text could not be updated: connection reset", result["note"])

    def test_a_message_closed_before_the_call_keeps_its_text(self):
        """Answered a moment before, or cancelled by an earlier attempt: the
        text there may be the human's answer, and it is not replaced."""
        qid = self.ask()
        relay = self.client_for("rly_hpl_token")
        relay.shown = "already_closed"
        result = self.withdraw(qid=qid)
        self.assertTrue(result["relay_cancelled"])
        self.assertNotIn("error", result)
        self.assertIn("already closed on the relay", result["note"])
        self.assertNotIn("original text", result["note"])
        self.assertEqual([c[:2] for c in relay.calls], [("cancel", 4187)])

    def test_closed_means_cancelled_answered_or_expired_and_nothing_else(self):
        relay = self.client_for("rly_hpl_token")
        for state, closed in (("cancelled", True), ("answered", True),
                              ("expired", True), ("open", False), ("mystery", False)):
            relay.state = state
            self.assertEqual(qops._closed_on_relay(relay, 1), closed, state)

        def boom(*_a, **_k):
            raise RelayStatus(404, "not_found")

        relay.wait_for_answer = boom
        self.assertFalse(qops._closed_on_relay(relay, 1), "any doubt is 'not closed'")
        self.assertFalse(qops._closed_on_relay(object(), 1), "a client that cannot say")

    def test_a_502_from_a_relay_that_did_close_the_message_is_a_cancel(self):
        """The relay deployed before this fix closes the message, then answers
        502 because Telegram refused to strip a keyboard that was never there.
        What matters to the caller is the reminders, and those are gone."""
        qid = self.ask()
        relay = self.client_for("rly_hpl_token")

        def closes_then_502(message_id, final_text=None):
            relay.calls.append(("cancel", int(message_id), final_text))
            relay.state = "cancelled"
            raise RelayStatus(502, "telegram_error")

        relay.cancel_message = closes_then_502
        result = self.withdraw(qid=qid)
        self.assertTrue(result["withdrawn"])
        self.assertTrue(result["relay_cancelled"], result)
        self.assertNotIn("error", result)
        self.assertIn("reminders and escalation are cancelled", result["note"])
        self.assertIn("may still show the original text", result["note"])
        self.assertEqual(
            [c[:2] for c in relay.calls], [("cancel", 4187), ("state", 4187)],
            "no edit after a refusal: it would only be refused again",
        )

    def test_a_502_that_left_the_message_open_is_a_failed_cancel(self):
        qid = self.ask()
        self.client_for("rly_hpl_token").fail_cancel = RelayStatus(502, "telegram_error")
        result = self.withdraw(qid=qid)
        self.assertTrue(result["withdrawn"], "the file is the durable act")
        self.assertFalse(result["relay_cancelled"])
        self.assertIn("reminders and escalation", result["error"])
        self.assertIn("relay HTTP 502", result["error"])
        self.assertIn("again to retry", result["error"])

    def test_an_old_relay_that_cannot_edit_the_message_has_still_cancelled_it(self):
        self.relay_class = OldFakeRelay
        qid = self.ask()
        self.client_for("rly_hpl_token").fail_edit = RelayStatus(502, "telegram_error")
        result = self.withdraw(qid=qid)
        self.assertTrue(result["relay_cancelled"])
        self.assertNotIn("error", result)
        self.assertIn("its text could not be updated: relay HTTP 502", result["note"])

    def test_an_already_withdrawn_entry_keeps_both_notes(self):
        qid = self.ask()
        self.withdraw(qid=qid)
        self.relays["rly_hpl_token"].shown = "reply"
        again = self.withdraw(qid=qid)
        self.assertTrue(again["relay_cancelled"])
        self.assertIn("already withdrawn", again["note"])
        self.assertIn("posted as a reply under it", again["note"])

    def test_not_found_conflict_and_missing_arguments(self):
        self.assertIn("No entry for Q-404", self.withdraw(qid="Q-404")["error"])
        self.assertIn("reason is required", self.withdraw(qid="Q-001", reason="  ")["error"])
        self.assertIn("question id", self.withdraw()["error"])

        qdir = self.ws / "docs" / "questions"
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / "for-product-lead.md").write_text(
            "## Q-005 — a  [open]\n\n## Q-005 — b  [open]\n", encoding="utf-8")
        self.assertIn("More than one entry", self.withdraw(qid="Q-005")["error"])

    def test_no_questions_section(self):
        (self.ws / ".claude" / "roles.toml").write_text(
            'workspace_id = "test-ws"\ndefault = "hpl"\n\n[role.hpl]\ntitle = "P"\n', encoding="utf-8")
        result = self.withdraw(qid="Q-001")
        self.assertFalse(result["withdrawn"])
        self.assertIn("no [questions] section", result["error"])


class TestWithdrawNotification(OpsTestCase):
    """``notify(ack=True)`` writes no file; its message can still go stale."""

    def _notify(self, message_id: int = 7001) -> None:
        qll.add_message_entry(message_id, qll.IndexEntry(
            workspace_id="test-ws", anchor="worktree", root=str(self.ws),
            rel_path="", qid=None, role="hpl", created_at="2026-11-15T10:00:00Z",
        ))

    def test_cancels_on_the_relay_and_touches_no_file(self):
        self._notify()
        result = self.withdraw(message_id=7001)
        self.assertEqual(result, {"withdrawn": True, "message_id": 7001, "relay_cancelled": True})
        calls = self.relays["rly_hpl_token"].calls
        self.assertEqual([c[:2] for c in calls], [("cancel", 7001)])
        self.assertIn("🚫 <b>Withdrawn</b>", calls[0][2])
        self.assertFalse((self.ws / "docs").exists())

    def test_an_unknown_message_is_refused(self):
        result = self.withdraw(message_id=9999)
        self.assertFalse(result["withdrawn"])
        self.assertIn("not a message this machine sent", result["error"])
        self.assertEqual(self.relays, {})

    def test_another_workspaces_notification_is_refused(self):
        qll.add_message_entry(7002, qll.IndexEntry(
            workspace_id="some-other-ws", anchor="worktree", root="/elsewhere",
            rel_path="", qid=None, role="hpl", created_at="2026-11-15T10:00:00Z",
        ))
        result = self.withdraw(message_id=7002)
        self.assertFalse(result["withdrawn"])
        self.assertIn("sent from workspace 'some-other-ws'", result["error"])
        self.assertEqual(self.relays, {})

    def test_a_question_must_be_withdrawn_by_id(self):
        qid = self.ask()
        result = self.withdraw(message_id=4187)
        self.assertFalse(result["withdrawn"])
        self.assertEqual(result["id"], qid)
        self.assertIn("withdraw it by id", result["error"])
        self.assertIn("[open]", self.queue())


class TestClaimAndSync(OpsTestCase):

    def test_claim_with_sync_off_is_local_and_says_so(self):
        self.ask(message_id=None)
        result = qops.claim(str(self.ws), count=2, note="hf-123", by="tester")
        self.assertEqual(result["ids"], ["Q-002", "Q-003"])
        self.assertEqual(result["file"], "docs/questions/claims.md")
        self.assertFalse(result["claimed_on_remote"])
        self.assertIn("this checkout only", result["note"])
        self.assertNotIn("sync", result)
        ledger = (self.ws / "docs" / "questions" / "claims.md").read_text(encoding="utf-8")
        self.assertIn(" · tester · hf-123\n", ledger)

    def test_claim_errors_are_results_not_exceptions(self):
        result = qops.claim(str(self.ws), count=0)
        self.assertEqual(result["ids"], [])
        self.assertIn("count must be between", result["error"])

    def test_sync_with_sync_off_explains_itself(self):
        result = qops.sync(str(self.ws))
        self.assertIn("sync is off", result["error"])

    def test_withdrawn_text_is_bounded(self):
        text = qops.withdrawn_text("t" * 5000, "r" * 9000, "Q-001", "ws")
        self.assertLess(len(text), 4096)


if __name__ == "__main__":
    unittest.main(verbosity=2)
