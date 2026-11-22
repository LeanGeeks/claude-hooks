#!/usr/bin/env python3
"""Unit tests for the questions MCP server (epic 23, task 23-04).

Covers the agent-facing surface:
- write-before-send ordering (invariant 1): simulated crash between write and
  send leaves an entry, never an orphan message
- role refusal before any write (unresolvable token)
- relay-failure return shape (dispatched: false with id)
- option → keyboard mapping
- index round-trip with the listener's reader (questions_listen_lib)
- per-call workspace resolution with two workspaces in one server process
- notify with and without ack
- max_open and min_interval_s caps (brd D12)
- missing [questions] section → clear actionable error (invariant 9)
- QuestionsStoreError surfaces without crashing the tool

Every case runs against scratch settings dirs and temp queue files; nothing
here touches the developer's real ~/.claude, the network, or a real bot.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Path setup ────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / ".claude" / "hooks"))
sys.path.insert(0, str(_REPO_ROOT / "questions-mcp"))

# Isolate the permission state store before any hook imports it.
_ISOLATED = Path(tempfile.mkdtemp(prefix="qs-mcp-test-"))
os.environ.setdefault("CLAUDE_PERMISSION_STATE_FILE", str(_ISOLATED / "permission_requests.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_AUDIT_FILE", str(_ISOLATED / "permission_actions.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_DEBUG_LOG", str(_ISOLATED / "permission_state_debug.log"))

# Pin CLAUDE_PROJECT_DIR to an empty temp dir so roles_config.find_roles_file
# never walks the developer's real tree at import time.
_ISOLATION_DIR = str(_ISOLATED)
_ORIG_PROJECT_DIR = os.environ.get("CLAUDE_PROJECT_DIR")
os.environ["CLAUDE_PROJECT_DIR"] = _ISOLATION_DIR

import questions_mcp_lib as lib  # noqa: E402
import questions_listen_lib as qll  # noqa: E402
import questions_store as qs  # noqa: E402
import roles_config  # noqa: E402


def tearDownModule():
    if _ORIG_PROJECT_DIR is None:
        os.environ.pop("CLAUDE_PROJECT_DIR", None)
    else:
        os.environ["CLAUDE_PROJECT_DIR"] = _ORIG_PROJECT_DIR
    shutil.rmtree(_ISOLATED, ignore_errors=True)


# ── Common fixtures ───────────────────────────────────────────────────────────

ROLES_TOML_HPL = """\
workspace_id = "test-ws"
default      = "hpl"

[role.hpl]
title = "Product lead"

[role.htl]
title = "Tech lead"

[questions]
dir            = "docs/questions"
anchor         = "worktree"
nudge          = "4h,1d,3d,7d*"
escalate_after = "24h"

[questions.queue]
hpl = "for-product-lead.md"
htl = "for-tech-lead.md"
"""

ROLES_TOML_NO_QUESTIONS = """\
workspace_id = "no-q-ws"
default      = "hpl"

[role.hpl]
title = "Product lead"
"""

# Config bindings fixture: two roles bound to dummy tokens.
CONFIG_TOML = """\
server_url          = "http://relay.test"
installation_token  = "rly_default_token"

