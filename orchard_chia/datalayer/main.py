# SPDX-License-Identifier: Apache-2.0
"""Season attestation writer — orchestrator.

Pulls per-Tree per-Season uptime from the oracle, builds and signs an
attestation record for every closed Season, and writes those records
to a Chia DataLayer store. Idempotent: re-running this re-writes only
records whose value changed since last time.

Run: ``python -m chia.datalayer``

Typical schedule: hourly via cron / Windows Task Scheduler, or on
demand when you want to push a fresh attestation. v1 has no
persistent watermark — re-runs are cheap and safe.
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from . import attest, config, confirm, exit_codes, ops_log, schema, seal
from .oracle import OracleClient, OracleError
from .rpc import ChiaRpcError, DataLayerRpc, FullNodeRpc


@dataclass
class _PendingAttestation:
    """A signed attestation that's queued for DataLayer in this run.
    After batch_update succeeds we POST one of these per row to the
    oracle's /attestations endpoint so the local DB tracks what's on
    chain (the dashboard reads this for the "On chain" card)."""
    node_id: str
    season: int
    hours_online: int
    data_hash: str
    oracle_sig: str
    key_hex: str
    block_height: int


def _report_to_oracle(
    *,
    oracle_url: str,
    pending: list[_PendingAttestation],
    tx_id: str,
    written_at: datetime,
) -> None:
    """POST one /attestations record per pending row. Failures are
    logged but don't abort — the DataLayer write already succeeded;
    re-running the writer will catch up the oracle's view."""
    base = oracle_url.rstrip("/")
    # Present the writer token if configured (the oracle requires it for
    # non-loopback callers; loopback works without one). Keep it out of
    # config.yaml — read from the environment alongside the oracle's own
    # ORCHARD_ORACLE_WRITER_TOKEN.
    writer_token = os.environ.get("ORCHARD_ORACLE_WRITER_TOKEN", "").strip()
    headers = {"X-Orchard-Writer-Token": writer_token} if writer_token else None
    for p in pending:
        body = {
            "node_id":               p.node_id,
            "season_number":         p.season,
            "hours_online":          p.hours_online,
            "data_hash":             p.data_hash,
            "oracle_sig":            p.oracle_sig,
            "dl_tx_id":              tx_id,
            "dl_key_hex":            p.key_hex,
            "block_height_at_write": p.block_height,
            "written_to_datalayer_at": written_at.isoformat(),
        }
        try:
            r = requests.post(f"{base}/attestations", json=body, timeout=10, headers=headers)
        except requests.RequestException as e:
            print(f"  WARN: oracle /attestations POST failed: {e}", file=sys.stderr)
            continue
        if r.status_code not in (200, 201):
            print(
                f"  WARN: oracle /attestations returned {r.status_code}: "
                f"{r.text[:160]}", file=sys.stderr)


def main() -> int:
    try:
        cfg = config.load()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return exit_codes.USAGE

    if not cfg.data_layer.store_id:
        print(
            "ERROR: chia/config.yaml -> datalayer.store_id is empty.\n"
            "Create a store first:\n"
            "    chia data create_data_store -m 0.0001\n"
            "Then put the returned `id` value into config.yaml.",
            file=sys.stderr,
        )
        return exit_codes.USAGE

    with ops_log.ops_run("attest", store_id_set=True) as run:
        return _attest_body(cfg, run)


