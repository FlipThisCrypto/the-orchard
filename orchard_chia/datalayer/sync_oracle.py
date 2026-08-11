# SPDX-License-Identifier: Apache-2.0
"""sync-oracle: teach the oracle what the chain already knows.

    python -m orchard_chia.datalayer sync-oracle [--dry-run] [--node ID] [--season N]

WHY THIS EXISTS
===============

``attest`` seals a season to DataLayer and then POSTs the record back to the
oracle, so ``/network/stats`` can report ``last_attestation_at``. When that
POST fails the DataLayer write has already succeeded and cannot be replayed —
so the code logs a warning, moves on, and relies on "re-running the writer will
catch up the oracle's view". It does not. The writer skips seasons already
sealed on chain, which is correct and fee-preserving, and therefore never
retries the POST for them.

The result is silent and durable divergence. Measured on 2026-08-11: season 76
sealed on chain and independently verified VALID, while the oracle reported its
newest attestation as 2026-08-09 — 185 legacy placeholders and nothing since.
Every public consumer of ``/network/stats`` had to conclude the chain pipeline
had stalled for two and a half days. It had not.

This command reconciles in the only direction that is safe: the chain is the
authority, the oracle's table is a cache of it. It reads sealed ``attest:``
records, compares against what the oracle holds, and POSTs the ones missing.

WHAT IT COSTS
=============

Nothing on chain. This reads DataLayer and writes only to the oracle's own
database, so there is no batch_update, no fee, and no new permanent record.
That is the whole reason it can be safely re-run and safely scheduled.

WHAT IT CANNOT RECOVER
======================

``dl_tx_id`` — the DataLayer transaction that carried the write. A sealed
record does not contain the id of the transaction that placed it, and there is
no reliable way to recover it after the fact. It is sent as null rather than
invented: the proof of an attestation is its ``data_hash`` and ``oracle_sig``
against the record on chain, and a fabricated transaction id would be worse
than an absent one because it would look checkable.

EXIT CODES
==========

    0  the oracle now matches the chain (or already did)
    1  at least one record could not be synced
    2  configuration/usage failure
"""
from __future__ import annotations

import argparse
import os
import sys

import requests

from . import config, fetch, schema
from .oracle import OracleClient, OracleError
from .rpc import ChiaRpcError, DataLayerRpc
from .verify_latest import _sealed_seasons


def _known_seasons(oracle_url: str, node_id: str, token: str | None) -> set[int]:
    """Seasons the oracle already has an attestation row for."""
    headers = {"X-Orchard-Writer-Token": token} if token else {}
    r = requests.get(f"{oracle_url.rstrip('/')}/attestations/{node_id}",
                     timeout=15, headers=headers)
    r.raise_for_status()
    rows = r.json()
    if not isinstance(rows, list):
        raise ValueError(f"expected a list of attestations, got {type(rows).__name__}")
    return {int(a["season_number"]) for a in rows if "season_number" in a}


