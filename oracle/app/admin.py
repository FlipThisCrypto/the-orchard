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
# Every table carrying node_id. `claims` was added by migration 03f4c36f5eb6
# AFTER the delete paths were written and was never added here, so deleting
# a Tree left an orphaned claim row behind. Silent on SQLite (the connect
# hook sets journal_mode and busy_timeout but never PRAGMA foreign_keys=ON)
# and a hard FK violation on Postgres.
_CHILD_TABLES = ("readings", "uptime_hours", "attestations", "claims")


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


def _retire(conn, ids: list[str], reason: str) -> None:
    """Mark Trees retired. Nothing is deleted — this is the reversible answer.

    A retired Tree leaves the living network (/nodes, trees_registered,
    trees_active_24h, payout) while its readings, uptime, attestations and
    claims stay exactly where they are. That matters because DataLayer records
    are permanent and public: deleting a Tree would leave on-chain attestations
    pointing at a node_id the oracle then denies ever existed.
    """
    stmt = text(
        "UPDATE nodes SET retired_at = :ts, retired_reason = :reason "
        "WHERE node_id IN :ids AND retired_at IS NULL"
    ).bindparams(bindparam("ids", expanding=True))
    conn.execute(stmt, {"ids": ids, "ts": datetime.now(timezone.utc).isoformat(),
                        "reason": (reason or "")[:200]})


def _unretire(conn, ids: list[str]) -> None:
    """Bring retired Trees back. Clearing the column restores them exactly."""
    stmt = text(
        "UPDATE nodes SET retired_at = NULL, retired_reason = NULL "
        "WHERE node_id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
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


def cmd_retire(eng, cmd: str, ids: list[str], reason: str, apply: bool) -> int:
    """Retire or un-retire Trees. Dry-run by default, like delete."""
    want = {i.strip().upper() for i in ids if i.strip()}
    with eng.connect() as conn:
        present = set(_all_node_ids(conn))
        rows = {r[0]: r[1] for r in conn.execute(text(
            "SELECT node_id, retired_at FROM nodes"))}
    for u in sorted(want - present):
        print(f"warning: node_id not found: {u}", file=sys.stderr)
    targets = sorted(want & present)
    if not targets:
        print("Nothing to do.", file=sys.stderr)
        return 1

    # Only act on Trees that actually change state, so a re-run is a no-op
    # rather than a silent second retirement with a different timestamp.
    if cmd == "retire":
        acting = [n for n in targets if not rows.get(n)]
        skipped = [n for n in targets if rows.get(n)]
        verb, note = "Retiring", "already retired"
    else:
        acting = [n for n in targets if rows.get(n)]
        skipped = [n for n in targets if not rows.get(n)]
        verb, note = "Un-retiring", "not retired"

    for n in skipped:
        print(f"  skip   {n}  ({note})")
    for n in acting:
        print(f"  {verb.lower():<10} {n}")
    if not acting:
        print("No state change needed.")
        return 0
    if not apply:
        print(f"\nDRY RUN — {verb.lower()} {len(acting)} node(s). Re-run with --yes to apply.")
        print("No data is deleted either way; retirement only changes visibility.")
        return 0

    backup = _backup_db()
    if backup:
        print(f"\nBacked up DB -> {backup}")
    with eng.begin() as conn:
        conn.exec_driver_sql("PRAGMA busy_timeout=10000")
        if cmd == "retire":
            _retire(conn, acting, reason)
        else:
            _unretire(conn, acting)
    print(f"{verb} {len(acting)} node(s). Readings, uptime and attestations untouched.")
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

    # Retire is the reversible answer, and should be reached for before delete.
    rp = sub.add_parser("retire", help="retire node(s): leave the living network, keep all data")
    rp.add_argument("node_id", nargs="+")
    rp.add_argument("--reason", required=True,
                    help="why (recorded; an unexplained retirement is a gap in the record)")
    rp.add_argument("--yes", action="store_true", help="apply (default is dry-run)")

    up = sub.add_parser("unretire", help="bring retired node(s) back")
    up.add_argument("node_id", nargs="+")
    up.add_argument("--yes", action="store_true", help="apply (default is dry-run)")

    args = p.parse_args(argv)

    eng = engine()
    if args.cmd == "list":
        with eng.connect() as conn:
            return cmd_list(conn)
    if args.cmd in ("retire", "unretire"):
        return cmd_retire(eng, args.cmd, args.node_id,
                          getattr(args, "reason", ""), args.yes)
    return cmd_modify(eng, args.cmd, args.node_id, args.yes)


if __name__ == "__main__":
    raise SystemExit(main())
