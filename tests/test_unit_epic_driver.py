#!/usr/bin/env python3
"""
Unit tests: the epic driver's lib (``.claude/hooks/epic_driver_lib.py``, epic 50).

One class per area; later tasks (50-05 … 50-08) add their own sections below
the delimited blocks of 50-04.

Isolation: every test points ``EPIC_DRIVER_STATE_ROOT`` at a temp dir, so
nothing writes ``~/.local/state/epic-driver``. Git runs only on repos created
in temp dirs; no tmux, no network, nothing under ``~/.claude`` (profiles come
from a fake ``World``). The process-group test kills its own children.

Run this module alone:
    python3 tests/run_all_tests.py --module unit_epic_driver
"""

import copy
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

import yaml

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "context-mcp"))
sys.path.insert(0, str(_REPO / ".claude" / "hooks"))

import epic_driver_lib as edl  # noqa: E402

ARCHITECTURE = _REPO / "tasks" / "50_epic_driver" / "architecture.md"
SHELL_ADDED_ENV = {"PWD", "SHLVL", "_", "OLDPWD"}


def T(text):
    return edl.parse_utc_time(text)


class FakeWorld(edl.World):
    """A fake clock, fake git repos, scripted checks and fake profiles."""

    def __init__(self, now=None, profiles=None, repos=None):
        self.clock = now or T("2026-11-24T00:00:00Z")
        self.profiles = profiles or {}
        self.repos = repos or {}          # realpath -> {"refs": set of refs}
        self.git_calls = []
        self.refused = []                 # git argvs the allow list refused: a correct run has none
        self.check_calls = []
        self.check_script = {}            # step id -> (rc, seconds)

    def now(self):
        return self.clock

    def advance(self, seconds):
        self.clock = self.clock + timedelta(seconds=seconds)

    def git(self, args, cwd, timeout_s=30.0):
        args = list(args)
        self.git_calls.append((tuple(args), cwd))
        refused = edl.git_refusal(args)          # the same runtime allow list as RealWorld.git
        if refused:
            self.refused.append(args)
            return edl.CommandResult(rc=None, error=refused)
        repo = self.repos.get(cwd)
        if repo is None:
            return edl.CommandResult(rc=128, stderr="fatal: not a git repository")
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return edl.CommandResult(rc=0, stdout="true\n")
        if args[:2] == ["rev-parse", "--verify"]:
            ref = args[-1]
            assert ref.endswith("^{commit}"), ref
            return edl.CommandResult(rc=0 if ref[: -len("^{commit}")] in repo["refs"] else 1)
        raise AssertionError(f"unexpected git call {args}")

    def run_check(self, command, cwd, env, timeout_s):
        sid = env["EPIC_DRIVER_STEP"]
        self.check_calls.append((sid, command, cwd, dict(env), timeout_s))
        rc, seconds = self.check_script.get(sid, (1, 1))
        started = self.clock
        self.advance(seconds)
        return edl.CheckRun(rc=rc, started_at=started, duration_s=seconds, output_tail=f"out {sid}\n")

    def profile_env(self, name):
        if name not in self.profiles:
            raise ValueError(f"Unknown profile: {name!r}")
        return dict(self.profiles[name])


class RecordingRealWorld(edl.RealWorld):
    """RealWorld that remembers every argv the runtime allow list refused."""

    def __init__(self):
        self.refused = []

    def git(self, args, cwd, timeout_s=30.0):
        res = super().git(args, cwd, timeout_s)
        if res.rc is None and (res.error or "").startswith("git refused"):
            self.refused.append(list(args))
        return res


class _Base(unittest.TestCase):
    """A temp workspace (``<tmp>/ws``, plan root ``plan``) and an isolated state root."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="epic-driver-test-"))
        self._env = mock.patch.dict(os.environ, {edl.STATE_ROOT_ENV: str(self.tmp / "state")})
        self._env.start()
        os.environ.pop("CONTEXT_WINDOW_MAP", None)
        self.ws = self.tmp / "ws"
        self.plan = self.ws / "plan"
        (self.plan / "cards").mkdir(parents=True)
        (self.plan / "cards" / "c.md").write_text("# card\n")
        self.world = FakeWorld()

    def tearDown(self):
        try:
            self.assertEqual(self.world.refused, [], "the module attempted a git form the allow list refuses")
        finally:
            self._env.stop()
            shutil.rmtree(str(self.tmp), ignore_errors=True)

    # -- ledger builders -------------------------------------------------
    def step(self, sid, **kw):
        d = {"id": sid, "title": sid, "card": "cards/c.md"}
        d.update(kw)
        return d

    def ledger_dict(self, steps, **top):
        d = {
            "version": 1,
            "epic": "t",
            "workspace": str(self.ws),
            "plan_root": "plan",
            "roles": {"driver": {"model": "claude-opus-5-5", "effort": "high"}},
            "steps": steps,
        }
        d.update(top)
        return d

    def write(self, data, name="ledger.yaml"):
        path = self.plan / name
        if isinstance(data, str):
            path.write_text(data)
        else:
            path.write_text(yaml.safe_dump(data, sort_keys=False))
        return str(path)

    def load(self, data, world=None, **kw):
        return edl.load_ledger(self.write(data), world or self.world, **kw)

    def issues(self, data, world=None, **kw):
        ledger, errors, warnings = edl.validate_ledger(self.write(data), world or self.world, **kw)
        return errors, warnings

    def assertIssue(self, issues, category, where, text=None):
        hits = [i for i in issues if i.category == category and where in i.where
                and (text is None or text in i.message)]
        self.assertTrue(hits, f"no {category} issue at {where!r} ({text!r}) in:\n  "
                        + "\n  ".join(repr(i) for i in issues))
        return hits[0]

    def assertError(self, data, category, where, text=None, world=None):
        errors, _ = self.issues(data, world)
        return self.assertIssue(errors, category, where, text)


# =============================================================================
# 50-04 — value grammar and names
# =============================================================================

class TestGrammar(unittest.TestCase):
    def test_durations(self):
        for text, secs in (("90s", 90), ("20m", 1200), ("2h", 7200), ("1h30m", 5400), ("7d", 604800),
                           ("0s", 0)):
            with self.subTest(text=text):
                self.assertEqual(edl.parse_duration(text), secs)
                self.assertEqual(edl.parse_duration(edl.format_duration(secs)), secs)
        for bad in ("1 h", "", "5", "1h 30m", 5, "1w", "h1", None):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                edl.parse_duration(bad)

    def test_utc_times_normalised_to_utc(self):
        z = T("2026-11-24T09:28:00Z")
        self.assertEqual(z.utcoffset(), timedelta(0))
        self.assertEqual(T("2026-11-24T11:28:00+02:00"), z)
        self.assertEqual(T("2026-11-24T03:58:00-05:30"), z)
        self.assertEqual(T("2026-11-24T09:28:00.5Z").microsecond, 500000)
        self.assertEqual(edl.iso(T("2026-11-24T11:28:00+02:00")), "2026-11-24T09:28:00Z")
        # YAML types an unquoted timestamp as an aware datetime.
        self.assertEqual(edl.parse_utc_time(yaml.safe_load("t: 2026-11-24T11:28:00+02:00")["t"]), z)
        for bad in ("2026-11-24T09:28:00", "2026-11-24", "09:28Z", 12,
                    yaml.safe_load("t: 2026-11-24T09:28:00")["t"]):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                edl.parse_utc_time(bad)

    def test_tod(self):
        self.assertEqual(edl.parse_tod("06:00Z"), 6 * 3600)
        self.assertEqual(edl.parse_tod("23:59Z"), 86340)
        for bad in ("24:00Z", "6:00Z", "06:00", "06:60Z"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                edl.parse_tod(bad)

    def test_daily_window(self):
        w = edl.parse_daily_window("06:00-12:00Z")
        self.assertEqual(w.length_s, 6 * 3600)
        self.assertFalse(w.crosses_midnight)
        self.assertTrue(w.contains(T("2026-11-24T06:00:00Z")))
        self.assertTrue(w.contains(T("2026-11-24T11:59:59Z")))
        self.assertFalse(w.contains(T("2026-11-24T12:00:00Z")))
        self.assertFalse(w.contains(T("2026-11-24T05:59:59Z")))
        with self.assertRaises(ValueError):
            edl.parse_daily_window("06:00-06:00Z")
        with self.assertRaises(ValueError):
            edl.parse_daily_window("06:00-12:00")

    def test_daily_window_across_midnight(self):
        w = edl.parse_daily_window("22:00-04:00Z")
        self.assertTrue(w.crosses_midnight)
        self.assertEqual(w.length_s, 6 * 3600)
        for t, inside in (("22:00", True), ("23:30", True), ("00:00", True), ("03:59", True),
                          ("04:00", False), ("21:59", False), ("12:00", False)):
            with self.subTest(t=t):
                self.assertEqual(w.contains(T(f"2026-11-24T{t}:00Z")), inside)
        self.assertEqual(w.offset(T("2026-11-25T01:00:00Z")), 3 * 3600)
        # coverage across midnight: 23:00 → 02:00 is 3 h inside
        self.assertEqual(w.coverage_s(T("2026-11-24T23:00:00Z"), T("2026-11-25T02:00:00Z")), 3 * 3600)
        # 20:00 → next day 06:00 covers the whole 6 h window once
        self.assertEqual(w.coverage_s(T("2026-11-24T20:00:00Z"), T("2026-11-25T06:00:00Z")), 6 * 3600)
        self.assertEqual(w.next_open(T("2026-11-24T12:00:00Z")), T("2026-11-24T22:00:00Z"))
        self.assertEqual(w.next_open(T("2026-11-25T01:00:00Z")), T("2026-11-24T22:00:00Z"))

    def test_daily_window_coverage_starting_after_midnight(self):
        # The part of last evening's window instance that lies after midnight must count when the
        # span starts there (a day loop starting at a.date() would miss it).
        w = edl.parse_daily_window("22:00-04:00Z")
        self.assertEqual(w.coverage_s(T("2026-11-25T01:00:00Z"), T("2026-11-25T03:00:00Z")), 2 * 3600)
        self.assertEqual(w.coverage_s(T("2026-11-25T03:00:00Z"), T("2026-11-25T05:00:00Z")), 3600)
        self.assertEqual(w.coverage_s(T("2026-11-25T05:00:00Z"), T("2026-11-25T21:00:00Z")), 0)

    def test_step_id_grammar(self):
        for ok in ("T1", "e1-useast01-roll", "a.b_c-d", "0"):
            self.assertTrue(edl.is_step_id(ok), ok)
        for bad in ("-a", ".a", "", "a b", "a/b", 1):
            self.assertFalse(edl.is_step_id(bad), bad)

    def test_sanitise_and_collisions(self):
        self.assertEqual(edl.sanitise("T1"), "t1")
        self.assertEqual(edl.sanitise("e1.useast_01"), "e1-useast-01")
        self.assertEqual(edl.sanitise("--A--"), "a")
        self.assertEqual(edl.sanitise("037"), "037")
        self.assertEqual(edl.sanitise("T.1"), edl.sanitise("t-1"))   # a collision the validator rejects
        with self.assertRaises(ValueError):
            edl.sanitise("...")

    def test_name_builder_matches_architecture_examples(self):
        self.assertEqual(edl.session_suffix("037", "driver", None, 1), "037-driver-g1")
        self.assertEqual(edl.session_name("acdn-workspace", edl.session_suffix("037", "driver", None, 2)),
                         "acdn-workspace-037-driver-g2")
        self.assertEqual(edl.worker_dir_name("037", "T1"), "037-t1")
        self.assertEqual(edl.session_name(edl.worker_dir_name("037", "T1"),
                                          edl.session_suffix("037", "implementer", "T1", 1)),
                         "037-t1-037-implementer-t1-g1")
        self.assertEqual(edl.session_suffix("037", "reviewer", "T1", 1), "037-reviewer-t1-g1")
        for bad in (0, -1, True, "1"):
            with self.assertRaises(ValueError):
                edl.session_suffix("037", "driver", None, bad)


# =============================================================================
# 50-04 — ledger validation (§5.5): one test per error, both warnings
# =============================================================================

class TestLedgerValidation(_Base):
    def test_minimal_ledger_loads_with_defaults(self):
        L = self.load(self.ledger_dict([self.step("A", role="driver", check="true")]))
        self.assertEqual(L.run_id, "epic-t")
        self.assertEqual(L.plan_dir, str(self.plan))
        self.assertTrue(L.worktree_root_default)
        self.assertEqual(L.worktree_root, str(self.tmp / "state" / "epic-t" / "worktrees"))   # OQ-1
        s = L.step("A")
        self.assertEqual((s.check.timeout_s, s.check.every_s), (60, 600))
        self.assertEqual((s.timeout_s, s.max_attempts, s.warn_before_s), (6 * 3600, 3, 24 * 3600))
        self.assertEqual(s.card, str(self.plan / "cards" / "c.md"))
        r = L.roles["driver"]
        self.assertEqual((r.cap, r.bootstrap_tokens, r.keep_warm, r.stuck_after_s), (250000, 40000, True, 1800))
        self.assertEqual(r.expected_model_id, "claude-opus-5-5")
        self.assertEqual(L.startup_dialogs, [])
        self.assertEqual(L.warnings, [])
        self.assertEqual(len(L.sha256), 64)

    def test_explicit_run_id_worktree_root_and_defaults(self):
        L = self.load(self.ledger_dict(
            [self.step("A", role="driver", check={"run": "true", "timeout": "2m", "every": "30m"})],
            run_id="my-run", worktree_root="/srv/wt",
            defaults={"check_timeout": "90s", "check_every": "20m", "timeout": "1h", "max_attempts": 5,
                      "warn_before": "2h"}))
        self.assertEqual((L.run_id, L.worktree_root, L.worktree_root_default), ("my-run", "/srv/wt", False))
        self.assertEqual((L.defaults.check_timeout_s, L.defaults.check_every_s), (90, 1200))
        s = L.step("A")
        self.assertEqual((s.check.timeout_s, s.check.every_s, s.timeout_s, s.max_attempts), (120, 1800, 3600, 5))

    def test_load_ledger_raises_with_step_and_field(self):
        path = self.write(self.ledger_dict([self.step("A", role="driver", check="true", needs=["nope"])]))
        with self.assertRaises(edl.LedgerError) as cm:
            edl.load_ledger(path, self.world)
        self.assertIn("steps.A.needs", str(cm.exception))
        self.assertIn("'nope'", str(cm.exception))

    # -- schema --------------------------------------------------------------
    def test_schema_unknown_top_key(self):
        self.assertError(self.ledger_dict([self.step("A", check="true")], colour="red"), edl.E_SCHEMA, "colour",
                         "unknown key")

    def test_schema_unknown_step_key(self):
        self.assertError(self.ledger_dict([self.step("A", check="true", granted=True)]), edl.E_SCHEMA,
                         "steps.A.granted", "unknown key")

    def test_schema_unknown_role_key(self):
        d = self.ledger_dict([self.step("A", check="true")])
        d["roles"]["driver"]["temperature"] = 1
        self.assertError(d, edl.E_SCHEMA, "roles.driver.temperature", "unknown key")

    def test_schema_wrong_types(self):
        d = self.ledger_dict([self.step("A", check="true", needs="B", window=6)])
        d["roles"]["driver"]["cap"] = "big"
        d["roles"]["driver"]["keep_warm"] = "yes"
        errors, _ = self.issues(d)
        self.assertIssue(errors, edl.E_SCHEMA, "roles.driver.cap", "int")
        self.assertIssue(errors, edl.E_SCHEMA, "roles.driver.keep_warm")
        self.assertIssue(errors, edl.E_SCHEMA, "steps.A.needs", "list")
        self.assertIssue(errors, edl.E_SCHEMA, "steps.A.window")

    def test_schema_version_not_1(self):
        self.assertError(self.ledger_dict([self.step("A", check="true")], version=2), edl.E_SCHEMA, "version",
                         "must be 1")

    def test_schema_version_must_be_an_int(self):
        for bad in (True, 1.0, "1"):
            with self.subTest(version=bad):
                self.assertError(self.ledger_dict([self.step("A", check="true")], version=bad),
                                 edl.E_SCHEMA, "version", "must be 1")
        self.assertEqual(self.issues(self.ledger_dict([self.step("A", check="true")], version=1))[0], [])

    def test_schema_duplicate_keys_are_an_error(self):
        base = yaml.safe_dump(self.ledger_dict([self.step("A", check="true")]), sort_keys=False)
        cases = {
            "top level": base + "epic: other\n",
            "in a step": base.replace("  title: A\n", "  title: A\n  title: B\n"),
            "in a role": base.replace("    effort: high\n", "    effort: high\n    effort: low\n"),
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertNotEqual(text, base)
                errors, _ = self.issues(text)
                self.assertEqual(len(errors), 1, errors)
                self.assertEqual(errors[0].category, edl.E_SCHEMA)
                self.assertIn("duplicate key", errors[0].message)
                self.assertRegex(errors[0].message, r"line \d+")
        errors, _ = self.issues(cases["top level"])
        self.assertIn("'epic'", errors[0].message)
        self.assertIn(f"line {base.count(chr(10)) + 1}", errors[0].message)

    def _merge_ledger(self, roles_text):
        base = yaml.safe_dump(self.ledger_dict([self.step("A", check="true")]), sort_keys=False)
        head, _, tail = base.partition("roles:\n")
        _, _, rest = tail.partition("steps:\n")
        return head + "roles:\n" + roles_text + "steps:\n" + rest

    def test_schema_merge_keys_are_supported(self):
        cases = {
            "anchor": ("  driver: &base\n    model: claude-opus-5-5\n    effort: high\n"
                       "  worker:\n    <<: *base\n", "high"),
            "list": ("  driver: &a\n    model: claude-opus-5-5\n    effort: high\n"
                     "  w1: &b\n    model: claude-sonnet-5-5\n    effort: low\n"
                     "  worker:\n    <<: [*a, *b]\n", "high"),
            "explicit overrides merged": ("  driver: &base\n    model: claude-opus-5-5\n    effort: high\n"
                                          "  worker:\n    <<: *base\n    effort: low\n", "low"),
        }
        for name, (roles, effort) in cases.items():
            with self.subTest(name):
                raw = yaml.load(self._merge_ledger(roles), Loader=edl._UniqueKeyLoader)
                self.assertEqual(raw["roles"]["worker"]["effort"], effort)
                self.assertEqual(raw["roles"]["worker"]["model"], "claude-opus-5-5")

    def test_schema_merge_list_earlier_source_wins(self):
        roles = ("  driver: &a\n    model: claude-opus-5-5\n    effort: high\n"
                 "  w1: &b\n    model: claude-sonnet-5-5\n    effort: low\n"
                 "  worker:\n    <<: [*b, *a]\n")
        raw = yaml.load(self._merge_ledger(roles), Loader=edl._UniqueKeyLoader)
        self.assertEqual(raw["roles"]["worker"]["model"], "claude-sonnet-5-5")
        self.assertEqual(raw["roles"]["worker"]["effort"], "low")

    def test_schema_explicit_duplicate_beside_a_merge_is_still_an_error(self):
        roles = ("  driver: &base\n    model: claude-opus-5-5\n    effort: high\n"
                 "  worker:\n    <<: *base\n    effort: low\n    effort: high\n")
        text = self._merge_ledger(roles)
        errors, _ = self.issues(text)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("duplicate key 'effort'", errors[0].message)
        line = text.splitlines().index("    effort: high", text.splitlines().index("    effort: low")) + 1
        self.assertIn(f"line {line}", errors[0].message)

    def test_schema_ledger_with_merged_role_defaults_validates(self):
        roles = ("  driver: &base\n    model: claude-opus-5-5\n    effort: high\n"
                 "  reviewer:\n    <<: *base\n    effort: low\n")
        text = self._merge_ledger(roles)
        errors, _ = self.issues(text)
        self.assertEqual(errors, [])
        ledger = self.load(text)
        self.assertEqual(ledger.roles["reviewer"].effort, "low")
        self.assertEqual(ledger.roles["reviewer"].model, "claude-opus-5-5")

    def test_schema_zero_durations_are_errors(self):
        zero = "0s"
        cases = {
            "steps.A.timeout": lambda d: d["steps"][0].update(timeout=zero),
            "steps.A.check.timeout": lambda d: d["steps"][0].update(check={"run": "true", "timeout": zero}),
            "steps.A.check.every": lambda d: d["steps"][0].update(check={"run": "true", "every": zero}),
            "steps.A.trigger.every": lambda d: d["steps"][0].update(trigger={"run": "true", "every": zero}),
            "steps.A.every": lambda d: d["steps"][0].update(every=zero),
            "steps.A.every.period": lambda d: d["steps"][0].update(every={"period": zero}),
            "steps.A.duration": lambda d: d["steps"][0].update(duration=zero),
            "steps.A.check_window.min": lambda d: d["steps"][0].update(
                check_window={"daily": "14:00-22:00Z", "min": zero}),
            "defaults.timeout": lambda d: d.update(defaults={"timeout": zero}),
            "defaults.check_timeout": lambda d: d.update(defaults={"check_timeout": zero}),
            "defaults.check_every": lambda d: d.update(defaults={"check_every": zero}),
            "roles.driver.stuck_after": lambda d: d["roles"]["driver"].update(stuck_after=zero),
        }
        for where, mutate in cases.items():
            with self.subTest(where):
                d = self.ledger_dict([self.step("A", check="true")])
                mutate(d)
                self.assertError(d, edl.E_SCHEMA, where, "must be > 0")
        # zero is meaningful for delays and warnings
        d = self.ledger_dict([self.step("A", check="true", warn_before="0s", needs=["B"], after={"step": "B", "delay": "0s"}),
                              self.step("B", check="true")])
        d["defaults"] = {"warn_before": "0s"}
        self.assertEqual(self.issues(d)[0], [])

    def test_schema_missing_driver_role(self):
        d = self.ledger_dict([self.step("A", check="true")])
        d["roles"] = {"implementer": {"model": "opus", "effort": "high"}}
        self.assertError(d, edl.E_SCHEMA, "roles.driver", "required")

    def test_schema_unquoted_epic_reads_as_octal(self):
        text = yaml.safe_dump(self.ledger_dict([self.step("A", check="true")]), sort_keys=False)
        text = text.replace("epic: t\n", "epic: 037\n")
        self.assertError(text, edl.E_SCHEMA, "epic", "octal")

    def test_schema_pinned_env_key_not_pinnable(self):
        d = self.ledger_dict([self.step("A", check="true")])
        d["roles"]["driver"]["pinned_env"] = {"ANTHROPIC_API_KEY": "x"}
        self.assertError(d, edl.E_SCHEMA, "roles.driver.pinned_env.ANTHROPIC_API_KEY", "not pinnable")

    def test_schema_watch_step_needs_a_check(self):
        self.assertError(self.ledger_dict([self.step("A")]), edl.E_SCHEMA, "steps.A.check", "watch step")

    def test_schema_worker_roles_must_differ(self):
        d = self.ledger_dict([self.step("A", role={"worker": {
            "implementer": "driver", "reviewer": "driver", "repos": [{"path": "/x", "base_ref": "main"}]}})])
        self.assertError(d, edl.E_SCHEMA, "steps.A.role.worker", "different roles")

    # -- graph ---------------------------------------------------------------
    def test_graph_unknown_needs(self):
        self.assertError(self.ledger_dict([self.step("A", check="true", needs=["Z"])]), edl.E_GRAPH,
                         "steps.A.needs", "unknown step 'Z'")

    def test_graph_unknown_after_step(self):
        self.assertError(self.ledger_dict([self.step("A", check="true", after={"step": "Z", "delay": "1h"})]),
                         edl.E_GRAPH, "steps.A.after.step", "unknown step")

    def test_graph_after_step_not_in_needs(self):
        d = self.ledger_dict([self.step("A", check="true"),
                              self.step("B", check="true", after={"step": "A", "delay": "1h"})])
        self.assertError(d, edl.E_GRAPH, "steps.B.after.step", "must also be in needs")

    def test_graph_unknown_approval_condition(self):
        d = self.ledger_dict([self.step("A", check="true")], approvals={"SA4": {"conditions": ["Z"]}})
        self.assertError(d, edl.E_GRAPH, "approvals.SA4.conditions", "unknown step 'Z'")

    def test_graph_cycle(self):
        d = self.ledger_dict([self.step("A", check="true", needs=["C"]), self.step("B", check="true", needs=["A"]),
                              self.step("C", check="true", needs=["B"])])
        err = self.assertError(d, edl.E_GRAPH, "needs", "cycle")
        self.assertIn("A", err.message)

    # -- files ---------------------------------------------------------------
    def test_files_missing_card(self):
        self.assertError(self.ledger_dict([self.step("A", check="true", card="cards/none.md")]), edl.E_FILES,
                         "steps.A.card", "missing")

    def test_files_missing_workspace(self):
        d = self.ledger_dict([self.step("A", check="true")], workspace=str(self.tmp / "gone"))
        path = self.plan / "l.yaml"
        path.write_text(yaml.safe_dump(d))
        _, errors, _ = edl.validate_ledger(str(path), self.world)
        self.assertIssue(errors, edl.E_FILES, "workspace", "missing")

    def test_files_missing_plan_root(self):
        self.assertError(self.ledger_dict([self.step("A", check="true")], plan_root="nope"), edl.E_FILES,
                         "plan_root", "missing")

    def test_files_missing_worker_epic_prompt(self):
        repo = str(self.tmp / "repo")
        os.mkdir(repo)
        world = FakeWorld(repos={repo: {"refs": {"main"}}})
        d = self.ledger_dict([self.step("A", role={"worker": {
            "implementer": "impl", "reviewer": "rev", "repos": [{"path": repo, "base_ref": "main"}]}})])
        d["roles"]["impl"] = {"model": "claude-opus-5-5", "effort": "high", "epic_prompt": "prompts/impl.md"}
        d["roles"]["rev"] = {"model": "claude-opus-5-5", "effort": "high"}
        self.assertError(d, edl.E_FILES, "roles.impl.epic_prompt", "missing", world=world)
        (self.ws / "prompts").mkdir()
        (self.ws / "prompts" / "impl.md").write_text("x")
        errors, _ = self.issues(d, world)
        self.assertEqual(errors, [])

    # -- names ---------------------------------------------------------------
    def test_names_sanitised_step_collision(self):
        d = self.ledger_dict([self.step("T.1", check="true"), self.step("t-1", check="true")])
        self.assertError(d, edl.E_NAMES, "steps.t-1.id", "collides")

    def test_names_duplicate_step_id(self):
        d = self.ledger_dict([self.step("A", check="true"), self.step("A", check="true")])
        self.assertError(d, edl.E_NAMES, "steps.A.id", "duplicate")

    def test_names_role_collision(self):
        d = self.ledger_dict([self.step("A", check="true")])
        d["roles"]["Impl_1"] = {"model": "opus", "effort": "high"}
        d["roles"]["impl-1"] = {"model": "opus", "effort": "high"}
        self.assertError(d, edl.E_NAMES, "roles.impl-1", "collides")

    def test_names_session_name_over_100(self):
        repo = str(self.tmp / "repo")
        os.mkdir(repo)
        world = FakeWorld(repos={repo: {"refs": {"main"}}})
        long_id = "s" + "x" * 60
        d = self.ledger_dict([self.step(long_id, role={"worker": {
            "implementer": "implementer", "reviewer": "reviewer", "repos": [{"path": repo, "base_ref": "main"}]}})])
        d["roles"]["implementer"] = {"model": "claude-opus-5-5", "effort": "high"}
        d["roles"]["reviewer"] = {"model": "claude-opus-5-5", "effort": "high"}
        self.assertError(d, edl.E_NAMES, f"steps.{long_id}", "over 100", world=world)

    def test_names_workspace_basename(self):
        ws = self.tmp / "my.ws"
        (ws / "plan" / "cards").mkdir(parents=True)
        (ws / "plan" / "cards" / "c.md").write_text("x")
        d = self.ledger_dict([self.step("A", check="true")], workspace=str(ws))
        path = ws / "plan" / "l.yaml"
        path.write_text(yaml.safe_dump(d))
        _, errors, _ = edl.validate_ledger(str(path), self.world)
        self.assertIssue(errors, edl.E_NAMES, "workspace", "outside [A-Za-z0-9_-]+")

    # -- approvals -----------------------------------------------------------
    def test_approvals_unknown_approval(self):
        self.assertError(self.ledger_dict([self.step("A", role="driver", check="true", approval="SA9")]),
                         edl.E_APPROVALS, "steps.A.approval", "unknown approval 'SA9'")

    def test_approval_map_order_is_kept(self):
        L = self.load(self.ledger_dict([self.step("A", check="true")],
                                       approvals={"SA9a": {"conditions": []}, "SA6": {"conditions": ["A"]}}))
        self.assertEqual([a.id for a in L.approvals_in_order()], ["SA9a", "SA6"])

    # -- timing --------------------------------------------------------------
    def test_timing_latest_start_and_duration(self):
        d = self.ledger_dict([self.step("A", role="driver", check="true", window="06:00-12:00Z",
                                        latest_start="09:30Z", duration="1h")])
        self.assertError(d, edl.E_TIMING, "steps.A", "both latest_start and duration")

    def test_timing_latest_start_outside_window(self):
        d = self.ledger_dict([self.step("A", role="driver", check="true", window="06:00-12:00Z",
                                        latest_start="13:00Z")])
        self.assertError(d, edl.E_TIMING, "steps.A.latest_start", "outside window")
        d = self.ledger_dict([self.step("A", role="driver", check="true", window="22:00-04:00Z",
                                        latest_start="03:00Z")])
        self.assertEqual(self.issues(d)[0], [])   # inside a window across midnight

    def test_timing_check_window_under_20_minutes(self):
        for cw in ({"daily": "14:00-14:19Z", "min": "5m"}, {"from": "0s", "to": "19m"},
                   {"from": "5m", "to": "10m"}):
            with self.subTest(cw=cw):
                self.assertError(self.ledger_dict([self.step("A", check="true", check_window=cw)]),
                                 edl.E_TIMING, "steps.A.check_window", "under 20m")
        self.assertEqual(self.issues(self.ledger_dict([self.step(
            "A", check="true", check_window={"from": "0s", "to": "20m"})]))[0], [])

    def test_timing_every_deadline_not_after_period(self):
        d = self.ledger_dict([self.step("A", role="driver", check="true",
                                        every={"period": "2d", "deadline": "2d"})])
        self.assertError(d, edl.E_TIMING, "steps.A.every.deadline", "longer than the period")

    def test_timing_at_needs_whole_days(self):
        d = self.ledger_dict([self.step("A", role="driver", check="true", every="12h", at="06:00Z")])
        self.assertError(d, edl.E_TIMING, "steps.A.at", "whole number of days")

    # -- resources -----------------------------------------------------------
    def test_resources_unknown(self):
        self.assertError(self.ledger_dict([self.step("A", check="true", resource={"gpu": 1})]),
                         edl.E_RESOURCES, "steps.A.resource.gpu", "unknown resource")

    def test_resources_above_cap(self):
        d = self.ledger_dict([self.step("A", check="true", resource={"mirror": 3})], resources={"mirror": 2})
        self.assertError(d, edl.E_RESOURCES, "steps.A.resource.mirror", "above the cap 2")

    # -- repos ---------------------------------------------------------------
    def _worker_ledger(self, repos):
        d = self.ledger_dict([self.step("W", role={"worker": {
            "implementer": "impl", "reviewer": "rev", "repos": repos}})])
        d["roles"]["impl"] = {"model": "claude-opus-5-5", "effort": "high"}
        d["roles"]["rev"] = {"model": "claude-opus-5-5", "effort": "high"}
        return d

    def test_repos_relative_path(self):
        self.assertError(self._worker_ledger([{"path": "repo", "base_ref": "main"}]), edl.E_REPOS,
                         "repos[0].path", "absolute")

    def test_repos_duplicate_path_by_realpath(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        (self.tmp / "link").symlink_to(repo)
        world = FakeWorld(repos={str(repo): {"refs": {"main"}}})
        d = self._worker_ledger([{"path": str(repo), "base_ref": "main", "name": "a"},
                                 {"path": str(self.tmp / "link"), "base_ref": "main", "name": "b"}])
        self.assertError(d, edl.E_REPOS, "repos[1].path", "duplicate", world=world)

    def test_repos_duplicate_name(self):
        a, b = self.tmp / "a" / "repo", self.tmp / "b" / "repo"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        world = FakeWorld(repos={str(a): {"refs": {"main"}}, str(b): {"refs": {"main"}}})
        d = self._worker_ledger([{"path": str(a), "base_ref": "main"}, {"path": str(b), "base_ref": "main"}])
        self.assertError(d, edl.E_REPOS, "repos[1].name", "duplicate name 'repo'", world=world)

    def test_repos_not_a_work_tree(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assertError(self._worker_ledger([{"path": str(plain), "base_ref": "main"}]), edl.E_REPOS,
                         "repos[0].path", "not a git work tree")

    def test_repos_base_ref_through_world_git(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        world = FakeWorld(repos={str(repo): {"refs": {"main"}}})
        self.assertError(self._worker_ledger([{"path": str(repo), "base_ref": "nope"}]), edl.E_REPOS,
                         "repos[0].base_ref", "does not resolve", world=world)
        self.assertIn((("rev-parse", "--verify", "--quiet", "--end-of-options", "nope^{commit}"), str(repo)), world.git_calls)
        # lint without the base_ref check still runs the work-tree test
        world2 = FakeWorld(repos={str(repo): {"refs": {"main"}}})
        _, errors, _ = edl.validate_ledger(self.write(self._worker_ledger([{"path": str(repo), "base_ref": "nope"}])),
                                           world2, verify_base_refs=False)
        self.assertEqual(errors, [])
        self.assertEqual([c[0][:2] for c in world2.git_calls], [("rev-parse", "--is-inside-work-tree")])
        L = self.load(self._worker_ledger([{"path": str(repo), "base_ref": "main"}]), world=world)
        self.assertEqual(L.step("W").worker.repos[0].name, "repo")
        self.assertEqual(L.step("W").target_role, "impl")

    def test_repos_with_real_git_in_a_temp_dir(self):
        if shutil.which("git") is None:
            self.skipTest("git not installed")
        repo = self.tmp / "realrepo"
        env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
        subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t",
                        "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty", "-m", "x"],
                       check=True, env=env)
        real = edl.RealWorld()
        self.assertEqual(self.issues(self._worker_ledger([{"path": str(repo), "base_ref": "HEAD"}]), real)[0], [])
        self.assertError(self._worker_ledger([{"path": str(repo), "base_ref": "no-such"}]), edl.E_REPOS,
                         "repos[0].base_ref", "does not resolve", world=real)

    # -- compaction (§11.6, OQ-2) ---------------------------------------------
    def _role_ledger(self, **role):
        d = self.ledger_dict([self.step("A", role="driver", check="true")])
        d["roles"]["driver"] = dict({"model": "fable", "effort": "high"}, **role)
        return d

    def test_compaction_idle_point_below_cap(self):
        world = FakeWorld(profiles={"claude-fable": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "65",
                                                     "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS": "200000"}})
        err = self.assertError(self._role_ledger(profile="claude-fable"), edl.E_COMPACTION, "roles.driver.cap",
                               "not below the compaction point", world=world)
        self.assertIn("idle 200000", err.message)
        L = self.load(self._role_ledger(profile="claude-fable",
                                        pinned_env={"CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS": "300000"}), world=world)
        r = L.roles["driver"]
        self.assertEqual((r.idle_compact_point, r.auto_compact_point), (300000, 650000))
        self.assertIsNone(r.expected_model_id)   # fable without a mapping: family match only

    def test_compaction_auto_point_from_the_model_window(self):
        # profile maps fable to a 200K-window model: 65 % of 200K = 130K < cap 250K
        world = FakeWorld(profiles={"p": {"ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-opus-4-6",
                                          "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "65"}})
        err = self.assertError(self._role_ledger(profile="p"), edl.E_COMPACTION, "roles.driver.cap",
                               "auto 130000", world=world)
        self.assertIn("200000", err.message)
        # pinned pct wins over the profile's
        L = self.load(self._role_ledger(profile="p", cap=100000,
                                        pinned_env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "60"}), world=world)
        self.assertEqual(L.roles["driver"].auto_compact_point, 120000)
        self.assertEqual(L.roles["driver"].expected_model_id, "claude-opus-4-6")

    def test_compaction_cap_equal_to_the_point_fails(self):
        for cap, ok in ((250000, False), (249999, True)):
            with self.subTest(cap=cap):
                d = self._role_ledger(cap=cap, pinned_env={"CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS": "250000"})
                errors, _ = self.issues(d)
                self.assertEqual(errors == [], ok, errors)
                if not ok:
                    self.assertIssue(errors, edl.E_COMPACTION, "roles.driver.cap", "not below")
        # the same boundary on the auto point: 50 % of the 1M default window of a mapped model
        d = self._role_ledger(cap=500000, pinned_env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"})
        window = self.load(self._role_ledger(pinned_env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "50"})).roles["driver"]
        self.assertEqual(window.auto_compact_point, window.context_window // 2)
        d["roles"]["driver"]["cap"] = window.auto_compact_point
        self.assertError(d, edl.E_COMPACTION, "roles.driver.cap", "not below")
        d["roles"]["driver"]["cap"] = window.auto_compact_point - 1
        self.assertEqual(self.issues(d)[0], [])

    def test_compaction_default_fallback_window_warns(self):
        d = self._role_ledger(model="mystery-model-9", pinned_env={"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "90"})
        L = self.load(d)
        self.assertEqual(L.roles["driver"].context_window_source, "default-fallback")
        errors, warnings = self.issues(d)
        self.assertEqual(errors, [])
        w = self.assertIssue(warnings, edl.W_CONTEXT_FALLBACK, "roles.driver", "mystery-model-9")
        self.assertIn("driver", w.message)
        # a known model does not warn
        self.assertEqual(self.issues(self._role_ledger(model="claude-opus-5-5"))[1], [])

    def test_compaction_unknown_profile(self):
        self.assertError(self._role_ledger(profile="nope"), edl.E_COMPACTION, "roles.driver.profile", "nope")

    def test_compaction_no_points_known_is_fine(self):
        self.assertEqual(self.issues(self._role_ledger())[0], [])

    # -- startup dialogs (§5.2, §5.5) ------------------------------------------
    def test_startup_dialogs_parsed(self):
        L = self.load(self.ledger_dict([self.step("A", check="true")], startup_dialogs=[
            {"id": "code-mode", "match": r"Code Mode \(experimental\)[\s\S]*❯ 1\. No", "key": "Enter"}]))
        (dlg,) = L.startup_dialogs
        self.assertEqual((dlg.id, dlg.key), ("code-mode", "Enter"))
        self.assertTrue(dlg.matches("Try Code Mode (experimental)?\n ❯ 1. No, start without it\n   2. Yes"))
        self.assertFalse(dlg.matches("Do you trust the files in this folder?"))

    def test_startup_dialogs_duplicate_id(self):
        d = self.ledger_dict([self.step("A", check="true")], startup_dialogs=[
            {"id": "x", "match": "a", "key": "Enter"}, {"id": "x", "match": "b", "key": "Escape"}])
        self.assertError(d, edl.E_STARTUP_DIALOGS, "startup_dialogs.x.id", "duplicate")

    def test_startup_dialogs_match_does_not_compile(self):
        d = self.ledger_dict([self.step("A", check="true")], startup_dialogs=[
            {"id": "x", "match": "(unclosed", "key": "Enter"}])
        self.assertError(d, edl.E_STARTUP_DIALOGS, "startup_dialogs.x.match", "does not compile")

    def test_startup_dialogs_bad_key(self):
        d = self.ledger_dict([self.step("A", check="true")], startup_dialogs=[
            {"id": "x", "match": "a", "key": "y"}])
        self.assertError(d, edl.E_STARTUP_DIALOGS, "startup_dialogs.x.key", "Enter, Escape")

    def test_startup_dialogs_schema(self):
        d = self.ledger_dict([self.step("A", check="true")], startup_dialogs=[
            {"id": "Bad_Id", "match": "a", "key": "Enter", "extra": 1}])
        errors, _ = self.issues(d)
        self.assertIssue(errors, edl.E_STARTUP_DIALOGS, "startup_dialogs.Bad_Id.id", "[a-z0-9-]+")
        self.assertIssue(errors, edl.E_SCHEMA, "extra", "unknown key")

    # -- warnings ------------------------------------------------------------
    def test_warning_driver_step_without_check(self):
        L = self.load(self.ledger_dict([self.step("A", role="driver")]))
        self.assertIssue(L.warnings, edl.W_DRIVER_STEP_NO_CHECK, "steps.A")

    def test_warning_ssh_without_connect_timeout(self):
        (self.plan / "checks").mkdir()
        (self.plan / "checks" / "probe.sh").write_text("#!/bin/sh\nssh h01 true\n")
        (self.plan / "checks" / "good.sh").write_text("#!/bin/sh\nssh $EPIC_DRIVER_SSH_OPTS h01 true\n")
        L = self.load(self.ledger_dict([
            self.step("A", check="ssh h01 uptime"),
            self.step("B", check="checks/probe.sh us-east01"),
            self.step("C", check="ssh -o ConnectTimeout=5 h01 true"),
            self.step("D", check="checks/good.sh"),
            self.step("E", check="true", trigger="ssh h02 test -f /x"),
        ]))
        where = sorted(w.where for w in L.warnings if w.category == edl.W_SSH_NO_TIMEOUT)
        self.assertEqual(where, ["steps.A.check", "steps.B.check", "steps.E.trigger"])

    # -- the 037 fragment of §5.6 ------------------------------------------------
    def _fragment(self):
        text = ARCHITECTURE.read_text()
        m = re.search(r"### 5\.6 .*?```yaml\n(.*?)```", text, re.S)
        self.assertIsNotNone(m, "architecture §5.6 fragment not found")
        return m.group(1)

    def test_the_037_fragment_validates(self):
        ws = self.tmp / "acdn-workspace"
        plan = ws / "tasks" / "037_image_optimization"
        frag = self._fragment().replace("/data/sync/work/activecdn/acdn-workspace", str(ws))
        # The fragment names e1-build (in `needs` and SA4's conditions) without
        # defining it: the full ledger does. Supply that one step.
        frag = frag.replace("steps:\n", "steps:\n  - id: e1-build\n    title: build\n    phase: E1\n"
                                        "    card: ledger/cards/e1-build.md\n    check: \"true\"\n", 1)
        for card in set(re.findall(r"card: (\S+)", frag)):
            (plan / card).parent.mkdir(parents=True, exist_ok=True)
            (plan / card).write_text("# card\n")
        path = plan / "ledger.yaml"
        path.write_text(frag)
        world = FakeWorld(profiles={"claude-fable": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "65",
                                                     "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS": "200000"}})
        ledger, errors, warnings = edl.validate_ledger(str(path), world)
        self.assertEqual(errors, [])
        # `fable` is not mapped by profile claude-fable here, so its window is the guessed default (§11.6)
        self.assertEqual([(w.category, w.where) for w in warnings],
                         [(edl.W_CONTEXT_FALLBACK, "roles.driver")])
        self.assertIn("'fable'", warnings[0].message)
        self.assertEqual((ledger.epic, ledger.run_id), ("037", "epic-037"))
        self.assertEqual([s.id for s in ledger.steps][:3], ["e1-build", "e1-useast01-roll", "e1-useast01-soak"])
        roll = ledger.step("e1-useast01-roll")
        self.assertEqual((roll.approval, roll.window.text, roll.latest_start_s, roll.timeout_s),
                         ("SA4", "06:00-12:00Z", 9 * 3600 + 1800, 3 * 3600))
        soak = ledger.step("e1-useast01-soak")
        self.assertTrue(soak.is_watch)
        self.assertEqual((soak.after.step, soak.after.delay_s), ("e1-useast01-roll", 7200))
        c15 = ledger.step("c15-us-daytime")
        self.assertEqual((c15.check_window.daily.text, c15.check_window.min_s, c15.timeout_s),
                         ("14:00-22:00Z", 3600, 2 * 86400))
        mon = ledger.step("monterey-export").every
        self.assertEqual((mon.series, mon.period_s, mon.deadline_s, mon.warn_before_s),
                         (True, 2 * 86400, 7 * 86400, 2 * 86400))
        self.assertEqual(mon.from_, T("2026-11-22T09:28:20Z"))
        self.assertEqual(ledger.roles["driver"].idle_compact_point, 300000)
        self.assertEqual(world.git_calls, [])   # no worker steps in the fragment
        # Without the OQ-2 pin, the profile's idle 200000 is below the 250K cap.
        path.write_text(frag.replace(', CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS: "300000"', ""))
        _, errors, _ = edl.validate_ledger(str(path), world)
        self.assertIssue(errors, edl.E_COMPACTION, "roles.driver.cap")


# =============================================================================
# 50-04 — schedule: due selection, holds, approvals (§6, §7.3, §15.1)
# =============================================================================

class _Sched(_Base):
    ENABLED = "2026-11-24T00:00:00Z"

    def sched(self, steps, **top):
        L = self.load(self.ledger_dict(steps, **top))
        return L, edl.new_state(L, T(self.ENABLED))

    def due(self, L, st, at):
        return [d.step_id for d in edl.due_steps(L, st, T(at))]

    def driver(self, sid, **kw):
        kw.setdefault("check", "true")
        return self.step(sid, role="driver", **kw)


class TestDueSelection(_Sched):
    def test_needs(self):
        L, st = self.sched([self.driver("A"), self.driver("B", needs=["A"])])
        self.assertEqual(self.due(L, st, "2026-11-24T01:00:00Z"), ["A"])
        edl.mark_done(L, st, "A", T("2026-11-24T02:00:00Z"))
        self.assertEqual(self.due(L, st, "2026-11-24T02:00:00Z"), ["B"])
        self.assertEqual(edl.step_due_at(L, st, "B"), T("2026-11-24T02:00:00Z"))

    def test_after_anchor_is_advanced_at_for_a_step_with_a_role(self):
        L, st = self.sched([self.driver("A"), self.step("B", check="true", needs=["A"],
                                                         after={"step": "A", "delay": "2h"})])
        st["steps"]["A"].update(status="done", advanced_at="2026-11-24T07:12:00Z", done_at="2026-11-24T07:20:00Z")
        self.assertEqual(edl.step_due_at(L, st, "B"), T("2026-11-24T09:12:00Z"))
        self.assertEqual(self.due(L, st, "2026-11-24T09:11:59Z"), [])
        self.assertEqual(self.due(L, st, "2026-11-24T09:12:00Z"), ["B"])

    def test_after_anchor_is_done_at_for_a_watch_step(self):
        L, st = self.sched([self.step("A", check="true"),
                            self.driver("B", needs=["A"], after={"step": "A", "delay": "1h"})])
        st["steps"]["A"].update(status="done", done_at="2026-11-24T10:00:00Z")
        self.assertEqual(edl.step_due_at(L, st, "B"), T("2026-11-24T11:00:00Z"))
        self.assertEqual(self.due(L, st, "2026-11-24T10:59:00Z"), [])
        self.assertEqual(self.due(L, st, "2026-11-24T11:00:00Z"), ["B"])

    def test_not_before(self):
        L, st = self.sched([self.driver("A", not_before="2026-11-24T08:00:00+02:00")])
        self.assertEqual(self.due(L, st, "2026-11-24T05:59:59Z"), [])
        self.assertEqual(self.due(L, st, "2026-11-24T06:00:00Z"), ["A"])

    def test_window_and_latest_start(self):
        L, st = self.sched([self.driver("A", window="06:00-12:00Z", latest_start="09:30Z")])
        sel = edl.select(L, st, T("2026-11-24T05:00:00Z"))
        self.assertEqual(sel.dispatchable, [])
        self.assertEqual([h.reason for h in sel.held["A"]], [edl.HOLD_WINDOW])
        self.assertIn("2026-11-24T06:00:00Z", sel.held["A"][0].detail)
        self.assertEqual(edl.select(L, st, T("2026-11-24T07:00:00Z")).dispatchable, ["A"])
        self.assertEqual(edl.select(L, st, T("2026-11-24T09:30:00Z")).dispatchable, ["A"])
        sel = edl.select(L, st, T("2026-11-24T09:31:00Z"))
        self.assertEqual([h.reason for h in sel.held["A"]], [edl.HOLD_LATEST_START])

    def test_duration_in_a_window_across_midnight(self):
        L, st = self.sched([self.driver("A", window="22:00-04:00Z", duration="1h")])
        for at, ok, reason in (("2026-11-24T23:00:00Z", True, None), ("2026-11-25T03:00:00Z", True, None),
                               ("2026-11-25T03:01:00Z", False, edl.HOLD_LATEST_START),
                               ("2026-11-25T05:00:00Z", False, edl.HOLD_WINDOW),
                               ("2026-11-25T21:59:00Z", False, edl.HOLD_WINDOW)):
            with self.subTest(at=at):
                sel = edl.select(L, st, T(at))
                self.assertEqual(sel.dispatchable == ["A"], ok)
                if reason:
                    self.assertEqual([h.reason for h in sel.held["A"]], [reason])

    def test_every_plain(self):
        L, st = self.sched([self.driver("R", every="1d")])
        self.assertEqual(edl.step_due_at(L, st, "R"), T("2026-11-25T00:00:00Z"))   # enable + period
        self.assertEqual(self.due(L, st, "2026-11-24T23:59:59Z"), [])
        self.assertEqual(self.due(L, st, "2026-11-25T00:00:00Z"), ["R"])
        edl.mark_done(L, st, "R", T("2026-11-25T00:05:00Z"))
        moves = edl.advance_schedule(L, st, T("2026-11-25T00:05:00Z"))
        self.assertIn(("R", "done", "pending"), moves)
        rec = st["steps"]["R"]
        self.assertEqual((rec["occurrence"], rec["last_done_at"], rec["status"]), (1, "2026-11-25T00:05:00Z", "pending"))
        self.assertEqual(edl.step_due_at(L, st, "R"), T("2026-11-26T00:05:00Z"))

    def test_every_plain_first_occurrence_at_not_before(self):
        L, st = self.sched([self.driver("R", every="7d", not_before="2026-11-24T12:00:00Z")])
        self.assertEqual(edl.step_due_at(L, st, "R"), T("2026-11-24T12:00:00Z"))

    def test_at_alone_and_with_every(self):
        self.ENABLED = "2026-11-24T10:00:00Z"
        L, st = self.sched([self.driver("D", at="06:00Z"), self.driver("E", every="2d", at="06:00Z")])
        self.assertEqual(edl.step_due_at(L, st, "D"), T("2026-11-25T06:00:00Z"))
        self.assertEqual(edl.step_due_at(L, st, "E"), T("2026-11-25T06:00:00Z"))
        for sid in ("D", "E"):
            edl.mark_done(L, st, sid, T("2026-11-25T06:10:00Z"))
        edl.advance_schedule(L, st, T("2026-11-25T06:10:00Z"))
        self.assertEqual(edl.step_due_at(L, st, "D"), T("2026-11-26T06:00:00Z"))
        self.assertEqual(edl.step_due_at(L, st, "E"), T("2026-11-27T06:00:00Z"))
        # a late pull (done after the next slot's day began) still lands on the grid
        edl.mark_done(L, st, "D", T("2026-11-27T01:00:00Z"))
        edl.advance_schedule(L, st, T("2026-11-27T01:00:00Z"))
        self.assertEqual(edl.step_due_at(L, st, "D"), T("2026-11-27T06:00:00Z"))

    def test_every_series_the_monterey_numbers(self):
        self.ENABLED = "2026-11-23T08:00:00Z"
        L, st = self.sched([self.driver("monterey-export", every={
            "period": "2d", "from": "2026-11-22T09:28:20Z", "deadline": "7d", "warn_before": "2d"})])
        self.assertEqual(edl.step_due_at(L, st, "monterey-export"), T("2026-11-24T09:28:20Z"))
        self.assertEqual(self.due(L, st, "2026-11-24T09:28:19Z"), [])
        self.assertEqual(self.due(L, st, "2026-11-24T09:28:20Z"), ["monterey-export"])

        def conds(at):
            return [(c.id, c.subject) for c in edl.deadline_conditions(L, st, T(at))]
        self.assertEqual(conds("2026-11-27T09:28:19Z"), [])
        self.assertEqual(conds("2026-11-27T09:28:20Z"), [("deadline_near", "monterey-export")])
        self.assertEqual(conds("2026-11-29T09:28:19Z"), [("deadline_near", "monterey-export")])
        self.assertEqual(conds("2026-11-29T09:28:20Z"), [("deadline_missed", "monterey-export")])
        # the next occurrence is measured from this one's done_at
        edl.mark_done(L, st, "monterey-export", T("2026-11-24T09:30:00Z"))
        self.assertEqual(conds("2026-11-29T09:28:20Z"), [])    # done: no deadline alarm
        edl.advance_schedule(L, st, T("2026-11-24T09:30:00Z"))
        self.assertEqual(edl.step_due_at(L, st, "monterey-export"), T("2026-11-26T09:30:00Z"))
        self.assertEqual(conds("2026-12-01T09:30:00Z"), [("deadline_missed", "monterey-export")])

    def test_not_after_deadline(self):
        L, st = self.sched([self.driver("A", not_after="2026-11-25T12:00:00Z", warn_before="6h")])
        self.assertEqual(edl.deadline_conditions(L, st, T("2026-11-25T05:59:00Z")), [])
        self.assertEqual([c.id for c in edl.deadline_conditions(L, st, T("2026-11-25T06:00:00Z"))],
                         ["deadline_near"])
        self.assertEqual([c.id for c in edl.deadline_conditions(L, st, T("2026-11-25T12:00:00Z"))],
                         ["deadline_missed"])

    def test_trigger(self):
        L, st = self.sched([self.driver("X", trigger="probe")])
        self.assertEqual(self.due(L, st, "2026-11-24T01:00:00Z"), [])
        jobs = edl.eligible_checks(L, st, T("2026-11-24T01:00:00Z"))
        self.assertEqual([(j.step_id, j.kind, j.command) for j in jobs], [("X", "trigger", "probe")])
        job = jobs[0]
        out = edl.apply_check_result(L, st, job, edl.CheckRun(1, T("2026-11-24T01:00:00Z"), 1.0),
                                     T("2026-11-24T01:00:01Z"))
        self.assertEqual(out.outcome, edl.OUTCOME_NOT_YET)
        self.assertEqual(self.due(L, st, "2026-11-24T01:00:01Z"), [])
        out = edl.apply_check_result(L, st, job, edl.CheckRun(0, T("2026-11-24T01:10:00Z"), 1.0),
                                     T("2026-11-24T01:10:01Z"))
        self.assertEqual(out.outcome, edl.OUTCOME_FIRED)
        self.assertEqual(self.due(L, st, "2026-11-24T01:10:01Z"), ["X"])
        self.assertEqual(edl.step_due_at(L, st, "X"), T("2026-11-24T01:10:01Z"))
        # a fired trigger does not run again for the same occurrence
        self.assertEqual(edl.eligible_checks(L, st, T("2026-11-24T02:00:00Z")), [])

    def test_trigger_errors_count_as_check_errors(self):
        L, st = self.sched([self.driver("X", trigger="probe")])
        job = edl.CheckJob("X", edl.KIND_TRIGGER, "probe", 60)
        for i in range(3):
            out = edl.apply_check_result(L, st, job, edl.CheckRun(2 if i else None, T("2026-11-24T01:00:00Z"), 1.0,
                                                                   error=None if i else "timed out"),
                                         T("2026-11-24T01:00:00Z"))
            self.assertEqual(out.outcome, edl.OUTCOME_ERROR)
        self.assertIn(("check_error", "X"), [(c.id, c.subject) for c in
                                             edl.step_alarm_conditions(L, st, T("2026-11-24T01:00:00Z"))])

    def test_resource_caps(self):
        L, st = self.sched([self.driver("A", resource={"mirror": 1}), self.driver("B", resource={"mirror": 1}),
                            self.driver("C", resource={"mirror": 2})], resources={"mirror": 2})
        sel = edl.select(L, st, T("2026-11-24T01:00:00Z"))
        self.assertEqual(sel.dispatchable, ["A", "B"])
        self.assertEqual([h.reason for h in sel.held["C"]], [edl.HOLD_RESOURCES])
        st["steps"]["A"]["status"] = "inflight"
        sel = edl.select(L, st, T("2026-11-24T01:00:00Z"))
        self.assertEqual(sel.dispatchable, ["B"])
        self.assertEqual([h.reason for h in sel.held["C"]], [edl.HOLD_RESOURCES])

    def test_ungranted_approval_holds_and_is_asked(self):
        L, st = self.sched([self.step("build", check="true"), self.driver("roll", needs=["build"], approval="SA4")],
                           approvals={"SA4": {"conditions": ["build"]}})
        st["steps"]["build"].update(status="done", done_at="2026-11-24T00:30:00Z")
        sel = edl.select(L, st, T("2026-11-24T01:00:00Z"))
        self.assertEqual(sel.dispatchable, [])
        self.assertEqual([h.reason for h in sel.held["roll"]], [edl.HOLD_APPROVAL])
        self.assertEqual((sel.approval_to_ask.approval_id, sel.approval_to_ask.steps,
                          sel.approval_to_ask.conditions), ("SA4", ["roll"], ["build"]))
        self.assertFalse(edl.due_not_started_quiet(sel.held["roll"]))
        g = edl.grant(L, st, "SA4", "Q-17", None, T("2026-11-24T01:05:00Z"))
        self.assertEqual(g, {"granted_at": "2026-11-24T01:05:00Z", "question": "Q-17", "note": None, "by": "mark"})
        sel = edl.select(L, st, T("2026-11-24T01:10:00Z"))
        self.assertEqual(sel.dispatchable, ["roll"])
        self.assertIsNone(sel.approval_to_ask)

    def test_approval_with_conditions_not_done_is_not_asked(self):
        L, st = self.sched([self.step("build", check="true"), self.driver("roll", approval="SA4")],
                           approvals={"SA4": {"conditions": ["build"]}})
        sel = edl.select(L, st, T("2026-11-24T01:00:00Z"))
        self.assertEqual([h.reason for h in sel.held["roll"]], [edl.HOLD_APPROVAL])
        self.assertIsNone(sel.approval_to_ask)
        edl.grant(L, st, "SA4", None, None, T("2026-11-24T01:00:00Z"))
        sel = edl.select(L, st, T("2026-11-24T01:00:00Z"))
        self.assertEqual([h.reason for h in sel.held["roll"]], [edl.HOLD_APPROVAL_CONDITIONS])

    def test_an_approval_gating_a_held_step_is_not_asked(self):
        L, st = self.sched([self.driver("roll", approval="SA4", window="06:00-12:00Z")],
                           approvals={"SA4": {"conditions": []}})
        sel = edl.select(L, st, T("2026-11-24T01:00:00Z"))   # window closed
        self.assertIsNone(sel.approval_to_ask)
        self.assertEqual(edl.select(L, st, T("2026-11-24T07:00:00Z")).approval_to_ask.approval_id, "SA4")

    def test_one_approval_at_a_time_in_map_order(self):
        L, st = self.sched([self.driver("x", approval="SA6"), self.driver("y", approval="SA9a")],
                           approvals={"SA9a": {"conditions": []}, "SA6": {"conditions": []}})
        sel = edl.select(L, st, T("2026-11-24T01:00:00Z"))
        self.assertEqual(sel.approval_to_ask.approval_id, "SA9a")    # map order, not ledger order
        st["approval_question"] = {"id": "SA9a", "question": "Q-3"}
        sel = edl.select(L, st, T("2026-11-24T01:10:00Z"))
        self.assertIsNone(sel.approval_to_ask)
        self.assertEqual({k: [h.reason for h in v] for k, v in sel.held.items()},
                         {"x": [edl.HOLD_APPROVAL_ASKED], "y": [edl.HOLD_APPROVAL_ASKED]})
        self.assertTrue(edl.due_not_started_quiet(sel.held["x"]))
        edl.grant(L, st, "SA9a", "Q-3", None, T("2026-11-24T01:20:00Z"))
        st["approval_question"] = None
        sel = edl.select(L, st, T("2026-11-24T01:30:00Z"))
        self.assertEqual(sel.dispatchable, ["y"])
        self.assertEqual(sel.approval_to_ask.approval_id, "SA6")

    def test_grant_is_idempotent_and_checks_the_id(self):
        L, st = self.sched([self.driver("x", approval="SA6")], approvals={"SA6": {"conditions": []}})
        first = edl.grant(L, st, "SA6", "Q-1", "ok", T("2026-11-24T01:00:00Z"))
        again = edl.grant(L, st, "SA6", "Q-9", None, T("2026-11-24T02:00:00Z"))
        self.assertIs(first, again)
        self.assertEqual(st["grants"]["SA6"]["question"], "Q-1")
        with self.assertRaises(KeyError) as ctx:
            edl.grant(L, st, "SA7", None, None, T("2026-11-24T01:00:00Z"))
        self.assertIn("SA7", str(ctx.exception))
        self.assertIn("SA6", str(ctx.exception))        # names what the ledger does define
        self.assertNotIn("SA7", st["grants"])
        with self.assertRaises(TypeError):               # the ledger is required
            edl.grant(st, "SA6", None, None, T("2026-11-24T01:00:00Z"))

    def test_grant_before_the_conditions_are_done_is_allowed(self):
        L, st = self.sched([self.step("build", check="true"), self.driver("roll", needs=["build"], approval="SA4")],
                           approvals={"SA4": {"conditions": ["build"]}})
        self.assertFalse(edl.approval_conditions_done(L, st, "SA4"))
        edl.grant(L, st, "SA4", None, "pre-granted", T("2026-11-24T01:00:00Z"))   # no error
        self.assertTrue(edl.approval_granted(st, "SA4"))

    def test_group(self):
        L, st = self.sched([self.driver("P", group="g"), self.driver("Q", group="g", window="06:00-12:00Z"),
                            self.driver("R")])
        sel = edl.select(L, st, T("2026-11-24T01:00:00Z"))
        self.assertEqual(sel.dispatchable, ["R"])
        self.assertEqual([h.reason for h in sel.held["P"]], [edl.HOLD_GROUP])
        self.assertIn("Q", sel.held["P"][0].detail)
        self.assertEqual([h.reason for h in sel.held["Q"]], [edl.HOLD_WINDOW])
        sel = edl.select(L, st, T("2026-11-24T07:00:00Z"))
        self.assertEqual(sel.dispatchable, ["P", "Q", "R"])
        self.assertEqual(sel.units, [["P", "Q"], ["R"]])

    def test_group_member_not_due_holds_the_group(self):
        L, st = self.sched([self.driver("P", group="g"), self.driver("Q", group="g", needs=["R"]),
                            self.driver("R")])
        sel = edl.select(L, st, T("2026-11-24T01:00:00Z"))
        self.assertEqual(sel.dispatchable, ["R"])
        self.assertEqual([h.reason for h in sel.held["P"]], [edl.HOLD_GROUP])

    def test_paused_model_limit_and_attempts(self):
        L, st = self.sched([self.driver("A")])
        sel = edl.select(L, st, T("2026-11-24T01:00:00Z"), paused=True)
        self.assertEqual([h.reason for h in sel.held["A"]], [edl.HOLD_PAUSED])
        st["limits"] = {"claude-opus-5-5": {"until": "2026-11-24T03:00:00Z", "source": "reset", "ladder_step": 0}}
        self.assertEqual([h.reason for h in edl.select(L, st, T("2026-11-24T01:00:00Z")).held["A"]],
                         [edl.HOLD_MODEL_LIMIT])
        self.assertEqual(edl.select(L, st, T("2026-11-24T03:00:00Z")).dispatchable, ["A"])
        st["steps"]["A"]["attempts"] = 3
        self.assertEqual([h.reason for h in edl.select(L, st, T("2026-11-24T04:00:00Z")).held["A"]],
                         [edl.HOLD_ATTEMPTS])

    def test_model_limit_by_family_for_an_unmapped_alias(self):
        d = self.ledger_dict([self.driver("A")])
        d["roles"]["driver"] = {"model": "opus", "effort": "high"}
        L = self.load(d)
        st = edl.new_state(L, T(self.ENABLED))
        st["limits"] = {"claude-opus-5-5": {"until": "2026-11-24T03:00:00Z"}}
        self.assertEqual(edl.model_limited(L, st, "driver", T("2026-11-24T01:00:00Z")), "claude-opus-5-5")
        st["limits"] = {"claude-sonnet-5-0": {"until": "2026-11-24T03:00:00Z"}}
        self.assertIsNone(edl.model_limited(L, st, "driver", T("2026-11-24T01:00:00Z")))

    def test_due_not_started_after_two_ticks_unless_quiet(self):
        L, st = self.sched([self.driver("A"), self.driver("B", window="06:00-12:00Z")])
        now = T("2026-11-24T01:00:00Z")
        for _ in range(2):
            sel = edl.select(L, st, now)
            edl.note_undispatched(L, st, sel, dispatched=[])
        conds = [(c.id, c.subject) for c in edl.step_alarm_conditions(L, st, now, sel)]
        self.assertIn(("due_not_started", "A"), conds)
        self.assertNotIn(("due_not_started", "B"), conds)   # a closed window is seen by the operator
        sel = edl.select(L, st, now, paused=True)
        self.assertNotIn("due_not_started", [c.id for c in edl.step_alarm_conditions(L, st, now, sel)])
        edl.note_undispatched(L, st, edl.select(L, st, now), dispatched=["A"])
        self.assertEqual(st["steps"]["A"]["undispatched_ticks"], 0)

    def test_dispatchable_wrapper(self):
        L, st = self.sched([self.driver("A"), self.driver("B", window="06:00-12:00Z")])
        ids, held = edl.dispatchable(L, st, T("2026-11-24T01:00:00Z"))
        self.assertEqual(ids, ["A"])
        self.assertEqual(list(held), ["B"])

    def test_advance_schedule_moves(self):
        L, st = self.sched([self.driver("A"), self.step("W", check="true"), self.driver("B", needs=["A"])])
        moves = edl.advance_schedule(L, st, T("2026-11-24T01:00:00Z"))
        self.assertEqual(moves, [("A", "pending", "due"), ("W", "pending", "watching")])
        self.assertEqual(st["steps"]["W"]["due_since"], "2026-11-24T01:00:00Z")
        self.assertEqual(edl.advance_schedule(L, st, T("2026-11-24T01:10:00Z")), [])
        # watch steps are due but never dispatchable: they have no action
        self.assertEqual(edl.select(L, st, T("2026-11-24T01:10:00Z")).dispatchable, ["A"])
        # a reset predecessor sends a due step back to pending
        st["steps"]["A"].update(status="done", done_at="2026-11-24T01:20:00Z")
        edl.advance_schedule(L, st, T("2026-11-24T01:20:00Z"))
        self.assertEqual(st["steps"]["B"]["status"], "due")
        edl.mark_reset(L, st, "A", T("2026-11-24T01:30:00Z"))
        self.assertIn(("B", "due", "pending"), edl.advance_schedule(L, st, T("2026-11-24T01:30:00Z")))

    def test_state_machine_as_data(self):
        self.assertEqual(set(edl.STEP_TRANSITIONS), set(edl.STEP_STATUSES))
        rec = edl.new_step_state()
        edl.set_status(rec, "due")
        edl.set_status(rec, "inflight")
        edl.set_status(rec, "verifying")
        with self.assertRaises(ValueError):
            edl.set_status(rec, "inflight")
        edl.set_status(rec, "failed")
        edl.set_status(rec, "due")     # a retry
        edl.set_status(rec, "inflight")
        edl.set_status(rec, "halted")
        with self.assertRaises(ValueError):
            edl.set_status(rec, "due")
        edl.set_status(rec, "pending", by_mark=True)
        with self.assertRaises(ValueError):
            edl.set_status(rec, "bogus")


class TestCheckWindow(_Sched):
    def test_c15_daily_coverage(self):
        L, st = self.sched([self.step("c15", check="true", check_window={"daily": "14:00-22:00Z", "min": "1h"})])
        edl.advance_schedule(L, st, T("2026-11-24T10:00:00Z"))
        self.assertEqual(st["steps"]["c15"]["status"], "watching")
        world = FakeWorld(now=T("2026-11-24T14:30:00Z"))
        world.check_script["c15"] = (0, 2)
        self.assertEqual(edl.run_checks(L, st, world, snapshot={}).ran, [])
        self.assertEqual(st["steps"]["c15"]["window_covered_s"], 1800)
        self.assertEqual(st["steps"]["c15"]["status"], "watching")
        world.clock = T("2026-11-24T15:00:00Z")
        p = edl.run_checks(L, st, world, snapshot={})
        self.assertEqual([j.step_id for j, _, _ in p.ran], ["c15"])
        self.assertEqual(st["steps"]["c15"]["window_covered_s"], 3600)
        self.assertEqual(st["steps"]["c15"]["status"], "done")

    def test_daily_coverage_accumulates_across_days(self):
        L, st = self.sched([self.step("c15", check="true", check_window={"daily": "14:00-22:00Z", "min": "1h"})])
        edl.advance_schedule(L, st, T("2026-11-24T21:30:00Z"))
        self.assertEqual(edl.eligible_checks(L, st, T("2026-11-25T14:29:00Z")), [])
        self.assertEqual(edl.window_coverage(L, st, T("2026-11-25T14:29:00Z")), {"c15": 59 * 60})
        self.assertEqual(len(edl.eligible_checks(L, st, T("2026-11-25T14:30:00Z"))), 1)

    def test_daily_window_across_midnight_with_an_anchor_after_midnight(self):
        L, st = self.sched([self.step("n", check="true", check_window={"daily": "22:00-04:00Z", "min": "2h"})])
        edl.advance_schedule(L, st, T("2026-11-25T01:00:00Z"))      # anchor: 01:00, inside the window
        self.assertEqual(st["steps"]["n"]["status"], "watching")
        self.assertEqual(edl.eligible_checks(L, st, T("2026-11-25T02:59:00Z")), [])
        self.assertEqual(edl.window_coverage(L, st, T("2026-11-25T02:59:00Z")), {"n": 2 * 3600 - 60})
        self.assertEqual(len(edl.eligible_checks(L, st, T("2026-11-25T03:00:00Z"))), 1)

    def test_relative_window_and_its_timeout(self):
        L, st = self.sched([self.step("w", check="true", check_window={"from": "30m", "to": "2h"})])
        edl.advance_schedule(L, st, T("2026-11-24T10:00:00Z"))
        self.assertEqual(edl.eligible_checks(L, st, T("2026-11-24T10:29:00Z")), [])
        self.assertEqual(len(edl.eligible_checks(L, st, T("2026-11-24T10:30:00Z"))), 1)
        self.assertEqual(len(edl.eligible_checks(L, st, T("2026-11-24T12:00:00Z"))), 1)
        self.assertEqual(edl.eligible_checks(L, st, T("2026-11-24T12:01:00Z")), [])
        conds = edl.step_alarm_conditions(L, st, T("2026-11-24T12:01:00Z"))
        self.assertEqual([(c.id, c.subject) for c in conds], [("step_timeout", "w")])
        self.assertIn("check_window.to", conds[0].detail)

    def test_action_step_window_anchors_on_advanced_at(self):
        L, st = self.sched([self.driver("a", check_window={"from": "1h", "to": "3h"})])
        st["steps"]["a"].update(status="verifying", due_since="2026-11-24T01:00:00Z",
                                advanced_at="2026-11-24T05:00:00Z")
        self.assertEqual(edl.eligible_checks(L, st, T("2026-11-24T05:59:00Z")), [])
        self.assertEqual(len(edl.eligible_checks(L, st, T("2026-11-24T06:00:00Z"))), 1)


class TestStepAlarmConditions(_Sched):
    def test_step_timeout_attempts_halt(self):
        L, st = self.sched([self.driver("A", timeout="3h"), self.step("W", check="true", timeout="1h"),
                            self.driver("H"), self.driver("X", max_attempts=2)])
        st["steps"]["A"].update(status="inflight", started_at="2026-11-24T01:00:00Z")
        st["steps"]["W"].update(status="watching", due_since="2026-11-24T01:00:00Z")
        st["steps"]["H"].update(status="halted", last_verdict={"line": "HALT: disk full"})
        st["steps"]["X"].update(status="failed", attempts=2)
        conds = {(c.id, c.subject): c.detail for c in edl.step_alarm_conditions(L, st, T("2026-11-24T04:00:00Z"))}
        self.assertIn(("step_timeout", "A"), conds)
        self.assertIn(("step_timeout", "W"), conds)
        self.assertEqual(conds[("halt", "H")], "HALT: disk full")
        self.assertIn(("attempts_exhausted", "X"), conds)
        conds = {(c.id, c.subject) for c in edl.step_alarm_conditions(L, st, T("2026-11-24T01:30:00Z"))}
        self.assertNotIn(("step_timeout", "A"), conds)
        self.assertNotIn(("step_timeout", "W"), conds)
        self.assertEqual(edl.AlarmCondition("halt", "H").key, "halt:H")


# =============================================================================
# 50-04 — checks and triggers (§7)
# =============================================================================

def _wait_gone(pid, timeout=5.0):
    """True once ``pid`` no longer exists (or is a zombie awaiting its reaper)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with open(f"/proc/{pid}/stat") as f:
                if f.read().split(")")[-1].split()[0] == "Z":
                    return True
        except OSError:
            return True
        time.sleep(0.05)
    return False


class TestCheckRunner(_Sched):
    def env(self, sid="A"):
        return edl.check_env("epic-t", sid, str(self.tmp / "state" / "epic-t"),
                             {"PATH": "/usr/bin:/bin"}, base_env={"HOME": str(self.tmp)})

    def test_exit_codes(self):
        L, st = self.sched([self.step("A", check="x")])
        real = edl.RealWorld()
        expect = {0: (edl.OUTCOME_DONE, "done", 0, 0), 1: (edl.OUTCOME_NOT_YET, "watching", 0, 0),
                  2: (edl.OUTCOME_FAILED, "failed", 1, 0), 3: (edl.OUTCOME_ERROR, "watching", 0, 1)}
        for rc, (outcome, status, attempts, errors) in expect.items():
            with self.subTest(rc=rc):
                st["steps"]["A"] = edl.new_step_state()
                st["steps"]["A"].update(status="watching", due_since="2026-11-24T00:00:00Z")
                run = real.run_check(f"echo hello {rc}; exit {rc}", str(self.plan), self.env(), 10)
                self.assertEqual((run.rc, run.timed_out), (rc, False))
                self.assertEqual(run.output_tail, f"hello {rc}\n")
                out = edl.apply_check_result(L, st, edl.CheckJob("A", "check", "x", 10), run, real.now())
                rec = st["steps"]["A"]
                self.assertEqual((out.outcome, rec["status"], rec["attempts"], rec["check"]["consecutive_errors"]),
                                 (outcome, status, attempts, errors))
                self.assertEqual(rec["check"]["last_rc"], rc)
                self.assertEqual(rec["check"]["output_tail"], f"hello {rc}\n")

    def test_check_error_at_three_consecutive_and_reset(self):
        L, st = self.sched([self.step("A", check="x")])
        st["steps"]["A"].update(status="watching")
        job = edl.CheckJob("A", "check", "x", 10)
        for _ in range(2):
            edl.apply_check_result(L, st, job, edl.CheckRun(None, T(self.ENABLED), 1, error="cannot start"),
                                   T(self.ENABLED))
        self.assertNotIn("check_error", [c.id for c in edl.step_alarm_conditions(L, st, T(self.ENABLED))])
        edl.apply_check_result(L, st, job, edl.CheckRun(7, T(self.ENABLED), 1), T(self.ENABLED))
        self.assertIn("check_error", [c.id for c in edl.step_alarm_conditions(L, st, T(self.ENABLED))])
        edl.apply_check_result(L, st, job, edl.CheckRun(1, T(self.ENABLED), 1), T(self.ENABLED))
        self.assertEqual(st["steps"]["A"]["check"]["consecutive_errors"], 0)
        self.assertNotIn("check_error", [c.id for c in edl.step_alarm_conditions(L, st, T(self.ENABLED))])

    def test_output_tail_is_4kb(self):
        run = edl.RealWorld().run_check("head -c 10000 /dev/zero | tr '\\0' a; echo END", str(self.plan),
                                        self.env(), 10)
        self.assertEqual(len(run.output_tail.encode()), 4096)
        self.assertTrue(run.output_tail.endswith("aEND\n"))

    def test_timeout_kills_the_whole_process_group(self):
        pidfile = self.tmp / "child.pid"
        pid = None
        try:
            t0 = time.monotonic()
            run = edl.RealWorld().run_check(f'sleep 300 & echo $! > "{pidfile}"; wait', str(self.plan),
                                            self.env(), 1)
            self.assertLess(time.monotonic() - t0, 10)
            self.assertTrue(run.timed_out)
            self.assertIsNone(run.rc)
            self.assertIn("timed out", run.error)
            pid = int(pidfile.read_text().strip())
            self.assertTrue(_wait_gone(pid), f"child sleep {pid} survived the group kill")
        finally:
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def test_leftover_children_are_killed_after_a_normal_exit(self):
        pidfile = self.tmp / "child.pid"
        pid = None
        try:
            run = edl.RealWorld().run_check(f'sleep 300 & echo $! > "{pidfile}"; exit 0', str(self.plan),
                                            self.env(), 10)
            self.assertEqual(run.rc, 0)
            self.assertLess(run.duration_s, 9)
            pid = int(pidfile.read_text().strip())
            self.assertTrue(_wait_gone(pid), f"leftover child {pid} still running")
        finally:
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def _record_leader_state_at_group_signals(self, command, timeout):
        """Run ``command`` and note, at every group signal, whether the leader was already reaped."""
        seen = []
        real_signal = edl._signal_group

        def spy(pgid, sig):
            try:    # a child that is running or an unreaped zombie answers; a reaped one does not
                os.waitid(os.P_PID, pgid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                reaped = False
            except ChildProcessError:
                reaped = True             # the pid could be recycled from here on
            seen.append((sig, reaped))
            real_signal(pgid, sig)

        with mock.patch.object(edl, "_signal_group", spy):
            run = edl.RealWorld().run_check(command, str(self.plan), self.env(), timeout)
        return run, seen

    def test_group_is_killed_before_the_leader_is_reaped(self):
        # Normal exit: the final SIGKILL goes out while the exited shell is still an unreaped zombie
        # (it keeps the pid/pgid reserved), so it can never reach a recycled process group.
        run, seen = self._record_leader_state_at_group_signals("exit 0", 10)
        self.assertEqual(run.rc, 0)
        self.assertEqual(seen, [(signal.SIGKILL, False)])

    def test_timeout_path_also_signals_an_unreaped_leader(self):
        run, seen = self._record_leader_state_at_group_signals("sleep 300", 1)
        self.assertTrue(run.timed_out)
        self.assertEqual(seen, [(signal.SIGTERM, False), (signal.SIGKILL, False)])

    def test_cannot_start(self):
        run = edl.RealWorld().run_check("true", str(self.tmp / "no-such-dir"), self.env(), 5)
        self.assertIsNone(run.rc)
        self.assertIn("cannot start", run.error)

    def test_environment_holds_only_the_fixed_keys(self):
        sock_path = str(self.tmp / "agent.sock")
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.bind(sock_path)
            base = {"HOME": "/home/x", "LANG": "en_US.UTF-8", "SECRET_TOKEN": "s", "VIRTUAL_ENV": "/v",
                    "SSH_AUTH_SOCK": sock_path, "PATH": "/service/path"}
            env = edl.check_env("epic-t", "A", "/state/epic-t", {"PATH": "/snap/bin:/usr/bin:/bin", "GOPATH": "/g"},
                                base_env=base)
            self.assertEqual(env, {
                "PATH": "/snap/bin:/usr/bin:/bin", "HOME": "/home/x", "LANG": "en_US.UTF-8",
                "EPIC_DRIVER_RUN_ID": "epic-t", "EPIC_DRIVER_STEP": "A", "EPIC_DRIVER_STATE_DIR": "/state/epic-t",
                "EPIC_DRIVER_SSH_OPTS": "-o ConnectTimeout=5 -o BatchMode=yes", "SSH_AUTH_SOCK": sock_path})
            base["SSH_AUTH_SOCK"] = str(self.tmp / "gone.sock")
            self.assertNotIn("SSH_AUTH_SOCK", edl.check_env("epic-t", "A", "/s", {}, base_env=base))
            self.assertEqual(edl.check_env("epic-t", "A", "/s", {}, base_env={})["PATH"], edl.FALLBACK_CHECK_PATH)
        finally:
            sock.close()
        run = edl.RealWorld().run_check("env", str(self.plan), self.env(), 10)
        keys = {line.split("=", 1)[0] for line in run.output_tail.splitlines() if "=" in line}
        self.assertEqual(keys - SHELL_ADDED_ENV, set(self.env()))

    def test_cwd_is_the_plan_root(self):
        run = edl.RealWorld().run_check("pwd -P", str(self.plan), self.env(), 10)
        self.assertEqual(run.output_tail.strip(), os.path.realpath(str(self.plan)))
        L, st = self.sched([self.step("A", check="probe a")])
        edl.advance_schedule(L, st, T("2026-11-24T01:00:00Z"))
        world = FakeWorld(now=T("2026-11-24T01:00:00Z"))
        edl.run_checks(L, st, world, snapshot={"PATH": "/snap"}, base_env={"HOME": "/h"})
        ((sid, command, cwd, env, timeout),) = world.check_calls
        self.assertEqual((sid, command, cwd, timeout), ("A", "probe a", str(self.plan), 60))
        self.assertEqual(env["PATH"], "/snap")
        self.assertEqual(env["EPIC_DRIVER_STATE_DIR"], str(self.tmp / "state" / "epic-t"))

    def test_run_checks_writes_the_check_log_under_the_state_root(self):
        L, st = self.sched([self.step("A", check="echo looks fine; exit 0")])
        edl.advance_schedule(L, st, T("2026-11-24T01:00:00Z"))
        p = edl.run_checks(L, st, edl.RealWorld(), snapshot={"PATH": "/usr/bin:/bin"})
        self.assertEqual([o.outcome for _, _, o in p.ran], [edl.OUTCOME_DONE])
        self.assertEqual(st["steps"]["A"]["status"], "done")
        log = self.tmp / "state" / "epic-t" / "logs" / "checks" / "A.log"
        text = log.read_text()
        self.assertIn("check rc=0", text)
        self.assertIn("looks fine", text)

    def test_budget_stops_at_150s_and_resumes_at_the_cursor(self):
        L, st = self.sched([self.step(f"s{i}", check={"run": "probe", "timeout": "20s"}) for i in range(1, 7)])
        edl.advance_schedule(L, st, T("2026-11-24T01:00:00Z"))
        world = FakeWorld(now=T("2026-11-24T01:00:00Z"))
        for i in range(1, 7):
            world.check_script[f"s{i}"] = (1, 40)
        p = edl.run_checks(L, st, world, snapshot={})
        self.assertEqual([j.step_id for j, _, _ in p.ran], ["s1", "s2", "s3", "s4"])   # 0, 40, 80, 120 s (+20 s <= 150)
        self.assertEqual([j.step_id for j in p.not_reached], ["s5", "s6"])
        self.assertEqual(st["check_cursor"], "s5")
        world.clock = T("2026-11-24T01:10:00Z")   # the next tick
        p = edl.run_checks(L, st, world, snapshot={})
        self.assertEqual([j.step_id for j, _, _ in p.ran], ["s5", "s6", "s1", "s2"])
        self.assertEqual(st["check_cursor"], "s3")
        # everything reached ⇒ the cursor clears
        world.clock = T("2026-11-24T01:20:00Z")
        p = edl.run_checks(L, st, world, snapshot={}, budget_s=1000)
        self.assertEqual([j.step_id for j, _, _ in p.ran], ["s3", "s4", "s5", "s6", "s1", "s2"])
        self.assertIsNone(st["check_cursor"])

    def _budget_world(self, steps, run_seconds):
        L, st = self.sched(steps)
        edl.advance_schedule(L, st, T("2026-11-24T01:00:00Z"))
        world = FakeWorld(now=T("2026-11-24T01:00:00Z"))
        for step in steps:
            world.check_script[step["id"]] = (1, run_seconds)
        return L, st, world

    def test_a_job_whose_timeout_ends_past_the_budget_is_not_started(self):
        # s1 (timeout 60 s) runs 100 s; s2 would end at 100 + 60 = 160 s > 150 s: left for the next pass.
        L, st, world = self._budget_world([self.step("s1", check="probe"), self.step("s2", check="probe")], 100)
        p = edl.run_checks(L, st, world, snapshot={})
        self.assertEqual([j.step_id for j, _, _ in p.ran], ["s1"])
        self.assertEqual([j.step_id for j in p.not_reached], ["s2"])
        self.assertEqual(st["check_cursor"], "s2")
        self.assertEqual(len(world.check_calls), 1)
        world.clock = T("2026-11-24T01:10:00Z")
        p = edl.run_checks(L, st, world, snapshot={})
        self.assertEqual([j.step_id for j, _, _ in p.ran][0], "s2")       # first next pass
        self.assertTrue(all(c[4] == 60 for c in world.check_calls))       # timeouts never clamped

    def test_budget_boundary_is_inclusive_and_timeouts_are_never_clamped(self):
        # s1 runs 90 s; s2 (timeout 60 s) ends exactly at 150 s: allowed. Its timeout is passed unchanged.
        L, st, world = self._budget_world([self.step("s1", check="probe"), self.step("s2", check="probe")], 90)
        p = edl.run_checks(L, st, world, snapshot={})
        self.assertEqual([j.step_id for j, _, _ in p.ran], ["s1", "s2"])
        self.assertEqual([c[4] for c in world.check_calls], [60, 60])
        self.assertEqual(st["steps"]["s2"]["check"]["consecutive_errors"], 0)

    def test_the_first_job_runs_even_if_its_timeout_exceeds_the_budget(self):
        L, st, world = self._budget_world([self.step("big", check={"run": "probe", "timeout": "10m"}),
                                           self.step("small", check="probe")], 100)
        p = edl.run_checks(L, st, world, snapshot={})
        self.assertEqual([j.step_id for j, _, _ in p.ran], ["big"])
        self.assertEqual(world.check_calls[0][4], 600)
        self.assertEqual([j.step_id for j in p.not_reached], ["small"])
        # the same holds when the cursor puts the big one first on a later pass
        self.assertEqual(st["check_cursor"], "small")

    def test_check_every_slack_scales_with_the_period(self):
        self.assertEqual(edl.check_every_slack_s(600), 180)          # the 10-min cadence: the full cap
        self.assertEqual(edl.check_every_slack_s(3600), 180)         # capped
        self.assertEqual(edl.check_every_slack_s(180), 54)           # 30 %
        self.assertEqual(edl.check_every_slack_s(120), 36)

    def test_every_10m_runs_on_every_tick_despite_jitter(self):
        # Worst case: last run started 150 s into its tick (the budget), the next tick fires 30 s early.
        L, st = self.sched([self.step("A", check={"run": "probe", "every": "10m"})])
        edl.advance_schedule(L, st, T("2026-11-24T01:00:00Z"))
        st["steps"]["A"]["check"]["last_run_at"] = "2026-11-24T01:02:30Z"
        self.assertEqual([j.step_id for j in edl.eligible_checks(L, st, T("2026-11-24T01:09:30Z"))], ["A"])
        self.assertEqual(edl.eligible_checks(L, st, T("2026-11-24T01:09:29Z")), [])   # one second short

    def test_short_every_is_limited_when_tick_is_run_by_hand(self):
        L, st = self.sched([self.step("A", check={"run": "probe", "every": "3m"})])
        edl.advance_schedule(L, st, T("2026-11-24T01:00:00Z"))
        world = FakeWorld(now=T("2026-11-24T01:00:00Z"))
        for _ in range(3):                      # hand-run ticks seconds apart
            edl.run_checks(L, st, world, snapshot={})
            world.advance(5)
        self.assertEqual(len(world.check_calls), 1)
        world.clock = T("2026-11-24T01:02:05Z")  # 125 s: under 180 - 54 = 126 s
        edl.run_checks(L, st, world, snapshot={})
        self.assertEqual(len(world.check_calls), 1)
        world.clock = T("2026-11-24T01:02:06Z")
        edl.run_checks(L, st, world, snapshot={})
        self.assertEqual(len(world.check_calls), 2)
        # 2 m is limited too
        L2, st2 = self.sched([self.step("B", check={"run": "probe", "every": "2m"})])
        st2["steps"]["B"].update(status="watching", due_since="2026-11-24T01:00:00Z")
        st2["steps"]["B"]["check"]["last_run_at"] = "2026-11-24T01:00:00Z"
        self.assertEqual(edl.eligible_checks(L2, st2, T("2026-11-24T01:01:00Z")), [])
        self.assertEqual(len(edl.eligible_checks(L2, st2, T("2026-11-24T01:01:24Z"))), 1)

    def test_eligible_checks_does_not_write_the_state(self):
        L, st = self.sched([self.step("c15", check="true", check_window={"daily": "14:00-22:00Z", "min": "1h"})])
        edl.advance_schedule(L, st, T("2026-11-24T10:00:00Z"))
        before = copy.deepcopy(st)
        edl.eligible_checks(L, st, T("2026-11-24T15:00:00Z"))
        self.assertEqual(st, before)
        self.assertEqual(edl.window_coverage(L, st, T("2026-11-24T15:00:00Z")), {"c15": 3600})
        self.assertEqual(st, before)

    def test_check_every_rate_limit(self):
        L, st = self.sched([self.step("A", check={"run": "probe", "every": "30m"}), self.step("B", check="probe")])
        edl.advance_schedule(L, st, T("2026-11-24T01:00:00Z"))
        world = FakeWorld(now=T("2026-11-24T01:00:00Z"))
        edl.run_checks(L, st, world, snapshot={})
        self.assertEqual([c[0] for c in world.check_calls], ["A", "B"])
        world.check_calls.clear()
        world.clock = T("2026-11-24T01:10:00Z")
        edl.run_checks(L, st, world, snapshot={})
        self.assertEqual([c[0] for c in world.check_calls], ["B"])       # default 10m: every tick
        world.check_calls.clear()
        world.clock = T("2026-11-24T01:20:00Z")
        edl.run_checks(L, st, world, snapshot={})
        self.assertEqual([c[0] for c in world.check_calls], ["B"])
        world.check_calls.clear()
        world.clock = T("2026-11-24T01:30:00Z")
        edl.run_checks(L, st, world, snapshot={})
        self.assertEqual([c[0] for c in world.check_calls], ["A", "B"])

    def test_only_checked_statuses_run_their_check(self):
        L, st = self.sched([self.driver("A"), self.driver("B"), self.driver("C")])
        st["steps"]["A"]["status"] = "inflight"
        st["steps"]["B"]["status"] = "verifying"
        st["steps"]["C"]["status"] = "waiting"
        self.assertEqual([j.step_id for j in edl.eligible_checks(L, st, T("2026-11-24T01:00:00Z"))], ["B", "C"])

    def test_check_failed_outcome_carries_what_the_event_needs(self):
        L, st = self.sched([self.driver("A")])
        st["steps"]["A"].update(status="verifying", attempts=1)
        out = edl.apply_check_result(L, st, edl.CheckJob("A", "check", "true", 60),
                                     edl.CheckRun(2, T(self.ENABLED), 3.0, output_tail="p95 too high\n"),
                                     T(self.ENABLED))
        self.assertEqual((out.outcome, out.rc, out.attempt, out.output_tail),
                         (edl.OUTCOME_FAILED, 2, 2, "p95 too high\n"))
        self.assertEqual(st["steps"]["A"]["status"], "failed")


# =============================================================================
# 50-04 — runtime state (§8) and paths (§4)
# =============================================================================

class TestRuntimeState(_Sched):
    def test_paths_honour_the_env_override(self):
        self.assertEqual(edl.state_root(), self.tmp / "state")
        self.assertEqual(edl.state_dir("epic-037"), self.tmp / "state" / "epic-037")
        p = edl.run_paths("epic-037")
        self.assertEqual(p.state, self.tmp / "state" / "epic-037" / "state.json")
        self.assertEqual(p.active, self.tmp / "state" / "active.json")
        self.assertEqual((p.lock.name, p.paused.name, p.env_snapshot.name, p.last_valid_ledger.name),
                         ("tick.lock", "paused", "env_snapshot.json", "ledger.last_valid.yaml"))
        self.assertEqual(p.check_log("c15").relative_to(p.dir), Path("logs/checks/c15.log"))
        self.assertEqual(p.worktrees, p.dir / "worktrees")
        for bad in ("../x", "Epic", "", "a/b"):
            with self.assertRaises(ValueError):
                edl.state_dir(bad)
        with mock.patch.dict(os.environ):
            os.environ.pop(edl.STATE_ROOT_ENV)
            self.assertEqual(edl.state_root(), Path.home() / ".local" / "state" / "epic-driver")

    def test_new_state_has_the_architecture_keys(self):
        text = ARCHITECTURE.read_text()
        example = json.loads(re.search(r"## 8\. Runtime state JSON.*?```json\n(.*?)```", text, re.S).group(1))
        L, st = self.sched([self.driver("A")])
        self.assertEqual(set(st), set(example))
        self.assertTrue(set(example["steps"]["e1-useast01-roll"]) <= set(st["steps"]["A"]))
        self.assertEqual(set(st["steps"]["A"]["check"]),
                         set(example["steps"]["e1-useast01-roll"]["check"]))
        self.assertEqual((st["schema"], st["run_id"], st["enabled_at"], st["next_event_n"]),
                         (1, "epic-t", self.ENABLED, 1))

    def test_save_and_load_roundtrip(self):
        L, st = self.sched([self.driver("A")])
        p = edl.run_paths(L.run_id).state
        edl.save_state(p, st)
        self.assertEqual(edl.load_state(p, run_id="epic-t"), st)
        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_atomic_write_keeps_the_old_file_on_a_crash(self):
        L, st = self.sched([self.driver("A")])
        p = edl.run_paths(L.run_id).state
        edl.save_state(p, st)
        before = p.read_bytes()
        st2 = copy.deepcopy(st)
        st2["tick_seq"] = 99
        with mock.patch.object(edl.os, "replace", side_effect=OSError("killed between write and rename")):
            with self.assertRaises(OSError):
                edl.save_state(p, st2)
        self.assertEqual(p.read_bytes(), before)
        self.assertEqual([f.name for f in p.parent.iterdir()], ["state.json"])   # no tmp left
        # a process killed outright leaves its tmp behind; the state file still reads
        (p.parent / ".state.json.abc.tmp").write_text('{"schema": 1, "tick_')
        self.assertEqual(edl.load_state(p)["tick_seq"], 0)
        edl.save_state(p, st2)
        self.assertEqual(edl.load_state(p)["tick_seq"], 99)

    def test_fsync_of_file_and_directory(self):
        L, st = self.sched([self.driver("A")])
        p = edl.run_paths(L.run_id).state
        calls = []
        real_fsync = os.fsync
        with mock.patch.object(edl.os, "fsync", side_effect=lambda fd: (calls.append(fd), real_fsync(fd))):
            edl.save_state(p, st)
        self.assertEqual(len(calls), 2)

    def test_schema_mismatch_refused(self):
        L, st = self.sched([self.driver("A")])
        p = edl.run_paths(L.run_id).state
        p.parent.mkdir(parents=True)
        p.write_text(json.dumps(dict(st, schema=2)))
        with self.assertRaises(edl.StateError) as cm:
            edl.load_state(p)
        self.assertIn("schema 2", str(cm.exception))
        with self.assertRaises(edl.StateError):
            edl.save_state(p, dict(st, schema=2))
        p.write_text(json.dumps(st))
        with self.assertRaises(edl.StateError):
            edl.load_state(p, run_id="epic-other")
        p.write_text("{not json")
        with self.assertRaises(edl.StateError):
            edl.load_state(p)
        with self.assertRaises(edl.StateError):
            edl.load_state(p.parent / "missing.json")

    def test_mark_reset(self):
        L, st = self.sched([self.driver("A"), self.driver("B")])
        st["steps"]["A"].update(
            status="failed", attempts=2, occurrence=3, last_done_at="2026-11-20T00:00:00Z",
            started_at="2026-11-24T01:00:00Z", advanced_at="2026-11-24T02:00:00Z",
            inflight={"step": "A", "event": "E-4"}, last_verdict={"event": "E-4", "line": "ADVANCED: A"},
            check={"last_run_at": "2026-11-24T02:10:00Z", "last_rc": 2, "consecutive_errors": 2,
                   "output_tail": "bad", "last_error": "exit 9"},
            trigger={"fired_at": "2026-11-24T00:30:00Z", "occurrence": 3}, questions=["Q-2"])
        st["sessions"] = {"driver": {"name": "ws-t-driver-g1", "gen": 1}}
        st["outbox"] = [
            {"event": "E-4", "steps": ["A"], "confirmed_at": None},
            {"event": "E-5", "steps": ["A", "B"], "confirmed_at": None},
            {"event": "E-3", "steps": ["A"], "confirmed_at": "2026-11-24T01:00:30Z"},
        ]
        st["alarms"] = {
            "check_error:A": {"id": "check_error", "subject": "A", "cleared_at": None},
            "delivery_deferred:E-4": {"id": "delivery_deferred", "subject": "E-4", "cleared_at": None},
            "halt:B": {"id": "halt", "subject": "B", "cleared_at": None},
        }
        sessions_before = copy.deepcopy(st["sessions"])
        out = edl.mark_reset(L, st, "A", T("2026-11-24T03:00:00Z"))
        rec = st["steps"]["A"]
        self.assertEqual((rec["status"], rec["attempts"], rec["inflight"], rec["last_verdict"], rec["trigger"]),
                         ("pending", 0, None, None, None))
        self.assertEqual(rec["check"], edl.new_check_record())
        self.assertEqual((rec["occurrence"], rec["last_done_at"], rec["questions"]),
                         (3, "2026-11-20T00:00:00Z", ["Q-2"]))
        self.assertEqual(out["cancelled_events"], ["E-4"])
        self.assertEqual([(e["event"], e["steps"]) for e in st["outbox"]], [("E-5", ["B"]), ("E-3", ["A"])])
        self.assertEqual(sorted(out["cleared_alarms"]), ["check_error:A", "delivery_deferred:E-4"])
        self.assertEqual(st["alarms"]["check_error:A"]["cleared_at"], "2026-11-24T03:00:00Z")
        self.assertIsNone(st["alarms"]["halt:B"]["cleared_at"])
        self.assertEqual(st["sessions"], sessions_before)
        self.assertEqual(self.due(L, st, "2026-11-24T03:00:00Z"), ["A", "B"])   # due is recomputed

    def test_mark_done(self):
        L, st = self.sched([self.driver("A"), self.driver("R", every="1d")])
        st["steps"]["A"].update(status="inflight", inflight={"step": "A", "event": "E-1"})
        rec = edl.mark_done(L, st, "A", T("2026-11-24T05:00:00Z"))
        self.assertEqual((rec["status"], rec["done_at"], rec["by"], rec["inflight"]),
                         ("done", "2026-11-24T05:00:00Z", "operator", None))
        self.assertEqual(rec["check"]["last_run_at"], None)    # no check run
        edl.mark_done(L, st, "R", T("2026-11-24T05:00:00Z"))
        edl.advance_schedule(L, st, T("2026-11-24T05:00:00Z"))
        self.assertEqual((st["steps"]["R"]["status"], st["steps"]["R"]["occurrence"]), ("pending", 1))
        with self.assertRaises(KeyError):
            edl.mark_done(L, st, "nope", T("2026-11-24T05:00:00Z"))

    def test_ensure_step_states_adds_new_ledger_steps(self):
        L, st = self.sched([self.driver("A")])
        del st["steps"]["A"]["window_covered_s"]
        L2 = self.load(self.ledger_dict([self.driver("A"), self.driver("B")]))
        edl.ensure_step_states(L2, st)
        self.assertEqual(set(st["steps"]), {"A", "B"})
        self.assertEqual(st["steps"]["A"]["window_covered_s"], 0)

    def test_env_snapshot_reader(self):
        p = edl.run_paths("epic-t")
        self.assertEqual(edl.load_env_snapshot(p), {})
        p.dir.mkdir(parents=True)
        p.env_snapshot.write_text(json.dumps({"PATH": "/a:/b", "N": 1}))
        self.assertEqual(edl.load_env_snapshot(p), {"PATH": "/a:/b"})

    def test_nothing_written_outside_the_temp_state_root(self):
        real = Path.home() / ".local" / "state" / "epic-driver" / "epic-t"
        L, st = self.sched([self.step("A", check="true")])
        edl.advance_schedule(L, st, T("2026-11-24T01:00:00Z"))
        edl.run_checks(L, st, edl.RealWorld(), snapshot={"PATH": "/usr/bin:/bin"})
        edl.save_state(edl.run_paths(L.run_id).state, st)
        self.assertTrue((self.tmp / "state" / "epic-t" / "state.json").exists())
        self.assertFalse(real.exists())


# =============================================================================
# 50-04 — module contract
# =============================================================================

class TestTime(unittest.TestCase):
    def test_from_iso_uses_the_public_epoch_parser(self):
        import amux_spawn_lib as asl
        for value in ("2026-11-24T09:28:20Z", "2026-11-24T09:28:20+00:00", "2026-11-24T09:28:20", 0, 1.5,
                      None, "", "junk", True):
            with self.subTest(value=value):
                self.assertEqual(asl._epoch_of(value), asl.epoch_of(value))
        self.assertEqual(edl.from_iso("2026-11-24T09:28:20Z"), T("2026-11-24T09:28:20Z"))
        self.assertIsNone(edl.from_iso("junk"))
        src = (_REPO / ".claude" / "hooks" / "epic_driver_lib.py").read_text()
        self.assertNotIn("asl._", src)            # no dependence on a private name of amux_spawn_lib


class TestModuleContract(unittest.TestCase):
    def test_imports_only_stdlib_pyyaml_and_hook_modules(self):
        src = (_REPO / ".claude" / "hooks" / "epic_driver_lib.py").read_text()
        tops = set(re.findall(r"^(?:import|from) ([A-Za-z_][A-Za-z0-9_]*)", src, re.M))
        # §17's hook modules; 50-05 adds the pure reducer (keep_awake_hook, questions_store,
        # statusline and context_mcp_lib are imported lazily where they are used).
        allowed_local = {"amux_spawn_lib", "claude_event_reducer", "yaml", "__future__"}
        stdlib = set(getattr(sys, "stdlib_module_names", ())) or tops - allowed_local
        self.assertEqual(tops - stdlib - allowed_local, set())

    def test_imports_without_pyyaml(self):
        code = ("import sys; sys.modules['yaml'] = None; sys.path.insert(0, sys.argv[1]);"
                "import epic_driver_lib as e; assert e.yaml is None; print('ok')")
        r = subprocess.run([sys.executable, "-c", code, str(_REPO / ".claude" / "hooks")],
                           capture_output=True, text=True, timeout=60, cwd=str(_REPO / "tests"))
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)

    def test_no_session_pane_or_notify_effects(self):
        src = (_REPO / ".claude" / "hooks" / "epic_driver_lib.py").read_text()
        code = re.sub(r"#[^\n]*", "", re.sub(r'""".*?"""', "", src, flags=re.S))
        self.assertIn("def run_check", code)   # the stripping kept the code
        for needle in ("kill-session", "start-server"):
            self.assertNotIn(needle, code, needle)
        # "tmux kill-server" only as advice in enable's refusal text, never run
        self.assertEqual(code.count("kill-server"), 1)
        self.assertIn("kill-server", code[code.index("def tmux_move_instructions("):code.index("def move_tmux_server(")])
        # 50-05: tmux reads (display-message, capture-pane) only through RealWorld._tmux_read;
        # 50-08 adds list-sessions (the server's pid).
        verbs = set(re.findall(r'_tmux_read\(\["([a-z-]+)"', code))
        self.assertEqual(verbs, {"display-message", "capture-pane", "list-sessions"})
        # 50-08: the one server start is the systemd-run argv of tmux_start_args (§21.1), run only by
        # ensure_tmux_server through World.systemd_run; systemd, busctl and the notify module are named
        # only in RealWorld; World.notify is called only by the Notifier and enable's test notify.
        self.assertEqual(code.count("new-session"), 1)
        start_args = code[code.index("def tmux_start_args("):code.index("_SYSTEMD_BUS =")]
        self.assertIn('"new-session"', start_args)
        self.assertEqual(code.count("world.systemd_run("), 1)
        ensure = code[code.index("def ensure_tmux_server("):code.index("def tmux_conditions(")]
        self.assertIn("world.systemd_run(tmux_start_args()", ensure)
        # 50-06: amux, amux-spawn and the one tmux write (send-keys) are each named once and run only
        # inside RealWorld; the pure dispatcher calls the World effects only from run_effect.
        start = code.index("class RealWorld(World):")
        real = code[start:start + 1 + re.search(r"\n(?=\S)", code[start + 1:]).start()]
        for literal in ('"amux-spawn"', '"amux"', '"send-keys"'):
            self.assertEqual(code.count(literal), 1, literal)
        self.assertEqual(real.count('"send-keys"'), 1)
        for const, uses in (("AMUX_BIN", 1), ("AMUX_SPAWN_BIN", 2), ("SYSTEMCTL_BIN", 1), ("SYSTEMD_RUN_BIN", 1),
                            ("SYSTEMD_INHIBIT_BIN", 1), ("CLAUDE_QUESTIONS_BIN", 2)):
            self.assertEqual(real.count(const), uses, const)
            self.assertEqual(code.count(const), uses + 1, f"{const}: defined once, used only in RealWorld")
        # TMUX_BIN: twice in RealWorld, once in the argv of tmux_start_args (run through systemd-run).
        self.assertEqual(real.count("TMUX_BIN"), 2)
        self.assertEqual(code.count("TMUX_BIN"), 4)
        self.assertIn("TMUX_BIN", start_args)
        # BUSCTL_BIN: run in RealWorld; also printed in enable's refusal instructions (text only).
        self.assertEqual(real.count("BUSCTL_BIN"), 1)
        self.assertEqual(code.count('"questions_mcp_lib"'), 1)
        self.assertIn('"questions_mcp_lib"', real)
        notify_calls = re.findall(r"[\w.]*\.notify\(", code)
        self.assertEqual(sorted(notify_calls), ["qml.notify(", "self.world.notify(", "world.notify("])
        self.assertIn("qml.notify(", real)
        sender = code[code.index("class Notifier:"):code.index("def record_notify_failure(")]
        self.assertIn("self.world.notify(", sender)
        enable = code[code.index("def cmd_enable("):code.index("def cmd_disable(")]
        self.assertIn("world.notify(", enable)
        run_effect = code[code.index("def run_effect("):code.index("def execute_dispatch(")]
        for call in ("world.spawn(", "world.resume(", "world.reap(", "world.amux_send(", "world.send_keys("):
            self.assertEqual(code.count(call), 1, call)
            self.assertIn(call, run_effect)

    def test_world_fakes_fail_loudly(self):
        w = edl.World()
        for call in (lambda: w.now(), lambda: w.git([], "/"), lambda: w.run_check("", "/", {}, 1),
                     lambda: w.profile_env("x")):
            with self.assertRaises(NotImplementedError):
                call()


# =============================================================================
# 50-05 — session reconcile and stall detection (§9.3, §10, §11.1, §12, §15.2)
# =============================================================================
#
# Pane fixtures (tests/fixtures/epic_driver/panes/), Claude Code 2.1.330:
#   cc-2.1.330-folder-trust.txt, -wide.txt   real captures (throwaway session in a fresh,
#                                             untrusted /var/tmp dir; the path scrubbed)
#   cc-2.1.330-startup-no-dialog.txt          real capture (fresh session in a trusted dir)
#   cc-2.1.330-idle-input-box.txt             real bottom rows of an idle session; the
#                                             conversation text above them replaced
#   synth-code-mode-optin.txt                 synthesised: the "Code Mode (experimental)"
#                                             opt-in (Enter = "No, start without it") in the
#                                             real folder-trust layout (not reproducible here)
#   synth-input-box-typed.txt, -multiline.txt synthesised from the real idle frame
#   synth-booting-splash.txt, synth-blank.txt synthesised (splash before the box; blank)

from datetime import timezone as _tz  # noqa: E402

PANES = _REPO / "tests" / "fixtures" / "epic_driver" / "panes"
UTC = _tz.utc
CODE_MODE_MATCH = r"Code Mode \(experimental\)\n[\s\S]*❯ No, start without it"


def pane(name):
    return (PANES / name).read_text(encoding="utf-8")


def u(ts, text):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def a(ts, text="ok", model="claude-opus-5-5", error=None, api=False):
    d = {"type": "assistant", "timestamp": ts,
         "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": text}]}}
    if error:
        d["error"] = error
    if api:
        d["isApiErrorMessage"] = True
    return d


def tool(ts, name, tid, **inp):
    return {"type": "assistant", "timestamp": ts,
            "message": {"role": "assistant", "model": "claude-opus-5-5",
                        "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]}}


def compact(ts):
    return {"type": "system", "subtype": "compact_boundary", "timestamp": ts,
            "compactMetadata": {"trigger": "auto", "preTokens": 200000}}


def ev(kind, ts, **kw):
    d = {"seq": 0, "ts": ts, "event": kind, "state": "running", "session_id": "s"}
    d.update(kw)
    return d


def limit(ts, text="You've reached your Fable limit. Run /usage-credits to continue."):
    return a(ts, text, model="<synthetic>", error="rate_limit", api=True)


class ReconWorld(FakeWorld):
    """A fake World for the reconcile: tmux, handles, lifecycle logs, processes, clock, queue."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.tmux = {}        # name -> {"present", "display", "pane"}
        self.handles = []
        self.events = {}
        self.amux = []
        self.work = {}
        self.sample = edl.ClockSample("boot-1", 100000.0, 5000.0, 5000.0)
        self.tz = UTC
        self.queue = None
        self.calls = []

    def tmux_has_session(self, name):
        self.calls.append(("has_session", name))
        return bool(self.tmux.get(name, {}).get("present"))

    def tmux_display(self, name, fields):
        self.calls.append(("display", name))
        disp = self.tmux.get(name, {}).get("display")
        return None if disp is None else {f: disp.get(f, "") for f in fields}

    def capture_pane(self, name):
        self.calls.append(("capture", name))
        return self.tmux.get(name, {}).get("pane")

    def list_handles(self):
        return copy.deepcopy(self.handles)

    def lifecycle_events(self, name):
        return copy.deepcopy(self.events.get(name, []))

    def amux_names(self):
        return list(self.amux)

    def process_work(self, pane_pid):
        self.calls.append(("process_work", pane_pid))
        return self.work.get(pane_pid, edl.ProcessWork(owner_pid=pane_pid + 1, tool_shell=False))

    def clock_sample(self):
        return self.sample

    def local_timezone(self):
        return self.tz

    def question_entries(self, workspace):
        self.calls.append(("questions", workspace))
        return self.queue


class _Recon(_Sched):
    NOW = "2026-11-24T10:00:00Z"
    LAUNCH = "2026-11-24T09:00:00Z"
    SEND = "2026-11-24T09:00:00Z"

    def setUp(self):
        super().setUp()
        self.world = ReconWorld(now=T(self.NOW), profiles={
            "claude": {"ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-opus-5-5"}})
        self.tdir = self.tmp / "transcripts"
        self.tdir.mkdir()

    def now(self):
        return self.world.now()

    def epoch(self, text):
        return T(text).timestamp()

    def ledger(self, steps=None, **top):
        L = self.load(self.ledger_dict(steps if steps is not None else [self.driver("A")], **top))
        return L, edl.new_state(L, T(self.ENABLED))

    def worker_ledger(self, **top):
        repo = self.tmp / "repo"
        repo.mkdir(exist_ok=True)
        self.world.repos[str(repo)] = {"refs": {"main"}}
        d = self.ledger_dict([self.step("W", role={"worker": {
            "implementer": "impl", "reviewer": "rev", "repos": [{"path": str(repo), "base_ref": "main"}]}}),
            self.driver("A")], **top)
        d["roles"]["impl"] = {"model": "claude-opus-5-5", "effort": "high"}
        d["roles"]["rev"] = {"model": "claude-opus-5-5", "effort": "high"}
        L = self.load(d)
        return L, edl.new_state(L, T(self.ENABLED))

    def session(self, st, key="driver", name="ws-t-driver-g1", *, alive=True, status="live",
                spawned_at=None, entries=(), events=(), display=None, pane=None, state="idle",
                model="claude-opus-5-5", profile=None, mtime=None, handle=True, run_id="epic-t", **rec_kw):
        tp = self.tdir / f"{name}.jsonl"
        tp.write_text("".join(json.dumps(e) + "\n" for e in entries))
        stamp = mtime if mtime is not None else self.now().timestamp()
        os.utime(str(tp), (stamp, stamp))
        rec = {"name": name, "gen": 1, "dir": str(self.ws), "status": status,
               "spawned_at": spawned_at or self.LAUNCH, "resumed_at": [], "model_verified": None,
               "compacted": False, "parked": [], "rotation": None}
        rec.update(rec_kw)
        if key is not None:
            st["sessions"][key] = rec
        h = None
        if handle:
            h = {"name": name, "provider": "claude", "run_id": run_id, "transcript_path": str(tp),
                 "state": state, "model": model, "profile": profile, "created_at": spawned_at or self.LAUNCH,
                 "last_message": None, "stopped_at": None, "attempt": 1}
            self.world.handles.append(h)
        self.world.tmux[name] = {"present": alive,
                                 "display": display if display is not None else
                                 {"pane_dead": "0", "pane_in_mode": "0", "session_attached": "0",
                                  "pane_pid": "4242"},
                                 "pane": pane}
        self.world.events[name] = list(events)
        return rec, h, tp

    def outbox(self, st, event, name="ws-t-driver-g1", steps=("A",), *, approvals=(), kinds=("step_due",),
               sends=None, confirmed_at=None, **kw):
        entry = {"event": event, "to": "driver", "session": name, "kinds": list(kinds), "steps": list(steps),
                 "created_at": self.SEND, "sends": list(sends if sends is not None else [self.SEND]),
                 "deferrals": 0, "confirmed_at": confirmed_at, "verdict": None, "reminded_at": None}
        if approvals:
            entry["approvals"] = list(approvals)
        entry.update(kw)
        st["outbox"].append(entry)
        return entry

    def inflight(self, st, step, event="E-1", name="ws-t-driver-g1"):
        rec = st["steps"][step]
        rec["status"] = "inflight"
        rec["started_at"] = self.SEND
        rec["inflight"] = {"step": step, "event": event, "started_at": self.SEND, "session": name}
        return rec

    def answered_turn(self, event, message, start="2026-11-24T09:00:05Z", stop="2026-11-24T09:05:00Z"):
        """Transcript entries and lifecycle events of one event turn ending with ``message``."""
        entries = [u(start.replace(":05Z", ":05.100Z"), f"EVENT {event}: read /x/{event}.md"), a(stop, message)]
        events = [ev("turn_start", start), ev("stop", stop, state="idle", last_message=message)]
        return entries, events

    def reconcile(self, L, st, world=None):
        world = world or self.world
        readings = edl.take_readings(L, st, world, world.now())
        return edl.reconcile(L, st, readings, world.now()), readings

    def keys(self, res, alarm_id=None):
        return sorted(c.key for c in res.conditions if alarm_id is None or c.id == alarm_id)


# -- verdict grammar (pure) --------------------------------------------------------

class TestVerdictGrammar(unittest.TestCase):
    def rv(self, msg, steps=("A",), **kw):
        return edl.read_verdict(msg, steps=list(steps), **kw)

    def test_each_verb_parses(self):
        cases = {
            "ADVANCED: A": ("ADVANCED", "A"),
            "ADVANCED: T1 0123abc": ("ADVANCED", "T1"),
            "ADVANCED: T1 live-metrics=0123abc,ws=89abcdef0": ("ADVANCED", "T1"),
            "WAITING: A 2026-11-24T09:28:00Z": ("WAITING", None),
            "WAITING: rows not pulled yet": ("WAITING", None),
            "ASKED: Q-17 SA4": ("ASKED", "SA4"),
            "DONE: T1": ("DONE", "T1"),
            "HALT: the node did not come back": ("HALT", None),
            "HANDOFF: /state/handoffs/driver-g1.md": ("HANDOFF", None),
        }
        for line, (verb, vid) in cases.items():
            with self.subTest(line=line):
                vs = edl.parse_verdict_line(line)
                self.assertIsNotNone(vs)
                self.assertEqual(vs[0].verb, verb)
                self.assertEqual(vs[0].id, vid)
        self.assertEqual(edl.parse_verdict_line("ADVANCED: T1 a=0123abc,b=fedcba9")[0].shas,
                         (("a", "0123abc"), ("b", "fedcba9")))
        self.assertEqual(edl.parse_verdict_line("ASKED: Q-17 SA4")[0].question, "Q-17")

    def test_no_match(self):
        for line in ("advanced: A", "ADVANCED:A", "ADVANCED: A extra words", "ADVANCED: A 0123ABC",
                     "ADVANCED: A 012", "DONE: A B", "ASKED: SA4", "ASKED: 17 SA4", "HALT: ",
                     "HANDOFF: relative/path.md", "ADVANCED: A;WAITING: B x", "ADVANCED: A; HALT: stop",
                     "WAITING: ", "Done.", ""):
            with self.subTest(line=line):
                self.assertIsNone(edl.parse_verdict_line(line))

    def test_last_line_and_trailing_whitespace(self):
        r = self.rv("I rolled it.\n\nADVANCED: A   \n\n  \n")
        self.assertTrue(r.valid, r.invalid)
        self.assertEqual(r.line, "ADVANCED: A")
        self.assertIn("A", r.covered)

    def test_inside_a_code_fence_is_invalid(self):
        r = self.rv("Here:\n```\nADVANCED: A")
        self.assertEqual(r.invalid, edl.INVALID_FENCE)
        self.assertEqual(r.covered, {})
        # a closed fence before the line is fine
        self.assertTrue(self.rv("```\ncode\n```\nADVANCED: A").valid)

    def test_two_verdict_lines_invalid(self):
        r = self.rv("WAITING: A later\nADVANCED: A")
        self.assertEqual(r.invalid, edl.INVALID_TWO_LINES)
        self.assertTrue(self.rv("Not a verdict\nADVANCED: A").valid)

    def test_unknown_step_handoff_done_from_driver(self):
        self.assertEqual(self.rv("ADVANCED: B").invalid, edl.INVALID_UNKNOWN_ID)
        self.assertEqual(self.rv("HANDOFF: /x/h.md").invalid, edl.INVALID_HANDOFF)
        self.assertTrue(self.rv("HANDOFF: /x/h.md", steps=(), rotate=True).valid)
        self.assertEqual(self.rv("DONE: A").invalid, edl.INVALID_DONE_FROM_DRIVER)
        self.assertTrue(self.rv("DONE: A", from_driver=False).valid)
        self.assertEqual(self.rv("ADVANCED: A", from_driver=False).invalid, edl.INVALID_WORKER_VERB)
        self.assertEqual(self.rv("WAITING: A later", from_driver=False).invalid, edl.INVALID_WORKER_VERB)
        self.assertTrue(self.rv("HALT: blocked", from_driver=False).valid)
        self.assertEqual(self.rv("WAITING: B soon", ledger_ids=["A", "B"]).invalid, edl.INVALID_UNKNOWN_ID)
        self.assertEqual(self.rv("ADVANCED: A; ADVANCED: A").invalid, edl.INVALID_DUPLICATE)

    def test_merged_event_full_and_partial_coverage(self):
        full = self.rv("ADVANCED: A; WAITING: B 2026-11-24T12:00:00Z; ASKED: Q-3 C", steps=("A", "B", "C"))
        self.assertTrue(full.valid)
        self.assertEqual(sorted(full.covered), ["A", "B", "C"])
        self.assertEqual(full.wait["B"], ("2026-11-24T12:00:00Z", T("2026-11-24T12:00:00Z")))
        part = self.rv("ADVANCED: A", steps=("A", "B"))
        self.assertTrue(part.valid)
        self.assertEqual(sorted(part.covered), ["A"])        # B uncovered = missing for B
        halt = self.rv("HALT: stop everything", steps=("A", "B"))
        self.assertEqual(sorted(halt.covered), ["A", "B"])
        unpref = self.rv("ADVANCED: A; WAITING: until the soak ends", steps=("A", "B"))
        self.assertEqual(unpref.covered["B"].verb, "WAITING")
        self.assertEqual(unpref.wait["B"], ("until the soak ends", None))

    def test_waiting_text_limit(self):
        self.assertTrue(self.rv("WAITING: A " + "x" * 200).valid)
        self.assertEqual(self.rv("WAITING: A " + "x" * 201).invalid, edl.INVALID_WAIT)
        self.assertEqual(self.rv("WAITING: A").invalid, edl.INVALID_WAIT)

    def test_advanced_approval_needs_the_grant(self):
        without = self.rv("ADVANCED: SA4", steps=(), approvals=("SA4",), grants={})
        self.assertTrue(without.valid)
        self.assertNotIn("SA4", without.covered)
        self.assertIn("SA4", without.unverified)
        with_grant = self.rv("ADVANCED: SA4", steps=(), approvals=("SA4",), grants={"SA4": {"by": "mark"}})
        self.assertIn("SA4", with_grant.covered)

    def test_worker_step_needs_its_shas(self):
        one = {"T1": ["live-metrics"]}
        self.assertTrue(self.rv("ADVANCED: T1 0123abc", steps=("T1",), worker_repos=one).valid)
        self.assertTrue(self.rv("ADVANCED: T1 live-metrics=0123abc", steps=("T1",), worker_repos=one).valid)
        self.assertEqual(self.rv("ADVANCED: T1", steps=("T1",), worker_repos=one).invalid, edl.INVALID_SHA_MISSING)
        two = {"T1": ["a", "b"]}
        self.assertEqual(self.rv("ADVANCED: T1 a=0123abc", steps=("T1",), worker_repos=two).invalid,
                         edl.INVALID_SHA_MISSING)
        self.assertTrue(self.rv("ADVANCED: T1 a=0123abc,b=fedcba9", steps=("T1",), worker_repos=two).valid)


def _t(iso_text):
    return edl._epoch(iso_text)


# -- delivery confirmation (§9.3, reading side) --------------------------------------

class TestDeliveryConfirmation(unittest.TestCase):
    def reading(self, prompts=(), events=(), pane_text=None, captured=False):
        return edl.SessionReading(name="s", prompts=list(prompts), events=list(events),
                                  pane_text=pane_text, captured=captured)

    def test_confirmed_needs_the_prompt_and_a_turn_start_after_the_send(self):
        entry = {"event": "E-41", "sends": ["2026-11-24T09:30:06Z"]}
        r = self.reading([{"timestamp": "2026-11-24T09:30:07.200Z", "text": "EVENT E-41: read /x"}],
                         [ev("turn_start", "2026-11-24T09:30:07.100000+00:00")])
        conf = edl.delivery_confirmation(entry, r)
        self.assertEqual(conf["prompt_at"], "2026-11-24T09:30:07.200Z")
        no_turn = self.reading([{"timestamp": "2026-11-24T09:30:07Z", "text": "EVENT E-41: read /x"}])
        self.assertIsNone(edl.delivery_confirmation(entry, no_turn))
        before = self.reading([{"timestamp": "2026-11-24T09:29:00Z", "text": "EVENT E-41: read /x"}],
                              [ev("turn_start", "2026-11-24T09:30:07Z")])
        self.assertIsNone(edl.delivery_confirmation(entry, before))

    def test_event_id_boundary(self):
        entry = {"event": "E-4", "sends": ["2026-11-24T09:30:06Z"]}
        r = self.reading([{"timestamp": "2026-11-24T09:30:07Z", "text": "EVENT E-41: read /x"}],
                         [ev("turn_start", "2026-11-24T09:30:07Z")])
        self.assertIsNone(edl.delivery_confirmation(entry, r))

    SEND_ISO = "2026-11-24T09:30:06Z"

    def proven(self, prompts=(), events=(), **kw):
        """A reading that proves absence: a transcript covering the send, nothing after it, a clean pane."""
        base = dict(transcript_path="/t.jsonl", transcript_first_at=_t("2026-11-24T09:00:00Z"),
                    pane_text=pane("cc-2.1.330-idle-input-box.txt"), captured=True)
        base.update(kw)
        return edl.SessionReading(name="s", prompts=list(prompts), events=list(events), **base)

    def test_provably_absent(self):
        entry = {"event": "E-41", "sends": [self.SEND_ISO], "confirmed_at": None}
        self.assertTrue(edl.delivery_provably_absent(entry, self.proven()))
        typed = pane("cc-2.1.330-idle-input-box.txt").replace("❯\xa0", "❯\xa0EVENT E-41: read /x", 1)
        self.assertFalse(edl.delivery_provably_absent(entry, self.proven(pane_text=typed)))
        self.assertFalse(edl.delivery_provably_absent(entry, self.proven(pane_text=None, captured=False)))
        landed = self.proven(prompts=[{"timestamp": "2026-11-24T09:30:07Z", "text": "EVENT E-41: read /x"}])
        self.assertFalse(edl.delivery_provably_absent(entry, landed))

    def test_not_provable_without_a_readable_transcript(self):
        entry = {"event": "E-41", "sends": [self.SEND_ISO], "confirmed_at": None}
        self.assertFalse(edl.delivery_provably_absent(entry, self.proven(transcript_path=None)))
        self.assertFalse(edl.delivery_provably_absent(entry, self.proven(transcript_first_at=None)))

    def test_not_provable_when_the_tail_may_be_truncated_before_the_send(self):
        entry = {"event": "E-41", "sends": [self.SEND_ISO], "confirmed_at": None}
        self.assertFalse(edl.delivery_provably_absent(
            entry, self.proven(transcript_first_at=_t("2026-11-24T09:31:00Z"))))

    def test_not_provable_when_a_turn_started_at_or_after_the_send(self):
        entry = {"event": "E-41", "sends": [self.SEND_ISO], "confirmed_at": None}
        for when in ("2026-11-24T09:30:11Z", self.SEND_ISO):
            with self.subTest(when=when):
                self.assertFalse(edl.delivery_provably_absent(
                    entry, self.proven(events=[ev("turn_start", when)])))
        # the reviewer's reproduction: no transcript at all, a turn 5 s after the send
        self.assertFalse(edl.delivery_provably_absent(
            entry, edl.SessionReading(name="s", events=[ev("turn_start", "2026-11-24T09:30:11Z")],
                                      pane_text=pane("cc-2.1.330-idle-input-box.txt"), captured=True)))
        # a turn that started before the send does not matter
        self.assertTrue(edl.delivery_provably_absent(
            entry, self.proven(events=[ev("turn_start", "2026-11-24T09:20:00Z")])))


# -- startup dialogs: the classifier and its fixtures (§12.11) ----------------------

class TestPaneClassifier(unittest.TestCase):
    def test_fixtures(self):
        expect = {
            "cc-2.1.330-folder-trust.txt": edl.PANE_DIALOG,
            "cc-2.1.330-folder-trust-wide.txt": edl.PANE_DIALOG,
            "synth-code-mode-optin.txt": edl.PANE_DIALOG,
            "cc-2.1.330-startup-no-dialog.txt": edl.PANE_NO_DIALOG,
            "cc-2.1.330-idle-input-box.txt": edl.PANE_NO_DIALOG,
            "synth-input-box-typed.txt": edl.PANE_NO_DIALOG,
            "synth-input-box-multiline.txt": edl.PANE_NO_DIALOG,
            "synth-booting-splash.txt": edl.PANE_UNREADABLE,
            "synth-blank.txt": edl.PANE_UNREADABLE,
            "synth-conversation-quote.txt": edl.PANE_NO_DIALOG,
            "synth-conversation-quote-pointer.txt": edl.PANE_NO_DIALOG,
            # 50-06: real captures of the input box (see the 50-06 section's provenance note)
            "cc-2.1.330-input-typed.txt": edl.PANE_NO_DIALOG,
            "cc-2.1.330-input-multiline-paste.txt": edl.PANE_NO_DIALOG,
            "cc-2.1.330-input-pasted-placeholder.txt": edl.PANE_NO_DIALOG,
            "cc-2.1.330-input-event-line.txt": edl.PANE_NO_DIALOG,
            "cc-2.1.330-input-wrapped-40col.txt": edl.PANE_NO_DIALOG,
            "cc-2.1.330-input-empty-after-clear.txt": edl.PANE_NO_DIALOG,
            "cc-2.1.330-copy-mode-empty-box.txt": edl.PANE_NO_DIALOG,
            "cc-2.1.330-shell-mode.txt": edl.PANE_UNREADABLE,
        }
        self.assertEqual(set(expect), {p.name for p in PANES.glob("*.txt")})
        for name, kind in expect.items():
            with self.subTest(name=name):
                self.assertEqual(edl.classify_pane(pane(name)).kind, kind)
        self.assertEqual(edl.classify_pane(None).kind, edl.PANE_UNREADABLE)

    def test_a_gt_prompt_glyph_input_box_is_not_a_dialog(self):
        gt = pane("cc-2.1.330-idle-input-box.txt").replace("❯", ">")
        self.assertEqual(edl.classify_pane(gt).kind, edl.PANE_NO_DIALOG)

    def test_a_gt_quote_is_never_a_pointer_even_with_a_hint(self):
        text = pane("cc-2.1.330-folder-trust.txt").replace("❯ No, exit", "> No, exit")
        self.assertNotEqual(edl.classify_pane(text).kind, edl.PANE_DIALOG)

    def test_a_numbered_pointer_row_is_a_dialog_without_the_hint(self):
        self.assertEqual(edl.classify_pane("\n Pick one\n\n ❯ 1. Yes\n   2. No\n").kind, edl.PANE_DIALOG)

    def test_dialog_detail_is_its_first_lines(self):
        self.assertEqual(edl.classify_pane(pane("cc-2.1.330-folder-trust.txt")).detail,
                         "Accessing workspace: / /var/tmp/epic-driver-fixture")
        self.assertTrue(edl.classify_pane(pane("synth-code-mode-optin.txt")).detail
                        .startswith("Code Mode (experimental) / "))

    def test_fixtures_are_scrubbed(self):
        for p in PANES.glob("*.txt"):
            text = p.read_text(encoding="utf-8")
            with self.subTest(p=p.name):
                self.assertNotIn(str(Path.home()), text)
                self.assertNotIn("edr-trust", text)
                self.assertNotIn("/home", text)
                self.assertNotIn("@", text)
                for word in ("Ultra", "Max", "Pro ", "login", "expires"):
                    self.assertNotIn(word, text)

    def test_listed_match_needs_the_highlighted_option(self):
        pat = re.compile(CODE_MODE_MATCH)
        self.assertTrue(pat.search(pane("synth-code-mode-optin.txt")))
        # the startup banner "Code Mode (experimental) is on" is not the opt-in
        self.assertFalse(pat.search(pane("cc-2.1.330-startup-no-dialog.txt")))
        self.assertFalse(pat.search(pane("cc-2.1.330-folder-trust.txt")))


# -- the reconcile pass ---------------------------------------------------------------

class TestReconcileVerdicts(_Recon):
    def test_advanced_moves_to_verifying_and_clears_inflight(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "Rolled.\nADVANCED: A")
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        s = res.state["steps"]["A"]
        self.assertEqual(s["status"], "verifying")
        self.assertIsNone(s["inflight"])
        self.assertEqual(s["advanced_at"], "2026-11-24T09:05:00Z")
        self.assertEqual(s["last_verdict"]["line"], "ADVANCED: A")
        e = res.state["outbox"][0]
        self.assertEqual(e["confirmed_at"], "2026-11-24T09:00:05Z")
        self.assertTrue(e["verdict"]["complete"])
        self.assertEqual(res.confirmed, ["E-1"])
        self.assertEqual(res.actions, [])
        self.assertEqual(st["steps"]["A"]["status"], "inflight")     # the input state is untouched

    def test_unchecked_driver_step_is_done_by_its_verdict(self):
        L, st = self.ledger([self.step("A", role="driver")])
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "ADVANCED: A")
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["A"]["status"], "done")
        self.assertEqual(res.state["steps"]["A"]["by"], "verdict")

    def test_failed_step_re_enters_due_on_advanced(self):
        L, st = self.ledger()
        st["steps"]["A"].update(status="failed", attempts=1)
        entries, events = self.answered_turn("E-2", "ADVANCED: A")
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-2", kinds=("check_failed",))
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["A"]["status"], "due")
        self.assertEqual(res.state["steps"]["A"]["attempts"], 1)    # counted once, at the failure

    def test_waiting_with_a_time_and_with_text(self):
        L, st = self.ledger([self.driver("A"), self.driver("B")])
        self.inflight(st, "A")
        self.inflight(st, "B")
        entries, events = self.answered_turn("E-1", "WAITING: A 2026-11-24T12:00:00Z; WAITING: B the soak")
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-1", steps=("A", "B"))
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["A"]["status"], "waiting")
        self.assertEqual(res.state["steps"]["A"]["wait"]["until"], "2026-11-24T12:00:00Z")
        self.assertEqual(res.state["steps"]["B"]["wait"], {"text": "the soak", "until": None, "event": "E-1",
                                                           "at": "2026-11-24T09:05:00Z"})

    def test_asked_records_the_question(self):
        L, st = self.ledger(approvals={"SA4": {"conditions": []}})
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "ASKED: Q-17 A; ASKED: Q-18 SA4")
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-1", approvals=("SA4",))
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["A"]["status"], "asked")
        self.assertEqual(res.state["steps"]["A"]["questions"], ["Q-17"])
        self.assertEqual(res.state["questions"]["Q-17"]["step"], "A")
        self.assertEqual(res.state["questions"]["Q-18"]["approval"], "SA4")
        self.assertEqual(res.state["approval_question"], {"id": "SA4", "question": "Q-18"})

    def test_driver_halt_raises_halt_and_a_later_verdict_clears_it(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "HALT: the node did not come back")
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["A"]["status"], "halted")
        self.assertIn("halt:A", [c.key for c in edl.step_alarm_conditions(L, res.state, self.now())])
        # a later event on the step (an answer, say) ends with a verdict: the halt clears
        st2 = res.state
        entries2 = entries + [u("2026-11-24T09:40:00.200Z", "EVENT E-2: read /x/E-2.md"),
                              a("2026-11-24T09:45:00Z", "ADVANCED: A")]
        events2 = events + [ev("turn_start", "2026-11-24T09:40:00Z"),
                            ev("stop", "2026-11-24T09:45:00Z", last_message="ADVANCED: A")]
        self.world.handles = []
        self.session(st2, entries=entries2, events=events2)
        self.outbox(st2, "E-2", kinds=("answer",), sends=["2026-11-24T09:40:00Z"])
        res2, _ = self.reconcile(L, st2)
        self.assertEqual(res2.state["steps"]["A"]["status"], "verifying")
        self.assertNotIn("halt:A", [c.key for c in edl.step_alarm_conditions(L, res2.state, self.now())])

    def test_worker_handoff_is_valid_only_in_a_rotate_turn(self):
        """§10/§14.3: a worker may use HANDOFF exactly where the driver may; every other verb stays DONE/HALT."""
        kw = dict(steps=(), from_driver=False)
        ok = edl.read_verdict("Written.\nHANDOFF: /x/h.md", rotate=True, **kw)
        self.assertTrue(ok.valid)
        self.assertEqual(ok.handoff.text, "/x/h.md")
        self.assertEqual(edl.read_verdict("HANDOFF: /x/h.md", rotate=False, **kw).invalid, edl.INVALID_HANDOFF)
        self.assertEqual(edl.read_verdict("HANDOFF: /x/h.md", steps=("W",), from_driver=False).invalid,
                         edl.INVALID_HANDOFF)
        # a rotate turn does not widen the worker's other verbs
        self.assertEqual(edl.read_verdict("ADVANCED: W", steps=("W",), from_driver=False, rotate=True).invalid,
                         edl.INVALID_WORKER_VERB)
        self.assertEqual(edl.read_verdict("WAITING: soak", steps=(), from_driver=False, rotate=True).invalid,
                         edl.INVALID_WORKER_VERB)

    def test_worker_handoff_outside_a_rotate_turn_is_not_applied(self):
        L, st = self.worker_ledger()
        self.inflight(st, "W", name="t-w-t-impl-w-g1")
        entries, events = self.answered_turn("E-3", "HANDOFF: /x/h.md")
        self.session(st, key="impl:W", name="t-w-t-impl-w-g1", entries=entries, events=events,
                     rotation={"event": "E-9", "requested_at": "2026-11-24T09:00:00Z", "handoff_path": "/x/h.md"})
        self.outbox(st, "E-3", name="t-w-t-impl-w-g1", steps=("W",))
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["outbox"][0]["verdict"]["invalid"], edl.INVALID_HANDOFF)
        self.assertIsNone(res.state["sessions"]["impl:W"]["rotation"].get("handoff"))
        self.assertFalse(res.state["outbox"][0]["verdict"]["complete"])

    def test_worker_handoff_in_a_rotate_turn_is_recorded(self):
        L, st = self.worker_ledger()
        self.inflight(st, "W", name="t-w-t-impl-w-g1")
        handoff = self.tmp / "wh.md"
        handoff.write_text("# Handoff\n")
        entries, events = self.answered_turn("E-7", f"HANDOFF: {handoff}")
        self.session(st, key="impl:W", name="t-w-t-impl-w-g1", entries=entries, events=events,
                     rotation={"event": "E-7", "requested_at": "2026-11-24T09:00:00Z", "handoff_path": str(handoff)})
        os.utime(str(handoff), (self.epoch("2026-11-24T09:04:00Z"),) * 2)
        self.outbox(st, "E-7", name="t-w-t-impl-w-g1", steps=(), kinds=("rotate",))
        res, _ = self.reconcile(L, st)
        rot = res.state["sessions"]["impl:W"]["rotation"]
        self.assertEqual(rot["handoff"]["path"], str(handoff))
        self.assertTrue(res.sessions["impl:W"].rotation_ready)
        self.assertTrue(res.state["outbox"][0]["verdict"]["complete"])

    def test_worker_done_and_halt_route_to_review_and_the_driver(self):
        L, st = self.worker_ledger()
        self.inflight(st, "W", name="t-w-t-impl-w-g1")
        entries, events = self.answered_turn("E-3", "Implemented.\nDONE: W")
        self.session(st, key="impl:W", name="t-w-t-impl-w-g1", entries=entries, events=events)
        self.outbox(st, "E-3", name="t-w-t-impl-w-g1", steps=("W",))
        res, _ = self.reconcile(L, st)
        self.assertEqual([(i.kind, i.to, i.step) for i in res.items], [("review_due", "rev:W", "W")])
        self.assertEqual(res.state["steps"]["W"]["status"], "inflight")     # the driver closes it later
        L, st = self.worker_ledger()
        self.world.handles = []
        entries, events = self.answered_turn("E-4", "DONE: W")
        self.session(st, key="rev:W", name="t-w-t-rev-w-g1", entries=entries, events=events)
        self.outbox(st, "E-4", name="t-w-t-rev-w-g1", steps=("W",), kinds=("review_due",))
        res, _ = self.reconcile(L, st)
        self.assertEqual([(i.kind, i.to, i.data["worker"]) for i in res.items], [("worker_settled", "driver", "reviewer")])
        self.world.handles = []
        L, st = self.worker_ledger()
        entries, events = self.answered_turn("E-5", "HALT: the tests need a fixture I cannot create")
        self.session(st, key="impl:W", name="t-w-t-impl-w-g1", entries=entries, events=events)
        self.outbox(st, "E-5", name="t-w-t-impl-w-g1", steps=("W",))
        res, _ = self.reconcile(L, st)
        self.assertEqual([(i.kind, i.data["worker"]) for i in res.items], [("worker_settled", "implementer")])
        self.assertNotEqual(res.state["steps"]["W"]["status"], "halted")      # a worker HALT is no alarm

    def test_driver_closes_a_worker_step_with_its_sha(self):
        L, st = self.worker_ledger()
        self.inflight(st, "W")
        entries, events = self.answered_turn("E-6", "ADVANCED: W 0123abcd")
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-6", steps=("W",), kinds=("worker_settled",))
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["W"]["status"], "verifying")
        self.assertEqual(res.state["steps"]["W"]["shas"], {"": "0123abcd"})

    def test_handoff_records_rotation_progress(self):
        L, st = self.ledger()
        handoff = self.tmp / "h.md"
        handoff.write_text("# Handoff\n")
        entries, events = self.answered_turn("E-7", f"HANDOFF: {handoff}")
        rec, _, _ = self.session(st, entries=entries, events=events,
                                 rotation={"event": "E-7", "requested_at": "2026-11-24T09:00:00Z",
                                           "handoff_path": str(handoff)})
        os.utime(str(handoff), (self.epoch("2026-11-24T09:04:00Z"),) * 2)
        self.outbox(st, "E-7", steps=(), kinds=("rotate",))
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["sessions"]["driver"]["rotation"]["handoff"]["path"], str(handoff))
        self.assertTrue(res.sessions["driver"].rotation_ready)
        self.assertTrue(res.state["outbox"][0]["verdict"]["complete"])

    def test_merged_event_partly_covered_gets_one_reminder_then_verdict_missing(self):
        L, st = self.ledger([self.driver("A"), self.driver("B")])
        self.inflight(st, "A")
        self.inflight(st, "B")
        entries, events = self.answered_turn("E-1", "ADVANCED: A")
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-1", steps=("A", "B"))
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["A"]["status"], "verifying")
        self.assertEqual(res.state["outbox"][0]["verdict"]["missing"], ["B"])
        self.assertEqual([(x.kind, x.data["event"], x.data["missing"]) for x in res.actions],
                         [("verdict_reminder", "E-1", ["B"])])
        self.assertEqual(self.keys(res, "verdict_missing"), [])
        # the dispatcher sent the reminder; the reminder turn still does not cover B
        st2 = res.state
        st2["outbox"][0]["reminded_at"] = "2026-11-24T09:10:00Z"
        self.world.events["ws-t-driver-g1"] += [ev("turn_start", "2026-11-24T09:10:01Z"),
                                                ev("stop", "2026-11-24T09:11:00Z", last_message="Still checking.")]
        res2, _ = self.reconcile(L, st2)
        self.assertEqual(self.keys(res2, "verdict_missing"), ["verdict_missing:E-1"])
        self.assertEqual(res2.actions, [])                          # only one reminder
        # a later turn naming the event covers B: verdict_missing clears
        st3 = res2.state
        self.world.handles = []
        self.session(st3, entries=entries + [u("2026-11-24T09:20:00.300Z", "E-1 B: what is the verdict?")],
                     events=self.world.events["ws-t-driver-g1"] + [
                         ev("turn_start", "2026-11-24T09:20:00Z"),
                         ev("stop", "2026-11-24T09:21:00Z", last_message="WAITING: B the soak")])
        res3, _ = self.reconcile(L, st3)
        self.assertEqual(self.keys(res3, "verdict_missing"), [])
        self.assertTrue(res3.state["outbox"][0]["verdict"]["complete"])

    def test_invalid_verdict_counts_as_missing(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "Done:\n```\nADVANCED: A")
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["A"]["status"], "inflight")
        self.assertEqual(res.actions[0].data["invalid"], edl.INVALID_FENCE)

    def test_operator_turn_is_exempt_and_the_event_turn_still_counts(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "ADVANCED: A")
        entries += [u("2026-11-24T09:30:00.100Z", "also tidy the notes, please"), a("2026-11-24T09:31:00Z", "Tidied.")]
        events += [ev("turn_start", "2026-11-24T09:30:00Z"),
                   ev("stop", "2026-11-24T09:31:00Z", last_message="Tidied.")]
        _, h, _ = self.session(st, entries=entries, events=events)
        h.update(last_message="Tidied.", stopped_at="2026-11-24T09:31:00Z")     # the handle holds the later turn
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["A"]["status"], "verifying")
        self.assertEqual(res.actions, [])
        self.assertEqual(self.keys(res, "verdict_missing"), [])

    INT_ENTRIES = [u("2026-11-24T09:00:05.100Z", "EVENT E-1: read /x/E-1.md"),
                   u("2026-11-24T09:10:00.100Z", "never mind, just list the files"),
                   a("2026-11-24T09:11:00Z", "ADVANCED: A")]

    def interrupted_session(self, st, *, open_turn=False):
        # the event turn starts at 09:00:05 and is interrupted (Esc fires no Stop); the operator
        # types another turn whose stop carries a perfectly valid verdict line for the event
        events = [ev("turn_start", "2026-11-24T09:00:05Z"), ev("turn_start", "2026-11-24T09:10:00Z")]
        if not open_turn:
            events.append(ev("stop", "2026-11-24T09:11:00Z", state="idle", last_message="ADVANCED: A"))
        _, h, _ = self.session(st, entries=list(self.INT_ENTRIES), events=events,
                               state="running" if open_turn else "idle")
        if not open_turn:
            h.update(last_message="ADVANCED: A", stopped_at="2026-11-24T09:11:00Z")
        return events

    def test_an_interrupted_event_turn_does_not_borrow_the_operator_turns_stop(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        self.interrupted_session(st)
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["A"]["status"], "inflight")      # no verdict read
        self.assertEqual(res.verdicts, [])
        self.assertFalse(res.state["outbox"][0]["verdict"]["complete"])
        self.assertEqual(res.state["outbox"][0]["verdict"]["turns"], [])
        self.assertEqual(self.keys(res, "verdict_missing"), [])

    def test_interrupted_event_turn_then_idle_gets_one_reminder_and_the_reminder_settles_it(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        events = self.interrupted_session(st)
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        # idle again: one VERDICT? reminder for the event; the operator turn's line is not applied
        self.assertEqual([(x.kind, x.data["event"], x.data["missing"]) for x in res.actions],
                         [("verdict_reminder", "E-1", ["A"])])
        self.assertEqual(res.state["steps"]["A"]["status"], "inflight")
        self.assertEqual(self.keys(res, "verdict_missing"), [])
        # the dispatcher sent it (reminded_at set); the reminder turn ends with a valid verdict
        st2 = res.state
        st2["outbox"][0]["reminded_at"] = "2026-11-24T09:20:00Z"
        self.world.handles = []
        _, h, _ = self.session(
            st2, entries=self.INT_ENTRIES + [u("2026-11-24T09:20:00.100Z", "VERDICT? E-1"),
                                             a("2026-11-24T09:21:00Z", "ADVANCED: A")],
            events=events + [ev("turn_start", "2026-11-24T09:20:00Z"),
                             ev("stop", "2026-11-24T09:21:00Z", state="idle", last_message="ADVANCED: A")])
        res2, _ = self.reconcile(L, st2)
        self.assertEqual(res2.state["steps"]["A"]["status"], "verifying")
        self.assertTrue(res2.state["outbox"][0]["verdict"]["complete"])
        self.assertEqual(res2.actions, [])
        self.assertEqual(self.keys(res2, "verdict_missing"), [])

    def test_interrupted_event_turn_while_the_operator_turn_is_open_emits_nothing(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        self.interrupted_session(st, open_turn=True)
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.actions, [])
        self.assertEqual(self.keys(res, "verdict_missing"), [])
        self.assertEqual(res.state["steps"]["A"]["status"], "inflight")

    def test_interrupted_event_turn_without_a_verdict_after_the_reminder_raises_verdict_missing(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        events = self.interrupted_session(st)
        self.outbox(st, "E-1", reminded_at="2026-11-24T09:20:00Z")
        self.world.handles = []
        events = events + [ev("turn_start", "2026-11-24T09:20:00Z"),
                           ev("stop", "2026-11-24T09:21:00Z", state="idle", last_message="Looking.")]
        entries = self.INT_ENTRIES + [u("2026-11-24T09:20:00.100Z", "VERDICT? E-1"), a("2026-11-24T09:21:00Z", "Looking.")]
        self.session(st, entries=entries, events=events)
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "verdict_missing"), ["verdict_missing:E-1"])
        self.assertEqual(res.actions, [])                       # only the one reminder
        # a later turn naming the event with a valid verdict settles it and clears the alarm
        self.world.handles = []
        self.session(res.state, entries=entries + [u("2026-11-24T09:30:00.100Z", "E-1 status?"),
                                                   a("2026-11-24T09:31:00Z", "ADVANCED: A")],
                     events=events + [ev("turn_start", "2026-11-24T09:30:00Z"),
                                      ev("stop", "2026-11-24T09:31:00Z", state="idle", last_message="ADVANCED: A")])
        res2, _ = self.reconcile(L, res.state)
        self.assertEqual(self.keys(res2, "verdict_missing"), [])
        self.assertEqual(res2.state["steps"]["A"]["status"], "verifying")
        self.assertTrue(res2.state["outbox"][0]["verdict"]["complete"])

    def test_interrupted_event_turn_with_background_tasks_running_emits_nothing(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        events = [ev("turn_start", "2026-11-24T09:00:05Z"), ev("turn_start", "2026-11-24T09:10:00Z"),
                  ev("stop", "2026-11-24T09:11:00Z", state="idle", last_message="Started a build.",
                     background_tasks_count=1)]
        _, h, _ = self.session(st, entries=list(self.INT_ENTRIES), events=events, state="running")
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.actions, [])
        self.assertEqual(self.keys(res, "verdict_missing"), [])

    def test_interrupted_reminder_turn_raises_verdict_missing_without_a_second_reminder(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        events = self.interrupted_session(st)
        self.outbox(st, "E-1", reminded_at="2026-11-24T09:20:00Z")
        self.world.handles = []
        # the reminder turn (09:20) is interrupted by an operator turn (09:30) that ends idle
        events = events + [ev("turn_start", "2026-11-24T09:20:00Z"), ev("turn_start", "2026-11-24T09:30:00Z"),
                           ev("stop", "2026-11-24T09:31:00Z", state="idle", last_message="Tidied.")]
        entries = self.INT_ENTRIES + [u("2026-11-24T09:20:00.100Z", "VERDICT? E-1"),
                                      u("2026-11-24T09:30:00.100Z", "just tidy up"), a("2026-11-24T09:31:00Z", "Tidied.")]
        self.session(st, entries=entries, events=events)
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "verdict_missing"), ["verdict_missing:E-1"])
        self.assertEqual(res.actions, [])
        self.world.handles = []
        self.session(res.state, entries=entries + [u("2026-11-24T09:40:00.100Z", "E-1 status?"),
                                                   a("2026-11-24T09:41:00Z", "ADVANCED: A")],
                     events=events + [ev("turn_start", "2026-11-24T09:40:00Z"),
                                      ev("stop", "2026-11-24T09:41:00Z", state="idle", last_message="ADVANCED: A")])
        res2, _ = self.reconcile(L, res.state)
        self.assertEqual(self.keys(res2, "verdict_missing"), [])
        self.assertEqual(res2.actions, [])
        self.assertEqual(res2.state["steps"]["A"]["status"], "verifying")

    def test_a_normal_turn_followed_by_an_operator_turn_still_settles(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "ADVANCED: A")
        entries += [u("2026-11-24T09:30:00.100Z", "also tidy the notes"), a("2026-11-24T09:31:00Z", "Tidied.")]
        events += [ev("turn_start", "2026-11-24T09:30:00Z"), ev("stop", "2026-11-24T09:31:00Z", last_message="Tidied.")]
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["A"]["status"], "verifying")
        self.assertTrue(res.state["outbox"][0]["verdict"]["complete"])

    def test_the_reading_records_how_far_back_the_tail_reaches(self):
        L, st = self.ledger()
        entries, events = self.answered_turn("E-1", "ADVANCED: A")
        self.session(st, entries=entries, events=events)
        res, readings = self.reconcile(L, st)
        r = readings.by_name()["ws-t-driver-g1"]
        self.assertEqual(r.transcript_first_at, _t("2026-11-24T09:00:05.100Z"))

    def test_the_ping_is_exempt(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "ADVANCED: A")
        ping = 'PING: no action needed; reply exactly "WAITING: ping"'
        entries += [u("2026-11-24T09:45:00.100Z", ping), a("2026-11-24T09:45:10Z", "WAITING: ping")]
        events += [ev("turn_start", "2026-11-24T09:45:00Z"),
                   ev("stop", "2026-11-24T09:45:10Z", last_message="WAITING: ping")]
        _, h, _ = self.session(st, entries=entries, events=events)
        h.update(last_message="WAITING: ping", stopped_at="2026-11-24T09:45:10Z")
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["steps"]["A"]["status"], "verifying")     # not waiting
        self.assertEqual([v["line"] for v in res.verdicts], ["ADVANCED: A"])
        # a ping alone, with nothing in the outbox, touches nothing
        L2, st2 = self.ledger()
        self.inflight(st2, "A")
        self.world.handles = []
        self.session(st2, entries=entries[2:], events=events[2:])
        res2, _ = self.reconcile(L2, st2)
        self.assertEqual(res2.state["steps"]["A"], st2["steps"]["A"])
        self.assertEqual((res2.items, res2.actions, res2.verdicts), ([], [], []))

    def test_limited_turn_is_not_missing_a_verdict(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        entries = [u("2026-11-24T09:00:05.100Z", "EVENT E-1: read /x"),
                   limit("2026-11-24T09:01:00Z", "You've hit your session limit · resets 3pm")]
        events = [ev("turn_start", "2026-11-24T09:00:05Z"),
                  ev("stop", "2026-11-24T09:01:01Z", last_message="You've hit your session limit · resets 3pm")]
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.actions, [])
        self.assertEqual(self.keys(res, "verdict_missing"), [])
        self.assertEqual(res.state["outbox"][0]["verdict"]["limited_at"], "2026-11-24T09:01:00Z")
        self.assertEqual(self.keys(res, "usage_limit"), ["usage_limit:claude-opus-5-5"])

    def test_unconfirmed_entry_reads_no_verdict(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        self.session(st, entries=[a("2026-11-24T09:05:00Z", "ADVANCED: A")],
                     events=[ev("stop", "2026-11-24T09:05:00Z", last_message="ADVANCED: A")])
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        self.assertIsNone(res.state["outbox"][0]["confirmed_at"])
        self.assertEqual(res.state["steps"]["A"]["status"], "inflight")

    def test_a_verdict_is_read_once(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "ADVANCED: A")
        self.session(st, entries=entries, events=events)
        self.outbox(st, "E-1")
        res, _ = self.reconcile(L, st)
        res2, _ = self.reconcile(L, res.state)
        self.assertEqual(res2.verdicts, [])
        self.assertEqual(res2.state["steps"]["A"], res.state["steps"]["A"])


class TestLiveness(_Recon):
    def test_dead_pane_and_missing_session(self):
        L, st = self.ledger()
        self.session(st, display={"pane_dead": "1", "pane_in_mode": "0", "session_attached": "0", "pane_pid": "1"})
        res, _ = self.reconcile(L, st)
        f = res.sessions["driver"]
        self.assertEqual((f.alive, f.status, f.died), (False, edl.ST_DEAD, True))
        self.assertEqual(res.state["sessions"]["driver"]["status"], "dead")
        L, st = self.ledger()
        self.world.handles = []
        self.session(st, alive=False)
        self.world.calls = []
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.sessions["driver"].status, edl.ST_DEAD)
        self.assertEqual(self.world.calls, [("has_session", "ws-t-driver-g1")])   # nothing else read

    def test_a_resumed_session_reads_alive_despite_a_stored_terminated(self):
        L, st = self.ledger()
        old = [ev("turn_start", "2026-11-24T08:00:00Z"), ev("stop", "2026-11-24T08:05:00Z"),
               ev("session_end", "2026-11-24T08:30:00Z")]
        # resumed, before its first turn: the log still ends in session_end, the handle says terminated
        self.session(st, events=old, state="terminated", status="resuming")
        res, _ = self.reconcile(L, st)
        f = res.sessions["driver"]
        self.assertTrue(f.alive)
        self.assertEqual(f.status, edl.ST_STARTING)
        self.assertTrue(f.came_up)
        self.assertEqual(res.state["sessions"]["driver"]["status"], "live")
        # its first turn after the resume clears the latch (50-02): idle, not terminated
        L, st = self.ledger()
        self.world.handles = []
        self.session(st, events=old + [ev("turn_start", "2026-11-24T09:00:10Z"), ev("stop", "2026-11-24T09:02:00Z")],
                     state="terminated")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.sessions["driver"].status, edl.ST_IDLE)

    def test_busy_attached_overage_and_context(self):
        L, st = self.ledger()
        self.session(st, events=[ev("turn_start", "2026-11-24T09:59:00Z")],
                     display={"pane_dead": "0", "pane_in_mode": "1", "session_attached": "2", "pane_pid": "77"})
        res, readings = self.reconcile(L, st)
        f = res.sessions["driver"]
        self.assertEqual((f.status, f.turn_open, f.attached, f.pane_in_mode), (edl.ST_BUSY, True, True, True))
        self.assertEqual(readings.sessions[0].pane_pid, 77)


class TestStuck(_Recon):
    def busy(self, st, mtime_ago, **kw):
        return self.session(st, events=[ev("turn_start", "2026-11-24T09:00:00Z")],
                            mtime=self.now().timestamp() - mtime_ago, **kw)

    def test_stuck_after_no_growth_and_no_tool_shell(self):
        L, st = self.ledger()
        self.busy(st, 31 * 60)
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "session_stuck"), ["session_stuck:ws-t-driver-g1"])
        L, st = self.ledger()
        self.world.handles = []
        self.busy(st, 29 * 60)
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "session_stuck"), [])

    def test_a_long_tool_shell_is_not_stuck(self):
        L, st = self.ledger()
        self.busy(st, 2 * 3600)
        self.world.work[4242] = edl.ProcessWork(owner_pid=4243, tool_shell=True)
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "session_stuck"), [])
        self.assertIn(("process_work", 4242), self.world.calls)

    def test_subagent_growth_counts_as_activity(self):
        L, st = self.ledger()
        _, _, tp = self.busy(st, 2 * 3600)
        sub = Path(str(tp)[:-len(".jsonl")]) / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-1.jsonl").write_text("{}\n")
        fresh = self.now().timestamp() - 60
        os.utime(str(sub / "agent-1.jsonl"), (fresh, fresh))
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "session_stuck"), [])

    def test_suppressed_for_the_tick_of_a_clock_jump_and_cleared_when_the_turn_closes(self):
        L, st = self.ledger()
        self.busy(st, 2 * 3600)
        st["clock"] = {"boot_id": "boot-1", "wall": 100000.0 - 7800, "boottime": 5000.0 - 600,
                       "monotonic": 5000.0 - 600}             # wall moved 2 h against boottime's 10 min
        res, _ = self.reconcile(L, st)
        self.assertIn("NTP", res.clock_jump)
        self.assertEqual(self.keys(res, "session_stuck"), [])
        # the next tick (no jump) raises it
        self.world.sample = edl.ClockSample("boot-1", 100600.0, 5600.0, 5600.0)
        res2, _ = self.reconcile(L, res.state)
        self.assertIsNone(res2.clock_jump)
        self.assertEqual(self.keys(res2, "session_stuck"), ["session_stuck:ws-t-driver-g1"])
        # the turn closes: cleared
        self.world.events["ws-t-driver-g1"].append(ev("stop", "2026-11-24T09:59:00Z"))
        res3, _ = self.reconcile(L, res2.state)
        self.assertEqual(self.keys(res3, "session_stuck"), [])

    def test_an_already_raised_stuck_alarm_survives_a_jump(self):
        L, st = self.ledger()
        self.busy(st, 2 * 3600)
        st["alarms"]["session_stuck:ws-t-driver-g1"] = {"id": "session_stuck", "subject": "ws-t-driver-g1",
                                                       "raised_at": "2026-11-24T09:50:00Z", "cleared_at": None}
        st["clock"] = {"boot_id": "boot-1", "wall": 100000.0 - 7800, "boottime": 4400.0, "monotonic": 4400.0}
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "session_stuck"), ["session_stuck:ws-t-driver-g1"])


class TestUsageLimits(_Recon):
    YEREVAN = None

    def setUp(self):
        super().setUp()
        from zoneinfo import ZoneInfo
        self.yer = ZoneInfo("Asia/Yerevan")     # UTC+4, no DST
        self.ny = ZoneInfo("America/New_York")

    def reset(self, text, at, tz=None):
        return edl.parse_reset_time(text, T(at), tz or self.yer)

    def test_reset_times_in_the_system_timezone(self):
        at = "2026-11-24T08:00:00Z"                              # 12:00 in Yerevan
        self.assertEqual(self.reset("You've hit your session limit · resets 3pm", at), T("2026-11-24T11:00:00Z"))
        self.assertEqual(self.reset("resets 3:30pm", at), T("2026-11-24T11:30:00Z"))
        self.assertEqual(self.reset("resets at 15:00", at), T("2026-11-24T11:00:00Z"))
        self.assertEqual(self.reset("resets 3pm", at, self.ny), T("2026-11-24T20:00:00Z"))   # EST, UTC-5
        self.assertEqual(self.reset("Limit reached · resets 12am", at), T("2026-11-24T20:00:00Z"))

    def test_a_past_time_rolls_to_tomorrow(self):
        self.assertEqual(self.reset("resets 9am", "2026-11-24T08:00:00Z"), T("2026-11-25T05:00:00Z"))
        self.assertEqual(self.reset("resets at 12:00", "2026-11-24T08:00:00Z"), T("2026-11-25T08:00:00Z"))

    def test_named_zone_dated_and_month_forms(self):
        self.assertEqual(self.reset("resets 9:40pm (Asia/Yerevan)", "2026-11-24T08:00:00Z", self.ny),
                         T("2026-11-24T17:40:00Z"))
        self.assertEqual(self.reset("Your limit will reset at 2026-11-24 20:16:08", "2026-11-24T08:00:00Z"),
                         T("2026-11-24T16:16:08Z"))
        self.assertEqual(self.reset("You've hit your weekly limit · resets Nov 27, 5pm", "2026-11-24T08:00:00Z"),
                         T("2026-11-27T13:00:00Z"))

    def test_no_reset_time(self):
        for text in ("You've reached your Fable limit. Run /usage-credits to continue or switch models with /model.",
                     "API Error: Request rejected (429) · All target providers failed.", "resets 3 times", ""):
            with self.subTest(text=text):
                self.assertIsNone(self.reset(text, "2026-11-24T08:00:00Z"))

    def test_the_ladder(self):
        prev = None
        got = []
        for i in range(7):
            entry = {"timestamp": f"2026-11-24T0{i}:00:00Z", "text": "You've reached your Fable limit."}
            prev = edl.limit_record(entry, prev, self.yer)
            got.append((edl.from_iso(prev["until"]) - T(entry["timestamp"])).total_seconds())
            self.assertEqual(prev["source"], "ladder")
        self.assertEqual(got, [900, 1800, 3600, 7200, 14400, 14400, 14400])
        rec = edl.limit_record({"timestamp": "2026-11-24T08:00:00Z", "text": "resets 3pm"}, None, self.yer)
        self.assertEqual((rec["source"], rec["until"]), ("reset", "2026-11-24T11:00:00Z"))

    def test_classification_by_error_field_only(self):
        L, st = self.ledger()
        self.session(st, entries=[a("2026-11-24T09:59:00Z", "You've reached your Fable limit. resets 3pm")])
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "usage_limit"), [])
        self.assertEqual(res.state["limits"], {})
        L, st = self.ledger()
        self.world.handles = []
        self.session(st, entries=[a("2026-11-24T09:59:00Z", "resets 3pm", error="rate_limit")])  # not an API error
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["limits"], {})

    def test_limit_entry_raises_holds_dispatch_steps_the_ladder_and_clears(self):
        L, st = self.ledger()
        self.session(st, entries=[limit("2026-11-24T09:50:00Z")])
        res, _ = self.reconcile(L, st)
        lim = res.state["limits"]["claude-opus-5-5"]
        self.assertEqual((lim["until"], lim["source"], lim["ladder_step"]), ("2026-11-24T10:05:00Z", "ladder", 0))
        self.assertEqual(self.keys(res, "usage_limit"), ["usage_limit:claude-opus-5-5"])
        self.assertEqual(res.sessions["driver"].limited, "claude-opus-5-5")
        self.assertEqual(edl.model_limited(L, res.state, "driver", self.now()), "claude-opus-5-5")
        # the same entry next pass is not counted again
        res2, _ = self.reconcile(L, res.state)
        self.assertEqual(res2.state["limits"]["claude-opus-5-5"]["ladder_step"], 0)
        # after the reset a retry hits the limit again: one step up the ladder
        self.world.clock = T("2026-11-24T10:10:00Z")
        self.world.handles = []
        self.session(res2.state, entries=[limit("2026-11-24T09:50:00Z"), limit("2026-11-24T10:06:00Z")])
        res3, _ = self.reconcile(L, res2.state)
        lim3 = res3.state["limits"]["claude-opus-5-5"]
        self.assertEqual((lim3["ladder_step"], lim3["until"]), (1, "2026-11-24T10:36:00Z"))
        # past the reset with a completed turn on the model: cleared
        self.world.clock = T("2026-11-24T10:50:00Z")
        self.world.handles = []
        self.session(res3.state, entries=[limit("2026-11-24T10:06:00Z"), a("2026-11-24T10:40:00Z", "ADVANCED: A")],
                     events=[ev("turn_start", "2026-11-24T10:39:00Z"), ev("stop", "2026-11-24T10:40:00Z")])
        res4, _ = self.reconcile(L, res3.state)
        self.assertEqual(res4.state["limits"], {})
        self.assertEqual(self.keys(res4, "usage_limit"), [])

    def test_past_the_reset_without_a_completed_turn_stays_raised(self):
        L, st = self.ledger()
        st["limits"]["claude-opus-5-5"] = {"until": "2026-11-24T09:00:00Z", "source": "reset", "ladder_step": 0,
                                           "entry_at": "2026-11-24T08:00:00Z"}
        self.session(st, entries=[limit("2026-11-24T08:00:00Z")])
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "usage_limit"), ["usage_limit:claude-opus-5-5"])
        self.assertIsNone(res.sessions["driver"].limited)        # dispatch is no longer held

    def test_a_limited_open_turn_is_not_stuck(self):
        L, st = self.ledger()
        self.session(st, entries=[limit("2026-11-24T09:00:30Z")], events=[ev("turn_start", "2026-11-24T09:00:00Z")],
                     mtime=self.now().timestamp() - 3600)
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "session_stuck"), [])


class TestModelCheck(_Recon):
    def test_alias_through_the_profile_mapping(self):
        L, st = self.ledger()
        self.session(st, model="fable", profile="claude",
                     entries=[limit("2026-11-24T09:00:10Z"), a("2026-11-24T09:01:00Z", model="claude-opus-5-5")])
        res, readings = self.reconcile(L, st)
        self.assertEqual(readings.sessions[0].expected_model_id, "claude-opus-5-5")
        self.assertTrue(res.state["sessions"]["driver"]["model_verified"])
        self.assertEqual(self.keys(res, "model_mismatch"), [])

    def test_mismatch_raises_and_holds_until_rotated(self):
        L, st = self.ledger()
        self.session(st, entries=[a("2026-11-24T08:00:00Z", model="claude-opus-5-5"),      # before the launch
                                  a("2026-11-24T09:01:00Z", model="claude-sonnet-4-6")])
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "model_mismatch"), ["model_mismatch:ws-t-driver-g1"])
        self.assertFalse(res.state["sessions"]["driver"]["model_verified"])
        res2, _ = self.reconcile(L, res.state)
        self.assertEqual(self.keys(res2, "model_mismatch"), ["model_mismatch:ws-t-driver-g1"])
        # rotated: the record names gen 2, which replies on the right model
        st3 = res2.state
        self.world.handles = []
        self.session(st3, name="ws-t-driver-g2", spawned_at="2026-11-24T09:30:00Z",
                     entries=[a("2026-11-24T09:31:00Z", model="claude-opus-5-5")])
        res3, _ = self.reconcile(L, st3)
        self.assertEqual(self.keys(res3, "model_mismatch"), [])

    def test_alias_without_a_mapping_matches_the_family(self):
        L, st = self.ledger()
        self.session(st, model="opus", entries=[a("2026-11-24T09:01:00Z", model="claude-opus-5-5")])
        res, _ = self.reconcile(L, st)
        self.assertTrue(res.state["sessions"]["driver"]["model_verified"])

    def test_legacy_handle_without_a_model_is_skipped(self):
        L, st = self.ledger()
        self.session(st, model=None, entries=[a("2026-11-24T09:01:00Z", model="claude-sonnet-4-6")])
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "model_mismatch"), [])
        self.assertIsNone(res.state["sessions"]["driver"]["model_verified"])
        self.assertNotIn("model_check", res.state["sessions"]["driver"])

    def test_a_resume_is_checked_again(self):
        L, st = self.ledger()
        self.session(st, resumed_at=["2026-11-24T09:30:00Z"],
                     entries=[a("2026-11-24T09:01:00Z", model="claude-opus-5-5"),
                              a("2026-11-24T09:31:00Z", model="claude-sonnet-4-6")])
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["sessions"]["driver"]["model_check"]["launch"], "2026-11-24T09:30:00Z")
        self.assertEqual(self.keys(res, "model_mismatch"), ["model_mismatch:ws-t-driver-g1"])


class TestCompaction(_Recon):
    def test_after_the_spawn_raises_once_before_is_ignored(self):
        L, st = self.ledger()
        self.session(st, entries=[compact("2026-11-24T08:00:00Z")])
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "compaction"), [])
        self.assertFalse(res.state["sessions"]["driver"]["compacted"])
        L, st = self.ledger()
        self.world.handles = []
        self.session(st, entries=[compact("2026-11-24T09:20:00Z"), compact("2026-11-24T09:40:00Z")])
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "compaction"), ["compaction:ws-t-driver-g1"])
        self.assertEqual(res.state["sessions"]["driver"]["compacted_at"], "2026-11-24T09:20:00Z")
        self.assertTrue(res.sessions["driver"].compacted)
        res2, _ = self.reconcile(L, res.state)
        self.assertEqual(self.keys(res2, "compaction"), ["compaction:ws-t-driver-g1"])   # one condition, still active
        # the rotation completes: gen 2, not compacted
        st3 = res2.state
        self.world.handles = []
        self.session(st3, name="ws-t-driver-g2", spawned_at="2026-11-24T09:50:00Z")
        res3, _ = self.reconcile(L, st3)
        self.assertEqual(self.keys(res3, "compaction"), [])


class TestOpenPrompts(_Recon):
    def test_ask_user_question_in_flight(self):
        for minutes, raised in ((11, True), (9, False)):
            with self.subTest(minutes=minutes):
                L, st = self.ledger()
                self.world.handles = []
                at = edl.iso(self.now() - timedelta(minutes=minutes))
                self.session(st, entries=[u("2026-11-24T09:00:01Z", "EVENT E-1: read /x"),
                                          tool(at, "AskUserQuestion", "tu_1", questions=[])],
                             events=[ev("turn_start", "2026-11-24T09:00:00Z")])
                res, _ = self.reconcile(L, st)
                self.assertEqual(self.keys(res, "ask_user_question_open"),
                                 ["ask_user_question_open:ws-t-driver-g1"] if raised else [])
                self.assertEqual(self.keys(res, "session_stuck"), [])

    def test_answered_ask_user_question_clears(self):
        L, st = self.ledger()
        at = edl.iso(self.now() - timedelta(minutes=30))
        result = {"type": "user", "timestamp": edl.iso(self.now() - timedelta(minutes=1)),
                  "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "tu_1",
                                                           "content": "answered"}]}}
        self.session(st, entries=[u("2026-11-24T09:00:01Z", "EVENT E-1: read /x"),
                                  tool(at, "AskUserQuestion", "tu_1", questions=[]), result],
                     events=[ev("turn_start", "2026-11-24T09:00:00Z")])
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "ask_user_question_open"), [])

    def test_permission_prompt(self):
        for minutes, raised in ((11, True), (9, False)):
            with self.subTest(minutes=minutes):
                L, st = self.ledger()
                self.world.handles = []
                at = edl.iso(self.now() - timedelta(minutes=minutes))
                self.session(st, events=[ev("turn_start", "2026-11-24T09:00:00Z"),
                                         ev("permission_prompt", at, permission_pending=True)],
                             mtime=self.now().timestamp() - 3600)
                res, _ = self.reconcile(L, st)
                self.assertEqual(self.keys(res, "permission_open"),
                                 ["permission_open:ws-t-driver-g1"] if raised else [])
                self.assertEqual(self.keys(res, "session_stuck"), [])

    def test_closed_prompt_clears(self):
        L, st = self.ledger()
        self.session(st, events=[ev("turn_start", "2026-11-24T09:00:00Z"),
                                 ev("permission_prompt", "2026-11-24T09:10:00Z", permission_pending=True),
                                 ev("stop", "2026-11-24T09:20:00Z")])
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "permission_open"), [])


class TestClock(_Recon):
    def test_first_pass_records_the_clock(self):
        L, st = self.ledger()
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.state["clock"], self.world.sample.as_state())
        self.assertIsNone(res.clock_jump)
        self.assertIsNone(res.reboot)

    def test_suspend_is_a_jump(self):
        L, st = self.ledger()
        st["clock"] = {"boot_id": "boot-1", "wall": 100000.0 - 4200, "boottime": 5000.0 - 4200,
                       "monotonic": 5000.0 - 600}
        res, _ = self.reconcile(L, st)
        self.assertIn("suspended", res.clock_jump)

    def test_boot_id_change_gives_the_reboot_facts(self):
        L, st = self.ledger([self.driver("A"), self.driver("B")])
        st["clock"] = {"boot_id": "boot-0", "wall": 1.0, "boottime": 1.0, "monotonic": 1.0}
        # driver: a turn was open when the machine went down; a worker with a step in flight; one idle
        self.session(st, alive=False, events=[ev("turn_start", "2026-11-24T08:00:00Z")])
        self.session(st, key="impl:A", name="t-a-t-impl-a-g1", alive=False,
                     events=[ev("turn_start", "2026-11-24T07:00:00Z"), ev("stop", "2026-11-24T07:10:00Z")])
        self.inflight(st, "A", name="t-a-t-impl-a-g1")
        self.session(st, key="rev:B", name="t-b-t-rev-b-g1", alive=False,
                     events=[ev("turn_start", "2026-11-24T07:00:00Z"), ev("stop", "2026-11-24T07:10:00Z")])
        res, _ = self.reconcile(L, st)
        self.assertEqual(sorted(res.reboot.resume_now), ["driver", "impl:A"])
        self.assertEqual(res.reboot.resume_lazily, ["rev:B"])
        self.assertEqual(res.state["clock"]["boot_id"], "boot-1")


class TestForeignAndOrphans(_Recon):
    def test_foreign_handle_raises_and_clears_when_gone(self):
        L, st = self.ledger()
        self.session(st)
        self.session(st, key=None, name="someone-else-g1")                  # carries run id epic-t
        self.session(st, key=None, name="unrelated", run_id="other-run")
        res, readings = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "foreign_handle"), ["foreign_handle:someone-else-g1"])
        self.assertEqual([r.kind for r in readings.sessions], ["record", "foreign"])
        self.assertEqual(set(res.state["sessions"]), {"driver"})          # never adopted
        self.world.handles = [h for h in self.world.handles if h["name"] != "someone-else-g1"]
        res2, _ = self.reconcile(L, res.state)
        self.assertEqual(self.keys(res2, "foreign_handle"), [])

    def test_parked_sessions_are_not_foreign(self):
        L, st = self.ledger()
        self.session(st, name="ws-t-driver-g2", parked=[{"name": "ws-t-driver-g1", "since": "2026-11-24T09:00:00Z"}])
        self.session(st, key=None, name="ws-t-driver-g1")
        res, readings = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "foreign_handle"), [])
        self.assertEqual([r.kind for r in readings.sessions], ["record", "parked"])

    def test_orphan_session_by_the_grammar(self):
        L, st = self.worker_ledger()
        self.session(st)
        self.world.amux = ["ws-t-driver-g1", "ws-t-driver-g3", "t-w-t-impl-w-g2", "t-w-t-rev-w-g1",
                           "ws-t-driver-g1-old", "ws-other-g1", "t-w-t-driver-w-g1"]
        self.world.handles.append({"name": "t-w-t-rev-w-g1", "run_id": "epic-t"})
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "orphan_session"),
                         ["orphan_session:t-w-t-impl-w-g2", "orphan_session:ws-t-driver-g3"])
        self.assertEqual(set(res.state["sessions"]), {"driver"})
        self.world.amux = ["ws-t-driver-g1"]
        res2, _ = self.reconcile(L, res.state)
        self.assertEqual(self.keys(res2, "orphan_session"), [])

    def test_name_patterns(self):
        L, _ = self.worker_ledger()
        names = [p.pattern for p in edl.run_name_patterns(L)]
        self.assertEqual(len(names), 3)
        self.assertTrue(any(re.match(p, "ws-t-driver-g12") for p in names))


class TestStartupDialogs(_Recon):
    def launched(self, st, pane_text, *, launch_event="E-1", confirmed=False, **kw):
        self.outbox(st, launch_event, confirmed_at="2026-11-24T09:00:05Z" if confirmed else None)
        return self.session(st, status="spawning", pane=pane_text, launch_event=launch_event, **kw)

    def test_unlisted_dialog_raises_and_is_never_answered(self):
        L, st = self.ledger()
        self.launched(st, pane("cc-2.1.330-folder-trust.txt"))
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "startup_dialog"), ["startup_dialog:ws-t-driver-g1"])
        self.assertIn("Accessing workspace:", res.conditions[0].detail)
        self.assertEqual(res.actions, [])
        self.assertEqual(res.sessions["driver"].pane, edl.PANE_DIALOG)

    def test_listed_dialog_answered_once_then_the_alarm(self):
        L, st = self.ledger(startup_dialogs=[{"id": "code-mode", "match": CODE_MODE_MATCH, "key": "Enter"}])
        self.launched(st, pane("synth-code-mode-optin.txt"))
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "startup_dialog"), [])
        self.assertEqual([(x.kind, x.session, x.data["dialog"], x.data["key"], x.data["launch"]) for x in res.actions],
                         [("startup_dialog_answer", "driver", "code-mode", "Enter", "2026-11-24T09:00:00Z")])
        # 50-06 records the answer before it sends the key; the dialog is still there next pass
        st2 = res.state
        rec = st2["sessions"]["driver"]
        edl.record_dialog_answer(rec, edl.session_launch_at(rec), "code-mode", "Enter", self.now())
        res2, _ = self.reconcile(L, st2)
        self.assertEqual(res2.actions, [])
        self.assertEqual(self.keys(res2, "startup_dialog"), ["startup_dialog:ws-t-driver-g1"])
        self.assertIn("still showing after its listed key", res2.conditions[0].detail)
        # the dialog is gone: cleared
        self.world.tmux["ws-t-driver-g1"]["pane"] = pane("cc-2.1.330-startup-no-dialog.txt")
        res3, _ = self.reconcile(L, res2.state)
        self.assertEqual(self.keys(res3, "startup_dialog"), [])

    def test_a_listed_dialog_is_answered_again_after_a_resume(self):
        L, st = self.ledger(startup_dialogs=[{"id": "code-mode", "match": CODE_MODE_MATCH, "key": "Enter"}])
        rec, _, _ = self.launched(st, pane("synth-code-mode-optin.txt"))
        edl.record_dialog_answer(rec, edl.session_launch_at(rec), "code-mode", "Enter", self.now())
        rec["resumed_at"] = ["2026-11-24T09:50:00Z"]
        res, _ = self.reconcile(L, st)
        self.assertEqual([x.data["launch"] for x in res.actions], ["2026-11-24T09:50:00Z"])

    def test_a_confirmed_prompt_classifies_no_capture(self):
        L, st = self.ledger()
        self.launched(st, pane("cc-2.1.330-folder-trust.txt"), confirmed=True)
        res, readings = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "startup_dialog"), [])
        self.assertNotIn(("capture", "ws-t-driver-g1"), self.world.calls)
        self.assertIsNone(res.sessions["driver"].pane)

    def test_input_box_and_unreadable_are_not_dialogs(self):
        for name, kind in (("cc-2.1.330-startup-no-dialog.txt", edl.PANE_NO_DIALOG),
                           ("synth-input-box-typed.txt", edl.PANE_NO_DIALOG),
                           ("synth-blank.txt", edl.PANE_UNREADABLE), (None, edl.PANE_UNREADABLE)):
            with self.subTest(name=name):
                L, st = self.ledger()
                self.world.handles = []
                self.launched(st, pane(name) if name else None)
                res, _ = self.reconcile(L, st)
                self.assertEqual(self.keys(res, "startup_dialog"), [])
                self.assertEqual(res.sessions["driver"].pane, kind)

    def test_an_unreadable_pane_keeps_a_raised_alarm(self):
        L, st = self.ledger()
        self.launched(st, pane("synth-blank.txt"))
        st["alarms"]["startup_dialog:ws-t-driver-g1"] = {"id": "startup_dialog", "subject": "ws-t-driver-g1",
                                                        "raised_at": "2026-11-24T09:50:00Z", "cleared_at": None}
        res, _ = self.reconcile(L, st)
        self.assertEqual(self.keys(res, "startup_dialog"), ["startup_dialog:ws-t-driver-g1"])

    def test_no_default_answers(self):
        L, st = self.ledger()
        self.assertEqual(L.startup_dialogs, [])
        self.launched(st, pane("synth-code-mode-optin.txt"))
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.actions, [])
        self.assertEqual(self.keys(res, "startup_dialog"), ["startup_dialog:ws-t-driver-g1"])

    def test_record_dialog_answer_refuses_other_keys(self):
        with self.assertRaises(ValueError):
            edl.record_dialog_answer({}, None, "x", "y", self.now())


class TestQuestionReconcile(_Recon):
    def snapshot(self, **statuses):
        return edl.QuestionsSnapshot(
            open_word="open", resolved_words=("resolved", "answered"), withdrawn_word="withdrawn",
            entries={q.replace("_", "-"): edl.QuestionEntry(q.replace("_", "-"), s, text=f"## {q} answer: yes",
                                                            path="/q/for-anton.md")
                     for q, s in statuses.items()})

    def state_with(self, st):
        st["questions"] = {
            "Q-17": {"step": None, "approval": "SA4", "asked_event": "E-3", "status": "open", "answer_event": None},
            "Q-18": {"step": "A", "approval": None, "asked_event": "E-4", "status": "open", "answer_event": None},
            "Q-19": {"step": "A", "approval": None, "asked_event": "E-5", "status": "open", "answer_event": None},
        }
        st["approval_question"] = {"id": "SA4", "question": "Q-17"}

    def test_resolved_once_withdrawn_closed_open_nothing(self):
        L, st = self.ledger(approvals={"SA4": {"conditions": []}})
        self.state_with(st)
        self.world.queue = self.snapshot(Q_17="resolved", Q_18="withdrawn", Q_19="open")
        res, _ = self.reconcile(L, st)
        self.assertEqual([(i.kind, i.to, i.data["question"], i.data["approval"]) for i in res.items],
                         [("answer", "driver", "Q-17", "SA4")])
        self.assertEqual(res.items[0].data["answer"], "## Q_17 answer: yes")
        qs = res.state["questions"]
        self.assertEqual((qs["Q-17"]["status"], qs["Q-18"]["status"], qs["Q-19"]["status"]),
                         ("resolved", "withdrawn", "open"))
        self.assertIsNone(res.state["approval_question"])
        res2, _ = self.reconcile(L, res.state)
        self.assertEqual(res2.items, [])

    def test_withdrawn_frees_the_approval_question(self):
        L, st = self.ledger(approvals={"SA4": {"conditions": []}})
        self.state_with(st)
        self.world.queue = self.snapshot(Q_17="withdrawn")
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.items, [])
        self.assertIsNone(res.state["approval_question"])
        self.assertEqual(res.state["questions"]["Q-17"]["status"], "withdrawn")

    def test_no_store_and_no_open_question(self):
        L, st = self.ledger()
        res, _ = self.reconcile(L, st)
        self.assertNotIn(("questions", str(self.ws)), self.world.calls)     # nothing open: not read
        self.state_with(st)
        self.world.queue = None
        res, _ = self.reconcile(L, st)
        self.assertEqual(res.items, [])
        self.assertEqual(res.state["questions"]["Q-17"]["status"], "open")

    def test_snapshot_vocabulary(self):
        snap = self.snapshot(Q_1="ANSWERED".lower(), Q_2=None)
        self.assertEqual(snap.classify("Q-1"), "resolved")
        self.assertEqual(snap.classify("Q-2"), "open")
        self.assertEqual(snap.classify("Q-9"), "missing")

    def test_real_store_reading(self):
        ws = self.tmp / "qws"
        (ws / "docs" / "questions").mkdir(parents=True)
        (ws / ".claude").mkdir()
        (ws / ".claude" / "roles.toml").write_text('[questions]\ndir = "docs/questions"\n')
        (ws / "docs" / "questions" / "questions.md").write_text(
            "# Questions\n\n## Q-001 — Grant SA4?  [resolved]\n\nYes, go.\n\n"
            "## Q-002 — Other?  [withdrawn]\n\nx\n\n## Q-003 — Third?  [open]\n")
        other = self.tmp / "elsewhere"
        (other / ".claude").mkdir(parents=True)
        with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(other)}):
            snap = edl.RealWorld().question_entries(str(ws))
        self.assertIsNotNone(snap)
        self.assertEqual(snap.classify("Q-001"), "resolved")
        self.assertEqual(snap.classify("Q-002"), "withdrawn")
        self.assertEqual(snap.classify("Q-003"), "open")
        self.assertIn("Yes, go.", snap.entries["Q-001"].text)


class TestReconcileContract(_Recon):
    def test_pure_over_readings_ledger_state_and_now(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "ADVANCED: A")
        self.session(st, entries=entries + [limit("2026-11-24T09:06:00Z")], events=events,
                     pane=pane("cc-2.1.330-folder-trust.txt"))
        self.outbox(st, "E-1")
        readings = edl.take_readings(L, st, self.world, self.now())
        before = copy.deepcopy(st)
        r1 = edl.reconcile(L, st, readings, self.now())
        r2 = edl.reconcile(L, st, readings, self.now())
        self.assertEqual(st, before)                       # the input state is untouched
        self.assertEqual(r1.state, r2.state)
        self.assertEqual(r1.conditions, r2.conditions)
        self.assertEqual((r1.items, r1.actions), (r2.items, r2.actions))
        # reconcile itself touches no World: the readings are all it needs
        r3 = edl.reconcile(L, st, readings, self.now())
        self.assertEqual(r3.state, r1.state)

    def test_readings_only_read(self):
        L, st = self.ledger()
        self.session(st, pane=pane("cc-2.1.330-idle-input-box.txt"))
        self.outbox(st, "E-1")
        edl.take_readings(L, st, self.world, self.now())
        verbs = {c[0] for c in self.world.calls}
        self.assertLessEqual(verbs, {"has_session", "display", "capture", "process_work", "questions"})

    def test_world_read_methods_fail_loudly_on_the_base_class(self):
        w = edl.World()
        for call in (lambda: w.tmux_has_session("x"), lambda: w.tmux_display("x", ["pane_dead"]),
                     lambda: w.capture_pane("x"), lambda: w.list_handles(), lambda: w.lifecycle_events("x"),
                     lambda: w.amux_names(), lambda: w.process_work(1), lambda: w.clock_sample(),
                     lambda: w.local_timezone(), lambda: w.question_entries("/x")):
            with self.assertRaises(NotImplementedError):
                call()

    def test_real_world_reads_without_a_tmux_session(self):
        rw = edl.RealWorld()
        sample = rw.clock_sample()
        self.assertGreater(sample.boottime, 0)
        self.assertIsNotNone(rw.local_timezone())
        work = rw.process_work(os.getpid())
        self.assertIsInstance(work, edl.ProcessWork)
        with mock.patch.object(edl.subprocess, "run", side_effect=OSError("no tmux")):
            self.assertIsNone(rw.tmux_display("nope", edl.TMUX_DISPLAY_FIELDS))
            self.assertIsNone(rw.capture_pane("nope"))
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="0\t1\t2\t4242\n", stderr="")
        with mock.patch.object(edl.subprocess, "run", side_effect=fake_run):
            self.assertEqual(rw.tmux_display("n", edl.TMUX_DISPLAY_FIELDS),
                             {"pane_dead": "0", "pane_in_mode": "1", "session_attached": "2", "pane_pid": "4242"})
        self.assertEqual(calls[0][:4], ["tmux", "display-message", "-p", "-t"])
        self.assertEqual(calls[0][4], "=amux-n:")

    def test_lifecycle_log_reader_skips_a_partial_tail(self):
        spawn = self.tmp / "spawn"
        spawn.mkdir()
        (spawn / "n.lifecycle.jsonl").write_text(json.dumps(ev("turn_start", "2026-11-24T09:00:00Z")) + "\n{\"ev")
        with mock.patch.object(edl.asl, "SPAWN_DIR", spawn):
            got = edl.RealWorld().lifecycle_events("n")
        self.assertEqual([e["event"] for e in got], ["turn_start"])


# =============================================================================
# 50-06 — dispatch: session policy (D4) and delivery (D5) (§9, §11, §12.11, §23)
# =============================================================================
#
# Pane fixtures added by 50-06 (Claude Code 2.1.330, real captures): a throwaway Claude
# Code in its own tmux server (`tmux -L`), a fresh /tmp directory, hooks disabled with
# `--settings '{"disableAllHooks": true}'`, text typed or pasted with send-keys -l /
# paste-buffer -p and never submitted; the server was killed afterwards. Scrubbed: the
# temp path → /tmp/epic-driver-fixture, the account tier, the login notice.
#   cc-2.1.330-input-typed.txt              one line typed
#   cc-2.1.330-input-multiline-paste.txt    a two-line draft (bracketed paste)
#   cc-2.1.330-input-pasted-placeholder.txt a 40-line paste: "[Pasted text #2 +40 lines]"
#   cc-2.1.330-input-event-line.txt         an EVENT line sitting in the box, not submitted
#   cc-2.1.330-input-wrapped-40col.txt      the same line wrapped by the TUI at 40 columns
#   cc-2.1.330-input-empty-after-clear.txt  the box emptied again (a "ctrl+y" hint above it)
#   cc-2.1.330-copy-mode-empty-box.txt      captured while #{pane_in_mode} was 1
#   cc-2.1.330-shell-mode.txt               "! " (shell mode): no prompt glyph

class Killed(Exception):
    """The test's stand-in for the tick being killed."""


def usage_entry(ts, ctx=100000, bucket="1h", text="ok"):
    """An assistant entry with a usage record: context ``ctx`` tokens, cache bucket 1h or 5m."""
    creation = {"ephemeral_1h_input_tokens": 1000} if bucket == "1h" else {"ephemeral_5m_input_tokens": 1000}
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": f"m-{ts}", "role": "assistant", "model": "claude-opus-5-5",
                        "content": [{"type": "text", "text": text}],
                        "usage": {"input_tokens": 10, "cache_creation_input_tokens": 1000,
                                  "cache_read_input_tokens": ctx - 1010, "output_tokens": 50,
                                  "cache_creation": creation}}}


IDLE_DISPLAY = {"pane_dead": "0", "pane_in_mode": "0", "session_attached": "0", "pane_pid": "4242"}


def typed_pane(text):
    """The real emptied box with ``text`` typed after the glyph."""
    empty = pane("cc-2.1.330-input-empty-after-clear.txt")
    out = re.sub(r"(?m)^❯\s*$", lambda m: f"❯\xa0{text}", empty, count=1)
    assert out != empty
    return out


class DispatchWorld(ReconWorld):
    """ReconWorld plus the dispatch effects, which change the fake world as amux would.

    A spawn creates the tmux session, the handle and the transcript; a spawn, a
    resume or a send "lands" by default: the line becomes a user prompt and a
    ``turn_start`` 1 s later, and a turn that ends with ``reply[name]`` 30 s
    later (None: the turn stays open). ``mode[name]``: ``lost`` (nothing lands)
    or ``box`` (the line stays in the input box).
    """

    def __init__(self, tdir, **kw):
        super().__init__(**kw)
        self.tdir = tdir
        self.spawns, self.created, self.resumes, self.sends, self.keys, self.reaps = [], [], [], [], [], []
        self.landed = []
        self.mode = {}
        self.reply = {}
        self.spawn_error = None
        self.resume_error = None
        self.key_error = None

    def _stamp(self, seconds):
        return edl.iso(self.clock + timedelta(seconds=seconds))

    def _handle(self, name):
        return next((h for h in self.handles if h.get("name") == name), None)

    def _append(self, name, *entries):
        tp = self.tdir / f"{name}.jsonl"
        with open(tp, "a", encoding="utf-8") as f:
            for e in entries:
                f.write(json.dumps(e) + "\n")

    def _turn(self, name, line):
        mode = self.mode.get(name, "land")
        if mode == "lost":
            return
        if mode == "box":
            self.tmux[name]["pane"] = typed_pane(line)
            return
        self.landed.append(line)
        t1 = self._stamp(1)
        self._append(name, u(t1.replace("Z", ".200Z"), line))
        self.events.setdefault(name, []).append(ev("turn_start", t1))
        reply = self.reply.get(name)
        if reply is not None:
            t2 = self._stamp(30)
            self._append(name, a(t2, reply))
            self.events[name].append(ev("stop", t2, state="idle", last_message=reply))
            h = self._handle(name)
            if h is not None:
                h.update(last_message=reply, stopped_at=t2, state="idle")

    def spawn(self, args):
        args = list(args)
        self.spawns.append(args)
        directory = args[args.index("--dir") + 1]
        name = f"{os.path.basename(directory.rstrip('/'))}-{args[1]}"
        if self.spawn_error:
            return edl.CommandResult(rc=1, stderr=self.spawn_error)
        if self.tmux.get(name, {}).get("present") or self._handle(name) or name in self.amux:
            return edl.CommandResult(rc=1, stderr=f"amux-spawn: error: session name already in use: {name}\n")
        self.created.append(name)
        tp = self.tdir / f"{name}.jsonl"
        tp.write_text(json.dumps(u(self._stamp(0), "(session start)")) + "\n")
        model = next(x.split("=", 1)[1] for x in args if x.startswith("--model="))
        profile = args[args.index("--profile") + 1] if "--profile" in args else None
        self.handles.append({"name": name, "provider": "claude", "run_id": args[args.index("--run-id") + 1],
                             "transcript_path": str(tp), "state": "spawning", "model": model, "profile": profile,
                             "created_at": self._stamp(0), "last_message": None, "stopped_at": None, "attempt": 1})
        self.tmux[name] = {"present": True, "display": dict(IDLE_DISPLAY),
                           "pane": pane("cc-2.1.330-startup-no-dialog.txt")}
        self.events[name] = []
        self.amux.append(name)
        self._turn(name, args[-1])
        return edl.CommandResult(rc=0, stdout=f"amux-spawn: spawned tracked session '{name}'\n")

    def resume(self, args):
        args = list(args)
        self.resumes.append(args)
        name = args[1]
        if self.resume_error:
            return edl.CommandResult(rc=1, stderr=self.resume_error)
        t = self.tmux.get(name, {})
        if t.get("present") and (t.get("display") or {}).get("pane_dead") != "1":
            return edl.CommandResult(rc=1, stderr=f"amux-spawn: error: session '{name}' is live\n")
        self.tmux[name] = {"present": True, "display": dict(IDLE_DISPLAY),
                           "pane": pane("cc-2.1.330-input-empty-after-clear.txt")}
        h = self._handle(name)
        if h is not None:
            h.update(state="spawning", attempt=int(h.get("attempt") or 1) + 1)
        self._turn(name, args[-1])
        return edl.CommandResult(rc=0, stdout=json.dumps({"name": name, "state": "spawning"}) + "\n")

    def reap(self, args):
        name = list(args)[1]
        self.reaps.append(name)
        self.tmux[name] = {"present": False}
        self.handles = [h for h in self.handles if h.get("name") != name]
        self.amux = [n for n in self.amux if n != name]
        return edl.CommandResult(rc=0, stdout=f"amux-spawn rm: {name} (idle) removed\n")

    def amux_send(self, name, line):
        self.sends.append((name, line))
        if not self.tmux.get(name, {}).get("present"):
            return edl.CommandResult(rc=1, stderr=f"error: session '{name}' is not running")
        self._turn(name, line)
        return edl.CommandResult(rc=0, stdout=f"sent to {name}: {line}\n")

    def send_keys(self, name, key):
        self.keys.append((name, key))
        if self.key_error:
            return edl.CommandResult(rc=1, stderr=self.key_error)      # ran, but failed: the key may have landed
        self.tmux[name]["pane"] = pane("cc-2.1.330-startup-no-dialog.txt")    # the dialog is answered
        return edl.CommandResult(rc=0)


class _Disp(_Recon):
    NOW = "2026-11-24T10:00:00Z"

    def setUp(self):
        super().setUp()
        self.world = DispatchWorld(self.tdir, now=T(self.NOW), profiles={
            "claude": {"ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-opus-5-5"}})
        self.paths = edl.run_paths("epic-t")

    def live(self, st, key="driver", name="ws-t-driver-g1", *, entries=None, events=None, **kw):
        """A live idle session that answered an earlier turn, with an empty input box on its pane."""
        if entries is None:
            entries = [u("2026-11-24T09:00:01Z", "EVENT E-0: read /x"), usage_entry("2026-11-24T09:55:00Z")]
        if events is None:
            events = [ev("turn_start", "2026-11-24T09:00:01Z"), ev("stop", "2026-11-24T09:55:00Z", state="idle")]
        kw.setdefault("pane", pane("cc-2.1.330-input-empty-after-clear.txt"))
        return self.session(st, key=key, name=name, entries=entries, events=events, **kw)

    def tick(self, L, st, *, items=(), crash_at=None, crash_mode="after", worker_dirs=None, paused=False,
             advance=None):
        """One pass: readings → reconcile → schedule → plan → execute; ``crash_at`` kills it at a commit.

        Returns ``(persisted state, plan, recon, commits)``; the persisted state is
        what a killed tick leaves on disk (the last commit, else the input).
        """
        if advance:
            self.world.advance(advance)
        w = self.world
        now = w.now()
        readings = edl.take_readings(L, st, w, now)
        res = edl.reconcile(L, st, readings, now)
        s = res.state
        edl.advance_schedule(L, s, now)
        sel = edl.select(L, s, now, paused=paused)
        plan = edl.plan_dispatch(L, s, res, readings, now, paths=self.paths, selection=sel, items=items,
                                 host="ws", paused=paused, worker_dirs=worker_dirs)
        commits = []

        def commit(x):
            n = len(commits) + 1
            if crash_at == n and crash_mode == "before":
                raise Killed()
            commits.append(copy.deepcopy(x))
            if crash_at == n and crash_mode == "after":
                raise Killed()
        try:
            edl.execute_dispatch(plan, s, w, commit)
            persisted = copy.deepcopy(s)
        except Killed:
            persisted = copy.deepcopy(commits[-1]) if commits else copy.deepcopy(st)
        return persisted, plan, res, commits

    def entry(self, st, event):
        return next((e for e in st["outbox"] if e["event"] == event), None)

    def conds(self, L, st, alarm_id=None):
        return sorted(c.key for c in edl.dispatch_conditions(L, st, self.world.now())
                      if alarm_id is None or c.id == alarm_id)


# -- the input box parser (§9.3, M6) ------------------------------------------------

class TestInputBox(unittest.TestCase):
    def test_fixtures(self):
        expect = {
            "cc-2.1.330-idle-input-box.txt": (edl.BOX_EMPTY, ""),
            "cc-2.1.330-startup-no-dialog.txt": (edl.BOX_EMPTY, ""),
            "cc-2.1.330-input-empty-after-clear.txt": (edl.BOX_EMPTY, ""),
            "cc-2.1.330-copy-mode-empty-box.txt": (edl.BOX_EMPTY, ""),
            "cc-2.1.330-input-typed.txt": (edl.BOX_TEXT, "please also check the deploy log before you continue"),
            "cc-2.1.330-input-multiline-paste.txt": (edl.BOX_TEXT, "first line of a draft\nsecond line of the same draft"),
            "cc-2.1.330-input-pasted-placeholder.txt": (edl.BOX_TEXT, "[Pasted text #2 +40 lines]"),
            "cc-2.1.330-input-event-line.txt": (edl.BOX_TEXT, "EVENT E-41: read /tmp/epic-driver-fixture/events/E-41.md"),
            "cc-2.1.330-input-wrapped-40col.txt": (edl.BOX_TEXT, "EVENT E-41: read /tmp/epic-driver-fi\nxture/events/E-41.md"),
            "synth-input-box-typed.txt": (edl.BOX_TEXT, "please also check the deploy log before you continue"),
            "synth-input-box-multiline.txt": (edl.BOX_TEXT, "first line of a draft\nsecond line of the same draft"),
            "cc-2.1.330-folder-trust.txt": (edl.BOX_DIALOG, ""),
            "cc-2.1.330-folder-trust-wide.txt": (edl.BOX_DIALOG, ""),
            "synth-code-mode-optin.txt": (edl.BOX_DIALOG, ""),
            "cc-2.1.330-shell-mode.txt": (edl.BOX_UNREADABLE, ""),
            "synth-booting-splash.txt": (edl.BOX_UNREADABLE, ""),
            "synth-blank.txt": (edl.BOX_UNREADABLE, ""),
            "synth-conversation-quote.txt": (edl.BOX_EMPTY, ""),
            "synth-conversation-quote-pointer.txt": (edl.BOX_EMPTY, ""),
        }
        self.assertEqual(set(expect), {p.name for p in PANES.glob("*.txt")})
        for name, (state, text) in expect.items():
            with self.subTest(name=name):
                box = edl.parse_input_box(pane(name))
                self.assertEqual((box.state, box.text), (state, text))
        self.assertEqual(edl.parse_input_box(None).state, edl.BOX_UNREADABLE)

    def test_the_empty_box_is_the_glyph_and_whitespace_only(self):
        empty = pane("cc-2.1.330-idle-input-box.txt")
        self.assertIn("❯\xa0\n", empty)                       # the real empty row: glyph + no-break space
        self.assertTrue(edl.parse_input_box(empty).empty)
        self.assertTrue(edl.parse_input_box(empty.replace("❯", ">")).empty)
        for typed in ("x", ".", "/", "!", "  a"):
            with self.subTest(typed=typed):
                self.assertEqual(edl.parse_input_box(empty.replace("❯\xa0", "❯\xa0" + typed, 1)).state,
                                 edl.BOX_TEXT)

    def test_only_the_bottom_most_box_counts(self):
        rule = "─" * 40
        text_above = f"{rule}\n❯ an old draft quoted in the output\n{rule}\nmore output\n{rule}\n❯ \n{rule}\n  status\n"
        self.assertTrue(edl.parse_input_box(text_above).empty)
        empty_above = f"{rule}\n❯ \n{rule}\nmore output\n{rule}\n❯ typed\n{rule}\n  status\n"
        self.assertEqual(edl.parse_input_box(empty_above).state, edl.BOX_TEXT)

    def test_a_box_far_above_the_bottom_is_unreadable(self):
        rule = "─" * 40
        far = f"{rule}\n❯ \n{rule}\n" + "".join(f"row {i}\n" for i in range(edl.INPUT_BOX_FOOTER_MAX_ROWS + 1))
        self.assertEqual(edl.parse_input_box(far).state, edl.BOX_UNREADABLE)
        near = f"{rule}\n❯ \n{rule}\n" + "".join(f"row {i}\n" for i in range(edl.INPUT_BOX_FOOTER_MAX_ROWS)) + "\n\n"
        self.assertTrue(edl.parse_input_box(near).empty)

    def test_pane_ready_reasons(self):
        empty = pane("cc-2.1.330-input-empty-after-clear.txt")
        self.assertIsNone(edl.pane_ready(False, empty))
        self.assertEqual(edl.pane_ready(True, pane("cc-2.1.330-copy-mode-empty-box.txt")), "copy-mode")
        self.assertEqual(edl.pane_ready(None, empty), "pane mode unknown")
        self.assertEqual(edl.pane_ready(False, pane("cc-2.1.330-input-typed.txt")), "input box not empty")
        self.assertEqual(edl.pane_ready(False, pane("synth-code-mode-optin.txt")), "a dialog shows")
        self.assertEqual(edl.pane_ready(False, pane("cc-2.1.330-shell-mode.txt")), "pane unreadable")
        self.assertEqual(edl.pane_ready(False, None), "pane unreadable")

    def test_lines_of_9_2(self):
        self.assertEqual(edl.event_line("E-41", "/s/events/E-41.md"), "EVENT E-41: read /s/events/E-41.md")
        self.assertEqual(edl.resume_line("E-4", "/p"), "RESUME E-4: read /p")
        self.assertEqual(edl.rotate_line("E-4", "/p"), "ROTATE E-4: read /p")
        self.assertEqual(edl.reminder_line("E-4", "/p"),
                         "VERDICT? E-4: end this turn with exactly one verdict line (see /p)")
        self.assertEqual(edl.PING_LINE, 'PING: no action needed; reply exactly "WAITING: ping"')


# -- the decision table (§11.2) with fixture readings ------------------------------

class TestPolicy(_Disp):
    def decide(self, L, st, key="driver"):
        res, readings = self.reconcile(L, st)
        rec = res.state["sessions"].get(key)
        reading = readings.by_name().get((rec or {}).get("name"))
        return edl.choose_action(L, res.state, key, res.sessions.get(key), self.now(),
                                 handoff_done=edl.handoff_complete(rec, reading))

    def idle(self, st, entry, **kw):
        return self.live(st, entries=[u("2026-11-24T09:00:01Z", "EVENT E-0: read /x"), entry],
                         events=[ev("turn_start", "2026-11-24T09:00:01Z"),
                                 ev("stop", entry["timestamp"], state="idle")], **kw)

    def test_no_session_yet_spawns(self):
        L, st = self.ledger()
        self.assertEqual(self.decide(L, st).action, edl.ACT_SPAWN)

    def test_warm_reuses(self):
        L, st = self.ledger()
        self.idle(st, usage_entry("2026-11-24T09:50:00Z"))          # 1 h bucket, 50 min left
        d = self.decide(L, st)
        self.assertEqual(d.action, edl.ACT_REUSE, d.reason)
        self.assertIn("warm", d.reason)

    def test_cold_on_the_hour_bucket_rotates(self):
        L, st = self.ledger()
        self.idle(st, usage_entry("2026-11-24T08:30:00Z"))          # expired 30 min ago
        self.assertEqual(self.decide(L, st).action, edl.ACT_ROTATE)
        L, st = self.ledger()
        self.world.handles = []
        self.idle(st, usage_entry("2026-11-24T09:04:00Z"))          # 4 min left: under the 5 min floor
        self.assertEqual(self.decide(L, st).action, edl.ACT_ROTATE)

    def test_over_cap_rotates(self):
        L, st = self.ledger()
        self.idle(st, usage_entry("2026-11-24T09:55:00Z", ctx=250000))
        d = self.decide(L, st)
        self.assertEqual(d.action, edl.ACT_ROTATE)
        self.assertIn("cap 250000", d.reason)

    def test_overage_cold_reuses_in_place(self):
        L, st = self.ledger()
        self.idle(st, usage_entry("2026-11-24T09:40:00Z", bucket="5m"))   # 5m bucket, cold for 15 min
        res, _ = self.reconcile(L, st)
        self.assertTrue(res.sessions["driver"].overage)
        d = self.decide(L, st)
        self.assertEqual(d.action, edl.ACT_REUSE)
        self.assertIn("m2", d.reason)
        # m2: rotation stays for over-cap sessions, overage or not
        L, st = self.ledger()
        self.world.handles = []
        self.idle(st, usage_entry("2026-11-24T09:40:00Z", ctx=260000, bucket="5m"))
        self.assertEqual(self.decide(L, st).action, edl.ACT_ROTATE)

    def test_dead_resumes_in_place_even_cold(self):
        L, st = self.ledger()
        self.idle(st, usage_entry("2026-11-24T06:00:00Z"), alive=False)
        d = self.decide(L, st)
        self.assertEqual(d.action, edl.ACT_RESUME)

    def test_dead_with_a_completed_handoff_spawns_the_next_generation(self):
        L, st = self.ledger()
        handoff = self.tmp / "h.md"
        handoff.write_text("# Handoff\n")
        os.utime(str(handoff), (self.epoch("2026-11-24T09:30:00Z"),) * 2)
        self.idle(st, usage_entry("2026-11-24T09:30:00Z"), alive=False, rotation={
            "event": "E-7", "requested_at": "2026-11-24T09:20:00Z", "handoff_path": str(handoff), "ticks": 0,
            "handoff": {"path": str(handoff), "at": "2026-11-24T09:30:00Z", "event": "E-7"}})
        self.assertEqual(self.decide(L, st).action, edl.ACT_SPAWN_NEXT)
        # the same record without the HANDOFF verdict: resume in place
        st["sessions"]["driver"]["rotation"]["handoff"] = None
        self.assertEqual(self.decide(L, st).action, edl.ACT_RESUME)

    def test_busy_waits(self):
        L, st = self.ledger()
        self.live(st, events=[ev("turn_start", "2026-11-24T09:59:00Z")])
        self.assertEqual(self.decide(L, st).action, edl.ACT_WAIT)

    def test_limited_holds(self):
        L, st = self.ledger()
        self.live(st, entries=[u("2026-11-24T09:50:00Z", "EVENT E-0: read /x"),
                               limit("2026-11-24T09:51:00Z", "limit · resets 3pm")],
                  events=[ev("turn_start", "2026-11-24T09:50:00Z"), ev("stop", "2026-11-24T09:51:00Z")])
        d = self.decide(L, st)
        self.assertEqual(d.action, edl.ACT_HOLD)
        # a held role with no session yet is not spawned either
        L2, st2 = self.ledger()
        st2["limits"] = {"claude-opus-5-5": {"until": "2026-11-24T15:00:00Z", "source": "reset", "ladder_step": 0}}
        self.assertEqual(edl.choose_action(L2, st2, "driver", None, self.now()).action, edl.ACT_HOLD)

    def test_compacted_rotates(self):
        L, st = self.ledger()
        self.live(st, entries=[u("2026-11-24T09:00:01Z", "EVENT E-0: read /x"), compact("2026-11-24T09:30:00Z"),
                               usage_entry("2026-11-24T09:55:00Z", ctx=40000)])
        d = self.decide(L, st)
        self.assertEqual(d.action, edl.ACT_ROTATE)
        self.assertIn("compacted", d.reason)

    def test_a_launch_not_up_and_a_rotation_under_way_wait(self):
        L, st = self.ledger()
        self.session(st, alive=False, status="spawning", handle=False)
        self.assertEqual(self.decide(L, st).action, edl.ACT_WAIT)
        L, st = self.ledger()
        self.world.handles = []
        self.idle(st, usage_entry("2026-11-24T09:55:00Z"), rotation={
            "event": "E-7", "requested_at": "2026-11-24T09:56:00Z", "handoff_path": "/nowhere.md", "ticks": 0})
        self.assertEqual(self.decide(L, st).action, edl.ACT_WAIT)


# -- event files and spawn ---------------------------------------------------------

class TestEventsAndSpawn(_Disp):
    def test_a_fresh_session_is_spawned_seeded_with_its_event(self):
        L, st = self.ledger(roles={"driver": {"model": "fable", "effort": "high", "profile": "claude",
                                              "pinned_env": {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "65"}}})
        st["next_event_n"] = 41
        self.world.reply["ws-t-driver-g1"] = None
        st2, plan, _, commits = self.tick(L, st)
        self.assertEqual([op.effect.kind for op in plan.ops], [edl.EFFECT_SPAWN])
        path = str(self.paths.events / "E-41.md")
        self.assertEqual(self.world.spawns, [[
            "spawn", "t-driver-g1", "--dir", str(self.ws), "--yolo", "--model=fable", "--effort=high",
            "--profile", "claude", "--run-id", "epic-t", "--pin-env", "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=65",
            "--detach", "--", f"EVENT E-41: read {path}"]])
        self.assertEqual(self.world.created, ["ws-t-driver-g1"])
        # binding: the commit before the spawn holds the event file, the outbox entry, the inflight
        # marker and the session record in `spawning` with its launch_event
        before = commits[0]
        rec = before["sessions"]["driver"]
        self.assertEqual((rec["status"], rec["name"], rec["gen"], rec["launch_event"]),
                         ("spawning", "ws-t-driver-g1", 1, "E-41"))
        e = self.entry(before, "E-41")
        self.assertEqual((e["to"], e["session"], e["kinds"], e["steps"], e["via"], e["sends"]),
                         ("driver", "ws-t-driver-g1", ["step_due"], ["A"], "spawn", [self.NOW]))
        self.assertEqual(before["steps"]["A"]["status"], "inflight")
        self.assertEqual(before["steps"]["A"]["inflight"],
                         {"step": "A", "event": "E-41", "started_at": self.NOW, "host": "ws", "session": "ws-t-driver-g1"})
        self.assertEqual(before["next_event_n"], 42)
        text = Path(path).read_text()
        self.assertTrue(text.startswith("---\nevent: E-41\nrun_id: epic-t\ncreated_at: 2026-11-24T10:00:00Z\n"
                                        "to: driver\nsession: ws-t-driver-g1\nkinds: [step_due]\nsteps: [A]\n"
                                        "needs_verdict: true\n---\n# E-41 — step due: A\n"))
        # §19: a fresh session's first event opens with the role item naming the installed driver prompt
        self.assertIn("## 1. role\n- Read your role prompt at "
                      + str(edl.ROLE_PROMPTS_DIR / edl.DRIVER_PROMPT_FILE) + " first.", text)
        self.assertIn("## 2. step_due — A (occurrence 1)\n- Card: " + str(self.plan / "cards" / "c.md"), text)
        self.assertIn("## Reply\nEnd this turn with exactly one verdict line (architecture §10), covering every "
                      "step above: A.", text)
        self.assertEqual(st2["sessions"]["driver"]["launch_error"], None)
        self.assertEqual(plan.dispatched_steps, ["A"])

    def test_one_driver_event_per_pass_merges_every_item(self):
        L, st = self.ledger([self.driver("A"), self.driver("B")])
        self.live(st)
        items = [edl.EventItem(edl.ITEM_ALARM_CLEARED, "driver", None, {"alarm": "listener_down", "subject": "run"})]
        st2, plan, _, _ = self.tick(L, st, items=items)
        self.assertEqual(len(plan.events), 1)
        pe = plan.events[0]
        self.assertEqual((pe.event, pe.kinds, pe.steps), ("E-1", ["step_due", "alarm_cleared"], ["A", "B"]))
        self.assertEqual(self.world.sends, [("ws-t-driver-g1", f"EVENT E-1: read {pe.path}")])

    def test_one_event_per_worker_and_a_worker_report_path(self):
        L, st = self.worker_ledger()
        wdir = self.tmp / "wt" / "t-w"
        wdir.mkdir(parents=True)
        st2, plan, _, _ = self.tick(L, st, worker_dirs={"W": str(wdir)})
        keys = sorted(pe.to for pe in plan.events)
        self.assertEqual(keys, ["driver", "impl:W"])
        worker = next(pe for pe in plan.events if pe.to == "impl:W")
        self.assertEqual(worker.report, str(self.paths.reports / f"W-implementation-{worker.n}.md"))
        spawn = next(s for s in self.world.spawns if s[1] == "t-impl-w-g1")
        self.assertEqual(spawn[:4], ["spawn", "t-impl-w-g1", "--dir", str(wdir)])
        self.assertIn("t-w-t-impl-w-g1", self.world.created)
        self.assertIn(str(edl.ROLE_PROMPTS_DIR / edl.WORKER_PROMPT_FILE), worker.text)
        self.assertIn(f"`DONE: W`", worker.text)
        # a worker step without its directory from 50-07 is not dispatched
        L, st = self.worker_ledger()
        self.world.handles, self.world.spawns = [], []
        _, plan, _, _ = self.tick(L, st, worker_dirs={})
        self.assertEqual([pe.to for pe in plan.events], ["driver"])

    def test_name_in_use_is_spawn_failed_and_the_same_event_is_tried_again(self):
        L, st = self.ledger()
        self.world.spawn_error = "amux-spawn: error: session name already in use: ws-t-driver-g1\n"
        st2, plan, _, _ = self.tick(L, st)
        rec = st2["sessions"]["driver"]
        self.assertEqual((rec["status"], rec["launch_error"]["reason"]), ("spawning", "name_in_use"))
        self.assertEqual(self.entry(st2, "E-1")["sends"], [])             # nothing was launched
        self.assertEqual(self.conds(L, st2), ["spawn_failed:ws-t-driver-g1"])
        self.assertEqual(st2["steps"]["A"]["inflight"]["event"], "E-1")    # the marker is kept
        # next pass: the same event, not a new one; it comes up and the alarm clears
        self.world.spawn_error = None
        st3, plan, _, _ = self.tick(L, st2, advance=600)
        self.assertEqual([op.effect.kind for op in plan.ops], [edl.EFFECT_SPAWN])
        self.assertEqual(self.world.spawns[-1][-1].split(":")[0], "EVENT E-1")
        self.assertEqual(st3["next_event_n"], 2)
        st4, _, _, _ = self.tick(L, st3, advance=600)
        self.assertEqual(self.conds(L, st4, "spawn_failed"), [])

    def test_the_launch_retry_schedule(self):
        wait = edl.launch_retry_wait_s
        t0 = edl.from_iso("2026-11-24T10:00:00Z")
        for attempts, interval in ((1, 600), (2, 1200), (3, 2400), (4, 4800), (5, 9600), (6, 14400), (9, 14400)):
            rec = {"launch_attempts": attempts, "launch_error": {"reason": "failed", "at": "2026-11-24T10:00:00Z"}}
            self.assertEqual(wait(rec, t0 + timedelta(seconds=interval - 1)), 1, attempts)
            self.assertIsNone(wait(rec, t0 + timedelta(seconds=interval)), attempts)
        # no error, or name_in_use: no wait
        self.assertIsNone(wait({"launch_attempts": 3, "launch_error": None}, t0))
        self.assertIsNone(wait({"launch_attempts": 3, "launch_error": {"reason": "name_in_use",
                                                                        "at": "2026-11-24T10:00:00Z"}}, t0))
        self.assertEqual((edl.LAUNCH_RETRY_BASE_S, edl.LAUNCH_RETRY_MAX_S), (600, 4 * 3600))

    def test_a_failed_spawn_backs_off_doubling_to_the_cap_and_resets_on_success(self):
        L, st = self.ledger()
        self.world.spawn_error = "amux-spawn: error: boom\n"
        st, _, _, _ = self.tick(L, st)
        self.assertEqual((len(self.world.spawns), st["sessions"]["driver"]["launch_attempts"]), (1, 1))
        elapsed = 0
        for n, interval in enumerate((600, 1200, 2400, 4800, 9600, 14400, 14400), start=2):
            with self.subTest(attempt=n):
                st, plan, _, _ = self.tick(L, st, advance=interval - 1 - elapsed)
                self.assertEqual(len(self.world.spawns), n - 1)                 # not before the interval
                self.assertEqual([op.effect.kind for op in plan.ops], [])
                st, plan, _, _ = self.tick(L, st, advance=1)
                self.assertEqual(len(self.world.spawns), n)                     # at the interval
                self.assertEqual(st["sessions"]["driver"]["launch_attempts"], n)
                self.assertEqual(self.conds(L, st, "spawn_failed"), ["spawn_failed:ws-t-driver-g1"])   # stays raised
                elapsed = 0
        # the next attempt succeeds: the error clears and the schedule starts over
        self.world.spawn_error = None
        st, _, _, _ = self.tick(L, st, advance=14400 - 1)
        self.assertEqual(len(self.world.spawns), 8)
        st, _, _, _ = self.tick(L, st, advance=1)
        self.assertEqual(len(self.world.spawns), 9)
        self.assertEqual((st["sessions"]["driver"]["launch_error"], st["sessions"]["driver"]["launch_attempts"]),
                         (None, 1))

    def test_name_in_use_keeps_its_every_tick_retry(self):
        L, st = self.ledger()
        self.world.spawn_error = "amux-spawn: error: session name already in use: ws-t-driver-g1\n"
        st, _, _, _ = self.tick(L, st)
        self.world.spawn_error = None
        st, _, _, _ = self.tick(L, st, advance=1)
        self.assertEqual(len(self.world.spawns), 2)

    def test_a_failed_resume_backs_off_too(self):
        L, st = self.ledger()
        self.inflight(st, "A", event="E-3")
        st["next_event_n"] = 4
        self.live(st, alive=False, status="dead")
        self.world.resume_error = "amux-spawn: error: boom\n"
        st, _, _, _ = self.tick(L, st)
        st, _, _, _ = self.tick(L, st, advance=599)
        self.assertEqual(len(self.world.resumes), 1)
        st, _, _, _ = self.tick(L, st, advance=1)
        self.assertEqual(len(self.world.resumes), 2)
        st, _, _, _ = self.tick(L, st, advance=1199)
        self.assertEqual(len(self.world.resumes), 2)
        st, _, _, _ = self.tick(L, st, advance=1)
        self.assertEqual(len(self.world.resumes), 3)

    def test_a_re_issued_launch_is_committed_before_it_runs(self):
        L, st = self.ledger()
        self.world.spawn_error = "amux-spawn: error: boom\n"
        st, _, _, _ = self.tick(L, st)
        before = st["sessions"]["driver"]["launch_error"]["at"]
        st2, _, _, commits = self.tick(L, st, advance=600, crash_at=1)          # killed after the commit
        self.assertEqual(len(self.world.spawns), 1)                              # the effect did not run
        self.assertEqual(st2["sessions"]["driver"]["launch_attempts"], 2)        # but the attempt is recorded
        self.assertEqual(self.entry(st2, "E-1")["sends"], ["2026-11-24T10:10:00Z"])
        self.assertEqual(st2["sessions"]["driver"]["launch_error"]["at"], before)

    def reboot(self, st):
        """Make the next pass see a boot id change (the fake clock sample says ``boot-1``)."""
        st["clock"] = {"boot_id": "boot-0", "wall": 1.0, "boottime": 1.0, "monotonic": 1.0}

    def test_a_reboot_resets_the_launch_backoff_and_the_next_pass_launches(self):
        L, st = self.ledger()
        self.world.spawn_error = "amux-spawn: error: boom\n"
        st, _, _, _ = self.tick(L, st)
        st, _, _, _ = self.tick(L, st, advance=600)
        self.assertEqual((len(self.world.spawns), st["sessions"]["driver"]["launch_attempts"]), (2, 2))
        st, _, _, _ = self.tick(L, st, advance=60)                       # 20 m wait: gated
        self.assertEqual(len(self.world.spawns), 2)
        self.reboot(st)
        st1, _, res, commits = self.tick(L, st, advance=60, crash_at=1)   # killed after the reset commit
        self.assertIsNotNone(res.reboot)
        self.assertEqual(len(self.world.spawns), 2)                      # the reset is committed before the effect
        self.assertEqual(st1["sessions"]["driver"]["launch_attempts"], 0)
        self.assertEqual(st1["sessions"]["driver"]["launch_error"]["reason"], "failed")   # alarm not cleared
        st2, _, _, _ = self.tick(L, st)
        self.assertEqual(len(self.world.spawns), 3)                      # launched at once (a pass of its own)
        # the post-reboot launch failed too: the ladder restarts at 10 m, not 40 m
        rec = st2["sessions"]["driver"]
        self.assertEqual(rec["launch_attempts"], 1)
        self.assertEqual(self.conds(L, st2, "spawn_failed"), ["spawn_failed:ws-t-driver-g1"])
        st3, _, _, _ = self.tick(L, st2, advance=599)
        self.assertEqual(len(self.world.spawns), 3)
        st3, _, _, _ = self.tick(L, st3, advance=1)
        self.assertEqual(len(self.world.spawns), 4)
        self.assertEqual(st3["sessions"]["driver"]["launch_attempts"], 2)
        st4, _, _, _ = self.tick(L, st3, advance=1199)
        self.assertEqual(len(self.world.spawns), 4)

    def test_a_name_known_to_amux_is_never_spawned(self):
        L, st = self.ledger()
        self.world.amux = ["ws-t-driver-g1"]          # an orphan by the grammar
        st2, plan, _, _ = self.tick(L, st)
        self.assertEqual(self.world.spawns, [])
        self.assertEqual(st2["sessions"]["driver"]["launch_error"]["reason"], "name_in_use")
        self.assertEqual(self.conds(L, st2, "spawn_failed"), ["spawn_failed:ws-t-driver-g1"])

    def test_a_held_item_is_still_committed_into_an_event(self):
        """Reconcile emits an answer once; a limited model holds delivery, the event keeps it."""
        L, st = self.ledger([])
        st["limits"] = {"claude-opus-5-5": {"until": "2026-11-24T15:00:00Z", "source": "reset", "ladder_step": 0}}
        item = edl.EventItem(edl.ITEM_ANSWER, "driver", None, {"question": "Q-3", "approval": "SA4", "answer": "yes",
                                                               "path": "/q.md", "asked_event": "E-0"})
        st2, plan, _, _ = self.tick(L, st, items=[item])
        self.assertEqual(plan.decisions["driver"].action, edl.ACT_HOLD)
        self.assertEqual(self.world.spawns, [])
        e = self.entry(st2, "E-1")
        self.assertEqual((e["sends"], e["approvals"], e["kinds"]), ([], ["SA4"], ["answer"]))
        # the limit lifts: spawned seeded with that same queued event folded in
        st2["limits"] = {}
        st3, plan, _, _ = self.tick(L, st2, advance=600)
        seed = plan.events[0]
        self.assertEqual((seed.supersedes, seed.approvals), (["E-1"], ["SA4"]))
        self.assertIn("queued as E-1", seed.text)
        self.assertIsNone(self.entry(st3, "E-1"))

    def test_check_failed_and_worker_items_carry_what_the_driver_needs(self):
        L, st = self.worker_ledger()
        st["steps"]["W"]["status"] = "inflight"
        st["steps"]["A"]["status"] = "failed"
        self.live(st)
        out = edl.CheckOutcome(step_id="A", kind=edl.KIND_CHECK, outcome=edl.OUTCOME_FAILED, rc=2,
                               output_tail="node down\n```\nx", attempt=1, consecutive_errors=0)
        settled = edl.EventItem(edl.ITEM_WORKER_SETTLED, "driver", "W", {
            "role": "rev", "worker": "reviewer", "verdict": "DONE: W", "event": "E-7"})
        readings = edl.take_readings(L, st, self.world, self.now())
        res = edl.reconcile(L, st, readings, self.now())
        plan = edl.plan_dispatch(L, res.state, res, readings, self.now(), paths=self.paths,
                                 check_outcomes=[out], items=[settled], host="ws")
        pe = plan.events[0]
        self.assertEqual((pe.to, pe.kinds, pe.steps), ("driver", ["check_failed", "worker_settled"], ["A", "W"]))
        self.assertIn("## 1. check_failed — A (attempt 1)", pe.text)
        self.assertIn("- Check exit code: 2.", pe.text)
        self.assertIn("    node down\n    ```\n    x", pe.text)            # indented: a fence in the tail is inert
        self.assertIn(f"- Report: {self.paths.reports / 'W-review-7.md'}", pe.text)
        preview = plan.preview["steps"]
        self.assertEqual((preview["A"]["status"], preview["A"]["inflight"]["event"]), ("failed", pe.event))
        self.assertEqual(preview["W"]["inflight"]["session"], "ws-t-driver-g1")

    def test_approval_needed_is_raised_once_per_ask_cycle(self):
        L, st = self.ledger([self.driver("C0"), self.driver("A", approval="SA4", needs=["C0"])],
                            approvals={"SA4": {"conditions": ["C0"]}})
        st["steps"]["C0"].update(status="done", done_at="2026-11-24T08:00:00Z")
        self.live(st)
        self.world.reply["ws-t-driver-g1"] = "WAITING: SA4 the operator is away"
        st1, plan, _, _ = self.tick(L, st)
        pe = plan.events[0]
        self.assertEqual((pe.kinds, pe.approvals, pe.steps), (["approval_needed"], ["SA4"], []))
        self.assertIn("then end with `ASKED: Q-<n> SA4`", pe.text)
        self.assertEqual(st1["approval_raised"]["SA4"]["event"], pe.event)
        # pending, then answered WAITING without a question: not raised again
        for _ in range(2):
            st1, plan, _, _ = self.tick(L, st1, advance=600)
            self.assertEqual(plan.events, [])
        # a question about it was asked and then closed after the raise: a new cycle
        st1["questions"]["Q-9"] = {"step": None, "approval": "SA4", "asked_event": pe.event, "status": "resolved",
                                   "asked_at": edl.iso(self.now()), "answer_event": None}
        _, plan, _, _ = self.tick(L, st1, advance=600)
        self.assertEqual([p.kinds for p in plan.events], [["approval_needed"]])

    def test_feedback_from_a_queue_request(self):
        L, st = self.worker_ledger()
        req = edl.queue_request(L, st, "W", "impl", "/plan/feedback/W-1.md", self.now())
        self.assertEqual(req["id"], "R-1")
        with self.assertRaises(ValueError):
            edl.queue_request(L, st, "A", "impl", "/f.md", self.now())
        with self.assertRaises(ValueError):
            edl.queue_request(L, st, "W", "driver", "/f.md", self.now())
        with self.assertRaises(ValueError):
            edl.queue_request(L, st, "W", "impl", "relative.md", self.now())
        st["steps"]["A"]["status"] = "done"          # nothing else due
        self.inflight(st, "W", event="E-0", name="t-w-t-impl-w-g1")
        self.live(st, key="impl:W", name="t-w-t-impl-w-g1")
        st2, plan, _, _ = self.tick(L, st)
        pe = next(p for p in plan.events if p.to == "impl:W")
        self.assertEqual((pe.kinds, pe.steps), (["feedback"], ["W"]))
        self.assertIn("Read the driver's feedback at /plan/feedback/W-1.md", pe.text)
        self.assertEqual(st2["queue"][0]["event"], pe.event)
        self.assertEqual(self.world.sends[-1], ("t-w-t-impl-w-g1", f"EVENT {pe.event}: read {pe.path}"))
        _, plan2, _, _ = self.tick(L, st2, advance=600)
        self.assertEqual([p for p in plan2.events if p.to == "impl:W"], [])     # consumed once


# -- rotation (§11.3) ---------------------------------------------------------------

class TestRotation(_Disp):
    def over_cap(self, st, **kw):
        return self.live(st, entries=[u("2026-11-24T09:00:01Z", "EVENT E-0: read /x"),
                                      usage_entry("2026-11-24T09:55:00Z", ctx=260000)], **kw)

    def test_the_full_sequence(self):
        L, st = self.ledger([self.driver("A"), self.driver("B", not_before="2026-11-24T10:05:00Z")])
        self.over_cap(st)
        name = "ws-t-driver-g1"
        self.world.reply[name] = None                     # the rotate turn is still running
        st1, plan, _, commits = self.tick(L, st)
        hp = str(self.paths.handoffs / "driver-g1.md")
        self.assertEqual(plan.decisions["driver"].action, edl.ACT_ROTATE)
        queued = self.entry(st1, "E-1")
        self.assertEqual((queued["kinds"], queued["steps"], queued["sends"]), (["step_due"], ["A"], []))
        rot_entry = self.entry(st1, "E-2")
        self.assertEqual(rot_entry["kinds"], ["rotate"])
        self.assertEqual(self.world.sends, [(name, f"ROTATE E-2: read {rot_entry['path']}")])
        self.assertIn(f"`HANDOFF: {hp}`", Path(rot_entry["path"]).read_text())
        rot = st1["sessions"]["driver"]["rotation"]
        self.assertEqual((rot["event"], rot["requested_at"], rot["handoff_path"]), ("E-2", self.NOW, hp))
        # the session writes the handoff and ends HANDOFF; B becomes due meanwhile
        self.world.advance(600)
        Path(hp).parent.mkdir(parents=True, exist_ok=True)
        Path(hp).write_text("# Handoff\n")
        os.utime(hp, (self.epoch("2026-11-24T10:07:00Z"),) * 2)
        self.world.events[name] += [ev("stop", "2026-11-24T10:08:00Z", state="idle", last_message=f"HANDOFF: {hp}")]
        self.world._append(name, a("2026-11-24T10:08:00Z", f"HANDOFF: {hp}"))
        self.world._handle(name).update(last_message=f"HANDOFF: {hp}", stopped_at="2026-11-24T10:08:00Z")
        self.world.reply["ws-t-driver-g2"] = "WAITING: soak"
        st2, plan, res, commits = self.tick(L, st1)
        self.assertTrue(res.sessions["driver"].rotation_ready)
        self.assertEqual(plan.decisions["driver"].action, edl.ACT_SPAWN_NEXT)
        seed = next(pe for pe in plan.events if pe.via == "spawn")
        self.assertEqual(self.world.created, ["ws-t-driver-g2"])
        self.assertEqual(self.world.spawns[-1][1], "t-driver-g2")
        self.assertEqual(self.world.spawns[-1][-1], f"EVENT {seed.event}: read {seed.path}")
        # E-m: the role, then the handoff first, then every event queued meanwhile (and B, new now)
        kinds = [it.kind for it in seed.items]
        self.assertEqual(kinds[:2], ["role", "handoff"])
        self.assertIn(f"Read the handoff at {hp} first", seed.text)
        self.assertEqual(sorted(seed.steps), ["A", "B"])
        self.assertEqual(seed.supersedes, ["E-1"])
        self.assertIsNone(self.entry(st2, "E-1"))                        # moved to the new session
        self.assertEqual(st2["steps"]["A"]["inflight"]["event"], seed.event)
        self.assertEqual(st2["steps"]["A"]["inflight"]["session"], "ws-t-driver-g2")
        # binding: the rotation spawn writes launch_event in the same commit as `spawning`
        spawn_commit = next(c for c in commits if c["sessions"]["driver"]["name"] == "ws-t-driver-g2")
        rec = spawn_commit["sessions"]["driver"]
        self.assertEqual((rec["status"], rec["gen"], rec["launch_event"]), ("spawning", 2, seed.event))
        self.assertEqual([p["name"] for p in rec["parked"]], [name])
        self.assertEqual(self.world.reaps, [])                          # not before the new session started
        # the new session's first turn_start is seen: the old one is reaped
        st3, plan, _, _ = self.tick(L, st2, advance=600)
        self.assertEqual(self.world.reaps, [name])
        self.assertEqual(st3["sessions"]["driver"]["parked"], [])

    def test_an_unsent_rotate_waits_for_a_busy_session(self):
        L, st = self.ledger([])
        self.over_cap(st, events=[ev("turn_start", "2026-11-24T09:59:00Z")],      # the operator's turn
                      rotation={"event": "E-2", "requested_at": None, "handoff_path": "/h.md", "ticks": 0})
        self.outbox(st, "E-2", steps=(), kinds=("rotate",), sends=[], line="ROTATE E-2: read /e2")
        st2, plan, _, _ = self.tick(L, st)
        self.assertEqual(self.world.sends, [])
        self.assertEqual(self.entry(st2, "E-2")["deferrals"], 0)
        self.assertIsNone(st2["sessions"]["driver"]["rotation"]["requested_at"])

    def rotated_with_parked(self, attached):
        L, st = self.ledger([])
        self.live(st, key=None, name="ws-t-driver-g1",
                  display={"pane_dead": "0", "pane_in_mode": "0", "session_attached": str(attached), "pane_pid": "9"})
        self.live(st, name="ws-t-driver-g2", gen=2, launch_event="E-5",
                  parked=[{"name": "ws-t-driver-g1", "since": "2026-11-24T09:00:00Z", "gen": 1}])
        return L, st

    def test_an_attached_client_delays_reaping(self):
        L, st = self.rotated_with_parked(attached=1)
        st2, plan, _, _ = self.tick(L, st)
        self.assertEqual(self.world.reaps, [])
        self.assertTrue(any("attached" in n for n in plan.notes))
        self.world.tmux["ws-t-driver-g1"]["display"]["session_attached"] = "0"
        st3, _, _, _ = self.tick(L, st2, advance=600)
        self.assertEqual(self.world.reaps, ["ws-t-driver-g1"])
        self.assertEqual(st3["sessions"]["driver"]["parked"], [])

    def test_a_worker_rotation_ends_with_handoff_and_spawns_the_next_generation(self):
        """Pin (50-09 fix 2): the parser, reconcile and dispatch carry a worker's rotation to gen + 1."""
        L, st = self.worker_ledger()
        st["steps"]["A"]["status"] = "done"                     # only the worker step is in play
        wdir = self.tmp / "wt" / "t-w"
        wdir.mkdir(parents=True)
        name = "ws-t-impl-w-g1"
        self.inflight(st, "W", event="E-0", name=name)
        self.live(st, key="impl:W", name=name,
                  entries=[u("2026-11-24T09:00:01Z", "EVENT E-0: read /x"),
                           usage_entry("2026-11-24T09:55:00Z", ctx=260000)])
        self.world.reply[name] = None                           # the rotate turn is still running
        edl.queue_request(L, st, "W", "impl", "/f.md", self.now())      # an event is waiting for the worker
        st1, plan, _, _ = self.tick(L, st, worker_dirs={"W": str(wdir)})
        self.assertEqual(plan.decisions["impl:W"].action, edl.ACT_ROTATE)
        rot = st1["sessions"]["impl:W"]["rotation"]
        hp = rot["handoff_path"]
        self.assertEqual(self.entry(st1, rot["event"])["kinds"], ["rotate"])
        # the worker writes the handoff and ends HANDOFF (a worker may, in a rotate turn)
        self.world.advance(600)
        Path(hp).parent.mkdir(parents=True, exist_ok=True)
        Path(hp).write_text("# Handoff\n")
        os.utime(hp, (self.epoch("2026-11-24T10:07:00Z"),) * 2)
        self.world.events[name] += [ev("stop", "2026-11-24T10:08:00Z", state="idle", last_message=f"HANDOFF: {hp}")]
        self.world._append(name, a("2026-11-24T10:08:00Z", f"HANDOFF: {hp}"))
        self.world._handle(name).update(last_message=f"HANDOFF: {hp}", stopped_at="2026-11-24T10:08:00Z")
        st2, plan, res, _ = self.tick(L, st1, worker_dirs={"W": str(wdir)})
        self.assertTrue(res.sessions["impl:W"].rotation_ready)
        self.assertEqual(plan.decisions["impl:W"].action, edl.ACT_SPAWN_NEXT)
        self.assertEqual(st2["sessions"]["impl:W"]["gen"], 2)
        self.assertEqual(self.world.spawns[-1][1], "t-impl-w-g2")
        seed = next(pe for pe in plan.events if pe.via == "spawn" and pe.to == "impl:W")
        self.assertEqual([it.kind for it in seed.items][:2], ["role", "handoff"])

    def test_no_handoff_after_two_ticks_falls_back_to_reuse(self):
        L, st = self.ledger()
        self.over_cap(st)
        name = "ws-t-driver-g1"
        self.world.mode[name] = "lost"                    # the rotate line never produces a handoff
        st1, _, _, _ = self.tick(L, st)
        self.assertEqual([x[1].split(":")[0] for x in self.world.sends], ["ROTATE E-2"])
        st2, plan, _, _ = self.tick(L, st1, advance=600)
        self.assertEqual(st2["sessions"]["driver"]["rotation"]["ticks"], 1)
        self.assertEqual(self.conds(L, st2, "handoff_timeout"), [])
        self.world.mode[name] = "land"
        st3, plan, _, _ = self.tick(L, st2, advance=600)
        self.assertIsNone(st3["sessions"]["driver"]["rotation"])
        self.assertEqual(self.conds(L, st3, "handoff_timeout"), [f"handoff_timeout:{name}"])
        self.assertIsNone(self.entry(st3, "E-2"))
        # the queued event goes to the old session instead
        self.assertEqual(self.world.sends[-1], (name, f"EVENT E-1: read {self.paths.events / 'E-1.md'}"))
        self.assertEqual(self.world.spawns, [])

    def timed_out(self, at):
        """A session over the cap that timed out a handoff at ``at``, with a new event waiting."""
        L, st = self.ledger()
        self.over_cap(st, handoff_timeout={"at": at, "event": "E-0", "requested_at": at})
        st["next_event_n"] = 5
        return L, st

    def test_inside_the_cool_down_after_a_handoff_timeout_the_session_is_reused(self):
        L, st = self.timed_out("2026-11-24T09:10:01Z")                 # 49m59s before NOW
        st2, plan, _, _ = self.tick(L, st)
        self.assertEqual(plan.decisions["driver"].action, edl.ACT_REUSE)
        self.assertEqual([x[1].split(":")[0] for x in self.world.sends], ["EVENT E-5"])
        self.assertIsNone(st2["sessions"]["driver"]["rotation"])
        self.assertEqual(self.conds(L, st2, "handoff_timeout"), ["handoff_timeout:ws-t-driver-g1"])

    def test_after_the_cool_down_a_rotation_is_attempted_again(self):
        self.assertEqual(edl.ROTATE_RETRY_AFTER_S, 3600)
        L, st = self.timed_out("2026-11-24T09:00:00Z")                 # exactly 1 h before NOW
        st2, plan, _, _ = self.tick(L, st)
        self.assertEqual(plan.decisions["driver"].action, edl.ACT_ROTATE)
        self.assertEqual([x[1].split(":")[0] for x in self.world.sends], ["ROTATE E-6"])
        self.assertIsNotNone(st2["sessions"]["driver"]["rotation"])


# -- resume (§11.4) -------------------------------------------------------------------

class TestResume(_Disp):
    def test_a_dead_session_with_a_step_in_flight_resumes_in_place(self):
        L, st = self.ledger()
        self.inflight(st, "A", event="E-3")
        st["next_event_n"] = 4
        self.live(st, alive=False, status="dead", last_event="E-3")
        self.outbox(st, "E-3", confirmed_at="2026-11-24T09:00:05Z")
        st2, plan, _, commits = self.tick(L, st)
        pe = plan.events[0]
        self.assertEqual(self.world.resumes, [["resume", "ws-t-driver-g1", "--json", "--",
                                               f"RESUME E-4: read {pe.path}"]])
        self.assertEqual(self.world.spawns, [])
        self.assertEqual((pe.kinds, pe.steps, pe.supersedes), (["resume", "step_due"], ["A"], ["E-3"]))
        self.assertIn("- In flight: A (event E-3, since 2026-11-24T09:00:00Z).", pe.text)
        self.assertIn("- The last event you received: E-3", pe.text)
        self.assertIn("Check the last action against ground truth before continuing.", pe.text)
        self.assertIn("## 1. role", pe.text)
        # binding: launch_event in the same commit as `resuming`
        rec = commits[0]["sessions"]["driver"]
        self.assertEqual((rec["status"], rec["launch_event"], rec["name"], rec["gen"]),
                         ("resuming", "E-4", "ws-t-driver-g1", 1))
        self.assertEqual(rec["resumed_at"], [self.NOW])
        self.assertEqual(commits[0]["steps"]["A"]["inflight"]["event"], "E-4")

    def test_after_a_reboot_only_the_sessions_that_should_resume_at_once_do(self):
        L, st = self.worker_ledger()
        st["clock"] = {"boot_id": "boot-0", "wall": 1.0, "boottime": 1.0, "monotonic": 1.0}
        st["steps"]["A"]["status"] = "done"
        # the driver had a turn open; the implementer has W in flight; the reviewer was idle
        self.live(st, alive=False, events=[ev("turn_start", "2026-11-24T09:50:00Z")])
        self.live(st, key="impl:W", name="t-w-t-impl-w-g1", alive=False)
        self.inflight(st, "W", event="E-2", name="t-w-t-impl-w-g1")
        self.live(st, key="rev:W", name="t-w-t-rev-w-g1", alive=False)
        st2, plan, res, _ = self.tick(L, st)
        self.assertEqual(sorted(res.reboot.resume_now), ["driver", "impl:W"])
        self.assertEqual(sorted(r[1] for r in self.world.resumes), ["t-w-t-impl-w-g1", "ws-t-driver-g1"])
        self.assertEqual(st2["sessions"]["rev:W"]["status"], "dead")
        # the reviewer resumes when its role next gets an event
        item = edl.EventItem(edl.ITEM_REVIEW_DUE, "rev:W", "W", {"implementer": "impl", "verdict": "DONE: W",
                                                                 "event": "E-2"})
        st3, plan, _, _ = self.tick(L, st2, items=[item], advance=600)
        self.assertEqual(self.world.resumes[-1][1], "t-w-t-rev-w-g1")
        pe = next(p for p in plan.events if p.to == "rev:W")
        self.assertIn(f"Implementer report: {self.paths.reports / 'W-implementation-2.md'}", pe.text)

    def test_a_failed_resume_is_resume_failed_and_tried_again_with_the_same_event(self):
        L, st = self.ledger()
        self.inflight(st, "A", event="E-3")
        st["next_event_n"] = 4
        self.live(st, alive=False, status="dead")
        self.world.resume_error = "amux-spawn: error: the transcript of session 'x' is missing\n"
        st2, _, _, _ = self.tick(L, st)
        rec = st2["sessions"]["driver"]
        self.assertEqual((rec["status"], rec["launch_error"]["kind"]), ("resuming", "resume"))
        self.assertEqual(self.entry(st2, "E-4")["sends"], [])
        self.assertEqual(self.conds(L, st2), ["resume_failed:ws-t-driver-g1"])
        self.world.resume_error = None
        st3, plan, _, _ = self.tick(L, st2, advance=600)
        self.assertEqual([r[-1].split(":")[0] for r in self.world.resumes], ["RESUME E-4", "RESUME E-4"])
        st4, _, _, _ = self.tick(L, st3, advance=600)
        self.assertEqual(self.conds(L, st4, "resume_failed"), [])

    def test_resumed_but_not_growing_keeps_the_send(self):
        L, st = self.ledger()
        self.inflight(st, "A", event="E-3")
        self.live(st, alive=False, status="dead")
        self.world.resume_error = "amux-spawn: error: session 'x' resumed but the transcript is not growing\n"
        st2, _, _, _ = self.tick(L, st)
        e = self.entry(st2, "E-1")
        self.assertEqual(e["sends"], [self.NOW])
        self.assertEqual(st2["sessions"]["driver"]["launch_error"]["reason"], "not_growing")


# -- delivery (§9.3) -------------------------------------------------------------------

class TestDelivery(_Disp):
    def due_with_live(self, **kw):
        L, st = self.ledger()
        self.live(st, **kw)
        return L, st

    def test_copy_mode_defers(self):
        L, st = self.due_with_live(display=dict(IDLE_DISPLAY, pane_in_mode="1"),
                                   pane=pane("cc-2.1.330-copy-mode-empty-box.txt"))
        st2, plan, _, _ = self.tick(L, st)
        self.assertEqual(self.world.sends, [])
        e = self.entry(st2, "E-1")
        self.assertEqual((e["sends"], e["deferrals"], e["last_deferral"]["why"]), ([], 1, "copy-mode"))

    def test_typed_text_defers_attached_and_detached(self):
        for attached in ("0", "1"):
            for fixture in ("cc-2.1.330-input-typed.txt", "cc-2.1.330-input-multiline-paste.txt",
                            "cc-2.1.330-input-pasted-placeholder.txt", "cc-2.1.330-input-wrapped-40col.txt"):
                with self.subTest(attached=attached, fixture=fixture):
                    self.world.handles, self.world.sends = [], []
                    L, st = self.due_with_live(display=dict(IDLE_DISPLAY, session_attached=attached))
                    # the operator types after the readings: the check just before the send refuses
                    self.world.tmux["ws-t-driver-g1"]["pane"] = pane(fixture)
                    st2, plan, _, commits = self.tick(L, st)
                    self.assertEqual(self.world.sends, [])
                    e = self.entry(st2, "E-1")
                    self.assertEqual((e["sends"], e["deferrals"]), ([], 1))
                    self.assertEqual(e["last_deferral"]["why"], "input box not empty")
                    # invariant 5: the send was committed before the check, then taken back
                    self.assertEqual(self.entry(commits[-2], "E-1")["sends"], [self.NOW])

    def test_a_typed_draft_seen_by_the_readings_defers_without_a_send_commit(self):
        L, st = self.ledger()
        self.live(st, pane=pane("cc-2.1.330-input-multiline-paste.txt"))
        self.outbox(st, "E-9", steps=(), kinds=("alarm_cleared",), sends=[], confirmed_at=None, line="EVENT E-9: read /x")
        st["next_event_n"] = 10
        st["steps"]["A"]["status"] = "done"
        self.outbox(st, "E-8", steps=(), sends=["2026-11-24T09:59:30Z"])       # captured: a send is unconfirmed
        st2, plan, _, _ = self.tick(L, st)
        self.assertEqual(self.world.sends, [])
        self.assertEqual(self.entry(st2, "E-9")["last_deferral"]["why"], "input box not empty")

    def test_an_unparseable_pane_defers(self):
        for fixture in ("cc-2.1.330-shell-mode.txt", "synth-blank.txt", "synth-booting-splash.txt"):
            with self.subTest(fixture=fixture):
                self.world.handles, self.world.sends = [], []
                L, st = self.due_with_live()
                self.world.tmux["ws-t-driver-g1"]["pane"] = pane(fixture)
                st2, _, _, _ = self.tick(L, st)
                self.assertEqual(self.world.sends, [])
                self.assertEqual(self.entry(st2, "E-1")["last_deferral"]["why"], "pane unreadable")

    def test_three_deferrals_raise_delivery_deferred_until_delivered(self):
        L, st = self.due_with_live()
        self.world.tmux["ws-t-driver-g1"]["pane"] = pane("cc-2.1.330-input-typed.txt")
        for i in range(3):
            st, _, _, _ = self.tick(L, st, advance=600 if i else None)
            self.assertEqual(self.conds(L, st, "delivery_deferred"),
                             ["delivery_deferred:E-1"] if i == 2 else [])
        self.assertEqual(self.entry(st, "E-1")["deferrals"], 3)
        self.world.tmux["ws-t-driver-g1"]["pane"] = pane("cc-2.1.330-input-empty-after-clear.txt")
        st, _, _, _ = self.tick(L, st, advance=600)
        self.assertEqual(len(self.world.sends), 1)
        self.assertEqual(self.conds(L, st, "delivery_deferred"), [])

    def test_a_busy_session_is_waited_for_not_deferred(self):
        L, st = self.due_with_live(events=[ev("turn_start", "2026-11-24T09:59:00Z")])
        st2, plan, _, _ = self.tick(L, st)
        self.assertEqual(plan.decisions["driver"].action, edl.ACT_WAIT)
        self.assertEqual((self.entry(st2, "E-1")["deferrals"], self.world.sends), (0, []))

    def test_confirmed_on_the_first_send(self):
        L, st = self.due_with_live()
        self.world.reply["ws-t-driver-g1"] = "ADVANCED: A"
        st1, _, _, _ = self.tick(L, st)
        st2, plan, res, _ = self.tick(L, st1, advance=600)
        self.assertEqual(res.confirmed, ["E-1"])
        self.assertEqual(len(self.world.sends), 1)
        self.assertEqual(st2["steps"]["A"]["status"], "verifying")
        self.assertIsNone(self.entry(st2, "E-1"))               # complete: it left the outbox
        self.assertEqual([c["left"] for c in plan.completed], ["completed"])

    def test_retry_once_when_provably_absent_then_no_third_send(self):
        L, st = self.due_with_live()
        name = "ws-t-driver-g1"
        self.world.mode[name] = "lost"
        st1, _, _, _ = self.tick(L, st)
        st2, _, _, _ = self.tick(L, st1, advance=600)
        e = self.entry(st2, "E-1")
        self.assertEqual(len(self.world.sends), 2)
        self.assertEqual(e["retry"]["sent"], True)
        self.assertEqual(self.conds(L, st2, "delivery_unconfirmed"), [])
        st3, _, _, _ = self.tick(L, st2, advance=600)
        self.assertEqual(self.conds(L, st3, "delivery_unconfirmed"), ["delivery_unconfirmed:E-1"])
        for _ in range(3):
            st3, _, _, _ = self.tick(L, st3, advance=600)
        self.assertEqual(len(self.world.sends), 2)                # never a third send
        # a late confirmation clears it
        self.world._append(name, u("2026-11-24T10:45:00.100Z", "EVENT E-1: read /x"))
        self.world.events[name].append(ev("turn_start", "2026-11-24T10:45:00Z"))
        st4, _, _, _ = self.tick(L, st3, advance=600)
        self.assertEqual(self.conds(L, st4, "delivery_unconfirmed"), [])

    def test_a_line_sitting_in_the_input_box_is_not_resent(self):
        L, st = self.due_with_live()
        name = "ws-t-driver-g1"
        self.world.mode[name] = "box"                         # Enter did not register
        st1, _, _, _ = self.tick(L, st)
        self.assertIn("EVENT E-1: read", self.world.tmux[name]["pane"])
        st2, plan, _, _ = self.tick(L, st1, advance=600)
        e = self.entry(st2, "E-1")
        self.assertEqual((len(self.world.sends), e["retry"]["sent"], e["retry"]["why"]),
                         (1, False, "absence not provable"))
        st3, _, _, _ = self.tick(L, st2, advance=600)
        self.assertEqual(self.conds(L, st3, "delivery_unconfirmed"), ["delivery_unconfirmed:E-1"])
        self.assertEqual(len(self.world.sends), 1)

    def lost_entry(self, **kw):
        L, st = self.due_with_live()
        self.world.mode["ws-t-driver-g1"] = "lost"
        self.inflight(st, "A", event="E-1")
        self.outbox(st, "E-1", sends=["2026-11-24T09:56:00Z"], **kw)
        st["next_event_n"] = 2
        return L, st

    def test_a_retry_without_a_line_falls_back_to_the_event_line(self):
        L, st = self.lost_entry(path="/x/events/E-1.md")
        st["outbox"][0].pop("line", None)
        self.tick(L, st)
        self.assertEqual(self.world.sends, [("ws-t-driver-g1", "EVENT E-1: read /x/events/E-1.md")])

    def test_a_retry_with_neither_line_nor_path_sends_nothing_and_keeps_the_entry(self):
        L, st = self.lost_entry()
        st["outbox"][0].pop("line", None)
        st["outbox"][0].pop("path", None)
        st2, _, _, _ = self.tick(L, st)
        self.assertEqual(self.world.sends, [])
        e = self.entry(st2, "E-1")
        self.assertEqual(e["sends"], ["2026-11-24T09:56:00Z"])

    def test_binding_b_no_retry_when_absence_is_not_provable(self):
        """50-05's delivery_provably_absent is False (here: no transcript covering the send) ⇒ no retry,
        and at the following tick delivery_unconfirmed with no further sends (invariant 2)."""
        L, st = self.due_with_live()
        name = "ws-t-driver-g1"
        self.world.mode[name] = "lost"
        st1, _, _, _ = self.tick(L, st)
        Path(self.world.handles[0]["transcript_path"]).unlink()        # nothing can prove absence now
        st2, _, res, _ = self.tick(L, st1, advance=600)
        self.assertEqual(self.entry(st2, "E-1")["retry"]["sent"], False)
        self.assertEqual(len(self.world.sends), 1)
        self.assertEqual(self.conds(L, st2, "delivery_unconfirmed"), [])
        st3, plan, _, _ = self.tick(L, st2, advance=600)
        self.assertEqual(self.conds(L, st3, "delivery_unconfirmed"), ["delivery_unconfirmed:E-1"])
        self.assertEqual([op.effect.kind for op in plan.ops if op.effect.kind != edl.EFFECT_NONE], [])
        st4, _, _, _ = self.tick(L, st3, advance=600)
        self.assertEqual(len(self.world.sends), 1)

    def test_no_judgement_on_a_send_seconds_old(self):
        L, st = self.due_with_live()
        self.world.mode["ws-t-driver-g1"] = "lost"
        st1, _, _, _ = self.tick(L, st)
        st2, _, _, _ = self.tick(L, st1, advance=5)                 # a hand-run tick 5 s later
        self.assertNotIn("retry", self.entry(st2, "E-1"))
        self.assertEqual(len(self.world.sends), 1)

    def test_binding_a_the_reminder_commits_reminded_at_before_the_send_and_only_once(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "Rolled it, checking.")
        self.live(st, entries=entries, events=events)
        self.outbox(st, "E-1", path=str(self.paths.events / "E-1.md"))
        st["next_event_n"] = 2
        st1, plan, res, commits = self.tick(L, st)
        self.assertEqual([x.kind for x in res.actions], ["verdict_reminder"])
        self.assertEqual(self.world.sends, [("ws-t-driver-g1", "VERDICT? E-1: end this turn with exactly one "
                                             f"verdict line (see {self.paths.events / 'E-1.md'})")])
        self.assertEqual(self.entry(commits[0], "E-1")["reminded_at"], self.NOW)   # before the send
        # the pass after the commit: no reminder item, nothing sent
        self.world.mode["ws-t-driver-g1"] = "lost"
        st2, plan2, res2, _ = self.tick(L, st1, advance=300)
        self.assertEqual(res2.actions, [])
        self.assertEqual(len(self.world.sends), 1)
        # killed between the commit and the send: never a reminder after all, never a second one
        L, st = self.ledger()
        self.world.handles, self.world.sends = [], []
        self.inflight(st, "A")
        self.live(st, entries=entries, events=events)
        self.outbox(st, "E-1")
        st1, _, _, _ = self.tick(L, st, crash_at=1)
        self.assertEqual((self.entry(st1, "E-1")["reminded_at"], self.world.sends), (edl.iso(self.now()), []))
        _, _, res3, _ = self.tick(L, st1, advance=300)
        self.assertEqual((res3.actions, self.world.sends), ([], []))

    def test_a_reminder_the_pane_refuses_is_not_sent_and_not_recorded(self):
        L, st = self.ledger()
        self.inflight(st, "A")
        entries, events = self.answered_turn("E-1", "Rolled it, checking.")
        self.live(st, entries=entries, events=events, pane=pane("cc-2.1.330-input-typed.txt"))
        self.outbox(st, "E-1")
        st1, plan, _, _ = self.tick(L, st)                  # the readings see the draft: no op at all
        self.assertEqual((self.entry(st1, "E-1")["reminded_at"], self.world.sends, plan.ops), (None, [], []))
        # the draft appears after the readings: committed, refused by the check, taken back
        self.world.tmux["ws-t-driver-g1"]["pane"] = pane("cc-2.1.330-input-empty-after-clear.txt")
        readings = edl.take_readings(L, st1, self.world, self.now())
        res = edl.reconcile(L, st1, readings, self.now())
        plan = edl.plan_dispatch(L, res.state, res, readings, self.now(), paths=self.paths, host="ws")
        self.world.tmux["ws-t-driver-g1"]["pane"] = pane("cc-2.1.330-input-typed.txt")
        s, commits = res.state, []
        edl.execute_dispatch(plan, s, self.world, lambda x: commits.append(copy.deepcopy(x)))
        self.assertEqual(self.entry(commits[0], "E-1")["reminded_at"], self.NOW)
        self.assertEqual((self.entry(s, "E-1")["reminded_at"], self.world.sends), (None, []))


# -- the startup-dialog answer (§12.11) -------------------------------------------------

class TestStartupDialogAnswer(_Disp):
    def launched(self):
        L, st = self.ledger([], startup_dialogs=[{"id": "code-mode", "match": CODE_MODE_MATCH, "key": "Enter"}])
        self.outbox(st, "E-1", steps=(), kinds=("alarm_cleared",), via="spawn", line="EVENT E-1: read /x")
        self.session(st, status="live", pane=pane("synth-code-mode-optin.txt"), launch_event="E-1",
                     entries=[u("2026-11-24T09:00:00Z", "(session start)")])
        st["next_event_n"] = 2
        return L, st

    def test_the_listed_key_is_sent_once_recorded_before_the_send(self):
        L, st = self.launched()
        st1, plan, res, commits = self.tick(L, st)
        self.assertEqual(self.world.keys, [("ws-t-driver-g1", "Enter")])
        answers = commits[0]["sessions"]["driver"]["dialog_answers"]
        self.assertEqual([(x["dialog"], x["key"], x["launch"]) for x in answers],
                         [("code-mode", "Enter", "2026-11-24T09:00:00Z")])      # committed before the key
        self.assertEqual(st1["sessions"]["driver"]["dialog_answers"][0]["sent_at"], self.NOW)
        # binding: a second pass after the commit emits no item and sends no key
        self.world.tmux["ws-t-driver-g1"]["pane"] = pane("synth-code-mode-optin.txt")   # still showing
        st2, plan2, res2, _ = self.tick(L, st1, advance=60)
        self.assertEqual([x.kind for x in res2.actions], [])
        self.assertEqual(len(self.world.keys), 1)
        self.assertEqual(self.keys(res2, "startup_dialog"), ["startup_dialog:ws-t-driver-g1"])

    def test_a_crash_between_the_commit_and_the_key_never_sends_a_key(self):
        L, st = self.launched()
        st1, _, _, _ = self.tick(L, st, crash_at=1)
        self.assertEqual(self.world.keys, [])
        self.assertEqual(len(st1["sessions"]["driver"]["dialog_answers"]), 1)
        st2, _, res2, _ = self.tick(L, st1, advance=60)
        self.assertEqual((self.world.keys, res2.actions), ([], []))
        self.assertIn("still showing after its listed key", res2.conditions[0].detail)

    def test_the_key_is_not_sent_once_the_dialog_is_gone(self):
        L, st = self.launched()
        readings = edl.take_readings(L, st, self.world, self.now())
        res = edl.reconcile(L, st, readings, self.now())
        plan = edl.plan_dispatch(L, res.state, res, readings, self.now(), paths=self.paths, host="ws")
        self.world.tmux["ws-t-driver-g1"]["pane"] = typed_pane("the operator answered it and is typing")
        s = res.state
        edl.execute_dispatch(plan, s, self.world, lambda x: None)
        self.assertEqual(self.world.keys, [])
        # the guard refused and nothing was sent: the record is cleared so a later pass may answer once
        self.assertEqual(s["sessions"]["driver"]["dialog_answers"], [])

    def test_a_refused_key_is_answered_on_the_next_pass_and_only_once(self):
        L, st = self.launched()
        self.world.tmux["ws-t-driver-g1"]["display"]["pane_in_mode"] = "1"        # copy-mode at the send
        st1, _, _, commits = self.tick(L, st)
        self.assertEqual(self.world.keys, [])
        self.assertEqual(len(commits[0]["sessions"]["driver"]["dialog_answers"]), 1)    # recorded before
        self.assertEqual(st1["sessions"]["driver"]["dialog_answers"], [])               # cleared after the refusal
        self.world.tmux["ws-t-driver-g1"]["display"]["pane_in_mode"] = "0"
        st2, _, _, _ = self.tick(L, st1, advance=60)
        self.assertEqual(self.world.keys, [("ws-t-driver-g1", "Enter")])
        self.assertEqual(len(st2["sessions"]["driver"]["dialog_answers"]), 1)
        st3, _, _, _ = self.tick(L, st2, advance=60)
        self.assertEqual(len(self.world.keys), 1)

    def test_a_key_sent_before_a_crash_is_never_resent(self):
        L, st = self.launched()
        # killed as the result is committed: the key went out, its outcome is unknown
        st1, _, _, _ = self.tick(L, st, crash_at=2, crash_mode="before")
        self.assertEqual(len(self.world.keys), 1)
        self.assertEqual(len(st1["sessions"]["driver"]["dialog_answers"]), 1)
        self.world.tmux["ws-t-driver-g1"]["pane"] = pane("synth-code-mode-optin.txt")   # even if it shows again
        for _ in range(2):
            st1, _, _, _ = self.tick(L, st1, advance=60)
        self.assertEqual(len(self.world.keys), 1)

    def test_a_key_that_ran_and_failed_keeps_its_record_and_is_never_sent_again(self):
        L, st = self.launched()
        self.world.key_error = "tmux: send-keys failed\n"
        st1, _, _, _ = self.tick(L, st)
        self.assertEqual(self.world.keys, [("ws-t-driver-g1", "Enter")])
        answers = st1["sessions"]["driver"]["dialog_answers"]
        self.assertEqual(len(answers), 1)                  # the key may have reached the pane: stays recorded
        self.assertTrue(answers[0].get("not_sent"))
        self.assertNotIn("sent_at", answers[0])
        for _ in range(3):
            st1, _, _, _ = self.tick(L, st1, advance=60)
        self.assertEqual(len(self.world.keys), 1)          # and no second key on later passes
        self.assertEqual(len(st1["sessions"]["driver"]["dialog_answers"]), 1)

    def test_no_event_is_typed_while_a_dialog_shows(self):
        L, st = self.ledger()
        self.live(st, pane=pane("synth-code-mode-optin.txt"), launch_event="E-0")
        self.outbox(st, "E-0", steps=(), kinds=("alarm_cleared",), via="spawn")   # launch unconfirmed: captured
        st["next_event_n"] = 1
        self.world.events["ws-t-driver-g1"] = [ev("turn_start", "2026-11-24T09:00:01Z"),
                                               ev("stop", "2026-11-24T09:55:00Z", state="idle")]
        st2, plan, _, _ = self.tick(L, st)
        self.assertEqual(self.world.sends, [])
        self.assertEqual(self.entry(st2, "E-1")["last_deferral"]["why"], "a dialog shows")
        # and at the moment of sending, too
        L, st = self.ledger()
        self.world.handles = []
        self.live(st)
        readings = edl.take_readings(L, st, self.world, self.now())
        res = edl.reconcile(L, st, readings, self.now())
        sel = edl.select(L, res.state, self.now())
        plan = edl.plan_dispatch(L, res.state, res, readings, self.now(), paths=self.paths, selection=sel, host="ws")
        self.world.tmux["ws-t-driver-g1"]["pane"] = pane("synth-code-mode-optin.txt")
        outcomes = edl.execute_dispatch(plan, res.state, self.world, lambda x: None)
        self.assertEqual([o.deferred for o in outcomes], ["a dialog shows"])
        self.assertEqual(self.world.sends, [])


# -- keep warm (§11.5) ---------------------------------------------------------------

class TestKeepWarm(_Disp):
    def facts(self, age_min=45, ctx=100000, ttl=3600, **kw):
        f = edl.SessionFacts(key="driver", name="ws-t-driver-g1", alive=True, status=edl.ST_IDLE,
                             context_tokens=ctx, cache_left_s=ttl - age_min * 60, cache_ttl=ttl,
                             overage=ttl == 300)
        for k, v in kw.items():
            setattr(f, k, v)
        return f

    def warm_ledger(self, model="claude-opus-5-5", due="2026-11-24T14:00:00Z", **role):
        r = {"model": model, "effort": "high"}
        r.update(role)
        L, st = self.ledger([self.driver("A", not_before=due)], roles={"driver": r})
        st["sessions"]["driver"] = {"name": "ws-t-driver-g1", "gen": 1, "status": "live"}
        return L, st

    def check(self, L, st, facts):
        return edl.keep_warm_check(L, st, "driver", facts, self.now())

    def test_the_40_to_50_minute_window(self):
        L, st = self.warm_ledger()
        for age, ok in ((39, False), (40, True), (45, True), (50, True), (51, False)):
            with self.subTest(age=age):
                self.assertEqual(self.check(L, st, self.facts(age_min=age))[0], ok)

    def test_next_event_inside_and_outside_k_b_over_c(self):
        # Opus: k=40, B=40000, C=100000 ⇒ 16 h
        L, st = self.warm_ledger(due="2026-11-25T01:59:00Z")
        ok, why = self.check(L, st, self.facts())
        self.assertTrue(ok, why)
        self.assertIn("k=40", why)
        L, st = self.warm_ledger(due="2026-11-25T02:01:00Z")
        ok, why = self.check(L, st, self.facts())
        self.assertFalse(ok)
        self.assertIn("beyond k·B/C = 16h", why)
        # a bigger context shrinks the horizon: C=400000 ⇒ 4 h
        L, st = self.warm_ledger(due="2026-11-24T15:00:00Z")
        self.assertFalse(self.check(L, st, self.facts(ctx=400000))[0])
        # no scheduled event (a trigger is not predicted)
        L, st = self.ledger([self.driver("A", trigger="true")])
        st["sessions"]["driver"] = {"name": "n", "status": "live"}
        self.assertEqual(self.check(L, st, self.facts()), (False, "no scheduled event for the role"))

    def test_fable_doubles_k(self):
        L, st = self.warm_ledger(model="fable", due="2026-11-25T08:00:00Z")    # 22 h: within 32 h, beyond 16 h
        self.assertEqual(edl.price_class(L.roles["driver"]), "fable")
        self.assertEqual(edl.keep_warm_k(L.roles["driver"]), 80)
        ok, why = self.check(L, st, self.facts())
        self.assertTrue(ok, why)
        # fable mapped to an Opus id by the profile is Opus-priced
        L2, st2 = self.warm_ledger(model="fable", profile="claude", due="2026-11-25T08:00:00Z")
        self.assertEqual(L2.roles["driver"].expected_model_id, "claude-opus-5-5")
        self.assertEqual(edl.keep_warm_k(L2.roles["driver"]), 40)
        self.assertFalse(self.check(L2, st2, self.facts())[0])

    def test_window_opening_counts_as_the_next_event(self):
        L, st = self.ledger([self.driver("A", window="20:00-22:00Z")], roles={
            "driver": {"model": "claude-opus-5-5", "effort": "high"}})
        nxt = edl.next_scheduled_event(L, st, "driver", self.now())
        self.assertEqual(nxt, T("2026-11-24T20:00:00Z"))

    def test_overage_limits_and_keep_warm_false_suppress(self):
        L, st = self.warm_ledger()
        self.assertEqual(self.check(L, st, self.facts(ttl=300, age_min=1))[1], "overage (5m bucket)")
        self.assertEqual(self.check(L, st, self.facts(limited="claude-opus-5-5"))[1], "model limited (claude-opus-5-5)")
        st["limits"] = {"claude-opus-5-5": {"until": "2026-11-24T15:00:00Z"}}
        self.assertFalse(self.check(L, st, self.facts())[0])
        L, st = self.warm_ledger(keep_warm=False)
        self.assertEqual(self.check(L, st, self.facts()), (False, "keep_warm: false"))
        L, st = self.warm_ledger()
        self.assertEqual(self.check(L, st, self.facts(turn_open=True))[1], "not idle")

    def test_the_ping_goes_through_the_dispatch_once(self):
        L, st = self.ledger([self.driver("A", not_before="2026-11-24T14:00:00Z")])
        self.live(st, entries=[u("2026-11-24T09:15:00Z", "EVENT E-0: read /x"), usage_entry("2026-11-24T09:15:00Z")],
                  events=[ev("turn_start", "2026-11-24T09:15:00Z"), ev("stop", "2026-11-24T09:15:00Z")])
        self.world.reply["ws-t-driver-g1"] = None
        st1, plan, _, commits = self.tick(L, st)
        self.assertEqual(self.world.sends, [("ws-t-driver-g1", edl.PING_LINE)])
        self.assertEqual(commits[0]["sessions"]["driver"]["last_ping_at"], self.NOW)
        st2, _, _, _ = self.tick(L, st1, advance=60)
        self.assertEqual(len(self.world.sends), 1)


# -- crash safety (invariants 2 and 5) ---------------------------------------------------

class TestCrashSafety(_Disp):
    """Kill the pass at every commit (after it, and as it is written), then run the next passes:
    no event lands twice, no name is spawned twice, no inflight marker is lost."""

    def scenarios(self):
        def spawn_fresh(self_):
            L, st = self_.ledger()
            self_.world.reply["ws-t-driver-g1"] = "ADVANCED: A"
            return L, st

        def send_into_live(self_):
            L, st = self_.ledger()
            self_.live(st)
            self_.world.reply["ws-t-driver-g1"] = "ADVANCED: A"
            return L, st

        def retry_after_a_lost_send(self_):
            L, st = self_.ledger()
            self_.inflight(st, "A", event="E-1")
            self_.live(st)
            self_.outbox(st, "E-1", sends=["2026-11-24T09:56:00Z"], line="EVENT E-1: read /e1",
                         path=str(self_.paths.events / "E-1.md"))
            st["next_event_n"] = 2
            self_.world.reply["ws-t-driver-g1"] = "ADVANCED: A"
            return L, st

        def resume_dead(self_):
            L, st = self_.ledger()
            self_.inflight(st, "A", event="E-1")
            self_.live(st, alive=False, status="dead")
            st["next_event_n"] = 2
            self_.world.reply["ws-t-driver-g1"] = "ADVANCED: A"
            return L, st

        def rotation_spawn(self_):
            L, st = self_.ledger()
            hp = self_.tmp / "handoff.md"
            hp.write_text("# Handoff\n")
            name = "ws-t-driver-g1"
            entries = [u("2026-11-24T09:50:01.200Z", "ROTATE E-2: read /e2"), a("2026-11-24T09:55:00Z", f"HANDOFF: {hp}")]
            events = [ev("turn_start", "2026-11-24T09:50:01Z"),
                      ev("stop", "2026-11-24T09:55:00Z", state="idle", last_message=f"HANDOFF: {hp}")]
            _, h, _ = self_.live(st, entries=entries, events=events, rotation={
                "event": "E-2", "requested_at": "2026-11-24T09:50:00Z", "handoff_path": str(hp), "ticks": 0})
            h.update(last_message=f"HANDOFF: {hp}", stopped_at="2026-11-24T09:55:00Z")
            os.utime(str(hp), (self_.epoch("2026-11-24T09:54:00Z"),) * 2)
            self_.outbox(st, "E-2", steps=(), kinds=("rotate",), sends=["2026-11-24T09:50:00Z"])
            self_.inflight(st, "A", event="E-1", name=name)
            self_.outbox(st, "E-1", sends=[])
            st["steps"]["A"]["status"] = "inflight"
            st["next_event_n"] = 3
            self_.world.reply["ws-t-driver-g2"] = "ADVANCED: A"
            return L, st

        def dialog_answer(self_):
            L, st = self_.ledger([], startup_dialogs=[{"id": "code-mode", "match": CODE_MODE_MATCH, "key": "Enter"}])
            self_.outbox(st, "E-1", steps=(), kinds=("alarm_cleared",), via="spawn", line="EVENT E-1: read /x")
            self_.session(st, pane=pane("synth-code-mode-optin.txt"), launch_event="E-1",
                          entries=[u("2026-11-24T09:00:00Z", "(start)")])
            st["next_event_n"] = 2
            return L, st

        def dialog_refused(self_):
            L, st = dialog_answer(self_)
            w, real, left = self_.world, self_.world.tmux_display, [1]

            def display(name, fields):          # the first key guard sees copy-mode, later ones do not
                if tuple(fields) == ("pane_in_mode",) and left[0]:
                    left[0] -= 1
                    return {"pane_in_mode": "1"}
                return real(name, fields)
            w.tmux_display = display
            return L, st

        def reminder(self_):
            L, st = self_.ledger()
            self_.inflight(st, "A")
            entries, events = self_.answered_turn("E-1", "Rolled it, checking.")
            self_.live(st, entries=entries, events=events)
            self_.outbox(st, "E-1")
            st["next_event_n"] = 2
            self_.world.reply["ws-t-driver-g1"] = "ADVANCED: A"
            return L, st

        return [spawn_fresh, send_into_live, retry_after_a_lost_send, resume_dead, rotation_spawn,
                dialog_answer, dialog_refused, reminder]

    def fresh(self, i):
        self.tmp_i = self.tmp / f"run-{i}"
        (self.tmp_i / "t").mkdir(parents=True)
        self.tdir = self.tmp_i / "t"
        os.environ[edl.STATE_ROOT_ENV] = str(self.tmp_i / "state")
        self.paths = edl.run_paths("epic-t")
        self.world = DispatchWorld(self.tdir, now=T(self.NOW))

    def check_invariants(self, label, L, st, first_markers):
        w = self.world
        counted = {}
        for line in w.landed:
            m = re.match(r"^(EVENT|RESUME|ROTATE|VERDICT\?) (E-\d+)", line)
            if m:
                counted[(m.group(1), m.group(2))] = counted.get((m.group(1), m.group(2)), 0) + 1
        self.assertTrue(all(n == 1 for n in counted.values()), f"{label}: an event landed twice: {counted}")
        sent = {}
        for _, line in w.sends:
            m = re.match(r"^(EVENT|RESUME|ROTATE|VERDICT\?) (E-\d+)", line)
            if m:
                sent[m.group(2)] = sent.get(m.group(2), 0) + 1
        self.assertTrue(all(n <= 2 for n in sent.values()), f"{label}: more than two sends: {sent}")
        self.assertEqual(len(w.created), len(set(w.created)), f"{label}: a name spawned twice: {w.created}")
        self.assertLessEqual(len(w.keys), 1, f"{label}: a second dialog key")
        for sid in first_markers:
            rec = st["steps"][sid]
            self.assertTrue(rec.get("inflight") or rec.get("last_verdict"),
                            f"{label}: the marker of {sid} was lost: {rec}")
        for sid, rec in st["steps"].items():
            if rec.get("status") == "inflight":
                self.assertTrue(rec.get("inflight"), f"{label}: {sid} inflight without a marker")

    def run_scenario(self, i, build, crash_at, mode):
        self.fresh(i)
        L, st = build(self)
        first = [sid for sid, r in st["steps"].items() if r.get("inflight")]
        st, _, _, _ = self.tick(L, st, crash_at=crash_at, crash_mode=mode)
        first += [sid for sid, r in st["steps"].items() if r.get("inflight") and sid not in first]
        for _ in range(4):
            st, _, _, _ = self.tick(L, st, advance=600)
        self.check_invariants(f"{build.__name__} killed at commit {crash_at} ({mode})", L, st, first)
        return st

    def test_killed_between_every_commit_and_its_effect(self):
        i = 0
        for build in self.scenarios():
            self.fresh(i)
            i += 1
            L, st = build(self)
            _, plan, _, commits = self.tick(L, st)
            self.assertTrue(any(op.effect.kind != edl.EFFECT_NONE for op in plan.ops), build.__name__)
            for k in range(1, len(commits) + 1):
                for mode in ("after", "before"):
                    with self.subTest(scenario=build.__name__, commit=k, mode=mode):
                        self.run_scenario(i, build, k, mode)
                        i += 1

    def test_the_uncrashed_scenarios_make_progress(self):
        expect = {"spawn_fresh": "verifying", "send_into_live": "verifying", "retry_after_a_lost_send": "verifying",
                  "resume_dead": "verifying", "rotation_spawn": "verifying"}
        for i, build in enumerate(self.scenarios()):
            with self.subTest(scenario=build.__name__):
                st = self.run_scenario(100 + i, build, None, "after")
                if build.__name__ in expect:
                    self.assertEqual(st["steps"]["A"]["status"], expect[build.__name__])
                if build.__name__ == "dialog_refused":      # refused once, then answered once
                    self.assertEqual(len(self.world.keys), 1)


class TestDispatchContract(_Disp):
    def test_plan_is_pure_and_the_input_state_untouched(self):
        L, st = self.ledger()
        self.live(st)
        readings = edl.take_readings(L, st, self.world, self.now())
        res = edl.reconcile(L, st, readings, self.now())
        sel = edl.select(L, res.state, self.now())
        before = copy.deepcopy(res.state)
        p1 = edl.plan_dispatch(L, res.state, res, readings, self.now(), paths=self.paths, selection=sel, host="ws")
        p2 = edl.plan_dispatch(L, res.state, res, readings, self.now(), paths=self.paths, selection=sel, host="ws")
        self.assertEqual(res.state, before)
        self.assertEqual([op.label for op in p1.ops], [op.label for op in p2.ops])
        self.assertEqual([op.effect for op in p1.ops], [op.effect for op in p2.ops])
        self.assertEqual(p1.preview, p2.preview)
        self.assertEqual((self.world.sends, self.world.spawns), ([], []))
        self.assertFalse((self.paths.events).exists())          # files are written by the executor only

    def test_paused_writes_events_and_starts_nothing(self):
        L, st = self.ledger([])
        self.live(st)
        item = edl.EventItem(edl.ITEM_ALARM_CLEARED, "driver", None, {"alarm": "x", "subject": "run"})
        st2, plan, _, _ = self.tick(L, st, items=[item], paused=True)
        self.assertEqual([op.effect.kind for op in plan.ops], [edl.EFFECT_NONE])
        self.assertEqual(self.entry(st2, "E-1")["sends"], [])
        self.assertEqual(self.world.sends, [])

    def test_world_effect_methods_fail_loudly_on_the_base_class(self):
        w = edl.World()
        for call in (lambda: w.spawn(["spawn"]), lambda: w.resume(["resume"]), lambda: w.reap(["rm"]),
                     lambda: w.amux_send("n", "x"), lambda: w.send_keys("n", "Enter")):
            with self.assertRaises(NotImplementedError):
                call()

    def test_real_world_effects_build_their_commands(self):
        rw = edl.RealWorld()
        calls = []

        def fake_run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")
        with mock.patch.object(edl.subprocess, "run", side_effect=fake_run), \
                mock.patch.object(edl.shutil, "which", return_value="/bin/amux-spawn"):
            self.assertTrue(rw.spawn(["spawn", "s"]).ok)
            rw.resume(["resume", "n", "--json", "--", "RESUME E-1: read /p"])
            rw.reap(["rm", "n"])
            rw.amux_send("n", "EVENT E-1: read /p")
            rw.send_keys("n", "Enter")
            self.assertIsNone(rw.send_keys("n", "C-c").rc)          # only Enter / Escape
        self.assertEqual(calls, [["/bin/amux-spawn", "spawn", "s"],
                                 ["/bin/amux-spawn", "resume", "n", "--json", "--", "RESUME E-1: read /p"],
                                 ["/bin/amux-spawn", "rm", "n"],
                                 ["amux", "send", "n", "EVENT E-1: read /p"],
                                 ["tmux", "send-keys", "-t", "=amux-n:", "Enter"]])
        with mock.patch.object(edl.subprocess, "run", side_effect=OSError("gone")):
            self.assertIsNone(rw.amux_send("n", "x").rc)

    def test_the_resume_form_matches_amux_spawns_parser(self):
        """`amux-spawn resume <name> --json -- "<prompt>"`: the prompt is split off at the first `--`."""
        import importlib.machinery
        import importlib.util
        path = str(_REPO / ".claude" / "bin" / "amux-spawn")
        loader = importlib.machinery.SourceFileLoader("amux_spawn_cli", path)
        spec = importlib.util.spec_from_loader("amux_spawn_cli", loader)
        cli = importlib.util.module_from_spec(spec)
        loader.exec_module(cli)
        args = edl.resume_args("n", "RESUME E-4: read /s/events/E-4.md")
        before, prompt = cli._split_prompt(args[1:])
        self.assertEqual((before, prompt), (["n", "--json"], "RESUME E-4: read /s/events/E-4.md"))
        ns, rest = cli.build_parser().parse_known_args(["resume", *before])
        self.assertEqual((ns.handle, ns.json, rest), ("n", True, []))
        spawn = edl.spawn_args(*self.ledger()[:1], "driver", 1, str(self.ws), "EVENT E-1: read /p")
        before, prompt = cli._split_prompt(spawn[1:])
        ns, claude_flags = cli.build_parser().parse_known_args(["spawn", *before])
        self.assertEqual((ns.suffix, ns.dir, ns.yolo, ns.run_id, ns.detach, prompt),
                         ("t-driver-g1", str(self.ws), True, "epic-t", True, "EVENT E-1: read /p"))
        self.assertEqual(claude_flags, ["--model=claude-opus-5-5", "--effort=high"])


# =============================================================================
# 50-07 — worker waves: worktrees, integration branch, the sha check (§14)
# =============================================================================
#
# Real git repositories in temp dirs, no network: a bare "remote", a primary clone that is left dirty and on
# another branch, and a second clone with no remote. HOME and GIT_CONFIG_GLOBAL point into the temp dir so
# the user's git config and hooks never leak in. Every test checks the primary checkouts are unchanged.

class _GitRepos(_Base):
    def setUp(self):
        super().setUp()
        home = self.tmp / "home"
        home.mkdir()
        cfg = home / "gitconfig"
        cfg.write_text("[user]\n\tname = T\n\temail = t@example.invalid\n[init]\n\tdefaultBranch = main\n"
                       "[commit]\n\tgpgsign = false\n")
        self._genv = mock.patch.dict(os.environ, {"HOME": str(home), "GIT_CONFIG_GLOBAL": str(cfg),
                                                  "GIT_CONFIG_NOSYSTEM": "1"})
        self._genv.start()
        self.gw = RecordingRealWorld()
        self.remote = self.tmp / "remote.git"
        self.primary = self.tmp / "primary"
        self.solo = self.tmp / "solo"
        self.g(self.tmp, "init", "--bare", "-b", "main", str(self.remote))
        self.g(self.tmp, "clone", "-q", str(self.remote), str(self.primary))
        self.commit(self.primary, "a.txt", "one\n", "first")
        self.g(self.primary, "push", "-q", "origin", "main")
        self.g(self.primary, "tag", "v1")
        self.commit(self.primary, "a.txt", "two\n", "second")
        self.second_sha = self.g(self.primary, "rev-parse", "HEAD")
        self.g(self.primary, "checkout", "-q", "-b", "operator-work")
        (self.primary / "a.txt").write_text("dirty\n")
        (self.primary / "untracked.txt").write_text("u\n")
        self.g(self.tmp, "init", "-q", str(self.solo))
        self.commit(self.solo, "s.txt", "s\n", "solo first")
        self.test_made_refs = set()
        self.before = {r: self.snapshot(r) for r in (self.primary, self.solo)}

    # the only things the driver may add to an operator's repo: its own branches and worktrees
    expects_refusals = False
    _ALLOWED_NEW_REF = re.compile(r"^refs/heads/(epic/[^/]+|epic-step/[^/]+/[^/]+)$")
    _ALLOWED_NEW_BRANCH = re.compile(r"^refs/heads/(epic/[^/]+|epic-step/[^/]+/[^/]+)$")

    def assert_unchanged(self, repo, snap):
        now = self.snapshot(repo)
        name = Path(repo).name
        for i, what in enumerate(("status", "HEAD", "symbolic-ref", "working files")):
            self.assertEqual(now[i], snap[i], f"{name}: {what} changed")
        old_refs, new_refs = snap[4], now[4]
        for ref, sha in old_refs.items():
            self.assertEqual(new_refs.get(ref), sha, f"{name}: ref {ref} moved or was deleted")
        for ref in set(new_refs) - set(old_refs) - self.test_made_refs:
            self.assertRegex(ref, self._ALLOWED_NEW_REF, f"{name}: unexpected new ref {ref}")
        old_wt, new_wt = snap[5], now[5]
        for path, info in old_wt.items():
            self.assertEqual(new_wt.get(path), info, f"{name}: worktree {path} changed or was removed")
        for path in set(new_wt) - set(old_wt):
            if new_wt[path][1] in self.test_made_refs:    # the driver's worktree, switched by the test itself
                continue
            self.assertRegex(new_wt[path][1] or "", self._ALLOWED_NEW_BRANCH,
                             f"{name}: unexpected new worktree {path}")

    def tearDown(self):
        try:
            if not self.expects_refusals:
                self.assertEqual(self.gw.refused, [], "the module attempted a git form the allow list refuses")
            for r, snap in self.before.items():
                self.assert_unchanged(r, snap)
        finally:
            self._genv.stop()
            super().tearDown()

    def g(self, cwd, *args):
        r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60,
                           env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
        self.assertEqual(r.returncode, 0, f"git {args}: {r.stderr}")
        return r.stdout.strip()

    def commit(self, repo, name, text, msg):
        Path(repo, name).write_text(text)
        self.g(repo, "add", name)
        self.g(repo, "commit", "-q", "-m", msg)

    def snapshot(self, repo):
        """Status, HEAD, branch, working files, every ref (name and sha) and the worktree list."""
        files = {n: (Path(repo) / n).read_bytes() for n in sorted(os.listdir(repo)) if n != ".git"}
        refs = {}
        for line in self.g(repo, "for-each-ref", "--format=%(refname) %(objectname)").splitlines():
            name, sha = line.split(" ")
            refs[name] = sha
        worktrees, cur = {}, None
        for line in self.g(repo, "worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                cur = worktrees[os.path.realpath(line[9:])] = [None, None]
            elif cur is not None and line.startswith("HEAD "):
                cur[0] = line[5:]
            elif cur is not None and line.startswith("branch "):
                cur[1] = line[7:]
        worktrees = {k: tuple(v) for k, v in worktrees.items()}
        return (self.g(repo, "status", "--porcelain=v1", "--untracked-files=all"), self.g(repo, "rev-parse", "HEAD"),
                self.g(repo, "symbolic-ref", "-q", "HEAD"), files, refs, worktrees)

    def repos(self, *which, base="main"):
        paths = {"primary": self.primary, "solo": self.solo}
        return [{"path": str(paths[w]), "base_ref": base if w == "primary" else "main", "name": w} for w in which]

    def wledger(self, *which, base="main", epic="e1", **top):
        d = self.ledger_dict([self.step("W", role={"worker": {
            "implementer": "impl", "reviewer": "rev", "repos": self.repos(*which, base=base)}})],
            **{"epic": epic, **top})
        d["roles"]["impl"] = {"model": "claude-opus-5-5", "effort": "high"}
        d["roles"]["rev"] = {"model": "claude-opus-5-5", "effort": "high"}
        L = self.load(d, self.gw)
        return L, edl.new_state(L, T("2026-11-24T00:00:00Z"))


class TestBaseRef(_GitRepos):
    def test_resolves_by_branch_tag_and_sha_whatever_the_primary_checkout_looks_like(self):
        repo = edl.WorkerRepo(str(self.primary), "main", "primary")
        for ref, want in (("main", self.second_sha), ("v1", self.g(self.primary, "rev-parse", "v1^{commit}")),
                          (self.second_sha, self.second_sha), (self.second_sha[:10], self.second_sha)):
            repo.base_ref = ref
            self.assertEqual(edl.resolve_base_ref(self.gw, repo), (want, None), ref)
        self.assertEqual(self.g(self.primary, "symbolic-ref", "HEAD"), "refs/heads/operator-work")

    def test_an_unknown_ref_is_refused(self):
        sha, why = edl.resolve_base_ref(self.gw, edl.WorkerRepo(str(self.primary), "nope", "primary"))
        self.assertIsNone(sha)
        self.assertIn("'nope' does not resolve", why)
        errors, _ = self.issues(self.ledger_dict([self.step("W", role={"worker": {
            "implementer": "impl", "reviewer": "rev", "repos": self.repos("primary", base="nope")}})],
            roles={"driver": {"model": "m", "effort": "high"}, "impl": {"model": "m", "effort": "high"},
                   "rev": {"model": "m", "effort": "high"}}), self.gw)
        self.assertIssue(errors, edl.E_REPOS, "repos[0].base_ref", "'nope'")

    def test_repo_problems_checks_paths_names_and_work_trees(self):
        repos = [edl.WorkerRepo(str(self.primary), "main", "a"),
                 edl.WorkerRepo(str(self.primary) + "/../primary", "main", "a"),
                 edl.WorkerRepo("rel", "main", "r"),
                 edl.WorkerRepo(str(self.remote), "main", "bare")]
        got = edl.repo_problems(repos, lambda args, path: self.gw.git(list(args), path))
        self.assertEqual([(i, f) for i, f, _ in got], [(1, "path"), (1, "name"), (2, "path"), (3, "path")])
        self.assertIn("not a git work tree", got[3][2])


class TestIntegrationWorktree(_GitRepos):
    def test_created_from_base_ref_then_reused(self):
        L, st = self.wledger("primary")
        repo = L.step("W").worker.repos[0]
        first = edl.ensure_integration(L, self.gw, repo)
        path = os.path.join(L.worktree_root, "integration", "primary")
        self.assertEqual((first.path, first.created, first.refused), (path, True, None))
        self.assertEqual(self.g(path, "symbolic-ref", "HEAD"), "refs/heads/epic/e1")
        self.assertEqual(self.g(path, "rev-parse", "HEAD"), self.second_sha)
        again = edl.ensure_integration(L, self.gw, repo)
        self.assertEqual((again.path, again.created, again.refused), (path, False, None))

    def test_the_branch_is_created_from_the_resolved_base_not_the_operators_branch(self):
        L, st = self.wledger("primary", base="v1")
        edl.ensure_integration(L, self.gw, L.step("W").worker.repos[0])
        self.assertEqual(self.g(os.path.join(L.worktree_root, "integration", "primary"), "log", "-1", "--format=%s"),
                         "first")

    def test_an_existing_branch_is_attached(self):
        self.g(self.primary, "branch", "epic/e1", "v1")
        L, st = self.wledger("primary")
        res = edl.ensure_integration(L, self.gw, L.step("W").worker.repos[0])
        self.assertTrue(res.created and not res.refused)
        self.assertEqual(self.g(res.path, "log", "-1", "--format=%s"), "first")

    def test_refused_when_the_operator_has_the_branch_checked_out(self):
        self.g(self.primary, "checkout", "-q", "-b", "epic/e1")
        self.before[self.primary] = self.snapshot(self.primary)
        L, st = self.wledger("primary")
        res = edl.ensure_integration(L, self.gw, L.step("W").worker.repos[0])
        self.assertIn("epic/e1 is checked out in", res.refused or "")
        self.assertFalse(os.path.exists(os.path.join(L.worktree_root, "integration", "primary")))
        self.assertEqual(self.g(self.primary, "symbolic-ref", "HEAD"), "refs/heads/epic/e1")

    def test_refused_when_dirty_or_on_another_branch_and_changes_are_kept(self):
        L, st = self.wledger("primary")
        repo = L.step("W").worker.repos[0]
        path = edl.ensure_integration(L, self.gw, repo).path
        Path(path, "work.txt").write_text("mine\n")
        dirty = edl.ensure_integration(L, self.gw, repo)
        self.assertIn("is dirty", dirty.refused or "")
        self.assertEqual(Path(path, "work.txt").read_text(), "mine\n")
        os.unlink(os.path.join(path, "work.txt"))
        self.g(path, "switch", "-q", "-c", "side")
        self.test_made_refs.add("refs/heads/side")      # the test's own branch, made in the driver's worktree
        other = edl.ensure_integration(L, self.gw, repo)
        self.assertIn("not epic/e1", other.refused or "")

    def test_an_unresolvable_base_is_refused_and_nothing_is_created(self):
        L, st = self.wledger("primary")
        repo = L.step("W").worker.repos[0]
        repo.base_ref = "gone"
        res = edl.ensure_integration(L, self.gw, repo)
        self.assertIn("does not resolve", res.refused or "")
        self.assertFalse(os.path.exists(L.worktree_root))

    def test_a_path_that_is_not_a_worktree_is_refused(self):
        L, st = self.wledger("primary")
        path = os.path.join(L.worktree_root, "integration", "primary")
        os.makedirs(path)
        res = edl.ensure_integration(L, self.gw, L.step("W").worker.repos[0])
        self.assertIn("is not a worktree", res.refused or "")


class TestStepWorktrees(_GitRepos):
    def test_one_repo_the_worktree_is_the_session_dir(self):
        L, st = self.wledger("primary")
        res = edl.ensure_step_worktrees(L, st, self.gw, "W")
        want = os.path.join(L.worktree_root, "e1-w")
        self.assertTrue(res.ok, res.refused)
        self.assertEqual((res.dir, res.repos), (want, {"primary": want}))
        self.assertEqual(os.path.basename(res.dir), edl.worker_dir_name("e1", "W"))
        self.assertEqual(self.g(want, "symbolic-ref", "HEAD"), "refs/heads/epic-step/e1/W")
        self.assertEqual(self.g(want, "rev-parse", "HEAD"), self.second_sha)
        self.assertEqual(edl.worker_dirs(st), {"W": want})
        self.assertEqual(st["worktrees"]["integration"], {"primary": os.path.join(L.worktree_root, "integration", "primary")})
        self.assertEqual(edl.worktree_conditions(L, st), [])

    def test_two_repos_get_a_wave_directory(self):
        L, st = self.wledger("primary", "solo")
        res = edl.ensure_step_worktrees(L, st, self.gw, "W")
        wave = os.path.join(L.worktree_root, "e1-w")
        self.assertTrue(res.ok, res.refused)
        self.assertEqual(res.dir, wave)
        self.assertEqual(res.repos, {"primary": os.path.join(wave, "primary"), "solo": os.path.join(wave, "solo")})
        self.assertEqual(os.path.basename(wave), edl.worker_dir_name("e1", "W"))
        for name, path in res.repos.items():
            self.assertEqual(self.g(path, "symbolic-ref", "HEAD"), "refs/heads/epic-step/e1/W", name)
        self.assertFalse(os.path.exists(os.path.join(wave, ".git")))

    def test_the_step_branch_starts_at_the_integration_tip(self):
        L, st = self.wledger("primary")
        edl.ensure_step_worktrees(L, st, self.gw, "W")
        integ = os.path.join(L.worktree_root, "integration", "primary")
        self.commit(integ, "i.txt", "i\n", "integration moved")
        tip = self.g(integ, "rev-parse", "HEAD")
        L2, st2 = self.wledger("primary", epic="e1")
        # a second step would start from the new tip
        d = self.ledger_dict([self.step("W", role={"worker": {"implementer": "impl", "reviewer": "rev",
                                                              "repos": self.repos("primary")}}),
                              self.step("X", role={"worker": {"implementer": "impl", "reviewer": "rev",
                                                              "repos": self.repos("primary")}})], epic="e1")
        d["roles"]["impl"] = d["roles"]["rev"] = {"model": "claude-opus-5-5", "effort": "high"}
        L3 = self.load(d, self.gw)
        res = edl.ensure_step_worktrees(L3, st2, self.gw, "X")
        self.assertEqual(self.g(res.dir, "rev-parse", "HEAD"), tip)

    def test_recreation_reuses_a_dirty_worktree_without_losing_changes(self):
        L, st = self.wledger("primary")
        first = edl.ensure_step_worktrees(L, st, self.gw, "W")
        Path(first.dir, "a.txt").write_text("in progress\n")
        Path(first.dir, "new.txt").write_text("new\n")
        status = self.g(first.dir, "status", "--porcelain")
        again = edl.ensure_step_worktrees(L, st, self.gw, "W")
        self.assertTrue(again.ok and not again.created, again.refused)
        self.assertEqual(self.g(first.dir, "status", "--porcelain"), status)
        self.assertEqual(Path(first.dir, "a.txt").read_text(), "in progress\n")
        # a fresh state (the tick lost it) finds the same worktree
        L2, st2 = self.wledger("primary")
        self.assertTrue(edl.ensure_step_worktrees(L2, st2, self.gw, "W").ok)
        self.assertEqual(Path(first.dir, "new.txt").read_text(), "new\n")

    def test_a_step_worktree_on_another_branch_is_refused_and_left_alone(self):
        L, st = self.wledger("primary")
        res = edl.ensure_step_worktrees(L, st, self.gw, "W")
        self.g(res.dir, "switch", "-q", "-c", "elsewhere")
        self.test_made_refs.add("refs/heads/elsewhere")
        again = edl.ensure_step_worktrees(L, st, self.gw, "W")
        self.assertIn("not epic-step/e1/W", again.refused or "")
        self.assertEqual(self.g(res.dir, "symbolic-ref", "HEAD"), "refs/heads/elsewhere")
        self.assertEqual(edl.worker_dirs(st), {})
        cond = edl.worktree_conditions(L, st)
        self.assertEqual([(c.id, c.subject) for c in cond], [("worktree_refused", "W")])
        self.assertIn("primary:", cond[0].detail)
        self.g(res.dir, "switch", "-q", "epic-step/e1/W")
        self.assertTrue(edl.ensure_step_worktrees(L, st, self.gw, "W").ok)
        self.assertEqual(edl.worktree_conditions(L, st), [])

    def test_the_dir_feeds_50_06s_spawn(self):
        L, st = self.wledger("primary", "solo")
        dirs = edl.prepare_worker_steps(L, st, self.gw, ["W"])
        self.assertEqual(edl.session_dir_for(L, "impl:W", dirs), os.path.join(L.worktree_root, "e1-w"))
        args = edl.spawn_args(L, "impl:W", 1, edl.session_dir_for(L, "impl:W", dirs), "EVENT E-1: read /p")
        self.assertEqual(args[args.index("--dir") + 1], dirs["W"])
        self.assertEqual(edl.session_name_for(L, "impl:W", 1, dirs), "e1-w-e1-impl-w-g1")

    def test_a_refused_integration_leaves_the_step_undispatchable(self):
        self.g(self.primary, "checkout", "-q", "-b", "epic/e1")
        self.before[self.primary] = self.snapshot(self.primary)
        L, st = self.wledger("primary")
        dirs = edl.prepare_worker_steps(L, st, self.gw, ["W"])
        self.assertEqual(dirs, {})
        self.assertEqual([c.id for c in edl.worktree_conditions(L, st)], ["worktree_refused"])

    def test_a_repo_without_a_remote_works_end_to_end(self):
        L, st = self.wledger("solo")
        res = edl.ensure_step_worktrees(L, st, self.gw, "W")
        self.assertTrue(res.ok, res.refused)
        self.assertEqual(self.g(self.solo, "remote"), "")
        self.commit(res.dir, "w.txt", "w\n", "work")
        sha = self.g(res.dir, "rev-parse", "HEAD")
        integ = st["worktrees"]["integration"]["solo"]
        self.g(integ, "merge", "--no-ff", "-q", "-m", "merge W", "epic-step/e1/W")
        merged = self.g(integ, "rev-parse", "HEAD")
        self.assertTrue(edl.sha_check(L, self.gw, "W", {"": merged}).passed)

    def test_a_worktree_root_defaults_under_the_state_dir(self):
        L, st = self.wledger("primary")
        self.assertEqual(L.worktree_root, str(edl.state_dir("epic-e1") / "worktrees"))

    def test_the_driver_workspace_and_primary_status_are_not_touched_by_the_wave(self):
        L, st = self.wledger("primary", "solo")
        edl.prepare_worker_steps(L, st, self.gw, ["W"])
        self.assertEqual(self.g(self.primary, "status", "--porcelain").splitlines(),
                         ["M a.txt", "?? untracked.txt"])


class TestShaCheck(_GitRepos):
    def setUp(self):
        super().setUp()
        self.L, self.st = self.wledger("primary", "solo")
        self.res = edl.ensure_step_worktrees(self.L, self.st, self.gw, "W")
        self.assertTrue(self.res.ok, self.res.refused)
        self.on, self.off = {}, {}
        for name in ("primary", "solo"):
            wt = self.res.repos[name]
            self.commit(wt, f"{name}.txt", "x\n", f"work {name}")
            self.off[name] = self.g(wt, "rev-parse", "HEAD")
            integ = self.st["worktrees"]["integration"][name]
            self.g(integ, "merge", "--no-ff", "-q", "-m", f"merge {name}", "epic-step/e1/W")
            self.on[name] = self.g(integ, "rev-parse", "HEAD")
        self.commit(self.res.repos["primary"], "late.txt", "x\n", "not merged")
        self.stray = self.g(self.res.repos["primary"], "rev-parse", "HEAD")

    def test_on_the_branch_passes(self):
        res = edl.sha_check(self.L, self.gw, "W", self.on)
        self.assertTrue(res.passed, res.detail)
        self.assertEqual(res.status, edl.SHA_PASS)
        # the step commit (an ancestor of the merge) passes too
        self.assertTrue(edl.sha_check(self.L, self.gw, "W", self.off).passed)

    def test_a_short_sha_passes(self):
        self.assertTrue(edl.sha_check(self.L, self.gw, "W", {n: s[:9] for n, s in self.on.items()}).passed)

    def test_a_sha_not_on_the_branch_fails_for_that_repo_only(self):
        res = edl.sha_check(self.L, self.gw, "W", {"primary": self.stray, "solo": self.on["solo"]})
        self.assertFalse(res.passed)
        self.assertEqual([(r.repo, r.status) for r in res.repos],
                         [("primary", edl.SHA_NOT_ANCESTOR), ("solo", edl.SHA_PASS)])
        self.assertEqual(res.status, edl.SHA_NOT_ANCESTOR)

    def test_a_missing_repo_sha_is_invalid(self):
        res = edl.sha_check(self.L, self.gw, "W", {"primary": self.on["primary"]})
        self.assertEqual([(r.repo, r.status) for r in res.repos],
                         [("primary", edl.SHA_PASS), ("solo", edl.SHA_INVALID)])
        self.assertEqual(res.status, edl.SHA_INVALID)
        self.assertEqual(edl.sha_check(self.L, self.gw, "W", None).status, edl.SHA_INVALID)

    def test_a_malformed_sha_is_invalid_and_never_reaches_git(self):
        calls = []
        real = self.gw.git
        with mock.patch.object(self.gw, "git", side_effect=lambda a, c, t=30.0: calls.append(a) or real(a, c, t)):
            res = edl.sha_check(self.L, self.gw, "W", {"primary": "--all", "solo": "XYZ"})
        self.assertEqual([r.status for r in res.repos], [edl.SHA_INVALID, edl.SHA_INVALID])
        self.assertEqual(calls, [])

    def test_an_unknown_sha_is_a_git_error_not_a_pass(self):
        res = edl.sha_check(self.L, self.gw, "W", {"primary": "0" * 40, "solo": self.on["solo"]})
        self.assertEqual(res.repos[0].status, edl.SHA_ERROR)
        self.assertFalse(res.passed)

    def test_the_check_runs_in_the_integration_worktree_per_repo(self):
        calls = []
        real = self.gw.git
        with mock.patch.object(self.gw, "git", side_effect=lambda a, c, t=30.0: calls.append((a, c)) or real(a, c, t)):
            edl.sha_check(self.L, self.gw, "W", self.on)
        root = self.L.worktree_root
        self.assertEqual(calls, [
            (["merge-base", "--is-ancestor", "--end-of-options", self.on["primary"], "refs/heads/epic/e1"],
             f"{root}/integration/primary"),
            (["merge-base", "--is-ancestor", "--end-of-options", self.on["solo"], "refs/heads/epic/e1"],
             f"{root}/integration/solo")])

    def test_a_single_repo_step_accepts_a_bare_sha(self):
        L, st = self.wledger("solo", epic="e2")
        res = edl.ensure_step_worktrees(L, st, self.gw, "W")
        self.assertTrue(res.ok, res.refused)
        integ = st["worktrees"]["integration"]["solo"]
        self.assertTrue(edl.sha_check(L, self.gw, "W", {"": self.g(integ, "rev-parse", "HEAD")}).passed)


class TestShaGate(_GitRepos):
    def setUp(self):
        super().setUp()
        self.L, self.st = self.wledger("solo", epic="e3", )
        self.assertTrue(edl.ensure_step_worktrees(self.L, self.st, self.gw, "W").ok)
        self.integ = self.st["worktrees"]["integration"]["solo"]
        self.tip = self.g(self.integ, "rev-parse", "HEAD")
        self.now = T("2026-11-24T01:00:00Z")

    def verifying(self, shas):
        rec = self.st["steps"]["W"]
        rec.update(status="verifying", shas=shas)
        return rec

    def test_a_worker_check_waits_for_the_sha_check_and_the_check_runs_after_it(self):
        d = self.ledger_dict([self.step("W", role={"worker": {"implementer": "impl", "reviewer": "rev",
                                                              "repos": self.repos("solo")}},
                                        check="true")], epic="e3")
        d["roles"]["impl"] = d["roles"]["rev"] = {"model": "claude-opus-5-5", "effort": "high"}
        L = self.load(d, self.gw)
        rec = self.verifying({"": self.tip})
        self.assertEqual(edl.eligible_checks(L, self.st, self.now), [])
        out = edl.run_sha_checks(L, self.st, self.gw, self.now)
        self.assertTrue(out[0].passed)
        self.assertEqual(rec["status"], "verifying")
        self.assertEqual([j.step_id for j in edl.eligible_checks(L, self.st, self.now)], ["W"])
        # a newer ADVANCED closes the gate again
        rec["shas"] = {"": "0" * 40}
        self.assertEqual(edl.eligible_checks(L, self.st, self.now), [])

    def test_no_own_check_the_sha_check_finishes_the_step(self):
        rec = self.verifying({"": self.tip})
        edl.run_sha_checks(self.L, self.st, self.gw, self.now)
        self.assertEqual((rec["status"], rec["by"]), ("done", "check"))
        self.assertTrue(rec["sha_check"]["passed"])

    def test_a_failing_sha_check_changes_nothing_but_the_record(self):
        rec = self.verifying({"": self.g(self.primary, "rev-parse", "operator-work")})
        out = edl.run_sha_checks(self.L, self.st, self.gw, self.now)
        self.assertFalse(out[0].passed)
        self.assertEqual(rec["status"], "verifying")
        self.assertEqual(rec["sha_check"]["status"], edl.SHA_ERROR)   # not in solo at all
        self.assertFalse(rec["sha_check"]["passed"])

    def test_other_statuses_and_non_worker_steps_are_skipped(self):
        self.st["steps"]["W"].update(status="inflight", shas={"": self.tip})
        self.assertEqual(edl.run_sha_checks(self.L, self.st, self.gw, self.now), [])


class TestWorktreeDiscipline(unittest.TestCase):
    SRC = (_REPO / ".claude" / "hooks" / "epic_driver_lib.py").read_text()

    def code(self):
        return re.sub(r"#[^\n]*", "", re.sub(r'''"""(.*?)"""''', "", self.SRC, flags=re.S))

    def test_no_git_argv_can_reset_clean_stash_force_remove_delete_or_push(self):
        whole = self.code()
        # the 50-07 section (50-08's, after it, runs no git and has its own contract test)
        code = whole[whole.index("ALARM_WORKTREE_REFUSED ="):whole.index("EXIT_OK = 0")]
        self.assertIn("def ensure_integration", code)
        for verb in ("reset", "clean", "stash", "push", "checkout", "restore", "rebase", "switch", "remove",
                     "prune", "gc", "-D", "-d", "-f", "-F", "-B", "-M", "-m", "-c", "--force", "--force-with-lease", "--hard",
                     "--mirror", "--orphan", "--detach", "update-ref"):
            self.assertNotRegex(code, rf'["\']{re.escape(verb)}["\']', verb)
        # every literal verb handed to the git runner is on the allow list
        verbs = set()
        for m in re.finditer(r"(?:world\.git|git)\(\s*[\[(]\s*\"([a-z-]+)\"", whole):
            verbs.add(m.group(1))
        self.assertEqual(verbs, {"rev-parse", "check-ref-format", "worktree", "status", "merge-base"})
        sub = set(re.findall(r'"worktree",\s*"([a-z-]+)"', whole))
        self.assertEqual(sub, {"list", "add"})

    def test_git_refusal_unit(self):
        ok = [["rev-parse", "--is-inside-work-tree"], ["rev-parse", "--verify", "--quiet", "--end-of-options", "x^{commit}"],
              ["check-ref-format", "--branch", "epic/e"], ["worktree", "list", "--porcelain"], ["status", "--porcelain"],
              ["worktree", "add", "-b", "epic/e", "--end-of-options", "/p", "abc1234"],
              ["worktree", "add", "--end-of-options", "/p", "epic/e"],
              ["merge-base", "--is-ancestor", "--end-of-options", "abc1234", "refs/heads/epic/e"]]
        for args in ok:
            self.assertIsNone(edl.git_refusal(args), args)
        for args in ([], "status", ["worktree"], ["worktree", "prune"], ["-c", "a=b", "status"], ["-C", "/", "status"],
                     ["status", "-c", "a=b"], ["status", "--porcelain", "--end-of-options", "-x"],
                     ["rev-parse", "--verify", "--end-of-options", "--abbrev-ref=3"], ["status", 1]):
            self.assertTrue(edl.git_refusal(args), args)

    def test_world_git_discipline(self):
        w = edl.RealWorld()
        seen = {}

        def fake_run(argv, **kw):
            seen.update(argv=argv, **kw)
            return subprocess.CompletedProcess(argv, 0, "", "")
        with mock.patch.object(edl.subprocess, "run", fake_run), \
                mock.patch.dict(os.environ, {"GIT_DIR": "/elsewhere"}):
            w.git(["status"], "/x", timeout_s=7)
        self.assertEqual(seen["argv"], ["git", "-c", "core.hooksPath=/dev/null", "-C", "/x", "status"])
        self.assertEqual(seen["timeout"], 7)
        self.assertEqual(seen["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertNotIn("GIT_DIR", seen["env"])
        with mock.patch.object(edl.subprocess, "run", side_effect=subprocess.TimeoutExpired("git", 1)):
            r = w.git(["status"], "/x", timeout_s=1)
        self.assertIsNone(r.rc)
        self.assertIn("timed out", r.error)


class TestGitAllowList(_GitRepos):
    """Behaviour: every forbidden git form is refused by RealWorld.git (and the fake) without running git."""

    expects_refusals = True

    def forbidden(self):
        wt = str(self.tmp / "wt-x")
        sha = self.second_sha
        return [
            ["reset", "--hard"], ["reset", "--hard", "HEAD~1"], ["reset", "--soft", "HEAD~1"], ["clean", "-fdx"],
            ["clean", "-n"], ["stash"], ["stash", "-u"], ["push", "origin", "main"], ["push", "--force", "origin"],
            ["update-ref", "-d", "refs/heads/main"], ["update-ref", "refs/heads/main", sha],
            ["branch", "-D", "main"], ["branch", "-d", "main"], ["branch", "-f", "main", "HEAD~1"],
            ["branch", "--force", "main", "HEAD~1"], ["branch", "-M", "main", "x"], ["branch", "-m", "main", "x"],
            ["branch", "newbranch"], ["checkout", "main"], ["checkout", "--", "a.txt"], ["checkout", "-b", "x"],
            ["checkout", "-f", "main"], ["switch", "main"], ["switch", "-c", "x"], ["restore", "a.txt"],
            ["rebase", "main"],
            ["worktree", "remove", wt], ["worktree", "remove", "--force", wt], ["worktree", "prune"],
            ["worktree", "move", wt, wt + "2"],
            ["worktree", "add", "-B", "main", wt, sha], ["worktree", "add", "--force", wt, "main"],
            ["worktree", "add", "-f", wt, "main"], ["worktree", "add", "--detach", wt, sha],
            ["worktree", "add", "--orphan", "-b", "o", wt], ["worktree", "add", "-b", "x", "-B", "main", wt, sha],
            ["-c", "core.hooksPath=/tmp/h", "status"], ["status", "-c", "core.pager=x"],
            ["-c", "alias.status=!touch /tmp/pwned", "status"], ["-C", str(self.tmp), "status"],
            ["--git-dir=/x", "status"], ["status", "--porcelain", "-c", "a=b"],
            ["worktree", "add", "-b", "x", "--end-of-options", wt, "-B"],
        ]

    def test_real_world_refuses_each_forbidden_form_and_changes_nothing(self):
        for args in self.forbidden():
            before = self.snapshot(self.primary)
            res = self.gw.git(args, str(self.primary))
            self.assertFalse(res.ok, args)
            self.assertIsNone(res.rc, args)
            self.assertIn("git refused", res.error or "", args)
            self.assertEqual(self.snapshot(self.primary), before, f"{args} changed the repo")
        self.assertFalse((self.tmp / "wt-x").exists())
        self.assertFalse(os.path.exists("/tmp/pwned"))

    def test_real_world_never_spawns_git_for_a_refused_form(self):
        with mock.patch.object(edl.subprocess, "run") as run:
            for args in self.forbidden():
                self.gw.git(args, str(self.primary))
        run.assert_not_called()

    def test_the_fake_world_refuses_the_same_forms(self):
        world = FakeWorld(repos={str(self.primary): {"refs": set()}})
        for args in self.forbidden():
            res = world.git(args, str(self.primary))
            self.assertIsNone(res.rc, args)
            self.assertIn("git refused", res.error or "", args)

    def test_the_forms_the_module_needs_still_run(self):
        p = str(self.primary)
        self.assertTrue(self.gw.git(["status", "--porcelain"], p).ok)
        self.assertTrue(self.gw.git(["rev-parse", "--verify", "--quiet", "--end-of-options", "main^{commit}"], p).ok)
        self.assertTrue(self.gw.git(["worktree", "list", "--porcelain"], p).ok)

    def test_a_dynamic_argv_is_refused_at_runtime(self):
        verb, flag = "res" + "et", "--ha" + "rd"
        res = self.gw.git([verb, flag], str(self.primary))
        self.assertIn("git refused", res.error)

    def test_dash_b_to_capital_b_in_add_worktree_would_be_refused(self):
        # what the -b -> -B mutation of _add_worktree does: the runner refuses, so no worktree is made
        path = str(self.tmp / "wt-b")
        res = self.gw.git(["worktree", "add", "-B", "epic/zz", "--end-of-options", path, self.second_sha], str(self.solo))
        self.assertIn("git refused", res.error)
        self.assertFalse(os.path.exists(path))


class TestShaAndRefHardening(_GitRepos):
    def test_trailing_newline_sha_is_invalid_and_the_regex_uses_fullmatch(self):
        L, st = self.wledger("solo", epic="e5")
        self.assertTrue(edl.ensure_step_worktrees(L, st, self.gw, "W").ok)
        integ = st["worktrees"]["integration"]["solo"]
        tip = self.g(integ, "rev-parse", "HEAD")
        calls = []
        real = self.gw.git
        with mock.patch.object(self.gw, "git", side_effect=lambda a, c, t=30.0: calls.append(a) or real(a, c, t)):
            res = edl.sha_check(L, self.gw, "W", {"": tip + "\n"})
        self.assertEqual(res.repos[0].status, edl.SHA_INVALID)
        self.assertEqual(calls, [])
        self.assertTrue(edl.sha_check(L, self.gw, "W", {"": tip}).passed)

    def test_a_trailing_newline_sha_in_a_verdict_is_not_a_valid_sha(self):
        self.assertIsNone(edl._SHA_RE.fullmatch("abc1234\n"))
        self.assertTrue(edl._SHA_RE.fullmatch("abc1234"))

    def test_a_tag_named_like_the_integration_branch_does_not_fool_the_sha_check(self):
        solo = self.solo
        self.g(solo, "checkout", "-q", "-b", "side")
        self.commit(solo, "side.txt", "x\n", "side only")
        side = self.g(solo, "rev-parse", "HEAD")
        self.g(solo, "tag", "epic/e6")                 # a tag, not a branch, at a commit the branch lacks
        self.g(solo, "checkout", "-q", "-")
        self.g(solo, "branch", "-q", "-D", "side")
        self.before[solo] = self.snapshot(solo)
        L, st = self.wledger("solo", epic="e6")
        self.assertTrue(edl.ensure_step_worktrees(L, st, self.gw, "W").ok)
        res = edl.sha_check(L, self.gw, "W", {"": side})
        self.assertEqual(res.repos[0].status, edl.SHA_NOT_ANCESTOR, res.detail)
        self.assertFalse(res.passed)

    def test_a_base_ref_starting_with_a_dash_is_refused_at_validation(self):
        d = self.ledger_dict([self.step("W", role={"worker": {
            "implementer": "impl", "reviewer": "rev", "repos": self.repos("primary", base="--abbrev-ref=3")}})])
        d["roles"]["impl"] = {"model": "claude-opus-5-5", "effort": "high"}
        d["roles"]["rev"] = {"model": "claude-opus-5-5", "effort": "high"}
        errors, _ = self.issues(d, self.gw)
        self.assertTrue([e for e in errors if "base_ref" in e.where and "'-'" in e.message], errors)

    def test_base_ref_is_resolved_after_end_of_options(self):
        calls = []
        real = self.gw.git
        repo = edl.WorkerRepo(str(self.primary), "main", "primary")
        with mock.patch.object(self.gw, "git", side_effect=lambda a, c, t=30.0: calls.append(a) or real(a, c, t)):
            edl.resolve_base_ref(self.gw, repo)
        self.assertEqual(calls[0][:4], ["rev-parse", "--verify", "--quiet", "--end-of-options"])

    def test_sanitised_step_id_collisions_are_rejected_with_both_ids(self):
        for a, b in (("W", "w"), ("50-07", "50.07")):
            w = {"worker": {"implementer": "impl", "reviewer": "rev", "repos": self.repos("primary")}}
            d = self.ledger_dict([self.step(a, role=w), self.step(b, role=w)])
            d["roles"]["impl"] = {"model": "claude-opus-5-5", "effort": "high"}
            d["roles"]["rev"] = {"model": "claude-opus-5-5", "effort": "high"}
            errors, _ = self.issues(d, self.gw)
            hits = [e for e in errors if e.category == edl.E_NAMES and "collides" in e.message]
            self.assertEqual(len(hits), 1, (a, b, errors))
            self.assertIn(repr(a), hits[0].message)
            self.assertIn(repr(b), hits[0].message)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# =============================================================================
# 50-08 — the tick, the CLI, alarms and notify, the tmux server (§12.10, §16–§18, §20–§23)
# =============================================================================
#
# Everything runs against TickWorld (DispatchWorld plus fake systemd, tmux server, notify and
# questions tooling): no real systemctl, systemd-run, systemd-inhibit, busctl, tmux, relay or
# Telegram. The CLI is loaded from .claude/bin/epic-driver and called in process with that world;
# the state root is the test's temp dir; EPIC_DRIVER_ALLOW_UNSTAMPED=1 except in the stamp tests.

import hashlib  # noqa: E402
import importlib.util  # noqa: E402
import io  # noqa: E402
import threading  # noqa: E402
from importlib.machinery import SourceFileLoader  # noqa: E402

CLI_PATH = _REPO / ".claude" / "bin" / "epic-driver"


def _load_cli():
    loader = SourceFileLoader("epic_driver_cli", str(CLI_PATH))
    spec = importlib.util.spec_from_loader("epic_driver_cli", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


CLI = _load_cli()

OWN_CG = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/epic-driver-tmux.scope\n"
TERM_CG = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/ptyxis-spawn-5916.scope\n"
SERVICE_CG = "0::/user.slice/user-1000.slice/user@1000.service/app.slice/claude-epic-driver.service\n"
INHIBIT_HEAD = ("WHO                UID  USER  PID  COMM            WHAT        WHY                MODE\n"
                "GNOME Shell        1000 anton 9014 gnome-shell     sleep       GNOME needs to save delay\n")
INHIBIT_LINE = "claude-epic-driver 1000 anton 4343 systemd-inhibit sleep:idle  epic driver is on  block\n"


class TickWorld(DispatchWorld):
    """DispatchWorld plus systemd, the tmux server, notify and the questions tooling, all faked."""

    def __init__(self, tdir, **kw):
        super().__init__(tdir, **kw)
        self.units = {edl.LISTENER_UNIT: "active", edl.INHIBIT_UNIT: "active", edl.TIMER_UNIT: "inactive"}
        self.start_fails = set()           # units whose `start` fails
        self.enable_fails = False
        self.inhibit_takes_lock = True     # the inhibitor's lock shows once the unit is active
        self.systemctl_calls = []
        self.server_pid = 16651
        self.cgroups = {16651: OWN_CG}
        self.runs = []                     # (args, env) of systemd-run
        self.run_lands_in = OWN_CG         # where a started server lands; None: starting fails
        self.busctl_calls = []
        self.busctl_moves = True
        self.notifies = []                 # dicts: text, role, ack, workspace_dir, at
        self.notify_ok = True
        self.next_mid = 9000
        self.index = set()
        self.settings = {"nudge": "1h", "sync": "commit"}
        self.qcheck = edl.CommandResult(rc=0, stdout="bound\n")
        self.slept = []
        self.cost = {}                     # World method name -> seconds the fake clock advances

    def _cost(self, what):
        if self.cost.get(what):
            self.advance(self.cost[what])

    def systemctl(self, args):
        args = list(args)
        self.systemctl_calls.append(args)
        self._cost("systemctl")
        verb = args[0]
        if verb == "is-active":
            on = self.units.get(args[1]) == "active"
            return edl.CommandResult(rc=0 if on else 3, stdout="active\n" if on else "inactive\n")
        if verb == "start":
            if args[1] in self.start_fails:
                return edl.CommandResult(rc=1, stderr=f"Job for {args[1]} failed.\n")
            self.units[args[1]] = "active"
            return edl.CommandResult(rc=0)
        if verb == "enable":
            if self.enable_fails:
                return edl.CommandResult(rc=1, stderr="Unit claude-epic-driver.timer not found.\n")
            for u in args[2:]:
                self.units[u] = "active"
            return edl.CommandResult(rc=0)
        if verb == "disable":
            for u in args[2:]:
                self.units[u] = "inactive"
            return edl.CommandResult(rc=0)
        if verb == "show":
            on = self.units.get(args[1]) == "active"
            return edl.CommandResult(rc=0, stdout=f"ActiveState={'active' if on else 'inactive'}\n"
                                                  f"NextElapseUSecRealtime={'Tue 2026-11-24 09:40:00 UTC' if on else ''}\n")
        raise AssertionError(f"unexpected systemctl {args}")

    def systemd_run(self, args, env):
        self.runs.append((list(args), dict(env)))
        self._cost("systemd_run")
        if self.run_lands_in is None:
            return edl.CommandResult(rc=1, stderr="Failed to start transient scope unit: Access denied\n")
        self.server_pid = 20001
        self.cgroups[20001] = self.run_lands_in
        return edl.CommandResult(rc=0)

    def inhibit_list(self):
        self._cost("inhibit_list")
        held = self.units.get(edl.INHIBIT_UNIT) == "active" and self.inhibit_takes_lock
        return edl.CommandResult(rc=0, stdout=INHIBIT_HEAD + (INHIBIT_LINE if held else ""))

    def busctl(self, args):
        args = list(args)
        self.busctl_calls.append(args)
        if not self.busctl_moves:
            return edl.CommandResult(rc=1, stderr="Call failed: Access denied\n")
        self.cgroups[int(args[-2] if args[4] == "StartTransientUnit" else args[-1])] = OWN_CG
        self.units[edl.TMUX_SCOPE_UNIT] = "active"
        return edl.CommandResult(rc=0, stdout='o "/org/freedesktop/systemd1/job/77"\n')

    def tmux_server_pid(self):
        self._cost("tmux_server_pid")
        return self.server_pid

    def read_cgroup(self, pid):
        return self.cgroups.get(pid)

    def notify(self, text, *, role, ack, workspace_dir):
        self._cost("notify")
        self.notifies.append({"text": text, "role": role, "ack": ack, "workspace_dir": workspace_dir,
                              "at": self.clock})
        if not self.notify_ok:
            return {"dispatched": False, "message_id": None, "error": "Relay send failed: relay down"}
        self.next_mid += 1
        if ack:
            self.index.add(str(self.next_mid))
        return {"dispatched": True, "message_id": self.next_mid}

    def notify_index_ids(self):
        return set(self.index)

    def questions_settings(self, workspace):
        return None if self.settings is None else dict(self.settings)

    def questions_check(self, workspace):
        return self.qcheck

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.advance(seconds)

    def alarm_notifies(self):
        return [n for n in self.notifies if n["ack"]]


class _Tick(_Disp):
    NOW = "2026-11-24T10:00:00Z"

    def setUp(self):
        super().setUp()
        self.world = TickWorld(self.tdir, now=T(self.NOW), profiles={
            "claude": {"ANTHROPIC_DEFAULT_FABLE_MODEL": "claude-opus-5-5"}})
        self.env = {edl.STATE_ROOT_ENV: os.environ[edl.STATE_ROOT_ENV], CLI.ALLOW_UNSTAMPED_ENV: "1",
                    "HOME": str(self.tmp), "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}

    # -- the run, set up as `enable` leaves it (without the gates) --
    def enable(self, steps=None, **top):
        d = self.ledger_dict(steps if steps is not None else [self.driver("A")], **top)
        path = self.write(d)
        L = edl.load_ledger(path, self.world)
        paths = edl.run_paths(L.run_id)
        paths.dir.mkdir(parents=True, exist_ok=True)
        edl.save_state(paths.state, edl.new_state(L, self.world.now()))
        edl.atomic_write_text(paths.last_valid_ledger, Path(path).read_text())
        edl.write_active({"run_id": L.run_id, "ledger": L.path, "state_dir": str(paths.dir),
                          "enabled_at": edl.iso(self.world.now()), "workspace": L.workspace, "epic": L.epic,
                          "notify_role": L.notify_role})
        self.lpath, self.L, self.rp = path, L, paths
        return L

    def state(self):
        return edl.load_state(self.rp.state)

    def save(self, st):
        edl.save_state(self.rp.state, st)

    def run_pass(self, advance=None):
        if advance:
            self.world.advance(advance)
        out, err = io.StringIO(), io.StringIO()
        rc = edl.run_tick(self.world, environ=self.env, out=out, err=err)
        self.last_out, self.last_err = out.getvalue(), err.getvalue()
        return rc

    def cli(self, *argv, environ=None):
        out, err = io.StringIO(), io.StringIO()
        rc = CLI.main(list(argv), world=self.world, environ=environ if environ is not None else dict(self.env),
                      out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def active_alarms(self, st=None):
        st = st or self.state()
        return sorted(k for k, r in (st.get("alarms") or {}).items() if not r.get("cleared_at"))


# -- the full pass on a toy ledger (§23) -------------------------------------------

class TestTickFullPass(_Tick):
    def test_a_toy_ledger_runs_end_to_end_across_ticks(self):
        self.enable([self.driver("A", phase="P1"), self.driver("B", needs=["A"], phase="P2")])
        name = "ws-t-driver-g1"
        self.world.check_script.update({"A": (0, 2), "B": (0, 2)})
        # tick 1: A is due → the driver session is spawned, seeded with E-1 (its turn ends ADVANCED: A)
        self.world.reply[name] = "Rolled.\nADVANCED: A"
        self.assertEqual(self.run_pass(), 0, self.last_err)
        st = self.state()
        self.assertEqual(st["steps"]["A"]["status"], "inflight")
        self.assertEqual(len(self.world.spawns), 1)
        self.assertEqual(self.world.spawns[0][-1], f"EVENT E-1: read {self.rp.events / 'E-1.md'}")
        self.assertEqual((st["tick_seq"], st["last_tick_error"]), (1, None))
        self.assertEqual(st["last_tick_at"], st["last_tick_ok_at"])
        # tick 2: E-1 confirmed, the verdict moves A to verifying, its check passes (done), B is due → E-2
        self.world.reply[name] = "Done.\nADVANCED: B"
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        st = self.state()
        self.assertEqual((st["steps"]["A"]["status"], st["steps"]["A"]["by"]), ("done", "check"))
        self.assertEqual(st["steps"]["B"]["status"], "inflight")
        self.assertEqual([s[1] for s in self.world.sends], [f"EVENT E-2: read {self.rp.events / 'E-2.md'}"])
        self.assertEqual([c[0] for c in self.world.check_calls], ["A"])
        # tick 3: B verifying → its check → done; then a quiet tick
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        st = self.state()
        self.assertEqual(st["steps"]["B"]["status"], "done")
        self.assertEqual([c[0] for c in self.world.check_calls], ["A", "B"])
        self.assertEqual(self.active_alarms(st), [])
        self.assertEqual(st["tick_seq"], 4)
        # the heartbeats: one per phase transition, ack=False, to the ledger's workspace
        beats = [n for n in self.world.notifies if not n["ack"]]
        self.assertEqual([n["text"] for n in beats],
                         ["[epic-driver t] phase P1 done; next: P2 (1 step)",
                          "[epic-driver t] phase P2 done; every phase is done"])
        self.assertTrue(all(n["workspace_dir"] == str(self.ws) for n in beats))
        self.assertEqual(self.world.alarm_notifies(), [])
        log = self.rp.tick_log.read_text()
        self.assertEqual(log.count(" ok in "), 4)

    def test_the_tick_unsets_a_dead_ssh_auth_sock(self):
        self.enable()
        self.env["SSH_AUTH_SOCK"] = str(self.tmp / "gone.sock")
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertNotIn("SSH_AUTH_SOCK", self.env)
        self.assertIn("SSH_AUTH_SOCK", self.rp.tick_log.read_text())

    def test_no_active_run_exits_0(self):
        self.assertEqual(self.run_pass(), 0)
        self.assertIn("no active run", self.last_out)


# -- tick.lock (§18) ---------------------------------------------------------------

class TestTickLock(_Tick):
    def hold_in_subprocess(self):
        code = ("import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f, fcntl.LOCK_EX); "
                "print('held', flush=True); time.sleep(60)")
        p = subprocess.Popen([sys.executable, "-c", code, str(self.rp.lock)], stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "held")
        return p

    def test_a_second_tick_while_one_holds_the_lock_exits_0_quietly(self):
        self.enable()
        p = self.hold_in_subprocess()
        try:
            self.assertEqual(self.run_pass(), 0)
            self.assertEqual(self.state()["tick_seq"], 0)              # nothing ran
            self.assertEqual(self.world.spawns, [])
            self.assertIn("already running", self.rp.tick_log.read_text())
        finally:
            p.kill()
            p.wait()
            p.stdout.close()

    def test_a_lock_left_by_a_dead_process_is_acquired(self):
        self.enable()
        p = self.hold_in_subprocess()
        p.kill()
        p.wait()
        p.stdout.close()
        self.assertTrue(self.rp.lock.exists())
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(self.state()["tick_seq"], 1)

    def test_mark_waits_for_the_lock(self):
        self.enable([self.driver("A")])
        fd = edl.acquire_lock(self.rp.lock, 0)
        self.assertIsNotNone(fd)
        threading.Timer(0.6, edl.release_lock, args=(fd,)).start()
        t0 = time.monotonic()
        rc, out, err = self.cli("mark", "A", "done")
        self.assertEqual(rc, 0, err)
        self.assertGreaterEqual(time.monotonic() - t0, 0.5)
        self.assertEqual(self.state()["steps"]["A"]["status"], "done")

    def test_a_writing_verb_gives_up_after_its_wait(self):
        self.enable()
        fd = edl.acquire_lock(self.rp.lock, 0)
        try:
            with mock.patch.object(edl, "LOCK_WAIT_S", 0.2):
                with mock.patch.object(edl._Locked.__init__, "__defaults__", (0.2,)):
                    rc, out, err = self.cli("pause")
            self.assertEqual(rc, 1)
            self.assertIn("is held", err)
        finally:
            edl.release_lock(fd)


# -- a crash mid-pass (§23) ---------------------------------------------------------

class TestTickCrash(_Tick):
    def test_an_exception_mid_pass_records_tick_error_and_the_next_pass_clears_it(self):
        self.enable()
        with mock.patch.object(edl, "reconcile", side_effect=RuntimeError("boom in reconcile")):
            self.assertEqual(self.run_pass(), 1)
        self.assertIn("RuntimeError: boom in reconcile", self.last_err)
        st = self.state()
        self.assertEqual(st["last_tick_error"]["error"], "RuntimeError: boom in reconcile")
        self.assertEqual(st["last_tick_ok_at"], None)
        self.assertEqual(self.active_alarms(st), ["tick_error:run"])
        alarm = self.world.alarm_notifies()
        self.assertEqual(len(alarm), 1)
        self.assertEqual(alarm[0]["text"], "[epic-driver t] ALARM tick_error run: RuntimeError: boom in reconcile"
                                           " — epic-driver status")
        self.assertEqual(self.world.spawns, [])                 # the pass's effects never ran
        self.assertIn("pass failed", self.rp.tick_log.read_text())
        # the next pass completes: last_tick_error cleared, tick_error cleared (no notify on a clear)
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        st = self.state()
        self.assertIsNone(st["last_tick_error"])
        self.assertEqual(st["alarms"]["tick_error:run"]["cleared_at"], edl.iso(self.world.now()))
        self.assertEqual(len(self.world.alarm_notifies()), 1)

    def test_the_error_is_recorded_on_the_last_committed_state(self):
        self.enable()
        st = self.state()
        st["steps"]["A"]["status"] = "verifying"
        self.save(st)
        with mock.patch.object(edl, "run_checks", side_effect=OSError("disk")):
            self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.state()["steps"]["A"]["status"], "verifying")
        self.assertEqual(self.state()["clock"], None)          # the reconcile's result was not committed


# -- stamps (§17, OQ-3) ---------------------------------------------------------------

class TestStamps(_Tick):
    def fake_install(self):
        """An installed layout in the temp dir: every §17 file, and a manifest that matches."""
        root = self.tmp / "home" / ".claude"
        modules = {}
        for name, rel in CLI.STAMPED_FILES.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# {name}\n")
            modules[name] = {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
        manifest = root / "epic-driver" / "stamps.json"
        manifest.write_text(json.dumps({"revision": "v1.4-12-g2a2f966", "installed_at": "2026-11-23T08:00:00Z",
                                        "modules": modules}))
        return CLI.layout(root / "bin" / "epic-driver"), manifest, modules

    def test_the_installed_layout(self):
        lay, manifest, _ = self.fake_install()
        root = self.tmp / "home" / ".claude"
        self.assertTrue(lay["installed"])
        self.assertEqual(lay["sys_path"], [str(root / "hooks"), str(root / "statusline"), str(root / "epic-driver" / "lib")])
        self.assertEqual(lay["hooks_repo"], str(self.tmp / "home"))          # CLAUDE_HOOKS_REPO=$HOME
        self.assertEqual(lay["manifest"], manifest)
        repo = CLI.layout(CLI_PATH)
        self.assertFalse(repo["installed"])
        self.assertEqual(repo["hooks_repo"], str(_REPO))
        self.assertIn(str(_REPO / "questions-mcp"), repo["sys_path"])

    def test_agreeing_stamps(self):
        lay, _, _ = self.fake_install()
        self.assertEqual(CLI.check_stamps(lay, {}), {"checked": True, "ok": True, "problem": None,
                                                    "revision": "v1.4-12-g2a2f966", "files": [],
                                                    "override_env": False})

    def test_missing_manifest_changed_file_missing_file_and_wrong_path(self):
        lay, manifest, modules = self.fake_install()
        with self.subTest("changed"):
            (self.tmp / "home" / ".claude" / "hooks" / "amux_spawn_lib.py").write_text("# reinstalled by amux\n")
            r = CLI.check_stamps(lay, {})
            self.assertFalse(r["ok"])
            self.assertIn("amux_spawn_lib.py", r["problem"])
            self.assertIn("changed since it was installed", r["problem"])
        with self.subTest("missing file"):
            os.unlink(self.tmp / "home" / ".claude" / "hooks" / "amux_spawn_lib.py")
            self.assertIn("is missing", CLI.check_stamps(lay, {})["problem"])
        with self.subTest("not listed"):
            data = json.loads(manifest.read_text())
            del data["modules"]["questions_mcp_lib.py"]
            manifest.write_text(json.dumps(data))
            self.assertIn("questions_mcp_lib.py is not in", CLI.check_stamps(lay, {})["problem"])
        with self.subTest("another path"):
            data["modules"]["questions_mcp_lib.py"] = {"path": str(_REPO / "questions-mcp" / "questions_mcp_lib.py"),
                                                       "sha256": "0"}
            manifest.write_text(json.dumps(data))
            self.assertIn("this CLI uses", CLI.check_stamps(lay, {})["problem"])
        with self.subTest("missing manifest"):
            os.unlink(manifest)
            r = CLI.check_stamps(lay, {})
            self.assertEqual((r["checked"], r["ok"]), (True, False))
            self.assertIn("no stamps manifest", r["problem"])

    def test_an_unstamped_repo_run_is_refused_unless_the_test_env_var_is_set(self):
        path = self.write(self.ledger_dict([self.driver("A")]))
        env = {k: v for k, v in self.env.items() if k != CLI.ALLOW_UNSTAMPED_ENV}
        rc, out, err = self.cli("validate", path, environ=env)
        self.assertEqual(rc, 3)
        self.assertIn("stamp_mismatch: no stamps manifest", err)
        env[CLI.ALLOW_UNSTAMPED_ENV] = "yes"                     # only exactly "1" counts
        self.assertEqual(self.cli("validate", path, environ=env)[0], 3)
        rc, out, err = self.cli("validate", path)
        self.assertEqual(rc, 0, err)
        self.assertIn("valid", out)

    def test_the_cli_drops_claude_hooks_repo_and_claude_project_dir(self):
        path = self.write(self.ledger_dict([self.driver("A")]))
        env = dict(self.env, CLAUDE_PROJECT_DIR="/elsewhere", CLAUDE_HOOKS_REPO="/wrong")
        self.assertEqual(self.cli("validate", path, environ=env)[0], 0)
        self.assertNotIn("CLAUDE_HOOKS_REPO", env)        # not left in the process for children
        self.assertNotIn("CLAUDE_PROJECT_DIR", env)

    def test_verbs_that_stop_or_observe_still_run_on_a_mismatch(self):
        self.enable()
        env = {k: v for k, v in self.env.items() if k != CLI.ALLOW_UNSTAMPED_ENV}
        rc, out, err = self.cli("status", environ=env)
        self.assertEqual(rc, 0, err)
        self.assertIn("stamps MISMATCH: no stamps manifest", out)
        self.assertEqual(self.cli("mark", "A", "done", environ=env)[0], 3)
        self.assertEqual(self.cli("pause", environ=env)[0], 0)
        with mock.patch.object(CLI, "tick_stamp_mismatch", return_value=1) as tsm:
            self.assertEqual(self.cli("tick", environ=env)[0], 1)
        tsm.assert_called_once()

    def test_a_mismatched_tick_records_notifies_once_and_exits_1_then_a_good_tick_clears_it(self):
        self.enable()
        sent = []

        def fake_notify(text, role=None, ack=True, *, workspace_dir=None):
            sent.append((text, ack, workspace_dir))
            return {"dispatched": True, "message_id": 4242}
        problem = "amux_spawn_lib.py: /x changed since it was installed"
        self.assertEqual(CLI.tick_stamp_mismatch(problem, self.env, notify=fake_notify, err=io.StringIO()), 1)
        st = self.state()
        self.assertEqual(st["last_tick_error"]["error"], f"stamp_mismatch: {problem}")
        self.assertEqual(st["alarms"]["stamp_mismatch:run"]["message_id"], 4242)
        self.assertEqual(sent, [(f"[epic-driver t] ALARM stamp_mismatch run: {problem} — epic-driver status",
                                 True, str(self.ws))])
        self.assertEqual((self.world.spawns, self.world.systemctl_calls, self.world.calls), ([], [], []))
        # the next mismatched tick records again, without a second notify
        self.assertEqual(CLI.tick_stamp_mismatch(problem, self.env, notify=fake_notify, err=io.StringIO()), 1)
        self.assertEqual(len(sent), 1)
        # stamps agree again: the tick runs and the alarm clears
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        st = self.state()
        self.assertIsNotNone(st["alarms"]["stamp_mismatch:run"]["cleared_at"])
        self.assertIsNone(st["last_tick_error"])

    def test_a_mismatched_tick_whose_notify_fails_records_the_failure(self):
        self.enable()

        def failing(text, role=None, ack=True, *, workspace_dir=None):
            raise RuntimeError("relay down")
        self.assertEqual(CLI.tick_stamp_mismatch("x", self.env, notify=failing, err=io.StringIO()), 1)
        st = self.state()
        self.assertIn("relay down", st["notify_failures"][-1]["error"])
        self.assertIn("dispatched=no", self.rp.notify_log.read_text())

    # -- EPIC_DRIVER_ALLOW_UNSTAMPED: repo runs only --------------------------------------------------

    def test_an_installed_copy_ignores_the_override_and_always_checks_stamps(self):
        lay, manifest, _ = self.fake_install()
        env = {CLI.ALLOW_UNSTAMPED_ENV: "1"}
        r = CLI.check_stamps(lay, env)
        self.assertEqual((r["checked"], r["ok"], r["override_env"]), (True, True, True))   # checked, and agrees
        (self.tmp / "home" / ".claude" / "hooks" / "amux_spawn_lib.py").write_text("# reinstalled\n")
        r = CLI.check_stamps(lay, env)
        self.assertEqual((r["checked"], r["ok"]), (True, False))                           # the override did not help
        self.assertIn("changed since it was installed", r["problem"])
        self.assertEqual(r["files"], ["amux_spawn_lib.py"])
        # a repo checkout honours it (the tests rely on that)
        repo = CLI.check_stamps(CLI.layout(CLI_PATH), env)
        self.assertEqual((repo["checked"], repo["ok"]), (False, True))

    def test_how_a_repo_run_and_an_installed_run_are_told_apart(self):
        home = self.tmp / "h2"
        (home / ".claude" / "bin").mkdir(parents=True)
        # the installer's bin dir, even before epic-driver/lib exists
        self.assertTrue(CLI.layout(home / ".claude" / "bin" / "epic-driver", home=str(home))["installed"])
        self.assertTrue(CLI.layout(home / ".claude" / "bin" / "epic-driver", home=str(home))["installed"])
        # <root>/epic-driver/lib exists: installed, wherever it is
        other = self.tmp / "h3" / ".claude"
        (other / "bin").mkdir(parents=True)
        (other / "epic-driver" / "lib").mkdir(parents=True)
        self.assertTrue(CLI.layout(other / "bin" / "epic-driver", home=str(home))["installed"])
        # a checkout is neither
        self.assertFalse(CLI.layout(CLI_PATH, home=str(home))["installed"])
        self.assertFalse(CLI.layout(CLI_PATH)["installed"])

    def test_an_installed_run_through_main_ignores_the_variable(self):
        self.enable()
        lay, manifest, _ = self.fake_install()
        (self.tmp / "home" / ".claude" / "hooks" / "amux_spawn_lib.py").write_text("# reinstalled\n")
        with mock.patch.object(CLI, "layout", lambda *a, **kw: lay):
            rc, out, err = self.cli("status")                        # self.env has ALLOW_UNSTAMPED=1
            self.assertEqual(rc, 0, err)
            self.assertIn("stamps MISMATCH", out)
            self.assertEqual(self.cli("mark", "A", "done")[0], 3)

    def test_the_variables_are_stripped_from_the_process_environment(self):
        path = self.write(self.ledger_dict([self.driver("A")]))
        env = dict(self.env, CLAUDE_HOOKS_REPO="/wrong")
        self.assertEqual(self.cli("validate", path, environ=env)[0], 0)
        self.assertNotIn(CLI.ALLOW_UNSTAMPED_ENV, env)
        self.assertNotIn("CLAUDE_HOOKS_REPO", env)

    def test_a_check_and_the_tmux_server_never_see_either_variable(self):
        self.enable([self.driver("A", check="true")])
        st = self.state()
        st["steps"]["A"].update(status="watching", due_since=self.NOW)
        self.save(st)
        self.world.server_pid = None                                  # the tick starts the server
        env = dict(self.env, CLAUDE_HOOKS_REPO="/repo", KEEP_ME="1")
        self.env.clear()
        self.env.update(env)
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertTrue(self.world.check_calls)
        for call in self.world.check_calls:
            self.assertNotIn(CLI.ALLOW_UNSTAMPED_ENV, call[3])
            self.assertNotIn("CLAUDE_HOOKS_REPO", call[3])
        self.assertTrue(self.world.runs)
        for _args, run_env in self.world.runs:
            self.assertNotIn(CLI.ALLOW_UNSTAMPED_ENV, run_env)
            self.assertNotIn("CLAUDE_HOOKS_REPO", run_env)
            self.assertEqual(run_env.get("KEEP_ME"), "1")             # everything else still goes through
        for name in (CLI.ALLOW_UNSTAMPED_ENV, "CLAUDE_HOOKS_REPO"):
            self.assertNotIn(name, edl.tmux_start_env({name: "1", "X": "y"}, {name: "2"}))

    # -- stamp_mismatch: repeat, and no import of a mismatched questions_mcp_lib -----------------------

    def poison(self, calls, seen_env=None):
        mod = mock.Mock()

        def notify(text, **kw):
            calls.append((text, kw))
            if seen_env is not None:
                seen_env.append(os.environ.get("CLAUDE_HOOKS_REPO"))
            return {"dispatched": True, "message_id": 77}
        mod.notify = notify
        return mod

    def mismatch_at(self, hours, mismatched, hooks_repo=None):
        t0 = T("2026-11-24T10:00:00Z")
        stamp = edl.iso(t0 + timedelta(hours=hours))
        with mock.patch.object(CLI, "_now_iso", lambda: stamp):
            return CLI.tick_stamp_mismatch("x changed", self.env, err=io.StringIO(), mismatched=mismatched,
                                           hooks_repo=hooks_repo)

    def test_stamp_mismatch_repeats_like_any_alarm_every_4h_not_once_per_episode(self):
        self.enable()
        calls, seen = [], []
        with mock.patch.dict(sys.modules, {"questions_mcp_lib": self.poison(calls, seen)}):
            os.environ.pop("CLAUDE_HOOKS_REPO", None)
            self.assertEqual(self.mismatch_at(0, ["amux_spawn_lib.py"], hooks_repo="/hooks"), 1)
            self.assertEqual(len(calls), 1)
            self.assertEqual(seen, ["/hooks"])                       # set around the call only
            self.assertNotIn("CLAUDE_HOOKS_REPO", os.environ)
            self.mismatch_at(3, ["amux_spawn_lib.py"])
            self.assertEqual(len(calls), 1)                          # not yet: 4 h after the last attempt
            self.mismatch_at(4.1, ["amux_spawn_lib.py"])
            self.assertEqual(len(calls), 2)                          # the first repeat
            self.mismatch_at(6, ["amux_spawn_lib.py"])
            self.assertEqual(len(calls), 2)
            self.mismatch_at(8.2, ["amux_spawn_lib.py"])
            self.assertEqual(len(calls), 3)
            self.assertTrue(all(c[1]["ack"] for c in calls))
        self.assertEqual(self.state()["alarms"]["stamp_mismatch:run"]["sends"], 3)

    def test_stamp_mismatch_waits_for_a_live_ladder(self):
        self.enable()
        calls = []
        with mock.patch.dict(sys.modules, {"questions_mcp_lib": self.poison(calls)}):
            self.mismatch_at(0, ["amux_spawn_lib.py"])
            st = self.state()
            st["alarms"]["stamp_mismatch:run"]["ladder_until"] = edl.iso(T("2026-11-24T10:00:00Z") + timedelta(hours=10))
            self.save(st)
            self.mismatch_at(5, ["amux_spawn_lib.py"])
            self.mismatch_at(13, ["amux_spawn_lib.py"])
            self.assertEqual(len(calls), 1)                          # 4 h after the ladder ends = hour 14
            self.mismatch_at(14.1, ["amux_spawn_lib.py"])
            self.assertEqual(len(calls), 2)

    def test_the_notify_import_closure_covers_what_questions_mcp_lib_really_imports(self):
        """Derive the project modules ``questions_mcp_lib`` imports, transitively, from its source (every
        import statement, nested ones too) and assert ``NOTIFY_IMPORT_CLOSURE`` lists them all."""
        import ast
        roots = [_REPO / ".claude" / "hooks", _REPO / "questions-mcp", _REPO / ".claude" / "statusline"]

        def source(mod):
            for d in roots:
                if (d / f"{mod}.py").is_file():
                    return d / f"{mod}.py"
            return None
        seen, todo = set(), ["questions_mcp_lib"]
        while todo:
            mod = todo.pop()
            if mod in seen:
                continue
            seen.add(mod)
            tree = ast.parse(source(mod).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    names = [node.module]
                else:
                    continue
                for name in names:
                    top = name.split(".")[0]
                    if source(top) is not None and top not in seen:
                        todo.append(top)
        derived = {f"{m}.py" for m in seen}
        self.assertGreaterEqual(len(derived), 6)                       # questions_mcp_lib and its five direct ones
        self.assertLessEqual(derived, set(CLI.NOTIFY_IMPORT_CLOSURE), "NOTIFY_IMPORT_CLOSURE went stale")
        self.assertLessEqual(set(CLI.NOTIFY_IMPORT_CLOSURE), set(CLI.STAMPED_FILES))   # and each is stamped

    def test_any_mismatched_module_of_the_import_closure_blocks_the_notify_import(self):
        self.enable()
        calls = []
        with mock.patch.dict(sys.modules, {"questions_mcp_lib": self.poison(calls)}):
            for name in CLI.NOTIFY_IMPORT_CLOSURE:
                with self.subTest(mismatched=name):
                    st = self.state()
                    st["alarms"], st["notify_failures"] = {}, []
                    self.save(st)
                    self.assertEqual(self.mismatch_at(0, ["amux_spawn_lib.py", name]), 1)
                    self.assertEqual(calls, [])                          # the notify path was not imported or called
                    st = self.state()
                    self.assertIn("not sent", st["alarms"]["stamp_mismatch:run"]["last_error"])
                    self.assertIn("not sent", st["notify_failures"][-1]["error"])

    def test_only_modules_outside_the_closure_mismatched_still_notifies(self):
        self.enable()
        calls = []
        outside = [n for n in CLI.STAMPED_FILES if n not in CLI.NOTIFY_IMPORT_CLOSURE]
        self.assertIn("amux_spawn_lib.py", outside)
        with mock.patch.dict(sys.modules, {"questions_mcp_lib": self.poison(calls)}):
            self.assertEqual(self.mismatch_at(0, outside), 1)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0][1]["ack"])

    def test_a_mismatched_questions_mcp_lib_is_never_imported(self):
        self.enable()
        calls = []
        with mock.patch.dict(sys.modules, {"questions_mcp_lib": self.poison(calls)}):
            for mismatched in (["questions_mcp_lib.py"], ["amux_spawn_lib.py", "questions_mcp_lib.py"], None):
                with self.subTest(mismatched=mismatched):
                    st = self.state()
                    st["alarms"], st["notify_failures"] = {}, []        # a fresh episode
                    self.save(st)
                    self.assertEqual(self.mismatch_at(0, mismatched), 1)
                    self.assertEqual(calls, [])
                    st = self.state()
                    self.assertIn("questions_mcp_lib.py", st["notify_failures"][-1]["error"])
                    self.assertIn("not sent", st["alarms"]["stamp_mismatch:run"]["last_error"])
                    log = self.rp.notify_log.read_text()
                    self.assertIn("alarm stamp_mismatch:run", log)
                    self.assertIn("dispatched=no", log)
                    rc, out, err = self.cli("status")
                    self.assertIn("not sent", out)                   # `status` shows it


# -- the tmux server (§21.1, P0-5) -------------------------------------------------------

class TestTmuxServer(_Tick):
    def test_no_server_starts_one_in_its_own_scope_with_the_snapshot_environment(self):
        self.enable()
        self.world.server_pid = None
        edl.write_env_snapshot(self.rp, {"PATH": "/usr/local/go/bin:/home/x/.cargo/bin:/usr/bin", "GOPATH": "/g"})
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(len(self.world.runs), 1)
        args, env = self.world.runs[0]
        self.assertEqual(args, ["--user", "--scope", "--unit=epic-driver-tmux", "tmux", "new-session", "-d", "-s",
                                "epic-driver-keepalive"])
        self.assertEqual(env["PATH"], "/usr/local/go/bin:/home/x/.cargo/bin:/usr/bin")   # over the service's
        self.assertEqual((env["GOPATH"], env["HOME"]), ("/g", str(self.tmp)))
        self.assertEqual(self.active_alarms(), [])
        self.assertEqual(len(self.world.spawns), 1)               # then the spawn went into that server
        self.assertIn("started the tmux server (pid 20001) in epic-driver-tmux.scope", self.rp.tick_log.read_text())

    def test_a_failed_start_raises_tmux_server_and_nothing_is_launched(self):
        self.enable()
        self.world.server_pid = None
        self.world.run_lands_in = None
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(self.active_alarms(), ["tmux_server:run"])
        self.assertIn("no tmux server, and starting it failed: Failed to start transient scope",
                      self.state()["alarms"]["tmux_server:run"]["detail"])
        self.assertEqual((self.world.spawns, self.world.sends), ([], []))
        self.assertEqual(self.state()["steps"]["A"]["status"], "due")
        # the start works on a later pass: the alarm clears and the step goes
        self.world.run_lands_in = OWN_CG
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(self.active_alarms(), [])
        self.assertEqual(len(self.world.spawns), 1)

    def test_a_started_server_outside_its_scope_raises_tmux_server(self):
        self.enable()
        self.world.server_pid = None
        self.world.run_lands_in = SERVICE_CG
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(self.active_alarms(), ["tmux_server:run"])
        self.assertIn("started, but it runs in claude-epic-driver.service",
                      self.state()["alarms"]["tmux_server:run"]["detail"])

    def test_the_tick_raises_tmux_server_for_a_server_in_a_terminal_scope_and_never_moves_it(self):
        self.enable()
        self.world.reply["ws-t-driver-g1"] = "ADVANCED: A"
        self.world.cgroups[16651] = TERM_CG
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(self.active_alarms(), ["tmux_server:run"])
        self.assertIn("ptyxis-spawn-5916.scope (pid 16651)", self.state()["alarms"]["tmux_server:run"]["detail"])
        self.assertEqual((self.world.busctl_calls, self.world.runs), ([], []))
        self.assertEqual(len(self.world.spawns), 1)               # a server exists: amux starts none
        # enable moved it: the alarm clears
        self.world.cgroups[16651] = OWN_CG
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(self.active_alarms(), [])

    def test_paused_no_server_is_not_started_and_not_an_alarm(self):
        self.enable()
        self.world.server_pid = None
        self.assertEqual(self.cli("pause", "--reason", "lunch")[0], 0)
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual((self.world.runs, self.world.spawns), ([], []))
        self.assertEqual(self.active_alarms(), [])

    def test_cgroup_scope(self):
        self.assertEqual(edl.cgroup_scope(OWN_CG), "epic-driver-tmux.scope")
        self.assertEqual(edl.cgroup_scope("12:pids:/x\n1:name=systemd:/a/b/ptyxis.scope\n"), "ptyxis.scope")
        self.assertIsNone(edl.cgroup_scope(None))
        self.assertIsNone(edl.cgroup_scope("0::/\n"))

    def test_the_move_commands(self):
        self.assertEqual(edl.start_scope_args(16651),
                         ["call", "org.freedesktop.systemd1", "/org/freedesktop/systemd1",
                          "org.freedesktop.systemd1.Manager", "StartTransientUnit", "ssa(sv)a(sa(sv))",
                          "epic-driver-tmux.scope", "fail", "1", "PIDs", "au", "1", "16651", "0"])
        self.assertEqual(edl.attach_scope_args(16651)[4:], ["AttachProcessesToUnit", "ssau", "epic-driver-tmux.scope",
                                                            "/", "1", "16651"])
        w = self.world
        w.cgroups[16651] = TERM_CG
        self.assertEqual(edl.move_tmux_server(w, 16651)[0], True)
        self.assertEqual(w.busctl_calls[-1][4], "StartTransientUnit")    # the scope did not exist yet
        w.cgroups[16651] = TERM_CG
        self.assertTrue(edl.move_tmux_server(w, 16651)[0])
        self.assertEqual(w.busctl_calls[-1][4], "AttachProcessesToUnit")  # now it does

    def test_no_amux_effect_without_a_server(self):
        """Never let an amux call start a server in the service's cgroup (§21.1): with no server,
        the plan is paused, so not one spawn, resume, send, key or reap runs."""
        self.enable([self.driver("A")])
        self.world.server_pid = None
        self.world.run_lands_in = None
        st = self.state()
        self.live(st)
        st["sessions"]["driver"]["parked"] = [{"name": "ws-t-driver-g0", "since": self.NOW}]
        self.save(st)
        self.assertEqual(self.run_pass(), 0, self.last_err)
        w = self.world
        self.assertEqual((w.spawns, w.resumes, w.sends, w.keys, w.reaps), ([], [], [], [], []))


# -- the listener and the inhibitor (§12.10) ----------------------------------------------

class TestInfrastructure(_Tick):
    def test_one_start_attempt_each_then_the_alarm(self):
        self.enable([])
        w = self.world
        w.units[edl.LISTENER_UNIT] = "inactive"
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertIn(["start", edl.LISTENER_UNIT], w.systemctl_calls)
        self.assertEqual(self.active_alarms(), [])                 # the start worked
        w.units[edl.LISTENER_UNIT] = "inactive"
        w.units[edl.INHIBIT_UNIT] = "inactive"
        w.start_fails = {edl.LISTENER_UNIT, edl.INHIBIT_UNIT}
        w.systemctl_calls.clear()
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual([c for c in w.systemctl_calls if c[0] == "start"],
                         [["start", edl.LISTENER_UNIT], ["start", edl.INHIBIT_UNIT]])
        self.assertEqual(self.active_alarms(), ["inhibitor_down:run", "listener_down:run"])
        st = self.state()
        self.assertIn("after one start attempt (Job for", st["alarms"]["listener_down:run"]["detail"])
        self.assertEqual([n["ack"] for n in w.alarm_notifies()], [True, True])
        w.start_fails = set()
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(self.active_alarms(), [])

    def test_the_inhibitor_lock_is_verified_in_systemd_inhibit_list(self):
        self.enable([])
        self.world.inhibit_takes_lock = False
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(self.active_alarms(), ["inhibitor_down:run"])
        self.assertIn("not in systemd-inhibit --list", self.state()["alarms"]["inhibitor_down:run"]["detail"])
        self.world.inhibit_takes_lock = True
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(self.active_alarms(), [])

    def test_inhibitor_lock_parser(self):
        self.assertTrue(edl.inhibitor_lock_held(INHIBIT_HEAD + INHIBIT_LINE))
        self.assertFalse(edl.inhibitor_lock_held(INHIBIT_HEAD))
        self.assertFalse(edl.inhibitor_lock_held(INHIBIT_LINE.replace("block", "delay")))
        self.assertFalse(edl.inhibitor_lock_held(INHIBIT_LINE.replace("sleep:idle", "sleep")))
        self.assertFalse(edl.inhibitor_lock_held(None))


# -- the alarm engine (§16.1, P0-8) ---------------------------------------------------------

class _Engine(_Tick):
    """run_alarms over a state, with a Notifier on TickWorld and a commit that records each state."""

    def setUp(self):
        super().setUp()
        self.enable([self.driver("A"), self.driver("B", needs=["A"])])
        self.world.reply["ws-t-driver-g1"] = "ADVANCED: A"
        self.st = self.state()
        self.commits = []

    def notifier(self, can_send=None):
        return edl.Notifier(self.world, self.rp, self.st, workspace=str(self.ws), role=None, epic="t",
                            can_send=can_send)

    def commit(self, st):
        self.commits.append(copy.deepcopy(st))

    def alarms(self, conds, span_s=3600, notifier="default", ledger=None):
        n = self.notifier() if notifier == "default" else notifier
        return edl.run_alarms(self.st, conds, n, self.world.now(), commit=self.commit, ledger=ledger or self.L,
                              span_s=span_s, index_ids=self.world.notify_index_ids)


class TestAlarmEngine(_Engine):
    C = edl.AlarmCondition("check_error", "A", "3 consecutive errors: exit 7")

    def test_a_raise_is_one_ack_notify_with_the_text_of_16_1(self):
        res = self.alarms([self.C])
        self.assertEqual(res.raised, ["check_error:A"])
        n = self.world.notifies
        self.assertEqual(len(n), 1)
        self.assertEqual(n[0]["text"], "[epic-driver t] ALARM check_error A: 3 consecutive errors: exit 7 — epic-driver status")
        self.assertEqual((n[0]["ack"], n[0]["workspace_dir"]), (True, str(self.ws)))
        rec = self.st["alarms"]["check_error:A"]
        self.assertEqual((rec["sends"], rec["message_id"], rec["raised_at"]), (1, 9001, self.NOW))
        self.assertEqual(rec["ladder_until"], "2026-11-24T11:00:00Z")
        # invariant 5: the attempt is committed before the notify, the result after
        self.assertEqual(self.commits[0]["alarms"]["check_error:A"]["last_attempt_at"], self.NOW)
        self.assertIsNone(self.commits[0]["alarms"]["check_error:A"]["message_id"])
        self.assertEqual(self.commits[1]["alarms"]["check_error:A"]["message_id"], 9001)
        self.assertIn("alarm check_error:A ack=yes dispatched=yes message_id=9001", self.rp.notify_log.read_text())

    def test_no_repeat_while_the_ladder_is_live_then_every_4h(self):
        self.alarms([self.C], span_s=3600)
        for minutes in (10, 59, 60, 4 * 60 + 59):           # the ladder (1 h), then 4 h after it ends
            self.world.clock = T(self.NOW) + timedelta(minutes=minutes)
            self.assertEqual(self.alarms([self.C]).repeated, [], minutes)
        self.world.clock = T(self.NOW) + timedelta(hours=5)
        res = self.alarms([self.C])
        self.assertEqual(res.repeated, ["check_error:A"])
        self.assertEqual(len(self.world.notifies), 2)
        self.assertTrue(self.world.notifies[1]["ack"])         # each repeat a new ack=True notify
        self.assertEqual(self.st["alarms"]["check_error:A"]["sends"], 2)
        self.world.clock = T(self.NOW) + timedelta(hours=9, minutes=59)
        self.assertEqual(self.alarms([self.C]).repeated, [])
        self.world.clock = T(self.NOW) + timedelta(hours=10)
        self.assertEqual(self.alarms([self.C]).repeated, ["check_error:A"])

    def test_a_long_ladder_holds_the_repeat_until_it_ends(self):
        span = edl.nudge_span_s("4h,1d,3d,7d*")
        self.assertEqual(span, (4 + 24 + 72 + 168) * 3600)
        self.alarms([self.C], span_s=span)
        self.world.clock = T(self.NOW) + timedelta(seconds=span + 4 * 3600 - 1)
        self.assertEqual(self.alarms([self.C], span_s=span).repeated, [])
        self.world.clock = T(self.NOW) + timedelta(seconds=span + 4 * 3600)
        self.assertEqual(self.alarms([self.C], span_s=span).repeated, ["check_error:A"])

    def test_a_message_gone_from_the_index_lets_the_4h_rule_alone_decide(self):
        self.alarms([self.C], span_s=24 * 3600)
        self.world.index.clear()
        self.world.clock = T(self.NOW) + timedelta(hours=4)
        self.assertEqual(self.alarms([self.C]).repeated, ["check_error:A"])

    def test_ladder_live(self):
        rec = {"message_id": 5, "ladder_until": "2026-11-24T11:00:00Z"}
        self.assertTrue(edl.ladder_live(rec, {"5"}, T("2026-11-24T10:59:59Z")))
        self.assertFalse(edl.ladder_live(rec, {"5"}, T("2026-11-24T11:00:00Z")))
        self.assertFalse(edl.ladder_live(rec, {"6"}, T("2026-11-24T10:00:00Z")))
        self.assertTrue(edl.ladder_live(rec, None, T("2026-11-24T10:00:00Z")))      # unreadable index: span
        self.assertFalse(edl.ladder_live({"message_id": None}, {"5"}, T("2026-11-24T10:00:00Z")))

    def test_a_failed_notify_lands_in_the_log_and_status_and_is_not_retried_in_a_loop(self):
        self.world.notify_ok = False
        res = self.alarms([self.C])
        self.assertEqual(res.failed, ["check_error:A"])
        rec = self.st["alarms"]["check_error:A"]
        self.assertEqual(rec["last_error"], "Relay send failed: relay down")
        self.assertEqual(self.st["notify_failures"][-1]["what"], "alarm check_error:A")
        self.assertIn("dispatched=no message_id=None error=Relay send failed: relay down",
                      self.rp.notify_log.read_text())
        for minutes in (10, 20, 239):
            self.world.clock = T(self.NOW) + timedelta(minutes=minutes)
            self.alarms([self.C])
        self.assertEqual(len(self.world.notifies), 1)          # the next repeat time retries, not the next pass
        self.world.clock = T(self.NOW) + timedelta(hours=4)
        self.world.notify_ok = True
        self.assertEqual(self.alarms([self.C]).repeated, ["check_error:A"])
        # status shows the failure and the alarm's notify error
        self.save(self.st)
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        self.assertIn("NOTIFY FAILURES (last 3)\n  2026-11-24T10:00:00Z alarm check_error:A: Relay send failed: relay down",
                      out)

    def test_notify_failures_keep_the_last_20(self):
        self.world.notify_ok = False
        for i in range(25):
            self.alarms([edl.AlarmCondition("orphan_session", f"n{i}")])
        self.assertEqual(len(self.st["notify_failures"]), 20)
        self.assertEqual(self.st["notify_failures"][0]["what"], "alarm orphan_session:n5")

    def test_a_clear_sends_nothing_and_keeps_the_record(self):
        self.alarms([self.C])
        self.world.clock = T(self.NOW) + timedelta(minutes=10)
        res = self.alarms([])
        self.assertEqual(res.cleared, ["check_error:A"])
        rec = self.st["alarms"]["check_error:A"]
        self.assertEqual((rec["cleared_at"], rec["cleared_by"]), ("2026-11-24T10:10:00Z", "condition"))
        self.assertEqual(len(self.world.notifies), 1)
        # raised again later: a fresh record and a fresh notify
        self.world.clock = T(self.NOW) + timedelta(minutes=20)
        self.assertEqual(self.alarms([self.C]).raised, ["check_error:A"])
        self.assertEqual(self.st["alarms"]["check_error:A"]["raised_at"], "2026-11-24T10:20:00Z")

    def test_no_time_left_defers_the_notify_to_the_next_pass(self):
        res = self.alarms([self.C], notifier=self.notifier(can_send=lambda: False))
        self.assertEqual((res.raised, res.deferred), (["check_error:A"], ["check_error:A"]))
        self.assertIsNone(self.st["alarms"]["check_error:A"]["last_attempt_at"])
        self.assertEqual(self.world.notifies, [])
        res = self.alarms([self.C])
        self.assertEqual(res.repeated, ["check_error:A"])      # never attempted: due at once
        self.assertEqual(len(self.world.notifies), 1)

    def test_a_pass_killed_between_the_attempt_and_its_result_never_sends_twice(self):
        def boom(text, **kw):
            raise Killed()
        with mock.patch.object(self.world, "notify", side_effect=boom):
            with self.assertRaises(Killed):
                self.alarms([self.C])
        self.st = copy.deepcopy(self.commits[-1])               # what the killed pass left on disk
        self.world.clock = T(self.NOW) + timedelta(minutes=10)
        self.assertEqual(self.alarms([self.C]).repeated, [])
        self.assertEqual(self.world.notifies, [])

    def test_alarm_cleared_items_for_a_driver_step_in_flight(self):
        self.st["steps"]["A"]["status"] = "inflight"
        self.alarms([self.C, edl.AlarmCondition("check_error", "B"), edl.AlarmCondition("halt", "A")])
        self.world.clock = T(self.NOW) + timedelta(minutes=10)
        self.alarms([])
        self.assertEqual(self.st["alarm_cleared_pending"], [
            {"alarm": "check_error", "subject": "A", "detail": "cleared: 3 consecutive errors: exit 7",
             "cleared_at": "2026-11-24T10:10:00Z"}])               # B is pending; a halt clears on a verdict
        items = edl.take_alarm_cleared_items(self.st)
        self.assertEqual([(i.kind, i.to, i.step, i.data["alarm"]) for i in items],
                         [("alarm_cleared", "driver", "A", "check_error")])

    def test_the_alarm_cleared_item_reaches_the_driver_in_the_next_pass(self):
        st = self.state()
        self.live(st)
        st["steps"]["A"].update(status="verifying", advanced_at=self.NOW)
        st["alarm_cleared_pending"] = [{"alarm": "check_error", "subject": "A", "detail": "cleared", "cleared_at": self.NOW}]
        self.save(st)
        self.assertEqual(self.run_pass(), 0, self.last_err)
        st = self.state()
        self.assertEqual(st["alarm_cleared_pending"], [])
        entry = st["outbox"][-1]
        self.assertEqual(entry["kinds"], ["alarm_cleared"])
        self.assertIn("alarm_cleared — check_error A", (self.rp.events / f"{entry['event']}.md").read_text())

    def test_mark_clears_a_still_true_condition_until_it_has_gone_once(self):
        cond = edl.AlarmCondition("deadline_missed", "A", "deadline passed")
        self.alarms([cond])
        cleared = edl.clear_alarms_by_mark(self.st, ["A"], self.world.now())
        self.assertEqual(cleared, ["deadline_missed:A"])
        for minutes in (10, 20):
            self.world.clock = T(self.NOW) + timedelta(minutes=minutes)
            self.assertEqual(self.alarms([cond]).raised, [])
        self.alarms([])                                         # the condition went (the step was done)
        self.world.clock = T(self.NOW) + timedelta(minutes=30)
        self.assertEqual(self.alarms([cond]).raised, ["deadline_missed:A"])   # a new occurrence missed it
        self.assertEqual(len(self.world.notifies), 2)


# -- the §16.2 catalogue: every id raised and cleared through the engine --------------------

def _arch_catalogue_ids():
    text = ARCHITECTURE.read_text(encoding="utf-8")
    block = text[text.index("### 16.2 Catalogue"):text.index("### 16.3")]
    return [m.group(1) for m in re.finditer(r"^\| `([a-z_]+)` \|", block, re.M)]


class TestAlarmCatalogue(_Engine):
    """Each row of §16.2: its producer's condition raised through the engine, then cleared."""

    def test_the_catalogue_is_the_architectures(self):
        self.assertEqual(list(edl.ALARM_IDS), _arch_catalogue_ids())
        self.assertEqual(len(edl.ALARM_IDS), 29)

    def cycle(self, key, produce, make_true, make_false):
        """``produce(st, now)`` → conditions; ``make_true``/``make_false`` change the state."""
        make_true(self.st)
        res = self.alarms(produce(self.st, self.world.now()))
        self.assertIn(key, res.raised, key)
        self.world.advance(600)
        make_false(self.st)
        res = self.alarms(produce(self.st, self.world.now()))
        rec = self.st["alarms"][key]
        self.assertTrue(key in res.cleared or rec.get("cleared_by") == "mark", key)   # by its condition, or by mark
        self.assertIsNotNone(rec["cleared_at"], key)
        self.assertNotIn(key, res.raised, key)

    def steps(self, st, now, sel=None):
        return edl.step_alarm_conditions(self.L, st, now, sel)

    def test_step_rows(self):
        L = self.L

        def rec(st, sid="A"):
            return st["steps"][sid]
        with self.subTest("step_timeout"):   # cleared by a verdict
            self.cycle("step_timeout:A", self.steps,
                       lambda st: rec(st).update(status="inflight", started_at="2026-11-24T03:00:00Z"),
                       lambda st: rec(st).update(status="verifying", advanced_at=self.NOW))
        with self.subTest("attempts_exhausted"):   # cleared by mark reset
            def reset(st):
                out = edl.mark_reset(L, st, "A", self.world.now())
                edl.clear_alarms_by_mark(st, ["A"], self.world.now(), keys=out["cleared_alarms"])
            self.cycle("attempts_exhausted:A", self.steps, lambda st: rec(st).update(status="failed", attempts=3), reset)
        with self.subTest("check_error"):   # cleared by a run returning 0, 1 or 2
            def errors(st):
                rec(st).update(status="verifying", advanced_at=self.NOW)
                rec(st)["check"].update(consecutive_errors=3, last_error="exit 7")
            job = edl.CheckJob("A", edl.KIND_CHECK, "true", 60)
            self.cycle("check_error:A", self.steps, errors, lambda st: edl.apply_check_result(
                L, st, job, edl.CheckRun(rc=1, started_at=self.world.now(), duration_s=1), self.world.now()))
        with self.subTest("halt"):   # cleared by a later verdict
            self.cycle("halt:A", self.steps, lambda st: rec(st).update(status="halted", last_verdict={"line": "HALT: x"}),
                       lambda st: rec(st).update(status="verifying"))
        with self.subTest("due_not_started"):   # cleared on dispatch
            def due(st):
                rec(st).update(status="due", due_since=self.NOW, undispatched_ticks=2)
            def prod(st, now):
                return self.steps(st, now, edl.Selection(due=[edl.DueStep("A", T(self.NOW), 0, "due")],
                                                         held={"A": [edl.Hold(edl.HOLD_RESOURCES, "busy")]}))
            self.cycle("due_not_started:A", prod, due, lambda st: rec(st).update(status="inflight", undispatched_ticks=0))

    def test_deadline_rows(self):
        self.enable([self.driver("A", not_after="2026-11-25T00:00:00Z", warn_before="1d")])
        self.st = self.state()
        self.world.clock = T("2026-11-24T00:00:00Z")
        res = self.alarms(self.steps(self.st, self.world.now()))
        self.assertEqual(res.raised, ["deadline_near:A"])
        self.world.clock = T("2026-11-25T00:00:00Z")
        res = self.alarms(self.steps(self.st, self.world.now()))
        self.assertEqual((res.raised, res.cleared), (["deadline_missed:A"], ["deadline_near:A"]))
        edl.mark_done(self.L, self.st, "A", self.world.now())
        res = self.alarms(self.steps(self.st, self.world.now()))
        self.assertEqual(res.cleared, ["deadline_missed:A"])     # done
        # deadline_near cleared by the step getting done before the deadline
        self.st = self.state()
        self.world.clock = T("2026-11-24T00:00:00Z")
        self.assertEqual(self.alarms(self.steps(self.st, self.world.now())).raised, ["deadline_near:A"])
        self.st["steps"]["A"].update(status="done", done_at="2026-11-24T06:00:00Z", by="check")
        self.world.clock = T("2026-11-24T06:00:00Z")
        self.assertEqual(self.alarms(self.steps(self.st, self.world.now())).cleared, ["deadline_near:A"])

    def test_dispatch_rows(self):
        def disp(st, now):
            return edl.dispatch_conditions(self.L, st, now)
        entry = {"event": "E-5", "to": "driver", "session": "ws-t-driver-g1", "kinds": ["step_due"], "steps": ["A"],
                 "sends": [], "deferrals": 3, "confirmed_at": None, "verdict": None}
        with self.subTest("delivery_deferred"):
            self.cycle("delivery_deferred:E-5", disp, lambda st: st["outbox"].append(copy.deepcopy(entry)),
                       lambda st: st["outbox"][0].update(sends=[self.NOW], confirmed_at=self.NOW))
        self.st["outbox"] = []
        with self.subTest("delivery_unconfirmed"):
            self.cycle("delivery_unconfirmed:E-5", disp,
                       lambda st: st["outbox"].append(dict(entry, deferrals=0, sends=["a", "b"], unconfirmed_at=self.NOW)),
                       lambda st: st["outbox"][0].update(confirmed_at=self.NOW))
        self.st["sessions"]["driver"] = {"name": "ws-t-driver-g1", "gen": 1, "status": "live", "parked": []}
        with self.subTest("handoff_timeout"):
            self.cycle("handoff_timeout:ws-t-driver-g1", disp,
                       lambda st: st["sessions"]["driver"].update(handoff_timeout={"at": self.NOW, "event": "E-6"}),
                       lambda st: st["sessions"]["driver"].update(name="ws-t-driver-g2", handoff_timeout=None))
        self.st["sessions"]["driver"] = {"name": "ws-t-driver-g1", "gen": 1, "status": "live", "parked": []}
        for alarm, via in (("spawn_failed", "spawn"), ("resume_failed", "resume")):
            with self.subTest(alarm):
                self.cycle(f"{alarm}:ws-t-driver-g1", disp,
                           lambda st, v=via: st["sessions"]["driver"].update(
                               launch_error={"kind": v, "at": self.NOW, "reason": "failed", "error": "rc 1"}),
                           lambda st: st["sessions"]["driver"].update(launch_error=None))

    def test_worktree_refused(self):
        repo = self.tmp / "repo"
        repo.mkdir(exist_ok=True)
        self.world.repos[str(repo)] = {"refs": {"main"}}
        roles = {r: {"model": "claude-opus-5-5", "effort": "high"} for r in ("driver", "impl", "rev")}
        self.enable([self.step("W", role={"worker": {"implementer": "impl", "reviewer": "rev", "repos": [
            {"path": str(repo), "base_ref": "main"}]}}), self.driver("A")], roles=roles)
        self.st = self.state()

        def prod(st, now):
            return edl.worktree_conditions(self.L, st)
        self.cycle("worktree_refused:W", prod,
                   lambda st: st.setdefault("worktrees", {}).setdefault("steps", {}).update(
                       {"W": {"dir": None, "repos": {}, "branch": None, "refused": "base main does not resolve"}}),
                   lambda st: st["worktrees"]["steps"]["W"].update(refused=None, dir=str(self.tmp / "wt")))

    def test_run_rows_are_raised_and_cleared_by_real_passes(self):
        # tick_error, stamp_mismatch, tmux_server, listener_down and inhibitor_down are cycled by
        # TestTickCrash, TestStamps, TestTmuxServer and TestInfrastructure; ledger_invalid here.
        self.st = None
        self.assertEqual(self.run_pass(), 0, self.last_err)
        good = Path(self.lpath).read_text()
        Path(self.lpath).write_text(good.replace("version: 1", "version: 2"))
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        st = self.state()
        self.assertEqual(self.active_alarms(st), ["ledger_invalid:run"])
        self.assertIn("version: must be 1, got 2; running on ledger.last_valid.yaml",
                      st["alarms"]["ledger_invalid:run"]["detail"])
        self.assertEqual(self.rp.last_valid_ledger.read_text(), good)       # the copy is the last valid one
        Path(self.lpath).write_text(good)
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(self.active_alarms(), [])

    def test_an_invalid_ledger_without_a_valid_copy_is_a_tick_error(self):
        os.unlink(self.rp.last_valid_ledger)
        Path(self.lpath).write_text("version: 2\n")
        self.assertEqual(self.run_pass(), 1)
        self.assertEqual(self.active_alarms(), ["tick_error:run"])
        self.assertIn("no valid copy exists", self.state()["last_tick_error"]["error"])


# -- enable (§18.1) ----------------------------------------------------------------------

class TestEnable(_Tick):
    def setUp(self):
        super().setUp()
        self.lpath = self.write(self.ledger_dict([self.driver("A")]))
        self.paths = edl.run_paths("epic-t")
        # `enable` refuses while EPIC_DRIVER_ALLOW_UNSTAMPED is set (gate 2), so these runs have stamps that
        # agree (a fake manifest check) instead of the override.
        self.env.pop(CLI.ALLOW_UNSTAMPED_ENV, None)
        self.stamps_patch = mock.patch.object(
            CLI, "check_stamps", side_effect=lambda lay, environ: {
                "checked": True, "ok": True, "problem": None, "revision": "t", "files": [],
                "override_env": bool(environ.get(CLI.ALLOW_UNSTAMPED_ENV))})
        self.stamps_patch.start()
        self.addCleanup(mock.patch.stopall)
        self.env.update({"PATH": "/home/x/.cargo/bin:/usr/local/go/bin:/usr/bin", "GOPATH": "/home/x/go",
                         "NVM_DIR": "/home/x/.nvm", "NVM_BIN": "/home/x/.nvm/v22/bin", "NVM_AUTH_TOKEN": "secret",
                         "SSH_AUTH_SOCK": "/run/user/1000/ssh", "VIRTUAL_ENV": "/venv", "EDITOR": "vim",
                         "ANTHROPIC_API_KEY": "sk-x"})

    def enable_cli(self, stamps_env=None):
        return self.cli("enable", self.lpath, environ=stamps_env)

    def assertNothingEnabled(self):
        self.assertNotIn("enable", [c[0] for c in self.world.systemctl_calls])
        self.assertNotEqual(self.world.units.get(edl.TIMER_UNIT), "active")
        self.assertIsNone(edl.read_active())

    def test_all_gates_pass(self):
        rc, out, err = self.enable_cli()
        self.assertEqual(rc, 0, err)
        w = self.world
        self.assertIn(["enable", "--now", edl.TIMER_UNIT, edl.INHIBIT_UNIT], w.systemctl_calls)
        self.assertEqual(w.units[edl.TIMER_UNIT], "active")
        active = edl.read_active()
        self.assertEqual({k: active[k] for k in ("run_id", "ledger", "state_dir", "workspace", "epic")},
                         {"run_id": "epic-t", "ledger": self.lpath, "state_dir": str(self.paths.dir),
                          "workspace": str(self.ws), "epic": "t"})
        self.assertEqual(json.loads((self.plan / "epic-driver.run.json").read_text()),
                         {"run_id": "epic-t", "state_dir": str(self.paths.dir)})
        self.assertEqual(edl.load_state(self.paths.state)["run_id"], "epic-t")
        self.assertEqual(self.paths.last_valid_ledger.read_text(), Path(self.lpath).read_text())
        snap = json.loads(self.paths.env_snapshot.read_text())
        self.assertEqual(snap, {"PATH": "/home/x/.cargo/bin:/usr/local/go/bin:/usr/bin", "GOPATH": "/home/x/go",
                                "NVM_DIR": "/home/x/.nvm", "NVM_BIN": "/home/x/.nvm/v22/bin", "EDITOR": "vim",
                                "LANG": "C.UTF-8"})
        self.assertEqual([(n["text"], n["ack"]) for n in w.notifies],
                         [("[epic-driver t] test notify from epic-driver enable (no action needed)", False),
                          ("[epic-driver t] epic-driver on for t", False)])
        self.assertIn("on for t", out)
        # enabling again (the same ledger) keeps the state
        st = edl.load_state(self.paths.state)
        st["tick_seq"] = 7
        edl.save_state(self.paths.state, st)
        self.assertEqual(self.enable_cli()[0], 0)
        self.assertEqual(edl.load_state(self.paths.state)["tick_seq"], 7)

    def gate_fails(self, n, text, setup, env=None):
        setup()
        before = edl.read_active()
        rc, out, err = self.enable_cli(env)
        self.assertEqual(rc, 3, (out, err))
        self.assertIn(f"refused: gate {n} ", err)
        self.assertIn(text, err)
        self.assertIn("fix:", err)
        # the pointer file (the one write into the tracked tree) is not left behind by a refusal
        self.assertFalse((self.plan / "epic-driver.run.json").exists(), f"gate {n}: pointer file left behind")
        if n == 10:      # enabled, then rolled back
            self.assertEqual(self.world.units[edl.TIMER_UNIT], "inactive")
            self.assertIsNone(edl.read_active())
        elif n == 1:     # the other run stays as it was
            self.assertEqual(edl.read_active(), before)
            self.assertNotIn("enable", [c[0] for c in self.world.systemctl_calls])
        else:
            self.assertNothingEnabled()

    def test_each_gate_failing_in_turn_refuses_and_leaves_no_timer_enabled(self):
        w = self.world
        cases = [
            (1, "run epic-other", lambda: edl.write_active({"run_id": "epic-other", "ledger": "/x/other.yaml"})),
            (3, "version: must be 1", lambda: Path(self.lpath).write_text(Path(self.lpath).read_text().replace(
                "version: 1", "version: 9"))),
            (4, "claude-questions-listen.service is not active", lambda: w.units.update({edl.LISTENER_UNIT: "inactive"})),
            (5, "no [questions] section", lambda: setattr(w, "settings", None)),
            (5, "claude-questions --check", lambda: setattr(w, "qcheck", edl.CommandResult(rc=2, stdout="not bound"))),
            (5, 'sync = "push"', lambda: setattr(w, "settings", {"nudge": "1h", "sync": "push"})),
            (6, "Relay send failed: relay down", lambda: setattr(w, "notify_ok", False)),
            (7, "could not be moved into epic-driver-tmux.scope (Call failed: Access denied)",
             lambda: (w.cgroups.update({16651: TERM_CG}), setattr(w, "busctl_moves", False))),
            (10, "systemctl --user enable --now failed", lambda: setattr(w, "enable_fails", True)),
            (10, "no sleep:idle block lock", lambda: setattr(w, "inhibit_takes_lock", False)),
        ]
        good = Path(self.lpath).read_text()
        for n, text, setup in cases:
            with self.subTest(gate=n, text=text):
                fresh = TickWorld(self.tdir, now=T(self.NOW))
                self.world = w = fresh
                Path(self.lpath).write_text(good)
                edl.remove_active()
                self.gate_fails(n, text, setup)
                if n == 10:
                    self.assertIn(["disable", "--now", edl.TIMER_UNIT, edl.INHIBIT_UNIT], w.systemctl_calls)
                    self.assertEqual(w.units[edl.INHIBIT_UNIT], "inactive")     # rolled back
        with self.subTest(gate=2):
            self.world = TickWorld(self.tdir, now=T(self.NOW))
            edl.remove_active()
            env = {k: v for k, v in self.env.items() if k != CLI.ALLOW_UNSTAMPED_ENV}
            self.stamps_patch.stop()                      # the real check: no manifest in a repo run
            try:
                rc, out, err = self.enable_cli(env)
            finally:
                self.stamps_patch.start()
            self.assertEqual(rc, 3)
            self.assertIn("stamp_mismatch", err)          # the CLI refuses before the lib is imported
            self.assertNothingEnabled()
            out2, err2 = io.StringIO(), io.StringIO()
            ns = CLI.parse(["enable", self.lpath])
            self.assertEqual(edl.cli(ns, self.world, {"checked": True, "ok": False, "problem": "x changed"},
                                     environ=env, out=out2, err=err2), 3)
            self.assertIn("refused: gate 2 (stamps): x changed", err2.getvalue())

    def test_a_server_in_a_terminal_scope_is_moved(self):
        self.world.cgroups[16651] = TERM_CG
        rc, out, err = self.enable_cli()
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.world.busctl_calls[0][4], "StartTransientUnit")
        self.assertEqual(self.world.cgroups[16651], OWN_CG)
        self.assertIn("moved the tmux server (pid 16651) into epic-driver-tmux.scope", out)

    def test_the_move_refusal_prints_instructions(self):
        self.world.cgroups[16651] = TERM_CG
        self.world.busctl_moves = False
        rc, out, err = self.enable_cli()
        self.assertEqual(rc, 3)
        self.assertIn("tmux kill-server", err)
        self.assertIn("busctl --user call org.freedesktop.systemd1", err)

    def pointer(self):
        return self.plan / "epic-driver.run.json"

    def test_gate_8_the_snapshot_cannot_be_written(self):
        with mock.patch.object(edl, "write_env_snapshot", side_effect=OSError("No space left on device")):
            rc, out, err = self.enable_cli()
        self.assertEqual(rc, 3, err)
        self.assertIn("refused: gate 8 (environment snapshot): cannot write it: No space left on device", err)
        self.assertNothingEnabled()
        self.assertFalse(self.pointer().exists())

    def test_gate_9_an_unreadable_state_refuses(self):
        self.paths.dir.mkdir(parents=True, exist_ok=True)
        self.paths.state.write_text("{ this is not json")
        rc, out, err = self.enable_cli()
        self.assertEqual(rc, 3, err)
        self.assertIn("refused: gate 9 (state)", err)
        self.assertNothingEnabled()
        self.assertFalse(self.pointer().exists())

    def test_gate_10_removes_the_pointer_file_this_run_created(self):
        self.world.enable_fails = True
        rc, out, err = self.enable_cli()
        self.assertEqual(rc, 3, err)
        self.assertIn("refused: gate 10 ", err)
        self.assertNotEqual(self.world.units.get(edl.TIMER_UNIT), "active")
        self.assertIsNone(edl.read_active())
        self.assertFalse(self.pointer().exists())               # created by this run: removed again
        self.assertTrue(self.paths.state.exists())              # state-dir files may stay

    def test_gate_10_inhibitor_rollback_also_removes_it(self):
        self.world.inhibit_takes_lock = False
        rc, out, err = self.enable_cli()
        self.assertEqual(rc, 3, err)
        self.assertEqual(self.world.units[edl.TIMER_UNIT], "inactive")
        self.assertFalse(self.pointer().exists())

    def test_gate_10_leaves_a_pointer_file_that_was_already_there(self):
        self.pointer().write_text('{"run_id": "older"}\n')
        self.world.enable_fails = True
        rc, out, err = self.enable_cli()
        self.assertEqual(rc, 3, err)
        self.assertEqual(self.pointer().read_bytes(), b'{"run_id": "older"}\n')   # restored byte for byte

    def test_a_refusal_restores_a_pre_existing_pointer_byte_for_byte_and_its_mode(self):
        """Gate 9 overwrites the pointer with this run's body; a later refusal puts the old bytes back
        (not valid JSON, no trailing newline, a non-default mode), for both gate 10 refusals."""
        prior = b'not json \xff\xfe, no newline'
        for fail in ("units", "inhibitor"):
            with self.subTest(fail=fail):
                self.pointer().write_bytes(prior)
                os.chmod(self.pointer(), 0o640)
                self.world.enable_fails = fail == "units"
                self.world.inhibit_takes_lock = fail != "inhibitor"
                rc, out, err = self.enable_cli()
                self.assertEqual(rc, 3, err)
                self.assertIn("refused: gate 10 ", err)
                self.assertEqual(self.pointer().read_bytes(), prior)
                self.assertEqual(self.pointer().stat().st_mode & 0o777, 0o640)
                self.assertEqual([p.name for p in self.plan.iterdir() if p.name.endswith(".tmp")], [])
        self.pointer().unlink()

    def test_a_refusal_without_a_prior_pointer_removes_the_file_it_wrote(self):
        self.assertFalse(self.pointer().exists())
        self.world.enable_fails = True
        self.assertEqual(self.enable_cli()[0], 3)
        self.assertFalse(self.pointer().exists())

    def test_gate_2_refuses_while_the_test_override_is_set(self):
        for how in ("cli", "lib"):
            with self.subTest(how=how):
                env = dict(self.env, **{CLI.ALLOW_UNSTAMPED_ENV: "1"})
                if how == "cli":
                    rc, out, err = self.enable_cli(env)
                else:
                    err_io = io.StringIO()
                    rc = edl.cli(CLI.parse(["enable", self.lpath]), self.world,
                                 {"checked": False, "ok": True, "problem": None, "revision": None},
                                 environ=env, out=io.StringIO(), err=err_io)
                    err = err_io.getvalue()
                self.assertEqual(rc, 3, err)
                self.assertIn("refused: gate 2 (stamps): EPIC_DRIVER_ALLOW_UNSTAMPED is set", err)
                self.assertIn("fix: unset EPIC_DRIVER_ALLOW_UNSTAMPED", err)
                self.assertNothingEnabled()
                self.assertFalse(self.pointer().exists())


# -- disable, pause/resume, mark, grant, queue (§18) ----------------------------------------

class TestVerbs(_Tick):
    def test_disable_stops_both_units_keeps_sessions_and_state_and_notifies_once(self):
        self.enable()
        st = self.state()
        self.live(st)
        self.save(st)
        self.world.units[edl.TIMER_UNIT] = "active"
        rc, out, err = self.cli("disable")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.world.systemctl_calls, [["disable", "--now", edl.TIMER_UNIT, edl.INHIBIT_UNIT]])
        self.assertIsNone(edl.read_active())
        self.assertEqual(self.state()["sessions"]["driver"]["name"], "ws-t-driver-g1")
        self.assertEqual((self.world.reaps, self.world.sends), ([], []))
        self.assertEqual([(n["text"], n["ack"]) for n in self.world.notifies], [("[epic-driver t] epic-driver off for t", False)])
        self.assertEqual(self.run_pass(), 0)                    # a stray tick after disable does nothing
        self.assertIn("no active run", self.last_out)
        self.assertEqual(self.cli("disable")[0], 0)

    def test_pause_starts_nothing_but_the_tick_still_watches_and_alarms(self):
        self.enable([self.driver("A", check="true")])
        rc, out, err = self.cli("pause", "--reason", "deploy freeze")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(self.rp.paused.read_text())["reason"], "deploy freeze")
        self.world.units[edl.LISTENER_UNIT] = "inactive"
        self.world.start_fails = {edl.LISTENER_UNIT}
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(self.world.spawns, [])
        self.assertEqual(self.active_alarms(), ["listener_down:run"])
        self.assertEqual(self.state()["steps"]["A"]["status"], "due")
        self.assertIn("PAUSED since", self.cli("status")[1])
        self.assertEqual(self.cli("resume")[0], 0)
        self.assertFalse(self.rp.paused.exists())
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(len(self.world.spawns), 1)

    def test_mark_done_and_reset(self):
        self.enable([self.driver("A", max_attempts=2)])
        st = self.state()
        st["steps"]["A"].update(status="failed", attempts=2)
        st["alarms"]["attempts_exhausted:A"] = {"id": "attempts_exhausted", "subject": "A", "cleared_at": None}
        self.save(st)
        rc, out, err = self.cli("mark", "A", "reset")
        self.assertEqual(rc, 0, err)
        st = self.state()
        self.assertEqual((st["steps"]["A"]["status"], st["steps"]["A"]["attempts"]), ("pending", 0))
        self.assertEqual(st["alarms"]["attempts_exhausted:A"]["cleared_by"], "mark")
        rc, out, err = self.cli("mark", "A", "done")
        self.assertEqual(rc, 0, err)
        st = self.state()
        self.assertEqual((st["steps"]["A"]["status"], st["steps"]["A"]["by"]), ("done", "operator"))
        self.assertEqual(self.cli("mark", "nope", "done")[0], 3)
        self.assertEqual(self.cli("mark", "A")[0], 2)            # usage
        self.assertEqual(self.cli("mark", "A", "done", "--note", "x")[0], 2)
        self.assertEqual(self.cli("mark", "A", "done", "--grant", "SA1")[0], 2)

    def test_mark_grant_is_the_only_writer_of_grants(self):
        self.enable([self.driver("A", approval="SA4")], approvals={"SA4": {"conditions": []}})
        rc, out, err = self.cli("mark", "--grant", "SA4", "--question", "Q-17", "--note", "yes from Telegram")
        self.assertEqual(rc, 0, err)
        g = self.state()["grants"]["SA4"]
        self.assertEqual((g["question"], g["note"], g["by"], g["granted_at"]), ("Q-17", "yes from Telegram", "mark", self.NOW))
        self.assertIn("SA4 granted", out)
        rc, out, err = self.cli("mark", "--grant", "SA9")
        self.assertEqual(rc, 3)
        self.assertIn("unknown approval 'SA9'", err)
        self.assertEqual(self.cli("mark", "--grant", "SA4")[0], 0)      # idempotent

    def test_queue_stores_the_request_for_the_dispatcher(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        self.world.repos[str(repo)] = {"refs": {"main"}}
        roles = {r: {"model": "claude-opus-5-5", "effort": "high"} for r in ("driver", "impl", "rev")}
        self.enable([self.step("W", role={"worker": {"implementer": "impl", "reviewer": "rev",
                                                     "repos": [{"path": str(repo), "base_ref": "main"}]}})],
                    roles=roles)
        fb = self.tmp / "feedback.md"
        rc, out, err = self.cli("queue", "W", "--to", "impl", "--file", str(fb))
        self.assertEqual(rc, 0, err)
        q = self.state()["queue"]
        self.assertEqual([(r["id"], r["step"], r["to"], r["file"], r["event"]) for r in q],
                         [("R-1", "W", "impl", str(fb), None)])
        self.assertIn("queued R-1 for impl:W", out)
        self.assertEqual(self.cli("queue", "W", "--to", "driver", "--file", str(fb))[0], 3)
        self.assertEqual(self.cli("queue", "W", "--to", "impl")[0], 2)

    def test_verbs_without_an_active_run_refuse(self):
        for argv in (("pause",), ("resume",), ("mark", "A", "done"), ("mark", "--grant", "SA4"),
                     ("queue", "W", "--to", "impl", "--file", "/x")):
            with self.subTest(argv=argv):
                rc, out, err = self.cli(*argv)
                self.assertEqual(rc, 3)
                self.assertIn("no active run", err)
        self.assertEqual(self.cli("status")[1].strip(), "epic-driver: off (no active run)")
        self.assertEqual(json.loads(self.cli("status", "--json")[1]), {"enabled": False})

    def test_usage_errors_exit_2(self):
        for argv in ((), ("frobnicate",), ("validate",), ("queue", "W")):
            with self.subTest(argv=argv):
                with mock.patch("sys.stderr", io.StringIO()):
                    self.assertEqual(self.cli(*argv)[0], 2)

    def test_validate(self):
        good = self.write(self.ledger_dict([self.driver("A")]))
        rc, out, err = self.cli("validate", good)
        self.assertEqual(rc, 0, err)
        bad = self.write(self.ledger_dict([self.driver("A", needs=["Z"])]), name="bad.yaml")
        rc, out, err = self.cli("validate", bad)
        self.assertEqual(rc, 1)
        self.assertIn("error: [graph]", err)


# -- heartbeats (§16.5, P0-11) ------------------------------------------------------------

class TestHeartbeats(_Tick):
    def test_none_on_a_quiet_day(self):
        self.enable([self.driver("A", phase="P1", not_before="2026-12-01T00:00:00Z")])
        for _ in range(144):                       # a day of ticks, nothing due, nothing wrong
            self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(self.world.notifies, [])
        self.assertEqual(self.state()["tick_seq"], 144)

    def test_one_per_phase_transition_recorded_before_the_notify(self):
        self.enable([self.driver("A", phase="E1"), self.driver("B", phase="E1"),
                     self.driver("C", phase="S0"), self.driver("D", phase="S0"), self.driver("E", phase="S0")])
        st = self.state()
        edl.mark_done(self.L, st, "A", self.world.now())
        commits = []
        n = edl.Notifier(self.world, self.rp, st, workspace=str(self.ws), role=None, epic="t")
        self.assertEqual(edl.run_heartbeats(self.L, st, n, commit=lambda s: commits.append(copy.deepcopy(s))), [])
        edl.mark_done(self.L, st, "B", self.world.now())
        self.assertEqual(edl.run_heartbeats(self.L, st, n, commit=lambda s: commits.append(copy.deepcopy(s))), ["E1"])
        self.assertEqual(commits[0]["phases"]["announced"], ["E1"])
        self.assertEqual([(x["text"], x["ack"]) for x in self.world.notifies],
                         [("[epic-driver t] phase E1 done; next: S0 (3 steps)", False)])
        self.assertEqual(edl.run_heartbeats(self.L, st, n, commit=lambda s: None), [])   # announced once


# -- status (§20) ---------------------------------------------------------------------------

class TestStatus(_Tick):
    def fixture(self):
        self.enable([self.driver("e1-roll", phase="E1", check="true"), self.step("e1-soak", phase="E1",
                     check="true", needs=["e1-roll"]), self.driver("monterey", phase="R",
                     not_after="2026-11-29T09:28:20Z")])
        st = self.state()
        st["steps"]["e1-roll"].update(status="done", done_at="2026-11-24T07:12:40Z", by="check",
                                      check={"last_run_at": "2026-11-24T07:20:00Z", "last_rc": 0,
                                             "consecutive_errors": 0, "output_tail": "", "last_error": None})
        st["steps"]["e1-soak"].update(status="watching", due_since="2026-11-24T07:12:40Z",
                                      check={"last_run_at": "2026-11-24T09:30:00Z", "last_rc": 1,
                                             "consecutive_errors": 0, "output_tail": "", "last_error": None})
        st["steps"]["monterey"].update(status="inflight", started_at="2026-11-24T09:30:05Z",
                                       inflight={"step": "monterey", "event": "E-41", "started_at": "2026-11-24T09:30:05Z"})
        st["outbox"].append({"event": "E-41", "to": "driver", "session": "ws-t-driver-g1", "kinds": ["step_due"],
                             "steps": ["monterey"], "sends": ["2026-11-24T09:30:06Z"], "deferrals": 0,
                             "confirmed_at": "2026-11-24T09:30:20Z", "verdict": None})
        st["sessions"]["driver"] = {"name": "ws-t-driver-g1", "gen": 2, "status": "live", "model_verified": True,
                                    "parked": []}
        st["questions"]["Q-17"] = {"approval": "SA4", "status": "resolved"}
        st["grants"]["SA4"] = {"granted_at": "2026-11-23T19:02:00Z", "question": "Q-17", "by": "mark"}
        st["last_tick_at"] = st["last_tick_ok_at"] = "2026-11-24T09:58:00Z"
        st["alarms"]["check_error:e1-soak"] = {"id": "check_error", "subject": "e1-soak", "detail": "3 errors",
                                               "raised_at": "2026-11-24T09:00:00Z", "sends": 1, "cleared_at": None}
        st["notify_failures"] = [{"at": "2026-11-24T09:00:01Z", "what": "alarm check_error:e1-soak",
                                  "error": "Relay send failed: down"}]
        self.save(st)
        self.world.units[edl.TIMER_UNIT] = "active"

    def test_text(self):
        self.fixture()
        rc, out, err = self.cli("status")
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "epic-driver t · run epic-t · ON (timer active, next Tue 2026-11-24 09:40:00 UTC; "
                                   "inhibitor held) · not paused")
        self.assertEqual(lines[1], "last tick 2026-11-24T09:58:00Z (2m ago) ok · last error: none")
        self.assertEqual(lines[2], "tmux server 16651 in epic-driver-tmux.scope · listener active · "
                                   "stamps unchecked (EPIC_DRIVER_ALLOW_UNSTAMPED)")
        self.assertEqual(lines[3], "phase E1: 1/2 done")
        self.assertIn("  e1-roll              done        11-24 07:12  check rc 0 07:20", lines)
        self.assertIn("  e1-soak              watching    11-24 07:12  check rc 1 09:30", lines)
        self.assertIn("  monterey             inflight    11-24 09:30  E-41 confirmed; deadline 11-29 09:28Z", lines)
        self.assertIn("  E-41 → driver     sent 09:30:06 · confirmed 09:30:20 · verdict pending", lines)
        self.assertIn("  Q-17 SA4 resolved (granted 11-23 19:02)", lines)
        self.assertIn("  check_error e1-soak  raised 11-24 09:00 · sends 1 · 3 errors", lines)
        self.assertIn("  2026-11-24T09:00:01Z alarm check_error:e1-soak: Relay send failed: down", lines)
        self.assertTrue(any(l.startswith("  driver           ws-t-driver-g1") and l.endswith("2    ok") for l in lines),
                        out)

    def test_json(self):
        self.fixture()
        rc, out, err = self.cli("status", "--json")
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual((data["epic"], data["run_id"], data["timer"]["active"], data["inhibitor"]["lock"]),
                         ("t", "epic-t", True, True))
        self.assertEqual(data["last_tick_at"], "2026-11-24T09:58:00Z")
        self.assertEqual(data["last_tick_ago_s"], 120)
        self.assertIsNone(data["last_tick_error"])
        self.assertEqual(data["tmux"]["pid"], 16651)
        self.assertTrue(data["tmux"]["own_scope"])
        self.assertEqual(data["phase"], {"name": "E1", "done": 1, "total": 2})
        self.assertEqual([s["id"] for s in data["steps"]], ["e1-roll", "e1-soak", "monterey"])
        self.assertEqual([a["key"] for a in data["alarms"]], ["check_error:e1-soak"])
        self.assertEqual(data["notify_failures"][0]["error"], "Relay send failed: down")

    def test_a_stale_tick_and_the_last_error_show(self):
        self.fixture()
        st = self.state()
        st["last_tick_error"] = {"at": "2026-11-24T09:58:00Z", "error": "RuntimeError: boom"}
        self.save(st)
        self.world.clock = T("2026-11-24T12:00:00Z")
        out = self.cli("status")[1]
        self.assertIn("last tick 2026-11-24T09:58:00Z (2h2m ago) ERROR · last error: 2026-11-24T09:58:00Z "
                      "RuntimeError: boom", out)


# -- the time bound (§22 TimeoutStartSec=5min, review m5) --------------------------------------

class TestTickTiming(_Tick):
    """``Done when``: one pass ends within 5 min on the toy ledger with the check budget saturated.

    The fake clock advances by each call's worst case: a check by its full timeout (60 s), a spawn
    by its full SPAWN_TIMEOUT_S, a notify by NOTIFY_TIMEOUT_S, every systemctl, systemd-inhibit and
    tmux read by its timeout. ``World.now`` is the pass's only clock, so the elapsed time it measures
    is the time the real pass would take in that worst case.
    """

    def worst_case(self):
        self.world.cost = {"systemctl": edl.SYSTEMCTL_TIMEOUT_S, "inhibit_list": edl.INHIBIT_LIST_TIMEOUT_S,
                           "tmux_server_pid": edl.TMUX_READ_TIMEOUT_S, "systemd_run": edl.SYSTEMD_RUN_TIMEOUT_S,
                           "notify": edl.NOTIFY_TIMEOUT_S}
        orig_spawn = self.world.spawn

        def slow_spawn(args):
            self.world.advance(edl.SPAWN_TIMEOUT_S)
            return orig_spawn(args)
        self.world.spawn = slow_spawn

    def saturated(self, n=8):
        self.enable([self.driver("A")] + [self.step(f"w{i}", check={"run": "true", "timeout": "60s"})
                                          for i in range(n)])
        st = self.state()
        for i in range(n):
            st["steps"][f"w{i}"].update(status="watching", due_since=self.NOW)
        self.save(st)
        self.world.check_script.update({f"w{i}": (1, 60) for i in range(n)})   # every check runs to its timeout
        self.world.units[edl.LISTENER_UNIT] = "inactive"                        # one start attempt, then an alarm
        self.world.start_fails = {edl.LISTENER_UNIT}

    def test_a_saturated_check_budget_and_a_spawn_end_within_5_minutes(self):
        self.saturated()
        self.world.cost = {"systemctl": 1, "inhibit_list": 1, "tmux_server_pid": 1, "notify": 5}
        start = self.world.now()
        self.assertEqual(self.run_pass(), 0, self.last_err)
        elapsed = (self.world.now() - start).total_seconds()
        ran = [c[0] for c in self.world.check_calls]
        self.assertEqual(ran, ["w0", "w1"])                            # 2 × 60 s; a third would end past 150 s
        self.assertEqual(len(self.world.spawns), 1)                    # the spawn fitted after them
        self.assertEqual(len(self.world.alarm_notifies()), 1)          # listener_down went out
        self.assertEqual(self.state()["check_cursor"], "w2")           # round-robin: w2 first next pass
        self.assertLessEqual(elapsed, 300)
        self.assertLess(elapsed, 200)

    # -- every World call at its timeout, several sessions, several alarms --------------------------------

    #: the worst case of each World call the tick can make (``RealWorld``'s timeouts), seconds.
    WORST = {"systemctl": edl.SYSTEMCTL_TIMEOUT_S, "systemd_run": edl.SYSTEMD_RUN_TIMEOUT_S,
             "inhibit_list": edl.INHIBIT_LIST_TIMEOUT_S, "tmux_server_pid": edl.TMUX_READ_TIMEOUT_S,
             "tmux_has_session": edl.TMUX_READ_TIMEOUT_S, "tmux_display": edl.TMUX_READ_TIMEOUT_S,
             "capture_pane": edl.TMUX_READ_TIMEOUT_S, "amux_names": edl.AMUX_LS_TIMEOUT_S,
             "spawn": edl.SPAWN_TIMEOUT_S, "resume": edl.RESUME_TIMEOUT_S, "reap": edl.REAP_TIMEOUT_S,
             "amux_send": edl.SEND_TIMEOUT_S, "send_keys": edl.KEYS_TIMEOUT_S, "notify": edl.NOTIFY_TIMEOUT_S,
             # calls with no subprocess: a nominal second each, so nothing is free
             "list_handles": 1, "lifecycle_events": 1, "process_work": 1, "clock_sample": 1,
             "local_timezone": 1, "question_entries": 1, "questions_settings": 1, "notify_index_ids": 1,
             "profile_env": 1}

    def everything_at_its_timeout(self):
        """Wrap EVERY World call so it costs its worst case on the fake clock (git: its ``timeout_s``)."""
        w = self.world
        self.calls_at_timeout = []
        self.call_ends = []                 # (name, seconds since this call began the pass, at the call's end)
        self.t0 = w.now()

        def wrap(name, cost):
            orig = getattr(w, name)

            def call(*args, **kw):
                self.calls_at_timeout.append(name)
                w.advance(cost(args, kw) if callable(cost) else cost)
                self.call_ends.append((name, (w.now() - self.t0).total_seconds()))
                return orig(*args, **kw)
            setattr(w, name, call)
        for name, cost in self.WORST.items():
            wrap(name, cost)

        def git_cost(args, kw):
            return kw.get("timeout_s", args[2] if len(args) > 2 else edl.GIT_TIMEOUT_S)
        orig_git = w.git

        def git(args, cwd, timeout_s=edl.GIT_TIMEOUT_S):
            self.calls_at_timeout.append(f"git {args[0]}")
            w.advance(timeout_s)
            self.call_ends.append((f"git {args[0]}", (w.now() - self.t0).total_seconds()))
            return edl.CommandResult(rc=0, stdout="true\n") if args[:1] == ["merge-base"] else orig_git(args, cwd, timeout_s)
        w.git = git

    def reboot_ledger(self, n=3):
        """n driven worker sessions that all resume at once after a reboot, plus a driver: n + 1 resumes,
        and one ``attempts_exhausted`` alarm per step."""
        repo = self.tmp / "repo"
        repo.mkdir(exist_ok=True)
        self.world.repos[str(repo)] = {"refs": {"main"}}
        steps = [self.step(f"W{i}", role={"worker": {"implementer": "impl", "reviewer": "rev",
                                                    "repos": [{"path": str(repo), "base_ref": "main"}]}})
                 for i in range(n)] + [self.driver("A")]
        roles = {"driver": {"model": "claude-opus-5-5", "effort": "high"},
                 "impl": {"model": "claude-opus-5-5", "effort": "high"},
                 "rev": {"model": "claude-opus-5-5", "effort": "high"}}
        self.enable(steps, roles=roles)
        st = self.state()
        st["clock"] = {"boot_id": "boot-0", "wall": 1.0, "boottime": 1.0, "monotonic": 1.0}
        self.live(st, alive=False, events=[edl and ev("turn_start", "2026-11-24T09:50:00Z")])
        st["steps"]["A"]["status"] = "inflight"
        for i in range(n):
            sid = f"W{i}"
            name = f"t-w{i}-t-impl-w{i}-g1"
            self.live(st, key=f"impl:{sid}", name=name, alive=False)
            self.inflight(st, sid, event=f"E-{i + 2}", name=name)
            st["steps"][sid]["attempts"] = 99                      # attempts_exhausted: an alarm each
        st["steps"]["A"]["attempts"] = 99
        self.save(st)
        return st

    def phase_end(self, names):
        return max([t for n, t in self.call_ends if n in names] or [0])

    def test_after_a_reboot_with_every_call_at_its_timeout_the_pass_ends_before_300s(self):
        self.reboot_ledger(6)                                       # 7 sessions: their reads alone are 100+ s
        self.world.units[edl.LISTENER_UNIT] = "inactive"
        self.world.units[edl.INHIBIT_UNIT] = "inactive"
        self.world.start_fails = {edl.LISTENER_UNIT, edl.INHIBIT_UNIT}
        self.world.server_pid = None
        self.world.run_lands_in = None                              # the tmux server cannot be started either
        self.everything_at_its_timeout()
        start = self.world.now()
        self.assertEqual(self.run_pass(), 0, self.last_err)
        elapsed = (self.world.now() - start).total_seconds()
        self.worst_case_elapsed = elapsed
        self.assertLess(elapsed, 300, self.rp.tick_log.read_text())
        self.assertLessEqual(elapsed, edl.TICK_HARD_LIMIT_S)
        # every phase ended by its own limit, whatever the later phases could absorb
        self.assertLessEqual(self.phase_end({"systemctl", "systemd_run", "inhibit_list", "tmux_server_pid"}),
                             edl.INFRA_END_S)
        self.assertLessEqual(self.phase_end({"tmux_has_session", "tmux_display", "capture_pane", "amux_names"}),
                             edl.READINGS_END_S)
        self.assertGreater(self.phase_end({"tmux_has_session"}), 0)  # and the readings did start
        st = self.state()
        self.assertEqual(st["tick_seq"], 1)                          # the pass completed and committed
        self.assertIsNone(st["last_tick_error"])
        self.assertEqual(self.world.resumes, [])                     # no server: nothing launched, and
        # nothing it deferred counts as an error: no alarm for the calls it did not make
        ids = {k.split(":")[0] for k in self.active_alarms(st)}
        self.assertNotIn("tick_error", ids)
        self.assertLessEqual(len(self.world.alarm_notifies()), 1 + edl.TICK_HARD_LIMIT_S // edl.NOTIFY_TIMEOUT_S)

    def test_after_a_reboot_the_resumes_that_fit_run_and_the_rest_wait_for_the_next_pass(self):
        self.reboot_ledger(3)
        self.everything_at_its_timeout()
        start = self.world.now()
        self.assertEqual(self.run_pass(), 0, self.last_err)
        elapsed = (self.world.now() - start).total_seconds()
        self.assertLess(elapsed, 300, self.rp.tick_log.read_text())
        resumed = len(self.world.resumes)
        self.assertGreaterEqual(resumed, 1)                          # the pass did work
        self.assertLess(resumed, 4)                                  # not all four fitted: 60 s each
        st = self.state()
        self.assertEqual(self.world.units[edl.LISTENER_UNIT], "active")
        self.assertIn("left for the next pass", self.rp.tick_log.read_text())
        # the deferral is consistent state, not an error: the next pass (fast calls) finishes the job
        self.world.cost = {}
        self.world.calls_at_timeout = []
        for name in list(self.WORST):
            setattr(self.world, name, getattr(type(self.world), name).__get__(self.world))
        self.world.git = type(self.world).git.__get__(self.world)
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(len(self.world.resumes), 4)
        exhausted = {f"attempts_exhausted:{s}" for s in ("A", "W0", "W1", "W2")}
        self.assertLessEqual(exhausted, set(self.active_alarms()))
        sent = [n["text"] for n in self.world.alarm_notifies() if "attempts_exhausted" in n["text"]]
        self.assertEqual(len(sent), 4)                              # every one went out, once
        self.assertEqual(len(set(sent)), 4)

    # -- a check or read the pass deadline skipped is unknown: no alarm raised, none cleared, nothing acted on --

    def test_a_deferred_unit_check_is_unknown_not_down(self):
        w = self.world
        w.units[edl.LISTENER_UNIT] = w.units[edl.INHIBIT_UNIT] = "inactive"
        w.start_fails = {edl.LISTENER_UNIT, edl.INHIBIT_UNIT}
        calls = len(w.systemctl_calls)
        for fits in (lambda cost: False, lambda cost: cost <= edl.SYSTEMCTL_TIMEOUT_S):
            # nothing fits / the is-active fits but the start and the check after it do not
            for unit in (edl.LISTENER_UNIT, edl.INHIBIT_UNIT):
                self.assertEqual(edl.ensure_unit(w, unit, fits=fits)[0], None)
        self.assertEqual([c for c in w.systemctl_calls[calls:] if c[0] == "start"], [])      # nothing was started
        infra = edl.check_infrastructure(w, {}, {}, fits=lambda cost: False)
        self.assertEqual((infra.listener_active, infra.inhibitor_active, infra.inhibitor_lock), (None, None, None))
        self.assertTrue(infra.deferred)
        ids = {c.id for c in infra.conditions}
        self.assertFalse(ids & {"listener_down", "inhibitor_down"}, ids)
        # asked for real, the same units are down
        infra = edl.check_infrastructure(w, {}, {})
        self.assertLessEqual({"listener_down", "inhibitor_down"}, {c.id for c in infra.conditions})

    def test_a_pass_that_defers_the_infrastructure_raises_no_listener_or_inhibitor_alarm(self):
        self.reboot_ledger(1)
        w = self.world
        w.units[edl.LISTENER_UNIT] = w.units[edl.INHIBIT_UNIT] = "inactive"
        w.start_fails = {edl.LISTENER_UNIT, edl.INHIBIT_UNIT}
        with mock.patch.object(edl, "INFRA_END_S", 0):
            self.assertEqual(self.run_pass(), 0, self.last_err)
        ids = {k.split(":")[0] for k in self.state()["alarms"]}
        self.assertFalse(ids & {"listener_down", "inhibitor_down"}, ids)
        self.assertFalse([n for n in w.alarm_notifies() if "listener_down" in n["text"] or "inhibitor_down" in n["text"]])
        self.assertEqual([c for c in w.systemctl_calls if c[0] == "start"], [])
        self.assertIn("infrastructure only partly looked at", self.rp.tick_log.read_text())

    def test_a_pass_that_defers_the_infrastructure_leaves_an_active_alarm_as_it_was(self):
        self.reboot_ledger(1)
        w = self.world
        w.units[edl.LISTENER_UNIT] = w.units[edl.INHIBIT_UNIT] = "inactive"
        w.start_fails = {edl.LISTENER_UNIT, edl.INHIBIT_UNIT}
        self.assertEqual(self.run_pass(), 0, self.last_err)                 # asked for real: both alarms
        before = self.state()["alarms"]
        self.assertLessEqual({"listener_down:run", "inhibitor_down:run"}, set(self.active_alarms()))
        def infra_notifies():
            return [n for n in w.alarm_notifies() if "listener_down" in n["text"] or "inhibitor_down" in n["text"]]
        sent = len(infra_notifies())
        self.assertEqual(sent, 2)
        for _ in range(2):                                                  # even past the 4 h repeat time
            with mock.patch.object(edl, "INFRA_END_S", 0):
                self.assertEqual(self.run_pass(advance=5 * 3600), 0, self.last_err)
            after = self.state()["alarms"]
            for key in ("listener_down:run", "inhibitor_down:run"):
                self.assertEqual(after[key], before[key], key)               # not cleared, not repeated, untouched
            self.assertEqual(len(infra_notifies()), sent)                   # and no notify for them
        # the units come back and are looked at: now they clear
        w.units[edl.LISTENER_UNIT] = w.units[edl.INHIBIT_UNIT] = "active"
        w.start_fails = set()
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertFalse({"listener_down:run", "inhibitor_down:run"} & set(self.active_alarms()))

    def unread_ledger(self):
        """One driver in flight on a dead session (a resume is due), a worker likewise, and two alarms that
        a read pass would clear (``session_stuck`` / ``verdict_missing``): active records seeded by hand."""
        self.reboot_ledger(1)
        st = self.state()
        for sid in ("A", "W0"):
            st["steps"][sid]["attempts"] = 0
        now = self.world.now()
        for key, aid, sid in (("session_stuck:A", "session_stuck", "A"), ("verdict_missing:W0", "verdict_missing", "W0")):
            st["alarms"][key] = edl._new_alarm(edl.AlarmCondition(aid, sid, "seeded"), now)
        self.save(st)
        return st

    def test_a_session_the_deadline_left_unread_is_not_dead_resumed_or_alarmed_and_keeps_its_state(self):
        st0 = self.unread_ledger()
        w = self.world
        # 12 s left for the readings: a session (15 s worst case) does not fit, the amux listing (10 s) does;
        # 0: neither fits
        for left in (12, 0):
            with self.subTest(readings_left_s=left), mock.patch.object(edl, "READINGS_END_S", left):
                self.assertEqual(self.run_pass(), 0, self.last_err)
            self.assertEqual((w.resumes, w.spawns, w.sends), ([], [], []))         # not resumed, not spawned
            st = self.state()
            self.assertEqual(st["sessions"], st0["sessions"])                      # session records unchanged
            for sid in ("A", "W0"):
                self.assertEqual(st["steps"][sid], st0["steps"][sid], sid)         # status, attempts, inflight
            for key in ("session_stuck:A", "verdict_missing:W0"):
                self.assertEqual(st["alarms"][key], st0["alarms"][key], key)       # not cleared, not repeated
            ids = {k.split(":")[0] for k in st["alarms"]}
            self.assertFalse(ids - {"session_stuck", "verdict_missing"}, ids)      # nothing new, no 'dead' alarm
            self.assertEqual(w.alarm_notifies(), [])
            self.assertIn("sessions not read in time", self.rp.tick_log.read_text())
        # the next pass reads them normally: the dead sessions are resumed and the seeded alarms clear
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(len(w.resumes), 2)

    def test_take_readings_flags_each_kind_of_session_the_deadline_left_unread(self):
        """Refuse exactly ONE session read (the amux listing still fits): the reading is incomplete whichever
        kind of session it was (a record, a parked generation, a foreign handle of the run id)."""
        st = self.unread_ledger()
        L, w = self.L, self.world
        st["sessions"]["driver"]["parked"] = [{"name": "ws-t-driver-g0"}]
        self.session(st, key=None, name="foreign-1", run_id="epic-t")        # a handle of this run, no record
        full = edl.take_readings(L, st, w, w.now())
        self.assertFalse(full.incomplete)
        n = len(full.sessions)
        self.assertEqual(n, 4)
        for refused in range(1, n + 1):
            with self.subTest(refused_read=refused):
                seen = []

                def fits(cost):
                    if cost == edl.SESSION_READ_WORST_S:
                        seen.append(cost)
                        return len(seen) != refused
                    return True                                              # the amux listing fits
                part = edl.take_readings(L, st, w, w.now(), fits=fits)
                self.assertTrue(part.incomplete)
                self.assertLess(len(part.sessions), n)                       # (a skipped record skips its parked ones too)
        # only the amux listing refused: also incomplete
        late = edl.take_readings(L, st, w, w.now(), fits=lambda cost: cost != edl.AMUX_LS_TIMEOUT_S)
        self.assertTrue(late.incomplete)
        self.assertEqual(len(late.sessions), n)
        self.assertFalse(edl.take_readings(L, st, w, w.now(), fits=lambda cost: True).incomplete)

    def test_a_normal_pass_with_fast_calls_does_all_its_work(self):
        self.reboot_ledger(3)
        self.world.cost = {k: 0.2 for k in self.WORST}
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(len(self.world.resumes), 4)
        self.assertGreaterEqual(len([n for n in self.world.alarm_notifies() if "attempts_exhausted" in n["text"]]), 4)
        self.assertNotIn("left for the next pass", self.rp.tick_log.read_text())

    def test_the_sha_checks_are_charged_before_the_check_budget_is_computed(self):
        n = 3
        repo = self.tmp / "repo"
        repo.mkdir(exist_ok=True)
        self.world.repos[str(repo)] = {"refs": {"main"}}
        steps = [self.step(f"W{i}", check={"run": "true", "timeout": "60s"},
                           role={"worker": {"implementer": "impl", "reviewer": "rev",
                                            "repos": [{"path": str(repo), "base_ref": "main"}]}})
                 for i in range(n)] + [self.step(f"c{i}", check={"run": "true", "timeout": "60s"}) for i in range(6)]
        self.enable(steps, roles={"driver": {"model": "claude-opus-5-5", "effort": "high"},
                                  "impl": {"model": "claude-opus-5-5", "effort": "high"},
                                  "rev": {"model": "claude-opus-5-5", "effort": "high"}})
        st = self.state()
        for i in range(n):
            st["steps"][f"W{i}"].update(status="verifying", advanced_at=self.NOW, shas={edl.BARE_SHA: "a" * 40})
        for i in range(6):
            st["steps"][f"c{i}"].update(status="watching", due_since=self.NOW)
        self.save(st)
        self.world.check_script.update({f"c{i}": (1, 70) for i in range(6)})     # timeout 60 s + 10 s kill grace
        self.everything_at_its_timeout()
        start = self.world.now()
        self.assertEqual(self.run_pass(), 0, self.last_err)
        elapsed = (self.world.now() - start).total_seconds()
        self.assertLess(elapsed, 300, self.rp.tick_log.read_text())
        # ledger git 30 s + infrastructure + readings leave room for two sha checks (30 s each); the
        # third would end past SHA_END_S, so it waits. The check budget is computed AFTER them: only
        # 180 - elapsed is left, and a 60 s check + 10 s grace does not fit it (and the pass is not young).
        self.assertEqual(self.calls_at_timeout.count("git merge-base"), 2)
        self.assertLessEqual(self.phase_end({"git merge-base"}), edl.SHA_END_S)
        self.assertEqual(len(self.world.check_calls), 0)
        st = self.state()
        self.assertEqual(sum(1 for i in range(n) if st["steps"][f"W{i}"].get("sha_check")), 2)
        self.assertEqual(st["steps"]["W2"].get("sha_check"), None)              # deferred, not an error
        self.assertFalse([k for k in self.active_alarms(st) if k.startswith("check_error")])
        # next pass, fast calls: the deferred sha check runs, then the checks
        for name in list(self.WORST):
            setattr(self.world, name, getattr(type(self.world), name).__get__(self.world))
        self.world.git = type(self.world).git.__get__(self.world)
        orig_git = type(self.world).git

        def git(args, cwd, timeout_s=30.0):
            return edl.CommandResult(rc=0) if args[:1] == ["merge-base"] else orig_git(self.world, args, cwd, timeout_s)
        self.world.git = git
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        st = self.state()
        self.assertTrue(all(st["steps"][f"W{i}"]["sha_check"]["passed"] for i in range(n)))
        self.assertGreaterEqual(len(self.world.check_calls), 1)

    def test_worktree_preparation_stops_at_its_deadline_and_defers_without_a_refusal(self):
        repo = self.tmp / "repo"
        repo.mkdir(exist_ok=True)
        self.world.repos[str(repo)] = {"refs": {"main"}}
        steps = [self.step(f"W{i}", role={"worker": {"implementer": "impl", "reviewer": "rev",
                                                    "repos": [{"path": str(repo), "base_ref": "main"}]}})
                 for i in range(3)]
        L = self.enable(steps, roles={"driver": {"model": "claude-opus-5-5", "effort": "high"},
                                      "impl": {"model": "claude-opus-5-5", "effort": "high"},
                                      "rev": {"model": "claude-opus-5-5", "effort": "high"}})
        state = self.state()
        gits = []

        def git(args, cwd, timeout_s=30.0):
            gits.append(timeout_s)
            self.world.advance(timeout_s)                    # every git call costs its timeout
            return edl.CommandResult(rc=0) if timeout_s >= 30 else edl.CommandResult(rc=None, error="timed out")

        def fake_ensure(ledger, st, world, step_id):         # two git calls per step, as the real one makes several
            recs = st.setdefault("worktrees", {}).setdefault("steps", {})
            for _ in range(2):
                res = world.git(["status"], "/x")
                if not res.ok:
                    recs[step_id] = {"refused": res.error}   # what a clamped call would be recorded as
                    return
            recs[step_id] = {"dir": f"/d/{step_id}", "refused": None}
        self.world.git = git
        clock = edl.TickClock(self.world)
        with mock.patch.object(edl, "ensure_step_worktrees", fake_ensure):
            dirs = edl.prepare_worker_steps(L, state, self.world, ["W0", "W1", "W2"],
                                            remaining=lambda: clock.remaining_to(100))
        self.assertEqual(dirs, {"W0": "/d/W0"})                      # W1's second call was clamped: deferred
        self.assertEqual(gits, [30, 30, 30, 10])                     # never past the 100 s deadline
        self.assertEqual((self.world.now() - clock.start).total_seconds(), 100)
        recs = state["worktrees"]["steps"]
        self.assertNotIn("W1", recs)                                 # no "refused" left behind: not worktree_refused
        self.assertNotIn("W2", recs)                                 # and W2 was not started
        self.assertEqual(edl.worktree_conditions(L, state), [])
        # the next pass, with time, prepares the rest
        gits.clear()
        with mock.patch.object(edl, "ensure_step_worktrees", fake_ensure):
            dirs = edl.prepare_worker_steps(L, state, self.world, ["W0", "W1", "W2"],
                                            remaining=lambda: 1000)
        self.assertEqual(sorted(dirs), ["W0", "W1", "W2"])

    def test_the_tick_bounds_worktree_preparation_by_its_deadline(self):
        repo = self.tmp / "repo"
        repo.mkdir(exist_ok=True)
        self.world.repos[str(repo)] = {"refs": {"main"}}
        steps = [self.step(f"W{i}", role={"worker": {"implementer": "impl", "reviewer": "rev",
                                                    "repos": [{"path": str(repo), "base_ref": "main"}]}})
                 for i in range(3)]
        self.enable(steps, roles={"driver": {"model": "claude-opus-5-5", "effort": "high"},
                                  "impl": {"model": "claude-opus-5-5", "effort": "high"},
                                  "rev": {"model": "claude-opus-5-5", "effort": "high"}})
        orig_git = self.world.git

        def git(args, cwd, timeout_s=30.0):
            if args[:1] == ["status"]:                       # the (faked) worktree work: each call at its timeout
                self.world.advance(timeout_s)
                return edl.CommandResult(rc=0) if timeout_s >= 30 else edl.CommandResult(rc=None, error="timed out")
            return orig_git(args, cwd, timeout_s)
        self.world.git = git

        def fake_ensure(ledger, st, world, step_id):          # 4 git calls a step: 3 steps = 360 s unbounded
            recs = st.setdefault("worktrees", {}).setdefault("steps", {})
            for _ in range(4):
                if not world.git(["status"], "/x").ok:
                    recs[step_id] = {"refused": "git timed out"}
                    return
            recs[step_id] = {"dir": f"/d/{step_id}", "repos": {}, "branch": "b", "refused": None}
        start = self.world.now()
        with mock.patch.object(edl, "ensure_step_worktrees", fake_ensure):
            self.assertEqual(self.run_pass(), 0, self.last_err)
        elapsed = (self.world.now() - start).total_seconds()
        self.assertLessEqual(elapsed, edl.PREPARE_END_S)             # stopped at the worktree deadline
        st = self.state()
        self.assertFalse([k for k in self.active_alarms(st) if k.startswith("worktree_refused")])
        recs = st["worktrees"]["steps"]
        self.assertFalse([k for k, r in recs.items() if r.get("refused")])    # a deferral is not a refusal
        self.assertLess(len([r for r in recs.values() if r.get("dir")]), 3)   # not all three fitted

    def test_the_first_check_is_exempt_from_the_budget_only_while_the_pass_is_young(self):
        self.enable([self.step(f"w{i}", check={"run": "true", "timeout": "100s"}) for i in range(3)])
        st = self.state()
        for i in range(3):
            st["steps"][f"w{i}"].update(status="watching", due_since=self.NOW)
        self.save(st)
        self.world.check_script.update({f"w{i}": (1, 100) for i in range(3)})
        ledger = edl.load_ledger(self.lpath, self.world)
        state = self.state()
        young = edl.run_checks(ledger, state, self.world, budget_s=50, overhead_s=10, first_exempt=True,
                               pass_left_s=270)
        self.assertEqual([c[0] for c in self.world.check_calls], ["w0"])           # ran although 110 > 50
        self.assertEqual(young.cursor, "w1")
        self.world.check_calls.clear()
        state = self.state()
        old = edl.run_checks(ledger, state, self.world, budget_s=50, overhead_s=10, first_exempt=False,
                             pass_left_s=270)
        self.assertEqual(self.world.check_calls, [])                                # not young: nothing runs
        self.assertEqual(old.cursor, "w0")                                          # w0 goes first next pass
        self.world.check_calls.clear()
        state = self.state()
        long_ = edl.run_checks(ledger, state, self.world, budget_s=500, overhead_s=10, first_exempt=True,
                               pass_left_s=105)                                     # 100 + 10 > 105: never started
        self.assertEqual(len(long_.too_long), 3)
        self.assertEqual(self.world.check_calls, [])

    def test_the_tick_passes_first_exempt_false_late_in_the_pass(self):
        self.enable([self.step("w0", check={"run": "true", "timeout": "100s"})])
        st = self.state()
        st["steps"]["w0"].update(status="watching", due_since=self.NOW)
        self.save(st)
        self.world.check_script["w0"] = (1, 100)
        self.world.cost = {"systemctl": 25, "inhibit_list": 25, "tmux_server_pid": 25}    # 100 s before the checks
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertGreater(float(self.state()["tick_seq"]), 0)
        self.assertEqual(self.world.check_calls, [])        # 100 s in: 100 + 10 > the 80 s budget left; not young
        self.assertEqual(self.state()["check_cursor"], "w0")

    def test_with_fast_infrastructure_the_checks_get_the_full_150s(self):
        self.enable([self.step(f"w{i}", check={"run": "true", "timeout": "20s"}) for i in range(10)])
        st = self.state()
        for i in range(10):
            st["steps"][f"w{i}"].update(status="watching", due_since=self.NOW)
        self.save(st)
        self.world.check_script.update({f"w{i}": (1, 20) for i in range(10)})
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(len(self.world.check_calls), 7)              # 7 × 20 s; the 8th would end at 160 s

    def test_an_effect_that_would_end_past_the_deadline_is_left_to_the_next_pass(self):
        self.enable([self.driver("A")])
        clock = edl.TickClock(self.world)
        plan_ops = []
        orig = edl.execute_dispatch

        def late(plan, state, world, commit, gate=None):
            plan_ops.extend(op.label for op in plan.ops)
            self.world.advance(edl.TICK_DEADLINE_S - 30)               # the pass is late by now
            return orig(plan, state, world, commit, gate=gate)
        with mock.patch.object(edl, "execute_dispatch", late):
            self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(self.world.spawns, [])
        st = self.state()
        # committed, not run: the next pass launches it with the same event (a kill after the commit)
        self.assertEqual(st["sessions"]["driver"]["status"], "spawning")
        self.assertEqual(st["sessions"]["driver"]["launch_event"], "E-1")
        self.assertIn("1 left for the next pass", self.rp.tick_log.read_text())
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(len(self.world.spawns), 1)
        self.assertTrue(self.world.spawns[0][-1].startswith("EVENT E-1: "))
        self.assertIsNotNone(clock)

    def test_the_gate(self):
        clock = edl.TickClock(self.world)
        spawn, send = edl.Effect(edl.EFFECT_SPAWN), edl.Effect(edl.EFFECT_SEND)
        self.world.advance(edl.TICK_DEADLINE_S - edl.SPAWN_TIMEOUT_S)
        self.assertTrue(clock.gate(spawn))
        self.world.advance(1)
        self.assertFalse(clock.gate(spawn))
        self.assertTrue(clock.gate(send))
        self.assertEqual(clock.check_budget(), 0)
        clock2 = edl.TickClock(self.world)
        self.assertEqual(clock2.check_budget(), edl.CHECK_BUDGET_S)
        self.world.advance(60)
        self.assertEqual(clock2.check_budget(), edl.TICK_DEADLINE_S - edl.TICK_DISPATCH_RESERVE_S - 60)

    def test_a_skipped_send_records_nothing(self):
        self.enable([self.driver("A")])
        st = self.state()
        self.live(st)
        self.save(st)
        with mock.patch.object(edl.TickClock, "gate", lambda self, effect: effect.kind != edl.EFFECT_SEND):
            self.assertEqual(self.run_pass(), 0, self.last_err)
        st = self.state()
        self.assertEqual(self.world.sends, [])
        self.assertEqual(st["outbox"][-1]["sends"], [])          # unsent, no deferral, no send recorded
        self.assertEqual(st["outbox"][-1]["deferrals"], 0)
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(len(self.world.sends), 1)


# -- --dry-run ---------------------------------------------------------------------------------

class TestDryRun(_Tick):
    def snapshot(self):
        """(path, size, mtime_ns, sha256) of every file under the state root, plus the epic folder's pointer."""
        root = Path(os.environ[edl.STATE_ROOT_ENV])
        files = [p for p in root.rglob("*") if p.is_file()] + [self.plan / "epic-driver.run.json"]
        out = {}
        for p in sorted(files):
            if p.exists():
                st = p.stat()
                out[str(p)] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            else:
                out[str(p)] = None
        return out

    def test_computes_and_prints_and_writes_nothing(self):
        self.enable([self.driver("A")])
        self.world.server_pid = None
        self.world.units[edl.LISTENER_UNIT] = "inactive"
        (self.plan / "epic-driver.run.json").write_text('{"run_id": "epic-t"}\n')
        # the ledger changed since the last valid copy: a pass that kept its copy would rewrite it
        Path(self.lpath).write_text(Path(self.lpath).read_text() + "\n# edited\n")
        self.assertNotEqual(self.rp.last_valid_ledger.read_text(), Path(self.lpath).read_text())
        (self.rp.dir / "tick.lock").write_text("")
        before = self.snapshot()
        self.assertIn(str(self.rp.last_valid_ledger), before)
        self.assertIn(str(self.rp.state), before)
        time.sleep(0.05)                                    # an mtime touch must be visible
        rc, out, err = self.cli("tick", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("ops (", out)
        self.assertIn("spawn ws-t-driver-g1 seeded with E-1 [spawn]", out)
        self.assertIn("listener_down:run", out)
        self.assertEqual(self.snapshot(), before)           # no write at all: not even an mtime
        self.assertEqual(self.world.spawns, [])
        self.assertEqual(self.world.notifies, [])

    def test_a_dry_run_that_keeps_the_last_valid_copy_fails_the_snapshot(self):
        """The reviewer's M5, as a unit: with ``keep_copy`` forced on, the snapshot differs."""
        self.enable([self.driver("A")])
        Path(self.lpath).write_text(Path(self.lpath).read_text() + "\n# edited\n")
        before = self.snapshot()
        orig = edl.load_run_ledger
        with mock.patch.object(edl, "load_run_ledger",
                               lambda *a, **kw: orig(*a, **dict(kw, keep_copy=True))):
            self.assertEqual(self.cli("tick", "--dry-run")[0], 0)
        self.assertNotEqual(self.snapshot(), before)


# -- the environment (§21.2) -----------------------------------------------------------------

class TestEnvironment(unittest.TestCase):
    def test_the_snapshot_keeps_the_allowlist_and_never_a_secret(self):
        env = {"PATH": "/p", "GOROOT": "/go", "NVM_DIR": "/n", "NVM_RC_VERSION": "22", "NVM_TOKEN": "s",
               "SSH_AUTH_SOCK": "/s", "VIRTUAL_ENV": "/v", "HOME": "/h", "LC_ALL": "C", "AWS_SECRET": "x"}
        self.assertEqual(edl.env_snapshot(env), {"PATH": "/p", "GOROOT": "/go", "NVM_DIR": "/n",
                                                 "NVM_RC_VERSION": "22", "LC_ALL": "C"})

    def test_is_secret_key_matches_amux_spawn(self):
        loader = SourceFileLoader("amux_spawn_cli_for_test", str(_REPO / ".claude" / "bin" / "amux-spawn"))
        spec = importlib.util.spec_from_loader("amux_spawn_cli_for_test", loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        for key in ("PATH", "GITHUB_TOKEN", "NVM_AUTH", "MONKEY", "API_KEY", "PASS", "PASSPHRASE", "GOPATH",
                    "AWS_SECRET_ACCESS_KEY", "ANTHROPIC_API_KEY", "MY_PAT", "PATTERN", "KEYS_DIR"):
            with self.subTest(key=key):
                self.assertEqual(edl.is_secret_key(key), mod.is_secret_key(key))

    def test_a_dead_ssh_auth_sock_is_unset_a_live_one_kept(self):
        tmp = Path(tempfile.mkdtemp(prefix="edl-sock-"))
        try:
            live = tmp / "agent.sock"
            s = socket.socket(socket.AF_UNIX)
            s.bind(str(live))
            env = {"SSH_AUTH_SOCK": str(live)}
            self.assertIsNone(edl.fix_ssh_auth_sock(env))
            self.assertEqual(edl.tmux_start_env(env, {})["SSH_AUTH_SOCK"], str(live))
            s.close()
            env = {"SSH_AUTH_SOCK": str(tmp / "gone")}
            self.assertIn("is gone", edl.fix_ssh_auth_sock(env))
            self.assertNotIn("SSH_AUTH_SOCK", env)
            self.assertNotIn("SSH_AUTH_SOCK", edl.tmux_start_env({"SSH_AUTH_SOCK": str(tmp / "gone")}, {}))
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)

    def test_nudge_span(self):
        self.assertEqual(edl.nudge_span_s("15m,45m,3h"), 15 * 60 + 45 * 60 + 3 * 3600)
        self.assertEqual(edl.nudge_span_s("2h30m,1d"), 2 * 3600 + 1800 + 86400)
        self.assertEqual(edl.nudge_span_s(None), 0)
        self.assertEqual(edl.nudge_span_s("junk"), 0)



# -- the World boundary of 50-08 -----------------------------------------------------------

class TestRealWorld50_08(unittest.TestCase):
    """RealWorld's new effects build the documented argv; subprocess is mocked, nothing runs."""

    def run_with(self, call, stdout="", rc=0):
        seen = []

        def fake_run(argv, **kw):
            seen.append((list(argv), kw))
            return subprocess.CompletedProcess(argv, rc, stdout, "")
        with mock.patch.object(edl.subprocess, "run", fake_run), \
                mock.patch.object(edl.shutil, "which", lambda b: f"/usr/bin/{b}"):
            res = call(edl.RealWorld())
        return res, seen

    def test_argv(self):
        _, seen = self.run_with(lambda w: w.systemctl(["is-active", edl.LISTENER_UNIT]))
        self.assertEqual(seen[0][0], ["systemctl", "--user", "is-active", "claude-questions-listen.service"])
        _, seen = self.run_with(lambda w: w.systemd_run(edl.tmux_start_args(), {"PATH": "/p"}))
        self.assertEqual(seen[0][0][:4], ["systemd-run", "--user", "--scope", "--unit=epic-driver-tmux"])
        self.assertEqual(seen[0][1]["env"], {"PATH": "/p"})
        _, seen = self.run_with(lambda w: w.inhibit_list())
        self.assertEqual(seen[0][0], ["systemd-inhibit", "--list", "--no-pager"])
        _, seen = self.run_with(lambda w: w.busctl(edl.attach_scope_args(7)))
        self.assertEqual(seen[0][0][:3], ["busctl", "--user", "call"])
        _, seen = self.run_with(lambda w: w.questions_check("/ws"))
        self.assertEqual(seen[0][0], ["/usr/bin/claude-questions", "--check", "--workspace-dir", "/ws"])
        pid, seen = self.run_with(lambda w: w.tmux_server_pid(), stdout="16651\n16651\n")
        self.assertEqual((pid, seen[0][0]), (16651, ["tmux", "list-sessions", "-F", "#{pid}"]))
        pid, _ = self.run_with(lambda w: w.tmux_server_pid(), rc=1)
        self.assertIsNone(pid)

    def test_notify_is_questions_mcp_lib_in_process_with_the_workspace(self):
        calls = []
        fake = mock.Mock()
        fake.notify = lambda text, **kw: calls.append((text, kw)) or {"dispatched": True, "message_id": 3}
        with mock.patch.object(edl.importlib, "import_module", lambda name: fake if name == "questions_mcp_lib" else None):
            res = edl.RealWorld().notify("hi", role="hpl", ack=True, workspace_dir="/ws")
        self.assertEqual(res, {"dispatched": True, "message_id": 3})
        self.assertEqual(calls, [("hi", {"role": "hpl", "ack": True, "workspace_dir": "/ws"})])

        def broken(name):
            raise ImportError("httpx missing")
        with mock.patch.object(edl.importlib, "import_module", broken):
            res = edl.RealWorld().notify("hi", role=None, ack=False, workspace_dir="/ws")
        self.assertEqual((res["dispatched"], res["error"]), (False, "ImportError: httpx missing"))

    def test_no_child_gets_the_override_or_the_hooks_repo(self):
        """A spawn (and every other RealWorld child) is started with an explicit environment without
        EPIC_DRIVER_ALLOW_UNSTAMPED and CLAUDE_HOOKS_REPO, whatever the process has."""
        extra = {"EPIC_DRIVER_ALLOW_UNSTAMPED": "1", "CLAUDE_HOOKS_REPO": "/some/repo", "CLAUDE_KEEP": "yes"}
        calls = {
            "spawn": lambda w: w.spawn(["amux-spawn", "x"]),
            "resume": lambda w: w.resume(["amux-spawn", "resume", "x"]),
            "reap": lambda w: w.reap(["amux-spawn", "rm", "x"]),
            "amux_send": lambda w: w.amux_send("x", "line"),
            "send_keys": lambda w: w.send_keys("x", "Enter"),
            "systemctl": lambda w: w.systemctl(["is-active", "u"]),
            "inhibit_list": lambda w: w.inhibit_list(),
            "busctl": lambda w: w.busctl(["call"]),
            "tmux_server_pid": lambda w: w.tmux_server_pid(),
            "systemd_run": lambda w: w.systemd_run(["--scope"], dict(extra, PATH="/p")),
            "git": lambda w: w.git(["rev-parse", "HEAD"], "/tmp"),
        }
        for name, call in calls.items():
            with self.subTest(call=name), mock.patch.dict(os.environ, extra):
                _, seen = self.run_with(call)
                self.assertTrue(seen, name)
                env = seen[0][1]["env"]
                self.assertNotIn("EPIC_DRIVER_ALLOW_UNSTAMPED", env)
                self.assertNotIn("CLAUDE_HOOKS_REPO", env)
                if name != "systemd_run":
                    self.assertEqual(env.get("CLAUDE_KEEP"), "yes")      # the rest of the environment still goes

    def test_claude_hooks_repo_is_set_around_the_import_alone_in_the_calling_thread(self):
        """questions_mcp_lib reads it at import time only: it is set around the import (main thread, once)
        and put back at once; the notify call, and the notify thread, never touch os.environ."""
        seen = []
        fake = mock.Mock()
        fake.notify = lambda text, **kw: seen.append(os.environ.get("CLAUDE_HOOKS_REPO")) or {"dispatched": True}
        imported = []

        def import_module(name):
            imported.append((os.environ.get("CLAUDE_HOOKS_REPO"), threading.current_thread().name))
            return fake
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(edl.importlib, "import_module", import_module):
            os.environ.pop("CLAUDE_HOOKS_REPO", None)
            w = edl.RealWorld(hooks_repo="/hooks")
            w.prepare_notify()
            self.assertEqual(imported, [("/hooks", threading.current_thread().name)])
            self.assertNotIn("CLAUDE_HOOKS_REPO", os.environ)              # put back right after the import
            w.notify("hi", role=None, ack=True, workspace_dir="/ws")
            w.prepare_notify()
            self.assertEqual(len(imported), 1)                             # imported once
            self.assertEqual(seen, [None])                                 # the call does not set it
            os.environ["CLAUDE_HOOKS_REPO"] = "/operator"
            edl.RealWorld(hooks_repo="/hooks").prepare_notify()
            self.assertEqual(os.environ["CLAUDE_HOOKS_REPO"], "/operator")  # a prior value comes back
            edl.RealWorld().prepare_notify()                               # no hooks_repo: untouched
            self.assertEqual(imported[-1][0], "/operator")

    def test_a_notifier_prepares_in_the_main_thread_and_its_thread_leaves_os_environ_alone(self):
        env_in_thread = []
        fake = mock.Mock()

        def fake_notify(text, **kw):
            env_in_thread.append((threading.current_thread().name, dict(os.environ)))
            return {"dispatched": True, "message_id": 1}
        fake.notify = fake_notify
        imported = []

        def import_module(name):
            imported.append(threading.current_thread() is threading.main_thread())
            return fake
        world = edl.RealWorld(hooks_repo="/hooks")
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(edl.importlib, "import_module", import_module):
            os.environ.pop("CLAUDE_HOOKS_REPO", None)
            before = dict(os.environ)
            n = edl.Notifier(world, None, {}, workspace="/ws", role=None, epic="t")
            self.assertEqual(imported, [True])                      # imported by the constructor, in the main thread
            res = n._call("hi", True)
            self.assertTrue(res["dispatched"])
            self.assertEqual(env_in_thread[0][0], "epic-driver-notify")
            self.assertNotIn("CLAUDE_HOOKS_REPO", env_in_thread[0][1])   # the thread ran with no variable set
            self.assertEqual(dict(os.environ), before)                    # and changed nothing

    def test_no_child_environment_contains_the_hooks_repo_after_a_notify(self):
        fake = mock.Mock()
        fake.notify = lambda text, **kw: {"dispatched": True}
        with mock.patch.dict(os.environ, {"CLAUDE_HOOKS_REPO": "/leaked"}), \
                mock.patch.object(edl.importlib, "import_module", lambda n: fake):
            w = edl.RealWorld(hooks_repo="/hooks")
            w.notify("hi", role=None, ack=True, workspace_dir="/ws")
            for call in (lambda x: x.spawn(["amux-spawn", "x"]), lambda x: x.systemctl(["is-active", "u"]),
                         lambda x: x.git(["rev-parse", "HEAD"], "/tmp")):
                _, seen = self.run_with(call)
                self.assertNotIn("CLAUDE_HOOKS_REPO", seen[0][1]["env"])

    def test_the_new_world_methods_fail_loudly_on_the_base_class(self):
        w = edl.World()
        for call in (lambda: w.systemctl([]), lambda: w.systemd_run([], {}), lambda: w.inhibit_list(),
                     lambda: w.busctl([]), lambda: w.tmux_server_pid(), lambda: w.read_cgroup(1),
                     lambda: w.notify("x", role=None, ack=True, workspace_dir="/"), lambda: w.notify_index_ids(),
                     lambda: w.questions_settings("/"), lambda: w.questions_check("/"), lambda: w.sleep(0)):
            with self.assertRaises(NotImplementedError):
                call()


class TestNotifyTimeout(_Tick):
    """The in-process notify runs in a daemon thread joined with NOTIFY_TIMEOUT_S."""

    def blocking(self):
        gate, entered = threading.Event(), threading.Event()
        orig = self.world.notify

        def notify(text, **kw):
            entered.set()
            gate.wait(10)
            return orig(text, **kw)
        self.world.notify = notify
        return gate, entered

    def test_a_notify_that_blocks_is_a_recorded_failure_and_the_pass_goes_on(self):
        self.enable([self.driver("A")])
        st = self.state()
        st["steps"]["A"]["attempts"] = 99                          # attempts_exhausted: one alarm
        self.save(st)
        gate, entered = self.blocking()
        t0 = time.monotonic()
        with mock.patch.object(edl, "NOTIFY_TIMEOUT_S", 0.2):
            rc = self.run_pass()
        took = time.monotonic() - t0
        self.assertEqual(rc, 0, self.last_err)
        self.assertTrue(entered.is_set())
        self.assertLess(took, 5)                                   # did not wait for the blocked notify
        st = self.state()
        self.assertEqual(st["tick_seq"], 1)                        # the pass completed
        self.assertIn("timed out after 0.2s", st["notify_failures"][-1]["error"])
        self.assertIn("(it may still complete)", st["notify_failures"][-1]["error"])
        rec = st["alarms"]["attempts_exhausted:A"]
        self.assertIn("timed out", rec["last_error"])
        self.assertIsNone(rec["message_id"])
        self.assertIn("dispatched=no", self.rp.notify_log.read_text())
        self.assertIn("timed out", self.cli("status")[1])          # `status` shows it
        gate.set()                                                 # the late completion: nothing breaks
        for t in threading.enumerate():
            if t.name == "epic-driver-notify":
                t.join(5)
        self.assertEqual(len(self.world.alarm_notifies()), 1)      # it did go out in the end (the documented duplicate risk)

    def test_a_notify_that_returns_in_time_is_unchanged(self):
        self.enable([self.driver("A")])
        st = self.state()
        st["steps"]["A"]["attempts"] = 99
        self.save(st)
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(self.state()["notify_failures"], [])
        self.assertEqual(len(self.world.alarm_notifies()), 1)


class TestReconcileAlarmsThroughTheTick(_Tick):
    """A 50-05 condition through real passes: raised with one ack=True notify, cleared with none."""

    def test_session_stuck(self):
        self.enable([])
        st = self.state()
        old = T("2026-11-24T09:00:00Z").timestamp()
        self.session(st, events=[ev("turn_start", "2026-11-24T09:00:01Z")], state="running", mtime=old,
                     entries=[u("2026-11-24T09:00:01Z", "fix it"), a("2026-11-24T09:00:30Z", "working")])
        self.save(st)
        self.assertEqual(self.run_pass(), 0, self.last_err)
        self.assertEqual(self.active_alarms(), ["session_stuck:ws-t-driver-g1"])
        self.assertEqual([n["text"].split(":")[0] for n in self.world.alarm_notifies()],
                         ["[epic-driver t] ALARM session_stuck ws-t-driver-g1"])
        self.world.events["ws-t-driver-g1"].append(ev("stop", "2026-11-24T10:05:00Z", state="idle"))
        self.world.handles[0].update(state="idle", stopped_at="2026-11-24T10:05:00Z")
        self.assertEqual(self.run_pass(advance=600), 0, self.last_err)
        self.assertEqual(self.active_alarms(), [])
        self.assertEqual(len(self.world.alarm_notifies()), 1)


# =============================================================================
# Role prompts (50-09): every verdict example parses with 50-05's parser
# =============================================================================

_PROMPTS_DIR = Path(__file__).resolve().parent.parent / "docs" / "prompts"
_ROLE_PROMPTS = ("epic-driver-driver.md", "epic-driver-worker.md")


def _prompt_examples(text: str):
    """``(valid_lines, invalid_lines)`` from the fenced ``verdict`` / ``verdict-invalid`` blocks
    (the convention documented in each prompt's header comment)."""
    valid, invalid, current, inside = [], [], None, False
    for raw in text.split("\n"):
        line = raw.rstrip()
        if not inside:
            m = re.match(r"^```(verdict|verdict-invalid)$", line.strip())
            if m:
                inside, current = True, (valid if m.group(1) == "verdict" else invalid)
        elif line.strip() == "```":
            inside = False
        elif line.strip():
            current.append(line.strip())
    assert not inside, "unterminated example block"
    return valid, invalid


def _prompt_fenced_blocks(text: str):
    """``[(tag, [lines])]`` for every three-backtick block (the four-backtick wrapper is not one)."""
    blocks, current, tag = [], None, None
    for raw in text.split("\n"):
        line = raw.strip()
        if current is None:
            m = re.match(r"^```([\w-]*)$", line)
            if m:
                current, tag = [], m.group(1)
        elif line == "```":
            blocks.append((tag, current))
            current = None
        else:
            current.append(line)
    return blocks


_PROHIBITION = re.compile(r"\b(never|no|not|forbidden)\b", re.I)


def _prompt_lines_with_section(text: str):
    """``(line, heading)``: each line with the nearest preceding ``## `` heading."""
    heading = ""
    for line in text.split("\n"):
        if line.startswith("## "):
            heading = line
        yield line, heading


class TestRolePrompts(unittest.TestCase):
    def text(self, name):
        return (_PROMPTS_DIR / name).read_text(encoding="utf-8")

    def test_force_flags_are_only_prohibited(self):
        flag = re.compile(r"--force\b|--force-with-lease|--mirror|(?<![\w-])-f(?![\w-])|(?<![\w`])\+refspec|`\+\w+`")
        for name in _ROLE_PROMPTS:
            for line, heading in _prompt_lines_with_section(self.text(name)):
                if flag.search(line):
                    with self.subTest(prompt=name, line=line):
                        self.assertTrue(_PROHIBITION.search(line) or "forbidden" in heading.lower(),
                                        "a force flag outside a prohibition")

    def test_no_push_without_dash_c(self):
        for name in _ROLE_PROMPTS:
            for line, _ in _prompt_lines_with_section(self.text(name)):
                if "git push" in line:
                    with self.subTest(prompt=name, line=line):
                        self.assertTrue("git -C" in line or _PROHIBITION.search(line),
                                        "git push recommended without -C")

    def test_worker_has_never_commit_rule(self):
        rule = [x for x in self.text("epic-driver-worker.md").split("\n") if "NEVER COMMIT" in x]
        self.assertTrue(rule)
        self.assertIn("git commit", rule[0])
        self.assertIn("uncommitted", rule[0])

    def test_closing_commands_carry_dash_c(self):
        driver = self.text("epic-driver-driver.md")
        for needle in ("git -C <integration worktree> merge --no-ff --no-edit epic-step/<epic>/<step>",
                       "git -C <integration worktree> push <remote> epic/<epic>",
                       "git -C <integration worktree> merge --abort", "git -C <step worktree> commit"):
            with self.subTest(needle=needle):
                self.assertIn(needle, driver)

    def test_verdict_shaped_blocks_are_tagged(self):
        verb = re.compile(r"^(ADVANCED|WAITING|ASKED|HALT|DONE|HANDOFF):")
        for name in _ROLE_PROMPTS:
            for tag, lines in _prompt_fenced_blocks(self.text(name)):
                if any(verb.match(x) for x in lines) and tag not in ("verdict", "verdict-invalid"):
                    self.fail(f"{name}: an untagged block holds verdict lines: {lines[:2]}")

    def test_prompts_exist_and_are_short(self):
        for name in _ROLE_PROMPTS:
            with self.subTest(prompt=name):
                self.assertLess(len(self.text(name).splitlines()), 250)

    def test_valid_examples_parse(self):
        for name in _ROLE_PROMPTS:
            valid, _ = _prompt_examples(self.text(name))
            self.assertGreaterEqual(len(valid), 3, name)
            for line in valid:
                with self.subTest(prompt=name, line=line):
                    self.assertIsNotNone(edl.parse_verdict_line(line))

    def test_counter_examples_fail_to_parse(self):
        for name in _ROLE_PROMPTS:
            _, invalid = _prompt_examples(self.text(name))
            self.assertGreaterEqual(len(invalid), 3, name)
            for line in invalid:
                with self.subTest(prompt=name, line=line):
                    self.assertIsNone(edl.parse_verdict_line(line))

    def test_driver_shows_every_verb_but_done(self):
        valid, _ = _prompt_examples(self.text("epic-driver-driver.md"))
        verbs = {v.verb for line in valid for v in edl.parse_verdict_line(line)}
        self.assertEqual(verbs, {"ADVANCED", "WAITING", "ASKED", "HALT", "HANDOFF"})
        self.assertTrue(any(len(edl.parse_verdict_line(x)) > 1 for x in valid), "a merged example")
        self.assertTrue(any(any(v.shas and v.shas[0][0] for v in edl.parse_verdict_line(x)) for x in valid))

    def test_worker_examples_are_done_halt_or_a_rotate_handoff(self):
        valid, _ = _prompt_examples(self.text("epic-driver-worker.md"))
        for line in valid:
            with self.subTest(line=line):
                self.assertTrue(all(v.verb in ("DONE", "HALT", "HANDOFF") for v in edl.parse_verdict_line(line)))
        self.assertEqual({edl.parse_verdict_line(x)[0].verb for x in valid}, {"DONE", "HALT", "HANDOFF"})
        # HANDOFF is the rotate turn's verdict (§10, §14.3): the prompt scopes it to section 5
        for line in valid:
            if line.startswith("HANDOFF"):
                self.assertTrue(edl.read_verdict(line, steps=(), from_driver=False, rotate=True).valid)
        self.assertIn("`HANDOFF:` only in a `rotate` turn", self.text("epic-driver-worker.md"))

    def test_forbidden_tools_and_force_ban_appear(self):
        for name in _ROLE_PROMPTS:
            text = self.text(name)
            for needle in ("amux-spawn", "`amux`", "Monitor", "`watch`", "AskUserQuestion", "sleep",
                           "--force", "--force-with-lease", "--mirror", "+refspec"):
                with self.subTest(prompt=name, needle=needle):
                    self.assertIn(needle, text)

    def test_commands_and_rules_match_the_code(self):
        driver, worker = self.text("epic-driver-driver.md"), self.text("epic-driver-worker.md")
        for needle in ("epic-driver mark --grant <approval id> --question Q-<n>",
                       "epic-driver queue <step> --to <role> --file <path>", "mcp__questions__ask",
                       "merge --no-ff --no-edit", "epic-step/<epic>/<step>", "Never commit or stage the workspace's question-queue"):
            with self.subTest(needle=needle):
                self.assertIn(needle, driver)
        self.assertIn("NEVER COMMIT", worker)
        self.assertIn("NEVER ASK THE OPERATOR", worker)
        self.assertIn("DONE: <step>", worker)
