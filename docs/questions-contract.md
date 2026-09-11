# The async-question queue-file contract

**Audience:** a workspace maintainer in any repository who wants to adopt the
async question system.  This document tells you what the contract is, how to
check conformance, and how to fix findings — without requiring you to read the
epic's design documents.

---

## Background in one paragraph

When an agent calls `mcp__questions__ask`, the tool writes a Markdown heading
into a queue file in your repository and sends a Telegram message.  When the
human answers, the listener (`questions-listen`) finds the entry by its id and
writes the answer back — with no session running, no AI in the loop.  For that
to work, the tool and the listener must agree on what an "entry" looks like.
That agreement is the contract below.

---

## The six rules

The contract is deliberately a *minimal* shape, not a full format string.
Matching `## Q-001 — MVP auth model  [resolved]  [blocks: phase-0.2/brd]` in
full is where a template-driven parser turns brittle across projects.  These
six rules are what the parser actually checks.

### Rule 1 — Entry

A heading at the configured level (default `##`) whose text **begins** with a
well-formed id token.  Not "contains the id somewhere".

> **Why:** Five headings in the reference workspace mention another entry's id
> *before* the entry's own heading (`## Q-388 — …after Q-253`,
> `[SUPERSEDED by Q-269]`, `(revives Q-173)`).  "Contains the id" would flip
> a **different question's** status token — silent corruption of a live
> artifact.

A heading that fails this rule is not an entry and is left exactly as found
(rule 6).

### Rule 2 — Id

Default shape: `Q-<digits>` with an optional short alpha suffix (`Q-200-ds`).
The full shape is configurable in `[questions.format].id`.

Allocation is `max + 1` across the queue set **plus `answered/`**, never
first-free: a gap may belong to an entry archived elsewhere or living on a
branch this checkout cannot see.

> **Why:** `Q-200-ds` appears beside `Q-200` in the reference workspace.
> Adopters use id suffixes; the shape is configurable.

### Rule 3 — Status

A bracketed token on the entry heading whose **first word** is in the
configured status set; any free text after it is preserved verbatim on rewrite.

`[resolved 2026-08-13]` → `resolved` ✓  
`[answered]` → `answered` (if declared) ✓  
`[SUPERSEDED by Q-269]` → `SUPERSEDED` (if declared) ✓  

The token is not necessarily the *first* bracket on the line.  If the first
bracket is `[D1]` or `[role="tablist"]` and `D1` is not in the configured set,
the parser keeps looking.

> **Why:** The reference workspace uses `answered`, `closed — superseded`,
> `resolved 2026-08-13`, `resolved · design-addressed @v14, pin-pending`, and
> others.  Declaring the vocabulary beats rewriting 33 headings; the free-text
> suffix keeps the date or note.

### Rule 4 — Boundary

An entry ends at the next heading of the same-or-higher level, else at EOF.
The answer block is inserted before this boundary.

### Rule 5 — Ambiguity refuses

More than one entry heading for an id in the target file returns `conflict`;
zero returns `not_found`.  Both are normal outcomes that route to `pending` and
surface in `pending_answers`.  The store never guesses which of two candidates
a human meant.

### Rule 6 — Non-conforming headings are ignored, never repaired

A heading that does not begin with a well-formed id is not an entry, and the
store leaves it exactly as it found it.

> **Why:** Schema-example headings (`## Q-NNN — Example  [open]`) and
> human-written summaries (`## ✅ ANSWER (HTL …) — Q-265 is CLOSED`) are
> common.  Ignoring them is what makes `max_open` count 5 open entries, not 7.

---

## `[questions.format]` overrides

Declare your workspace's vocabulary in `.claude/roles.toml` rather than
rewriting files.

### Status vocabulary

```toml
[questions.format]
status = ["open", "resolved", "answered", "closed"]
```

The **first** token is what a freshly composed entry carries.
The **second** is what `apply_answer` flips to when the human answers.
Additional tokens are recognised (rule 3) but never written by the store.

**Worked example — the reference workspace:**

The reference workspace (`hyppie-flow`) uses `answered` as a live token for 25
entries and decorates `resolved` with dates and notes.  Adding one line:

```toml
status = ["open", "resolved", "answered"]
```

makes all 290 entries conformant with zero heading edits.

### Id shape

```toml
[questions.format]
id = "Q-{n:03d}"    # default: Q-001, Q-002, …, Q-200-ds
```

Change the prefix or padding to match your workspace's convention.

### Heading template

```toml
[questions.format]
heading = "## {id} — {title}  [{status}]{tags}"
```

The `[{status}]` brackets are required — rule 3 reads a bracketed token.

