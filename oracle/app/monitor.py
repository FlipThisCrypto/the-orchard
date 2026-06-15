# SPDX-License-Identifier: Apache-2.0
"""Offline-Tree monitor (HANDOVER T13).

Flags Trees that have gone silent — no reading within a threshold (default
2h) — so a dead Tree is noticed before payout instead of at it. Designed to
run from cron / a systemd timer on the oracle host: it logs the silent set
and exits non-zero when any *previously-active* Tree is offline, so a wrapper
(or `OnFailure=` unit) can alert.

v1 is log-only and stateless — no email, no per-alert dedup table (each run
reports the current silent set). Email + a `node_alerts` table to avoid
re-alerting are noted follow-ups; both are additive and need no schema change
here. A Tree that has never reported is listed separately (informational) and
does NOT trip the exit code — it hasn't started yet, it didn't die.

Run from the repo root, same environment as the oracle:
    python -m oracle.app.monitor                 # 2h threshold, human output
    python -m oracle.app.monitor --threshold-minutes 60 --json
Example systemd timer: a .service running this every 30 min, with the unit's
failure (offline Trees -> exit 1) wired to your notification of choice.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from . import models
from .db import session_factory


@dataclass
class OfflineReport:
    now: datetime
    threshold_minutes: int
    offline: list[tuple[str, datetime, int]]  # (node_id, last_reading_at, minutes_silent)
    never_reported: list[str]                  # node_ids that never posted


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def find_offline(db, threshold: timedelta) -> OfflineReport:
    """Pure-ish core (takes a Session so it's testable): partition registered
    Trees into silent-after-active (offline) vs never-reported."""
    now = datetime.now(timezone.utc)
    cutoff = now - threshold
    offline: list[tuple[str, datetime, int]] = []
    never: list[str] = []
    for n in db.scalars(select(models.Node)):
        if n.last_reading_at is None:
            never.append(n.node_id)
            continue
        last = _aware(n.last_reading_at)
        if last < cutoff:
            mins = int((now - last).total_seconds() // 60)
            offline.append((n.node_id, last, mins))
    offline.sort(key=lambda t: t[2], reverse=True)  # most-silent first
    return OfflineReport(now, int(threshold.total_seconds() // 60), offline, never)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m oracle.app.monitor",
                                 description="Flag Trees silent beyond a threshold.")
    ap.add_argument("--threshold-minutes", type=int, default=120)
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    db = session_factory()()
    try:
        rep = find_offline(db, timedelta(minutes=args.threshold_minutes))
    finally:
        db.close()

    if args.json:
        print(json.dumps({
            "checked_at": rep.now.isoformat(),
            "threshold_minutes": rep.threshold_minutes,
            "offline": [{"node_id": nid, "last_reading_at": last.isoformat(),
                         "minutes_silent": mins} for nid, last, mins in rep.offline],
            "never_reported": rep.never_reported,
        }))
    else:
        if rep.offline:
            print(f"OFFLINE ({len(rep.offline)} Tree(s) silent > {rep.threshold_minutes}m):",
                  file=sys.stderr)
            for nid, last, mins in rep.offline:
                print(f"  {nid}  last={last.isoformat()}  silent={mins}m", file=sys.stderr)
        else:
            print(f"OK — no Tree silent > {rep.threshold_minutes}m")
        if rep.never_reported:
            print(f"(never reported: {len(rep.never_reported)} registered Tree(s) — "
                  f"not counted as offline)")

    # Non-zero exit iff a previously-active Tree went silent — the alert signal.
    return 1 if rep.offline else 0


if __name__ == "__main__":
    raise SystemExit(main())
