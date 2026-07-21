# SPDX-License-Identifier: Apache-2.0
"""Safe oracle SQLite backup + restore drill.

Why this exists
---------------
The live network's uptime ledger lives in one SQLite file. ``shutil.copy`` of a
live WAL database is not a consistent backup. This tool uses the SQLite
online backup API (``Connection.backup``), which is safe while the oracle is
running, then runs ``PRAGMA integrity_check``.

Commands
--------
From the **repo root** (or pass ``--db`` explicitly)::

    python -m tools.oracle_backup backup \\
        --db /opt/orchard/data/orchard.db \\
        --dest /opt/orchard/backups

    python -m tools.oracle_backup verify --db /path/to/orchard-backup.db

    python -m tools.oracle_backup restore-drill \\
        --backup /path/to/orchard-YYYYMMDD-HHMMSS.db \\
        --scratch /tmp/orchard-restore-scratch.db

    python -m tools.oracle_backup companion-list

``restore-drill`` never touches the live DB. It only writes to ``--scratch``.
A backup that has never passed ``restore-drill`` is not a backup.

Companion secrets (``.env``, tunnel credential, signing keys) are listed by
``companion-list`` — copy them off-box by hand; this tool does not move secrets
into the backup directory by default.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_DB_CANDIDATES = (
    Path("oracle/data/orchard.db"),
    Path("/opt/orchard/data/orchard.db"),
)

# Tables the oracle cares about for a useful restore smoke check.
_COUNT_TABLES = ("nodes", "readings", "uptime_hours", "attestations", "seasons")


def _die(msg: str, code: int = 2) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def _resolve_db(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser().resolve()
        if not p.is_file():
            _die(f"database not found: {p}")
        return p
    for cand in DEFAULT_DB_CANDIDATES:
        if cand.is_file():
            return cand.resolve()
    _die(
        "no database found. Pass --db PATH. Tried: "
        + ", ".join(str(c) for c in DEFAULT_DB_CANDIDATES)
    )


def _connect_ro(path: Path) -> sqlite3.Connection:
    # URI read-only when possible; still works if the file is busy for backup source.
    uri = path.resolve().as_uri() + "?mode=ro"
    try:
        return sqlite3.connect(uri, uri=True, timeout=30.0)
    except sqlite3.Error:
        return sqlite3.connect(str(path), timeout=30.0)


def _connect_rw(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(str(path), timeout=60.0)


def integrity_check(path: Path) -> str:
    conn = _connect_ro(path)
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return (row[0] if row else "missing") or "missing"
    finally:
        conn.close()


def table_counts(path: Path) -> dict[str, int | None]:
    conn = _connect_ro(path)
    out: dict[str, int | None] = {}
    try:
        existing = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        for t in _COUNT_TABLES:
            if t not in existing:
                out[t] = None
                continue
            out[t] = int(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0])
    finally:
        conn.close()
    return out


def cmd_verify(db: Path, as_json: bool) -> int:
    result = integrity_check(db)
    counts = table_counts(db)
    size = db.stat().st_size
    payload = {
        "db": str(db),
        "size_bytes": size,
        "integrity_check": result,
        "counts": counts,
        "ok": result == "ok",
    }
    if as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"db:        {db}")
        print(f"size:      {size} bytes")
        print(f"integrity: {result}")
        for k, v in counts.items():
            print(f"  {k}: {v if v is not None else '(table missing)'}")
        print("OK" if payload["ok"] else "FAIL")
    return 0 if payload["ok"] else 1


def cmd_backup(db: Path, dest_dir: Path, keep: int) -> int:
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    out = dest_dir / f"orchard-{stamp}.db"
    if out.exists():
        _die(f"refusing to overwrite existing file: {out}")

    # Online backup API: consistent snapshot even if WAL is active.
    src = sqlite3.connect(str(db), timeout=60.0)
    try:
        dst = sqlite3.connect(str(out), timeout=60.0)
        try:
            src.backup(dst)
            dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dst.close()
    finally:
        src.close()

    try:
        os.chmod(out, 0o600)
    except OSError:
        pass

    result = integrity_check(out)
    counts = table_counts(out)
    if result != "ok":
        print(f"FAIL integrity_check={result} for {out}", file=sys.stderr)
        return 1

    print(f"backup:    {out}")
    print(f"size:      {out.stat().st_size} bytes")
    print(f"integrity: {result}")
    for k, v in counts.items():
        print(f"  {k}: {v if v is not None else '(table missing)'}")

    if keep > 0:
        _prune(dest_dir, keep)

    print(
        "\nNEXT: copy this file OFF the oracle box, then run:\n"
        f"  python -m tools.oracle_backup restore-drill "
        f"--backup {out} --scratch /tmp/orchard-restore-scratch.db"
    )
    return 0


def _prune(dest_dir: Path, keep: int) -> None:
    files = sorted(
        dest_dir.glob("orchard-*.db"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in files[keep:]:
        try:
            old.unlink()
            print(f"pruned:    {old}")
        except OSError as e:
            print(f"warn: could not prune {old}: {e}", file=sys.stderr)


def cmd_restore_drill(backup: Path, scratch: Path) -> int:
    if not backup.is_file():
        _die(f"backup not found: {backup}")
    scratch = scratch.expanduser().resolve()
    if scratch.exists():
        _die(
            f"scratch already exists (refusing to overwrite): {scratch}\n"
            "  delete it or pick another --scratch path"
        )
    if scratch.resolve() == backup.resolve():
        _die("scratch path must differ from backup path")

    # Forbidden patterns: never write drill output over live default paths.
    forbidden_names = {"orchard.db"}
    if scratch.name in forbidden_names and "scratch" not in scratch.name.lower():
        _die(
            f"refusing scratch name {scratch.name!r} without 'scratch' in the path; "
            "pick e.g. orchard-restore-scratch.db"
        )

    scratch.parent.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(str(backup), timeout=60.0)
    try:
        dst = sqlite3.connect(str(scratch), timeout=60.0)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    src_counts = table_counts(backup)
    dst_result = integrity_check(scratch)
    dst_counts = table_counts(scratch)

    ok = dst_result == "ok" and src_counts == dst_counts
    print(f"backup:    {backup}")
    print(f"scratch:   {scratch}")
    print(f"integrity: {dst_result}")
    print("counts (backup → scratch):")
    for k in _COUNT_TABLES:
        print(f"  {k}: {src_counts.get(k)} → {dst_counts.get(k)}")

    if ok:
        print("RESTORE DRILL PASSED")
        print(
            f"\nSafe to delete scratch when done:\n  del {scratch}"
            if os.name == "nt"
            else f"\nSafe to delete scratch when done:\n  rm {scratch}"
        )
        return 0

    print("RESTORE DRILL FAILED", file=sys.stderr)
    return 1


def cmd_companion_list() -> int:
    """Print companion files that must leave the box with the DB."""
    items = [
        ("Oracle env", "/opt/orchard/repo/oracle/.env  OR  <repo>/oracle/.env"),
        ("SQLite DB", "/opt/orchard/data/orchard.db  (this tool backs this up)"),
        ("Cloudflared tunnel credential JSON", "path from cloudflared config (often ~/.cloudflared/*.json)"),
        ("Attestation / oracle signing key", "if separate from .env — see orchard_chia config"),
        ("systemd unit overrides", "/etc/systemd/system/orchard-oracle.service*"),
    ]
    print("Companion secrets & config (copy off-box; do NOT commit):")
    for name, path in items:
        print(f"  - {name}: {path}")
    print(
        "\nGuardrails:\n"
        "  - Never put secrets in git, chat logs, or public object storage without encryption.\n"
        "  - DB contains per-Tree signing_key_hex — treat backups as credentials.\n"
        "  - chmod 600 backup files on POSIX hosts."
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m tools.oracle_backup",
        description="Safe oracle SQLite backup + restore drill.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backup", help="consistent online backup + integrity_check")
    b.add_argument("--db", default=None, help="path to orchard.db")
    b.add_argument(
        "--dest",
        default="oracle/backups",
        help="directory for orchard-TIMESTAMP.db (default: oracle/backups)",
    )
    b.add_argument(
        "--keep",
        type=int,
        default=14,
        help="keep N newest backups in --dest (0 = do not prune). Default 14.",
    )

    v = sub.add_parser("verify", help="integrity_check + table counts")
    v.add_argument("--db", default=None, help="path to db or backup file")
    v.add_argument("--json", action="store_true")

    r = sub.add_parser(
        "restore-drill",
        help="restore a backup to a scratch file and verify (never touches live DB)",
    )
    r.add_argument("--backup", required=True, help="path to backup .db")
    r.add_argument(
        "--scratch",
        required=True,
        help="new path for restored copy (must not exist)",
    )

    sub.add_parser(
        "companion-list",
        help="list non-DB secrets/config that must also leave the box",
    )

    args = p.parse_args(argv)

    if args.cmd == "companion-list":
        return cmd_companion_list()
    if args.cmd == "verify":
        return cmd_verify(_resolve_db(args.db), args.json)
    if args.cmd == "backup":
        db = _resolve_db(args.db)
        return cmd_backup(db, Path(args.dest).expanduser(), args.keep)
    if args.cmd == "restore-drill":
        return cmd_restore_drill(
            Path(args.backup).expanduser().resolve(),
            Path(args.scratch).expanduser(),
        )
    _die(f"unknown command: {args.cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
