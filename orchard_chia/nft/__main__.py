# SPDX-License-Identifier: Apache-2.0
"""Orchard Pass NFT CLI.

Subcommands:
  generate  — write 10 stub metadata files into nft/metadata/
  validate  — sanity-check a mint plan YAML
  mint      — mint the genesis batch from a plan
  verify    — check an NFT wallet for Pass ownership

Run:
  python -m orchard_chia.nft generate
  python -m orchard_chia.nft validate --plan nft/mint_plan.yaml
  python -m orchard_chia.nft mint     --plan nft/mint_plan.yaml
  python -m orchard_chia.nft verify
  python -m orchard_chia.nft verify --address xch1...
  python -m orchard_chia.nft verify --wallet-id 4
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import generate, mint, verify
from ..datalayer import config as base_config
from ..wallet.rpc import WalletRpc, WalletRpcError


def _operator_address_from_config() -> str | None:
    """Read the operator's Pass-holding XCH address from config.yaml's
    ``operator.pass_address`` field, if set. Returns None if absent —
    caller decides whether that's an error."""
    import yaml
    try:
        raw = yaml.safe_load(
            base_config.CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return None
    op = raw.get("operator") or {}
    addr = (op.get("pass_address") or "").strip()
    return addr or None


REPO_ROOT = Path(__file__).resolve().parents[2]
NFT_DIR = REPO_ROOT / "nft"


def _wallet_rpc_from_config() -> WalletRpc:
    base_config.load()  # fail fast, with a helpful error, if config.yaml is absent
    # The base config has full_node + datalayer creds; for wallet we
    # need a separate section (added in config.example.yaml).
    import yaml
    raw = yaml.safe_load(base_config.CONFIG_PATH.read_text(encoding="utf-8")) or {}
    w = raw.get("wallet") or {}
    return WalletRpc(
        host=w.get("host", "127.0.0.1"),
        port=int(w.get("port", 9256)),
        cert_path=base_config._expand(w.get("cert_path", "")),
        key_path=base_config._expand(w.get("key_path", "")),
        fingerprint=int(w.get("fingerprint", 0)),
    )


def _cmd_generate(args) -> int:
    target = NFT_DIR / "metadata"
    written = generate.write_genesis_batch(target, total=args.total)
    # Also write the collection-level metadata.
    coll = generate.build_collection_metadata()
    coll_path = NFT_DIR / "collection.json"
    coll_path.write_text(json.dumps(coll, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(written)} metadata files to {target}")
    print(f"wrote collection metadata to {coll_path}")
    return 0


def _cmd_validate(args) -> int:
    plan_path = Path(args.plan).resolve()
    plan = mint.load_plan(plan_path)
    problems = mint.validate_plan(plan, plan_path=plan_path)
    if problems:
        print(f"plan has {len(problems)} problem(s):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"plan OK: {len(plan.passes)} passes, edition_total={plan.edition_total}")
    return 0


def _parse_editions(spec: str | None) -> set[int] | None:
    """Parse '2-10' / '1,3,5' / '7' into a set of edition numbers,
    or None if spec is empty (= mint everything)."""
    if not spec:
        return None
    out: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo, hi = part.split("-", 1)
            for n in range(int(lo), int(hi) + 1):
                out.add(n)
        elif part:
            out.add(int(part))
    return out


def _cmd_mint(args) -> int:
    plan_path = Path(args.plan).resolve()
    plan = mint.load_plan(plan_path)

    selected = _parse_editions(args.editions)
    if selected is not None:
        kept = [p for p in plan.passes if p.edition_number in selected]
        if not kept:
            print(f"--editions {args.editions!r} selected zero passes from the plan",
                  file=sys.stderr)
            return 1
        plan = mint.MintPlan(
            collection_id=plan.collection_id,
            target_address=plan.target_address,
            royalty_address=plan.royalty_address,
            royalty_percentage=plan.royalty_percentage,
            edition_total=plan.edition_total,
            fee_mojos=plan.fee_mojos,
            passes=kept,
        )

    problems = mint.validate_plan(plan, plan_path=plan_path)
    if problems and not args.force:
        print("plan has problems; refusing to mint. Pass --force to mint anyway.")
        for p in problems:
            print(f"  - {p}")
        return 1

    rpc = _wallet_rpc_from_config()
    results_path = plan_path.parent / "mint_results.json"
    summary = mint.mint_batch(rpc, plan, results_path,
                              delay_seconds=args.delay_seconds)
    return 0 if summary["minted_failed"] == 0 else 2


def _cmd_verify(args) -> int:
    # Resolve which path to use:
    #   --wallet-id N  -> local wallet RPC (one specific NFT wallet)
    #   --address X    -> MintGarden indexer (any XCH address)
    #   neither        -> indexer using config.operator.pass_address
    if args.wallet_id is not None and args.address:
        print("specify only one of --wallet-id or --address", file=sys.stderr)
        return 1

    if args.wallet_id is not None:
        rpc = _wallet_rpc_from_config()
        try:
            passes = verify.list_owned_passes(rpc, nft_wallet_id=args.wallet_id)
        except WalletRpcError as e:
            print(f"verify failed (wallet RPC): {e}", file=sys.stderr)
            return 3
        label = f"wallet_id={args.wallet_id}"
    else:
        address = args.address or _operator_address_from_config()
        if not address:
            print(
                "no address to check. Pass --address xch1... or set "
                "operator.pass_address in orchard_chia/config.yaml.",
                file=sys.stderr,
            )
            return 1
        try:
            passes = verify.list_passes_by_address(address)
        except verify.IndexerError as e:
            print(f"verify failed (indexer): {e}", file=sys.stderr)
            return 3
        label = f"address={address}"

    print(f"{label} holds {len(passes)} Orchard Pass(es)")
    for p in passes:
        nft_id = p.get("nft_coin_id") or p.get("launcher_id") or "<unknown>"
        name = p.get("name") or ""
        edition = p.get("edition_number") or "?"
        suffix = f"  {name}" if name else ""
        print(f"  - nft_id={nft_id} edition={edition}{suffix}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchard_chia.nft")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_gen = sub.add_parser("generate", help="write stub metadata files")
    p_gen.add_argument("--total", type=int, default=generate.GENESIS_TOTAL)
    p_gen.set_defaults(func=_cmd_generate)

    p_val = sub.add_parser("validate", help="sanity-check a mint plan")
    p_val.add_argument("--plan", default="nft/mint_plan.yaml")
    p_val.set_defaults(func=_cmd_validate)

    p_mint = sub.add_parser("mint", help="mint from a plan")
    p_mint.add_argument("--plan", default="nft/mint_plan.yaml")
    p_mint.add_argument("--force", action="store_true",
                        help="mint even if validate() reports problems")
    p_mint.add_argument("--editions", default=None,
                        help="filter passes by edition_number — '2-10', "
                             "'1,3,5', '7'. Defaults to all passes in the plan.")
    p_mint.add_argument("--delay-seconds", type=int, default=90,
                        help="seconds to wait between individual mints "
                             "(default 90; needed for DID-bound NFT wallets).")
    p_mint.set_defaults(func=_cmd_mint)

    p_ver = sub.add_parser(
        "verify",
        help="check Pass ownership — by address (chain indexer) or by "
             "local NFT wallet id (Chia wallet RPC)")
    p_ver.add_argument("--wallet-id", type=int, default=None,
                       help="check a specific NFT wallet via local "
                            "Chia wallet RPC. The Pass must be in a key "
                            "the running wallet daemon controls.")
    p_ver.add_argument("--address", default=None,
                       help="check a specific xch1... address via the "
                            "MintGarden indexer API. Works for any "
                            "address regardless of local wallet state. "
                            "Defaults to operator.pass_address in "
                            "config.yaml if neither flag is given.")
    p_ver.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
