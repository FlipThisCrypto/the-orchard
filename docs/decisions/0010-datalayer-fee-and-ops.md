# ADR-0010: DataLayer Season-publish fee & ops policy

- **Status:** Accepted (2026-06-15)
- **Date:** 2026-06-15
- **Deciders:** Richard Aubrey (FlipThisCrypto)
- **Related:** ADR-0003 (verifiable dataset), ADR-0004 (central oracle),
  the Season attestation writer (`orchard_chia/datalayer/main.py`), HANDOVER T16.

## Context

Each Season, the writer makes **one DataLayer `batch_update`** — an on-chain
spend that updates the store's Merkle root to commit that Season's attestations
(ADR-0003). That spend costs a fee and takes a block or two to confirm. Three
things needed a written policy: the fee, what happens if a root update doesn't
confirm, and the guarantee that an attestation is never silently lost.

## Decision

### Fee policy
- **One root update per Season** (~daily), regardless of how many Trees/rows it
  carries — fee cost is bounded and roughly constant as the fleet grows (the
  batch size grows, the spend count does not).
- Use a **modest fixed fee** sized to confirm within a block or two under normal
  conditions (the `chia data`/DataLayer mempool fee; keep it in
  `orchard_chia/config.yaml`, not hard-coded). Chia's fee market is usually
  near-zero; revisit only under sustained congestion.
- Fund the publishing wallet thinly (DEPLOY_ORACLE.md Part 5): roughly one
  payout cycle of $JUICE + a small XCH fee balance. A compromise costs one
  cycle, not the treasury.

### Confirmation monitoring
- After `batch_update` is accepted into the mempool, the writer **polls the
  store root** (`DataLayerRpc.get_root` → `confirmed`) until it lands on chain,
  up to `CONFIRM_TIMEOUT_S` (default 180s).
- Confirmed → exit 0. Not confirmed in budget → a loud `WARNING` and a distinct
  non-zero exit (**6**) so a cron/systemd wrapper can alert. Non-fatal to the
  write itself (the spend is already submitted and may still confirm).

### Failure mode — attestations are never silently dropped
The writer is **convergent**, which is a stronger guarantee than "Season N+1
carries N": every run re-reads uptime from the oracle and reconciles it against
what's actually on chain (`get_value(key) == desired` → skip; otherwise
re-publish). So if a Season's publish **fails or never confirms**, the next run
simply finds the on-chain value missing/stale and **re-includes it** — no
persistent watermark, no manual carry-forward, no silent loss. The confirmation
check above turns "didn't land" into a visible alert rather than a silent gap.

## Consequences

- Bounded, predictable on-chain cost: one spend per Season.
- Operators get an explicit signal (exit 6 + WARNING) when a root update
  doesn't confirm, instead of discovering a missing attestation at payout.
- No watermark/state to corrupt: convergence makes re-runs safe and
  self-healing. The trade-off is each run re-checks all in-range Seasons
  against the store (a `get_value` per row) — fine at v1 scale; if it ever
  matters, add a confirmed-watermark cache, not a change to the guarantee.
