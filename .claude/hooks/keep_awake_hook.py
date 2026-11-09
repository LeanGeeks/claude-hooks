#!/usr/bin/env python3
"""Keep the machine awake while a Claude Code session is working (task 44).

One logind inhibitor lock (``systemd-inhibit --what=sleep:idle --mode=block``)
per working session. The lock is *held by a process*, not by a file: logind
drops it the moment the holder exits, so a lock can never outlive its holder.
The holder exits on its own when the session it serves is gone or idle, which
is what makes the design crash-safe — cleanup never depends on a later hook
firing.

Process shape (one per working session)::

    systemd-inhibit --what=sleep:idle ... python3 keep_awake_hook.py --watch <sid>
                                          └─ the watcher; when it exits, the
                                             lock is released

Hook events (``--event``), all fail-open and silent on stdout:

- ``UserPromptSubmit``: acquire (or refresh) the session's lock; the turn is open.
- ``Notification`` (matcher ``permission_prompt``): note that a terminal
  permission dialog is up, so the session counts as parked until the human
  answers (any answer, including Esc, writes the transcript).
- ``Stop``: the turn ended. Release now, unless background work is
  outstanding (``background_tasks`` in the payload, or a live tool shell).
- ``SessionEnd``: release.

The watcher (``--watch``) releases when any of these holds, checked every
``POLL`` seconds (owner exit is detected immediately through a pidfd):

1. The owning ``claude`` process exited — crash, ``kill -9``, OOM, closed pane.
2. No hook event for ``MAX_HOURS`` (default 24h). The backstop for a session
   that is alive but wedged. A relay-parked prompt waits at most 12h.
3. The turn is open, nothing is parked, and the transcript (or a subagent
   transcript) has not changed for ``IDLE`` minutes (default 30). This is the
   Esc case: Claude Code fires no ``Stop`` on an interrupted turn (verified
   live, 2026-11-08). A tool shell still running then (a background dev
   server; foreground Bash is capped at 10 min) holds at most
   ``BACKGROUND_HOURS`` since the last activity.
4. The turn ended with background work outstanding, and that work is done or
   ``BACKGROUND_HOURS`` (default 2h) have passed since the turn ended.

"Parked" is a blocking hook under the owner (``permission_request_hook.py``
waiting on the relay) or a terminal permission dialog (``Notification``),
each until the main transcript gets a ``user`` entry stamped after it — the
answer, a denial or an Esc. "Running" is a live tool shell under the owner
(Claude Code's Bash tool runs ``bash -c source …/shell-snapshots/…``).

State lives in ``~/.claude/keep-awake/<session_id>.json``; it describes the
holder but is never the lock itself. A state file whose holder is dead is
swept on the next hook event.

Linux only (``/proc``, logind). Without ``systemd-inhibit`` every event is a
no-op.

Configuration (environment, read by the hook and inherited by the watcher):

- ``CLAUDE_KEEP_AWAKE=0`` disables (an existing lock is released on the next event).
- ``CLAUDE_KEEP_AWAKE_IDLE_MINUTES`` (default 30).
- ``CLAUDE_KEEP_AWAKE_MAX_HOURS`` (default 24; 0 disables the backstop).
- ``CLAUDE_KEEP_AWAKE_BACKGROUND_HOURS`` (default 2).

``--status`` prints the held locks; ``--release-all`` drops every lock (used
by the uninstaller).
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import select
import shutil
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

SELF_NAME = "keep_awake_hook.py"
SELF_PATH = str(Path(__file__).resolve())

# Claude Code's Bash tool (foreground and background) runs commands as
# ``/bin/bash -c source ~/.claude/shell-snapshots/snapshot-…``.
TOOL_SHELL_MARKER = "/shell-snapshots/"
# Hooks of ours that block for a human answer. Short-lived hooks are not
# listed: a Stop-time scan would otherwise see a sibling Stop hook as "busy".
BLOCKING_HOOKS = ("permission_request_hook.py",)

WHO = "claude-hooks keep-awake"
LOG_MAX_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, ""))
    except ValueError:
        return default


def state_dir() -> Path:
    return Path(os.environ.get("CLAUDE_KEEP_AWAKE_STATE_DIR")
                or Path.home() / ".claude" / "keep-awake")


def log_path() -> Path:
    return Path(os.environ.get("CLAUDE_KEEP_AWAKE_LOG")
                or Path.home() / ".claude" / "keep_awake.log")


def enabled() -> bool:
    return os.environ.get("CLAUDE_KEEP_AWAKE", "1").strip().lower() not in (
        "0", "off", "false", "no")


def idle_seconds() -> float:
    return _env_float("CLAUDE_KEEP_AWAKE_IDLE_MINUTES", 30.0) * 60.0


def max_hold_seconds() -> float:
    return _env_float("CLAUDE_KEEP_AWAKE_MAX_HOURS", 24.0) * 3600.0


def background_seconds() -> float:
    return _env_float("CLAUDE_KEEP_AWAKE_BACKGROUND_HOURS", 2.0) * 3600.0


def poll_seconds() -> float:
    return max(0.05, _env_float("CLAUDE_KEEP_AWAKE_POLL_SECONDS", 30.0))


def inhibit_binary() -> Optional[str]:
    return os.environ.get("CLAUDE_KEEP_AWAKE_INHIBIT") or shutil.which("systemd-inhibit")


# ---------------------------------------------------------------------------
# Logging (always on, small, self-capping)
# ---------------------------------------------------------------------------

def log(session_id: str, message: str) -> None:
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > LOG_MAX_BYTES:
                os.replace(path, str(path) + ".1")
        except FileNotFoundError:
            pass
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with open(path, "a") as f:
            f.write(f"{stamp} pid={os.getpid()} {session_id[:8] or '-'} {message}\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# /proc helpers
# ---------------------------------------------------------------------------

def _proc_stat_fields(pid: int) -> Optional[List[str]]:
    """Fields 3.. of /proc/<pid>/stat. ``comm`` may contain spaces and ')',
    so split on the LAST ') ' (same rule as permission_state_store)."""
    try:
        with open(f"/proc/{pid}/stat", "r") as f:
            return f.read().rsplit(") ", 1)[1].split()
    except Exception:
        return None


def proc_start_ticks(pid: int) -> Optional[int]:
    fields = _proc_stat_fields(pid)
    try:
        return int(fields[19]) if fields else None   # field 22
    except (IndexError, ValueError):
        return None


def proc_start_epoch(pid: int) -> Optional[float]:
    """Wall-clock start time of a process (boot time + start ticks)."""
    ticks = proc_start_ticks(pid)
    if ticks is None:
        return None
    try:
        with open("/proc/stat") as f:
            btime = next(int(l.split()[1]) for l in f if l.startswith("btime "))
        return btime + ticks / os.sysconf("SC_CLK_TCK")
    except Exception:
        return None


def proc_ppid(pid: int) -> Optional[int]:
    fields = _proc_stat_fields(pid)
    try:
        return int(fields[1]) if fields else None    # field 4
    except (IndexError, ValueError):
        return None


def proc_is_zombie(pid: int) -> bool:
    fields = _proc_stat_fields(pid)
    return bool(fields) and fields[0] in ("Z", "X")


def proc_comm(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/comm", "r") as f:
            return f.read().strip()
    except Exception:
        return ""


def proc_cmdline(pid: int) -> List[str]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
    except Exception:
        return []
    return [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]


def is_alive(pid: Optional[int], ticks: Optional[int]) -> bool:
    """Same process still running: the PID exists, is not a zombie, and its
    start time matches (guards against PID reuse). Unknown ticks = not alive."""
    if not pid or ticks is None:
        return False
    return proc_start_ticks(pid) == ticks and not proc_is_zombie(pid)


def _all_pids() -> List[int]:
    try:
        return [int(n) for n in os.listdir("/proc") if n.isdigit()]
    except Exception:
        return []


def descendants(root: int) -> List[int]:
    children: dict = {}
    for pid in _all_pids():
        ppid = proc_ppid(pid)
        if ppid is not None:
            children.setdefault(ppid, []).append(pid)
    out: List[int] = []
    stack = list(children.get(root, []))
    while stack:
        pid = stack.pop()
        out.append(pid)
        stack.extend(children.get(pid, []))
    return out


def _looks_like_claude(pid: int) -> bool:
    if proc_comm(pid) == "claude":
        return True
    argv = proc_cmdline(pid)
    if argv and os.path.basename(argv[0]) == "claude":
        return True
    # npm install: ``node …/@anthropic-ai/claude-code/cli.js``
    return any("@anthropic-ai/claude-code" in a for a in argv[:3])


def find_owner_pid() -> Optional[int]:
    """The ``claude`` process this hook runs under (walk up at most 8 parents)."""
    override = os.environ.get("CLAUDE_KEEP_AWAKE_OWNER_PID")
    if override:
        try:
            return int(override)
        except ValueError:
            return None
    pid = os.getppid()
    for _ in range(8):
        if pid is None or pid <= 1:
            return None
        if _looks_like_claude(pid):
            return pid
        pid = proc_ppid(pid)
    return None


# ---------------------------------------------------------------------------
# State files
# ---------------------------------------------------------------------------

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def _safe_id(session_id: str) -> Optional[str]:
    return session_id if _SAFE_ID.match(session_id) and session_id not in (".", "..") else None


def state_file(session_id: str) -> Path:
    return state_dir() / f"{session_id}.json"


@contextmanager
def session_lock(session_id: str) -> Iterator[None]:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(d / f"{session_id}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.utime(fd)            # age = last use; sweep() deletes only stale ones
        except Exception:
            pass
        yield
    finally:
        os.close(fd)


def read_state(session_id: str) -> Optional[dict]:
    try:
        with open(state_file(session_id)) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_state(session_id: str, state: dict) -> None:
    path = state_file(session_id)
    tmp = path.with_suffix(f".json.tmp{os.getpid()}")
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def remove_state(session_id: str) -> None:
    # The .lock file stays: unlinking a file another process is blocked on in
    # flock() would let two processes "hold" the lock. sweep() removes lock
    # files that have had no state file for a day.
    try:
        state_file(session_id).unlink()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Holder lifecycle
# ---------------------------------------------------------------------------

def holder_alive(state: dict) -> bool:
    return is_alive(state.get("holder_pid"), state.get("holder_ticks"))


def owner_alive(state: dict) -> bool:
    return is_alive(state.get("owner_pid"), state.get("owner_ticks"))


def kill_holder(state: dict) -> None:
    pid = state.get("holder_pid")
    if not holder_alive(state):
        return
    try:
        os.killpg(pid, signal.SIGTERM)   # holder leads its own session/group
        os.killpg(pid, signal.SIGCONT)   # a stopped process acts on SIGTERM only once continued
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass


def spawn_holder(session_id: str, cwd: str) -> Optional[Tuple[int, int]]:
    binary = inhibit_binary()
    if not binary:
        log(session_id, "systemd-inhibit not found; not holding a lock")
        return None
    why = f"Claude session {session_id[:8]} working in {cwd or '?'}"
    cmd = [binary, "--what=sleep:idle", "--mode=block", f"--who={WHO}",
           f"--why={why}", sys.executable, SELF_PATH, "--watch", session_id]
    # Detached: own session (a process-group kill of the hook must not reach
    # it), no inherited pipes (Claude Code waits for the hook's stdout/stderr
    # to close), cwd "/" (never pin a workspace mount).
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True,
                            close_fds=True, cwd="/")
    ticks = None
    for _ in range(20):                  # /proc entry appears immediately; be safe
        ticks = proc_start_ticks(proc.pid)
        if ticks is not None:
            break
        time.sleep(0.005)
    if ticks is None:
        return None
    return proc.pid, ticks


def acquire(session_id: str, payload: dict, now: float, *, turn_open: bool = True,
            keep_prompt: bool = False) -> Optional[dict]:
    """Make sure the session has a live holder; refresh its state. Caller holds the lock."""
    owner = find_owner_pid()
    owner_ticks = proc_start_ticks(owner) if owner else None
    if owner is None or owner_ticks is None:
        log(session_id, "owning claude process not found; not holding a lock")
        return None

    state = read_state(session_id)
    same_owner = bool(state) and state.get("owner_pid") == owner \
        and state.get("owner_ticks") == owner_ticks
    if state and holder_alive(state) and same_owner:
        state["last_event_at"] = now
        state["turn_open"] = turn_open
        if not keep_prompt:
            state["permission_prompt_at"] = None
        tp = payload.get("transcript_path")
        if isinstance(tp, str) and tp:
            state["transcript_path"] = tp
        write_state(session_id, state)
        return state

    if state:
        kill_holder(state)

    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    tp = payload.get("transcript_path")
    state = {
        "session_id": session_id,
        "owner_pid": owner,
        "owner_ticks": owner_ticks,
        "holder_pid": None,
        "holder_ticks": None,
        "transcript_path": tp if isinstance(tp, str) else None,
        "cwd": cwd,
        "acquired_at": now,
        "last_event_at": now,
        "turn_open": turn_open,
        "stopped_at": None,
        "permission_prompt_at": None,
    }
    held = spawn_holder(session_id, cwd)
    if not held:
        remove_state(session_id)
        return None
    state["holder_pid"], state["holder_ticks"] = held
    write_state(session_id, state)
    log(session_id, f"acquired (owner={owner} holder={held[0]} cwd={cwd})")
    return state


def release(session_id: str, reason: str) -> None:
    """Drop the session's lock. Caller holds the lock."""
    state = read_state(session_id)
    if state is None:
        return
    kill_holder(state)
    remove_state(session_id)
    log(session_id, f"released: {reason}")


