# SPDX-License-Identifier: Apache-2.0
"""Mint the Orchard Pass genesis batch via Chia wallet RPC.

Reads a YAML "mint plan" describing each Pass (uris, hashes, edition
number) and calls ``nft_mint_nft`` on the operator's wallet daemon for
each entry. Writes per-mint results to ``nft/mint_results.json``.

The mint plan keeps the user-supplied content (URIs, hashes) separate
from the local metadata JSON files — so you can iterate on hosting
without re-running the generator.

Example plan: ``nft/mint_plan.example.yaml``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..wallet.rpc import WalletRpc, WalletRpcError


@dataclass
class MintPlanEntry:
    edition_number: int
    metadata_file: str   # relative to plan file directory
    data_uris: list[str]
    data_hash: str
    meta_uris: list[str]
    meta_hash: str
    license_uris: list[str]
    license_hash: str


@dataclass
class MintPlan:
    collection_id: str
    target_address: str
    royalty_address: str
    royalty_percentage: int
    edition_total: int
    fee_mojos: int
    passes: list[MintPlanEntry]


def load_plan(path: str | Path) -> MintPlan:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    passes = []
    for entry in raw.get("passes", []):
        passes.append(MintPlanEntry(
            edition_number=int(entry["edition_number"]),
            metadata_file=entry["metadata_file"],
            data_uris=list(entry.get("data_uris", []) or []),
            data_hash=str(entry.get("data_hash") or "").lower(),
            meta_uris=list(entry.get("meta_uris", []) or []),
            meta_hash=str(entry.get("meta_hash") or "").lower(),
            license_uris=list(entry.get("license_uris", []) or []),
            license_hash=str(entry.get("license_hash") or "").lower(),
        ))
    return MintPlan(
        collection_id=raw["collection_id"],
        target_address=raw["target_address"],
        royalty_address=raw["royalty_address"],
        royalty_percentage=int(raw.get("royalty_percentage", 0)),
        edition_total=int(raw.get("edition_total", len(passes))),
        fee_mojos=int(raw.get("fee_mojos", 0)),
        passes=passes,
    )


def validate_plan(plan: MintPlan, plan_path: Path | None = None) -> list[str]:
    """Return a list of human-readable problems with the plan. Empty
    list = plan is mintable. Caller decides whether to proceed.
    """
    problems: list[str] = []
    if not plan.target_address.startswith("xch"):
        problems.append("target_address must start with 'xch'")
    if not plan.royalty_address.startswith("xch"):
        problems.append("royalty_address must start with 'xch'")
    seen: set[int] = set()
    for e in plan.passes:
        if e.edition_number in seen:
            problems.append(f"duplicate edition_number {e.edition_number}")
        seen.add(e.edition_number)
        if not e.data_uris:
            problems.append(f"pass {e.edition_number}: data_uris is empty")
        if len(e.data_hash) != 64:
            problems.append(f"pass {e.edition_number}: data_hash should be 64 hex chars")
        if not e.meta_uris:
            problems.append(f"pass {e.edition_number}: meta_uris is empty")
        if len(e.meta_hash) != 64:
            problems.append(f"pass {e.edition_number}: meta_hash should be 64 hex chars")
        if plan_path is not None:
            metadata_path = (plan_path.parent / e.metadata_file).resolve()
            if not metadata_path.exists():
                problems.append(
                    f"pass {e.edition_number}: metadata_file not found: {metadata_path}")
    return problems


def mint_pass(rpc: WalletRpc, nft_wallet_id: int, plan: MintPlan,
              entry: MintPlanEntry) -> dict:
    """Mint a single Pass. Returns the wallet RPC's response."""
    return rpc.nft_mint_nft(
        wallet_id=nft_wallet_id,
        target_address=plan.target_address,
        royalty_address=plan.royalty_address,
        uris=entry.data_uris,
        meta_uris=entry.meta_uris,
        license_uris=entry.license_uris,
        hash=entry.data_hash,
        meta_hash=entry.meta_hash,
        license_hash=entry.license_hash,
        edition_number=entry.edition_number,
        edition_total=plan.edition_total,
        royalty_percentage=plan.royalty_percentage,
        fee=plan.fee_mojos,
    )


def mint_batch(rpc: WalletRpc, plan: MintPlan,
               results_path: str | Path,
               delay_seconds: int = 90) -> dict:
    """Mint each pass via individual ``nft_mint_nft`` calls with a
    delay between them.

    DID-bound NFT wallets re-spend the underlying DID coin on every
    mint. The replacement coin doesn't become spendable until the
    previous spend confirms — so back-to-back mints fail with
    ``DID is not currently spendable``. ``delay_seconds`` waits long
    enough between mints for that confirmation (~60-90s typical on
    Chia mainnet).
    """
    nft_wallet_id = rpc.first_nft_wallet_id()
    sorted_passes = sorted(plan.passes, key=lambda e: e.edition_number)
    print(f"[orchard.nft.mint] NFT wallet_id: {nft_wallet_id}")
    print(f"[orchard.nft.mint] minting {len(sorted_passes)} pass(es) "
          f"as individual nft_mint_nft calls "
          f"with {delay_seconds}s between each...")

    results: list[dict] = []
    bulk_response: dict = {}
    for idx, entry in enumerate(sorted_passes):
        print(f"  + pass {entry.edition_number}/{plan.edition_total} -> "
              f"{entry.data_uris[0] if entry.data_uris else '?'}")
        try:
            resp = rpc.nft_mint_nft(
                wallet_id=nft_wallet_id,
                target_address=plan.target_address,
                royalty_address=plan.royalty_address,
                uris=entry.data_uris,
                meta_uris=entry.meta_uris,
                license_uris=entry.license_uris,
                hash=entry.data_hash,
                meta_hash=entry.meta_hash,
                license_hash=entry.license_hash,
                edition_number=entry.edition_number,
                edition_total=plan.edition_total,
                royalty_percentage=plan.royalty_percentage,
                fee=plan.fee_mojos,
            )
            tx_id = (resp.get("transaction_id")
                     or resp.get("tx_id")
                     or resp.get("spend_bundle", {}).get("name", ""))
            print(f"    -> tx_id={tx_id}")
            results.append({
                "edition_number": entry.edition_number,
                "ok": True,
                "wallet_response": resp,
            })
        except WalletRpcError as e:
            print(f"    ! FAILED: {e}")
            results.append({
                "edition_number": entry.edition_number,
                "ok": False,
                "error": str(e),
            })

        # Wait between mints so the DID coin has time to confirm and
        # become spendable again. Skip after the final mint.
        if idx < len(sorted_passes) - 1 and delay_seconds > 0:
            print(f"    (waiting {delay_seconds}s for DID confirm before next mint)")
            time.sleep(delay_seconds)

    summary = {
        "collection_id": plan.collection_id,
        "edition_total": plan.edition_total,
        "minted_ok": sum(1 for r in results if r["ok"]),
        "minted_failed": sum(1 for r in results if not r["ok"]),
        "bulk_response": bulk_response,
        "results": results,
    }
    Path(results_path).write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[orchard.nft.mint] results written to {results_path}")
    print(f"[orchard.nft.mint] ok={summary['minted_ok']} failed={summary['minted_failed']}")
    return summary
