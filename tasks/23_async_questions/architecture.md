# Epic 23 — Architecture

**Rev 1 · 2026-08-26.** Design detail behind [brd.md](./brd.md). Shapes and
invariants are settled here; field-by-field specification lands with the task
files.

Read [`../../architecture.md`](../../architecture.md) first — this document only
describes what changes.

---

## 1. Component map

```
agent ──mcp__questions__ask──► questions-mcp/server.py
                                   │
                                   ├─ questions_store.py ──► docs/questions/for-*.md
                                   │                          (workspace, git-tracked)
                                   ├─ roles_config.py ──────► role → token
                                   └─ RelayClient ─────────► POST /v1/messages
                                                                (never_expires,
                                                                 escalate_to)
                                            │
                                            ▼
                                    relay: messages row
                                            │  human answers, hours later
                                            ▼
                                    reaper: nudge pass · escalation pass
                                            │
   questions-listen ──GET /v1/answers?after=&wait=25──────────┘
       │ (systemd --user, one per machine)
       ├─ index lookup: message_id → workspace_id, qid
       ├─ questions_store.apply_answer()  ──► the same queue file
       ├─ PATCH /v1/messages/{id}  ──► ✅ answer
       └─ advance watermark
```

Two rules keep the split honest:

1. **The relay routes and reminds; it never learns a workspace.** No path, no
   role binding, no queue file ever reaches the server. Escalation is delivered
   to it as a *token to also send to*, not as a role to resolve.
2. **The queue file is the product; everything else is transport.** If the relay
   is unreachable, the file still gets written. If the listener dies, the file
   still gets its answer on the next connect.

---

## 2. Relay changes

### 2.1 Non-expiring messages

**Not `NULL`** (brd D11). `messages.expires_at` is `NOT NULL` (`db.py:47`), SQLite
cannot drop that constraint without rebuilding the table, and four sites parse the
column unconditionally (`app.py:514-522`, `app.py:2159-2161`, `reaper.py:316`,
`client_cli.py:163`). Instead:

`CreateMessageRequest` gains `never_expires: bool = False`. When true the server
stores `expires_at = NEVER` (`9999-12-31T00:00:00Z`) and ignores `ttl_sec`, which
stays **required and capped at 24 h exactly as today** (`models.py:25`) for every
existing sender. The expiry pass (`state='open' AND expires_at < now`) then never
matches, with no schema change, no reader audit and no index change.

Assert the consequence in a test rather than assuming it, and confirm
`messages_state_expiry (state, expires_at)` still serves ordinary rows. The
sentinel is a module constant, so a later move to real NULL is one place.

### 2.2 Per-message reminder policy

Three nullable columns on `messages`:

| Column | Meaning |
|---|---|
| `nudge_schedule_override` | this row's ladder, e.g. `4h,1d,3d,7d*`; NULL = use the chat's `recipients` config |
| `escalate_at` | absolute deadline for the escalation pass; NULL = never |
| `escalate_to_token_hash` | the installation to duplicate to when `escalate_at` fires; NULL = never |

