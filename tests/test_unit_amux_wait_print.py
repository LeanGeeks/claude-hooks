#!/usr/bin/env python3
"""F1 regression suite: what ``--wait`` / ``--notify`` PRINTS on completion.

Epic-20 live sign-off (tasks/20_codex_background_workers/20-06-signoff-evidence.md,
finding F1) shipped a stale print: ``spawn --wait`` (and ``resume --wait``) for
a Codex worker exited with the correct code but printed NOTHING on the first
attempt and the PREVIOUS attempt's message after a resume, while ``last`` and
the result artifact were always correct. Two root causes, both pinned here:

1. the reducer resolved the governing turn outcome last-wins across the WHOLE
   appended event log, so a resumed worker mid-turn false-read ``idle`` off the
   previous attempt's ``turn.completed`` once the new segment's first event
   landed — the wait accepted that false idle and printed the old result;
2. codex flushes ``turn.completed`` to the event log BEFORE it writes the
   ``--output-last-message`` file (written at exec end; amux pre-creates it
   empty and writes the ``.rc`` sibling only after codex exits), so the wait's
   poll could land between the two and print a not-yet-written result.

The fix: the reducer scopes the governing outcome per segment, the wait
accepts a Codex idle only once the run's writes are final (``.rc`` present),
and the payload is derived by the SAME read path ``last`` uses. Claude prints
byte-identically; the timeout contract (``AMUX_WAIT_TIMEOUT`` + exit 3) is
untouched.

Hermetic: throwaway ``~/.amux`` (patched lib path constants), tmux
has-session stubbed, time.sleep stubbed, vendored amux 01-01 fixtures in
``tests/fixtures/codex/`` (codex-cli 0.149.0). No network, no live codex
turn, no real ``~/.amux`` / ``~/.claude`` / ``~/.codex``.
"""

import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
_BIN = Path(__file__).parent.parent / ".claude" / "bin" / "amux-spawn"
_FIXTURES = Path(__file__).parent / "fixtures" / "codex"
sys.path.insert(0, str(_HOOKS))

import amux_spawn_lib as lib  # noqa: E402


def _load_cli():
    spec = importlib.util.spec_from_loader(
        "amux_spawn_cli_wait_print",
        importlib.machinery.SourceFileLoader("amux_spawn_cli_wait_print", str(_BIN)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()

# The thread id the vendored success fixtures actually captured.
THREAD_ID = "01a00000-0000-7000-8000-000000000001"


def _redirect_amux_home(tmp: Path):
    return patch.multiple(
        lib,
        AMUX_HOME=tmp,
        AMUX_SESSIONS_DIR=tmp / "sessions",
        SPAWN_DIR=tmp / "spawn",
        SPAWN_LOCK=tmp / "spawn" / ".lock",
    )


def _fixture(name: str) -> bytes:
    path = _FIXTURES / f"{name}.jsonl"
    assert path.is_file(), f"missing vendored fixture: {path}"
    return path.read_bytes()


def _stage(tmp: Path, name: str, *, events: bytes, rc: int | None = None,
           result: str | None = None, result_missing: bool = False,
           **handle_overrides) -> tuple[Path, Path]:
    """Stage one bounded worker's world (artifacts + handle) like amux would.

    ``rc=None`` writes NO ``.rc`` sibling (amux pre-creates the result file
    EMPTY and removes the stale ``.rc`` at every launch, so its absence is the
    documented "this attempt has not finished" signal). Returns
    ``(events_path, result_path)``.
    """
    spawn = tmp / "spawn"
    spawn.mkdir(parents=True, exist_ok=True)
    events_path = spawn / f"{name}.events.jsonl"
    events_path.write_bytes(events)
    if rc is not None:
        Path(f"{events_path}.rc").write_text(str(rc))  # printf '%s' $?
    result_path = spawn / f"{name}.result.md"
    if not result_missing:
        result_path.write_text(result if result is not None else "")
    handle = lib.new_handle(
        name=name, session_id=None, run_id="rid-codex", abs_dir="/ws/p",
        transcript_path="", stuck_after_s=600, provider=lib.PROVIDER_CODEX,
        activity_path=str(events_path), result_path=str(result_path),
    )
    handle.update(handle_overrides)
    lib.write_handle(name, handle)
    return events_path, result_path


def _write_transcript(path: Path, lines: list) -> float:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x) + "\n" for x in lines))
    return path.stat().st_mtime


