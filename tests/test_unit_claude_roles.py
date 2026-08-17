#!/usr/bin/env python3
"""Tests for shell/claude-roles — availability column in --check output (19-06).

Coverage:
- ``_check_tokens`` against an old relay that omits the availability fields
  (tz / active_now / nudge_enabled): blank column, no error, exit 0.
- ``_check_tokens`` against a new relay that includes the availability fields:
  ``active_now`` is stored verbatim from the server response — the test asserts
  the stored value equals the server's, which is the guard that catches a
  well-meaning local recompute on a machine in a different timezone.
- The column is blank when the chat is not bound (fields are never present).

Isolation: no network, no real bot token, ``httpx.get`` is monkey-patched.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── Load claude-roles as a module ─────────────────────────────────────────────
# The script's module-level code calls _load_roles_config() which walks up from
# shell/claude-roles to find .claude/hooks/roles_config.py in the repo root.
# That file exists in a normal repo checkout, so the exec is safe.
#
# spec_from_file_location returns None for files with no .py extension, so we
# use SourceFileLoader directly — the stdlib-sanctioned workaround.

from importlib.machinery import SourceFileLoader as _SourceFileLoader

_SCRIPT = Path(__file__).parent.parent / "shell" / "claude-roles"
_loader = _SourceFileLoader("claude_roles_mod", str(_SCRIPT))
_spec = importlib.util.spec_from_loader("claude_roles_mod", _loader)
_cr_mod = importlib.util.module_from_spec(_spec)
sys.modules["claude_roles_mod"] = _cr_mod
_loader.exec_module(_cr_mod)

# After load, _cr_mod.rc is the imported roles_config module.

# Load roles_config directly so we can build fixtures.
_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
if str(_HOOKS) not in sys.path:
    sys.path.insert(0, str(_HOOKS))
import roles_config as _rc


# ── Fixture helpers ───────────────────────────────────────────────────────────

_MINIMAL_ROLES_TOML = """\
workspace_id = "test-ws"
default      = "op"

