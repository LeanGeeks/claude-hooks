# 15-04 — Wait phase: concurrent waits and terminal-answer propagation

**Status:** todo · **Depends on:** 15-03
**Read first:** [brd.md](./brd.md) §5.5 (terminal wins), §2.2 (why groups are chat-scoped)

## Goal

Two changes to the same code, done together because doing them apart means
writing the terminal branch twice:

1. **Convert the wait phase to one thread per open message.** 15-05 needs to
   wait on two live groups at once; the current sequential loop cannot.
2. **Carry the terminal's answers into whichever chat the question went to.**
   Today the terminal branch (`permission_request_hook.py:523`) strips keyboards
   and nothing else, because the waiting process never learns *what* was
   answered. With roles, the reader of that message is no longer the person who
   just typed the answer.

After this task the wait phase is in its final shape; 15-05 adds a second group
to it and changes nothing else.

## Part 1 — the concurrent wait loop

### Why the sequential loop has to go

`handle_ask_user_question` polls children one at a time
(`permission_request_hook.py:505`). That is correct today: the relay only
releases a grouped answer once the whole group finalizes (`app.py:1203`), so
every child of a group becomes answerable at the same instant and poll order
does not matter. It stops being correct the moment two independent groups are
live (15-05) — the loop would sit on a role child while the escalation group
completes.

### Shape

- One **daemon thread per open child message**. Each loops
  `wait_for_relay_answer(msg_id, timeout=chunk, long_poll_chunk=chunk, token=…)`
  and pushes `(group_key, msg_id, result)` onto a `queue.Queue`, where `result`
  is the answer dict, the `{"_state": …}` sentinel, or `None` on chunk timeout.
- The **main thread owns everything else**: terminal detection, state-store
  writes, and the final decision. No state-store writes from child threads. The
  store is JSONL under `flock` (`permission_state_store.py:143`) and safe across
  processes, but there is no reason to also exercise it from several threads.
- Main loop, one tick per `queue.get(timeout≈2)`, until the `REQUEST_TTL`
  deadline the function already computes:
  1. Terminal resolution on **any** child → Part 2, then `return None`.
  2. Drain whatever the queue produced. A group is **won** when every one of its
     children has an answer.
- Daemon threads are never joined. When the hook exits, threads still parked in
  a long-poll die with the process; that is the cheapest correct way to stop
  them. Anything that must happen first happens on the main thread.
- Keep the 25 s long-poll chunk cap. The queue makes wake-up latency independent
  of chunk size, so shorter chunks would only add request volume.
- A child reporting `{"_state": …}` (expired or cancelled) marks that child
  non-answerable. Every child of the only group going non-answerable → the group
  is dead → `return None`, native UI stands, exactly as today.

With one group this is behaviourally identical to the sequential loop. That
equivalence is the acceptance bar for Part 1 — the existing integration tests
must pass unchanged.

## Part 2 — terminal-answer propagation

### The source: a structured object, not prose

**Verified 2026-08-02 against a live `PostToolUse` payload** — captured from a
real session, and confirmed byte-identical to the transcript's `toolUseResult`
for the same `tool_use_id`. Do not re-derive this from the prose the model sees.

Real payloads are committed at
[`fixtures/posttool_askuserquestion.json`](./fixtures/posttool_askuserquestion.json).
**Build the unit tests from that file, not from hand-written examples.**

`tool_response` is a `dict`:

```python
{
  "questions":   [ {header, question, options, multiSelect}, ... ],
  "answers":     { "<question text>": "<answer>", ... },     # <- what we want
  "annotations": { "<question text>": {"preview"?: str, "notes"?: str}, ... },
}
```

`answers` is keyed by the exact question string, which is also how children are
identified here. Read it directly.

**A free-text answer needs no special handling** (fixture case 2): typing a
custom answer puts the text straight into `answers`, with `annotations` empty.
Only the placeholder case below is special.

**Two traps, both observed in real data:**

1. **The prose form is not parseable and must not be the primary path.** The
   string the model receives renders an un-selected option as
   `"…never get used."=(no option selected) notes: It doesn't have to be…` —
   the answer is *unquoted*. On a real 4-question call, `"([^"]*)"="([^"]*)"`
   matched **2 of 4** pairs, so an exact-match parse loses half the answers and a
   count-based positional fallback (2 ≠ 4) refuses to fire at all. Both questions
   would have rendered the generic fallback text.

