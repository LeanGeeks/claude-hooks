# Improvement opportunities

Findings collected on **2026-09-18** while adding the prompt-cache cell to the status
line (commits `03d4584`, `c44a6cb`, `bdacb1f`). Nothing here is implemented.

Most of it comes from reading the shipped Claude Code bundle (**2.1.274**), so every item
cites its evidence and should be re-checked against the installed version before acting —
these are internals, and they move. [Re-verifying](#re-verifying-against-a-new-claude-code)
is a two-command job; the method is at the bottom.

| # | Opportunity | Effort | Why now |
|---|---|---|---|
| [1.1](#11-prompt-cache-miss-diagnostics) | Cache-miss cause + cold-restart cost in the main line | S | Data already in the payload |
| [1.2](#12-worktree-pr-and-session-name) | Worktree / branch / PR indicator | S | Several epics share one checkout |
| [1.3](#13-rate_limitsspend_limit) | Gateway spend limit | XS | Documented window we silently drop |
| [1.4](#14-tokensamples-and-cwd-on-agent-rows) | Stalled-agent detection from `tokenSamples` | M | Fleet supervision |
| [2.1](#21-subagents-could-get-the-1h-cache) | Give subagents a 1h cache | XS config | Agents idle between messages |
| [2.2](#22-subagent-cache-eviction-is-opt-in-for-now) | Watch subagent cache eviction | — | Would make our `warm` optimistic |
| [3.1](#31-the-installer-suite-does-not-configure-itself) | `CLAUDE_INSTALL_NO_EXTERNAL` in conftest | XS | 207/208 fail without it |
| [3.2](#32-payload-schema-drift-has-no-guard) | Payload-drift probe + fixture | M | README was a version stale |
| [3.3](#33-clock-boundary-flakiness-in-tests) | Mid-bucket test helper | XS | Bit twice in one session |
| [3.4](#34-the-effort-catalogue-is-a-hand-maintained-model-list) | Effort catalogue upkeep | — | Drifts every model release |

---

## 1. Data already in the payloads that nothing reads

### 1.1 Prompt-cache miss diagnostics

`statusline.py` consumes three fields of the `prompt_cache` block (`expires_at`, `ttl`,
`warm`). The harness publishes far more in the same object:

| Field | Meaning (statusLine stdin schema) |
|---|---|
| `misses` | Requests whose cached prefix shrank materially without a compaction explaining it |
| `last_miss_cause.causes` | Closed set: `system_prompt_changed`, `tools_changed`, `model_changed`, `messages_rewritten`, `ttl_expired_5m`, `ttl_expired_1h`, `likely_server_side`, `unknown` |
| `last_miss_cause.tools_added` / `tools_removed` / `system_char_delta` | Counts accompanying some causes |
| `miss_causes` | Misses per cause this session |
| `hit_ratio` | `cache_read / (cache_read + cache_creation + uncached input)` |
| `expected_rebuilds` | Rebuilds a compaction or tool-result clearing announced |
| `cache_write_tokens`, `miss_recache_tokens` | Tokens written, and the share written by misses |
| `recache_tokens_if_cold` | Tokens the next request re-caches if the cache is cold by then |

Two segments worth considering: a miss indicator (`miss 3 · tools_changed`) that names what
keeps breaking the prefix, and a cold-restart cost hint built on `recache_tokens_if_cold` —
the one number that says what letting the cache lapse will actually cost.

Evidence: `pqo()` in the bundle spreads the block into the payload; schema documented in the
`statusline-setup` agent prompt. Fields are absent until the first API response.

### 1.2 Worktree, PR and session name

Unused today (`grep` over `statusline.py` finds zero references):

- `worktree` — `{name, path, branch, original_cwd, original_branch}`, present in `--worktree` sessions
- `workspace.git_worktree` — worktree name when cwd is in a linked worktree
- `workspace.repo` — `{host, owner, name}` from the origin remote
- `pr` — `{number, url, review_state, kind}` for the current branch, mirroring the footer badge
- `session_name` — set via `/rename`
- plus `thinking.enabled`, `output_style.name`, `version`, `fast_mode`, `vim.mode`, `remote.session_id`

The first two matter most here: this repo is worked as **one checkout with several epics in
flight**, and a status line that names the worktree/branch makes "which tree am I in" a
glance instead of a `git status`. `pr.number` would pair with the review workflow.

### 1.3 `rate_limits.spend_limit`

`format_claude_rate_limits()` reads `five_hour` and `seven_day` only. The schema also
documents `spend_limit` — "behind a Claude gateway, your fullest spend limit", with
`used_percentage` and `resets_at`, same shape as the other two. Gateway users currently
see nothing for it. Roughly a five-line addition next to the existing windows.

### 1.4 `tokenSamples` and `cwd` on agent rows

The subagent payload carries `tokenSamples` — a 16-deep ring buffer of `tokenCount`, one
sample per tick, i.e. **about 80 s of history** — and `cwd`. Neither is used.

A flat tail of samples on a row whose `status` is still `running` is a stalled agent: no
tokens for 80 s while nominally working. That is exactly the failure
[task 37](../tasks/37_agent_facing_lifecycle_events/brd.md) describes hunting by hand, and
here it needs no transcript parsing at all. `cwd` would show which worktree an agent is
running in, which matters once amux fans agents across directories.

Caveat: 80 s is the whole window. It detects a stall, it cannot measure a long idle — that
is what the cache cell's clock is for.

## 2. Configuration levers found in the bundle

### 2.1 Subagents could get the 1h cache

Subagents cache for 5m because the 1h window is granted per **query source** against an
allowlist that ships as:

```js
var Krt = ["repl_main_thread*", "sdk", "auto_mode", "memdir_relevance"];
```

Agent query sources are not on it. But the TTL resolver (`yRn`) checks two knobs *before*
that allowlist:

1. `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` (env)
2. `subagentPromptCacheTtl` (settings)

Setting either to `"1h"` should give agent caches an hour. For fleet work — agents that
finish, sit idle, then get continued via `SendMessage` — that is the difference between a
warm continue and re-caching the whole context. The status line cell now measures it
directly, so the experiment is self-verifying: set it, spawn an agent, watch whether the
cell opens at `warm 59m`.

**Check the cache-write price premium for 1h vs 5m writes before turning this on
fleet-wide** — a longer window costs more per write, and an agent that never gets continued
pays that for nothing. Worth measuring against `cache_write_tokens` before committing.

### 2.2 Subagent cache eviction is opt-in (for now)

```js
function PGo(){
  if(!sy() || !uTe()) return false;
  if (env.CLAUDE_CODE_SUBAGENT_CACHE_EVICT) return true;
  return I("tengu_subagent_cache_evict", false);   // remote gate, default false
}
```

Off by default, but it is a **remote gate**: Anthropic can enable it without a client
release, and `evictCacheOnComplete` is already plumbed through the agent query path (beta
`prompt-caching-evict-2026-05-12`). If it ever turns on, a completed agent's cache is gone
the moment it finishes and our `warm 3m` on a `done` row becomes a lie.

Cheap insurance if that day comes: treat terminal rows as cold regardless of the clock.
Not worth doing pre-emptively — but worth knowing where to look when the numbers stop
matching reality.

## 3. Repo and developer experience

### 3.1 The installer suite does not configure itself

`tests/test_unit_installer.py` asserts `CLAUDE_INSTALL_NO_EXTERNAL` is in `os.environ` in
`setUp`, so a plain `pytest tests/test_unit_installer.py` fails **207 of 208 tests** with a
message that reads like catastrophic breakage. The seam is documented in
[installer.md](./installer.md), but nothing points there from the failure.

A `conftest.py` that sets the variable (or a `pytest.ini` `env` entry) would make the suite
self-configuring and keep the assertion as a backstop for anyone overriding it.

### 3.2 Payload-schema drift has no guard

The statusline README claimed the subagent payload had no `type`, `cwd` or `tokenSamples`.
By 2.1.274 all three were present — the doc was simply a version behind, and nothing would
have flagged it. `prompt_cache` on the main payload went unnoticed the same way, which is
how the first version of the cache cell ended up parsing transcripts next to a field that
states the answer.

Proposal: a probe that captures one real payload of each shape into `fixtures/`, plus a test
that fails when a key appears that the scripts do not know about. Drift becomes a failing
test the week it lands instead of a discovery a year later. The capture is the fiddly part —
the statusline command's stdin is only reachable from inside the command, so the probe has
to be wired as the `statusLine`/`subagentStatusLine` command for one run.

### 3.3 Clock-boundary flakiness in tests

Two assertions in `test_cache_freshness.py` had to be re-pinned because they landed exactly
on a minute boundary: an age of 120 s against a 3600 s TTL renders `58m` when computed
instantly and `57m` a fraction of a second later. Both were fixed by choosing ages that land
mid-bucket, which leaves ~50 s of slack.

A shared helper (`mid_bucket_age(ttl, minutes)`) or a regex assertion where the exact minute
is not the point would stop this recurring. Related: the box hit load average 41 during this
work and an untouched suite went 2.1 s → 20.3 s, so any timing assumption wants that much
headroom.

### 3.4 The effort catalogue is a hand-maintained model list

`subagent.py` carries `_EFFORT_CAPABLE` (with each model's `default_effort`) and
`_EFFORT_INCAPABLE` as literal dictionaries. Unknown `claude-*` models already fall back to
"capable, inherited `high`", so a new release degrades gracefully rather than breaking — but
its catalogue default will be wrong until someone edits the list. No fix suggested; noting
it so the next wrong-looking effort level is diagnosed in seconds.

## 4. Unknowns worth a look

- **GLM and other non-Anthropic providers report no TTL bucket at all** — large
  `cache_read_input_tokens`, `ephemeral_1h`/`ephemeral_5m` both zero on every turn (42/42
  turns in the session sampled). The cache cell falls back to `idle <age>` there. If z.ai
  documents a cache lifetime, a provider-keyed default would turn that into a real
  `warm`/`cold` reading — but it needs a documented number, not a guess.
- **Global-scope cache entries.** `rL({scope, ttl})` can emit `scope: "global"` alongside
  the TTL, behind beta `prompt-caching-scope-2026-01-05`. If that means a prefix shared
  across sessions or agents, it changes what "this agent's cache" even means. Unread.

## Re-verifying against a new Claude Code

Everything above was read out of the compiled binary; there is no source to grep:

```bash
strings -n 6 ~/.local/share/claude/versions/<version> > /tmp/cc.strings
python3 - <<'EOF'
import re
data = open('/tmp/cc.strings', errors='replace').read()
for m in list(re.finditer(r'function yRn\(', data))[:2]:   # the TTL resolver
    print(data[m.start():m.start()+700])
EOF
```

Useful anchors: `yRn` / `GAn` (TTL selection), `Krt` (the 1h allowlist), `pqo` (the
`prompt_cache` block), `PGo` (subagent cache eviction), `Wbt` (the subagent statusline
payload builder), `pl` (the shared session fields), `Vd` / `D2t` (agent transcript paths).
Plain regex on the minified text backtracks badly — slice by index instead of writing
`.{900}pattern`.