[role.op]
aliases = ["op"]
title   = "Operator"
"""

_CONFIG_WITH_TOKEN = """\
server_url         = "http://test-relay"
installation_token = "rly_test_token_xxxx"
"""


def _make_workspace(tmp: Path, roles_toml: str) -> Path:
    ws = tmp / "workspace"
    ws.mkdir()
    dot_claude = ws / ".claude"
    dot_claude.mkdir()
    (dot_claude / "roles.toml").write_text(textwrap.dedent(roles_toml))
    return ws


def _fake_resp(json_body: dict, status_code: int = 200):
    """Build a minimal fake httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    return resp


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestCheckTokensAvailabilityColumn(unittest.TestCase):
    """Availability fields in ``_check_tokens`` output."""

    def setUp(self):
        self._tmp_obj = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp_obj.name)
        ws = _make_workspace(self.tmp, _MINIMAL_ROLES_TOML)
        cfg_path = self.tmp / "config.toml"
        cfg_path.write_text(textwrap.dedent(_CONFIG_WITH_TOKEN))
        self.catalog = _rc.load_catalog(
            str(ws), path=ws / ".claude" / "roles.toml"
        )
        self.bindings = _rc.load_bindings(cfg_path)
        assert self.catalog is not None, "catalog must load for tests to be valid"

    def tearDown(self):
        self._tmp_obj.cleanup()

    # ── Helper ────────────────────────────────────────────────────────────────

    def _run_check_tokens(self, relay_response: dict, status_code: int = 200):
        """Invoke ``_check_tokens`` with ``httpx.get`` patched to return *relay_response*."""
        import httpx as _httpx
        original_get = _httpx.get
        try:
            _httpx.get = lambda *a, **kw: _fake_resp(relay_response, status_code)
            results, any_problem = _cr_mod._check_tokens(self.catalog, self.bindings)
        finally:
            _httpx.get = original_get
        return results, any_problem

    # ── Old relay ─────────────────────────────────────────────────────────────

    def test_old_relay_no_fields_blank_and_no_problem(self):
        """Old relay without tz/active_now/nudge_enabled → no avail info, exit 0."""
        results, any_problem = self._run_check_tokens({
            "id": 1,
            "label": "test-machine",
            "chat_bound": True,
            "last_seen_at": None,
            # No tz, active_now, nudge_enabled
        })

        self.assertFalse(any_problem, "old relay must not set any_problem")
        self.assertEqual(len(results), 1)
        r = results[0]
        self.assertEqual(r["status"], "bound")
        # Availability fields must be absent / None — no local default.
        self.assertIsNone(r.get("tz"))
        self.assertIsNone(r.get("active_now"))
        self.assertIsNone(r.get("nudge_enabled"))

    def test_old_relay_unbound_also_no_avail_fields(self):
        """Old relay with chat_bound=False → not-bound status, no avail fields."""
        results, any_problem = self._run_check_tokens({
            "id": 2,
            "label": "unbound-machine",
            "chat_bound": False,
            "last_seen_at": None,
        })

        self.assertTrue(any_problem, "unbound must set any_problem")
        self.assertIn("not bound", results[0]["status"])
        self.assertIsNone(results[0].get("tz"))
        self.assertIsNone(results[0].get("active_now"))
        self.assertIsNone(results[0].get("nudge_enabled"))

    # ── New relay — verbatim storage, no local recompute ─────────────────────

    def test_new_relay_active_now_true_stored_verbatim(self):
        """active_now=True from server is stored as True, not recomputed locally."""
        # The server says the recipient is active.  The local machine's timezone
        # is irrelevant — this is the assertion that catches a local recompute.
        results, any_problem = self._run_check_tokens({
            "id": 1,
            "label": "test-machine",
            "chat_bound": True,
            "last_seen_at": None,
            "tz": "America/New_York",
            "windows": "mon-fri 09:00-17:00",
            "active_now": True,
            "nudge_enabled": False,
        })

        self.assertFalse(any_problem)
        r = results[0]
        self.assertEqual(r["status"], "bound")
        self.assertEqual(r["tz"], "America/New_York")
        # active_now must be exactly what the server returned, not a local
        # recomputation (which would differ if the machine is in a different zone).
        self.assertIs(r["active_now"], True)
        self.assertIs(r["nudge_enabled"], False)

    def test_new_relay_active_now_false_stored_verbatim(self):
        """active_now=False from server is stored as False, not recomputed."""
        results, _ = self._run_check_tokens({
            "id": 1,
            "label": "off-hours",
            "chat_bound": True,
            "last_seen_at": None,
            "tz": "Asia/Tokyo",
            "windows": "mon-fri 09:00-18:00",
            "active_now": False,
            "nudge_enabled": True,
        })
        r = results[0]
        self.assertIs(r["active_now"], False)
        self.assertIs(r["nudge_enabled"], True)
        self.assertEqual(r["tz"], "Asia/Tokyo")

    def test_new_relay_nudge_enabled_on(self):
        """nudge_enabled=True stored as True."""
        results, _ = self._run_check_tokens({
            "id": 1,
            "label": "nudger",
            "chat_bound": True,
            "last_seen_at": None,
            "tz": "Europe/Berlin",
            "windows": None,
            "active_now": True,
            "nudge_enabled": True,
        })
        r = results[0]
        self.assertIs(r["nudge_enabled"], True)

    def test_new_relay_no_tz_configured(self):
        """A bound chat with no tz set returns tz=None from server."""
        results, _ = self._run_check_tokens({
            "id": 1,
            "label": "no-tz",
            "chat_bound": True,
            "last_seen_at": None,
            "tz": None,
            "windows": None,
            "active_now": True,
            "nudge_enabled": False,
        })
        r = results[0]
        # tz=None means it was present in the response (new relay) but not configured.
        # The column must stay blank — not shown — when the key returns None.
        self.assertIsNone(r.get("tz"))

    def test_active_now_identical_across_local_timezones(self):
        """active_now is the server's verbatim value regardless of the local TZ.

        The server reports active_now=False for a Pacific/Kiritimati window.
        Under UTC (14 hours behind Kiritimati) a local recompute might yield a
        different answer.  The guard: run under both UTC and Pacific/Kiritimati
        and assert the stored value is identical in both — and equals what the
        server returned.
        """
        relay_response = {
            "id": 1,
            "label": "kiritimati-chat",
            "chat_bound": True,
            "last_seen_at": None,
            "tz": "Pacific/Kiritimati",
            "windows": "mon-fri 09:00-18:00",
            # Server says False.  A local recompute in UTC could disagree.
            "active_now": False,
            "nudge_enabled": True,
        }

        results_by_tz: dict[str, object] = {}
        for zone in ("UTC", "Pacific/Kiritimati"):
            saved = os.environ.get("TZ")
            try:
                os.environ["TZ"] = zone
                results, _ = self._run_check_tokens(relay_response)
                results_by_tz[zone] = results[0]["active_now"]
            finally:
                if saved is None:
                    os.environ.pop("TZ", None)
                else:
                    os.environ["TZ"] = saved

        self.assertIs(results_by_tz["UTC"], False,
                      "active_now must equal the server value under UTC")
        self.assertIs(results_by_tz["Pacific/Kiritimati"], False,
                      "active_now must equal the server value under Pacific/Kiritimati")
        self.assertIs(results_by_tz["UTC"], results_by_tz["Pacific/Kiritimati"],
                      "active_now must be identical regardless of the local TZ")

    def test_old_relay_main_exits_zero(self):
        """main() returns 0 (not just any_problem=False) when the relay lacks avail fields.

        This is the integration-level guard for the degradation requirement: the
        *exit code* from main() must be 0 against an old relay, not just the
        internal any_problem flag.
        """
        import httpx as _httpx

        old_relay_response = {
            "id": 1,
            "label": "old-relay-machine",
            "chat_bound": True,
            "last_seen_at": None,
            # No tz, active_now, nudge_enabled — as returned by a pre-19-02 relay.
        }

        original_get = _httpx.get
        try:
            _httpx.get = lambda *a, **kw: _fake_resp(old_relay_response, 200)
            with patch("sys.argv", ["claude-roles", "--check",
                                    "--workspace-dir", str(self.tmp / "workspace")]):
                exit_code = _cr_mod.main()
        finally:
            _httpx.get = original_get

        self.assertEqual(exit_code, 0,
                         "main() must return 0 (not 2) when old relay omits avail fields")


if __name__ == "__main__":
    unittest.main()
