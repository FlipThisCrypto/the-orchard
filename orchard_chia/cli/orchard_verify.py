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


def _print_report(
    rep: verify.Report, *, as_json: bool = False, result_label: str | None = None
) -> None:
    if as_json:
        import json
        d = rep.as_dict()
        # Expose the tri-state verdict so automation can tell CANNOT-VERIFY
        # (retry) from INVALID (fraud); `valid` alone collapses both to false.
        d["result"] = result_label or ("VALID" if rep.valid else "INVALID")
        print(json.dumps(d, indent=2, sort_keys=True))
        return
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
    label = result_label or ("VALID" if rep.valid else "INVALID")
    print(f"Result: {label}")


INCLUSION_CHECK_NAME = "DataLayer inclusion proof"


def _live_exit_code(rep: verify.Report, incl: inclusion.InclusionReport) -> int:
    """Map a live verification to an exit code, distinguishing cannot-verify.

    0 VALID, 1 INVALID (a definitive contradiction — tampering), 2 CANNOT
    (transient/unprovable: RPC down, root not yet confirmed, key not published,
    proof stale). Only collapse to INVALID when something is *definitively*
    wrong: an offline check failed (bad signature / Merkle / score / oracle sig
    / anchor), or the inclusion failure was a value mismatch (cannot_verify
    False). An inclusion that merely couldn't be established is exit 2.
    """
    if rep.valid:
        return 0
    offline_bad = any(
        not c.ok for c in rep.checks if c.name != INCLUSION_CHECK_NAME
    )
    if not offline_bad and incl.cannot_verify:
        return 2
    return 1


def _bundle_proof_pairs(bundle: dict, node_id: str, season: int) -> dict[str, str]:
    """Map every key the verdict trusts → its canonical value hex, for a live
    inclusion + value bind.

    Covers meta:schema (oracle season pubkey, checks §7.7), node:<id> (device
    pubkey, §7.2), attest:<id>:<season> (the reward anchor, §7.5-6), and each
    readings hour present (§7.3). Proving only readings would leave the pubkeys
    and the reward summary unbound — a tampered mirror could swap them while
    serving a valid readings proof.
    """
    pairs: dict[str, str] = {
        schema.meta_key(): schema.value_hex(bundle["meta"]),
        schema.node_key(node_id): schema.value_hex(bundle["node"]),
        schema.attest_key(node_id, season): schema.value_hex(bundle["attest"]),
    }
    for r in bundle.get("readings_records", []):
        if "hour" in r:
            pairs[schema.readings_key(node_id, season, int(r["hour"]))] = (
                schema.value_hex(r)
            )
    return pairs


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
    _print_report(rep, as_json=bool(getattr(args, "json", False)))
    return 0 if rep.valid else 1