def _post(oracle_url: str, record: dict, key_hex: str,
          token: str | None) -> tuple[bool, str]:
    """Upsert one chain record into the oracle. Returns (ok, detail)."""
    body = {
        "node_id":      record["node_id"],
        "season_number": int(record["season"]),
        "hours_online": int(record["hours_online"]),
        "data_hash":    record["data_hash"],
        "oracle_sig":   record.get("oracle_sig"),
        "dl_tx_id":     None,          # unrecoverable; see the module docstring
        "dl_key_hex":   key_hex,
        "block_height_at_write": record.get("block_height_at_write"),
        # The season's own close boundary, not wall-clock now(). Re-running
        # this command must produce an identical write, and `signed_at` is
        # already the deterministic seal time inside the signed body. The
        # authoritative moment of the write is the store's root history.
        "written_to_datalayer_at": record.get("signed_at"),
    }
    headers = {"X-Orchard-Writer-Token": token} if token else {}
    try:
        r = requests.post(f"{oracle_url.rstrip('/')}/attestations", json=body,
                          timeout=20, headers=headers)
    except requests.RequestException as e:
        return False, str(e)[:120]
    if r.status_code not in (200, 201):
        return False, f"HTTP {r.status_code}: {r.text[:140]}"
    # The oracle recomputes hours_online from its OWN uptime data for closed
    # seasons and reports whether it agrees. A disagreement means the chain and
    # the oracle hold different stories about the same season — worth printing
    # loudly, but not worth refusing the sync: the chain record IS what is
    # published, and hiding it from the oracle would not make it less true.
    try:
        out = r.json()
    except ValueError:
        return True, "synced"
    if out.get("hours_match") is False:
        return True, (f"synced, but MISMATCH: chain says "
                      f"{body['hours_online']}h, oracle recomputes "
                      f"{out.get('oracle_hours_online')}h")
    return True, "synced"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m orchard_chia.datalayer sync-oracle",
        description="Post chain-sealed attestations the oracle is missing.")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be synced and write nothing")
    ap.add_argument("--node", help="limit to one node id")
    ap.add_argument("--season", type=int, help="limit to one season")
    args = ap.parse_args(argv if argv is not None else [])

    try:
        cfg = config.load()
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not cfg.data_layer.store_id:
        print("error: datalayer.store_id is not configured", file=sys.stderr)
        return 2

    token = (os.environ.get("ORCHARD_ORACLE_WRITER_TOKEN", "").strip()
             or (cfg.oracle.writer_token or "").strip() or None)
    oracle = OracleClient(cfg.oracle.url, token)
    try:
        nodes = oracle.list_nodes()
    except OracleError as e:
        print(f"error: oracle unreachable: {e}", file=sys.stderr)
        return 2

    rpc = DataLayerRpc(cfg.data_layer.host, cfg.data_layer.port,
                       cfg.data_layer.cert_path, cfg.data_layer.key_path)

    synced = failed = already = 0
    for node in nodes:
        node_id = str(node.get("node_id") or "").upper()
        if not node_id or (args.node and node_id != args.node.upper()):
            continue
        try:
            on_chain = _sealed_seasons(rpc, cfg.data_layer.store_id, node_id)
        except ChiaRpcError as e:
            print(f"{node_id[:12]}: store unreadable ({e})", file=sys.stderr)
            failed += 1
            continue
        if args.season is not None:
            on_chain = [s for s in on_chain if s == args.season]
        try:
            known = _known_seasons(cfg.oracle.url, node_id, token)
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"{node_id[:12]}: cannot read the oracle's attestations "
                  f"({str(e)[:90]})", file=sys.stderr)
            failed += 1
            continue

        missing = [s for s in on_chain if s not in known]
        already += len(on_chain) - len(missing)
        if not missing:
            print(f"{node_id[:12]}: in sync ({len(on_chain)} sealed season(s))")
            continue

        print(f"{node_id[:12]}: {len(missing)} sealed season(s) the oracle is "
              f"missing: {', '.join(str(s) for s in missing)}")
        for season in missing:
            key_hex = schema.attest_key(node_id, season)
            try:
                record = schema.parse_value(
                    rpc.get_value(cfg.data_layer.store_id, key_hex))
            except (ChiaRpcError, fetch.FetchError) as e:
                print(f"    season {season}: unreadable ({str(e)[:80]})",
                      file=sys.stderr)
                failed += 1
                continue
            if not isinstance(record, dict) or "data_hash" not in record:
                print(f"    season {season}: not an attestation record — skipped",
                      file=sys.stderr)
                failed += 1
                continue
            if args.dry_run:
                print(f"    season {season}: would sync "
                      f"({record.get('hours_online')}h)")
                continue
            ok, detail = _post(cfg.oracle.url, record, key_hex, token)
            print(f"    season {season}: {detail}")
            if ok:
                synced += 1
            else:
                failed += 1

    verb = "would sync" if args.dry_run else "synced"
    print(f"sync-oracle: {verb} {synced}, already present {already}, "
          f"failed {failed}.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
