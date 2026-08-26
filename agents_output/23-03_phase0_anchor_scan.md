# 23-03 Phase 0 — parse-contract scan against the real reference files

**Run by:** implementation manager · **Date:** 2026-08-26 · **Read-only.**
Files scanned: `/data/sync/work/natasha/hyppie-flow/docs/questions/{for-designer,
for-product-lead,for-tech-lead}.md` (~917 KB). `answered/` is **empty**.
No file was modified.

## What this scan is for

**Not a compatibility audit.** Epic 23 defines a *contract* that a workspace
adopts; adoption is out of scope and happens per-workspace. hyppie-flow is
evidence about what real queue files look like, and it is **not this epic's to
modify**. The bar it has to clear is *migratable* compatibility — editable into
conformance **without information loss**.

Read that way, every "failure" below is an observation that shapes a contract
rule, not a defect to absorb. The conformance measurement is at the end.

## Note on the "279 entries" figure

brd §1 and architecture §3.2 cite 279 entries. That was exactly right on
2026-08-18 (commit `7734bdde`); the queue is live and gained 11 entries in the
two days after. It is **290** today by the same rule. The figure is motivation,
not a contract — but the anchor claim attached to it was wrong at either count.

## Anchor 1 — "the first heading line containing the id as a word"

**Fails for 5 ids**, each because another question's heading mentions the id
*before* the id's own entry exists:

| id | first heading that matches is actually… |
|---|---|
| Q-102 | `## Q-111 — …iter-007 question sweep…` |
| Q-253 | `## Q-388 — Delete-account modal: …after Q-253` |
| Q-269 | `## Q-142 — …  [SUPERSEDED by Q-269]` |
| Q-271 | `## Q-212 — …  [superseded — merged into Q-271 …]` |
| Q-173 | `## Q-262 — …(revives Q-173)` |

This is the dangerous one: `apply_answer` locating by this rule would flip the
status token of a **different question's** entry and insert the answer under it.
Silent corruption of a live artifact.

**6 ids carry more than one entry-shaped heading**, including a suffixed-id
convention (`## Q-200-ds` beside `## Q-200`, likewise `Q-268-ds`) — note `\bQ-200\b`
still matches `Q-200-ds` because `-` is a non-word character — and a
second-entry-for-same-id pattern (`## Q-291 — ANSWERED: … [resolved]` after
`## Q-291 — …`).

## Anchor 2 — "a bracket token on that heading line, from the configured pair"

**Fails for 35 of 295.** The real status vocabulary is much wider than
`{open, resolved}`:

| count | first-bracket value |
|---|---|
| 246 | `resolved` |
| **25** | **`answered`** ← a third token, not in the pair |
| 5 | `open` |
| 4 | *(empty first bracket)* |
| 2 | `closed — superseded` |
| 1 | `closed — deferred to backlog` |
| 1 | `SUPERSEDED by Q-269` |
| 1 | `SUPERSEDED by Q-268` |
| 1 | `superseded — merged into Q-271 on 2026-08-09; closed 2026-08-12` |
| 1 | `resolved · design-addressed @v14, pin-pending` |
| 1 | `resolved 2026-08-13` |
| 1 | `resolved — model ADOPTED 2026-08-11` |
| 1 | `resolved 2026-06-26` |
| 2 | *(no bracket at all)* |
| 1 each | `D1`, `D5`, `role="tablist"` |

Three separate problems:

1. **`answered` is a third status token** in live use (25 entries).
2. **Status tokens carry free-text suffixes** — dates, notes, `SUPERSEDED by Q-nnn`.
   An exact-match `[resolved]` parser fails the 4 decorated ones too; they only
   passed this scan because it matched on the first word.
3. **The status is not always the first bracket** — `D1`, `D5`,
   `role="tablist"` are first brackets on headings whose status lives elsewhere
   or nowhere.

## Anchor 3 — boundary

