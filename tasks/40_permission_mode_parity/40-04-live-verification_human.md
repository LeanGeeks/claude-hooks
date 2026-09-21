# Task 40-04 — Live verification across all six modes

**Status:** todo · **Depends on:** 40-01 **and** 40-02, both **installed**
**Read first:** [brd.md](./brd.md) §4 (acceptance), §3 **H6/H8**
**Agent:** human

Nothing below is verified until `./install.sh --yes` has copied the new hooks into
`~/.claude/hooks/` (brd H6). The script is `install.sh`; `install-claude-config.sh`
was deleted in epic 29-09.

## 1. Baseline first

Take the "before" numbers so the "after" ones mean something:

```bash
grep -o "Permission mode: .*" ~/.claude/permission_request_debug.log \
  | sort | uniq -c | sort -rn
```

On 2026-09-21 that read 236 `bypassPermissions` / 94 `default` / 86 `auto`
(416 total, since 2026-09-14), of which 69 auto-mode rows were `Bash`
(brd §1.2). Record the equivalent numbers after a few days of normal use.

## 2. The matrix

One session per mode. In each, run the four probes and record what the terminal
did **and** what Telegram did — the two must agree (state.md invariant 5).

| Probe | Command |
|---|---|
| **P1** allowlisted | `git status` |
| **P2** unknown but harmless | a command not in `permissions.allow`, e.g. `some_unknown_command --flag` (or any real tool you have not allowlisted) |
| **P3** risk gate | `echo probe > ~/.claude/probe-40-04` — outside the workspace, `/tmp` and `/dev/null`, so the redirect gate fires |
| **P4** denied | `dd --version` — matches the `Bash(dd:*)` deny pattern |

**Both probes are deliberately harmless if the guard they test fails.** P3 writes
one throwaway file into your own `~/.claude`; P4 prints a version string. Do
**not** substitute `> /etc/passwd` or `dd if=/dev/zero of=/dev/sda`: a
verification probe whose failure mode is a wiped disk is not a probe, and the
whole point of the exercise is that the guard might not fire.

| Mode | P1 | P2 | P3 | P4 |
|---|---|---|---|---|
| `default` | runs, silent | prompt **+ Telegram card** | prompt + card | hard-blocked by the hook |
| `auto` | runs, silent | **no prompt, no card** if the classifier allows; card **and** prompt if it flags | prompt + card | hard-blocked |
| `acceptEdits` | runs, silent | prompt + card | prompt + card | hard-blocked |
| `plan` | runs, silent | prompt + card (`plan` is not a deferring mode) | prompt + card | hard-blocked |
| `bypassPermissions` | runs, silent | **runs silently, no card** | **no card** — the risk gate asks, the harness raises the prompt, and `permission_request_hook.py:1640-1660` auto-allows it *before* any Telegram send. A row is recorded; the terminal prompt may flash and resolve itself | hard-blocked |
| `dontAsk` | runs, silent | refused by the harness, **no card** | refused, no card | hard-blocked |

In `plan` mode also confirm **`ExitPlanMode` still reaches Telegram** — it is
the one prompt that mode exists to raise.

Plus, in every mode: **`AskUserQuestion` still reaches Telegram** (state.md
invariant 6). One question per mode is enough.

## 3. The evidence to capture

For each interesting probe:

```bash
grep -n "Decision: \|permissionDecision" ~/.claude/bash_hook_debug.log | tail -20
grep -n "Permission mode: \|Created request" ~/.claude/permission_request_debug.log | tail -20
```

A P2 in auto mode must show the hook emitting **`defer`** and **no** matching
`Created request` line. That single pair is the epic's acceptance criterion 1.

## 4. The open question this task answers (brd H8)

**Does a subagent's tool call carry the session's `permission_mode`?** Unmeasured
today. Spawn a subagent in an `auto`-mode session (a `Task`/`Agent` call that
runs Bash), then:

```bash
grep -B 4 "Agent ID: " ~/.claude/permission_request_debug.log | grep -A 4 "Agent ID: [^N]"
```

- Mode present and equal to the parent's → record it in
  [state.md](./state.md) and the invariant holds as written.
- Mode absent or `default` for a subagent in an auto session → the subagent's
  Bash calls keep asking (invariant 3 makes that the safe fallback, not a bug),
  and that is a finding worth a line in state.md so nobody re-derives it.

## 4b. A second question worth answering while you are here

`acceptEdits` was left **out** of the deferring set on the conservative reading
that it only auto-allows edits and treats Bash like `default`. The harness's own
mode predicate groups `acceptEdits` with `auto` / `bypassPermissions` / `dontAsk`
in at least one place, so that reading may be incomplete.

If the `acceptEdits` row of the §2 matrix behaves like `default` — P2 prompts —
the current set is right and nothing changes. If you see any sign that the
harness would have resolved a Bash call by itself in `acceptEdits` (a classifier
line in the transcript, an auto-allow that our floor pre-empted), record it in
[state.md](./state.md): it would mean `acceptEdits` belongs in
`DEFERRING_MODES`, which is a one-line follow-up, not a redesign.

## 5. Done criteria

1. The §2 matrix filled in, with the mismatches (if any) named.
2. Acceptance criteria 1–5 in [brd.md](./brd.md) §4 each marked pass/fail.
3. The post-change `Permission mode:` tally recorded in state.md's Log, next to
   the 2026-09-21 baseline.
4. H8 answered in state.md, either way — and §4b's question too.
5. Anything that contradicts brd §1 written down — the bundle claims there were
   read off `2.1.278` and the CLI moves fast (brd §1.6).
