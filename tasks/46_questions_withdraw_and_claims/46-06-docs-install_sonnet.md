# 46-06 — Docs and installation

**Status:** done · **Depends on:** 46-04, 46-05

- `docs/async-questions.md` — "Withdrawing a question", "Reliable ids: claiming
  on the upstream" (what is pushed and what is not; what happens to the primary
  checkout; the degraded-state table; who commits an answer; claiming; the lock
  file), the three new CLI flags, the new config keys.
- `docs/questions-prompt-example.md` — agent-facing prose for `withdraw` and
  `claim`, and how to read `sync` / `warnings` in an `ask` result.
- `docs/questions.example.toml` — `sync`, `claims`, `[questions.git]`,
  `[questions.format] withdrawn`, and the ladder note.
- `docs/questions-contract.md` — the withdrawn word, "an answer flips only from
  open", files beside the queue.
- `architecture.md` — the module list and the async-question section.
- Install on this machine with `./install.sh --yes`; the listener is restarted
  by the installer.

The rebase recipe in the operator guide was run in a scratch repository before
it was written down (an unpushed hand-made queue commit under a tool-pushed
one: `checkout --ours`, `add`, `rebase --continue`; the hand commit and the
twin both drop out).
