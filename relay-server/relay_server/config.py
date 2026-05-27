"""Server configuration loading.

Precedence (highest first):
    1. Explicit overrides passed to ``load_config``.
    2. Environment variables (``RELAY_*``).
    3. Optional ``/etc/relay/config.toml`` (or path given by ``RELAY_CONFIG``).
    4. Built-in defaults.

A plain dataclass keeps the dependency surface minimal — pydantic-settings is
overkill for half a dozen scalar fields.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = "/etc/relay/config.toml"


@dataclass
class RelayConfig:
    bot_token: str | None = None
    webhook_secret: str | None = None
    public_url: str | None = None
    db_path: str = "relay.db"
    listen: str = "127.0.0.1:8080"
    # When False, skip the setWebhook call on startup. Tests set this off.
    set_webhook_on_startup: bool = True
    # Interval in seconds between reaper ticks. Defaults to 30 s; tests may
    # set a shorter value to avoid slow test suites.
    reaper_interval: float = 30.0

    extra: dict[str, Any] = field(default_factory=dict)


_ENV_MAP = {
    "RELAY_BOT_TOKEN": "bot_token",
    "RELAY_WEBHOOK_SECRET": "webhook_secret",
    "RELAY_PUBLIC_URL": "public_url",
    "RELAY_DB_PATH": "db_path",
    "RELAY_LISTEN": "listen",
    "RELAY_REAPER_INTERVAL": "reaper_interval",
}

# Type coercions for env vars whose field type is not str.
_ENV_COERCE: dict[str, Any] = {
    "reaper_interval": float,
}


def _load_toml(path: str | Path) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    with p.open("rb") as f:
        return tomllib.load(f)


def load_config(
    *,
    config_path: str | Path | None = None,
    env: dict[str, str] | None = None,
    overrides: dict[str, Any] | None = None,
) -> RelayConfig:
    env = env if env is not None else os.environ
    cfg = RelayConfig()

    # 1. Optional TOML file (env override path wins if set).
    path = config_path or env.get("RELAY_CONFIG", DEFAULT_CONFIG_PATH)
    toml_data = _load_toml(path)
    for key, value in toml_data.items():
        if hasattr(cfg, key):
            setattr(cfg, key, value)

    # 2. Env vars.
    for env_key, attr in _ENV_MAP.items():
        if env_key in env and env[env_key]:
            raw = env[env_key]
            coerce = _ENV_COERCE.get(attr)
            setattr(cfg, attr, coerce(raw) if coerce is not None else raw)

    # ``set_webhook_on_startup`` is intentionally not an env-mapped scalar
    # (it's only flipped from tests/callers via overrides) but we accept
    # ``RELAY_SET_WEBHOOK=0`` as an escape hatch for ops.
    if "RELAY_SET_WEBHOOK" in env:
        cfg.set_webhook_on_startup = env["RELAY_SET_WEBHOOK"].lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    # 3. Explicit overrides.
    if overrides:
        cfg = replace(cfg, **{k: v for k, v in overrides.items() if hasattr(cfg, k)})

    return cfg