**Accepted deviation (23-02): a fourth column, `parent_message_id`.** The three
columns above all live on the *original* row, so nothing links an escalated copy
back to its parent. Answer attribution ("answering either copy resolves both,
attributed to the original id") and sibling cancellation both need that reverse
link. Reviewed and accepted as the minimal mechanism — the alternatives were a
separate `message_escalations` table or smuggling the parent id through
`payload_json`, both more invasive. Nullable, no default, NULL for all 5006 rows
of the production snapshot, added by `ALTER TABLE ADD COLUMN` with no rebuild.

The escalation target is supplied **by the sender as a token**, hashed the same
way installation tokens already are. The relay resolves it to an installation
and chat; it never learns that "hpl escalates to operator".

**Ladder syntax gains a repeating tail.** `availability.py` parses
comma-separated active-time intervals; a trailing `*` on the last rung
(`4h,1d,3d,7d*`) means *repeat that interval indefinitely* instead of retiring
the ladder. Without it a never-expiring question goes permanently silent
(brd §2.4). Active-time arithmetic is unchanged, so windows and timezones apply
as they do today.

### 2.3 The escalation pass

A fourth reaper pass, beside the nudge pass: rows `state='open' AND escalate_at
IS NOT NULL AND escalate_at < now`. It sends a duplicate of the row's rendered
body to the escalation installation, records the child's telegram message id,
and clears `escalate_at` so it fires **once**.

Both copies stay live and either may be answered; the first answer wins and the
other is patched and cancelled — the same shape as the blocking path's
escalation, moved from the client to where it can outlive the sender.

### 2.4 The answer feed

```
GET /v1/answers?after=<message_id>&wait=N
```

Installation-authenticated. Returns answered rows belonging to this installation
with `id > after`, oldest first, each carrying `{id, answer_text, via, option_idx,
answered_at, kind}`; `204` on timeout. Parks on a waiter registry keyed by
`installation_id` (the same pattern as `GET /v1/messages/{id}/answer`, which keys
by `message_id`).

**Why a watermark and not a queue** (brd D5): the feed is a *view over existing
rows*, so it stores nothing, needs no claim, no TTL and no delivery state, and
a consumer that crashes mid-apply simply re-reads. Answers are already durable in
`messages`; adding a second durable copy would create two truths.

`after=0` is a full replay of everything this installation has ever had answered,
which is the recovery path for a lost index — bounded by a server-side page cap.

An index `messages_answer_feed (telegram_chat_id, state, id)` covers the scan.

---

## 3. The queue-file engine — `.claude/hooks/questions_store.py`

A pure library with no relay knowledge, installed through `REQUIRED_HOOKS` so
both the MCP server and the listener import the *same* module — the discipline
epic 22 uses for `permission_state_store.py`. It is the only component that
knows what a question file looks like.

### 3.1 Anchor resolution

```
anchor = "repo"      → git rev-parse --path-format=absolute --git-common-dir
                       → strip trailing /.git → primary worktree root
                       → if that root has no working tree (bare), fall through
anchor = "worktree"  → the cwd's worktree root
anchor = "path"      → the `path` key (absolute, ~ expanded); required with it
not a git repo       → nearest .claude/.git ancestor of cwd, else cwd
```

Resolution runs **at ask time and again at apply time**. The index stores
`workspace_id` (from `roles.toml`, which the reference workspace already sets
explicitly so its machine bindings survive a rename) plus the anchor mode and a
path *relative* to the resolved root — so a moved or re-cloned checkout still
receives its answers. `anchor = "worktree"` can only use the stored absolute
path, which is precisely why it is not the default.

### 3.2 The parse contract

Deliberately **not** a format string for the whole heading. Matching
`## Q-001 — MVP auth model  [resolved]  [blocks: phase-0.2/brd]` in full is where
a template-driven parser turns brittle across projects.

**This is a contract a workspace adopts, not a parser that retrofits one.**
Adoption is out of scope for this epic and happens in each workspace
individually (see state.md, Phase 0). The compatibility bar is *migratable*
compatibility: an existing queue must be **editable into conformance without
information loss** — not necessarily conformant as it stands.

The contract, every part overridable in `[questions.format]`:

1. **Entry** — a heading at the configured level (default `##`) whose text
   **begins** with a well-formed id token. Not "contains the id somewhere".
2. **Id** — default `Q-<digits>`, optional short alpha suffix (`Q-200-ds`).
   Allocation is `max + 1` across the queue set **plus `answered/`**, never
   first-free: a gap may belong to an entry archived elsewhere or living on a
   branch this checkout cannot see.
3. **Status** — a bracketed token on the entry heading whose **first word** is
   from the configured status set; any free text after it is preserved and
   ignored (`[resolved 2026-08-13]` is a valid `resolved`). Default set is
   `open` → `resolved`; a workspace declares its own vocabulary rather than
   rewriting its files.
4. **Boundary** — the answer block is inserted before the next heading of the
   same-or-higher level, else at EOF.
5. **Ambiguity refuses.** More than one entry heading for an id in the target
   file returns `conflict`; zero returns `not_found`. Both are normal outcomes
   that route to `pending` and surface in `pending_answers`. The store never
   guesses which of two candidates a human meant.
6. **Non-conforming headings are ignored, never repaired.** A heading that does
   not begin with a well-formed id is not an entry, and the store leaves it
   exactly as it found it.

#### Why each rule — evidence from the reference workspace

Scanned read-only over 290 live entries (`agents_output/23-03_phase0_anchor_scan.md`);
those files were not modified and are not this epic's to modify.

| Observed | Rule it justifies |
|---|---|
| 5 headings mention another entry's id (`## Q-388 — …after Q-253`, `[SUPERSEDED by Q-269]`, `(revives Q-173)`) | **Rule 1.** "Contains the id" would locate a *different question's* entry and flip its status. Cross-referencing ids in titles is normal authoring, not a defect. |
| 25 entries use `[answered]`; others `[closed — superseded]`, `[resolved 2026-08-13]`, `[resolved · design-addressed @v14, pin-pending]` | **Rule 3.** Real vocabularies outgrow a pair and decorate tokens with dates and notes. Declaring the set beats editing 33 headings; the free-text suffix keeps the note. |
| `Q-200-ds` beside `Q-200` | **Rule 2.** Adopters use id suffixes; the id pattern is configurable. |
| Three `## Q-NNN — <short title>  [open]` schema examples live inside the queues | **Rule 6.** A malformed id is not an entry — otherwise `max_open` counts 7 open entries where 5 exist. |
| `## ✅ ANSWER (HTL …) — … Q-265 is CLOSED.` (h2), `### Q-450 item 4 — MOVED` (h3) | **Rules 1 and 6.** Neither begins with an id at the entry level; both are correctly invisible. |
| `## Q-291 — ANSWERED: …` duplicating `## Q-291 — …` | **Rule 5.** A genuine ambiguity a machine must refuse rather than resolve. |

#### Measured adoption cost

Against the reference workspace, with its vocabulary declared in
`[questions.format].status` and no other configuration:

- **290 of 290** entries recognised;
- **0** edits for status vocabulary — declaring beats rewriting;
- **0** edits for the five id-mention collisions — rule 1 dissolves them;
- **0** edits for the five non-entry headings — rule 6 ignores them;
- **2** edits total, both the duplicated `## Q-29x — ANSWERED:` summary headings,
  each fixable by demoting the heading or dropping its leading id — **lossless**.

**2 edits across 290 entries: 99.3% of the file untouched.** That is the
migratable-compatibility bar being met, and it is the number to re-measure if
the contract ever changes.

### 3.3 Writes

Every mutation takes `flock` on `<dir>/.lock` and writes tmp + `os.replace` — the
discipline of `permission_state_store.py`. The lock covers
[scan for ids → allocate → compose → append], which is what makes id allocation
safe under parallelism (brd §2.7). Id scanning spans the configured `glob` **plus
`answered/`**, so an archived entry can never have its id reissued.

Composition is frame-only (brd D3): heading, id, status token, tags, the caller's
`body` verbatim, rendered options, `**Routed to:** @alias`, and later the
`**Dispatched:**` marker.

### 3.4 Answer escaping — an invariant, not a nicety

Answer text is human free-form and arrives from Telegram. Before insertion it is
escaped so it cannot forge structure: no line may begin with `#`, with the
configured status tokens, or with a `**Answered by:**`-shaped field. The
mechanism is a fenced, indented block plus a per-line guard; the requirement is
that **no possible answer text changes how any later parse of that file
resolves** (invariant 11, brd §8). This is both the corruption fix and the
injection bound.

### 3.5 `apply_answer`

```
apply_answer(qid, answer_text, answered_by, message_id) → applied | not_found | conflict
```

Idempotent on `message_id`: if an answer block already cites it, the call is a
no-op success. Flips the status token, inserts the rendered `answer_template`,
and never moves or deletes anything (archival is a workspace action).

---

## 4. The MCP server — `questions-mcp/server.py`

The `context-mcp` pattern, which epic 22's `permissions-mcp` also follows: a uv
single-file script (`# /// script`, `mcp>=2,<3`), `MCPServer("questions", …)`,
registered user-scoped in place by the installer, importing `.claude/hooks` off
the checkout.

Identity and config are resolved **per call**, not at startup: an MCP server is
long-lived and may serve a session whose cwd differs from the server's, so
`CLAUDE_PROJECT_DIR`/`PWD` is read per invocation and the workspace's
`roles.toml` is loaded through the existing loader with a short TTL cache.

`ask` sequence (brd §6.1) — note the ordering guarantee: **the file write is
committed and fsynced before the first relay call**, so no crash between the two
can produce a message referencing an entry that does not exist. The reverse
(entry exists, message never sent) is the acceptable failure and is reported.

Escalation resolution happens **here**, in the client, where role bindings live:
the server resolves the escalation role to a token via `roles_config` and passes
that token to the relay (§2.2).

---

## 5. The listener — `.claude/bin/questions-listen`

A separate small binary rather than an `amux-spawn` subcommand: `amux-spawn` owns
spawning, this owns answers, and this epic has no dependency on epic 16 (brd D5).
Logic lives in `.claude/hooks/questions_listen_lib.py`; the binary is process
lifecycle only.

Three files beside the existing hook stores:

| File | Purpose |
|---|---|
| `~/.claude/async_questions.json` | the routing index + watermark + pending applies |
| `~/.claude/questions-listen.lock` | single instance (`flock`) |
| `~/.claude/questions-listen.status.json` | refreshed by the loop so `--status`, which cannot hold the lock, has something truthful to read |

**23-05 owns the index** — its shape, its lock protocol and its reader/writer
helpers live in `questions_listen_lib.py`; 23-04 imports them and never
hand-parses the file (invariant 5 applies to the index as much as to a queue).

Index shape:

```json
{ "watermark": 4192,
  "messages": { "4187": { "workspace_id": "hyppie-flow", "anchor": "repo",
                          "root": "/data/sync/work/natasha/hyppie-flow",
                          "rel_path": "docs/questions/for-product-lead.md",
                          "qid": "Q-042", "role": "hpl",
                          "created_at": "2026-08-26T14:02:11Z" } },
  "pending": [ { "message_id": 4187, "answer": "…", "attempts": 3,
                 "first_failed_at": "…", "last_error": "id not found" } ] }
```

An entry with **no `qid`** is an ack-notification: finalize the Telegram message,
write no file (brd D7).

### 5.1 Loop

Long-poll `GET /v1/answers?after=<watermark>&wait=25`; jittered backoff on
network error; `401` is fatal-with-slow-retry and surfaced in `--status`.
Laptops sleep, so a dropped connection is the normal case, not an error worth
logging loudly.

Per answer: resolve → apply → PATCH → **then** advance the watermark. A failed
apply moves to `pending`, the watermark still advances past it (it is now tracked
elsewhere), and a retry pass re-attempts every `pending` entry on each wake with
backoff. After a configurable number of attempts the answer is written to
`<dir>/answered/<qid>.md` with a note, and dropped from `pending` — visible,
never silently lost (brd D10).

### 5.2 Systemd

```ini
# ~/.config/systemd/user/claude-questions-listen.service
[Unit]
Description=Claude async-question answer listener
After=network-online.target

[Service]
ExecStart=%h/.local/bin/questions-listen
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Installed by `install-claude-config.sh`, enabled when
`~/.config/claude-tg-relay/config.toml` carries `[questions_listen] enabled = true`
— the same file and opt-in shape epic 16 gives its listener;
`loginctl enable-linger` so it survives logout.

---

## 6. Diagnostics — `shell/claude-questions`

Mirrors `claude-roles`: for the current workspace, print the resolved anchor and
its root, the role→queue map with each file's existence and open-entry count, the
allocated-id high-water mark, the listener's connection state and watermark, and
any pending applies with their last error. Never prints token material.

---

## 7. Timeouts and limits

| Thing | Value | Why |
|---|---|---|
| Answer-feed long poll | 25 s | matches the existing answer long-poll chunk |
| Async message TTL | none | brd §2.4 — the whole point |
| Default nudge ladder | `4h,1d,3d,7d*` | slower than the interactive ladder; repeats forever |
| Default `escalate_after` | 24 h | a day of silence is a real signal; 30 m is not, for async |
| Pending-apply retries | ~10, backing off to hourly | then the sidecar fallback |
| Feed page cap | 500 rows | bounds an `after=0` full replay |

---

## 8. Testing

Existing conventions — `tests/run_all_tests.py`, `FakeTelegramBackend`, patched
`RelayClient`, no network:

- **Store:** anchor resolution across git/worktree/bare/non-git; id allocation
  under concurrent writers; the three parse anchors against **fixtures copied
  from the reference workspace's real entries**; `apply_answer` idempotency,
  not-found, and status-flip; atomic replace; corrupt-file degradation.
- **Relay:** a `never_expires` row survives a reaper tick; repeating-tail ladder
  arithmetic incl. availability windows; escalation fires exactly once; the feed's
  watermark ordering, page cap, and installation scoping.
- **MCP:** role refusal before write; write-then-send ordering; `dispatched:false`
  on relay failure with the entry still present; `notify` with and without ack.
- **Listener:** watermark advance only after apply; crash between apply and PATCH
  replays without doubling; pending retry and sidecar fallback; single-instance
  lock.
- **Regression floor:** with the listener stopped and no `[questions]` section,
  every existing flow produces byte-identical relay calls.

Live gates (brd §7) need a real relay, a real bot, a real multi-day wait and a
machine that actually sleeps — task 23-07.
