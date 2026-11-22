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
    VERIFYING: (DONE, FAILED),
    WAITING: (INFLIGHT, VERIFYING, DONE, FAILED, WAITING, ASKED, HALTED),
    ASKED: (INFLIGHT, VERIFYING, WAITING, ASKED, HALTED, DONE),
    HALTED: (),
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
