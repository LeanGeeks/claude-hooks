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
        self.check_calls = []
        self.check_script = {}            # step id -> (rc, seconds)

    def now(self):
        return self.clock

    def advance(self, seconds):
        self.clock = self.clock + timedelta(seconds=seconds)

    def git(self, args, cwd, timeout_s=30.0):
        args = list(args)
        self.git_calls.append((tuple(args), cwd))
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
        self.assertIn((("rev-parse", "--verify", "--quiet", "nope^{commit}"), str(repo)), world.git_calls)
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
        for needle in ('"amux-spawn"', '"amux"', "send-keys", "new-session", "kill-session", "questions_mcp_lib",
                       ".notify(", "systemctl"):
            self.assertNotIn(needle, code, needle)
        # 50-05: tmux is read only (display-message, capture-pane), and only through RealWorld._tmux_read.
        self.assertEqual(code.count("TMUX_BIN"), 2, "TMUX_BIN is defined once and used once")
        verbs = set(re.findall(r'_tmux_read\(\["([a-z-]+)"', code))
        self.assertEqual(verbs, {"display-message", "capture-pane"})

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


if __name__ == "__main__":
    unittest.main(verbosity=2)