def _user_turn(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def _assistant_text(text: str) -> dict:
    return {"type": "assistant", "message": {"role": "assistant",
            "content": [{"type": "text", "text": text}]}}


# ── Codex --wait: the FRESH artifact text, never empty, never stale ───────────


class TestCodexWaitPrintFreshness(unittest.TestCase):
    """The F1 scenarios, replayed against the real derivation + wait loop."""

    def test_attempt1_prints_fresh_result_despite_the_write_lag(self):
        """Live step 1: the poll observes turn.completed BEFORE codex has
        written the result file (pre-created empty, no .rc yet). Old behavior:
        accepted the idle and printed an empty line. The wait must keep
        polling until the run's writes are final, then print the FRESH text."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                lines = _fixture("exec_success").splitlines(keepends=True)
                events, result = _stage(
                    tmp, "p-2", events=b"".join(lines[:3]),  # mid-turn, no verdict
                    rc=None, result="",                      # the lag window
                )
                polls = [0]
                real_derive = cli._derive_status

                def derive(handle, override):
                    polls[0] += 1
                    if polls[0] == 2:
                        # turn.completed is flushed to the event log…
                        with open(events, "ab") as f:
                            f.write(lines[3])
                    if polls[0] >= 3:
                        # …and the run's writes land one poll later.
                        Path(f"{events}.rc").write_text("0")
                        result.write_text("fresh answer")
                    return real_derive(handle, override)

                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch.object(cli, "_derive_status", side_effect=derive), \
                        patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p-2", timeout_s=30)
                self.assertEqual(outcome, "idle")
                self.assertEqual(payload, "fresh answer")
                self.assertGreaterEqual(polls[0], 3,
                                        "the lagging idle must be re-polled")

    def test_resume_mid_turn_waits_and_prints_attempt2_not_attempt1(self):
        """Live step 3: after a resume, the appended second segment has started
        but not completed; the result file still holds attempt 1's answer and
        amux removed the stale .rc at launch. Old behavior: the previous
        attempt's turn.completed governed, the wait accepted a FALSE idle and
        printed attempt 1's message. It must wait for attempt 2's completion
        and print attempt 2's message."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                segment2_started = (
                    b'{"type":"thread.started","thread_id":"' + THREAD_ID.encode()
                    + b'"}\n{"type":"turn.started"}\n'
                )
                events, result = _stage(
                    tmp, "p-2",
                    events=_fixture("exec_success") + segment2_started,
                    rc=None, result="attempt-1 answer",
                    attempt=2, state="spawning",
                )
                # The derivation itself must not false-read idle mid-attempt
                # (the F1 root cause): the newest segment has no verdict yet.
                with patch.object(lib, "tmux_has_session", return_value=True):
                    self.assertEqual(
                        cli._derive_status(lib.read_handle("p-2"), None)["state"],
                        "running",
                    )
                polls = [0]
                real_derive = cli._derive_status

                def derive(handle, override):
                    polls[0] += 1
                    if polls[0] == 3:
                        # Attempt 2's turn completes; the result file still
                        # holds attempt 1's text and no .rc exists yet.
                        with open(events, "ab") as f:
                            f.write(b'{"type":"turn.completed","usage":{}}\n')
                    if polls[0] >= 4:
                        Path(f"{events}.rc").write_text("0")
                        result.write_text("attempt-2 answer")
                    return real_derive(handle, override)

                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch.object(cli, "_derive_status", side_effect=derive), \
                        patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p-2", timeout_s=30)
                self.assertEqual(outcome, "idle")
                self.assertEqual(payload, "attempt-2 answer")
                self.assertNotIn("attempt-1", payload)

    def test_timeout_mid_resume_prints_only_the_marker_exit_3(self):
        """The timeout contract is unchanged by the fix — and a mid-attempt
        resume must TIME OUT (not false-idle): stdout is exactly the
        AMUX_WAIT_TIMEOUT marker, exit 3, and the previous attempt's result is
        never printed."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                segment2_started = (
                    b'{"type":"thread.started","thread_id":"' + THREAD_ID.encode()
                    + b'"}\n{"type":"turn.started"}\n'
                )
                _stage(
                    tmp, "p-2",
                    events=_fixture("exec_success") + segment2_started,
                    rc=None, result="attempt-1 answer",
                    attempt=2, state="spawning",
                )
                stdout, stderr = io.StringIO(), io.StringIO()
                now = [time.time()]

                def advancing_time():
                    now[0] += 0.25
                    return now[0]

                with patch.object(lib, "tmux_has_session", return_value=True), \
                        patch("time.sleep"), \
                        patch("time.time", side_effect=advancing_time), \
                        patch("sys.stdout", stdout), patch("sys.stderr", stderr):
                    rc = cli._wait_and_report("p-2", "1s")
                self.assertEqual(rc, 3)
                self.assertEqual(stdout.getvalue().strip(),
                                 cli.WAIT_TIMEOUT_MARKER)
                self.assertNotIn("attempt-1 answer", stdout.getvalue())
                self.assertIn("timed out", stderr.getvalue())


class TestCodexWaitFailOpen(unittest.TestCase):
    def test_idle_without_rc_accepts_fail_open_after_the_grace(self):
        """A completed turn whose .rc never lands (killed wrapper) must not
        hang the wait forever: after CODEX_RESULT_GRACE_S it accepts idle
        fail-open (§5: completion evidence beats the artifact) and prints
        whatever the shared reader gives — "" per the documented contract."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage(tmp, "p-2", events=_fixture("exec_success"),
                       rc=None, result="")  # result stays empty, .rc never lands
                polls = [0]
                real_derive = cli._derive_status

                def derive(handle, override):
                    polls[0] += 1
                    return real_derive(handle, override)

                with patch.object(lib, "tmux_has_session", return_value=False), \
                        patch.object(cli, "_derive_status", side_effect=derive), \
                        patch.object(cli, "CODEX_RESULT_GRACE_S", 0.05), \
                        patch("time.sleep"):
                    outcome, payload = cli._wait_for_idle("p-2", timeout_s=30)
                self.assertEqual(outcome, "idle")
                self.assertEqual(payload, "")
                self.assertGreaterEqual(polls[0], 2,
                                        "the grace must re-poll before failing open")


