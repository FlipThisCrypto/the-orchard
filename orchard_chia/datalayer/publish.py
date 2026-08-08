# SPDX-License-Identifier: Apache-2.0
"""Hot-path DataLayer publisher (SPEC §6) — meta / node / readings / latest.

Builds one ``batch_update`` changelist per cycle from already device-signed
readings (ADR-0003). Pure planning is separated from I/O so tests never need
a live Chia node.

The Season ``attest:`` sealed path remains in ``main.py`` (HMAC today; migrates
to ed25519 ``schema.sign_attest`` in a later step). This module is the *hot*
path: hourly ``readings:`` permanence + live ``latest:`` pointer.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import clock, confirm, metrics as metrics_mod
from . import ops_log, schedule, schema
from .config import CONFIG_PATH, load
from .oracle import OracleClient, OracleError
from .publish_watermark import PublishWatermark
from . import exit_codes
from .rpc import ChiaRpcError, DataLayerRpc

WRITER_VERSION = "0.2.1"
DEFAULT_WATERMARK_PATH = (
    Path(__file__).resolve().parents[1] / "data" / "publish_watermark.db"
)
# How many closed hours to scan when catching up after downtime.
DEFAULT_LOOKBACK_HOURS = 48


@dataclass
class HourBatchInput:
    """One closed hour of device-signed readings for a single Tree."""
    node_id: str
    season: int
    hour: int
    readings: list[dict]
    # Optional node card fields (written when provided and key changed).
    node_pubkey: str | None = None
    board: str = "unknown"
    fw: str = "unknown"
    sensors: list[dict] = field(default_factory=list)
    geohash: str = ""
    first_seen_utc: str = ""
    label: str | None = None
    running_hours_online: int = 0
    last_sealed_season: int | None = None


@dataclass
class PublishPlan:
    """Result of planning a cycle — ready for DataLayer batch_update."""
    changelist: list[dict]
    # Hours that will be watermarked after a successful batch_update.
    hours: list[tuple[str, int, int, str]] = field(default_factory=list)
    # Human-readable skip reasons (node:season:hour → reason).
    skipped: list[str] = field(default_factory=list)
    meta_written: bool = False
    nodes_written: list[str] = field(default_factory=list)


def _upsert(changelist: list[dict], key_hex: str, value_hex: str, existing: str | None) -> bool:
    """Append delete+insert if value changed. Returns True if a write is needed."""
    if existing == value_hex:
        return False
    if existing is not None:
        changelist.append({"action": "delete", "key": key_hex})
    changelist.append({"action": "insert", "key": key_hex, "value": value_hex})
    return True


def plan_publish(
    *,
    batches: list[HourBatchInput],
    season_pubkey: str,
    writer_version: str = WRITER_VERSION,
    created_at: str | None = None,
    existing_values: dict[str, str | None] | None = None,
    already_published: Callable[[str, int, int], bool] | None = None,
) -> PublishPlan:
    """Build a DataLayer changelist for the given hour batches.

    ``existing_values`` maps key_hex → current value_hex (or None if missing).
    When omitted, every planned key is treated as insert-only (no delete).

    Only readings that already carry a device ``sig`` are accepted — the
    publisher never re-signs device data (SPEC §2.3 / ADR-0003).
    """
    existing = existing_values or {}
    is_pub = already_published or (lambda _n, _s, _h: False)
    now = created_at or clock.utc_now_iso()
    plan = PublishPlan(changelist=[])

    # Always ensure meta:schema is present / current.
    meta = schema.build_meta(
        writer_version=writer_version,
        created_at=now,
        season_pubkey=season_pubkey,
    )
    mk = schema.meta_key()
    mv = schema.value_hex(meta)
    if _upsert(plan.changelist, mk, mv, existing.get(mk)):
        plan.meta_written = True

    for batch in batches:
        node_id = batch.node_id.upper()
        season = int(batch.season)
        hour = int(batch.hour)

        if hour < 0 or hour > 23:
            plan.skipped.append(f"{node_id}:{season}:{hour}: invalid hour")
            continue
        if is_pub(node_id, season, hour):
            plan.skipped.append(f"{node_id}:{season}:{hour}: already published")
            continue

        signed = [r for r in batch.readings if isinstance(r, dict) and r.get("sig")]
        if not signed:
            plan.skipped.append(
                f"{node_id}:{season}:{hour}: no device-signed readings "
                f"(firmware must attach SPEC device_reading with sig)"
            )
            continue

        # Optional node card.
        if batch.node_pubkey:
            node_rec = schema.build_node(
                node_id=node_id,
                pubkey=batch.node_pubkey,
                board=batch.board,
                fw=batch.fw,
                sensors=batch.sensors or [],
                geohash=batch.geohash or "",
                first_seen_utc=batch.first_seen_utc or now,
                label=batch.label,
            )
            nk = schema.node_key(node_id)
            nv = schema.value_hex(node_rec)
            if _upsert(plan.changelist, nk, nv, existing.get(nk)):
                plan.nodes_written.append(node_id)

        readings_rec = schema.build_readings_batch(
            node_id=node_id,
            season=season,
            hour=hour,
            readings=signed,
        )
        rk = schema.readings_key(node_id, season, hour)
        rv = schema.value_hex(readings_rec)
        # readings: is append-only — never rewrite an existing hour.
        if existing.get(rk) is not None:
            plan.skipped.append(
                f"{node_id}:{season}:{hour}: readings key already on chain (append-only)"
            )
            continue
        # A reading is unverifiable without the node's pubkey on chain. Only
        # publish if we're writing the node: card now (pubkey in this batch) or
        # one already exists in the store — else we'd persist data no one can
        # verify (the tenet). This is the first hour a pubkey-less node hits.
        if not batch.node_pubkey and existing.get(schema.node_key(node_id)) is None:
            plan.skipped.append(
                f"{node_id}:{season}:{hour}: no device pubkey on chain — "
                f"readings would be unverifiable"
            )
            continue
        plan.changelist.append({"action": "insert", "key": rk, "value": rv})

        ordered = schema._sorted_readings(signed)
        last = ordered[-1] if ordered else signed[-1]
        latest_rec = schema.build_latest(
            node_id=node_id,
            season=season,
            hour=hour,
            last_sealed_season=batch.last_sealed_season,
            running_hours_online=batch.running_hours_online or (hour + 1),
            last_reading=last,
            updated_at=now,
        )
        lk = schema.latest_key(node_id)
        lv = schema.value_hex(latest_rec)
        _upsert(plan.changelist, lk, lv, existing.get(lk))

        plan.hours.append((node_id, season, hour, readings_rec["hour_root"]))

    return plan


def readings_from_oracle_payloads(
    payloads: list[dict],
    *,
    node_id: str,
) -> list[dict]:
    """Pull device-signed SPEC readings out of oracle reading rows/payloads.

    Accepts either raw POST bodies or ``ReadingResponse``-shaped dicts with a
    nested ``payload`` field. Unsigned sensor blobs are ignored (not re-signed).
    """
    out: list[dict] = []
    for row in payloads:
        body = row.get("payload") if isinstance(row, dict) and "payload" in row else row
        if not isinstance(body, dict):
            continue
        reading = metrics_mod.extract_device_reading(body)
        if reading is None:
            continue
        # Normalize node_id to uppercase for consistency.
        reading = {**reading, "node_id": reading.get("node_id", node_id).upper()}
        if reading["node_id"] != node_id.upper():
            continue
        out.append(reading)
    return out


def hour_of_ts_ms(ts_ms: int) -> int:
    """UTC hour-of-day (0–23) for a device epoch-millis timestamp."""
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).hour


def season_hour_bounds_for_reading(ts_ms: int, season: int) -> tuple[int, int]:
    """Return (season, hour) for a reading. Season is caller-provided (oracle)."""
    return int(season), hour_of_ts_ms(int(ts_ms))


def _lookback_hours(args: list[str]) -> int:
    for i, a in enumerate(args):
        if a == "--lookback-hours" and i + 1 < len(args):
            try:
                return max(1, min(int(args[i + 1]), 24 * 14))
            except ValueError:
                return DEFAULT_LOOKBACK_HOURS
    env = os.environ.get("ORCHARD_PUBLISH_LOOKBACK_HOURS", "").strip()
    if env:
        try:
            return max(1, min(int(env), 24 * 14))
        except ValueError:
            pass
    return DEFAULT_LOOKBACK_HOURS


def harvest_closed_hour_batches(
    oracle: OracleClient,
    nodes: list[dict],
    closed_hours: list[schedule.ClosedHour],
    *,
    already_published: Callable[[str, int, int], bool],
    current_season: int,
    published_hours: Callable[[str, int], int] | None = None,
    sealed_season: Callable[[str, int], int | None] | None = None,
) -> tuple[list[HourBatchInput], list[str]]:
    """Fetch device-signed readings for closed hours not yet watermarked.

    ``published_hours(node_id, season) -> int`` and
    ``sealed_season(node_id, season) -> int | None`` supply the two values that
    ``latest:`` publishes about a node beyond its readings. Both are injected
    rather than computed here because both must be MEASURED — the previous
    inline expressions asserted them, and both assertions were false on the
    first real publish. When either is absent the honest answer is used
    (0 / None), never a guess.

    Uses oracle ``since_ms``/``until_ms`` so each hour is a bounded window
    rather than a full-history scan (Round 2: operational scale).
    """
    batches: list[HourBatchInput] = []
    notes: list[str] = []
    for node in nodes:
        node_id = node["node_id"]
        sensors_public = [
            {"name": n, "active": True}
            for n in (node.get("sensors") or [])
            if isinstance(n, str)
        ]
        first_seen = (
            node.get("registered_at")
            or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )
        for ch in closed_hours:
            if already_published(node_id, ch.season, ch.hour):
                notes.append(
                    f"{node_id[:8]}…:{ch.season}:{ch.hour:02d}: watermarked"
                )
                continue
            try:
                rows = oracle.get_readings(
                    node_id,
                    limit=2000,
                    since_ms=ch.start_ms,
                    until_ms=ch.end_ms,
                )
            except OracleError as e:
                notes.append(f"{node_id[:8]}…:{ch.season}:{ch.hour:02d}: oracle {e}")
                continue
            signed = readings_from_oracle_payloads(rows or [], node_id=node_id)
            # Keep only readings whose ts falls in this closed hour
            # (defense if tree_ts_ms nulls fall outside the SQL window).
            in_window = [
                r for r in signed
                if ch.start_ms <= int(r.get("ts_ms") or 0) < ch.end_ms
            ]
            if not in_window:
                notes.append(
                    f"{node_id[:8]}…:{ch.season}:{ch.hour:02d}: no device-signed rows"
                )
                continue
            batches.append(
                HourBatchInput(
                    node_id=node_id,
                    season=ch.season,
                    hour=ch.hour,
                    readings=in_window,
                    node_pubkey=node.get("device_pubkey") or node.get("pubkey"),
                    board="unknown",
                    fw=node.get("fw_version") or "unknown",
                    sensors=sensors_public,
                    geohash=node.get("geohash") or "",
                    first_seen_utc=first_seen,
                    label=node.get("label"),
                    # MEASURED, not asserted. See HourBatchInput for why both
                    # of these were wrong, and why it mattered.
                    running_hours_online=(
                        published_hours(node_id, ch.season) + 1  # +1 = this hour
                        if published_hours is not None
                        else 0
                    ),
                    last_sealed_season=(
                        sealed_season(node_id, ch.season)
                        if sealed_season is not None
                        else None
                    ),
                )
            )
    return batches, notes


def main(argv: list[str] | None = None) -> int:
    """CLI entry for the hot-path publisher.

    Usage:
      python -m orchard_chia.datalayer publish
      python -m orchard_chia.datalayer publish --dry-run
      python -m orchard_chia.datalayer publish --lookback-hours 72
    """
    args = list(argv if argv is not None else sys.argv[1:])
    dry_run = "--dry-run" in args
    lookback = _lookback_hours(args)

    try:
        cfg = load()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return exit_codes.USAGE

    if not cfg.data_layer.store_id and not dry_run:
        print(
            "ERROR: datalayer.store_id is empty in config.yaml.\n"
            "Create a store: chia data create_data_store -m 0.0001",
            file=sys.stderr,
        )
        return exit_codes.USAGE

    with ops_log.ops_run(
        "publish",
        dry_run=dry_run,
        lookback_hours=lookback,
        store_id_set=bool(cfg.data_layer.store_id),
    ) as run:
        return _publish_body(cfg, dry_run=dry_run, lookback=lookback, run=run)


def _publish_body(cfg, *, dry_run: bool, lookback: int, run: ops_log.OpsRun) -> int:
    season_pubkey = schema.pubkey_for_seed(cfg.signing_key_hex.lower())
    watermark_path = Path(
        os.environ.get("ORCHARD_PUBLISH_WATERMARK", str(DEFAULT_WATERMARK_PATH))
    )
    wm = PublishWatermark(watermark_path)

    oracle = OracleClient(cfg.oracle.url, cfg.oracle.writer_token)
    try:
        current_season = oracle.current_season()
        nodes = oracle.list_nodes()
    except OracleError as e:
        print(f"ERROR: oracle unreachable: {e}", file=sys.stderr)
        wm.close()
        run.finish("error", error="OracleError", error_msg=str(e)[:200])
        return exit_codes.ORACLE

    closed = schedule.iter_closed_hours(lookback_hours=lookback)
    last = closed[-1] if closed else None
    print(f"[orchard.publish] oracle:   {cfg.oracle.url}")
    print(f"[orchard.publish] store_id: {cfg.data_layer.store_id or '(dry-run)'}")
    print(f"[orchard.publish] season:   {current_season}")
    print(f"[orchard.publish] trees:    {len(nodes)}")
    print(f"[orchard.publish] season_pubkey: {season_pubkey[:16]}…")
    print(
        f"[orchard.publish] closed hours: lookback={lookback} "
        f"latest={last.season if last else '-'}/"
        f"{last.hour if last is not None else '--':0>2} "
        f"({last.start_utc.isoformat() if last else 'none'})"
    )
    run.note(
        "context",
        season=current_season,
        trees=len(nodes),
        closed_hours=len(closed),
        latest_closed_hour=last.hour if last else None,
    )

    # Closed hours only, windowed harvest, skip watermarked (SPEC §6).
    # Built before the harvest, because _sealed() below reads the store while
    # batches are being assembled. Constructing the client does no I/O.
    dl: DataLayerRpc | None = None
    if not dry_run and cfg.data_layer.store_id:
        dl = DataLayerRpc(
            cfg.data_layer.host,
            cfg.data_layer.port,
            cfg.data_layer.cert_path,
            cfg.data_layer.key_path,
        )

    # A season counts as sealed only if its attest record is actually IN the
    # store. Asserting `current_season - 1` published "last_sealed_season: 73"
    # into a store holding zero attest records for that node.
    def _sealed(node_id: str, season: int) -> int | None:
        if dl is None or not cfg.data_layer.store_id:
            return None
        for s in range(season - 1, max(0, season - 13), -1):
            try:
                if dl.get_value(cfg.data_layer.store_id, schema.attest_key(node_id, s)):
                    return s
            except ChiaRpcError:
                return None       # can't see the store — claim nothing
        return None

    batches, harvest_notes = harvest_closed_hour_batches(
        oracle,
        nodes,
        closed,
        already_published=wm.is_published,
        current_season=current_season,
        published_hours=wm.published_hours_count,
        sealed_season=_sealed,
    )
    gap_notes = [n for n in harvest_notes if "watermarked" not in n]
    for n in gap_notes:
        print(f"  harvest: {n}")
    run.note(
        "harvest",
        batches=len(batches),
        harvest_gaps=len(gap_notes),
        watermark_skips=sum(1 for n in harvest_notes if "watermarked" in n),
    )

    # Resolve existing store values for keys we might touch (idempotency).
    existing: dict[str, str | None] = {}
    if dl is not None:
        keys_to_check = [schema.meta_key()]
        for b in batches:
            keys_to_check.append(schema.node_key(b.node_id))
            keys_to_check.append(schema.readings_key(b.node_id, b.season, b.hour))
            keys_to_check.append(schema.latest_key(b.node_id))
        for k in dict.fromkeys(keys_to_check):
            try:
                existing[k] = dl.get_value(cfg.data_layer.store_id, k)
            except ChiaRpcError:
                existing[k] = None

    plan = plan_publish(
        batches=batches,
        season_pubkey=season_pubkey,
        existing_values=existing,
        already_published=wm.is_published,
    )

    for sk in plan.skipped:
        print(f"  skip: {sk}")
    print(
        f"[orchard.publish] plan: {len(plan.changelist)} ops, "
        f"{len(plan.hours)} hour(s), meta={plan.meta_written}, "
        f"nodes={plan.nodes_written}"
    )

    if not plan.changelist:
        print("[orchard.publish] nothing to write")
        wm.close()
        run.finish(
            "noop",
            plan_ops=0,
            plan_hours=0,
            skips=len(plan.skipped),
        )
        return 0

    if dry_run:
        print("[orchard.publish] dry-run — not calling DataLayer")
        for item in plan.changelist:
            action = item["action"]
            key_ascii = bytes.fromhex(item["key"]).decode("utf-8", errors="replace")
            print(f"  {action:6} {key_ascii}")
        wm.close()
        run.finish(
            "dry_run",
            plan_ops=len(plan.changelist),
            plan_hours=len(plan.hours),
            meta_written=plan.meta_written,
        )
        return 0

    assert dl is not None
    # Read the root BEFORE the write. Confirmation means "this hash moved and
    # the new one is confirmed" — polling for `confirmed` alone would pass
    # instantly against the pre-write root and prove nothing.
    try:
        root_before = (dl.get_root(cfg.data_layer.store_id) or {}).get("hash")
    except Exception:
        root_before = None          # unknown: fall back to confirmed-only
    try:
        result = dl.batch_update(
            cfg.data_layer.store_id,
            plan.changelist,
            fee=cfg.data_layer.fee or None,
        )
    except ChiaRpcError as e:
        print(f"ERROR: DataLayer batch_update failed: {e}", file=sys.stderr)
        wm.close()
        run.finish(
            "error",
            error="ChiaRpcError",
            error_msg=str(e)[:200],
            rpc_attempts=getattr(dl, "last_retry_attempts", 1),
        )
        return exit_codes.DATALAYER

    txn_id = result.get("tx_id") or result.get("transaction_id") or "<unknown>"
    print(f"[orchard.publish] batch_update accepted tx_id={txn_id}")
    if getattr(dl, "last_retried", False):
        print(
            f"[orchard.publish] succeeded after {dl.last_retry_attempts} RPC attempt(s)"
        )

    # Round 3: wait for the write to reach a block, THEN confirm the values,
    # then advance the watermark. Reading back immediately reported a perfectly
    # good write as missing — see confirm.py's docstring.
    inserts = confirm.inserts_from_changelist(plan.changelist)
    print("[orchard.publish] waiting for the write to confirm on chain…")
    conf = confirm.confirm_after_write(
        dl, cfg.data_layer.store_id, inserts, root_before=root_before
    )
    print(f"[orchard.publish] {conf.detail}")
    if conf.pending:
        # Submitted, not yet in a block. Nothing is wrong — and "re-run to
        # converge" would be the wrong advice here, because a re-run still sees
        # the old value, plans the same write, and pays the fee twice.
        print(
            f"NOTE: tx {txn_id} is accepted but not yet confirmed. This is not a "
            f"failure. Wait for it to confirm, then re-run — re-running NOW would "
            f"submit the same write again and pay the fee twice.",
            file=sys.stderr,
        )
        wm.close()
        run.finish(
            "pending",
            error="ConfirmPending",
            error_msg=conf.detail,
            tx_id=str(txn_id),
            rpc_attempts=getattr(dl, "last_retry_attempts", 1),
        )
        return exit_codes.CONFIRM
    if not conf.ok:
        print(
            f"ERROR: post-write confirm failed "
            f"(missing={conf.missing[:5]} mismatched={conf.mismatched[:5]}). "
            f"The root moved but the values are wrong — this is a real failure. "
            f"Watermark NOT advanced.",
            file=sys.stderr,
        )
        wm.close()
        run.finish(
            "error",
            error="ConfirmError",
            error_msg=conf.detail,
            confirm_checked=conf.checked,
            confirm_missing=len(conf.missing),
            confirm_mismatched=len(conf.mismatched),
            rpc_attempts=getattr(dl, "last_retry_attempts", 1),
        )
        return exit_codes.CONFIRM

    for node_id, season, hour, hour_root in plan.hours:
        wm.record(
            node_id=node_id,
            season=season,
            hour=hour,
            hour_root=hour_root,
            tx_id=txn_id,
        )
    wm.close()
    print(f"[orchard.publish] watermarked {len(plan.hours)} hour(s) "
          f"(config: {CONFIG_PATH})")
    run.finish(
        "ok",
        plan_ops=len(plan.changelist),
        plan_hours=len(plan.hours),
        meta_written=plan.meta_written,
        nodes_written=len(plan.nodes_written),
        # Only short prefix — full tx ids can be long hex.
        tx_id_prefix=(txn_id[:16] if isinstance(txn_id, str) else None),
        rpc_attempts=getattr(dl, "last_retry_attempts", 1),
        rpc_retried=bool(getattr(dl, "last_retried", False)),
        confirm_checked=conf.checked,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
