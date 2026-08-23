# 21-01 — `amux-spawn` warns when a non-TTY spawn pins no model or no effort

**Status:** todo · **Depends on:** none
**Read first:** [brd.md](./brd.md) §1 (findings 1–4), §2.2, §3 H1/H5 · [state.md](./state.md) invariants

## Goal

One stderr warning per unpinned knob, on the agent path only. After this task, a
spawn that lets its child read the floating harness default **says so**, and the
next spawn site somebody adds without pins reports itself the first time it runs.

**This task ships no policy and no default.** It does not choose a model, does
not choose an effort, does not refuse, and does not change any exit code. It
prints.

**This task does not touch amux.** The env-allowlist half is
[21-02](./21-02-allowlist-decision_human.md) plus the fork's own epic 02 — and it
resolved to *guarding* the allowlist rather than extending it (brd §1 finding 6).
Do not edit `../amux/amux` here, and do not run `install-amux.sh`.

## Scope

### 1. A general flag extractor — `.claude/hooks/amux_spawn_lib.py`

`extract_model_flag()` (currently ~`:206`) already handles both token forms
(`--model X` and `--model=X`). Generalize it and keep the existing name working:

```python
def extract_flag_value(flags: list[str], flag: str) -> str | None:
    """Return the value of ``flag`` within a flag token list, else None."""
    for i, tok in enumerate(flags):
        if tok == flag and i + 1 < len(flags):
            return flags[i + 1]
        if tok.startswith(flag + "="):
            return tok.split("=", 1)[1]
    return None


def extract_model_flag(flags: list[str]) -> str | None:
    """Return the value of ``--model`` within a flag token list, else None."""
    return extract_flag_value(flags, "--model")
```

`extract_model_flag` has callers in `amux-spawn` and its own tests — keep its
signature and behaviour byte-for-byte identical. The bare-`--flag`-at-end-of-list
case must return `None`, not raise `IndexError`; that is already the behaviour and
there is a new test for it below.

### 2. The guard — `.claude/bin/amux-spawn`, inside `cmd_spawn`

**Placement is the substance of this task.** Put the check **after** the
inherited-model block (currently `:208–210`, `inh_model = inherited_model_flag(...)`
and the `forward_flags += ["--model", inh_model]` that follows) and **before**
`stuck_after_s = parse_stuck_after(...)`. Two reasons, both load-bearing:

- by then `forward_flags` already carries an **inherited** `--model`, so a chained
  opus→opus spawn is correctly seen as pinned and does not cry wolf;
- it is after the profile env export at the top of `cmd_spawn`, so a profile that
  really does pin the model (`claude_glm_env` exports `ANTHROPIC_MODEL=glm-4.7`;
  epic 12 verified it reaching a child) is also seen as pinned.

```python
    # Automated spawns must not read the harness default (epic 21). The non-TTY
    # path IS the agent path, so warn there — never refuse (brd §2.2), never
    # raise (brd §3 H1).
    if not is_tty:
        model_pinned = (lib.extract_model_flag(forward_flags) is not None
                        or bool(os.environ.get("ANTHROPIC_MODEL")))
        effort_pinned = lib.extract_flag_value(forward_flags, "--effort") is not None
        if not model_pinned:
            _eprint("amux-spawn: warning: non-TTY (agent) spawn pins no model — "
                    "the child reads the harness default, which floats between "
                    "sessions; pass --model <alias>")
        if not effort_pinned:
            _eprint("amux-spawn: warning: non-TTY (agent) spawn pins no effort — "
                    "the child reads the harness default; pass --effort <level>")
```

Three decisions in that block that are **not** free to change:

1. **Key on `not is_tty`, not on `tracked`.** `tracked` is also true for a human
   `--wait`/`--notify` at a TTY (`:191`), and that human is not the hazard — they
   are interactively awaiting a result and cannot silence the warning without
   changing how they work. The agent path is the non-TTY path.
2. **`CLAUDE_CODE_EFFORT_LEVEL` in the environment does NOT count as pinned
   effort — permanently.** Two independent reasons, either sufficient. It cannot
   reach the child at all: `amux`'s allowlist does not carry it (brd §1 finding
   5), so counting it would make the guard go quiet about a path that does not
   work. And if it ever could reach the child, it would **override** the
   `--effort` flag rather than back it up (finding 6) — so treating it as a pin
   would report "pinned" about the one input that can silently un-pin everything
   else. Leave a comment citing finding 6, so the next reader does not
   "complete" the check.
3. **`ANTHROPIC_MODEL` counts, `--profile` alone does not.** `--profile` carries
   only the alias→id map for the default profile and pins nothing (brd §1
   finding 2); testing for `args.profile` would be a false negative on the
   default profile, which is the common case.

