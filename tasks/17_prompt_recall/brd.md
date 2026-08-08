# Epic 17 — Prompt recall in Telegram (templates + history)

**Status:** backlog · **Owner:** Anton · **Created:** 2026-08-02 · **Rev:** 1

> Vision transfer only. In-depth planning happens when this leaves the backlog;
> there are deliberately no task files yet.

## 1. Problem & thesis

[Epic 16](../16_telegram_spawn/brd.md) ships with free-text prompt entry only:
`/new <workspace>` force-replies and you type. That is correct as a floor and
wrong as a destination. Real seeding prompts are long, structured and repetitive
— the same "review the diff and…", "continue the epic-15 chain from…" shapes,
retyped on a phone keyboard.

**Thesis:** the machine already holds both halves of the answer.
`~/.claude/history.jsonl` carries every prompt with its `project` path
(84 entries over 2.5 months across 8 projects in the live sample; `shell/claude-history`
already parses it), and a project can carry committed starter prompts. Surface
both in the prompt step, with **three distinct invocations** — typed, from a
template, from history — and let each one be edited before it is sent.

## 2. Constraints discovered

The Telegram UI facts that bound every design here. They are the reason this is
a separate epic and not a footnote in 16.

**2.1 — A bot cannot prefill the user's compose box.** Force-reply opens an
*empty* box. There is no API to seed it with text for a normal chat message.
Everything below is a consequence of this one fact.

**2.2 — Inline mode can prefill, but cannot edit.**
`switch_inline_query_current_chat` does insert `@bot <query>` into the input and
lets the user edit *the query*. But choosing a result sends
`input_message_content` **verbatim** — the bot composed it, the user cannot amend
it. Inline mode buys live search and loses editing, which is the wrong trade for
prompts. It also forces prompt text through the relay (stored server-side, or
proxied per keystroke), against epic 16's division of knowledge.

**2.3 — `copy_text` buttons cap at 256 characters.** Bot API 7.11's
`CopyTextButton` would be the natural "copy this template" affordance, but 256
chars does not hold a real prompt. It is also a new button *type*: the relay's
keyboard model is `{label, value}` callback buttons only (`models.py:15`), so any
new type is a change to the model, the backend renderer and
`_payload_keyboard_for`.

**2.4 — Code blocks carry a copy affordance for free.** The relay already sends
HTML parse mode, and current Telegram clients render `<pre>` with a copy control
(long-press → Copy as the floor). Unlimited length, no API change, no new button
type. This is the editing path: copy → paste into the force-reply → edit → send.

**2.5 — Button labels are one short line.** A prompt preview belongs in the
message body, not on the button. Each entry is therefore *body text + a button*,
which bounds how many fit in one message (≈5–8 before it needs paging).

**2.6 — History is per-project and already structured.** Records are
`{display, project, timestamp, sessionId}`; filtering by the chosen workspace's
absolute path is exact, not fuzzy. No new capture code is needed for history —
unlike the workspace seen-store epic 16 has to introduce.

## 3. Three invocations

The distinction is the requirement, not an implementation detail: the three cases
have different costs and different failure modes, so they get different entry
points rather than one overloaded picker.

| Case | Invocation | Result |
|---|---|---|
| **Typed** | `/new <ws>` → force-reply | today's behaviour, unchanged floor |
| **Template** | `/new <ws> #<name>`, or `[📋 Templates]` on the prompt step | named starter, verbatim or copy-to-edit |
| **History** | `[🕘 Recent]` on the prompt step; `/new <ws> !!` for the last one | that workspace's recent prompts, newest first, paged |

`#` for template names, `+` already taken by profile/model modifiers (epic 16
§5.3), `@` reserved for epic 15's role aliases. A `#name` that matches no
template lists the available ones instead of guessing.

Each listed entry offers two verbs:

- **`[▶ Use]`** — send verbatim, spawn immediately. The common case for a
  template that needs no adjustment.
- **`[✏️ Edit]`** — the listener posts the full text as a `<pre>` block; copy it,
  paste into the still-open force-reply, edit, send (§2.4). Clunkier than a
  textarea, and honest about it — the textarea is [epic 18](../18_telegram_miniapp/brd.md).

## 4. Templates

Two locations, workspace first:

```
<workspace>/.claude/prompt_templates/*.md     committed, project vocabulary
~/.claude/prompt_templates/*.md               global starters
```

Filename (sans `.md`) is the name and the `#<name>` key; an optional leading
`# Heading` line is the display title; the rest of the file is the prompt.
Workspace templates shadow global ones with the same name and are listed first.
`.claude/prompt_templates/` rather than `.claude/prompts/` — there is no existing
Claude Code convention for either (checked: `commands`, `agents`, `skills`,
`hooks`, `output-styles` exist; `prompts` does not), and the longer name says
what it holds. This repo's own `docs/prompts/` is a different thing (agent role
prompts for the implementation pipeline) and must not be confused with it.

## 5. History selection

- Filter `history.jsonl` by the resolved workspace's absolute `project` path.
- Newest first, deduplicated on normalized text, dropping entries below a minimum
  length and anything that is itself a slash command.
- Cap the page at what fits one message (§2.5) with `[⌄ More]` paging.
- Preview truncated in the body; `[▶ Use]` and `[✏️ Edit]` always act on the
  **full** text.

Privacy note: previews put past prompt text into the chat. That text was already
typed by the operator and the chat is already the destination for agent output,
so this is not a new exposure — but the list is scoped to the *chosen workspace*
so an unrelated project's prompts never appear.

## 6. Success criteria

- [ ] `/new claude-hooks #review-diff` spawns with that template's text, no taps.
- [ ] `[📋 Templates]` lists workspace templates above global ones, with
      shadowing by name.
- [ ] `[🕘 Recent]` lists that workspace's distinct recent prompts, newest first,
      pages, and never shows another workspace's.
- [ ] `[✏️ Edit]` yields text that can be copied in one gesture on both mobile
      and desktop clients, pasted into the open force-reply, edited and sent.
- [ ] A workspace with no templates and no history degrades to epic 16's plain
      force-reply with no error and no empty menus.
- [ ] Nothing here reaches the relay: no template text, no history, no paths.

## 7. Out of scope

- **Inline-mode search** (§2.2) — it cannot edit, and it would move prompt text
  onto the server.
- **A real editor** — [epic 18](../18_telegram_miniapp/brd.md).
- **Template variables / placeholder substitution.** A template is a text file;
  if it needs arguments, edit after pasting.
- **Cross-machine template sync.** Workspace templates travel in git; global ones
  are a machine fact.
- **History beyond `history.jsonl`** (transcripts, session summaries).
- **Recall anywhere other than the epic-16 prompt step.** Replies to an idle
  session keep using the plain compose box.
