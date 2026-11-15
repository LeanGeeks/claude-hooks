#!/usr/bin/env python3
"""Git sync for the question queue (epic 46) — the only module that starts ``git``.

``questions_store`` is pure by contract: files only, no subprocess.  This module
wraps **one store mutation** in a transaction that makes the change reliable
across checkouts and machines:

    result, report = run(store, lambda s: s.create_entry(...), summary="ask Q-042")

What ``run`` does depends on ``[questions] sync`` in the workspace's roles.toml:

``off`` (default)
    ``return op(store), None``.  No git process is started, ever (brd D13).

``commit``
    The mutation is written and committed in the primary checkout with
    ``git commit --only -- <queue file>``.

``push``
    The id is **claimed on the upstream branch before anything is written
    locally** (brd D6).  Under the store lock: fetch; run the mutation against
    the upstream's content in a :meth:`~questions_store.QuestionsStore.capture`;
    build a commit on the upstream tip that changes only queue files; push it by
    hash.  A rejected push discards the capture and starts again — the id is
    re-allocated for free, because nothing local exists yet (brd §2.4).  Only
    after the remote has accepted is the file written and committed locally.

Three properties the rest of the design leans on:

* **Only queue files are ever pushed** (brd D7).  The pushed commit is built
  with plumbing on a private index and pushed as ``<sha>:refs/heads/<branch>``;
  the local branch is never pushed, so a checkout that is 26 commits ahead of
  its upstream publishes one commit touching one file.  The engine enforces
  this itself rather than trusting its callers: a path that is not a regular
  file in the workspace's queue set is never committed and never pushed.
* **The checkout is only touched through a pathspec** (brd D8): the queue file
  is written, and ``git commit --only -- <file>`` records it.  No checkout,
  stash, reset, rebase, merge or pull.  When ``HEAD`` *is* the upstream tip the
  branch ref is moved onto the pushed commit, so an in-sync checkout stays in
  sync; otherwise the local commit is a *twin* of the pushed one.
* **Every failure degrades to "written locally, and said so"** (brd D9).  ``run``
  never raises for a git reason; the :class:`SyncReport` says what was
  committed, what was pushed, and why not, and the repository is marked
  *pending* so the listener and ``claude-questions --sync`` retry it.

Architecture: ``tasks/46_questions_withdraw_and_claims/architecture.md`` §4.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import random
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import questions_store as qs  # noqa: E402

logger = logging.getLogger("questions_git")

#: Timeouts.  A prompt, a hung credential helper or a black-holed remote is a
#: failure to report, never a hang — ``run`` holds the repository lock, and
#: every other tool write in that repository waits on it.
#:
#: * Each fetch and each push gets ``NETWORK_TIMEOUT_S``.
#: * No new push attempt starts once ``TOTAL_BUDGET_S`` has been spent, so the
#:   worst case for one transaction is the budget plus one attempt in flight
#:   (60 + 40 s), and only against a remote that accepts connections and then
#:   stalls every time.  A dead network fails the first fetch.
#: * After a network failure the remote is not tried again for
#:   ``NETWORK_COOLDOWN_S``: a burst of asks during an outage waits once.
#:
#: The listener passes a tighter per-call budget (``questions_listen_lib``): an
#: answer must not wait long for git.
LOCAL_TIMEOUT_S = 15.0
NETWORK_TIMEOUT_S = 20.0
TOTAL_BUDGET_S = 60.0
NETWORK_COOLDOWN_S = 60.0

STATE_FILENAME = "claude-questions-sync.json"

#: Hooks are switched off for **every** git command this module runs.  A
#: workspace's pre-commit / pre-push / post-commit / reference-transaction hook
#: may run a test suite or a package install, and this code runs inside ``ask``
#: and inside the resident listener.
_NO_HOOKS = ("-c", "core.hooksPath=/dev/null")
_NO_GC = ("-c", "gc.auto=0", "-c", "maintenance.auto=false")

#: Tree modes of a regular file.  A symlink (120000) or a submodule (160000) in
#: the queue directory is never read as a queue file: a link's "content" is its
#: target path, and writing through it would land on whatever it points at.
_REGULAR_MODES = ("100644", "100755")

#: What a push that lost a race looks like.  Not only "non-fast-forward": when
#: two pushes update the ref at the same instant the loser is told
#: ``[remote rejected] (failed to update ref)`` / ``cannot lock ref … is at X
#: but expected Y`` — a *remote rejected* line that a retry does fix.
_RACE_MARKERS = (
    "fetch first", "non-fast-forward", "stale info",
    "failed to update ref", "cannot lock ref", "failed to lock",
    "reference already exists", "unable to create",
)

#: A transaction that died between "the remote accepted" and "the local file
#: is written" leaves this marker behind.  Under the repository lock any marker
#: found is stale by definition; without the lock (``is_pending``) it counts
#: once it is older than any live transaction can be.
IN_FLIGHT_STALE_S = TOTAL_BUDGET_S + 3 * NETWORK_TIMEOUT_S

#: The merge driver (architecture §4.7).  The command is fixed text — the same
#: on every machine and from every copy of this module — so two generations of
#: the code never rewrite each other's configuration.  Without the installed
#: CLI it is exactly git's own three-way merge.
#:
#: The CLI exits with ``MERGE_DRIVER_MERGED`` or ``MERGE_DRIVER_CONFLICT`` only
#: once it has written the result; any other status — a CLI too old to know the
#: flag (2), a missing module, an interpreter that died (1) — means it did
#: nothing, and git's own merge runs instead.  A driver that fails must never
#: leave a file that looks resolved, or one without markers.
MERGE_DRIVER_NAME = "claude-questions"
MERGE_DRIVER_MERGED = 10
MERGE_DRIVER_CONFLICT = 11
MERGE_DRIVER_FAILED = 3


def merge_driver_command(cli: str) -> str:
    """The shell command git runs as the driver, for the CLI at *cli*."""
    return (
        f'x="{cli}"; rc=2; '
        'if [ -x "$x" ]; then "$x" --merge-driver %O %A %B %P; rc=$?; fi; '
        f'case "$rc" in {MERGE_DRIVER_MERGED}) exit 0;; {MERGE_DRIVER_CONFLICT}) exit 1;; esac; '
        "git merge-file %A %O %B"
    )


MERGE_DRIVER_COMMAND = merge_driver_command("$HOME/.claude/shell/claude-questions")


# ── Report ────────────────────────────────────────────────────────────────────


@dataclass
class SyncReport:
    """What a transaction did.  ``to_dict()`` is the ``sync`` object in a tool result."""

    mode: str
    committed: bool = False
    pushed: bool = False
    commit: str = ""                 # short sha: the pushed commit, else the local one
    upstream: str = ""               # e.g. "origin/main"
    attempts: int = 0                # pushes tried (1 + re-allocations)
    twin: bool = False               # the local commit is a twin of the pushed one
    carried_local_changes: bool = False
    pending: bool = False            # something is owed; a retry is recorded
    reason: str = ""                 # why committed/pushed is false
    files: list[str] = field(default_factory=list)
    changed: bool = True             # False: the mutation wrote nothing

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": self.mode,
            "committed": self.committed,
            "pushed": self.pushed,
        }
        if not self.changed:
            out["changed"] = False
        for key in ("commit", "upstream", "reason"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.attempts:
            out["attempts"] = self.attempts
        for key in ("twin", "carried_local_changes", "pending"):
            if getattr(self, key):
                out[key] = True
        if self.files:
            out["files"] = list(self.files)
        return out


# ── Git plumbing ──────────────────────────────────────────────────────────────


class GitError(Exception):
    """A git command failed.  Never escapes :func:`run`."""


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    # A caller running inside a git hook (or with a stray GIT_DIR) must not
    # redirect these commands at another repository or index.
    for key in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_PREFIX",
    ):
        env.pop(key, None)
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["LC_ALL"] = "C"
    return env


def _subcommand(args: Sequence[str]) -> str:
    """The git subcommand in *args*, skipping ``-c key=value`` pairs."""
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "-c":
            skip = True
            continue
        if not arg.startswith("-"):
            return arg
    return "git"


class _Git:
    """Thin subprocess wrapper bound to one work tree."""

    def __init__(self, toplevel: Path, git_dir: Path, common_dir: Path) -> None:
        self.toplevel = toplevel
        self.git_dir = git_dir
        self.common_dir = common_dir
        #: ``{(rev, path): blob id}`` for everything :meth:`list_files` has seen.
        self.blob_shas: dict[tuple[str, str], str] = {}

    # ── running ──

    def raw(
        self,
        *args: str,
        timeout: float = LOCAL_TIMEOUT_S,
        stdin: bytes | None = None,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", *_NO_HOOKS, *args],
                cwd=str(cwd or self.toplevel),
                input=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env or _clean_env(),
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git {_subcommand(args)} timed out after {timeout:.0f}s") from exc
        except OSError as exc:
            raise GitError(f"git could not be started: {exc}") from exc

    def ok(self, *args: str, **kwargs: Any) -> bool:
        return self.raw(*args, **kwargs).returncode == 0

    def out(self, *args: str, **kwargs: Any) -> str:
        """stdout, stripped — or :class:`GitError` on a non-zero exit."""
        proc = self.raw(*args, **kwargs)
        if proc.returncode != 0:
            raise GitError(
                _tail(proc.stderr) or f"git {_subcommand(args)} exited {proc.returncode}"
            )
        return proc.stdout.decode("utf-8", "replace").strip()

    def try_out(self, *args: str, **kwargs: Any) -> str | None:
        proc = self.raw(*args, **kwargs)
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", "replace").strip()

    # ── discovery ──

    @classmethod
    def discover(cls, start: Path) -> "_Git | None":
        """The work tree containing *start*, or ``None`` when there is none."""
        probe = start
        while not probe.is_dir():
            if probe.parent == probe:
                return None
            probe = probe.parent
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"],
                cwd=str(probe),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_clean_env(),
                timeout=LOCAL_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        lines = proc.stdout.decode("utf-8", "replace").splitlines()
        if len(lines) < 3 or not lines[0]:
            return None
        toplevel = Path(lines[0])
        git_dir = Path(lines[1])
        common = Path(lines[2])
        if not common.is_absolute():
            common = (probe / common)
        try:
            common = common.resolve()
        except OSError:
            pass
        return cls(toplevel, git_dir, common)

    def rel(self, path: Path) -> str:
        """*path* relative to the work tree root, with ``/`` separators.

        The **directory** is resolved (a work tree is often reached through a
        symlinked prefix); the final component is not.  Resolving the file
        itself would turn a symlinked queue file into the path of its target —
        and a commit "to the queue file" into a commit to something else.
        """
        try:
            parent = Path(os.path.realpath(path.parent))
            top = Path(os.path.realpath(self.toplevel))
            return (parent / path.name).relative_to(top).as_posix()
        except ValueError as exc:
            raise GitError(f"{path} is outside the work tree {self.toplevel}") from exc

    def rel_dir(self, directory: Path) -> str:
        """*directory* relative to the work tree root; ``""`` for the root itself."""
        try:
            top = Path(os.path.realpath(self.toplevel))
            rel = Path(os.path.realpath(directory)).relative_to(top).as_posix()
        except ValueError as exc:
            raise GitError(f"{directory} is outside the work tree {self.toplevel}") from exc
        return "" if rel == "." else rel

    # ── reading objects ──

    def blob(self, rev: str, rel: str) -> str | None:
        """Text of ``<rev>:<rel>``, ``None`` when it does not exist."""
        proc = self.raw("cat-file", "blob", f"{rev}:{rel}")
        if proc.returncode != 0:
            return None
        try:
            return proc.stdout.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitError(f"{rev}:{rel} is not valid UTF-8: {exc}") from exc

    def blob_by_sha(self, sha: str) -> str | None:
        proc = self.raw("cat-file", "blob", sha)
        if proc.returncode != 0:
            return None
        try:
            return proc.stdout.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def list_files(self, rev: str, rel_dir: str) -> dict[str, str]:
        """``{path: mode}`` for every blob under ``<rev>:<rel_dir>/`` (recursive).

        The blob ids seen on the way are kept in :attr:`blob_shas`.
        """
        args = ["ls-tree", "-r", "-z", rev]
        if rel_dir:
            args += ["--", rel_dir.rstrip("/") + "/"]
        proc = self.raw(*args)
        if proc.returncode != 0:
            return {}
        found: dict[str, str] = {}
        for record in proc.stdout.decode("utf-8", "replace").split("\0"):
            meta, _, name = record.partition("\t")
            fields = meta.split()
            if name and len(fields) >= 3 and fields[1] == "blob":
                found[name] = fields[0]
                self.blob_shas[(rev, name)] = fields[2]
        return found


#: ``scheme://user:secret@host`` — git prints the remote URL in its errors, and
#: a URL may carry credentials.  What reaches a tool result, the sync-state
#: file or the journal must not.
_URL_CREDENTIALS_RE = re.compile(r"(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")


def _tail(data: bytes, limit: int = 300) -> str:
    text = data.decode("utf-8", "replace").strip()
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    # git's "hint:" lines are advice for a person at a prompt, not the error.
    meaningful = [line for line in lines if not line.lower().startswith("hint:")]
    text = " | ".join((meaningful or lines)[-3:])
    text = _URL_CREDENTIALS_RE.sub(r"\g<scheme>…@", text)
    return text[-limit:]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ── Sync state (architecture §4.3, §4.5) ──────────────────────────────────────


def state_path_for(store: qs.QuestionsStore) -> Path | None:
    """``<git-common-dir>/claude-questions-sync.json`` — pure filesystem lookup.

    No subprocess, so the listener can ask "does this repository owe anything?"
    on every cycle for free.
    """
    common = qs.git_common_dir_for(store.queue_dir)
    return common / STATE_FILENAME if common is not None else None


def read_state(store: qs.QuestionsStore) -> dict[str, Any]:
    """The repository's sync state; ``{}`` when there is none.

    An unreadable state file reads as **pending**: it is where a debt would
    have been recorded, and forgetting a debt silently is the one thing this
    file exists to prevent.  The next sync rewrites it.
    """
    path = state_path_for(store)
    if path is None or not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (OSError, ValueError) as exc:
        return {
            "pending": True,
            "pending_since": "",
            "pending_reason": f"the sync state file was unreadable ({exc}); re-syncing to be safe",
        }
    return data


def _in_flight_age(state: dict[str, Any]) -> float | None:
    """Seconds since the state's ``in_flight`` marker was written; ``None`` if absent."""
    marker = state.get("in_flight")
    if not isinstance(marker, dict):
        return None
    try:
        since = datetime.fromisoformat(str(marker.get("since", "")).replace("Z", "+00:00"))
        return max(0.0, (datetime.now(timezone.utc) - since).total_seconds())
    except ValueError:
        return float("inf")


