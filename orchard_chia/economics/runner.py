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

from ..datalayer import ops_log, schedule
from ..datalayer.oracle import OracleClient, OracleError
from . import ledger as ledger_mod
from .constants import format_juice
from .settlement import Settlement, settle_day, tree_day_from_observation

DEFAULT_LEDGER = Path(__file__).resolve().parents[1] / "data" / "pool_ledger.db"


def day_index_for_season(season: int) -> int:
    if season < 1:
        raise ValueError(f"season must be >= 1, got {season}")
    return season - 1


def _duplicate_pubkeys(nodes: list[dict]) -> set[str]:
    """Device pubkeys claimed by more than one live node_id.

    A cloned key is one physical device earning as several Trees — top of the
    tokenomics spec's anti-gaming list, and not hypothetical here: re-flashing
    used to mint fresh node_ids for the same board. ALL claimants go
    ineligible, not just the newer one, because between a clone and its
    original the oracle has no way to know which is the imposter — and the
    honest operator is the one person who can fix it (re-key one board).
    """
    seen: dict[str, int] = {}
    for n in nodes:
        pk = (n.get("device_pubkey") or "").strip().lower()
        if pk:
            seen[pk] = seen.get(pk, 0) + 1
    return {pk for pk, count in seen.items() if count > 1}


class ChainConsultError(RuntimeError):
    """The chain could not be consulted, so its verdict is unknown."""


# One store read per PROCESS, indexed by season. Reading per-season meant
# re-scanning every attestation in the store for each season — fine while the
# consult was opt-in and used for one day, fatal the moment it became the
# default and `settle --all` asked for 76 of them: 76 full store scans, and
# the command simply stopped responding. The store is append-only and a
# settle run is short, so one snapshot is both correct and honest here.
_CHAIN_INDEX: dict[int, dict[str, tuple[int, str]]] | None = None


def reset_chain_index() -> None:
    """Drop the cached snapshot (tests, and long-lived processes)."""
    global _CHAIN_INDEX
    _CHAIN_INDEX = None


def _load_chain_index() -> dict[int, dict[str, tuple[int, str]]]:
    """season -> {node_id: (hours, basis)} for every seal in the store."""
    from ..datalayer import config as dl_config
    from ..datalayer.rpc import DataLayerRpc
    from ..payout import reader
    from ..payout.calculator import paid_hours

    cfg = dl_config.load()
    dl = cfg.data_layer
    rpc = DataLayerRpc(host=dl.host, port=dl.port,
                       cert_path=dl.cert_path, key_path=dl.key_path)
    index: dict[int, dict[str, tuple[int, str]]] = {}
    for att in reader.read_all_attestations(rpc, dl.store_id):
        hours, basis = paid_hours(att.signed)
        index.setdefault(int(att.season), {})[att.node_id.upper()] = (
            int(hours), f"chain:{basis}")
    return index


def _chain_hours_for_season(season: int) -> dict[str, tuple[int, str]]:
    """node_id -> (hours, basis) from sealed on-chain attestations.

    ON BY DEFAULT. "Don't trust the oracle, verify it" is the product thesis;
    paying on the oracle's self-report while a signed, independently
    verifiable seal for that exact season sits on chain contradicts it. Where
    a seal exists it DOMINATES: paid_hours() prices it exactly as the payout
    rules do — a proof-backed seal yields its verified_hours, a placeholder
    yields 0, and that zero is the honest answer for a sealed season with no
    evidence. Where no seal exists, the oracle's hours stand, labelled so.

    A FAILED consult RAISES rather than falling back. The fallback is not
    neutral: the chain's number is never higher than the oracle's (a
    placeholder is 0, a real seal counts only signature-verified hours), so
    quietly reverting to the oracle on a transient DataLayer hiccup would
    overpay — and a day settles ONCE, so the overpayment would be permanent.
    Set ORCHARD_SETTLE_CHAIN=0 to settle on oracle hours deliberately, e.g. on
    a host with no DataLayer daemon.
    """
    global _CHAIN_INDEX
    if os.environ.get("ORCHARD_SETTLE_CHAIN", "1").strip().lower() in (
            "0", "false", "no", "off"):
        return {}
    try:
        if _CHAIN_INDEX is None:
            _CHAIN_INDEX = _load_chain_index()
        return dict(_CHAIN_INDEX.get(int(season), {}))
    except Exception as e:               # noqa: BLE001
        raise ChainConsultError(
            f"could not consult the chain for season {season}: {e}. Refusing "
            f"to fall back to the oracle's own hours — the chain's figure is "
            f"never higher, so falling back can only overpay, and a day "
            f"settles once. Fix the DataLayer connection, or set "
            f"ORCHARD_SETTLE_CHAIN=0 to settle on oracle hours deliberately."
        ) from e


