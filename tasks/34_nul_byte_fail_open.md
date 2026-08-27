# Task 34 — A NUL byte in a redirect target crashes the validator, which fails open (bug)

**Status:** todo · **Type:** bug · **Created:** 2026-08-27 · **Rev:** 1
**Priority:** medium — small and self-contained, but the *class* in §3 matters
more than this instance
**Suggested worker:** one implement → review → fix loop; genuinely small
**Read first:** §1 · §3 (the class — the part worth the effort)
**Scope:** `.claude/hooks/pretool_hook.py`, `tests/`, this file.
**Origin:** task 28's round-3/4/5 reviewers, consistently, in fuzzing — 63 of
9014 and 226 of 30000 structured inputs. Reproduced by the manager.

## 1. The defect

`_resolve_target_path` (`pretool_hook.py:742`) calls `os.path.realpath`, which
raises on an embedded NUL. With a redirect target containing a NUL byte:

```
File "<frozen posixpath>", line 457, in realpath
ValueError: lstat: embedded null character in path
```

Reproduce from the repo root (the NUL is built in Python so this file stays
free of control characters):

```
python3 -c 'import json,sys; sys.stdout.write(json.dumps({"command": "echo hi > /tmp/a" + chr(0) + "b"}))' \
  | CLAUDE_HOOK_REPLAY=1 python3 .claude/hooks/pretool_hook.py --dry-run
```

`main()`'s blanket `except Exception` (`:2133`) then exits 0 with no stdout.
Exit 0 with no output is this hook's documented "no decision" path — the harness
falls back to its native prompt. So a crash is a **soft allow**: the gate that
should have decided did not decide.

Whether this specific input is weaponisable is doubtful — a NUL truncates a
`bash -c` string, so what bash runs is probably not what the validator choked
on. That is why this is medium, not critical. The class is the point.

## 2. The fix

Have `_resolve_target_path` return `None` — its own documented "cannot resolve
with confidence" answer — for a path it cannot lstat, instead of propagating.
The containment check already treats `None` as unsafe, so the redirect gate
reaches its existing conservative branch.

Do **not** just wrap the call site in `try/except ValueError`: check the operand
for a NUL explicitly, so the reason is legible in the log and so a future
`realpath` failure mode is not silently swallowed by the same handler.

## 3. The class — the part actually worth doing

`main()`'s blanket `except Exception: sys.exit(0)` means **any** unhandled
exception anywhere in validation is a soft allow. That is the right default for
availability — a broken hook must not brick every session — and the wrong
default for a permission gate. Nothing currently distinguishes the two.

Deliverable beyond the one-line fix:

1. **Enumerate what else can raise out of validation.** The fuzz corpora that
   found this are the tool, and the surface is small enough to finish: 60,000+
   inputs through `validate_bash_command`, `parse_compound_command`,
   `extract_write_redirect_targets` and `extract_assignments` came back clean
   apart from this one class.
2. **Make the fail-open loud.** A crash currently leaves no trace an operator
   would ever find. It should write the command and traceback to
   `~/.claude/bash_hook_debug.log` (or the `CLAUDE_MANUAL_CONFIRM_LOG`
   destination) **unconditionally** — not behind `CLAUDE_HOOK_DEBUG`. A gate
   that fails open silently cannot be noticed; one that fails open loudly can be
   fixed.
3. **Decide, and record, whether fail-open is still right** for the *validation*
   path specifically, as distinct from the Telegram/relay paths where it plainly
   is. Epic 22's H1 argues a false deny is worse than a false allow because it
   hard-blocks with no rescue — but exit 0 with no stdout does not hard-block
   either, it hands back to the native prompt. That may make fail-*closed* cheap
   here. Measure before deciding.

Second instance of this class in one epic, which is the argument for item 1:
task 28's round-5 review found that inlining `_resolve_constant` had dropped its
`cmd_offset is None` arm, so a future third caller would raise `TypeError` →
`sys.exit(0)` → allow. An `assert` now guards it.

## 4. Tests

Watched to fail first, output quoted:

1. §1's command decides `ask` (or whatever §2 lands on) rather than crashing.
2. A NUL in other operand positions: the command word, an argument, a `source`
   operand, and inside quotes.
3. §3 item 2's fail-open logging fires and names the command.
4. **Regression floor:** ordinary redirect targets keep their verdicts —
   `> /tmp/x` allows, `> /etc/cron.d/pwn` asks.

Note `_assert_probe_safe` in `tests/test_integration_pretool.py` refuses to
spawn bash for non-inert commands; keep any real-bash case inert.

## 5. Constraints

- **Shared checkout.** Never `git checkout`/`stash`/`reset`/`restore`, with or
  without a pathspec. Mutation-test in `/tmp` copies.
- **A repo edit is not live.** Do not run `./install-claude-config.sh`.
- Full suite green, counts reported. Baseline at filing: **1393 ran, OK, 1
  skipped** (`test_headless_spawn`).
- Re-run a fuzz pass of comparable size to confirm no new crash class appeared
  and that item 1's enumeration is complete.

## 6. Done criteria

1. §1 no longer crashes.
2. §3's enumeration recorded, the fail-open made loud, and the
   fail-open/fail-closed question answered with a measurement rather than an
   assertion.
3. §4's tests; suite green with counts.
4. Implementation log here.
