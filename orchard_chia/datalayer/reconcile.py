# SPDX-License-Identifier: Apache-2.0
"""Reconcile oracle uptime claims against public DataLayer readings.

Implements the product tenet operationally: if ``verified_hours`` from
on-chain ``readings:`` is below the oracle's ``hours_online``, the
discrepancy is surfaceable — not alleged.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass

from . import config, seal
from .oracle import OracleClient, OracleError
from .rpc import ChiaRpcError, DataLayerRpc


@dataclass
class NodeSeasonRow:
    node_id: str
    season: int
    hours_online: int | None
    verified_hours: int | None
    reading_count: int
    seal_source: str
    status: str  # match | overclaim | underclaim | no_oracle | no_dl | error
    detail: str = ""


def reconcile_node_season(
    *,
    oracle: OracleClient,
    dl: DataLayerRpc,
    store_id: str,
    node_id: str,
    season: int,
    device_pubkey: str | None,
) -> NodeSeasonRow:
    hours_online: int | None = None
    try:
        up = oracle.get_uptime(node_id, season)
        if up is not None:
            hours_online = int(up.get("hours_online", 0))
    except OracleError as e:
        return NodeSeasonRow(
            node_id=node_id,
            season=season,
            hours_online=None,
            verified_hours=None,
            reading_count=0,
            seal_source="none",
            status="error",
            detail=str(e)[:120],
        )

    try:
        rows = seal.load_season_readings(
            dl, store_id, node_id=node_id, season=season
        )
    except Exception as e:  # noqa: BLE001
        return NodeSeasonRow(
            node_id=node_id,
            season=season,
            hours_online=hours_online,
            verified_hours=None,
            reading_count=0,
            seal_source="none",
            status="error",
            detail=str(e)[:120],
        )

    sealed = seal.seal_from_readings(rows, device_pubkey=device_pubkey)
    if sealed is None:
        if hours_online is None:
            status, detail = "no_oracle", "no uptime and no readings"
        elif hours_online == 0:
            status, detail = "match", "both empty"
        else:
            status, detail = "no_dl", "oracle has hours but no public readings"
        return NodeSeasonRow(
            node_id=node_id,
            season=season,
            hours_online=hours_online,
            verified_hours=None,
            reading_count=0,
            seal_source="none",
            status=status,
            detail=detail,
        )

    vh = sealed.verified_hours
    if hours_online is None:
        status = "no_oracle"
        detail = f"verified={vh} from readings only"
    elif vh == hours_online:
        status = "match"
        detail = f"hours={vh}"
    elif vh < hours_online:
        status = "overclaim"
        detail = f"oracle={hours_online} verified={vh}"
    else:
        status = "underclaim"
        detail = f"oracle={hours_online} verified={vh}"

    return NodeSeasonRow(
        node_id=node_id,
        season=season,
        hours_online=hours_online,
        verified_hours=vh,
        reading_count=sealed.reading_count,
        seal_source=sealed.source,
        status=status,
        detail=detail,
    )


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    season: int | None = None
    for i, a in enumerate(args):
        if a == "--season" and i + 1 < len(args):
            season = int(args[i + 1])

    try:
        cfg = config.load()
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    if not cfg.data_layer.store_id:
        print("ERROR: datalayer.store_id empty", file=sys.stderr)
        return 2

    oracle = OracleClient(cfg.oracle.url)
    try:
        current = oracle.current_season()
        nodes = oracle.list_nodes()
    except OracleError as e:
        print(f"ERROR: oracle: {e}", file=sys.stderr)
        return 3

    if season is None:
        season = max(1, current - 1)  # last closed

    dl = DataLayerRpc(
        cfg.data_layer.host,
        cfg.data_layer.port,
        cfg.data_layer.cert_path,
        cfg.data_layer.key_path,
    )

    print(f"Orchard reconcile — season {season} (current={current})")
    print(f"{'node':32}  {'oracle':>6}  {'verified':>8}  status")
    over = 0
    for n in nodes:
        nid = n["node_id"]
        pub = n.get("device_pubkey") or n.get("pubkey")
        row = reconcile_node_season(
            oracle=oracle,
            dl=dl,
            store_id=cfg.data_layer.store_id,
            node_id=nid,
            season=season,
            device_pubkey=pub,
        )
        if row.status == "overclaim":
            over += 1
        oh = "—" if row.hours_online is None else str(row.hours_online)
        vh = "—" if row.verified_hours is None else str(row.verified_hours)
        print(
            f"{nid[:32]:32}  {oh:>6}  {vh:>8}  {row.status}  {row.detail}"
        )

    print()
    print(f"overclaims: {over}")
    # Exit 1 if any overclaim so cron/alerting can page.
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main())
