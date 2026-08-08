# SPDX-License-Identifier: Apache-2.0
"""Operator preflight for DataLayer jobs.

Run before cron/Task Scheduler so misconfigured certs, empty store_id,
or a down oracle fail loudly with actionable messages — not mid-batch.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg_mod
from .oracle import OracleClient, OracleError
from .rpc import ChiaRpcError, DataLayerRpc, FullNodeRpc


def store_id_wellformed(store_id: str | None) -> bool:
    """A DataLayer store id is a 32-byte hex string (64 chars), optionally
    ``0x``-prefixed. Catches truncated/typo'd ids before a batch run."""
    if not isinstance(store_id, str):
        return False
    s = store_id.strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    if len(s) != 64:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))


def run_preflight(*, skip_chia: bool = False) -> Report:
    """Inspect config + optional live pings. Never raises for soft fails."""
    rep = Report()

    # Config file
    try:
        cfg = cfg_mod.load()
        rep.add("config.yaml", True, str(cfg_mod.CONFIG_PATH))
    except FileNotFoundError as e:
        rep.add("config.yaml", False, str(e))
        return rep
    except Exception as e:  # noqa: BLE001
        rep.add("config.yaml", False, f"{type(e).__name__}: {e}")
        return rep

    # Store id — must be present AND well-formed (64 hex), not just non-empty.
    sid = (cfg.data_layer.store_id or "").strip()
    if not sid:
        rep.add("datalayer.store_id", False, "empty — create_data_store")
    elif not store_id_wellformed(sid):
        rep.add(
            "datalayer.store_id",
            False,
            f"malformed (expected 64 hex chars): {sid[:20]}…",
        )
    else:
        rep.add("datalayer.store_id", True, sid[:16] + "…")

    # Signing key material present
    key_path = cfg_mod.SIGNING_KEY_PATH
    key_ok = key_path.exists() and len(cfg.signing_key_hex) == 64
    rep.add(
        "oracle_signing_key",
        key_ok,
        str(key_path) if key_ok else f"missing/invalid at {key_path}",
    )

    # Cert files
    for label, path in (
        ("full_node.cert", cfg.full_node.cert_path),
        ("full_node.key", cfg.full_node.key_path),
        ("datalayer.cert", cfg.data_layer.cert_path),
        ("datalayer.key", cfg.data_layer.key_path),
    ):
        p = Path(os.path.expanduser(os.path.expandvars(path or "")))
        rep.add(label, p.is_file(), str(p) if p.is_file() else f"not found: {p}")

    # Oracle liveness
    try:
        oracle = OracleClient(cfg.oracle.url, cfg.oracle.writer_token)
        season = oracle.current_season()
        nodes = oracle.list_nodes()
        rep.add(
            "oracle",
            True,
            f"{cfg.oracle.url} season={season} trees={len(nodes)}",
        )
    except OracleError as e:
        rep.add("oracle", False, str(e)[:160])

    if skip_chia:
        rep.add("chia_rpc", True, "skipped (--skip-chia)")
        return rep

    # Full node peak
    try:
        fn = FullNodeRpc(
            cfg.full_node.host,
            cfg.full_node.port,
            cfg.full_node.cert_path,
            cfg.full_node.key_path,
        )
        h = fn.peak_height()
        rep.add("full_node", True, f"peak_height={h}")
    except ChiaRpcError as e:
        rep.add("full_node", False, str(e)[:160])

    # DataLayer root (requires store_id)
    if sid:
        try:
            dl = DataLayerRpc(
                cfg.data_layer.host,
                cfg.data_layer.port,
                cfg.data_layer.cert_path,
                cfg.data_layer.key_path,
            )
            root = dl.get_root(sid)
            rh = root.get("hash") or root.get("root_hash") or "?"
            conf = root.get("confirmed")
            rep.add(
                "datalayer",
                True,
                f"root={str(rh)[:16]}… confirmed={conf}",
            )
        except ChiaRpcError as e:
            rep.add("datalayer", False, str(e)[:160])
    else:
        rep.add("datalayer", False, "skipped — no store_id")

    return rep


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    skip = "--skip-chia" in args
    as_json = "--json" in args
    rep = run_preflight(skip_chia=skip)
    if as_json:
        import json
        print(json.dumps({
            "ok": rep.ok,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail}
                for c in rep.checks
            ],
        }, indent=2))
        return 0 if rep.ok else 1
    ok_m, fail_m = "OK", "FAIL"
    print("Orchard DataLayer preflight\n")
    for c in rep.checks:
        mark = ok_m if c.ok else fail_m
        line = f"  [{mark:4}] {c.name}"
        if c.detail:
            line += f" — {c.detail}"
        print(line)
    print()
    print(f"Result: {'READY' if rep.ok else 'NOT READY'}")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
