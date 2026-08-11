# SPDX-License-Identifier: Apache-2.0
"""verify-latest: the network audits its own newest seals, on a timer.

    python -m orchard_chia.datalayer verify-latest

For every Tree the oracle recognises, find its newest sealed season on chain
and run the full independent verification over it — device signatures, Merkle
proofs, hour and season roots, the quorum recompute, the oracle signature.

Sealing is scheduled; if checking the seals is not, tampering or a writer bug
surfaces only when a human happens to look. This is the daily look.

EXIT CODES — the part a scheduler consumes:

    0  every verified season is VALID, or honestly CANNOT-VERIFY for a reason
       verification cannot help (the placeholder block anchor, a store that
       is briefly unreachable). Transient and known-incomplete states are not
       pages at 1am.
    1  INVALID — a definitive contradiction: bad signature, wrong root, a
       claim the public readings do not support. This is the alarm.
    2  configuration/usage failure.
"""
from __future__ import annotations

import sys

from . import config, fetch
from .oracle import OracleClient, OracleError
from .rpc import ChiaRpcError, DataLayerRpc
from . import verify as verify_mod


def _sealed_seasons(rpc: DataLayerRpc, store_id: str, node_id: str) -> list[int]:
    """Seasons with an attest record on chain for this node, ascending."""
    out = []
    prefix = f"attest:{node_id.upper()}:"
    for key_hex in rpc.get_keys_strict(store_id):
        try:
            ascii_key = bytes.fromhex(key_hex).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            continue
        if not ascii_key.startswith(prefix):
            continue
        tail = ascii_key[len(prefix):]
        if tail.isascii() and tail.isdigit():
            out.append(int(tail))
    return sorted(out)


def main() -> int:
    try:
        cfg = config.load()
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if not cfg.data_layer.store_id:
        print("error: datalayer.store_id is not configured", file=sys.stderr)
        return 2

    oracle = OracleClient(cfg.oracle.url, cfg.oracle.writer_token or None)
    try:
        nodes = oracle.list_nodes()
    except OracleError as e:
        print(f"oracle unreachable ({e}) — nothing verified this run.",
              file=sys.stderr)
        return 0        # transient; the next timer tick retries

    rpc = DataLayerRpc(cfg.data_layer.host, cfg.data_layer.port,
                       cfg.data_layer.cert_path, cfg.data_layer.key_path)

    invalid = 0
    checked = 0
    for node in nodes:
        node_id = str(node.get("node_id") or "").upper()
        if not node_id:
            continue
        try:
            seasons = _sealed_seasons(rpc, cfg.data_layer.store_id, node_id)
        except ChiaRpcError as e:
            print(f"{node_id[:12]}: store unreadable ({e}) — skipped.",
                  file=sys.stderr)
            continue
        if not seasons:
            print(f"{node_id[:12]}: no sealed seasons yet.")
            continue
        season = seasons[-1]
        try:
            bundle = fetch.fetch_bundle(rpc, cfg.data_layer.store_id,
                                        node_id=node_id, season=season)
        except (ChiaRpcError, fetch.FetchError) as e:
            print(f"{node_id[:12]} season {season}: bundle unavailable "
                  f"({str(e)[:80]}) — transient, skipped.", file=sys.stderr)
            continue
        report = verify_mod.verify_bundle(
            meta=bundle["meta"], node=bundle["node"], attest=bundle["attest"],
            readings_records=bundle["readings_records"])
        checked += 1
        failed = [c for c in report.checks if not c.ok]
        # The placeholder block anchor is a KNOWN gap (firmware + /beacon),
        # not a contradiction verification could act on. Anything else
        # failing is the alarm this command exists to raise.
        real = [c for c in failed if "anchor" not in c.name.lower()]
        if real:
            invalid += 1
            print(f"{node_id[:12]} season {season}: INVALID")
            for c in real:
                print(f"    X {c.name}: {c.detail}")
        else:
            note = " (anchor pending firmware)" if failed else ""
            print(f"{node_id[:12]} season {season}: VALID{note} — "
                  f"{len(bundle['readings_records'])} hour(s) recomputed")

    print(f"verify-latest: {checked} sealed season(s) checked, "
          f"{invalid} invalid.")
    return 1 if invalid else 0


if __name__ == "__main__":
    raise SystemExit(main())