2. **A selected-nothing answer is a placeholder.** When the user leaves notes
   without picking an option, `answers[q]` is literally `"(notes only)"` and the
   real content is `annotations[q]["notes"]` (fixture case 3). Patching
   `✅ (notes only)` into the designer's chat is worse than useless. When an
   answer is one of these placeholders and `annotations[q]` carries `notes`,
   render the notes instead.

### `permission_state_store.py`

Add a second field beside `role` (15-02):

```python
terminal_answers: Optional[str] = None   # JSON: {"answers": {...}, "notes": {...}}
```

Named for what it holds. It is **not** the raw `tool_response` — see the
reduction below — and calling it `tool_response` would invite someone to expect
the full payload.

`resolve_via_terminal(request_id, terminal_answers: Optional[str] = None)` writes
it **only when a value is passed**, so the hook's own
`resolve_via_terminal(sibling_id)` sweep cannot blank out what `PostToolUse`
recorded a moment earlier.

### `posttool_hook.py` — reduce before storing

Do **not** store the raw payload. The state store is append-only JSONL, re-read
in full on every hook invocation, and real payloads are large: the two captured
in `fixtures/` are **11,757 and 8,873 bytes**, almost all of it `questions`
(option descriptions) and `annotations[*].preview`. None of that is needed to
render an answer.

Reduce to the two things that are:

```python
{"answers": tool_response.get("answers", {}),
 "notes":   {q: a["notes"] for q, a in (tool_response.get("annotations") or {}).items()
             if isinstance(a, dict) and "notes" in a}}
```

The same two payloads reduce to **1,580 and 1,201 bytes**.

Then `json.dumps` and pass to `resolve_via_terminal`. Cap at 8 KB as a backstop
only — after reduction nothing realistic approaches it. An earlier draft of this
task capped the *raw* payload at 8 KB, which would have truncated both real
captures into invalid JSON and silently disabled the structured path for exactly
the rich calls it exists to serve. Do not reintroduce that.

If `tool_response` is not a dict (older Claude Code, or a shape change), store
`str(...)` of it unchanged and let the parser's prose tier deal with it.

### The terminal branch

Replacing `permission_request_hook.py:523-536`:

1. Scan **all** children's rows for a non-empty `terminal_answers` and take the
   first. `PostToolUse` only ever flips one row
   (`find_pending_request_by_tool_session` returns a single row), and it is not
   necessarily the child that detected the terminal state — the existing
   any-child detection already accounts for this.
2. `parse_terminal_answers(terminal_answers, questions) -> dict[str, str]`.
3. Per child: `finalize_message(msg_id, body, text, prefix="✅ ", token=token)`
   where `text` is the matched answer, or `"Answered in the terminal"` when
   there is none. `body` is the rendered text kept by 15-03.
4. Then the existing `resolve_via_terminal` sweep over still-pending siblings.

`finalize_message` supersedes the `remove_inline_buttons` + `set_message_reaction`
pair here. `set_message_reaction` is a no-op shim
(`telegram_permission_router.py:598`) and drops out of this path.

### `parse_terminal_answers`

Put it in `permission_request_hook.py` next to the terminal branch — it is
specific to this one Claude Code output format and does not belong in the
router.

```python
def parse_terminal_answers(stored: str | None, questions: list[str]) -> dict[str, str]
```

Three tiers, in order. Never raise: `None`, `""`, invalid JSON, or unrelated
prose all return `{}`, and any question left unmatched gets the generic text.

1. **Structured (the real path).** `json.loads` → take `answers[q]` for each
   question. Substitute `notes[q]` when it exists **and** `answers[q]` is a
   placeholder — meaning empty, or one of the literal strings `(notes only)`
   (observed in real data) and `(no option selected)` (defensive; observed only
   in the prose form). Match those exactly. Do **not** generalise to "any
   parenthesised value" — a user can legitimately type `(none of these)` as a
   free-text answer, and a heuristic would swallow it.
