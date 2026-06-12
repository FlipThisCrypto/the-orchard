# SPDX-License-Identifier: Apache-2.0
"""orchard-verify — verify an Orchard reading bundle without trusting the oracle.

    python -m orchard_chia.cli.orchard_verify vectors <path-to-vectors.json>
    python -m orchard_chia.cli.orchard_verify live \\
        --store-id <ID> --node-id <ID> --season 42 --hour 13

Phase 1 (now): offline verification against the golden ``vectors.json``.
Phase 2 (stub): live DataLayer verification — interface frozen, not yet wired.

Exit codes:
    0  VALID    — every check passed
    1  INVALID  — a check failed (tampering, bad signature, wrong score …)
    2  CANNOT   — couldn't verify (live not wired, file missing/malformed, usage)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..datalayer import schema, verify


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
    print("Live DataLayer verification is not wired yet.")
    print("Next step: fetch node, readings, attest, latest, and the DataLayer "
          "inclusion proof for")
    print(f"  store={args.store_id} node={args.node_id} "
          f"season={int(args.season):08d} hour={int(args.hour):02d}")
    print("then run the same checks as `vectors`, plus on-chain get_proof (SPEC §7).")
    return 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="orchard-verify",
        description="Verify Orchard DataLayer reading bundles — trust no oracle.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("vectors", help="verify the offline golden-vectors bundle")
    v.add_argument("path", help="path to vectors.json")
    v.set_defaults(func=cmd_vectors)

    live = sub.add_parser("live", help="(stub) verify a live DataLayer store")
    live.add_argument("--store-id", required=True)
    live.add_argument("--node-id", required=True)
    live.add_argument("--season", required=True, type=int)
    live.add_argument("--hour", required=True, type=int)
    live.set_defaults(func=cmd_live)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
