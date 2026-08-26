#!/usr/bin/env python3
"""Project identity for the permission-proposal queue (epic 22, task 22-04).

**This module owns the only project-identity function in the epic**
(state.md invariant 7). The enqueue side (the ``permissions`` MCP's
``allowlist_add`` / ``report_parser_issue``) and the drain side (22-05's daily
reviewer) both import ``resolve_project_key`` from here. Two implementations
that disagreed about a worktree would silently drop proposals into a queue
directory nobody drains.

The rule (brd D7): **every worktree of a repo maps to the main checkout.**
``git rev-parse --git-common-dir`` is the same directory for a repo and all of
its linked worktrees (``<main>/.git``), so its parent is the main checkout. Odd
layouts (a bare repo, a ``.git`` file pointing somewhere unusual, a submodule
whose common dir is ``<super>/.git/modules/<name>``) do not end in a ``.git``
basename and fall back to the caller's own path — a stable key either way, just
not a shared one.

Everything that shells out lives here: the queue writer, the settings writer and
the MCP tools call these functions instead of running ``git`` themselves.
Results are cached per input path for the process lifetime, which matters for
the long-lived MCP server (one ``git`` invocation per distinct cwd, not one per
tool call).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Dict, Optional

# ``git rev-parse`` on a healthy repo answers in milliseconds; the timeout only
# exists so a wedged filesystem cannot hang the MCP server forever.
GIT_TIMEOUT_SECONDS = 10

# Per-path caches, process lifetime. Keyed by the *input* path, since that is
# what callers repeat.
_ROOT_CACHE: Dict[str, str] = {}
_WORKSPACE_ROOT_CACHE: Dict[str, str] = {}


def clear_cache() -> None:
    """Drop the memoized git lookups (tests; a long-lived process never needs it)."""
    _ROOT_CACHE.clear()
    _WORKSPACE_ROOT_CACHE.clear()


def _run_git(cwd: str, *args: str) -> Optional[str]:
    """Run ``git -C cwd <args>`` and return stripped stdout, or ``None``.

    ``None`` covers every "no answer" case — git is not installed, the path is
    not a repo, the path does not exist, git hung. Callers fall back to the
    path itself, so a missing git degrades to per-directory keys rather than an
    exception (the fail-open convention).
    """
    try:
        completed = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    out = (completed.stdout or "").strip()
    return out or None


def _realpath(path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(path or os.getcwd())))


def resolve_project_root(cwd: str) -> str:
    """The main checkout for ``cwd``: the identity every worktree shares.

    Inside a git repo this is the parent of ``--git-common-dir`` when that
    directory is named ``.git`` (the normal layout, worktrees included).
    Anything else — bare repos, submodules, no git, not a repo — resolves to
    ``os.path.realpath(cwd)``.
    """
    key = str(cwd or os.getcwd())
    cached = _ROOT_CACHE.get(key)
    if cached is not None:
        return cached

    root = _realpath(key)
    common_dir = _run_git(key, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common_dir:
        common_path = Path(common_dir)
        if common_path.name == ".git":
            root = _realpath(str(common_path.parent))

    _ROOT_CACHE[key] = root
    return root


def encode_project_key(path: str) -> str:
    """Encode a resolved path as a directory name: ``/`` → ``-``.

    The ``~/.claude/projects`` convention, reused so a queue directory is
    eyeball-matchable against the session directory for the same project.
    """
    encoded = str(path).replace("/", "-")
    return encoded or "-"


def resolve_project_key(cwd: str) -> str:
    """The queue key for ``cwd`` — one key per repo, every worktree included.

    This is *the* project-identity function (invariant 7). Import it; do not
    re-derive a key anywhere else.
    """
    return encode_project_key(resolve_project_root(cwd))


def resolve_workspace_root(cwd: str) -> str:
    """The top of the checkout ``cwd`` actually lives in — where *we* may write.

    Deliberately **not** the project key: for a linked worktree this is the
    worktree's own top level, while ``resolve_project_root`` is the main
    checkout they share. The distinction is invariant 4 — an agent writes
    ``.claude/settings.json`` in the checkout it is running in and nowhere else,
    even when a foreign path shares its project key.

    Falls back to ``realpath(cwd)`` outside a repo, so a non-git workspace
    writes settings next to itself.
    """
    key = str(cwd or os.getcwd())
    cached = _WORKSPACE_ROOT_CACHE.get(key)
    if cached is not None:
        return cached

    top_level = _run_git(key, "rev-parse", "--show-toplevel")
    root = _realpath(top_level) if top_level else _realpath(key)
    _WORKSPACE_ROOT_CACHE[key] = root
    return root
