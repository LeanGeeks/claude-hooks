#!/usr/bin/env python3
"""Builders for the fake ``amux`` / ``codex`` / ``tmux`` executables (task 20-05).

The integration tests run the REAL ``.claude/bin/amux-spawn`` CLI end to end as
a subprocess. Everything behind its public process boundary is faked on PATH:

``amux``     a faithful stand-in for the pinned amux CLI contract surface the
             launcher uses (``docs/codex-provider.md`` in the sibling repo):
             ``exec``/``start --resume``/``rm``/``ls``, persisted
             ``<name>.env`` keys, ``codex_session_id`` capture into
             ``<name>.meta.json``, bounded-run artifact preparation, provider
             argv construction (including the --yolo expansion), and rc-66
             thread-mismatch quarantine. It creates the "pane" through
             whatever ``tmux`` resolves on PATH.
``tmux``     either a state-file fake (hermetic suite — no tmux binary needed)
             or a shim that prefixes the real tmux with ``-L <private socket>``
             (private-tmux suite — real panes, private server).
``codex``    the fake provider executable. It records the exact argv it was
             launched with, then emits fixture-shaped JSONL from
             ``tests/fixtures/codex/`` (vendored amux 01-01 captures) and
             writes the artifacts the real wrapper would rely on. NO real
             codex process, no network, no credentials — and the real Codex
             bypass flag is never executed against anything: it only ever
             appears inside fakes as a recorded string.

Isolation contract for every child process: throwaway ``HOME`` + ``CC_HOME``
(so the ``~/.amux`` registry is private), fake bin first on ``PATH``, and
``TMUX``/``TMUX_PANE`` scrubbed so the launcher never resolves the developer's
real amux session. Tests enumerate and reap every spawned pid in teardown.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
CLI_PATH = REPO / ".claude" / "bin" / "amux-spawn"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex"

# Documented names only — they appear in the fakes as the expansion targets the
# pinned amux maps --yolo to (sibling docs/codex-provider.md §3 "YOLO"). They
# are never executed against a real provider binary in these tests (they
# appear as subprocess argv strings only).
CODEX_YOLO_EXPANSION = "--dangerously-bypass-approvals-and-sandbox"
CLAUDE_YOLO_EXPANSION = "--dangerously-skip-permissions"


# ── the fake `amux` ──────────────────────────────────────────────────────────

FAKE_AMUX = r'''#!/usr/bin/env python3
"""Fake amux CLI (task 20-05) — the pinned amux contract surface amux-spawn uses.

