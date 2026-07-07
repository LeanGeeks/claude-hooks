# 13-01 — Profile loader

**Status:** todo · **Depends on:** none
**Read first:** [brd.md](./brd.md) §2 (config format), §4 (components)

## Goal

Add a TOML profile loader to `amux_spawn_lib.py` that reads
`~/.claude/profiles.toml`, resolves `${var}` interpolation, merges
`[all-profiles]` with per-profile env vars, and returns clean dicts.

## Scope

Public API in `amux_spawn_lib.py`:

- `load_profiles(path=None) -> dict[str, dict[str, str]]`
  Returns `{profile_name: {ENV_VAR: resolved_value}}` for all profiles.
  Each profile's dict is the merged result: `[all-profiles]` globals with
  `[profile.X]` overrides applied, all `${var}` refs resolved.

- `resolve_profile(name, path=None) -> dict[str, str]`
  Returns merged env vars for a single profile. Raises on unknown profile.

- `emit_shell_functions(path=None) -> str`
  Returns bash source text defining one shell function per profile. Each
  function checks for `amux-spawn` on PATH and routes accordingly (amux-spawn
  spawn --profile, or subshell with env + exec claude).

- `profile_names(path=None) -> list[str]`
  Returns sorted list of profile names (for completion).

Default path: `~/.claude/profiles.toml`. Return empty dict / empty list if
file does not exist (not an error — profiles are opt-in).

## Var interpolation rules

- `${name}` replaced with value from `[vars]`. Works in both `[all-profiles]`
  and `[profile.*]` values.
- Undefined `${name}` → error at load time (not silent empty string).
- Literal `${` escaped as `$${`.
- One-pass, non-recursive: `${a}` doesn't expand `${b}` inside `a`'s value.
- `[vars]` keys are never exported.

## Config format (concrete example)

The TOML file has three top-level sections. This is the canonical reference
for what the loader must parse:

```toml
[vars]
glm_token  = "e468a9fd..."
github_pat = "ghp_ND0y..."
opus       = "claude-opus-4-6"

[all-profiles]
API_TIMEOUT_MS = "600000"
GITHUB_MCP_PAT = "${github_pat}"

[profile.claude]
ANTHROPIC_DEFAULT_OPUS_MODEL   = "${opus}"
ANTHROPIC_DEFAULT_SONNET_MODEL = "claude-sonnet-4-6"

[profile.claude-latest]
# empty — gets only [all-profiles] vars

[profile.claude-glm5]
ANTHROPIC_BASE_URL            = "https://api.z.ai/api/anthropic"
ANTHROPIC_AUTH_TOKEN           = "${glm_token}"
ANTHROPIC_MODEL               = "glm-5.2[1m]"
API_TIMEOUT_MS                = "900000"
```

Expected output of `resolve_profile("claude-glm5")`:
```python
{
    "API_TIMEOUT_MS": "900000",           # profile overrides [all-profiles]
    "GITHUB_MCP_PAT": "ghp_ND0y...",      # inherited from [all-profiles]
    "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
    "ANTHROPIC_AUTH_TOKEN": "e468a9fd...", # ${glm_token} resolved
    "ANTHROPIC_MODEL": "glm-5.2[1m]",
}
```

Expected output of `resolve_profile("claude-latest")`:
```python
{
    "API_TIMEOUT_MS": "600000",
    "GITHUB_MCP_PAT": "ghp_ND0y...",
}
```

## Expected output of `emit_shell_functions()`

The function returns bash source text. For each profile, it emits a shell
function whose name equals the profile name. The function checks whether
`amux-spawn` is on PATH: if yes, routes through `amux-spawn spawn --profile`;
if no, exports the profile's env vars in a subshell and exec's `claude`.

Example for the `claude-glm5` profile:

```bash
claude-glm5() {
    if command -v amux-spawn &>/dev/null; then
        amux-spawn spawn --profile claude-glm5 "$@"
    else
        (
            export ANTHROPIC_BASE_URL="https://api.z.ai/api/anthropic"
            export ANTHROPIC_AUTH_TOKEN="e468a9fd..."
            export ANTHROPIC_MODEL="glm-5.2[1m]"
            export API_TIMEOUT_MS="900000"
            export GITHUB_MCP_PAT="ghp_ND0y..."
            exec claude "$@"
        )
    fi
}
```

Values in the fallback branch are fully resolved (no `${var}` references —
those are resolved at source-time by the Python loader, not at invocation-time
by bash). The `amux-spawn` branch does NOT inline env vars — `amux-spawn`
reads the TOML itself.

## Implementation notes

- Use `tomllib` (Python 3.11+ stdlib). The project already requires 3.11+.
- Keep the loader pure (no side effects, no os.environ mutation).
- Var interpolation is a simple `re.sub` on `\$\{(\w+)\}`.
- Add new functions at the end of `amux_spawn_lib.py`, after the existing
  spawn/handle/state code. Don't restructure the existing code.
- Quote all values in the emitted bash with double quotes (handles values
  containing spaces, brackets, etc.).

## Testing

Add tests to a new file: `tests/test_profile_loader.py`.

- Write all tests against temporary TOML files (use `tmp_path` or `tempfile`).
  Never read `~/.claude/profiles.toml` in tests.
- Wire the new test module into `tests/run_all_tests.py` by adding an entry
  to the `TEST_MODULES` dict: `"unit_profile_loader": "test_profile_loader"`.
  Follow the existing pattern (key is the `--module` name, value is the
  filename without `.py`).
- Test the Python functions directly. Do NOT attempt to eval the generated bash
  in tests — just verify the string output contains expected patterns.

## Done criteria

- [ ] `load_profiles()` returns correct merged dicts for a multi-profile TOML.
- [ ] `${var}` interpolation resolves correctly; undefined vars raise.
- [ ] `[all-profiles]` values appear in every profile; per-profile overrides win.
- [ ] Empty profiles (like `[profile.claude-latest]`) get only `[all-profiles]` vars.
- [ ] Missing file returns empty dict (no crash).
- [ ] `emit_shell_functions()` produces valid bash that handles amux/no-amux.
- [ ] Unit tests cover: happy path, var interpolation, undefined var error, merge
      override, empty profile, missing file, `$${` escape.
