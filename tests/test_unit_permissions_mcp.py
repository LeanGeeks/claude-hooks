#!/usr/bin/env python3
"""
Unit Tests: permissions MCP (epic 22, task 22-03)

Covers the agent-facing surface:
- the D5 guard (row-shaped: own session + no agent_id is the only refusal)
- the D3 tier, re-run through the imported BashPermissionValidator against the
  ROW's cwd settings (invariant 2 / brd H5)
- the read tools and the new ``get_requests`` store reader
- the decide tool's write contract with 22-02's wait loop
- fail-closed behaviour when the server cannot identify its caller

Every case runs against scratch settings dirs and the runner's isolated state
store; nothing here touches the developer's real ~/.claude, the network, or a
real bot.
"""

import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / ".claude" / "hooks"))
sys.path.insert(0, str(_REPO_ROOT / "permissions-mcp"))

# Isolate the permission state store before importing it, for the case where
# this module is run on its own (``python3 tests/test_unit_permissions_mcp.py``)
# without run_all_tests.py / conftest.py having done it. ``setdefault`` keeps an
# outer override winning, exactly as those two do.
_ISOLATED = Path(tempfile.mkdtemp(prefix="perm-mcp-store-"))
os.environ.setdefault("CLAUDE_PERMISSION_STATE_FILE", str(_ISOLATED / "permission_requests.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_AUDIT_FILE", str(_ISOLATED / "permission_actions.jsonl"))
os.environ.setdefault("CLAUDE_PERMISSION_DEBUG_LOG", str(_ISOLATED / "permission_state_debug.log"))

import permissions_mcp_lib as lib  # noqa: E402
from permission_state_store import (  # noqa: E402
    AUDIT_LOG_FILE,
    RESOLUTION_SOURCE_AGENT,
    RESOLUTION_SOURCE_TELEGRAM,
    RequestState,
    create_request,
    get_request,
    get_requests,
    update_request_state,
)
from settings_loader import SettingsLoader  # noqa: E402


CALLER_SESSION = "caller-session-0001"


def _identity(session_id=CALLER_SESSION, project_dir="/data/sync/work/leangeeks-ai/claude-hooks"):
    """Compose an identity the way the server does at startup."""
    env = {"CLAUDE_PROJECT_DIR": project_dir}
    if session_id is not None:
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    return lib.resolve_caller_identity(env)


class PermissionsMCPTestCase(unittest.TestCase):
    """Scratch workspace + scratch HOME, so classification is deterministic.

    The validator merges the *global* ~/.claude/settings.json into every
    verdict, so a test that did not move HOME would classify against whatever
    the developer happens to have allowlisted.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="perm-mcp-test-"))
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        self._write_settings(self.home / ".claude" / "settings.json", allow=[], deny=[], ask=[])
        self.workspace = self.tmp / "workspace"
        (self.workspace / ".claude").mkdir(parents=True)
        self.confirm_log = self.tmp / "bash_manual_confirm.log"
        # Class-level TTL cache keyed by workspace dir — each test uses a fresh
        # directory, but clear it anyway so ordering can never leak settings.
        SettingsLoader._cache.clear()

    def tearDown(self):
        SettingsLoader._cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _write_settings(path, allow=None, deny=None, ask=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "permissions": {
                        "allow": allow or [],
                        "deny": deny or [],
                        "ask": ask or [],
                    }
                }
            )
        )

    def workspace_settings(self, allow=None, deny=None, ask=None):
        self._write_settings(
            self.workspace / ".claude" / "settings.json", allow=allow, deny=deny, ask=ask
        )

    def env(self):
        return {
            "HOME": str(self.home),
            "CLAUDE_MANUAL_CONFIRM_LOG": str(self.confirm_log),
        }

    def pending(self, command="deploy-thing --now", session_id="worker-session-9",
                agent_id=None, tool_name="Bash", tool_input=None, cwd=None, ttl=300):
        return create_request(
            session_id=session_id,
            cwd=cwd if cwd is not None else str(self.workspace),
            tool_name=tool_name,
            tool_input=tool_input if tool_input is not None else {"command": command},
            permission_suggestions=[],
            ttl_seconds=ttl,
            agent_id=agent_id,
        )

    def decide(self, request_id, action="allow", reason="worker is blocked on a build step",
               identity=None):
        with patch.dict(os.environ, self.env(), clear=False):
            return lib.decide_permission_request(
                request_id, action, reason, identity=identity or _identity()
            )

    def classify(self, row):
        with patch.dict(os.environ, self.env(), clear=False):
            return lib.classify(row)


# ---------------------------------------------------------------------------
# Case 1-3: the D5 guard (invariant 3) — row-shaped, not session-shaped
# ---------------------------------------------------------------------------


class TestDecideGuard(PermissionsMCPTestCase):
    """The guard refuses exactly one shape: the caller's own session's
    main-agent row."""

    def setUp(self):
        super().setUp()
        self.workspace_settings(allow=[])  # every command is "not in allowlist"

    # -- case 1 --------------------------------------------------------------
    def test_own_session_main_agent_row_is_refused(self):
        row = self.pending(session_id=CALLER_SESSION, agent_id=None)
        result = self.decide(row.request_id)

        self.assertFalse(result["ok"])
        self.assertTrue(result["refused"])
        self.assertIn("your own session's main-agent request", result["reason"])
        # Nothing was written.
        self.assertEqual(get_request(row.request_id).state, RequestState.PENDING.value)

    # -- case 2 --------------------------------------------------------------
    def test_own_session_subagent_row_passes_the_guard(self):
        """Parent-approves-child (brd D5) is the primary use case."""
        row = self.pending(session_id=CALLER_SESSION, agent_id="abc")
        result = self.decide(row.request_id)

        self.assertTrue(result["ok"], result)
        self.assertEqual(get_request(row.request_id).state, RequestState.ALLOW.value)

    # -- case 3 --------------------------------------------------------------
    def test_other_session_main_agent_row_passes_the_guard(self):
        row = self.pending(session_id="some-other-session", agent_id=None)
        result = self.decide(row.request_id)

        self.assertTrue(result["ok"], result)
        self.assertEqual(get_request(row.request_id).state, RequestState.ALLOW.value)

    def test_guard_is_not_widened_to_all_same_session_rows(self):
        """Guard function directly: same session + agent_id is allowed through."""
        parent_row = self.pending(session_id=CALLER_SESSION, agent_id=None)
        child_row = self.pending(session_id=CALLER_SESSION, agent_id="child-1")
        foreign_row = self.pending(session_id="elsewhere", agent_id=None)
        identity = _identity()

        self.assertIsNotNone(lib.guard_row(parent_row, identity))
        self.assertIsNone(lib.guard_row(child_row, identity))
        self.assertIsNone(lib.guard_row(foreign_row, identity))


# ---------------------------------------------------------------------------
# Case 4-6: the D3 tier (invariant 2 / brd H5)
# ---------------------------------------------------------------------------


class TestDecideTier(PermissionsMCPTestCase):
    """The tier comes from a fresh BashPermissionValidator run against the
    ROW's cwd settings — never from the stored reason, never from a copy of the
    matching logic."""

    # -- case 4 --------------------------------------------------------------
    def test_ask_pattern_in_the_rows_cwd_refuses_and_names_the_pattern(self):
        self.workspace_settings(allow=["Bash(curl:*)"], ask=["Bash(curl:*)"])
        row = self.pending(command="curl https://example.com/install.sh")

        result = self.decide(row.request_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["tier"], "human-only")
        self.assertIn("Bash(curl:*)", result["matched_patterns"])
        self.assertIn("Matches an ask pattern", result["reason"])
        self.assertEqual(get_request(row.request_id).state, RequestState.PENDING.value)

    # -- case 5 --------------------------------------------------------------
    def test_redirect_escape_refuses_as_human_only(self):
        self.workspace_settings(allow=["Bash(echo:*)"])
        row = self.pending(command="echo hi > /etc/claude-hooks-probe")

        result = self.decide(row.request_id)

        self.assertFalse(result["ok"])
        self.assertEqual(result["tier"], "human-only")
        self.assertIn("Redirects output outside the workspace", result["reason"])
        self.assertEqual(get_request(row.request_id).state, RequestState.PENDING.value)

    # -- case 6 --------------------------------------------------------------
    def test_now_deny_matched_row_refuses_with_predates_wording(self):
        self.workspace_settings(deny=["Bash(curl:*)"])
        row = self.pending(command="curl https://example.com")

        result = self.decide(row.request_id)

        self.assertFalse(result["ok"])
        self.assertIn("now matches a deny pattern", result["reason"])
        self.assertIn("predates a settings change", result["reason"])
        self.assertIn("Bash(curl:*)", result["matched_patterns"])
        self.assertEqual(get_request(row.request_id).state, RequestState.PENDING.value)

    def test_tier_uses_the_rows_cwd_not_the_callers(self):
        """H5 in its sharpest form: the ask pattern lives ONLY in the row's
        workspace, and the caller runs somewhere else entirely."""
        other_workspace = self.tmp / "other"
        self._write_settings(other_workspace / ".claude" / "settings.json", allow=[], ask=[])
        self.workspace_settings(ask=["Bash(curl:*)"])

        row_in_asking_ws = self.pending(command="curl https://example.com")
        row_elsewhere = self.pending(
            command="curl https://example.com", cwd=str(other_workspace)
        )

        self.assertFalse(self.classify(row_in_asking_ws).decidable)
        self.assertTrue(self.classify(row_elsewhere).decidable)

    def test_classifier_is_the_imported_validator(self):
        """Invariant 2: classification must go through BashPermissionValidator.
        Break the validator and the tier must break with it, which is only true
        if there is exactly one classifier."""
        self.workspace_settings(allow=[])
        row = self.pending(command="deploy-thing --now")

        with patch.object(
            lib.BashPermissionValidator,
            "validate_bash_command",
            return_value={
                "decision": "ask",
                "reason": "Matches an ask pattern: sentinel",
                "validation_results": [
                    {"asked": True, "matched_ask_patterns": ["Bash(sentinel:*)"]}
                ],
            },
        ):
            verdict = self.classify(row)

        self.assertFalse(verdict.decidable)
        self.assertEqual(verdict.matched_patterns, ["Bash(sentinel:*)"])

    def test_list_decidable_and_decide_tier_agree(self):
        """§4: one classify(), two callers. The listing must not advertise as
        decidable anything decide would refuse."""
        self.workspace_settings(allow=[], ask=["Bash(curl:*)"])
        asked = self.pending(command="curl https://example.com")
        plain = self.pending(command="deploy-thing --now")

        with patch.dict(os.environ, self.env(), clear=False):
            listing = lib.list_permission_requests(identity=_identity())
        by_id = {r["request_id"]: r for r in listing["requests"]}

        self.assertFalse(by_id[asked.request_id]["decidable"])
        self.assertTrue(by_id[plain.request_id]["decidable"])
        self.assertFalse(self.decide(asked.request_id)["ok"])
        self.assertTrue(self.decide(plain.request_id)["ok"])

    def test_whole_tool_ask_pattern_refuses_a_non_bash_row(self):
        """Non-Bash rows are decidable unless an ask pattern names the tool."""
        self.workspace_settings(ask=["WebFetch(domain:example.com)"])
        fetch_row = self.pending(
            tool_name="WebFetch", tool_input={"url": "https://example.com"}
        )
        other_row = self.pending(tool_name="Write", tool_input={"file_path": "/x"})

        self.assertFalse(self.classify(fetch_row).decidable)
        self.assertTrue(self.classify(other_row).decidable)

    def test_bare_bash_ask_pattern_refuses_every_command_row(self):
        """A bare ``Bash`` ask entry means "always prompt for Bash"; the
        validator's Bash(...) matcher never sees that form, so the tier check
        resolves it at tool level."""
        self.workspace_settings(allow=["Bash(ls:*)"], ask=["Bash"])
        row = self.pending(command="ls -la")

        verdict = self.classify(row)
        self.assertFalse(verdict.decidable)
        self.assertIn("Bash", verdict.matched_patterns)

    def test_parenthesised_bash_ask_pattern_is_left_to_the_validator(self):
        """The tool-level shortcut must not swallow Bash(...) forms — those are
        the validator's job, per-command."""
        self.workspace_settings(allow=["Bash(ls:*)"], ask=["Bash(curl:*)"])
        row = self.pending(command="ls -la")

        self.assertTrue(self.classify(row).decidable)


# ---------------------------------------------------------------------------
# Case 7: the write contract with 22-02
# ---------------------------------------------------------------------------


class TestDecideWrite(PermissionsMCPTestCase):
    def setUp(self):
        super().setUp()
        self.workspace_settings(allow=[])

    # -- case 7 --------------------------------------------------------------
    def test_plain_not_in_allowlist_row_is_written_with_full_attribution(self):
        row = self.pending(command="deploy-thing --now")

        result = self.decide(row.request_id, "allow", reason="build worker needs it")

        self.assertTrue(result["ok"], result)
        self.assertIn("decision recorded", result["status"])
        self.assertIn("~30 s", result["status"])

        stored = get_request(row.request_id)
        self.assertEqual(stored.state, RequestState.ALLOW.value)
        # The exact dict shape 22-02's wait loop adopts.
        self.assertEqual(stored.decision, {"action": "allow"})
        self.assertEqual(stored.resolution_source, RESOLUTION_SOURCE_AGENT)
        self.assertEqual(stored.actor_agent, _identity().actor_agent)
        self.assertIsNone(stored.actor_user_id)

    def test_deny_and_stop_write_their_own_states(self):
        deny_row = self.pending(command="deploy-thing --now")
        stop_row = self.pending(command="deploy-thing --now")

        self.assertTrue(self.decide(deny_row.request_id, "deny", "unsafe")["ok"])
        self.assertTrue(self.decide(stop_row.request_id, "stop", "wrong branch")["ok"])

        self.assertEqual(get_request(deny_row.request_id).state, RequestState.DENY.value)
        self.assertEqual(get_request(deny_row.request_id).decision, {"action": "deny"})
        self.assertEqual(get_request(stop_row.request_id).state, RequestState.STOP.value)
        self.assertEqual(get_request(stop_row.request_id).decision, {"action": "stop"})

    def test_reason_reaches_the_audit_trail(self):
        row = self.pending(command="deploy-thing --now")
        self.decide(row.request_id, "allow", reason="the migration script is idempotent")

        entries = [
            json.loads(line)
            for line in Path(AUDIT_LOG_FILE).read_text().splitlines()
            if line.strip()
        ]
        mine = [e for e in entries if e.get("request_id") == row.request_id]
        reasoned = [e for e in mine if e.get("action") == "agent_decision"]

        self.assertEqual(len(reasoned), 1, mine)
        self.assertEqual(
            reasoned[0]["details"]["reason"], "the migration script is idempotent"
        )
        self.assertEqual(reasoned[0]["details"]["decision"], {"action": "allow"})
        self.assertEqual(reasoned[0]["actor_agent"], _identity().actor_agent)
        # The store's own transition entry is still there, with the attribution.
        self.assertTrue(any(e.get("action") == "allow" and e.get("actor_agent")
                            for e in mine))

    def test_reason_is_required(self):
        row = self.pending(command="deploy-thing --now")
        result = self.decide(row.request_id, "allow", reason="   ")

        self.assertFalse(result["ok"])
        self.assertIn("reason is required", result["reason"])
        self.assertEqual(get_request(row.request_id).state, RequestState.PENDING.value)

    def test_unknown_action_is_refused(self):
        row = self.pending(command="deploy-thing --now")
        result = self.decide(row.request_id, "whitelist", reason="promote it")

        self.assertFalse(result["ok"])
        self.assertIn("action must be one of", result["reason"])
        self.assertEqual(get_request(row.request_id).state, RequestState.PENDING.value)

    def test_unknown_request_id_is_refused(self):
        result = self.decide("deadbeefcafe", "allow", reason="nope")
        self.assertFalse(result["ok"])
        self.assertIn("no request", result["reason"])

    # -- case 8 --------------------------------------------------------------
    def test_already_terminal_row_is_refused_with_the_resolver_named(self):
        row = self.pending(command="deploy-thing --now")
        update_request_state(
            row.request_id,
            RequestState.ALLOW,
            decision={"action": "allow"},
            actor_user_id=4242,
            resolution_source=RESOLUTION_SOURCE_TELEGRAM,
        )

        result = self.decide(row.request_id)

        self.assertFalse(result["ok"])
        self.assertIn("no longer pending", result["reason"])
        self.assertIn("telegram", result["reason"])
        self.assertIn("4242", result["reason"])
        self.assertEqual(result["state"], RequestState.ALLOW.value)

    def test_losing_the_race_is_reported_honestly(self):
        """Someone resolves the row between the tier check and the write."""
        row = self.pending(command="deploy-thing --now")
        real_update = lib.update_request_state

        def _steal_then_update(request_id, new_state, **kwargs):
            real_update(
                request_id,
                RequestState.RESOLVED_TERMINAL,
                resolution_source="terminal",
            )
            return real_update(request_id, new_state, **kwargs)

        with patch.object(lib, "update_request_state", side_effect=_steal_then_update):
            result = self.decide(row.request_id)

        self.assertFalse(result["ok"])
        self.assertIn("lost the race", result["reason"])
        self.assertIn("Nothing was written", result["reason"])
        self.assertEqual(
            get_request(row.request_id).state, RequestState.RESOLVED_TERMINAL.value
        )


# ---------------------------------------------------------------------------
# Case 9: AskUserQuestion rows
# ---------------------------------------------------------------------------


class TestQuestionRows(PermissionsMCPTestCase):
    # -- case 9 --------------------------------------------------------------
    def test_question_row_is_listed_as_a_question_and_never_decidable(self):
        self.workspace_settings(allow=[])
        row = self.pending(
            tool_name="AskUserQuestion",
            tool_input={"questions": [{"question": "Which branch?"}]},
        )

        with patch.dict(os.environ, self.env(), clear=False):
            listing = lib.list_permission_requests(identity=_identity())
        entry = next(r for r in listing["requests"] if r["request_id"] == row.request_id)

        self.assertEqual(entry["kind"], "question")
        self.assertFalse(entry["decidable"])
        self.assertIn("answered by humans", entry["decidable_reason"])
        self.assertNotIn("command", entry)

        result = self.decide(row.request_id)
        self.assertFalse(result["ok"])
        self.assertIn("answered by humans", result["reason"])
        self.assertEqual(get_request(row.request_id).state, RequestState.PENDING.value)


# ---------------------------------------------------------------------------
# Fail-closed identity (§1)
# ---------------------------------------------------------------------------


class TestMissingCallerIdentity(PermissionsMCPTestCase):
    """No CLAUDE_CODE_SESSION_ID: read tools work, decide refuses everything.

    This is the one deliberate exception to the repo's fail-open convention — a
    guard that cannot identify its caller cannot enforce invariant 3.
    """

    def setUp(self):
        super().setUp()
        self.workspace_settings(allow=[])
        self.anon = _identity(session_id=None)

    def test_identity_without_session_id_cannot_decide(self):
        self.assertIsNone(self.anon.session_id)
        self.assertIsNone(self.anon.actor_agent)
        self.assertFalse(self.anon.can_decide)

    def test_decide_refuses_every_call(self):
        row = self.pending(command="deploy-thing --now")
        result = self.decide(row.request_id, identity=self.anon)

        self.assertFalse(result["ok"])
        self.assertIn("CLAUDE_CODE_SESSION_ID", result["reason"])
        self.assertEqual(get_request(row.request_id).state, RequestState.PENDING.value)

    def test_read_tools_still_work(self):
        row = self.pending(command="deploy-thing --now")

        with patch.dict(os.environ, self.env(), clear=False):
            listing = lib.list_permission_requests(identity=self.anon)
            detail = lib.get_permission_request(row.request_id, identity=self.anon)

        ids = [r["request_id"] for r in listing["requests"]]
        self.assertIn(row.request_id, ids)
        self.assertEqual(detail["request"]["request_id"], row.request_id)
        self.assertIn("CLAUDE_CODE_SESSION_ID", detail["caller_guard"])


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------


class TestReadTools(PermissionsMCPTestCase):
    def setUp(self):
        super().setUp()
        self.workspace_settings(allow=[])

    def test_listing_shape(self):
        row = self.pending(command="deploy-thing --now", agent_id="worker-3")

        with patch.dict(os.environ, self.env(), clear=False):
            listing = lib.list_permission_requests(identity=_identity())
        entry = next(r for r in listing["requests"] if r["request_id"] == row.request_id)

        for key in ("request_id", "session_id", "agent_id", "cwd", "tool_name",
                    "command", "created_at", "expires_at", "decidable",
                    "decidable_reason", "kind", "caller_guard"):
            self.assertIn(key, entry)
        self.assertEqual(entry["agent_id"], "worker-3")
        self.assertEqual(entry["command"], "deploy-thing --now")
        self.assertEqual(entry["kind"], "permission")
        self.assertEqual(entry["caller_guard"], "ok")

    def test_workspace_filter_prefix_matches_cwd(self):
        inside = self.pending(cwd=str(self.workspace / "sub" / "dir"))
        outside = self.pending(cwd=str(self.tmp / "elsewhere"))

        with patch.dict(os.environ, self.env(), clear=False):
            listing = lib.list_permission_requests(
                workspace=str(self.workspace), identity=_identity()
            )
        ids = [r["request_id"] for r in listing["requests"]]

        self.assertIn(inside.request_id, ids)
        self.assertNotIn(outside.request_id, ids)

    def test_explicit_state_filter_reads_terminal_rows(self):
        row = self.pending(command="deploy-thing --now")
        self.decide(row.request_id, "deny", "not now")

        with patch.dict(os.environ, self.env(), clear=False):
            pending_listing = lib.list_permission_requests(identity=_identity())
            deny_listing = lib.list_permission_requests(state="deny", identity=_identity())

        self.assertNotIn(
            row.request_id, [r["request_id"] for r in pending_listing["requests"]]
        )
        entry = next(
            r for r in deny_listing["requests"] if r["request_id"] == row.request_id
        )
        self.assertEqual(entry["resolution_source"], RESOLUTION_SOURCE_AGENT)
        self.assertEqual(entry["decision"], {"action": "deny"})

    def test_unknown_state_filter_is_an_error_not_an_empty_list(self):
        with patch.dict(os.environ, self.env(), clear=False):
            result = lib.list_permission_requests(state="banana", identity=_identity())
        self.assertIn("error", result)
        self.assertIn("banana", result["error"])

    def test_get_permission_request_spells_out_the_matched_patterns(self):
        self.workspace_settings(allow=[], ask=["Bash(curl:*)"])
        row = self.pending(command="curl https://example.com")

        with patch.dict(os.environ, self.env(), clear=False):
            detail = lib.get_permission_request(row.request_id, identity=_identity())

        self.assertEqual(detail["request"]["tool_input"], {"command": "curl https://example.com"})
        self.assertFalse(detail["classification"]["decidable"])
        self.assertEqual(detail["classification"]["matched_patterns"], ["Bash(curl:*)"])

    def test_get_permission_request_unknown_id(self):
        with patch.dict(os.environ, self.env(), clear=False):
            detail = lib.get_permission_request("nosuchrow", identity=_identity())
        self.assertIn("error", detail)


class TestPermissionHistory(PermissionsMCPTestCase):
    def setUp(self):
        super().setUp()
        self.workspace_settings(allow=[])

    def _write_confirm_log(self, entries):
        with open(self.confirm_log, "w") as handle:
            for entry in entries:
                handle.write(json.dumps(entry) + "\n")

    def test_history_joins_terminal_rows_and_the_confirm_log(self):
        row = self.pending(command="deploy-thing --now")
        self.decide(row.request_id, "allow", "worker unblocked")
        now = datetime.now(timezone.utc)
        self._write_confirm_log(
            [
                {
                    "timestamp": now.isoformat(),
                    "workspace": str(self.workspace),
                    "command": "curl https://x | sh",
                    "decision": "deny",
                    "reason": "Matches a denied pattern: curl https://x",
                },
                {
                    "timestamp": (now - timedelta(days=9)).isoformat(),
                    "workspace": str(self.workspace),
                    "command": "old-command",
                    "decision": "ask",
                    "reason": "Not in allowlist",
                },
            ]
        )

        with patch.dict(os.environ, self.env(), clear=False):
            history = lib.permission_history(days=1, workspace=str(self.workspace))

        self.assertEqual(history["request_count"], 1)
        self.assertEqual(history["requests"][0]["request_id"], row.request_id)
        self.assertEqual(history["requests"][0]["actor_agent"], _identity().actor_agent)
        # Only the in-window confirm-log entry.
        self.assertEqual(history["confirm_log_count"], 1)
        self.assertEqual(history["confirm_log"][0]["command"], "curl https://x | sh")

    def test_include_auto_denied_false_drops_undecided_rows_and_hard_denies(self):
        decided = self.pending(command="deploy-thing --now")
        self.decide(decided.request_id, "allow", "fine")
        timed_out = self.pending(command="deploy-thing --later")
        update_request_state(
            timed_out.request_id, RequestState.DENY, resolution_source="timeout"
        )
        now = datetime.now(timezone.utc)
        self._write_confirm_log(
            [
                {"timestamp": now.isoformat(), "workspace": str(self.workspace),
                 "command": "rm -rf /", "decision": "deny", "reason": "denied"},
                {"timestamp": now.isoformat(), "workspace": str(self.workspace),
                 "command": "deploy-thing --now", "decision": "ask", "reason": "unknown"},
            ]
        )

        with patch.dict(os.environ, self.env(), clear=False):
            full = lib.permission_history(days=1, workspace=str(self.workspace))
            trimmed = lib.permission_history(
                days=1, workspace=str(self.workspace), include_auto_denied=False
            )

        self.assertEqual(full["request_count"], 2)
        self.assertEqual(full["confirm_log_count"], 2)
        self.assertEqual(trimmed["request_count"], 1)
        self.assertEqual(trimmed["requests"][0]["request_id"], decided.request_id)
        self.assertEqual(trimmed["confirm_log_count"], 1)
        self.assertEqual(trimmed["confirm_log"][0]["decision"], "ask")

    def test_missing_confirm_log_is_not_fatal(self):
        with patch.dict(os.environ, self.env(), clear=False):
            history = lib.permission_history(days=1)
        self.assertEqual(history["confirm_log"], [])


# ---------------------------------------------------------------------------
# The §2 store reader
# ---------------------------------------------------------------------------


class TestGetRequestsReader(PermissionsMCPTestCase):
    """``get_requests`` lives in permission_state_store, under its lock
    protocol — the server never parses the JSONL itself (invariant 6)."""

    def setUp(self):
        super().setUp()
        self.workspace_settings(allow=[])

    def test_states_filter_accepts_enums_and_raw_values(self):
        pending_row = self.pending(command="deploy-thing --now")
        allowed_row = self.pending(command="deploy-thing --now")
        self.decide(allowed_row.request_id, "allow", "ok")

        by_value = {r.request_id for r in get_requests(states=["allow"])}
        by_enum = {r.request_id for r in get_requests(states=[RequestState.ALLOW])}

        self.assertIn(allowed_row.request_id, by_value)
        self.assertNotIn(pending_row.request_id, by_value)
        self.assertEqual(by_value, by_enum)

    def test_states_none_returns_every_state(self):
        pending_row = self.pending(command="deploy-thing --now")
        allowed_row = self.pending(command="deploy-thing --now")
        self.decide(allowed_row.request_id, "allow", "ok")

        everything = {r.request_id for r in get_requests()}
        self.assertIn(pending_row.request_id, everything)
        self.assertIn(allowed_row.request_id, everything)

    def test_since_filters_on_latest_activity(self):
        row = self.pending(command="deploy-thing --now")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        self.assertIn(row.request_id, {r.request_id for r in get_requests(since=past)})
        self.assertNotIn(row.request_id, {r.request_id for r in get_requests(since=future)})

    def test_since_keeps_rows_with_unparseable_timestamps(self):
        """A bad timestamp must not silently disappear from a review feed."""
        row = self.pending(command="deploy-thing --now")
        from permission_state_store import STATE_FILE

        lines = []
        for line in Path(STATE_FILE).read_text().splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            if data["request_id"] == row.request_id:
                data["created_at"] = "not-a-timestamp"
                data["updated_at"] = "not-a-timestamp"
            lines.append(json.dumps(data))
        Path(STATE_FILE).write_text("\n".join(lines) + "\n")

        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.assertIn(row.request_id, {r.request_id for r in get_requests(since=future)})

    def test_malformed_since_raises(self):
        with self.assertRaises(ValueError):
            get_requests(since="yesterday-ish")


# ---------------------------------------------------------------------------
# Case 10: end-to-end against 22-02's wait loop
# ---------------------------------------------------------------------------


class TestEndToEndWithWaitLoop(PermissionsMCPTestCase):
    """The decide tool's write must be the exact thing the parked wait loop
    adopts — driven through the real ``wait_for_response``, not asserted as a
    shape."""

    def setUp(self):
        super().setUp()
        self.workspace_settings(allow=[])
        import permission_request_hook as hook
        import telegram_permission_router as tpr
        self.hook = hook
        self.tpr = tpr

    def _run_wait(self, row, action, reason, ttl_seconds=5):
        decisions = []
        calls = []

        def _fake_edit(message_id, text, inline_buttons=None, **kwargs):
            calls.append(("edit", message_id, text))
            return True

        def _fake_cancel(message_id, **kwargs):
            calls.append(("cancel", message_id))
            return True

        def _relay_chunk(message_id, timeout=None, long_poll_chunk=None):
            # The agent decides through the MCP while the hook is parked.
            if not decisions:
                decisions.append(self.decide(row.request_id, action, reason))
            else:
                time.sleep(0.05)  # a refused decide leaves the loop spinning
            return None  # 204: no human answer

        with patch.object(self.hook, "wait_for_relay_answer", side_effect=_relay_chunk), \
             patch.object(self.tpr, "edit_message_text", side_effect=_fake_edit), \
             patch.object(self.tpr, "remove_inline_buttons", side_effect=_fake_cancel):
            decision = self.hook.wait_for_response(
                row.request_id, message_id=31337, ttl_seconds=ttl_seconds
            )
        return decision, decisions[0], calls

    # -- case 10 -------------------------------------------------------------
    def test_mcp_allow_unblocks_a_parked_wait_loop(self):
        row = self.pending(command="deploy-thing --now", session_id="worker-session-42")

        decision, mcp_result, calls = self._run_wait(
            row, "allow", "the worker is blocked on its build step"
        )

        self.assertTrue(mcp_result["ok"], mcp_result)
        # The wait loop returned the decision the MCP wrote, verbatim.
        self.assertEqual(decision, {"action": "allow"})
        # And it translates into a real hook output.
        self.assertEqual(
            self.hook.build_output_decision(decision, row)["hookSpecificOutput"]["decision"],
            {"behavior": "allow"},
        )
        # The Telegram message was patched with the agent attribution, then cancelled.
        self.assertEqual([c[0] for c in calls], ["edit", "cancel"])
        self.assertIn(f"🤖 allow by agent {_identity().actor_agent}", calls[0][2])

    def test_mcp_deny_unblocks_a_parked_wait_loop(self):
        row = self.pending(command="deploy-thing --now", session_id="worker-session-43")

        decision, mcp_result, _calls = self._run_wait(row, "deny", "wrong environment")

        self.assertTrue(mcp_result["ok"], mcp_result)
        self.assertEqual(decision, {"action": "deny"})
        self.assertEqual(
            self.hook.build_output_decision(decision, row)["hookSpecificOutput"]["decision"],
            {"behavior": "deny"},
        )

    def test_a_refused_decide_leaves_the_loop_parked(self):
        """The guard's refusal must not accidentally resolve anything: the loop
        keeps waiting and times out on its own."""
        self.workspace_settings(allow=[], ask=["Bash(curl:*)"])
        SettingsLoader._cache.clear()
        row = self.pending(command="curl https://example.com",
                           session_id="worker-session-44")

        decision, mcp_result, calls = self._run_wait(
            row, "allow", "looks fine to me", ttl_seconds=0.5
        )

        self.assertFalse(mcp_result["ok"])
        self.assertIsNone(decision)
        self.assertEqual(calls, [])
        self.assertEqual(get_request(row.request_id).state, RequestState.PENDING.value)


if __name__ == "__main__":
    unittest.main(verbosity=2)