Implements exactly what .claude/bin/amux-spawn calls, per the sibling repo's
docs/codex-provider.md: exec (register+start bounded codex), start --resume,
rm, ls. Everything lives under $CC_HOME like the real amux. Unsupported
commands die loudly so a contract drift in the CLI shows up as a test failure,
not a silent pass.
"""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

CC_HOME = Path(os.environ.get("CC_HOME", str(Path.home() / ".amux")))
SESSIONS = CC_HOME / "sessions"
CODEX_THREAD_KEY = "codex_session_id"
CODEX_YOLO_EXPANSION = "--dangerously-bypass-approvals-and-sandbox"
CLAUDE_YOLO_EXPANSION = "--dangerously-skip-permissions"

# amux-level value-taking options (parse_amux_opts in the real script).
_VALUE_OPTS = {
    "--provider", "--agent-mode", "--dir", "--event-log",
    "--output-last-message", "--session-id", "--model",
}
_FLAG_OPTS = {"--no-attach", "--no-default-model", "--yolo", "--resume",
              "--new-thread"}


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(code)


def parse(argv):
    """Split (amux_opts, passthrough_flags, prompt) at the first ``--``."""
    opts: dict[str, str] = {}
    flags: list[str] = []
    prompt = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            rest = argv[i + 1:]
            if rest:
                prompt = rest[0]
            break
        key, eq, val = tok.partition("=")
        if key in _VALUE_OPTS:
            opts[key] = val if eq else argv[i + 1]
            if not eq:
                i += 1
        elif key in _FLAG_OPTS:
            opts[key] = "1"
        else:
            flags.append(tok)  # provider pass-through flag
        i += 1
    return opts, flags, prompt


def read_env_file(name):
    env = {}
    path = SESSIONS / f"{name}.env"
    try:
        text = path.read_text()
    except OSError:
        return env
    for line in text.splitlines():
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        env[k.strip()] = v
    return env


def write_env_file(name, entries):
    SESSIONS.mkdir(parents=True, exist_ok=True)
    lines = [f'{k}="{v}"' for k, v in entries.items()]
    (SESSIONS / f"{name}.env").write_text("\n".join(lines) + "\n")


def read_meta(name):
    try:
        with open(SESSIONS / f"{name}.meta.json") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_uuid(value):
    return bool(isinstance(value, str) and re.match(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", value))


def tmux_has(name):
    return subprocess.run(
        ["tmux", "has-session", "-t", f"=amux-{name}"],
        capture_output=True).returncode == 0


def tmux_kill(name):
    subprocess.run(["tmux", "kill-session", "-t", f"=amux-{name}"],
                   capture_output=True)


def validate_artifact(label, path):
    if not path or not os.path.isabs(path) or not os.path.isdir(os.path.dirname(path)):
        die(f"{label} must be an absolute path whose parent directory exists, got: {path!r}")


def prepare_artifacts(event_log, last_message):
    """The pinned amux's _prepare_bounded_artifacts: create E/.err/R 0600,
    remove the stale .rc so its absence refers to THIS attempt."""
    for f in (event_log, f"{event_log}.err", last_message):
        if not os.path.exists(f):
            fd = os.open(f, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(fd)
    try:
        os.unlink(f"{event_log}.rc")
    except OSError:
        pass


def launch_pane(name, dir_, wrapper_argv):
    """Create the detached pane exactly like the pinned amux: tmux new-session
    -d running the (shlex-quoted) wrapper command through the shell."""
    pane_cmd = "exec " + " ".join(shlex.quote(x) for x in wrapper_argv)
    result = subprocess.run(
        ["tmux", "new-session", "-d", "-s", f"amux-{name}", "-c", dir_, pane_cmd],
        capture_output=True, text=True, env=os.environ.copy())
    if result.returncode != 0:
        die(f"tmux new-session failed: {(result.stderr or '').strip()}")


def build_codex_argv(*, resume, dir_, last_message, opts, flags, prompt,
                     thread_id=None, env_yolo=False):
    """The pinned amux's provider argv (docs/codex-provider.md §3):
    `codex exec [--json] -C <dir> -o <R> [flags] PROMPT`, and for a resume
    `codex exec resume --json -o <R> THREAD_ID PROMPT` (no -C)."""
    argv = ["codex", "exec"]
    if resume:
        argv.append("resume")
    argv.append("--json")
    if not resume:
        argv += ["-C", dir_]
    argv += ["-o", last_message]
    # --yolo is provider-neutral at the amux level and expanded AFTER the
    # persisted provider resolves; the persisted CC_YOLO=1 carries it across
    # resumes. Recorded inside these fakes only — never run against a real
    # provider binary.
    if opts.get("--yolo") or env_yolo:
        argv.append(CODEX_YOLO_EXPANSION)
    model = opts.get("--model")
    if model:
        argv += ["-m", model]
    argv += list(flags)
    if resume:
        argv.append(thread_id)
    argv.append(prompt)
    return argv


def start_bounded(name, *, event_log, last_message, dir_, opts, flags, prompt,
                  env_yolo=False):
    """Shared bounded-launch path for exec (fresh) and start --resume."""
    resume = "--resume" in opts
    meta = read_meta(name)
    thread_id = None
    if resume:
        thread_id = meta.get(CODEX_THREAD_KEY) or ""
        if not thread_id:
            die(f"cannot resume session '{name}': no captured codex thread id")
        if not is_uuid(thread_id):
            die(f"cannot resume session '{name}': stored thread id "
                f"{thread_id!r} is not a UUID — refusing to launch")
    else:
        if meta.get(CODEX_THREAD_KEY) and "--new-thread" not in opts:
            die(f"session '{name}' already owns codex thread "
                f"{meta[CODEX_THREAD_KEY]}; a bare start would open a NEW, "
                f"unrelated thread under the same handle")
    if prompt is None:
        die("a bounded Codex run needs a prompt: amux start name [--resume] "
            "-- \"do the thing\"")
    validate_artifact("--event-log", event_log)
    validate_artifact("--output-last-message", last_message)
    prepare_artifacts(event_log, last_message)

    codex_argv = build_codex_argv(
        resume=resume, dir_=dir_, last_message=last_message, opts=opts,
        flags=flags, prompt=prompt, thread_id=thread_id, env_yolo=env_yolo)

    wrapper = [sys.executable, os.environ["PANE_WRAPPER"],
               "--event-log", event_log,
               "--meta", str(SESSIONS / f"{name}.meta.json")]
    if resume:
        wrapper += ["--expect-thread", thread_id]
    wrapper += ["--", *codex_argv]
    launch_pane(name, dir_, wrapper)
    print(f"started {name} in {dir_}")
    print(f"  events: {event_log}")
    print(f"  result: {last_message}")


def cmd_exec(name, rest):
    opts, flags, prompt = parse(rest)
    provider = opts.get("--provider", "claude")
    mode = opts.get("--agent-mode", "interactive")
    if provider != "codex" or mode != "exec":
        die("fake amux: only the bounded codex path is implemented "
            "(--provider codex --agent-mode exec); the Claude path is "
            "covered by the epic-10 suites")
    dir_ = opts.get("--dir") or os.getcwd()
    if not os.path.isabs(dir_):
        die(f"CC_DIR must be absolute, got: {dir_}")
    write_env_file(name, {
        "CC_NAME": name,
        "CC_DIR": dir_,
        "CC_PROVIDER": provider,
        "CC_AGENT_MODE": mode,
        "CC_EVENT_LOG": opts.get("--event-log", ""),
        "CC_LAST_MESSAGE": opts.get("--output-last-message", ""),
        **({"CC_YOLO": "1"} if opts.get("--yolo") else {}),
    })
    start_bounded(name, event_log=opts.get("--event-log"),
                  last_message=opts.get("--output-last-message"), dir_=dir_,
                  opts=opts, flags=flags, prompt=prompt)


def cmd_start(name, rest):
    if "--resume" in rest and rest.index("--resume") != 0:
        die("--resume must be the first option after the session name")
    opts, flags, prompt = parse(rest)
    env = read_env_file(name)
    if not env:
        die(f"session '{name}' not found")
    if env.get("CC_PROVIDER") != "codex" or env.get("CC_AGENT_MODE") != "exec":
        die("fake amux: only bounded codex sessions are implemented")
    start_bounded(name, event_log=env.get("CC_EVENT_LOG"),
                  last_message=env.get("CC_LAST_MESSAGE"),
                  dir_=env.get("CC_DIR") or os.getcwd(), opts=opts,
                  flags=flags, prompt=prompt,
                  env_yolo=env.get("CC_YOLO") == "1")


def cmd_rm(name):
    env_path = SESSIONS / f"{name}.env"
    meta_path = SESSIONS / f"{name}.meta.json"
    known = env_path.exists() or meta_path.exists() or tmux_has(name)
    tmux_kill(name)
    for p in (env_path, meta_path):
        try:
            p.unlink()
        except OSError:
            pass
    if not known:
        # Same wording the real amux resolve_session emits; amux-spawn's
        # _amux_rm tolerates exactly this "not found" case.
        die(f"session '{name}' not found")


def cmd_ls():
    i = 0
    for env_file in sorted(SESSIONS.glob("*.env")):
        env = read_env_file(env_file.stem)
        if not env:
            continue
        i += 1
        print(f"  {i}  {env_file.stem}  {env.get('CC_DIR', '')}")


def main():
    if len(sys.argv) < 2:
        die("usage: amux <exec|start|rm|ls> ...")
    cmd, name, *rest = sys.argv[1:]
    if cmd in ("exec", "run"):
        cmd_exec(name, rest)
    elif cmd == "start":
        cmd_start(name, rest)
    elif cmd == "rm":
        cmd_rm(name)
    elif cmd == "ls":
        cmd_ls()
    else:
        die(f"fake amux: unsupported command {cmd!r}")


if __name__ == "__main__":
    main()
'''


# ── the pane wrapper amux's start would install ──────────────────────────────

PANE_WRAPPER = r'''#!/usr/bin/env python3
"""Fake `amux __codex-run` (task 20-05) — the process the pane really runs.

