# Verification record — set 1, iteration 48 (2026-08-10)

The broadest practical sweep at the end of the first 50-iteration set.
Everything below was executed, not asserted.

## Offline

| Check | Result |
|---|---|
| the-orchard full suite | **1,026 passed**, 0 failed |
| golden vectors drift (`test_vectors_drift`) | passed — committed vectors match the generator |
| Alembic vs `create_all` schema agreement | passed (includes `slots_mask`, location, retirement columns) |
| v2 website gate (unit tests, board, stamps, CSP, secrets, snapshot) | all 7 passed |

## Live (read-only)

| Probe | Result |
|---|---|
| `/network/stats` | 1 Tree registered/active, 1,304 readings in 24h |
| `economics status` (fresh ledger) | 85,000,000.000 JUICE pool, year 1, ≥1,518-day runway floor |
| `economics audit` | ledger internally consistent |
| v2 `--live` gate | production matches the repo; oracle contract holds |

## Deploy-pending (branch `fix/mintgarden-user-agent`, not yet on the box)

The live oracle returned `last_reading_at: None`: the box runs pre-branch
code. Until the branch is merged and the box updated
(`git checkout origin/main -- oracle/` + `systemctl restart orchard-oracle`),
the following are **built and tested but not yet in effect live**:

- 30-reading hour quorum + 4-slot spread (uptime will drop for thin/burst
  hours when deployed — the number becoming honest, not a regression)
- replay enforcement default-on; pipeline liveness fields on `/network/stats`
- sensor qualification (classes, persistence, plausibility)
- everything in `orchard_chia/` runs operator-side and is live on merge:
  economics settle/pay/status/audit, provenance gate, writer lock,
  single-shot batch_update, baseline refusal, key-rotation guard,
  season-window seal, chain-derived public hours.

## Known-good invariants re-measured this set

- store readable: 200/200 keys (was 0)
- unproven records pay 0.000 JUICE (was 170.033)
- schedule totals the 85M pool over 8 years; 75%-uptime runway 14.6 years


---

## Addendum — 2026-08-11, Set 2: the pipeline went live

- PR #55 merged; the box deployed and confirmed (real `last_reading_at`).
- **First proof-backed seasons on mainnet**: publish tx `0x45de6fa2…` (30
  hours, 32/32 inserts confirmed), attest tx `0x2a3c9156…` (seasons 74–76
  sealed from readings). Independent verifier: 490 device signatures valid,
  all Merkle roots recompute, **verified_hours = 9 for season 76** under the
  30-reading quorum. Sole failing check: the block anchor (firmware
  placeholder; /beacon still Cloudflare-blocked).
- **Timers registered and verified on the operator machine**: Publish hourly
  :10, Attest 00:25, Settle 00:40, Status 08:00 — Status fired once,
  end-to-end, output in `ops/scheduler-orchard-status.log`. Paying remains
  manual by design. Settle refuses safely until
  `setx ORCHARD_ORACLE_WRITER_TOKEN` is done (wallet-blind guard).
- Live finding for the operator: the Tree's own connectivity is degrading
  (2,143 → 1,304 → 310 readings/day); the oracle verified healthy at 60s
  cadence when the device is up.
