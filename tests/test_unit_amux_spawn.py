#!/usr/bin/env python3
"""Unit + headless tests for ``amux-spawn`` (task 10-01).

Two layers:

1. Unit tests of ``amux_spawn_lib`` (transcript encoding, env parsing, naming,
   atomic handle read/write against the architecture s6.0 schema, the
   per-workspace fork-bomb cap) and the CLI dispatch / lock / cap path with amux
   + tmux mocked. These run everywhere, network-free, fail-open friendly.

2. A live headless spawn (``TestLiveSpawn``) that drives the real ``amux`` +
   ``claude`` against a temp dir, asserting the handle JSON, the minted UUID id,
   the transcript path, ``tmux has-session`` true, and that the seeded prompt
   produced a turn. Skipped automatically unless ``AMUX_SPAWN_LIVE_TEST=1`` is
   set (it needs a working tmux server and live model auth, and would create a
   real session) — so the default suite stays hermetic.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
_BIN = Path(__file__).parent.parent / ".claude" / "bin" / "amux-spawn"
sys.path.insert(0, str(_HOOKS))

import amux_spawn_lib as lib  # noqa: E402


def _load_cli():
    """Import the executable ``amux-spawn`` (no .py extension) as a module."""
    spec = importlib.util.spec_from_loader(
        "amux_spawn_cli",
        importlib.machinery.SourceFileLoader("amux_spawn_cli", str(_BIN)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()


def _redirect_amux_home(tmp: Path):
    """Point the lib's amux paths at a throwaway dir for a test."""
    return patch.multiple(
        lib,
        AMUX_HOME=tmp,
        AMUX_SESSIONS_DIR=tmp / "sessions",
        SPAWN_DIR=tmp / "spawn",
        SPAWN_LOCK=tmp / "spawn" / ".lock",
    )


class TestTranscriptEncoding(unittest.TestCase):
    def test_slash_and_dot_both_become_dash(self):
        # /home/anton/.local/x -> -home-anton--local-x (dot in .local -> dash,
        # plus the leading slash -> the double dash).
        self.assertEqual(
            lib.encode_project_dir("/home/anton/.local/x"),
            "-home-anton--local-x",
        )

    def test_keeps_underscore(self):
        self.assertEqual(
            lib.encode_project_dir("/a/my_dir/v3.2"),
            "-a-my_dir-v3-2",
        )

    def test_transcript_path_uses_session_id_stem(self):
        sid = "11111111-2222-3333-4444-555555555555"
        p = lib.transcript_path_for("/tmp/work", sid)
        self.assertTrue(p.endswith(f"-tmp-work/{sid}.jsonl"))