def cmd_live(args: argparse.Namespace) -> int:
    """Fetch a bundle from DataLayer RPC and run the same offline checks, plus a
    live on-chain inclusion check (SPEC §7 check 1) prepended as a Check.

    The inclusion check requires a confirmed store root (get_root.confirmed),
    a get_proof covering every readings key, verify_proof's current_root, and a
    value-hash bind of each key to the readings record being verified.
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

    # SPEC §7.1 — DataLayer inclusion / permanence (RPC-level). Prove and
    # value-bind every key the verdict trusts: meta (oracle pubkey), node
    # (device pubkey), attest (reward anchor), and each readings hour.
    proof_pairs = _bundle_proof_pairs(bundle, args.node_id, season_n)
    incl = inclusion.check_inclusion(
        rpc, store_id, list(proof_pairs), expected_values=proof_pairs
    )
    rep.checks.insert(
        0,
        verify.Check(
            INCLUSION_CHECK_NAME,
            incl.ok,
            incl.detail,
        ),
    )

    code = _live_exit_code(rep, incl)
    label = {0: "VALID", 1: "INVALID", 2: "CANNOT-VERIFY"}[code]
    _print_report(rep, as_json=bool(getattr(args, "json", False)), result_label=label)
    return code


def _fetch_and_verify_reading(
    rpc, store_id: str, node_id: str, season: int, hour: int, ts_ms: int
) -> tuple[verify.ReadingCheck | None, str | None]:
    """Fetch the node pubkey + hour record and verify one reading by ts_ms.

    Returns (ReadingCheck, None) on success or (None, error) if the node/hour/
    reading can't be found. Pure over the RPC interface (testable with a fake).
    """
    node = schema.parse_value(rpc.get_value(store_id, schema.node_key(node_id)))
    if not node:
        return None, f"node:{node_id} not found in store"
    rec = schema.parse_value(
        rpc.get_value(store_id, schema.readings_key(node_id, season, hour))
    )
    if not rec:
        return None, f"readings hour {hour:02d} not found for season {season}"
    match = next(
        (r for r in rec.get("readings", []) if int(r.get("ts_ms", -1)) == int(ts_ms)),
        None,
    )
    if match is None:
        return None, f"no reading with ts_ms={ts_ms} in hour {hour:02d}"
    return verify.verify_reading_in_hour(match, node.get("pubkey", ""), rec), None


def cmd_reading(args: argparse.Namespace) -> int:
    """Verify a single reading (SPEC §8) from the live store by ts_ms."""
    try:
        cfg = config.load()
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    store_id = args.store_id or cfg.data_layer.store_id
    if not store_id:
        print("error: --store-id required (or set datalayer.store_id)", file=sys.stderr)
        return 2

    from ..datalayer.parse import parse_hour, parse_season

    try:
        season_n = parse_season(args.season)
        hour_n = parse_hour(args.hour)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    rpc = DataLayerRpc(
        cfg.data_layer.host, cfg.data_layer.port,
        cfg.data_layer.cert_path, cfg.data_layer.key_path,
    )
    try:
        check, err = _fetch_and_verify_reading(
            rpc, store_id, args.node_id, season_n, hour_n, int(args.ts_ms)
        )
    except ChiaRpcError as e:
        print(f"error: DataLayer RPC failed: {e}", file=sys.stderr)
        return 2
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 2

    if bool(getattr(args, "json", False)):
        print(json.dumps(check.as_dict(), indent=2, sort_keys=True))
    else:
        ok, fail = _marks()
        print("Orchard Verify — single reading\n")
        print(f"{ok if check.signature_ok else fail} Device signature")
        print(f"{ok if check.in_hour_tree else fail} Merkle membership in hour tree")
        print(f"{ok if check.hour_root_ok else fail} Hour root matches recompute")
        print(f"\nResult: {'VALID' if check.ok else 'INVALID'}")
    return 0 if check.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orchard-verify",
        description="Verify Orchard DataLayer reading bundles — trust no oracle.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("vectors", help="verify the offline golden-vectors bundle")
    v.add_argument("path", help="path to vectors.json")
    v.add_argument("--json", action="store_true", help="emit Report.as_dict JSON")
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
    live.add_argument("--json", action="store_true", help="emit Report.as_dict JSON")
    live.set_defaults(func=cmd_live)

    rd = sub.add_parser(
        "reading",
        help="verify a single reading (by ts_ms) from the live store (SPEC §8)",
    )
    rd.add_argument("--store-id", default=None,
                    help="DataLayer store id (default: config.yaml datalayer.store_id)")
    rd.add_argument("--node-id", required=True)
    rd.add_argument("--season", required=True, type=int)
    rd.add_argument("--hour", required=True, type=int)
    rd.add_argument("--ts-ms", required=True, type=int, dest="ts_ms",
                    help="device epoch-millis timestamp identifying the reading")
    rd.add_argument("--json", action="store_true", help="emit ReadingCheck JSON")
    rd.set_defaults(func=cmd_reading)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
