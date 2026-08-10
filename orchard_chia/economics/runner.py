# SPDX-License-Identifier: Apache-2.0
"""Daily settlement runner: oracle -> rewards -> ledger. Dry by default.

    python -m orchard_chia.economics report            # settle nothing, show all
    python -m orchard_chia.economics settle --season N # record one closed season

A SEASON IS AN EMISSION DAY
===========================

Seasons are UTC-day-aligned (oracle/app/uptime_calc.py; the 4608-block figure
in old docs is read by no code). So the emission calendar does not need a
second genesis: emission ``day_index`` is ``season - 1``, day 0 being season 1.
One calendar, one boundary, no drift between "the day rewards think it is" and
"the day uptime was counted against".

WHAT MAKES A RUN SAFE
=====================

  * Only CLOSED seasons settle. Today's season is still accumulating hours;
    settling it early would under-pay every Tree and then refuse the corrected
    figure as a "different answer for a settled day" — the ledger doing its
    job against us.
  * The ledger is consulted first and written last. A season already settled
    is reported, not re-settled.
  * Settlement is a LEDGER write, not a spend. Actually moving $JUICE remains
    the operator-driven payout flow; this records what is owed under the
    ratified model so that spend has a number to check against.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..datalayer import schedule
from ..datalayer.oracle import OracleClient, OracleError
from . import ledger as ledger_mod
from .constants import format_juice
from .settlement import Settlement, settle_day, tree_day_from_observation

DEFAULT_LEDGER = Path(__file__).resolve().parents[1] / "data" / "pool_ledger.db"


def day_index_for_season(season: int) -> int:
    if season < 1:
        raise ValueError(f"season must be >= 1, got {season}")
    return season - 1


def observe_season(oracle: OracleClient, season: int) -> list:
    """One TreeDay per registered Tree, from the oracle's own accounting."""
    trees = []
    for node in oracle.list_nodes():
        node_id = str(node.get("node_id") or "")
        if not node_id:
            continue
        try:
            uptime = oracle.get_uptime(node_id, season)
            hours = int(uptime.get("hours_online", 0) or 0)
        except OracleError as e:
            # An unreadable Tree earns nothing THIS run and the report says
            # why; it must not zero everyone else or crash the settlement.
            trees.append(tree_day_from_observation(
                tree_id=node_id, wallet_address=node.get("wallet_address"),
                declared_sensors=node.get("sensors") or [],
                hours_with_readings=0, eligible=False,
                ineligible_reason=f"uptime unreadable: {str(e)[:80]}"))
            continue
        # Prefer the oracle's QUALIFIED sensor classes (approved + persistent)
        # over the payload's declared names. Declared names are what a Tree
        # says about itself; qualified classes are what it demonstrated all
        # day. Fall back to declarations only for an oracle predating the
        # field, and the report's weight column makes which one was used
        # visible per Tree.
        q = uptime.get("qualifying_sensor_classes")
        sensors = q if isinstance(q, list) else (node.get("sensors") or [])
        trees.append(tree_day_from_observation(
            tree_id=node_id, wallet_address=node.get("wallet_address"),
            declared_sensors=sensors,
            hours_with_readings=hours))
    return trees


def render(settlement: Settlement, *, season: int, dry: bool) -> str:
    r = settlement.rewards
    L = ["=" * 70,
         f"SETTLEMENT season {season} (emission day {settlement.day_index}, "
         f"year {settlement.ceiling.year})"
         + ("   DRY RUN — ledger untouched" if dry else ""),
         "=" * 70,
         f"  ceiling      {format_juice(settlement.ceiling.ceiling_mojos)} JUICE"
         + ("  [pool-limited]" if settlement.ceiling.limited_by_pool else ""),
         f"  distributed  {format_juice(settlement.distributed_mojos)}",
         f"  unearned     {format_juice(settlement.unearned_mojos)}  (stays in pool)",
         f"  pool after   {format_juice(settlement.pool_closing_mojos)}",
         ""]
    for x in r.rewards:
        L.append(f"   {x.tree_id[:12]:12} {x.verified_heartbeats:2d}/24  "
                 f"w={float(x.sensor_weight):.2f}  "
                 f"-> {format_juice(x.reward_mojos):>14}  {x.wallet_address[:18]}…")
    for t in r.ineligible:
        L.append(f"   {t.tree_id[:12]:12} ineligible: {t.ineligible_reason}")
    if not r.rewards and not r.ineligible:
        L.append("   (no Trees)")
    L.append("=" * 70)
    return "\n".join(L)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m orchard_chia.economics")
    sub = p.add_subparsers(dest="cmd", required=True)
    rp = sub.add_parser("report", help="dry-run the next unsettled closed season")
    rp.add_argument("--season", type=int, default=None)
    sp = sub.add_parser("settle", help="record one closed season in the ledger")
    sp.add_argument("--season", type=int, required=True)
    sp.add_argument("--yes", action="store_true",
                    help="actually write (default is dry-run)")
    args = p.parse_args(argv)

    oracle_url = os.environ.get("ORCHARD_ORACLE_URL",
                                "https://oracle.theorchard.network")
    oracle = OracleClient(oracle_url, os.environ.get(
        "ORCHARD_ORACLE_WRITER_TOKEN") or None)
    ledger_path = Path(os.environ.get("ORCHARD_POOL_LEDGER", str(DEFAULT_LEDGER)))

    current = schedule.season_number_for(datetime.now(timezone.utc))

    with ledger_mod.PoolLedger(ledger_path) as led:
        snap = led.snapshot()
        if args.cmd == "report" and args.season is None:
            season = (snap.last_day_index + 2 if snap.last_day_index is not None
                      else current - 1)
        else:
            season = args.season

        if season >= current:
            print(f"season {season} is not closed (current is {current}). "
                  f"Settling an open season would under-pay every Tree and "
                  f"then poison the ledger with a figure that changes.",
                  file=sys.stderr)
            return 2
        day = day_index_for_season(season)
        if led.is_settled(day):
            row = led.day(day)
            print(f"season {season} already settled: "
                  f"{format_juice(row['distributed_mojos'])} JUICE distributed.")
            return 0

        try:
            trees = observe_season(oracle, season)
        except OracleError as e:
            print(f"oracle unreachable: {e}", file=sys.stderr)
            return 3

        settlement = settle_day(trees, day_index=day,
                                pool_remaining_mojos=snap.remaining_mojos)
        dry = args.cmd == "report" or not args.yes
        print(render(settlement, season=season, dry=dry))
        if dry:
            if args.cmd == "settle":
                print("\nDry run — re-run with --yes to record.")
            return 0
        led.record(settlement)
        print(f"\nrecorded. pool: {format_juice(led.snapshot().remaining_mojos)} "
              f"JUICE remaining.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
