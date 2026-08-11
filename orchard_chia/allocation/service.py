# SPDX-License-Identifier: Apache-2.0
"""Config, the cycle runner, the dry-run report, and the scheduler.

One module because these are all "how the parts are wired", and splitting the
wiring across four files makes the safety story harder to read than the code it
describes. The parts themselves stay separate: collector, engine, planner,
executor and audit know nothing about any of this.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path

from . import audit as audit_mod
from .collector import CollectionResult, OracleUptimeSource, collect
from .engine import BASIS_POINTS_FULL, allocate
from .executor import ExecutionReport, execute
from .lock import RunLock
from .planner import PlannerLimits, SpendPlan, persist, plan

# 3-decimal CAT, matching orchard_chia/payout/calculator.py.
MOJOS_PER_TOKEN = 1000

DEFAULT_DB = Path(__file__).resolve().parents[1] / "data" / "allocation.db"
DEFAULT_PAUSE = Path(__file__).resolve().parents[1] / "data" / "PAUSED"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from e


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """Every knob, with DRY_RUN true unless someone deliberately says otherwise.

    The default is not caution for its own sake. A misconfigured live run is
    irreversible and a misconfigured dry run is a text file.
    """
    oracle_url: str
    asset_id: str
    budget_mojos: int
    period_hours: int
    stale_after_hours: int
    min_uptime_bp: int
    max_per_cycle_mojos: int
    max_per_wallet_mojos: int
    min_payout_mojos: int
    fee_mojos: int
    dry_run: bool
    db_path: Path
    pause_file: Path
    wallet_id: int | None
    interval_seconds: int

    @classmethod
    def from_env(cls, cfg: dict | None = None) -> "Settings":
        cfg = cfg or {}
        token = (cfg.get("token") or {})
        alloc = (cfg.get("allocation") or {})
        budget_tokens = float(alloc.get("budget_tokens", 0))
        return cls(
            oracle_url=os.environ.get("ORCHARD_ORACLE_URL")
                or (cfg.get("oracle") or {}).get("url", "https://oracle.theorchard.network"),
            asset_id=os.environ.get("ORCHARD_ASSET_ID") or token.get("asset_id", ""),
            budget_mojos=_env_int("ORCHARD_ALLOC_BUDGET_MOJOS",
                                  int(round(budget_tokens * MOJOS_PER_TOKEN))),
            period_hours=_env_int("ORCHARD_ALLOC_PERIOD_HOURS",
                                  int(alloc.get("period_hours", 24))),
            stale_after_hours=_env_int("ORCHARD_ALLOC_STALE_HOURS",
                                       int(alloc.get("stale_after_hours", 3))),
            min_uptime_bp=_env_int("ORCHARD_ALLOC_MIN_UPTIME_BP",
                                   int(alloc.get("min_uptime_bp", 0))),
            # DELIBERATELY environment-only, and deliberately NOT defaulted from
            # the budget. A ceiling that lives in the same file as the number it
            # bounds is not a ceiling: the edit that moves the budget moves the
            # limit with it, so a misplaced digit is still a 100x payout, now
            # with a safety rule that appears to have approved it. 0 means
            # "unset", which a live run refuses rather than interprets.
            max_per_cycle_mojos=_env_int("ORCHARD_ALLOC_MAX_CYCLE_MOJOS", 0),
            max_per_wallet_mojos=_env_int("ORCHARD_ALLOC_MAX_WALLET_MOJOS", 0),
            min_payout_mojos=_env_int("ORCHARD_ALLOC_MIN_PAYOUT_MOJOS",
                                      int(alloc.get("min_payout_mojos", 1))),
            fee_mojos=_env_int("ORCHARD_ALLOC_FEE_MOJOS", int(alloc.get("fee_mojos", 0))),
            dry_run=_env_bool("DRY_RUN", True),
            db_path=Path(os.environ.get("ORCHARD_ALLOC_DB", str(DEFAULT_DB))),
            pause_file=Path(os.environ.get("ORCHARD_ALLOC_PAUSE_FILE", str(DEFAULT_PAUSE))),
            wallet_id=(_env_int("ORCHARD_ALLOC_WALLET_ID", 0) or None),
            interval_seconds=_env_int("ORCHARD_ALLOC_INTERVAL_SECONDS",
                                      int(alloc.get("interval_seconds", 86400))),
        )

    def validate(self) -> list[str]:
        problems = []
        if not self.asset_id:
            problems.append("no token asset_id configured — refusing to guess which CAT to send")
        if self.budget_mojos < 0:
            problems.append(f"budget_mojos is negative ({self.budget_mojos})")
        if not self.dry_run:
            if self.wallet_id is None:
                problems.append("a live run needs ORCHARD_ALLOC_WALLET_ID (the CAT wallet)")
            for name, value in (("ORCHARD_ALLOC_MAX_CYCLE_MOJOS", self.max_per_cycle_mojos),
                                ("ORCHARD_ALLOC_MAX_WALLET_MOJOS", self.max_per_wallet_mojos)):
                if value <= 0:
                    problems.append(
                        f"{name} is not set. A live run must be bounded by a limit "
                        f"that lives somewhere other than the file holding the "
                        f"budget, so that editing the budget cannot quietly raise "
                        f"the ceiling too.")
        if self.max_per_cycle_mojos and self.max_per_cycle_mojos < self.budget_mojos:
            problems.append(
                f"max_per_cycle_mojos ({self.max_per_cycle_mojos}) is below the "
                f"budget ({self.budget_mojos}); every cycle would be blocked")
        if self.min_uptime_bp < 0 or self.min_uptime_bp > BASIS_POINTS_FULL:
            problems.append(f"min_uptime_bp out of range: {self.min_uptime_bp}")
        return problems

    def advisories(self) -> list[str]:
        """Non-fatal, but the operator should see them before going live."""
        out = []
        if self.dry_run and self.max_per_cycle_mojos <= 0:
            out.append("no per-cycle ceiling set — a live run will refuse until "
                       "ORCHARD_ALLOC_MAX_CYCLE_MOJOS is exported")
        if self.dry_run and self.max_per_wallet_mojos <= 0:
            out.append("no per-wallet ceiling set — a live run will refuse until "
                       "ORCHARD_ALLOC_MAX_WALLET_MOJOS is exported")
        return out


@dataclass
class CycleOutcome:
    collection: CollectionResult
    plan: SpendPlan
    report: ExecutionReport | None


def run_cycle(settings: Settings, *, source=None, spender=None,
              now: datetime | None = None) -> CycleOutcome:
    """One payout cycle, end to end. Safe to call repeatedly."""
    problems = settings.validate()
    if problems:
        raise SystemExit("configuration refused:\n  - " + "\n  - ".join(problems))

    now = now or datetime.now(timezone.utc)
    source = source or OracleUptimeSource(settings.oracle_url)

    # Held across collect/plan/execute, not just around the spend: the
    # duplicate check happens at plan time, so two processes that overlap
    # anywhere in that window can both decide a wallet is unpaid.
    with RunLock(settings.db_path.with_suffix(".lock")):
        return _run_cycle_locked(settings, source, spender, now)


def _run_cycle_locked(settings: Settings, source, spender,
                      now: datetime) -> CycleOutcome:
    collection = collect(
        source, now=now, period_hours=settings.period_hours,
        stale_after_hours=settings.stale_after_hours,
        min_uptime_bp=settings.min_uptime_bp)

    result = allocate(list(collection.records), settings.budget_mojos)

    with audit_mod.AuditStore(settings.db_path) as store:
        balance = spender.spendable_balance() if spender else None
        the_plan = plan(
            result, store=store, asset_id=settings.asset_id,
            period_start=collection.period_start, period_end=collection.period_end,
            limits=PlannerLimits(
                max_per_cycle_mojos=settings.max_per_cycle_mojos or (1 << 62),
                max_per_wallet_mojos=settings.max_per_wallet_mojos or (1 << 62),
                min_payout_mojos=settings.min_payout_mojos,
                fee_mojos=settings.fee_mojos,
                pause_file=settings.pause_file),
            available_balance_mojos=balance,
            uptime_basis=collection.uptime_basis,
            dry_run=settings.dry_run)

        persist(the_plan, result, collection.records, store, collection.uptime_basis)
        report = execute(the_plan, store=store, spender=spender)

    return CycleOutcome(collection=collection, plan=the_plan, report=report)


def render_report(outcome: CycleOutcome, settings: Settings) -> str:
    """The dry-run report: exactly what would be spent, and why."""
    c, p, r = outcome.collection, outcome.plan, outcome.report
    tok = lambda m: f"{m / MOJOS_PER_TOKEN:,.3f}"          # noqa: E731
    L: list[str] = []
    L.append("=" * 74)
    L.append(f"ALLOCATION CYCLE {p.cycle_id[:16]}   "
             f"{'DRY RUN — nothing sent' if p.dry_run else '*** LIVE ***'}")
    L.append("=" * 74)
    L.append(f"  period      {c.period_start:%Y-%m-%d %H:%M} .. {c.period_end:%Y-%m-%d %H:%M} UTC")
    L.append(f"  budget      {tok(p.budget_mojos)} tokens ({p.budget_mojos} mojos)")
    L.append(f"  asset       {p.asset_id[:16]}…")
    L.append(f"  uptime from {c.source}  (basis: {c.uptime_basis})")
    L.append("")

    L.append(f"  MEASURED — {len(c.records)} tree/sensor pair(s)")
    for rec in sorted(c.records, key=lambda x: (not x.eligible, x.tree_id)):
        mark = " " if rec.eligible else "x"
        why = "" if rec.eligible else f"   [{rec.exclusion_reason}]"
        L.append(f"   {mark} {rec.tree_id[:12]:12} {rec.sensor_id[:18]:18} "
                 f"{rec.uptime_bp / 100:6.2f}%{why}")
    for node_id, why in c.skipped:
        L.append(f"   - {node_id[:12]:12} {'':18} {'':7} [{why}]")
    L.append("")

    if p.instructions:
        L.append(f"  WOULD PAY — {len(p.instructions)} wallet(s)")
        L.append(f"   {'wallet':<26} {'avg uptime':>11} {'pairs':>6} {'amount':>14}")
        for i in sorted(p.instructions, key=lambda x: -x.amount_mojos):
            # Fraction("9000/2") round-trips exactly; eval() would too, and
            # would also execute whatever else ended up in that column.
            avg = float(Fraction(i.wallet_avg_uptime))
            L.append(f"   {i.wallet_address[:24]:<26} {avg / 100:10.2f}% "
                     f"{i.pair_count:6d} {tok(i.amount_mojos):>14}")
        L.append(f"   {'':<26} {'':>11} {'TOTAL':>6} {tok(p.total_mojos):>14}")
    else:
        L.append("  WOULD PAY — nothing")
    L.append("")

    if p.dropped:
        L.append("  NOT PAID")
        for w, m, why in p.dropped:
            L.append(f"   - {w[:24]:<26} {tok(m):>12}   {why}")
        L.append("")

    if p.warnings:
        for w in p.warnings:
            L.append(f"  note: {w}")
        L.append("")

    if p.blocked_by:
        L.append("  BLOCKED — nothing was sent")
        for b in p.blocked_by:
            L.append(f"   ! {b}")
    elif r and not r.dry_run:
        L.append(f"  SENT {tok(r.sent_mojos)} tokens in {len(r.sent)} transaction(s)")
        for w, m, tx in r.sent:
            L.append(f"   -> {w[:24]:<26} {tok(m):>12}  tx {tx[:20]}…")
        for w, m, e in r.failed:
            L.append(f"   !! {w[:24]:<26} {tok(m):>12}  FAILED {e}")
        if r.halted_reason:
            L.append(f"   !! HALTED: {r.halted_reason}")
    L.append("=" * 74)
    return "\n".join(L)


def run_scheduler(settings: Settings, *, source=None, spender=None,
                  max_cycles: int | None = None, sleep=time.sleep,
                  now_fn=lambda: datetime.now(timezone.utc),
                  on_cycle=None) -> int:
    """Run cycles forever at ``interval_seconds``.

    A failing cycle does NOT stop the scheduler — the next one re-derives
    everything from the oracle, and a transient outage should not need a human
    to restart a timer. A HALT does stop it, because a halt means we do not
    know whether money moved, and continuing past that is the one thing
    automation must never do.
    """
    cycles = 0
    while max_cycles is None or cycles < max_cycles:
        cycles += 1
        try:
            outcome = run_cycle(settings, source=source, spender=spender, now=now_fn())
            if on_cycle:
                on_cycle(outcome)
            if outcome.report and outcome.report.halted_reason:
                return 3
        except SystemExit:
            raise
        except Exception as e:                      # noqa: BLE001
            if on_cycle:
                on_cycle(e)
        if max_cycles is None or cycles < max_cycles:
            sleep(settings.interval_seconds)
    return 0