def observe_season(oracle: OracleClient, season: int) -> list:
    """One TreeDay per registered Tree. A sealed on-chain season dominates
    the oracle's accounting; otherwise the oracle's hours are used, and each
    Tree's basis says which."""
    trees = []
    # include_retired: settlement is about a PAST season, and retirement ends
    # a Tree's future, not its history. A Tree retired yesterday genuinely
    # earned two days ago; excluding it here would confiscate rewards the
    # retire flow explicitly promised to preserve ("nothing it produced is
    # deleted"). It still has to prove that season's uptime like everyone
    # else, so a long-dead ghost earns nothing anyway — its hours are zero.
    try:
        nodes = oracle.list_nodes(include_retired=True)
    except TypeError:       # an older client without the parameter
        nodes = oracle.list_nodes()
    dup_keys = _duplicate_pubkeys(nodes)
    chain = _chain_hours_for_season(season)
    for node in nodes:
        node_id = str(node.get("node_id") or "")
        if not node_id:
            continue
        pk = (node.get("device_pubkey") or "").strip().lower()
        if pk and pk in dup_keys:
            trees.append(tree_day_from_observation(
                tree_id=node_id, wallet_address=node.get("wallet_address"),
                declared_sensors=[], hours_with_readings=0, eligible=False,
                ineligible_reason=f"device key shared with another Tree "
                                  f"({pk[:12]}…) — one board, one identity"))
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
        basis = "oracle-hours"
        if node_id.upper() in chain:
            hours, basis = chain[node_id.upper()]
        td = tree_day_from_observation(
            tree_id=node_id, wallet_address=node.get("wallet_address"),
            declared_sensors=sensors,
            hours_with_readings=hours)
        import dataclasses as _dc
        td = _dc.replace(td, heartbeat_basis=basis)
        trees.append(td)
    return trees


