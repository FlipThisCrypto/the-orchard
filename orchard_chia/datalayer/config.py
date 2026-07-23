# SPDX-License-Identifier: Apache-2.0
"""Configuration for the Season attestation writer.

Loads from ``chia/config.yaml`` (operator-private; gitignored) and
falls back to defaults when fields are missing. Operators copy
``chia/config.example.yaml`` to ``chia/config.yaml`` and fill in
local node + DataLayer paths.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
SIGNING_KEY_PATH = Path(__file__).resolve().parents[1] / "data" / "oracle_signing_key.hex"


@dataclass
class FullNodeConfig:
    host: str
    port: int
    cert_path: str
    key_path: str


@dataclass
class DataLayerConfig:
    host: str
    port: int
    cert_path: str
    key_path: str
    store_id: str
    # On-chain transaction fee for batch_update writes, in mojos (1 XCH =
    # 1e12 mojos). 0 = node default; raise it if writes stall unconfirmed
    # under mempool congestion. See datalayer/rpc.py::batch_update.
    fee: int = 0


@dataclass
class OracleConfig:
    url: str = "http://127.0.0.1:8000"


@dataclass
class AttestationConfig:
    # How many Seasons before the current one to attest.
    # Default: all closed Seasons (None = no limit).
    max_lookback_seasons: int | None = None
    # Skip Seasons where uptime_hours is 0 (Tree never reported during them).
    skip_empty_seasons: bool = True


@dataclass
class Config:
    network: str
    full_node: FullNodeConfig
    data_layer: DataLayerConfig
    oracle: OracleConfig
    attestation: AttestationConfig
    signing_key_hex: str


def _expand(path_str: str) -> str:
    return os.path.expandvars(os.path.expanduser(path_str))


def load() -> Config:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"chia/config.yaml not found at {CONFIG_PATH}. "
            f"Copy chia/config.example.yaml to chia/config.yaml and edit."
        )
    raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}

    fn = raw.get("full_node", {})
    dl = raw.get("datalayer", {}) or raw.get("data_layer", {})
    orcl = raw.get("oracle", {})
    att = raw.get("attestation", {}) or {}

    return Config(
        network=raw.get("network", "mainnet"),
        full_node=FullNodeConfig(
            host=fn.get("host", "127.0.0.1"),
            port=int(fn.get("port", 8555)),
            cert_path=_expand(fn.get("cert_path", "")),
            key_path=_expand(fn.get("key_path", "")),
        ),
        data_layer=DataLayerConfig(
            host=dl.get("host", "127.0.0.1"),
            port=int(dl.get("port", 8562)),
            cert_path=_expand(dl.get("cert_path", "")),
            key_path=_expand(dl.get("key_path", "")),
            store_id=dl.get("store_id", ""),
            fee=int(dl.get("fee", 0) or 0),
        ),
        oracle=OracleConfig(
            url=orcl.get("url", "http://127.0.0.1:8000"),
        ),
        attestation=AttestationConfig(
            max_lookback_seasons=att.get("max_lookback_seasons"),
            skip_empty_seasons=bool(att.get("skip_empty_seasons", True)),
        ),
        signing_key_hex=_load_or_make_signing_key(),
    )


def _restrict_signing_key_perms() -> None:
    """Force the signing-key file to 0600 (owner read/write only).

    Phase 5 attestations are signed with this key. If it leaks, an
    attacker can forge attestations the payout script accepts. Default
    umask leaves it group/other-readable on many Unix systems; tighten
    on every load so the chmod survives operator edits and file
    re-creations.

    On Windows ``os.chmod`` only flips the read-only bit (NTFS ACLs
    aren't POSIX modes). Real protection there comes from the parent
    directory's ACL, which is why we put the key under the project
    tree owned by the operator's user account.
    """
    try:
        os.chmod(SIGNING_KEY_PATH, 0o600)
    except OSError as e:
        print(f"[orchard.datalayer] WARN: could not chmod "
              f"{SIGNING_KEY_PATH} to 0600: {e}", file=sys.stderr)


def _load_or_make_signing_key() -> str:
    """Per-oracle signing key. Generated on first run, persisted locally,
    never transmitted. 32 bytes / 64 hex chars. Gitignored under
    orchard_chia/data/.

    The file is forced to mode 0600 (owner-only) on every load — see
    ``_restrict_signing_key_perms`` for the Windows caveat.
    """
    SIGNING_KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SIGNING_KEY_PATH.exists():
        text = SIGNING_KEY_PATH.read_text(encoding="utf-8").strip()
        if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
            _restrict_signing_key_perms()
            return text.upper()
    # Generate fresh. Write via os.open with O_CREAT|O_WRONLY and an
    # explicit 0o600 mode so the file is born owner-only on POSIX —
    # closes the small race where a previous default-umask write could
    # leak the bytes to a concurrent reader before chmod lands.
    import secrets
    new_hex = secrets.token_hex(32).upper()
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(str(SIGNING_KEY_PATH), flags, 0o600)
    try:
        os.write(fd, (new_hex + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    _restrict_signing_key_perms()  # belt-and-braces on Windows
    return new_hex