Mirrors the pinned amux wrapper (docs/codex-provider.md §4-5): streams the
provider's stdout to the event log (append-only), stderr to E.err, merges the
first thread.started id into <name>.meta.json, compares it against
--expect-thread on a resume (mismatch -> quarantine to E.err + SIGINT +
E.rc=66), and finally writes E.rc with the child's exit status. SIGTERM kills
it without the .rc write — the documented "killed wrapper" evidence.
"""
import json
import os
import signal
import subprocess
import sys

MISMATCH_RC = 66


def parse(argv):
    event_log = meta_file = expect_thread = None
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            return event_log, meta_file, expect_thread, argv[i + 1:]
        if tok == "--event-log":
            event_log = argv[i + 1]; i += 2
        elif tok == "--meta":
            meta_file = argv[i + 1]; i += 2
        elif tok == "--expect-thread":
            expect_thread = argv[i + 1]; i += 2
        else:
            print(f"__codex-run: unexpected argument {tok!r}", file=sys.stderr)
            sys.exit(1)
    return event_log, meta_file, expect_thread, []


def merge_thread(meta_file, thread_id):
    try:
        with open(meta_file) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data["codex_session_id"] = thread_id
    tmp = meta_file + ".amux-tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, meta_file)


def write_rc(path, code):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(str(code).encode("ascii"))


def drop_fake_tmux_session():
    """A pane exiting ends its tmux session: drop our state-file entry."""
    state = os.environ.get("FAKE_TMUX_STATE")
    session = os.environ.get("FAKE_TMUX_SESSION")
    if not state or not session or not os.path.exists(state):
        return
    try:
        with open(state + ".lock", "a+") as lock:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX)
            with open(state) as f:
                data = json.load(f)
            if data.get(session) == os.getpid():
                del data[session]
                tmp = state + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, state)
    except Exception:
        pass  # fail-soft: a stale entry self-heals via the pid-liveness check


event_log, meta_file, expect_thread, cmd_argv = parse(sys.argv[1:])
if not event_log or not meta_file or not cmd_argv:
    print("__codex-run: --event-log, --meta and a command are required",
          file=sys.stderr)
    sys.exit(1)
rc_path = event_log + ".rc"
err_path = event_log + ".err"

events = os.fdopen(os.open(event_log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "wb", 0)
errf = os.fdopen(os.open(err_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600), "wb", 0)

proc = subprocess.Popen(cmd_argv, stdin=subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=errf)

holding = bool(expect_thread)
pending = []
mismatch = False
seen_thread_line = False
rc = 1

try:
    for raw in proc.stdout:
        try:
            obj = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            obj = None
        if isinstance(obj, dict) and obj.get("type") == "thread.started" \
                and not seen_thread_line:
            seen_thread_line = True
            thread_id = str(obj.get("thread_id", ""))
            merge_thread(meta_file, thread_id)
            if expect_thread and thread_id != expect_thread:
                mismatch = True
                # Fail-closed part (b): interrupt the foreign conversation.
                try:
                    proc.send_signal(signal.SIGINT)
                except OSError:
                    pass
        if mismatch:
            errf.write(b"amux-rejected| " + raw)
        elif holding:
            pending.append(raw)
        else:
            events.write(raw)
        if seen_thread_line and holding and not mismatch:
            holding = False
            for held in pending:
                events.write(held)
            del pending[:]
    proc.wait()
    write_rc(rc_path, MISMATCH_RC if mismatch else proc.returncode)
    rc = MISMATCH_RC if mismatch else proc.returncode
finally:
    try:
        proc.stdout.close()
    except Exception:
        pass
    drop_fake_tmux_session()
sys.exit(rc)
'''


# ── the fake `codex` provider executable ─────────────────────────────────────

FAKE_CODEX = r'''#!/usr/bin/env python3
"""Fake `codex` CLI (task 20-05) — records its argv, replays vendored fixtures.

