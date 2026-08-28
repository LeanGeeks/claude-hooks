# Epic 22 — State & orchestration

**For the implementing orchestrator.** Read this first, then [brd.md](./brd.md).
Each task file is written for a fresh-context agent and carries its own "read
first" refs, done criteria and tests. This file owns **cross-task invariants**,
**ordering**, and the **defaults that were chosen without a Phase 0**.

**No Phase 0.** The design was settled in discussion (brd §2, D1–D8). Three
defaults were chosen by recommendation rather than hard requirement and are
one-line changes if they prove wrong in practice:

1. redirect-escape requests are **human-only** (brd D3) — flipping them to
   agent-decidable is one constant in the tier check;
2. state-store retention is **30 days of terminal rows** in the hot file
   (22-05) — a number, not a design;
3. non-Bash tool requests are agent-decidable unless an ask pattern matches the
   tool name (22-03 §tier) — tightening means adding ask entries, not code.

## Tasks

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 22-01 | [Validator: deny denies, ask asks](./22-01-validator-deny-and-ask.md) | done | — | Independently shippable; changes live behavior on install. Watch H1 (no more human-rescue for deny false positives). |
| 22-02 | [External decisions reach the wait loop](./22-02-external-decisions-wait-loop.md) | done | — | Store schema (`actor_agent`, `agent` source) + relay-path loop widening + Telegram finalization. No agent-facing surface yet. Concurrency + state-store races — the manager prompt's opus-implementer rule applies. |
| 22-03 | [Permissions MCP: read + decide](./22-03-permissions-mcp.md) | done | 22-01, 22-02 | The server, registration, D5 guard, D3 tier. 22-01 defines the tier vocabulary; 22-02 makes decide effective. |
| 22-04 | [Allowlist writers + queue](./22-04-allowlist-writes-and-queue.md) | done | 22-03 | `resolve_project_key`, queue format, versioned-settings writer, `allowlist_add` + `report_parser_issue` tools. |
| 22-05 | [Daily reviewer + compaction](./22-05-daily-reviewer-and-compaction.md) | done | 22-01, 22-04 | Prompt, schedule, queue drain, installer merge, store compaction. |
| 22-06 | [Live verification](./22-06-live-verification_human.md) | blocked | all | **human** — walks brd §5 end to end with real sessions and a real Telegram chat. |

## Dependency graph

```
22-01 ──┬────────────────► 22-03 ───► 22-04 ───► 22-05
22-02 ──┘                                          │
                              all ───────────────► 22-06 (human)
```

22-01 and 22-02 are independent roots and can run in parallel.

## Recommended order

1. **22-01 first, and let it soak.** It changes live gate behavior (deny
   hard-blocks; ask prompts). A few days of `bash_manual_confirm.log` under the
   new mapping tells you whether H1 bites before any agent is allowed to decide
   anything.
2. **22-02 alongside** — inert until something writes agent decisions.
3. **22-03**, then **22-04** — the MCP surface, read-only tools first if
   splitting the landing.
4. **22-05** once a queue exists to drain.
5. **22-06** last, against installed state, not the repo copy.

## Cross-task invariants

1. **Deny is final and prompts no one.** After 22-01, no path — hook, MCP, or
   reviewer — downgrades a deny match to a prompt. (brd D1)
2. **One classifier.** The MCP's tier decision re-runs `BashPermissionValidator`
   against the row's `cwd`; it never reimplements pattern matching. (brd H5)
3. **The guard is row-shaped, not session-shaped.** Refuse
   `row.session_id == caller ∧ row.agent_id is None`; allow same-session rows
   with an `agent_id`. (brd D5)
4. **Own workspace writes, foreign workspace enqueues.** No agent ever edits,
   commits, or pushes in a checkout it is not running in. (brd D6)
5. **Every agent decision is attributed.** `actor_agent` on the row,
   `resolution_source: "agent"`, an audit-log entry, and the attribution
   patched into the Telegram message before cancel. A decision the human cannot
   later see happened is a bug.
6. **All store writers go through `permission_state_store`'s flock protocol.**
   No bespoke JSONL writers anywhere in this epic, including compaction.
   (brd H7)
7. **`resolve_project_key` is the only project-identity function.** Enqueue and
   drain must import the same helper; two implementations that disagree on a
   worktree lose proposals silently. (brd D7)
8. **Repo-settings edits name their propagation.** Any claim that a user-scope
   pattern "is live" must say whether `install-claude-config.sh`'s merge ran.
   (brd H6, and the standing rule from the installed-hooks memory)
