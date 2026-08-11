# SPDX-License-Identifier: Apache-2.0
"""The audit store, and the duplicate-spend guard.

These are one module because they are the same fact. "Has this already been
paid?" is a question about the audit record, and if the answer lives anywhere
else the two can disagree — which is precisely how something gets paid twice.

WHAT IS WRITTEN, AND WHEN
=========================

A cycle passes through three states and each transition is committed before the
next action is attempted:

    planned  -> the plan exists, nothing has been sent
    sending  -> a transaction has been handed to the wallet for THIS wallet
                address; whether it was accepted is not yet known
    settled  -> every instruction has a terminal outcome

The ``sending`` row is the important one. It is written BEFORE the spend RPC,
not after, because a process that dies mid-spend must leave evidence that it
tried. A record written after the call would be absent in exactly the case
where it matters most: the transaction went out and the crash hid it. On
restart, an instruction found in ``sending`` is never re-sent — it is surfaced
for a human, because the only safe automatic action when you cannot tell
whether money moved is to stop.

IDEMPOTENCY
===========

Every instruction carries a key derived from the cycle identity and the
recipient. The key is a UNIQUE column, so a second attempt to insert it fails
at the database rather than at a code path someone might refactor away.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cycles (
    cycle_id         TEXT    PRIMARY KEY,
    created_at       TEXT    NOT NULL,
    period_start     TEXT    NOT NULL,
    period_end       TEXT    NOT NULL,
    budget_mojos     INTEGER NOT NULL,
    allocated_mojos  INTEGER NOT NULL,
    total_weight     TEXT    NOT NULL,
    asset_id         TEXT    NOT NULL,
    uptime_basis     TEXT    NOT NULL,
    dry_run          INTEGER NOT NULL,
    state            TEXT    NOT NULL,
    notes            TEXT
);

CREATE TABLE IF NOT EXISTS cycle_inputs (
    cycle_id            TEXT    NOT NULL,
    tree_id             TEXT    NOT NULL,
    sensor_id           TEXT    NOT NULL,
    wallet_address      TEXT    NOT NULL,
    raw_uptime_bp       INTEGER NOT NULL,
    eligible            INTEGER NOT NULL,
    exclusion_reason    TEXT,
    PRIMARY KEY (cycle_id, tree_id, sensor_id)
);

CREATE TABLE IF NOT EXISTS instructions (
    cycle_id            TEXT    NOT NULL,
    idempotency_key     TEXT    NOT NULL UNIQUE,
    wallet_address      TEXT    NOT NULL,
    amount_mojos        INTEGER NOT NULL,
    wallet_avg_uptime   TEXT    NOT NULL,
    pair_count          INTEGER NOT NULL,
    state               TEXT    NOT NULL,
    tx_id               TEXT,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    sent_at             TEXT,
    settled_at          TEXT,
    confirmed           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (cycle_id, wallet_address)
);

CREATE INDEX IF NOT EXISTS idx_instr_state ON instructions(state);
CREATE INDEX IF NOT EXISTS idx_instr_wallet ON instructions(wallet_address);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    cycle_id    TEXT,
    wallet      TEXT,
    kind        TEXT NOT NULL,
    detail      TEXT
);
"""

PLANNED, SENDING, SENT, FAILED, SKIPPED = (
    "planned", "sending", "sent", "failed", "skipped")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cycle_id_for(*, period_start: datetime, period_end: datetime,
                 budget_mojos: int, asset_id: str) -> str:
    """A cycle's identity, derived from what it is rather than when it ran.

    Two runs over the same period with the same budget and asset ARE the same
    cycle and must collide, so that re-running a crashed job resumes it instead
    of paying it again. A timestamp or a random id would make every run unique,
    which is the same as having no idempotency at all.
    """
    material = "|".join([
        period_start.astimezone(timezone.utc).isoformat(),
        period_end.astimezone(timezone.utc).isoformat(),
        str(int(budget_mojos)),
        asset_id.lower(),
    ])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def instruction_key(cycle_id: str, wallet_address: str) -> str:
    return hashlib.sha256(
        f"{cycle_id}|{wallet_address}".encode("utf-8")).hexdigest()[:32]