The argv amux built reaches this script verbatim; it is recorded to
$CODEX_ARGV_LOG for the tests' boundary/expansion assertions. Behaviour is
selected per invocation via $FAKE_CODEX_MODE; every mode replays a fixture
from tests/fixtures/codex/ (byte-for-byte amux 01-01 captures) so the JSONL
the CLI reduces is fixture-shaped. No network, no credentials.
"""
import json
import os
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
mode = os.environ.get("FAKE_CODEX_MODE", "success")
fixtures = Path(os.environ["CODEX_FIXTURES"])

log = os.environ.get("CODEX_ARGV_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": argv, "mode": mode}) + "\n")

print("Reading additional input from stdin...", file=sys.stderr, flush=True)

# -o/--output-last-message value (contract option on the bounded path).
last_message = None
i = 0
while i < len(argv):
    if argv[i] in ("-o", "--output-last-message") and i + 1 < len(argv):
        last_message = argv[i + 1]
    i += 1

time.sleep(float(os.environ.get("FAKE_CODEX_DELAY", "0")))


def emit(fixture):
    data = (fixtures / f"{fixture}.jsonl").read_bytes()
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


if mode == "success":
    emit("exec_success")
    Path(last_message).write_text(os.environ.get("FAKE_CODEX_RESULT", "OK"))
    sys.exit(0)
if mode == "resume_success":
    emit("exec_resume_success")
    Path(last_message).write_text(
        os.environ.get("FAKE_CODEX_RESULT", "OK, second turn"))
    sys.exit(0)
if mode == "fail":
    emit("exec_turn_failed")
    sys.exit(1)
if mode == "hang":
    emit("exec_terminated_sigterm")  # thread.started + turn.started, no verdict
    time.sleep(float(os.environ.get("FAKE_CODEX_HANG_S", "120")))
    sys.exit(0)
if mode == "newthread":
    # First line carries a FOREIGN thread id; the wrapper must quarantine the
    # segment and SIGINT us before the turn completes or any result is written.
    data = (fixtures / "exec_success.jsonl").read_bytes()
    data = data.replace(b"01a00000-0000-7000-8000-000000000001",
                        b"02b00000-0000-7000-8000-000000000009")
    lines = data.splitlines(keepends=True)
    sys.stdout.buffer.write(lines[0])
    sys.stdout.buffer.flush()
    time.sleep(10)  # the wrapper SIGINTs during this window
    sys.stdout.buffer.write(b"".join(lines[1:]))
    sys.stdout.buffer.flush()
    Path(last_message).write_text("SHOULD NEVER LAND")
    sys.exit(0)
print(f"fake codex: unknown FAKE_CODEX_MODE {mode!r}", file=sys.stderr)
sys.exit(2)
'''


# ── the state-file fake `tmux` (hermetic suite) ──────────────────────────────

FAKE_TMUX_STATE = r'''#!/usr/bin/env python3
"""Fake `tmux` (task 20-05) — session liveness as a locked state file.

