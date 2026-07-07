#!/usr/bin/env python3
"""Unit tests for the profile-loader functions added to amux_spawn_lib (epic 13-01).

All tests use temporary TOML files — never ``~/.claude/profiles.toml``.
"""

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
sys.path.insert(0, str(_HOOKS))

import amux_spawn_lib as lib  # noqa: E402


# ── Helpers ──────────────────────────────────────────────────────────────────

def _write_toml(tmp_dir: Path, content: str) -> Path:
    """Write *content* to a temp TOML file and return its path."""
    p = tmp_dir / "profiles.toml"
    p.write_text(textwrap.dedent(content))
    return p


# ── Canonical multi-profile TOML (mirrors the task-spec example) ─────────────

_CANONICAL_TOML = """\
[vars]
glm_token  = "e468a9fd..."
github_pat = "ghp_ND0y..."
opus       = "claude-opus-4-6"

[all-profiles]
API_TIMEOUT_MS = "600000"
GITHUB_MCP_PAT = "${github_pat}"

[profile.claude]
ANTHROPIC_DEFAULT_OPUS_MODEL   = "${opus}"
ANTHROPIC_DEFAULT_SONNET_MODEL = "claude-sonnet-4-6"

[profile.claude-latest]
# empty — gets only [all-profiles] vars

[profile.claude-glm5]
ANTHROPIC_BASE_URL   = "https://api.z.ai/api/anthropic"
ANTHROPIC_AUTH_TOKEN = "${glm_token}"
ANTHROPIC_MODEL      = "glm-5.2[1m]"
API_TIMEOUT_MS       = "900000"
"""


class TestLoadProfilesHappyPath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)

    def _canonical_path(self) -> Path:
        return _write_toml(self.tmp, _CANONICAL_TOML)

    def test_returns_all_three_profiles(self):
        profiles = lib.load_profiles(self._canonical_path())
        self.assertIn("claude", profiles)
        self.assertIn("claude-latest", profiles)
        self.assertIn("claude-glm5", profiles)

    def test_resolve_claude_glm5_matches_spec(self):
        profiles = lib.load_profiles(self._canonical_path())
        got = profiles["claude-glm5"]
        self.assertEqual(got["API_TIMEOUT_MS"], "900000")          # profile overrides
        self.assertEqual(got["GITHUB_MCP_PAT"], "ghp_ND0y...")     # inherited from all-profiles
        self.assertEqual(got["ANTHROPIC_BASE_URL"], "https://api.z.ai/api/anthropic")
        self.assertEqual(got["ANTHROPIC_AUTH_TOKEN"], "e468a9fd...") # ${glm_token} resolved
        self.assertEqual(got["ANTHROPIC_MODEL"], "glm-5.2[1m]")

    def test_resolve_claude_latest_gets_only_all_profiles(self):
        profiles = lib.load_profiles(self._canonical_path())
        got = profiles["claude-latest"]
        self.assertEqual(got, {
            "API_TIMEOUT_MS": "600000",
            "GITHUB_MCP_PAT": "ghp_ND0y...",
        })

    def test_resolve_claude_has_opus_and_sonnet_and_all_profiles(self):
        profiles = lib.load_profiles(self._canonical_path())
        got = profiles["claude"]
        self.assertEqual(got["ANTHROPIC_DEFAULT_OPUS_MODEL"], "claude-opus-4-6")
        self.assertEqual(got["ANTHROPIC_DEFAULT_SONNET_MODEL"], "claude-sonnet-4-6")
        self.assertEqual(got["API_TIMEOUT_MS"], "600000")
        self.assertEqual(got["GITHUB_MCP_PAT"], "ghp_ND0y...")

    def test_vars_are_not_exported(self):
        """[vars] keys must never appear in any profile's env dict."""
        profiles = lib.load_profiles(self._canonical_path())
        for name, env in profiles.items():
            for var_key in ("glm_token", "github_pat", "opus"):
                self.assertNotIn(var_key, env, msg=f"var {var_key!r} leaked into profile {name!r}")

    def test_profile_wins_on_collision(self):
        """API_TIMEOUT_MS is in [all-profiles] (600000) but claude-glm5 overrides to 900000."""
        profiles = lib.load_profiles(self._canonical_path())
        self.assertEqual(profiles["claude-glm5"]["API_TIMEOUT_MS"], "900000")