def is_pending(store: qs.QuestionsStore) -> bool:
    """True when this repository owes its upstream (or its branch) a commit.

    That includes a transaction that was interrupted after its push: the entry
    is upstream and this checkout has not written it yet.
    """
    state = read_state(store)
    if state.get("pending"):
        return True
    age = _in_flight_age(state)
    return age is not None and age > IN_FLIGHT_STALE_S


def _write_state(store: qs.QuestionsStore, state: dict[str, Any]) -> None:
    path = state_path_for(store)
    if path is None:
        return
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    except OSError as exc:  # state is advisory; never fail a write over it
        logger.warning("could not write %s: %s", path, exc)


# ── Checkout inspection (architecture §4.4) ───────────────────────────────────


@dataclass
class _Checkout:
    branch: str = ""            # "" when detached
    head: str = ""              # "" when the branch has no commits
    remote: str = ""
    sync_branch: str = ""
    upstream_ref: str = ""      # refs/remotes/<remote>/<sync_branch>
    no_commit: str = ""         # non-empty: why a local commit is not possible
    no_push: str = ""           # non-empty: why a push is not possible

    @property
    def upstream_name(self) -> str:
        return f"{self.remote}/{self.sync_branch}" if self.remote and self.sync_branch else ""


_IN_PROGRESS = (
    ("rebase-merge", "a rebase is in progress"),
    ("rebase-apply", "a rebase or am is in progress"),
    ("MERGE_HEAD", "a merge is in progress"),
    ("CHERRY_PICK_HEAD", "a cherry-pick is in progress"),
    ("REVERT_HEAD", "a revert is in progress"),
    ("BISECT_LOG", "a bisect is in progress"),
    ("index.lock", "another git process holds the index (index.lock)"),
)