def _wallet_blind(settlement: Settlement) -> list[str]:
    """Trees that EARNED hours but were excluded only because no wallet was
    visible to this caller. Recording such a day writes distributed=0 into an
    append-only ledger for rewards genuinely earned — and a day settles once,
    so a scheduler running without ORCHARD_ORACLE_WRITER_TOKEN would silently
    burn every day's earnings forever. That is a caller-configuration failure,
    never an empty day, and it must stop the write."""
    out = []
    for t in settlement.rewards.ineligible:
        if (t.verified_heartbeats > 0
                and "wallet" in (t.ineligible_reason or "").lower()):
            out.append(t.tree_id)
    return out


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
        # The basis is the difference between "the chain proves this" and "the
        # oracle says so" — exactly what the human deciding on --yes needs in
        # front of them, not only in the ledger afterwards.
        L.append(f"   {x.tree_id[:12]:12} {x.verified_heartbeats:2d}/24  "
                 f"w={float(x.sensor_weight):.2f}  "
                 f"-> {format_juice(x.reward_mojos):>14}  "
                 f"[{getattr(x, 'heartbeat_basis', 'oracle-hours')}]  "
                 f"{x.wallet_address[:18]}…")
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
    sp = sub.add_parser("settle", help="record closed season(s) in the ledger")
    sp.add_argument("--season", type=int, default=None,
                    help="one season (default with --all: every unsettled closed one)")
    sp.add_argument("--all", action="store_true", dest="settle_all",
                    help="settle every unsettled closed season, oldest first")
    sp.add_argument("--yes", action="store_true",
                    help="actually write (default is dry-run)")
    sub.add_parser("status", help="pool balance, runway, unpaid backlog")
    sub.add_parser("audit", help="the ledger proves itself, or says exactly how it fails")
    pp = sub.add_parser("pay", help="plan (and with two explicit acts, send) "
                                    "the spend for settled unpaid days")
    pp.add_argument("--day", type=int, default=None,
                    help="one settled day (default: oldest unpaid)")
    pp.add_argument("--i-understand-this-spends-real-tokens",
                    action="store_true", dest="live_ack")
    args = p.parse_args(argv)

    oracle_url = os.environ.get("ORCHARD_ORACLE_URL",
                                "https://oracle.theorchard.network")
    oracle = OracleClient(oracle_url, os.environ.get(
        "ORCHARD_ORACLE_WRITER_TOKEN") or None)
    ledger_path = Path(os.environ.get("ORCHARD_POOL_LEDGER", str(DEFAULT_LEDGER)))

    current = schedule.season_number_for(datetime.now(timezone.utc))

    if args.cmd == "pay":
        return _cmd_pay(ledger_path, args)
    if args.cmd == "status":
        return _cmd_status(ledger_path, current)
    if args.cmd == "audit":
        with ledger_mod.PoolLedger(ledger_path) as led:
            problems = led.audit()
        if problems:
            for pr in problems:
                print(f"  ! {pr}", file=sys.stderr)
            print(f"{len(problems)} contradiction(s). Do not settle or pay "
                  f"against this ledger until resolved.", file=sys.stderr)
            return 1
        print("ledger is internally consistent: per-day sums, ceilings, and "
              "the pool chain all re-derive.")
        return 0
    if args.cmd == "settle" and args.settle_all:
        return _cmd_settle_all(ledger_path, oracle, current, yes=args.yes)
    if args.cmd == "settle" and args.season is None:
        print("settle needs --season N or --all", file=sys.stderr)
        return 2

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
        except ChainConsultError as e:
            print(f"{e}", file=sys.stderr)
            return 3

        settlement = settle_day(trees, day_index=day,
                                pool_remaining_mojos=snap.remaining_mojos)
        dry = args.cmd == "report" or not args.yes
        print(render(settlement, season=season, dry=dry))
        if dry:
            if args.cmd == "settle":
                print("\nDry run — re-run with --yes to record.")
            return 0
        blind = _wallet_blind(settlement)
        if blind:
            print(f"REFUSED: {', '.join(t[:12] for t in blind)} earned hours "
                  f"this season but no wallet binding is visible to this "
                  f"caller. Recording now would permanently write 0 for "
                  f"rewards genuinely earned — set ORCHARD_ORACLE_WRITER_TOKEN "
                  f"(or run where wallets are visible) and re-run.",
                  file=sys.stderr)
            return 4
        with ops_log.ops_run("settle", season=season,
                             distributed_mojos=settlement.distributed_mojos,
                             unearned_mojos=settlement.unearned_mojos,
                             eligible=len(settlement.rewards.rewards)) as run:
            led.record(settlement)
            run.note("recorded", pool_closing=settlement.pool_closing_mojos)
        print(f"\nrecorded. pool: {format_juice(led.snapshot().remaining_mojos)} "
              f"JUICE remaining.")
        return 0


