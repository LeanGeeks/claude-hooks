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

import importlib.util
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
