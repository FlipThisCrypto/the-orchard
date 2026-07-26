# SPDX-License-Identifier: Apache-2.0
"""Season harvest — orchestrator.

Reads every signed attestation from the Chia DataLayer store, verifies
each signature with the oracle's signing key, computes per-Tree
rewards, aggregates per wallet, and (in live mode) sends $JUICE via
``cat_spend``. Idempotent via a local watermark SQLite that records
every ``(node, season)`` already paid.

Run:
    python -m orchard_chia.payout                # dry-run (default)
    python -m orchard_chia.payout --confirm      # interactive prompt
    python -m orchard_chia.payout --yes          # spend without prompt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml

# Recipient sanity-check at the spend boundary (defense in depth: the
# oracle already validates wallet_address at registration).
_XCH_ADDR = re.compile(r"^xch1[0-9a-z]{50,80}$")

from .. import datalayer as dl_pkg  # type: ignore  # noqa: F401
from ..datalayer import attest, config as base_config, exit_codes
from ..datalayer.oracle import OracleClient, OracleError
from ..datalayer.rpc import ChiaRpcError, DataLayerRpc
from ..wallet.rpc import WalletRpc, WalletRpcError
from . import calculator, reader, watermark


WATERMARK_DEFAULT_PATH = (
    Path(base_config.CONFIG_PATH).parent / "data" / "payout_watermark.db"
)


# ---------------------------------------------------------------------
# Config helpers (additions to the datalayer config — wallet + token)
# ---------------------------------------------------------------------

def _load_raw_config() -> dict:
    return yaml.safe_load(base_config.CONFIG_PATH.read_text(encoding="utf-8")) or {}


def _wallet_rpc() -> WalletRpc:
    raw = _load_raw_config()
    w = raw.get("wallet") or {}
    return WalletRpc(
        host=w.get("host", "127.0.0.1"),
        port=int(w.get("port", 9256)),
        cert_path=base_config._expand(w.get("cert_path", "")),
        key_path=base_config._expand(w.get("key_path", "")),
        fingerprint=int(w.get("fingerprint", 0)),
    )


def _token_asset_id() -> str:
    raw = _load_raw_config()
    return ((raw.get("token") or {}).get("asset_id") or "").lower().replace("0x", "")


def _daily_rate() -> float:
    raw = _load_raw_config()
    return float((raw.get("reward") or {}).get("daily_rate", 1.0))


# ---------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------

def _attestations_to_plan(
    attestations: list[reader.StoredAttestation],
    *,
    oracle: OracleClient,
    signing_key_hex: str,
    daily_rate: float,
    wm: watermark.Watermark,
) -> list[dict]:
    """Per-Tree, per-Season reward intent. One row per attestation,
    after signature verification and watermark filtering."""
    plan: list[dict] = []
    node_cache: dict[str, dict | None] = {}

    for s in attestations:
        # 1. Verify oracle_sig. Prefer public secp256r1 (ADR-0003 / schema);
        # fall back to legacy HMAC for pre-migration attestations still on
        # chain so operators aren't blocked mid-cutover.
        from ..datalayer import schema as dl_schema
        season_pub = dl_schema.pubkey_for_seed(signing_key_hex.lower())
        sig_ok = dl_schema.verify_attest(s.signed, season_pub)
        if not sig_ok:
            sig_ok = attest.verify_signature(s.signed, signing_key_hex)
        if not sig_ok:
            plan.append({
                "node_id":   s.node_id,
                "season":    s.season,
                "status":    "skipped:bad_sig",
                "hours":     s.signed.get("hours_online"),
                "mojos":     0,
            })
            continue

        # 2. Skip already-paid.
        if wm.is_paid(s.node_id, s.season):
            plan.append({
                "node_id":   s.node_id,
                "season":    s.season,
                "status":    "skipped:already_paid",
                "hours":     s.signed.get("hours_online"),
                "mojos":     wm.get_paid_amount(s.node_id, s.season) or 0,
            })
            continue

        # 3. Look up the Tree's wallet address from the oracle.
        node = node_cache.get(s.node_id)
        if node is None:
            try:
                node = oracle.get_node(s.node_id) or {}
            except OracleError:
                node = {}
            node_cache[s.node_id] = node
        wallet_address = (node or {}).get("wallet_address") or ""

        if not wallet_address:
            plan.append({
                "node_id":   s.node_id,
                "season":    s.season,
                "status":    "skipped:no_wallet",
                "hours":     s.signed.get("hours_online"),
                "mojos":     0,
            })
            continue

        # 4. Compute reward on the verifiable metric (verified_hours when
        #    present), and record the hours actually paid on — not a claim.
        #    One malformed record must not abort the whole run: every other
        #    failure mode in this loop appends a skipped:* row, so do the same
        #    here instead of letting a bad value propagate out and kill the
        #    payout for every node.
        try:
            hours_paid, basis = calculator.paid_hours(s.signed)
            mojos = calculator.juice_mojos_for_attestation(
                s.signed, daily_rate=daily_rate,
            )
        except (ValueError, TypeError) as e:
            plan.append({
                "node_id":  s.node_id,
                "season":   s.season,
                "status":   "skipped:invalid_attestation",
                "hours":    "?",
                "mojos":    0,
                "detail":   str(e)[:120],
            })
            continue
        plan.append({
            "node_id":         s.node_id,
            "season":          s.season,
            "wallet_address":  wallet_address,
            "hours":           hours_paid,
            "hours_basis":     basis,
            "claimed_hours":   int(s.signed.get("hours_online", 0)),
            "mojos":           mojos,
            "status":          "ready" if mojos > 0 else "skipped:zero",
        })
    return plan


def _hours_cell(p: dict) -> str:
    """Hours paid on, annotated with what the number actually rests on.

    - ``N (claim M)`` — paid the verifiable count, below the oracle's claim
      (an over-count surfaced at the payment boundary).
    - ``N (unverified)`` — the attestation declares a placeholder basis: nothing
      was published on chain, so this payment rests on the oracle's self-report,
      not on proof. Same amount as always; the operator can now SEE that.
    """
    hours = p.get("hours", "?")
    claimed = p.get("claimed_hours")
    basis = p.get("hours_basis") or ""
    if basis.startswith("hours_online (unverified)"):
        return f"{hours} (unverified)"
    if basis.startswith("unpayable"):
        return f"{hours} (unpayable)"
    if basis.startswith("verified_hours ("):
        # e.g. "(sigs unchecked)" / "(basis unrecognized)" / "(basis undeclared)"
        return f"{hours} {basis[len('verified_hours '):]}"
    if (
        basis == "verified_hours"
        and isinstance(claimed, int)
        and isinstance(hours, int)
        and claimed != hours
    ):
        return f"{hours} (claim {claimed})"
    return str(hours)


def _format_table(plan: list[dict]) -> str:
    rows = [
        ("NODE", "SEASON", "HOURS", "WALLET", "$JUICE", "STATUS"),
    ]
    for p in plan:
        rows.append((
            p["node_id"][:8] + "..",
            str(p["season"]),
            _hours_cell(p),
            (p.get("wallet_address") or "—")[:24] + ("…" if len(p.get("wallet_address") or "") > 24 else ""),
            f"{calculator.mojos_to_juice(int(p.get('mojos', 0))):.3f}",
            p["status"],
        ))
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    lines = []
    for i, row in enumerate(rows):
        lines.append("  ".join(c.ljust(widths[j]) for j, c in enumerate(row)))
        if i == 0:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def _confirm_interactive(total_recipients: int, total_juice: float) -> bool:
    print()
    print(f"About to send {total_juice:.3f} $JUICE to {total_recipients} wallet(s).")
    print("Type   PAY   to confirm, anything else to abort:")
    typed = input("> ").strip()
    return typed == "PAY"


# ---------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m orchard_chia.payout")
    parser.add_argument("--confirm", action="store_true",
                        help="prompt before sending (interactive)")
    parser.add_argument("--yes", action="store_true",
                        help="actually send, no prompt (CAUTION)")
    parser.add_argument("--fee", type=int, default=0,
                        help="XCH mojos fee per spend (default 0)")
    parser.add_argument("--memo", default="",
                        help="UTF-8 memo to attach to each spend (optional)")
    parser.add_argument("--plan-out", default=None,
                        help="write the plan as JSON to this path")
    parser.add_argument("--watermark", default=str(WATERMARK_DEFAULT_PATH),
                        help="watermark DB path")
    args = parser.parse_args(argv)

    try:
        cfg = base_config.load()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if not cfg.data_layer.store_id:
        print("ERROR: orchard_chia/config.yaml -> datalayer.store_id is empty.",
              file=sys.stderr)
        return 2

    asset_id = _token_asset_id()
    if not asset_id:
        print("ERROR: orchard_chia/config.yaml -> token.asset_id is empty.",
              file=sys.stderr)
        return 2

    daily_rate = _daily_rate()
    oracle = OracleClient(cfg.oracle.url, cfg.oracle.writer_token)
    dl = DataLayerRpc(
        cfg.data_layer.host, cfg.data_layer.port,
        cfg.data_layer.cert_path, cfg.data_layer.key_path,
    )

    print(f"[orchard.payout] oracle:    {cfg.oracle.url}")
    print(f"[orchard.payout] datalayer: {cfg.data_layer.host}:{cfg.data_layer.port}")
    print(f"[orchard.payout] store:     {cfg.data_layer.store_id}")
    print(f"[orchard.payout] asset_id:  {asset_id}")
    print(f"[orchard.payout] daily_rate:{daily_rate} $JUICE/Tree/day")

    print("[orchard.payout] reading attestations from DataLayer ...")
    try:
        attestations = reader.read_all_attestations(dl, cfg.data_layer.store_id)
    except ChiaRpcError as e:
        print(f"ERROR: DataLayer get_keys failed: {e}", file=sys.stderr)
        return 3
    print(f"[orchard.payout] {len(attestations)} attestation(s) found on chain")

    with watermark.Watermark(args.watermark) as wm:
        plan = _attestations_to_plan(
            attestations,
            oracle=oracle,
            signing_key_hex=cfg.signing_key_hex,
            daily_rate=daily_rate,
            wm=wm,
        )

        if args.plan_out:
            Path(args.plan_out).write_text(
                json.dumps(plan, indent=2), encoding="utf-8")
            print(f"[orchard.payout] plan written to {args.plan_out}")

        print()
        print(_format_table(plan))
        print()

        # Aggregate per wallet.
        ready_rows = [p for p in plan if p["status"] == "ready"]
        per_wallet = calculator.aggregate_by_wallet([
            {"wallet_address": p["wallet_address"], "mojos": p["mojos"]}
            for p in ready_rows
        ])
        total_mojos = sum(per_wallet.values())
        total_juice = calculator.mojos_to_juice(total_mojos)
        print(f"[orchard.payout] ready: {len(ready_rows)} attestation(s) "
              f"-> {len(per_wallet)} wallet(s) -> {total_juice:.3f} $JUICE total")

        if not per_wallet:
            # "nothing to send" is indistinguishable from "everyone is already
            # paid", which is exactly how a total payout failure hid before: the
            # oracle scrubs wallet_address for anyone who can't prove they are
            # the operator's writer, so an unconfigured token made EVERY node
            # resolve to no_wallet while the run exited 0. Diagnose it loudly.
            no_wallet = [p for p in plan if p["status"] == "skipped:no_wallet"]
            if no_wallet and len(no_wallet) == len([p for p in plan if p["mojos"] == 0]):
                token_set = bool((cfg.oracle.writer_token or "").strip())
                print(
                    f"[orchard.payout] ERROR: every attestation ({len(no_wallet)}) "
                    f"resolved to an empty wallet_address.",
                    file=sys.stderr,
                )
                if not token_set:
                    print(
                        "[orchard.payout] Cause: no writer token configured, so the "
                        "oracle withholds operator-private wallet_address. Set "
                        "ORCHARD_ORACLE_WRITER_TOKEN (or oracle.writer_token in "
                        "config.yaml) to the SAME value as the oracle's "
                        "ORCHARD_ORACLE_WRITER_TOKEN and re-run.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        "[orchard.payout] A writer token IS configured, so either it "
                        "does not match the oracle's, or these Trees genuinely have "
                        "no wallet bound (operator never completed registration).",
                        file=sys.stderr,
                    )
                return exit_codes.ORACLE
            print("[orchard.payout] nothing to send.")
            return 0

        # Dry-run by default.
        if not (args.confirm or args.yes):
            print("[orchard.payout] DRY RUN (re-run with --confirm or --yes "
                  "to actually send).")
            return 0

        if args.confirm and not args.yes:
            if not _confirm_interactive(len(per_wallet), total_juice):
                print("[orchard.payout] aborted by user.")
                return 1

        # Live: find the $JUICE CAT wallet, then iterate.
        rpc = _wallet_rpc()
        try:
            cat_wallet_id = rpc.find_cat_wallet_id_by_asset(asset_id)
        except WalletRpcError as e:
            print(f"ERROR: wallet RPC unreachable: {e}", file=sys.stderr)
            return 4
        if cat_wallet_id is None:
            print(f"ERROR: no CAT wallet found for asset_id {asset_id}. "
                  f"Add it once in the Chia GUI / CLI, then retry.",
                  file=sys.stderr)
            return 4

        print(f"[orchard.payout] $JUICE CAT wallet_id: {cat_wallet_id}")

        # One cat_spend per recipient. Could batch later via
        # send_transaction_multi; for v1, keeping it simple + auditable.
        sent_ok = 0
        sent_fail = 0
        for wallet_address, owed_mojos in per_wallet.items():
            print(f"  + {wallet_address}: {calculator.mojos_to_juice(owed_mojos):.3f} $JUICE")

            # L6: re-validate the recipient at the spend boundary. The
            # oracle validates at registration, but never broadcast to an
            # unvalidated address (a malformed/wrong-network address is an
            # unrecoverable burn).
            if not _XCH_ADDR.match(wallet_address or ""):
                print(f"    ! SKIP: invalid recipient address {wallet_address!r}")
                sent_fail += 1
                continue

            contributing = [p for p in ready_rows if p["wallet_address"] == wallet_address]

            # M3: mark each (node, season) provisionally BEFORE broadcasting,
            # so a crash after the tx is sent can't double-pay on the next
            # run. On a clean RPC failure we leave the marks (we can't be
            # sure the tx didn't broadcast) — reconcile failed rows against
            # wallet history before re-running. Biases to at-most-once over
            # double-spend.
            for p in contributing:
                wm.record_payment(
                    node_id=p["node_id"],
                    season=p["season"],
                    wallet_address=wallet_address,
                    paid_mojos=p["mojos"],
                    tx_id=None,
                )

            try:
                resp = rpc.cat_spend(
                    wallet_id=cat_wallet_id,
                    inner_address=wallet_address,
                    amount=int(owed_mojos),
                    fee=int(args.fee),
                    memos=[args.memo] if args.memo else None,
                )
            except WalletRpcError as e:
                print(f"    ! FAILED (left provisionally marked — reconcile "
                      f"before re-running): {e}")
                sent_fail += 1
                continue

            tx_id = (resp.get("transaction_id")
                     or resp.get("tx_id")
                     or resp.get("transaction", {}).get("name", ""))
            print(f"    tx_id={tx_id}")
            sent_ok += 1

            # Confirm: attach the tx_id to the provisional rows.
            for p in contributing:
                wm.set_tx(p["node_id"], p["season"], tx_id)

        print(f"[orchard.payout] sent ok={sent_ok} failed={sent_fail}")
        return 0 if sent_fail == 0 else 5


if __name__ == "__main__":
    sys.exit(main())
