# Task 37-07 — Remote event fan-out (deferred)

**Status:** deferred — not scheduled · **Depends on:** 37-03

Recorded so the reasoning survives, not because it is queued. Do not start this
without a concrete requirement that the local path cannot meet.

## What it would be

A network fan-out over the same per-session event logs, letting a consumer
subscribe from another machine. `amux-server.py` already serves REST on `:8822`
with a token at `~/.amux/auth_token`, so an `/events` endpoint is achievable, and
Claude Code's `Monitor` accepts a WebSocket source directly — the consumer side
would be `Monitor({ws: {url: …}})` with no shell in the loop at all.

## Why it is deferred

It buys **cross-machine reach and nothing else**. It would fan out over the
identical files that 37-03 reads locally, so it is not a stepping stone toward a
better architecture — the local log is the whole mechanism, and the socket is a
transport bolted beside it.

Against that: it adds a daemon dependency to a supervision path whose current
virtue is that it has none, and it invites the mistake of moving event
*production* onto the network. That must not happen —
[architecture.md](./architecture.md) §3 keeps the producer on local file appends
precisely because the hook runs on the hot path of every turn-end of every
tracked session, and an `O_APPEND` write cannot hang where a socket write can.
If this task is ever taken up, the fan-out reads the logs; the hook keeps writing
files.

## What would justify starting it

- Orchestrators genuinely running on a different machine from their workers —
  the direction `REMOTE.md` and `cloud/` point, but not today's usage.
- A consumer that cannot mount `~/.amux/spawn/`.
- Verified evidence that the assumption in [architecture.md](./architecture.md)
  §6 is wrong and some push transport can wake an idle agent better than the
  local stream does — see [37-06](./37-06-live-verification_human.md) item 12.

## What would not

Wanting typed queries. That is an MCP-shaped want, it is separable from events,
and a well-shaped `watch` removes most of the querying that motivates it.
