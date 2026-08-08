# Epic 18 — Telegram Mini App composer

**Status:** backlog · **Owner:** Anton · **Created:** 2026-08-02 · **Rev:** 1

> Vision transfer only. In-depth planning happens when this leaves the backlog;
> there are deliberately no task files yet.

## 1. Problem & thesis

Epics 16 and 17 are built out of chat primitives, and chat primitives have a hard
ceiling: **a bot cannot prefill the compose box** (epic 17 §2.1). Everything
follows from that — a spawn is a sequence of message round trips, "edit before
sending" is copy-and-paste, and search is paging a short list. Each step is a
message in the chat, so a two-minute setup leaves ten messages of debris.

**Thesis:** a Telegram **Mini App** is the one surface where all of it collapses
into a single screen — machine, workspace, profile, model, a searchable list of
templates and history, and an **editable textarea** holding the chosen text —
submitted once. It is the only option that satisfies "search history *and* edit
before sending" without compromise, and it retires the wizard rather than
extending it.

This is the largest piece of the three and the only one that adds an internet-
facing surface, which is why it is last and separate.

## 2. What it is

An HTML page served by the relay, opened from a `web_app` inline-keyboard button
(`/new` with no arguments, or a persistent menu entry). Inside:

- **Target row** — machine · workspace, defaulted to the last used, both from the
  installations bound to this chat and each machine's seen-store.
- **Model row** — profile and tier, defaulted from the machine's `[listen]` config.
- **Source tabs** — *Blank* · *Templates* · *History*, with a live filter box.
  History is per-workspace, deduplicated, unbounded by message size for once.
- **Editor** — a textarea, prefilled by whatever was picked, freely edited,
  multi-line, no length ceiling and no copy-paste dance.
- **Send** — one POST; the chat receives only the epic-16 acknowledgement.

The same shell has obvious later tenants: an `/ls` view with per-machine session
state, and a composer for **replies to an idle session** (today a phone types
those into the plain compose box, where multi-line is awkward and history is
unavailable).

## 3. Constraints to design against

**3.1 — Data return path.** `Telegram.WebApp.sendData` works only for *reply
keyboard* buttons. From an inline-keyboard `web_app` button the page must call
the backend itself: POST to the relay with `initData`, which the relay validates
by HMAC-SHA256 against the bot token. The relay already owns that token, so it is
the only component that *can* validate — a natural fit, and the reason the page
is served by the relay rather than by each machine.

**3.2 — The relay still must not learn your directories.** Workspaces, templates
and history live on the machines. The page therefore reads them through the
epic-16 command channel: new `catalog` / `templates` / `history` command kinds,
proxied to a live listener with a short deadline. The relay passes bytes through;
it stores none of them. If that proves too slow in practice, the alternative is a
short-lived per-session cache on the relay — a real privacy decision, to be taken
deliberately and not by accident.

**3.3 — Offline machines.** The page must show a machine as offline and stay
usable for the others. No spinner that never resolves.

**3.4 — Client floor.** `web_app` buttons need a recent Telegram client; the
epic-16/17 chat flows remain the fallback and must keep working unchanged. This
epic adds a surface, it does not replace one.

**3.5 — HTTPS and hosting.** The relay already terminates HTTPS behind Caddy with
a `public_url`, so serving the page is a route, not new infrastructure. Static,
self-contained, no CDN.

## 4. Security

This is the first part of the system a browser can reach, so it is also the first
that needs web-shaped defences:

- `initData` HMAC validation on **every** request, plus an expiry check on
  `auth_date`; a request whose user is not the bound user of that chat is
  rejected the same way loose replies are today.
- Page-scoped, short-lived API credentials — never the installation token in the
  page.
- Strict CSP, no third-party assets, no analytics.
- The epic-16 bounds hold unchanged: seen-store workspaces only, no arbitrary
  paths, no `--yolo`, per-machine spawn caps.
- The page can *start* work and read session lists. It cannot read files, run
  commands, or answer permission prompts — those stay in the chat, where the
  existing audit trail is.

## 5. Success criteria

- [ ] From one screen: pick machine and workspace, filter history, pick an entry,
      edit it, pick a model, send — and a session starts, with a single ack
      message in the chat and no wizard debris.
- [ ] Long multi-line prompts survive verbatim, including code blocks.
- [ ] Templates and history are fetched live from the target machine; an offline
      machine is labelled, not hung.
- [ ] A forged or expired `initData` is rejected; a user who is not the bound
      user of the chat cannot open a working session view.
- [ ] With the Mini App unavailable (old client, page down), `/new` and `/ls`
      behave exactly as in epics 16/17.

## 6. Out of scope

- **A terminal.** No command execution, no file browsing, no shell.
- **Live output streaming / transcript viewer.** Session output keeps arriving as
  idle notifications.
- **Answering permission prompts in the page.** Approvals stay in the chat.
- **Replacing the chat flows.** They remain the supported fallback (§3.4).
- **Multi-user / team views.** One bound user, as everywhere else in this system.