No failures found, but two shapes to know about: a level-2 heading that is *not*
an entry (`## ✅ ANSWER (HTL, 2026-08-19, PM tick 58) — … Q-265 is CLOSED.`) and
a level-3 heading inside an entry (`### Q-450 item 4 — MOVED …`). Both behave
correctly as boundaries; the first is also an anchor-1 collision source.

## Template placeholders live in the files

Three heading lines are schema examples, not entries:

```
for-designer.md:22   ## Q-NNN — <short title>  [open]  [blocks: iter-NNN/task-NNN-MMM]
for-tech-lead.md:13  ## Q-NNN — <short title>  [open]  [blocks: iter-NNN/task-NNN-MMM]
for-tech-lead.md:25  ## Q-NNN — <short title>  [resolved]  [blocks: iter-NNN/task-NNN-MMM]
```

Two of them say `[open]`, so a naive open-entry count reports 7 rather than the
real 5, and `max_open` (brd D12) would be computed against a wrong number.

## Ids

- 285 distinct ids, min 1, **high-water mark 471** → next allocation is **Q-472**.
- **186 gaps** inside the range. Gaps must NOT be back-filled: an id may belong to
  an entry archived elsewhere or living on a branch this checkout cannot see.
  Allocation must be `max + 1`, never first-free.
- `answered/` is empty here, so it contributed nothing — but the requirement to
  scan it stands, since it is empty only in this snapshot.

## A non-queue file shares the directory

`docs/questions/hue-label-proposal.md` (22 KB) is not a queue. A default `glob`
of `*.md` would scan it; the default must be narrower (e.g. `for-*.md`) or the
queue set must come from `[questions.queue]` alone.

## What this means

Most failures are in **legacy** entries our tool would never touch — brd §9 puts
"authoring entries by any means other than `ask`" and "dispatching a hand-written
entry" out of scope, so `apply_answer` only ever answers entries this tool wrote
in its own frame. Under that reading the contract needs to be *correct for our
own entries* and merely *non-destructive* for everything else.

But two findings bite regardless of that narrowing:

1. **Anchor 1's collision is a corruption risk** and must be tightened
   (id must appear at the **start of the heading text**, not anywhere in it).
2. **Id allocation must read every shape** — including `Q-200-ds` and the
   template `Q-NNN` — to compute a correct high-water mark without reissuing.


---

# Conformance measurement against the adopted contract

Re-run after the contract was tightened (architecture §3.2 rules 1–6), with
hyppie-flow's own vocabulary declared in `[questions.format].status`
(`open`, `resolved`, `answered`, `closed`, `superseded`) and nothing else
configured:

```
entries recognised            : 290
edits required — no status    : 0
edits required — duplicate id : 2   (Q-291, Q-292)

TOTAL EDITS TO ADOPT          : 2 of 290 entries
                              = 99.3% of the file untouched
```

What each contract rule bought, measured:

| Rule | Edits avoided |
|---|---|
| 1 — id at the **start** of the heading | 5 (the id-mention collisions dissolve; `## Q-388 — …after Q-253` is simply Q-388's entry) |
| 3 — status set declared, free-text suffix preserved | 33 (25 × `[answered]`, 6 closed/superseded variants, 5 date-decorated `resolved` — the last 5 need no declaration at all, just the suffix rule) |
| 6 — malformed id is not an entry | 5 (3 × `Q-NNN` template, the `## ✅ ANSWER …` heading, the `### Q-450 item 4` sub-heading) |
| 5 — ambiguity returns `conflict` | 2 remain as genuine edits, but are never *silently* mishandled |

The two remaining edits are the duplicated `## Q-291 — ANSWERED: …` /
`## Q-292 — ANSWERED: …` summary headings. Either demote them to `###` or drop
the leading id token; the heading text and all body content survive, so the edit
is lossless. **Adoption is the workspace's own action, not this epic's.**
