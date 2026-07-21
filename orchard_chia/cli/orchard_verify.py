# SPDX-License-Identifier: Apache-2.0
"""orchard-verify — verify an Orchard reading bundle without trusting the oracle.

    python -m orchard_chia.cli.orchard_verify vectors <path-to-vectors.json>
    python -m orchard_chia.cli.orchard_verify live \\
        --store-id <ID> --node-id <ID> --season 42 [--hour 13]

Exit codes:
    0  VALID    — every check passed
    1  INVALID  — a check failed (tampering, bad signature, wrong score …)
    2  CANNOT   — couldn't verify (RPC down, missing keys, file malformed, usage)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..datalayer import config, fetch, inclusion, schema, verify
from ..datalayer.rpc import ChiaRpcError, DataLayerRpc


def _marks() -> tuple[str, str]:
    """✓ / ✗ when the terminal can encode them, else ASCII fallbacks."""
    ok, fail = "✓", "✗"
    enc = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        ok.encode(enc)
        fail.encode(enc)
    except (UnicodeEncodeError, LookupError):
        ok, fail = "OK", " X"
    return ok, fail


def _print_report(rep: verify.Report) -> None:
    ok, fail = _marks()
    print("Orchard Verify\n")
    print(f"Schema: orchard.datalayer v{schema.SCHEMA_VERSION}")
    print(f"Node:   {rep.node_id}")
    print(f"Season: {rep.season:08d}")
    if rep.hours:
        print(f"Hours:  {', '.join(f'{h:02d}' for h in rep.hours)}")
    print()
    for c in rep.checks:
        line = f"{ok if c.ok else fail} {c.name}"
        if c.detail:
            line += f"  ({c.detail})"
        print(line)
    print()
    print(f"Result: {'VALID' if rep.valid else 'INVALID'}")


def _load_vectors_bundle(path: Path) -> dict:
    rec = json.loads(path.read_text(encoding="utf-8"))["records"]
    return {
        "meta": rec["meta"],
        "node": rec["node"],
        "attest": rec["attest"],
        "readings_records": [rec["readings"]],
    }


def cmd_vectors(args: argparse.Namespace) -> int:
    path = Path(args.path)
    if not path.exists():
        print(f"error: vectors file not found: {path}", file=sys.stderr)
        return 2
    try:
        bundle = _load_vectors_bundle(path)
    except (json.JSONDecodeError, KeyError, OSError) as e:
        print(f"error: malformed vectors file: {e}", file=sys.stderr)
        return 2
    rep = verify.verify_bundle(**bundle)
    _print_report(rep)
    return 0 if rep.valid else 1


def cmd_live(args: argparse.Namespace) -> int:
    """Fetch a bundle from DataLayer RPC and run the same offline checks.

    On-chain inclusion proof (get_proof vs store root) is a separate SPEC §7
    step — when the RPC supports it we will add it as an extra Check.
    """
    try:
        cfg = config.load()
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    store_id = args.store_id or cfg.data_layer.store_id
    if not store_id:
        print("error: --store-id required (or set datalayer.store_id in config)",
              file=sys.stderr)
        return 2

    from ..datalayer.parse import parse_hour, parse_season

    try:
        season_n = parse_season(args.season)
        hours = [parse_hour(args.hour)] if args.hour is not None else None
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    rpc = DataLayerRpc(
        cfg.data_layer.host,
        cfg.data_layer.port,
        cfg.data_layer.cert_path,
        cfg.data_layer.key_path,
    )
    try:
        bundle = fetch.fetch_bundle(
            rpc,
            store_id,
            node_id=args.node_id,
            season=season_n,
            hours=hours,
        )
    except fetch.FetchError as e:
        print(f"error: cannot assemble bundle: {e}", file=sys.stderr)
        return 2
    except ChiaRpcError as e:
        print(f"error: DataLayer RPC failed: {e}", file=sys.stderr)
        return 2

    print(
        f"[live] store={store_id[:16]}… node={args.node_id[:8]}… "
        f"season={season_n} hours="
        f"{hours if hours is not None else 'auto'}"
    )
    rep = verify.verify_bundle(**bundle)

    # SPEC §7.1 — DataLayer inclusion / permanence (RPC-level).
    proof_keys = [
        schema.readings_key(args.node_id, season_n, int(h))
        for h in rep.hours
    ]
    if not proof_keys and bundle.get("readings_records"):
        proof_keys = [
            schema.readings_key(
                args.node_id, season_n, int(r["hour"])
            )
            for r in bundle["readings_records"]
        ]
    incl = inclusion.check_inclusion(rpc, store_id, proof_keys)
    rep.checks.insert(
        0,
        verify.Check(
            "DataLayer inclusion proof",
            incl.ok,
            incl.detail,
        ),
    )

    _print_report(rep)
    return 0 if rep.valid else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orchard-verify",
        description="Verify Orchard DataLayer reading bundles — trust no oracle.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("vectors", help="verify the offline golden-vectors bundle")
    v.add_argument("path", help="path to vectors.json")
    v.set_defaults(func=cmd_vectors)

    live = sub.add_parser(
        "live",
        help="fetch a store via DataLayer RPC and verify the season bundle",
    )
    live.add_argument(
        "--store-id",
        default=None,
        help="DataLayer store id (default: config.yaml datalayer.store_id)",
    )
    live.add_argument("--node-id", required=True)
    live.add_argument("--season", required=True, type=int)
    live.add_argument(
        "--hour",
        type=int,
        default=None,
        help="single hour 0-23; omit to discover all hours present in the store",
    )
    live.set_defaults(func=cmd_live)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