Match the file's existing warning form — `resolve_dir`'s is printed at `:193–195`
as `amux-spawn: warning: {warning}`.

## Testing

`tests/test_unit_amux_spawn.py` (`unittest`, driven by
`python3 tests/run_all_tests.py`). `TestSpawnDispatch._run_spawn_nontty` already
mocks amux/tmux and forces both `isatty` calls to `False` — reuse it rather than
building a second harness, but **do not change its return arity**: it has existing
callers that unpack three values (`grep -n "_run_spawn_nontty(" tests/`). Capture
stderr around `cli.main(argv)` — `_eprint` is `print(msg, file=sys.stderr)` — via
a sibling helper or `contextlib.redirect_stderr`, not by changing the tuple.

**Two cases need setup the existing helper deliberately does not provide. Read
this before writing them — one of them can kill the test runner.**

- **Case 5 (TTY) will `exec` away your test process if you only flip `isatty`.**
  With both `isatty` mocks `True` and no `--detach`/`--wait`,
  `detach = bool(args.detach) or wait_mode or (not is_tty)` is `False`, so
  `cmd_spawn` reaches `_amux_attach(chosen_name)` (`:301`), which is
  `os.execvp("amux", ["amux", "attach", name])` (`:384`). `amux` **is** installed,
  so that call succeeds and replaces the running interpreter — the suite never
  returns. Mock it: `patch.object(cli, "_amux_attach")`, and assert it was called
  if you want the TTY path pinned as well. (Passing `--detach` also avoids the
  exec, but it tests a different path than "a human at a terminal".)
- **Case 6 (inheritance) is inert unless you give it a parent.** The helper
  patches `lib.resolve_amux_session` to `None`, so `inherited_model_flag` returns
  at its first branch and no `--model` is ever added — the case would pass with
  the guard in *either* position and prove nothing. Give it a real parent:
  `patch.object(cli.lib, "resolve_amux_session", return_value="parent-x")` plus
  either `patch.object(cli.lib, "parent_cc_flags", return_value=["--model", "opus"])`
  or a `CC_FLAGS="--model opus"` line in `<tmp>/sessions/parent-x.env` (the
  redirected `AMUX_SESSIONS_DIR`; `parent_cc_flags` tokenizes on whitespace).
  Keep passing `--dir`, as the existing helper does — with a parent name present,
  `resolve_dir` would otherwise go looking for the parent's `CC_DIR`.

Cases (new class `TestSpawnPinWarnings` unless the file's layout suggests
otherwise):

| # | Spawn | Expect |
|---|---|---|
| 1 | non-TTY, no flags | both warnings; `rc` unchanged from the existing baseline test |
| 2 | non-TTY, `--model opus` | effort warning only |
| 3 | non-TTY, `--model=opus --effort=high` | neither |
| 4 | non-TTY, `--effort high` (space form) | model warning only |
| 5 | **TTY** (both `isatty` → `True`), no flags | neither |
| 6 | non-TTY, no `--model`, parent `CC_FLAGS` carrying `--model opus` | effort warning only (inheritance counts) |
| 7 | non-TTY, no `--model`, `ANTHROPIC_MODEL=glm-4.7` in env | effort warning only |
| 8 | `extract_flag_value` units | `--effort high`, `--effort=high`, absent → `None`, **trailing bare `--effort` → `None`** |

Case 6 is the regression guard for the placement decision: written before the
`inh_model` block it fails, after it passes. Case 5 guards the `is_tty` decision.

Baseline: the suite was green at **716 root hooks / 252 relay** at epic 19's
close (its state.md records `Relay 249 → 252, root hooks 716`) — **re-measure
rather than trusting those numbers**, and report the before and after counts.

## Done criteria

1. `python3 -m py_compile .claude/bin/amux-spawn .claude/hooks/amux_spawn_lib.py`
   clean, and `amux-spawn --help` still runs.
2. `python3 tests/run_all_tests.py` green, with the 8 cases above added and the
   before/after counts reported.
3. Manual check, reported with its literal output:
   `printf '' | amux-spawn spawn --dir /tmp --profile claude -- 'hi'` (non-TTY via
   the pipe) prints both warnings; adding `--model sonnet --effort medium` prints
   neither. Clean up any session it creates (`amux-spawn rm <name>`), and say
   which name you removed.
4. **Not live until installed:** note in your report that
   `./install-claude-config.sh` (step 3, `.claude/bin/amux-spawn` →
   `~/.local/bin/amux-spawn`, no sudo) must run for the change to reach `PATH`.
   Run it if the epic manager asks; otherwise flag it.
5. No edit outside the three files named above.
