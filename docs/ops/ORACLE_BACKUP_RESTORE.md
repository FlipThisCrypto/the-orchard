# Oracle backup & restore runbook

> **One Thing this serves:** defend the live network (single oracle box is a SPOF).  
> **Definition of finished for a drill:** off-box copy exists **and** `restore-drill` prints `RESTORE DRILL PASSED`.  
> A file that has never been restore-tested is **not** a backup.

Related: deploy baseline in [`DEPLOY_ORACLE.md`](../DEPLOY_ORACLE.md) Part 6; offline Trees via `python -m oracle.app.monitor`.

---

## What must survive a dead disk

| Asset | Why | Tool |
|-------|-----|------|
| `orchard.db` | Registrations, readings, uptime ledger, claim bindings | `python -m tools.oracle_backup` |
| `oracle/.env` | Service config, feature flags, public WC id, paths | manual copy (secrets) |
| Cloudflared tunnel credential | Public hostname → box | manual copy |
| Attestation / signing material | If separate from env | manual copy |
| systemd unit (if customized) | How the service starts | manual copy |

List paths anytime:

```bash
python -m tools.oracle_backup companion-list
```

Treat every DB backup as **credentials**: it may contain per-Tree `signing_key_hex`.

---

## Prerequisites

- Access to the oracle host (SSH) **or** a filesystem path to a copy of `orchard.db`
- Python 3 with stdlib only (no extra pip deps for this tool)
- Repo checkout that contains `tools/oracle_backup.py` (run from **repo root**)
- A second location **off the oracle box** (laptop, encrypted USB, object storage, NAS)

Default live path from deploy docs: `/opt/orchard/data/orchard.db`  
Dev default: `oracle/data/orchard.db`

---

## A. Daily / first-time backup (on the oracle host)

```bash
cd /opt/orchard/repo    # or your checkout root
python -m tools.oracle_backup backup \
  --db /opt/orchard/data/orchard.db \
  --dest /opt/orchard/backups \
  --keep 14
```

Expected: prints `integrity: ok`, node/reading counts, and a path like  
`/opt/orchard/backups/orchard-YYYYMMDD-HHMMSSZ.db`.

### Off-box copy (required)

```bash
# Example: pull to your laptop (run from laptop)
scp user@oracle-host:/opt/orchard/backups/orchard-YYYYMMDD-HHMMSSZ.db \
  ~/orchard-offbox/orchard-YYYYMMDD-HHMMSSZ.db
```

Also copy companions (paths vary — confirm on box):

```bash
scp user@oracle-host:/opt/orchard/repo/oracle/.env ~/orchard-offbox/oracle.env
# + cloudflared credential JSON used by the tunnel
```

Record in the drill log (section E): date, backup filename, size, off-box path.

---

## B. Restore drill (does **not** touch production)

Run on laptop **or** a scratch directory on the box. Never point `--scratch` at the live DB path.

```bash
cd /path/to/repo
python -m tools.oracle_backup restore-drill \
  --backup ~/orchard-offbox/orchard-YYYYMMDD-HHMMSSZ.db \
  --scratch /tmp/orchard-restore-scratch.db
```

Pass criteria:

- Exit code `0`
- Line: `RESTORE DRILL PASSED`
- Table counts match backup → scratch (`nodes`, `readings`, …)

Optional independent check:

```bash
python -m tools.oracle_backup verify --db /tmp/orchard-restore-scratch.db
```

Delete the scratch file when done (`rm` / `del`).

---

## C. Real recovery (production broken — founder only)

Only when the live oracle data is lost or corrupt. This **replaces** the live DB.

1. Stop the oracle service (`systemctl stop orchard-oracle` or equivalent).
2. Move any damaged file aside (do not delete until the new live DB is verified):
   ```bash
   sudo mv /opt/orchard/data/orchard.db /opt/orchard/data/orchard.db.broken-$(date +%F)
   # also move -wal / -shm if present
   ```
3. Restore from the **off-box** backup into the live path:
   ```bash
   python -m tools.oracle_backup restore-drill \
     --backup /path/to/good-backup.db \
     --scratch /opt/orchard/data/orchard.db
   ```
   (Using `restore-drill` here only as a consistent copy helper into a **non-existing** live path after the damaged file was moved aside.)
4. Fix ownership (`chown orchard:orchard …`) and mode (`0600`) per deploy doc.
5. Start the service.
6. Smoke:
   - `curl -sS https://oracle.theorchard.network/health` → `{"ok": true}` (or local `/health`)
   - `python -m oracle.app.admin list` → expected Trees
   - One Tree posts a reading, or monitor is clean: `python -m oracle.app.monitor`

If smoke fails: stop service, restore previous file, escalate — do not keep a half-restored live DB.

---

## D. Cron (minimum viable, on-box)

Prefer the Python tool over raw `sqlite3 .backup` so integrity_check always runs.

```bash
# /etc/cron.daily/orchard-backup  (chmod 755)
#!/bin/sh
set -e
cd /opt/orchard/repo
/usr/bin/python3 -m tools.oracle_backup backup \
  --db /opt/orchard/data/orchard.db \
  --dest /opt/orchard/backups \
  --keep 14 >> /var/log/orchard-backup.log 2>&1
```

Still required: **off-box** copy (cron on the same disk is not disaster recovery).

External uptime: free pinger on `https://oracle.theorchard.network/health`.

---

## E. Drill log (fill when you run it)

| Date (UTC) | Backup file | Size | integrity | Off-box path | restore-drill | Notes |
|------------|-------------|------|-----------|--------------|---------------|-------|
| | | | | | PASS / FAIL | |

First row target: **this week** (founder).

---

## F. Resilience target (decision — not re-litigated here)

| Option | When |
|--------|------|
| **A. Nightly backup + monthly restore drill** (current target) | Solo PoC, lowest cost |
| B. Nightly backup + managed off-site object store | When off-box scp is unreliable |
| C. Hot standby / managed Postgres | After external growth makes downtime expensive |

Default until an ADR says otherwise: **Option A**.

---

## Verification status of this tooling

| Check | Status |
|-------|--------|
| Tool uses SQLite online backup API | By implementation |
| integrity_check after backup | By implementation |
| restore-drill refuses overwrite / live-ish names | By implementation |
| Executed against a real local `orchard.db` snapshot | See workspace verification notes / command log |
| Executed on production oracle host | **Founder** — not claimed until you run it |

---

*Tool: `tools/oracle_backup.py` · Keep this runbook next to deploy ops.*
