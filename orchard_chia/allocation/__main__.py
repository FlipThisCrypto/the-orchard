# SPDX-License-Identifier: Apache-2.0
"""CLI for the allocation service.

    python -m orchard_chia.allocation report          # dry run, print the plan
    python -m orchard_chia.allocation run             # one cycle (DRY_RUN honoured)
    python -m orchard_chia.allocation serve           # scheduled cycles
    python -m orchard_chia.allocation confirmations <cycle_id>
    python -m orchard_chia.allocation history
    python -m orchard_chia.allocation pause / resume

Going live takes two deliberate acts, not one: ``DRY_RUN=false`` in the
environment AND ``--i-understand-this-spends-real-tokens`` on the command line.
One flag is too easy to leave in a shell history, a systemd unit, or a script
someone copied. This is the same lesson as the ``--dry-run`` that did not exist
and wrote 185 records to the chain: a flag that is ignored, or set once and
forgotten, is not a safety feature.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import audit as audit_mod
from .service import (MOJOS_PER_TOKEN, Settings, render_report, run_cycle,
                      run_scheduler)

LIVE_FLAG = "--i-understand-this-spends-real-tokens"


def _load_config() -> dict:
    cfg_path = Path(__file__).resolve().parents[1] / "config.yaml"
    if not cfg_path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    with cfg_path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _spender(settings: Settings):
    """Build the wallet adapter. Only reached on a live run."""
    from .executor import build_spender
    cfg = _load_config()
    return build_spender(wallet_id=settings.wallet_id,
                         fee_mojos=settings.fee_mojos,
                         wallet_cfg=(cfg.get("wallet") or {}))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m orchard_chia.allocation")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, helptext in (("report", "collect + allocate + print, never sends"),
                           ("run", "one cycle"),
                           ("serve", "scheduled cycles at the configured interval")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument(LIVE_FLAG, action="store_true", dest="live_ack",
                        help="required alongside DRY_RUN=false to actually spend")
        if name == "serve":
            sp.add_argument("--max-cycles", type=int, default=None)
    cf = sub.add_parser("confirmations", help="ask the wallet what settled")
    cf.add_argument("cycle_id")
    sub.add_parser("history", help="recent cycles")
    sub.add_parser("pause", help="stop all future cycles")
    sub.add_parser("resume", help="allow cycles again")

    args = p.parse_args(argv)
    settings = Settings.from_env(_load_config())

    if args.cmd == "pause":
        settings.pause_file.parent.mkdir(parents=True, exist_ok=True)
        settings.pause_file.write_text("paused\n", encoding="utf-8")
        print(f"PAUSED — {settings.pause_file}\nNo cycle will plan or send until "
              f"this file is removed.")
        return 0

    if args.cmd == "resume":
        if settings.pause_file.exists():
            settings.pause_file.unlink()
            print(f"resumed — removed {settings.pause_file}")
        else:
            print("not paused")
        return 0

    if args.cmd == "history":
        with audit_mod.AuditStore(settings.db_path) as store:
            rows = store._c.execute(
                "SELECT * FROM cycles ORDER BY created_at DESC LIMIT 20").fetchall()
            if not rows:
                print("no cycles recorded")
                return 0
            print(f"{'cycle':18} {'when':20} {'budget':>12} {'allocated':>12}  state")
            for r in rows:
                print(f"{r['cycle_id'][:16]:18} {r['created_at'][:19]:20} "
                      f"{r['budget_mojos'] / MOJOS_PER_TOKEN:12,.3f} "
                      f"{r['allocated_mojos'] / MOJOS_PER_TOKEN:12,.3f}  "
                      f"{r['state']}{' (dry)' if r['dry_run'] else ''}")
            stuck = store.in_flight()
            if stuck:
                print(f"\n!! {len(stuck)} instruction(s) mid-send — cycles are "
                      f"blocked until resolved:")
                for s in stuck:
                    print(f"   {s.wallet_address}  {s.amount_mojos} mojos  "
                          f"cycle {s.cycle_id[:12]}  tx={s.tx_id or 'unknown'}")
        return 0

    if args.cmd == "confirmations":
        with audit_mod.AuditStore(settings.db_path) as store:
            from .executor import track_confirmations
            got = track_confirmations(args.cycle_id, store=store,
                                      spender=_spender(settings))
        for wallet, ok in sorted(got.items()):
            print(f"  {'confirmed' if ok else 'pending  '}  {wallet}")
        return 0

    # report / run / serve
    force_dry = args.cmd == "report"
    # SUPERSEDED MODEL — spending is disarmed, same rule as the legacy payout
    # CLI. This service allocates by each WALLET's mean Tree uptime; the
    # ratified model (docs/token/EMISSION.md) is
    # `python -m orchard_chia.economics pay`. Reports and dry runs remain; a
    # live spend under a dead model must be unmistakably deliberate.
    import os as _os
    if (not force_dry and not settings.dry_run
            and _os.environ.get("ORCHARD_ALLOCATION_SUPERSEDED_MODEL_ACK", "")
            != "i-know"):
        print(
            "allocation REFUSED: this spends under the SUPERSEDED wallet-mean "
            "model.\nThe current model is:  python -m orchard_chia.economics "
            "pay\nTo proceed anyway set "
            "ORCHARD_ALLOCATION_SUPERSEDED_MODEL_ACK=i-know.", file=sys.stderr)
        return 2
    if not force_dry and not settings.dry_run and not args.live_ack:
        print(
            f"DRY_RUN is false but {LIVE_FLAG} was not given.\n"
            f"Refusing to spend on one flag alone — going live is meant to take "
            f"two deliberate acts.\n"
            f"Nothing was collected, planned, or sent.", file=sys.stderr)
        return 2

    if force_dry:
        settings = Settings(**{**settings.__dict__, "dry_run": True})

    spender = None if settings.dry_run else _spender(settings)

    if args.cmd == "serve":
        def report(outcome):
            if isinstance(outcome, Exception):
                print(f"cycle failed: {type(outcome).__name__}: {outcome}",
                      file=sys.stderr)
            else:
                print(render_report(outcome, settings), flush=True)
        return run_scheduler(settings, spender=spender,
                             max_cycles=args.max_cycles, on_cycle=report)

    outcome = run_cycle(settings, spender=spender)
    print(render_report(outcome, settings))
    if outcome.report and outcome.report.halted_reason:
        return 3
    if outcome.plan.blocked_by:
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