2. **Prose (defensive only).** If the payload is not JSON, fall back to
   `re.findall(r'"([^"]*)"="([^"]*)"', …)` with exact question-text matching.
   Keep it, because it costs five lines and covers a shape change, but do not
   add the positional-zip fallback that was in an earlier draft of this task —
   real data shows the pair count disagrees with the question count precisely
   when the parse is already wrong, so zipping would mis-assign answers to
   questions rather than degrade cleanly.
3. **Nothing matched** → `{}`.

### Degradation ladder

| Situation | The asked-for chat shows |
|---|---|
| Structured read succeeds | `✅ Sidebar` |
| Answer is a placeholder, notes present | `✅ <the notes text>` |
| Parse fails / no `terminal_answers` | `✅ Answered in the terminal` |
| PATCH fails, cancel succeeds | keyboard stripped, body unchanged — today's behaviour |
| Parked hook already gone | `posttool_hook`'s own revoke strips the keyboard (15-02), no patch |

Every rung leaves the keyboard stripped. Nothing here can be worse than the
current behaviour.

Note what is *not* possible: when the terminal wins, the hook returns `None` and
Claude Code takes the answers from its own UI, so there is no channel to annotate
them for the agent. brd §5.6 applies to escalation only.

## Implementation notes

- The thread body must swallow every exception and push a sentinel rather than
  dying silently — a thread that raises leaves the main loop waiting for a
  result that will never arrive, until the 12 h TTL.
- `handle_ask_user_question` is the only caller of this loop. Do **not** touch
  `wait_for_response` (`permission_request_hook.py:255`), which serves the
  permission path and stays sequential.
- A group finalizing server-side wakes every one of its children at once; the
  loop must not assume answers arrive one at a time.
- **`answers` can be partial.** Claude Code has an `askUserQuestionTimeout`
  setting (`60s` / `5m` / `10m`, default `never`) that auto-continues a question
  with whatever the user has selected so far. When it fires, `PostToolUse`
  arrives carrying a *subset* of the questions while the Telegram messages are
  still live. The design already handles this — an unmatched question falls to
  the generic text — but do not write code that assumes one entry per question.

## Testing

- Unit-test `parse_terminal_answers` in a new
  `tests/test_unit_terminal_answers.py`, registered in `tests/run_all_tests.py`
  as `"unit_terminal_answers": "test_unit_terminal_answers"`.
- Extend the terminal-resolution cases in
  `tests/test_integration_permission_request.py:471` for the finalize behaviour.
- Script the fake relay's `wait_for_answer` per message id so threads resolve
  deterministically. Give every test a hard timeout: a wait-loop bug otherwise
  hangs the suite for 12 h instead of failing.

## Done criteria

- [ ] With one group, the existing integration tests pass **unchanged** — the
      conversion is behaviour-preserving.
- [ ] Answers arriving for several children at once are all collected.
- [ ] A child thread that raises does not hang the main loop.
- [ ] Every child going expired/cancelled → `return None`, native UI intact.
- [ ] No state-store writes happen off the main thread.
- [ ] `parse_terminal_answers` reads the **structured** `answers` map for one and
      for several questions, matching by exact question text.
- [ ] A `(notes only)` answer with a matching `notes` entry renders the notes,
      not the placeholder; a free-text `(none of these)` is left alone.
- [ ] The prose fallback fires only when the payload is not JSON, and does **not**
      zip by position.
- [ ] `None`, `""`, invalid JSON, and unrelated prose all return `{}` without
      raising.
- [ ] A free-text answer is read straight from `answers` with no special casing.
- [ ] Tests are driven from `fixtures/posttool_askuserquestion.json`, covering
      all three real cases, not from hand-written examples.
- [ ] **The reducer is tested on the full fixture payloads**: each reduces to
      well under the 8 KB backstop and survives a JSON round-trip. Both raw
      fixtures exceed 8 KB, so a test that stores them unreduced must fail.
- [ ] Terminal win patches every message with `✅ <answer>` and then cancels it,
      in that order, through the role's token.
- [ ] Unparseable `terminal_answers` still patches `✅ Answered in the terminal`
      and cancels.
- [ ] `terminal_answers` round-trips through the JSONL row.
- [ ] A later `resolve_via_terminal(sibling_id)` with no `terminal_answers`
      leaves the stored value intact.
- [ ] With no roles configured, a terminal win behaves as before except that the
      body now carries the answer — assert the keyboard is still stripped.
