# Epic 13 — Model profiles: execution state

| # | Task | Status | Depends on | Notes |
|---|------|--------|------------|-------|
| 13-01 | [Profile loader](./13-01-profile-loader.md) | done | — | Core TOML parser + var interpolation + merge + shell emitter |
| 13-02 | [CLI and shell integration](./13-02-cli-and-shell.md) | done | 13-01 | amux-spawn --profile, profiles subcommand, shell wrappers, completion, example TOML |
| 13-03 | [Installer and migration](./13-03-installer-and-migration.md) | done | 13-02 | install-claude-config.sh step, migration guidance |