def _attest_body(cfg, run: ops_log.OpsRun) -> int:
    oracle = OracleClient(cfg.oracle.url, cfg.oracle.writer_token)
    fn = FullNodeRpc(
        cfg.full_node.host, cfg.full_node.port,
        cfg.full_node.cert_path, cfg.full_node.key_path,
    )
    dl = DataLayerRpc(
        cfg.data_layer.host, cfg.data_layer.port,
        cfg.data_layer.cert_path, cfg.data_layer.key_path,
    )

    print(f"[orchard.attest] oracle:    {cfg.oracle.url}")
    print(f"[orchard.attest] datalayer: {cfg.data_layer.host}:{cfg.data_layer.port}")
    print(f"[orchard.attest] store_id:  {cfg.data_layer.store_id}")

    try:
        current_season = oracle.current_season()
    except OracleError as e:
        print(f"ERROR: oracle unreachable: {e}", file=sys.stderr)
        run.finish("error", error="OracleError", error_msg=str(e)[:200])
        return exit_codes.ORACLE
    print(f"[orchard.attest] current_season: {current_season} (closed: 1..{current_season - 1})")

    if current_season < 2:
        print("[orchard.attest] No closed Seasons yet. Nothing to attest.")
        run.finish("noop", reason="no_closed_seasons", season=current_season)
        return 0

    try:
        nodes = oracle.list_nodes()
    except OracleError as e:
        print(f"ERROR: oracle list_nodes failed: {e}", file=sys.stderr)
        run.finish("error", error="OracleError", error_msg=str(e)[:200])
        return exit_codes.ORACLE
    print(f"[orchard.attest] registered Trees: {len(nodes)}")
    if not nodes:
        run.finish("noop", reason="no_trees", season=current_season)
        return 0

    try:
        block_height = fn.peak_height()
    except ChiaRpcError as e:
        print(f"ERROR: chia full-node unreachable: {e}", file=sys.stderr)
        return exit_codes.CHIA
    print(f"[orchard.attest] chia peak height: {block_height}")

    # Determine Season range to process.
    first_season = 1
    if cfg.attestation.max_lookback_seasons is not None:
        first_season = max(1, current_season - cfg.attestation.max_lookback_seasons)

    changelist: list[dict] = []
    pending: list[_PendingAttestation] = []   # parallel to inserts so
                                              # we can POST back after
                                              # the on-chain write lands
    stats = Counter()
    lower_lookback = cfg.attestation.max_lookback_seasons or "all"
    print(f"[orchard.attest] lookback: seasons {first_season}..{current_season - 1} ({lower_lookback})")

    for node in nodes:
        node_id = node["node_id"]
        for season in range(first_season, current_season):
            try:
                uptime = oracle.get_uptime(node_id, season)
            except OracleError as e:
                print(f"  WARN: oracle uptime {node_id[:8]}.. season={season}: {e}", file=sys.stderr)
                stats["error"] += 1
                continue
            if uptime is None:
                stats["no_data"] += 1
                continue
            hours = int(uptime.get("hours_online", 0))
            if hours == 0 and cfg.attestation.skip_empty_seasons:
                stats["empty"] += 1
                continue

            # SPEC §2.4 sealed attest (ADR-0003): prefer season_root +
            # verified_hours from published readings: batches; fall back to
            # the uptime-derived placeholder when nothing is on-chain yet.
            pub_readings = seal.load_season_readings(
                dl, cfg.data_layer.store_id, node_id=node_id, season=season
            )
            device_pub = (
                node.get("device_pubkey")
                or node.get("pubkey")
            )
            sealed = seal.seal_from_readings(
                pub_readings, device_pubkey=device_pub
            )
            if sealed is not None:
                season_root_hex = sealed.season_root
                verified_hrs = sealed.verified_hours
                reading_count = sealed.reading_count
                seal_src = sealed.source
                # Carry the integrity signals into the SIGNED record instead of
                # dropping them: presence-only counting (no device pubkey) and
                # hour_root mismatches are exactly what a consumer needs to know
                # before treating verified_hours as proof.
                sigs_verified = sealed.sigs_verified
                root_mismatches = sealed.root_mismatches
            else:
                season_root_hex = attest.data_hash_for_uptime(
                    node_id, season, hours
                )
                # Nothing is published for this season, so NOTHING was verified.
                # Writing `hours` here would sign the oracle's own claim into a
                # field named "verified" — the one case where there is no
                # evidence at all. hours_online below still records the claim;
                # the payout falls back to it for a declared-placeholder record,
                # so amounts are unchanged — but the record no longer lies.
                verified_hrs = 0
                reading_count = 0
                seal_src = schema.SEAL_SOURCE_PLACEHOLDER
                sigs_verified = False
                root_mismatches = 0

            signed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            payload = schema.build_attest(
                node_id=node_id,
                season=season,
                season_start_utc=uptime["season_start_utc"],
                season_end_utc=uptime["season_end_utc"],
                hours_online=hours,
                verified_hrs=verified_hrs,
                reading_count=reading_count,
                block_height_at_write=block_height,
                season_root_hex=season_root_hex,
                signed_at=signed_at,
                seal_source=seal_src,
                sigs_verified=sigs_verified,
                root_mismatches=root_mismatches,
            )
            # signing_key_hex is 64 hex chars — valid secp256r1 scalar seed
            # (generated once per operator; same file as legacy HMAC key).
            signed = schema.sign_attest(payload, cfg.signing_key_hex.lower())

            key_hex = schema.attest_key(node_id, season)
            value_hex = schema.value_hex(signed)

            try:
                existing_hex = dl.get_value(cfg.data_layer.store_id, key_hex)
            except ChiaRpcError as e:
                print(f"  WARN: datalayer get_value failed: {e}", file=sys.stderr)
                existing_hex = None

            if existing_hex == value_hex:
                stats["unchanged"] += 1
                continue

            if existing_hex is not None:
                # DataLayer batch_update supports delete-then-insert for replaces.
                changelist.append({"action": "delete", "key": key_hex})
            changelist.append({"action": "insert", "key": key_hex, "value": value_hex})
            pending.append(_PendingAttestation(
                node_id=node_id,
                season=season,
                hours_online=hours,
                data_hash=payload["data_hash"],
                oracle_sig=signed["oracle_sig"],
                key_hex=key_hex,
                block_height=block_height,
            ))
            stats["written"] += 1
            flags = ""
            if seal_src == schema.SEAL_SOURCE_PLACEHOLDER:
                flags += " [NOT proof-backed]"
            if not sigs_verified and seal_src != schema.SEAL_SOURCE_PLACEHOLDER:
                flags += " [sigs unchecked: no device pubkey]"
            if root_mismatches:
                flags += f" [!] {root_mismatches} hour_root mismatch(es)"
            print(
                f"  + node={node_id[:8]}.. season={season:>4} "
                f"hours={hours:>2} verified={verified_hrs} "
                f"seal={seal_src} sig={signed['oracle_sig'][:10]}..{flags}"
            )

    if not changelist:
        print(f"[orchard.attest] Nothing to update ({dict(stats)})")
        run.finish("noop", stats=dict(stats), season=current_season)
        return 0

    print(f"[orchard.attest] sending {len(changelist)} changelist items to DataLayer ...")
    try:
        result = dl.batch_update(
            cfg.data_layer.store_id, changelist, fee=cfg.data_layer.fee or None
        )
    except ChiaRpcError as e:
        print(f"ERROR: DataLayer batch_update failed: {e}", file=sys.stderr)
        run.finish(
            "error",
            error="ChiaRpcError",
            error_msg=str(e)[:200],
            stats=dict(stats),
            rpc_attempts=getattr(dl, "last_retry_attempts", 1),
        )
        return exit_codes.DATALAYER
    txn_id = result.get("tx_id") or result.get("transaction_id") or "<unknown>"
    written_at = datetime.now(timezone.utc)
    print(f"[orchard.attest] DataLayer batch_update accepted. tx_id={txn_id}")
    if getattr(dl, "last_retried", False):
        print(
            f"[orchard.attest] succeeded after {dl.last_retry_attempts} RPC attempt(s)"
        )
    print(f"[orchard.attest] stats: {dict(stats)}")

    inserts = confirm.inserts_from_changelist(changelist)
    conf = confirm.confirm_inserts(dl, cfg.data_layer.store_id, inserts)
    print(f"[orchard.attest] {conf.detail}")
    if not conf.ok:
        print(
            f"ERROR: post-write confirm failed "
            f"(missing={conf.missing[:5]} mismatched={conf.mismatched[:5]}). "
            f"Oracle NOT updated — re-run to converge.",
            file=sys.stderr,
        )
        run.finish(
            "error",
            error="ConfirmError",
            error_msg=conf.detail,
            stats=dict(stats),
            confirm_checked=conf.checked,
            confirm_missing=len(conf.missing),
            confirm_mismatched=len(conf.mismatched),
            rpc_attempts=getattr(dl, "last_retry_attempts", 1),
        )
        return exit_codes.CONFIRM

    # Report back to the oracle so its local DB tracks what's on chain.
    # Failures here don't roll back the DataLayer write — the chain
    # state is already accepted. The oracle catches up on the next run.
    print(f"[orchard.attest] reporting {len(pending)} record(s) back to "
          f"oracle /attestations …")
    _report_to_oracle(
        oracle_url=cfg.oracle.url,
        pending=pending,
        tx_id=txn_id,
        written_at=written_at,
    )
    run.finish(
        "ok",
        stats=dict(stats),
        pending=len(pending),
        season=current_season,
        tx_id_prefix=(txn_id[:16] if isinstance(txn_id, str) else None),
        rpc_attempts=getattr(dl, "last_retry_attempts", 1),
        rpc_retried=bool(getattr(dl, "last_retried", False)),
        confirm_checked=conf.checked,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
