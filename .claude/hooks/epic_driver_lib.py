#!/usr/bin/env python3
"""The epic driver's tick logic (epic 50; ``tasks/50_epic_driver/architecture.md``).

One module, in sections that the epic's tasks add in turn (architecture §2):

====================  ==========================================  ======
Section               What                                        Task
====================  ==========================================  ======
Constants             the architecture's *defaults*, one block    50-04
Time                  aware-UTC helpers                           50-04
World                 the effect boundary (``World``/``RealWorld``) 50-04 …
Value grammar         §5.1 and the name builder of §3             50-04
Ledger                model + validator (§5.2–§5.5)               50-04
Runtime state         the JSON of §8, paths of §4, ``mark``       50-04
Schedule              step state machine, due/dispatchable (§6)   50-04
Approvals             the selection side of §15.1                 50-04
Checks and triggers   the runner and its budget (§7)              50-04
Step alarm conds.     step-level rows of §16.2 (conditions only)  50-04
Session readings      ``take_readings``: the §11.1 inputs via World 50-05
Verdicts              the §10 grammar, its checks, its application 50-05
Delivery (reading)    §9.3 confirmation, "provably did not land"  50-05
Usage limits          §12.4 classification, reset times, ladder   50-05
Startup dialogs       §12.11 pane classifier and listed answers   50-05
Session reconcile     ``reconcile``: §12 facts, conditions, items 50-05
====================  ==========================================  ======

Later tasks append their own sections (reconcile §12, dispatch §9/§11,
worktrees §14, alarms/notify §16, the tick §23) and extend ``World`` with one
method per new effect.

Rules every section follows:

- **Every effect goes through ``World``** (subprocess, git, tmux, amux,
  systemctl, notify, the clock). Everything else is pure over explicit inputs
  and an explicit ``now`` (UTC, timezone-aware), so it is testable with a fake
  ``World`` and a fake clock.
- **Python floor** (``MIN_PYTHON_MINOR`` in ``install.sh``): no PEP 604 unions
  evaluated at import (``from __future__ import annotations`` below), no
  ``tomllib``. PyYAML is imported lazily-guarded: the module imports without
  it, and only :func:`load_ledger` needs it (``SafeLoader`` only).
- Nothing in this module starts a session, sends to a pane or notifies by
  itself; the tick (50-08) runs the effects a pass decides on.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, TypedDict

try:  # PyYAML is a system package (6.0.3 on the workstation); optional at import.
    import yaml  # type: ignore[import-untyped]
except ImportError:  # pragma: no cover - exercised only where PyYAML is absent
    yaml = None  # type: ignore[assignment]

import amux_spawn_lib as asl
import claude_event_reducer as cer

if yaml is not None:
    class _UniqueKeyLoader(yaml.SafeLoader):
        """``SafeLoader`` that rejects a duplicate key in any mapping (key and line reported)."""

    def _construct_unique_mapping(loader: Any, node: Any, deep: bool = False) -> Dict[Any, Any]:
        # Duplicates are checked among the keys written explicitly in this mapping. Merge keys
        # (``<<: *anchor`` / ``<<: [*a, *b]``) are left to PyYAML's standard flatten_mapping, which
        # lets explicit keys override merged ones and earlier merge sources win over later ones.
        seen = set()
        for key_node, _ in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                continue
            key = loader.construct_object(key_node, deep=True)
            try:
                duplicate = key in seen
            except TypeError:   # unhashable key: construct_mapping reports it
                continue
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    f"found duplicate key {key!r}", key_node.start_mark)
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep)

    _UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                                     _construct_unique_mapping)


# =============================================================================
# Constants (architecture *defaults*; one block — later tasks add theirs here)
# =============================================================================

#: The ledger schema version (§5.2) and the runtime-state schema version (§8).
LEDGER_VERSION = 1
STATE_SCHEMA = 1

#: The state root (§4) and the environment override tests use for it.
STATE_ROOT_ENV = "EPIC_DRIVER_STATE_ROOT"
DEFAULT_STATE_ROOT = Path.home() / ".local" / "state" / "epic-driver"

#: Ledger ``defaults:`` (§5.2).
DEFAULT_CHECK_TIMEOUT_S = 60
DEFAULT_CHECK_EVERY_S = 10 * 60
DEFAULT_STEP_TIMEOUT_S = 6 * 3600
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_WARN_BEFORE_S = 24 * 3600

#: Role defaults (§5.3).
DEFAULT_CAP_TOKENS = 250_000
DEFAULT_BOOTSTRAP_TOKENS = 40_000
DEFAULT_KEEP_WARM = True
DEFAULT_STUCK_AFTER_S = 30 * 60

#: Names (§3).
MAX_SESSION_NAME_LEN = 100

#: Checks and triggers (§7, P0-10).
CHECK_BUDGET_S = 150                 # about 150 s of checks per tick
CHECK_KILL_GRACE_S = 5               # SIGTERM, then SIGKILL after 5 s
CHECK_OUTPUT_TAIL_BYTES = 4096       # the last 4 KB kept in the state and the log
CHECK_ERROR_ALARM_AT = 3             # check_error at 3 consecutive errors
MIN_CHECK_WINDOW_S = 20 * 60         # a check_window span under 20 min is rejected
#: ``check_every`` is measured from the previous run's START, with slack
#: ``min(CHECK_EVERY_SLACK_MAX_S, every * CHECK_EVERY_SLACK_PERCENT / 100)``
#: (:func:`check_every_slack_s`). The cap is the worst gap between two ticks'
#: runs of one check at the 10-min cadence: a run may start up to CHECK_BUDGET_S
#: into its tick and the next tick may fire 30 s early (timer accuracy), so
#: ``every: 10m`` needs 180 s to run on every tick. The percentage (30 % = 180 s
#: of 10 m) keeps shorter periods limited: a 2-3 min ``every`` still holds back
#: a hand-run ``tick`` repeated within seconds, which a flat 180 s would not.
CHECK_EVERY_SLACK_MAX_S = CHECK_BUDGET_S + 30
CHECK_EVERY_SLACK_PERCENT = 30
CHECK_SSH_OPTS = "-o ConnectTimeout=5 -o BatchMode=yes"
#: PATH for checks when no environment snapshot exists yet (the unit's PATH).
FALLBACK_CHECK_PATH = "/usr/local/bin:/usr/bin:/bin"
FALLBACK_LANG = "C.UTF-8"

#: Schedule (§6.2): due but not dispatched for this many ticks ⇒ due_not_started.
DUE_NOT_STARTED_TICKS = 2

#: Logs (§4): rotated at 1 MB, 3 kept.
LOG_ROTATE_BYTES = 1024 * 1024
LOG_KEEP = 3

#: §21.2: the allowlisted keys of the operator's interactive environment.
ENV_SNAPSHOT_KEYS = (
    "PATH", "GOPATH", "GOROOT", "GOBIN", "CARGO_HOME", "RUSTUP_HOME",
    "NVM_DIR", "NVM_BIN", "NVM_INC", "LANG", "LC_ALL", "LC_CTYPE", "EDITOR",
    "PYENV_ROOT",
)
ENV_SNAPSHOT_PREFIXES = ("NVM_",)

#: §5.2 startup_dialogs: the only keys the tick may send to a listed dialog.
STARTUP_DIALOG_KEYS = ("Enter", "Escape")

#: §11.6: the compaction knobs a role may pin (owned by amux_spawn_lib).
PINNABLE_ENV_KEYS = asl.PINNABLE_ENV_KEYS
AUTOCOMPACT_PCT_KEY = "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE"
IDLE_COMPACT_KEY = "CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS"

#: §4 file names under ``<state_dir>``.
STATE_FILE = "state.json"
ACTIVE_FILE = "active.json"
LOCK_FILE = "tick.lock"
PAUSED_FILE = "paused"
ENV_SNAPSHOT_FILE = "env_snapshot.json"
LAST_VALID_LEDGER_FILE = "ledger.last_valid.yaml"
WORKTREES_DIR = "worktrees"
POINTER_FILE = "epic-driver.run.json"

DAY_S = 86400

# -- 50-05: session reconcile and stalls (§9.3, §10, §11.1, §12, §15.2) --
TMUX_BIN = "tmux"                    # read-only verbs here: display-message, capture-pane
TMUX_READ_TIMEOUT_S = 5
LIFECYCLE_SUFFIX = ".lifecycle.jsonl"  # amux-spawn's per-handle lifecycle log (epic 37)
TMUX_DISPLAY_FIELDS = ("pane_dead", "pane_in_mode", "session_attached", "pane_pid")
OPEN_PROMPT_ALARM_S = 10 * 60        # permission prompt / AskUserQuestion open > 10 min (§12.6)
CLOCK_JUMP_S = 60                    # |Δwall − Δboottime| or Δboottime − Δmonotonic above it (§12.7)
#: §12.4: the fixed ladder when a limit entry names no reset time (15m, 30m, 1h, 2h, 4h, then 4h).
LIMIT_LADDER_S = (15 * 60, 30 * 60, 3600, 2 * 3600, 4 * 3600)
VERDICT_WAIT_TEXT_MAX = 200          # §10: a WAITING text is at most 200 characters
QUESTION_TEXT_MAX = 8000             # the answer text carried in an `answer` item
DIALOG_DETAIL_LINES = 2              # the startup_dialog alarm detail: the dialog's first lines
DIALOG_DETAIL_MAX = 160
PROMPT_GLYPHS = ("❯", ">")           # the TUI's prompt glyph (input box and menu pointer)


# =============================================================================
# Time
# =============================================================================

UTC = timezone.utc


def require_aware(now: datetime) -> datetime:
    """``now`` as aware UTC; a naive datetime is a programming error here."""
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be a timezone-aware datetime")
    return now.astimezone(UTC)


def iso(dt: Optional[datetime]) -> Optional[str]:
    """``2026-11-24T09:28:20Z`` (seconds precision), or None."""
    if dt is None:
        return None
    return require_aware(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(value: Any) -> Optional[datetime]:
    """An aware UTC datetime from a state timestamp (naive reads as UTC), else None."""
    epoch = asl.epoch_of(value)
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, UTC)


def tod_seconds(dt: datetime) -> int:
    """Seconds since UTC midnight."""
    dt = require_aware(dt)
    return dt.hour * 3600 + dt.minute * 60 + dt.second


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


# =============================================================================
# World — the effect boundary (architecture §2)
# =============================================================================

@dataclass
class CommandResult:
    """The result of a short command (git, …). ``rc`` None = could not run / timed out."""

    rc: Optional[int]
    stdout: str = ""
    stderr: str = ""
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.rc == 0


@dataclass
class CheckRun:
    """One run of a check or trigger command (§7.2).

    ``rc`` is None when the command timed out or could not start; ``error``
    then says which. ``output_tail`` is the last 4 KB of stdout+stderr.
    """

    rc: Optional[int]
    started_at: datetime
    duration_s: float
    output_tail: str = ""
    timed_out: bool = False
    error: Optional[str] = None


class World:
    """Every effect the driver has, one method each.

    Tests subclass this with fakes. Later tasks add methods (``tmux_display``,
    ``capture_pane``, ``amux_send``, ``spawn``, ``resume``, ``reap``,
    ``notify``, ``systemctl`` …) to both ``World`` and ``RealWorld``; a fake
    only overrides what its test needs, and an effect a fake did not expect
    fails loudly (``NotImplementedError``).
    """

    def now(self) -> datetime:
        raise NotImplementedError("World.now")

    def git(self, args: Sequence[str], cwd: str, timeout_s: float = 30.0) -> CommandResult:
        raise NotImplementedError("World.git")

    def run_check(self, command: str, cwd: str, env: Dict[str, str],
                  timeout_s: float) -> CheckRun:
        raise NotImplementedError("World.run_check")

    def profile_env(self, name: str) -> Dict[str, str]:
        """The merged environment of a ``~/.claude/profiles.toml`` profile.

        Raises ``ValueError`` for an unknown profile.
        """
        raise NotImplementedError("World.profile_env")

    # -- 50-05: the read-only effects of the session reconcile (§11.1, §12) --

    def tmux_has_session(self, name: str) -> bool:
        """``amux_spawn_lib.tmux_has_session(name)`` (exact ``=amux-<name>``)."""
        raise NotImplementedError("World.tmux_has_session")

    def tmux_display(self, name: str, fields: Sequence[str]) -> Optional[Dict[str, str]]:
        """``tmux display -p -t =amux-<name>: '#{f1}…'`` → ``{field: value}``; None on failure."""
        raise NotImplementedError("World.tmux_display")

    def capture_pane(self, name: str) -> Optional[str]:
        """``tmux capture-pane -p -t =amux-<name>:`` (text, no escapes); None on failure."""
        raise NotImplementedError("World.capture_pane")

    def list_handles(self) -> List[Dict[str, Any]]:
        """Every amux-spawn handle (``amux_spawn_lib.list_handles``)."""
        raise NotImplementedError("World.list_handles")

    def lifecycle_events(self, name: str) -> List[Dict[str, Any]]:
        """The handle's lifecycle log (``<name>.lifecycle.jsonl``) as parsed records, oldest first."""
        raise NotImplementedError("World.lifecycle_events")

    def amux_names(self) -> List[str]:
        """Every amux session name (``amux ls``, via ``amux_spawn_lib.list_amux_names``)."""
        raise NotImplementedError("World.amux_names")

    def process_work(self, pane_pid: int) -> "ProcessWork":
        """The Claude owner under ``pane_pid`` and whether a tool shell runs under it (§12.3)."""
        raise NotImplementedError("World.process_work")

    def clock_sample(self) -> "ClockSample":
        """Boot id, wall clock, ``CLOCK_BOOTTIME`` and ``CLOCK_MONOTONIC`` (§12.7)."""
        raise NotImplementedError("World.clock_sample")

    def local_timezone(self) -> Any:
        """The system timezone (a ``tzinfo``) that usage-limit reset times are read in (§12.4)."""
        raise NotImplementedError("World.local_timezone")

    def question_entries(self, workspace: str) -> Optional["QuestionsSnapshot"]:
        """The workspace's question queue (``questions_store.open_store(ws).all_entries()``, §15.2).

        None when the workspace declares no ``[questions]`` section.
        """
        raise NotImplementedError("World.question_entries")


def _git_env() -> Dict[str, str]:
    """``questions_git``'s discipline: no stray GIT_* redirection, never prompt."""
    env = dict(os.environ)
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX"):
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    return env


def _wait_exited(pid: int, timeout_s: float) -> bool:
    """Wait until child ``pid`` has exited, WITHOUT reaping it; False on timeout.

    ``WNOWAIT`` leaves the zombie in place, so the pid (which is also the
    process group id of a ``start_new_session`` child) stays reserved until the
    caller reaps it: a signal sent to the group in between can never reach a
    recycled pid (§7.2).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None:
                return True
        except ChildProcessError:   # already reaped by someone else
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.02)


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


class _TailReader(threading.Thread):
    """Drains a pipe, keeping only its last ``limit`` bytes."""

    def __init__(self, stream: Any, limit: int) -> None:
        super().__init__(daemon=True)
        self._stream = stream
        self._limit = limit
        self._buf = bytearray()

    def run(self) -> None:
        try:
            while True:
                chunk = self._stream.read1(65536) if hasattr(self._stream, "read1") \
                    else self._stream.read(65536)
                if not chunk:
                    break
                self._buf.extend(chunk)
                if len(self._buf) > self._limit:
                    del self._buf[: len(self._buf) - self._limit]
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._stream.close()
            except OSError:
                pass

    def text(self) -> str:
        return bytes(self._buf[-self._limit:]).decode("utf-8", errors="replace")


class RealWorld(World):
    """The production effects."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def git(self, args: Sequence[str], cwd: str, timeout_s: float = 30.0) -> CommandResult:
        argv = ["git", "-c", "core.hooksPath=/dev/null", "-C", cwd, *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                                  env=_git_env(), stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return CommandResult(rc=None, error=f"git timed out after {timeout_s:g}s")
        except OSError as exc:
            return CommandResult(rc=None, error=f"git could not run: {exc}")
        return CommandResult(rc=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    def run_check(self, command: str, cwd: str, env: Dict[str, str],
                  timeout_s: float) -> CheckRun:
        """``/bin/sh -c`` in its own process group, the whole group killed at the timeout.

        After the shell exits, whatever is left of its group is killed too
        (checks are read-only and ``KillMode=process`` would otherwise leave
        their children running, §7.2). At the timeout the group gets SIGTERM,
        then SIGKILL once the shell has exited or after the 5 s grace. The
        group is always signalled before the leader is reaped (see
        :func:`_wait_exited`).
        """
        started = self.now()
        t0 = time.monotonic()
        try:
            proc = subprocess.Popen(
                ["/bin/sh", "-c", command], cwd=cwd, env=env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            return CheckRun(rc=None, started_at=started, duration_s=time.monotonic() - t0,
                            error=f"cannot start: {exc}")
        reader = _TailReader(proc.stdout, CHECK_OUTPUT_TAIL_BYTES)
        reader.start()
        pgid = proc.pid
        timed_out = not _wait_exited(pgid, timeout_s)
        if timed_out:
            _signal_group(pgid, signal.SIGTERM)
            _wait_exited(pgid, CHECK_KILL_GRACE_S)
        # Kill the group BEFORE reaping the leader: the unreaped zombie keeps
        # the pid/pgid reserved, so the signal cannot hit a recycled group.
        _signal_group(pgid, signal.SIGKILL)
        returncode = proc.wait()
        rc: Optional[int] = None if timed_out else returncode
        reader.join(timeout=5)
        duration = time.monotonic() - t0
        error = f"timed out after {timeout_s:g}s" if timed_out else None
        return CheckRun(rc=rc, started_at=started, duration_s=duration,
                        output_tail=reader.text(), timed_out=timed_out, error=error)

    def profile_env(self, name: str) -> Dict[str, str]:
        return asl.resolve_profile(name)

    # -- 50-05: read-only tmux, handle, process, clock and queue reads --

    def tmux_has_session(self, name: str) -> bool:
        return asl.tmux_has_session(name)

    def _tmux_read(self, args: Sequence[str]) -> Optional[str]:
        argv = [TMUX_BIN, *args]
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=TMUX_READ_TIMEOUT_S,
                                  stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return proc.stdout if proc.returncode == 0 else None

    def tmux_display(self, name: str, fields: Sequence[str]) -> Optional[Dict[str, str]]:
        fmt = "\t".join("#{%s}" % f for f in fields)
        out = self._tmux_read(["display-message", "-p", "-t", f"={asl.tmux_session_name(name)}:", fmt])
        if out is None:
            return None
        values = out.rstrip("\n").split("\t")
        if len(values) != len(fields):
            return None
        return dict(zip(fields, values))

    def capture_pane(self, name: str) -> Optional[str]:
        return self._tmux_read(["capture-pane", "-p", "-t", f"={asl.tmux_session_name(name)}:"])

    def list_handles(self) -> List[Dict[str, Any]]:
        return asl.list_handles()

    def lifecycle_events(self, name: str) -> List[Dict[str, Any]]:
        path = asl.SPAWN_DIR / f"{name}{LIFECYCLE_SUFFIX}"
        events: List[Dict[str, Any]] = []
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            return events
        for line in raw.splitlines():
            try:
                ev = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue        # a partial tail line of a live append, or garbage
            if isinstance(ev, dict):
                events.append(ev)
        return events

    def amux_names(self) -> List[str]:
        return sorted(asl.list_amux_names())

    def process_work(self, pane_pid: int) -> "ProcessWork":
        kah = importlib.import_module("keep_awake_hook")
        candidates = [pane_pid, *kah.descendants(pane_pid)]
        owner = next((p for p in candidates if kah._looks_like_claude(p)), None)
        if owner is None:
            return ProcessWork(owner_pid=None, tool_shell=None)
        tool, _parked = kah.running_work(owner)
        return ProcessWork(owner_pid=owner, tool_shell=bool(tool))

    def clock_sample(self) -> "ClockSample":
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as f:
                boot_id: Optional[str] = f.read().strip() or None
        except OSError:
            boot_id = None
        boottime_clock = getattr(time, "CLOCK_BOOTTIME", None)
        boottime = time.clock_gettime(boottime_clock) if boottime_clock is not None else time.monotonic()
        return ClockSample(boot_id=boot_id, wall=time.time(), boottime=boottime,
                           monotonic=time.monotonic())

    def local_timezone(self) -> Any:
        return system_timezone()

    def question_entries(self, workspace: str) -> Optional["QuestionsSnapshot"]:
        qs = importlib.import_module("questions_store")
        # The workspace's own .claude/roles.toml, found by walking up from it. Passed
        # explicitly so a CLAUDE_PROJECT_DIR in the caller's environment (a tick run
        # by hand from a Claude session) cannot point the reader at another queue.
        roles_path = None
        for d in (Path(workspace).resolve(), *Path(workspace).resolve().parents):
            if (d / ".claude" / "roles.toml").is_file():
                roles_path = d / ".claude" / "roles.toml"
                break
        if roles_path is None:
            return None
        store = qs.open_store(workspace, roles_path=roles_path)
        if store is None:
            return None
        fmt = store.fmt
        entries: Dict[str, QuestionEntry] = {}
        texts: Dict[str, List[str]] = {}
        for entry in store.all_entries():
            body = ""
            if entry.path is not None:
                key = str(entry.path)
                if key not in texts:
                    try:
                        texts[key] = Path(entry.path).read_text(encoding="utf-8").split("\n")
                    except (OSError, UnicodeDecodeError):
                        texts[key] = []
                body = "\n".join(texts[key][entry.start:entry.end]).strip()[:QUESTION_TEXT_MAX]
            entries[entry.qid] = QuestionEntry(qid=entry.qid, status=entry.status, text=body,
                                               path=str(entry.path) if entry.path else None)
        return QuestionsSnapshot(
            open_word=fmt.open_status.lower(),
            resolved_words=tuple(s.lower() for s in fmt.status[1:] if s.lower() != fmt.withdrawn.lower()),
            withdrawn_word=fmt.withdrawn.lower(),
            entries=entries,
        )


# =============================================================================
# Value grammar (§5.1) and names (§3, P0-1)
# =============================================================================

_DURATION_RE = re.compile(r"^(?:\d+[smhd])+$")
_DURATION_PART_RE = re.compile(r"(\d+)([smhd])")
_DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": DAY_S}
_UTC_TIME_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?"
    r"(Z|z|[+-]\d{2}:?\d{2})$"
)
_TOD_RE = re.compile(r"^(\d{2}):(\d{2})Z$")
_DAILY_WINDOW_RE = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})Z$")
STEP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RUN_ID_RE = re.compile(r"^[a-z0-9-]+$")
WORKSPACE_BASENAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
STARTUP_DIALOG_ID_RE = re.compile(r"^[a-z0-9-]+$")


def parse_duration(value: Any) -> int:
    """``90s``, ``20m``, ``1h30m``, ``7d`` → seconds. Raises ``ValueError``."""
    if not isinstance(value, str) or not _DURATION_RE.match(value):
        raise ValueError(f"not a duration (e.g. 90s, 20m, 1h30m, 7d): {value!r}")
    return sum(int(n) * _DURATION_UNITS[u] for n, u in _DURATION_PART_RE.findall(value))


def format_duration(seconds: int) -> str:
    """The inverse of :func:`parse_duration` (largest units first)."""
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    out = []
    for unit, size in (("d", DAY_S), ("h", 3600), ("m", 60), ("s", 1)):
        n, seconds = divmod(seconds, size)
        if n:
            out.append(f"{n}{unit}")
    return "".join(out)


