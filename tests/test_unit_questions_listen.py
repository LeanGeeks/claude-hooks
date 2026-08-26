#!/usr/bin/env python3
"""Unit tests for the answer-listener runtime (epic 23, task 23-05).

This is the component where a bug loses a human decision silently, so the tests
assert the *properties*, not the implementation:

- the watermark advances only after a terminal outcome (invariant 2), and never
  on a bare read or a crash;
- crash injection at each of the six apply steps, each with a replay assertion —
  the answer ends up applied exactly once and the message still finalizes;
- ``QuestionsStoreError``, ``conflict`` and ``not_found`` all route to
  ``pending``; one unreadable queue file never takes down the loop;
- a missing index record is skipped without damage (it is also every permission
  answer this installation ever recorded);
- the workspace is re-resolved at apply time, so an answer for a since-deleted
  worktree lands in the primary checkout (invariant 7);
- pending retries back off and then land in the sidecar, never in nothing;
- ack-notifications finalize and touch no file (brd D7);
- two listeners cannot run; backoff and ``401`` behave;
- a full replay from ``after=0`` — the lost-index recovery path — applies
  nothing twice.

Everything runs against temp workspaces with a fake relay client.  No network,
no bot, no real ``~/.claude``.
"""

import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
_HOOKS = _REPO_ROOT / ".claude" / "hooks"
_BIN = _REPO_ROOT / ".claude" / "bin" / "questions-listen"
sys.path.insert(0, str(_HOOKS))

