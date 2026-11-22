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
        allowed_local = {"amux_spawn_lib", "yaml", "__future__"}
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
        for needle in ('"amux-spawn"', '"amux"', '"tmux"', "send-keys", "questions_mcp_lib", ".notify(",
                       "systemctl"):
            self.assertNotIn(needle, code, needle)

    def test_world_fakes_fail_loudly(self):
        w = edl.World()
        for call in (lambda: w.now(), lambda: w.git([], "/"), lambda: w.run_check("", "/", {}, 1),
                     lambda: w.profile_env("x")):
            with self.assertRaises(NotImplementedError):
                call()


if __name__ == "__main__":
    unittest.main(verbosity=2)
