# 21-03 — Effort survives a spawn chain, by flag (optional)

**Status:** todo · **Depends on:** 21-01 · **Optional** — ask before running
**Read first:** [brd.md](./brd.md) §1 findings 3–6, §2.2 · [21-01](./21-01-spawn-pin-warning_sonnet.md)

## Goal

Give DW-68 item 2 what it actually wanted — *effort that survives a spawn chain
without every caller restating it* — through the channel that is already proven
safe for the model: **an explicit flag**, resolved at spawn time, visible in
`CC_FLAGS`, and beatable by the caller.

`inherited_model_flag()` already does this for `--model`: when the caller passes
none, propagate the parent's explicit `--model` read from the parent's
`CC_FLAGS`. Nothing equivalent exists for effort, which is why brd §1 finding 4
says effort has no inheritance path at all.

**Why not the environment:** finding 6. An inherited env var *overrides* the
flag, so it cannot be a fallback — it would be a silent override. A flag-shaped
inheritance is a fallback by construction: it is only added when the caller
passed nothing.

## Scope

### `.claude/bin/amux-spawn`

Mirror the model path, immediately after it:

```python
def inherited_effort_flag(caller_flags: list[str], parent_name: str | None) -> str | None:
    """``--effort`` inheritance, mirroring inherited_model_flag (epic 21)."""
    if lib.extract_flag_value(caller_flags, "--effort") is not None:
        return None  # caller already specified one; don't override
    if parent_name:
        return lib.extract_flag_value(lib.parent_cc_flags(parent_name), "--effort")
    return None
```

and, next to the existing `inh_model` block in `cmd_spawn`:

```python
    inh_effort = inherited_effort_flag(forward_flags, parent_name)
    if inh_effort:
        forward_flags += ["--effort", inh_effort]
```

`extract_flag_value` is 21-01's generalization of `extract_model_flag` — this
task depends on it existing.

**Ordering matters:** both inheritance blocks must run **before** 21-01's warning
block, so an inherited effort counts as pinned and the warning stays quiet on a
chain that is in fact pinned. If 21-01's block sits where its spec puts it (after
`inh_model`), insert this one directly above it.

### Two questions to answer before writing the code, not after

1. **Does an inherited effort reach the child at all?** The model path works
   because amux forwards unknown flags to `claude` (`parse_claude_flags` →
   `CC_EXTRA_ARGS`). Confirm `--effort` takes the same route through the
   *installed* `/usr/local/bin/amux`, by spawning a child with `--effort` and
   reading the effort out of its transcript. If it does not, this task is
   blocked on an amux change and should be re-scoped rather than merged half-way.
2. **Is inheriting effort even wanted by default?** Model inheritance exists so
   `opus → opus` holds down a chain. Effort is cheaper to get wrong in the
   expensive direction: a `max` parent would silently make every descendant
   `max`. If that is unwanted, the honest shape is inheritance behind an opt-in
   flag, or no inheritance at all and a louder 21-01 warning. **Answer this with
   the operator before implementing** — it is a policy question wearing a code
   question's clothes.

## Testing

Mirror 21-01's cases in `tests/test_unit_amux_spawn.py` — **and read its
§Testing notes on the two harness traps first**: every case below needs a real
parent, and `_run_spawn_nontty` patches `lib.resolve_amux_session` to `None`, so
a case written against the bare helper is inert and passes whatever you do.

- parent `CC_FLAGS` carries `--effort high`, caller passes none → `forward_flags`
  gains `--effort high`, and 21-01's effort warning does **not** fire;
- caller passes `--effort low` with the same parent → caller wins, exactly one
  `--effort` in `forward_flags`;
- no parent → no `--effort` added, warning fires.

Report the suite's before/after counts.

## Done criteria

1. Both questions above answered in writing, with the transcript evidence for
   question 1, **before** any code lands.
2. Code + tests as above; `python3 tests/run_all_tests.py` green.
3. `install-claude-config.sh` re-run note as in 21-01 §Done criteria 4.
4. state.md Log records the answer to question 2 as a decision, not a preference.