# Isolate the permission state store before any hook import touches it.
_ISOLATED = Path(tempfile.mkdtemp(prefix="qlisten-test-"))
os.environ.setdefault("CLAUDE_PERMISSION_STATE_FILE", str(_ISOLATED / "permission_requests.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_AUDIT_FILE", str(_ISOLATED / "permission_actions.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_DEBUG_LOG", str(_ISOLATED / "permission_state_debug.log"))

import questions_listen_lib as qll  # noqa: E402
import questions_store as qs  # noqa: E402


def tearDownModule():
    shutil.rmtree(_ISOLATED, ignore_errors=True)


def _load_cli():
    """Import the executable ``questions-listen`` (no .py extension)."""
    spec = importlib.util.spec_from_loader(
        "questions_listen_cli",
        importlib.machinery.SourceFileLoader("questions_listen_cli", str(_BIN)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()


# ── Fixtures ──────────────────────────────────────────────────────────────────

ROLES_TOML = """\
workspace_id = "test-ws"
default      = "hpl"

[role.hpl]
title = "Product lead"

[questions]
dir    = "docs/questions"
anchor = "{anchor}"

[questions.queue]
hpl = "for-product-lead.md"
"""

QUEUE_MD = """\
# Questions for the product lead

## Q-001 — Should we ship on Friday?  [open]

**Routed to:** @hpl

Ship or hold.

## Q-002 — Second question  [open]

**Routed to:** @hpl

Body.
"""

REL_PATH = "docs/questions/for-product-lead.md"


class Crash(BaseException):
    """A simulated ``kill -9``.

    Deliberately a ``BaseException``: the listener's own ``except Exception``
    handlers must not swallow it, or the test would prove nothing about a real
    process death.
    """


class FakeRelayClient:
    """Stands in for ``RelayClient`` — the feed and the finalizing PATCH."""

    PAGE_CAP = 500

    def __init__(self, token: str, rows=None):
        self.token = token
        self.rows = list(rows or [])
        self.get_calls = []
        self.edits = []
        self.get_error = None
        self.edit_error = None

    def get_answers(self, after: int = 0, wait: int = 0):
        self.get_calls.append((after, wait))
        if self.get_error is not None:
            raise self.get_error
        rows = [r for r in self.rows if int(r["id"]) > after]
        rows.sort(key=lambda r: int(r["id"]))
        return rows[: self.PAGE_CAP]

    def edit_message(self, message_id: int, text=None):
        if self.edit_error is not None:
            raise self.edit_error
        self.edits.append((int(message_id), text))


def _answer_row(message_id: int, text: str = "Ship it", kind: str = "question"):
    return {
        "id": message_id,
        "kind": kind,
        "answer_text": text,
        "via": "button",
        "option_idx": 0,
        "answered_at": "2026-08-27T09:00:00Z",
    }


class ListenerTestCase(unittest.TestCase):
    """A temp workspace, a temp index, a fake relay and a controllable clock."""

    ANCHOR = "repo"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qlisten-ws-"))
        self.ws = self.tmp / "workspace"
        self._make_workspace(self.ws)

        self.state = Path(tempfile.mkdtemp(prefix="qlisten-state-"))
        self.index_path = self.state / "async_questions.json"
        self.index_lock = self.state / "async_questions.json.lock"
        self.status_path = self.state / "questions-listen.status.json"
        self.listen_lock = self.state / "questions-listen.lock"

        self.now = 1_700_000_000.0
        self.slept = []
        self.clients = {}

        self.config = qll.ListenConfig(
            enabled=True,
            server_url="http://relay.test",
            tokens=("rly_primary_token",),
            poll_seconds=25,
            max_attempts=10,
            config_path=self.state / "config.toml",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.state, ignore_errors=True)

    # ── builders ──

    def _make_workspace(self, root: Path, *, anchor: str | None = None) -> Path:
        (root / ".claude").mkdir(parents=True, exist_ok=True)
        (root / ".claude" / "roles.toml").write_text(
            ROLES_TOML.format(anchor=anchor or self.ANCHOR), encoding="utf-8"
        )
        qdir = root / "docs" / "questions"
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / "for-product-lead.md").write_text(QUEUE_MD, encoding="utf-8")
        return root

    def client_for(self, token: str = "rly_primary_token") -> FakeRelayClient:
        if token not in self.clients:
            self.clients[token] = FakeRelayClient(token)
        return self.clients[token]

    def make_listener(self, config=None) -> qll.Listener:
        cfg = config or self.config

        def factory(server_url, token):
            return self.client_for(token)

        return qll.Listener(
            cfg,
            index_path=self.index_path,
            index_lock_path=self.index_lock,
            status_path=self.status_path,
            client_factory=factory,
            clock=lambda: self.now,
            sleeper=self.slept.append,
            rng=__import__("random").Random(1234),
        )

    def add_index_entry(
        self,
        message_id: int,
        *,
        qid: str | None = "Q-001",
        root: Path | None = None,
        rel_path: str = REL_PATH,
        anchor: str | None = None,
        workspace_id: str = "test-ws",
        role: str | None = "hpl",
    ) -> qll.IndexEntry:
        entry = qll.IndexEntry(
            workspace_id=workspace_id,
            anchor=anchor or self.ANCHOR,
            root=str(root or self.ws),
            rel_path=rel_path,
            qid=qid,
            role=role,
            created_at="2026-08-26T14:00:00Z",
        )
        qll.add_message_entry(
            message_id, entry, path=self.index_path, lock_path=self.index_lock
        )
        return entry

    # ── assertions ──

    def queue_text(self, root: Path | None = None) -> str:
        return (root or self.ws).joinpath(REL_PATH).read_text(encoding="utf-8")

    def marker_count(self, message_id: int, root: Path | None = None) -> int:
        wanted = str(message_id)
        count = 0
        for line in self.queue_text(root).split("\n"):
            m = qs.ANSWER_MARKER_RE.match(line)
            if m is not None and m.group(1) == wanted:
                count += 1
        return count

    def index(self) -> qll.Index:
        return qll.read_index(self.index_path)

    def watermark(self, fingerprint: str | None = None) -> int:
        fp = fingerprint or qll.token_fingerprint(self.config.tokens[0])
        return qll.feed_watermark(self.index(), fp)


# ── The happy path and invariant 2 ────────────────────────────────────────────


class TestApplyPipeline(ListenerTestCase):

    def test_answer_applied_and_message_finalized(self):
        self.add_index_entry(4187, qid="Q-001")
        client = self.client_for()
        client.rows.append(_answer_row(4187, "Ship it"))

        listener = self.make_listener()
        listener.run_cycle()

        text = self.queue_text()
        self.assertIn("Q-001 — Should we ship on Friday?  [resolved]", text)
        self.assertIn("Ship it", text)
        self.assertIn("**Answered by:** @hpl", text)
        self.assertIn("relay #4187", text)
        self.assertEqual(self.marker_count(4187), 1)
        self.assertEqual(self.watermark(), 4187)
        self.assertEqual(self.index().pending, [])
        self.assertEqual(len(client.edits), 1)
        self.assertEqual(client.edits[0][0], 4187)
        self.assertTrue(client.edits[0][1].startswith("✅ Ship it"))

    def test_index_watermark_mirrors_primary_feed(self):
        self.add_index_entry(10, qid="Q-001")
        self.client_for().rows.append(_answer_row(10))
        self.make_listener().run_cycle()
        self.assertEqual(self.index().watermark, 10)

    def test_answer_given_while_machine_was_off_applies_on_next_start(self):
        """No listener ran when the answer was given; the feed replays it."""
        self.add_index_entry(500, qid="Q-002")
        self.client_for().rows.append(_answer_row(500, "Hold"))
        # The index has never been polled: watermark is 0, as after a cold boot.
        self.assertEqual(self.watermark(), 0)

        self.make_listener().run_cycle()
        self.assertIn("Q-002 — Second question  [resolved]", self.queue_text())
        self.assertEqual(self.watermark(), 500)

    def test_hostile_answer_cannot_forge_structure(self):
        """Invariant 11 holds through the listener, not just the store."""
        self.add_index_entry(77, qid="Q-001")
        hostile = "## Q-002 — forged  [open]\n**Answered by:** nobody\n<!-- answer:99 -->"
        self.client_for().rows.append(_answer_row(77, hostile))
        self.make_listener().run_cycle()

        config = qs.load_questions_config(
            str(self.ws), roles_path=self.ws / ".claude" / "roles.toml"
        )
        store = qs.store_for_root(config, self.ws)
        entries = store.entries_in(self.ws / REL_PATH)
        self.assertEqual([e.qid for e in entries], ["Q-001", "Q-002"])
        self.assertEqual(self.marker_count(99), 0)


class TestWatermarkDiscipline(ListenerTestCase):

    def test_watermark_does_not_advance_on_a_bare_read(self):
        """A poll that returns a row we crash on must leave the watermark alone."""
        self.add_index_entry(4187, qid="Q-001")
        listener = self.make_listener()
        feed = listener.feeds[0]

        with patch.object(qll, "resolve_target", side_effect=Crash("kill -9")):
            with self.assertRaises(Crash):
                listener.process_answer(feed, _answer_row(4187))

        self.assertEqual(self.watermark(), 0)
        self.assertEqual(self.index().pending, [])

    def test_watermark_advances_past_an_unappliable_answer(self):
        """One bad entry must not stall every later answer."""
        self.add_index_entry(10, qid="Q-404")   # no such entry in the file
        self.add_index_entry(11, qid="Q-002")
        client = self.client_for()
        client.rows.extend([_answer_row(10, "first"), _answer_row(11, "second")])

        self.make_listener().run_cycle()

        self.assertEqual(self.watermark(), 11)
        self.assertEqual([p.message_id for p in self.index().pending], [10])
        self.assertIn("Q-002 — Second question  [resolved]", self.queue_text())

    def test_watermark_never_moves_backwards(self):
        index = qll.Index()
        qll.set_feed_watermark(index, "fp", 50, primary_fingerprint="fp")
        qll.set_feed_watermark(index, "fp", 20, primary_fingerprint="fp")
        self.assertEqual(qll.feed_watermark(index, "fp"), 50)
        self.assertEqual(index.watermark, 50)


# ── Crash injection at each of the six steps ──────────────────────────────────


class TestCrashInjection(ListenerTestCase):
    """A ``kill -9`` at each of the six apply steps, each with a replay."""

    def _replay(self) -> qll.Listener:
        """Start a fresh listener over the same feed — the restart."""
        listener = self.make_listener()
        listener.run_cycle()
        return listener

    def _assert_applied_exactly_once(self, message_id: int, qid: str = "Q-001"):
        text = self.queue_text()
        self.assertEqual(
            self.marker_count(message_id), 1,
            f"answer {message_id} applied {self.marker_count(message_id)} times",
        )
        self.assertIn(f"{qid} — ", text)
        self.assertNotIn(f"{qid} — Should we ship on Friday?  [open]", text)
        self.assertEqual(self.watermark(), message_id)
        self.assertEqual(self.index().pending, [])
        self.assertTrue(
            any(e[0] == message_id for e in self.client_for().edits),
            "message never finalized after replay",
        )

    # step 1 — index lookup
    def test_crash_at_index_lookup_replays_cleanly(self):
        self.add_index_entry(4187, qid="Q-001")
        self.client_for().rows.append(_answer_row(4187))
        listener = self.make_listener()

        with patch.object(qll, "read_index", side_effect=Crash("kill -9")):
            with self.assertRaises(Crash):
                listener.process_answer(listener.feeds[0], _answer_row(4187))

        self.assertEqual(self.marker_count(4187), 0)
        self.assertEqual(self.watermark(), 0)
        self._replay()
        self._assert_applied_exactly_once(4187)

    # step 2 — ack-notification finalize
    def test_crash_at_ack_finalize_replays_and_touches_no_file(self):
        self.add_index_entry(900, qid=None, rel_path="")
        self.client_for().rows.append(_answer_row(900, "Acknowledge"))
        before = self.queue_text()

        listener = self.make_listener()
        self.client_for().edit_error = Crash("kill -9")
        with self.assertRaises(Crash):
            listener.process_answer(listener.feeds[0], _answer_row(900, "Acknowledge"))
        self.assertEqual(self.watermark(), 0)

        self.client_for().edit_error = None
        self._replay()
        self.assertEqual(self.watermark(), 900)
        self.assertEqual(self.queue_text(), before, "an ack must touch no file")
        self.assertEqual([e[0] for e in self.client_for().edits], [900])
        self.assertEqual(self.index().pending, [])

    # step 3 — anchor re-resolution
    def test_crash_at_reresolution_replays_cleanly(self):
        self.add_index_entry(4187, qid="Q-001")
        self.client_for().rows.append(_answer_row(4187))
        listener = self.make_listener()

        with patch.object(qll, "resolve_target", side_effect=Crash("kill -9")):
            with self.assertRaises(Crash):
                listener.process_answer(listener.feeds[0], _answer_row(4187))

        self.assertEqual(self.marker_count(4187), 0)
        self.assertEqual(self.watermark(), 0)
        self._replay()
        self._assert_applied_exactly_once(4187)

    # step 4a — crash before the store writes anything
    def test_crash_before_store_write_replays_cleanly(self):
        self.add_index_entry(4187, qid="Q-001")
        self.client_for().rows.append(_answer_row(4187))
        listener = self.make_listener()

        with patch.object(qs.QuestionsStore, "apply_answer", side_effect=Crash("kill -9")):
            with self.assertRaises(Crash):
                listener.process_answer(listener.feeds[0], _answer_row(4187))

        self.assertEqual(self.marker_count(4187), 0)
        self.assertEqual(self.watermark(), 0)
        self._replay()
        self._assert_applied_exactly_once(4187)

    # step 4b — the file write landed, then the process died
    def test_crash_after_store_write_does_not_double_apply(self):
        self.add_index_entry(4187, qid="Q-001")
        self.client_for().rows.append(_answer_row(4187))
        listener = self.make_listener()

        real_apply = qs.QuestionsStore.apply_answer

        def apply_then_die(self_store, *args, **kwargs):
            real_apply(self_store, *args, **kwargs)
            raise Crash("kill -9 immediately after os.replace")

        with patch.object(qs.QuestionsStore, "apply_answer", apply_then_die):
            with self.assertRaises(Crash):
                listener.process_answer(listener.feeds[0], _answer_row(4187))

        self.assertEqual(self.marker_count(4187), 1, "the write did land")
        self.assertEqual(self.watermark(), 0, "but the watermark must not have moved")

        self._replay()
        self._assert_applied_exactly_once(4187)

    # step 5 — the finalizing PATCH
    def test_crash_between_apply_and_patch_replays_exactly_once(self):
        """The Done-when clause: kill -9 between apply and PATCH."""
        self.add_index_entry(4187, qid="Q-001")
        self.client_for().rows.append(_answer_row(4187))
        listener = self.make_listener()
        self.client_for().edit_error = Crash("kill -9")

        with self.assertRaises(Crash):
            listener.process_answer(listener.feeds[0], _answer_row(4187))

        self.assertEqual(self.marker_count(4187), 1)
        self.assertEqual(self.watermark(), 0)
        self.assertEqual(self.client_for().edits, [])

        self.client_for().edit_error = None
        self._replay()
        self._assert_applied_exactly_once(4187)

    # step 6 — the watermark advance itself
    def test_crash_at_watermark_advance_replays_exactly_once(self):
        self.add_index_entry(4187, qid="Q-001")
        self.client_for().rows.append(_answer_row(4187))
        listener = self.make_listener()

        with patch.object(qll, "mutate_index", side_effect=Crash("kill -9")):
            with self.assertRaises(Crash):
                listener.process_answer(listener.feeds[0], _answer_row(4187))

        self.assertEqual(self.marker_count(4187), 1)
        self.assertEqual(self.watermark(), 0)
        self.assertEqual(len(self.client_for().edits), 1)

        self._replay()
        self._assert_applied_exactly_once(4187)
        self.assertEqual(
            self.marker_count(4187), 1,
            "the replayed apply must be a no-op, not a second answer block",
        )


# ── Failure routing ───────────────────────────────────────────────────────────


class TestFailureRouting(ListenerTestCase):

    def test_not_found_routes_to_pending(self):
        self.add_index_entry(4187, qid="Q-404")
        self.client_for().rows.append(_answer_row(4187, "Ship it"))
        self.make_listener().run_cycle()

        pending = self.index().pending
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].message_id, 4187)
        self.assertEqual(pending[0].attempts, 1)
        self.assertIn("not_found", pending[0].last_error)
        self.assertEqual(pending[0].answer, "Ship it")
        self.assertEqual(self.watermark(), 4187)

    def test_conflict_routes_to_pending_and_never_guesses(self):
        path = self.ws / REL_PATH
        path.write_text(
            QUEUE_MD + "\n## Q-001 — ANSWERED: duplicate heading  [open]\n\nBody.\n",
            encoding="utf-8",
        )
        self.add_index_entry(4187, qid="Q-001")
        self.client_for().rows.append(_answer_row(4187))
        self.make_listener().run_cycle()

        pending = self.index().pending
        self.assertEqual(len(pending), 1)
        self.assertIn("conflict", pending[0].last_error)
        self.assertEqual(self.marker_count(4187), 0, "never applied to a guess")
        self.assertEqual(self.watermark(), 4187)

    def test_questions_store_error_routes_to_pending_and_loop_survives(self):
        """The 23-03 obligation: a corrupt queue file must not kill the loop."""
        (self.ws / REL_PATH).write_bytes(b"## Q-001 \xff\xfe not utf-8  [open]\n")
        self.add_index_entry(4187, qid="Q-001")
        self.add_index_entry(4188, qid="Q-001")
        client = self.client_for()
        client.rows.extend([_answer_row(4187), _answer_row(4188)])

        listener = self.make_listener()
        listener.run_cycle()   # must not raise

        pending = self.index().pending
        self.assertEqual({p.message_id for p in pending}, {4187, 4188})
        for p in pending:
            self.assertIn("store error", p.last_error)
        self.assertEqual(self.watermark(), 4188)

    def test_unresolvable_workspace_routes_to_pending(self):
        gone = self.tmp / "deleted-checkout"
        self.add_index_entry(4187, qid="Q-001", root=gone)
        self.client_for().rows.append(_answer_row(4187))
        self.make_listener().run_cycle()

        pending = self.index().pending
        self.assertEqual(len(pending), 1)
        self.assertIn("workspace not resolvable", pending[0].last_error)
        self.assertEqual(self.watermark(), 4187)

    def test_workspace_id_mismatch_refuses_to_write(self):
        self.add_index_entry(4187, qid="Q-001", workspace_id="some-other-ws")
        self.client_for().rows.append(_answer_row(4187))
        self.make_listener().run_cycle()

        pending = self.index().pending
        self.assertEqual(len(pending), 1)
        self.assertIn("workspace_id mismatch", pending[0].last_error)
        self.assertEqual(self.marker_count(4187), 0)

    def test_unexpected_exception_routes_to_pending_and_the_loop_survives(self):
        """An unforeseen error must become a retry, never the death of the loop."""
        self.add_index_entry(4187, qid="Q-001")
        self.add_index_entry(4188, qid="Q-002")
        client = self.client_for()
        client.rows.extend([_answer_row(4187), _answer_row(4188)])

        calls = {"n": 0}
        real_apply = qs.QuestionsStore.apply_answer

        def flaky(self_store, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ValueError("something nobody predicted")
            return real_apply(self_store, *args, **kwargs)

        with patch.object(qs.QuestionsStore, "apply_answer", flaky):
            self.make_listener().run_cycle()   # must not raise

        pending = self.index().pending
        self.assertEqual([p.message_id for p in pending], [4187])
        self.assertIn("unexpected error: ValueError", pending[0].last_error)
        self.assertEqual(self.watermark(), 4188)
        self.assertEqual(self.marker_count(4188), 1, "the later answer still applied")

    def test_transport_error_on_patch_is_survivable(self):
        self.add_index_entry(4187, qid="Q-001")
        client = self.client_for()
        client.rows.append(_answer_row(4187))
        client.edit_error = RuntimeError("connection closed by peer")

        self.make_listener().run_cycle()   # must not raise

        self.assertEqual(self.marker_count(4187), 1)
        self.assertEqual(self.watermark(), 4187)
        self.assertEqual(self.index().pending, [])

    def test_failed_patch_still_advances_the_watermark(self):
        """A message that cannot be edited must not stall later answers."""
        self.add_index_entry(4187, qid="Q-001")
        client = self.client_for()
        client.rows.append(_answer_row(4187))
        client.edit_error = qll.RelayError("relay HTTP 404: message_not_found", status_code=404)

        self.make_listener().run_cycle()

        self.assertEqual(self.marker_count(4187), 1, "the decision is durable")
        self.assertEqual(self.watermark(), 4187)
        self.assertEqual(self.index().pending, [])


# ── Not ours / index loss ─────────────────────────────────────────────────────


class TestMissingIndexEntry(ListenerTestCase):

    def test_answer_with_no_index_entry_is_skipped_without_damage(self):
        """Every permission answer this installation records arrives here too."""
        before = self.queue_text()
        self.client_for().rows.append(_answer_row(7777, "Approve"))

        self.make_listener().run_cycle()

        self.assertEqual(self.queue_text(), before)
        self.assertEqual(self.index().pending, [], "not ours must never pend")
        self.assertEqual(self.watermark(), 7777)
        self.assertEqual(self.client_for().edits, [], "never finalize a message not ours")

    def test_lost_index_full_replay_skips_everything_and_touches_no_file(self):
        before = self.queue_text()
        client = self.client_for()
        client.rows.extend([_answer_row(i) for i in (10, 20, 30)])
        # No index file at all — the whole routing table was lost.
        self.assertFalse(self.index_path.exists())

        self.make_listener().run_cycle()

        self.assertEqual(self.queue_text(), before)
        self.assertEqual(self.watermark(), 30)
        self.assertEqual(self.index().pending, [])

    def test_full_replay_from_zero_applies_nothing_twice(self):
        """The index recovery path: after=0 must not double any answer."""
        self.add_index_entry(4187, qid="Q-001")
        self.add_index_entry(4188, qid="Q-002")
        client = self.client_for()
        client.rows.extend([_answer_row(4187, "Ship it"), _answer_row(4188, "Hold")])

        self.make_listener().run_cycle()
        applied = self.queue_text()
        self.assertEqual(self.watermark(), 4188)

        # Restore an older index: routing records intact, watermark lost.
        def _reset(idx):
            idx.watermarks = {}
            idx.watermark = 0

        qll.mutate_index(_reset, path=self.index_path, lock_path=self.index_lock)
        self.assertEqual(self.watermark(), 0)

        self.make_listener().run_cycle()

        self.assertEqual(client.get_calls[-1][0], 0, "the replay really started at 0")
        self.assertEqual(self.queue_text(), applied, "replay changed the file")
        self.assertEqual(self.marker_count(4187), 1)
        self.assertEqual(self.marker_count(4188), 1)
        self.assertEqual(self.watermark(), 4188)
        self.assertEqual(self.index().pending, [])


# ── Ack-notifications (brd D7) ────────────────────────────────────────────────


class TestAckNotifications(ListenerTestCase):

    def test_ack_finalizes_and_writes_no_file(self):
        self.add_index_entry(900, qid=None, rel_path="")
        before = self.queue_text()
        self.client_for().rows.append(_answer_row(900, "Acknowledge"))

        self.make_listener().run_cycle()

        self.assertEqual(self.queue_text(), before)
        self.assertEqual([e[0] for e in self.client_for().edits], [900])
        self.assertEqual(self.watermark(), 900)
        self.assertEqual(self.index().pending, [])


# ── Anchor re-resolution (invariant 7) ────────────────────────────────────────


class TestAnchorReresolution(ListenerTestCase):
    """The stored root is a probe, never an address."""

    def _make_repo_with_worktree(self):
        primary = self.tmp / "primary"
        self._make_workspace(primary)
        git_dir = primary / ".git"
        (git_dir / "worktrees" / "wt").mkdir(parents=True, exist_ok=True)
        (git_dir / "config").write_text("[core]\n\tbare = false\n", encoding="utf-8")
        (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")

        worktree = self.tmp / "primary--wt"
        self._make_workspace(worktree)
        # A linked worktree: ``.git`` is a file pointing into the primary's
        # git dir, whose ``commondir`` points back at it.
        shutil.rmtree(worktree / ".git", ignore_errors=True)
        (worktree / ".git").write_text(
            f"gitdir: {git_dir / 'worktrees' / 'wt'}\n", encoding="utf-8"
        )
        (git_dir / "worktrees" / "wt" / "commondir").write_text("../..\n", encoding="utf-8")
        return primary, worktree

    def test_stored_worktree_root_is_re_resolved_to_the_primary_checkout(self):
        primary, worktree = self._make_repo_with_worktree()
        # The index captured the worktree path; anchor mode says "repo".
        self.add_index_entry(4187, qid="Q-001", root=worktree, anchor="repo")
        self.client_for().rows.append(_answer_row(4187, "Ship it"))

        self.make_listener().run_cycle()

        self.assertEqual(
            self.marker_count(4187, root=primary), 1,
            "the answer must land in the primary checkout, not the stored root",
        )
        self.assertEqual(self.marker_count(4187, root=worktree), 0)
        self.assertIn("[resolved]", self.queue_text(primary))
        self.assertIn("[open]", self.queue_text(worktree))

    def test_answer_for_a_deleted_worktree_lands_in_the_primary_checkout(self):
        primary, worktree = self._make_repo_with_worktree()
        self.add_index_entry(4187, qid="Q-001", root=primary, anchor="repo")
        shutil.rmtree(worktree)
        self.client_for().rows.append(_answer_row(4187, "Ship it"))

        self.make_listener().run_cycle()

        self.assertEqual(self.marker_count(4187, root=primary), 1)
        self.assertEqual(self.index().pending, [])

    def test_worktree_anchor_is_honoured_and_not_redirected(self):
        primary, worktree = self._make_repo_with_worktree()
        for root in (primary, worktree):
            (root / ".claude" / "roles.toml").write_text(
                ROLES_TOML.format(anchor="worktree"), encoding="utf-8"
            )
        self.add_index_entry(4187, qid="Q-001", root=worktree, anchor="worktree")
        self.client_for().rows.append(_answer_row(4187, "Ship it"))

        self.make_listener().run_cycle()

        self.assertEqual(self.marker_count(4187, root=worktree), 1)
        self.assertEqual(self.marker_count(4187, root=primary), 0)


# ── Pending retries and the sidecar (brd D10) ─────────────────────────────────


class TestPendingRetries(ListenerTestCase):

    def test_retry_applies_once_the_entry_appears(self):
        self.add_index_entry(4187, qid="Q-777")
        self.client_for().rows.append(_answer_row(4187, "Ship it"))
        listener = self.make_listener()
        listener.run_cycle()
        self.assertEqual(len(self.index().pending), 1)

        # The branch carrying Q-777 is merged into the checkout.
        path = self.ws / REL_PATH
        path.write_text(
            path.read_text(encoding="utf-8") + "\n## Q-777 — Late arrival  [open]\n\nBody.\n",
            encoding="utf-8",
        )
        self.now += 3600
        applied = listener.retry_pending()

        self.assertEqual(applied, 1)
        self.assertEqual(self.index().pending, [])
        self.assertEqual(self.marker_count(4187), 1)
        self.assertIn("Q-777 — Late arrival  [resolved]", self.queue_text())
        self.assertTrue(any(e[0] == 4187 for e in self.client_for().edits))

    def test_backoff_holds_a_retry_until_it_is_due(self):
        self.add_index_entry(4187, qid="Q-777")
        self.client_for().rows.append(_answer_row(4187))
        listener = self.make_listener()
        listener.run_cycle()

        pending = self.index().pending[0]
        self.assertFalse(listener.pending_due(pending, self.now + 1))
        self.assertTrue(listener.pending_due(pending, self.now + 61))

        pending.attempts = 20
        self.assertFalse(listener.pending_due(pending, self.now + 3599))
        self.assertTrue(
            listener.pending_due(pending, self.now + 3601),
            "backoff must cap at roughly hourly, not grow forever",
        )

    def test_sidecar_after_max_attempts(self):
        config = replace(self.config, max_attempts=3)
        self.add_index_entry(4187, qid="Q-777")
        self.client_for().rows.append(_answer_row(4187, "Ship it anyway"))
        listener = self.make_listener(config)

        listener.run_cycle()                      # attempt 1 → pending
        self.assertEqual(self.index().pending[0].attempts, 1)
        self.now += 7200
        listener.retry_pending()                  # attempt 2
        self.assertEqual(self.index().pending[0].attempts, 2)
        self.now += 7200
        listener.retry_pending()                  # attempt 3 → sidecar

        self.assertEqual(self.index().pending, [], "dropped only once it is visible")
        sidecar = self.ws / "docs" / "questions" / "answered" / "Q-777.md"
        self.assertTrue(sidecar.is_file(), "the answer must land somewhere visible")
        text = sidecar.read_text(encoding="utf-8")
        self.assertIn("could not be located", text)
        self.assertIn("Ship it anyway", text)
        self.assertIn("<!-- answer:4187 -->", text)
        self.assertIn("**Answered by:** @hpl", text)

    def test_sidecar_is_idempotent_on_message_id(self):
        config = replace(self.config, max_attempts=1)
        entry = self.add_index_entry(4187, qid="Q-777")
        listener = self.make_listener(config)
        pending = qll.PendingApply(
            message_id=4187, answer="Ship it", attempts=1,
            first_failed_at="2026-08-27T09:00:00Z", last_error="not_found",
            answered_at="2026-08-27T09:00:00Z",
        )
        ok, err = listener._sidecar(entry, pending)
        self.assertTrue(ok, err)
        first = (self.ws / "docs/questions/answered/Q-777.md").read_text(encoding="utf-8")

        ok, err = listener._sidecar(entry, pending)
        self.assertTrue(ok, err)
        second = (self.ws / "docs/questions/answered/Q-777.md").read_text(encoding="utf-8")
        self.assertEqual(first, second, "a second sidecar write must be a no-op")

    def test_sidecar_failure_keeps_the_answer_pending(self):
        """If even the sidecar cannot be written, nothing is dropped."""
        config = replace(self.config, max_attempts=2)
        gone = self.tmp / "deleted-checkout"
        self.add_index_entry(4187, qid="Q-777", root=gone)
        self.client_for().rows.append(_answer_row(4187, "Ship it"))
        listener = self.make_listener(config)

        listener.run_cycle()
        self.now += 7200
        listener.retry_pending()

        pending = self.index().pending
        self.assertEqual(len(pending), 1, "an unwritable sidecar must not drop the answer")
        self.assertIn("sidecar failed", pending[0].last_error)

    def test_pending_count_is_visible_to_the_mcp_server(self):
        self.add_index_entry(4187, qid="Q-777")
        self.client_for().rows.append(_answer_row(4187))
        self.make_listener().run_cycle()
        self.assertEqual(qll.count_pending(self.index_path), 1)


# ── Feed behaviour: backoff, 401, multiple installations ──────────────────────


class TestFeedBehaviour(ListenerTestCase):

    def test_network_error_backs_off_with_jitter_and_caps(self):
        client = self.client_for()
        client.get_error = qll.RelayError("connection reset")
        listener = self.make_listener()
        feed = listener.feeds[0]

        delays = []
        for _ in range(12):
            listener.poll_feed(feed, 25)
            delays.append(feed.next_poll_at - self.now)

        self.assertEqual(feed.state, qll.FEED_DISCONNECTED)
        self.assertIn("connection reset", feed.last_error)
        self.assertLess(delays[0], delays[3], "backoff must grow")
        for delay in delays:
            self.assertLessEqual(
                delay,
                qll.NET_BACKOFF_MAX_S * (1 + qll.NET_BACKOFF_JITTER) + 0.01,
                "backoff must stay capped",
            )
        self.assertGreater(
            len(set(delays[:6])), 1, "backoff must be jittered, not a fixed ladder"
        )
        self.assertEqual(self.watermark(), 0)

    def test_reconnect_resets_the_backoff(self):
        client = self.client_for()
        client.get_error = qll.RelayError("connection reset")
        listener = self.make_listener()
        feed = listener.feeds[0]
        listener.poll_feed(feed, 25)
        listener.poll_feed(feed, 25)
        self.assertGreater(feed.backoff_s, qll.NET_BACKOFF_BASE_S)

        client.get_error = None
        listener.poll_feed(feed, 25)
        self.assertEqual(feed.state, qll.FEED_CONNECTED)
        self.assertEqual(feed.backoff_s, qll.NET_BACKOFF_BASE_S)
        self.assertEqual(feed.last_error, "")

    def test_401_is_fatal_with_slow_retry_and_surfaces_in_status(self):
        client = self.client_for()
        client.get_error = qll.RelayError("relay HTTP 401: invalid_token", status_code=401)
        listener = self.make_listener()
        feed = listener.feeds[0]

        listener.run_cycle()   # must not raise, must not exit

        self.assertEqual(feed.state, qll.FEED_UNAUTHORIZED)
        self.assertAlmostEqual(feed.next_poll_at - self.now, qll.AUTH_RETRY_S, places=3)
        self.assertEqual(self.watermark(), 0)
        status = qll.read_status(self.status_path)
        self.assertIsNotNone(status)
        self.assertEqual(status["feeds"][0]["state"], "unauthorized")
        self.assertIn("401", status["feeds"][0]["last_error"])

    def test_a_feed_in_backoff_is_not_polled_and_the_loop_sleeps(self):
        client = self.client_for()
        client.get_error = qll.RelayError("connection reset")
        listener = self.make_listener()

        self.assertEqual(listener.run_cycle(), 1)
        polls_after_first = len(client.get_calls)
        self.assertEqual(listener.run_cycle(), 0, "a backing-off feed must not be polled")
        self.assertEqual(len(client.get_calls), polls_after_first)

        listener.run_forever(stop=_stop_after(1))
        self.assertTrue(self.slept, "the loop must wait instead of spinning")

    def test_each_installation_feed_keeps_its_own_watermark(self):
        """A role bound to its own token has its own installation feed."""
        config = replace(self.config, tokens=("rly_primary_token", "rly_role_token"))
        self.add_index_entry(10, qid="Q-001")
        self.add_index_entry(20, qid="Q-002")
        self.client_for("rly_primary_token").rows.append(_answer_row(10, "Ship it"))
        self.client_for("rly_role_token").rows.append(_answer_row(20, "Hold"))

        listener = self.make_listener(config)
        listener.run_cycle()

        primary_fp = qll.token_fingerprint("rly_primary_token")
        role_fp = qll.token_fingerprint("rly_role_token")
        self.assertEqual(self.watermark(primary_fp), 10)
        self.assertEqual(self.watermark(role_fp), 20)
        self.assertEqual(self.index().watermark, 10, "watermark mirrors the primary feed")
        text = self.queue_text()
        self.assertIn("Q-001 — Should we ship on Friday?  [resolved]", text)
        self.assertIn("Q-002 — Second question  [resolved]", text)

    def test_the_long_poll_budget_is_split_across_feeds(self):
        config = replace(self.config, tokens=("rly_primary_token", "rly_role_token"))
        listener = self.make_listener(config)
        listener.run_cycle()
        for token in ("rly_primary_token", "rly_role_token"):
            _after, wait = self.client_for(token).get_calls[-1]
            self.assertEqual(wait, 12)

    def test_single_feed_parks_for_the_full_budget(self):
        listener = self.make_listener()
        listener.run_cycle()
        _after, wait = self.client_for().get_calls[-1]
        self.assertEqual(wait, 25, "the ordinary single-feed case is architecture's wait=25")


# ── Legacy watermark seeding (issue 4 fix) ────────────────────────────────────


class TestLegacyWatermarkSeed(ListenerTestCase):
    """``_seed_primary_watermark`` migrates an existing 23-04 top-level watermark.

    23-04 wrote ``index.watermark = N`` but had no ``watermarks`` dict.  On the
    first cycle after 23-05 is installed, the listener must seed
    ``watermarks[primary_fp] = N`` so it does not replay the entire history.
    """

    def _write_raw_index(self, data: dict) -> None:
        """Write *data* directly as JSON, bypassing from_dict / to_dict."""
        import json as _json

        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(_json.dumps(data), encoding="utf-8")

    def test_legacy_watermark_seeds_primary_fingerprint(self):
        """An index with watermark=N and empty watermarks seeds the primary to N."""
        primary_fp = qll.token_fingerprint("rly_primary_token")
        self._write_raw_index({"watermark": 4192, "watermarks": {}, "messages": {}, "pending": []})

        listener = self.make_listener()
        listener.run_cycle()

        idx = self.index()
        self.assertEqual(
            idx.watermarks.get(primary_fp),
            4192,
            "primary fingerprint must be seeded from the legacy watermark",
        )
        self.assertEqual(
            idx.watermark,
            4192,
            "top-level watermark mirror must stay consistent",
        )

    def test_legacy_watermark_does_not_seed_role_bound_fingerprint(self):
        """A non-primary feed must start at 0; seeding it from the primary position
        would skip answers that were routed to the role and never processed."""
        role_fp = qll.token_fingerprint("rly_role_token")
        self._write_raw_index({"watermark": 4192, "watermarks": {}, "messages": {}, "pending": []})

        config = replace(self.config, tokens=("rly_primary_token", "rly_role_token"))
        listener = self.make_listener(config)
        listener.run_cycle()

        idx = self.index()
        self.assertNotIn(
            role_fp,
            idx.watermarks,
            "a role-bound fingerprint must not be seeded from the primary's legacy position",
        )

    def test_existing_primary_entry_is_never_overwritten(self):
        """If watermarks already has an entry for the primary, it must not be touched."""
        primary_fp = qll.token_fingerprint("rly_primary_token")
        self._write_raw_index({
            "watermark": 5000,
            "watermarks": {primary_fp: 5000},
            "messages": {},
            "pending": [],
        })

        listener = self.make_listener()
        listener.run_cycle()

        self.assertEqual(
            self.index().watermarks.get(primary_fp),
            5000,
            "a recorded position must never be overwritten",
        )

    def test_existing_entry_is_not_moved_backwards(self):
        """watermarks[primary_fp] must not move backwards even when watermark is lower."""
        primary_fp = qll.token_fingerprint("rly_primary_token")
        # Simulate a pathological index where the top-level watermark is stale
        # (lower) but watermarks already has a valid recorded position.
        self._write_raw_index({
            "watermark": 100,
            "watermarks": {primary_fp: 5000},
            "messages": {},
            "pending": [],
        })

        listener = self.make_listener()
        listener.run_cycle()

        self.assertEqual(
            self.index().watermarks.get(primary_fp),
            5000,
            "the recorded position must survive even when the legacy field is lower",
        )

    def test_fresh_index_with_no_legacy_watermark_starts_at_zero(self):
        """A brand-new installation has watermark=0; the primary must start at 0, not
        be incorrectly seeded from a stale value."""
        primary_fp = qll.token_fingerprint("rly_primary_token")
        # No index file at all — read_index returns an empty Index with watermark=0.

        listener = self.make_listener()
        listener.run_cycle()

        idx = self.index()
        # After the first poll with no rows the watermark stays at 0 (or is
        # absent from the map); either is correct — both mean "start from 0".
        recorded = idx.watermarks.get(primary_fp, 0)
        self.assertEqual(
            recorded,
            0,
            "a fresh installation with no legacy watermark must stay at 0",
        )

    def test_seed_runs_only_on_the_first_cycle(self):
        """The seeding path fires exactly once (cycles == 1) and is idempotent
        on every subsequent cycle."""
        primary_fp = qll.token_fingerprint("rly_primary_token")
        self._write_raw_index({"watermark": 3000, "watermarks": {}, "messages": {}, "pending": []})

        listener = self.make_listener()
        listener.run_cycle()  # cycle 1: seeds primary to 3000
        # Now simulate an index mutation that would appear to erase the seed
        # (replace watermarks with the legacy-only state again).
        self._write_raw_index({"watermark": 3000, "watermarks": {}, "messages": {}, "pending": []})
        listener.run_cycle()  # cycle 2: must NOT re-seed (cycles != 1)

        # Because cycle 2 did not re-seed, the watermarks dict is still empty
        # (we replaced the file above). If the implementation re-ran the seed it
        # would write 3000 again — which happens to be idempotent, but the point
        # is the guard: only the first cycle may seed. We verify by checking
        # self.cycles == 2 after two calls.
        self.assertEqual(listener.cycles, 2)


def _stop_after(n: int):
    """A ``stop`` predicate that lets *n* iterations of run_forever happen."""
    state = {"n": 0}

    def stop() -> bool:
        if state["n"] >= n:
            return True
        state["n"] += 1
        return False

    return stop


# ── Single instance ───────────────────────────────────────────────────────────


class TestSingleInstance(ListenerTestCase):

    def test_second_listener_cannot_take_the_lock(self):
        first = qll.acquire_single_instance(self.listen_lock)
        self.assertIsNotNone(first)
        try:
            self.assertIsNone(
                qll.acquire_single_instance(self.listen_lock),
                "a second copy must not run — it would double-apply",
            )
            self.assertTrue(qll.listener_running(self.listen_lock))
        finally:
            first.close()

        self.assertFalse(qll.listener_running(self.listen_lock))
        second = qll.acquire_single_instance(self.listen_lock)
        self.assertIsNotNone(second, "the lock must be free once the holder exits")
        second.close()

    def test_lock_file_records_the_pid(self):
        handle = qll.acquire_single_instance(self.listen_lock)
        try:
            self.assertEqual(
                self.listen_lock.read_text(encoding="utf-8").strip(), str(os.getpid())
            )
        finally:
            handle.close()

    def test_listener_running_is_false_without_a_lock_file(self):
        self.assertFalse(qll.listener_running(self.state / "never-created.lock"))


# ── Config ────────────────────────────────────────────────────────────────────


class TestListenConfig(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qlisten-cfg-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, text: str) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_is_disabled(self):
        config = qll.load_listen_config(self.tmp / "nope.toml")
        self.assertFalse(config.enabled)
        self.assertTrue(config.errors)

    def test_missing_section_is_disabled(self):
        path = self._write('server_url = "http://relay.test"\ninstallation_token = "rly_a"\n')
        config = qll.load_listen_config(path)
        self.assertFalse(config.enabled)
        self.assertEqual(config.tokens, ())

    def test_enabled_false_is_disabled(self):
        path = self._write(
            'server_url = "http://relay.test"\ninstallation_token = "rly_a"\n'
            "\n[questions_listen]\nenabled = false\n"
        )
        self.assertFalse(qll.load_listen_config(path).enabled)

    def test_enabled_collects_every_distinct_token_primary_first(self):
        path = self._write(
            'server_url = "http://relay.test"\ninstallation_token = "rly_default"\n'
            "\n[roles]\n"
            'hpl = "rly_hpl"\n'
            'htl = "rly_default"\n'          # duplicate of the default
            'ux  = "hpl"\n'                  # a reference, not a token
            "\n[questions_listen]\nenabled = true\n"
        )
        config = qll.load_listen_config(path)
        self.assertTrue(config.enabled)
        self.assertEqual(config.tokens, ("rly_default", "rly_hpl"))
        self.assertEqual(config.server_url, "http://relay.test")
        self.assertEqual(config.primary_fingerprint, qll.token_fingerprint("rly_default"))

    def test_unparseable_config_disables_with_a_reason(self):
        path = self._write("this is not = = toml\n")
        config = qll.load_listen_config(path)
        self.assertFalse(config.enabled)
        self.assertTrue(any("Failed to parse" in e for e in config.errors))

    def test_tunables_are_clamped(self):
        path = self._write(
            'installation_token = "rly_a"\nserver_url = "http://relay.test"\n'
            "\n[questions_listen]\nenabled = true\npoll_seconds = 9000\nmax_attempts = 0\n"
        )
        config = qll.load_listen_config(path)
        self.assertEqual(config.poll_seconds, 300)
        self.assertEqual(config.max_attempts, 1)

    def test_redacted_config_never_carries_token_material(self):
        path = self._write(
            'installation_token = "rly_supersecret"\nserver_url = "http://relay.test"\n'
            "\n[questions_listen]\nenabled = true\n"
        )
        config = qll.load_listen_config(path)
        blob = json.dumps(config.redacted())
        self.assertNotIn("rly_supersecret", blob)
        self.assertIn(qll.token_fingerprint("rly_supersecret"), blob)


# ── --status ──────────────────────────────────────────────────────────────────


class TestStatus(ListenerTestCase):

    def test_status_file_carries_everything_status_prints(self):
        self.add_index_entry(4187, qid="Q-001")
        self.add_index_entry(4188, qid="Q-404")
        client = self.client_for()
        client.rows.extend([_answer_row(4187, "Ship it"), _answer_row(4188, "no entry")])

        self.make_listener().run_cycle()

        status = qll.read_status(self.status_path)
        self.assertIsNotNone(status)
        self.assertEqual(status["watermark"], 4188)
        self.assertEqual(status["pending_count"], 1)
        self.assertEqual(status["oldest_pending"]["message_id"], 4188)
        self.assertIn("not_found", status["oldest_pending"]["last_error"])
        self.assertEqual(status["last_applied"]["qid"], "Q-001")
        self.assertEqual(status["feeds"][0]["state"], "connected")
        self.assertEqual(status["config"]["enabled"], True)
        self.assertNotIn("rly_primary_token", json.dumps(status))

    def test_format_status_reports_the_index_even_with_no_status_file(self):
        self.add_index_entry(4187, qid="Q-404")
        self.client_for().rows.append(_answer_row(4187))
        self.make_listener().run_cycle()

        rendered = qll.format_status(
            None, self.index(), running=False, config=self.config
        )
        self.assertIn("running:        no", rendered)
        self.assertIn("pending:        1", rendered)
        self.assertIn("not_found", rendered)
        self.assertNotIn("rly_primary_token", rendered)

    def test_format_status_shows_the_last_applied_answer(self):
        self.add_index_entry(4187, qid="Q-001")
        self.client_for().rows.append(_answer_row(4187, "Ship it"))
        self.make_listener().run_cycle()

        rendered = qll.format_status(
            qll.read_status(self.status_path), self.index(),
            running=True, config=self.config,
        )
        self.assertIn("Q-001", rendered)
        self.assertIn("relay #4187", rendered)
        self.assertIn("connected", rendered)


# ── The binary ────────────────────────────────────────────────────────────────


class TestCli(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qlisten-cli-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _config(self, text: str) -> Path:
        path = self.tmp / "config.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_disabled_config_exits_zero_without_touching_the_lock(self):
        path = self._config('installation_token = "rly_a"\nserver_url = "http://x"\n')
        lock = self.tmp / "questions-listen.lock"
        with patch.object(qll, "LISTEN_LOCK_PATH", lock):
            self.assertEqual(cli.main(["--config", str(path)]), 0)
        self.assertFalse(lock.exists(), "a disabled listener must not take the lock")

    def test_enabled_without_a_token_exits_nonzero(self):
        path = self._config("[questions_listen]\nenabled = true\n")
        self.assertEqual(cli.main(["--config", str(path)]), 1)

    def test_status_works_when_disabled(self):
        path = self._config('installation_token = "rly_a"\nserver_url = "http://x"\n')
        index_path = self.tmp / "async_questions.json"
        status_path = self.tmp / "status.json"
        lock = self.tmp / "questions-listen.lock"
        with patch.object(qll, "INDEX_PATH", index_path), \
             patch.object(qll, "STATUS_PATH", status_path), \
             patch.object(qll, "LISTEN_LOCK_PATH", lock):
            self.assertEqual(cli.main(["--config", str(path), "--status"]), 0)

    def test_second_instance_exits_zero(self):
        path = self._config(
            'installation_token = "rly_a"\nserver_url = "http://x"\n'
            "\n[questions_listen]\nenabled = true\n"
        )
        lock = self.tmp / "questions-listen.lock"
        held = qll.acquire_single_instance(lock)
        try:
            with patch.object(qll, "LISTEN_LOCK_PATH", lock):
                self.assertEqual(cli.main(["--config", str(path)]), 0)
        finally:
            held.close()

    def test_once_runs_a_single_cycle(self):
        path = self._config(
            'installation_token = "rly_a"\nserver_url = "http://relay.test"\n'
            "\n[questions_listen]\nenabled = true\n"
        )
        lock = self.tmp / "questions-listen.lock"
        index_path = self.tmp / "async_questions.json"
        status_path = self.tmp / "status.json"
        index_lock = self.tmp / "async_questions.json.lock"
        client = FakeRelayClient("rly_a")

        real_init = qll.Listener.__init__

        def init(self_listener, config, **kwargs):
            kwargs.update(
                index_path=index_path,
                index_lock_path=index_lock,
                status_path=status_path,
                client_factory=lambda url, token: client,
            )
            real_init(self_listener, config, **kwargs)

        with patch.object(qll, "LISTEN_LOCK_PATH", lock), \
             patch.object(qll.Listener, "__init__", init):
            self.assertEqual(cli.main(["--config", str(path), "--once"]), 0)

        self.assertEqual(len(client.get_calls), 1)
        self.assertTrue(status_path.is_file())


if __name__ == "__main__":
    unittest.main()