9. **Scheduled spawns pin model and effort.** (brd H8, epic 21)

## Log

- **2026-08-26 — epic created.** Filed from the AI-flows design discussion
  (Anton + agent, this repo, 2026-08-26). Review findings and the native-
  semantics verification (deny → ask → allow order; deny overrides hooks;
  `permissions.ask` exists) are recorded in brd §1; decisions D1–D8 in brd §2.
  Option B for foreign-workspace writes (edit+commit+push+install from outside)
  was **rejected** in favor of A+C — the reviewer-mutation incident is the
  precedent for why foreign-checkout git surgery is out. Specs only; no code
  written this sitting.
- **2026-08-26 — pre-handover review pass.** Two substantive finds, both fixed
  in the specs: (1) `install-claude-config.sh` Step 5's jq replaces
  `permissions` wholesale with `{allow, deny}` — repo `ask` lists would never
  propagate and a global `ask` key is erased on every install; now 22-01 §3b
  with its own done criterion. (2) The MCP guard had no behavior for a missing
  `CLAUDE_CODE_SESSION_ID`; now fail-closed (decide refuses) in 22-03 §1.
  Recorded as accepted, not fixed: session YOLO supersedes the ask tier (a
  human grant — brd D3 note); the Whitelist button on an ask-matched request
  is a persistent no-op (22-01 §4 note); `resolve_project_key` keys a
  submodule by its own path (falls back on non-`.git` basename — no submodules
  in current workflows). Also tightened: explicit-state listing goes through a
  new `get_requests` reader in the store module rather than server-side JSONL
  parsing (22-03 §2); the cron launcher must set `PATH`/`HOME` and log stderr
  (22-05 §3); the reviewer dry-run must eliminate the reviewer's own
  permission prompts (22-05 testing). Verified by reading, no spec change
  needed: PostToolUse's pending-only sweep skips agent-resolved rows
  (`find_pending_request_by_tool_session` matches `PENDING` only), and the
  decision dict `{"action": ...}` round-trips through `_ACTION_TO_STATE` and
  `build_output_decision` unchanged.
- **2026-08-26 — 22-01 done.** Deny hard-denies (D1), `permissions.ask` loaded,
  merged and checked between deny and allow (D2), deny arm emitted in `main()`
  and the replay path, installer Step 5 jq carries `ask` (§3b), downstream
  helper now names ask-matched parts. Tests 923 → 931, all green; review PASS
  with no BLOCKER/HIGH/MEDIUM findings. **Not live in other workspaces:**
  `install-claude-config.sh` has not been re-run, so `~/.claude/hooks/` still
  carries the old mapping and user-scope `ask` patterns do not yet exist
  (invariant 8, H6). Run the installer before 22-06.
- **2026-08-26 — 22-02 done.** Store carries `actor_agent` and
  `RESOLUTION_SOURCE_AGENT`; the relay-path wait loop adopts an external
  decision only when the row is agent-sourced *and* in `{ALLOW, DENY, STOP}`
  (both halves of the gate now isolated by tests); `_finalize_agent_decision`
  patches the Telegram message with 🤖 attribution then cancels, best-effort.
  Tests 931 → 942. Review PASS; one MEDIUM (source gate untested in isolation)
  and two LOW findings fixed and re-reviewed PASS. Inert until 22-03 writes
  decisions. Decision-dict contract for 22-03: `{"action": "allow"|"deny"|"stop"}`.
- **2026-08-26 — 22-03 done, and the installer has run.** `permissions-mcp/`
  ships the server plus `permissions_mcp_lib.py`; `get_requests(states, since)`
  added to the store module (readers follow invariant 6 too);
  `append_agent_decision_reason` exposed as the sanctioned public path for the
  caller's free-text reason. Guard is row-shaped (D5), tier re-runs the real
  `BashPermissionValidator` against the row's `cwd` (invariant 2), decide fails
  closed with no `CLAUDE_CODE_SESSION_ID`. Tests 942 → 984. Review PASS, one
  LOW fixed and one LOW (stray untracked `tasks/23_async_questions/`) left
  alone as out-of-epic.
  **`./install-claude-config.sh` was re-run (user-approved, 2026-08-26 17:10).**
  So from now on invariant 8 reads differently: the installed hooks under
  `~/.claude/hooks/` now carry 22-01's deny flip and 22-02's agent path,
  `permissions` is registered in `~/.claude.json` with `CLAUDE_HOOKS_REPO`, the
  four `mcp__permissions__*` grants are in the global allowlist, and
  `permissions.ask` now exists as a key in the global settings instead of being
  erased (§3b proven end to end). Backup:
  `~/.claude/backups/settings.json.20260826_171034.bak`.
  Live criteria verified with a real `claude -p` session driving the MCP
  cross-session against a scratch store: list → decide → row carries
  `actor_agent` + `resolution_source: "agent"` + the reason.
  **H1 watch is now open** — the deny flip is live with no soak period, so
  `bash_manual_confirm.log` is the place to catch false-positive hard blocks.
