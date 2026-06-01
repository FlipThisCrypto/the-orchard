# SPDX-License-Identifier: Apache-2.0
"""Oracle service configuration.

All settings load from environment variables (or `oracle/.env`), prefixed
with ``ORCHARD_ORACLE_`` so they don't collide with other components.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


# pydantic-settings resolves a relative `env_file` against the process
# current working directory. Operators run `python -m oracle.app.main`
# from the repo root, so a plain "env_file=.env" would look for the
# file at the repo root and silently miss oracle/.env where every
# README + example tells the operator to put it. Pin the path
# explicitly relative to THIS file so it works regardless of cwd.
_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    """Runtime configuration. Override via env vars or oracle/.env."""

    # Default to localhost-only. `/register` accepts any caller with a
    # node_id + signing key and no auth, so a 0.0.0.0 bind would let
    # any LAN device pre-claim a node_id and lock out the real Tree.
    # If you need real Trees on the WiFi to POST signed readings to
    # this oracle, set ORCHARD_ORACLE_HOST=0.0.0.0 explicitly in your
    # `.env` AND understand that anyone on the same network can also
    # call /register.
    host: str = "127.0.0.1"
    port: int = 8000
    db_url: str = "sqlite:///./oracle/data/orchard.db"
    log_level: str = "info"

    # v1 Season math: day-aligned. Phase 5 will swap this for
    # Chia-block-aligned Seasons.
    season_genesis_date: date = date(2026, 5, 27)

    # Phase 6.6 session auth.
    # HS256 signing key for issued session tokens. Empty default
    # triggers a per-process ephemeral secret (restart rotates all
    # sessions). Operators with multi-process deployment should set
    # ORCHARD_ORACLE_SESSION_SECRET to a long random string.
    session_secret: str = ""
    # Session TTL — default 1 hour. Operators can drop to (e.g.)
    # 900 for stricter posture via ORCHARD_ORACLE_SESSION_TTL_SECONDS.
    session_ttl_seconds: int = 3600
    # Challenge nonce TTL — the window between /auth/challenge and
    # /auth/verify. 120s is generous for a real wallet sign prompt.
    challenge_ttl_seconds: int = 120
    # Test-mode bypass for the BLS signature step. The pubkey -> address
    # binding check still runs (so a wrong-pk submission fails). Use
    # ONLY in tests/CI. Defaults False so production fails closed.
    auth_test_mode: bool = False

    model_config = SettingsConfigDict(
        env_prefix="ORCHARD_ORACLE_",
        env_file=str(_ENV_PATH),
        env_file_encoding="utf-8",
        extra="ignore",
    )


_settings: Settings | None = None


def settings() -> Settings:
    """Lazy singleton so tests can override env before the first call."""
    global _settings
    if _settings is None:
        _settings = Settings()
        # Ensure the SQLite data directory exists if we're using one.
        if _settings.db_url.startswith("sqlite:///"):
            db_path = Path(_settings.db_url.replace("sqlite:///", "", 1))
            db_path.parent.mkdir(parents=True, exist_ok=True)
    return _settings


def reset_settings_for_tests() -> None:
    """Tests can call this after monkeypatching the env."""
    global _settings
    _settings = None
