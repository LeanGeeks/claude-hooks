# Human roles — prompt guidance (adapt into CLAUDE.md)

This file is a template to copy into your workspace `CLAUDE.md` or any prompt
file and edit to match your project.  Nothing generates or validates it; keeping
it in sync with `.claude/roles.toml` is your responsibility.  That is deliberate:
the two files answer different questions and are allowed to differ.

---

## Addressing a human role with AskUserQuestion

Prefix the `header` field with `@alias` (and a space) to route the question to
a specific person.  The alias must be one of the short tags listed in
`.claude/roles.toml`.  Keep the alias you type in `header` strings to 2–4
characters — the terminal shows the header as a narrow chip and a longer tag
like `@designer` leaves little room for the actual header text.  A role may
list longer readable synonyms (e.g. `design` alongside `ux`) but always
use the shortest alias when composing a `header`.

**Worked example — routing a layout decision to the designer:**

```
AskUserQuestion({ questions: [{
  header:   "@ux Layout",
  question: "Sidebar or top nav for the settings area?",
  options:  ["Sidebar", "Top nav", "Both (responsive)"],
}]})
```

The question goes to the UX/UI designer's Telegram chat, headed
`Question — myproject · for UX/UI designer`, with the `@ux ` prefix stripped.
The person at the keyboard can still answer in the terminal — that is a feature,
not a leak.

---

## When to use each role

**@op / operator** (default)

The person running Claude Code.  All questions without an `@alias` prefix arrive
here automatically.  You only need to tag a question for a *different* role; do
not tag operator questions unless you have a specific reason.  When any other
role is unreachable, the question falls back to the operator with a note
explaining what happened.

**@ux** (synonym: `@design`) — UX/UI designer

Decisions about visual design, information architecture, interaction patterns,
and what the interface looks and feels like.  Route here when you need a
judgment call on layout, colour, copy, accessibility, or any choice that a
designer would own.  Do not route pure engineering decisions here.

**@arch** — Tech lead / architect

Technology tradeoffs and structural decisions: database choice, API shape,
library selection, cross-service contracts, performance budgets, scaling
strategy.  Route here when the call will shape the codebase for months and needs
an experienced technical perspective beyond the immediate task.

**@prod** — Product lead

Scope, priority, and business alignment: what ships in this release, what gets
cut, whether a feature is worth the cost, what the acceptance criteria are.
Route here when the answer depends on product strategy or customer commitments
rather than on technical or design expertise.

---

## Defaults and exceptions

An untagged question always goes to the operator (the default role).  Tagging is
for *exceptions*, not for every question.  Most questions belong to the operator;
only route to another role when you have a clear reason that a specific person
owns the decision.

---

## One call, one role

A single `AskUserQuestion` call must address exactly one role.  If your
questions span two roles, make two separate calls:

```
# Wrong — mixed roles in one call:
AskUserQuestion({ questions: [
  { header: "@ux Layout",  question: "..." },
  { header: "@arch Stack", question: "..." },
]})
# The call will be denied with an explanation naming both aliases.

# Right — one call per role:
AskUserQuestion({ questions: [{ header: "@ux Layout", question: "..." }]})
AskUserQuestion({ questions: [{ header: "@arch Stack", question: "..." }]})
```

The deny message tells you exactly which aliases conflicted and asks you to
split the call.  This is a technical constraint (§2.2 in the design doc): a
question group is scoped to a single Telegram chat, and a group split across two
chats can never finalise.
