#!/usr/bin/env python3
"""
Unit tests for the minimum-Python contract.

The hooks are invoked by Claude Code as bare ``python3``, so they run on
whatever interpreter is first on the user's PATH — not on a venv we control.
A module that fails to *import* there takes the whole hook down (including the
parts that never touch the failing dependency), and the user sees a raw
traceback in their session. That happened in the field: ``import tomllib`` at
the top of ``amux_spawn_lib`` killed the Stop hook on a pre-3.11 interpreter.

The floor is ``MIN_PYTHON_MINOR`` in install.sh, which these tests read so the
gate and the checks can never drift apart. Two hazards are covered:

1. Syntax newer than the floor (``ast.parse(feature_version=...)``).
2. Constructs that parse everywhere but are *evaluated* at import time and
   raise on older interpreters — PEP 604 ``X | Y`` annotations (3.10+) and
   ``import tomllib`` (3.11+).

Both are checked statically, so this module needs no old interpreter present.
If one happens to be installed, the last test additionally imports every hook
module under it for real.

Run this module alone:
    python3 tests/run_all_tests.py --module unit_python_compat
"""

import ast
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from test_unit_installer import run_installer

REPO = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO / ".claude" / "hooks"
INSTALL_SH = REPO / "install.sh"

# relay_server modules reachable from a hook: telegram_permission_router and
# questions_listen_lib put relay-server/ on sys.path and import client.py, so
# these run on the same bare python3 as the hooks even though the relay-server
# *package* declares requires-python >= 3.11 for the server half.
RELAY_CLIENT_MODULES = [
    REPO / "relay-server" / "relay_server" / "client.py",
    REPO / "relay-server" / "relay_server" / "config.py",
    REPO / "relay-server" / "relay_server" / "client_cli.py",
]

_IMPORT_ERRORS = {"ImportError", "ModuleNotFoundError"}


def min_python() -> tuple:
    """Read the supported floor out of install.sh, so there is one source of truth."""
    m = re.search(r"^MIN_PYTHON_MINOR=(\d+)$", INSTALL_SH.read_text(), re.MULTILINE)
    assert m, "MIN_PYTHON_MINOR not found in install.sh — the version gate is missing"
    return (3, int(m.group(1)))


def hook_modules() -> list:
    return sorted(HOOKS_DIR.glob("*.py"))


def _guarded_import_nodes(tree: ast.AST) -> set:
    """ids of Import nodes sitting inside a try/except that catches ImportError."""
    guarded = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        catches_import = any(
            handler.type is None
            or (isinstance(handler.type, ast.Name) and handler.type.id in _IMPORT_ERRORS)
            or (
                isinstance(handler.type, ast.Tuple)
                and any(
                    isinstance(e, ast.Name) and e.id in _IMPORT_ERRORS
                    for e in handler.type.elts
                )
            )
            for handler in node.handlers
        )
        if catches_import:
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    guarded.add(id(sub))
    return guarded


def _has_union_annotation(tree: ast.AST) -> list:
    """Line numbers of PEP 604 ``X | Y`` unions used in an annotation."""
    def is_union(node) -> bool:
        return any(
            isinstance(sub, ast.BinOp) and isinstance(sub.op, ast.BitOr)
            for sub in ast.walk(node)
        )

    hits = []
    for node in ast.walk(tree):
        annotations = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                        args.vararg, args.kwarg]:
                if arg is not None and arg.annotation is not None:
                    annotations.append(arg.annotation)
            if node.returns is not None:
                annotations.append(node.returns)
        elif isinstance(node, ast.AnnAssign) and node.annotation is not None:
            annotations.append(node.annotation)
        hits.extend(a.lineno for a in annotations if is_union(a))
    return sorted(hits)


def _has_future_annotations(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(a.name == "annotations" for a in node.names)
        for node in ast.walk(tree)
    )