def _cmd_settle_all(ledger_path: Path, oracle, current: int, *, yes: bool) -> int:
    """Every unsettled closed season, oldest first, one ledger write each.

    Stops at the first failure rather than skipping it: a gap left silently
    would make every later day's opening balance wrong, which is exactly the
    corruption the ledger's settle-backwards refusal exists to catch. Dry by
    default like everything else; the dry run prints the season list and the
    would-be totals without touching the ledger.
    """
    from .constants import format_juice
    with ledger_mod.PoolLedger(ledger_path) as led:
        first = (led.snapshot().last_day_index + 2
                 if led.snapshot().last_day_index is not None else 1)
        pending = [s for s in range(first, current)
                   if not led.is_settled(day_index_for_season(s))]
        if not pending:
            print("nothing to settle — the ledger is current.")
            return 0
        print(f"{len(pending)} closed season(s) to settle: "
              f"{pending[0]}..{pending[-1]}"
              + ("" if yes else "   DRY RUN — ledger untouched"))
        total = 0
        for season in pending:
            try:
                trees = observe_season(oracle, season)
            except OracleError as e:
                print(f"season {season}: oracle unreachable ({e}); stopping "
                      f"here so no gap is skipped.", file=sys.stderr)
                return 3
            except ChainConsultError as e:
                print(f"season {season}: {e}", file=sys.stderr)
                return 3
            snap = led.snapshot()
            settlement = settle_day(trees, day_index=day_index_for_season(season),
                                    pool_remaining_mojos=snap.remaining_mojos)
            total += settlement.distributed_mojos
            print(f"  season {season:4d}: distributed "
                  f"{format_juice(settlement.distributed_mojos):>14}, "
                  f"unearned {format_juice(settlement.unearned_mojos):>14}")
            blind = _wallet_blind(settlement)
            if blind:
                print(f"season {season}: REFUSED — {', '.join(t[:12] for t in blind)} "
                      f"earned hours but no wallet is visible to this caller. "
                      f"Stopping so no earned day is recorded as zero; set "
                      f"ORCHARD_ORACLE_WRITER_TOKEN and re-run.",
                      file=sys.stderr)
                return 4
            if yes:
                with ops_log.ops_run("settle", season=season,
                                     distributed_mojos=settlement.distributed_mojos,
                                     unearned_mojos=settlement.unearned_mojos,
                                     eligible=len(settlement.rewards.rewards)):
                    led.record(settlement)
        print(f"{'recorded' if yes else 'would record'}: "
              f"{format_juice(total)} JUICE across {len(pending)} day(s); "
              f"pool {'now' if yes else 'would be'} "
              f"{format_juice(led.snapshot().remaining_mojos - (0 if yes else total))} JUICE.")
    return 0


def _cmd_status(ledger_path: Path, current_season: int) -> int:
    """Where the programme stands. Reads only; always safe to run."""
    from . import payment
    from .constants import format_juice, TREE_REWARDS_POOL_MOJOS
    from .emission import runway_days_remaining, emission_year_for_day

    with ledger_mod.PoolLedger(ledger_path) as led:
        snap = led.snapshot()
        unpaid = payment.unpaid_days(led)
        # The self-check is cheap and status is what people actually run;
        # audit-on-demand only protects those who remember it exists.
        contradictions = led.audit()
        if contradictions:
            print(f"!! LEDGER FAILS ITS OWN AUDIT — {len(contradictions)} "
                  f"contradiction(s). Run `economics audit` for detail; do "
                  f"not settle or pay until resolved.")
        day_now = max(0, current_season - 1)
        spent_pct = 100 * snap.distributed_total_mojos / TREE_REWARDS_POOL_MOJOS
        print("POOL")
        print(f"  remaining     {format_juice(snap.remaining_mojos)} JUICE "
              f"({100 - spent_pct:.4f}%)")
        print(f"  distributed   {format_juice(snap.distributed_total_mojos)} "
              f"across {snap.days_settled} settled day(s)")
        print(f"  emission year {emission_year_for_day(day_now)} "
              f"(season {current_season})")
        print(f"  runway        >= {runway_days_remaining(snap.remaining_mojos, day_now):,} days "
              f"at the current ceiling — longer at real uptime")
        behind = (current_season - 2) - (snap.last_day_index
                                         if snap.last_day_index is not None else -1)
        print("SETTLEMENT")
        print("  last settled  "
              + (f"day {snap.last_day_index} (season {snap.last_day_index + 1})"
                 if snap.last_day_index is not None else "never"))
        if behind > 0:
            print(f"  BEHIND by {behind} closed season(s) — run: "
                  f"python -m orchard_chia.economics settle")
        print("PAYMENT")
        if unpaid:
            print(f"  {len(unpaid)} settled day(s) unpaid: "
                  f"{', '.join(str(d) for d in unpaid[:8])}"
                  + (" …" if len(unpaid) > 8 else ""))
        else:
            print("  nothing owed.")
        # A payment that died mid-send blocks every later pay — and the block
        # is silent until someone runs pay again. status is the operator's
        # first stop, so it says so here, with the wallet check that resolves
        # it.
        audit_path = ledger_path.with_name("payment_audit.db")
        if audit_path.exists():
            from ..allocation import audit as audit_mod
            with audit_mod.AuditStore(audit_path) as store:
                stuck = store.in_flight()
            if stuck:
                print(f"  !! {len(stuck)} instruction(s) MID-SEND — every "
                      f"further pay is blocked until resolved:")
                for x in stuck:
                    print(f"     {x.wallet_address[:24]}…  {x.amount_mojos} "
                          f"mojos  cycle {x.cycle_id[:12]}")
                print("     Check the wallet for these transactions, then mark "
                      "the rows sent or failed in the audit store.")
    return 0


