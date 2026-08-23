#!/usr/bin/env python3
"""Unit tests for ``amux-spawn resume`` — task 20-04 (epic 20).

Covers the resume surface and its §7 coordination order, thread-id authority
(amux's ``meta.json``, fail-closed on missing/malformed/mismatched ids), the
attempt/segment scheme with newest-successful ``last`` semantics, failed-launch
state preservation (including amux's rc-66 thread-mismatch outcome), and the
model/profile ergonomics (pass-through, no injection, refusal with guidance).

Driven by the vendored amux 01-01 fixtures in ``tests/fixtures/codex/``
(codex-cli 0.149.0) — the same thread id the fixtures captured is the id that
must be resumed.

Hermetic: throwaway ``~/.amux`` (patched lib path constants), ``tmux
has-session`` stubbed, ``amux`` stubbed (the stub reproduces amux's documented
launch-time behavior: it removes the stale ``.rc`` sibling), private temp
files. No network, no live codex turn, no real ``~/.amux`` / ``~/.claude`` /
``~/.codex`` — and ``--dangerously-bypass-approvals-and-sandbox`` never appears
in any constructed argv.
"""

import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
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
        "amux_spawn_cli_resume",
        importlib.machinery.SourceFileLoader("amux_spawn_cli_resume", str(_BIN)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()

# The thread id the vendored success/resume fixtures actually captured
# (exec_success.jsonl / exec_resume_success.jsonl agree on it).
THREAD_ID = "01a00000-0000-7000-8000-000000000001"

ADVERSARIAL_PROMPT = (
    "address 'this' \"thing\" $HOME $(touch /dev/null/PWNED) `id` *.py ; echo pwned\n"
    "second line\twith a tab"
)


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


def _write_meta(tmp: Path, name: str, thread_id: str | None) -> Path:
    """Write amux's ``<name>.meta.json`` with the captured thread id."""
    sessions = tmp / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    meta = sessions / f"{name}.meta.json"
    if thread_id is None:
        meta.write_text(json.dumps({"started": True}))
    else:
        meta.write_text(json.dumps({"codex_session_id": thread_id}))
    return meta


def _fixture_thread(fixture: str | None) -> str | None:
    """The thread id a fixture's first ``thread.started`` captured, or None."""
    if not fixture:
        return None
    for line in _fixture(fixture).splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("type") == "thread.started":
            return event["thread_id"]
    return None


def _stage_worker(
    tmp: Path,
    name: str = "p-2",
    *,
    fixture: str | None = "exec_success",
    rc: int | None = 0,
    result: str | None = "attempt-1 answer",
    attempt: int = 1,
    meta_thread: str | None = None,
    handle_session_id: str | None = None,
    **handle_overrides,
) -> tuple[Path, Path]:
    """Stage one bounded worker's complete world: artifacts + meta + handle.

    Defaults describe a COMPLETED first attempt: real fixture events, ``.rc``,
    a non-empty result file, amux meta with the id that fixture actually
    captured (what amux's wrapper would have persisted), and a handle at
    attempt 1 (the post-20-04 schema; pass ``attempt=0`` to also drop the key
    for legacy-handle coverage). ``meta_thread`` overrides the captured id —
    pass a different UUID to stage a corrupted pair.
    """
    spawn = tmp / "spawn"
    spawn.mkdir(parents=True, exist_ok=True)
    events = spawn / f"{name}.events.jsonl"
    events.write_bytes(_fixture(fixture) if fixture else b"")
    if rc is not None:
        Path(f"{events}.rc").write_text(str(rc))  # printf '%s' $? — no newline
    result_path = spawn / f"{name}.result.md"
    if result is not None:
        result_path.write_text(result)
    if meta_thread is None:
        meta_thread = _fixture_thread(fixture) or THREAD_ID
    _write_meta(tmp, name, meta_thread)
    handle = lib.new_handle(
        name=name, session_id=handle_session_id, run_id="rid-codex",
        abs_dir="/ws/p", transcript_path="", stuck_after_s=600,
        provider=lib.PROVIDER_CODEX,
        activity_path=str(events), result_path=str(result_path),
        attempt=attempt,
    )
    if attempt == 0:  # simulate a legacy pre-20-04 handle: no attempt key
        handle.pop("attempt", None)
    handle.update(handle_overrides)
    lib.write_handle(name, handle)
    return events, result_path


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _drive_resume(
    argv: list[str],
    *,
    live_names: set[str] | None = None,
    amux_rc: int = 0,
    amux_launch_live: bool = True,
    tmux_alive: bool | None = None,
):
    """Run ``cli.main(argv)`` with amux/tmux stubbed; return what happened.

    The amux stub reproduces amux's documented launch-time behavior for
    ``amux start <name> --resume``: the stale ``<event-log>.rc`` sibling is
    REMOVED at launch (docs/codex-provider.md §4), then the launch succeeds
    (pane live) or fails (rc != 0). Returns
    ``(rc, amux_calls, stdout, stderr, live_names)``.
    """
    calls: list[list[str]] = []
    live = live_names if live_names is not None else set()
    stdout, stderr = io.StringIO(), io.StringIO()

    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        if cmd[:2] == ["amux", "start"] and "--resume" in cmd:
            name = cmd[2]
            spawn = lib.SPAWN_DIR
            rc_sibling = spawn / f"{name}.events.jsonl.rc"
            # amux's _prepare_bounded_artifacts removes the previous .rc at
            # launch so its absence refers to the current attempt.
            try:
                rc_sibling.unlink()
            except OSError:
                pass
            if amux_rc == 0 and amux_launch_live:
                live.add(name)
                return _FakeCompleted(0, stdout="started")
            return _FakeCompleted(
                amux_rc,
                stderr="amux: cannot resume: session already running a bounded "
                       "Codex turn" if amux_rc else "amux: tmux failed",
            )
        return _FakeCompleted(0)

    def fake_tmux(name: str) -> bool:
        if tmux_alive is not None:
            return tmux_alive
        return name in live

    with patch.object(cli.lib, "resolve_amux_session", return_value=None), \
            patch.object(cli.lib, "tmux_has_session", side_effect=fake_tmux), \
            patch.object(cli.subprocess, "run", side_effect=fake_run), \
            patch("sys.stdin") as stdin, \
            patch("sys.stdout", stdout), patch("sys.stderr", stderr):
        stdin.isatty.return_value = False
        rc = cli.main(argv)
    return rc, calls, stdout.getvalue(), stderr.getvalue(), live


def _amux_resume_call(calls):
    for c in calls:
        if c[:2] == ["amux", "start"] and "--resume" in c:
            return c
    return None


# ── The resume argv surface ───────────────────────────────────────────────────


class TestResumeArgv(unittest.TestCase):
    """The exact amux argv — asserted on the REAL argv, not a rendered string."""

    def _resume(self, tmp: Path, extra: list[str]):
        with _redirect_amux_home(tmp):
            lib.ensure_dirs()
            _stage_worker(tmp)
            return _drive_resume(
                ["resume", "p-2", *extra, "--", "continue the review"]
            )

    def test_argv_is_amux_start_resume_no_attach(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, calls, _out, _err, _live = self._resume(tmp, [])
            self.assertEqual(rc, 0)
            self.assertEqual(_amux_resume_call(calls), [
                "amux", "start", "p-2", "--resume", "--no-attach",
                "--", "continue the review",
            ])

    def test_explicit_model_eq_form_reaches_argv_verbatim(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, calls, _out, _err, _live = self._resume(tmp, ["--model=gpt-5.5"])
            self.assertEqual(rc, 0)
            call = _amux_resume_call(calls)
            self.assertIn("--model=gpt-5.5", call)  # one argv element
            self.assertEqual(call[-1], "continue the review")  # prompt last

    def test_explicit_model_space_form_reaches_argv_intact(self):
        """resume has no suffix positional, so BOTH spellings round-trip
        (the documented spawn-side `--flag=value` advice is about spawn's
        optional <suffix>, which resume does not have)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, calls, _out, _err, _live = self._resume(
                tmp, ["--model", "gpt-5.5"])
            self.assertEqual(rc, 0)
            call = _amux_resume_call(calls)
            i = call.index("--model")
            self.assertEqual(call[i + 1], "gpt-5.5")
            self.assertEqual(call[-1], "continue the review")

    def test_no_model_in_argv_when_omitted(self):
        """BRD §3 / done-when: no Codex model is injected when the user did
        not request one — Codex's own config decides."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, calls, _out, _err, _live = self._resume(tmp, [])
            self.assertEqual(rc, 0)
            call = _amux_resume_call(calls)
            for forbidden in ("--model", "-m", "gpt-5.5", "sonnet", "opus"):
                self.assertNotIn(forbidden, call, forbidden)

    def test_multiline_prompt_is_one_argv_element(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, calls, _out, _err, _live = self._resume(tmp, [])
            self.assertEqual(rc, 0)
            # (re-driven with the adversarial prompt for the boundary check)
            with _redirect_amux_home(tmp):
                rc2, calls2, _o, _e, _l = _drive_resume(
                    ["resume", "p-2", "--", ADVERSARIAL_PROMPT])
            self.assertEqual(rc2, 0)
            call = _amux_resume_call(calls2)
            self.assertEqual(call[-1], ADVERSARIAL_PROMPT)
            self.assertEqual(call.count(ADVERSARIAL_PROMPT), 1)

    def test_no_yolo_expansion_or_provider_argv_ever_constructed(self):
        """Ownership boundary (invariant 3 / §8): only ``amux`` is invoked; no
        codex subcommand, no YOLO expansion, no Claude-only option appears."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, calls, _out, _err, _live = self._resume(tmp, [])
            self.assertEqual(rc, 0)
            for call in calls:
                self.assertEqual(call[0], "amux")
                for forbidden in (
                    "codex", "resume-after", "--dangerously-bypass-approvals-and-sandbox",
                    "--dangerously-skip-permissions", "--session-id",
                    "--no-default-model", "--event-log", "--output-last-message",
                ):
                    # "resume" legitimately appears as amux's own flag; the
                    # forbidden list above has no such collision.
                    self.assertNotIn(forbidden, call, forbidden)

    def test_resume_json_outcome_shape(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(tmp)
                rc, calls, out, _err, _live = _drive_resume(
                    ["resume", "p-2", "--json", "--", "go again"])
            self.assertEqual(rc, 0)
            self.assertIsNotNone(_amux_resume_call(calls))
            data = json.loads(out.strip())
            self.assertEqual(data["provider"], "codex")
            self.assertEqual(data["state"], "spawning")
            self.assertEqual(data["attempt"], 2)
            self.assertEqual(data["thread_id"], THREAD_ID)


# ── Thread-id authority: fail closed, never a fresh thread ───────────────────


class TestThreadIdAuthority(unittest.TestCase):
    """BRD §4.3: missing/malformed/mismatched ids error without launching."""

    def test_missing_thread_id_never_launches(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # meta.json exists but carries no codex_session_id key.
                events, result = _stage_worker(tmp, meta_thread="")
                before = lib.read_handle("p-2")
                rc, calls, _out, err, _live = _drive_resume(
                    ["resume", "p-2", "--", "continue"])
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])           # NOTHING was launched
                self.assertIn("no captured Codex thread id", err)
                self.assertIn("NEVER falls back", err)
                # The readable prior state is untouched.
                self.assertEqual(lib.read_handle("p-2"), before)
                self.assertEqual(result.read_text(), "attempt-1 answer")
                self.assertTrue(events.exists())

    def test_missing_meta_file_never_launches(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(tmp)
                (tmp / "sessions" / "p-2.meta.json").unlink()
                rc, calls, _out, err, _live = _drive_resume(
                    ["resume", "p-2", "--", "continue"])
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])
                self.assertIn("no captured Codex thread id", err)

    def test_malformed_thread_id_never_launches(self):
        """The 01-01 hazard: a non-UUID id is a thread NAME to Codex, which
        silently starts a brand-new conversation and exits 0."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(tmp, meta_thread="not-a-uuid-zzz")
                rc, calls, _out, err, _live = _drive_resume(
                    ["resume", "p-2", "--", "continue"])
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])
                self.assertIn("malformed", err)
                self.assertIn("unrelated new conversation", err)

    def test_meta_artifact_disagreement_never_launches(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(
                    tmp, meta_thread="02b00000-0000-7000-8000-000000000002")
                rc, calls, _out, err, _live = _drive_resume(
                    ["resume", "p-2", "--", "continue"])
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])
                self.assertIn("corrupted thread identity", err)

    def test_handle_session_id_disagreement_never_launches(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(
                    tmp, handle_session_id="03c00000-0000-7000-8000-000000000003")
                rc, calls, _out, _err, _live = _drive_resume(
                    ["resume", "p-2", "--", "continue"])
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])

    def test_wrong_provider_resume_never_launches(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # A Claude handle (legacy shape) plus an amux meta with a
                # thread id: resume must still refuse — it is a Codex op.
                lib.write_handle("cl-1", lib.new_handle(
                    name="cl-1", session_id="11111111-2222-3333-4444-555555555555",
                    run_id="r", abs_dir="/ws/p", transcript_path="/t.jsonl",
                    stuck_after_s=600,
                ))
                _write_meta(tmp, "cl-1", THREAD_ID)
                rc, calls, _out, err, _live = _drive_resume(
                    ["resume", "cl-1", "--", "continue"])
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])
                self.assertIn("Codex-thread operation", err)

    def test_missing_prompt_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(tmp)
                rc, calls, _out, _err, _live = _drive_resume(["resume", "p-2"])
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])

    def test_unknown_handle_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                rc, calls, _out, _err, _live = _drive_resume(
                    ["resume", "ghost", "--", "go"])
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])