### Glob for extra files

```toml
[questions.format]
glob = "for-*.md"   # default; the tool also uses [questions.queue] files
```

---

## `claude-questions --check-contract`

Run from the workspace directory after adopting:

```
claude-questions --check-contract
```

Reports every heading that does not conform and what to do about it.
Never modifies any file.

### Categories reported

| Category | Meaning | Fix |
|---|---|---|
| `MALFORMED ID` | Heading at entry level, starts with the id prefix, but fails the id parse — looks like a template example (`Q-NNN`) | Move to a fenced block if it is documentation; fix the id spelling if it is a typo |
| `MISSING STATUS` | Entry heading with no token from the configured set | Declare the token in `[questions.format].status`, or edit the heading |
| `DUPLICATE` | Two entry headings with the same id in one file | Demote or de-id one heading |
| `INFO (no edit needed)` | Id appears as a mention inside another entry's title | Nothing — rule 1 already handles this correctly |

The tool exits non-zero if any of the first three categories are found.
It exits 0 on a conforming workspace, and also 0 when there is no `[questions]`
section (invariant 9: clean no-op for workspaces that have not adopted).

### Summary line

```
288 of 290 entries conform; 2 edits needed
```

### Example output

```
workspace:  /data/sync/work/myproject
queue dir:  /data/sync/work/myproject/docs/questions
checking 3 file(s):
  for-product-lead.md  [exists, 5 open]
  for-tech-lead.md     [exists, 3 open]
  for-designer.md      [exists, 0 open]

--- for-product-lead.md ---
  DUPLICATE  line 52: Q-291
    → duplicate id in for-product-lead.md — demote or de-id one heading to resolve the conflict
  DUPLICATE  line 67: Q-291
    → duplicate id in for-product-lead.md — demote or de-id one heading to resolve the conflict

288 of 290 entries conform; 2 edits needed
```

---

## Measured adoption cost — the reference workspace

Scanned read-only over 290 live entries
(`agents_output/23-03_phase0_anchor_scan.md`); those files were not modified
and are not this epic's to modify.

- **290 of 290** entries recognised;
- **0** edits for status vocabulary — declaring `answered` in
  `[questions.format].status` beats rewriting 25 headings;
- **0** edits for the five id-mention collisions — rule 1 dissolves them;
- **0** edits for the five non-entry headings — rule 6 ignores them;
- **2** edits total, both duplicated `## Q-29x — ANSWERED:` summary headings,
  each fixable by demoting the heading or dropping its leading id — **lossless**.

**2 edits across 290 entries: 99.3% of the file untouched.**

---

## Adoption walkthrough — greenfield workspace

A brand-new workspace with a single `questions.md` file and no roles:

### 1. Create the queue file

```
mkdir -p docs/questions
touch docs/questions/questions.md
git add docs/questions/questions.md
```

### 2. Add four lines to `.claude/roles.toml`

```toml
[questions]
dir    = "docs/questions"
anchor = "repo"
file   = "questions.md"
```

That is the brd §5 adoption test: four lines and a working `ask`.

### 3. Run the installer

```
./install.sh
```

This registers the `questions` MCP server in `~/.claude.json` and installs the
listener binary.  The hook library (`questions_store.py`,
`questions_listen_lib.py`) and the MCP server are installed in the same pass —
the installer guarantees this so no partial-install window can arise.

### 4. Verify

```
claude-questions             # show anchor, queue file, listener state
claude-questions --check-contract   # confirm 0 edits needed
```

### 5. Enable the listener (optional but recommended)

```bash
./install.sh enable questions-listen
```

This enables the systemd unit and runs `loginctl enable-linger` so it survives
logout.

```
systemctl --user start claude-questions-listen
```

---

## "A question was answered and nothing happened"

This is the most common support question.  The answer is always one of:

1. **Listener not running.**  Run `claude-questions --status`.  If `running: no`
   and `enabled: yes`, start it: `systemctl --user start claude-questions-listen`.

2. **Pending apply.**  `claude-questions --status` shows a `pending` list.  Each
   item has its last error.  Common causes: a rebase was in progress, the queue
   file was on a deleted worktree branch.  Resolve the underlying issue and the
   next listener poll retries.

3. **Missing index record** (rare).  Happens when 23-04's index write fails after
   retries (the `ask` return value has `index_routing_failed: True`).  Run:
   ```
   claude-questions --reindex
   questions-listen --once
   ```
   `--reindex` scans the queue files for `**Dispatched:** … relay #N` markers
   and rebuilds any missing index entries.  The listener then picks up the
   pending answer on its next poll.  Applies are idempotent so re-running is
   safe.