def parse_utc_time(value: Any) -> datetime:
    """ISO-8601 with ``Z`` or an offset → aware UTC datetime. Raises ``ValueError``.

    A YAML-typed aware datetime (unquoted timestamps) is accepted; a naive one
    is not (the grammar requires ``Z`` or an offset).
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError(f"utc_time needs Z or an offset: {value.isoformat()}")
        return value.astimezone(UTC)
    if not isinstance(value, str):
        raise ValueError(f"not a utc_time: {value!r}")
    m = _UTC_TIME_RE.match(value.strip())
    if not m:
        raise ValueError(f"not a utc_time (ISO-8601 with Z or an offset): {value!r}")
    y, mo, d, h, mi, s, frac, tz = m.groups()
    micro = int((frac or "0")[:6].ljust(6, "0"))
    if tz in ("Z", "z"):
        offset = timedelta(0)
    else:
        sign = 1 if tz[0] == "+" else -1
        digits = tz[1:].replace(":", "")
        offset = sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(s or 0), micro,
                      tzinfo=timezone(offset))
    except ValueError as exc:
        raise ValueError(f"not a utc_time: {value!r} ({exc})") from None
    return dt.astimezone(UTC)


def _hm(h: str, m: str, text: str) -> int:
    hh, mm = int(h), int(m)
    if hh > 23 or mm > 59:
        raise ValueError(f"time of day out of range: {text!r}")
    return hh * 3600 + mm * 60


def parse_tod(value: Any) -> int:
    """``HH:MMZ`` → seconds since UTC midnight. Raises ``ValueError``."""
    m = _TOD_RE.match(value) if isinstance(value, str) else None
    if not m:
        raise ValueError(f"not a time of day (HH:MMZ): {value!r}")
    return _hm(m.group(1), m.group(2), value)


@dataclass(frozen=True)
class DailyWindow:
    """``HH:MM-HH:MMZ``, UTC; may cross midnight (§5.1)."""

    start_s: int
    end_s: int
    text: str = ""

    @property
    def length_s(self) -> int:
        return (self.end_s - self.start_s) % DAY_S

    @property
    def crosses_midnight(self) -> bool:
        return self.end_s < self.start_s

    def offset(self, t: datetime) -> Optional[int]:
        """Seconds since the start of the window instance containing ``t``, or None."""
        x = (tod_seconds(t) - self.start_s) % DAY_S
        return x if x < self.length_s else None

    def contains(self, t: datetime) -> bool:
        return self.offset(t) is not None

    def coverage_s(self, a: datetime, b: datetime) -> int:
        """Total seconds of [a, b] that fall inside the daily window."""
        a, b = require_aware(a), require_aware(b)
        if b <= a:
            return 0
        total = 0.0
        d = a.date() - timedelta(days=1)
        while d <= b.date():
            start = _midnight(d) + timedelta(seconds=self.start_s)
            end = start + timedelta(seconds=self.length_s)
            lo, hi = max(a, start), min(b, end)
            if hi > lo:
                total += (hi - lo).total_seconds()
            d += timedelta(days=1)
        return int(total)

    def next_open(self, t: datetime) -> datetime:
        """The start of the window instance containing ``t``, else of the next one."""
        off = self.offset(t)
        if off is not None:
            return require_aware(t) - timedelta(seconds=off)
        ahead = (self.start_s - tod_seconds(t)) % DAY_S
        return require_aware(t).replace(microsecond=0) + timedelta(seconds=ahead)


def parse_daily_window(value: Any) -> DailyWindow:
    """``HH:MM-HH:MMZ`` → :class:`DailyWindow`. Raises ``ValueError``."""
    m = _DAILY_WINDOW_RE.match(value) if isinstance(value, str) else None
    if not m:
        raise ValueError(f"not a daily window (HH:MM-HH:MMZ): {value!r}")
    start = _hm(m.group(1), m.group(2), value)
    end = _hm(m.group(3), m.group(4), value)
    if start == end:
        raise ValueError(f"daily window has zero length: {value!r}")
    return DailyWindow(start, end, value)


def is_step_id(value: Any) -> bool:
    return isinstance(value, str) and bool(STEP_ID_RE.match(value))


def sanitise(value: Any) -> str:
    """§3: lowercase; runs outside ``[a-z0-9]`` → ``-``; strip ``-``. Empty → ``ValueError``."""
    out = re.sub(r"[^a-z0-9]+", "-", str(value).lower()).strip("-")
    if not out:
        raise ValueError(f"sanitises to nothing: {value!r}")
    return out


def session_suffix(epic: str, role: str, step: Optional[str], gen: int) -> str:
    """``<epic>-<role>[-<step>]-g<gen>`` (P0-1), every part sanitised."""
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 1:
        raise ValueError(f"gen must be an int >= 1: {gen!r}")
    parts = [sanitise(epic), sanitise(role)]
    if step is not None:
        parts.append(sanitise(step))
    return "-".join(parts) + f"-g{gen}"


def worker_dir_name(epic: str, step: str) -> str:
    """The directory basename the tick fixes for a worker step (§14.2): ``<epic>-<step>``."""
    return f"{sanitise(epic)}-{sanitise(step)}"


def session_name(dir_basename: str, suffix: str) -> str:
    """``basename(<dir>)-<suffix>``, as ``amux_spawn_lib.pick_free_name`` composes it."""
    return asl.pick_free_name(dir_basename, suffix)


# =============================================================================
# Ledger (§5.2–§5.5)
# =============================================================================

class LedgerIssue:
    """One validation error or warning: ``category``, the place (``where``), the text."""

    __slots__ = ("category", "where", "message")

    def __init__(self, category: str, where: str, message: str) -> None:
        self.category = category
        self.where = where
        self.message = message

    def __str__(self) -> str:
        return f"{self.where}: {self.message}" if self.where else self.message

    def __repr__(self) -> str:
        return f"LedgerIssue({self.category!r}, {self.where!r}, {self.message!r})"


# Error categories (§5.5) and warning kinds.
E_SCHEMA = "schema"
E_GRAPH = "graph"
E_FILES = "files"
E_NAMES = "names"
E_APPROVALS = "approvals"
E_TIMING = "timing"
E_RESOURCES = "resources"
E_REPOS = "repos"
E_COMPACTION = "compaction"
E_STARTUP_DIALOGS = "startup_dialogs"
W_DRIVER_STEP_NO_CHECK = "driver_step_no_check"
W_SSH_NO_TIMEOUT = "ssh_without_connect_timeout"
W_CONTEXT_FALLBACK = "context_window_default_fallback"


class LedgerError(Exception):
    """The ledger failed validation. ``errors`` and ``warnings`` are :class:`LedgerIssue` lists."""

    def __init__(self, path: str, errors: List[LedgerIssue],
                 warnings: Optional[List[LedgerIssue]] = None) -> None:
        self.path = path
        self.errors = list(errors)
        self.warnings = list(warnings or [])
        lines = "\n  ".join(str(e) for e in self.errors)
        super().__init__(f"{path}: {len(self.errors)} error(s):\n  {lines}")


@dataclass
class Defaults:
    check_timeout_s: int = DEFAULT_CHECK_TIMEOUT_S
    check_every_s: int = DEFAULT_CHECK_EVERY_S
    timeout_s: int = DEFAULT_STEP_TIMEOUT_S
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    warn_before_s: int = DEFAULT_WARN_BEFORE_S


@dataclass
class Role:
    """§5.3. ``expected_model_id`` and the compaction points are derived at validation."""

    key: str
    model: str
    effort: str
    profile: Optional[str] = None
    cap: int = DEFAULT_CAP_TOKENS
    bootstrap_tokens: int = DEFAULT_BOOTSTRAP_TOKENS
    keep_warm: bool = DEFAULT_KEEP_WARM
    stuck_after_s: int = DEFAULT_STUCK_AFTER_S
    pinned_env: Dict[str, str] = field(default_factory=dict)
    prompt: Optional[str] = None
    epic_prompt: Optional[str] = None
    expected_model_id: Optional[str] = None
    context_window: Optional[int] = None
    context_window_source: Optional[str] = None
    auto_compact_point: Optional[int] = None
    idle_compact_point: Optional[int] = None


@dataclass
class CommandSpec:
    """A ``check`` or ``trigger``: the command, its timeout and its rate limit."""

    run: str
    timeout_s: int
    every_s: int


@dataclass
class After:
    step: str
    delay_s: int


@dataclass
class Every:
    """Recurrence (§6.3). ``series`` is the ``{period, from, deadline, warn_before}`` form."""

    period_s: int
    series: bool = False
    from_: Optional[datetime] = None
    deadline_s: Optional[int] = None
    warn_before_s: Optional[int] = None


@dataclass
class CheckWindow:
    """§7.3: ``{daily, min}`` or ``{from, to}`` (relative to the step's anchor)."""

    daily: Optional[DailyWindow] = None
    min_s: int = 0
    from_s: Optional[int] = None
    to_s: Optional[int] = None

    @property
    def span_s(self) -> int:
        if self.daily is not None:
            return self.daily.length_s
        return int((self.to_s or 0) - (self.from_s or 0))


@dataclass
class WorkerRepo:
    path: str
    base_ref: str
    name: str


@dataclass
class Worker:
    implementer: str
    reviewer: str
    repos: List[WorkerRepo]


ROLE_KIND_DRIVER = "driver"
ROLE_KIND_WORKER = "worker"


@dataclass
class Step:
    """§5.4, defaults applied. ``role_kind`` None = a watch step."""

    id: str
    key: str
    index: int
    card: str
    title: str = ""
    phase: Optional[str] = None
    needs: List[str] = field(default_factory=list)
    after: Optional[After] = None
    not_before: Optional[datetime] = None
    not_after: Optional[datetime] = None
    warn_before_s: int = DEFAULT_WARN_BEFORE_S
    window: Optional[DailyWindow] = None
    latest_start_s: Optional[int] = None
    duration_s: Optional[int] = None
    check_window: Optional[CheckWindow] = None
    every: Optional[Every] = None
    at_s: Optional[int] = None
    trigger: Optional[CommandSpec] = None
    group: Optional[str] = None
    resource: Dict[str, int] = field(default_factory=dict)
    approval: Optional[str] = None
    role_kind: Optional[str] = None
    worker: Optional[Worker] = None
    check: Optional[CommandSpec] = None
    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    timeout_s: int = DEFAULT_STEP_TIMEOUT_S

    @property
    def is_watch(self) -> bool:
        return self.role_kind is None

    @property
    def is_recurring(self) -> bool:
        return self.every is not None

    @property
    def target_role(self) -> Optional[str]:
        """The role a dispatch of this step goes to (a worker step's first event: the implementer)."""
        if self.role_kind == ROLE_KIND_DRIVER:
            return "driver"
        if self.worker is not None:
            return self.worker.implementer
        return None

    def latest_start_offset_s(self) -> Optional[int]:
        """Latest start as seconds after the window opens (``latest_start`` or ``duration``)."""
        if self.window is None:
            return None
        if self.latest_start_s is not None:
            return (self.latest_start_s - self.window.start_s) % DAY_S
        if self.duration_s is not None:
            return self.window.length_s - self.duration_s
        return None


@dataclass
class StartupDialog:
    """§5.2 ``startup_dialogs`` entry: the tick may answer this dialog with ``key``."""

    id: str
    match: str
    key: str
    pattern: Any = None

    def matches(self, pane_text: str) -> bool:
        return bool(self.pattern is not None and self.pattern.search(pane_text))


@dataclass
class Approval:
    id: str
    conditions: List[str]
    order: int


@dataclass
class Ledger:
    path: str
    sha256: str
    version: int
    epic: str
    epic_key: str
    run_id: str
    workspace: str
    plan_root: str
    plan_dir: str
    worktree_root: str
    worktree_root_default: bool
    defaults: Defaults
    notify_role: Optional[str]
    roles: Dict[str, Role]
    resources: Dict[str, int]
    startup_dialogs: List[StartupDialog]
    approvals: Dict[str, Approval]
    steps: List[Step]
    warnings: List[LedgerIssue] = field(default_factory=list)

    def step(self, step_id: str) -> Step:
        for s in self.steps:
            if s.id == step_id:
                return s
        raise KeyError(step_id)

    def has_step(self, step_id: str) -> bool:
        return any(s.id == step_id for s in self.steps)

    @property
    def steps_by_id(self) -> Dict[str, Step]:
        return {s.id: s for s in self.steps}

    def approvals_in_order(self) -> List[Approval]:
        return sorted(self.approvals.values(), key=lambda a: a.order)

    def resolve_path(self, value: str) -> str:
        """§5.1: a relative path resolves against ``<workspace>/<plan_root>``."""
        return value if os.path.isabs(value) else os.path.normpath(os.path.join(self.plan_dir, value))


_TOP_KEYS = {
    "version", "epic", "run_id", "workspace", "plan_root", "worktree_root", "defaults",
    "notify", "roles", "resources", "startup_dialogs", "approvals", "steps",
}
_TOP_REQUIRED = ("version", "epic", "workspace", "plan_root", "roles", "steps")
_DEFAULTS_KEYS = {"check_timeout", "check_every", "timeout", "max_attempts", "warn_before"}
_NOTIFY_KEYS = {"role"}
_ROLE_KEYS = {
    "model", "effort", "profile", "cap", "bootstrap_tokens", "keep_warm", "stuck_after",
    "pinned_env", "prompt", "epic_prompt",
}
_STEP_KEYS = {
    "id", "title", "phase", "card", "needs", "after", "not_before", "not_after",
    "warn_before", "window", "latest_start", "duration", "check_window", "every", "at",
    "trigger", "group", "resource", "approval", "role", "check", "max_attempts", "timeout",
}
_SSH_RE = re.compile(r"(?<![\w.-])ssh(?![\w.-])")
_SSH_SCAN_BYTES = 256 * 1024


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class _Validator:
    """Collects issues while the ledger is parsed; never raises."""

    def __init__(self) -> None:
        self.errors: List[LedgerIssue] = []
        self.warnings: List[LedgerIssue] = []

    def err(self, category: str, where: str, message: str) -> None:
        self.errors.append(LedgerIssue(category, where, message))

    def warn(self, kind: str, where: str, message: str) -> None:
        self.warnings.append(LedgerIssue(kind, where, message))

    def unknown_keys(self, obj: Dict[str, Any], allowed: Iterable[str], where: str) -> None:
        for k in obj:
            if k not in allowed:
                self.err(E_SCHEMA, f"{where}.{k}" if where else str(k), "unknown key")

    def string(self, obj: Dict[str, Any], key: str, where: str, required: bool = False,
               hint: str = "") -> Optional[str]:
        if key not in obj or obj[key] is None:
            if required:
                self.err(E_SCHEMA, f"{where}.{key}" if where else key, "required")
            return None
        v = obj[key]
        if not isinstance(v, str) or not v.strip():
            self.err(E_SCHEMA, f"{where}.{key}" if where else key,
                     f"must be a non-empty string, got {v!r}{hint}")
            return None
        return v

    def integer(self, obj: Dict[str, Any], key: str, where: str, default: Optional[int],
                minimum: int = 0) -> Optional[int]:
        if key not in obj or obj[key] is None:
            return default
        v = obj[key]
        if not _is_int(v) or v < minimum:
            self.err(E_SCHEMA, f"{where}.{key}", f"must be an int >= {minimum}, got {v!r}")
            return default
        return v

    def duration(self, obj: Dict[str, Any], key: str, where: str,
                 default: Optional[int], positive: bool = False) -> Optional[int]:
        """A duration; with ``positive`` a zero (``0s``) is an error and yields ``default``."""
        if key not in obj or obj[key] is None:
            return default
        try:
            seconds = parse_duration(obj[key])
        except ValueError as exc:
            self.err(E_SCHEMA, f"{where}.{key}", str(exc))
            return default
        if positive and seconds <= 0:
            self.err(E_SCHEMA, f"{where}.{key}", f"must be > 0, got {obj[key]!r}")
            return default
        return seconds

    def utc_time(self, obj: Dict[str, Any], key: str, where: str) -> Optional[datetime]:
        if key not in obj or obj[key] is None:
            return None
        try:
            return parse_utc_time(obj[key])
        except ValueError as exc:
            self.err(E_SCHEMA, f"{where}.{key}", str(exc))
            return None

    def mapping(self, obj: Dict[str, Any], key: str, where: str) -> Optional[Dict[str, Any]]:
        if key not in obj or obj[key] is None:
            return None
        v = obj[key]
        if not isinstance(v, dict):
            self.err(E_SCHEMA, f"{where}.{key}" if where else key, f"must be a map, got {type(v).__name__}")
            return None
        return v

    def id_list(self, obj: Dict[str, Any], key: str, where: str) -> List[str]:
        if key not in obj or obj[key] is None:
            return []
        v = obj[key]
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            self.err(E_SCHEMA, f"{where}.{key}", f"must be a list of ids, got {v!r}")
            return []
        return list(v)


def _yaml_hint(value: Any) -> str:
    if _is_int(value):
        return " (quote it: YAML reads an unquoted 037 as the octal number 31)"
    return ""


def _parse_command(v: _Validator, raw: Any, where: str, defaults: Defaults,
                   allowed: Sequence[str]) -> Optional[CommandSpec]:
    """``command`` or ``{run, timeout?, every?}`` (keys limited to ``allowed``)."""
    if raw is None:
        return None
    if isinstance(raw, str):
        if not raw.strip():
            v.err(E_SCHEMA, where, "empty command")
            return None
        return CommandSpec(raw, defaults.check_timeout_s, defaults.check_every_s)
    if isinstance(raw, dict):
        v.unknown_keys(raw, ("run",) + tuple(allowed), where)
        run = v.string(raw, "run", where, required=True)
        timeout = v.duration(raw, "timeout", where, defaults.check_timeout_s, positive=True) \
            if "timeout" in allowed else defaults.check_timeout_s
        every = v.duration(raw, "every", where, defaults.check_every_s, positive=True)
        if run is None:
            return None
        return CommandSpec(run, timeout if timeout is not None else defaults.check_timeout_s,
                           every if every is not None else defaults.check_every_s)
    v.err(E_SCHEMA, where, f"must be a command string or a map, got {type(raw).__name__}")
    return None


def _parse_defaults(v: _Validator, raw: Optional[Dict[str, Any]]) -> Defaults:
    d = Defaults()
    if raw is None:
        return d
    v.unknown_keys(raw, _DEFAULTS_KEYS, "defaults")
    d.check_timeout_s = v.duration(raw, "check_timeout", "defaults", d.check_timeout_s, positive=True) \
        or d.check_timeout_s
    d.check_every_s = v.duration(raw, "check_every", "defaults", d.check_every_s, positive=True) \
        or d.check_every_s
    d.timeout_s = v.duration(raw, "timeout", "defaults", d.timeout_s, positive=True) or d.timeout_s
    d.max_attempts = v.integer(raw, "max_attempts", "defaults", d.max_attempts, minimum=1) or d.max_attempts
    wb = v.duration(raw, "warn_before", "defaults", d.warn_before_s)
    d.warn_before_s = d.warn_before_s if wb is None else wb
    return d


def _parse_role(v: _Validator, key: str, raw: Any, plan_dir: str, workspace: str) -> Optional[Role]:
    where = f"roles.{key}"
    if not isinstance(raw, dict):
        v.err(E_SCHEMA, where, f"must be a map, got {type(raw).__name__}")
        return None
    v.unknown_keys(raw, _ROLE_KEYS, where)
    model = v.string(raw, "model", where, required=True)
    effort = v.string(raw, "effort", where, required=True)
    profile = v.string(raw, "profile", where)
    cap = v.integer(raw, "cap", where, DEFAULT_CAP_TOKENS, minimum=1)
    boot = v.integer(raw, "bootstrap_tokens", where, DEFAULT_BOOTSTRAP_TOKENS, minimum=0)
    keep_warm = raw.get("keep_warm", DEFAULT_KEEP_WARM)
    if not isinstance(keep_warm, bool):
        v.err(E_SCHEMA, f"{where}.keep_warm", f"must be true or false, got {keep_warm!r}")
        keep_warm = DEFAULT_KEEP_WARM
    stuck = v.duration(raw, "stuck_after", where, DEFAULT_STUCK_AFTER_S, positive=True)
    pinned: Dict[str, str] = {}
    pinned_raw = v.mapping(raw, "pinned_env", where)
    for k, val in (pinned_raw or {}).items():
        if k not in PINNABLE_ENV_KEYS:
            v.err(E_SCHEMA, f"{where}.pinned_env.{k}",
                  f"not pinnable (allowed: {', '.join(PINNABLE_ENV_KEYS)})")
            continue
        if not isinstance(val, str):
            v.err(E_SCHEMA, f"{where}.pinned_env.{k}", f"must be a string (quote it), got {val!r}")
            continue
        pinned[k] = val
    prompt = v.string(raw, "prompt", where)
    if prompt is not None and not os.path.isabs(prompt):
        prompt = os.path.normpath(os.path.join(plan_dir, prompt))
    epic_prompt = v.string(raw, "epic_prompt", where)
    if epic_prompt is not None and not os.path.isabs(epic_prompt):
        epic_prompt = os.path.normpath(os.path.join(workspace, epic_prompt))
    if model is None or effort is None:
        return None
    return Role(key=key, model=model, effort=effort, profile=profile,
                cap=cap if cap is not None else DEFAULT_CAP_TOKENS,
                bootstrap_tokens=boot if boot is not None else DEFAULT_BOOTSTRAP_TOKENS,
                keep_warm=keep_warm,
                stuck_after_s=stuck if stuck is not None else DEFAULT_STUCK_AFTER_S,
                pinned_env=pinned, prompt=prompt, epic_prompt=epic_prompt)


def _parse_every(v: _Validator, raw: Any, where: str, defaults: Defaults) -> Optional[Every]:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            period = parse_duration(raw)
        except ValueError as exc:
            v.err(E_SCHEMA, where, str(exc))
            return None
        if period <= 0:
            v.err(E_SCHEMA, where, "period must be > 0")
            return None
        return Every(period_s=period)
    if isinstance(raw, dict):
        v.unknown_keys(raw, ("period", "from", "deadline", "warn_before"), where)
        if "period" not in raw:
            v.err(E_SCHEMA, f"{where}.period", "required")
            return None
        period = v.duration(raw, "period", where, None, positive=True)
        if period is None:
            return None
        start = v.utc_time(raw, "from", where)
        deadline = v.duration(raw, "deadline", where, None)
        warn = v.duration(raw, "warn_before", where, None)
        if deadline is not None and deadline <= period:
            v.err(E_TIMING, f"{where}.deadline",
                  f"{format_duration(deadline)} must be longer than the period {format_duration(period)}")
        return Every(period_s=period, series=True, from_=start, deadline_s=deadline,
                     warn_before_s=warn if warn is not None else defaults.warn_before_s)
    v.err(E_SCHEMA, where, f"must be a duration or a map, got {type(raw).__name__}")
    return None


def _parse_check_window(v: _Validator, raw: Any, where: str) -> Optional[CheckWindow]:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        v.err(E_SCHEMA, where, f"must be a map, got {type(raw).__name__}")
        return None
    if "daily" in raw:
        v.unknown_keys(raw, ("daily", "min"), where)
        try:
            daily = parse_daily_window(raw["daily"])
        except ValueError as exc:
            v.err(E_SCHEMA, f"{where}.daily", str(exc))
            return None
        if "min" not in raw:
            v.err(E_SCHEMA, f"{where}.min", "required with daily")
            return None
        minimum = v.duration(raw, "min", where, None, positive=True)
        if minimum is None:
            return None
        cw = CheckWindow(daily=daily, min_s=minimum)
    elif "from" in raw or "to" in raw:
        v.unknown_keys(raw, ("from", "to"), where)
        if "from" not in raw or "to" not in raw:
            v.err(E_SCHEMA, where, "needs both from and to")
            return None
        a = v.duration(raw, "from", where, None)
        b = v.duration(raw, "to", where, None)
        if a is None or b is None:
            return None
        if b <= a:
            v.err(E_TIMING, f"{where}.to", "must be after from")
            return None
        cw = CheckWindow(from_s=a, to_s=b)
    else:
        v.err(E_SCHEMA, where, "must be {daily, min} or {from, to}")
        return None
    if cw.span_s < MIN_CHECK_WINDOW_S:
        v.err(E_TIMING, where,
              f"span {format_duration(cw.span_s)} is under 20m: shorter checks belong in the "
              "action's step card or on the epic's server runners (P0-10)")
    return cw


def _parse_worker(v: _Validator, raw: Any, where: str) -> Optional[Worker]:
    if not isinstance(raw, dict):
        v.err(E_SCHEMA, where, f"must be a map, got {type(raw).__name__}")
        return None
    v.unknown_keys(raw, ("implementer", "reviewer", "repos"), where)
    impl = v.string(raw, "implementer", where, required=True)
    rev = v.string(raw, "reviewer", where, required=True)
    repos_raw = raw.get("repos")
    repos: List[WorkerRepo] = []
    if not isinstance(repos_raw, list) or not repos_raw:
        v.err(E_SCHEMA, f"{where}.repos", "must be a non-empty list of {path, base_ref, name?}")
    else:
        for i, r in enumerate(repos_raw):
            rw = f"{where}.repos[{i}]"
            if not isinstance(r, dict):
                v.err(E_SCHEMA, rw, "must be a map {path, base_ref, name?}")
                continue
            v.unknown_keys(r, ("path", "base_ref", "name"), rw)
            path = v.string(r, "path", rw, required=True)
            base = v.string(r, "base_ref", rw, required=True)
            name = v.string(r, "name", rw)
            if path is None or base is None:
                continue
            repos.append(WorkerRepo(path=path, base_ref=base,
                                    name=name if name is not None else os.path.basename(path.rstrip("/"))))
    if impl is None or rev is None:
        return None
    return Worker(implementer=impl, reviewer=rev, repos=repos)


def _parse_step(v: _Validator, index: int, raw: Any, defaults: Defaults,
                plan_dir: str) -> Optional[Step]:
    if not isinstance(raw, dict):
        v.err(E_SCHEMA, f"steps[{index}]", f"must be a map, got {type(raw).__name__}")
        return None
    sid = raw.get("id")
    if not is_step_id(sid):
        v.err(E_SCHEMA, f"steps[{index}].id", f"must match [A-Za-z0-9][A-Za-z0-9._-]*, got {sid!r}")
        return None
    where = f"steps.{sid}"
    v.unknown_keys(raw, _STEP_KEYS, where)
    try:
        key = sanitise(sid)
    except ValueError:
        v.err(E_NAMES, f"{where}.id", "sanitises to nothing")
        return None
    card = v.string(raw, "card", where, required=True)
    title = v.string(raw, "title", where) or ""
    phase = v.string(raw, "phase", where)
    step = Step(id=sid, key=key, index=index,
                card=(os.path.normpath(os.path.join(plan_dir, card)) if card and not os.path.isabs(card)
                      else (card or "")),
                title=title, phase=phase,
                warn_before_s=defaults.warn_before_s, max_attempts=defaults.max_attempts,
                timeout_s=defaults.timeout_s)
    step.needs = v.id_list(raw, "needs", where)
    after_raw = v.mapping(raw, "after", where)
    if after_raw is not None:
        v.unknown_keys(after_raw, ("step", "delay"), f"{where}.after")
        a_step = v.string(after_raw, "step", f"{where}.after", required=True)
        a_delay = v.duration(after_raw, "delay", f"{where}.after", 0)
        if a_step is not None:
            step.after = After(a_step, a_delay or 0)
    step.not_before = v.utc_time(raw, "not_before", where)
    step.not_after = v.utc_time(raw, "not_after", where)
    wb = v.duration(raw, "warn_before", where, None)
    if wb is not None:
        step.warn_before_s = wb
    if raw.get("window") is not None:
        try:
            step.window = parse_daily_window(raw["window"])
        except ValueError as exc:
            v.err(E_SCHEMA, f"{where}.window", str(exc))
    if raw.get("latest_start") is not None:
        try:
            step.latest_start_s = parse_tod(raw["latest_start"])
        except ValueError as exc:
            v.err(E_SCHEMA, f"{where}.latest_start", str(exc))
    step.duration_s = v.duration(raw, "duration", where, None, positive=True)
    step.check_window = _parse_check_window(v, raw.get("check_window"), f"{where}.check_window")
    step.every = _parse_every(v, raw.get("every"), f"{where}.every", defaults)
    if raw.get("at") is not None:
        try:
            step.at_s = parse_tod(raw["at"])
        except ValueError as exc:
            v.err(E_SCHEMA, f"{where}.at", str(exc))
    step.trigger = _parse_command(v, raw.get("trigger"), f"{where}.trigger", defaults, ("every",))
    step.group = v.string(raw, "group", where)
    res_raw = v.mapping(raw, "resource", where)
    for name, units in (res_raw or {}).items():
        if not _is_int(units) or units < 1:
            v.err(E_SCHEMA, f"{where}.resource.{name}", f"must be an int >= 1, got {units!r}")
            continue
        step.resource[str(name)] = units
    step.approval = v.string(raw, "approval", where)
    role = raw.get("role")
    if role is not None:
        if role == ROLE_KIND_DRIVER:
            step.role_kind = ROLE_KIND_DRIVER
        elif isinstance(role, dict) and set(role) == {"worker"}:
            step.role_kind = ROLE_KIND_WORKER
            step.worker = _parse_worker(v, role["worker"], f"{where}.role.worker")
        else:
            v.err(E_SCHEMA, f"{where}.role", f"must be 'driver' or {{worker: ...}}, got {role!r}")
    step.check = _parse_command(v, raw.get("check"), f"{where}.check", defaults, ("timeout", "every"))
    step.max_attempts = v.integer(raw, "max_attempts", where, defaults.max_attempts, minimum=1) \
        or defaults.max_attempts
    step.timeout_s = v.duration(raw, "timeout", where, defaults.timeout_s, positive=True) \
        or defaults.timeout_s

    # `at` implies every 1d; with `every` it needs a plain whole-day period.
    if step.at_s is not None:
        if step.every is None:
            step.every = Every(period_s=DAY_S)
        elif step.every.series or step.every.period_s % DAY_S != 0:
            v.err(E_TIMING, f"{where}.at", "needs `every` to be a plain whole number of days (1d, 2d, …)")
    if step.is_watch and step.check is None and "role" not in raw:
        v.err(E_SCHEMA, f"{where}.check", "required for a watch step (no role)")
    return step


def _validate_timing(v: _Validator, step: Step) -> None:
    where = f"steps.{step.id}"
    if step.latest_start_s is not None and step.duration_s is not None:
        v.err(E_TIMING, where, "both latest_start and duration: use at most one")
        return
    if step.latest_start_s is not None:
        if step.window is None:
            v.err(E_TIMING, f"{where}.latest_start", "outside window (the step has no window)")
        elif not step.window.contains(_midnight(date(2000, 1, 3)) + timedelta(seconds=step.latest_start_s)):
            v.err(E_TIMING, f"{where}.latest_start", f"outside window {step.window.text}")
    if step.duration_s is not None:
        if step.window is None:
            v.err(E_TIMING, f"{where}.duration", "needs a window")
        elif not 0 < step.duration_s <= step.window.length_s:
            v.err(E_TIMING, f"{where}.duration",
                  f"{format_duration(step.duration_s)} leaves no start time inside window {step.window.text}")


def _find_cycles(steps: Dict[str, Step]) -> List[List[str]]:
    """Cycles in the ``needs`` graph (each reported once, by its first node in ledger order)."""
    cycles: List[List[str]] = []
    color: Dict[str, int] = {}
    stack: List[str] = []

    def visit(n: str) -> None:
        color[n] = 1
        stack.append(n)
        for m in steps[n].needs:
            if m not in steps:
                continue
            if color.get(m) == 1:
                cycles.append(stack[stack.index(m):] + [m])
            elif color.get(m) is None:
                visit(m)
        stack.pop()
        color[n] = 2

    for n in steps:
        if color.get(n) is None:
            visit(n)
    return cycles


def _context_mcp_lib() -> Any:
    """Import ``context_mcp_lib`` (§17): from sys.path, else beside this copy.

    Installed: ``~/.claude/hooks/epic_driver_lib.py`` → ``~/.claude/epic-driver/lib``.
    Repo:      ``.claude/hooks/epic_driver_lib.py``   → ``<repo>/context-mcp``.
    """
    try:
        return importlib.import_module("context_mcp_lib")
    except ImportError:
        pass
    here = Path(__file__).resolve().parent
    for cand in (here.parent / "epic-driver" / "lib", here.parent.parent / "context-mcp"):
        if (cand / "context_mcp_lib.py").is_file():
            if str(cand) not in sys.path:
                sys.path.append(str(cand))
            return importlib.import_module("context_mcp_lib")
    raise ImportError("context_mcp_lib not found (installed epic-driver/lib or the repo's context-mcp/)")


def _number(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _validate_compaction(v: _Validator, role: Role, world: World) -> None:
    """§11.6: ``cap < min(auto_point, idle_point)`` whenever either point is known."""
    where = f"roles.{role.key}"
    profile_env: Dict[str, str] = {}
    if role.profile:
        try:
            profile_env = world.profile_env(role.profile)
        except (ValueError, OSError) as exc:
            v.err(E_COMPACTION, f"{where}.profile",
                  f"cannot resolve profile {role.profile!r} for the compaction rule: {exc}")
            return
    role.expected_model_id = asl.expected_model_id(role.model, profile_env)
    try:
        cml = _context_mcp_lib()
    except ImportError as exc:
        v.err(E_COMPACTION, where, f"cannot compute the context window: {exc}")
        return
    window, source = cml.resolve_context_window(role.expected_model_id or role.model)
    role.context_window, role.context_window_source = int(window), source
    if source == "default-fallback":
        v.warn(W_CONTEXT_FALLBACK, where,
               f"no known context window for model {(role.expected_model_id or role.model)!r} "
               f"(role {role.key!r}): the compaction rule uses the default {int(window)} tokens; "
               "add the model to context-mcp/models.json or set CONTEXT_WINDOW_MAP")

    pct_raw = role.pinned_env.get(AUTOCOMPACT_PCT_KEY, profile_env.get(AUTOCOMPACT_PCT_KEY))
    idle_raw = role.pinned_env.get(IDLE_COMPACT_KEY, profile_env.get(IDLE_COMPACT_KEY))
    if pct_raw is not None:
        pct = _number(pct_raw)
        if pct is None or not 0 < pct <= 100:
            v.err(E_COMPACTION, f"{where}.{AUTOCOMPACT_PCT_KEY}", f"not a percentage: {pct_raw!r}")
        else:
            role.auto_compact_point = int(pct / 100.0 * window)
    if idle_raw is not None:
        idle = _number(idle_raw)
        if idle is None or idle <= 0:
            v.err(E_COMPACTION, f"{where}.{IDLE_COMPACT_KEY}", f"not a token count: {idle_raw!r}")
        else:
            role.idle_compact_point = int(idle)
    points = [p for p in (role.auto_compact_point, role.idle_compact_point) if p is not None]
    if points and not role.cap < min(points):
        detail = []
        if role.auto_compact_point is not None:
            detail.append(f"auto {role.auto_compact_point} = {pct_raw}% of {window} ({source})")
        if role.idle_compact_point is not None:
            detail.append(f"idle {role.idle_compact_point}")
        v.err(E_COMPACTION, f"{where}.cap",
              f"cap {role.cap} is not below the compaction point ({'; '.join(detail)}); "
              f"pin {IDLE_COMPACT_KEY} / {AUTOCOMPACT_PCT_KEY} above it in pinned_env (OQ-2)")


def _ssh_without_timeout(text: str) -> bool:
    return bool(_SSH_RE.search(text)) and "ConnectTimeout" not in text \
        and "EPIC_DRIVER_SSH_OPTS" not in text


def _command_script(command: str, plan_dir: str) -> Optional[str]:
    """The file a command runs first, if it is one (``ledger/checks/x.sh …``)."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv:
        return None
    path = argv[0] if os.path.isabs(argv[0]) else os.path.join(plan_dir, argv[0])
    return path if os.path.isfile(path) else None


def _warn_ssh(v: _Validator, spec: CommandSpec, where: str, plan_dir: str) -> None:
    texts = [spec.run]
    script = _command_script(spec.run, plan_dir)
    if script is not None:
        try:
            with open(script, "rb") as f:
                texts.append(f.read(_SSH_SCAN_BYTES).decode("utf-8", errors="replace"))
        except OSError:
            pass
    if any(_ssh_without_timeout(t) for t in texts):
        v.warn(W_SSH_NO_TIMEOUT, where,
               "calls ssh without ConnectTimeout (use $EPIC_DRIVER_SSH_OPTS)")


def _git_cached(world: World, cache: Dict[Tuple[str, Tuple[str, ...]], CommandResult],
                path: str, args: Tuple[str, ...]) -> CommandResult:
    key = (path, args)
    if key not in cache:
        cache[key] = world.git(list(args), path)
    return cache[key]


def _validate_repos(v: _Validator, step: Step, world: World, verify_base_refs: bool,
                    cache: Dict[Tuple[str, Tuple[str, ...]], CommandResult]) -> None:
    if step.worker is None:
        return
    seen_paths: Dict[str, int] = {}
    seen_names: Dict[str, int] = {}
    for i, repo in enumerate(step.worker.repos):
        where = f"steps.{step.id}.role.worker.repos[{i}]"
        if not os.path.isabs(repo.path):
            v.err(E_REPOS, f"{where}.path", f"must be absolute: {repo.path!r}")
            continue
        real = os.path.realpath(repo.path)
        if real in seen_paths:
            v.err(E_REPOS, f"{where}.path", f"duplicate of repos[{seen_paths[real]}] ({real})")
        seen_paths.setdefault(real, i)
        if repo.name in seen_names:
            v.err(E_REPOS, f"{where}.name", f"duplicate name {repo.name!r}")
        seen_names.setdefault(repo.name, i)
        try:
            sanitise(repo.name)
        except ValueError:
            v.err(E_REPOS, f"{where}.name", f"unusable name {repo.name!r}")
        if not os.path.isdir(real):
            v.err(E_REPOS, f"{where}.path", f"not a git work tree (no such directory): {repo.path}")
            continue
        inside = _git_cached(world, cache, real, ("rev-parse", "--is-inside-work-tree"))
        if not inside.ok or inside.stdout.strip() != "true":
            v.err(E_REPOS, f"{where}.path", f"not a git work tree: {repo.path}")
            continue
        if verify_base_refs:
            res = _git_cached(world, cache, real,
                              ("rev-parse", "--verify", "--quiet", f"{repo.base_ref}^{{commit}}"))
            if not res.ok:
                v.err(E_REPOS, f"{where}.base_ref",
                      f"{repo.base_ref!r} does not resolve to a commit in {repo.path}")


def _parse_startup_dialogs(v: _Validator, raw: Any) -> List[StartupDialog]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        v.err(E_SCHEMA, "startup_dialogs", "must be a list of {id, match, key}")
        return []
    out: List[StartupDialog] = []
    seen: set = set()
    for i, entry in enumerate(raw):
        where = f"startup_dialogs[{i}]"
        if not isinstance(entry, dict):
            v.err(E_SCHEMA, where, "must be a map {id, match, key}")
            continue
        v.unknown_keys(entry, ("id", "match", "key"), where)
        did = v.string(entry, "id", where, required=True)
        match = v.string(entry, "match", where, required=True)
        key = v.string(entry, "key", where, required=True)
        if did is not None:
            where = f"startup_dialogs.{did}"
            if not STARTUP_DIALOG_ID_RE.match(did):
                v.err(E_STARTUP_DIALOGS, f"{where}.id", "must match [a-z0-9-]+")
            if did in seen:
                v.err(E_STARTUP_DIALOGS, f"{where}.id", "duplicate id")
            seen.add(did)
        pattern = None
        if match is not None:
            try:
                pattern = re.compile(match)
            except re.error as exc:
                v.err(E_STARTUP_DIALOGS, f"{where}.match", f"does not compile: {exc}")
        if key is not None and key not in STARTUP_DIALOG_KEYS:
            v.err(E_STARTUP_DIALOGS, f"{where}.key",
                  f"must be one of {', '.join(STARTUP_DIALOG_KEYS)}, got {key!r}")
        if did is not None and match is not None and key is not None:
            out.append(StartupDialog(id=did, match=match, key=key, pattern=pattern))
    return out


def _file_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def validate_ledger(path: str, world: Optional[World] = None, *,
                    verify_base_refs: bool = True) -> Tuple[Optional[Ledger], List[LedgerIssue], List[LedgerIssue]]:
    """Parse and validate a ledger: ``(ledger or None, errors, warnings)``.

    The ledger is returned only when there are no errors. ``verify_base_refs``
    runs ``rev-parse --verify <base_ref>^{commit}`` per repo (``enable`` and the
    tick, §5.5); the work-tree test always runs. All git goes through ``world``.
    """
    world = world if world is not None else RealWorld()
    v = _Validator()
    path = os.path.abspath(path)
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as exc:
        v.err(E_FILES, "", f"cannot read the ledger: {exc}")
        return None, v.errors, v.warnings
    if yaml is None:
        v.err(E_SCHEMA, "", "PyYAML is not installed (python3 -c 'import yaml' fails)")
        return None, v.errors, v.warnings
    try:
        raw = yaml.load(data, Loader=_UniqueKeyLoader)  # noqa: S506 - SafeLoader subclass
    except yaml.YAMLError as exc:
        v.err(E_SCHEMA, "", f"not valid YAML: {exc}")
        return None, v.errors, v.warnings
    if not isinstance(raw, dict):
        v.err(E_SCHEMA, "", "the ledger must be a YAML map")
        return None, v.errors, v.warnings

    v.unknown_keys(raw, _TOP_KEYS, "")
    for k in _TOP_REQUIRED:
        if k not in raw:
            v.err(E_SCHEMA, k, "required")
    version = raw.get("version")
    if "version" in raw and not (_is_int(version) and version == LEDGER_VERSION):
        v.err(E_SCHEMA, "version", f"must be {LEDGER_VERSION}, got {version!r}")
    epic = v.string(raw, "epic", "", hint=_yaml_hint(raw.get("epic")))
    epic_key = ""
    if epic is not None:
        try:
            epic_key = sanitise(epic)
        except ValueError:
            v.err(E_NAMES, "epic", f"sanitises to nothing: {epic!r}")
    run_id = v.string(raw, "run_id", "")
    if run_id is not None and not RUN_ID_RE.match(run_id):
        v.err(E_SCHEMA, "run_id", f"must match [a-z0-9-]+, got {run_id!r}")
    if run_id is None and epic_key:
        run_id = f"epic-{epic_key}"

    workspace = v.string(raw, "workspace", "") or ""
    plan_root = v.string(raw, "plan_root", "") or ""
    workspace_ok = False
    if workspace:
        if not os.path.isabs(workspace):
            v.err(E_SCHEMA, "workspace", f"must be an absolute path: {workspace!r}")
        elif not os.path.isdir(workspace):
            v.err(E_FILES, "workspace", f"missing: {workspace}")
        else:
            workspace_ok = True
        base = os.path.basename(workspace.rstrip("/"))
        if not WORKSPACE_BASENAME_RE.match(base):
            v.err(E_NAMES, "workspace",
                  f"basename {base!r} is outside [A-Za-z0-9_-]+ (tmux rewrites . and :)")
    plan_dir = os.path.normpath(os.path.join(workspace, plan_root)) if workspace and plan_root else ""
    plan_ok = False
    if plan_root:
        if os.path.isabs(plan_root):
            v.err(E_SCHEMA, "plan_root", f"must be relative to workspace: {plan_root!r}")
        elif workspace_ok and not os.path.isdir(plan_dir):
            v.err(E_FILES, "plan_root", f"missing: {plan_dir}")
        elif workspace_ok:
            plan_ok = True

    worktree_root = v.string(raw, "worktree_root", "")
    worktree_default = worktree_root is None
    if worktree_root is not None and not os.path.isabs(worktree_root):
        v.err(E_SCHEMA, "worktree_root", f"must be an absolute path: {worktree_root!r}")
    if worktree_root is None and run_id and RUN_ID_RE.match(run_id):
        worktree_root = str(state_dir(run_id) / WORKTREES_DIR)   # OQ-1

    defaults = _parse_defaults(v, v.mapping(raw, "defaults", ""))
    notify = v.mapping(raw, "notify", "")
    notify_role = None
    if notify is not None:
        v.unknown_keys(notify, _NOTIFY_KEYS, "notify")
        notify_role = v.string(notify, "role", "notify")

    roles: Dict[str, Role] = {}
    roles_raw = v.mapping(raw, "roles", "")
    role_keys: Dict[str, str] = {}
    for key, rraw in (roles_raw or {}).items():
        key = str(key)
        try:
            skey = sanitise(key)
        except ValueError:
            v.err(E_NAMES, f"roles.{key}", "role key sanitises to nothing")
            continue
        if skey in role_keys:
            v.err(E_NAMES, f"roles.{key}", f"collides with role {role_keys[skey]!r} after sanitising ({skey})")
        role_keys.setdefault(skey, key)
        role = _parse_role(v, key, rraw, plan_dir, workspace)
        if role is not None:
            roles[key] = role
    if roles_raw is not None and "driver" not in roles_raw:
        v.err(E_SCHEMA, "roles.driver", "required: the ledger must define the driver role")

    resources: Dict[str, int] = {}
    for name, cap in (v.mapping(raw, "resources", "") or {}).items():
        if not _is_int(cap) or cap < 1:
            v.err(E_SCHEMA, f"resources.{name}", f"must be an int >= 1, got {cap!r}")
            continue
        resources[str(name)] = cap

    startup_dialogs = _parse_startup_dialogs(v, raw.get("startup_dialogs"))

    approvals: Dict[str, Approval] = {}
    for order, (aid, araw) in enumerate((v.mapping(raw, "approvals", "") or {}).items()):
        aid = str(aid)
        where = f"approvals.{aid}"
        if araw is None:
            araw = {}
        if not isinstance(araw, dict):
            v.err(E_SCHEMA, where, "must be a map {conditions: [step_id]}")
            continue
        v.unknown_keys(araw, ("conditions",), where)
        approvals[aid] = Approval(id=aid, conditions=v.id_list(araw, "conditions", where), order=order)

    steps: List[Step] = []
    steps_raw = raw.get("steps")
    if "steps" in raw and not isinstance(steps_raw, list):
        v.err(E_SCHEMA, "steps", "must be a list")
        steps_raw = []
    ids: Dict[str, int] = {}
    keys: Dict[str, str] = {}
    for i, sraw in enumerate(steps_raw or []):
        step = _parse_step(v, i, sraw, defaults, plan_dir)
        if step is None:
            continue
        if step.id in ids:
            v.err(E_NAMES, f"steps.{step.id}.id", "duplicate id")
            continue
        if step.key in keys:
            v.err(E_NAMES, f"steps.{step.id}.id",
                  f"collides with step {keys[step.key]!r} after sanitising ({step.key})")
        ids[step.id] = i
        keys.setdefault(step.key, step.id)
        steps.append(step)
    by_id = {s.id: s for s in steps}

    # graph
    for s in steps:
        where = f"steps.{s.id}"
        for n in s.needs:
            if n not in by_id:
                v.err(E_GRAPH, f"{where}.needs", f"unknown step {n!r}")
        if s.after is not None:
            if s.after.step not in by_id:
                v.err(E_GRAPH, f"{where}.after.step", f"unknown step {s.after.step!r}")
            elif s.after.step not in s.needs:
                v.err(E_GRAPH, f"{where}.after.step", f"{s.after.step!r} must also be in needs")
    for a in approvals.values():
        for c in a.conditions:
            if c not in by_id:
                v.err(E_GRAPH, f"approvals.{a.id}.conditions", f"unknown step {c!r}")
    for cyc in _find_cycles(by_id):
        v.err(E_GRAPH, f"steps.{cyc[0]}.needs", "cycle: " + " -> ".join(cyc))

    # per-step references, files, timing, resources, names
    git_cache: Dict[Tuple[str, Tuple[str, ...]], CommandResult] = {}
    ws_base = os.path.basename(workspace.rstrip("/")) if workspace else ""
    if epic_key and ws_base and "driver" in roles:
        name = f"{ws_base}-{session_suffix(epic_key, 'driver', None, 1)}"
        if len(name) > MAX_SESSION_NAME_LEN:
            v.err(E_NAMES, "roles.driver", f"session name over {MAX_SESSION_NAME_LEN} characters: {name}")
    for s in steps:
        where = f"steps.{s.id}"
        if plan_ok and s.card and not os.path.isfile(s.card):
            v.err(E_FILES, f"{where}.card", f"missing: {s.card}")
        if s.approval is not None and s.approval not in approvals:
            v.err(E_APPROVALS, f"{where}.approval", f"unknown approval {s.approval!r}")
        _validate_timing(v, s)
        for rname, units in s.resource.items():
            if rname not in resources:
                v.err(E_RESOURCES, f"{where}.resource.{rname}", "unknown resource")
            elif units > resources[rname]:
                v.err(E_RESOURCES, f"{where}.resource.{rname}",
                      f"requests {units}, above the cap {resources[rname]}")
        if s.worker is not None:
            for which in ("implementer", "reviewer"):
                rk = getattr(s.worker, which)
                if rk not in roles and (roles_raw is None or rk not in roles_raw):
                    v.err(E_SCHEMA, f"{where}.role.worker.{which}", f"unknown role {rk!r}")
                elif rk in roles:
                    r = roles[rk]
                    if r.epic_prompt is not None and not os.path.isfile(r.epic_prompt):
                        v.err(E_FILES, f"roles.{rk}.epic_prompt", f"missing: {r.epic_prompt}")
                    if epic_key:
                        name = f"{worker_dir_name(epic_key, s.id)}-{session_suffix(epic_key, rk, s.id, 1)}"
                        if len(name) > MAX_SESSION_NAME_LEN:
                            v.err(E_NAMES, where, f"session name over {MAX_SESSION_NAME_LEN} characters: {name}")
            if s.worker.implementer == s.worker.reviewer:
                v.err(E_SCHEMA, f"{where}.role.worker",
                      "implementer and reviewer must be different roles (D14)")
            _validate_repos(v, s, world, verify_base_refs, git_cache)
        if s.role_kind == ROLE_KIND_DRIVER and s.check is None:
            v.warn(W_DRIVER_STEP_NO_CHECK, where, "driver step without a check: ADVANCED marks it done (unchecked)")
        for spec, sub in ((s.check, "check"), (s.trigger, "trigger")):
            if spec is not None:
                _warn_ssh(v, spec, f"{where}.{sub}", plan_dir)

    for role in roles.values():
        _validate_compaction(v, role, world)

    if v.errors:
        return None, v.errors, v.warnings
    ledger = Ledger(
        path=path, sha256=_file_sha256(data), version=LEDGER_VERSION, epic=epic or "",
        epic_key=epic_key, run_id=run_id or "", workspace=workspace, plan_root=plan_root,
        plan_dir=plan_dir, worktree_root=worktree_root or "", worktree_root_default=worktree_default,
        defaults=defaults, notify_role=notify_role, roles=roles, resources=resources,
        startup_dialogs=startup_dialogs, approvals=approvals, steps=steps, warnings=list(v.warnings),
    )
    return ledger, [], v.warnings


def load_ledger(path: str, world: Optional[World] = None, *, verify_base_refs: bool = True) -> Ledger:
    """Load and validate a ledger; raise :class:`LedgerError` on any error.

    Warnings are on ``ledger.warnings`` (and on the error, when it raises).
    """
    ledger, errors, warnings = validate_ledger(path, world, verify_base_refs=verify_base_refs)
    if ledger is None:
        raise LedgerError(os.path.abspath(path), errors, warnings)
    return ledger


# =============================================================================
# Runtime state (§8) and paths (§4)
# =============================================================================

class StateError(Exception):
    """The state file is unreadable or of another schema; the tick refuses it."""


class CheckRecord(TypedDict, total=False):
    last_run_at: Optional[str]
    last_rc: Optional[int]
    consecutive_errors: int
    output_tail: str
    last_error: Optional[str]


class TriggerRecord(TypedDict, total=False):
    fired_at: Optional[str]
    occurrence: int
    last_run_at: Optional[str]
    last_rc: Optional[int]
    output_tail: str


class StepState(TypedDict, total=False):
    status: str
    occurrence: int
    attempts: int
    due_since: Optional[str]
    started_at: Optional[str]
    advanced_at: Optional[str]
    done_at: Optional[str]
    last_done_at: Optional[str]
    by: Optional[str]
    inflight: Optional[Dict[str, Any]]
    last_verdict: Optional[Dict[str, Any]]
    check: CheckRecord
    trigger: Optional[TriggerRecord]
    window_covered_s: int
    undispatched_ticks: int
    questions: List[str]


class Grant(TypedDict, total=False):
    granted_at: str
    question: Optional[str]
    note: Optional[str]
    by: str


class RunState(TypedDict, total=False):
    schema: int
    run_id: str
    epic: str
    ledger: str
    ledger_sha256: Optional[str]
    enabled_at: Optional[str]
    last_tick_at: Optional[str]
    last_tick_ok_at: Optional[str]
    last_tick_error: Optional[str]
    tick_seq: int
    next_event_n: int
    clock: Optional[Dict[str, Any]]
    check_cursor: Optional[str]
    steps: Dict[str, StepState]
    sessions: Dict[str, Dict[str, Any]]
    outbox: List[Dict[str, Any]]
    grants: Dict[str, Grant]
    approval_question: Optional[Dict[str, Any]]
    questions: Dict[str, Dict[str, Any]]
    limits: Dict[str, Dict[str, Any]]
    alarms: Dict[str, Dict[str, Any]]
    phases: Dict[str, Any]
    notify_failures: List[Dict[str, Any]]
    worktrees: Dict[str, Any]


def state_root() -> Path:
    """``~/.local/state/epic-driver``, or ``$EPIC_DRIVER_STATE_ROOT`` (tests)."""
    override = os.environ.get(STATE_ROOT_ENV)
    return Path(override) if override else DEFAULT_STATE_ROOT


def state_dir(run_id: str) -> Path:
    if not isinstance(run_id, str) or not RUN_ID_RE.match(run_id):
        raise ValueError(f"bad run_id: {run_id!r}")
    return state_root() / run_id


@dataclass(frozen=True)
class RunPaths:
    """Every runtime path of §4 for one run."""

    root: Path
    dir: Path

    @property
    def active(self) -> Path:
        return self.root / ACTIVE_FILE

    @property
    def state(self) -> Path:
        return self.dir / STATE_FILE

    @property
    def lock(self) -> Path:
        return self.dir / LOCK_FILE

    @property
    def paused(self) -> Path:
        return self.dir / PAUSED_FILE

    @property
    def env_snapshot(self) -> Path:
        return self.dir / ENV_SNAPSHOT_FILE

    @property
    def last_valid_ledger(self) -> Path:
        return self.dir / LAST_VALID_LEDGER_FILE

    @property
    def events(self) -> Path:
        return self.dir / "events"

    @property
    def handoffs(self) -> Path:
        return self.dir / "handoffs"

    @property
    def reports(self) -> Path:
        return self.dir / "reports"

    @property
    def logs(self) -> Path:
        return self.dir / "logs"

    @property
    def tick_log(self) -> Path:
        return self.logs / "tick.log"

    @property
    def notify_log(self) -> Path:
        return self.logs / "notify.log"

    @property
    def worktrees(self) -> Path:
        return self.dir / WORKTREES_DIR

    def check_log(self, step_id: str) -> Path:
        return self.logs / "checks" / f"{step_id}.log"


def run_paths(run_id: str) -> RunPaths:
    return RunPaths(root=state_root(), dir=state_dir(run_id))


def pointer_path(ledger: Ledger) -> Path:
    """The one tracked-tree file (``enable`` writes it once, P0-2)."""
    return Path(ledger.plan_dir) / POINTER_FILE


def new_check_record() -> CheckRecord:
    return {"last_run_at": None, "last_rc": None, "consecutive_errors": 0,
            "output_tail": "", "last_error": None}


def new_step_state() -> StepState:
    return {
        "status": "pending", "occurrence": 0, "attempts": 0, "due_since": None,
        "started_at": None, "advanced_at": None, "done_at": None, "last_done_at": None,
        "by": None, "inflight": None, "last_verdict": None, "check": new_check_record(),
        "trigger": None, "window_covered_s": 0, "undispatched_ticks": 0, "questions": [],
    }


def new_state(ledger: Ledger, now: datetime) -> RunState:
    """A fresh state for ``ledger`` (``enable``, §18.1 gate 9)."""
    return {
        "schema": STATE_SCHEMA, "run_id": ledger.run_id, "epic": ledger.epic,
        "ledger": ledger.path, "ledger_sha256": ledger.sha256, "enabled_at": iso(now),
        "last_tick_at": None, "last_tick_ok_at": None, "last_tick_error": None,
        "tick_seq": 0, "next_event_n": 1, "clock": None, "check_cursor": None,
        "steps": {s.id: new_step_state() for s in ledger.steps},
        "sessions": {}, "outbox": [], "grants": {}, "approval_question": None,
        "questions": {}, "limits": {}, "alarms": {}, "phases": {"announced": []},
        "notify_failures": [], "worktrees": {},
    }


def ensure_step_states(ledger: Ledger, state: RunState) -> None:
    """Add a fresh record for every ledger step the state does not know yet.

    Records of steps no longer in the ledger are kept (history), never used.
    """
    steps = state.setdefault("steps", {})
    for s in ledger.steps:
        rec = steps.get(s.id)
        if rec is None:
            steps[s.id] = new_step_state()
            continue
        for k, val in new_step_state().items():
            rec.setdefault(k, val)  # type: ignore[misc]


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """tmp in the same directory + fsync + ``os.replace`` + fsync of the directory."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(path.parent)


def save_state(path: Path, state: RunState) -> None:
    """Commit the state (§23 commit point): atomic, fsynced file and directory."""
    if state.get("schema") != STATE_SCHEMA:
        raise StateError(f"refusing to write a state of schema {state.get('schema')!r}")
    atomic_write_text(Path(path), json.dumps(state, indent=1, sort_keys=False) + "\n")


def load_state(path: Path, run_id: Optional[str] = None) -> RunState:
    """Read the state; refuse another schema version or run id (``StateError``)."""
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
    except OSError as exc:
        raise StateError(f"cannot read {path}: {exc}") from None
    except ValueError as exc:
        raise StateError(f"{path} is not valid JSON: {exc}") from None
    if not isinstance(state, dict):
        raise StateError(f"{path}: not a JSON object")
    if state.get("schema") != STATE_SCHEMA:
        raise StateError(f"{path}: schema {state.get('schema')!r}, this driver reads {STATE_SCHEMA}")
    if run_id is not None and state.get("run_id") != run_id:
        raise StateError(f"{path}: run_id {state.get('run_id')!r}, expected {run_id!r}")
    return state  # type: ignore[return-value]


def load_env_snapshot(paths: RunPaths) -> Dict[str, str]:
    """``env_snapshot.json`` (written by ``enable``, §21.2); missing or bad → ``{}``."""
    try:
        with open(paths.env_snapshot, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}


def _step_state(state: RunState, step_id: str) -> StepState:
    steps = state.setdefault("steps", {})
    if step_id not in steps:
        steps[step_id] = new_step_state()
    return steps[step_id]


def mark_done(ledger: Ledger, state: RunState, step_id: str, now: datetime) -> StepState:
    """``mark <step> done`` (§8): done now, by the operator, inflight cleared, no check run.

    A recurring step's current occurrence is done; :func:`advance_schedule`
    rolls it to the next occurrence.
    """
    ledger.step(step_id)
    rec = _step_state(state, step_id)
    rec["status"] = DONE
    rec["done_at"] = iso(now)
    rec["by"] = "operator"
    rec["inflight"] = None
    return rec


def mark_reset(ledger: Ledger, state: RunState, step_id: str, now: datetime) -> Dict[str, Any]:
    """``mark <step> reset`` (§8).

    Status → pending (due is recomputed), attempts → 0, inflight, last verdict,
    check and trigger counters cleared; unconfirmed outbox entries for the step
    cancelled; the step's alarms (and those of the cancelled events) cleared.
    Sessions are untouched. Returns ``{"cancelled_events": [...],
    "cleared_alarms": [...]}`` for the caller's log.
    """
    ledger.step(step_id)
    rec = _step_state(state, step_id)
    occurrence = rec.get("occurrence", 0)
    last_done = rec.get("last_done_at")
    questions = rec.get("questions", [])
    fresh = new_step_state()
    fresh["occurrence"] = occurrence
    fresh["last_done_at"] = last_done
    fresh["questions"] = questions
    rec.clear()
    rec.update(fresh)

    cancelled: List[str] = []
    kept: List[Dict[str, Any]] = []
    for entry in state.get("outbox", []):
        steps = entry.get("steps") or []
        if step_id in steps and not entry.get("confirmed_at"):
            remaining = [s for s in steps if s != step_id]
            if remaining:
                entry["steps"] = remaining
                kept.append(entry)
            else:
                cancelled.append(str(entry.get("event")))
            continue
        kept.append(entry)
    state["outbox"] = kept

    cleared: List[str] = []
    subjects = {step_id, *cancelled}
    for key, alarm in state.get("alarms", {}).items():
        if alarm.get("subject") in subjects and not alarm.get("cleared_at"):
            alarm["cleared_at"] = iso(now)
            cleared.append(key)
    return {"cancelled_events": cancelled, "cleared_alarms": cleared}


# =============================================================================
# Schedule (§6): the step state machine, due and dispatchable steps
# =============================================================================

PENDING = "pending"
DUE = "due"
INFLIGHT = "inflight"
VERIFYING = "verifying"
WAITING = "waiting"
ASKED = "asked"
HALTED = "halted"
FAILED = "failed"
DONE = "done"
WATCHING = "watching"

STEP_STATUSES = (PENDING, DUE, INFLIGHT, VERIFYING, WAITING, ASKED, HALTED, FAILED, DONE, WATCHING)

#: §6.1 as data: status → statuses it may move to. ``mark`` (done/reset) may
#: move any status to ``done`` or ``pending`` (:data:`MARK_TARGETS`).
STEP_TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    PENDING: (DUE, WATCHING),
    DUE: (INFLIGHT, PENDING),
    INFLIGHT: (VERIFYING, DONE, WAITING, ASKED, HALTED),     # DONE: a driver step with no check
    VERIFYING: (DONE, FAILED, HALTED),                        # HALTED: a driver HALT (50-05)
    WAITING: (INFLIGHT, VERIFYING, DONE, FAILED, WAITING, ASKED, HALTED),
    ASKED: (INFLIGHT, VERIFYING, WAITING, ASKED, HALTED, DONE),
    # §16.2: `halt` clears on "a later verdict on the step, or mark" (50-05).
    HALTED: (VERIFYING, WAITING, ASKED, HALTED, DONE),
    FAILED: (DUE, INFLIGHT, VERIFYING, WAITING, ASKED, HALTED),
    DONE: (PENDING,),                                         # recurring: next occurrence
    WATCHING: (DONE, FAILED),
}
MARK_TARGETS = (DONE, PENDING)

#: Statuses that hold a step's ``resource`` units (in flight, §5.4).
RESOURCE_HOLDING = (INFLIGHT, VERIFYING, WAITING, ASKED)
#: Statuses whose ``check`` runs (§6.1: verifying; waiting keeps running it; watch steps).
CHECKED_STATUSES = (VERIFYING, WAITING, WATCHING)


def can_transition(old: str, new: str, *, by_mark: bool = False) -> bool:
    if by_mark and new in MARK_TARGETS:
        return True
    return new in STEP_TRANSITIONS.get(old, ())


def set_status(rec: StepState, new: str, *, by_mark: bool = False) -> None:
    """Move a step record to ``new``; an undeclared transition raises ``ValueError``."""
    old = rec.get("status", PENDING)
    if new not in STEP_STATUSES:
        raise ValueError(f"unknown step status {new!r}")
    if not can_transition(old, new, by_mark=by_mark):
        raise ValueError(f"step status {old} -> {new} is not a transition of §6.1")
    rec["status"] = new


# Hold reasons (§6.2): why a due step is not dispatchable.
HOLD_PAUSED = "paused"
HOLD_APPROVAL = "approval"                       # ungranted, no approval question open
HOLD_APPROVAL_ASKED = "approval_question_open"   # ungranted, an approval question is open
HOLD_APPROVAL_CONDITIONS = "approval_conditions"  # granted, conditions not done
HOLD_WINDOW = "window"
HOLD_LATEST_START = "latest_start"
HOLD_RESOURCES = "resources"
HOLD_MODEL_LIMIT = "model_limit"
HOLD_GROUP = "group"
HOLD_ATTEMPTS = "attempts_exhausted"

#: Reasons the operator already sees, so they never raise ``due_not_started``
#: (§6.2: paused, an approval question open, a model limit, a closed window —
#: past the latest start is the window being closed for today; exhausted
#: attempts have their own alarm).
QUIET_HOLDS = frozenset({HOLD_PAUSED, HOLD_APPROVAL_ASKED, HOLD_MODEL_LIMIT, HOLD_WINDOW,
                         HOLD_LATEST_START, HOLD_ATTEMPTS})


@dataclass
class Hold:
    reason: str
    detail: str = ""


@dataclass
class DueStep:
    step_id: str
    due_at: datetime
    occurrence: int
    why: str


@dataclass
class ApprovalAsk:
    """The approval to raise as ``approval_needed`` (§15.1)."""

    approval_id: str
    steps: List[str]
    conditions: List[str]


@dataclass
class Selection:
    """The result of :func:`select` at one ``now``."""

    due: List[DueStep] = field(default_factory=list)
    dispatchable: List[str] = field(default_factory=list)
    units: List[List[str]] = field(default_factory=list)   # one step, or a group's members
    held: Dict[str, List[Hold]] = field(default_factory=dict)
    approval_to_ask: Optional[ApprovalAsk] = None

    def due_ids(self) -> List[str]:
        return [d.step_id for d in self.due]


def step_done_at(step: Step, rec: Optional[StepState]) -> Optional[datetime]:
    """When the step (or, recurring, its latest occurrence) was done; None if never."""
    if not rec:
        return None
    if rec.get("status") == DONE and rec.get("done_at"):
        return from_iso(rec.get("done_at"))
    if step.is_recurring and rec.get("last_done_at"):
        return from_iso(rec.get("last_done_at"))
    return None


def step_is_done(step: Step, rec: Optional[StepState]) -> bool:
    return step_done_at(step, rec) is not None


def _after_anchor(step: Step, rec: Optional[StepState]) -> Optional[datetime]:
    """§5.4 ``after``: the predecessor's ``advanced_at`` if it has a role, else its ``done_at``."""
    if not rec:
        return None
    if step.role_kind is not None and rec.get("advanced_at"):
        return from_iso(rec.get("advanced_at"))
    return step_done_at(step, rec)


def _first_slot_at_or_after(t: datetime, at_s: int, strictly: bool = False) -> datetime:
    t = require_aware(t)
    slot = _midnight(t.date()) + timedelta(seconds=at_s)
    while slot < t or (strictly and slot == t):
        slot += timedelta(days=1)
    return slot


def series_reference(step: Step, rec: Optional[StepState], state: RunState) -> Optional[datetime]:
    """§6.3 series reference: ``from`` until the first occurrence is done, then the previous ``done_at``."""
    if step.every is None:
        return None
    last = from_iso((rec or {}).get("last_done_at"))
    if last is not None:
        return last
    if step.every.series and step.every.from_ is not None:
        return step.every.from_
    return from_iso(state.get("enabled_at"))


def occurrence_due_at(step: Step, rec: Optional[StepState], state: RunState) -> Optional[datetime]:
    """When the current occurrence of a recurring step falls due (§6.3); None if not recurring."""
    ev = step.every
    if ev is None:
        return None
    last = from_iso((rec or {}).get("last_done_at"))
    if step.at_s is not None:
        if last is not None:
            return _first_slot_at_or_after(last + timedelta(seconds=ev.period_s - DAY_S),
                                           step.at_s, strictly=True)
        start = step.not_before or from_iso(state.get("enabled_at"))
        return _first_slot_at_or_after(start, step.at_s) if start is not None else None
    if ev.series:
        ref = series_reference(step, rec, state)
        return ref + timedelta(seconds=ev.period_s) if ref is not None else None
    if last is not None:
        return last + timedelta(seconds=ev.period_s)
    if step.not_before is not None:
        return step.not_before
    enabled = from_iso(state.get("enabled_at"))
    return enabled + timedelta(seconds=ev.period_s) if enabled is not None else None


def occurrence_deadline(step: Step, rec: Optional[StepState],
                        state: RunState) -> Optional[Tuple[datetime, datetime]]:
    """``(near_at, missed_at)`` of the step: ``not_after`` or the series deadline; else None."""
    ev = step.every
    if ev is not None and ev.series and ev.deadline_s is not None:
        ref = series_reference(step, rec, state)
        if ref is None:
            return None
        missed = ref + timedelta(seconds=ev.deadline_s)
        return missed - timedelta(seconds=ev.warn_before_s or 0), missed
    if step.not_after is not None:
        return step.not_after - timedelta(seconds=step.warn_before_s), step.not_after
    return None


def _time_conditions(ledger: Ledger, state: RunState, step: Step,
                     ) -> Tuple[bool, Optional[datetime], List[str]]:
    """Every due condition except the trigger and the status: ``(met_so_far, due_at, why)``.

    ``due_at`` is the latest of the times the conditions were (or will be)
    met; ``met_so_far`` is False when a ``needs`` is not done (no time known).
    """
    steps = state.get("steps", {})
    by_id = ledger.steps_by_id
    times: List[datetime] = []
    why: List[str] = []
    for n in step.needs:
        pred = by_id.get(n)
        t = step_done_at(pred, steps.get(n)) if pred is not None else None
        if t is None:
            return False, None, [f"needs {n}"]
        times.append(t)
    if step.needs:
        why.append("needs done")
    if step.after is not None:
        pred = by_id.get(step.after.step)
        anchor = _after_anchor(pred, steps.get(step.after.step)) if pred is not None else None
        if anchor is None:
            return False, None, [f"after {step.after.step}"]
        times.append(anchor + timedelta(seconds=step.after.delay_s))
        why.append(f"{format_duration(step.after.delay_s)} after {step.after.step}")
    if step.not_before is not None:
        times.append(step.not_before)
        why.append(f"not before {iso(step.not_before)}")
    if step.every is not None:
        occ = occurrence_due_at(step, steps.get(step.id), state)
        if occ is None:
            return False, None, ["recurrence has no reference (enabled_at unset)"]
        times.append(occ)
        why.append(f"every {format_duration(step.every.period_s)}"
                   + (f" at {step.at_s // 3600:02d}:{step.at_s % 3600 // 60:02d}Z" if step.at_s is not None else ""))
    return True, (max(times) if times else None), why


def _trigger_fired(rec: Optional[StepState]) -> Optional[datetime]:
    trig = (rec or {}).get("trigger") or {}
    if trig.get("fired_at") and trig.get("occurrence", 0) == (rec or {}).get("occurrence", 0):
        return from_iso(trig.get("fired_at"))
    return None


def step_due_at(ledger: Ledger, state: RunState, step_id: str) -> Optional[datetime]:
    """When the step's due conditions are (or will be) met; None while blocked (needs, trigger).

    Pure; ignores the status. 50-06's keep-warm prediction uses it for future due times.
    """
    step = ledger.step(step_id)
    ok, due_at, _ = _time_conditions(ledger, state, step)
    if not ok:
        return None
    if step.trigger is not None:
        fired = _trigger_fired(state.get("steps", {}).get(step_id))
        if fired is None:
            return None
        due_at = max(due_at, fired) if due_at is not None else fired
    return due_at


def _is_due(ledger: Ledger, state: RunState, step: Step, now: datetime) -> Optional[DueStep]:
    rec = state.get("steps", {}).get(step.id) or {}
    if rec.get("status", PENDING) not in (PENDING, DUE):
        return None
    ok, due_at, why = _time_conditions(ledger, state, step)
    if not ok:
        return None
    if step.trigger is not None:
        fired = _trigger_fired(rec)
        if fired is None:
            return None
        due_at = max(due_at, fired) if due_at is not None else fired
        why.append("trigger fired")
    if due_at is not None and due_at > now:
        return None
    since = from_iso(rec.get("due_since")) or due_at or now
    return DueStep(step_id=step.id, due_at=since, occurrence=int(rec.get("occurrence", 0)),
                   why="; ".join(why) or "no conditions")


def due_steps(ledger: Ledger, state: RunState, now: datetime) -> List[DueStep]:
    """Steps due at ``now`` (§6.2), ledger order: action steps pending/due, watch steps pending."""
    now = require_aware(now)
    out = []
    for step in ledger.steps:
        d = _is_due(ledger, state, step, now)
        if d is not None:
            out.append(d)
    return out


def advance_schedule(ledger: Ledger, state: RunState, now: datetime) -> List[Tuple[str, str, str]]:
    """The status moves that need no session, applied in memory; returns ``(step, old, new)``.

    - a recurring step that is done rolls to its next occurrence (pending;
      attempts, counters and the trigger reset — §6.3);
    - a pending action step that is due becomes ``due`` (``due_since`` = when);
    - a pending watch step that is due becomes ``watching`` (its activation);
    - a ``due`` step that is no longer due (e.g. a need was reset) returns to pending.
    """
    now = require_aware(now)
    ensure_step_states(ledger, state)
    steps = state["steps"]
    moves: List[Tuple[str, str, str]] = []
    for step in ledger.steps:
        rec = steps[step.id]
        if step.is_recurring and rec.get("status") == DONE:
            occurrence = int(rec.get("occurrence", 0)) + 1
            last_done = rec.get("done_at")
            questions = rec.get("questions", [])
            rec.clear()
            rec.update(new_step_state())
            rec["occurrence"] = occurrence
            rec["last_done_at"] = last_done
            rec["questions"] = questions
            moves.append((step.id, DONE, PENDING))
    for step in ledger.steps:
        rec = steps[step.id]
        status = rec.get("status", PENDING)
        d = _is_due(ledger, state, step, now)
        if status == PENDING and d is not None:
            new = WATCHING if step.is_watch else DUE
            set_status(rec, new)
            rec["due_since"] = iso(d.due_at)
            moves.append((step.id, PENDING, new))
        elif status == DUE and d is None:
            set_status(rec, PENDING)
            rec["due_since"] = None
            rec["undispatched_ticks"] = 0
            moves.append((step.id, DUE, PENDING))
    return moves


def approval_granted(state: RunState, approval_id: str) -> bool:
    return approval_id in (state.get("grants") or {})


def approval_conditions_done(ledger: Ledger, state: RunState, approval_id: str) -> bool:
    appr = ledger.approvals[approval_id]
    by_id = ledger.steps_by_id
    steps = state.get("steps", {})
    return all(c in by_id and step_is_done(by_id[c], steps.get(c)) for c in appr.conditions)


def model_limited(ledger: Ledger, state: RunState, role_key: str, now: datetime) -> Optional[str]:
    """The ``limits`` key that holds ``role_key``'s model at ``now`` (§12.4), else None."""
    role = ledger.roles.get(role_key)
    if role is None:
        return None
    for key, rec in (state.get("limits") or {}).items():
        until = from_iso((rec or {}).get("until"))
        if until is not None and until > now and asl.model_matches(role.expected_model_id, role.model, key):
            return key
    return None


def _resource_use(ledger: Ledger, state: RunState) -> Dict[str, int]:
    use: Dict[str, int] = {}
    steps = state.get("steps", {})
    for s in ledger.steps:
        if (steps.get(s.id) or {}).get("status") in RESOURCE_HOLDING:
            for name, units in s.resource.items():
                use[name] = use.get(name, 0) + units
    return use


def _individual_holds(ledger: Ledger, state: RunState, step: Step, now: datetime,
                      paused: bool) -> List[Hold]:
    holds: List[Hold] = []
    rec = state.get("steps", {}).get(step.id) or {}
    if paused:
        holds.append(Hold(HOLD_PAUSED))
    if int(rec.get("attempts", 0)) >= step.max_attempts:
        holds.append(Hold(HOLD_ATTEMPTS, f"{rec.get('attempts')}/{step.max_attempts}"))
    if step.approval is not None:
        if not approval_granted(state, step.approval):
            q = state.get("approval_question")
            if q:
                holds.append(Hold(HOLD_APPROVAL_ASKED,
                                  f"{step.approval} ungranted; question {q.get('question')} open "
                                  f"for {q.get('id')}"))
            else:
                holds.append(Hold(HOLD_APPROVAL, f"{step.approval} ungranted"))
        elif not approval_conditions_done(ledger, state, step.approval):
            holds.append(Hold(HOLD_APPROVAL_CONDITIONS, f"{step.approval}: conditions not done"))
    if step.window is not None:
        off = step.window.offset(now)
        if off is None:
            holds.append(Hold(HOLD_WINDOW, f"window {step.window.text} closed; opens "
                                           f"{iso(step.window.next_open(now))}"))
        else:
            latest = step.latest_start_offset_s()
            if latest is not None and off > latest:
                holds.append(Hold(HOLD_LATEST_START, f"past the latest start in {step.window.text}"))
    role = step.target_role
    if role is not None:
        limit = model_limited(ledger, state, role, now)
        if limit is not None:
            holds.append(Hold(HOLD_MODEL_LIMIT, f"{limit} limited until "
                                                f"{(state.get('limits') or {}).get(limit, {}).get('until')}"))
    return holds


def select(ledger: Ledger, state: RunState, now: datetime, *, paused: bool = False) -> Selection:
    """Due steps, the dispatchable ones, why the others are held, and the approval to ask.

    Pure (§6.2, §15.1). Watch steps are due (to become ``watching``) but never
    dispatchable: they have no action. Ledger order breaks ties; resources are
    allocated in that order. A group dispatches only when every member is
    dispatchable now (one event, one session, the intersection of windows).
    """
    now = require_aware(now)
    sel = Selection(due=due_steps(ledger, state, now))
    by_id = ledger.steps_by_id
    action = [d.step_id for d in sel.due if not by_id[d.step_id].is_watch]
    for sid in action:
        sel.held[sid] = _individual_holds(ledger, state, by_id[sid], now, paused)

    due_set = set(action)
    blocked_alone = {sid for sid in action if sel.held[sid]}
    for sid in action:
        step = by_id[sid]
        if step.group is None:
            continue
        others = [s.id for s in ledger.steps
                  if s.group == step.group and s.id != sid
                  and (s.id not in due_set or s.id in blocked_alone)]
        if others:
            sel.held[sid].append(Hold(HOLD_GROUP, f"group {step.group} waits for {', '.join(others)}"))

    use = _resource_use(ledger, state)
    granted_units: List[str] = []
    seen_groups: set = set()
    for sid in action:
        if sel.held[sid]:
            continue
        step = by_id[sid]
        if step.group is not None:
            if step.group in seen_groups:
                continue
            unit = [s.id for s in ledger.steps if s.group == step.group]
        else:
            unit = [sid]
        need: Dict[str, int] = {}
        for m in unit:
            for name, units in by_id[m].resource.items():
                need[name] = need.get(name, 0) + units
        short = [n for n, u in need.items() if use.get(n, 0) + u > ledger.resources.get(n, 0)]
        if short:
            for m in unit:
                sel.held[m].append(Hold(HOLD_RESOURCES, "busy: " + ", ".join(short)))
            continue
        for n, u in need.items():
            use[n] = use.get(n, 0) + u
        if step.group is not None:
            seen_groups.add(step.group)
        sel.units.append(unit)
        granted_units.extend(unit)
    sel.dispatchable = [sid for sid in action if sid in set(granted_units)]
    sel.held = {k: v for k, v in sel.held.items() if v}
    sel.approval_to_ask = next_approval_to_ask(ledger, state, sel)
    return sel


def dispatchable(ledger: Ledger, state: RunState, now: datetime, *,
                 paused: bool = False) -> Tuple[List[str], Dict[str, List[Hold]]]:
    """``(dispatchable step ids, holds per held due step)`` — :func:`select` in short."""
    sel = select(ledger, state, now, paused=paused)
    return sel.dispatchable, sel.held


def due_not_started_quiet(holds: Sequence[Hold]) -> bool:
    """True when a hold is one the operator already sees (§6.2): no ``due_not_started``."""
    return any(h.reason in QUIET_HOLDS for h in holds)


def note_undispatched(ledger: Ledger, state: RunState, selection: Selection,
                      dispatched: Iterable[str]) -> None:
    """Count ticks a due action step was not dispatched (``undispatched_ticks``, §6.2)."""
    done_now = set(dispatched)
    due_action = {d.step_id for d in selection.due if not ledger.step(d.step_id).is_watch}
    for sid, rec in state.get("steps", {}).items():
        if sid in due_action and sid not in done_now:
            rec["undispatched_ticks"] = int(rec.get("undispatched_ticks", 0)) + 1
        else:
            rec["undispatched_ticks"] = 0


# =============================================================================
# Approvals, selection side (§15.1, P0-13)
# =============================================================================

def next_approval_to_ask(ledger: Ledger, state: RunState,
                         selection: Selection) -> Optional[ApprovalAsk]:
    """The first approval in map order to raise as ``approval_needed``, or None.

    Only when no approval question is open; the approval is ungranted, its
    conditions are done, and it gates a due step whose only hold is the
    approval itself (it would otherwise be dispatchable).
    """
    if state.get("approval_question"):
        return None
    for appr in ledger.approvals_in_order():
        if approval_granted(state, appr.id) or not approval_conditions_done(ledger, state, appr.id):
            continue
        gated = []
        for d in selection.due:
            step = ledger.step(d.step_id)
            if step.approval != appr.id or step.is_watch:
                continue
            holds = selection.held.get(step.id, [])
            if holds and all(h.reason == HOLD_APPROVAL for h in holds):
                gated.append(step.id)
        if gated:
            return ApprovalAsk(approval_id=appr.id, steps=gated, conditions=list(appr.conditions))
    return None


def grant(ledger: Ledger, state: RunState, approval_id: str, question: Optional[str],
          note: Optional[str], now: datetime) -> Grant:
    """``epic-driver mark --grant <id>`` (the only writer of ``state.grants``, invariant 9).

    Idempotent: an existing grant is returned unchanged. An approval id the
    ledger does not define raises ``KeyError``. The approval's conditions need
    not be done: the operator may grant ahead of time (the hold
    ``approval_conditions`` keeps the step back until they are).
    """
    if approval_id not in ledger.approvals:
        raise KeyError(f"unknown approval {approval_id!r}; the ledger defines: "
                       f"{', '.join(ledger.approvals) or 'none'}")
    grants = state.setdefault("grants", {})
    if approval_id in grants:
        return grants[approval_id]
    rec: Grant = {"granted_at": iso(now) or "", "question": question, "note": note, "by": "mark"}
    grants[approval_id] = rec
    return rec


# =============================================================================
# Checks and triggers (§7, P0-10)
# =============================================================================

KIND_CHECK = "check"
KIND_TRIGGER = "trigger"

OUTCOME_DONE = "done"            # check rc 0
OUTCOME_NOT_YET = "not_yet"      # rc 1
OUTCOME_FAILED = "failed"        # check rc 2 → check_failed event
OUTCOME_ERROR = "error"          # other rc, timeout, cannot start
OUTCOME_FIRED = "fired"          # trigger rc 0


@dataclass
class CheckJob:
    step_id: str
    kind: str
    command: str
    timeout_s: int


@dataclass
class CheckOutcome:
    """What one run did to the state (§7.1); 50-08 turns ``failed`` into a ``check_failed`` event."""

    step_id: str
    kind: str
    outcome: str
    rc: Optional[int]
    output_tail: str
    attempt: int
    consecutive_errors: int
    error: Optional[str] = None


@dataclass
class CheckPass:
    ran: List[Tuple[CheckJob, CheckRun, CheckOutcome]] = field(default_factory=list)
    not_reached: List[CheckJob] = field(default_factory=list)
    cursor: Optional[str] = None


def check_anchor(step: Step, rec: Optional[StepState]) -> Optional[datetime]:
    """§7.3 anchor: an action step's ``advanced_at``, else the step's activation (``due_since``)."""
    rec = rec or {}
    if step.role_kind is not None and rec.get("advanced_at"):
        return from_iso(rec.get("advanced_at"))
    return from_iso(rec.get("due_since"))


def check_window_status(step: Step, rec: Optional[StepState], now: datetime) -> Tuple[bool, int, bool]:
    """``(eligible, window_covered_s, past_end)`` of the step's ``check_window`` at ``now``.

    No window ⇒ always eligible. ``{daily, min}``: eligible once [anchor, now]
    overlapped the daily window for ``min`` in total. ``{from, to}``: eligible
    inside [anchor + from, anchor + to]; ``past_end`` after it.
    """
    cw = step.check_window
    if cw is None:
        return True, 0, False
    anchor = check_anchor(step, rec)
    if anchor is None:
        return False, 0, False
    if cw.daily is not None:
        covered = cw.daily.coverage_s(anchor, now)
        return covered >= cw.min_s, covered, False
    start = anchor + timedelta(seconds=cw.from_s or 0)
    end = anchor + timedelta(seconds=cw.to_s or 0)
    return start <= now <= end, 0, now > end


def check_every_slack_s(every_s: int) -> int:
    """Slack granted to ``every`` (see ``CHECK_EVERY_SLACK_MAX_S`` for the rule)."""
    return min(CHECK_EVERY_SLACK_MAX_S, every_s * CHECK_EVERY_SLACK_PERCENT // 100)


def _rate_ok(last_run_at: Any, every_s: int, now: datetime) -> bool:
    last = from_iso(last_run_at)
    if last is None:
        return True
    return (now - last).total_seconds() >= every_s - check_every_slack_s(every_s)


def window_coverage(ledger: Ledger, state: RunState, now: datetime) -> Dict[str, int]:
    """``{step id: seconds}`` of daily ``check_window`` coverage at ``now`` (§8 ``window_covered_s``).

    Pure: :func:`run_checks` writes the values into the state. Only steps
    with a ``{daily, min}`` window appear.
    """
    now = require_aware(now)
    steps = state.get("steps", {})
    return {step.id: check_window_status(step, steps.get(step.id) or {}, now)[1]
            for step in ledger.steps
            if step.check is not None and step.check_window is not None
            and step.check_window.daily is not None
            and (steps.get(step.id) or {}).get("status", PENDING) in CHECKED_STATUSES}


def eligible_checks(ledger: Ledger, state: RunState, now: datetime) -> List[CheckJob]:
    """Every check or trigger that may run at ``now``, in ledger order (no budget applied).

    Pure: reads the state, never writes it.

    Checks: status verifying / waiting / watching, the ``check_window``
    eligible, ``check_every`` elapsed. Triggers: status pending, every other
    due condition met, not yet fired for the occurrence, the trigger's
    ``every`` elapsed. (The worker sha check of §14.4 is 50-07's, in front.)
    """
    now = require_aware(now)
    steps = state.get("steps", {})
    jobs: List[CheckJob] = []
    for step in ledger.steps:
        rec = steps.get(step.id) or {}
        status = rec.get("status", PENDING)
        if step.check is not None and status in CHECKED_STATUSES:
            eligible, _, _ = check_window_status(step, rec, now)
            if eligible and _rate_ok((rec.get("check") or {}).get("last_run_at"), step.check.every_s, now):
                jobs.append(CheckJob(step.id, KIND_CHECK, step.check.run, step.check.timeout_s))
        elif step.trigger is not None and status == PENDING and _trigger_fired(rec) is None:
            ok, due_at, _ = _time_conditions(ledger, state, step)
            if ok and (due_at is None or due_at <= now) \
                    and _rate_ok((rec.get("trigger") or {}).get("last_run_at"), step.trigger.every_s, now):
                jobs.append(CheckJob(step.id, KIND_TRIGGER, step.trigger.run, step.trigger.timeout_s))
    return jobs


def order_by_cursor(jobs: List[CheckJob], ledger: Ledger, cursor: Optional[str]) -> List[CheckJob]:
    """Round-robin (§7.2): start at the cursor's step (or the next one after it in ledger order)."""
    if not cursor or not jobs:
        return list(jobs)
    index = {s.id: s.index for s in ledger.steps}
    if cursor not in index:
        return list(jobs)
    pos = index[cursor]
    for i, job in enumerate(jobs):
        if index.get(job.step_id, -1) >= pos:
            return jobs[i:] + jobs[:i]
    return list(jobs)


def check_env(run_id: str, step_id: str, state_dir_path: str, snapshot: Dict[str, str],
              base_env: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """The fixed environment of a check (§7.2); nothing else from the service environment."""
    base = os.environ if base_env is None else base_env
    env = {
        "PATH": snapshot.get("PATH") or FALLBACK_CHECK_PATH,
        "HOME": base.get("HOME") or str(Path.home()),
        "LANG": snapshot.get("LANG") or base.get("LANG") or FALLBACK_LANG,
        "EPIC_DRIVER_RUN_ID": run_id,
        "EPIC_DRIVER_STEP": step_id,
        "EPIC_DRIVER_STATE_DIR": state_dir_path,
        "EPIC_DRIVER_SSH_OPTS": CHECK_SSH_OPTS,
    }
    sock = base.get("SSH_AUTH_SOCK")
    if sock:
        try:
            if stat.S_ISSOCK(os.stat(sock).st_mode):
                env["SSH_AUTH_SOCK"] = sock
        except OSError:
            pass
    return env


def apply_check_result(ledger: Ledger, state: RunState, job: CheckJob, run: CheckRun,
                       now: datetime) -> CheckOutcome:
    """Apply one run to the state (§7.1, §7.4); pure over its inputs."""
    now = require_aware(now)
    step = ledger.step(job.step_id)
    rec = _step_state(state, job.step_id)
    check = rec.setdefault("check", new_check_record())
    rc = run.rc
    tail = run.output_tail[-CHECK_OUTPUT_TAIL_BYTES:]
    is_error = rc is None or rc not in (0, 1, 2) or (job.kind == KIND_TRIGGER and rc == 2)
    if is_error:
        check["consecutive_errors"] = int(check.get("consecutive_errors", 0)) + 1
        check["last_error"] = run.error or f"exit {rc}"
    else:
        check["consecutive_errors"] = 0
        check["last_error"] = None

    if job.kind == KIND_TRIGGER:
        trig = rec.get("trigger") or {}
        trig.update({"last_run_at": iso(run.started_at), "last_rc": rc, "output_tail": tail})
        trig.setdefault("fired_at", None)
        trig.setdefault("occurrence", int(rec.get("occurrence", 0)))
        if rc == 0:
            trig["fired_at"] = iso(now)
            trig["occurrence"] = int(rec.get("occurrence", 0))
        rec["trigger"] = trig  # type: ignore[typeddict-item]
        outcome = OUTCOME_FIRED if rc == 0 else (OUTCOME_ERROR if is_error else OUTCOME_NOT_YET)
    else:
        check.update({"last_run_at": iso(run.started_at), "last_rc": rc, "output_tail": tail})
        if rc == 0:
            set_status(rec, DONE)
            rec["done_at"] = iso(now)
            rec["by"] = "check"
            rec["inflight"] = None
            outcome = OUTCOME_DONE
        elif rc == 1:
            outcome = OUTCOME_NOT_YET
        elif rc == 2:
            set_status(rec, FAILED)
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            outcome = OUTCOME_FAILED
        else:
            outcome = OUTCOME_ERROR
    return CheckOutcome(step_id=step.id, kind=job.kind, outcome=outcome, rc=rc, output_tail=tail,
                        attempt=int(rec.get("attempts", 0)),
                        consecutive_errors=int(check.get("consecutive_errors", 0)), error=run.error)


def _append_rotating(path: Path, text: str, max_bytes: int = LOG_ROTATE_BYTES,
                     keep: int = LOG_KEEP) -> None:
    """Append to a log, rotating ``path`` → ``path.1`` … ``path.<keep>`` at ``max_bytes``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.exists() and path.stat().st_size + len(text.encode("utf-8")) > max_bytes:
            for i in range(keep, 0, -1):
                src = path if i == 1 else path.with_name(f"{path.name}.{i - 1}")
                if src.exists():
                    os.replace(str(src), str(path.with_name(f"{path.name}.{i}")))
    except OSError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(text)


def log_check_run(paths: RunPaths, job: CheckJob, run: CheckRun) -> None:
    """``logs/checks/<step>.log``: start, rc, duration, the last 4 KB of output."""
    rc = "timeout" if run.timed_out else ("error" if run.rc is None else str(run.rc))
    line = (f"--- {iso(run.started_at)} {job.kind} rc={rc} duration={run.duration_s:.1f}s"
            + (f" error={run.error}" if run.error else "") + "\n")
    tail = run.output_tail
    _append_rotating(paths.check_log(job.step_id), line + tail + ("" if tail.endswith("\n") or not tail else "\n"))


def run_checks(ledger: Ledger, state: RunState, world: World, *,
               snapshot: Optional[Dict[str, str]] = None, budget_s: float = CHECK_BUDGET_S,
               base_env: Optional[Dict[str, str]] = None, log: bool = True) -> CheckPass:
    """Tick step 4: run eligible checks and triggers round-robin within the budget.

    Starts at ``state.check_cursor``; stops starting new runs once ``budget_s``
    has elapsed (by ``world.now()``) and leaves the cursor on the first job not
    reached, so it goes first next tick. A job is not started when its own
    timeout would end past the budget (elapsed + timeout > ``budget_s``); the
    first job of a pass always runs, so a check whose timeout alone exceeds
    the budget still gets its turn. Timeouts are never clamped: a clamped
    timeout would count as a check error. Results are applied to the state as
    each run ends, and the daily-window coverage (``window_covered_s``) is
    refreshed first. Never runs an action (invariant 1).
    """
    start = require_aware(world.now())
    paths = run_paths(ledger.run_id)
    snap = snapshot if snapshot is not None else load_env_snapshot(paths)
    for step_id, covered in window_coverage(ledger, state, start).items():
        _step_state(state, step_id)["window_covered_s"] = covered
    jobs = order_by_cursor(eligible_checks(ledger, state, start), ledger, state.get("check_cursor"))
    result = CheckPass()
    for i, job in enumerate(jobs):
        elapsed = (require_aware(world.now()) - start).total_seconds()
        if result.ran and elapsed + job.timeout_s > budget_s:
            result.not_reached = jobs[i:]
            result.cursor = job.step_id
            break
        env = check_env(ledger.run_id, job.step_id, str(paths.dir), snap, base_env)
        run = world.run_check(job.command, ledger.plan_dir, env, job.timeout_s)
        outcome = apply_check_result(ledger, state, job, run, require_aware(world.now()))
        if log:
            try:
                log_check_run(paths, job, run)
            except OSError:
                pass
        result.ran.append((job, run, outcome))
    state["check_cursor"] = result.cursor
    return result


# =============================================================================
# Step alarm conditions (§16.2, step-level rows; conditions only — 50-08 notifies)
# =============================================================================

@dataclass
class AlarmCondition:
    """An active alarm condition ``(id, subject)``; 50-08's engine raises/clears it."""

    id: str
    subject: str
    detail: str = ""

    @property
    def key(self) -> str:
        return f"{self.id}:{self.subject}"


def deadline_conditions(ledger: Ledger, state: RunState, now: datetime) -> List[AlarmCondition]:
    """``deadline_near`` / ``deadline_missed`` for steps not done (§5.4, §6.3).

    Once missed, only ``deadline_missed`` is reported for the step.
    """
    now = require_aware(now)
    out: List[AlarmCondition] = []
    steps = state.get("steps", {})
    for step in ledger.steps:
        rec = steps.get(step.id) or {}
        if rec.get("status") == DONE:
            continue
        dl = occurrence_deadline(step, rec, state)
        if dl is None:
            continue
        near, missed = dl
        if now >= missed:
            out.append(AlarmCondition("deadline_missed", step.id, f"deadline {iso(missed)} passed"))
        elif now >= near:
            out.append(AlarmCondition("deadline_near", step.id,
                                      f"deadline {iso(missed)} (warning from {iso(near)})"))
    return out


def step_alarm_conditions(ledger: Ledger, state: RunState, now: datetime,
                          selection: Optional[Selection] = None) -> List[AlarmCondition]:
    """The step-level conditions of §16.2 this section can see.

    ``deadline_near``/``deadline_missed``, ``attempts_exhausted``,
    ``check_error`` (3 consecutive check or trigger errors), ``step_timeout``
    (no verdict within ``timeout`` for an action step; no pass within it, or
    past ``check_window.to``, for a checked step), ``halt``; and, with a
    ``selection``, ``due_not_started`` (due, not dispatched for 2 ticks, for
    a reason the operator does not already see). ``worktree_refused`` is 50-07's.
    """
    now = require_aware(now)
    out = deadline_conditions(ledger, state, now)
    steps = state.get("steps", {})
    for step in ledger.steps:
        rec = steps.get(step.id) or {}
        status = rec.get("status", PENDING)
        if status == DONE:
            continue
        attempts = int(rec.get("attempts", 0))
        if attempts >= step.max_attempts:
            out.append(AlarmCondition("attempts_exhausted", step.id, f"{attempts}/{step.max_attempts} attempts"))
        errors = int((rec.get("check") or {}).get("consecutive_errors", 0))
        if errors >= CHECK_ERROR_ALARM_AT:
            out.append(AlarmCondition("check_error", step.id,
                                      f"{errors} consecutive errors: {(rec.get('check') or {}).get('last_error')}"))
        if status == HALTED:
            verdict = (rec.get("last_verdict") or {}).get("line", "")
            out.append(AlarmCondition("halt", step.id, verdict))
        timeout = None
        if status == INFLIGHT and step.role_kind is not None:
            started = from_iso(rec.get("started_at"))
            if started is not None and now >= started + timedelta(seconds=step.timeout_s):
                timeout = f"no verdict within {format_duration(step.timeout_s)} of {iso(started)}"
        elif status in CHECKED_STATUSES and step.check is not None:
            anchor = check_anchor(step, rec)
            _, _, past_end = check_window_status(step, rec, now)
            if past_end:
                timeout = "past check_window.to with no passing check"
            elif step.is_watch and anchor is not None and now >= anchor + timedelta(seconds=step.timeout_s):
                timeout = f"no passing check within {format_duration(step.timeout_s)} of {iso(anchor)}"
        if timeout:
            out.append(AlarmCondition("step_timeout", step.id, timeout))
    if selection is not None:
        for d in selection.due:
            step = ledger.step(d.step_id)
            if step.is_watch:
                continue
            rec = steps.get(step.id) or {}
            holds = selection.held.get(step.id, [])
            if int(rec.get("undispatched_ticks", 0)) >= DUE_NOT_STARTED_TICKS and not due_not_started_quiet(holds):
                why = ", ".join(f"{h.reason}" + (f" ({h.detail})" if h.detail else "") for h in holds)
                out.append(AlarmCondition("due_not_started", step.id,
                                          f"due since {iso(d.due_at)}; " + (why or "not dispatched")))
    return out


# =============================================================================
# Session readings (§11.1, §12) — 50-05
# =============================================================================
#
# ``take_readings`` is the only part of the reconcile that touches the world:
# tmux, handles, lifecycle logs, the process tree, the clock and the question
# queue go through ``World``; transcripts are read through the amux_spawn_lib /
# 50-03 helpers at the paths the handles name. ``reconcile`` (below) is then a
# pure function of the readings, the ledger, the state and ``now``.

SESSION_RECORD = "record"     # a session the state names (``state.sessions[key]``)
SESSION_PARKED = "parked"     # an older generation the state keeps parked (§11.3)
SESSION_FOREIGN = "foreign"   # a handle of this run id that no record names (§12.8)

# The derived session status (§11.1). Liveness comes from tmux (session present,
# ``pane_dead``) and the reducer; the stored handle state alone never decides it
# (the 50-02 review note: ``_derive_claude_status`` short-circuits on a stored
# ``terminated`` until the next prompt hook rewrites it).
ST_DEAD = "dead"              # tmux session gone or pane dead ⇒ ``died`` (§12.1)
ST_STARTING = "starting"      # alive, no turn seen since the launch yet
ST_BUSY = "busy"              # alive, a turn open or background work outstanding
ST_IDLE = "idle"              # alive, the last turn stopped with nothing outstanding

#: A synthetic assistant entry (API errors) carries this model, never a real one.
SYNTHETIC_MODEL = "<synthetic>"


@dataclass(frozen=True)
class ProcessWork:
    """The Claude process under a pane (``owner_pid``) and whether a tool shell runs under it."""

    owner_pid: Optional[int]
    tool_shell: Optional[bool]      # None: no Claude owner found under the pane


@dataclass(frozen=True)
class ClockSample:
    """§12.7: ``/proc/sys/kernel/random/boot_id`` and the three clocks, in seconds."""

    boot_id: Optional[str]
    wall: float
    boottime: float
    monotonic: float

    def as_state(self) -> Dict[str, Any]:
        return {"boot_id": self.boot_id, "wall": self.wall, "boottime": self.boottime,
                "monotonic": self.monotonic}


@dataclass(frozen=True)
class QuestionEntry:
    qid: str
    status: Optional[str]          # the lower-cased status word, None when the heading has none
    text: str = ""                 # the entry as written in the queue file (heading to boundary)
    path: Optional[str] = None


@dataclass(frozen=True)
class QuestionsSnapshot:
    """The workspace's queue as the tick read it (§15.2), with its status vocabulary."""

    open_word: str
    resolved_words: Tuple[str, ...]
    withdrawn_word: str
    entries: Dict[str, QuestionEntry]

    def classify(self, qid: str) -> str:
        """``open`` / ``resolved`` / ``withdrawn``, or ``missing`` when the id is not in the queue."""
        entry = self.entries.get(qid)
        if entry is None:
            return "missing"
        status = (entry.status or "").lower()
        if status == self.withdrawn_word:
            return "withdrawn"
        if status in self.resolved_words:
            return "resolved"
        return "open"


@dataclass
class SessionReading:
    """Everything measured about one session this pass (§11.1).

    ``kind`` is :data:`SESSION_RECORD`, :data:`SESSION_PARKED` or
    :data:`SESSION_FOREIGN`; parked and foreign readings carry only the tmux
    facts and the handle. Timestamps are as the sources wrote them (ISO
    strings); ``activity_at`` and ``handoff_mtime`` are epoch seconds.
    """

    name: str
    kind: str = SESSION_RECORD
    key: Optional[str] = None
    handle: Optional[Dict[str, Any]] = None
    tmux_present: bool = False
    pane_dead: Optional[bool] = None
    pane_in_mode: Optional[bool] = None
    attached: Optional[int] = None
    pane_pid: Optional[int] = None
    events: List[Dict[str, Any]] = field(default_factory=list)
    transcript_path: Optional[str] = None
    transcript_first_at: Optional[float] = None   # epoch of the earliest entry in the tail that was read
    assistant: List[Dict[str, Any]] = field(default_factory=list)
    prompts: List[Dict[str, Any]] = field(default_factory=list)
    compactions: List[Dict[str, Any]] = field(default_factory=list)
    context_tokens: Optional[int] = None
    cache_left_s: Optional[float] = None
    cache_ttl: Optional[int] = None
    activity_at: Optional[float] = None
    owner_pid: Optional[int] = None
    tool_shell: Optional[bool] = None
    foreground_tool: Optional[Dict[str, Any]] = None
    pane_text: Optional[str] = None
    captured: bool = False
    expected_model_id: Optional[str] = None
    handoff_mtime: Optional[float] = None

    @property
    def alive(self) -> bool:
        """§11.1: ``tmux_has_session`` and ``#{pane_dead}`` = 0 (an unread pane counts as alive:
        a resume must never start a second process)."""
        return self.tmux_present and self.pane_dead is not True


@dataclass
class Readings:
    """One pass's ground truth: every driven session, the amux names, the clock, the queue."""

    sessions: List[SessionReading] = field(default_factory=list)
    handle_names: List[str] = field(default_factory=list)
    amux_names: List[str] = field(default_factory=list)
    clock: Optional[ClockSample] = None
    tz: Any = None
    questions: Optional[QuestionsSnapshot] = None
    questions_error: Optional[str] = None

    def by_name(self) -> Dict[str, SessionReading]:
        return {r.name: r for r in self.sessions}


def system_timezone() -> Any:
    """The system timezone: ``$TZ``, else ``/etc/localtime``'s zone, else the current fixed offset."""
    try:
        from zoneinfo import ZoneInfo  # Python ≥ 3.9
    except ImportError:  # pragma: no cover - below the floor
        ZoneInfo = None  # type: ignore[assignment]
    if ZoneInfo is not None:
        candidates = []
        tz_env = (os.environ.get("TZ") or "").lstrip(":")
        if tz_env:
            candidates.append(tz_env)
        try:
            link = os.path.realpath("/etc/localtime")
            if "zoneinfo/" in link:
                candidates.append(link.split("zoneinfo/", 1)[1])
        except OSError:
            pass
        for key in candidates:
            try:
                return ZoneInfo(key)
            except Exception:  # noqa: BLE001 - unknown key or no tzdata
                continue
    return datetime.now().astimezone().tzinfo


def _module_beside(name: str, *subdirs: str) -> Any:
    """Import ``name`` from sys.path, else from a sibling directory of this copy (§17)."""
    try:
        return importlib.import_module(name)
    except ImportError:
        pass
    here = Path(__file__).resolve().parent
    for sub in subdirs:
        cand = here.parent / sub
        if (cand / f"{name}.py").is_file():
            if str(cand) not in sys.path:
                sys.path.append(str(cand))
            return importlib.import_module(name)
    raise ImportError(f"{name} not found")


def _statusline() -> Any:
    """``statusline`` (§17): installed ``~/.claude/statusline``, or the repo's ``.claude/statusline``."""
    return _module_beside("statusline", "statusline")


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def session_launch_at(rec: Optional[Dict[str, Any]], handle: Optional[Dict[str, Any]] = None) -> Optional[datetime]:
    """The latest spawn or resume of a session: ``max(spawned_at, resumed_at[-1])``, else the
    handle's ``created_at``. Model checks, compaction and startup dialogs count from it."""
    times: List[datetime] = []
    if rec:
        t = from_iso(rec.get("spawned_at"))
        if t is not None:
            times.append(t)
        for r in rec.get("resumed_at") or []:
            t = from_iso(r)
            if t is not None:
                times.append(t)
    if times:
        return max(times)
    if handle:
        return from_iso(handle.get("created_at"))
    return None


def _outbox_for(state: RunState, name: str) -> List[Dict[str, Any]]:
    return [e for e in state.get("outbox") or [] if e.get("session") == name]


_CONFIRMED = object()   # the launch event is no longer in the outbox (completed)


def launch_entry(state: RunState, rec: Dict[str, Any]) -> Any:
    """The outbox entry of the session's seed/resume prompt for its current launch.

    ``rec["launch_event"]`` names it (50-06 writes it with the session record);
    an event no longer in the outbox was completed. Without the field, the
    earliest entry for the session sent at or after the launch. None when
    there is no such prompt.
    """
    ev = rec.get("launch_event")
    name = rec.get("name")
    if ev:
        for entry in state.get("outbox") or []:
            if entry.get("event") == ev:
                return entry
        return _CONFIRMED
    launch = session_launch_at(rec)
    if launch is None or not name:
        return None
    best = None
    for entry in _outbox_for(state, name):
        sends = [from_iso(s) for s in entry.get("sends") or []]
        sends = [s for s in sends if s is not None]
        if sends and min(sends) >= launch - timedelta(seconds=1):
            if best is None or min(sends) < best[0]:
                best = (min(sends), entry)
    return best[1] if best else None


def launch_prompt_unconfirmed(state: RunState, rec: Dict[str, Any]) -> bool:
    """§12.11: the session's seed/resume prompt is not yet confirmed (§9.3)."""
    entry = launch_entry(state, rec)
    if entry is None or entry is _CONFIRMED:
        return False
    return not entry.get("confirmed_at")


def _needs_capture(state: RunState, rec: Dict[str, Any]) -> bool:
    """Capture the pane when a startup dialog could be blocking, or a send is unconfirmed."""
    if launch_prompt_unconfirmed(state, rec):
        return True
    return any(e.get("sends") and not e.get("confirmed_at") for e in _outbox_for(state, rec.get("name") or ""))


def _read_tmux(world: World, reading: SessionReading) -> None:
    reading.tmux_present = bool(world.tmux_has_session(reading.name))
    if not reading.tmux_present:
        return
    disp = world.tmux_display(reading.name, TMUX_DISPLAY_FIELDS)
    if not disp:
        return
    dead = disp.get("pane_dead")
    reading.pane_dead = None if dead is None or dead == "" else dead.strip() == "1"
    mode = disp.get("pane_in_mode")
    reading.pane_in_mode = None if mode is None or mode == "" else mode.strip() != "0"
    reading.attached = _int_or_none(disp.get("session_attached"))
    reading.pane_pid = _int_or_none(disp.get("pane_pid"))


def _read_transcript(reading: SessionReading, now: datetime) -> None:
    tp = reading.transcript_path
    if not tp:
        return
    assistant = asl.assistant_entries(tp)
    for a in assistant:       # the limit text is all that is read later; keep the entries small
        if not a.get("is_api_error") and isinstance(a.get("text"), str):
            a["text"] = a["text"][-500:]
    reading.assistant = assistant
    reading.prompts = asl.user_prompt_entries(tp)
    reading.transcript_first_at = asl.transcript_tail_first_epoch(tp)
    reading.compactions = asl.compaction_boundaries(tp)
    reading.foreground_tool = asl.inflight_foreground_tool(tp)
    try:
        measured = _context_mcp_lib().measure_transcript(tp)
    except ImportError:
        measured = None
    if isinstance(measured, dict):
        reading.context_tokens = _int_or_none((measured.get("latest_turn") or {}).get("effective_context_tokens"))
    try:
        left, ttl = _statusline().cache_seconds_left(tp, now.timestamp())
    except ImportError:
        left, ttl = None, None
    reading.cache_left_s = left
    reading.cache_ttl = ttl
    try:
        activity = importlib.import_module("keep_awake_hook").transcript_activity({"transcript_path": tp})
    except ImportError:
        activity = 0.0
    reading.activity_at = activity or None


def _read_session(world: World, name: str, kind: str, key: Optional[str],
                  handle: Optional[Dict[str, Any]], rec: Optional[Dict[str, Any]],
                  state: RunState, now: datetime) -> SessionReading:
    reading = SessionReading(name=name, kind=kind, key=key, handle=handle)
    _read_tmux(world, reading)
    if kind != SESSION_RECORD:
        return reading
    rec = rec or {}
    reading.events = list(world.lifecycle_events(name))
    tp = (handle or {}).get("transcript_path") or rec.get("transcript_path")
    reading.transcript_path = tp if isinstance(tp, str) and tp else None
    _read_transcript(reading, now)
    if reading.alive and reading.pane_pid:
        work = world.process_work(reading.pane_pid)
        reading.owner_pid, reading.tool_shell = work.owner_pid, work.tool_shell
    if reading.alive and _needs_capture(state, rec):
        reading.pane_text = world.capture_pane(name)
        reading.captured = True
    model = (handle or {}).get("model")
    if isinstance(model, str) and model:
        profile = (handle or {}).get("profile")
        env: Dict[str, str] = {}
        if isinstance(profile, str) and profile:
            try:
                env = world.profile_env(profile)
            except (ValueError, OSError):
                env = {}
        reading.expected_model_id = asl.expected_model_id(model, env)
    rotation = rec.get("rotation") or {}
    hp = rotation.get("handoff_path")
    if isinstance(hp, str) and hp:
        try:
            reading.handoff_mtime = os.stat(hp).st_mtime
        except OSError:
            reading.handoff_mtime = None
    return reading


def take_readings(ledger: Ledger, state: RunState, world: World, now: datetime) -> Readings:
    """Measure every session record, its parked generations and every handle of the run id.

    One ``SessionReading`` per name. Also the amux names (orphans, §12.8), the
    clock (§12.7), the system timezone (§12.4) and, when a question recorded
    in the state is still open, the workspace's queue (§15.2). Reads only:
    nothing is sent, spawned or written.
    """
    now = require_aware(now)
    handles = [asl.upgrade_handle(h) for h in world.list_handles() if isinstance(h, dict)]
    by_name = {h.get("name"): h for h in handles if isinstance(h.get("name"), str)}
    out = Readings(handle_names=sorted(by_name))
    known: set = set()
    for key, rec in (state.get("sessions") or {}).items():
        name = rec.get("name")
        if isinstance(name, str) and name and name not in known:
            known.add(name)
            out.sessions.append(_read_session(world, name, SESSION_RECORD, key, by_name.get(name), rec, state, now))
        for p in rec.get("parked") or []:
            pname = (p or {}).get("name")
            if isinstance(pname, str) and pname and pname not in known:
                known.add(pname)
                out.sessions.append(_read_session(world, pname, SESSION_PARKED, key, by_name.get(pname),
                                                  None, state, now))
    for h in handles:
        name = h.get("name")
        if h.get("run_id") == ledger.run_id and isinstance(name, str) and name not in known:
            known.add(name)
            out.sessions.append(_read_session(world, name, SESSION_FOREIGN, None, h, None, state, now))
    out.amux_names = sorted(set(world.amux_names()))
    out.clock = world.clock_sample()
    out.tz = world.local_timezone()
    if any((q or {}).get("status") == "open" for q in (state.get("questions") or {}).values()):
        try:
            out.questions = world.question_entries(ledger.workspace)
        except Exception as exc:  # noqa: BLE001 - a queue the store cannot read is a fact, not a crash
            out.questions_error = f"{type(exc).__name__}: {exc}"
    return out


# -- derived facts over one reading (pure) --------------------------------------

def _epoch(value: Any) -> Optional[float]:
    return asl.epoch_of(value)


def reduction_of(reading: SessionReading) -> Dict[str, Any]:
    """The reducer over the reading's lifecycle events (``claude_event_reducer.reduce_events``)."""
    return cer.reduce_events(list(reading.events))


def session_status(reading: SessionReading, reduction: Optional[Dict[str, Any]] = None) -> str:
    """Dead / starting / busy / idle from tmux and the reducer (``_derive_claude_status`` logic).

    Unlike ``_derive_claude_status``, a stored ``terminated`` never decides:
    an alive pane whose log still ends in ``session_end`` (a resumed session
    before its first turn) reads :data:`ST_STARTING`.
    """
    if not reading.alive:
        return ST_DEAD
    red = reduction if reduction is not None else reduction_of(reading)
    stored = (reading.handle or {}).get("state")
    if red.get("terminated"):
        return ST_STARTING if stored in (None, "spawning", "terminated") else ST_IDLE
    hint = red.get("state_hint")
    if hint == "running":
        return ST_BUSY
    if hint == "idle":
        return ST_IDLE
    if stored in (None, "spawning", "terminated"):
        return ST_STARTING
    return ST_BUSY if stored == "running" else ST_IDLE


def _events_of(reading: SessionReading, kind: str) -> List[Tuple[float, Dict[str, Any]]]:
    out = []
    for ev in reading.events:
        if isinstance(ev, dict) and ev.get("event") == kind:
            ts = _epoch(ev.get("ts"))
            if ts is not None:
                out.append((ts, ev))
    out.sort(key=lambda p: p[0])
    return out


def _first_at_or_after(pairs: List[Tuple[float, Dict[str, Any]]], t: float) -> Optional[Tuple[float, Dict[str, Any]]]:
    for ts, ev in pairs:
        if ts >= t:
            return ts, ev
    return None


def permission_open_since(reading: SessionReading) -> Optional[float]:
    """Epoch of the ``permission_prompt`` event behind a pending prompt, else None."""
    red = reduction_of(reading)
    if not red.get("permission_pending"):
        return None
    prompts = _events_of(reading, "permission_prompt")
    return prompts[-1][0] if prompts else _epoch(red.get("last_event_ts"))


def last_assistant(reading: SessionReading) -> Optional[Dict[str, Any]]:
    return reading.assistant[-1] if reading.assistant else None


def is_limit_entry(entry: Optional[Dict[str, Any]]) -> bool:
    """§12.4: ``error == "rate_limit"`` on an API-error entry; never classified by its text."""
    return bool(entry) and entry.get("error") == "rate_limit" and bool(entry.get("is_api_error"))


def first_model_entry(reading: SessionReading, since: datetime) -> Optional[Dict[str, Any]]:
    """The first real (non-synthetic) assistant entry at or after ``since`` (§12.5)."""
    t = since.timestamp()
    for a in reading.assistant:
        ts = _epoch(a.get("timestamp"))
        if ts is None or ts < t:
            continue
        if a.get("is_api_error") or a.get("model") in (None, "", SYNTHETIC_MODEL):
            continue
        return a
    return None


# =============================================================================
# Verdicts (§10, D6) — 50-05
# =============================================================================

V_ADVANCED = "ADVANCED"
V_WAITING = "WAITING"
V_ASKED = "ASKED"
V_DONE = "DONE"
V_HALT = "HALT"
V_HANDOFF = "HANDOFF"
VERDICT_VERBS = (V_ADVANCED, V_WAITING, V_ASKED, V_DONE, V_HALT, V_HANDOFF)

_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_QID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+(?:-[A-Za-z]{1,4})?$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")
#: A worker's ADVANCED shas: ``{"": sha}`` for a bare sha, else ``{repo name: sha}``.
BARE_SHA = ""


@dataclass(frozen=True)
class Verdict:
    """One verdict of a verdict line (syntax only; ids are checked against the event later)."""

    verb: str
    id: Optional[str] = None                 # step or approval id (ASKED: the target id)
    shas: Tuple[Tuple[str, str], ...] = ()   # ADVANCED: ((repo name or BARE_SHA, sha), …)
    text: str = ""                           # WAITING: the rest; HALT: the text; HANDOFF: the path
    question: Optional[str] = None           # ASKED: the question id
    raw: str = ""


def _parse_one(part: str) -> Optional[Verdict]:
    for verb in (V_ADVANCED, V_WAITING, V_ASKED, V_DONE):
        prefix = verb + ": "
        if not part.startswith(prefix):
            continue
        rest = part[len(prefix):]
        if not rest or rest != rest.strip():
            return None
        if verb == V_ADVANCED:
            tokens = rest.split(" ")
            if len(tokens) not in (1, 2) or not STEP_ID_RE.match(tokens[0]):
                return None
            shas: List[Tuple[str, str]] = []
            if len(tokens) == 2:
                if "=" in tokens[1]:
                    for pair in tokens[1].split(","):
                        name, eq, sha = pair.partition("=")
                        if not eq or not _REPO_NAME_RE.match(name) or not _SHA_RE.match(sha):
                            return None
                        shas.append((name, sha))
                elif _SHA_RE.match(tokens[1]):
                    shas.append((BARE_SHA, tokens[1]))
                else:
                    return None
            return Verdict(V_ADVANCED, id=tokens[0], shas=tuple(shas), raw=part)
        if verb == V_WAITING:
            return Verdict(V_WAITING, text=rest, raw=part)
        if verb == V_ASKED:
            tokens = rest.split(" ")
            if len(tokens) != 2 or not _QID_RE.match(tokens[0]) or not STEP_ID_RE.match(tokens[1]):
                return None
            return Verdict(V_ASKED, id=tokens[1], question=tokens[0], raw=part)
        if verb == V_DONE:
            if " " in rest or not STEP_ID_RE.match(rest):
                return None
            return Verdict(V_DONE, id=rest, raw=part)
    return None


def parse_verdict_line(line: str) -> Optional[List[Verdict]]:
    """The §10 grammar, syntax only: the verdicts of one line, or None when it does not match.

    ``HALT: <text>`` and ``HANDOFF: <abs path>`` must stand alone; any other
    line is ``verdict *( "; " verdict )``. Surrounding whitespace is ignored.
    """
    if not isinstance(line, str):
        return None
    line = line.strip()
    if line.startswith(V_HALT + ": "):
        text = line[len(V_HALT) + 2:].strip()
        return [Verdict(V_HALT, text=text, raw=line)] if text else None
    if line.startswith(V_HANDOFF + ": "):
        path = line[len(V_HANDOFF) + 2:].strip()
        return [Verdict(V_HANDOFF, text=path, raw=line)] if path.startswith("/") else None
    verdicts: List[Verdict] = []
    for part in line.split("; "):
        v = _parse_one(part)
        if v is None:
            return None
        verdicts.append(v)
    return verdicts or None


# Why a verdict line is invalid (invalid = missing, §10).
INVALID_NONE = "no verdict line"
INVALID_NO_MATCH = "no match"
INVALID_FENCE = "inside a code fence"
INVALID_TWO_LINES = "two verdict lines"
INVALID_UNKNOWN_ID = "id not in the event"
INVALID_HANDOFF = "HANDOFF outside a rotate event"
INVALID_DONE_FROM_DRIVER = "DONE from the driver"
INVALID_WORKER_VERB = "a worker may end only DONE or HALT (§14.3)"
INVALID_SHA_MISSING = "a sha missing from the verdict"
INVALID_WAIT = "a WAITING without a wait, or over 200 characters"
INVALID_DUPLICATE = "an id covered twice"


def verdict_line_of(message: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """``(last non-empty line, why it is invalid or None)`` for a turn's final message (§10)."""
    lines = [ln.rstrip() for ln in (message or "").replace("\r\n", "\n").split("\n")]
    idx = [i for i, ln in enumerate(lines) if ln.strip()]
    if not idx:
        return None, INVALID_NONE
    last = idx[-1]
    line = lines[last].strip()
    fences = sum(1 for ln in lines[:last] if _FENCE_RE.match(ln))
    if fences % 2 == 1:
        return line, INVALID_FENCE
    if parse_verdict_line(line) is None:
        return line, INVALID_NO_MATCH
    if len(idx) >= 2 and parse_verdict_line(lines[idx[-2]]) is not None:
        return line, INVALID_TWO_LINES
    return line, None


@dataclass
class VerdictReading:
    """A turn's verdict checked against its event (§10)."""

    line: Optional[str]
    invalid: Optional[str] = None
    verdicts: List[Verdict] = field(default_factory=list)
    covered: Dict[str, Verdict] = field(default_factory=dict)       # id → the verdict that covers it
    wait: Dict[str, Tuple[str, Optional[datetime]]] = field(default_factory=dict)  # id → (text, until)
    unverified: Dict[str, str] = field(default_factory=dict)        # id → why the claim does not hold
    halt: Optional[Verdict] = None
    handoff: Optional[Verdict] = None

    @property
    def valid(self) -> bool:
        return self.invalid is None


def read_verdict(message: Optional[str], *, steps: Sequence[str], approvals: Sequence[str] = (),
                 from_driver: bool = True, rotate: bool = False,
                 grants: Optional[Dict[str, Any]] = None,
                 worker_repos: Optional[Dict[str, Sequence[str]]] = None,
                 ledger_ids: Iterable[str] = ()) -> VerdictReading:
    """Check a turn's final message against the event it answers (§10, §14.4, §15.1).

    ``steps``/``approvals`` are the ids the event carries; ``grants`` is
    ``state.grants`` (an ``ADVANCED: <approval>`` holds only with its grant);
    ``worker_repos`` maps each worker step of the event to its repo names
    (its ``ADVANCED`` must carry a sha per repo); ``ledger_ids`` are every
    step and approval id the ledger knows (a ``WAITING`` prefix naming one
    that is not in the event is invalid). A structural problem makes the
    whole line invalid; an approval claimed without a grant only leaves that
    id uncovered.
    """
    line, why = verdict_line_of(message)
    out = VerdictReading(line=line, invalid=why)
    if why is not None:
        return out
    ids = list(dict.fromkeys([*steps, *approvals]))
    id_set = set(ids)
    known = set(ledger_ids) | id_set
    grants = grants or {}
    worker_repos = worker_repos or {}
    verdicts = parse_verdict_line(line) or []
    out.verdicts = verdicts

    def fail(reason: str) -> VerdictReading:
        out.invalid = reason
        out.covered.clear()
        out.wait.clear()
        out.unverified.clear()
        out.halt = out.handoff = None
        return out

    first = verdicts[0]
    if first.verb == V_HALT:
        out.halt = first
        for i in ids:
            out.covered[i] = first
        return out
    if first.verb == V_HANDOFF:
        if not rotate:
            return fail(INVALID_HANDOFF)
        out.handoff = first
        for i in ids:
            out.covered[i] = first
        return out
    unprefixed: Optional[Verdict] = None
    for v in verdicts:
        if not from_driver and v.verb not in (V_DONE, V_HALT):
            return fail(INVALID_WORKER_VERB)
        if v.verb == V_DONE and from_driver:
            return fail(INVALID_DONE_FROM_DRIVER)
        target = v.id
        if v.verb == V_WAITING:
            head, sep, tail = v.text.partition(" ")
            if head in id_set:
                if not sep or not tail:
                    return fail(INVALID_WAIT)
                target, wait_text = head, tail
            elif head in known and sep:
                return fail(INVALID_UNKNOWN_ID)
            else:
                target, wait_text = None, v.text
            if len(wait_text) > VERDICT_WAIT_TEXT_MAX:
                return fail(INVALID_WAIT)
            try:
                until: Optional[datetime] = parse_utc_time(wait_text)
            except ValueError:
                until = None
            if target is None:
                if unprefixed is not None:
                    return fail(INVALID_DUPLICATE)
                unprefixed = v
                out.wait[""] = (wait_text, until)
                continue
            out.wait[target] = (wait_text, until)
        if target not in id_set:
            return fail(INVALID_UNKNOWN_ID)
        if target in out.covered or target in out.unverified:
            return fail(INVALID_DUPLICATE)
        if v.verb == V_DONE and target not in steps:
            return fail(INVALID_UNKNOWN_ID)
        if v.verb == V_ADVANCED and target in worker_repos:
            repos = list(worker_repos[target])
            named = dict(v.shas)
            if len(repos) <= 1:
                ok = len(named) == 1 and (BARE_SHA in named or (repos and repos[0] in named))
            else:
                ok = set(named) == set(repos)
            if not ok:
                return fail(INVALID_SHA_MISSING)
        if v.verb == V_ADVANCED and target in approvals and target not in steps:
            if target not in grants:
                out.unverified[target] = "no grant in the state"
                continue
        out.covered[target] = v
    if unprefixed is not None:
        text_until = out.wait.pop("")
        for i in ids:
            if i not in out.covered and i not in out.unverified:
                out.covered[i] = unprefixed
                out.wait[i] = text_until
    return out


# =============================================================================
# Delivery confirmation, reading side (§9.3) — 50-05
# =============================================================================

def _event_id_re(event: str) -> "re.Pattern[str]":
    return re.compile(r"(?<![A-Za-z0-9-])" + re.escape(event) + r"(?![0-9])")


def prompt_with_event(reading: SessionReading, event: str, since: float) -> Optional[Dict[str, Any]]:
    """The first user prompt at or after ``since`` (epoch) whose text contains ``event``."""
    pattern = _event_id_re(event)
    for p in reading.prompts:
        ts = _epoch(p.get("timestamp"))
        if ts is not None and ts >= since and pattern.search(str(p.get("text") or "")):
            return p
    return None


def _first_send(entry: Dict[str, Any]) -> Optional[float]:
    sends = [_epoch(s) for s in entry.get("sends") or []]
    sends = [s for s in sends if s is not None]
    return min(sends) if sends else None


def delivery_confirmation(entry: Dict[str, Any], reading: SessionReading) -> Optional[Dict[str, Any]]:
    """§9.3: a user entry containing ``E-<n>`` at or after the send, and a ``turn_start`` after it.

    Returns ``{"prompt_at", "turn_start_at"}`` (as written by the sources) or None.
    """
    t0 = _first_send(entry)
    event = entry.get("event")
    if t0 is None or not event:
        return None
    prompt = prompt_with_event(reading, event, t0)
    if prompt is None:
        return None
    start = _first_at_or_after(_events_of(reading, "turn_start"), t0)
    if start is None:
        return None
    return {"prompt_at": prompt.get("timestamp"), "turn_start_at": start[1].get("ts")}


def delivery_provably_absent(entry: Dict[str, Any], reading: SessionReading) -> bool:
    """§9.3 retry test (invariant 2): absence must be PROVEN, anything else is False (no retry).

    Provably absent needs all of: a readable transcript whose tail reaches back
    to the send time (``transcript_first_at <= send``, so the tail is not
    truncated before it); no user entry with ``E-<n>`` since the send; no
    ``turn_start`` at or after the send (something started a turn); and a pane
    that was captured and does not show the id.
    """
    t0 = _first_send(entry)
    event = entry.get("event")
    if t0 is None or not event or entry.get("confirmed_at"):
        return False
    if not reading.transcript_path or reading.transcript_first_at is None or reading.transcript_first_at > t0:
        return False
    if _first_at_or_after(_events_of(reading, "turn_start"), t0) is not None:
        return False
    if prompt_with_event(reading, event, t0) is not None:
        return False
    if not reading.captured or reading.pane_text is None or not reading.pane_text.strip():
        return False
    return _event_id_re(event).search(reading.pane_text) is None


# =============================================================================
# Usage limits (§12.4) — 50-05
# =============================================================================

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_RESET_RE = re.compile(
    r"\breset(?:s)?\s+(?:at\s+)?"
    r"(?:(?P<date>\d{4}-\d{2}-\d{2})[ T]"
    r"|(?P<mon>jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?P<day>\d{1,2})"
    r"(?:st|nd|rd|th)?,?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?(?::(?P<s>\d{2}))?\s*(?P<ampm>[ap]\.?m\b\.?)?"
    r"(?:\s*\((?P<tz>[A-Za-z_]+(?:/[A-Za-z0-9_+-]+)*)\))?",
    re.IGNORECASE)


def _zone(name: Optional[str], default: Any) -> Any:
    if not name:
        return default
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:  # noqa: BLE001 - unknown zone or no tzdata: the system timezone
        return default


def parse_reset_time(text: str, observed_at: datetime, tz: Any) -> Optional[datetime]:
    """The reset time a limit entry names, as aware UTC, or None (§12.4).

    Forms: ``resets 3pm``, ``resets 3:30pm``, ``resets at 15:00``, with an
    optional month and day (``resets Nov 25, 5pm``), an optional zone in
    parentheses (``resets 9:40pm (Asia/Yerevan)``, used when it resolves) and
    the dated ``reset at 2026-07-02 20:16:08``. Times are read in ``tz`` (the
    system timezone); a time of day already past at ``observed_at`` (the limit
    entry's own time) rolls to the next day, a month-day to the next year.
    """
    observed_at = require_aware(observed_at)
    for m in _RESET_RE.finditer(text or ""):
        ampm = (m.group("ampm") or "").lower().replace(".", "")
        if not (ampm or m.group("m") or m.group("date")):
            continue        # "resets 3" alone is not a time
        hour, minute, second = int(m.group("h")), int(m.group("m") or 0), int(m.group("s") or 0)
        if ampm:
            if not 1 <= hour <= 12:
                continue
            hour = hour % 12 + (12 if ampm == "pm" else 0)
        if hour > 23 or minute > 59 or second > 59:
            continue
        zone = _zone(m.group("tz"), tz)
        local = observed_at.astimezone(zone)
        try:
            if m.group("date"):
                y, mo, d = (int(x) for x in m.group("date").split("-"))
                return datetime(y, mo, d, hour, minute, second, tzinfo=zone).astimezone(UTC)
            if m.group("mon"):
                mo, d = _MONTHS[m.group("mon").lower()[:3]], int(m.group("day"))
                cand = datetime(local.year, mo, d, hour, minute, second, tzinfo=zone)
                if cand <= local:
                    cand = datetime(local.year + 1, mo, d, hour, minute, second, tzinfo=zone)
                return cand.astimezone(UTC)
            day = local.date()
            cand = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=zone)
            if cand <= local:
                day = day + timedelta(days=1)
                cand = datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=zone)
            return cand.astimezone(UTC)
        except ValueError:
            continue
    return None


def limit_record(entry: Dict[str, Any], prev: Optional[Dict[str, Any]], tz: Any,
                 session: Optional[str] = None) -> Dict[str, Any]:
    """The ``limits[<model>]`` record for a new limit entry (§12.4).

    A reset time in the text ⇒ ``source: "reset"``; otherwise the fixed
    ladder, one step further than the previous record of the same model while
    it is still held (15m, 30m, 1h, 2h, 4h, then 4h), counted from the entry.
    """
    at = from_iso(entry.get("timestamp"))
    if at is None:
        raise ValueError("a limit entry without a timestamp")
    step = int(prev.get("ladder_step", -1)) + 1 if prev else 0
    until = parse_reset_time(str(entry.get("text") or ""), at, tz)
    source = "reset"
    if until is None:
        source = "ladder"
        until = at + timedelta(seconds=LIMIT_LADDER_S[min(step, len(LIMIT_LADDER_S) - 1)])
    return {"until": iso(until), "source": source, "ladder_step": step,
            "entry_at": entry.get("timestamp"), "session": session,
            "text": str(entry.get("text") or "")[:200]}


# =============================================================================
# Startup dialogs (§12.11) — 50-05
# =============================================================================
#
# The classifier is pinned by captured panes of the installed Claude Code
# (tests/fixtures/epic_driver/panes/). On 2.1.330 the input box is the prompt
# glyph on the row directly under a full-width rule, closed by another rule; a
# dialog is a selection menu (a pointer row plus its options or its key hint)
# outside that box.

PANE_NO_DIALOG = "no_dialog"
PANE_DIALOG = "dialog"
PANE_UNREADABLE = "unreadable"

_RULE_RE = re.compile(r"^\s*[─━═]{8,}\s*$")
_GLYPH_ROW_RE = re.compile(r"^\s*[❯>](?:\s|$)")
# The selection pointer is ❯ (or ›), never ">": ">" is a conversation quote and also the
# input box's alternative prompt glyph (§9.3).
_POINTER_RE = re.compile(r"^(\s*)[❯›]\s+(?:(?P<num>\d+)\.\s+)?\S")
_MENU_HINT_RE = re.compile(r"(enter to (confirm|select|continue)|esc to (cancel|exit|go back)|↑/↓|↑↓)",
                           re.IGNORECASE)
_INPUT_BOX_MAX_ROWS = 30


@dataclass(frozen=True)
class PaneClass:
    kind: str            # PANE_NO_DIALOG / PANE_DIALOG / PANE_UNREADABLE
    detail: str = ""     # a dialog's first lines (the alarm detail)


def _input_box_rows(lines: List[str]) -> set:
    rows: set = set()
    for i, ln in enumerate(lines):
        if i == 0 or not _GLYPH_ROW_RE.match(ln) or not _RULE_RE.match(lines[i - 1]):
            continue
        for j in range(i + 1, min(len(lines), i + 1 + _INPUT_BOX_MAX_ROWS)):
            if _RULE_RE.match(lines[j]):
                rows.update(range(i - 1, j + 1))
                break
    return rows


def classify_pane(text: Optional[str]) -> PaneClass:
    """No dialog / dialog / unreadable, for a captured pane (§12.11).

    A dialog is a pointer row (``❯ <option>``, never ``>``) outside the input
    box with a menu key hint below it (``Enter to confirm``, ``Esc to cancel``)
    or a numbered pointer row (``❯ 1. Yes``). The input box, typed text in it included, is not a dialog. A
    blank pane, or one with neither, is unreadable.
    """
    if text is None or not text.strip():
        return PaneClass(PANE_UNREADABLE)
    lines = [ln.rstrip() for ln in text.replace("\r\n", "\n").split("\n")]
    box = _input_box_rows(lines)
    outside = [(i, ln) for i, ln in enumerate(lines) if i not in box]
    dialog = False
    for i, ln in outside:
        if not _POINTER_RE.match(ln):
            continue
        # a dialog needs its footer hint below the pointer, or a numbered pointer row; an indented
        # sibling line alone is not enough (a conversation row followed by a continuation is)
        hint_below = any(_MENU_HINT_RE.search(l2) for j, l2 in outside if j > i)
        numbered = _POINTER_RE.match(ln).group("num") is not None
        if hint_below or numbered:
            dialog = True
            break
    if dialog:
        head = [ln.strip() for _, ln in outside if ln.strip() and not _RULE_RE.match(ln)]
        detail = " / ".join(head[:DIALOG_DETAIL_LINES])
        return PaneClass(PANE_DIALOG, detail[:DIALOG_DETAIL_MAX])
    if box:
        return PaneClass(PANE_NO_DIALOG)
    return PaneClass(PANE_UNREADABLE)


def match_startup_dialog(ledger: Ledger, pane_text: str) -> Optional[StartupDialog]:
    """The first ``startup_dialogs`` entry whose ``match`` matches the capture (§5.2), else None."""
    for d in ledger.startup_dialogs:
        if d.matches(pane_text):
            return d
    return None


def dialog_answered(rec: Dict[str, Any], launch: Optional[datetime], dialog_id: str) -> bool:
    """Was ``dialog_id`` already answered for this spawn/resume (``rec["dialog_answers"]``)?"""
    stamp = iso(launch) if launch is not None else None
    return any((a or {}).get("dialog") == dialog_id and (a or {}).get("launch") == stamp
               for a in rec.get("dialog_answers") or [])


def record_dialog_answer(rec: Dict[str, Any], launch: Optional[datetime], dialog_id: str,
                         key: str, now: datetime) -> Dict[str, Any]:
    """The state mutation 50-06 commits before sending a listed dialog's key (invariant 5)."""
    if key not in STARTUP_DIALOG_KEYS:
        raise ValueError(f"startup dialog key {key!r} is not one of {STARTUP_DIALOG_KEYS}")
    answer = {"launch": iso(launch) if launch is not None else None, "dialog": dialog_id,
              "key": key, "at": iso(now)}
    rec.setdefault("dialog_answers", []).append(answer)
    return answer


# =============================================================================
# Session reconcile (§12, §10 application, §15.2) — 50-05
# =============================================================================

#: Event items for the dispatcher (they go into event files, §9.1).
ITEM_ANSWER = "answer"
ITEM_WORKER_SETTLED = "worker_settled"
ITEM_REVIEW_DUE = "review_due"
#: Action items: sends that are not events (50-06 commits, then sends).
ACTION_VERDICT_REMINDER = "verdict_reminder"
ACTION_STARTUP_DIALOG_ANSWER = "startup_dialog_answer"


@dataclass
class EventItem:
    """One item of a future event (§9.1): ``to`` is a session key (``driver``, ``<role>:<step>``)."""

    kind: str
    to: str
    step: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ActionItem:
    """A send the dispatcher performs that is not an event (the ``VERDICT?`` line, a dialog key)."""

    kind: str
    session: str          # the session key
    name: str             # the session name
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SessionFacts:
    """What the dispatcher (50-06) needs about one session record (§11.1, §11.2)."""

    key: str
    name: str
    alive: bool
    status: str                         # ST_DEAD / ST_STARTING / ST_BUSY / ST_IDLE
    died: bool = False                  # was live last pass, dead now (§12.1)
    came_up: bool = False               # a spawning/resuming record whose session now exists
    turn_open: bool = False
    permission_pending: bool = False
    attached: bool = False
    pane_in_mode: Optional[bool] = None
    context_tokens: Optional[int] = None
    cache_left_s: Optional[float] = None
    cache_ttl: Optional[int] = None
    overage: bool = False               # the last call's bucket is 5m (§11.1)
    launch_at: Optional[str] = None
    launch_unconfirmed: bool = False
    pane: Optional[str] = None          # the startup-dialog classification, when classified
    limited: Optional[str] = None       # the limits key holding this session's model now
    on_limit_entry: bool = False        # the newest assistant entry is a limit entry
    model_verified: Optional[bool] = None
    compacted: bool = False
    tool_shell: Optional[bool] = None
    rotation_ready: bool = False        # HANDOFF seen, idle, handoff file newer than the request


@dataclass
class RebootFacts:
    """§11.4 after a reboot: who resumes at once (a turn was open or a step in flight)."""

    resume_now: List[str] = field(default_factory=list)
    resume_lazily: List[str] = field(default_factory=list)


@dataclass
class ReconcileResult:
    """The output of one pure reconcile pass. ``state`` is a new state; the input is untouched."""

    state: RunState
    conditions: List[AlarmCondition] = field(default_factory=list)
    items: List[EventItem] = field(default_factory=list)
    actions: List[ActionItem] = field(default_factory=list)
    sessions: Dict[str, SessionFacts] = field(default_factory=dict)
    reboot: Optional[RebootFacts] = None
    clock_jump: Optional[str] = None
    confirmed: List[str] = field(default_factory=list)       # events confirmed this pass
    verdicts: List[Dict[str, Any]] = field(default_factory=list)   # the verdicts read this pass
    notes: List[str] = field(default_factory=list)

    def condition_keys(self) -> List[str]:
        return [c.key for c in self.conditions]


def _role_of(key: str) -> Tuple[str, Optional[str]]:
    role, _, step = key.partition(":")
    return role, (step or None)


def _limit_key(role: Optional[Role]) -> Optional[str]:
    """The ``limits`` key of a role's model: its expected id, else the alias (§12.4)."""
    if role is None:
        return None
    return role.expected_model_id or role.model


def _alarm_active(state: RunState, alarm_id: str, subject: str) -> bool:
    rec = (state.get("alarms") or {}).get(f"{alarm_id}:{subject}")
    return bool(rec) and not rec.get("cleared_at")


def run_name_patterns(ledger: Ledger) -> List["re.Pattern[str]"]:
    """Every session name this run's grammar can produce (§3, P0-1), with ``g<gen>`` free.

    The driver: ``basename(workspace)-<epic>-driver-g<n>``. A worker step:
    ``<epic>-<step>-<epic>-<role>-<step>-g<n>`` for its implementer and reviewer.
    """
    def pattern(prefix: str, role: str, step: Optional[str]) -> str:
        suffix = session_suffix(ledger.epic, role, step, 1)
        return re.escape(f"{prefix}-{suffix[:-1]}") + r"\d+"

    pats = [pattern(os.path.basename(ledger.workspace.rstrip("/")), "driver", None)]
    for step in ledger.steps:
        if step.worker is not None:
            d = worker_dir_name(ledger.epic, step.id)
            for role in (step.worker.implementer, step.worker.reviewer):
                pats.append(pattern(d, role, step.id))
    return [re.compile(f"^{p}$") for p in dict.fromkeys(pats)]


def _turn_message(reading: SessionReading, stop: Tuple[float, Dict[str, Any]],
                  stops: List[Tuple[float, Dict[str, Any]]]) -> Optional[str]:
    """The final message of the turn that ``stop`` closed.

    The handle's ``last_message`` (§10) when that stop is the latest one;
    otherwise (a later turn, e.g. the operator's own, has rewritten the
    handle) the stop record's ``last_message``, whose tail the lifecycle log
    keeps.
    """
    handle = reading.handle or {}
    if stops and stop is stops[-1]:
        msg = handle.get("last_message")
        stopped = _epoch(handle.get("stopped_at"))
        if isinstance(msg, str) and (stopped is None or stopped >= stop[0] - 2):
            return msg
    msg = stop[1].get("last_message")
    return msg if isinstance(msg, str) else None


def _turn_limited(reading: SessionReading, start: float, end: Optional[float]) -> Optional[Dict[str, Any]]:
    """The limit entry a turn ended on (§12.4: checked before the missing-verdict rule), else None."""
    within = []
    for a in reading.assistant:
        ts = _epoch(a.get("timestamp"))
        if ts is None or ts < start or (end is not None and ts > end + 2):
            continue
        within.append(a)
    if within and is_limit_entry(within[-1]):
        return within[-1]
    return None


class _Pass:
    """The working set of one :func:`reconcile` call."""

    def __init__(self, ledger: Ledger, state: RunState, readings: Readings, now: datetime) -> None:
        self.ledger = ledger
        self.state = state
        self.readings = readings
        self.now = now
        self.by_name = readings.by_name()
        self.result = ReconcileResult(state=state)
        self.reductions: Dict[str, Dict[str, Any]] = {}

    def cond(self, alarm_id: str, subject: str, detail: str = "") -> None:
        self.result.conditions.append(AlarmCondition(alarm_id, subject, detail))

    def red(self, reading: SessionReading) -> Dict[str, Any]:
        if reading.name not in self.reductions:
            self.reductions[reading.name] = reduction_of(reading)
        return self.reductions[reading.name]

    def records(self) -> List[Tuple[str, Dict[str, Any], Optional[SessionReading]]]:
        out = []
        for key, rec in (self.state.get("sessions") or {}).items():
            name = rec.get("name")
            out.append((key, rec, self.by_name.get(name) if isinstance(name, str) else None))
        return out


def _reconcile_clock(p: _Pass) -> bool:
    """§12.7. Returns True when stuck alarms are suppressed this pass (a jump)."""
    sample = p.readings.clock
    if sample is None:
        return False
    prev = p.state.get("clock") or {}
    p.state["clock"] = sample.as_state()
    if not prev:
        return False
    if prev.get("boot_id") and sample.boot_id and prev.get("boot_id") != sample.boot_id:
        facts = RebootFacts()
        steps = p.state.get("steps") or {}
        for key, rec, reading in p.records():
            name = rec.get("name")
            in_flight = any((s.get("inflight") or {}).get("session") == name for s in steps.values())
            turn_open = bool(reading is not None and p.red(reading).get("turn_open"))
            (facts.resume_now if (in_flight or turn_open) else facts.resume_lazily).append(key)
        p.result.reboot = facts
        return True
    try:
        d_wall = sample.wall - float(prev["wall"])
        d_boot = sample.boottime - float(prev["boottime"])
        d_mono = sample.monotonic - float(prev["monotonic"])
    except (KeyError, TypeError, ValueError):
        return False
    if abs(d_wall - d_boot) > CLOCK_JUMP_S:
        p.result.clock_jump = f"wall clock moved {d_wall - d_boot:+.0f}s against boottime (NTP)"
    elif d_boot - d_mono > CLOCK_JUMP_S:
        p.result.clock_jump = f"suspended for about {d_boot - d_mono:.0f}s"
    return p.result.clock_jump is not None


def _reconcile_liveness(p: _Pass) -> None:
    for key, rec, reading in p.records():
        name = rec.get("name") or ""
        if reading is None:
            reading = SessionReading(name=name, key=key)
        red = p.red(reading)
        status = session_status(reading, red)
        facts = SessionFacts(key=key, name=name, alive=reading.alive, status=status)
        facts.turn_open = bool(reading.alive and red.get("turn_open"))
        facts.permission_pending = bool(reading.alive and red.get("permission_pending"))
        facts.attached = bool(reading.attached and reading.attached > 0)
        facts.pane_in_mode = reading.pane_in_mode
        facts.context_tokens = reading.context_tokens
        facts.cache_left_s = reading.cache_left_s
        facts.cache_ttl = reading.cache_ttl
        facts.overage = reading.cache_ttl == 300
        facts.tool_shell = reading.tool_shell
        launch = session_launch_at(rec, reading.handle)
        facts.launch_at = iso(launch)
        old = rec.get("status")
        if reading.alive:
            if old in ("spawning", "resuming"):
                facts.came_up = True
            if old != "live":
                rec["status"] = "live"
        elif old == "live":
            rec["status"] = "dead"
            facts.died = True
        facts.on_limit_entry = is_limit_entry(last_assistant(reading))
        facts.compacted = bool(rec.get("compacted"))
        facts.model_verified = rec.get("model_verified")
        p.result.sessions[key] = facts


def _reconcile_limits(p: _Pass) -> None:
    """§12.4: limit entries ⇒ ``state.limits``; cleared once reset passed and a turn completed."""
    limits = p.state.setdefault("limits", {})
    on_model: Dict[str, List[SessionReading]] = {}
    for key, rec, reading in p.records():
        if reading is None:
            continue
        role = p.ledger.roles.get(_role_of(key)[0])
        lim = _limit_key(role)
        if lim is None:
            continue
        on_model.setdefault(lim, []).append(reading)
        entry = last_assistant(reading)
        if not is_limit_entry(entry):
            continue
        prev = limits.get(lim)
        entry_at = _epoch(entry.get("timestamp"))
        if entry_at is None:
            continue
        if prev and (_epoch(prev.get("entry_at")) or 0) >= entry_at:
            continue     # this entry is already counted
        limits[lim] = limit_record(entry, prev, p.readings.tz or UTC, session=reading.name)
    for lim in list(limits):
        rec = limits[lim] or {}
        until = from_iso(rec.get("until"))
        if until is not None and p.now >= until:
            completed = False
            for reading in on_model.get(lim, []):
                last = last_assistant(reading)
                last_ts = _epoch((last or {}).get("timestamp"))
                stops = _events_of(reading, "stop")
                if last and not last.get("is_api_error") and last_ts is not None and last_ts >= until.timestamp() \
                        and stops and stops[-1][0] >= last_ts - 2:
                    completed = True
            if completed or lim not in on_model:
                del limits[lim]
                continue
        p.cond("usage_limit", lim, f"until {rec.get('until')} ({rec.get('source')}"
               + (f", ladder step {rec.get('ladder_step')}" if rec.get("source") == "ladder" else "") + ")")
    for key, facts in p.result.sessions.items():
        facts.limited = model_limited(p.ledger, p.state, _role_of(key)[0], p.now)


def _reconcile_confirmations(p: _Pass) -> None:
    for entry in p.state.get("outbox") or []:
        if entry.get("confirmed_at") or not entry.get("sends"):
            continue
        reading = p.by_name.get(entry.get("session"))
        if reading is None:
            continue
        conf = delivery_confirmation(entry, reading)
        if conf is None:
            continue
        entry["confirmed_at"] = iso(from_iso(conf["prompt_at"])) or iso(p.now)
        p.result.confirmed.append(str(entry.get("event")))


def _worker_kind(ledger: Ledger, key: str) -> Optional[str]:
    """``implementer`` / ``reviewer`` for a worker session key, None for the driver."""
    role, step_id = _role_of(key)
    if step_id is None or not ledger.has_step(step_id):
        return None
    step = ledger.step(step_id)
    if step.worker is None:
        return None
    if role == step.worker.implementer:
        return "implementer"
    if role == step.worker.reviewer:
        return "reviewer"
    return None


def _session_key_of(state: RunState, name: str) -> Optional[str]:
    for key, rec in (state.get("sessions") or {}).items():
        if rec.get("name") == name:
            return key
    return None


def _apply_step_verdict(p: _Pass, step_id: str, v: Verdict, vr: VerdictReading, event: str,
                        at: datetime, from_key: str, worker: Optional[str]) -> str:
    """Apply one covering verdict to a step; returns what happened (for the log)."""
    ledger, state = p.ledger, p.state
    if not ledger.has_step(step_id):
        return "unknown step"
    step = ledger.step(step_id)
    rec = _step_state(state, step_id)
    rec["inflight"] = None
    rec["last_verdict"] = {"event": event, "line": vr.line, "at": iso(at), "session": from_key}
    status = rec.get("status", PENDING)

    def move(new: str) -> bool:
        if can_transition(status, new):
            set_status(rec, new)
            return True
        return False

    if v.verb == V_HALT:
        if worker is not None:
            p.result.items.append(EventItem(ITEM_WORKER_SETTLED, "driver", step_id, {
                "role": _role_of(from_key)[0], "worker": worker, "verdict": vr.line, "event": event}))
            return "worker HALT → worker_settled"
        return "halted" if move(HALTED) else f"HALT ignored in {status}"
    if v.verb == V_DONE:
        if worker == "implementer" and step.worker is not None:
            p.result.items.append(EventItem(ITEM_REVIEW_DUE, f"{step.worker.reviewer}:{step_id}", step_id, {
                "implementer": _role_of(from_key)[0], "verdict": vr.line, "event": event}))
            return "implementer DONE → review_due"
        p.result.items.append(EventItem(ITEM_WORKER_SETTLED, "driver", step_id, {
            "role": _role_of(from_key)[0], "worker": worker, "verdict": vr.line, "event": event}))
        return "reviewer DONE → worker_settled"
    if v.verb == V_ADVANCED:
        if v.shas:
            rec["shas"] = {name or BARE_SHA: sha for name, sha in v.shas}
        if status == FAILED:          # §6.1: failed re-enters due on ADVANCED (a retry)
            if move(DUE):
                rec["due_since"] = iso(at)
                return "retry: failed → due"
            return f"ADVANCED ignored in {status}"
        if step.check is None and step.role_kind == ROLE_KIND_DRIVER:
            if move(DONE):              # §5.4: no check ⇒ ADVANCED marks it done (unchecked)
                rec["done_at"] = iso(at)
                rec["advanced_at"] = iso(at)
                rec["by"] = "verdict"
                return "done (unchecked)"
            return f"ADVANCED ignored in {status}"
        if move(VERIFYING):
            rec["advanced_at"] = iso(at)
            return "verifying"
        return f"ADVANCED ignored in {status}"
    if v.verb == V_WAITING:
        text, until = vr.wait.get(step_id, (v.text, None))
        if move(WAITING):
            rec["wait"] = {"text": text, "until": iso(until), "event": event, "at": iso(at)}
            return "waiting"
        return f"WAITING ignored in {status}"
    if v.verb == V_ASKED:
        qid = v.question or ""
        qs = rec.setdefault("questions", [])
        if qid not in qs:
            qs.append(qid)
        state.setdefault("questions", {})[qid] = {
            "step": step_id, "approval": None, "asked_event": event, "status": "open",
            "answer_event": None, "asked_at": iso(at)}
        return "asked" if move(ASKED) else f"ASKED recorded; status stays {status}"
    if v.verb == V_HANDOFF:
        return "handoff"
    return "ignored"


def _apply_approval_verdict(p: _Pass, approval_id: str, v: Verdict, event: str, at: datetime) -> str:
    state = p.state
    if v.verb == V_ASKED:
        qid = v.question or ""
        state["approval_question"] = {"id": approval_id, "question": qid}
        state.setdefault("questions", {})[qid] = {
            "step": None, "approval": approval_id, "asked_event": event, "status": "open",
            "answer_event": None, "asked_at": iso(at)}
        return "approval asked"
    if v.verb == V_ADVANCED:
        return "grant verified"
    return v.verb.lower()


#: A prompt and the ``turn_start`` it opened are at most this far apart (hook vs transcript clocks).
PROMPT_TURN_SLACK_S = 5.0


def _event_turns(reading: SessionReading, entry: Dict[str, Any],
                 starts: List[Tuple[float, Dict[str, Any]]]) -> List[float]:
    """The ``turn_start`` times of the turns that answer an event, oldest first.

    The first turn after the first send (the event's own turn), the first one
    after ``reminded_at`` (the ``VERDICT?`` turn), and any turn whose prompt
    names the event (an operator turn that does is not exempt). A turn whose
    prompt carries no event id never answers one (§10 exemptions).
    """
    out: List[float] = []
    t0 = _first_send(entry)
    if t0 is None:
        return out
    first = _first_at_or_after(starts, t0)
    if first is not None:
        out.append(first[0])
    reminded = _epoch(entry.get("reminded_at"))
    if reminded is not None:
        rem = _first_at_or_after(starts, reminded)
        if rem is not None:
            out.append(rem[0])
    pattern = _event_id_re(str(entry.get("event")))
    for prompt in reading.prompts:
        pts = _epoch(prompt.get("timestamp"))
        if pts is None or pts < t0 or not pattern.search(str(prompt.get("text") or "")):
            continue
        near = [ts for ts, _ in starts if abs(ts - pts) <= PROMPT_TURN_SLACK_S]
        if near:
            out.append(min(near, key=lambda ts: abs(ts - pts)))
    return sorted(set(out))


def _reconcile_verdicts(p: _Pass) -> None:
    """§12.2 + §10: read the verdict of every turn that answers a confirmed outbox entry.

    A turn counts once it has a ``stop`` (it ended). A turn that ended on a
    usage-limit entry is recorded as ``limited_at`` and never counts as a
    missing verdict (§12.4 runs first). The entry's ``verdict`` record keeps
    what was covered across turns, so a reminder turn completes a partial one.
    """
    ledger, state = p.ledger, p.state
    ledger_ids = [s.id for s in ledger.steps] + list(ledger.approvals)
    for entry in state.get("outbox") or []:
        if not entry.get("confirmed_at") or entry.get("needs_verdict") is False:
            continue
        vstate = entry.get("verdict")
        if isinstance(vstate, dict) and vstate.get("complete"):
            continue
        name = entry.get("session")
        reading = p.by_name.get(name)
        key = _session_key_of(state, name) if isinstance(name, str) else None
        if reading is None or key is None:
            continue
        if not isinstance(vstate, dict):
            vstate = {"line": None, "at": None, "covered": [], "missing": [], "invalid": None,
                      "turns": [], "after_reminder": False, "limited_at": None, "complete": False}
        event = str(entry.get("event"))
        steps = list(entry.get("steps") or [])
        approvals = list(entry.get("approvals") or [])
        ids = list(dict.fromkeys([*steps, *approvals]))
        worker = _worker_kind(ledger, key)
        kinds = list(entry.get("kinds") or [])
        worker_repos = {}
        for sid in steps:
            if ledger.has_step(sid) and ledger.step(sid).worker is not None:
                worker_repos[sid] = [r.name for r in ledger.step(sid).worker.repos]
        starts = _events_of(reading, "turn_start")
        stops = _events_of(reading, "stop")
        covered = list(vstate.get("covered") or [])
        reminded = _epoch(entry.get("reminded_at"))
        interrupted = False
        for start in _event_turns(reading, entry, starts):
            label = "reminder" if reminded is not None and start >= reminded else "event"
            if any(abs(float(t) - start) < 1e-6 for t in vstate.get("turns") or []):
                continue
            stop = _first_at_or_after(stops, start)
            # Esc fires no Stop: a stop past another turn_start belongs to that later turn. The
            # interrupted turn is unsettled, no verdict is read from it (and no limit either).
            nxt = _first_at_or_after(starts, start + 1e-6)
            if nxt is not None and (stop is None or stop[0] > nxt[0]):
                interrupted = True
                continue
            limit = _turn_limited(reading, start, stop[0] if stop else None)
            if limit is not None:
                vstate["limited_at"] = limit.get("timestamp")
                if stop is not None:
                    vstate.setdefault("turns", []).append(start)
                continue
            if stop is None:
                continue            # the turn is still open
            message = _turn_message(reading, stop, stops)
            vr = read_verdict(message, steps=steps, approvals=approvals, from_driver=worker is None,
                              rotate="rotate" in kinds, grants=state.get("grants") or {},
                              worker_repos=worker_repos, ledger_ids=ledger_ids)
            at = datetime.fromtimestamp(stop[0], UTC)
            vstate.setdefault("turns", []).append(start)
            vstate["line"], vstate["invalid"], vstate["at"] = vr.line, vr.invalid, iso(at)
            if label == "reminder":
                vstate["after_reminder"] = True
            applied: Dict[str, str] = {}
            for i, v in vr.covered.items():
                if i in covered:
                    continue
                if i in steps:
                    applied[i] = _apply_step_verdict(p, i, v, vr, event, at, key, worker)
                else:
                    applied[i] = _apply_approval_verdict(p, i, v, event, at)
                covered.append(i)
            if vr.handoff is not None:
                rec = (state.get("sessions") or {}).get(key) or {}
                rotation = rec.get("rotation")
                if isinstance(rotation, dict):
                    rotation["handoff"] = {"path": vr.handoff.text, "at": iso(at), "event": event}
            if vr.valid and not ids and (vr.handoff is not None or "rotate" not in kinds):
                covered.append("*")
            p.result.verdicts.append({"event": event, "session": key, "line": vr.line,
                                      "invalid": vr.invalid, "applied": applied,
                                      "unverified": dict(vr.unverified), "turn": label})
        vstate["covered"] = covered
        needed = ids if ids else ["*"]
        missing = [i for i in needed if i not in covered]
        vstate["missing"] = missing
        vstate["complete"] = not missing
        entry["verdict"] = vstate
        unanswered = interrupted and _settled_idle(p, reading, starts, stops)
        if (unanswered and reminded is not None and not vstate.get("after_reminder")
                and any(ts >= reminded for ts, _ in starts)):
            # §10: the one reminder was sent and a turn has run since (the reminder turn itself may
            # have been interrupted): there is no valid verdict after it, so never remind again.
            vstate["after_reminder"] = True
        _verdict_followups(p, entry, vstate, key, unanswered=unanswered)


def _settled_idle(p: _Pass, reading: SessionReading, starts: List[Tuple[float, Dict[str, Any]]],
                  stops: List[Tuple[float, Dict[str, Any]]]) -> bool:
    """The reducer says idle, no turn is open and the latest turn has stopped.

    The reducer check does the work (a stop with background tasks still running leaves the latest
    turn stopped but the state ``running``). The latest-stop check is normally implied by it; it
    stays as a cheap guard against a reducer idle with an unstopped latest turn.
    """
    red = p.red(reading)
    if red.get("state_hint") != "idle" or red.get("turn_open") or not starts:
        return False
    return _first_at_or_after(stops, starts[-1][0]) is not None


def _verdict_followups(p: _Pass, entry: Dict[str, Any], vstate: Dict[str, Any], key: str,
                       unanswered: bool = False) -> None:
    """Missing (§10): one reminder, then ``verdict_missing``; a limited turn is never missing.

    ``unanswered``: the event turn was interrupted (a later ``turn_start`` before any stop of it)
    and the session is idle again. That turn ended without a verdict, so the same one-reminder
    rule applies (unless a usage limit was seen, §12.4 first).
    """
    missing = vstate.get("missing") or []
    if not missing:
        return
    event = str(entry.get("event"))
    read_turns = (vstate.get("line") is not None or vstate.get("invalid") is not None
                  or (unanswered and vstate.get("limited_at") is None))
    if not read_turns:
        return          # no turn ended yet (or only a limited one): nothing is missing
    if vstate.get("after_reminder"):
        p.cond("verdict_missing", event, f"no valid verdict for {', '.join(missing)} after the reminder"
               + (f" (last line: {vstate.get('line')!r}, {vstate.get('invalid')})" if vstate.get("invalid") else ""))
        return
    if not entry.get("reminded_at"):
        p.result.actions.append(ActionItem(ACTION_VERDICT_REMINDER, key, str(entry.get("session")), {
            "event": event, "missing": list(missing), "invalid": vstate.get("invalid")}))


def _reconcile_stalls(p: _Pass, suppress_stuck: bool) -> None:
    """§12.3 stuck and §12.6 open prompts, for every live session record."""
    now_s = p.now.timestamp()
    for key, rec, reading in p.records():
        if reading is None or not reading.alive:
            continue
        name = reading.name
        red = p.red(reading)
        perm_since = permission_open_since(reading)
        if perm_since is not None and now_s - perm_since > OPEN_PROMPT_ALARM_S:
            p.cond("permission_open", name,
                   f"a permission prompt open since {iso(datetime.fromtimestamp(perm_since, UTC))}")
        fg = reading.foreground_tool or {}
        ask_since = _epoch(fg.get("timestamp")) if fg.get("tool") == "AskUserQuestion" else None
        if ask_since is not None and now_s - ask_since > OPEN_PROMPT_ALARM_S:
            p.cond("ask_user_question_open", name,
                   f"AskUserQuestion in flight since {iso(datetime.fromtimestamp(ask_since, UTC))}")
        # Stuck: a turn open, no tool shell under the owner, no transcript or subagent growth for
        # stuck_after. A prompt waiting on a human, or a turn that ended on a usage limit, has its
        # own alarm and is not stuck.
        if not red.get("turn_open") or perm_since is not None or ask_since is not None:
            continue
        if is_limit_entry(last_assistant(reading)) or reading.tool_shell is True:
            continue
        role = p.ledger.roles.get(_role_of(key)[0])
        stuck_after = role.stuck_after_s if role is not None else DEFAULT_STUCK_AFTER_S
        activity = reading.activity_at
        if activity is None:
            launch = session_launch_at(rec, reading.handle)
            activity = launch.timestamp() if launch is not None else None
        if activity is None or now_s - activity < stuck_after:
            continue
        if suppress_stuck and not _alarm_active(p.state, "session_stuck", name):
            continue        # §12.7: no new stuck alarm in the pass that saw a clock jump
        p.cond("session_stuck", name,
               f"turn open, no tool shell, no transcript growth for {format_duration(int(now_s - activity))}")


def _reconcile_model_and_compaction(p: _Pass) -> None:
    """§12.5 (first model after the launch) and §11.6 (a compact boundary after it)."""
    for key, rec, reading in p.records():
        if reading is None:
            continue
        launch = session_launch_at(rec, reading.handle)
        if launch is None:
            continue
        stamp = iso(launch)
        handle = reading.handle or {}
        model = handle.get("model")
        if isinstance(model, str) and model:     # a legacy handle without a model is skipped
            check = rec.get("model_check")
            if not (isinstance(check, dict) and check.get("launch") == stamp):
                first = first_model_entry(reading, launch)
                if first is not None:
                    ok = asl.model_matches(reading.expected_model_id, model, first.get("model"))
                    check = {"launch": stamp, "ok": ok, "observed": first.get("model"),
                             "expected": reading.expected_model_id or model}
                    rec["model_check"] = check
                    rec["model_verified"] = ok
            if isinstance(check, dict) and check.get("launch") == stamp and check.get("ok") is False:
                p.cond("model_mismatch", reading.name,
                       f"expected {check.get('expected')}, the first reply after {stamp} came from "
                       f"{check.get('observed')}")
        after = [b for b in reading.compactions
                 if (_epoch(b.get("timestamp")) or 0) >= launch.timestamp()]
        if after and not rec.get("compacted"):
            rec["compacted"] = True
            rec["compacted_at"] = after[0].get("timestamp")
        if rec.get("compacted"):
            p.cond("compaction", reading.name,
                   f"compacted at {rec.get('compacted_at')}; the next event rotates")
        facts = p.result.sessions.get(key)
        if facts is not None:
            facts.compacted = bool(rec.get("compacted"))
            facts.model_verified = rec.get("model_verified")


def _reconcile_dialogs(p: _Pass) -> None:
    """§12.11: a startup dialog on a launched session whose prompt is not confirmed."""
    for key, rec, reading in p.records():
        if reading is None or not reading.alive:
            continue
        launch = session_launch_at(rec, reading.handle)
        if launch is None or not launch_prompt_unconfirmed(p.state, rec):
            continue
        facts = p.result.sessions.get(key)
        if facts is not None:
            facts.launch_unconfirmed = True
        if not reading.captured:
            continue
        cls = classify_pane(reading.pane_text)
        if facts is not None:
            facts.pane = cls.kind
        name = reading.name
        if cls.kind == PANE_DIALOG:
            listed = match_startup_dialog(p.ledger, reading.pane_text or "")
            if listed is not None and not dialog_answered(rec, launch, listed.id):
                p.result.actions.append(ActionItem(ACTION_STARTUP_DIALOG_ANSWER, key, name, {
                    "dialog": listed.id, "key": listed.key, "launch": iso(launch), "detail": cls.detail}))
                continue
            why = "still showing after its listed key" if listed is not None else "not in startup_dialogs"
            p.cond("startup_dialog", name, f"{cls.detail} ({why})")
        elif cls.kind == PANE_UNREADABLE and _alarm_active(p.state, "startup_dialog", name):
            p.cond("startup_dialog", name, "the pane is unreadable; the dialog was last seen on it")


def _reconcile_foreign(p: _Pass) -> None:
    """§12.8 (review m4): foreign handles and orphan sessions raise alarms; none is adopted."""
    known: set = set()
    for _, rec, _ in p.records():
        if rec.get("name"):
            known.add(rec.get("name"))
        for parked in rec.get("parked") or []:
            if (parked or {}).get("name"):
                known.add(parked["name"])
    for reading in p.readings.sessions:
        if reading.kind == SESSION_FOREIGN and reading.name not in known:
            p.cond("foreign_handle", reading.name,
                   f"a handle with run id {p.ledger.run_id} that this run did not create")
    patterns = run_name_patterns(p.ledger)
    handles = set(p.readings.handle_names)
    for name in p.readings.amux_names:
        if name in known or name in handles:
            continue
        if any(pt.match(name) for pt in patterns):
            p.cond("orphan_session", name, "a session named by this run's grammar with no record and no handle")


def _reconcile_questions(p: _Pass) -> None:
    """§15.2: resolved ⇒ one ``answer`` item (never twice); withdrawn ⇒ closed, no item."""
    snap = p.readings.questions
    if snap is None:
        if p.readings.questions_error:
            p.result.notes.append(f"question queue unreadable: {p.readings.questions_error}")
        return
    state = p.state
    for qid, q in (state.get("questions") or {}).items():
        if not isinstance(q, dict) or q.get("status") != "open":
            continue
        word = snap.classify(qid)
        if word == "resolved":
            q["status"] = "resolved"
            q["resolved_at"] = iso(p.now)
            entry = snap.entries.get(qid)
            p.result.items.append(EventItem(ITEM_ANSWER, "driver", q.get("step"), {
                "question": qid, "approval": q.get("approval"), "answer": entry.text if entry else "",
                "path": entry.path if entry else None, "asked_event": q.get("asked_event")}))
        elif word == "withdrawn":
            q["status"] = "withdrawn"
            q["closed_at"] = iso(p.now)
        else:
            continue
        aq = state.get("approval_question") or {}
        if aq.get("question") == qid:
            state["approval_question"] = None


def _reconcile_rotation(p: _Pass) -> None:
    for key, rec, reading in p.records():
        facts = p.result.sessions.get(key)
        rotation = rec.get("rotation")
        if facts is None or reading is None or not isinstance(rotation, dict):
            continue
        requested = from_iso(rotation.get("requested_at"))
        facts.rotation_ready = bool(
            rotation.get("handoff") and facts.status == ST_IDLE and requested is not None
            and reading.handoff_mtime is not None and reading.handoff_mtime > requested.timestamp())


def reconcile(ledger: Ledger, state: RunState, readings: Readings, now: datetime) -> ReconcileResult:
    """Tick step 2–3 (§23): the session and question reconcile, pure over its inputs.

    Order: clock (§12.7) → liveness (§12.1) → usage limits (§12.4, before any
    verdict) → delivery confirmations (§9.3) → verdicts and their application
    (§10, §14.3) → stuck and open prompts (§12.3, §12.6) → model check and
    compaction (§12.5, §11.6) → startup dialogs (§12.11) → foreign handles and
    orphans (§12.8) → questions (§15.2) → rotation readiness (§11.3).

    The input ``state`` is not modified: the result carries a new one. Nothing
    is sent, spawned or notified; the conditions go to the alarm engine
    (50-08), the items and actions to the dispatcher (50-06).
    """
    now = require_aware(now)
    p = _Pass(ledger, json.loads(json.dumps(state)), readings, now)
    ensure_step_states(ledger, p.state)
    suppress = _reconcile_clock(p)
    _reconcile_liveness(p)
    _reconcile_limits(p)
    _reconcile_confirmations(p)
    _reconcile_verdicts(p)
    _reconcile_stalls(p, suppress)
    _reconcile_model_and_compaction(p)
    _reconcile_dialogs(p)
    _reconcile_foreign(p)
    _reconcile_questions(p)
    _reconcile_rotation(p)
    return p.result