# ── Flags that cannot ride a resume: refusal with guidance ───────────────────


class TestResumeFlagRefusals(unittest.TestCase):
    def _refused(self, tmp: Path, extra: list[str], needle: str):
        with _redirect_amux_home(tmp):
            lib.ensure_dirs()
            _stage_worker(tmp)
            rc, calls, _out, err, _live = _drive_resume(
                ["resume", "p-2", *extra, "--", "continue"])
            self.assertEqual(rc, 1, err)
            self.assertEqual(calls, [], "a refused flag must not launch")
            self.assertIn(needle, err)
            return err

    def test_profile_flag_refused_with_guidance(self):
        with tempfile.TemporaryDirectory() as d:
            err = self._refused(
                Path(d), ["--profile", "glm"], "--profile cannot ride a resume")
            # The guidance covers BOTH meanings: our Claude profiles.toml
            # concept and codex's own -p/--profile rejection on resume.
            self.assertIn("profiles.toml", err)
            self.assertIn("codex exec resume", err)

    def test_codex_short_profile_flag_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self._refused(Path(d), ["-p", "ci"], "cannot ride a resume")

    def test_yolo_refused_on_resume(self):
        with tempfile.TemporaryDirectory() as d:
            self._refused(Path(d), ["--yolo"], "registration-time choice")

    def test_claude_permission_flag_refused_on_resume(self):
        with tempfile.TemporaryDirectory() as d:
            self._refused(
                Path(d), ["--dangerously-skip-permissions"], "Claude flag")

    def test_sandbox_flag_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self._refused(Path(d), ["-s", "danger-full-access"], "cannot ride a resume")

    def test_cd_flag_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self._refused(Path(d), ["-C", "/elsewhere"], "cannot ride a resume")

    def test_dir_override_refused(self):
        with tempfile.TemporaryDirectory() as d:
            self._refused(
                Path(d), ["--dir", "/elsewhere"], "workspace the session was spawned")