def _cmd_pay(ledger_path: Path, args) -> int:
    """Pay one settled day. Dry unless DRY_RUN=false AND the explicit flag —
    the same two-act rule as the allocation service, for the same reason: a
    flag alone survives in shell history and systemd units; a flag PLUS an
    environment variable is a decision made today."""
    from ..allocation import audit as audit_mod
    from ..allocation.executor import execute
    from ..allocation.planner import PlannerLimits
    from . import payment
    from .constants import format_juice

    dry = os.environ.get("DRY_RUN", "true").strip().lower() not in (
        "false", "0", "no", "off")
    if not dry and not args.live_ack:
        print("DRY_RUN is false but --i-understand-this-spends-real-tokens "
              "was not given. Nothing was planned or sent.", file=sys.stderr)
        return 2

    genesis = datetime.combine(schedule.season_genesis_from_env(),
                               datetime.min.time(), tzinfo=timezone.utc)
    asset_id = os.environ.get("ORCHARD_ASSET_ID", "").strip()
    if not asset_id:
        print("ORCHARD_ASSET_ID is not set — refusing to guess which CAT to "
              "send.", file=sys.stderr)
        return 2

    max_cycle = int(os.environ.get("ORCHARD_PAY_MAX_CYCLE_MOJOS", "0") or 0)
    max_wallet = int(os.environ.get("ORCHARD_PAY_MAX_WALLET_MOJOS", "0") or 0)
    if not dry and (max_cycle <= 0 or max_wallet <= 0):
        print("a live payment needs ORCHARD_PAY_MAX_CYCLE_MOJOS and "
              "ORCHARD_PAY_MAX_WALLET_MOJOS — ceilings that live outside the "
              "files holding the amounts.", file=sys.stderr)
        return 2

    audit_path = ledger_path.with_name("payment_audit.db")
    with ledger_mod.PoolLedger(ledger_path) as led,             audit_mod.AuditStore(audit_path) as store:
        day = args.day
        if day is None:
            pending = payment.unpaid_days(led)
            if not pending:
                print("no settled unpaid days.")
                return 0
            day = pending[0]
        try:
            dp = payment.plan_day_payment(
                led, day, store=store, asset_id=asset_id, genesis=genesis,
                limits=PlannerLimits(
                    max_per_cycle_mojos=max_cycle or (1 << 62),
                    max_per_wallet_mojos=max_wallet or (1 << 62)),
                available_balance_mojos=None if dry else _spender_balance(),
                dry_run=dry)
        except payment.PaymentError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 2

        # Crash-heal: a death between "executor sent everything" and
        # "mark_paid" leaves the ledger saying unpaid while the audit store
        # says sent — and every re-run is then blocked "already sent", forever,
        # unless someone edits a database by hand. When the plan is blocked AND
        # the cycle's every instruction reads SENT, the money demonstrably
        # moved: record the fact rather than refusing to acknowledge it.
        # Anything short of ALL sent (a mid-send row, a failed row) stays
        # blocked — healing is only for the case where nothing is in doubt.
        if dp.plan.blocked_by:
            rows = store.instructions(dp.plan.cycle_id)
            if rows and all(r.state == "sent" and r.tx_id for r in rows):
                payment.mark_paid(led, day, cycle_id=dp.plan.cycle_id)
                print(f"healed: day {day} was fully sent in a previous run "
                      f"(cycle {dp.plan.cycle_id[:12]}, every instruction has "
                      f"a tx id) but the ledger was never marked. Marked paid.")
                return 0

        print(f"day {day}: {format_juice(dp.total_mojos)} JUICE across "
              f"{len(dp.plan.instructions)} wallet(s)"
              + ("   DRY RUN — nothing sent" if dry else ""))
        for i in dp.plan.instructions:
            print(f"   {i.wallet_address[:24]}…  {format_juice(i.amount_mojos)}")
        for b in dp.plan.blocked_by:
            print(f"   ! {b}")
        if dry:
            return 0
        with ops_log.ops_run("pay", day=day, cycle=dp.plan.cycle_id,
                             total_mojos=dp.total_mojos,
                             wallets=len(dp.plan.instructions)) as run:
            report = execute(dp.plan, store=store, spender=_spender())
            run.note("executed", sent=len(report.sent),
                     failed=len(report.failed),
                     halted=bool(report.halted_reason))
        if report.halted_reason:
            print(f"HALTED: {report.halted_reason}", file=sys.stderr)
            return 3
        if not report.ok:
            print("some instructions failed; day stays unpaid — see the audit "
                  "store.", file=sys.stderr)
            return 3
        payment.mark_paid(led, day, cycle_id=dp.plan.cycle_id)
        print(f"paid. day {day} marked in the ledger.")
        return 0