[roles]
hpl = "rly_hpl_token"
htl = "rly_htl_token"
"""


class _WorkspaceFixture:
    """Build a throwaway workspace with roles.toml + optional config.toml."""

    def __init__(self, roles_toml: str = ROLES_TOML_HPL):
        self.tmp = Path(tempfile.mkdtemp(prefix="qs-mcp-ws-"))
        (self.tmp / ".claude").mkdir(parents=True, exist_ok=True)
        self.roles_path = self.tmp / ".claude" / "roles.toml"
        self.roles_path.write_text(roles_toml, encoding="utf-8")
        self.q_dir = self.tmp / "docs" / "questions"
        self.q_dir.mkdir(parents=True, exist_ok=True)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def workspace_dir(self) -> str:
        return str(self.tmp)


def _make_config_toml(tmp: Path, text: str = CONFIG_TOML) -> Path:
    p = tmp / "config.toml"
    p.write_text(text, encoding="utf-8")
    return p


def _make_fake_bindings(
    server_url: str = "http://relay.test",
    default_token: str = "rly_default_token",
    roles: dict | None = None,
) -> roles_config.Bindings:
    return roles_config.Bindings(
        server_url=server_url,
        default_token=default_token,
        roles=roles or {"hpl": "rly_hpl_token", "htl": "rly_htl_token"},
        workspace_roles={},
        escalate_after={},
        workspace_escalate_after={},
        errors=(),
    )


def _make_fake_handle(message_id: int = 999) -> MagicMock:
    h = MagicMock()
    h.message_id = message_id
    h.telegram_message_id = message_id
    return h


# ── Base test case ────────────────────────────────────────────────────────────


class QuestionsLibTestCase(unittest.TestCase):
    """Scratch workspace + patched relay client so no network is hit."""

    def setUp(self):
        self.ws = _WorkspaceFixture()
        self.index_tmp = Path(tempfile.mkdtemp(prefix="qs-idx-"))
        self.index_path = self.index_tmp / "async_questions.json"
        self.index_lock = self.index_tmp / "async_questions.json.lock"

        # Clear the lib's per-workspace cache between tests.
        with lib._ws_lock:
            lib._ws_cache.clear()
        # Clear rate-limit state
        with lib._rate_lock:
            lib._last_ask_time.clear()
        # Clear relay client registry
        with lib._clients_lock:
            lib._clients.clear()

    def tearDown(self):
        self.ws.cleanup()
        shutil.rmtree(self.index_tmp, ignore_errors=True)
        with lib._ws_lock:
            lib._ws_cache.clear()
        with lib._rate_lock:
            lib._last_ask_time.clear()
        with lib._clients_lock:
            lib._clients.clear()

    def _call_ask(
        self,
        *,
        title: str = "Is the approach correct?",
        body: str = "Details.",
        options: list | None = None,
        role: str | None = None,
        tags: list | None = None,
        message_id: int = 999,
        ws_dir: str | None = None,
        bindings: roles_config.Bindings | None = None,
        index_path: Path | None = None,
        relay_raises: Exception | None = None,
    ) -> dict:
        """Patch env + relay, call lib.ask(), return parsed result."""
        ws = ws_dir or self.ws.workspace_dir
        idx = index_path or self.index_path
        lck = self.index_lock

        fake_handle = _make_fake_handle(message_id)
        fake_bindings = bindings or _make_fake_bindings()

        def fake_client_ctor(server_url, token):
            c = MagicMock()
            if relay_raises is not None:
                c.send_message.side_effect = relay_raises
            else:
                c.send_message.return_value = fake_handle
            return c

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": ws}), \
             patch.object(roles_config, "load_bindings", return_value=fake_bindings), \
             patch.object(lib, "_client", side_effect=lambda token: fake_client_ctor(None, token)), \
             patch.object(qll, "INDEX_PATH", idx), \
             patch.object(qll, "_LOCK_PATH", lck):
            return lib.ask(title=title, body=body, options=options, role=role, tags=tags)

    def _call_notify(
        self,
        *,
        text: str = "Build complete.",
        role: str | None = None,
        ack: bool = True,
        message_id: int = 888,
        ws_dir: str | None = None,
        bindings: roles_config.Bindings | None = None,
        index_path: Path | None = None,
        relay_raises: Exception | None = None,
    ) -> dict:
        ws = ws_dir or self.ws.workspace_dir
        idx = index_path or self.index_path
        lck = self.index_lock

        fake_handle = _make_fake_handle(message_id)
        fake_bindings = bindings or _make_fake_bindings()

        def fake_client_factory(token):
            c = MagicMock()
            if relay_raises is not None:
                c.send_message.side_effect = relay_raises
            else:
                c.send_message.return_value = fake_handle
            return c

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": ws}), \
             patch.object(roles_config, "load_bindings", return_value=fake_bindings), \
             patch.object(lib, "_client", side_effect=fake_client_factory), \
             patch.object(qll, "INDEX_PATH", idx), \
             patch.object(qll, "_LOCK_PATH", lck):
            return lib.notify(text=text, role=role, ack=ack)


# ── Test: missing [questions] section ─────────────────────────────────────────


class TestMissingQuestionsSection(QuestionsLibTestCase):
    """A workspace with no [questions] section gets a clear, actionable error."""

    def setUp(self):
        super().setUp()
        self.ws = _WorkspaceFixture(ROLES_TOML_NO_QUESTIONS)
        with lib._ws_lock:
            lib._ws_cache.clear()

    def test_ask_returns_clear_error(self):
        result = self._call_ask()
        self.assertIn("error", result)
        self.assertIn("[questions]", result["error"])
        self.assertNotIn("id", result)

    def test_no_file_written(self):
        self._call_ask()
        # No entry should exist in the queue directory.
        q_dir = self.ws.tmp / "docs" / "questions"
        md_files = list(q_dir.glob("*.md"))
        self.assertEqual(md_files, [])


# ── Test: role refusal before write ───────────────────────────────────────────


class TestRoleRefusal(QuestionsLibTestCase):
    """Unresolvable role (no token) is refused before anything is written."""

    def test_no_token_returns_error_no_file(self):
        # Bindings with NO token for any role → token=None for all destinations
        no_token_bindings = roles_config.Bindings(
            server_url="http://relay.test",
            default_token=None,
            roles={},
            workspace_roles={},
            escalate_after={},
            workspace_escalate_after={},
            errors=(),
        )
        result = self._call_ask(bindings=no_token_bindings)
        self.assertIn("error", result)
        self.assertNotIn("id", result)
        q_dir = self.ws.tmp / "docs" / "questions"
        self.assertEqual(list(q_dir.glob("*.md")), [])

    def test_chat_only_role_refused(self):
        """A role in the catalog but absent from [questions.queue] is refused."""
        # 'operator' is in the catalog but not in [questions.queue]
        roles_toml = ROLES_TOML_HPL + "\n[role.operator]\ntitle = \"Operator\"\n"
        ws2 = _WorkspaceFixture(roles_toml)
        self.addCleanup(ws2.cleanup)
        result = self._call_ask(role="operator", ws_dir=ws2.workspace_dir)
        self.assertIn("error", result)
        self.assertNotIn("id", result)


# ── Test: write-before-send ordering (invariant 1) ────────────────────────────


class TestWriteBeforeSend(QuestionsLibTestCase):
    """The file write must precede the relay call.

    A crash between write and send (simulated by relay raising) leaves an
    entry in the queue file.  A crash before the write (unreachable) leaves
    nothing.  There is never an orphan Telegram message with no queue entry.
    """

    def test_relay_failure_leaves_entry_returns_dispatched_false(self):
        """Relay failure returns dispatched: false WITH the id; entry exists."""
        # Simulate relay raising on send
        from relay_server.client import RelayError  # type: ignore

        result = self._call_ask(
            title="Write-before-send test",
            relay_raises=RelayError("connection refused"),
        )
        # id must be returned even on failure
        self.assertIn("id", result)
        self.assertFalse(result["dispatched"])
        self.assertIsNone(result["message_id"])
        self.assertIn("error", result)

        # The entry must exist in the queue file
        qfile = self.ws.tmp / "docs" / "questions" / "for-product-lead.md"
        self.assertTrue(qfile.exists(), "queue file was not created")
        text = qfile.read_text(encoding="utf-8")
        self.assertIn(result["id"], text)

    def test_entry_exists_before_relay_is_called(self):
        """Verify file write precedes relay call by checking entry existence
        from inside the patched send_message (the write must have happened)."""
        qfile = self.ws.tmp / "docs" / "questions" / "for-product-lead.md"
        entry_seen_during_send = []

        def fake_send(*args, **kwargs):
            # At this point, the queue file must already exist with the entry.
            if qfile.exists():
                entry_seen_during_send.append(qfile.read_text(encoding="utf-8"))
            handle = MagicMock()
            handle.message_id = 42
            handle.telegram_message_id = 42
            return handle

        fake_bindings = _make_fake_bindings()
        fake_client = MagicMock()
        fake_client.send_message.side_effect = fake_send

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(roles_config, "load_bindings", return_value=fake_bindings), \
             patch.object(lib, "_client", return_value=fake_client), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            with lib._ws_lock:
                lib._ws_cache.clear()
            result = lib.ask(title="ordering test")

        self.assertTrue(result.get("dispatched"), f"expected dispatched, got: {result}")
        self.assertEqual(len(entry_seen_during_send), 1)
        self.assertIn("ordering test", entry_seen_during_send[0])


# ── Test: successful ask — return shape ───────────────────────────────────────


class TestAskSuccess(QuestionsLibTestCase):

    def test_returns_all_required_fields(self):
        result = self._call_ask(title="MVP auth model", message_id=101)
        self.assertIn("id", result)
        self.assertIn("file", result)
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["message_id"], 101)
        self.assertIn("pending_answers", result)

    def test_id_starts_with_q(self):
        result = self._call_ask(title="first question", message_id=200)
        self.assertTrue(result["id"].startswith("Q-"), result["id"])

    def test_entry_written_to_queue_file(self):
        result = self._call_ask(title="Will this work?", message_id=300)
        qfile = self.ws.tmp / "docs" / "questions" / "for-product-lead.md"
        self.assertTrue(qfile.exists())
        text = qfile.read_text(encoding="utf-8")
        self.assertIn(result["id"], text)
        self.assertIn("Will this work?", text)

    def test_dispatched_marker_written(self):
        result = self._call_ask(title="Marker test", message_id=401)
        qfile = self.ws.tmp / "docs" / "questions" / "for-product-lead.md"
        text = qfile.read_text(encoding="utf-8")
        self.assertIn("**Dispatched:**", text)
        self.assertIn("relay #401", text)

    def test_options_in_queue_file(self):
        result = self._call_ask(
            title="Choice", options=["Option A", "Option B"], message_id=501
        )
        qfile = self.ws.tmp / "docs" / "questions" / "for-product-lead.md"
        text = qfile.read_text(encoding="utf-8")
        self.assertIn("Option A", text)
        self.assertIn("Option B", text)


# ── Test: option → keyboard mapping ───────────────────────────────────────────


class TestKeyboardMapping(QuestionsLibTestCase):
    """Each option produces one button row with the correct value."""

    def test_two_options_two_button_rows(self):
        captured_calls = []

        def fake_send(*args, **kwargs):
            captured_calls.append(kwargs)
            h = MagicMock()
            h.message_id = 600
            h.telegram_message_id = 600
            return h

        fake_bindings = _make_fake_bindings()
        fake_client = MagicMock()
        fake_client.send_message.side_effect = fake_send

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(roles_config, "load_bindings", return_value=fake_bindings), \
             patch.object(lib, "_client", return_value=fake_client), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            with lib._ws_lock:
                lib._ws_cache.clear()
            lib.ask(title="Multi-option", options=["Alpha", "Beta"])

        self.assertEqual(len(captured_calls), 1)
        keyboard = captured_calls[0].get("keyboard")
        self.assertIsNotNone(keyboard)
        self.assertEqual(len(keyboard), 2)
        self.assertEqual(keyboard[0][0]["value"], "qa0")
        self.assertEqual(keyboard[1][0]["value"], "qa1")
        self.assertIn("Alpha", keyboard[0][0]["label"])
        self.assertIn("Beta", keyboard[1][0]["label"])

    def test_no_options_no_keyboard(self):
        captured_calls = []

        def fake_send(*args, **kwargs):
            captured_calls.append(kwargs)
            h = MagicMock()
            h.message_id = 700
            h.telegram_message_id = 700
            return h

        fake_bindings = _make_fake_bindings()
        fake_client = MagicMock()
        fake_client.send_message.side_effect = fake_send

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(roles_config, "load_bindings", return_value=fake_bindings), \
             patch.object(lib, "_client", return_value=fake_client), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            with lib._ws_lock:
                lib._ws_cache.clear()
            lib.ask(title="Free text question")

        self.assertEqual(len(captured_calls), 1)
        # Keyboard should be None (no options = no buttons)
        keyboard = captured_calls[0].get("keyboard")
        self.assertIsNone(keyboard)

    def test_never_expires_set_in_relay_call(self):
        """ask always passes never_expires=True to the relay."""
        captured_calls = []

        def fake_send(*args, **kwargs):
            captured_calls.append(kwargs)
            h = MagicMock()
            h.message_id = 800
            h.telegram_message_id = 800
            return h

        fake_bindings = _make_fake_bindings()
        fake_client = MagicMock()
        fake_client.send_message.side_effect = fake_send

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(roles_config, "load_bindings", return_value=fake_bindings), \
             patch.object(lib, "_client", return_value=fake_client), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            with lib._ws_lock:
                lib._ws_cache.clear()
            lib.ask(title="Never-expires test")

        self.assertTrue(captured_calls[0].get("never_expires", False))


# ── Test: index round-trip ────────────────────────────────────────────────────


class TestIndexRoundTrip(QuestionsLibTestCase):
    """Index is written by ask and read back correctly by questions_listen_lib."""

    def test_ask_writes_index_entry(self):
        result = self._call_ask(title="Index test", message_id=1001)
        self.assertTrue(result["dispatched"])

        idx = qll.read_index(self.index_path)
        self.assertIn("1001", idx.messages)
        entry = idx.messages["1001"]
        self.assertEqual(entry.workspace_id, "test-ws")
        self.assertEqual(entry.qid, result["id"])
        self.assertIsNotNone(entry.root)

    def test_index_entry_shape(self):
        result = self._call_ask(title="Shape test", message_id=1002)
        idx = qll.read_index(self.index_path)
        entry = idx.messages["1002"]
        self.assertIn(entry.anchor, ("repo", "worktree", "path"))
        self.assertIsInstance(entry.root, str)
        self.assertIsInstance(entry.rel_path, str)
        self.assertIsInstance(entry.created_at, str)

    def test_notify_ack_writes_index_entry_no_qid(self):
        result = self._call_notify(text="Build done", ack=True, message_id=2001)
        self.assertTrue(result["dispatched"])
        idx = qll.read_index(self.index_path)
        self.assertIn("2001", idx.messages)
        entry = idx.messages["2001"]
        self.assertIsNone(entry.qid)  # ack-notification has no qid

    def test_notify_no_ack_no_index_entry(self):
        result = self._call_notify(text="Silent note", ack=False, message_id=3001)
        self.assertTrue(result["dispatched"])
        idx = qll.read_index(self.index_path)
        # No entry written for ack=False
        self.assertNotIn("3001", idx.messages)

    def test_multiple_asks_accumulate_in_index(self):
        self._call_ask(title="Q1", message_id=4001)
        # Clear rate-limit so second ask isn't throttled
        with lib._rate_lock:
            lib._last_ask_time.clear()
        self._call_ask(title="Q2", message_id=4002)
        idx = qll.read_index(self.index_path)
        self.assertIn("4001", idx.messages)
        self.assertIn("4002", idx.messages)


# ── Test: notify modes ────────────────────────────────────────────────────────


class TestNotify(QuestionsLibTestCase):

    def test_notify_ack_returns_dispatched_true(self):
        result = self._call_notify(text="Doing it.", ack=True, message_id=5001)
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["message_id"], 5001)
        self.assertNotIn("error", result)

    def test_notify_no_ack_returns_dispatched_true(self):
        result = self._call_notify(text="FYI.", ack=False, message_id=5002)
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["message_id"], 5002)
        self.assertNotIn("error", result)

    def test_notify_ack_sends_kind_question(self):
        """ack=True must send kind=question (brd D7, invariant 10)."""
        captured = []

        def fake_send(*args, **kwargs):
            captured.append(kwargs)
            h = MagicMock()
            h.message_id = 5010
            h.telegram_message_id = 5010
            return h

        fake_bindings = _make_fake_bindings()
        fake_client = MagicMock()
        fake_client.send_message.side_effect = fake_send

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(roles_config, "load_bindings", return_value=fake_bindings), \
             patch.object(lib, "_client", return_value=fake_client), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            with lib._ws_lock:
                lib._ws_cache.clear()
            lib.notify(text="Ack me", ack=True)

        self.assertEqual(captured[0].get("kind"), "question")
        # Keyboard must contain the Acknowledge button
        kb = captured[0].get("keyboard")
        self.assertIsNotNone(kb)
        self.assertEqual(len(kb), 1)

    def test_notify_no_ack_sends_kind_notification(self):
        """ack=False must send kind=notification and carry no keyboard."""
        captured = []

        def fake_send(*args, **kwargs):
            captured.append(kwargs)
            h = MagicMock()
            h.message_id = 5011
            h.telegram_message_id = 5011
            return h

        fake_bindings = _make_fake_bindings()
        fake_client = MagicMock()
        fake_client.send_message.side_effect = fake_send

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(roles_config, "load_bindings", return_value=fake_bindings), \
             patch.object(lib, "_client", return_value=fake_client), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            with lib._ws_lock:
                lib._ws_cache.clear()
            lib.notify(text="Silent", ack=False)

        self.assertEqual(captured[0].get("kind"), "notification")
        self.assertIsNone(captured[0].get("keyboard"))

    def test_notify_relay_failure_returns_dispatched_false(self):
        from relay_server.client import RelayError  # type: ignore
        result = self._call_notify(text="Oh no.", relay_raises=RelayError("down"))
        self.assertFalse(result["dispatched"])
        self.assertIsNone(result["message_id"])
        self.assertIn("error", result)



# ── Test: notify(workspace_dir=…) for the epic driver (epic 50, task 50-08) ──


class TestNotifyWorkspaceDir(QuestionsLibTestCase):
    """The keyword-only ``workspace_dir``: honoured when given; ``None`` is today's resolution."""

    def _notify_capturing(self, *, env, **kwargs):
        captured = []

        def fake_send(*args, **kw):
            captured.append(kw)
            return _make_fake_handle(7001)

        client = MagicMock()
        client.send_message.side_effect = fake_send
        resolved = []
        real_resolve = lib._resolve_workspace_dir

        def spy():
            resolved.append(True)
            return real_resolve()

        with patch.dict(os.environ, env), \
             patch.object(roles_config, "load_bindings", return_value=_make_fake_bindings()), \
             patch.object(lib, "_client", return_value=client), \
             patch.object(lib, "_resolve_workspace_dir", side_effect=spy), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            if "CLAUDE_PROJECT_DIR" not in env:
                os.environ.pop("CLAUDE_PROJECT_DIR", None)     # as the epic driver's CLI runs it
            with lib._ws_lock:
                lib._ws_cache.clear()
            result = lib.notify(**kwargs)
        return result, captured, resolved

    def test_the_signature(self):
        import inspect
        params = inspect.signature(lib.notify).parameters
        self.assertEqual(list(params), ["text", "role", "ack", "workspace_dir"])
        self.assertEqual(params["workspace_dir"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIsNone(params["workspace_dir"].default)
        with self.assertRaises(TypeError):
            lib.notify("x", None, True, self.ws.workspace_dir)   # not positional

    def test_workspace_dir_is_honoured(self):
        """No CLAUDE_PROJECT_DIR (the epic driver drops it) and PWD elsewhere: the named workspace wins."""
        elsewhere = tempfile.mkdtemp(prefix="qs-elsewhere-")
        try:
            result, captured, resolved = self._notify_capturing(
                env={"PWD": elsewhere}, text="ALARM x", ack=True, workspace_dir=self.ws.workspace_dir)
        finally:
            shutil.rmtree(elsewhere, ignore_errors=True)
        self.assertTrue(result["dispatched"], result)
        self.assertEqual(resolved, [])                       # the per-call resolution was not consulted
        self.assertIn("test-ws", captured[0]["text"])
        self.assertEqual(captured[0]["nudge_schedule"], "4h,1d,3d,7d*")   # that workspace's ladder
        entry = qll.read_index(self.index_path).messages["7001"]
        self.assertEqual(entry.workspace_id, "test-ws")
        self.assertEqual(entry.root, str(Path(self.ws.workspace_dir).resolve()))

    def test_without_it_a_workspace_named_nowhere_is_refused_as_today(self):
        elsewhere = tempfile.mkdtemp(prefix="qs-elsewhere-")
        try:
            result, captured, resolved = self._notify_capturing(env={"PWD": elsewhere}, text="x", ack=False)
        finally:
            shutil.rmtree(elsewhere, ignore_errors=True)
        self.assertFalse(result["dispatched"])
        self.assertIn("no role catalog", result["error"])
        self.assertEqual(resolved, [True])
        self.assertEqual(captured, [])

    def test_the_mcp_tool_path_is_unchanged(self):
        """server.py's tool passes no workspace_dir, and a call without it resolves exactly as before:
        the same message, kind, ladder and index entry as naming the resolved workspace explicitly."""
        import ast
        tree = ast.parse((_REPO_ROOT / "questions-mcp" / "server.py").read_text(encoding="utf-8"))
        tool = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "notify")
        self.assertEqual([a.arg for a in tool.args.args], ["text", "role", "ack"])
        self.assertEqual(tool.args.kwonlyargs, [])
        call = next(n for n in ast.walk(tool) if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute) and n.func.attr == "notify")
        self.assertEqual(sorted(k.arg for k in call.keywords), ["ack", "role", "text"])

        env = {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}
        r1, c1, resolved = self._notify_capturing(env=env, text="Build done", ack=True)
        self.assertEqual(resolved, [True])
        e1 = qll.read_index(self.index_path).messages["7001"]
        os.unlink(self.index_path)
        r2, c2, _ = self._notify_capturing(env=env, text="Build done", ack=True, workspace_dir=self.ws.workspace_dir)
        e2 = qll.read_index(self.index_path).messages["7001"]
        self.assertEqual(r1, r2)
        for a, b in zip(c1, c2):
            a, b = dict(a), dict(b)
            self.assertTrue(a.pop("idempotency_key").startswith("notify:test-ws:"))
            b.pop("idempotency_key")
            self.assertEqual(a, b)
        self.assertEqual((e1.workspace_id, e1.root, e1.qid, e1.role), (e2.workspace_id, e2.root, e2.qid, e2.role))


