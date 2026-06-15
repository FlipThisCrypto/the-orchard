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
    # WalletConnect project id for the hosted claim page (GET /claim). A
    # PUBLIC client identifier (not a secret) the browser uses to open the
    # WalletConnect modal — get a free one at https://cloud.reown.com
    # (formerly cloud.walletconnect.com). Empty default -> the claim page
    # renders a "WalletConnect not configured" notice. Reuse the same value
    # as the dashboard's ORCHARD_VIEW_WC_PROJECT_ID.
    wc_project_id: str = ""
    # Where the claim page sends operators. home_url is the "Back to The
    # Orchard" button + the post-claim auto-redirect target (the landing page).
    # dashboard_url is the "View your Tree" button shown after a successful
    # claim — leave empty to hide that button until the hosted dashboard exists.
    home_url: str = "https://theorchard.network"
    dashboard_url: str = ""
    # Test-mode bypass for the BLS signature step. The pubkey -> address
    # binding check still runs (so a wrong-pk submission fails). Use
    # ONLY in tests/CI. Defaults False so production fails closed.
    # Hardening: honored ONLY for loopback callers (see routes/auth.py),
    # so a mis-set env var can't downgrade auth for remote clients.
    auth_test_mode: bool = False

    # Security hardening (2026-06-09 audit).
    # Shared secret the DataLayer writer presents on POST /attestations
    # as the X-Orchard-Writer-Token header. Empty default: when unset,
    # /attestations writes are accepted only from loopback (the writer
    # runs on the oracle host). Set this to a long random string if the
    # writer runs on a different machine than the oracle.
    writer_token: str = ""
    # Per-IP rate limits (requests/minute) on sensitive endpoints.
    # Loopback (the operator's own dashboard + local writer) is ALWAYS
    # exempt; these bound remote LAN callers only. 0 disables a limit.
    auth_rate_limit_per_min: int = 30
    readings_rate_limit_per_min: int = 600

    # Phase 6.6 register hardening: /register requires a verified
    # wallet session by default. Closes the "anyone can claim someone
    # else's address" attack — the requester's session.address becomes
    # the source of truth for the bound wallet, not a free-form body
    # field. Operators with legacy curl/CI registration scripts that
    # haven't been updated to do the WalletConnect challenge first can
    # set this to false during transition, but the default is True for
    # security-by-default. Once everything is migrated this option
    # goes away.
    require_wallet_session: bool = True

    # Replay protection (docs/replay-protection.md, HANDOVER T3).
    # When True, POST /readings requires a strictly-increasing `seq`
    # inside the signed body and 409s anything stale. Default False —
    # staged rollout mirroring require_wallet_session: ship the oracle,
    # reflash/OTA the fleet with seq-capable firmware, THEN flip this.
    # (last_seq is tracked passively even while False, so the flip is
    # seamless for Trees already sending seq.)
    require_seq: bool = False

    # Reading freshness (HANDOVER D6/T6). When > 0, POST /readings rejects a
    # reading whose signed `ts` (real UTC epoch seconds, set once the Tree's
    # SNTP clock syncs) is older than this many seconds — a data-quality
    # guard that complements `seq`. Default 0 = off; readings without a `ts`
    # (pre-SNTP firmware, or before first sync) are never rejected by it, so
    # a mixed fleet is safe. Turn it on (e.g. 900) once the fleet reports ts.
    max_reading_age_seconds: int = 0

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
