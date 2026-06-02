# 0002 — Grove federation direction (DataLayer mirror redundancy)

**Status:** Direction; not v1 scope.
**Date:** 2026-06-01
**Context for:** anyone reading register.py or the writer and wondering
"why is each operator publishing to their own DataLayer store?"

## What's in scope for v1

Each operator runs ONE oracle (per-PC service) which aggregates many
Trees and produces one DataLayer store. The store is owned by the
operator's wallet (the same one the Orchard Pass NFT lives on). The
writer publishes per-season uptime attestations into that store.

When an operator registers via the wizard, the oracle binds the Tree
to the operator's session-verified wallet address. There's a 1:N
relationship between operator and Trees but a 1:1 relationship between
operator and DataLayer store.

This is sufficient for v1: the attestation IS on chain (the
DataLayer batch_update transaction is a chain-confirmed event), and
the operator's full node + DataLayer service serves the content to
peers. If the operator's node goes offline, the chain transition is
still permanent but the file content stops being served until they
come back.

## What's deferred: Grove federation

The natural next step is letting operators MIRROR each other's
stores. Chia's DataLayer already supports this — any peer can run
`chia data add_mirror <store_id> --amount <bond>` to publish a
bonded mirror of any public store. What's missing is *discovery*:
new operators don't know which stores exist.

The architectural piece that closes this:

1. `/register` already collects wallet_address → so a wallet:store_id
   index is derivable. Two paths:
   a) Add a `dl_store_id` column to the `Node` table and let the
      writer set it on first publish.
   b) Make store_id deterministic from the wallet master pubkey
      (no registration needed) — riskier because operators could
      run multiple stores per wallet.
   Recommended: (a).
2. New oracle endpoint `/network/stores` returns the operator →
   store_id map. Public-mode-safe: only exposes store IDs, not
   amounts/IPs.
3. The Pass NFT collection (already indexed by MintGarden) serves
   as the operator directory — anyone can enumerate Pass owners and
   their store IDs.
4. A new "Grove keeper" cron job (sibling of the writer) calls
   `chia data add_mirror` for every store in the directory, subject
   to a config policy:
       `ORCHARD_MIRROR_POLICY = none | own | grove | all`
   - `none`: only my own store gets bonded
   - `own`: same as `none` (just clearer naming)
   - `grove`: mirror operators in my declared Grove (geographic/
     trust-cluster grouping; not yet defined)
   - `all`: mirror every operator on the network

## Cost considerations

- Each `add_mirror` requires a small XCH bond. Exact economics need
  checking against current DataLayer fee schedule; rough order is
  sub-mojo per byte (so a few mojos per store). 100 mirrors ≈ tens
  of mojos. Not free, not painful.
- Disk: ~100 bytes per attestation × 1 attestation/day → ~36KB per
  Tree per year. 1000 Trees ≈ 36MB/year total — trivially mirrorable.
- The bond is recoverable on `remove_mirror`, so net cost is locked
  XCH, not spent XCH.

## v2 carrot

Mirror operators could earn a slice of $JUICE proportional to mirror
uptime, turning "I run a Tree" into "I run a Tree AND help keep
everyone else's attestations durable." Economic alignment plus
infrastructure resilience. Don't build it yet — but the design space
is open.

## Why not in v1

Phase 6.6 is about closing the security loop (wallet-verified
operator → Tree binding). Federation is orthogonal — it's about
content distribution resilience after attestations are on chain.
Mixing them dilutes both. Federation also needs more research on
the bond-cost story and an operator-side UX for "do you want to
mirror N other operators? here's the bond requirement" before it's
shippable.

## Pointers

- Chia DataLayer docs:
  https://docs.chia.net/datalayer-cli/
- `chia data add_mirror` / `remove_mirror` / `subscribe`:
  full CLI reference in `chia data --help`
- Current writer (publishes to operator's own store):
  `orchard_chia/writer/`
- Pass-bound Node model (where dl_store_id would land):
  `oracle/app/models.py`
