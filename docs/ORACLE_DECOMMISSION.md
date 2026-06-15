<!-- SPDX-License-Identifier: Apache-2.0 -->
# Oracle decommission plan (HANDOVER T24)

> **Purpose.** The central oracle (ADR-0004) is an explicit **transitional
> bridge**, not permanent infrastructure (ADR-0008 north star: serverless). This
> document is the plan for *ending* it — the cutover criteria that say "it's
> safe to turn off," how the accumulated history is preserved forever, and the
> teardown checklist. Writing this now keeps "the oracle gets deleted, not
> maintained forever" a committed decision rather than an afterthought.
>
> Owner action is required to *execute* a decommission; the thresholds below are
> proposed defaults to tune, not triggers I act on. No infrastructure specifics
> (hosts, addresses, certs) live here — those stay out of the repo
> (`docs/DEPLOY_ORACLE.md` / out-of-band).

## What replaces the oracle

Nothing, as a service. By decommission time the reward path is on-chain:
- **Trees** heartbeat their own singletons (T20 puzzle / T21 client, ADR-0008 §1).
- **Rewards** are claimed from on-chain epoch vaults (T22, ADR-0008 §2–3).
- **Observability** is a static chain-reading page (T23) — no backend.
- **History** (the Season ledger the oracle accumulated) is published as a final
  immutable DataLayer root (below) and continues to be readable after teardown.

If any of those isn't true yet, the oracle is **not** ready to retire.

## Cutover criteria (all must hold — defaults, owner-tunable)

| # | Criterion | Proposed default |
|---|---|---|
| 1 | Consecutive incident-free epochs on **testnet11** (heartbeat + vault claims completing end-to-end) | ≥ 4 |
| 2 | **Mainnet pilot** epochs with on-chain vault claims completing for real operators | ≥ 2 |
| 3 | Share of the **live fleet** running heartbeat firmware (T21) and successfully heartbeating its singleton | ≥ 90% |
| 4 | **Static status page (T23)** live and reading chain state — observability survives the oracle | shipped |
| 5 | **Final DataLayer archival** of the Season ledger published and on-chain-confirmed | done (next section) |
| 6 | Operator comms sent: claim path, status page URL, and the sunset date | ≥ 2 weeks notice |

"Incident-free" = no consensus rejections of well-formed heartbeats/claims, no
fund-loss or stuck-claim events, no rollbacks of the vault or singleton puzzles.

## Pre-deletion: final archival publication (irreversible-history step)

The oracle's value that must outlive it is the **historical Season ledger**
(readings, attestations, uptime). Per ADR-0010 the DataLayer writer is
**convergent** (never silently drops attestations), so:

1. Quiesce writes: put the oracle in **read-only sunset mode** (reject new
   registrations + readings; keep serving GETs) so the dataset stops changing.
2. Compute the final Season root over the complete ledger and publish it to
   DataLayer; **wait for on-chain confirmation** (the `get_root` poll added in
   T16 / PR #17 — exit non-zero on miss; do not proceed on an unconfirmed root).
3. Record the final `store_id` + confirmed root hash in the repo
   (`docs/` or `JUICE.md`/status page) so anyone can verify the history
   independently of any server.
4. Keep a tarball of the SQLite DB as a belt-and-braces local archive
   (off the soon-to-be-deleted host).

This step is the point of no return for the *data*; everything after is just
turning off compute.

## Deletion checklist (generic — fill specifics out-of-band)

Do in order; each is reversible up to the host destroy, which is not.

- [ ] Sunset-mode announced and active (read-only) for the notice period.
- [ ] Final DataLayer root published + **confirmed** (above) and recorded.
- [ ] DB archived off-host (encrypted) and a restore test done.
- [ ] Clients/docs/status page no longer point at the oracle API; the flasher
      and quickstart describe the on-chain claim path only.
- [ ] Tear down inbound ingress (the tunnel) so the API is unreachable.
- [ ] Observe one notice-period with the API down + no operator breakage
      reported (last reversible checkpoint).
- [ ] Destroy the compute host (VPS) — **after** the DB archive is verified.
- [ ] Remove the oracle DNS record(s).
- [ ] Rotate/retire any oracle-held keys/secrets (Season signer, RPC creds);
      revoke tokens; remove from secret stores.
- [ ] Close monitoring/alerting tied to the oracle (T13 offline-monitor cron).
- [ ] Final note in `docs/LOG.md` + status page: oracle decommissioned on
      `<date>`; history at DataLayer root `<hash>`.

## Explicitly NOT deleted

- On-chain singletons, epoch vaults, treasury, and crank — those **are** the
  system now.
- The final DataLayer historical root (the permanent Season ledger).
- The firmware, puzzles, and this repository.
- Operator wallets / Genesis Passes (owner-held; untouched).

## Rollback

Until the host is destroyed and DNS removed, decommission is reversible: restart
the API behind the tunnel and re-point DNS. After host destroy, "rollback" means
standing up a fresh oracle from the repo + the archived DB — possible but
treated as a new deployment, not a revert. Hence the ordered checklist: prove no
breakage with the API *down* before destroying anything.

## References

- ADR-0004 (central oracle as transitional bridge), ADR-0008 (serverless north
  star), ADR-0010 (DataLayer convergent writer + confirmation).
- T16 / PR #17 — the root-confirmation check reused in the archival step.
- T23 — static chain-reading status page (criterion 4).
- `docs/DEPLOY_ORACLE.md` — the deployment runbook this reverses (specifics live
  there / out-of-band, not here).
