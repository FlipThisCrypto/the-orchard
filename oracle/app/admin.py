# SPDX-License-Identifier: Apache-2.0
"""Oracle node administration CLI — manual Tree management.

List registered Trees and delete stale ones. The motivating case: a full
("merged") firmware flash wipes a board's NVS, so the Tree boots with a
fresh identity and re-registers under a NEW node_id — leaving its previous
node_id orphaned in the registry. This tool removes those.

Run from the repo root, with the same environment as the oracle (it targets
the same database — ``ORCHARD_ORACLE_DB_URL`` / ``oracle/.env`` / the default
``oracle/data/orchard.db``):

    python -m oracle.app.admin list
    python -m oracle.app.admin delete <node_id> [<node_id> ...]
    python -m oracle.app.admin keep   <node_id> [<node_id> ...]

``delete`` removes the named nodes; ``keep`` removes every node EXCEPT the
named ones (handy for "keep only my real Trees"). Both are **dry-run by
default** — they print exactly what would be removed and change nothing.
Add ``--yes`` to apply; an on-disk SQLite DB is backed up first
(``<db>.bak-YYYYMMDD-HHMMSS``).

Deletes cascade across ``readings``, ``uptime_hours``, and ``attestations``
so no orphaned child rows are left behind. node_ids are matched
case-insensitively (the registry stores them uppercase).

Implementation note: this tool talks to the tables with plain SQL (not the
ORM) on purpose, so it keeps working against a DB whose schema is a
migration behind the running code — exactly the situation an operator is
in when they reach for a cleanup tool.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone

from sqlalchemy import bindparam, text

from .config import settings
from .db import _sqlite_path_from_url, engine

# Child tables that carry a node_id FK, deleted before the node row itself.
_CHILD_TABLES = ("readings", "uptime_hours", "attestations")


def _all_node_ids(conn) -> list[str]:
    return [r[0] for r in conn.execute(text(
        "SELECT node_id FROM nodes ORDER BY last_reading_at"))]


def _count(conn, table: str, node_id: str) -> int:
    return conn.execute(
        text(f"SELECT count(*) FROM {table} WHERE node_id = :n"), {"n": node_id}
    ).scalar() or 0


def _node_meta(conn, node_id: str) -> tuple[str, str]:
    row = conn.execute(text(
        "SELECT fw_version, label FROM nodes WHERE node_id = :n"), {"n": node_id}).first()
    if not row:
        return "?", ""
    return (row[0] or "?"), (row[1] or "")


def _fmt_age(raw) -> str:
    if not raw:
        return "never"
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return str(raw)[:16]
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    mins = (datetime.now(timezone.utc) - dt).total_seconds() / 60
    if mins < 90:
        return f"{int(mins)}m ago"
    if mins < 60 * 48:
        return f"{int(mins / 60)}h ago"
    return f"{int(mins / 1440)}d ago"


def cmd_list(conn) -> int:
    rows = list(conn.execute(text(
        "SELECT node_id, fw_version, label, last_reading_at "
        "FROM nodes ORDER BY last_reading_at")))
    print(f"{len(rows)} node(s) in {settings().db_url}\n")
    hdr = f"{'node_id':32}  {'fw':6}  {'readings':>8}  {'uptime':>6}  {'last_reading':>12}  label"
    print(hdr)
    print("-" * len(hdr))
    for nid, fw, label, last in rows:
        r = _count(conn, "readings", nid)
        u = _count(conn, "uptime_hours", nid)
        print(f"{nid:32}  {(fw or '?'):6}  {r:8d}  {u:6d}  {_fmt_age(last):>12}  {label or ''}")
    return 0


def _resolve_targets(present: list[str], cmd: str, ids: list[str]) -> tuple[list[str], list[str]]:
    pset = set(present)
    want = {i.strip().upper() for i in ids if i.strip()}
    unknown = sorted(want - pset)
    targets = sorted(want & pset) if cmd == "delete" else sorted(pset - want)
    return targets, unknown


def _backup_db() -> str | None:
    path = _sqlite_path_from_url(settings().db_url)
    if path is None or not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_suffix(path.suffix + f".bak-{stamp}")
    shutil.copy2(path, dest)
    return str(dest)


def _delete(conn, ids: list[str]) -> None:
    for table in (*_CHILD_TABLES, "nodes"):
        stmt = text(f"DELETE FROM {table} WHERE node_id IN :ids").bindparams(
            bindparam("ids", expanding=True))
        conn.execute(stmt, {"ids": ids})


def cmd_modify(eng, cmd: str, ids: list[str], apply: bool) -> int:
    with eng.connect() as conn:
        present = _all_node_ids(conn)
        targets, unknown = _resolve_targets(present, cmd, ids)
        for u in unknown:
            print(f"warning: node_id not found: {u}", file=sys.stderr)
        if cmd == "keep" and not ({i.upper() for i in ids} & set(present)):
            print("refusing to 'keep' when none of the given ids exist "
                  "(that would delete everything). Double-check the ids.", file=sys.stderr)
            return 2
        if not targets:
            print("Nothing to delete.")
            return 0

        tot_r = tot_u = tot_a = 0
        print(("WILL DELETE" if apply else "DRY RUN — would delete")
              + f" {len(targets)} node(s):\n")
        for nid in targets:
            fw, label = _node_meta(conn, nid)
            r = _count(conn, "readings", nid)
            u = _count(conn, "uptime_hours", nid)
            a = _count(conn, "attestations", nid)
            tot_r += r; tot_u += u; tot_a += a
            print(f"  {nid}  ({fw}, {label or 'no label'})  "
                  f"-> {r} readings, {u} uptime rows, {a} attestations")
        print(f"\n  totals: {tot_r} readings, {tot_u} uptime rows, {tot_a} attestations")
        kept = len(present) - len(targets)
        print(f"  nodes remaining after: {kept}")

    if not apply:
        print("\nDry run — nothing changed. Re-run with --yes to apply.")
        return 0

    backup = _backup_db()
    if backup:
        print(f"\nBacked up DB -> {backup}")
    with eng.begin() as conn:
        conn.exec_driver_sql("PRAGMA busy_timeout=10000")
        _delete(conn, targets)
    print(f"Deleted {len(targets)} node(s) and their data.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m oracle.app.admin",
                                description="Oracle node administration.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list registered nodes + row counts")
    for name, helptext in (("delete", "delete the named node(s)"),
                           ("keep", "delete every node EXCEPT the named one(s)")):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("node_id", nargs="+")
        sp.add_argument("--yes", action="store_true", help="apply (default is dry-run)")
    args = p.parse_args(argv)

    eng = engine()
    if args.cmd == "list":
        with eng.connect() as conn:
            return cmd_list(conn)
    return cmd_modify(eng, args.cmd, args.node_id, args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
