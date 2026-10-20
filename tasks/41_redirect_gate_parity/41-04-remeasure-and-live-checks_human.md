# Task 41-04 — Re-measure after install, and the live checks the agents owe

**Status:** todo · **Depends on:** 41-01, 41-05, 41-02, 41-03 committed and
installed (`./install.sh --yes`, never `--all`)
**Agent:** human
**Owner:** human (normal use) — an agent can run the greps and the tally.

## 1. Live checks (once, right after install)

Each implementer was allowed to leave these as "owed". Close whichever its
report lists as owed:

1. **H1 — `CLAUDE_PROJECT_DIR` reaches the hooks.** Also confirm 41-02's log
   line shows a `transcript_path` in the PreToolUse payload. In a main session and a
   worktree session, find 41-05's debug line in
   `~/.claude/bash_hook_debug.log` / `~/.claude/permission_request_debug.log`
   and record the values. If the fallback branch's debug line appears instead,
   the env var is missing and the fix is running on the git-top-level fallback.
2. **H2 — worktree memory key.** In a worktree session, confirm which
   `~/.claude/projects/<key>/memory/` Claude Code writes, and that
   `echo x >> <that dir>/MEMORY.md` raises no card in auto mode.
3. **H5 — memory writes in `default` mode.** In a `default`-mode session, have
   Claude save a memory with the Write tool. 41-02 ships the memory root in
   `DEFERRING_MODES` only. If Claude Code does **not** prompt, file a one-line
   follow-up to extend it to every mode (invariant 5: mirror the terminal); if
   it prompts, record that and leave it.
4. **The Whitelist button from a subdirectory.** In a `default`-mode session,
   `cd` into a subdirectory, trigger a card for an unlisted command, tap
   Whitelist, and confirm the rule lands in the project root's
   `.claude/settings.local.json`.

## 2. The tally (after about a week of normal use)

Re-run the brd §1.2 count over entries newer than the install and record it in
state.md's Log next to the original 279 prompts / 365 targets:

```bash
python3 - <<'EOF'
import json, os, re, collections
SINCE = "<install timestamp, ISO>"
rows = [json.loads(l) for l in open(os.path.expanduser('~/.claude/bash_manual_confirm.log')) if l.strip()]
def asked(r):
    # 'emitted' is the printed permissionDecision as a string ('ask' / 'allow'
    # / 'deny'), or null when the hook printed nothing (pretool_hook.py:417).
    return r.get('emitted') == 'ask'
red = [r for r in rows if r.get('timestamp', '') >= SINCE
       and r.get('permission_mode') in ('auto', 'bypassPermissions', 'dontAsk')
       and asked(r)]
print(len(red), collections.Counter(r['permission_mode'] for r in red))
print(collections.Counter((r.get('reason') or '')[:40] for r in red).most_common())
for r in red:
    print(r['workspace'], '->', re.findall(r'`([^`]*)`', r.get('reason') or '')[:3])
EOF
```

Filter on `emitted`, not on the reason prefix: after 41-03 an unresolved target
is logged with `emitted: null` in deferring modes, and its reason text changes.

## Expect

- No `)`-suffixed targets, no own-memory-dir targets, no unanchored `$…`
  targets in deferring modes.
- Any remaining memory-dir target names a *different* project's key — check it
  was a real cross-project write, not a key-derivation miss (H2).
- What remains is the "outside project" row (brd §1.2, 87) — the gate doing its
  job. If a pattern in it looks like a legitimate root Claude Code also allows,
  file it as a follow-up rather than widening the roots here.
