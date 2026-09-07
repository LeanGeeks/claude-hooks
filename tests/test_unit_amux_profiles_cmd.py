#!/usr/bin/env python3
"""Unit tests for ``amux-spawn profiles`` — the per-profile tier-map output.

The human output is a block per profile: ``name  provider`` followed by every
model tier the profile sets, so a caller can see what ``--profile X --model
<tier>`` resolves to before spawning.  ``--json`` is unchanged.

All tests point the loader at a temporary TOML — never ``~/.claude/profiles.toml``.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

_HOOKS = Path(__file__).parent.parent / ".claude" / "hooks"
_BIN = Path(__file__).parent.parent / ".claude" / "bin" / "amux-spawn"
sys.path.insert(0, str(_HOOKS))

import amux_spawn_lib as lib  # noqa: E402


def _load_cli():
    spec = importlib.util.spec_from_loader(
        "amux_spawn_cli_profiles",
        importlib.machinery.SourceFileLoader("amux_spawn_cli_profiles", str(_BIN)),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cli = _load_cli()


_TOML = """\
[all-profiles]
API_TIMEOUT_MS = "600000"

[profile.claude]
ANTHROPIC_DEFAULT_FABLE_MODEL  = "claude-opus-5"
ANTHROPIC_DEFAULT_OPUS_MODEL   = "claude-opus-4-6"
ANTHROPIC_DEFAULT_SONNET_MODEL = "claude-sonnet-4-6"

[profile.claude-glm]
ANTHROPIC_BASE_URL             = "https://api.z.ai/api/anthropic"
ANTHROPIC_AUTH_TOKEN           = "secret-token-value"
ANTHROPIC_MODEL                = "glm-5.3[1m]"
ANTHROPIC_DEFAULT_OPUS_MODEL   = "glm-5.3[1m]"
ANTHROPIC_DEFAULT_SONNET_MODEL = "glm-5.3-flash[1m]"
ANTHROPIC_DEFAULT_HAIKU_MODEL  = "glm-5.3-flash"
ANTHROPIC_SMALL_FAST_MODEL     = "glm-5.3-flash"

[profile.claude-oc]
GITHUB_MCP_PAT               = "ghp_secret_pat_value"
CLAUDE_CODE_EXTRA_PATH       = "/opt/tools/bin"
ANTHROPIC_BASE_URL           = "http://claude-router.localhost"
ANTHROPIC_DEFAULT_OPUS_MODEL = "ocg-glm-5.2"
CLAUDE_CODE_EFFORT_LEVEL     = "high"

[profile.claude-bare]
# no models at all — backend-less, picks up only [all-profiles]

