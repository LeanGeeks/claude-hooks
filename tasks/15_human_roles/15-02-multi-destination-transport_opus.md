# 15-02 — Multi-destination transport

**Status:** todo · **Depends on:** 15-01
**Read first:** [brd.md](./brd.md) §3.2 (bindings), §7 (components)

## Goal

Today the router owns exactly one relay client
(`telegram_permission_router.py:101`) and every helper implicitly targets it.
Make the router able to talk to *any* destination by installation token, and
make the two processes that outlive a send — the parked `PermissionRequest`
hook and `PostToolUse` — able to find the right client again.

Nothing about routing *policy* belongs here. This task moves tokens around;
15-03 decides which token.

## Why a token and not a role

The router stays ignorant of `roles.toml`. Callers resolve a role to a
`Destination` (15-01) and hand the router a token string. The one place that
needs the reverse — `posttool_hook`, a separate process that only has a
state-store row — re-resolves `role → token` through `roles_config` using the
row's own `cwd`. The token is therefore **never persisted**; only the role id is.

## Scope

### `telegram_permission_router.py`

Replace the `_relay_client` singleton with a token-keyed registry.

```python
_clients: dict[str, RelayClient] = {}   # keyed by installation token
_clients_lock = threading.Lock()
_server_url: str | None = None
```

- `load_telegram_config()` keeps its name, its `TELEGRAM_ENABLED` /
  `RELAY_CONFIG_SOURCE` globals, and its current failure logging. Internally it
  now calls `roles_config.load_bindings()` for `server_url` and
  `installation_token` instead of `RelayClient.from_config()`, then constructs
  the default client. `TELEGRAM_ENABLED` continues to mean *the default
  destination is usable*.

  **Guard the import.** This makes the router depend on a module the installer
  only learned about in 15-01. Wrap the `roles_config` import in `try/except`
  and fall back to the existing `RelayClient.from_config()` path when it is
  missing, so a stale or partial `~/.claude/hooks/` degrades to today's
  behaviour rather than losing Telegram altogether. Same fail-open convention
  the rest of these hooks follow.

- `_client(token: str | None = None) -> RelayClient`
  `None` → the default client (current behaviour). A token → a client from
  `_clients`, created lazily under `_clients_lock` as
  `RelayClient(_server_url, token)`. Raises `RelayError` when the relay is
  disabled or `_server_url` is unknown.

- Every helper that reaches the network gains a keyword-only
  `token: str | None = None`, defaulting to the current behaviour:
  `_send_relay`, `send_question_message`, `wait_for_relay_answer`,
  `remove_inline_buttons`, `edit_message_text`, `delete_message`.
  `send_permission_message` and `send_idle_notification` deliberately do **not**
  — they are default-destination only (brd §6).

- `send_question_message` gains **four** keyword-only parameters, not just
  `token`: `role_title`, `notes` and `banner` as well, forwarded verbatim to
  `render_question_body`. All four default to the current behaviour. Missing
  the forwarding is the easy mistake here — the send path would then route
  correctly while rendering none of the role context that tells the human why
  they got the message.

- New `finalize_message(message_id, body_text, answer_text, *, prefix="✍️ ", token=None) -> bool`
  PATCH the message to `body_text + "\n\n" + prefix + html-escaped answer_text`,
  then cancel it so the keyboard is stripped. `prefix` mirrors the relay's own
  finalization markers (`app.py:1003`): `✍️ ` for a typed answer, `✅ ` for a
  chosen one — 15-04 uses the latter for terminal wins. Patch-then-cancel, in that order: a
  cancelled message can still be edited, but doing it the other way round leaves
  a live keyboard visible for the duration of the round trip. Either step
  failing is logged and non-fatal — returns `True` only when both succeeded.
  Used by 15-04 (terminal wins) and 15-05 (escalation loser).