class TestEnvParsing(unittest.TestCase):
    def test_parse_quoted_env(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.AMUX_SESSIONS_DIR.mkdir(parents=True)
                (lib.AMUX_SESSIONS_DIR / "foo.env").write_text(
                    '# comment\nCC_NAME="foo"\nCC_DIR="/abs/path"\n'
                    'CC_FLAGS="--model opus --yolo"\n'
                )
                self.assertEqual(lib.parent_cc_dir("foo"), "/abs/path")
                self.assertEqual(
                    lib.parent_cc_flags("foo"), ["--model", "opus", "--yolo"]
                )

    def test_missing_env_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                self.assertIsNone(lib.parent_cc_dir("nope"))
                self.assertEqual(lib.parent_cc_flags("nope"), [])

    def test_extract_model_flag(self):
        self.assertEqual(
            lib.extract_model_flag(["--model", "opus", "--yolo"]), "opus"
        )
        self.assertEqual(lib.extract_model_flag(["--model=sonnet"]), "sonnet")
        self.assertIsNone(lib.extract_model_flag(["--yolo"]))


class TestNaming(unittest.TestCase):
    def test_prefix_is_basename(self):
        self.assertEqual(lib.workspace_prefix("/a/b/claude-hooks"), "claude-hooks")

    def test_pick_free_name_increments(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                with patch.object(lib, "list_amux_names", return_value=set()), \
                        patch.object(lib, "tmux_has_session", return_value=False):
                    # nothing taken -> bare prefix
                    self.assertEqual(lib.pick_free_name("proj"), "proj")
                    # prefix taken -> -2
                    (lib.AMUX_SESSIONS_DIR / "proj.env").write_text('CC_NAME="proj"\n')
                    self.assertEqual(lib.pick_free_name("proj"), "proj-2")
                    (lib.AMUX_SESSIONS_DIR / "proj-2.env").write_text('CC_NAME="proj-2"\n')
                    self.assertEqual(lib.pick_free_name("proj"), "proj-3")

    def test_explicit_suffix(self):
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                lib.ensure_dirs()
                self.assertEqual(lib.pick_free_name("proj", "review"), "proj-review")


class TestHandle(unittest.TestCase):
    def test_new_handle_has_exactly_schema_fields(self):
        h = lib.new_handle(
            name="proj-2",
            session_id="abc",
            run_id="rid",
            abs_dir="/abs",
            transcript_path="/t.jsonl",
            stuck_after_s=600,
        )
        self.assertEqual(set(h.keys()), set(lib.HANDLE_FIELDS))
        self.assertEqual(h["state"], "spawning")
        self.assertEqual(h["stuck_after_s"], 600)
        self.assertIsNone(h["mtime_at_stop"])
        self.assertEqual(h["background_tasks"], [])
        self.assertFalse(h["permission_pending"])
        # Epic-20 (20-01) migration: provider defaults to claude, the activity
        # clock mirrors the transcript, and the Codex lifecycle fields start empty.
        self.assertEqual(h["provider"], lib.PROVIDER_CLAUDE)
        self.assertEqual(h["activity_path"], "/t.jsonl")
        self.assertEqual(h["transcript_path"], "/t.jsonl")
        self.assertIsNone(h["result_path"])
        self.assertIsNone(h["process_pid"])
        self.assertIsNone(h["exit_code"])
        self.assertIsNone(h["failure"])

    def test_codex_handle_atomic_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                lib.ensure_dirs()
                h = lib.new_handle(
                    name="review-123",
                    session_id="01a00000-0000-7000-8000-000000000001",
                    run_id="rid", abs_dir="/abs", transcript_path="",
                    stuck_after_s=600, provider=lib.PROVIDER_CODEX,
                    activity_path="/abs/review-123.events.jsonl",
                    result_path="/abs/review-123.last.md",
                    process_pid=4242,
                )
                self.assertEqual(set(h.keys()), set(lib.HANDLE_FIELDS))
                lib.write_handle("review-123", h)
                back = lib.read_handle("review-123")
                self.assertEqual(back, h)
                self.assertTrue(lib.is_codex_handle(back))
                self.assertEqual(list(lib.SPAWN_DIR.glob(".review-123.*.tmp")), [])

    def test_atomic_write_read_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                lib.ensure_dirs()
                h = lib.new_handle(
                    name="proj-2", session_id="abc", run_id="rid",
                    abs_dir="/abs", transcript_path="/t.jsonl", stuck_after_s=10,
                )
                lib.write_handle("proj-2", h)
                back = lib.read_handle("proj-2")
                self.assertEqual(back, h)
                # No stray temp files left behind.
                leftovers = list(lib.SPAWN_DIR.glob(".proj-2.*.tmp"))
                self.assertEqual(leftovers, [])

    def test_read_malformed_handle_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                lib.ensure_dirs()
                lib.handle_path("bad").write_text("{not json")
                self.assertIsNone(lib.read_handle("bad"))

    def test_mint_uuid_is_valid_uuid(self):
        v = lib.mint_uuid()
        # Round-trips through uuid.UUID -> a real UUID (Claude requires this).
        self.assertEqual(str(uuid.UUID(v)), v)


class TestForkBombCap(unittest.TestCase):
    def test_counts_only_live_tracked_in_workspace(self):
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                lib.ensure_dirs()
                # two handles in /ws, one in /other
                for name, wsdir in [("a", "/ws"), ("b", "/ws"), ("c", "/other")]:
                    lib.write_handle(name, lib.new_handle(
                        name=name, session_id="s", run_id="r", abs_dir=wsdir,
                        transcript_path="/t", stuck_after_s=1,
                    ))
                # 'a' live, 'b' dead, 'c' live but other workspace
                live = {"a", "c"}
                with patch.object(lib, "tmux_has_session",
                                  side_effect=lambda n: n in live):
                    self.assertEqual(lib.live_tracked_count("/ws"), 1)
                    self.assertEqual(lib.live_tracked_count("/other"), 1)

    def test_max_sessions_env_override(self):
        with patch.dict(os.environ, {"AMUX_SPAWN_MAX_SESSIONS": "3"}):
            self.assertEqual(lib.max_sessions(), 3)
        with patch.dict(os.environ, {"AMUX_SPAWN_MAX_SESSIONS": "bogus"}):
            self.assertEqual(lib.max_sessions(), lib.DEFAULT_MAX_SESSIONS)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AMUX_SPAWN_MAX_SESSIONS", None)
            self.assertEqual(lib.max_sessions(), 16)


class TestStuckAfterParsing(unittest.TestCase):
    def test_parsing(self):
        self.assertEqual(cli.parse_stuck_after("600"), 600)
        self.assertEqual(cli.parse_stuck_after("10m"), 600)
        self.assertEqual(cli.parse_stuck_after("2h"), 7200)
        self.assertEqual(cli.parse_stuck_after("30s"), 30)
        self.assertEqual(cli.parse_stuck_after(None), lib.DEFAULT_STUCK_AFTER_S)
        self.assertEqual(cli.parse_stuck_after("junk"), lib.DEFAULT_STUCK_AFTER_S)


class TestSplitPrompt(unittest.TestCase):
    def test_split(self):
        before, prompt = cli._split_prompt(["foo", "--yolo", "--", "do", "the", "thing"])
        self.assertEqual(before, ["foo", "--yolo"])
        self.assertEqual(prompt, "do the thing")

    def test_no_separator(self):
        before, prompt = cli._split_prompt(["foo", "--yolo"])
        self.assertEqual(before, ["foo", "--yolo"])
        self.assertIsNone(prompt)


class TestSpawnDispatch(unittest.TestCase):
    """Drive cmd_spawn with amux/tmux mocked, asserting the tracked agent path."""

    def _run_spawn_nontty(self, tmp: Path, prompt: str, extra_argv=None):
        created = {}
        # Names that "exist" in tmux. Empty until create runs, so name allocation
        # picks the bare prefix; True afterward so the post-create liveness check
        # passes.
        live_names: set[str] = set()

        def fake_create(*, name, abs_dir, forward_flags, session_id, prompt):
            created["name"] = name
            created["abs_dir"] = abs_dir
            created["forward_flags"] = forward_flags
            created["session_id"] = session_id
            created["prompt"] = prompt
            live_names.add(name)
            return 0, None

        argv = ["spawn"] + (extra_argv or []) + ["--", prompt]
        with _redirect_amux_home(tmp), \
                patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                patch.object(cli.lib, "list_amux_names", return_value=set()), \
                patch.object(cli.lib, "tmux_has_session",
                             side_effect=lambda n: n in live_names), \
                patch.object(cli, "_amux_create_detached", side_effect=fake_create), \
                patch("sys.stdin") as stdin, patch("sys.stdout") as stdout:
            stdin.isatty.return_value = False
            stdout.isatty.return_value = False
            rc = cli.main(argv)
            # Read the handle back inside the redirected-home context (outside it,
            # lib.SPAWN_DIR reverts to the real path).
            handle = lib.read_handle(created.get("name", "")) if created else None
        return rc, created, handle

    def test_nontty_tracked_writes_handle_with_uuid(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, created, h = self._run_spawn_nontty(
                tmp, "hello world", extra_argv=["--dir", str(ws)]
            )
            self.assertEqual(rc, 0)
            self.assertEqual(created["name"], "myproj")
            self.assertEqual(created["prompt"], "hello world")
            # minted session-id passed to amux, and a valid UUID
            sid = created["session_id"]
            self.assertIsNotNone(sid)
            self.assertEqual(str(uuid.UUID(sid)), sid)
            # handle written under the spawn registry
            self.assertIsNotNone(h)
            self.assertEqual(h["session_id"], sid)
            self.assertEqual(h["dir"], str(ws))
            self.assertEqual(h["state"], "spawning")
            self.assertTrue(h["transcript_path"].endswith(f"{sid}.jsonl"))
            self.assertEqual(set(h.keys()), set(lib.HANDLE_FIELDS))

    def test_fork_bomb_cap_refuses(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            with patch.dict(os.environ, {"AMUX_SPAWN_MAX_SESSIONS": "2"}):
                # pre-seed 2 live tracked handles in the workspace
                with _redirect_amux_home(tmp):
                    lib.ensure_dirs()
                    for nm in ("x", "y"):
                        lib.write_handle(nm, lib.new_handle(
                            name=nm, session_id="s", run_id="r", abs_dir=str(ws),
                            transcript_path="/t", stuck_after_s=1,
                        ))

                create_called = {"n": 0}

                def fake_create(**_kw):
                    create_called["n"] += 1
                    return 0, None

                argv = ["spawn", "--dir", str(ws), "--", "go"]
                with _redirect_amux_home(tmp), \
                        patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                        patch.object(cli.lib, "tmux_has_session", return_value=True), \
                        patch.object(cli, "_amux_create_detached", side_effect=fake_create), \
                        patch("sys.stdin") as stdin, patch("sys.stdout") as stdout, \
                        patch("sys.stderr"):
                    stdin.isatty.return_value = False
                    stdout.isatty.return_value = False
                    rc = cli.main(argv)
                self.assertEqual(rc, 1)
                self.assertEqual(create_called["n"], 0)  # never tried to create

    def test_model_inheritance_from_parent(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # parent session env declares --model opus
                (lib.AMUX_SESSIONS_DIR / "parent.env").write_text(
                    f'CC_NAME="parent"\nCC_DIR="{ws}"\nCC_FLAGS="--model opus"\n'
                )

            created = {}
            live_names: set[str] = set()

            def fake_create(*, name, abs_dir, forward_flags, session_id, prompt):
                created["forward_flags"] = forward_flags
                created["abs_dir"] = abs_dir
                live_names.add(name)
                return 0, None

            with _redirect_amux_home(tmp), \
                    patch.object(cli.lib, "resolve_amux_session", return_value="parent"), \
                    patch.object(cli.lib, "list_amux_names", return_value=set()), \
                    patch.object(cli.lib, "tmux_has_session",
                                 side_effect=lambda n: n in live_names), \
                    patch.object(cli, "_amux_create_detached", side_effect=fake_create), \
                    patch("sys.stdin") as stdin, patch("sys.stdout") as stdout:
                stdin.isatty.return_value = False
                stdout.isatty.return_value = False
                rc = cli.main(["spawn", "--", "go"])
            self.assertEqual(rc, 0)
            # inherited CC_DIR from parent, and propagated --model opus
            self.assertEqual(created["abs_dir"], str(ws))
            self.assertIn("--model", created["forward_flags"])
            self.assertIn("opus", created["forward_flags"])


class TestRunIdInheritance(unittest.TestCase):
    """Unit tests for resolve_run_id() — the three D-RunId branches."""

    def test_explicit_run_id_override_wins(self):
        # --run-id always takes precedence, even when a parent handle exists.
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                lib.ensure_dirs()
                lib.write_handle("parent", lib.new_handle(
                    name="parent", session_id="s", run_id="from-parent",
                    abs_dir="/ws", transcript_path="/t", stuck_after_s=1,
                ))
                result = cli.resolve_run_id("my-override", "parent")
                self.assertEqual(result, "my-override")

    def test_inherits_parent_run_id_when_no_override(self):
        # No --run-id: inherit the parent handle's run_id.
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                lib.ensure_dirs()
                lib.write_handle("parent", lib.new_handle(
                    name="parent", session_id="s", run_id="parent-run-id",
                    abs_dir="/ws", transcript_path="/t", stuck_after_s=1,
                ))
                result = cli.resolve_run_id(None, "parent")
                self.assertEqual(result, "parent-run-id")

    def test_mints_new_uuid_when_no_override_and_no_parent(self):
        # No --run-id and no parent: mint a fresh UUID.
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                lib.ensure_dirs()
                result = cli.resolve_run_id(None, None)
                # Must be a valid UUID.
                self.assertEqual(str(uuid.UUID(result)), result)

    def test_new_sentinel_mints_fresh_uuid(self):
        # --run-id new must return a valid UUID, not the literal string "new".
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                lib.ensure_dirs()
                result = cli.resolve_run_id("new", None)
                self.assertNotEqual(result, "new", "'new' sentinel must not be stored literally")
                self.assertEqual(str(uuid.UUID(result)), result, "result must be a valid UUID")

    def test_new_sentinel_mints_distinct_uuids(self):
        # Two --run-id new calls must produce two different UUIDs.
        with tempfile.TemporaryDirectory() as d:
            with _redirect_amux_home(Path(d)):
                lib.ensure_dirs()
                first = cli.resolve_run_id("new", None)
                second = cli.resolve_run_id("new", None)
                self.assertNotEqual(first, second, "each 'new' call must produce a distinct UUID")


class TestTTYPlainPath(unittest.TestCase):
    """TTY/human path: tracked=False, no handle written, no session_id passed."""

    def test_tty_plain_no_handle_no_session_id(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()

            create_calls = []
            live_names: set[str] = set()

            def fake_create(*, name, abs_dir, forward_flags, session_id, prompt):
                create_calls.append({"name": name, "session_id": session_id})
                live_names.add(name)
                return 0, None

            argv = ["spawn", "--detach", "--dir", str(ws)]
            with _redirect_amux_home(tmp), \
                    patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                    patch.object(cli.lib, "list_amux_names", return_value=set()), \
                    patch.object(cli.lib, "tmux_has_session",
                                 side_effect=lambda n: n in live_names), \
                    patch.object(cli, "_amux_create_detached", side_effect=fake_create), \
                    patch("sys.stdin") as stdin, patch("sys.stdout") as stdout:
                stdin.isatty.return_value = True
                stdout.isatty.return_value = True
                rc = cli.main(argv)

            self.assertEqual(rc, 0)
            # One create call, no session_id on the plain path.
            self.assertEqual(len(create_calls), 1)
            self.assertIsNone(create_calls[0]["session_id"])
            # No handle written in the spawn registry.
            spawn_dir = tmp / "spawn"
            handles = list(spawn_dir.glob("*.json")) if spawn_dir.exists() else []
            self.assertEqual(handles, [])


class TestExplicitSuffixConflict(unittest.TestCase):
    """Explicit suffix conflict: rc==1, _amux_create_detached never called."""

    def test_explicit_suffix_conflict_returns_1_no_create(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()

            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # Pre-create a .env for "myproj-review" to simulate a taken name.
                (lib.AMUX_SESSIONS_DIR / "myproj-review.env").write_text(
                    'CC_NAME="myproj-review"\nCC_DIR="/ws"\n'
                )

            create_called = {"n": 0}

            def fake_create(**_kw):
                create_called["n"] += 1
                return 0, None

            argv = ["spawn", "review", "--dir", str(ws)]
            with _redirect_amux_home(tmp), \
                    patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                    patch.object(cli.lib, "list_amux_names", return_value=set()), \
                    patch.object(cli.lib, "tmux_has_session", return_value=False), \
                    patch.object(cli, "_amux_create_detached", side_effect=fake_create), \
                    patch("sys.stdin") as stdin, patch("sys.stdout") as stdout, \
                    patch("sys.stderr"):
                stdin.isatty.return_value = False
                stdout.isatty.return_value = False
                rc = cli.main(argv)

            self.assertEqual(rc, 1)
            self.assertEqual(create_called["n"], 0)


class TestLivenessCheckFailure(unittest.TestCase):
    """_amux_create_detached rc=0 but tmux_has_session returns False -> rc==1."""

    def test_create_succeeds_but_session_not_live_returns_1(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()

            def fake_create(*, name, abs_dir, forward_flags, session_id, prompt):
                # Succeeds (rc=0) but does NOT add the name to any live set.
                return 0, None

            argv = ["spawn", "--dir", str(ws), "--", "go"]
            with _redirect_amux_home(tmp), \
                    patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                    patch.object(cli.lib, "list_amux_names", return_value=set()), \
                    patch.object(cli.lib, "tmux_has_session", return_value=False), \
                    patch.object(cli, "_amux_create_detached", side_effect=fake_create), \
                    patch("sys.stdin") as stdin, patch("sys.stdout") as stdout, \
                    patch("sys.stderr"):
                stdin.isatty.return_value = False
                stdout.isatty.return_value = False
                rc = cli.main(argv)

            self.assertEqual(rc, 1)
            # No handle should have been written (liveness check failed before that).
            spawn_dir = tmp / "spawn"
            handles = list(spawn_dir.glob("*.json")) if spawn_dir.exists() else []
            self.assertEqual(handles, [])


class TestSpawnPinWarnings(unittest.TestCase):
    """Warn on an unpinned model or effort on the non-TTY path (epic 21, 21-01).

    Cases 1–7 drive ``cmd_spawn`` end-to-end (amux/tmux mocked).
    Case 8 is a unit test of ``extract_flag_value``.
    """

    # ── helpers ──────────────────────────────────────────────────────────────

    def _run_nontty(self, tmp: Path, ws: Path, extra_argv=None,
                    parent_name=None, parent_flags=None, anthropic_model=None):
        """Drive a non-TTY spawn via ``cli.main`` and return
        ``(rc, stderr_text, forward_flags)``.

        ``anthropic_model``: when not None, sets ANTHROPIC_MODEL in the env.
        When None, removes ANTHROPIC_MODEL so each test is isolated.

        Does NOT change the 3-tuple arity of
        ``TestSpawnDispatch._run_spawn_nontty``; use a sibling helper instead
        of modifying that method.

        Limitation: ``parse_known_args`` inside ``cli.main`` consumes a bare
        token following an unknown flag (e.g. ``--model opus``) as the
        positional ``suffix`` rather than as the flag's value. Use
        ``--model=opus`` (equals form) for model/effort pins in ``extra_argv``
        to avoid this. For space-form tests use ``_run_cmd_spawn_direct``.
        """
        created: dict = {}
        live_names: set[str] = set()

        def fake_create(*, name, abs_dir, forward_flags, session_id, prompt):
            created["name"] = name
            created["forward_flags"] = forward_flags
            live_names.add(name)
            return 0, None

        argv = ["spawn", "--dir", str(ws)] + (extra_argv or []) + ["--", "go"]
        stderr_buf = io.StringIO()

        with contextlib.ExitStack() as stack:
            stack.enter_context(_redirect_amux_home(tmp))
            stack.enter_context(
                patch.object(cli.lib, "resolve_amux_session", return_value=parent_name)
            )
            stack.enter_context(
                patch.object(cli.lib, "list_amux_names", return_value=set())
            )
            stack.enter_context(
                patch.object(cli.lib, "tmux_has_session",
                             side_effect=lambda n: n in live_names)
            )
            stack.enter_context(
                patch.object(cli, "_amux_create_detached", side_effect=fake_create)
            )
            mock_stdin = stack.enter_context(patch("sys.stdin"))
            mock_stdout = stack.enter_context(patch("sys.stdout"))
            mock_stdin.isatty.return_value = False
            mock_stdout.isatty.return_value = False

            if parent_flags is not None:
                stack.enter_context(
                    patch.object(cli.lib, "parent_cc_flags", return_value=parent_flags)
                )

            # Isolate ANTHROPIC_MODEL: set it or ensure it is absent.
            if anthropic_model is not None:
                stack.enter_context(
                    patch.dict(os.environ, {"ANTHROPIC_MODEL": anthropic_model}, clear=False)
                )
            else:
                stack.enter_context(patch.dict(os.environ, {}, clear=False))
                os.environ.pop("ANTHROPIC_MODEL", None)

            stack.enter_context(contextlib.redirect_stderr(stderr_buf))
            rc = cli.main(argv)

        return rc, stderr_buf.getvalue(), created.get("forward_flags", [])

    def _run_cmd_spawn_direct(self, tmp: Path, ws: Path, claude_flags=None,
                              anthropic_model=None):
        """Drive ``cmd_spawn`` directly with pre-built ``claude_flags``.

        This bypasses ``parse_known_args`` so the exact tokens in
        ``claude_flags`` (including space-form ``["--effort", "high"]`` or
        ``["--model", "opus"]``) end up verbatim in ``forward_flags``, which
        is what the warning block inspects. Returns ``(rc, stderr_text)``.
        """
        live_names: set[str] = set()

        def fake_create(*, name, abs_dir, forward_flags, session_id, prompt):
            live_names.add(name)
            return 0, None

        stderr_buf = io.StringIO()

        # Build a Namespace with spawn defaults so cmd_spawn gets everything
        # it expects, then override just the dir.
        parser = cli.build_parser()
        ns, _ = parser.parse_known_args(["spawn", "--dir", str(ws)])

        with contextlib.ExitStack() as stack:
            stack.enter_context(_redirect_amux_home(tmp))
            stack.enter_context(
                patch.object(cli.lib, "resolve_amux_session", return_value=None)
            )
            stack.enter_context(
                patch.object(cli.lib, "list_amux_names", return_value=set())
            )
            stack.enter_context(
                patch.object(cli.lib, "tmux_has_session",
                             side_effect=lambda n: n in live_names)
            )
            stack.enter_context(
                patch.object(cli, "_amux_create_detached", side_effect=fake_create)
            )
            mock_stdin = stack.enter_context(patch("sys.stdin"))
            mock_stdout = stack.enter_context(patch("sys.stdout"))
            mock_stdin.isatty.return_value = False
            mock_stdout.isatty.return_value = False

            if anthropic_model is not None:
                stack.enter_context(
                    patch.dict(os.environ, {"ANTHROPIC_MODEL": anthropic_model}, clear=False)
                )
            else:
                stack.enter_context(patch.dict(os.environ, {}, clear=False))
                os.environ.pop("ANTHROPIC_MODEL", None)

            stack.enter_context(contextlib.redirect_stderr(stderr_buf))
            rc = cli.cmd_spawn(ns, list(claude_flags or []), "go")

        return rc, stderr_buf.getvalue()

    # ── cases ─────────────────────────────────────────────────────────────────

    def test_case1_nontty_no_flags_both_warnings(self):
        """Case 1: non-TTY, no flags → both model and effort warnings."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "proj"
            ws.mkdir()
            rc, stderr, _ = self._run_nontty(tmp, ws)
        self.assertEqual(rc, 0)
        self.assertIn("pins no model", stderr)
        self.assertIn("pins no effort", stderr)

    def test_case2_nontty_model_flag_only_effort_warning(self):
        """Case 2: non-TTY, forward_flags carries ["--model", "opus"] (space form).

        Uses _run_cmd_spawn_direct so the space-form tokens reach forward_flags
        verbatim — parse_known_args would otherwise consume "opus" as ns.suffix.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "proj"
            ws.mkdir()
            rc, stderr = self._run_cmd_spawn_direct(
                tmp, ws, claude_flags=["--model", "opus"]
            )
        self.assertEqual(rc, 0)
        self.assertNotIn("pins no model", stderr)
        self.assertIn("pins no effort", stderr)

    def test_case3_nontty_both_flags_no_warnings(self):
        """Case 3: non-TTY, --model=opus --effort=high → neither warning."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "proj"
            ws.mkdir()
            rc, stderr, _ = self._run_nontty(
                tmp, ws, extra_argv=["--model=opus", "--effort=high"]
            )
        self.assertEqual(rc, 0)
        self.assertNotIn("pins no model", stderr)
        self.assertNotIn("pins no effort", stderr)

    def test_case4_nontty_effort_spaceform_model_warning_only(self):
        """Case 4: non-TTY, forward_flags carries ["--effort", "high"] (space form).

        Uses _run_cmd_spawn_direct so the space-form tokens reach forward_flags
        verbatim — parse_known_args would otherwise consume "high" as ns.suffix.
        Verifies that extract_flag_value correctly detects the space-form pin.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "proj"
            ws.mkdir()
            rc, stderr = self._run_cmd_spawn_direct(
                tmp, ws, claude_flags=["--effort", "high"]
            )
        self.assertEqual(rc, 0)
        self.assertIn("pins no model", stderr)
        self.assertNotIn("pins no effort", stderr)

    def test_case5_tty_no_flags_no_warnings(self):
        """Case 5: TTY (both isatty True), no flags → neither warning.

        Must mock _amux_attach to prevent os.execvp from replacing the
        interpreter (amux is installed; the exec would succeed and the test
        process would vanish — the suite would never return).
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "proj"
            ws.mkdir()

            live_names: set[str] = set()
            stderr_buf = io.StringIO()

            def fake_create(*, name, abs_dir, forward_flags, session_id, prompt):
                live_names.add(name)
                return 0, None

            argv = ["spawn", "--dir", str(ws), "--", "go"]

            with _redirect_amux_home(tmp), \
                    patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                    patch.object(cli.lib, "list_amux_names", return_value=set()), \
                    patch.object(cli.lib, "tmux_has_session",
                                 side_effect=lambda n: n in live_names), \
                    patch.object(cli, "_amux_create_detached", side_effect=fake_create), \
                    patch.object(cli, "_amux_attach"), \
                    patch("sys.stdin") as mock_stdin, \
                    patch("sys.stdout") as mock_stdout, \
                    patch.dict(os.environ, {}, clear=False), \
                    contextlib.redirect_stderr(stderr_buf):
                os.environ.pop("ANTHROPIC_MODEL", None)
                mock_stdin.isatty.return_value = True
                mock_stdout.isatty.return_value = True
                rc = cli.main(argv)

        self.assertEqual(rc, 0)
        self.assertNotIn("pins no model", stderr_buf.getvalue())
        self.assertNotIn("pins no effort", stderr_buf.getvalue())

    def test_case6_nontty_inherited_model_effort_warning_only(self):
        """Case 6: non-TTY, no --model, parent CC_FLAGS carries --model opus.

        Regression guard for placement: the check runs AFTER the inherited-model
        block, so the inherited --model is in forward_flags and model_pinned is
        True. If the block were placed BEFORE inheritance, this test would fail
        (model warning would fire incorrectly).
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "proj"
            ws.mkdir()
            rc, stderr, fwd_flags = self._run_nontty(
                tmp, ws,
                parent_name="parent-x",
                parent_flags=["--model", "opus"],
            )
        self.assertEqual(rc, 0)
        # Inherited --model was found in forward_flags — no model warning.
        self.assertNotIn("pins no model", stderr)
        self.assertIn("pins no effort", stderr)
        # Confirm inheritance actually added --model to forward_flags.
        self.assertIn("--model", fwd_flags)
        self.assertIn("opus", fwd_flags)

    def test_case7_nontty_anthropic_model_env_effort_warning_only(self):
        """Case 7: non-TTY, no --model, ANTHROPIC_MODEL=glm-4.7 in env.

        ANTHROPIC_MODEL counts as a model pin even though it is an env var, not
        a flag. Effort is still unpinned, so the effort warning fires.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "proj"
            ws.mkdir()
            rc, stderr, _ = self._run_nontty(tmp, ws, anthropic_model="glm-4.7")
        self.assertEqual(rc, 0)
        self.assertNotIn("pins no model", stderr)
        self.assertIn("pins no effort", stderr)

    def test_case8_extract_flag_value_units(self):
        """Case 8: extract_flag_value — all four sub-cases."""
        # Space form.
        self.assertEqual(
            lib.extract_flag_value(["--effort", "high"], "--effort"), "high"
        )
        # Equals form.
        self.assertEqual(
            lib.extract_flag_value(["--effort=high"], "--effort"), "high"
        )
        # Absent flag → None.
        self.assertIsNone(lib.extract_flag_value(["--model", "opus"], "--effort"))
        # Trailing bare flag at end of list → None (not IndexError).
        self.assertIsNone(lib.extract_flag_value(["--effort"], "--effort"))

# ══ Task 20-02: provider-aware launcher ═══════════════════════════════════════
#
# Everything below is hermetic: ``lib``'s amux paths point at a tmp dir, tmux is
# mocked, and ``cli.subprocess.run`` is replaced by a recorder — so the argv the
# launcher WOULD hand to amux is asserted as a real argv list, never as a
# rendered string, and no ``amux`` / ``tmux`` / ``codex`` process is ever run.
# No live Codex turn happens anywhere in this file (no API credits, and
# --dangerously-bypass-approvals-and-sandbox is never spelled, let alone run).

import importlib.machinery  # noqa: E402  (already used by _load_cli above)

_reducer_spec = importlib.util.spec_from_file_location(
    "codex_event_reducer_t2002", _HOOKS / "codex_event_reducer.py"
)
reducer = importlib.util.module_from_spec(_reducer_spec)
_reducer_spec.loader.exec_module(reducer)

# A prompt with every metacharacter class the contract names: spaces, both quote
# kinds, $VAR, $(...) and backticks, a glob, a `;`, and a newline.
ADVERSARIAL_PROMPT = (
    "review 'this' \"thing\" $HOME $(touch {sentinel}) `id` *.py ; echo pwned\n"
    "second line\twith a tab"
)


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _spawn_capturing_argv(tmp: Path, argv: list[str], *, tty=False,
                          amux_rc=0, live=True):
    """Drive ``cli.main`` with amux replaced by an argv recorder.

    Returns ``(rc, calls, handle_files, attach_called)`` where ``calls`` is the
    list of real
    argv lists passed to ``subprocess.run`` inside the CLI.
    """
    calls: list[list[str]] = []
    live_names: set[str] = set()

    def fake_run(cmd, *_a, **_kw):
        calls.append(list(cmd))
        if cmd[:2] == ["amux", "exec"] and amux_rc == 0 and live:
            live_names.add(cmd[2])
        if cmd[:2] == ["amux", "rm"]:
            live_names.discard(cmd[2])
            return _FakeCompleted(0)
        return _FakeCompleted(amux_rc, stderr="" if amux_rc == 0 else "boom")

    with _redirect_amux_home(tmp), \
            patch.object(cli.lib, "resolve_amux_session", return_value=None), \
            patch.object(cli.lib, "list_amux_names", return_value=set()), \
            patch.object(cli.lib, "tmux_has_session",
                         side_effect=lambda n: n in live_names), \
            patch.object(cli.subprocess, "run", side_effect=fake_run), \
            patch.object(cli, "_amux_attach") as attach, \
            patch("sys.stdin") as stdin, patch("sys.stdout") as stdout, \
            patch("sys.stderr"):
        stdin.isatty.return_value = tty
        stdout.isatty.return_value = tty
        rc = cli.main(argv)
        handles = sorted(p.name for p in (tmp / "spawn").glob("*.json")) \
            if (tmp / "spawn").is_dir() else []
        attached = attach.called
    return rc, calls, handles, attached


def _amux_exec_call(calls):
    for c in calls:
        if c[:2] == ["amux", "exec"]:
            return c
    return None


class TestProviderValidation(unittest.TestCase):
    """Work item 1: validated ``--provider claude|codex``, default claude."""

    def test_default_is_claude(self):
        self.assertEqual(cli.resolve_provider(None), lib.PROVIDER_CLAUDE)
        self.assertEqual(cli.resolve_provider(""), lib.PROVIDER_CLAUDE)

    def test_case_and_whitespace_normalized(self):
        self.assertEqual(cli.resolve_provider("  CODEX "), lib.PROVIDER_CODEX)

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ValueError) as ctx:
            cli.resolve_provider("gemini")
        self.assertIn("claude, codex", str(ctx.exception))

    def test_cli_refuses_unknown_provider_without_creating_anything(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, calls, handles, _ = _spawn_capturing_argv(
                tmp, ["spawn", "--provider", "gemini", "--dir", str(ws), "--", "go"]
            )
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [])
            self.assertEqual(handles, [])

    def test_codex_without_prompt_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, calls, handles, _ = _spawn_capturing_argv(
                tmp, ["spawn", "--provider", "codex", "--dir", str(ws)]
            )
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [])
            self.assertEqual(handles, [])

    def test_codex_refuses_claude_profile_and_wait(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            # --profile remains refused (a Claude model profile means nothing
            # to Codex). --wait/--notify became SUPPORTED for Codex in task
            # 20-03 (exit-code contract pinned in test_unit_amux_codex_reads),
            # so they are no longer part of this refusal set.
            rc, calls, _h, _ = _spawn_capturing_argv(
                tmp, ["spawn", "--provider", "codex", "--dir", str(ws),
                      "--profile", "glm", "--", "go"]
            )
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [])

    def test_codex_refuses_claude_permission_flag(self):
        """Architecture §8: Claude's permission flag must never reach Codex."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, calls, _h, _ = _spawn_capturing_argv(
                tmp, ["spawn", "--provider", "codex", "--dir", str(ws),
                      "--dangerously-skip-permissions", "--", "go"]
            )
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [])


class TestAmuxArgvBothProviders(unittest.TestCase):
    """Work item 2 / Done-when 1: the EXACT amux argv, for both providers.

    The Codex form uses only amux's public options, documented in the pinned
    revision's ``docs/codex-provider.md`` §§2–3: ``--provider``,
    ``--agent-mode``, ``--event-log``, ``--output-last-message``. No ``codex``
    argv is built here (invariant 3).
    """

    def test_claude_argv_is_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, calls, handles, _ = _spawn_capturing_argv(
                tmp, ["spawn", "--dir", str(ws), "--", "hello world"]
            )
            self.assertEqual(rc, 0)
            call = _amux_exec_call(calls)
            handle = None
            with _redirect_amux_home(tmp):
                handle = lib.read_handle("myproj")
            sid = handle["session_id"]
            self.assertEqual(call, [
                "amux", "exec", "myproj", "--no-attach", "--no-default-model",
                "--dir", str(ws), "--session-id", sid, "hello world",
            ])
            # No Codex option leaks onto the Claude path.
            for leaked in ("--provider", "--agent-mode", "--event-log",
                           "--output-last-message"):
                self.assertNotIn(leaked, call)

    def test_claude_yolo_argv_forwards_the_neutral_alias(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, calls, _h, _ = _spawn_capturing_argv(
                tmp, ["spawn", "--yolo", "--dir", str(ws), "--", "go"]
            )
            self.assertEqual(rc, 0)
            call = _amux_exec_call(calls)
            self.assertIn("--yolo", call)
            self.assertNotIn("--dangerously-skip-permissions", call)

    def test_codex_argv_uses_only_public_amux_options(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, calls, handles, _ = _spawn_capturing_argv(
                tmp, ["spawn", "--provider", "codex", "--yolo",
                      "--dir", str(ws), "--", "review the diff"]
            )
            self.assertEqual(rc, 0)
            events = str(tmp / "spawn" / "myproj.events.jsonl")
            result = str(tmp / "spawn" / "myproj.result.md")
            self.assertEqual(_amux_exec_call(calls), [
                "amux", "exec", "myproj", "--no-attach",
                "--provider", "codex", "--agent-mode", "exec",
                "--dir", str(ws),
                "--event-log", events,
                "--output-last-message", result,
                "--yolo",
                "--", "review the diff",
            ])

    def test_codex_argv_never_contains_a_provider_command_or_yolo_expansion(self):
        """Ownership boundary: amux-spawn builds no `codex …` argv at all.

        The only command constructed is ``amux`` itself. "codex"/"exec" appear
        ONLY as the values of amux's own ``--provider``/``--agent-mode`` options
        (checked positionally); no Codex subcommand, contract option or YOLO
        expansion is ever spelled here.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            _rc, calls, _h, _ = _spawn_capturing_argv(
                tmp, ["spawn", "--provider", "codex", "--yolo",
                      "--dir", str(ws), "--", "go"]
            )
            call = _amux_exec_call(calls)
            # The one and only executable is amux; everything else is options.
            self.assertEqual(call[0], "amux")
            self.assertEqual(call[1], "exec")      # amux's own subcommand
            self.assertEqual(call[2], "myproj")
            # "codex"/"exec" only as VALUES of amux's public provider options
            # (amux's own subcommand "exec" at index 1 aside).
            codex_positions = [i for i, tok in enumerate(call) if tok == "codex"]
            self.assertEqual(codex_positions, [call.index("--provider") + 1])
            exec_positions = [i for i, tok in enumerate(call[2:], start=2)
                              if tok == "exec"]
            self.assertEqual(exec_positions, [call.index("--agent-mode") + 1])
            # No Codex contract option, YOLO expansion, or Claude-only option.
            for forbidden in ("--json", "-o", "-C", "resume", "--cd",
                              "--dangerously-bypass-approvals-and-sandbox",
                              "--dangerously-skip-permissions",
                              "--no-default-model", "--session-id"):
                self.assertNotIn(forbidden, call[3:], forbidden)

    def test_codex_forwards_explicit_model_but_inherits_none(self):
        """BRD §3: no hardcoded model; a Claude parent's tier is not inherited."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                (lib.AMUX_SESSIONS_DIR / "parent.env").write_text(
                    f'CC_NAME="parent"\nCC_DIR="{ws}"\nCC_FLAGS="--model opus"\n'
                )
            calls_seen = {}
            live_names: set[str] = set()

            def fake_run(cmd, *_a, **_kw):
                calls_seen.setdefault("cmd", list(cmd))
                if cmd[:2] == ["amux", "exec"]:
                    live_names.add(cmd[2])
                return _FakeCompleted(0)

            with _redirect_amux_home(tmp), \
                    patch.object(cli.lib, "resolve_amux_session", return_value="parent"), \
                    patch.object(cli.lib, "list_amux_names", return_value=set()), \
                    patch.object(cli.lib, "tmux_has_session",
                                 side_effect=lambda n: n in live_names), \
                    patch.object(cli.subprocess, "run", side_effect=fake_run), \
                    patch("sys.stdin") as stdin, patch("sys.stdout") as stdout, \
                    patch("sys.stderr"):
                stdin.isatty.return_value = False
                stdout.isatty.return_value = False
                rc = cli.main(["spawn", "--provider", "codex", "--", "go"])
            self.assertEqual(rc, 0)
            self.assertNotIn("--model", calls_seen["cmd"])
            self.assertNotIn("opus", calls_seen["cmd"])

    def test_explicit_codex_model_is_forwarded_verbatim(self):
        """An explicit model rides along untouched (BRD §3: none is injected).

        Value-taking provider flags must use the ``--flag=value`` form: the
        space form is eaten by this CLI's optional positional ``suffix``
        (pre-existing epic-10 behavior, identical on the Claude path — the bare
        flag amux then receives makes amux die with a usage error).
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            _rc, calls, _h, _ = _spawn_capturing_argv(
                tmp, ["spawn", "--provider", "codex", "--dir", str(ws),
                      "--model=gpt-5.5", "--", "go"]
            )
            call = _amux_exec_call(calls)
            self.assertIn("--model=gpt-5.5", call)   # one argv element, verbatim
            i = call.index("--")
            self.assertEqual(call[i + 1], "go")      # the prompt is still last

    def test_space_form_model_keeps_the_pre_existing_cli_behavior(self):
        """The ``--model X`` space form behaves exactly as it does for Claude
        today: argparse eats the value as the suffix positional and amux (not
        amux-spawn) rejects the bare flag. Nothing is orphaned."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, calls, handles, _ = _spawn_capturing_argv(
                tmp, ["spawn", "--provider", "codex", "--dir", str(ws),
                      "--model", "gpt-5.5", "--", "go"], amux_rc=1
            )
            self.assertEqual(rc, 1)
            call = _amux_exec_call(calls)
            self.assertEqual(call[2], "myproj-gpt-5.5")   # eaten as the suffix
            self.assertIn("--model", call)
            self.assertNotIn("gpt-5.5", call)
            self.assertEqual(handles, [])                 # rolled back


class TestMultilinePromptBoundary(unittest.TestCase):
    """Work item 6 / verification: one argv element, nothing executed."""

    def _assert_prompt_intact(self, provider_argv, tmp, ws, expect_separator):
        sentinel = tmp / "PWNED"
        prompt = ADVERSARIAL_PROMPT.format(sentinel=sentinel)
        rc, calls, _h, _ = _spawn_capturing_argv(
            tmp, ["spawn", *provider_argv, "--dir", str(ws), "--", prompt]
        )
        self.assertEqual(rc, 0)
        call = _amux_exec_call(calls)
        # The prompt is ONE real argv element (asserted on the argv list, not on
        # any rendered string), and it is the last one.
        self.assertEqual(call[-1], prompt)
        self.assertEqual(call.count(prompt), 1)
        if expect_separator:
            self.assertEqual(call[-2], "--")
        # Nothing in the prompt was evaluated by any shell.
        self.assertFalse(sentinel.exists(), "command substitution executed!")
        self.assertIn("\n", call[-1])
        self.assertIn("$HOME", call[-1])
        self.assertIn("`id`", call[-1])

    def test_claude_prompt_survives(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            self._assert_prompt_intact([], tmp, ws, expect_separator=False)

    def test_codex_prompt_survives_behind_the_separator(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            self._assert_prompt_intact(
                ["--provider", "codex"], tmp, ws, expect_separator=True
            )

    def test_split_prompt_preserves_a_newline(self):
        before, prompt = cli._split_prompt(["--", "a\nb"])
        self.assertEqual(before, [])
        self.assertEqual(prompt, "a\nb")


class TestCodexArtifactAllocation(unittest.TestCase):
    """Work item 3: collision-safe, mode-0600 artifacts under ~/.amux/spawn."""

    def test_paths_are_keyed_by_session_name_and_created_0600(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                ev, res = lib.allocate_codex_artifacts("proj-2")
                self.assertEqual(Path(ev).parent, tmp / "spawn")
                self.assertEqual(Path(ev).name, "proj-2.events.jsonl")
                self.assertEqual(Path(res).name, "proj-2.result.md")
                for p in (ev, res):
                    self.assertTrue(os.path.exists(p))
                    self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)

    def test_umask_cannot_loosen_the_mode(self):
        old = os.umask(0)
        try:
            with tempfile.TemporaryDirectory() as d:
                tmp = Path(d)
                with _redirect_amux_home(tmp):
                    lib.ensure_dirs()
                    ev, res = lib.allocate_codex_artifacts("wide")
                    for p in (ev, res):
                        self.assertEqual(os.stat(p).st_mode & 0o777, 0o600)
        finally:
            os.umask(old)

    def test_allocation_is_exclusive_and_never_reuses_a_stem(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                first_ev, first_res = lib.allocate_codex_artifacts("p")
                second_ev, second_res = lib.allocate_codex_artifacts("p")
                self.assertNotEqual(first_ev, second_ev)
                self.assertNotEqual(first_res, second_res)
                self.assertEqual(Path(second_ev).name, "p.2.events.jsonl")

    def test_a_stale_amux_sibling_also_forces_a_new_stem(self):
        """amux APPENDS to the event log — mixing two runs must be impossible."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                # Only the .rc sibling survives from an earlier reaped run.
                (tmp / "spawn" / "p.events.jsonl.rc").write_text("0")
                ev, _res = lib.allocate_codex_artifacts("p")
                self.assertEqual(Path(ev).name, "p.2.events.jsonl")

    def test_artifacts_do_not_collide_with_the_handle_glob(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                lib.allocate_codex_artifacts("p")
                self.assertEqual(list((tmp / "spawn").glob("*.json")), [])

    def test_remove_reaps_every_amux_owned_sibling(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            with _redirect_amux_home(tmp):
                lib.ensure_dirs()
                ev, res = lib.allocate_codex_artifacts("p")
                Path(ev + ".err").write_text("stderr")
                Path(ev + ".rc").write_text("0")
                lib.remove_codex_artifacts(ev, res)
                self.assertEqual(sorted(p.name for p in (tmp / "spawn").iterdir()
                                        if p.name != ".lock"), [])


class TestCodexHandle(unittest.TestCase):
    """Work item 4 + Done-when 3/4: the provider-aware handle, no fake identity."""

    def _spawn_codex(self, tmp: Path, ws: Path, extra=()):
        rc, calls, _h, attached = _spawn_capturing_argv(
            tmp, ["spawn", "--provider", "codex", "--dir", str(ws), *extra,
                  "--", "do the thing"]
        )
        with _redirect_amux_home(tmp):
            handle = lib.read_handle("myproj")
        return rc, calls, handle, attached

    def test_handle_records_provider_and_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, _calls, h, _ = self._spawn_codex(tmp, ws)
            self.assertEqual(rc, 0)
            self.assertEqual(set(h.keys()), set(lib.HANDLE_FIELDS))
            self.assertEqual(h["provider"], "codex")
            self.assertTrue(lib.is_codex_handle(h))
            self.assertEqual(h["activity_path"],
                             str(tmp / "spawn" / "myproj.events.jsonl"))
            self.assertEqual(h["result_path"],
                             str(tmp / "spawn" / "myproj.result.md"))
            self.assertEqual(h["state"], "spawning")
            self.assertEqual(h["exit_code"], None)
            self.assertEqual(h["failure"], None)
            self.assertEqual(lib.handle_activity_path(h), h["activity_path"])

    def test_no_transcript_guess_and_no_minted_thread_id(self):
        """Done-when 3: no Claude transcript guess, no fake Codex UUID."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, calls, h, _ = self._spawn_codex(tmp, ws)
            self.assertEqual(rc, 0)
            self.assertIsNone(h["session_id"])
            self.assertIsNone(h["transcript_path"])
            self.assertNotIn("--session-id", _amux_exec_call(calls))
            # run_id is still minted: it is OUR workflow id, not an agent identity.
            self.assertEqual(str(uuid.UUID(h["run_id"])), h["run_id"])

    def test_codex_is_always_tracked_and_detached_even_at_a_tty(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, _calls, handles, attached = _spawn_capturing_argv(
                tmp, ["spawn", "--provider", "codex", "--dir", str(ws), "--", "go"],
                tty=True,
            )
            self.assertEqual(rc, 0)
            self.assertEqual(handles, ["myproj.json"])
            self.assertFalse(attached, "a bounded codex run has no TUI to attach")

    def test_a_finished_bounded_run_is_not_a_failed_launch(self):
        """Architecture §5: completion evidence beats liveness at confirm time."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()

            def fake_run(cmd, *_a, **_kw):
                # amux "starts" the run, which finishes (and drops its tmux
                # session) before the launcher's confirmation check.
                if cmd[:2] == ["amux", "exec"]:
                    i = cmd.index("--event-log")
                    Path(cmd[i + 1] + ".rc").write_text("0")
                return _FakeCompleted(0)

            with _redirect_amux_home(tmp), \
                    patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                    patch.object(cli.lib, "list_amux_names", return_value=set()), \
                    patch.object(cli.lib, "tmux_has_session", return_value=False), \
                    patch.object(cli.subprocess, "run", side_effect=fake_run), \
                    patch("sys.stdin") as stdin, patch("sys.stdout") as stdout, \
                    patch("sys.stderr"):
                stdin.isatty.return_value = False
                stdout.isatty.return_value = False
                rc = cli.main(["spawn", "--provider", "codex", "--dir", str(ws),
                               "--", "go"])
                handle = lib.read_handle("myproj")
            self.assertEqual(rc, 0)
            self.assertIsNotNone(handle)

    def test_claude_liveness_failure_behavior_is_unchanged(self):
        """A dead Claude session is still a failed launch (epic-10 behavior)."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, _calls, handles, _ = _spawn_capturing_argv(
                tmp, ["spawn", "--dir", str(ws), "--", "go"], live=False
            )
            self.assertEqual(rc, 1)
            self.assertEqual(handles, [])

    def test_stub_event_stream_populates_the_thread_id_after_launch(self):
        """Done-when 4: the thread id arrives from the stream, not from us."""
        thread = "01a00000-0000-7000-8000-0000000000ff"
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, _calls, h, _ = self._spawn_codex(tmp, ws)
            self.assertEqual(rc, 0)
            self.assertIsNone(h["session_id"])

            # A stub `codex exec --json` segment lands in the artifact the
            # launcher allocated (this is what amux's pane wrapper writes).
            with open(h["activity_path"], "a") as f:
                for event in (
                    {"type": "thread.started", "thread_id": thread},
                    {"type": "turn.started"},
                    {"type": "item.completed",
                     "item": {"id": "item_0", "type": "agent_message",
                              "text": "done"}},
                    {"type": "turn.completed", "usage": {"input_tokens": 1}},
                ):
                    f.write(json.dumps(event) + "\n")
            Path(h["result_path"]).write_text("done")

            reduction = reducer.reduce_handle(h, read_result=True)
            self.assertEqual(reduction["thread_id"], thread)
            self.assertTrue(reduction["turn_completed"])
            self.assertEqual(reduction["state_hint"], reducer.STATE_IDLE)
            self.assertEqual(reduction["result_text"], "done")


class TestCodexLaunchRollback(unittest.TestCase):
    """Work item 7: a partial launch leaves no handle, artifact, or session."""

    def _failing_spawn(self, tmp: Path, ws: Path, *, amux_rc=0, live=True):
        rm_calls: list[str] = []
        live_names: set[str] = set()

        def fake_run(cmd, *_a, **_kw):
            if cmd[:2] == ["amux", "exec"] and amux_rc == 0 and live:
                live_names.add(cmd[2])
            return _FakeCompleted(amux_rc, stderr="boom" if amux_rc else "")

        def fake_rm(name):
            rm_calls.append(name)
            live_names.discard(name)

        with _redirect_amux_home(tmp), \
                patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                patch.object(cli.lib, "list_amux_names", return_value=set()), \
                patch.object(cli.lib, "tmux_has_session",
                             side_effect=lambda n: n in live_names), \
                patch.object(cli.subprocess, "run", side_effect=fake_run), \
                patch.object(cli, "_amux_rm", side_effect=fake_rm), \
                patch("sys.stdin") as stdin, patch("sys.stdout") as stdout, \
                patch("sys.stderr"):
            stdin.isatty.return_value = False
            stdout.isatty.return_value = False
            rc = cli.main(["spawn", "--provider", "codex", "--dir", str(ws),
                           "--", "go"])
            leftovers = sorted(p.name for p in (tmp / "spawn").iterdir()
                               if p.name != ".lock")
        return rc, rm_calls, leftovers, live_names

    def test_amux_create_failure_rolls_everything_back(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, rm_calls, leftovers, live = self._failing_spawn(
                tmp, ws, amux_rc=1
            )
            self.assertEqual(rc, 1)
            self.assertEqual(rm_calls, ["myproj"])   # session torn down
            self.assertEqual(leftovers, [])          # no handle, no artifacts
            self.assertEqual(live, set())

    def test_launch_not_confirmed_rolls_everything_back(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rc, rm_calls, leftovers, live = self._failing_spawn(
                tmp, ws, amux_rc=0, live=False
            )
            self.assertEqual(rc, 1)
            self.assertEqual(rm_calls, ["myproj"])
            self.assertEqual(leftovers, [])
            self.assertEqual(live, set())

    def test_handle_write_failure_rolls_everything_back(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            rm_calls: list[str] = []
            live_names: set[str] = set()

            def fake_run(cmd, *_a, **_kw):
                if cmd[:2] == ["amux", "exec"]:
                    live_names.add(cmd[2])
                return _FakeCompleted(0)

            def fake_rm(name):
                rm_calls.append(name)
                live_names.discard(name)

            with _redirect_amux_home(tmp), \
                    patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                    patch.object(cli.lib, "list_amux_names", return_value=set()), \
                    patch.object(cli.lib, "tmux_has_session",
                                 side_effect=lambda n: n in live_names), \
                    patch.object(cli.subprocess, "run", side_effect=fake_run), \
                    patch.object(cli, "_amux_rm", side_effect=fake_rm), \
                    patch.object(cli.lib, "write_handle",
                                 side_effect=OSError("disk full")), \
                    patch("sys.stdin") as stdin, patch("sys.stdout") as stdout, \
                    patch("sys.stderr"):
                stdin.isatty.return_value = False
                stdout.isatty.return_value = False
                rc = cli.main(["spawn", "--provider", "codex", "--dir", str(ws),
                               "--", "go"])
                leftovers = sorted(p.name for p in (tmp / "spawn").iterdir()
                                   if p.name != ".lock")
            self.assertEqual(rc, 1)
            self.assertEqual(rm_calls, ["myproj"])
            self.assertEqual(leftovers, [])
            self.assertEqual(live_names, set())

    def test_rollback_never_touches_a_claude_launch(self):
        """Backward compatibility: the Claude failure path is byte-for-byte."""
        rm_calls: list[str] = []
        with patch.object(cli, "_amux_rm", side_effect=rm_calls.append):
            cli._rollback_launch(name="x", provider=lib.PROVIDER_CLAUDE,
                                 event_log=None, result_path=None,
                                 handle_written=True)
        self.assertEqual(rm_calls, [])


class TestCodexCapAndNaming(unittest.TestCase):
    """Done-when 5: naming + cap stay green with a provider in the mix."""

    def test_codex_spawn_counts_against_the_workspace_cap(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            with patch.dict(os.environ, {"AMUX_SPAWN_MAX_SESSIONS": "1"}):
                with _redirect_amux_home(tmp):
                    lib.ensure_dirs()
                    lib.write_handle("x", lib.new_handle(
                        name="x", session_id="s", run_id="r", abs_dir=str(ws),
                        transcript_path="/t", stuck_after_s=1,
                    ))
                with _redirect_amux_home(tmp), \
                        patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                        patch.object(cli.lib, "tmux_has_session", return_value=True), \
                        patch.object(cli.subprocess, "run") as run, \
                        patch("sys.stdin") as stdin, patch("sys.stdout") as stdout, \
                        patch("sys.stderr"):
                    stdin.isatty.return_value = False
                    stdout.isatty.return_value = False
                    rc = cli.main(["spawn", "--provider", "codex",
                                   "--dir", str(ws), "--", "go"])
                    self.assertEqual(rc, 1)
                    run.assert_not_called()
                    # The cap refusal allocated no artifacts.
                    self.assertEqual(
                        sorted(p.name for p in (tmp / "spawn").glob("*.events.jsonl")),
                        [])

    def test_second_codex_spawn_gets_its_own_name_and_artifacts(self):
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            names = []
            for i in range(2):
                rc, calls, _h, _ = _spawn_capturing_argv(
                    tmp, ["spawn", "--provider", "codex", "--dir", str(ws),
                          "--", "go"]
                )
                self.assertEqual(rc, 0)
                names.append(_amux_exec_call(calls)[2])
                # Real `amux exec` registers the session: <name>.env now exists,
                # so the next pick_free_name must move on to <name>-2.
                with _redirect_amux_home(tmp):
                    (lib.AMUX_SESSIONS_DIR / names[-1]).with_suffix(
                        ".env"
                    ).write_text(f'CC_NAME="{names[-1]}"\n')
            self.assertEqual(names, ["myproj", "myproj-2"])
            with _redirect_amux_home(tmp):
                a = lib.read_handle("myproj")
                b = lib.read_handle("myproj-2")
            self.assertNotEqual(a["activity_path"], b["activity_path"])
            self.assertNotEqual(a["result_path"], b["result_path"])


@unittest.skipUnless(
    os.environ.get("AMUX_SPAWN_LIVE_TEST") == "1",
    "live spawn test (needs tmux + model auth); set AMUX_SPAWN_LIVE_TEST=1",
)
class TestLiveSpawn(unittest.TestCase):
    """End-to-end headless spawn against the real amux + claude."""

    def test_headless_spawn(self):
        import subprocess
        import time

        workdir = tempfile.mkdtemp(prefix="amux-spawn-live-")
        name = Path(workdir).name
        try:
            proc = subprocess.run(
                [str(_BIN), "spawn", "--dir", workdir,
                 "--stuck-after", "5m", "--",
                 "Reply with exactly the word PONG and nothing else."],
                stdin=subprocess.DEVNULL,  # force non-TTY (agent path)
                capture_output=True, text=True, timeout=120,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)

            handle = lib.read_handle(name)
            self.assertIsNotNone(handle, "handle JSON should be written")
            sid = handle["session_id"]
            self.assertEqual(str(uuid.UUID(sid)), sid)
            self.assertTrue(lib.tmux_has_session(name), "tmux session should be live")

            # The seeded prompt should produce a transcript turn within a bit.
            tpath = handle["transcript_path"]
            deadline = time.time() + 90
            saw_turn = False
            while time.time() < deadline:
                if os.path.exists(tpath) and os.path.getsize(tpath) > 0:
                    saw_turn = True
                    break
                time.sleep(2)
            self.assertTrue(saw_turn, f"expected a transcript turn at {tpath}")
        finally:
            subprocess.run(["amux", "rm", name], capture_output=True)


# ── Task 37-04: transcript persistence default ───────────────────────────────


class TestPersistenceDefault(unittest.TestCase):
    """Tracked Claude spawns default to CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1.

    BRD §4.4 / task 37-04: CLAUDE_CODE_CHILD_SESSION is ambient and inherited
    down the process tree.  On the driving run it silently disabled persistence
    for 26 worker sessions (evidence.md §3).  After 37-02, persistence is an
    observability choice, and the default for tracked workers is ON.

    The default must take effect for a plain ``amux-spawn spawn`` with no
    ``--profile`` (the [all-profiles] path does NOT apply without a named profile).
    """

    def _run_spawn_capture_env(self, tmp: Path, ws: Path, extra_argv=None,
                               extra_env=None):
        """Run cmd_spawn (non-TTY/tracked) and capture env at create time."""
        captured_env: dict = {}
        live_names: set[str] = set()

        def fake_create(*, name, abs_dir, forward_flags, session_id, prompt):
            # Capture os.environ snapshot at the instant of amux create.
            captured_env.update(os.environ.copy())
            live_names.add(name)
            return 0, None

        argv = ["spawn", "--dir", str(ws)] + (extra_argv or []) + ["--", "go"]
        env_patch = extra_env or {}
        with _redirect_amux_home(tmp), \
                patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                patch.object(cli.lib, "list_amux_names", return_value=set()), \
                patch.object(cli.lib, "tmux_has_session",
                             side_effect=lambda n: n in live_names), \
                patch.object(cli, "_amux_create_detached", side_effect=fake_create), \
                patch.dict(os.environ, env_patch), \
                patch("sys.stdin") as stdin, patch("sys.stdout") as stdout, \
                patch("sys.stderr"):
            stdin.isatty.return_value = False
            stdout.isatty.return_value = False
            rc = cli.main(argv)
        return rc, captured_env

    def test_plain_spawn_sets_persistence_to_1(self):
        """A plain tracked spawn with no --profile sets persistence to '1'."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            # Remove the variable if present — simulates a clean environment.
            clean_env = {"CLAUDE_CODE_FORCE_SESSION_PERSISTENCE": ""}
            rc, env = self._run_spawn_capture_env(tmp, ws, extra_env=clean_env)
            self.assertEqual(rc, 0)
            self.assertEqual(
                env.get("CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"), "1",
                "tracked Claude spawn must set CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1",
            )

    def test_persistence_default_takes_effect_without_profile(self):
        """The default applies for a plain spawn — [all-profiles] is NOT sufficient
        because it only applies when --profile is passed.  This test ensures the
        default is unconditional for tracked Claude workers.
        """
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            # Explicitly clear the var so this test doesn't silently pass because
            # the var was already set in the outer environment.
            clean_env = {"CLAUDE_CODE_FORCE_SESSION_PERSISTENCE": ""}
            rc, env = self._run_spawn_capture_env(tmp, ws, extra_env=clean_env)
            self.assertEqual(rc, 0)
            self.assertEqual(
                env.get("CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"), "1",
                "default persistence must apply for plain tracked spawn with no --profile",
            )

    def test_explicit_zero_suppresses_default(self):
        """A caller that sets CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=0 opts out."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            # Caller explicitly opts out.
            rc, env = self._run_spawn_capture_env(
                tmp, ws,
                extra_env={"CLAUDE_CODE_FORCE_SESSION_PERSISTENCE": "0"},
            )
            self.assertEqual(rc, 0)
            self.assertEqual(
                env.get("CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"), "0",
                "explicit CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=0 must not be overridden",
            )

    def test_codex_spawn_does_not_set_persistence_var(self):
        """Codex spawns are unaffected — CLAUDE_* vars have no meaning for Codex."""
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            ws = tmp / "myproj"
            ws.mkdir()
            # Pre-remove the var to see if the Codex path would incorrectly set it.
            codex_env: dict = {}
            live_names: set[str] = set()

            def fake_create_codex(*, name, abs_dir, forward_flags, session_id,
                                  prompt, provider, event_log, result_path):
                codex_env.update(os.environ.copy())
                live_names.add(name)
                return 0, None

            # Seed an amux sessions dir for Codex artifact allocation.
            argv = ["spawn", "--provider", "codex", "--dir", str(ws),
                    "--", "do the thing"]
            with _redirect_amux_home(tmp), \
                    patch.object(cli.lib, "resolve_amux_session", return_value=None), \
                    patch.object(cli.lib, "list_amux_names", return_value=set()), \
                    patch.object(cli.lib, "tmux_has_session",
                                 side_effect=lambda n: n in live_names), \
                    patch.object(cli, "_amux_create_detached",
                                 side_effect=fake_create_codex), \
                    patch.object(cli.lib, "allocate_codex_artifacts",
                                 return_value=("/tmp/test.events.jsonl",
                                               "/tmp/test.result.md")), \
                    patch.dict(os.environ, {"CLAUDE_CODE_FORCE_SESSION_PERSISTENCE": ""},
                               clear=False), \
                    patch("sys.stdin") as stdin, patch("sys.stdout") as stdout, \
                    patch("sys.stderr"):
                stdin.isatty.return_value = False
                stdout.isatty.return_value = False
                rc = cli.main(argv)
            # Codex spawn must not set the Claude persistence var to "1".
            self.assertNotEqual(
                codex_env.get("CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"), "1",
                "Codex spawn must not set CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1",
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
