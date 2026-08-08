# Going live: real sensor data in Chia DataLayer

The step-by-step for turning on the verifiable-dataset pipeline, from the merge
in this PR to a published reading a stranger can independently verify.

Companion to [`DATALAYER_OPERATOR.md`](DATALAYER_OPERATOR.md) (day-to-day
running) and [ADR-0003](../decisions/0003-datalayer-verifiable-dataset.md) (why).

## Proven on mainnet, 2026-08-08

The chain leg — wallet → `batch_update` → fee → confirmed root → value read back —
**works**, verified before any of the steps below:

```
store  d0bb705ed0f9e32fcdae20467e3d64e6aedd9d957b494ae4377ab9c381fd2e37
root   0x436a5e46…5b4e  →  0x5ccff1fec262477212fffbc9c2061a568e1e00232a0c85a16a635ae7d3ee97e3
tx     0xa3577121244520750aa9a77efe813868c297c1cd3ace1757e55054b1f2f00ea6   confirmed
```

`meta:schema` reads back exactly as written, declaring `geohash_precision: 5` on
chain — the same ~5 km contract the oracle and worldview enforce. This was
`meta:schema` only: no readings, because no Tree signs them yet.

## What was already true before this PR

- The store **exists on mainnet**: `d0bb705ed0f9e32fcdae20467e3d64e6aedd9d957b494ae4377ab9c381fd2e37`.
  Launcher coin confirmed at height **8,794,669**, owned by the `miner` wallet
  key (fingerprint `3541438827`). Current root
  `0x436a5e46…5b4e`, `confirmed: true`.
- It contains **one legacy HMAC attestation** from 2026-05-29 and no ADR-0003
  data. Nothing has ever been published by the current pipeline.
- The pipeline (publisher, attestor, Merkle inclusion proofs, verifier) is
  **written and tested**, and has never completed a run.

## Why the last attempt (2026-07-26) failed

Both the `publish` and `attest` runs died in under 35 ms with a raw
`JSONDecodeError`. Root cause chain, in order:

1. The publisher's very first call is `GET /` on the oracle (to read
   `current_season`). It received HTTP 200 with a **non-JSON body**.
2. `oracle.py`'s `r.json()` sat **outside** its `try/except`, so the decode
   error never became an `OracleError` and was never handled — it killed the
   process instead of producing the intended exit-code 3.
3. Underneath that, the run was structurally doomed anyway: production's
   `/readings` capped `limit` at 500 (the publisher sends 2000), ignored the
   `since_ms`/`until_ms` window, and `/nodes` served no `device_pubkey` — and
   firmware 0.5.1 attaches no device signature at all, so there were **zero
   publishable readings**.

This PR fixes (1)–(3). Item (3)'s firmware half is the one physical step below.

## Order of operations

### 1. Merge this PR

Nothing deploys automatically. Merging is safe on its own.

### 2. Deploy the oracle — **on the box, by the founder**

SSH from the build machine is key-denied by design, so these are yours to run.
The service reads its code from `/opt/orchard/app`:

```bash
sudo git -C /opt/orchard/app fetch origin
sudo git -C /opt/orchard/app checkout origin/main -- oracle/
```

**Back up the database first.** This deploy carries a schema migration:

```bash
sudo cp /opt/orchard/app/oracle/data/orchard.db ~/orchard.db.bak-$(date +%F)
```

Migration `b7c41d2e9a05` adds `nodes.device_pubkey` (nullable) and the
`audit_events` table. It runs automatically on startup. Both changes are
**additive** — rehearsed against a production-shaped database: existing node
and reading rows including stored GPS were byte-identical afterwards, and the
downgrade path restores the previous schema without touching rows.

Then restart the service and confirm:

```bash
curl -s https://oracle.theorchard.network/health
curl -s https://oracle.theorchard.network/nodes | head -c 400   # expect device_pubkey present, wallet_address absent
```

### 3. Flash one Tree with the signing firmware

This is the gating data prerequisite and it is physical work. Until a Tree
signs its readings there is **nothing publishable** — the publisher discards
unsigned readings and never re-signs them, which is the whole point: the oracle
must not be able to manufacture data it can then "verify".

Build from `firmware/`, flash the chosen Tree, leave its NVS oracle URL as it
is. The Tree keeps its identity (`node_id` and its P-256 device key live in NVS
and are not touched by a firmware update).

After it reports once, confirm the oracle learned the key:

```bash
curl -s "https://oracle.theorchard.network/nodes/<NODE_ID>" | python -m json.tool | grep device_pubkey
```

### 4. Publish (operator machine, Windows)

Requires the Chia **wallet** and **data_layer** services running and synced on
the `miner` key. A full node is **not** needed for `publish`.

```powershell
python -m orchard_chia.datalayer preflight
python -m orchard_chia.datalayer publish --dry-run
python -m orchard_chia.datalayer publish
```

Cost: one `batch_update` per run at `datalayer.fee: 100000000` = **0.0001 XCH**.
At hourly cadence that is ≤ 0.0024 XCH/day.

Only **fully closed** UTC hours are published, so allow at least an hour after
the Tree starts reporting.

### 5. Verify — the step that matters

```powershell
chia rpc data_layer get_root '{"id": "0xd0bb705e…fd2e37"}'
python -m orchard_chia.cli.orchard_verify live --node-id <NODE_ID> --season <N> --hour <H>
```

Expected verdicts:

| Exit | Meaning | Action |
|---|---|---|
| `0` | VALID (`partial` for a single hour) | done |
| `2` | CANNOT-VERIFY | **expected at first** — current firmware sends a placeholder `block_anchor`, which by design cannot be anchored. Not fraud, not a defect. |
| `1` | INVALID | stop and investigate — a genuine contradiction |

## Known gaps, stated plainly

- **`attest` (season sealing) cannot run yet.** It makes exactly one full-node
  RPC (`peak_height` on 8555) with no fallback, and the RPC client refuses
  non-loopback hosts, so it cannot borrow a remote node. `publish`, `verify`
  and `reconcile` all work without it. Options: run a local full node, source
  the height from the wallet RPC instead, or leave sealing off for now.
- ~~**`confirm.py` only does a local read-back**~~ — **fixed.** The writers now
  wait for the store's root to *move past the pre-write hash* and confirm before
  reading values back. Previously they read back instantly and reported a
  perfectly good write as `missing`, because `batch_update` submits a
  transaction that is not visible until it reaches a block. Proven on the first
  real publish (2026-08-08, tx `0xa3577121…`): reported failure, then confirmed
  and read back correctly a minute later. `pending` is now a distinct outcome
  from `failed`, and the old advice on that path — "re-run to converge" — was
  actively harmful while a transaction was in flight, because a re-run submits
  the same write again and pays the fee twice.

  Exit `6` therefore now means one of two things, and the message says which:
  *submitted but not yet confirmed* (harmless — wait, then re-run), or *the root
  moved and the values are wrong* (a real failure).
- **The block anchor is a placeholder** in current firmware, so readings are
  provably *published* and *device-signed*, but not yet provably *not
  backdated*. That is ADR-0003's remaining open question.
- **Location.** The oracle computes `geohash` at request time from the latest
  reading's GPS, coarsened to precision 5 (~5 km), and never stores or serves
  the precise coordinates. The publisher passes that coarse value through
  verbatim and whitelists the fields it publishes — `wallet_address` is never
  among them. Precise GPS stays in the oracle's private database.