[profile.claude-effort-only]
CLAUDE_CODE_EFFORT_LEVEL = "max"
"""


class _ProfilesCase(unittest.TestCase):
    """Runs ``cmd_profiles`` against a temp TOML and captures stdout."""

    toml = _TOML

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.path = Path(self._tmpdir.name) / "profiles.toml"
        self.path.write_text(textwrap.dedent(self.toml))

    def _blocks(self) -> dict:
        """Parse the human output into ``{name: (provider, [(label, value)])}``."""
        blocks = {}
        current = None
        for line in self._run().splitlines():
            if not line.strip():
                continue
            if line.startswith("  "):
                parts = line.strip().split(None, 1)
                blocks[current][1].append((parts[0], parts[1] if len(parts) > 1 else ""))
            else:
                name, provider = line.split(None, 1)
                current = name
                blocks[name] = (provider.strip(), [])
        return blocks

    def _run(self, json_out=False, no_redact=False) -> str:
        return self._run_full(json_out=json_out, no_redact=no_redact)[0]

    def _run_full(self, json_out=False, no_redact=False):
        """Return ``(stdout, stderr)`` for one ``cmd_profiles`` invocation."""
        out, err = io.StringIO(), io.StringIO()
        argv = ["profiles"]
        if json_out:
            argv.append("--json")
        if no_redact:
            argv.append("--no-redact")
        ns = cli.build_parser().parse_args(argv)
        with patch.object(lib, "PROFILES_TOML", self.path), \
                contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            rc = cli.cmd_profiles(ns)
        self.assertEqual(rc, 0)
        return out.getvalue(), err.getvalue()

    def _json(self, no_redact=False) -> dict:
        """``--json`` output as ``{name: env}``."""
        data = json.loads(self._run(json_out=True, no_redact=no_redact))
        return {p["name"]: p["env"] for p in data}


class TestTierMapOutput(_ProfilesCase):
    def test_every_tier_of_a_profile_is_listed(self):
        """The whole point: all overridden models, not just the headline one."""
        rows = dict(self._blocks()["claude-glm"][1])
        for label, model in (
            ("model", "glm-5.3[1m]"),
            ("opus", "glm-5.3[1m]"),
            ("sonnet", "glm-5.3-flash[1m]"),
            ("haiku", "glm-5.3-flash"),
            ("small-fast", "glm-5.3-flash"),
        ):
            self.assertEqual(rows[label].split()[0], model)

    def test_tier_order_is_canonical_not_alphabetical(self):
        """model (a pin) first, then fable/opus/sonnet/haiku/small-fast."""
        labels = [lbl for lbl, _ in self._blocks()["claude-glm"][1]]
        self.assertEqual(labels, ["model", "opus", "sonnet", "haiku", "small-fast"])

    def test_unset_tiers_are_omitted(self):
        """claude sets no haiku/small-fast — no empty rows, no '-' placeholders."""
        labels = [lbl for lbl, _ in self._blocks()["claude"][1]]
        self.assertEqual(labels, ["fable", "opus", "sonnet"])

    def test_anthropic_model_is_annotated_as_a_pin(self):
        out = self._run()
        self.assertRegex(out, r"\n  model\s+glm-5\.3\[1m\]\s+\(ANTHROPIC_MODEL\)")

    def test_effort_level_is_shown_with_its_override_warning(self):
        """A profile-set CLAUDE_CODE_EFFORT_LEVEL silently beats --effort."""
        out = self._run()
        self.assertRegex(
            out, r"\n  effort\s+high\s+\(CLAUDE_CODE_EFFORT_LEVEL, overrides --effort\)"
        )

    def test_profile_with_no_models_says_so(self):
        rows = self._blocks()["claude-bare"][1]
        self.assertEqual(len(rows), 1)
        self.assertIn("no model pinned", " ".join(rows[0]))

    def test_effort_only_profile_still_reports_no_model(self):
        """An effort row is not a model row — the hint must survive it."""
        rows = self._blocks()["claude-effort-only"][1]
        self.assertIn("no model pinned", " ".join(rows[0]))
        self.assertEqual(rows[1][0], "effort")
        self.assertTrue(rows[1][1].startswith("max"))

    def test_provider_is_the_base_url_host_else_anthropic(self):
        blocks = self._blocks()
        self.assertEqual(blocks["claude"][0], "anthropic")
        self.assertEqual(blocks["claude-glm"][0], "api.z.ai")
        self.assertEqual(blocks["claude-oc"][0], "claude-router.localhost")

    def test_no_secret_leaks_into_human_output(self):
        """Only --json exposes env wholesale; the table must not."""
        out = self._run()
        self.assertNotIn("secret-token-value", out)
        self.assertNotIn("API_TIMEOUT_MS", out)

    def test_profiles_are_sorted_and_blank_line_separated(self):
        headers = list(self._blocks())
        self.assertEqual(headers, sorted(headers))
        self.assertEqual(len(headers), 5)
        # Blocks are separated by exactly one blank line.
        self.assertEqual(self._run().count("\n\n"), 4)


class TestJsonShape(_ProfilesCase):
    def test_json_is_name_env_objects_sorted_by_name(self):
        data = json.loads(self._run(json_out=True))
        self.assertEqual(
            [p["name"] for p in data],
            ["claude", "claude-bare", "claude-effort-only", "claude-glm", "claude-oc"],
        )
        glm = next(p for p in data if p["name"] == "claude-glm")
        self.assertEqual(glm["env"]["ANTHROPIC_MODEL"], "glm-5.3[1m]")
        # [all-profiles] merged in, as before.
        self.assertEqual(glm["env"]["API_TIMEOUT_MS"], "600000")


class TestJsonRedaction(_ProfilesCase):
    """Credentials are redacted by default; --no-redact opts out."""

    def test_auth_token_is_redacted_by_default(self):
        self.assertEqual(
            self._json()["claude-glm"]["ANTHROPIC_AUTH_TOKEN"], cli.REDACTED
        )

    def test_raw_secret_appears_nowhere_in_default_output(self):
        out = self._run(json_out=True)
        self.assertNotIn("secret-token-value", out)
        self.assertNotIn("ghp_secret_pat_value", out)

    def test_no_redact_prints_the_real_values(self):
        env = self._json(no_redact=True)["claude-glm"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "secret-token-value")

    def test_redaction_hides_values_not_keys(self):
        """Which vars a profile sets stays visible — only the values go."""
        self.assertEqual(
            sorted(self._json()["claude-glm"]),
            sorted(self._json(no_redact=True)["claude-glm"]),
        )

    def test_non_secret_values_survive_redaction(self):
        env = self._json()["claude-glm"]
        self.assertEqual(env["ANTHROPIC_MODEL"], "glm-5.3[1m]")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.z.ai/api/anthropic")
        self.assertEqual(env["API_TIMEOUT_MS"], "600000")

    def test_pat_is_redacted_but_a_path_key_is_not(self):
        """Segment matching, not substring: PAT != PATH."""
        env = self._json()["claude-oc"]
        self.assertEqual(env["GITHUB_MCP_PAT"], cli.REDACTED)
        self.assertEqual(env["CLAUDE_CODE_EXTRA_PATH"], "/opt/tools/bin")

    def test_no_redact_without_json_warns_and_changes_nothing(self):
        plain, _ = self._run_full()
        out, err = self._run_full(no_redact=True)
        self.assertIn("--no-redact only affects --json", err)
        self.assertEqual(out, plain)


class TestSecretKeyClassifier(unittest.TestCase):
    """Unit-level: the segment matcher behind redaction."""

    def test_credential_names_match(self):
        for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY", "GITHUB_MCP_PAT",
                    "MY_SECRET", "DB_PASSWORD", "SOME_CREDENTIALS", "lowercase_token"):
            self.assertTrue(cli.is_secret_key(key), key)

    def test_lookalike_names_do_not_match(self):
        for key in ("PATH", "CLAUDE_CODE_EXTRA_PATH", "MONKEY", "ANTHROPIC_BASE_URL",
                    "API_TIMEOUT_MS", "ANTHROPIC_DEFAULT_OPUS_MODEL", "KEYBOARD"):
            self.assertFalse(cli.is_secret_key(key), key)

    def test_redact_env_is_a_copy(self):
        src = {"ANTHROPIC_AUTH_TOKEN": "t", "ANTHROPIC_MODEL": "m"}
        out = cli.redact_env(src)
        self.assertEqual(src["ANTHROPIC_AUTH_TOKEN"], "t")
        self.assertEqual(out, {"ANTHROPIC_AUTH_TOKEN": cli.REDACTED,
                               "ANTHROPIC_MODEL": "m"})


class TestNoProfilesFile(unittest.TestCase):
    def test_missing_file_is_a_clean_message_not_a_crash(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "nope.toml"
            buf = io.StringIO()
            ns = cli.build_parser().parse_args(["profiles"])
            with patch.object(lib, "PROFILES_TOML", missing), \
                    contextlib.redirect_stdout(buf):
                rc = cli.cmd_profiles(ns)
            self.assertEqual(rc, 0)
            self.assertIn("no profiles found", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