class TestMissingFile(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        result = lib.load_profiles("/nonexistent/path/profiles.toml")
        self.assertEqual(result, {})

    def test_resolve_profile_missing_file_raises_value_error(self):
        with self.assertRaises(ValueError):
            lib.resolve_profile("any-name", "/nonexistent/path/profiles.toml")

    def test_profile_names_missing_file_returns_empty_list(self):
        result = lib.profile_names("/nonexistent/path/profiles.toml")
        self.assertEqual(result, [])

    def test_emit_shell_functions_missing_file_returns_empty_string(self):
        result = lib.emit_shell_functions("/nonexistent/path/profiles.toml")
        self.assertEqual(result, "")


class TestVarInterpolation(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)

    def test_var_resolved_in_all_profiles(self):
        p = _write_toml(self.tmp, """\
[vars]
tok = "secret"

[all-profiles]
MY_TOKEN = "${tok}"

[profile.x]
""")
        env = lib.resolve_profile("x", p)
        self.assertEqual(env["MY_TOKEN"], "secret")

    def test_var_resolved_in_profile(self):
        p = _write_toml(self.tmp, """\
[vars]
base = "https://example.com"

[all-profiles]

[profile.x]
BASE_URL = "${base}"
""")
        env = lib.resolve_profile("x", p)
        self.assertEqual(env["BASE_URL"], "https://example.com")

    def test_dollar_brace_escape(self):
        """``$${`` should produce a literal ``${`` in the output."""
        p = _write_toml(self.tmp, """\
[vars]

[all-profiles]

[profile.x]
LITERAL = "$${not_a_var}"
""")
        env = lib.resolve_profile("x", p)
        self.assertEqual(env["LITERAL"], "${not_a_var}")

    def test_undefined_var_raises_at_load_time(self):
        p = _write_toml(self.tmp, """\
[vars]

[all-profiles]
BAD = "${undefined_var}"

[profile.x]
""")
        with self.assertRaises(ValueError) as ctx:
            lib.load_profiles(p)
        self.assertIn("undefined_var", str(ctx.exception))

    def test_undefined_var_in_profile_raises(self):
        p = _write_toml(self.tmp, """\
[vars]

[all-profiles]

[profile.x]
FOO = "${missing}"
""")
        with self.assertRaises(ValueError) as ctx:
            lib.load_profiles(p)
        self.assertIn("missing", str(ctx.exception))

    def test_var_not_recursive(self):
        """A var's value is not re-expanded even if it contains ``${...}``."""
        p = _write_toml(self.tmp, """\
[vars]
a = "${b}"
b = "hello"

[all-profiles]

[profile.x]
OUT = "${a}"
""")
        # ${a} expands to "${b}" literally (one-pass), NOT to "hello".
        env = lib.resolve_profile("x", p)
        self.assertEqual(env["OUT"], "${b}")


class TestResolveProfile(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)

    def test_unknown_profile_raises_value_error(self):
        p = _write_toml(self.tmp, """\
[vars]

[all-profiles]

[profile.exists]
""")
        with self.assertRaises(ValueError):
            lib.resolve_profile("does-not-exist", p)

    def test_returns_correct_profile(self):
        p = _write_toml(self.tmp, """\
[vars]

[all-profiles]

[profile.myprofile]
MY_KEY = "my-value"
""")
        env = lib.resolve_profile("myprofile", p)
        self.assertEqual(env["MY_KEY"], "my-value")


class TestProfileNames(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)

    def test_sorted_list_of_names(self):
        p = _write_toml(self.tmp, """\
[vars]

[all-profiles]

[profile.zzz]

[profile.aaa]

[profile.mmm]
""")
        names = lib.profile_names(p)
        self.assertEqual(names, ["aaa", "mmm", "zzz"])

    def test_empty_profiles_returns_empty_list(self):
        p = _write_toml(self.tmp, """\
[vars]
x = "y"

[all-profiles]
FOO = "bar"
""")
        names = lib.profile_names(p)
        self.assertEqual(names, [])


class TestEmitShellFunctions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)

    def _canonical_path(self) -> Path:
        return _write_toml(self.tmp, _CANONICAL_TOML)

    def test_contains_function_per_profile(self):
        output = lib.emit_shell_functions(self._canonical_path())
        self.assertIn("claude()", output)
        self.assertIn("claude-latest()", output)
        self.assertIn("claude-glm5()", output)

    def test_amux_spawn_branch_present(self):
        output = lib.emit_shell_functions(self._canonical_path())
        self.assertIn("command -v amux-spawn", output)
        self.assertIn("amux-spawn spawn --profile claude-glm5", output)

    def test_fallback_branch_exports_resolved_values(self):
        output = lib.emit_shell_functions(self._canonical_path())
        # Values must be fully resolved — no ${...} references remaining.
        self.assertNotIn("${", output)
        self.assertIn('ANTHROPIC_AUTH_TOKEN="e468a9fd..."', output)
        self.assertIn('ANTHROPIC_MODEL="glm-5.2[1m]"', output)

    def test_exec_claude_present(self):
        output = lib.emit_shell_functions(self._canonical_path())
        self.assertIn('exec claude "$@"', output)

    def test_values_double_quoted(self):
        p = _write_toml(self.tmp, """\
[vars]

[all-profiles]

[profile.myp]
SOME_VAR = "value with spaces"
""")
        output = lib.emit_shell_functions(p)
        self.assertIn('SOME_VAR="value with spaces"', output)

    def test_backslash_in_value_escaped(self):
        p = _write_toml(self.tmp, r"""
[vars]

[all-profiles]

[profile.myp]
SOME_VAR = "back\\slash"
""")
        output = lib.emit_shell_functions(p)
        # The backslash pair in the TOML literal becomes a single backslash in Python,
        # which must be escaped to \\ in the emitted double-quoted bash string.
        self.assertIn('SOME_VAR="back\\\\slash"', output)

    def test_double_quote_in_value_escaped(self):
        p = _write_toml(self.tmp, """\
[vars]

[all-profiles]

[profile.myp]
SOME_VAR = 'has "quotes"'
""")
        output = lib.emit_shell_functions(p)
        self.assertIn(r'SOME_VAR="has \"quotes\""', output)

    def test_dollar_sign_in_value_escaped(self):
        p = _write_toml(self.tmp, """\
[vars]

[all-profiles]

[profile.myp]
SOME_VAR = "price$100"
""")
        output = lib.emit_shell_functions(p)
        self.assertIn(r'SOME_VAR="price\$100"', output)

    def test_missing_file_returns_empty_string(self):
        output = lib.emit_shell_functions("/no/such/file.toml")
        self.assertEqual(output, "")

    def test_empty_profile_gets_all_profiles_vars(self):
        """claude-latest has no per-profile keys but gets [all-profiles] exports."""
        output = lib.emit_shell_functions(self._canonical_path())
        # Find the claude-latest function block and verify it exports all-profiles vars.
        idx = output.find("claude-latest()")
        self.assertGreater(idx, -1)
        # The block after this point (up to the closing brace) should have
        # API_TIMEOUT_MS and GITHUB_MCP_PAT.
        block = output[idx:]
        self.assertIn("API_TIMEOUT_MS", block)
        self.assertIn("GITHUB_MCP_PAT", block)

    def test_subshell_wrapper_present(self):
        output = lib.emit_shell_functions(self._canonical_path())
        self.assertIn("(\n", output)
        self.assertIn("        )", output)