# ── §7 coordination: lock, serialization, concurrent rejection ────────────────


class TestResumeCoordination(unittest.TestCase):
    def test_resume_holds_the_spawn_lock_across_the_launch(self):
        """§7 step 1: the NORMAL spawn/session lock is acquired before any
        check and held across the amux launch + handle update."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            events: list[str] = []
            real_lock = cli.SpawnLock

            class InstrumentedLock(real_lock):  # type: ignore[misc, valid-type]
                def __enter__(self):
                    events.append("lock-enter")
                    return super().__enter__()

                def __exit__(self, *exc):
                    events.append("lock-exit")
                    return super().__exit__(*exc)

            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(tmp)

                def fake_run(cmd, *_a, **_kw):
                    events.append("amux:" + " ".join(cmd[:4]))
                    return _FakeCompleted(0)

                with patch.object(cli, "SpawnLock", InstrumentedLock), \
                        patch.object(cli.lib, "resolve_amux_session",
                                     return_value=None), \
                        patch.object(cli.lib, "tmux_has_session",
                                     return_value=True), \
                        patch.object(cli.subprocess, "run", side_effect=fake_run), \
                        patch("sys.stdin") as stdin, \
                        patch("sys.stdout", io.StringIO()), \
                        patch("sys.stderr", io.StringIO()):
                    stdin.isatty.return_value = False
                    rc = cli.main(["resume", "p-2", "--", "go"])
                self.assertEqual(rc, 0)
            self.assertEqual(events[0], "lock-enter")
            self.assertTrue(any(e.startswith("amux:") for e in events))
            self.assertEqual(events[-1], "lock-exit")
            self.assertLess(events.index("lock-enter"),
                            next(i for i, e in enumerate(events)
                                 if e.startswith("amux:")))
            self.assertGreater(events.index("lock-exit"),
                               next(i for i, e in enumerate(events)
                                    if e.startswith("amux:")))

    def test_second_resume_while_attempt_in_flight_is_rejected(self):
        """Concurrent live attempts: after one resume launches, the handle is
        at attempt 2 with 1 segment — a second resume must refuse, with no
        second amux launch."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(tmp)
                live: set[str] = set()
                rc1, calls1, _o1, _e1, live = _drive_resume(
                    ["resume", "p-2", "--", "first"], live_names=live)
                self.assertEqual(rc1, 0)
                rc2, calls2, _o2, err2, _live = _drive_resume(
                    ["resume", "p-2", "--", "second"], live_names=live)
                self.assertEqual(rc2, 1)
                self.assertEqual(len(calls2), 0)  # no second launch
                self.assertIn("refusing", err2)
                # And exactly one resume was ever launched (the first call).
                self.assertIsNotNone(_amux_resume_call(calls1))

    def test_running_worker_resume_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(tmp, fixture="exec_terminated_sigterm", rc=None,
                              result=None, attempt=1)
                rc, calls, _out, err, _live = _drive_resume(
                    ["resume", "p-2", "--", "go"],
                    live_names={"p-2"})
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])
                self.assertIn("refusing to resume a live attempt", err)

    def test_terminated_worker_can_resume(self):
        """A failed first attempt is resumable (§7 step 2: idle/terminated)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(tmp, fixture="exec_turn_failed", rc=1, result=None)
                rc, calls, _out, _err, _live = _drive_resume(
                    ["resume", "p-2", "--", "try again"])
                self.assertEqual(rc, 0)
                self.assertIsNotNone(_amux_resume_call(calls))


# ── Attempt/segment scheme + newest-successful `last` ────────────────────────


class TestAttemptSemantics(unittest.TestCase):
    """Real-fixture evidence: the exact captured thread id is resumed, the
    new segment appends (never truncates), and `last` returns the newest
    SUCCESSFUL result."""

    def _resume_ok(self, tmp: Path, **stage_kw):
        events, result = _stage_worker(tmp, **stage_kw)
        rc, calls, _out, _err, live = _drive_resume(
            ["resume", "p-2", "--", "and now?"])
        assert rc == 0, calls
        return events, result, live

    def test_exact_thread_id_is_resumed_and_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result, _live = self._resume_ok(tmp)
                h = lib.read_handle("p-2")
                # The handle now carries the EXACT captured id (from amux's
                # meta.json — never minted here) and the bumped attempt.
                self.assertEqual(h["session_id"], THREAD_ID)
                self.assertEqual(h["attempt"], 2)
                self.assertEqual(h["state"], "spawning")
                # Prior evidence is intact — byte-for-byte.
                self.assertEqual(events.read_bytes(), _fixture("exec_success"))
                self.assertEqual(result.read_text(), "attempt-1 answer")

    def test_pending_attempt_does_not_false_read_idle(self):
        """A freshly resumed worker must read running/spawning even though the
        artifact still ENDS with the previous attempt's turn.completed."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, _result, live = self._resume_ok(tmp)
                handle = lib.read_handle("p-2")
                buf = io.StringIO()
                with patch.object(cli.lib, "tmux_has_session",
                                  side_effect=lambda n: n in live), \
                        patch("sys.stdout", buf):
                    rc = cli.main(["status", "p-2", "--json"])
                self.assertEqual(rc, 0)
                data = json.loads(buf.getvalue())
                self.assertEqual(data["state"], "running")
                self.assertEqual(data["attempt"], 2)
                self.assertTrue(data["signals"]["attempt_pending"])
                self.assertTrue(data["signals"]["turn_completed"])  # attempt 1's

    def test_attempt2_success_supersedes_attempt1_in_last(self):
        """The resumed segment (real fixture) lands appended; an idle attempt
        2 supersedes attempt 1 and `last` returns the NEW result."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result, live = self._resume_ok(tmp)
                # The second segment arrives: the real resume fixture — the
                # SAME thread id, proving the resumed turn really continued it.
                second = _fixture("exec_resume_success")
                first_thread = json.loads(events.read_bytes().splitlines()[0])
                appended_thread = json.loads(second.splitlines()[0])
                self.assertEqual(appended_thread["thread_id"],
                                 first_thread["thread_id"])
                with open(events, "ab") as f:
                    f.write(second)
                Path(f"{events}.rc").write_text("0")
                result.write_text("attempt-2 answer")

                handle = lib.read_handle("p-2")
                with patch.object(cli.lib, "tmux_has_session",
                                  return_value=False):
                    status = cli._derive_status(handle, None)
                    self.assertEqual(status["state"], "idle")
                    self.assertEqual(status["attempt"], 2)
                    self.assertFalse(status["signals"]["attempt_pending"])
                buf = io.StringIO()
                with patch.object(cli.lib, "tmux_has_session",
                                  return_value=False), patch("sys.stdout", buf):
                    cli.main(["last", "p-2"])
                self.assertEqual(buf.getvalue().strip(), "attempt-2 answer")

    def test_attempt2_failure_does_not_erase_attempt1_result(self):
        """A failed attempt 2 reads terminated at attempt 2, but `last` still
        returns attempt 1's readable result — the newest SUCCESSFUL one."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result, _live = self._resume_ok(tmp)
                # Attempt 2's segment: same thread, explicit turn failure.
                with open(events, "ab") as f:
                    f.write(b'{"type":"thread.started","thread_id":"'
                            + THREAD_ID.encode()
                            + b'"}\n{"type":"turn.started"}\n'
                              b'{"type":"turn.failed","error":{"message":"boom"}}\n')
                Path(f"{events}.rc").write_text("1")
                # Codex does not write R on failure — attempt 1's answer stays.

                handle = lib.read_handle("p-2")
                with patch.object(cli.lib, "tmux_has_session",
                                  return_value=False):
                    status = cli._derive_status(handle, None)
                self.assertEqual(status["state"], "terminated")
                self.assertEqual(status["attempt"], 2)
                self.assertEqual(status["failure"]["reason"], "turn_failed")
                buf = io.StringIO()
                with patch.object(cli.lib, "tmux_has_session",
                                  return_value=False), patch("sys.stdout", buf):
                    cli.main(["last", "p-2"])
                self.assertEqual(buf.getvalue().strip(), "attempt-1 answer")

    def test_legacy_handle_without_attempt_still_resumes_and_counts(self):
        """A pre-20-04 codex handle (no ``attempt`` key): the base attempt is
        taken from the artifact's segments, and the bump fills the key."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage_worker(tmp, attempt=0)
                self.assertNotIn("attempt", lib.read_handle("p-2"))
                rc, calls, _out, _err, _live = _drive_resume(
                    ["resume", "p-2", "--", "and now?"])
                self.assertEqual(rc, 0)
                self.assertIsNotNone(_amux_resume_call(calls))
                h = lib.read_handle("p-2")
                self.assertEqual(h.get("attempt"), 2)  # 1 segment + 1
                self.assertEqual(h["session_id"], THREAD_ID)

    def test_ls_json_exposes_attempt_for_codex_rows_only(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(tmp, name="rev-1")  # completed -> idle
                _stage_worker(tmp, name="rev-2", fixture="exec_terminated_sigterm",
                              rc=None, result=None, attempt=0)  # running
                lib.write_handle("cl-1", lib.new_handle(
                    name="cl-1", session_id="s", run_id="r", abs_dir="/ws/p",
                    transcript_path="/t.jsonl", stuck_after_s=600,
                ))
                live = {"rev-2", "cl-1"}  # rev-1 idle, rev-2 running
                buf = io.StringIO()
                with patch.object(cli.lib, "tmux_has_session",
                                  side_effect=lambda n: n in live), \
                        patch("sys.stdout", buf):
                    rc = cli.main(["ls", "--json", "--dir", "/ws/p"])
                self.assertEqual(rc, 0)
                rows = {s["name"]: s for s in json.loads(buf.getvalue())["sessions"]}
                self.assertEqual(rows["rev-1"]["state"], "idle")
                self.assertEqual(rows["rev-1"]["attempt"], 1)
                # Legacy handle: the attempt is recovered from the segments.
                self.assertEqual(rows["rev-2"]["state"], "running")
                self.assertEqual(rows["rev-2"]["attempt"], 1)
                self.assertNotIn("attempt", rows["cl-1"])  # Claude row unchanged


# ── Failed launch preserves the prior readable result/state ──────────────────


class TestFailedLaunchPreservesState(unittest.TestCase):
    def test_failed_amux_launch_restores_prior_state(self):
        """Work item 4: the amux launch fails after amux removed ``.rc`` —
        the prior attempt's exit status is restored, the result file and
        event evidence stay byte-for-byte, and the handle is untouched."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage_worker(tmp)
                before = lib.read_handle("p-2")
                rc, calls, _out, err, _live = _drive_resume(
                    ["resume", "p-2", "--", "continue"], amux_rc=1)
                self.assertEqual(rc, 1)
                self.assertIsNotNone(_amux_resume_call(calls))
                self.assertIn("amux failed to resume", err)
                # .rc restored to the prior attempt's status (amux removed it
                # at launch; the failed launch wrote nothing).
                self.assertEqual(Path(f"{events}.rc").read_text(), "0")
                self.assertEqual(result.read_text(), "attempt-1 answer")
                self.assertEqual(events.read_bytes(), _fixture("exec_success"))
                self.assertEqual(lib.read_handle("p-2"), before)

    def test_unconfirmed_launch_restores_prior_state(self):
        """amux returned 0 but nothing came up (no pane, no .rc, no events):
        read as a failed resume, prior state preserved."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # A terminated attempt-1 that never wrote events (killed
                # pre-flush) but whose thread id amux did capture.
                events, result = _stage_worker(
                    tmp, fixture=None, rc=None, result=None)
                before = lib.read_handle("p-2")
                rc, calls, _out, err, _live = _drive_resume(
                    ["resume", "p-2", "--", "continue"],
                    amux_launch_live=False)
                self.assertEqual(rc, 1)
                self.assertIn("did not come up", err)
                self.assertFalse(Path(f"{events}.rc").exists())
                self.assertEqual(lib.read_handle("p-2"), before)

    def test_rc66_mismatch_reads_as_failed_resume_not_success(self):
        """amux's fail-closed outcome: the new segment's thread id did not
        echo the requested one — foreign output quarantined to ``.err``,
        SIGINT, ``.rc`` = 66. That is a FAILED resume: terminated, not idle,
        and the prior attempt's result stays readable."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                events, result = _stage_worker(tmp)
                # The launch succeeds at the amux level, but nothing comes
                # live and the wrapper's final write is the mismatch code.
                rc, calls, _out, _err, _live = _drive_resume(
                    ["resume", "p-2", "--", "continue"], amux_launch_live=False)
                self.assertEqual(rc, 0)
                Path(f"{events}.rc").write_text("66")

                handle = lib.read_handle("p-2")
                with patch.object(cli.lib, "tmux_has_session",
                                  return_value=False):
                    status = cli._derive_status(handle, None)
                self.assertEqual(status["state"], "terminated")
                self.assertEqual(status["attempt"], 2)
                self.assertEqual(status["failure"]["reason"],
                                 cli.CODEX_REASON_THREAD_MISMATCH)
                self.assertEqual(status["failure"]["exit_code"],
                                 cli.CODEX_EXIT_THREAD_MISMATCH)
                self.assertIn(".err", status["failure"]["message"])
                # The quarantine never touched the event log; the prior
                # result was not overwritten.
                self.assertEqual(events.read_bytes(), _fixture("exec_success"))
                buf = io.StringIO()
                with patch.object(cli.lib, "tmux_has_session",
                                  return_value=False), patch("sys.stdout", buf):
                    cli.main(["last", "p-2"])
                self.assertEqual(buf.getvalue().strip(), "attempt-1 answer")


