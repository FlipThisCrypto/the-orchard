# SPDX-License-Identifier: Apache-2.0
"""The pool ledger — the durable memory of what has ever been emitted.

The reward calculation is pure; this is the state it runs against. One SQLite
file records every settled day, and the pool balance is DERIVED — always
``85,000,000 - sum(distributed)`` — never stored as a mutable number that
drift, a crash, or a hand edit could quietly corrupt. To falsify the balance
you would have to falsify the day rows it is computed from, which is exactly
the property an auditor wants.

Append-only by construction: a day can be settled once. Re-settling is refused
rather than replaced, because the second answer being different from the first
is precisely the situation in which neither should be trusted silently.

The fixed-supply invariant is enforced HERE, at write time, not merely tested:
a settlement that would push cumulative distribution past the Tree Rewards
Pool is refused whatever the calculation said. Defence in depth — the
arithmetic already cannot produce it, but the ledger is the last line and the
one whose refusal survives a bug in everything upstream.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .constants import MODEL_VERSION, TREE_REWARDS_POOL_MOJOS
from .settlement import Settlement

SCHEMA = """
CREATE TABLE IF NOT EXISTS settled_days (
    day_index          INTEGER PRIMARY KEY,
    settled_at         TEXT    NOT NULL,
    model_version      TEXT    NOT NULL,
    ceiling_mojos      INTEGER NOT NULL,
    distributed_mojos  INTEGER NOT NULL,
    unearned_mojos     INTEGER NOT NULL,
    eligible_trees     INTEGER NOT NULL,
    pool_closing_mojos INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS day_rewards (
    day_index       INTEGER NOT NULL,
    tree_id         TEXT    NOT NULL,
    wallet_address  TEXT    NOT NULL,
    sensor_weight   TEXT    NOT NULL,
    heartbeats      INTEGER NOT NULL,
    reward_mojos    INTEGER NOT NULL,
    PRIMARY KEY (day_index, tree_id)
);
"""


class LedgerError(RuntimeError):
    pass


@dataclass(frozen=True)
class PoolSnapshot:
    distributed_total_mojos: int
    remaining_mojos: int
    days_settled: int
    last_day_index: int | None


class PoolLedger:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(self.path)
        self._c.row_factory = sqlite3.Row
        self._c.executescript(SCHEMA)
        # This file IS the answer to "how much has ever been emitted". Losing a
        # committed row to a crash would understate distribution and let a
        # re-run pay a day twice, so it gets full durability.
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA synchronous=FULL")
        self._c.commit()

    def close(self) -> None:
        self._c.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()

    # -- reads ---------------------------------------------------------------

    def snapshot(self) -> PoolSnapshot:
        row = self._c.execute(
            "SELECT COALESCE(SUM(distributed_mojos),0) AS total, "
            "COUNT(*) AS n, MAX(day_index) AS last FROM settled_days"
        ).fetchone()
        total = int(row["total"])
        if total > TREE_REWARDS_POOL_MOJOS:
            raise LedgerError(
                f"ledger records {total} mojos distributed — more than the "
                f"Tree Rewards Pool itself. The fixed-supply invariant is "
                f"broken in the data; refusing every operation until a human "
                f"has looked.")
        return PoolSnapshot(
            distributed_total_mojos=total,
            remaining_mojos=TREE_REWARDS_POOL_MOJOS - total,
            days_settled=int(row["n"]),
            last_day_index=row["last"] if row["last"] is not None else None,
        )

    def is_settled(self, day_index: int) -> bool:
        return self._c.execute(
            "SELECT 1 FROM settled_days WHERE day_index=?", (day_index,)
        ).fetchone() is not None

    def day(self, day_index: int) -> sqlite3.Row | None:
        return self._c.execute(
            "SELECT * FROM settled_days WHERE day_index=?", (day_index,)
        ).fetchone()

    # -- the one write -------------------------------------------------------

    def record(self, settlement: Settlement) -> PoolSnapshot:
        """Append a settled day. Refuses duplicates, gaps backwards, and any
        write that would breach the pool. One transaction: the day row and its
        per-Tree rewards land together or not at all."""
        day = settlement.day_index
        if self.is_settled(day):
            existing = self.day(day)
            if int(existing["distributed_mojos"]) == settlement.distributed_mojos:
                raise LedgerError(
                    f"day {day} is already settled with the same total. A "
                    f"re-run must read the ledger before settling, not settle "
                    f"again — refusing the duplicate rather than hiding it.")
            raise LedgerError(
                f"day {day} is already settled with a DIFFERENT total "
                f"({existing['distributed_mojos']} then, "
                f"{settlement.distributed_mojos} now). Two answers for one day "
                f"means inputs changed after settlement; neither should be "
                f"trusted silently. Human required.")

        snap = self.snapshot()
        if snap.last_day_index is not None and day < snap.last_day_index:
            raise LedgerError(
                f"day {day} precedes already-settled day "
                f"{snap.last_day_index}. Settling backwards would mean the "
                f"pool balance used for that later day was wrong.")

        new_total = snap.distributed_total_mojos + settlement.distributed_mojos
        if new_total > TREE_REWARDS_POOL_MOJOS:
            raise LedgerError(
                f"settling day {day} would take cumulative distribution to "
                f"{new_total}, past the {TREE_REWARDS_POOL_MOJOS} pool. The "
                f"calculation upstream is wrong; the ledger will not record "
                f"an impossible emission.")

        with self._c:
            self._c.execute(
                "INSERT INTO settled_days (day_index, settled_at, model_version, "
                "ceiling_mojos, distributed_mojos, unearned_mojos, "
                "eligible_trees, pool_closing_mojos) VALUES (?,?,?,?,?,?,?,?)",
                (day, datetime.now(timezone.utc).isoformat(), MODEL_VERSION,
                 settlement.ceiling.ceiling_mojos, settlement.distributed_mojos,
                 settlement.unearned_mojos, len(settlement.rewards.rewards),
                 settlement.pool_closing_mojos))
            self._c.executemany(
                "INSERT INTO day_rewards (day_index, tree_id, wallet_address, "
                "sensor_weight, heartbeats, reward_mojos) VALUES (?,?,?,?,?,?)",
                [(day, r.tree_id, r.wallet_address, str(r.sensor_weight),
                  r.verified_heartbeats, r.reward_mojos)
                 for r in settlement.rewards.rewards])
        return self.snapshot()