Implements exactly the subcommands the amux-spawn stack issues:
has-session (liveness), kill-session (process-group SIGTERM, the documented
way to stop a bounded run), and new-session -d (spawn the pane command in its
own session/process group, like a detached tmux pane). Session death when the
pane exits is modelled by the pane wrapper dropping its own entry; entries
whose pid died self-heal in has-session.
"""
import fcntl
import json
import os
import signal
import subprocess
import sys

STATE = os.environ["FAKE_TMUX_STATE"]
PIDLOG = os.environ.get("FAKE_PID_LOG", "")


def _locked():
    lock = open(STATE + ".lock", "a+")
    fcntl.flock(lock, fcntl.LOCK_EX)
    return lock


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save(data):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, STATE)


def norm_target(target):
    t = (target or "").lstrip("=")
    return t.split(":")[0]


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


argv = sys.argv[1:]
if not argv:
    sys.exit(1)
cmd = argv[0]

if cmd == "has-session":
    target = None
    i = 1
    while i < len(argv):
        if argv[i] == "-t" and i + 1 < len(argv):
            target = argv[i + 1]
        i += 1
    lock = _locked()
    try:
        data = load()
        entry = data.get(norm_target(target))
        # "starting" = reserved, pane process not yet recorded -> alive.
        # int = pane pid -> alive only while the process exists.
        ok = entry == "starting" or (isinstance(entry, int) and alive(entry))
        sys.exit(0 if ok else 1)
    finally:
        lock.close()

if cmd == "kill-session":
    target = None
    i = 1
    while i < len(argv):
        if argv[i] == "-t" and i + 1 < len(argv):
            target = argv[i + 1]
        i += 1
    lock = _locked()
    try:
        data = load()
        entry = data.pop(norm_target(target), None)
        save(data)
    finally:
        lock.close()
    if isinstance(entry, int):
        try:
            os.killpg(os.getpgid(entry), signal.SIGTERM)
        except OSError:
            pass
    sys.exit(0)

if cmd == "new-session":
    detached = False
    session = None
    cwd = None
    cmd_start = None
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok == "-d":
            detached = True
        elif tok in ("-s", "-c") and i + 1 < len(argv):
            if tok == "-s":
                session = argv[i + 1]
            else:
                cwd = argv[i + 1]
            i += 1
        elif cmd_start is None:
            cmd_start = i
        i += 1
    if not detached or not session or cmd_start is None:
        print("fake tmux: only `new-session -d -s NAME [-c DIR] CMD`", file=sys.stderr)
        sys.exit(1)
    command = " ".join(argv[cmd_start:])
    lock = _locked()
    try:
        data = load()
        data[session] = "starting"  # reserve before the pane exists (TOCTOU)
        save(data)
    finally:
        lock.close()
    env = dict(os.environ)
    env["FAKE_TMUX_SESSION"] = session
    # Detached like a real pane on the tmux SERVER: the pane must NOT inherit
    # our stdout/stderr pipes. Real tmux's panes belong to the server process,
    # which is why `amux exec` (a mere client) returns while the pane runs —
    # if the pane inherited the client's pipes, every caller up the chain
    # (amux-spawn's subprocess.run) would block on EOF until the pane exited.
    log = env.get("FAKE_PANE_LOG", "")
    pane_out = open(log, "ab") if log else subprocess.DEVNULL
    proc = subprocess.Popen(["/bin/sh", "-c", command], cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, stdout=pane_out,
                            stderr=subprocess.STDOUT,
                            start_new_session=True)
    if PIDLOG:
        with open(PIDLOG, "a") as f:
            f.write(f"{proc.pid}\n")
    lock = _locked()
    try:
        data = load()
        if data.get(session) == "starting":
            data[session] = proc.pid
            save(data)
    finally:
        lock.close()
    sys.exit(0)

print(f"fake tmux: unsupported command {cmd!r}", file=sys.stderr)
sys.exit(1)
'''


# ── the real-tmux `-L <socket>` shim (private-tmux suite) ────────────────────

REAL_TMUX_SHIM = r'''#!/usr/bin/env python3
"""Prefix every tmux invocation with `-L <private socket>` (task 20-05).