- **2026-08-26 — 22-04 done.** `.claude/hooks/project_key.py` is now the only
  project-identity function (invariant 7); `.claude/hooks/settings_writer.py`
  generalizes the router's atomic writer to any of allow/ask/deny, with the
  router delegating byte-compatibly and still targeting `settings.local.json`;
  `permissions-mcp/permission_queue.py` owns queue IO (a third module the task
  did not name — reviewer judged it sensible factoring, and 22-05's drain can
  import it without the MCP lib). `allowlist_add` writes the caller's own
  `.claude/settings.json` and enqueues for every other project key;
  `report_parser_issue` always enqueues to the claude-hooks key. Refusals
  (malformed pattern, deny collision) happen before any disk effect. Tests
  984 → 1020, review PASS with two LOW notes both marked "no change needed".
  **Installer re-run (2026-08-26 17:38):** `project_key.py` and
  `settings_writer.py` are installed under `~/.claude/hooks/` and import
  cleanly there; the global allowlist now carries all six
  `mcp__permissions__*` grants. Queue root override for tests:
  `CLAUDE_PERMISSION_QUEUE_DIR`.
  **For 22-05:** import `resolve_project_key` from `.claude/hooks/project_key.py`
  and the drain helpers from `permissions-mcp/permission_queue.py` — do not
  reimplement either. This repo's key is `-data-sync-work-leangeeks-ai-claude-hooks`.
- **2026-08-26 — 22-05 done; all five engineering tasks landed.**
  `compact(max_age_days=30)` lives in the store under the same `LOCK_EX` as
  every other writer, moves only **terminal** rows to
  `permission_requests.archive.jsonl` (append across runs), never touches
  pending rows, and has a CLI entry; `bash_manual_confirm.log` rotates to a
  dated sibling over the size threshold. `docs/prompts/permission-review-daily.md`
  is the reviewer prompt (drain → judge → replay parser issues → review
  traffic incl. the H1 deny-false-positive watch → apply + installer merge →
  compact → summarize), with the constraints restated inside and a
  workspace-generic adoption note in the header. `shell/` carries the launcher
  with **model and effort pinned** (invariant 9 / H8), `--dir` passed, explicit
  `PATH`/`HOME`, and dated stderr under `temp/`.
  Commits: `092362d` (implementation), `b650a51` (the fixture dry-run's own
  output — 1 applied, 2 rejected, 1 stubbed), `f7c3ba2` (newline restore).
  Tests 1020 → 1030. Review PASS, two LOW, neither fixed:
  (1) `b650a51`'s commit body reads "No evidence_request_ids supplied" where
  the evidence is actually a replay reproduction in the same sentence —
  wording only; (2) **follow-up worth filing:** `pretool_hook.py:46` hard-codes
  `~/.claude/bash_manual_confirm.log` while the store and the MCP lib both
  honor `CLAUDE_MANUAL_CONFIRM_LOG`, so a sandboxed run cannot redirect the
  hook's writes. Pre-existing, out of this epic's scope.
  **The dry-run produced zero permission prompts of its own** — the 06:15
  unattended run will not block on Telegram for its own tooling.
- **2026-08-26 — 22-06 blocked, awaiting human evidence.** Engineering is
  complete; the epic cannot close on suite-green (precedent 19-07 / 20-06).
  Two preconditions are already met: `./install-claude-config.sh` was re-run
  twice (17:10 and 17:38; backup
  `~/.claude/backups/settings.json.20260826_171034.bak`), so the installed
  hooks, the MCP registration and all six `mcp__permissions__*` grants are
  live. **Still outstanding: the crontab line is NOT installed** (documented
  in 22-05, a human step), and §1–§7 need real sessions and a real Telegram
  chat. Nothing in this epic is behind a default-off control, so the live
  behavior changes (deny hard-blocks; ask prompts) are already in effect on
  this machine.
