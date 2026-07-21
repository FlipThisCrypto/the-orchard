# Production flip: `ORCHARD_ORACLE_REQUIRE_SEQ=true`

> **Purpose:** turn on monotonic sequence enforcement on the hosted oracle so
> captured reading bodies cannot be replayed for uptime credit.  
> **Audience:** oracle operator (founder / deploy owner).  
> **Risk class:** reversible config change (set back to `false` if a Tree is
> stranded). No eFuses, no payouts, no secret generation.

Related: [`docs/replay-protection.md`](../replay-protection.md),
[`docs/DEPLOY_ORACLE.md`](../DEPLOY_ORACLE.md).

---

## Preconditions (all must be true)

1. **Fleet firmware ≥ 0.4.8** (ships `seq` inside the signed body). Prefer
   **v0.5.1** (hardware-verified flash → Improv → claim → `/readings`).
2. No known Trees still on pre-seq firmware that must keep posting.
3. Hosted oracle DB is backed up and a **restore-drill has passed** recently
   ([`ORACLE_BACKUP_RESTORE.md`](ORACLE_BACKUP_RESTORE.md)).
4. You can SSH / edit env and restart the oracle service.

Check current posture without secrets:

```bash
curl -sS https://oracle.theorchard.network/health | jq .
# Expect flags.require_seq == false before flip, true after.
```

Or from a Windows box:

```powershell
powershell -File tools/preflight_orchard.ps1
# After deploy of health flags, the health line should show require_seq.
```

---

## Flip procedure

1. Backup live DB:

   ```bash
   python -m tools.oracle_backup backup \
     --db /opt/orchard/data/orchard.db \
     --dest /opt/orchard/backups \
     --keep 14
   ```

2. Set in `oracle/.env` (or systemd `Environment=`):

   ```bash
   ORCHARD_ORACLE_REQUIRE_SEQ=true
   ```

3. Restart the oracle service (example):

   ```bash
   sudo systemctl restart orchard-oracle
   ```

4. Confirm:

   ```bash
   curl -sS https://oracle.theorchard.network/health | jq .flags.require_seq
   # true
   ```

5. Smoke with the simulator (against a **staging** oracle if available; or one
   registered test node on production with a known secret — prefer staging):

   ```bash
   python -m tools.tree_sim.sim --oracle http://127.0.0.1:8000 --mode functional
   python -m tools.tree_sim.sim --oracle http://127.0.0.1:8000 --mode negative
   ```

6. Watch one real Tree post after reboot; confirm 202s and rising `last_seq`
   in the admin / DB.

---

## Rollback

If a Tree is stuck (400 missing seq / 409 forever):

1. Set `ORCHARD_ORACLE_REQUIRE_SEQ=false` and restart.
2. Reflash or OTA that Tree to ≥0.4.8 (prefer 0.5.1).
3. Re-register if NVS was wiped (`last_seq` resets on re-registration with the
   same signing key).
4. Flip `true` again once the fleet is clean.

---

## Why this is safe to leave default-false in the repo

The **code default** stays `false` so fresh local/dev oracles and mixed
test fleets do not brick old firmware. Production posture is env-driven.
`last_seq` is tracked passively while the flag is off, so the flip does not
strand Trees that already send seq.
