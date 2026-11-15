# Task 48 — Allow rules for the question tools, installed on every host

**Status:** done · **Type:** installer · **Created:** 2026-11-15 · **Rev:** 1

## Problem

hyppie-flow's workflow makes these calls routine, including in unattended
sessions: `mcp__questions__ask`, `notify`, `claim`, `withdraw`, and the
`claude-questions` command (`--sync` after a hand edit, `--status`, `--check`,
the bare form once a tick). On this machine only `ask` and `notify` were on an
allow list; a session with nobody at the keyboard stopped at a permission
prompt on the other three.

## Ruling

The operator, asked whether to add the rules to hyppie-flow's committed
settings: "yes, and make that in claude-hooks repo, to make it expand on other
hosts too". The ruling reached this repository through another session; it was
confirmed by the operator in the implementing session before anything was
changed ("Yes, add all three"), because it widens what sessions may do without
a prompt.

## Decision

The rules go where `ask` and `notify` already were: `permissions.allow` in this
repository's `.claude/settings.json`, which the `permissions-allowlist` feature
(default install) unions into `~/.claude/settings.json`.

```
mcp__questions__ask
mcp__questions__notify
mcp__questions__claim
mcp__questions__withdraw
Bash(claude-questions:*)
```

- **File they land in:** `~/.claude/settings.json`, `.permissions.allow`.
- **Shell rule:** `Bash(claude-questions:*)` — this repository's existing
  prefix form (`Bash(amux:*)`), scoped to the one command.
- **Not tied to the `questions` toggle.** Neither were `ask` and `notify`. A
  host without the questions feature has no such tool and no such command, so
  the rules permit nothing there; tying them to the toggle would need a second
  owner for allow rules, which the installer does not have.
- **Uninstall / idempotence:** the allowlist's own. Rules it added are recorded
  in the manifest and subtracted on uninstall; a re-install adds nothing twice.
- **Found on the way, fixed:** a re-install recorded only what *that run* added
  — nothing, the second time — and replaced the manifest's list with it, so an
  uninstall after any re-install left every rule behind. On this machine the
  rules arrive on a re-install, so the gap was not hypothetical. The manifest
  now carries earlier additions forward (union mode only).

## What it permits

In a workspace with `[questions] sync = "push"`, `claim`, `withdraw` and
`claude-questions --sync` commit and push queue files — only queue files, and
never the local branch (epic 46) — without the approval an agent's own
`git push` needs. `sync` is off unless a workspace writes it down.

## Test

`tests/test_unit_installer.py`: `test_question_tool_rules_reach_every_host`
(two installs: each rule present once, no wider shell rule, all on record in the
manifest) and `test_uninstall_after_a_reinstall_removes_what_was_added_and_only_that`.

## Installed here

2026-11-15, `./install.sh --yes`: `Permissions merged (union): +3 allow`.
`~/.claude/settings.json` `.permissions.allow` now holds all five.