def _find_old_interpreter():
    """An interpreter older than 3.11 that has a TOML parser, or None.

    Set CLAUDE_HOOKS_OLD_PYTHON to point at one explicitly — distro Pythons are
    usually PEP 668 "externally managed", so the tomli backport tends to live in
    a venv this search cannot guess.
    """
    candidates = []
    explicit = os.environ.get("CLAUDE_HOOKS_OLD_PYTHON")
    if explicit:
        candidates.append(explicit)
    for name in ("python3.9", "python3.10"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if shutil.which("uv"):
        for version in ("3.9", "3.10"):
            try:
                out = subprocess.run(
                    ["uv", "python", "find", version],
                    capture_output=True, text=True, timeout=30,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if out.returncode == 0 and out.stdout.strip():
                candidates.append(out.stdout.strip())

    probe = "import importlib.util as u, sys; sys.exit(0 if u.find_spec('tomli') else 1)"
    for path in candidates:
        try:
            probed = subprocess.run(
                [path, "-c", probe], capture_output=True, timeout=30,
            )
            if probed.returncode == 0:
                return path
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _find_interpreter_without_toml():
    """A pre-3.11 interpreter with neither tomllib nor tomli, or None.

    Set CLAUDE_HOOKS_NO_TOML_PYTHON to point at one explicitly. A uv-managed
    3.9/3.10 is the easy source: bare, so it has no backport installed.
    """
    candidates = []
    explicit = os.environ.get("CLAUDE_HOOKS_NO_TOML_PYTHON")
    if explicit:
        candidates.append(explicit)
    for name in ("python3.9", "python3.10"):
        found = shutil.which(name)
        if found:
            candidates.append(found)
    if shutil.which("uv"):
        for version in ("3.9", "3.10"):
            try:
                out = subprocess.run(
                    ["uv", "python", "find", version],
                    capture_output=True, text=True, timeout=60,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if out.returncode == 0 and out.stdout.strip():
                candidates.append(out.stdout.strip())

    probe = (
        "import importlib.util as u, sys;"
        "sys.exit(0 if not (u.find_spec('tomllib') or u.find_spec('tomli')) else 1)"
    )
    for path in candidates:
        try:
            probed = subprocess.run([path, "-c", probe], capture_output=True, timeout=60)
            if probed.returncode == 0:
                return path
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def _path_with_python3(interpreter: str, bin_dir: Path) -> dict:
    """PATH env override that makes bare `python3` resolve to `interpreter`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "python3"
    if shim.exists() or shim.is_symlink():
        shim.unlink()
    shim.symlink_to(interpreter)
    return {"PATH": f"{bin_dir}:{os.environ.get('PATH', '')}"}


# =============================================================================
# The installer gate
# =============================================================================

class TestInstallerVersionGate(unittest.TestCase):
    """install.sh must refuse an interpreter the hooks cannot run on."""

    def setUp(self):
        self.src = INSTALL_SH.read_text()

    def _function_body(self, name: str) -> str:
        """The body of a top-level shell function, comment lines stripped."""
        m = re.search(r"\n%s\(\) \{\n(.*?)\n\}\n" % re.escape(name),
                      self.src, re.DOTALL)
        self.assertIsNotNone(m, f"{name} not found in install.sh")
        return "\n".join(
            line for line in m.group(1).splitlines()
            if not line.lstrip().startswith("#")
        )

    def test_min_python_is_declared(self):
        self.assertRegex(
            self.src,
            re.compile(r"^MIN_PYTHON_MINOR=\d+$", re.MULTILINE),
            "install.sh must declare MIN_PYTHON_MINOR",
        )

    def test_gate_exits_on_old_python(self):
        self.assertIn(
            "sys.version_info >= (3, $MIN_PYTHON_MINOR)",
            self.src,
            "install.sh must compare the live interpreter against MIN_PYTHON_MINOR",
        )
        self.assertIn("but the hooks require 3.$MIN_PYTHON_MINOR or newer", self.src)

    def test_gate_checks_for_a_toml_parser(self):
        # tomllib is 3.11+; below that the hooks need the tomli backport, and
        # without either they traceback on every fire.
        self.assertIn("u.find_spec('tomllib') or u.find_spec('tomli')", self.src)
        self.assertIn("pip install --user tomli", self.src)

    def test_toml_gate_is_not_fatal_in_check_dependencies(self):
        """
        The hard TOML failure must not sit in _check_dependencies.

        _check_dependencies runs on every invocation, uninstall included.
        Refusing to *remove* hooks because the interpreter cannot *run* them
        strands a user whose PATH moved after install. The fatal check belongs
        to the module closure, which an uninstall-only run never enters.
        """
        deps = self._function_body("_check_dependencies")
        self.assertNotIn(
            "_require_toml_parser", deps,
            "_check_dependencies must not hard-fail on a missing TOML parser",
        )
        # It should still say something -- just not exit.
        self.assertIn("u.find_spec('tomllib') or u.find_spec('tomli')", deps)
        self.assertIn("has no TOML parser", deps)
        self.assertNotIn("has no TOML parser available", deps)

        closure = self._function_body("_compute_and_install_module_closure")
        self.assertIn(
            "_require_toml_parser", closure,
            "the module closure must require a TOML parser before copying modules",
        )
        # ... and only after the empty-closure early return, or an uninstall-only
        # run (which reaches the closure and finds it empty) would still be gated.
        self.assertGreater(
            closure.index("_require_toml_parser"),
            closure.index("Module closure: empty"),
            "the TOML gate must sit after the empty-closure early return",
        )
        self.assertLess(
            closure.index("_require_toml_parser"),
            closure.index('cp "$PROJECT_HOOKS_DIR/$mod"'),
            "the TOML gate must fire before any module is copied",
        )

    def test_dependency_check_runs_before_install(self):
        self.assertIn("\n_check_dependencies\n", self.src)

    def test_smoke_test_does_not_excuse_import_failures(self):
        # The old wording ("this may be okay if dependencies are missing") is
        # what let the tomllib break reach a user as a runtime traceback.
        self.assertNotIn("this may be okay if dependencies are missing", self.src)
        self.assertIn("Hook modules failed to import", self.src)


# =============================================================================
# Static compatibility of the shipped modules
# =============================================================================

class TestHookModulesParseUnderMinPython(unittest.TestCase):
    """No hook may use syntax newer than the declared floor."""

    def test_hook_modules_parse(self):
        floor = min_python()
        failures = []
        for path in hook_modules() + RELAY_CLIENT_MODULES:
            try:
                ast.parse(path.read_text(), feature_version=floor)
            except SyntaxError as exc:
                failures.append(f"{path.relative_to(REPO)}:{exc.lineno}: {exc.msg}")
        self.assertEqual(
            failures, [],
            f"syntax newer than Python {floor[0]}.{floor[1]}:\n  " + "\n  ".join(failures),
        )


class TestTomllibIsGuarded(unittest.TestCase):
    """``import tomllib`` must always carry the tomli fallback."""

    def test_no_bare_tomllib_import(self):
        offenders = []
        for path in hook_modules() + RELAY_CLIENT_MODULES:
            tree = ast.parse(path.read_text())
            guarded = _guarded_import_nodes(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Import) or id(node) in guarded:
                    continue
                if any(alias.name == "tomllib" for alias in node.names):
                    offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
        self.assertEqual(
            offenders, [],
            "tomllib is stdlib only on 3.11+; these imports need the "
            "try/except ImportError -> tomli fallback:\n  " + "\n  ".join(offenders),
        )

    def test_fallback_actually_resolves_to_tomli(self):
        for path in hook_modules() + RELAY_CLIENT_MODULES:
            text = path.read_text()
            if "import tomllib" not in text:
                continue
            with self.subTest(module=path.name):
                self.assertIn("import tomli as tomllib", text)


class TestUnionAnnotations(unittest.TestCase):
    """PEP 604 unions are evaluated at import time unless annotations are lazy."""

    def test_union_annotations_require_future_import(self):
        floor = min_python()
        if floor >= (3, 10):
            self.skipTest("PEP 604 is native from 3.10")
        offenders = []
        for path in hook_modules() + RELAY_CLIENT_MODULES:
            tree = ast.parse(path.read_text())
            if _has_future_annotations(tree):
                continue
            for lineno in _has_union_annotation(tree):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}")
        self.assertEqual(
            offenders, [],
            "'X | Y' annotations raise TypeError at import time on Python 3.9. "
            "Add 'from __future__ import annotations' to:\n  " + "\n  ".join(offenders),
        )


# =============================================================================
# Real interpreter, when one is around
# =============================================================================

class TestImportUnderOldInterpreter(unittest.TestCase):
    """Belt-and-braces: import every hook for real on a pre-3.11 interpreter."""

    def test_all_hook_modules_import(self):
        interpreter = _find_old_interpreter()
        if interpreter is None:
            self.skipTest("no pre-3.11 interpreter with tomli available")

        script = (
            "import importlib, pathlib, sys\n"
            "d = pathlib.Path(sys.argv[1])\n"
            "sys.path.insert(0, str(d))\n"
            "bad = []\n"
            "for name in sorted(p.stem for p in d.glob('*.py')):\n"
            "    try:\n"
            "        importlib.import_module(name)\n"
            "    except BaseException as exc:\n"
            "        bad.append('%s: %s: %s' % (name, type(exc).__name__, exc))\n"
            "print('\\n'.join(bad))\n"
            "sys.exit(1 if bad else 0)\n"
        )
        result = subprocess.run(
            [interpreter, "-c", script, str(HOOKS_DIR)],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(
            result.returncode, 0,
            f"hook modules failed to import under {interpreter}:\n{result.stdout}",
        )


class TestTomlGateScope(unittest.TestCase):
    """
    The TOML gate blocks installing, never managing.

    Needs a real interpreter with no TOML parser, so it skips on a machine that
    has none. `uv python find 3.10` supplies one on most dev boxes.
    """

    def setUp(self):
        self.interpreter = _find_interpreter_without_toml()
        if self.interpreter is None:
            self.skipTest("no pre-3.11 interpreter without a TOML parser available")
        self.tmp_home = Path(tempfile.mkdtemp(prefix="toml-gate-"))
        self.crippled = _path_with_python3(
            self.interpreter, self.tmp_home / "no-toml-bin"
        )

    def tearDown(self):
        shutil.rmtree(str(self.tmp_home), ignore_errors=True)

    def test_install_is_refused_and_copies_nothing(self):
        result = run_installer(
            self.tmp_home,
            extra_args=["--only", "permission-hooks,telegram", "--yes"],
            extra_env=self.crippled,
        )
        self.assertEqual(result.returncode, 1, result.stdout[-2000:])
        self.assertIn("has no TOML parser available", result.stdout + result.stderr)
        hooks = self.tmp_home / ".claude" / "hooks"
        self.assertFalse(
            hooks.exists() and any(hooks.iterdir()),
            "a refused install must not leave hook modules behind",
        )

    def test_uninstall_still_works(self):
        installed = run_installer(
            self.tmp_home,
            extra_args=["--only", "permission-hooks,telegram", "--yes"],
        )
        self.assertEqual(installed.returncode, 0, installed.stdout[-2000:])
        router = self.tmp_home / ".claude" / "hooks" / "telegram_permission_router.py"
        self.assertTrue(router.exists(), "setup failed: telegram was not installed")

        result = run_installer(
            self.tmp_home,
            extra_args=["--uninstall", "telegram", "--yes"],
            extra_env=self.crippled,
        )
        self.assertEqual(
            result.returncode, 0,
            "uninstall must not be blocked by a missing TOML parser:\n"
            + result.stdout[-2000:],
        )
        self.assertFalse(router.exists(), "telegram was not actually removed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