# ── Claude --wait: byte-identical print ───────────────────────────────────────


class TestClaudeWaitPrintUnchanged(unittest.TestCase):
    """The Claude path must print exactly what it printed before the fix."""

    def _drive_claude_wait(self, tmp: Path, last_message: str | None):
        tpath = tmp / "p" / "sid.jsonl"
        m = _write_transcript(tpath, [_user_turn("go"),
                                      _assistant_text("done")])
        handle = lib.new_handle(
            name="p", session_id="11111111-2222-3333-4444-555555555555",
            run_id="rid", abs_dir="/ws/p", transcript_path=str(tpath),
            stuck_after_s=600,
        )
        handle.update(state="idle", mtime_at_stop=m,
                      background_tasks=[], last_message=last_message)
        lib.write_handle("p", handle)

        polls = [0]
        real_derive = cli._derive_status

        def derive(h, override):
            polls[0] += 1
            if polls[0] == 1:
                r = dict(real_derive(h, override))
                r["state"] = "running"  # the seeded turn is observed running…
                return r
            return real_derive(h, override)  # …then the draining Stop lands.

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(lib, "tmux_has_session", return_value=True), \
                patch.object(cli, "_derive_status", side_effect=derive), \
                patch("time.sleep"), \
                patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            rc = cli._wait_and_report("p", None)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_claude_wait_prints_exactly_the_last_message_line(self):
        """Success (exit 0): stdout is EXACTLY the last_message line — the
        pre-F1 Claude behavior, byte-for-byte."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                rc, stdout, _stderr = self._drive_claude_wait(
                    tmp, "the claude answer")
        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "the claude answer\n")

    def test_claude_wait_without_message_prints_exactly_an_empty_line(self):
        """A Claude idle with last_message=None prints exactly one empty line
        (the ``or ""`` path) — unchanged."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                rc, stdout, _stderr = self._drive_claude_wait(tmp, None)
        self.assertEqual(rc, 0)
        self.assertEqual(stdout, "\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