- **2026-08-27 — invariant 1, after task 32.** Task 31 closed the `&`
  separator hole and task 32 closed the two suppression tokens (`[[` as an
  argument, a mid-word `#`) that switched separator detection off wholesale.
  The deny-defeating shapes measured for both tasks (`echo ok & shred …`,
  `echo [[ ; shred …`, `echo ok#c ; shred …`) now verdict `deny`.

  **Do not read that as "no parser path hides a sub-command".** Two successive
  revisions of this entry claimed a version of it and both were refuted within
  the day. Round 2 found `X=1 [[ ; shred … ]]` (a `KEY=VALUE` prefix left
  command position open). Round 3 found `2>&1 [[ ; shred … ]]`, `$(true) [[ ;
  shred … ]]` and `> [[ ; shred … ]]` — a fused redirect, a lifted substitution
  and a redirect target each consume a bash word without leaving characters in
  the token buffer, so a flag maintained below `flush_current`'s early-out
  never saw them. The **491-case sweep quoted in the previous revision was
  corpus-dependent**: its bolded "zero" measured only the prefixes round 2
  happened to think of. Re-measured over a corpus generated from the
  tokenizer's own `continue`/early-return paths (49 prefix families × 16
  payloads + 17 hand-written = **801 cases**, each ground-truthed with `set -T`
  and a DEBUG trap):

  | parser | bash ran a command it never reported |
  |---|---|
  | pre-task-32 (`ca2a81a`) | 275 of 801 |
  | task 32 round 1 | 89 |
  | task 32 round 2 | 54 |
  | task 32 round 3 | 7 |
  | task 32 round 4 | **7** (same corpus; see the operator-space table below) |

  > **CORRECTION (round 5, 2026-08-28).** This 801-case corpus **cannot be
  > re-derived from the checkout** — nothing in the repo produces 801 cases, so
  > the column above is not reproducible by the next reviewer. The
  > path-derived corpus that DOES exist is
  > `_RESERVED_WORD_POSITION_PATHS × _GROUND_TRUTH_PAYLOADS` in
  > `tests/test_integration_pretool.py`, consumed by
  > `test_bash_never_runs_a_command_the_parser_did_not_report`: **60 rows × 4
  > payloads = 240** cases after round 5 (58 × 4 = 232 before it). Re-measured
  > on that corpus: **`ca2a81a` 70 hidden, round 4 15, round 5 0.** Round 4
  > could not see its own 15 because three rows carried a `cat ` prefix (which
  > tests the position after a command word, not the redirect at command start)
  > and one carried the `known_gap` exclusion. Quote the reproducible number.

  The prefix families swept: no prefix; a plain/quoted/escaped/arithmetic word;
  a keyword at command position and one in argument position; a `KEY=VALUE`
  prefix (single, multiple, substitution-valued); a fused `2>&1`/`1>&2`, a
  stacked pair, an fd close, a redirect with a target, a redirect whose target
  is the next token, an fd-prefixed redirect, a `>& FILE`, a heredoc; `$(…)`,
  backtick, `<(…)`, `>(…)`, an in-quote substitution, a glued one, a nested
  arithmetic one, and one as a redirect target; every separator and the
  newline and comment paths; a `case` arm body; and the compositions of a
  keyword, an assignment or a separator with a redirect or a substitution.

  The residual **7 are one family and one pre-existing defect, not this one**:
  a LEADING `n>&-`/`n>&m` leaks its fd words into the command
  (`2>&- printf ok` → `2 - printf ok`), so the head becomes `2`. It is
  byte-identical on `ca2a81a`, round 1 and round 2, and it fails toward `ask`
  (`2` matches no allow pattern) — it can downgrade a deny to a prompt, never
  to an allow. Pinned as it stands by
  `test_leading_fd_duplication_still_corrupts_the_head`.

  > **CORRECTION (round 4, 2026-08-27).** The sentence above is **refuted for
  > the second half of its claim**: the residual 7 were one family, but this
  > corpus also contained *two live allow-producing families it could not see*.
  > `echo hi >| [[ ; shred -u /etc/passwd ]]` and `echo hi 1>& [[ ; shred -u
  > /etc/passwd ]]` both verdicted **allow** on the round-3 parser. `>|`
  > (noclobber override) and `1>&` (bash's `&>` synonym on fd 1) were in
  > neither operator table, so each lexed as a shorter redirect plus a spurious
  > `OP` — and an `OP` reopens reserved-word position, handing the redirect's
  > TARGET to `[[`.
  >
  > The corpus was the problem, not the count. Round 3's 801 cases were
  > generated from the tokenizer's own `continue` paths, and **an enumeration
  > of the parser's paths structurally cannot contain an operator the parser
  > does not know**. Round 4 adds a second corpus derived from *bash's
  > grammar* instead — every 1–3 character string over `< > & | ; 1 2 -` under
  > the fd-word prefixes `''`, `3`, `{v}`, in two shapes: **3504 cases**,
  > ground-truthed by whether bash ran a shell function of that name (a
  > function runs only when bash treats the word as a COMMAND; as a redirect
  > target it silently creates a file instead, so the oracle needs no parsing
  > of `$BASH_COMMAND`).
  >
  > | parser | bash ran a command it never reported, of 3504 |
  > |---|---|
  > | pre-task-32 (`ca2a81a`) | 924 |
  > | task 32 round 1 | 174 |
  > | task 32 round 2 | 174 |
  > | task 32 round 3 | 21 (18 `>|` spellings, 3 `1>&`) |
  > | task 32 round 4 | **0** |
  >
  > Round 4's fix is the axis, not the two operators: `_check_operator`'s table
  > is now transcribed from bash's grammar (`BASH_OPERATOR_TOKENS`, sorted
  > longest-first for maximal munch), `1>` — which bash does not have — is
  > gone, and the sweep runs in the suite at a cost of ~2.4 s.
  > `test_the_parser_knows_exactly_bashs_operators` fails loudly on any drift
  > in either direction.
  >
  > Round 4 also fixed an `IndexError` that made `( case a in a) shred -u
  > /etc/passwd ;; esac)` — valid, idiomatic bash — raise out of
  > `validate_bash_command`, where `main()`'s blanket `except Exception:
  > sys.exit(0)` turned it into **no decision at all**: the deny was lost.

  Round 3 also replaced the mechanism rather than adding a fourth condition:
  reserved-word position is now **derived from the emitted token stream** (the
  previous token's type), not tracked in a flag. The honest claim remains a
  measured one, not a universal: **no parser path measured over the tokenizer's
  own enumerated paths hides a sub-command, except the fd-duplication head gap
  above.** The splitter is a hand-written tokenizer, not a bash grammar.

  Round 4's measured claim, stated on the axis round 3 could not reach: **no
  1–3 character operator spelling, under any fd-word prefix, hides a command**
  (3504 cases, 0 hidden), and the parser's operator table is set-equal to
  bash's. The `n>&-` head gap above is unchanged and still the only residual
  on the 801-case corpus. It **deserves its own task** — see task 32 §10.10.

  > **CORRECTION (round 5, 2026-08-28).** Refuted in the same shape as round
  > 4's correction of round 3's, one axis over. Round 4's 3504 cases varied
  > OPERATOR SPELLING and held POSITION fixed: every case was
  > `printf A <slot> TAIL`, the slot after a command word — the one position
  > where a word the parser leaks is a harmless extra ARGUMENT rather than the
  > sub-command HEAD. The same 1752 slots, in twelve positions (command start
  > after each of bash's five separators, inside a heredoc body, inside a `<<-`
  > heredoc body, inside a `case` pattern list, glued to `esac`, and the two
  > argument shapes), give **21024 cases**, 7771 of which bash executes:
  >
  > | parser | hid a command bash ran | laundered a deny to `allow` |
  > |---|---|---|
  > | pre-task-32 (`ca2a81a`) | 2079 | 548 |
  > | task 32 round 4 | 1946 | **1836** |
  > | task 32 round 5 | **0** | **0** |
  >
  > **Round 4 laundered more denied commands on this corpus than the parser it
  > replaced**, because it introduced three deny→allow regressions: it taught
  > the tokenizer the `<<-` spelling without its tab-stripped terminator (1752
  > of the 1946), left `<<`/`<<-` in `REDIRECTIONS_WITH_ARG` so the operator ate
  > the command name after them, and kept a `<<<` guard that skipped the flush
  > which closes a `case` (`case a in esac<<<x ; shred -u /etc/passwd` →
  > `allow`, where HEAD denied). It also left two inputs that made the parser
  > RAISE — `("1"*4301) + ">& f\nshred …"` and `("$((" * 3000) + "\nshred …"` —
  > which `main()`'s blanket `except Exception: sys.exit(0)` turns into **no
  > decision at all**, exit 0 with empty stdout.
  >
  > The residual `n>&-` head gap is **CLOSED** in round 5, together with the
  > fd-dup OPERAND leak that is the same defect one token later. Its
  > "direction-safe" record held only in argument position: `echo hi ; 1>&-
  > shred -u /etc/passwd` verdicted `ask` on both HEAD and round 4, which is a
  > hard deny downgraded to a prompt. It now verdicts `deny`, and the path
  > table's `known_gap` column is empty (asserted). Round 5 needs no separate
  > task for it.
  >
  > Round 5's measured claim, narrower on purpose: **no 1–3 character operator
  > spelling, under any of three fd-word prefixes, in any of twelve positions,
  > hides a command from the parser** (21024 cases, 0 hidden, 0 laundered), and
  > the corpus that says so exists in the checkout and is non-vacuous per
  > shape. It is not a claim about the parser. The next axis is already known
  > to be unswept: a QUOTED command word (`'shred' git status`, `s""hred git
  > status`, `\shred git status`) is never unquoted, so bash resolves a command
  > name the parser never sees — `ask`, never `allow`, but the same class. See
  > task 32 §12.9 for that and three more, one of which (`FUSED_FD_OPERATORS`
  > is not word-boundary gated: `./scripts/build2>&1 --all` → `allow` as
  > `./scripts/build --all`, while bash runs `./scripts/build2`) is toward
  > **allow**.

  > **CORRECTION (round 6, 2026-08-28).** Same shape, third time: the round
  > that closed a class opened two of its own, and both turned a HEAD `deny`
  > into an explicit **`allow`** — which is worse than the failures they
  > replaced, because a fail-open at least falls through to the native prompt.
  >
  > 1. **The substitution depth cap truncated in SILENCE.** Past
  >    `MAX_SUBSTITUTION_DEPTH` (64) the nested text stays glued to its
  >    enclosing word, and round 5's comment claimed the head then becomes that
  >    unmatchable word. It does not: the head is the ALLOWLISTED WORD IN FRONT
  >    of the substitution. `echo A $($(… 65 deep … shred -u /etc/passwd …))` —
  >    HEAD `deny`, round 5 `allow ['echo A']`, bash runs the payload. Every
  >    carrier, including the write-target gate.
  > 2. **The heredoc terminator was a PREFIX match, and round 5 added a test of
  >    the first body line.** Together those two are a bypass: the residual of
  >    the line that falsely closed the body reopens a swallowing state that
  >    eats the real terminator and the command after it.
  >    `cat <<EOF\nEOF cat <<Z\nEOF\nshred -u /etc/passwd` — HEAD `deny`,
  >    round 5 `allow ['cat cat']`, bash runs the payload. 24 such rows in a
  >    102-case fuzz, bash executes the payload in 24/24.
  >
  > Round 5's own tests could not see either. Its crash corpus nests **3000**
  > deep — above the depth at which HEAD itself raises, so the band where HEAD
  > parses and round 5 does not (65 up to 993 for `$(`, 497 for `$((`) was
  > never swept; and nothing tested a body line that merely BEGINS with the
  > delimiter. Both families are now standing corpora
  > (`_DEPTH_CAP_DOORS`, `_HEREDOC_PREFIX_TERMINATOR_TRIPLES` and the 108-cell
  > fuzz grid) in `tests/test_integration_pretool.py`.
  >
  > The laundering column above also needs a caveat: `548` is the figure under
  > the three-pattern `_FakeLoader`, not under the live merged settings, where
  > the same corpus gives **731** for HEAD (round 4: 1836 under both). Round 5
  > and round 6 are 0 under both. Everything else in round 5's table
  > re-measured to the digit in round 6 — path corpus 70/15/0, operator space
  > 2079/1946/0 over 7771 executed. See task 32 §13.

  The invariant is also **not yet whole** for three reasons outside the
  splitter: (1) **task 30** — `workspace_binary`/`workspace_rm`/`local_function`
  still outrank deny and ask, so a denied command with an in-workspace target is
  allowed on a tier that never consults the deny list; (2) **name resolution** —
  the validator vouches for a *name*, not for what the name resolves to at run
  time (`X=echo; f(){ $X http://e/x; }; trap f EXIT; X=curl` allows); filed as
  [task 33](../33_name_resolution_not_vouched.md); (3) **NUL fail-open** — a NUL
  byte in a redirect target raises out of `_resolve_target_path` and `main()`'s
  blanket `except` turns it into `sys.exit(0)`, an allow; filed as
  [task 34](../34_nul_byte_fail_open.md). Also standing, per invariant 8: the
  fixes are in the repo working tree only — `./install-claude-config.sh` has not
  been re-run since, so `~/.claude/hooks/` still carries all three bugs.