# ── Test: caps (brd D12) ──────────────────────────────────────────────────────


class TestCaps(QuestionsLibTestCase):

    def _fill_open_entries(self, count: int, title_prefix: str = "Q") -> None:
        """Write *count* open entries directly into the queue file."""
        qfile = self.ws.tmp / "docs" / "questions" / "for-product-lead.md"
        lines: list[str] = []
        for i in range(1, count + 1):
            lines.append(f"## Q-{i:03d} — {title_prefix} {i}  [open]")
            lines.append("")
            lines.append(f"Body {i}.")
            lines.append("")
        qfile.write_text("\n".join(lines), encoding="utf-8")

    def test_max_open_refuses_when_full(self):
        # The default max_open is 20; fill exactly 20 entries.
        self._fill_open_entries(20)
        result = self._call_ask(title="one too many")
        self.assertIn("error", result)
        self.assertIn("max_open", result["error"])
        self.assertNotIn("id", result)

    def test_max_open_allows_below_limit(self):
        # 19 open entries → should be allowed.
        self._fill_open_entries(19)
        result = self._call_ask(title="nineteenth+1")
        self.assertTrue(result.get("dispatched"), result)

    def test_min_interval_refuses_too_fast(self):
        # First ask succeeds.
        result1 = self._call_ask(title="Fast 1", message_id=6001)
        self.assertTrue(result1.get("dispatched"), result1)
        # Second ask immediately — rate limit fires.
        result2 = self._call_ask(title="Fast 2", message_id=6002)
        self.assertIn("error", result2)
        self.assertIn("Rate limit", result2["error"])
        self.assertNotIn("id", result2)

    def test_min_interval_allows_after_cooldown(self):
        # Override min_interval_s to 0.01 s so the test can complete quickly.
        # Pass roles_path directly so find_roles_file is bypassed and
        # CLAUDE_PROJECT_DIR does not need to point at the test workspace.
        patched_config = qs.load_questions_config(
            self.ws.workspace_dir, roles_path=self.ws.roles_path
        )
        self.assertIsNotNone(
            patched_config,
            "load_questions_config returned None — fixture is broken, not a skip",
        )
        slow_config = qs.QuestionsConfig(
            **{
                **patched_config.__dict__,
                "min_interval_s": 0.01,
            }
        )

        from unittest.mock import MagicMock
        wc_mock = lib._WorkspaceCache(
            ts=0.0,
            qs_config=slow_config,
            # Pass path= explicitly so find_roles_file is bypassed and
            # CLAUDE_PROJECT_DIR (pointing at _ISOLATION_DIR) is irrelevant.
            catalog=roles_config.load_catalog(self.ws.workspace_dir, path=self.ws.roles_path),
            bindings=_make_fake_bindings(),
        )

        fake_handle = _make_fake_handle(7001)
        fake_client = MagicMock()
        fake_client.send_message.return_value = fake_handle

        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(lib, "_load_workspace", return_value=wc_mock), \
             patch.object(lib, "_client", return_value=fake_client), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            r1 = lib.ask(title="First")
            time.sleep(0.05)
            with lib._rate_lock:
                # Force the timestamp back so the second ask is allowed
                lib._last_ask_time[slow_config.workspace_id] = time.monotonic() - 1.0
            r2 = lib.ask(title="Second")

        self.assertTrue(r1.get("dispatched"), r1)
        self.assertTrue(r2.get("dispatched"), r2)


