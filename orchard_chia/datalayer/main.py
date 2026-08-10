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
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from . import (attest, config, confirm, exit_codes, ops_log, provenance, schedule,
               schema, seal)
from .oracle import OracleClient, OracleError
from .rpc import ChiaRpcError, DataLayerRpc, FullNodeRpc


def _first_plausible_season(node: dict, *, floor: int = 1) -> int:
    """Earliest Season this Tree could possibly have uptime in.

    Derived from the Tree's own ``registered_at``/``first_seen_utc``, so it
    self-tunes as Seasons accumulate rather than needing a config number
    re-tuned forever. Returns ``floor`` when the date is missing or
    unparseable — an unknown registration date must widen the search, never
    narrow it, because skipping a Season a Tree really was online in would
    silently lose a sealed attestation.
    """
    raw = node.get("registered_at") or node.get("first_seen_utc")
    if not isinstance(raw, str) or not raw.strip():
        return floor
    text = raw.strip().replace("Z", "+00:00")
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return floor
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    try:
        season = schedule.season_number_for(when)
    except Exception:
        return floor
    return max(floor, int(season))


def _wallet_for_height(cfg):
    """Wallet client used ONLY to read a synced peak height (no spending).

    Built lazily, and only when the full node is unreachable, so the ordinary
    path never touches the wallet at all.
    """
    import yaml
    from ..wallet.rpc import WalletRpc
    raw = yaml.safe_load(config.CONFIG_PATH.read_text(encoding="utf-8")) or {}
    w = raw.get("wallet") or {}
    return WalletRpc(
        host=w.get("host", "127.0.0.1"),
        port=int(w.get("port", 9256)),
        cert_path=config._expand(w.get("cert_path", "")),
        key_path=config._expand(w.get("key_path", "")),
        fingerprint=int(w.get("fingerprint", 0)),
    )

# T16 confirmation monitoring: after a batch_update is accepted into the
# mempool, poll the store root until the spend confirms on chain. Bounded +
# non-fatal — the write is already submitted, and the writer is convergent so
# an unconfirmed root is simply retried on the next run. A root update is
# typically mined within a block or two (~tens of seconds).
CONFIRM_TIMEOUT_S = 180
CONFIRM_POLL_S = 10


def wait_for_root_confirmation(
    dl: DataLayerRpc,
    store_id: str,
    *,
    timeout_s: float = CONFIRM_TIMEOUT_S,
    poll_s: float = CONFIRM_POLL_S,
    _sleep=time.sleep,
    _clock=time.monotonic,
) -> bool:
    """Poll the DataLayer store root until ``confirmed`` is true or the budget
    runs out. Returns True iff confirmed within ``timeout_s``. Never raises —
    an RPC hiccup is treated as 'not yet confirmed' and retried within budget.
    (``_sleep`` / ``_clock`` are injectable for tests.)"""
    deadline = _clock() + timeout_s
    while True:
        try:
            if dl.get_root(store_id).get("confirmed") is True:
                return True
        except ChiaRpcError:
            pass  # transient — keep polling within the budget
        if _clock() >= deadline:
            return False
        _sleep(poll_s)


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


