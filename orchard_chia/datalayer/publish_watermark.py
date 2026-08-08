# SPDX-License-Identifier: Apache-2.0
"""Watermark for the DataLayer hot-path publisher (SPEC §6).

Tracks which ``(node_id, season, hour)`` reading batches have already been
submitted to DataLayer so the hourly writer does not re-scan from genesis.

Lives at ``orchard_chia/data/publish_watermark.db`` (gitignored). Distinct from
the payout watermark: this one records *publish* progress, not payments.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS published_hours (
    node_id     TEXT    NOT NULL,
    season      INTEGER NOT NULL,
    hour        INTEGER NOT NULL,
    hour_root   TEXT,
    published_at TEXT   NOT NULL,
    tx_id       TEXT,
    PRIMARY KEY (node_id, season, hour)
);

CREATE INDEX IF NOT EXISTS idx_published_node_season
    ON published_hours(node_id, season);
"""


DEFAULT_BUSY_TIMEOUT_MS = 5000


class PublishWatermark:
    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        # Wait for a held lock rather than crashing with "database is locked"
        # when an overlapping publisher run (cron firing while a slow run is
        # still going) touches the same file.
        self._conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "PublishWatermark":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def is_published(self, node_id: str, season: int, hour: int) -> bool:
        cur = self._conn.execute(
            "SELECT 1 FROM published_hours "
            "WHERE node_id=? AND season=? AND hour=?",
            (node_id.upper(), int(season), int(hour)),
        )
        return cur.fetchone() is not None

    def last_published_hour(self, node_id: str, season: int) -> int | None:
        """Highest published hour for a node·season, or None if none yet."""
        cur = self._conn.execute(
            "SELECT MAX(hour) AS h FROM published_hours "
            "WHERE node_id=? AND season=?",
            (node_id.upper(), int(season)),
        )
        row = cur.fetchone()
        if row is None or row["h"] is None:
            return None
        return int(row["h"])

    def published_hours_count(self, node_id: str, season: int) -> int:
        """How many hours this node has ACTUALLY had published in a season.

        The honest source for ``latest:.running_hours_online``. That field was
        previously ``hour + 1`` — the UTC hour-of-day index, which has nothing
        to do with uptime: publishing a single hour numbered 14 asserted "15
        hours online". Caught on the first real publish, where the store proved
        exactly one hour and the node had only existed for 12.25 hours, so the
        claim exceeded even the physical ceiling.

        A count of rows in this table is measurable and provable: every row is
        an hour whose readings were written and confirmed on chain.
        """
        cur = self._conn.execute(
            "SELECT COUNT(*) AS n FROM published_hours "
            "WHERE node_id=? AND season=?",
            (node_id.upper(), int(season)),
        )
        row = cur.fetchone()
        return int(row["n"] if row is not None else 0)

    def record(
        self,
        *,
        node_id: str,
        season: int,
        hour: int,
        hour_root: str | None = None,
        tx_id: str | None = None,
    ) -> None:
        """Idempotent insert for a published hour batch."""
        self._conn.execute(
            "INSERT OR IGNORE INTO published_hours "
            "(node_id, season, hour, hour_root, published_at, tx_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                node_id.upper(),
                int(season),
                int(hour),
                hour_root,
                datetime.now(timezone.utc).isoformat(),
                tx_id,
            ),
        )
        self._conn.commit()

    def last_tx(self, node_id: str, season: int, hour: int) -> str | None:
        cur = self._conn.execute(
            'SELECT tx_id FROM published_hours WHERE node_id=? AND season=? AND hour=?',
            (node_id.upper(), int(season), int(hour)),
        )
        row = cur.fetchone()
        return row['tx_id'] if row else None

    def count(self) -> int:
        cur = self._conn.execute('SELECT COUNT(*) AS n FROM published_hours')
        return int(cur.fetchone()[0])

    def set_tx(
        self,
        node_id: str,
        season: int,
        hour: int,
        tx_id: str | None,
    ) -> None:
        self._conn.execute(
            "UPDATE published_hours SET tx_id=? "
            "WHERE node_id=? AND season=? AND hour=?",
            (tx_id, node_id.upper(), int(season), int(hour)),
        )
        self._conn.commit()