# ── Test: two workspaces in one server process ────────────────────────────────


class TestTwoWorkspaces(QuestionsLibTestCase):
    """Per-call workspace resolution: two workspaces in one server process."""

    def setUp(self):
        super().setUp()
        self.ws2 = _WorkspaceFixture(
            ROLES_TOML_HPL.replace("test-ws", "second-ws")
        )

    def tearDown(self):
        self.ws2.cleanup()
        super().tearDown()

    def test_each_workspace_gets_its_own_config(self):
        """Asks from two different workspace dirs are independent."""
        r1 = self._call_ask(
            title="From WS1", ws_dir=self.ws.workspace_dir, message_id=8001
        )
        r2 = self._call_ask(
            title="From WS2", ws_dir=self.ws2.workspace_dir, message_id=8002
        )
        self.assertTrue(r1.get("dispatched"), r1)
        self.assertTrue(r2.get("dispatched"), r2)

        # Each workspace gets its own queue file
        q1 = self.ws.tmp / "docs" / "questions" / "for-product-lead.md"
        q2 = self.ws2.tmp / "docs" / "questions" / "for-product-lead.md"
        self.assertTrue(q1.exists())
        self.assertTrue(q2.exists())

        # Each file contains only its own entry
        text1 = q1.read_text()
        text2 = q2.read_text()
        self.assertIn("From WS1", text1)
        self.assertNotIn("From WS2", text1)
        self.assertIn("From WS2", text2)
        self.assertNotIn("From WS1", text2)

    def test_cache_is_per_workspace(self):
        """Cache must be keyed by workspace_dir; a second workspace must not
        reuse the first's config.

        The cache is NOT cleared between the two calls — a single-key global
        cache would return ws1's config for ws2 and this test would catch that.
        """
        # Call ws1 first so its config is populated in the cache.
        self._call_ask(title="Pre-warm", ws_dir=self.ws.workspace_dir, message_id=9001)

        # Call ws2 WITHOUT clearing the cache between calls.
        # If the cache is keyed globally rather than per-workspace_dir,
        # ws2 would receive ws1's config (workspace_id="test-ws"), not its own.
        self._call_ask(title="WS2 call", ws_dir=self.ws2.workspace_dir, message_id=9002)

        # Both workspaces must appear in the index under their own workspace_ids.
        idx = qll.read_index(self.index_path)
        ws_ids = {v.workspace_id for v in idx.messages.values()}
        self.assertIn("test-ws", ws_ids, "ws1 entry missing from index")
        self.assertIn("second-ws", ws_ids, "ws2 entry missing — cache isolation broken")