The launcher and the fake amux call plain `tmux ...`; this shim on PATH makes
all of them — plus the tests — talk to one private, per-test tmux server, so
real panes are exercised without touching the developer's real server.
"""
import os
import sys

os.execv({real_tmux!r}, ["tmux", "-L", {socket!r}] + sys.argv[1:])
'''


# ── the harness-facing helper ─────────────────────────────────────────────────

class FakeAmuxEnv:
    """One isolated end-to-end environment: fake bin + private HOME/CC_HOME.

    ``tmux_mode="state"`` installs the state-file fake tmux (fully hermetic);
    ``tmux_mode="real"`` installs the ``-L <socket>`` shim over the real tmux
    binary (skip the test first if no real tmux exists).
    """

    def __init__(self, tmp: Path, *, tmux_mode: str = "state",
                 real_tmux: str | None = None):
        self.tmp = tmp
        self.home = tmp / "home"
        self.cc_home = tmp / "amux-home"
        self.bin = tmp / "bin"
        self.home.mkdir(parents=True, exist_ok=True)
        self.cc_home.mkdir(parents=True, exist_ok=True)
        self.bin.mkdir(parents=True, exist_ok=True)

        self.wrapper = self.bin / "pane_wrapper.py"
        self.wrapper.write_text(PANE_WRAPPER)
        self._install("amux", FAKE_AMUX)
        self._install("codex", FAKE_CODEX)

        self.argv_log = tmp / "codex-argv.jsonl"
        self.pid_log = tmp / "pids.log"
        self.tmux_state = tmp / "fake-tmux-state.json"
        self.socket = f"cc2050-{os.getpid()}-{os.urandom(3).hex()}"

        if tmux_mode == "state":
            self._install("tmux", FAKE_TMUX_STATE)
            self.real_tmux = None
        elif tmux_mode == "real":
            assert real_tmux and Path(real_tmux).exists(), "real tmux required"
            self.real_tmux = str(Path(real_tmux).resolve())
            self._install("tmux", REAL_TMUX_SHIM.format(
                real_tmux=self.real_tmux, socket=self.socket))
        else:
            raise ValueError(tmux_mode)

        self.base_env = self._build_base_env()

    def _install(self, name: str, text: str) -> None:
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def _build_base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.home)
        env["CC_HOME"] = str(self.cc_home)
        env["PATH"] = f"{self.bin}:{env.get('PATH', '')}"
        for key in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR"):
            env.pop(key, None)
        env["CODEX_FIXTURES"] = str(FIXTURES)
        env["CODEX_ARGV_LOG"] = str(self.argv_log)
        env["PANE_WRAPPER"] = str(self.wrapper)
        env["FAKE_PID_LOG"] = str(self.pid_log)
        env["FAKE_PANE_LOG"] = str(self.tmp / "pane.log")
        if self.real_tmux is None:
            env["FAKE_TMUX_STATE"] = str(self.tmux_state)
        return env

    # ── running the real CLI ─────────────────────────────────────────────

    def env(self, **overrides: str) -> dict[str, str]:
        env = dict(self.base_env)
        if "codex_mode" in overrides:
            env["FAKE_CODEX_MODE"] = overrides.pop("codex_mode")
        env.update(overrides)
        return env

    def run_cli(self, *args: str, codex_mode: str = "success",
                timeout: float = 120.0,
                **env_extra: str) -> subprocess.CompletedProcess:
        """Run the REAL repo CLI end to end through the fakes on PATH."""
        return subprocess.run(
            [sys.executable, str(CLI_PATH), *args],
            capture_output=True, text=True, timeout=timeout, cwd=str(self.tmp),
            env=self.env(codex_mode=codex_mode, **env_extra),
        )

    def spawn(self, suffix: str, prompt: str, *extra: str,
              codex_mode: str = "success",
              **env_extra: str) -> subprocess.CompletedProcess:
        return self.run_cli(
            "spawn", suffix, "--provider", "codex", "--dir", str(self.tmp),
            *extra, "--", prompt, codex_mode=codex_mode, **env_extra)

    # ── reading the private registry ─────────────────────────────────────

    @property
    def prefix(self) -> str:
        return self.tmp.name

    def handle_path(self, name: str) -> Path:
        return self.cc_home / "spawn" / f"{name}.json"

    def handle(self, name: str) -> dict[str, Any] | None:
        try:
            with open(self.handle_path(name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def meta(self, name: str) -> dict[str, Any]:
        try:
            with open(self.cc_home / "sessions" / f"{name}.meta.json") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def codex_calls(self) -> list[dict[str, Any]]:
        try:
            lines = self.argv_log.read_text().splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out

    def status(self, name: str) -> dict[str, Any]:
        proc = self.run_cli("status", name, "--json")
        try:
            return json.loads(proc.stdout)
        except ValueError:
            return {"stdout": proc.stdout, "stderr": proc.stderr,
                    "returncode": proc.returncode}

    def await_status(self, name: str, want: tuple[str, ...],
                     timeout: float = 20.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.status(name)
            if last.get("state") in want:
                return last
            time.sleep(0.15)
        return last

    def live_sessions(self) -> list[str]:
        """Session names our tmux (fake state or real private socket) reports."""
        if self.real_tmux is None:
            try:
                with open(self.tmux_state) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                return []
            return [n for n, pid in data.items()
                    if pid == "starting" or (
                        isinstance(pid, int) and _pid_alive(pid))]
        proc = subprocess.run(
            [self.real_tmux, "-L", self.socket, "list-sessions"],
            capture_output=True, text=True)
        if proc.returncode != 0:
            return []
        return [line.split(":")[0] for line in proc.stdout.splitlines() if line]

    # ── teardown / hygiene ───────────────────────────────────────────────

    def teardown(self) -> None:
        """Leave zero live tmux sessions and zero stray processes/artifacts."""
        if self.real_tmux is not None:
            subprocess.run(
                [self.real_tmux, "-L", self.socket, "kill-server"],
                capture_output=True)
            # kill-server does not always unlink a socket it did not create
            # on startup; remove ours so no stray socket file lingers.
            sock_dir = Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / \
                f"tmux-{os.getuid()}"
            for sock in (sock_dir / self.socket,
                         sock_dir / f"{self.socket}.sock"):
                try:
                    sock.unlink()
                except OSError:
                    pass
        else:
            try:
                with open(self.tmux_state) as f:
                    data = json.load(f)
                for name, pid in list(data.items()):
                    subprocess.run(
                        [str(self.bin / "tmux"), "kill-session", "-t",
                         f"={name}"], capture_output=True)
            except (OSError, ValueError):
                pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for pid in self._recorded_pids():
                if _pid_alive(pid) and pid != os.getpid():
                    try:
                        os.killpg(os.getpgid(pid), sig)
                    except OSError:
                        try:
                            os.kill(pid, sig)
                        except OSError:
                            pass
            time.sleep(0.1)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _recorded_pids(self) -> list[int]:
        try:
            text = self.pid_log.read_text()
        except OSError:
            return []
        return [int(x) for x in text.split() if x.isdigit()]

    # Public alias used by the integration tests' teardown hygiene gate.
    env_pids = _recorded_pids


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def new_env(tmux_mode: str = "state",
            real_tmux: str | None = None) -> tuple["FakeAmuxEnv", Path]:
    """Fresh FakeAmuxEnv over a new temp dir (caller calls teardown())."""
    tmp = Path(tempfile.mkdtemp(prefix="codex-itest-"))
    return FakeAmuxEnv(tmp, tmux_mode=tmux_mode, real_tmux=real_tmux), tmp


__all__ = [
    "CLI_PATH", "FIXTURES", "FakeAmuxEnv", "new_env",
    "CODEX_YOLO_EXPANSION", "CLAUDE_YOLO_EXPANSION",
]