- New `render_question_body(request, workspace_name, index, total, *, role_title=None, notes=(), banner=None) -> str`
  Extract the body-building half of `send_question_message` into a pure
  function; `send_question_message` calls it. This is what lets a caller
  reconstruct the exact text it sent when it later needs to PATCH it, without
  changing `send_question_message`'s return type (existing tests assert it
  returns an `int` — `test_integration_permission_request.py:493`).
  `role_title`, `notes` and `banner` render per brd §5.1 and 15-03; all three
  default to nothing, so with no roles configured the output is byte-identical
  to today. `banner` is the escalation line (15-05); it renders with a `⏳`
  above the `for:` line.

### `permission_state_store.py`

Add one field to `PermissionRequest` (`permission_state_store.py:96`, after
`agent_id`):

```python
role: Optional[str] = None    # resolved role id; None = default destination
```

Thread it through `create_request(..., role: Optional[str] = None)`.
`from_dict` already filters unknown keys and the dataclass default covers
missing ones, so rows written before this change deserialize unchanged — verify,
don't assume.

### `posttool_hook.py`

`revoke_telegram_message()` (`posttool_hook.py:45`) currently calls
`remove_inline_buttons(message_id)` against the default client, which would
**404** for a role message: `_load_message` filters on `installation_id`
(`app.py:849`), so a `@ux` message is invisible to the operator's token.

Take the whole `PermissionRequest` instead of a bare id, and when
`request.role` is set, resolve `role → token` via
`roles_config.load_catalog(request.cwd)` + `load_bindings()` and pass it
through. Any failure in that resolution falls back to the default client and is
logged — a failed revoke must never affect tool execution, which is why this
hook already swallows everything.

## Concurrency

15-05 waits on several messages at once from one process. `httpx.Client` is
safe for concurrent requests from multiple threads, so a single `RelayClient`
per token is fine; only lazy creation needs `_clients_lock`. Do not add
per-request locking — it would serialise the long-polls this design depends on.

## Implementation notes

- Keep the module-level import fallback for `relay_server`
  (`telegram_permission_router.py:53`) untouched.
- `RelayClient` already accepts `(server_url, installation_token)` positionally
  (`client.py:88`), so no client-library change is needed.
- Do not close clients explicitly. Hook processes are short-lived and the
  existing code already leaks the singleton at exit; adding teardown here would
  risk closing a client another thread is long-polling on.
- Registry keyed by token, not by role: two roles pointing at the same human
  (`arch = "operator"`, brd §3.2) then share one client and one connection pool.

## Testing

Extend `tests/test_integration_permission_request.py` (it already patches
`RelayClient` and exercises this module) and add state-store cases to
`tests/test_unit_state_store.py`.

## Done criteria

- [ ] `_client(None)` returns the default client; `_client("rly_x")` returns a
      distinct client built on the shared `server_url`; a second call with the
      same token returns the *same* object.
- [ ] `_client()` raises `RelayError` when the relay is disabled.
- [ ] With `roles_config` unimportable, `load_telegram_config()` still produces a
      working default client via `RelayClient.from_config()` — simulate by
      patching the import to raise.
- [ ] Every send/wait/cancel/edit helper honours an explicit `token`, and with
      no `token` produces exactly the calls it produces today.
- [ ] `send_permission_message` and `send_idle_notification` have no `token`
      parameter.
- [ ] `render_question_body` with no `role_title`/`notes` returns a string
      identical to what `send_question_message` sent before this change (assert
      against the existing multi-select and single-select fixtures).
- [ ] `finalize_message` issues PATCH then cancel, in that order, against the
      token's client, and returns `False` when either fails.
- [ ] `PermissionRequest.role` round-trips through the JSONL store; a row
      written without the field loads with `role=None`.
- [ ] `posttool_hook` revokes a role-tagged request through that role's client,
      and falls back to the default client when role resolution fails.
- [ ] Existing tests in `test_integration_permission_request.py` pass unchanged.