def _inspect(git: _Git, config: qs.QuestionsConfig) -> _Checkout:
    co = _Checkout()
    co.branch = git.try_out("symbolic-ref", "--quiet", "--short", "HEAD") or ""
    co.head = git.try_out("rev-parse", "--verify", "--quiet", "HEAD") or ""

    # ── which remote, which branch ──
    remote = config.git_remote
    sync_branch = config.git_branch
    if not remote:
        for candidate in (sync_branch, co.branch):
            if candidate:
                remote = git.try_out("config", "--get", f"branch.{candidate}.remote") or ""
                if remote and remote != ".":
                    break
                remote = ""
    if not remote:
        remote = "origin"
    has_remote = git.ok("remote", "get-url", remote)

    if not sync_branch and has_remote:
        default = git.try_out("symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
        if default and default.startswith(remote + "/"):
            sync_branch = default[len(remote) + 1:]
    if not sync_branch and co.branch:
        merge = git.try_out("config", "--get", f"branch.{co.branch}.merge") or ""
        sync_branch = merge[len("refs/heads/"):] if merge.startswith("refs/heads/") else co.branch
    co.remote = remote if has_remote else ""
    co.sync_branch = sync_branch
    if co.remote and co.sync_branch:
        co.upstream_ref = f"refs/remotes/{co.remote}/{co.sync_branch}"

    # ── what the checkout allows ──
    if not co.head:
        co.no_commit = "the repository has no commits yet"
    elif not co.branch:
        co.no_commit = "detached HEAD"
    else:
        for name, why in _IN_PROGRESS:
            if (git.git_dir / name).exists():
                co.no_commit = why
                break
    if not co.no_commit and co.sync_branch and co.branch != co.sync_branch:
        co.no_commit = (
            f"the primary checkout is on branch '{co.branch}', not the sync branch "
            f"'{co.sync_branch}' (set [questions.git] branch to change which branch "
            "the queue lives on)"
        )

    if co.no_commit:
        co.no_push = co.no_commit
    elif not has_remote:
        co.no_push = f"no remote named '{remote}'" if config.git_remote else "no upstream: the repository has no remote"
    elif not co.sync_branch:
        co.no_push = "no upstream: could not determine the branch to push to"
    return co


# ── Entry-level three-way merge (architecture §4.3) ───────────────────────────


def _strip_blank_tail(lines: Sequence[str]) -> list[str]:
    out = list(lines)
    while out and out[-1].strip() == "":
        out.pop()
    return out


class _Split:
    """A queue file as a preamble plus one block per entry.

    A block runs from an entry's heading to the next *entry* heading, so
    anything between two entries travels with the first of them and the blocks
    partition the file exactly.
    """

    def __init__(self, text: str, fmt: qs.QuestionsFormat) -> None:
        self.lines = text.split("\n")
        entries = qs.parse_entries(self.lines, fmt)
        self.keys = [e.key for e in entries]
        self.entries = {e.key: e for e in entries}
        self.duplicate = len(self.keys) != len(set(self.keys))
        starts = [e.start for e in entries] + [len(self.lines)]
        self.preamble = self.lines[: starts[0]]
        self.spans = {e.key: (starts[i], starts[i + 1]) for i, e in enumerate(entries)}

    def block(self, key: tuple[int, str]) -> list[str] | None:
        span = self.spans.get(key)
        return None if span is None else self.lines[span[0]: span[1]]

    def body(self, key: tuple[int, str]) -> list[str] | None:
        """The block without its trailing blank lines — what is compared.

        The same entry has different trailing blank lines at the end of a file
        and in the middle of one; that is spacing, not content.
        """
        block = self.block(key)
        return None if block is None else _strip_blank_tail(block)


def _heading_without_status(entry: qs.Entry) -> str:
    token = entry.status_token
    heading = entry.heading.rstrip("\r")
    if token is not None:
        heading = heading[: token.start] + heading[token.end:]
    return " ".join(heading.split())


def _is_subsequence(short: Sequence[str], long: Sequence[str]) -> bool:
    remaining = iter(long)
    return all(any(line == other for other in remaining) for line in short)


def _later_state(
    mine: list[str], mine_entry: qs.Entry,
    other: list[str], other_entry: qs.Entry,
    fmt: qs.QuestionsFormat,
    base: list[str] | None = None,
) -> str | None:
    """Which of two versions of one entry is the other *plus what came after*.

    An entry only ever grows: asked → dispatched → answered, or → withdrawn
    (→ a late answer).  Each step keeps every line of the one before, flips
    the status word and adds lines.  So when two versions of the same question
    differ and one contains the other line for line, in order, the containing
    one is simply later, and taking it loses nothing.  That is the case a
    three-way base cannot settle when the base predates the entry — a twin
    carries another machine's question as it was when the twin was made, and
    the upstream has since recorded the answer.

    When the entry is in *base* too, the earlier version must itself contain
    every line of the base: otherwise it is not an earlier state at all but an
    edit — somebody deleted a line — and taking the "later" one would quietly
    put the line back (review round 5).

    ``"ours"`` | ``"theirs"`` | ``None`` (not one history: two different
    answers, two different questions under one id, a hand edit on each side).
    """
    if _heading_without_status(mine_entry) != _heading_without_status(other_entry):
        return None                           # two questions, one id
    mine_body = [line.rstrip("\r") for line in mine[1:]]
    other_body = [line.rstrip("\r") for line in other[1:]]
    in_other = _is_subsequence(mine_body, other_body)
    in_mine = _is_subsequence(other_body, mine_body)
    open_word = fmt.open_status.lower()
    mine_open, other_open = mine_entry.status == open_word, other_entry.status == open_word
    if not mine_open and not other_open and mine_entry.status != other_entry.status:
        return None                           # two different closures: a human decides
    if base is not None:
        base_body = [line.rstrip("\r") for line in base[1:]]
        earlier = mine_body if in_other else other_body
        if (in_other or in_mine) and not _is_subsequence(base_body, earlier):
            return None                       # the "earlier" one dropped a line: an edit
    if in_other and in_mine:                  # same body; only the status differs
        if mine_open != other_open:
            return "theirs" if mine_open else "ours"
        return None
    if in_other:
        # Theirs has everything of ours and more — unless it is the *open* one
        # and ours is not: then ours was closed while theirs was added to.
        return None if (other_open and not mine_open) else "theirs"
    if in_mine:
        return None if (mine_open and not other_open) else "ours"
    return None


def merge_queue_text(ours: str, base: str | None, theirs: str, fmt: qs.QuestionsFormat) -> str | None:
    """Merge the upstream's copy of a queue file into the local one, by entry.

    The unit is the entry, never the line.  For each entry (and for the
    preamble): the two sides agree → take it; only one side changed it since
    *base* → take that side's; both changed it → take the one that is the
    other's later state (:func:`_later_state`), and if neither is,
    **refuse**.  New entries from the upstream are appended.  ``None`` means
    "cannot be merged automatically": the caller reports it and a human
    reconciles.

    This replaced ``git merge-file --union``, which kept both sides' lines of a
    conflicting hunk and merged their common tail — so two answers applied to
    one entry on two machines came out as one fence left open with the other's
    marker inside it, in a file that still parsed and so passed validation.
    A line merge cannot be trusted with a block-structured file.

    Whatever is taken from *ours* is kept byte for byte, spacing included.
    """
    o = _Split(ours, fmt)
    t = _Split(theirs, fmt)
    b = _Split(base if base is not None else "", fmt)
    if o.duplicate or t.duplicate:
        return None                           # an id already heads two entries

    def pick(mine: list[str] | None, base_: list[str] | None, other: list[str] | None) -> str:
        """``"ours"`` | ``"theirs"`` | ``"conflict"`` for one unit."""
        if mine == other:
            return "ours"
        if base is not None and mine == base_:
            return "theirs"
        if base is not None and other == base_:
            return "ours"
        return "conflict"

    # ── preamble ──
    verdict = pick(_strip_blank_tail(o.preamble), _strip_blank_tail(b.preamble),
                   _strip_blank_tail(t.preamble))
    if verdict == "conflict":
        return None
    if verdict == "theirs":
        tail = o.preamble[len(_strip_blank_tail(o.preamble)):]
        preamble = _strip_blank_tail(t.preamble) + (tail or ([""] if o.keys else []))
    else:
        preamble = list(o.preamble)

    # ── entries the local file has ──
    out: list[str] = preamble
    for key in o.keys:
        mine, other = o.body(key), t.body(key)
        block = o.block(key)
        assert mine is not None and block is not None
        if other is None:
            if b.body(key) is not None:
                return None                   # deleted upstream; not ours to decide
            out.extend(block)                 # new here, unknown upstream
            continue
        verdict = pick(mine, b.body(key), other)
        if verdict == "conflict":
            later = _later_state(mine, o.entries[key], other, t.entries[key], fmt, b.body(key))
            if later is None:
                return None
            verdict = later
        if verdict == "theirs":
            out.extend(other + block[len(mine):])     # their content, our spacing
        else:
            out.extend(block)

    # ── entries only the upstream has ──
    for key in t.keys:
        if key in o.spans:
            continue
        if b.body(key) is not None:
            return None                       # deleted here; refuse to resurrect or drop
        other = t.body(key)
        assert other is not None
        out = _strip_blank_tail(out)
        if out:
            # A blank line in the file's own line ending — and an ending for a
            # last line that had none (a file without a final newline).
            crlf = "\r\n" in ours
            if crlf and not out[-1].endswith("\r"):
                out[-1] += "\r"
            out.append("\r" if crlf else "")
        out.extend(other)
        out.append("")

    merged = "\n".join(out)
    check = _Split(merged, fmt)
    if check.duplicate or set(check.keys) != set(o.keys) | set(t.keys):
        return None
    return merged


def explain_merge_refusal(ours: str, base: str | None, theirs: str, fmt: qs.QuestionsFormat) -> str:
    """Why :func:`merge_queue_text` refused, naming the entries — for a human."""

    def ids(text: str) -> dict[tuple[int, str], str]:
        return {e.key: e.qid for e in qs.parse_entries(text.split("\n"), fmt)}

    o, t = _Split(ours, fmt), _Split(theirs, fmt)
    b = _Split(base if base is not None else "", fmt)
    names = {**ids(theirs), **ids(ours)}

    def listed(keys: list[tuple[int, str]]) -> str:
        return ", ".join(names.get(k, str(k[0])) for k in sorted(keys))

    reasons: list[str] = []
    if o.duplicate or t.duplicate:
        side = "the local file" if o.duplicate else "the upstream file"
        reasons.append(f"an id heads two entries in {side}")
    taken, edited = [], []
    for key in o.keys:
        mine, other = o.body(key), t.body(key)
        if other is None or mine == other:
            continue
        old = b.body(key)
        if _later_state(mine, o.entries[key], other, t.entries[key], fmt, old) is not None:
            continue                          # one is the other's later state: mergeable
        if base is None:
            edited.append(key)                # no common version to tell which
        elif old is None:
            taken.append(key)                 # two different questions, one id
        elif mine != old and other != old:
            edited.append(key)
    if taken:
        reasons.append(
            f"{listed(taken)} was given to two different questions (one here, one upstream)"
        )
    if edited:
        reasons.append(f"{listed(edited)} differs here and upstream and neither is the older version")
    gone_upstream = [k for k in o.keys if k not in t.spans and b.body(k) is not None]
    gone_here = [k for k in t.keys if k not in o.spans and b.body(k) is not None]
    if gone_upstream:
        reasons.append(f"{listed(gone_upstream)} was removed upstream")
    if gone_here:
        reasons.append(f"{listed(gone_here)} was removed here")
    if not reasons:
        reasons.append("the text before the first entry was changed on both sides")
    return "; ".join(reasons)


def merge_ledger_text(
    ours: str,
    base: str | None,
    theirs: str,
    claim_re: "re.Pattern[str]",
) -> str | None:
    """Merge two copies of the claims ledger: the union of their claim lines.

    A line both sides have is kept once.  **The same id claimed differently on
    the two sides is a collision** — exactly what a claim exists to prevent —
    and is refused.  A line one side pruned is dropped.
    """

    def key_of(line: str) -> tuple[int, str] | None:
        m = claim_re.match(line)
        if m is None:
            return None
        return int(m.group("num")), (m.group("var") or "").lower()

    def claims(text: str | None) -> dict[tuple[int, str], str]:
        found: dict[tuple[int, str], str] = {}
        for line in (text or "").split("\n"):
            key = key_of(line)
            if key is not None:
                found[key] = line.rstrip("\r")
        return found

    mine, other, old = claims(ours), claims(theirs), claims(base)
    drop: set[tuple[int, str]] = set()
    add: list[str] = []
    for key, line in other.items():
        if key not in mine:
            if key in old:
                continue                      # we pruned it; stay pruned
            add.append(line)
        elif mine[key] != line and key not in old:
            return None                       # one id, two different claims
    for key in mine:
        if key not in other and key in old:
            drop.add(key)                     # pruned upstream
    lines = [line for line in ours.split("\n") if key_of(line) not in drop]
    if add:
        lines = _strip_blank_tail(lines)
        suffix = "\r" if lines and lines[-1].endswith("\r") else ""
        lines.extend(line + suffix for line in add)
        lines.append("")
    return "\n".join(lines)


# ── The merge driver (architecture §4.7) ──────────────────────────────────────


def merge_driver(base_path: str, ours_path: str, theirs_path: str, rel_path: str = "") -> int:
    """``git`` merge driver for queue files: ``%O %A %B %P``.  Result in *ours_path*.

    1. The entry-level merge the tools themselves use (:func:`merge_queue_text`,
       :func:`merge_ledger_text`).  If it can decide, that is the result:
       entries appended on both sides, an answer on one side and a new question
       on the other, a twin of something already upstream, a twin that shows a
       question open which the upstream has since closed.
    2. Otherwise git's own three-way line merge — for whatever the entry-level
       merge will not decide (an entry removed on one side, the text before the
       first entry changed on both) and for a file that is not a queue at all.
       Its result is taken only if it is **sound**: a line merge that comes out
       "clean" with one id heading two entries is not a merge (review round 4
       got one from an ordinary ``git pull``), and is turned into a conflict.
    3. Otherwise conflict markers, exit status 1, and a human decides.

    Run by git from the top of the work tree.  Never raises.

    The return value is a contract with :func:`merge_driver_command`:
    :data:`MERGE_DRIVER_MERGED` and :data:`MERGE_DRIVER_CONFLICT` are returned
    **only after the result has been written**; anything else means "this
    driver did nothing" and the command falls back to git's own merge.  The two
    statuses are deliberately not 0 and 1: 1 is what an interpreter exits with
    when it dies, and a driver that died must never look like one that wrote
    conflict markers.
    """

    def finish(data: bytes, status: int) -> int:
        tmp = ""
        try:
            directory = os.path.dirname(os.path.abspath(ours_path))
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".claude-questions-merge.")
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            os.replace(tmp, ours_path)
        except OSError:
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return MERGE_DRIVER_FAILED
        return status

    # ── what the three sides are, and whether this is a queue at all ──
    fmt: qs.QuestionsFormat | None = None
    texts: list[str] | None = None
    merged: str | None = None
    try:
        texts = []
        for name in (ours_path, base_path, theirs_path):
            with open(name, "rb") as fh:
                texts.append(fh.read().decode("utf-8"))
        ours, base, theirs = texts
        top = Path.cwd()
        git = _Git.discover(top)
        if git is not None:
            top = git.toplevel
        config = qs.load_questions_config(str(top), roles_path=top / ".claude" / "roles.toml")
        if config is not None:
            fmt = config.fmt
            if rel_path and Path(rel_path).name == config.claims:
                store = qs.store_for_root(config, top)
                merged = merge_ledger_text(ours, base, theirs, store.claim_line_re())
            elif _Split(ours, fmt).keys or _Split(theirs, fmt).keys:
                merged = merge_queue_text(ours, base, theirs, fmt)
    except Exception as exc:  # noqa: BLE001 — fall through to git's own merge
        logger.warning("queue merge driver: %s", exc)
        merged = None
    if merged is not None:
        return finish(merged.encode("utf-8"), MERGE_DRIVER_MERGED)

    # ── git's own merge, if what it produces is sound ──
    try:
        proc = subprocess.run(
            ["git", "merge-file", "-p", "-L", "ours", "-L", "base", "-L", "theirs",
             ours_path, base_path, theirs_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=LOCAL_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return MERGE_DRIVER_FAILED
    if proc.returncode < 0 or proc.returncode > 127:
        return MERGE_DRIVER_FAILED            # merge-file itself failed; ours untouched
    if proc.returncode != 0:
        return finish(proc.stdout, MERGE_DRIVER_CONFLICT)

    if fmt is not None and texts is not None:
        try:
            unsound = _Split(proc.stdout.decode("utf-8"), fmt).duplicate
        except Exception:  # noqa: BLE001
            unsound = False
        if unsound:
            # No hunk to mark — the line merge thinks it succeeded.  Hand the
            # human both whole files; the upstream's is usually the one to keep.
            ours, _base, theirs = texts

            def closed(text: str) -> str:
                return text if text.endswith("\n") or not text else text + "\n"

            whole = (
                "<<<<<<< ours\n" + closed(ours) + "=======\n" + closed(theirs) + ">>>>>>> theirs\n"
            )
            return finish(whole.encode("utf-8"), MERGE_DRIVER_CONFLICT)
    return finish(proc.stdout, MERGE_DRIVER_MERGED)


# ── The transaction ───────────────────────────────────────────────────────────


def run(
    store: qs.QuestionsStore,
    op: Callable[[qs.QuestionsStore], Any],
    *,
    summary: str | Callable[[Any], str] = "update",
    sync_only: bool = False,
    network_timeout: float | None = None,
    retries: int | None = None,
    offline: bool = False,
) -> tuple[Any, SyncReport | None]:
    """Run *op* — one store mutation — and publish it as ``[questions] sync`` says.

    Returns ``(op's result, report)``; the report is ``None`` when sync is off.
    *op* may be called more than once (once per push attempt, each time against
    a fresh capture), so it must do nothing but call the store.

    Exceptions raised by *op* propagate unchanged.  Nothing about git raises.

    The three keyword budgets are for callers that must not wait on a network:

    * *network_timeout* — seconds allowed for each fetch and each push
      (default :data:`NETWORK_TIMEOUT_S`);
    * *retries* — re-allocations after a lost push race (default: the
      workspace's ``[questions.git] retries``);
    * *offline* — do not talk to the remote at all in this call: write, commit
      locally, mark the repository pending.  For a caller that knows the last
      attempt failed moments ago and would only time out again.
    """
    mode = store.config.sync
    if mode not in ("commit", "push"):
        return op(store), None
    with store.lock():
        txn = _Transaction(store, op, summary, sync_only)
        if network_timeout is not None:
            txn.network_timeout = max(1.0, float(network_timeout))
        if retries is not None:
            txn.retries = max(0, int(retries))
        txn.offline = bool(offline)
        return txn.execute()


def sync(
    store: qs.QuestionsStore,
    *,
    network_timeout: float | None = None,
    retries: int | None = None,
) -> SyncReport | None:
    """Commit and push whatever the queue files owe (``claude-questions --sync``)."""
    _result, report = run(
        store, lambda _store: None, summary="sync queue files", sync_only=True,
        network_timeout=network_timeout, retries=retries,
    )
    return report


class _Transaction:
    def __init__(
        self,
        store: qs.QuestionsStore,
        op: Callable[[qs.QuestionsStore], Any],
        summary: str | Callable[[Any], str],
        sync_only: bool,
    ) -> None:
        self.store = store
        self.op = op
        self.summary = summary
        self.config = store.config
        self.report = SyncReport(mode=self.config.sync)
        self.state = read_state(store)
        #: Publish every queue file that differs from the upstream, not just the
        #: ones this mutation touched: the explicit sync, and any transaction in
        #: a repository that already owes something.
        self.everything = sync_only or bool(self.state.get("pending"))
        # ``run`` holds the repository lock, so a marker found here was left by
        # a process that died after its push was accepted and before it wrote
        # the file.  The entry is upstream; this checkout owes itself a copy.
        interrupted = self.state.pop("in_flight", None)
        if isinstance(interrupted, dict):
            logger.warning(
                "a previous write was interrupted after pushing %s (%s); syncing",
                str(interrupted.get("commit", ""))[:10], interrupted.get("summary", ""),
            )
            self.everything = True
            self.state["pending"] = True
            if not self.state.get("pending_since"):
                self.state["pending_since"] = str(interrupted.get("since") or _utc_now())
            self.state["pending_reason"] = (
                "a previous write was interrupted after its push was accepted "
                f"({str(interrupted.get('commit', ''))[:10]})"
            )
            _write_state(store, self.state)
        self.sync_only = sync_only
        self.network_timeout = NETWORK_TIMEOUT_S
        self.retries = max(0, int(self.config.git_retries))
        self.offline = False
        self.started = time.monotonic()

    # ── helpers ──

    def _message(self, result: Any) -> str:
        try:
            text = self.summary(result) if callable(self.summary) else str(self.summary)
        except Exception:  # noqa: BLE001 — a summary must never fail a write
            text = "update"
        text = " ".join(str(text).split()) or "update"
        try:
            return self.config.git_message.format(summary=text)
        except (KeyError, IndexError, ValueError):
            return qs.DEFAULT_GIT_MESSAGE.format(summary=text)

    def _queue_paths(self) -> list[Path]:
        """Every path this workspace calls a queue file, plus the ledger."""
        paths = list(self.store.queue_files())
        ledger = self.store.claims_path
        if ledger not in paths:
            paths.append(ledger)
        return paths

    def _named_paths(self) -> set[Path]:
        """Queue files the config names outright (role map / flat file) + ledger."""
        store = self.store
        if store.config.queue:
            named = {store.queue_dir / name for name in store.config.queue.values()}
        else:
            named = {store.queue_dir / store.config.file}
        named.add(store.claims_path)
        return named

    def _owed_paths(self) -> list[Path]:
        """What an "everything" sync may publish beyond the paths just written.

        Narrower than the queue set on purpose.  A file the glob merely
        *matches* is published only if it really is a queue file — it parses as
        at least one entry — and is not a dotfile or a temp file.  Under
        ``glob = "*.md"`` a ``README.md`` beside the queue is not the tool's to
        push, and under ``glob = "*"`` neither is ``.lock``.
        """
        named = self._named_paths()
        owed: list[Path] = []
        for path in self._queue_paths():
            if path in named:
                owed.append(path)
                continue
            if path.name.startswith(".") or path.name.endswith((".tmp", ".lock", "~")):
                continue
            try:
                if self.store.entries_in(path):
                    owed.append(path)
            except qs.QuestionsStoreError:
                continue
        return owed

    def _text(self, path: Path) -> str | None:
        """The store's current view of *path* (overlay first), or ``None``."""
        return self.store.read_text(path)

    def _mark_pending(self, reason: str) -> None:
        self.report.pending = True
        self.state["pending"] = True
        if not self.state.get("pending_since"):
            self.state["pending_since"] = _utc_now()
        self.state["pending_reason"] = reason
        _write_state(self.store, self.state)

    def _clear_pending(self) -> None:
        self.state["pending"] = False
        self.state["pending_since"] = ""
        self.state["pending_reason"] = ""

    def _note_network(self, ok: bool) -> None:
        """Remember when the remote last failed, so a burst waits only once."""
        if ok:
            if self.state.pop("network_failed_at", None) is not None:
                _write_state(self.store, self.state)
        else:
            self.state["network_failed_at"] = time.time()
            _write_state(self.store, self.state)

    def _cooling_down(self) -> float:
        """Seconds since the last network failure, if within the cool-down; else -1."""
        try:
            age = time.time() - float(self.state.get("network_failed_at"))
        except (TypeError, ValueError):
            return -1.0
        return age if 0 <= age < NETWORK_COOLDOWN_S else -1.0

    def _ensure_merge_driver(self, git: _Git) -> None:
        """Teach this repository's git to merge queue files entry by entry.

        The tool's commits reach the operator's branch twice — once from the
        upstream, once as the local twin — and git's line merge calls two
        machines' appended entries a conflict.  A merge driver registered in
        the repository's *local* configuration (``.git/config`` and
        ``.git/info/attributes``; nothing tracked) makes the operator's own
        ``git pull`` and ``git pull --rebase`` clean.  See :func:`merge_driver`.

        Idempotent and best-effort: recorded in the sync state so it costs
        nothing once done, and a failure only means pulls merge by line.
        """
        if not self.config.git_merge_driver:
            return
        try:
            store = self.store
            rel_dir = git.rel_dir(store.queue_dir)
            prefix = "/" + (rel_dir + "/" if rel_dir else "")
            patterns = {prefix + store.fmt.glob}
            patterns.update("/" + git.rel(path) for path in self._named_paths())
            lines = [
                f"{pattern} merge={MERGE_DRIVER_NAME}"
                for pattern in sorted(patterns)
                if not any(ch.isspace() for ch in pattern)
            ]
            signature = MERGE_DRIVER_COMMAND + "\n" + "\n".join(lines)
            if self.state.get("merge_driver") == signature:
                return

            key = f"merge.{MERGE_DRIVER_NAME}"
            git.out("config", "--local", f"{key}.name", "claude-questions: queue files, entry by entry")
            git.out("config", "--local", f"{key}.driver", MERGE_DRIVER_COMMAND)

            attributes = git.common_dir / "info" / "attributes"
            try:
                with open(attributes, "r", encoding="utf-8", newline="") as fh:
                    existing = fh.read().split("\n")
            except FileNotFoundError:
                existing = []
            kept = _strip_blank_tail(
                [line for line in existing if f"merge={MERGE_DRIVER_NAME}" not in line.split()]
            )
            wanted = kept + lines + [""]
            if wanted != existing:
                attributes.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=str(attributes.parent), prefix=".attributes.", suffix=".tmp")
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    fh.write("\n".join(wanted))
                os.replace(tmp, attributes)

            self.state["merge_driver"] = signature
            _write_state(store, self.state)
        except (GitError, OSError) as exc:
            logger.warning("could not register the queue merge driver: %s", exc)

    def _layout_problem(self, git: _Git) -> str:
        """Why this workspace's queue cannot be put under git at all, or ``""``.

        * A queue path that is a **symbolic link**: git records a link as its
          target path, so the upstream "content" is not the queue, and writing
          through the link lands on whatever it points at.
        * A queue path **git ignores**: plumbing would push it while
          ``git add`` refuses it, leaving a repository that owes forever.
        """
        store = self.store
        if store.queue_dir.is_symlink():
            return (
                f"the queue directory {store.rel_path(store.queue_dir)} is a symbolic "
                "link; git sync does not follow links"
            )
        paths = self._queue_paths()
        for path in paths:
            if path.is_symlink():
                return (
                    f"{store.rel_path(path)} is a symbolic link; git sync does not "
                    "follow links — make it a regular file, or set sync = \"off\""
                )
        try:
            rels = [git.rel(path) for path in paths]
        except GitError as exc:
            return str(exc)
        proc = git.raw("check-ignore", "--", *rels)
        if proc.returncode == 0:
            ignored = proc.stdout.decode("utf-8", "replace").split()
            return (
                f"{', '.join(ignored)} is ignored by git (.gitignore); a queue that "
                "git does not track cannot be synced — un-ignore it, or set sync = \"off\""
            )
        return ""

    # ── entry ──

    def execute(self) -> tuple[Any, SyncReport]:
        report = self.report
        try:
            git = _Git.discover(self.store.queue_dir)
        except Exception as exc:  # noqa: BLE001
            logger.warning("git discovery failed: %s", exc)
            git = None
        if git is None:
            return self._local(None, None, "not a git work tree", commit=False), report

        try:
            problem = self._layout_problem(git)
            if problem:
                return self._local(git, None, problem, commit=False), report
            checkout = _inspect(git, self.config)
        except GitError as exc:
            return self._local(
                git, None, f"could not inspect the checkout: {exc}", commit=False
            ), report
        report.upstream = checkout.upstream_name
        self._ensure_merge_driver(git)

        if self.config.sync != "push":
            return self._local(git, checkout, ""), report
        if checkout.no_push:
            return self._local(git, checkout, checkout.no_push), report
        if self.offline:
            previous = str(self.state.get("pending_reason") or "").strip()
            return self._local(
                git, checkout,
                "push deferred: the last sync attempt failed moments ago"
                + (f" ({previous})" if previous else ""),
            ), report
        cooling = self._cooling_down()
        if cooling >= 0 and not self.sync_only:
            return self._local(
                git, checkout,
                f"push deferred: the remote was unreachable {cooling:.0f}s ago; "
                "retried by the listener, or run claude-questions --sync",
            ), report

        try:
            done, result, reason = self._push(git, checkout)
        except GitError as exc:               # nothing local has been written yet
            done, result, reason = False, None, str(exc)
        if done:
            return result, report
        return self._local(git, checkout, reason), report

    # ── local path: write, commit if possible, record what is owed ──

    def _local(
        self,
        git: _Git | None,
        checkout: _Checkout | None,
        reason: str,
        *,
        commit: bool = True,
    ) -> Any:
        report = self.report
        store = self.store
        want_push = self.config.sync == "push"

        # Allocate above everything the last known upstream contains, even
        # though we could not (or were not asked to) talk to it now.
        if git is not None and checkout is not None and checkout.upstream_ref:
            try:
                tip = git.try_out("rev-parse", "--verify", "--quiet", checkout.upstream_ref)
                if tip:
                    store.id_floor = max(store.id_floor, self._upstream_floor(git, tip))
            except Exception as exc:  # noqa: BLE001 — a floor is best-effort here
                logger.debug("upstream id floor unavailable: %s", exc)

        with store.capture() as cap:
            result = self.op(store)
            allowed = set(self._queue_paths())
        touched = sorted(cap.touched)
        if not touched and not self.everything:
            report.changed = False
            if reason and want_push:
                report.reason = reason
            return result

        store.materialize(cap)

        strangers = [p for p in touched if p not in allowed]
        paths = [p for p in touched if p in allowed]
        if self.everything:
            paths = sorted(set(paths) | {p for p in self._owed_paths() if p.is_file()})
        report.files = [store.rel_path(p) for p in touched] or [store.rel_path(p) for p in paths]

        blocker = ""
        if git is None or checkout is None or not commit:
            blocker = reason
        elif checkout.no_commit:
            blocker = checkout.no_commit
        elif strangers:
            # brd D7 is the engine's promise, not its callers': a path outside
            # the queue set is written (the store was asked to) and never
            # committed, whatever handed it in.
            blocker = (
                f"{', '.join(store.rel_path(p) for p in strangers)} is not one of this "
                "workspace's queue files; written, not committed"
            )
        else:
            try:
                sha, committed = self._commit_only(git, paths, self._message(result))
                report.committed = True
                report.commit = sha[:10] if committed else ""
                if not committed and not touched:
                    report.changed = False
            except GitError as exc:
                blocker = f"local commit failed: {exc}"

        if blocker and not report.committed:
            report.reason = blocker if not reason or reason == blocker else f"{reason}; {blocker}"
            self._mark_pending(report.reason)
        elif want_push:
            report.reason = reason or "not pushed"
            self._mark_pending(report.reason)
        elif self.state.get("pending"):
            self._clear_pending()
            _write_state(store, self.state)
        return result

    # ── push path ──

    def _fetch(self, git: _Git, checkout: _Checkout) -> str:
        """Fetch the sync branch.  Returns ``""`` or the reason it failed."""
        refspec = f"+refs/heads/{checkout.sync_branch}:{checkout.upstream_ref}"
        try:
            proc = git.raw(
                *_NO_GC, "fetch", "--quiet", "--no-tags", checkout.remote, refspec,
                timeout=self.network_timeout,
            )
        except GitError as exc:
            self._note_network(False)
            return f"fetch failed: {exc}"
        if proc.returncode != 0:
            detail = _tail(proc.stderr)
            if "couldn't find remote ref" in detail.lower():
                return (
                    f"no upstream: branch '{checkout.sync_branch}' does not exist on "
                    f"'{checkout.remote}' — push it once by hand"
                )
            self._note_network(False)
            return f"fetch failed: {detail or 'exit ' + str(proc.returncode)}"
        self._note_network(True)
        return ""

    def _push(self, git: _Git, checkout: _Checkout) -> tuple[bool, Any, str]:
        """Returns ``(done, result, reason)``; ``done`` False → take the local path.

        Nothing is written to the working tree before a push has been accepted,
        so every early ``return False`` leaves the checkout exactly as it was.
        """
        report = self.report
        store = self.store
        retries = self.retries

        last_refusal = ""
        for attempt in range(1, retries + 2):
            if attempt > 1 and time.monotonic() - self.started > TOTAL_BUDGET_S:
                return False, None, (
                    f"lost the push race {attempt - 1} time(s) on {checkout.upstream_name} "
                    f"and ran out of time ({TOTAL_BUDGET_S:.0f}s budget)"
                )
            report.attempts = attempt
            if attempt > 1:
                # Several checkouts that lost to the same winner would otherwise
                # all retry in step and lose to each other again.
                time.sleep(random.uniform(0.05, 0.3) * (attempt - 1))
            problem = self._fetch(git, checkout)
            if problem:
                return False, None, problem

            try:
                tip = git.out("rev-parse", "--verify", checkout.upstream_ref)
                theirs = self._upstream_files(git, tip)
                store.id_floor = max(store.id_floor, self._upstream_floor(git, tip, theirs))
            except GitError as exc:
                return False, None, f"could not read {checkout.upstream_name}: {exc}"

            with store.capture() as cap:
                try:
                    problem = self._integrate(git, checkout, tip, theirs, cap)
                except GitError as exc:
                    problem = str(exc)
                if problem:
                    return False, None, problem
                before = {path: self._text(path) for path in self._queue_paths()}
                result = self.op(store)
                touched = sorted(cap.touched)
                allowed = set(self._queue_paths())

                strangers = [p for p in touched if p not in allowed]
                if strangers:
                    # Hand over to the local path, which writes and does not commit.
                    return False, None, (
                        f"{', '.join(store.rel_path(p) for p in strangers)} is not one of "
                        "this workspace's queue files"
                    )

                files: dict[Path, str] = {path: cap.writes[path] for path in touched}
                if self.everything:
                    for path in self._owed_paths():
                        text = self._text(path)
                        if text is not None and text != theirs.get(path):
                            files.setdefault(path, text)
                # A seeded file only needs writing locally; it is already upstream.
                seeded = {p: t for p, t in cap.writes.items() if p not in files}

            if not files:
                # Nothing of ours to publish.
                report.changed = False
                if self.everything:
                    # …but a repository that owed something may still owe its
                    # own checkout something: the upstream's copy of a file it
                    # pushed and then failed to write (a full disk), or a
                    # commit on its own branch (pushed earlier, commit failed).
                    # A sync brings both in.  An ordinary no-op write does not:
                    # the next real write integrates the upstream again.
                    written, committed = self._finish_local(
                        git, checkout, tip, None, {}, seeded, self._message(result)
                    )
                    report.changed = bool(seeded) or report.twin
                    # What was brought down is now common to both sides: record
                    # it, or the upstream's next change to one of those entries
                    # would be compared against a base that predates them.
                    self._record_base(
                        checkout, self._upstream_blobs(git, tip, theirs),
                        written=written, clear=written and committed,
                    )
                return True, result, ""

            report.carried_local_changes = any(
                before.get(path) is not None and before.get(path) != theirs.get(path)
                for path in touched
            ) or (self.everything and any(p not in touched for p in files))
            message = self._message(result)

            try:
                commit, blobs = self._commit_on(git, tip, files, message)
            except GitError as exc:
                return False, None, f"could not build the commit: {exc}"

            if commit is not None:
                # From here to the end of _finish_local a crash leaves a commit
                # upstream that this checkout has not recorded.  Say so first.
                self.state["in_flight"] = {
                    "commit": commit, "summary": message, "since": _utc_now(),
                }
                _write_state(store, self.state)
                try:
                    verdict, detail = self._push_commit(git, checkout, commit)
                except GitError as exc:
                    verdict, detail = "unknown", str(exc)
                if verdict in ("unknown", "refused"):
                    # Ask the remote what actually happened before believing
                    # the push's own account of it.
                    outcome = self._after_push(git, checkout, commit, tip)
                    if outcome == "accepted":
                        # A push that timed out, or whose connection dropped
                        # after the remote took it, is an accepted push.
                        # Treating it as a failure would allocate the same id
                        # again locally.
                        verdict = "ok"
                    elif outcome == "moved":
                        # Not accepted, and the branch is no longer where this
                        # attempt was built: whatever the remote called it,
                        # that is a lost race.  Falling to the local path here
                        # would hand out the id the winner just took.
                        verdict = "rejected"
                        last_refusal = detail
                if verdict == "rejected":
                    self.state.pop("in_flight", None)
                    logger.info(
                        "push of %s lost a race on %s (attempt %d); re-allocating",
                        commit[:10], checkout.upstream_name, attempt,
                    )
                    continue
                if verdict != "ok":
                    self.state.pop("in_flight", None)
                    _write_state(store, self.state)
                    if verdict == "unknown":
                        self._note_network(False)
                        return False, None, f"push failed: {detail}"
                    return False, None, f"push refused: {detail}"
                report.commit = commit[:10]

            report.pushed = True
            report.files = [store.rel_path(p) for p in sorted(files)]
            written, committed = self._finish_local(
                git, checkout, tip, commit, files, seeded, message
            )
            self.state.pop("in_flight", None)
            # The base for every integrated file: the upstream's blob for the
            # ones only brought down, the pushed blob for the ones published.
            self._record_base(
                checkout, {**self._upstream_blobs(git, tip, theirs), **blobs},
                written=written, clear=written and committed,
            )
            return True, result, ""

        # Out of attempts.  The upstream has moved since the last fetch (that is
        # what losing means), so look once more: the local path allocates above
        # the remote-tracking ref, and a stale one would hand out the winner's id.
        self._fetch(git, checkout)
        return False, None, (
            f"lost the push race {retries + 1} time(s) on {checkout.upstream_name}"
            + (f" (the remote last answered: {last_refusal})" if last_refusal else "")
        )

    def _upstream_blobs(self, git: _Git, tip: str, theirs: dict[Path, str]) -> dict[str, str]:
        """``{rel: blob id}`` at *tip* for every integrated file."""
        found: dict[str, str] = {}
        for path in theirs:
            rel = git.rel(path)
            sha = git.blob_shas.get((tip, rel))
            if sha:
                found[rel] = sha
        return found

    def _after_push(self, git: _Git, checkout: _Checkout, commit: str, tip: str) -> str:
        """After a push that did not report success: what is true on the remote?

        ``"accepted"`` — *commit* is on the upstream branch after all;
        ``"moved"`` — it is not, and the branch has moved off *tip* (a race);
        ``""`` — it is not and the branch has not moved, or nobody can tell.
        """
        try:
            if self._fetch(git, checkout):
                return ""
            if git.ok("merge-base", "--is-ancestor", commit, checkout.upstream_ref):
                return "accepted"
            now = git.try_out("rev-parse", "--verify", "--quiet", checkout.upstream_ref)
            return "moved" if now and now != tip else ""
        except GitError:
            return ""

    def _record_base(
        self,
        checkout: _Checkout,
        blobs: dict[str, str],
        *,
        written: bool,
        clear: bool,
    ) -> None:
        """Remember what was just pushed as the base for the next three-way merge.

        The base means "the content both sides had".  It is recorded **only if
        the local file was actually written** (*written*): integration treats
        "the upstream equals the base" as "the upstream has not moved, the local
        copy wins" — so a base the local file never reached would make the next
        sync push the *old* local content over the entry that was just
        published, deleting it.

        *clear* is whether the local commit landed too.  A debt recorded a
        moment ago by :meth:`_finish_local` must not be wiped here.
        """
        base = self.state.get("base")
        if not isinstance(base, dict) or self.state.get("upstream") != checkout.upstream_name:
            base = {}
        if written:
            base.update(blobs)
        self.state["base"] = base
        self.state["upstream"] = checkout.upstream_name
        if self.everything and clear and not self.report.pending:
            self._clear_pending()
        _write_state(self.store, self.state)

    # ── reading the upstream ──

    def _upstream_files(self, git: _Git, tip: str) -> dict[Path, str]:
        """Upstream text of every queue-set file and the ledger, keyed by local path.

        Regular files only.  A symlink in the upstream tree is skipped: its
        blob is the *path it points at*, not a queue.
        """
        store = self.store
        rel_dir = git.rel_dir(store.queue_dir)
        prefix = rel_dir + "/" if rel_dir else ""
        upstream = git.list_files(tip, rel_dir)

        wanted: dict[str, Path] = {git.rel(path): path for path in self._queue_paths()}
        for name in upstream:
            if not name.startswith(prefix):
                continue
            tail = name[len(prefix):]
            if "/" not in tail and fnmatch.fnmatch(tail, store.fmt.glob):
                wanted.setdefault(name, store.queue_dir / tail)

        named = self._named_paths()
        found: dict[Path, str] = {}
        for rel, path in wanted.items():
            if upstream.get(rel) not in _REGULAR_MODES:
                continue
            text = git.blob(tip, rel)
            if text is None:
                continue
            if path not in named and not self._is_queue_like(path, text):
                # The glob matched it; that does not make it a queue.  Under
                # ``glob = "*.md"`` a README beside the queue is neither merged,
                # nor brought down, nor allowed to block a push.
                continue
            found[path] = text
        return found

    def _is_queue_like(self, path: Path, upstream_text: str) -> bool:
        """A glob match that really is a queue: it holds an entry, here or upstream."""
        if path.name.startswith(".") or path.name.endswith((".tmp", ".lock", "~")):
            return False
        try:
            if qs.parse_entries(upstream_text.split("\n"), self.store.fmt):
                return True
        except Exception:  # noqa: BLE001 — unparseable is "not a queue"
            return False
        try:
            return bool(self.store.entries_in(path))
        except (qs.QuestionsStoreError, OSError):
            return False

    def _upstream_floor(
        self,
        git: _Git,
        tip: str,
        theirs: dict[Path, str] | None = None,
    ) -> int:
        """The highest id the upstream contains: queue files, ledger, ``answered/``."""
        store = self.store
        if theirs is None:
            theirs = self._upstream_files(git, tip)
        saved = store.id_floor
        store.id_floor = 0
        try:
            with store.capture() as cap:
                for path, text in theirs.items():
                    cap.seed(path, text)
                floor = store.high_water_mark()
        finally:
            store.id_floor = saved
        rel_dir = git.rel_dir(store.queue_dir)
        rel_answered = (rel_dir + "/" if rel_dir else "") + qs.ANSWERED_DIRNAME
        for name in git.list_files(tip, rel_answered):
            key = qs.parse_id(store.fmt, Path(name).stem)
            if key is not None:
                floor = max(floor, key[0])
        return floor

    # ── integration (architecture §4.3) ──

    def _integrate(
        self,
        git: _Git,
        checkout: _Checkout,
        tip: str,
        theirs_by_path: dict[Path, str],
        cap: qs.Capture,
    ) -> str:
        """Bring the upstream's queue-file changes into the capture.

        Returns ``""`` on success, else the reason integration is impossible.
        """
        store = self.store
        base_shas = self.state.get("base") if self.state.get("upstream") == checkout.upstream_name else None
        if not isinstance(base_shas, dict):
            base_shas = {}
        merge_base: str | None = None
        merge_base_known = False

        for path, theirs in theirs_by_path.items():
            ours = self._text(path)
            if ours == theirs:
                continue
            rel = git.rel(path)

            base: str | None = None
            sha = base_shas.get(rel)
            if isinstance(sha, str) and sha:
                base = git.blob_by_sha(sha)
            if base is None:
                if not merge_base_known:
                    merge_base = git.try_out("merge-base", "HEAD", tip)
                    merge_base_known = True
                if merge_base:
                    base = git.blob(merge_base, rel)

            if base is not None and theirs == base:
                continue                      # upstream has not moved; ours wins
            if ours is None or (base is not None and ours == base):
                cap.seed(path, theirs)        # we have not moved; take theirs
                continue

            if path == store.claims_path:
                merged = merge_ledger_text(ours, base, theirs, store.claim_line_re())
            else:
                merged = merge_queue_text(ours, base, theirs, store.fmt)
            if merged is None:
                if path == store.claims_path:
                    why = "the same id is claimed differently on the two sides"
                else:
                    why = explain_merge_refusal(ours, base, theirs, store.fmt)
                return (
                    f"{rel} differs from {checkout.upstream_name} in a way that cannot be "
                    f"merged automatically: {why}. Reconcile the file by hand (see "
                    "\"When the files cannot be merged\" in docs/async-questions.md), "
                    "then run claude-questions --sync"
                )
            if merged != ours:
                cap.seed(path, merged)
        return ""

    # ── writing objects ──

    def _commit_on(
        self,
        git: _Git,
        tip: str,
        files: dict[Path, str],
        message: str,
    ) -> tuple[str | None, dict[str, str]]:
        """A commit on *tip* that changes exactly *files*.  Private index only.

        Returns ``(sha, {rel: blob sha})``; ``sha`` is ``None`` when the tree
        would equal the tip's.
        """
        index = git.git_dir / f"claude-questions-index.{os.getpid()}.{time.time_ns()}"
        env = _clean_env()
        env["GIT_INDEX_FILE"] = str(index)
        blobs: dict[str, str] = {}
        try:
            git.out("read-tree", tip, env=env)
            for path, text in files.items():
                rel = git.rel(path)
                blob = git.out(
                    "hash-object", "-w", f"--path={rel}", "--stdin",
                    stdin=text.encode("utf-8"),
                )
                mode = "100644"
                listed = git.try_out("ls-tree", tip, "--", rel)
                if listed and listed.split()[0] in _REGULAR_MODES:
                    mode = listed.split()[0]
                git.out("update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel}", env=env)
                blobs[rel] = blob
            tree = git.out("write-tree", env=env)
            if tree == git.out("rev-parse", f"{tip}^{{tree}}"):
                return None, blobs
            commit = git.out("commit-tree", tree, "-p", tip, "-m", message)
            return commit, blobs
        finally:
            for leftover in (index, Path(str(index) + ".lock")):
                try:
                    leftover.unlink()
                except OSError:
                    pass

    def _push_commit(self, git: _Git, checkout: _Checkout, commit: str) -> tuple[str, str]:
        """``("ok" | "rejected" | "refused", detail)``.

        ``rejected`` is the lost race (non-fast-forward): retry.  ``refused`` is
        everything a retry cannot fix — a protected branch, a server-side hook,
        credentials, the network.
        """
        proc = git.raw(
            "push", "--porcelain", "--no-verify", checkout.remote,
            f"{commit}:refs/heads/{checkout.sync_branch}",
            timeout=self.network_timeout,
        )
        if proc.returncode == 0:
            return "ok", ""
        stdout = proc.stdout.decode("utf-8", "replace")
        stderr = proc.stderr.decode("utf-8", "replace").lower()
        for line in stdout.splitlines():
            if not line.startswith("!"):
                continue
            lowered = line.lower()
            detail = line.split("\t")[-1].strip()
            # Race markers first: a simultaneous update is reported as
            # "[remote rejected] (failed to update ref)", and that is a race.
            if any(marker in lowered for marker in _RACE_MARKERS):
                return "rejected", detail
            if any(marker in stderr for marker in ("cannot lock ref", "failed to lock")):
                return "rejected", detail
            return "refused", detail
        return "refused", _tail(proc.stderr) or f"exit {proc.returncode}"

    # ── the local half, after the remote has accepted ──

    def _finish_local(
        self,
        git: _Git,
        checkout: _Checkout,
        tip: str,
        commit: str | None,
        files: dict[Path, str],
        seeded: dict[Path, str],
        message: str,
    ) -> tuple[bool, bool]:
        """Write the files and record them on the local branch.

        Returns ``(written, committed)``.  **Nothing here raises**: the remote
        has the change, so the claim stands and the caller must get its result
        back (the id, above all) — a full disk at this point is a debt to
        record, not a reason to lose the question.
        """
        report = self.report
        store = self.store

        landed = qs.Capture()
        for path, text in {**seeded, **files}.items():
            landed.seed(path, text)
        try:
            store.materialize(landed)
        except (OSError, qs.QuestionsStoreError) as exc:
            report.reason = (
                f"pushed to {checkout.upstream_name}, but the local write failed: {exc} — "
                "the entry exists upstream; run claude-questions --sync once the cause is fixed"
            )
            self._mark_pending(report.reason)
            return False, False

        paths = sorted(landed.writes)
        if self.everything:
            paths = sorted(set(paths) | {p for p in self._owed_paths() if p.is_file()})
        try:
            if commit is not None and checkout.head == tip:
                # In sync: put the branch on the very commit that was pushed.
                # Plumbing rather than `git merge --ff-only`: only the queue
                # file's index entry is touched.
                rels = [git.rel(p) for p in paths]
                git.out("add", "--", *rels)
                moved = git.raw(
                    "update-ref", "-m", "claude-questions: fast-forward to the pushed commit",
                    "HEAD", commit, tip,
                )
                if moved.returncode == 0:
                    report.committed = True
                    return True, True
                # HEAD moved under us (a session committed meanwhile): fall
                # through to the twin.
            _sha, committed = self._commit_only(git, paths, message)
            report.committed = True
            report.twin = committed
            return True, True
        except GitError as exc:
            report.reason = f"pushed, but the local commit failed: {exc}"
            self._mark_pending(report.reason)
            return True, False

    def _commit_only(self, git: _Git, paths: list[Path], message: str) -> tuple[str, bool]:
        """``git commit --only -- <paths>``.  Returns ``(sha, a commit was made)``."""
        rels = [git.rel(p) for p in paths if p.is_file() and not p.is_symlink()]
        if not rels:
            return "", False
        for rel in rels:
            if not git.ok("ls-files", "--error-unmatch", "--", rel):
                git.out("add", "--", rel)
        if git.ok("diff", "--quiet", "HEAD", "--", *rels):
            return git.try_out("rev-parse", "--verify", "--quiet", "HEAD") or "", False
        git.out(
            *_NO_GC, "commit", "--quiet", "--only", "--no-verify",
            "-m", message, "--", *rels,
        )
        return git.out("rev-parse", "--verify", "HEAD"), True