# ── --wait on resume: the shared supervision contract ────────────────────────


class TestResumeWait(unittest.TestCase):
    def _resume_with_wait(self, tmp: Path, wait_result, extra=()):
        with _redirect_amux_home(tmp):
            lib.ensure_dirs()
            _stage_worker(tmp)
            with patch.object(cli, "_wait_for_idle",
                              return_value=wait_result) as mock_wait:
                rc, calls, out, err, _live = _drive_resume(
                    ["resume", "p-2", *extra, "--wait", "--", "go"])
            self.assertIsNotNone(_amux_resume_call(calls))
            return rc, out, err, mock_wait

    def test_wait_success_payload_on_stdout(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, out, _err, mock_wait = self._resume_with_wait(
                tmp, ("idle", "attempt-2 answer"))
            self.assertEqual(rc, 0)
            self.assertEqual([l for l in out.splitlines() if l.strip()],
                             ["attempt-2 answer"])
            mock_wait.assert_called_once_with("p-2", None)

    def test_wait_timeout_exit_3_marker(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, out, err, mock_wait = self._resume_with_wait(
                tmp, ("timeout", None), extra=["--timeout", "5s"])
            self.assertEqual(rc, 3)
            self.assertIn(cli.WAIT_TIMEOUT_MARKER, out)
            self.assertIn("timed out", err)
            mock_wait.assert_called_once_with("p-2", 5)

    def test_wait_error_exit_1_stdout_clean(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            rc, out, err, _mock = self._resume_with_wait(
                tmp, ("error", "session 'p-2' terminated (thread_mismatch: exit 66)"))
            self.assertEqual(rc, 1)
            self.assertEqual(out.strip(), "")
            self.assertIn("thread_mismatch", err)


# ── Claude-path drift guards ──────────────────────────────────────────────────


class TestClaudePathUnchanged(unittest.TestCase):
    """The space-form decision: DOCUMENTED, parser unchanged. The spawn-side
    space form behaves exactly as 20-02 pinned it for BOTH providers, and the
    Claude profiles surface is untouched by resume."""

    def test_spawn_space_form_still_eats_suffix(self):
        """Pre-existing epic-10 behavior, byte-for-byte: the space form's
        value becomes the suffix and amux receives the bare --model (which
        amux rejects). This is why --model=<name> is the documented form."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            calls: list[list[str]] = []
            live: set[str] = set()

            def fake_run(cmd, *_a, **_kw):
                calls.append(list(cmd))
                if cmd[:2] == ["amux", "exec"]:
                    live.add(cmd[2])
                return _FakeCompleted(1, stderr="boom")

            with _redirect_amux_home(tmp), \
                    patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                    patch.object(cli.lib, "list_amux_names", return_value=set()), \
                    patch.object(cli.lib, "tmux_has_session",
                                 side_effect=lambda n: n in live), \
                    patch.object(cli.subprocess, "run", side_effect=fake_run), \
                    patch("sys.stdin") as stdin, \
                    patch("sys.stdout"), patch("sys.stderr"):
                stdin.isatty.return_value = False
                rc = cli.main(["spawn", "--provider", "codex", "--dir", str(ws),
                               "--model", "gpt-5.5", "--", "go"])
            self.assertEqual(rc, 1)  # amux rejects the bare flag; rolled back
            call = next(c for c in calls if c[:2] == ["amux", "exec"])
            self.assertEqual(call[2], "myproj-gpt-5.5")  # eaten as the suffix
            self.assertIn("--model", call)
            self.assertNotIn("gpt-5.5", call)

    def test_claude_handle_resume_refusal_mentions_attach_not_profiles(self):
        """The Claude refusal is about the provider, not about profiles.toml —
        Claude profile behavior on spawn is governed by its own suites."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                lib.write_handle("cl-1", lib.new_handle(
                    name="cl-1", session_id="11111111-2222-3333-4444-555555555555",
                    run_id="r", abs_dir="/ws/p", transcript_path="/t.jsonl",
                    stuck_after_s=600,
                ))
                rc, calls, _out, err, _live = _drive_resume(
                    ["resume", "cl-1", "--", "go"])
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [])
                self.assertIn("a|attach", err)


# ── Hermeticity ───────────────────────────────────────────────────────────────


class TestHermeticity(unittest.TestCase):
    def test_no_yolo_bypass_flag_in_any_constructed_argv(self):
        """Only amux expands --yolo (architecture §8): no constructed argv in
        this suite may spell Codex's bypass flag or Claude's skip-permissions."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                _stage_worker(tmp)
                rc, calls, _out, _err, _live = _drive_resume(
                    ["resume", "p-2", "--yolo", "--", "go"])  # refused anyway
                self.assertEqual(rc, 1)
                for call in calls:
                    self.assertNotIn(
                        "--dangerously-bypass-approvals-and-sandbox", call)
                    self.assertNotIn("--dangerously-skip-permissions", call)

    def test_uuid_helper_matches_amux_shape(self):
        self.assertTrue(lib.is_uuid_string(THREAD_ID))
        self.assertTrue(lib.is_uuid_string(THREAD_ID.upper()))
        self.assertFalse(lib.is_uuid_string("not-a-uuid-zzz"))
        self.assertFalse(lib.is_uuid_string(""))
        self.assertFalse(lib.is_uuid_string(None))
        self.assertFalse(lib.is_uuid_string(
            "01a00000-0000-7000-8000-000000000001x"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