# ── Test: index helpers standalone ────────────────────────────────────────────


class TestIndexHelpers(unittest.TestCase):
    """questions_listen_lib read/write helpers used by the listener."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="idx-test-"))
        self.idx_path = self.tmp / "async_questions.json"
        self.lck_path = self.tmp / "async_questions.json.lock"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_read_empty_when_no_file(self):
        idx = qll.read_index(self.idx_path)
        self.assertEqual(idx.watermark, 0)
        self.assertEqual(idx.messages, {})
        self.assertEqual(idx.pending, [])

    def test_write_read_roundtrip(self):
        entry = qll.IndexEntry(
            workspace_id="demo-ws",
            anchor="repo",
            root="/tmp/demo",
            rel_path="docs/questions/for-product-lead.md",
            qid="Q-001",
            role="hpl",
            created_at="2026-08-26T14:00:00Z",
        )
        idx = qll.Index(watermark=42, messages={"123": entry}, pending=[])

        with patch.object(qll, "INDEX_PATH", self.idx_path), \
             patch.object(qll, "_LOCK_PATH", self.lck_path):
            qll.write_index(idx, self.idx_path)
            back = qll.read_index(self.idx_path)

        self.assertEqual(back.watermark, 42)
        self.assertIn("123", back.messages)
        e = back.messages["123"]
        self.assertEqual(e.workspace_id, "demo-ws")
        self.assertEqual(e.qid, "Q-001")
        self.assertEqual(e.role, "hpl")

    def test_ack_entry_has_no_qid(self):
        """An ack-notification entry stored without qid reads back as None."""
        entry = qll.IndexEntry(
            workspace_id="demo-ws",
            anchor="repo",
            root="/tmp/demo",
            rel_path="",
            qid=None,
            role="hpl",
            created_at="2026-08-26T14:00:00Z",
        )
        idx = qll.Index(messages={"999": entry})
        qll.write_index(idx, self.idx_path)
        back = qll.read_index(self.idx_path)
        self.assertIsNone(back.messages["999"].qid)

    def test_add_message_entry_atomic(self):
        entry = qll.IndexEntry(
            workspace_id="demo-ws",
            anchor="repo",
            root="/tmp/demo",
            rel_path="docs/questions/for-tech-lead.md",
            qid="Q-007",
            role="htl",
            created_at="2026-08-26T15:00:00Z",
        )
        qll.add_message_entry(
            7007, entry, path=self.idx_path, lock_path=self.lck_path
        )
        back = qll.read_index(self.idx_path)
        self.assertIn("7007", back.messages)

    def test_pending_and_watermark_preserved(self):
        """23-05 uses watermark and pending — the shape must survive a round trip."""
        pa = qll.PendingApply(
            message_id=42,
            answer="no",
            attempts=3,
            first_failed_at="2026-08-26T00:00:00Z",
            last_error="not_found",
        )
        idx = qll.Index(watermark=1234, pending=[pa])
        qll.write_index(idx, self.idx_path)
        back = qll.read_index(self.idx_path)
        self.assertEqual(back.watermark, 1234)
        self.assertEqual(len(back.pending), 1)
        self.assertEqual(back.pending[0].message_id, 42)
        self.assertEqual(back.pending[0].attempts, 3)

    def test_count_pending(self):
        pa1 = qll.PendingApply(message_id=1, answer="a", attempts=1)
        pa2 = qll.PendingApply(message_id=2, answer="b", attempts=2)
        idx = qll.Index(pending=[pa1, pa2])
        qll.write_index(idx, self.idx_path)
        with patch.object(qll, "INDEX_PATH", self.idx_path):
            self.assertEqual(qll.count_pending(self.idx_path), 2)


# ── Task 45: a missing relay client must name itself ─────────────────────────


class TestRelayClientUnavailable(QuestionsLibTestCase):
    """The defect: inside the MCP server's uv environment ``httpx`` was missing,
    ``relay_server.client`` failed to import, the fallback aliased
    ``NotBoundError`` to ``Exception`` — and every send reported
    "Relay refused: installation not bound".  Nothing had been refused; nothing
    had been sent."""

    def _notify_with_no_client(self, **kwargs) -> dict:
        fake_bindings = _make_fake_bindings()
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(roles_config, "load_bindings", return_value=fake_bindings), \
             patch.object(lib, "RelayClient", None), \
             patch.object(lib, "RELAY_IMPORT_ERROR", "ModuleNotFoundError: No module named 'httpx'"), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            return lib.notify(**kwargs)

    def test_notify_reports_the_import_error_not_a_refusal(self):
        result = self._notify_with_no_client(text="hello", ack=False)
        self.assertFalse(result["dispatched"])
        self.assertIn("No module named 'httpx'", result["error"])
        self.assertIn("could not be imported", result["error"])
        self.assertNotIn("not bound", result["error"])
        self.assertNotIn("Relay refused", result["error"])

    def test_ask_reports_the_import_error_and_keeps_the_entry(self):
        fake_bindings = _make_fake_bindings()
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(roles_config, "load_bindings", return_value=fake_bindings), \
             patch.object(lib, "RelayClient", None), \
             patch.object(lib, "RELAY_IMPORT_ERROR", "ModuleNotFoundError: No module named 'httpx'"), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            result = lib.ask(title="Still written?", body="b")
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["id"], "Q-001")      # write-before-send still holds
        self.assertIn("No module named 'httpx'", result["error"])
        self.assertNotIn("not bound", result["error"])

    def test_a_real_not_bound_refusal_still_reads_as_one(self):
        """The fix must not blur the genuine case."""
        result = self._call_notify(
            text="x", ack=False, relay_raises=lib.NotBoundError("not_bound"),
        )
        self.assertIn("installation not bound", result["error"])

    def test_fallback_exception_types_are_distinct(self):
        """Whatever the import outcome, ``except NotBoundError`` may never be a
        catch-all: it must be a strict subclass of ``RelayError``, which must
        not be ``Exception`` itself."""
        self.assertIsNot(lib.NotBoundError, Exception)
        self.assertIsNot(lib.RelayError, Exception)
        self.assertTrue(issubclass(lib.NotBoundError, lib.RelayError))
        self.assertFalse(issubclass(lib.RelayUnavailableError, lib.RelayError))


class TestServerDeclaresWhatTheRelayClientImports(unittest.TestCase):
    """``server.py`` runs under ``uv run --script`` with only its declared
    dependencies.  Every third-party module ``relay_server/client.py`` imports
    at module scope must therefore be declared there — ``mcp`` 2.x stopped
    bringing ``httpx`` along, and nothing noticed."""

    def test_script_dependencies_cover_the_relay_client(self):
        import ast
        import re as _re

        server = (_REPO_ROOT / "questions-mcp" / "server.py").read_text(encoding="utf-8")
        block = _re.search(r"^# /// script\n(.*?)^# ///$", server, _re.S | _re.M)
        self.assertIsNotNone(block, "PEP 723 script block not found")
        deps_line = _re.search(r"^# dependencies = \[(.*)\]$", block.group(1), _re.M)
        self.assertIsNotNone(deps_line)
        declared = {
            _re.split(r"[<>=!~\[ ]", d.strip().strip('"\''), maxsplit=1)[0].lower()
            for d in deps_line.group(1).split(",") if d.strip()
        }

        client = (_REPO_ROOT / "relay-server" / "relay_server" / "client.py").read_text(encoding="utf-8")
        imported = set()
        for node in ast.parse(client).body:
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        third_party = {
            m for m in imported
            if m not in sys.stdlib_module_names and m not in ("relay_server", "tomli")
        }
        self.assertTrue(third_party, "expected the relay client to import httpx")
        self.assertEqual(third_party - declared, set(),
                         f"server.py must declare {sorted(third_party - declared)}")


# ── Epic 46: ask(id=…), the ladder fallback, withdraw/claim wrappers ─────────


class _LadderRejection(Exception):
    """Shaped like ``RelayError`` for an HTTP 422 from ``POST /v1/messages``."""

    def __init__(self, message, status_code=422):
        super().__init__(message)
        self.status_code = status_code


class TestAskEpic46(QuestionsLibTestCase):

    def _ask_with_client(self, client, **kwargs) -> dict:
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(roles_config, "load_bindings", return_value=_make_fake_bindings()), \
             patch.object(lib, "_client", return_value=client), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            return lib.ask(**kwargs)

    def test_result_shape_is_unchanged_when_sync_is_off(self):
        """Invariant: a workspace that did not ask for git sync gets exactly
        the keys it always got — no ``sync``, no ``warnings``."""
        result = self._call_ask(title="Plain")
        self.assertEqual(
            sorted(result), ["dispatched", "file", "id", "message_id", "pending_answers"]
        )

    def test_explicit_id_must_have_been_claimed(self):
        client = MagicMock()
        client.send_message.return_value = _make_fake_handle(999)
        refused = self._ask_with_client(client, title="Mine", id="Q-077")
        self.assertIn("has not been claimed", refused["error"])
        self.assertNotIn("id", refused)
        client.send_message.assert_not_called()
        self.assertFalse((self.ws.q_dir / "for-product-lead.md").exists(), "nothing may be written")

    def test_a_claimed_id_is_used_and_then_spent(self):
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}):
            claimed = lib.claim(count=1, note="for the test")
        self.assertEqual(claimed["ids"], ["Q-001"])
        self.assertFalse(claimed["claimed_on_remote"])

        client = MagicMock()
        client.send_message.return_value = _make_fake_handle(999)
        first = self._ask_with_client(client, title="Uses the claim", id="Q-001")
        self.assertEqual(first["id"], "Q-001")
        self.assertTrue(first["dispatched"])

        with lib._rate_lock:
            lib._last_ask_time.clear()
        again = self._ask_with_client(client, title="Again", id="Q-001")
        self.assertIn("already heads an entry", again["error"])
        # …and an ordinary ask allocates past the claim.
        with lib._rate_lock:
            lib._last_ask_time.clear()
        self.assertEqual(self._ask_with_client(client, title="Next")["id"], "Q-002")

    def test_a_rejected_ladder_is_retried_without_it_and_reported(self):
        """brd D10: a relay that caps per-message ladders at 3 rungs rejects the
        stock 4-rung ladder with 422.  Before this, ``ask`` returned
        ``dispatched: false`` and left an entry no human would ever be told of."""
        client = MagicMock()
        client.send_message.side_effect = [
            _LadderRejection("relay HTTP 422: invalid nudge_schedule: nudge schedule has 4 entries; server cap is 3"),
            _make_fake_handle(1234),
        ]
        with patch.object(lib, "RelayError", _LadderRejection):
            result = self._ask_with_client(client, title="Ladder too long")

        self.assertTrue(result["dispatched"])
        self.assertEqual(result["message_id"], 1234)
        self.assertNotIn("error", result)
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("rejected the nudge ladder '4h,1d,3d,7d*'", result["warnings"][0])

        first, second = client.send_message.call_args_list
        self.assertEqual(first.kwargs["nudge_schedule"], "4h,1d,3d,7d*")
        self.assertIsNone(second.kwargs["nudge_schedule"])
        self.assertTrue(second.kwargs["never_expires"], "the retry is still a never-expiring question")
        self.assertNotEqual(first.kwargs["idempotency_key"], second.kwargs["idempotency_key"])
        self.assertIn("relay #1234", (self.ws.q_dir / "for-product-lead.md").read_text(encoding="utf-8"))

    def test_any_other_422_is_not_retried(self):
        client = MagicMock()
        client.send_message.side_effect = _LadderRejection("relay HTTP 422: escalation_token_not_bound")
        with patch.object(lib, "RelayError", _LadderRejection):
            result = self._ask_with_client(client, title="Other 422")
        self.assertFalse(result["dispatched"])
        self.assertEqual(client.send_message.call_count, 1)
        self.assertNotIn("warnings", result)

    def test_escalation_fields_are_sent_together_or_not_at_all(self):
        """The relay answers HTTP 422 to ``escalate_after_sec`` without
        ``escalate_to_token``.  A question to the *default* role has nobody to
        escalate to, so it must carry neither — it used to carry the delay
        alone, and every such ``ask`` was written and then refused."""
        client = MagicMock()
        client.send_message.return_value = _make_fake_handle(999)

        default = self._ask_with_client(client, title="To the default role")     # hpl is default
        self.assertTrue(default["dispatched"])
        kwargs = client.send_message.call_args.kwargs
        self.assertIsNone(kwargs["escalate_after_sec"])
        self.assertIsNone(kwargs["escalate_to_token"])

        with lib._rate_lock:
            lib._last_ask_time.clear()
        self._ask_with_client(client, title="To another role", role="htl")
        kwargs = client.send_message.call_args.kwargs
        self.assertEqual(kwargs["escalate_after_sec"], 24 * 3600)
        self.assertEqual(kwargs["escalate_to_token"], "rly_hpl_token")

    def test_withdraw_goes_through_the_shared_operation(self):
        asked = self._call_ask(title="To be withdrawn", message_id=4321)
        relay = MagicMock()
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(roles_config, "load_bindings", return_value=_make_fake_bindings()), \
             patch.object(lib, "_client", return_value=relay), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            result = lib.withdraw(reason="went ahead", id=asked["id"])

        self.assertTrue(result["withdrawn"])
        self.assertTrue(result["relay_cancelled"])
        self.assertEqual(result["message_id"], 4321)
        relay.edit_message.assert_called_once()
        relay.cancel_message.assert_called_once()
        self.assertEqual(relay.cancel_message.call_args.args[0], 4321)
        text = (self.ws.q_dir / "for-product-lead.md").read_text(encoding="utf-8")
        self.assertIn("[withdrawn]", text)
        self.assertIn("    went ahead", text)

    def test_a_withdrawn_question_frees_its_max_open_slot(self):
        toml = ROLES_TOML_HPL.replace('anchor         = "worktree"', 'anchor         = "worktree"\nmax_open       = 1')
        self.ws.roles_path.write_text(toml, encoding="utf-8")
        first = self._call_ask(title="One", message_id=1)
        with lib._rate_lock:
            lib._last_ask_time.clear()
        self.assertIn("Too many open questions", self._call_ask(title="Two")["error"])
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": self.ws.workspace_dir}), \
             patch.object(roles_config, "load_bindings", return_value=_make_fake_bindings()), \
             patch.object(lib, "_client", return_value=MagicMock()), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            lib.withdraw(reason="moot", id=first["id"])
        with lib._rate_lock:
            lib._last_ask_time.clear()
        with lib._ws_lock:
            lib._ws_cache.clear()
        self.assertEqual(self._call_ask(title="Two")["id"], "Q-002")

    def test_caller_names_the_session_without_leaking_it_whole(self):
        with patch.dict(os.environ, {"CLAUDE_CODE_SESSION_ID": "1350e006-43b2-43fb-b1da-ba703a1b4e41"}):
            self.assertEqual(lib._caller(), "agent · session 1350e006")
        without = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
        with patch.dict(os.environ, without, clear=True):
            self.assertEqual(lib._caller(), "agent")



# ── ask with [questions] sync = "push" (epic 46, review round 1 issue 6) ──────

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

ROLES_TOML_SYNC_PUSH = ROLES_TOML_HPL.replace(
    'anchor         = "worktree"', 'anchor         = "worktree"\nsync           = "push"'
)


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=60,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{proc.stdout}\n{proc.stderr}")
    return proc.stdout.strip()


class TestAskWithSyncPush(QuestionsLibTestCase):
    """The whole ``ask`` path against a real repository and a real remote:
    the entry and its Dispatched line are both on the upstream when ``ask``
    returns, and nothing else is."""

    QUEUE = "docs/questions/for-product-lead.md"

    def setUp(self):
        super().setUp()
        self.ws.cleanup()
        patcher = patch.dict(os.environ, _GIT_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.assertIn('sync           = "push"', ROLES_TOML_SYNC_PUSH)
        self.tmp = Path(tempfile.mkdtemp(prefix="qs-mcp-git-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = self.tmp / "remote.git"
        _git(self.tmp, "init", "--quiet", "--bare", "-b", "main", str(self.remote))
        self.repo = self.tmp / "ws"
        _git(self.tmp, "clone", "--quiet", str(self.remote), str(self.repo))
        _git(self.repo, "checkout", "--quiet", "-b", "main")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "roles.toml").write_text(ROLES_TOML_SYNC_PUSH, encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "app.txt").write_text("v1\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "--quiet", "-m", "base")
        _git(self.repo, "push", "--quiet", "-u", "origin", "main")

    def _ask(self, client, **kwargs) -> dict:
        with patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(self.repo)}), \
             patch.object(roles_config, "load_bindings", return_value=_make_fake_bindings()), \
             patch.object(lib, "_client", return_value=client), \
             patch.object(qll, "INDEX_PATH", self.index_path), \
             patch.object(qll, "_LOCK_PATH", self.index_lock):
            return lib.ask(**kwargs)

    def _remote_file(self) -> str:
        return subprocess.run(
            ["git", "show", f"main:{self.QUEUE}"], cwd=str(self.remote),
            capture_output=True, text=True, check=True,
        ).stdout

    def test_the_entry_and_its_dispatch_line_reach_the_upstream(self):
        # An unrelated local commit and an unrelated dirty file: neither may
        # travel with the question.
        (self.repo / "src" / "app.txt").write_text("v2\n", encoding="utf-8")
        _git(self.repo, "commit", "--quiet", "-am", "local work, not pushed")
        (self.repo / "notes.txt").write_text("scratch\n", encoding="utf-8")

        client = MagicMock()
        client.send_message.return_value = _make_fake_handle(4242)
        result = self._ask(client, title="Synced question", body="Body.")

        self.assertNotIn("error", result, result)
        self.assertEqual(result["id"], "Q-001")
        self.assertTrue(result["dispatched"])
        self.assertTrue(result["sync"]["pushed"], result["sync"])
        self.assertNotIn("dispatch_marker", result["sync"],
                         "the marker commit fared the same as the claim: nothing to add")

        upstream = self._remote_file()
        self.assertIn("## Q-001", upstream)
        self.assertIn("4242", upstream, "the Dispatched line must be upstream too")
        with open(self.repo / self.QUEUE, encoding="utf-8", newline="") as fh:
            self.assertEqual(fh.read(), upstream, "local and upstream copies agree")

        # Only the queue file was published.
        names = _git(self.remote, "log", "--name-only", "--format=", "main").split()
        self.assertEqual(set(names) - {".claude/roles.toml", "src/app.txt"}, {self.QUEUE})
        self.assertEqual(_git(self.remote, "show", "main:src/app.txt"), "v1")
        # The checkout: the queue file is clean, the stranger is untouched.
        status = _git(self.repo, "status", "--porcelain").splitlines()
        self.assertEqual(status, ["?? notes.txt"])

    def test_an_unreachable_remote_still_writes_sends_and_says_so(self):
        _git(self.repo, "remote", "set-url", "origin", str(self.tmp / "gone.git"))
        client = MagicMock()
        client.send_message.return_value = _make_fake_handle(4243)
        result = self._ask(client, title="Offline question", body="Body.")

        self.assertNotIn("error", result, result)
        self.assertEqual(result["id"], "Q-001")
        self.assertTrue(result["dispatched"], "a git failure never stops the question")
        self.assertFalse(result["sync"]["pushed"])
        self.assertTrue(result["sync"]["pending"])
        self.assertTrue(result["sync"]["reason"])
        with open(self.repo / self.QUEUE, encoding="utf-8") as fh:
            self.assertIn("## Q-001", fh.read())
        client.send_message.assert_called_once()


if __name__ == "__main__":
    unittest.main()