# Fees are XCH, not JUICE, and sit outside every JUICE ceiling. 0.01 XCH
# per spend is far above any congestion need; anything higher is almost
# certainly a pasted-wrong number, and each instruction would burn it
# silently. Raising the cap is one deliberate env var, same shape as every
# other ceiling in this stack.
MAX_SANE_FEE_MOJOS = 10_000_000_000          # 0.01 XCH
FEE_CAP_ACK = "ORCHARD_PAY_FEE_CAP_ACK"


def _fee_mojos() -> int:
    fee = int(os.environ.get("ORCHARD_PAY_FEE_MOJOS", "0") or 0)
    if fee < 0:
        raise SystemExit("ORCHARD_PAY_FEE_MOJOS cannot be negative")
    if fee > MAX_SANE_FEE_MOJOS and os.environ.get(FEE_CAP_ACK, "") != "i-know":
        raise SystemExit(
            f"ORCHARD_PAY_FEE_MOJOS={fee} is {fee / 1e12:.4f} XCH PER SPEND — "
            f"above the {MAX_SANE_FEE_MOJOS} (0.01 XCH) sanity cap and almost "
            f"certainly a typo. Fees are XCH, not JUICE, and no JUICE ceiling "
            f"covers them. If genuinely intended set {FEE_CAP_ACK}=i-know.")
    return fee


def _spender():
    from ..allocation.__main__ import _load_config
    from ..allocation.executor import build_spender
    wallet_id = int(os.environ.get("ORCHARD_PAY_WALLET_ID", "0") or 0)
    if not wallet_id:
        raise SystemExit("ORCHARD_PAY_WALLET_ID is required for a live payment")
    return build_spender(
        wallet_id=wallet_id,
        fee_mojos=_fee_mojos(),
        wallet_cfg=(_load_config().get("wallet") or {}))


def _spender_balance():
    try:
        return _spender().spendable_balance()
    except SystemExit:
        raise
    except Exception:
        return None


if __name__ == "__main__":
    raise SystemExit(main())
