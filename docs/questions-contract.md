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

## What the tools write into an entry

The six rules say what the parser *reads*.  This section says what the tools
*write*, line by line, so a script can be built against it.  Everything here is
pinned by `tests/test_unit_questions_store.py` (`TestDocumentedEntryShape`).
It describes the default `heading` and `answer_template`; a workspace that
overrides either gets what it asked for instead.

### The entry as `ask` composes it

```text
## Q-593 — Red gate on main  [open]  [rung-0]  [red-gate]
                                                          (blank)
<the body, exactly as given>
                                                          (blank)
**Options:**                                              only with options
1. Hotfix now
2. Wait
                                                          (blank)
**Routed to:** @htl
                                                          (blank)
**Dispatched:** 2026-11-15T15:02:02Z · relay #8826        added once sent
```

- The frame is the heading, `**Options:**` with a numbered list, `**Routed
  to:**` and `**Dispatched:**`.  Nothing else.
- **The title** is made one line (line breaks become a space, control
  characters are dropped) and `[` `]` are written as `(` `)`.
- **Each option** is made one line the same way; brackets are kept.
- **The body is written as given and is not escaped.**  A body line that looks
  like an entry heading starts a new entry; a body line that looks like a
  marker is read as one.  Whoever calls `ask` owns that; nothing a human types
  in Telegram reaches the file unescaped (see the answer block).
- `**Dispatched:**` carries a UTC time to the second with `Z`.  It is absent on
  an entry whose send failed.  Re-sending replaces the line, it never adds one.

### Tags

Each tag is written after the status bracket as two spaces and one bracket:

```text
ask(tags=["rung-0", "red gate: e2e"])  →  …  [open]  [rung-0]  [red gate: e2e]
```

- Order is the order given.  A tag that is empty after cleaning is left out.
- **Allowed characters: anything.**  The tool does not validate a tag.  It
  cleans it the way it cleans the title: line breaks become a space, control
  characters are dropped, leading and trailing space is trimmed, and `[` `]`
  become `(` `)`.  Spaces, colons and punctuation stay.  There is no length
  limit.
- **A tag is not checked against the status words.**  `tags=["resolved"]`
  writes `[open]  [resolved]`.  Rule 3 takes the *leftmost* bracket whose first
  word is a status word, and in a heading the tool wrote that is always the
  real one: it comes before every tag, and the title cannot contain a bracket.
  So a tag is never mistaken for the status on a tool-written heading.  It
  *is* mistaken for it if a human later deletes or misspells the status
  bracket, or if a custom `heading` puts `{tags}` before `[{status}]`.
  "First word" is the leading run of letters, digits and `_`, so
  `[open-items]` and `[resolved: yes]` both count.  The safe habit is not to
  begin a tag with a status word.
- To read tags back: every bracket to the right of the status bracket.

### The answer block

Written by the listener at the end of the entry, after one blank line.  The heading's status word changes
from the open word to the second status word, in place; text after the word
inside the bracket, and every tag, is left alone.

```text
<!-- answer:8828 -->
**Answered by:** @htl · 2026-11-15T15:14:39.120394+00:00 · relay #8828
                                                          (blank)
```text
    1. Hotfix now
```
```

Line by line:

1. `<!-- answer:N -->`.  `N` is the relay message id.  Always written, whatever
   `answer_template` says, and written once per `N`: applying the same answer
   again changes nothing.
2. `**Answered by:** <who> · <when> · relay #N`.
   - `<who>` is `@<role>`: the role the message was sent as.  It names the
     chat the question went to, not the person who answered.  It is `telegram`
     when the role was not recorded.
   - `<when>` is the relay's own timestamp, copied as given: UTC with
     microseconds and `+00:00`.  When the relay supplied none it is the
     listener's clock: UTC to the second with `Z`.  **Two shapes**; parse both.
3. A blank line.
4. The answer, in a fenced block with the info string `text`.

The answer text inside the fence:

- **A button answer** is the button's label: `N. ` and the option's text **cut
  to 64 characters** (Telegram's limit for a button).  The number is the
  reliable part; the full text is item `N` of the entry's `**Options:**` list.
- **A typed reply** is the reply's text.  It is written for a question with
  buttons too, when the human uses Telegram's Reply on the message instead of
  tapping.
- Every line is indented four spaces.  Tabs become four spaces, control
  characters are dropped, trailing empty lines are dropped.
- A line that would begin with `#`, `<!--`, `**`, a code fence (three backticks
  or `~~~`), a status bracket such as `[resolved]`, or that is only `---`,
  `===` or `___`, gets a backslash in front.
- **The fence is not always three backticks.**  It is one backtick longer than
  the longest run of backticks in the answer, and at least three.  Read the
  block from the opening fence to the next line that is exactly the same run
  of backticks.

So an answer cannot start an entry, close its own fence or forge a marker, and
the price is that the text is **not byte-for-byte** what was typed:

```text
typed:                          written:
Use this:                       `````text
```python                           Use this:
x = 1                               \```python
```                                 x = 1
## Q-999 — fake  [open]             \```
                                    \## Q-999 — fake  [open]
                                `````
```

To recover the text, drop four spaces from each line and one leading backslash
where what follows is one of the guarded openings.  That is exact except for
one case the file cannot tell apart: a typed line that itself began with a
backslash followed by a guarded opening.

#### A late answer

An answer that arrives when the entry is no longer open (withdrawn, or given
another status by hand) is recorded, and the heading is not touched.  One line
and a blank go between the marker and `**Answered by:**`:

```text
<!-- answer:8826 -->
**Late answer** — recorded after this entry was marked [withdrawn]; status unchanged.
                                                          (blank)
**Answered by:** @htl · 2026-11-15T15:06:49.560465+00:00 · relay #8826
                                                          (blank)
```text
    Yellow marshmallow
```
```

The bracketed word is the status the entry carried, in lower case.

### The withdrawal block

Written by `withdraw` at the end of the entry, after one blank line.  The
heading's status word changes from the open word to the withdrawn word.

```text
<!-- withdrawn -->
**Withdrawn:** 2026-11-15T15:02:09Z · agent · session bf2dc39d
                                                          (blank)
```text
    The change went ahead by itself.
```
```

1. `<!-- withdrawn -->`.  This marker, not the status word, is what makes an
   entry withdrawn.
2. `**Withdrawn:** <when>` and, when known, ` · <who>`.  `<when>` is UTC to the
   second with `Z`.  `<who>` is free text: `agent · session <8 characters>` or
   `agent` from the MCP tool, `<user> (claude-questions)` from the command.
3. A blank line and the reason, fenced and escaped exactly like an answer.  The
   tools always require a reason.

A withdrawn entry can carry a late answer after this block, as above.

### What a reader may rely on

- Both markers count only at the start of a line (up to three spaces of
  indent).  Inside a fence they are indented four spaces and carry a backslash,
  so they never match.
- **Answered:** the entry contains `<!-- answer:N -->`.  An entry can contain
  more than one, with different `N`; the first that is not a late answer is
  the one that changed the status.
- **Withdrawn:** the entry contains `<!-- withdrawn -->`.
- **Answered and not yet acted on** is not something these tools record.  They
  write the marker and the block and then never touch the entry again.  A line
  such as `**Answer:**` or `**Status:**` under the block is the workspace's own
  convention, written by a person or a session when the answer is folded in.
  "Marker present, no such line after it" is a sound test exactly as far as
  the workspace keeps to its convention.
- Blocks are appended in the order things happened, each before the entry's
  boundary (rule 4).  Nothing the tools wrote is ever rewritten, except the
  status word on the heading and the `**Dispatched:**` line.

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

**An answer flips the status only from the first (open) token.**  An entry that
carries any other status when its answer arrives — withdrawn, resolved by hand,
`[SUPERSEDED by Q-269]` — keeps its heading exactly as it is; the answer is
still written under it, marked as a late answer.  The store never overwrites a
status a human set.

#### The withdrawn word

```toml
[questions.format]
withdrawn = "withdrawn"     # the default
```

`withdraw` flips an open entry to this word and inserts a `<!-- withdrawn -->`
block carrying who, when and why.  The word is **always** part of the
recognised status set, listed in `status` or not, so a withdrawn entry never
reads as "no status".  Rules: one word, and not the open word.

If your vocabulary already has a word for "closed without an answer", name it
here (`withdrawn = "closed"`) rather than adding a new one.  Whether an entry
*is* withdrawn is decided by the marker, not by the word, so hand-written
entries that use the same word are unaffected.

For an adopter this is the only new thing to check: if any existing heading
has a bracket whose first word is your withdrawn word (default `withdrawn`), it
now parses as that entry's status.

**Worked example — the reference workspace:**

The reference workspace (`hyppie-flow`) uses `answered` as a live token for 25
entries and decorates `resolved` with dates and notes.  Adding one line:

```toml
status = ["open", "resolved", "answered"]
```

makes all 290 entries conformant with zero heading edits.

### Files beside the queue

Two files that are not queue files can appear in the queue directory:

- **`claims.md`** (`[questions] claims`) — the id ledger written by `claim`.
  One list item per reserved id.  Allocation reads it; it is never parsed for
  entries and is excluded from the queue set even under a wide `glob`.  Commit
  it.
- **`.lock`** — the store's lock file, *only* outside a git repository, or left
  over from before epic 46.  In a git workspace the lock now lives in the git
  directory and nothing untracked is created in the working tree.  A leftover
  `.lock` can be deleted once every session started before the upgrade has
  ended (see `docs/async-questions.md`, "The lock file"); do not commit it.

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
