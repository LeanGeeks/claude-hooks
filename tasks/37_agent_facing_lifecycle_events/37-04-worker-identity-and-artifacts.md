# Task 37-04 — Worker identity, artifact paths, and transcript persistence

**Status:** todo · **Depends on:** 37-02 (so persistence is no longer load-bearing)

## Goal

Make a tracked worker's recorded identity and artifacts correct and deliberate:
paths that can exist, transcript persistence chosen rather than inherited, and a
handle you can refer to by the name you spawned it with.

## Why

Two defects and one silent failure path, independent of the state machine, each
of which cost real time on the driving run
([evidence.md](./evidence.md) §§2, 3):

- A stat against a never-created artifact returned nothing, and every consumer
  carried on to a confidently wrong answer. Nothing anywhere reported that a
  depended-on artifact was absent. Recorded artifact *paths* were correct — see
  [evidence.md](./evidence.md) §2 for what was checked and ruled out.
- `CLAUDE_CODE_CHILD_SESSION` is ambient and inherited down the process tree, not
  set per spawn. It silently disabled transcript persistence for **26 worker
  sessions**, leaving zero transcripts and making the run unreconstructable.
- Spawning suffix `unit-028-pre` registers handle `leads-platform-unit-028-pre`;
  querying the name you spawned returns `unknown (no tracked handle found)`.

After 37-02 none of these can break state. They still break observability and
ergonomics, which is why they are worth fixing on their own terms rather than as
a side effect.

## Read first

- [brd.md](./brd.md) §4.4 · [evidence.md](./evidence.md) §§2, 3
- `.claude/hooks/amux_spawn_lib.py` — deterministic transcript path construction
  (`:83-89`), handle naming/workspace prefixing, `resolve_amux_session`
- `.claude/bin/amux-spawn` — `_resolve_handle`, and the spawn path that composes
  the registry key from workspace + suffix
- `shell/profiles.example.toml` and `install-claude-config.sh` — where a
  persistence default would be expressed and installed
- `tests/test_unit_amux_ergonomics.py`, `tests/test_unit_amux_spawn.py`

## Work

1. Make a missing or stale artifact degrade **visibly** rather than silently
   producing a false negative, and confirm that every recorded path refers to
   something that can exist.
2. Make transcript persistence for spawned workers an explicit, documented
   decision with a discoverable default — not a property inherited from whatever
   started the process tree. Decide what the default should be and record why.
3. Establish and test the boundary the epic depends on: persistence affects
   **observability only**. Every state and supervision path must behave
   identically with it on and off.
4. Make handle reference forgiving. A caller that spawned a worker under a given
   suffix should be able to refer to it by that suffix; ambiguity should produce
   an actionable error naming the candidates, not `unknown`.
5. Check whether the same class of defect exists elsewhere in recorded handle
   fields — `activity_path`, `result_path`, `process_pid` are all nullable
   best-effort fields and at least one was observed wrong.

## Implementation hints — suggestions, not instructions

- The interesting question is not path construction — that was checked and is
  correct — but why an absent artifact produced silence instead of a complaint.
  `transcript_mtime()` returning `None` for "no file" is indistinguishable from
  `None` for "no path recorded", and every caller treats both as "no
  information" rather than "something is wrong". Separating those two may be
  worth more than any individual fix here.
- `[all-profiles]` in `~/.claude/profiles.toml` looks like the natural home for
  a persistence default, and it is *not* — check this before building on it.
  `load_profiles()` (`.claude/hooks/amux_spawn_lib.py:1266-1283`) only ever
  builds env for `[profile.X]` sections, folding `[all-profiles]` into each one;
  a file with `[all-profiles]` and no profiles resolves to `{}`. And `cmd_spawn`
  applies profile env only when `--profile` was passed
  (`.claude/bin/amux-spawn:264-270`). So a plain `amux-spawn spawn` — the common
  case, and the one the driving run used — would inherit nothing from the file,
  and the default would silently not apply exactly where it matters. Either put
  the default somewhere `spawn` always reads, or extend the profile resolution
  to apply `[all-profiles]` with no named profile, and say which you did.
- Whichever way it goes, note the trade-off honestly: leaving worker transcripts
  on costs disk and was presumably suppressed on purpose at some point. Recall
  also that amux's env allowlist is prefix-based over `ANTHROPIC_*` / `CLAUDE_*`,
  so a `CLAUDE_CODE_*` persistence variable does reach the spawned child once
  something sets it.
- For handle resolution, the workspace prefix is derivable at query time from the
  caller's directory. Exact-match-then-prefix-search, erroring on ambiguity, is
  the conventional shape; `ls` already enumerates the registry.
- Whatever is decided about persistence, the epic's §6 criterion stands: the
  event stream alone must be enough to reconstruct a run. Transcripts are a
  convenience on top, never the record of last resort.

## Done when

- An absent or stale artifact is reported as such rather than silently treated
  as "no information"; a test covers it. Recorded paths are confirmed to refer
  to artifacts that can exist.
- Transcript persistence for spawned workers is explicit, documented, and
  defaulted deliberately — and the default demonstrably takes effect for a plain
  `amux-spawn spawn` with no `--profile`, proven by a test rather than by
  reading the config file.
- A matrix test shows identical state and supervision behavior with persistence
  on and off.
- A worker can be addressed by the suffix it was spawned with; ambiguous
  references produce an error naming the candidates.
- Existing spawn, ergonomics, and reads suites stay green.
