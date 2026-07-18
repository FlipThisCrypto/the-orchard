# SPDX-License-Identifier: Apache-2.0
"""Tests for tools.oracle_backup (stdlib only)."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

# Allow `python -m pytest tools/test_oracle_backup.py` from repo root.
REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tools.oracle_backup import main  # noqa: E402


def _mini_db(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE nodes (node_id TEXT)")
        conn.execute("INSERT INTO nodes VALUES ('ORCHTEST')")
        for t in ("readings", "uptime_hours", "attestations", "seasons"):
            conn.execute(f"CREATE TABLE {t} (x INTEGER)")
        conn.execute("INSERT INTO readings VALUES (1)")
        conn.execute("INSERT INTO readings VALUES (2)")
        conn.commit()
    finally:
        conn.close()


def test_backup_verify_restore_drill(tmp_path: Path) -> None:
    db = tmp_path / "orchard.db"
    _mini_db(db)
    dest = tmp_path / "backups"
    assert main(["backup", "--db", str(db), "--dest", str(dest), "--keep", 0]) == 0
    backups = list(dest.glob("orchard-*.db"))
    assert len(backups) == 1
    assert main(["verify", "--db", str(backups[0])]) == 0
    scratch = tmp_path / "orchard-restore-scratch.db"
    assert main(
        ["restore-drill", "--backup", str(backups[0]), "--scratch", str(scratch)]
    ) == 0
    # Refuse overwrite of existing scratch
    with pytest.raises(SystemExit) as ei:
        main(
            ["restore-drill", "--backup", str(backups[0]), "--scratch", str(scratch)]
        )
    assert ei.value.code == 2


def test_companion_list() -> None:
    assert main(["companion-list"]) == 0
