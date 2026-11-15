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


class FakeRelay:
    """Records the calls a withdrawal makes; can be told to fail."""

    def __init__(self, token: str):
        self.token = token
        self.calls: list[tuple] = []
        self.fail: Exception | None = None

    def edit_message(self, message_id, text=None):
        if self.fail is not None:
            raise self.fail
        self.calls.append(("edit", int(message_id), text))

    def cancel_message(self, message_id, final_text=None):
        if self.fail is not None:
            raise self.fail
        self.calls.append(("cancel", int(message_id), final_text))


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
        self.assertEqual([c[:2] for c in relay.calls], [("edit", 4187), ("cancel", 4187)])
        shown = relay.calls[0][2]
        self.assertEqual(relay.calls[1][2], shown, "the escalated copy must end on the same text")
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
        self.assertEqual(self.relays["rly_hpl_token"].calls[0][:2], ("edit", 4187))

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
        self.assertIn("buttons are still live", first["error"])
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
        self.assertEqual(
            [c[:2] for c in self.relays["rly_hpl_token"].calls],
            [("edit", 4187), ("cancel", 4187)],
        )

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
        self.assertEqual([c[:2] for c in calls], [("edit", 7001), ("cancel", 7001)])
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