@dataclass(frozen=True)
class InstructionRow:
    cycle_id: str
    idempotency_key: str
    wallet_address: str
    amount_mojos: int
    state: str
    tx_id: str | None
    attempts: int
    last_error: str | None
    confirmed: bool


class AuditStore:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._c = sqlite3.connect(self.path)
        self._c.row_factory = sqlite3.Row
        self._c.executescript(SCHEMA)
        # A payout ledger that loses a write to a crash is worse than no ledger,
        # because it looks authoritative. WAL plus a full fsync is the cheap
        # insurance and this table is written a handful of times per cycle.
        self._c.execute("PRAGMA journal_mode=WAL")
        self._c.execute("PRAGMA synchronous=FULL")
        self._c.commit()

    def close(self) -> None:
        self._c.close()

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    # --- cycles ------------------------------------------------------------

    def cycle_exists(self, cycle_id: str) -> bool:
        return self._c.execute("SELECT 1 FROM cycles WHERE cycle_id=?",
                               (cycle_id,)).fetchone() is not None

    def get_cycle(self, cycle_id: str) -> sqlite3.Row | None:
        return self._c.execute("SELECT * FROM cycles WHERE cycle_id=?",
                               (cycle_id,)).fetchone()

    def open_cycle(self, *, cycle_id: str, period_start: datetime,
                   period_end: datetime, budget_mojos: int,
                   allocated_mojos: int, total_weight: str, asset_id: str,
                   uptime_basis: str, dry_run: bool, notes: str = "") -> None:
        self._c.execute(
            "INSERT OR REPLACE INTO cycles (cycle_id, created_at, period_start, "
            "period_end, budget_mojos, allocated_mojos, total_weight, asset_id, "
            "uptime_basis, dry_run, state, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cycle_id, _now(), period_start.isoformat(), period_end.isoformat(),
             int(budget_mojos), int(allocated_mojos), str(total_weight),
             asset_id.lower(), uptime_basis, 1 if dry_run else 0, PLANNED, notes))
        self._c.commit()

    def set_cycle_state(self, cycle_id: str, state: str) -> None:
        self._c.execute("UPDATE cycles SET state=? WHERE cycle_id=?", (state, cycle_id))
        self._c.commit()

    def record_inputs(self, cycle_id: str, records) -> None:
        """Every record considered, eligible or not, with its raw uptime."""
        self._c.executemany(
            "INSERT OR REPLACE INTO cycle_inputs (cycle_id, tree_id, sensor_id, "
            "wallet_address, raw_uptime_bp, eligible, exclusion_reason) "
            "VALUES (?,?,?,?,?,?,?)",
            [(cycle_id, r.tree_id, r.sensor_id, r.wallet_address, r.uptime_bp,
              1 if r.eligible else 0, r.exclusion_reason) for r in records])
        self._c.commit()

    # --- instructions ------------------------------------------------------

    def put_instruction(self, *, cycle_id: str, wallet_address: str,
                        amount_mojos: int, wallet_avg_uptime: str,
                        pair_count: int, state: str = PLANNED) -> str:
        key = instruction_key(cycle_id, wallet_address)
        self._c.execute(
            "INSERT OR IGNORE INTO instructions (cycle_id, idempotency_key, "
            "wallet_address, amount_mojos, wallet_avg_uptime, pair_count, state) "
            "VALUES (?,?,?,?,?,?,?)",
            (cycle_id, key, wallet_address, int(amount_mojos),
             str(wallet_avg_uptime), int(pair_count), state))
        self._c.commit()
        return key

    def instructions(self, cycle_id: str) -> list[InstructionRow]:
        rows = self._c.execute(
            "SELECT * FROM instructions WHERE cycle_id=? ORDER BY wallet_address",
            (cycle_id,)).fetchall()
        return [InstructionRow(
            cycle_id=r["cycle_id"], idempotency_key=r["idempotency_key"],
            wallet_address=r["wallet_address"], amount_mojos=r["amount_mojos"],
            state=r["state"], tx_id=r["tx_id"], attempts=r["attempts"],
            last_error=r["last_error"], confirmed=bool(r["confirmed"]),
        ) for r in rows]

    def mark_sending(self, cycle_id: str, wallet: str) -> None:
        """Written BEFORE the spend RPC. See the module docstring."""
        self._c.execute(
            "UPDATE instructions SET state=?, attempts=attempts+1, sent_at=? "
            "WHERE cycle_id=? AND wallet_address=?",
            (SENDING, _now(), cycle_id, wallet))
        self._c.commit()

    def mark_sent(self, cycle_id: str, wallet: str, tx_id: str) -> None:
        self._c.execute(
            "UPDATE instructions SET state=?, tx_id=?, settled_at=?, last_error=NULL "
            "WHERE cycle_id=? AND wallet_address=?",
            (SENT, tx_id, _now(), cycle_id, wallet))
        self._c.commit()

    def mark_failed(self, cycle_id: str, wallet: str, error: str) -> None:
        self._c.execute(
            "UPDATE instructions SET state=?, last_error=?, settled_at=? "
            "WHERE cycle_id=? AND wallet_address=?",
            (FAILED, error[:500], _now(), cycle_id, wallet))
        self._c.commit()

    def mark_skipped(self, cycle_id: str, wallet: str, why: str) -> None:
        self._c.execute(
            "UPDATE instructions SET state=?, last_error=?, settled_at=? "
            "WHERE cycle_id=? AND wallet_address=?",
            (SKIPPED, why[:500], _now(), cycle_id, wallet))
        self._c.commit()

    def mark_confirmed(self, cycle_id: str, wallet: str) -> None:
        self._c.execute(
            "UPDATE instructions SET confirmed=1 WHERE cycle_id=? AND wallet_address=?",
            (cycle_id, wallet))
        self._c.commit()

    def in_flight(self) -> list[InstructionRow]:
        """Instructions left in `sending` — we do not know if money moved.

        Any of these blocks the next cycle. Resolving one is a human act.
        """
        rows = self._c.execute(
            "SELECT * FROM instructions WHERE state=? ORDER BY sent_at", (SENDING,)
        ).fetchall()
        return [InstructionRow(
            cycle_id=r["cycle_id"], idempotency_key=r["idempotency_key"],
            wallet_address=r["wallet_address"], amount_mojos=r["amount_mojos"],
            state=r["state"], tx_id=r["tx_id"], attempts=r["attempts"],
            last_error=r["last_error"], confirmed=bool(r["confirmed"]),
        ) for r in rows]

    def already_paid(self, cycle_id: str, wallet: str) -> bool:
        r = self._c.execute(
            "SELECT state FROM instructions WHERE cycle_id=? AND wallet_address=?",
            (cycle_id, wallet)).fetchone()
        return bool(r) and r["state"] in (SENT, SENDING)

    def total_sent_mojos(self, cycle_id: str) -> int:
        r = self._c.execute(
            "SELECT COALESCE(SUM(amount_mojos),0) AS t FROM instructions "
            "WHERE cycle_id=? AND state IN (?,?)", (cycle_id, SENT, SENDING)).fetchone()
        return int(r["t"])

    # --- events ------------------------------------------------------------

    def event(self, kind: str, *, cycle_id: str | None = None,
              wallet: str | None = None, **detail) -> None:
        self._c.execute(
            "INSERT INTO events (ts, cycle_id, wallet, kind, detail) VALUES (?,?,?,?,?)",
            (_now(), cycle_id, wallet, kind,
             json.dumps(detail, sort_keys=True, default=str)))
        self._c.commit()

    def events(self, cycle_id: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        if cycle_id:
            return self._c.execute(
                "SELECT * FROM events WHERE cycle_id=? ORDER BY id DESC LIMIT ?",
                (cycle_id, limit)).fetchall()
        return self._c.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