def _season_seal_time(uptime: dict) -> str:
    """Deterministic ``signed_at`` for a sealed Season.

    Uses the Season's end boundary normalized to ``%Y-%m-%dT%H:%M:%SZ`` so
    re-sealing the same closed Season reproduces byte-identical signed content
    (the precondition for the writer being idempotent). Falls back to the raw
    string, then to the epoch, rather than reintroducing now().
    """
    raw = (uptime or {}).get("season_end_utc")
    if not isinstance(raw, str) or not raw:
        return "1970-01-01T00:00:00Z"
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
    # An empty node set used to end the run as a clean no-op. An oracle that
    # cannot be read looks exactly like a network with no Trees, and "nothing
    # to do" is a comfortable conclusion for a scheduled job to reach wrongly.
    try:
        known = provenance.require_live_network(nodes, source=cfg.oracle.url)
        provenance.filter_writable(nodes, known)
    except provenance.ProvenanceError as e:
        print(f"ERROR: refusing to attest: {e}", file=sys.stderr)
        run.finish("error", error="ProvenanceError", error_msg=str(e)[:200])
        return exit_codes.ORACLE

    # The anti-backdate anchor. A full node is the ideal source, but requiring
    # one to seal a season means an operator running only wallet + data_layer
    # cannot seal at all — which is exactly the state the first real publish
    # hit: readings on chain, verifier stuck at "attest missing from store",
    # and a ~200 GB dependency standing between the two.
    #
    # A SYNCED wallet's height is the peak, so it is an equivalent anchor. The
    # sync gate in synced_peak_height() is what makes that true; without it the
    # height would be a guess, and an anchor that guesses is worse than none.
    block_height, height_source = None, None
    try:
        block_height = fn.peak_height()
        height_source = "full_node"
    except ChiaRpcError as e:
        print(f"[orchard.attest] full node unavailable ({e}); trying the wallet")
        try:
            block_height = _wallet_for_height(cfg).synced_peak_height()
            height_source = "wallet(synced)"
        except Exception as e2:
            print(
                f"ERROR: no trustworthy block height available — full node: {e}; "
                f"wallet: {e2}. Refusing to seal a season with an anchor that "
                f"cannot be substantiated.",
                file=sys.stderr,
            )
            return exit_codes.CHIA
    print(f"[orchard.attest] chia peak height: {block_height} (via {height_source})")
    run.note("anchor", block_height=block_height, height_source=height_source)

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
        # A Tree cannot have uptime in a Season that ended before it existed.
        # Walking from Season 1 for every Tree meant 73 x 6 = 438 oracle round
        # trips, 72 Seasons of which predate every Tree in the network — the
        # run could not finish, and the waste grows by one Season per day
        # forever. Deriving the floor from the Tree's own registration is
        # self-tuning: a Tree registered today is checked for today.
        node_first = _first_plausible_season(node, floor=first_season)
        if node_first > first_season:
            stats["seasons_skipped_pre_registration"] += node_first - first_season
        for season in range(node_first, current_season):
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
            # STRICT: this data decides a permanent, signed conclusion. A
            # transient RPC failure must never be read as "nothing published"
            # (-> placeholder) or silently drop an hour (-> understated
            # verified_hours over a partial season_root). Skip the season
            # instead; the next run re-attempts it.
            try:
                pub_readings = seal.load_season_readings(
                    dl, cfg.data_layer.store_id,
                    node_id=node_id, season=season, strict=True,
                )
            except seal.SealReadError as e:
                print(
                    f"  WARN: skipping {node_id[:8]}.. season={season}: {e}",
                    file=sys.stderr,
                )
                stats["read_error"] += 1
                continue
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
                min_rph = sealed.min_readings_per_hour
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
                # Recorded even here. A placeholder proves nothing, but the rule
                # it would have been judged by is still part of what the record
                # says about itself.
                min_rph = schema.MIN_VERIFIED_READINGS_PER_HOUR
                sigs_verified = False
                root_mismatches = 0

            # Deterministic seal time: the Season's own close boundary, NOT
            # wall-clock now(). signed_at is inside the signed body, so a
            # per-run timestamp changed value_hex on every run — which made the
            # `existing_hex == value_hex` short-circuit below unreachable and
            # caused every daily run to delete+insert EVERY attestation for
            # EVERY past season: unbounded, fee-bearing, and rewriting the seal
            # time of historical records. A sealed season is a fixed fact, so
            # re-signing it must reproduce identical bytes.
            signed_at = _season_seal_time(uptime)
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
            min_readings_per_hour=min_rph,
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

            # BASIS-DOWNGRADE GUARD. Never replace a proof-backed attestation
            # with a weaker one. Without this, a single bad read (or a store
            # that has since been pruned) could overwrite a sealed,
            # Merkle-rooted record with a placeholder claiming nothing was ever
            # published — destroying the evidence for that season permanently.
            if existing_hex is not None:
                prev = schema.parse_value(existing_hex) or {}
                prev_backed = schema.attest_is_proof_backed(prev)
                new_backed = schema.attest_is_proof_backed(signed)
                if prev_backed is True and new_backed is not True:
                    print(
                        f"  WARN: refusing to downgrade {node_id[:8]}.. "
                        f"season={season}: on-chain record is proof-backed, "
                        f"new one would be {signed.get('seal_source')!r}",
                        file=sys.stderr,
                    )
                    stats["downgrade_refused"] += 1
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
    # Root BEFORE the write — confirmation means "this hash moved", not merely
    # "a root is confirmed" (the pre-write root is confirmed too).
    try:
        root_before = (dl.get_root(cfg.data_layer.store_id) or {}).get("hash")
    except Exception:
        root_before = None
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
    print("[orchard.attest] waiting for the write to confirm on chain…")
    conf = confirm.confirm_after_write(
        dl, cfg.data_layer.store_id, inserts, root_before=root_before
    )
    print(f"[orchard.attest] {conf.detail}")
    if conf.pending:
        print(
            f"NOTE: tx {txn_id} is accepted but not yet confirmed. This is not a "
            f"failure. Wait for it to confirm, then re-run — re-running NOW would "
            f"submit the same write again and pay the fee twice.",
            file=sys.stderr,
        )
        run.finish(
            "pending",
            error="ConfirmPending",
            error_msg=conf.detail,
            stats=dict(stats),
            tx_id=str(txn_id),
            rpc_attempts=getattr(dl, "last_retry_attempts", 1),
        )
        return exit_codes.CONFIRM
    if not conf.ok:
        print(
            f"ERROR: post-write confirm failed "
            f"(missing={conf.missing[:5]} mismatched={conf.mismatched[:5]}). "
            f"The root moved but the values are wrong — a real failure. "
            f"Oracle NOT updated.",
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

    # T16 / ADR-0010: confirm the root update actually landed on chain.
    # Non-fatal to the write — the spend is submitted and the writer is
    # convergent (an unconfirmed root is re-attempted next run, so attestation
    # data is never silently dropped), but the operator gets a loud WARNING and
    # a distinct non-zero exit so a cron/timer wrapper can alert.
    print(f"[orchard.attest] waiting up to {CONFIRM_TIMEOUT_S}s for the root "
          f"update to confirm on chain …")
    root_confirmed = wait_for_root_confirmation(dl, cfg.data_layer.store_id)

    # Journal fields are identical either way; only the status/exit differ, so
    # the ops journal records every run (confirmed or not) exactly once.
    finish_fields = dict(
        stats=dict(stats),
        pending=len(pending),
        season=current_season,
        # Only a short prefix — full tx ids can be long hex.
        tx_id_prefix=(txn_id[:16] if isinstance(txn_id, str) else None),
        rpc_attempts=getattr(dl, "last_retry_attempts", 1),
        rpc_retried=bool(getattr(dl, "last_retried", False)),
        confirm_checked=conf.checked,
        root_confirmed=root_confirmed,
    )

    if root_confirmed:
        print("[orchard.attest] root update CONFIRMED on chain.")
        run.finish("ok", **finish_fields)
        return 0

    print(
        f"WARNING: root update tx_id={txn_id} not confirmed within "
        f"{CONFIRM_TIMEOUT_S}s. The spend may still confirm shortly; if it "
        f"does not, the next run re-publishes (convergent writer). Check the "
        f"Season ledger before relying on this attestation.",
        file=sys.stderr,
    )
    run.finish(
        "error",
        error="RootUnconfirmed",
        error_msg=f"root update not confirmed within {CONFIRM_TIMEOUT_S}s",
        **finish_fields,
    )
    # ADR-0010: distinct non-zero exit (exit_codes.CONFIRM == 6, the value the
    # production writer has always returned here) so a cron/timer wrapper can
    # alert on "submitted but not confirmed".
    return exit_codes.CONFIRM


if __name__ == "__main__":
    sys.exit(main())
