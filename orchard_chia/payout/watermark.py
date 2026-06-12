# SPDX-License-Identifier: Apache-2.0
"""Watermark — local SQLite recording which ``(node_id, season)`` pairs
have already been paid out.

Prevents double-payment if the operator runs the payout multiple times.
Lives at ``orchard_chia/data/payout_watermark.db`` (gitignored).

Schema is intentionally minimal — Phase 7 owns this state entirely;
the oracle doesn't know about it. If the file is lost, the worst case
is that you pay the same Season twice; the safer recovery is to
restore from your backup of the file.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS paid_attestations (
    node_id          TEXT    NOT NULL,
    season           INTEGER NOT NULL,
    wallet_address   TEXT    NOT NULL,
    paid_mojos       INTEGER NOT NULL,
    paid_at          TEXT    NOT NULL,
    tx_id            TEXT,
    PRIMARY KEY (node_id, season)
);

CREATE INDEX IF NOT EXISTS idx_paid_wallet
    ON paid_attestations(wallet_address);
"""


class Watermark:
    def __init__(self, db_path: str | Path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Watermark":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def is_paid(self, node_id: str, season: int) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM paid_attestations WHERE node_id=? AND season=?",
            (node_id.upper(), int(season)),
        )
        return cur.fetchone() is not None

    def get_paid_amount(self, node_id: str, season: int) -> int | None:
        cur = self._conn.execute(
            "SELECT paid_mojos FROM paid_attestations "
            "WHERE node_id=? AND season=?",
            (node_id.upper(), int(season)),
        )
        row = cur.fetchone()
        return int(row["paid_mojos"]) if row else None

    def all_paid(self) -> list[dict]:
        cur = self._conn.execute(
            "SELECT node_id, season, wallet_address, paid_mojos, paid_at, tx_id "
            "FROM paid_attestations ORDER BY paid_at DESC"
        )
        return [dict(r) for r in cur.fetchall()]

    def total_paid_to_wallet(self, wallet_address: str) -> int:
        cur = self._conn.execute(
            "SELECT COALESCE(SUM(paid_mojos), 0) AS total "
            "FROM paid_attestations WHERE wallet_address=?",
            (wallet_address,),
        )
        return int(cur.fetchone()["total"])

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def record_payment(
        self,
        *,
        node_id: str,
        season: int,
        wallet_address: str,
        paid_mojos: int,
        tx_id: str | None = None,
    ) -> None:
        """Idempotent — if a record already exists for (node, season),
        it is left untouched. Caller should check ``is_paid()`` first
        if they want to error on double-write.
        """
        self._conn.execute(
            "INSERT OR IGNORE INTO paid_attestations "
            "(node_id, season, wallet_address, paid_mojos, paid_at, tx_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                node_id.upper(),
                int(season),
                wallet_address,
                int(paid_mojos),
                datetime.now(timezone.utc).isoformat(),
                tx_id,
            ),
        )
        self._conn.commit()

    def set_tx(self, node_id: str, season: int, tx_id: str | None) -> None:
        """Attach/replace the tx_id on an already-recorded payment.

        The payout marks a ``(node, season)`` provisionally (tx_id=None)
        *before* broadcasting the spend — so a crash after the tx is sent
        can't double-pay on the next run — then calls this to record the
        confirmed tx_id once ``cat_spend`` returns.
        """
        self._conn.execute(
            "UPDATE paid_attestations SET tx_id=? WHERE node_id=? AND season=?",
            (tx_id, node_id.upper(), int(season)),
        )
        self._conn.commit()