class TestEmitProfileFunctions(unittest.TestCase):
    """Tests for emit_profile_functions() — profiles only, no amux-spawn."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)

    def _canonical_path(self) -> Path:
        return _write_toml(self.tmp, _CANONICAL_TOML)

    def test_contains_function_per_profile(self):
        output = lib.emit_profile_functions(self._canonical_path())
        self.assertIn("claude()", output)
        self.assertIn("claude-latest()", output)
        self.assertIn("claude-glm5()", output)

    def test_no_amux_spawn_branch(self):
        output = lib.emit_profile_functions(self._canonical_path())
        self.assertNotIn("amux-spawn", output)
        self.assertNotIn("command -v", output)

    def test_exports_resolved_values(self):
        output = lib.emit_profile_functions(self._canonical_path())
        self.assertNotIn("${", output)
        self.assertIn('ANTHROPIC_AUTH_TOKEN="e468a9fd..."', output)
        self.assertIn('ANTHROPIC_MODEL="glm-5.2[1m]"', output)

    def test_exec_claude_present(self):
        output = lib.emit_profile_functions(self._canonical_path())
        self.assertIn('exec claude "$@"', output)

    def test_subshell_wrapper_present(self):
        output = lib.emit_profile_functions(self._canonical_path())
        self.assertIn("(\n", output)

    def test_missing_file_returns_empty_string(self):
        output = lib.emit_profile_functions("/no/such/file.toml")
        self.assertEqual(output, "")

    def test_empty_profile_gets_all_profiles_vars(self):
        output = lib.emit_profile_functions(self._canonical_path())
        idx = output.find("claude-latest()")
        self.assertGreater(idx, -1)
        block = output[idx:]
        self.assertIn("API_TIMEOUT_MS", block)
        self.assertIn("GITHUB_MCP_PAT", block)


class TestMergeOverride(unittest.TestCase):
    """Focused tests for the merge order: [all-profiles] → [profile.X] wins."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.tmp = Path(self._tmp)

    def test_profile_overrides_all_profiles_key(self):
        p = _write_toml(self.tmp, """\
[vars]

[all-profiles]
SHARED = "global"
TIMEOUT = "100"

[profile.a]
TIMEOUT = "999"
""")
        env = lib.resolve_profile("a", p)
        self.assertEqual(env["SHARED"], "global")    # inherited
        self.assertEqual(env["TIMEOUT"], "999")      # overridden

    def test_all_profiles_key_not_in_profile_is_inherited(self):
        p = _write_toml(self.tmp, """\
[vars]

[all-profiles]
ONLY_GLOBAL = "yes"

[profile.b]
OWN_KEY = "own"
""")
        env = lib.resolve_profile("b", p)
        self.assertEqual(env["ONLY_GLOBAL"], "yes")
        self.assertEqual(env["OWN_KEY"], "own")


if __name__ == "__main__":
    unittest.main()
