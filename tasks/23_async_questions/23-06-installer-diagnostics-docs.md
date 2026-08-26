# 23-06 — Installer, diagnostics, docs

**Status:** todo · **Depends on:** 23-04, 23-05
**Read first:** [brd.md](./brd.md) §5 · [architecture.md](./architecture.md) §5.2,
§6 · [state.md](./state.md) invariant 9 · `install-claude-config.sh` (MCP
registration and hook install patterns) · `shell/claude-roles` (the diagnostic to
mirror) · `docs/roles.example.toml`, `docs/roles-prompt-example.md`

## Goal

Make the epic installable, inspectable and adoptable by a workspace that has
never seen it.

## Scope

### 1. Installer

- Register `questions-mcp` user-scoped, in place off the checkout, following the
  existing MCP registration pattern.
- Add `questions_store.py` and `questions_listen_lib.py` to `REQUIRED_HOOKS`;
  symlink `questions-listen` into `~/.local/bin/`.
- Install `claude-questions-listen.service` and enable it when the client config
  opts in; `loginctl enable-linger`. Never enable it silently for an installation
  that has not asked.
- Idempotent re-run, like every other installer step.

### 2. Diagnostic — `shell/claude-questions`

Mirrors `claude-roles` (architecture §6): resolved anchor and root, the role→queue
map with each file's existence and open-entry count, the id high-water mark, the
listener's connection state and watermark, pending applies with last error. Never
prints token material. `--check` probes the relay for each distinct token, like
`claude-roles --check`.

This is the tool a human runs when an answer "didn't arrive" — make its output
answer that question directly.

### 3. Docs

- `docs/async-questions.md` — the operator guide: what the two tools do, the
  `[questions]` config with a worked example, the anchor modes and when to pick
  each, how answers travel, what `pending` means, and how to run the listener.
  Include the **adoption walkthrough** for a greenfield workspace: a flat
  `questions.md`, no roles, `dir = …` and nothing else (brd §5 adoption test).
- `docs/questions.example.toml` — copy-paste `[questions]` block, in the shape of
  `docs/roles.example.toml`.
- Agent-facing prose to adapt into `CLAUDE.md`, in the shape of
  `docs/roles-prompt-example.md`: when to prefer `ask` over `AskUserQuestion`,
  what belongs in `body` vs `options`, citing the returned id when halting.
- Top-level `architecture.md`: a new section for the async path, and an update to
  the `Repository layout` block. Keep it to what changes.

## Done when

- A clean install on a machine with no `[questions]` anywhere changes no
  behaviour (invariant 9), and `claude-questions` says so clearly.
- A workspace adopts by adding four lines and gets a working `ask`.
- `claude-questions` diagnoses each of: no config, unbound role, listener down,
  pending applies.

## Tests

Installer idempotency; registration present and correct; the diagnostic against
fixtures for each failure mode; a docs lint that the example TOML parses and its
keys all exist in the loader.

## Conformance checker and adoption guide (added 2026-08-26)

Adoption of the queue-file contract happens **per workspace**, by people this
epic will never meet, against files this epic must never touch. A contract that
ships without a way to check conformance gets adopted approximately. So:

### `claude-questions --check-contract [dir]`

**Flag naming — do not collide.** §2 above already defines `--check` as the
relay-token probe that mirrors `claude-roles --check`. The conformance checker is
a separate flag, `--check-contract`. Keep both; they answer different questions
("can I reach the relay?" vs "do my queue files conform?").

Reports, for the workspace's configured queue set, every heading that does **not**
conform to architecture §3.2 and what the adopter must do about it:

- headings that look like entries but whose id is malformed (the `Q-NNN`
  template-example shape) — *ignored by the store; move to a fenced block if you
  meant them as documentation*;
- entry headings with **no status token from the configured set** — *declare the
  token in `[questions.format].status`, or edit the heading*; name the unknown
  token, since declaring is almost always the lossless fix;
- **duplicate ids within one file** — *the store returns `conflict` and refuses;
  demote or de-id one heading*;
- ids that appear only as mentions inside another entry's title — informational,
  since the contract handles these with no edit.

Exit non-zero if anything in the first three categories is present. Print a
summary line of the form `N of M entries conform; K edits needed`. Never print
token material, and **never modify a file** — it is a checker, not a fixer.

### `docs/questions-contract.md`

The adopter-facing statement of the contract: the six rules, the
`[questions.format]` overrides with worked examples, what `--check` reports and
how to fix each finding, and the measured result that the reference workspace
adopts with 2 lossless edits across 290 entries. Written for someone in another
repo who has never read this epic.

Tests: `--check` against a conforming fixture (exit 0), against the deliberately
non-conforming fixture from 23-03 (exit non-zero, every category reported), and
against a workspace with no `[questions]` section (clean no-op, exit 0).

## Obligations inherited from 23-05's review (added 2026-08-27)

### `claude-questions --reindex` — closes the one hole in "never lose a decision"

23-05's review found the epic's only remaining gap in its central guarantee, and
it is real though narrow:

1. `ask` writes the entry, sends to Telegram, then writes the routing index.
2. If that index write fails after its retries, `ask` returns
   `index_routing_failed: True` — entry and message both exist, no index record
   does.
3. The listener resolves answers by `message_id → index`. An unknown id is
   skipped (correctly — the feed carries *every* answered message of the
   installation, including permission approvals, so pending them all would make
   `pending_answers` meaningless).
4. So that human's answer is **never applied**, with no pending entry and no
   sidecar. It is the one outcome brd D10 forbids.

**The data to recover it already exists.** `mark_dispatched` runs at step 7
*unconditionally* — it does not return early when the index write fails — so the
queue entry carries `**Dispatched:** <timestamp> · relay #<message_id>`. The
mapping `message_id → qid` is therefore durable **in the queue file itself**, and
the index is fully reconstructible.

Add `claude-questions --reindex`:

- scan the workspace's configured queue set for `**Dispatched:** … relay #N`
  markers;
- for every `N` with no index record, rebuild the `IndexEntry` from the entry's
  own location (`workspace_id`, anchor, root, `rel_path`, `qid`, role);
- never overwrite an existing record, never move a watermark, never touch a
  queue file — it is a repair for the index only;
- report how many records were rebuilt, and print the ids so an operator can see
  what was recovered.

Recovery is then: run `--reindex`, and the listener picks the answers up on its
next poll (or via an `after=0` replay, which is safe because applies are
idempotent on relay message id). Document this in `docs/async-questions.md` as
*the* answer to "a question was answered and nothing happened".

### Install `questions_listen_lib.py` and `questions-mcp` together

23-05's review raised two transition-window LOWs. Between 23-05 landing and the
installer running, the **installed** `questions_listen_lib.py` is still 23-04's
version, which does not know the fields 23-05 added:

- `Index.to_dict` drops `watermarks` → the listener replays from `after=0`
  (idempotent, just slow);
- `PendingApply.to_dict` drops `answered_at`, `last_attempt_at` and `token_fp` →
  a retry fires immediately, and an empty `token_fp` makes `_finalize` skip the
  Telegram PATCH, so the answer still reaches the queue file but the message is
  never ticked.

Neither loses an answer, and both close the moment the installer runs. The
requirement: **install the hook library and the MCP server in the same pass**,
and do not ship a state where one is new and the other old. Note it in the
installer so a future partial install does not reintroduce the window.