# ---------------------------------------------------------------------------
# The busy predicate
# ---------------------------------------------------------------------------

def _subagent_dir(tp: str) -> Path:
    return Path(tp[:-len(".jsonl")] if tp.endswith(".jsonl") else tp) / "subagents"


def transcript_activity(state: dict) -> float:
    """Latest mtime of the session transcript and its subagent transcripts."""
    tp = state.get("transcript_path")
    if not tp:
        return 0.0
    latest = 0.0
    candidates = [Path(tp)]
    try:
        candidates.extend(_subagent_dir(tp).glob("*.jsonl"))
    except Exception:
        pass
    for p in candidates:
        try:
            latest = max(latest, p.stat().st_mtime)
        except Exception:
            pass
    return latest


TAIL_BYTES = 256 * 1024


def _parse_ts(value) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def user_entry_after(transcript_path: Optional[str], since: float) -> bool:
    """True if the main transcript has a ``user`` entry stamped after ``since``.

    A user entry after a permission prompt or a parked hook started is the
    answer: the tool's result (run, denied, or Esc'd) or a new prompt.
    Assistant entries do not count (Claude Code may write the tool_use line
    after the prompt is up), and subagent transcripts are separate files, so a
    sibling subagent's chatter cannot void the hold. Unreadable → False
    (keep holding; the idle rules and the backstop still apply).
    """
    if not transcript_path:
        return False
    try:
        with open(transcript_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            tail = f.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return False
    for line in reversed(tail):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "user":
            continue
        ts = _parse_ts(entry.get("timestamp"))
        if ts is not None and ts > since:
            return True
    return False


def running_work(owner_pid: int) -> Tuple[bool, Optional[float]]:
    """(tool shell running, earliest start of a blocking hook) under the owner."""
    tool = False
    parked_since: Optional[float] = None
    for pid in descendants(owner_pid):
        argv = proc_cmdline(pid)
        cmd = " ".join(argv)
        if not cmd or SELF_NAME in cmd:
            continue
        if TOOL_SHELL_MARKER in cmd:
            tool = True
        # Match on the script name, not the full path: the hook command is
        # built from $HOME/.claude/hooks, which a symlink may make differ from
        # this file's resolved location.
        if any(f"/{h}" in cmd for h in BLOCKING_HOOKS):
            started = proc_start_epoch(pid)
            if started is not None:
                parked_since = started if parked_since is None else min(parked_since, started)
    return tool, parked_since


def evaluate(state: dict, now: float) -> Tuple[bool, str]:
    """(hold, reason). Pure over the state, the clock, /proc and the transcripts."""
    if not owner_alive(state):
        return False, "session process exited"
    limit = max_hold_seconds()
    if limit > 0 and now - float(state.get("last_event_at") or 0) > limit:
        return False, f"no session event for {limit / 3600:g}h"

    tp = state.get("transcript_path")
    tool, parked_since = running_work(state["owner_pid"])
    # A blocking hook is parked only until the transcript shows an answer; one
    # that lingers after its prompt was answered elsewhere no longer counts.
    # +1.5 s: the start time derives from btime (1 s resolution), and the tool
    # result that preceded the prompt must not read as its answer.
    if parked_since is not None and not user_entry_after(tp, parked_since + 1.5):
        return True, "parked on a relay prompt"
    prompt_at = state.get("permission_prompt_at")
    if prompt_at and not user_entry_after(tp, float(prompt_at)):
        return True, "waiting on a terminal permission prompt"

    activity = transcript_activity(state)
    last = max(activity, float(state.get("last_event_at") or 0))
    if state.get("turn_open", True):
        if now - last < idle_seconds():
            return True, "recent activity"
        # A foreground Bash call is capped at 10 min, so a shell that runs on
        # with no activity is background work (a dev server after an Esc):
        # the background cap applies, counted from the last activity.
        if tool and now - last < background_seconds():
            return True, "tool running"
        if tool:
            return False, f"tool running past {background_seconds() / 3600:g}h with no activity"
        return False, f"idle for {idle_seconds() / 60:g} min"

    # Turn ended with background work outstanding.
    stopped = float(state.get("stopped_at") or 0)
    if now - stopped > background_seconds():
        return False, f"background work past {background_seconds() / 3600:g}h"
    if tool:
        return True, "background tool running"
    if state.get("background_pending") and now - max(activity, stopped) < idle_seconds():
        return True, "background task active"
    return False, "background work done"


# ---------------------------------------------------------------------------
# Hook entry points
# ---------------------------------------------------------------------------

def sweep(now: float) -> None:
    """Drop state whose holder is gone; release holders whose owner is gone."""
    try:
        files = list(state_dir().glob("*.json"))
    except Exception:
        return
    for f in files:
        sid = f.stem
        if not _safe_id(sid):
            continue
        try:
            with session_lock(sid):
                st = read_state(sid)
                if st is None:
                    remove_state(sid)
                elif not holder_alive(st):
                    remove_state(sid)
                    log(sid, "swept: holder gone")
                elif not owner_alive(st):
                    release(sid, "swept: session process exited")
        except Exception:
            pass
    try:
        for lock in state_dir().glob("*.lock"):
            if lock.with_suffix(".json").exists() or now - lock.stat().st_mtime <= 86400:
                continue
            fd = os.open(str(lock), os.O_RDWR)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # in use → skip
                if now - os.fstat(fd).st_mtime > 86400:
                    lock.unlink()
            except OSError:
                pass
            finally:
                os.close(fd)
    except Exception:
        pass


def _background_pending(payload: dict) -> bool:
    tasks = payload.get("background_tasks")
    return isinstance(tasks, list) and len(tasks) > 0


def handle_event(event: str, payload: dict) -> None:
    now = time.time()
    sid = _safe_id(str(payload.get("session_id") or ""))
    if not sid:
        return
    sweep(now)

    with session_lock(sid):
        if not enabled():
            release(sid, "disabled (CLAUDE_KEEP_AWAKE=0)")
            return

        if event == "UserPromptSubmit":
            acquire(sid, payload, now)
        elif event == "Notification":
            state = acquire(sid, payload, now, keep_prompt=True)
            if state is not None:
                state["permission_prompt_at"] = now
                write_state(sid, state)
        elif event == "Stop":
            state = read_state(sid)
            if state is None:
                return
            state["last_event_at"] = now
            state["permission_prompt_at"] = None
            state["turn_open"] = False
            state["stopped_at"] = now
            state["background_pending"] = _background_pending(payload)
            tp = payload.get("transcript_path")
            if isinstance(tp, str) and tp:
                state["transcript_path"] = tp
            hold, why = evaluate(state, now) if (
                state["background_pending"] or running_work(state["owner_pid"])[0]
            ) else (False, "turn ended")
            if hold:
                write_state(sid, state)
                log(sid, f"holding after turn end: {why}")
            else:
                release(sid, why)
        elif event == "SessionEnd":
            release(sid, f"session ended ({payload.get('reason') or 'unknown'})")


# ---------------------------------------------------------------------------
# Watcher (runs under systemd-inhibit)
# ---------------------------------------------------------------------------

def _open_pidfd(pid: int) -> Optional[int]:
    try:
        return os.pidfd_open(pid)
    except Exception:
        return None


def watch(session_id: str) -> int:
    holder = os.getppid()
    with session_lock(session_id):          # acquire() holds it until state is written
        state = read_state(session_id)
    if not state or state.get("holder_pid") != holder:
        return 0
    owner = state["owner_pid"]
    pidfd = _open_pidfd(owner)
    if not owner_alive(state):              # checked after open: the fd is the right process
        with session_lock(session_id):
            remove_state(session_id)
        log(session_id, "released: session process exited")
        return 0

    poller = None
    if pidfd is not None:
        poller = select.poll()
        poller.register(pidfd, select.POLLIN)

    while True:
        interval = poll_seconds()
        if poller is not None:
            if poller.poll(interval * 1000):
                with session_lock(session_id):
                    st = read_state(session_id)
                    if st and st.get("holder_pid") == holder:
                        remove_state(session_id)
                log(session_id, "released: session process exited")
                return 0
        else:
            time.sleep(interval)

        if os.getppid() != holder:          # systemd-inhibit is gone; so is the lock
            return 0
        with session_lock(session_id):
            st = read_state(session_id)
            if not st or st.get("holder_pid") != holder:
                return 0                    # released or replaced by a hook
            hold, why = evaluate(st, time.time())
            if not hold:
                remove_state(session_id)
        if not hold:
            log(session_id, f"released: {why}")
            return 0                        # exiting ends systemd-inhibit → lock dropped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def status() -> int:
    now = time.time()
    files = sorted(state_dir().glob("*.json")) if state_dir().is_dir() else []
    rows = []
    for f in files:
        st = read_state(f.stem)
        if not st:
            continue
        if holder_alive(st):
            _, why = evaluate(st, now)
            age = int((now - float(st.get("acquired_at") or now)) / 60)
            rows.append(f"{f.stem[:8]}  held {age:>4} min  {why:<40} {st.get('cwd') or ''}")
        else:
            rows.append(f"{f.stem[:8]}  stale (holder gone; swept on next event)")
    if not rows:
        print("keep-awake: no session is holding the machine awake")
    else:
        print("keep-awake: sessions holding the machine awake")
        print("\n".join(rows))
    if not enabled():
        print("(CLAUDE_KEEP_AWAKE=0 in this shell)")
    return 0


def release_all() -> int:
    for f in (list(state_dir().glob("*.json")) if state_dir().is_dir() else []):
        sid = f.stem
        if _safe_id(sid):
            with session_lock(sid):
                release(sid, "release-all")
    return 0


def main(argv: List[str]) -> int:
    try:
        if len(argv) >= 2 and argv[0] == "--watch":
            return watch(argv[1])
        if argv[:1] == ["--status"]:
            return status()
        if argv[:1] == ["--release-all"]:
            return release_all()
        if len(argv) >= 2 and argv[0] == "--event":
            raw = sys.stdin.read()
            payload = json.loads(raw) if raw.strip() else {}
            if isinstance(payload, dict):
                handle_event(argv[1], payload)
            return 0
    except Exception as exc:   # fail-open: never disturb the session
        log("", f"error: {type(exc).__name__}: {exc}")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
