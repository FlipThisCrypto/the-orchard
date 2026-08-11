# DataLayer operator runbook

How to run The Orchard’s verifiable DataLayer pipeline on an operator host.

## Prerequisites

1. Chia full node + DataLayer service running (mainnet or testnet).
2. Oracle and Orchard View on the LAN.
3. `orchard_chia/config.yaml` filled from `config.example.yaml` with SSL cert paths and `datalayer.store_id`.

Create a store once:

```text
chia data create_data_store -m 0.0001
```

Paste the returned `id` into `datalayer.store_id`. Preflight now rejects a
`store_id` that isn't 64 hex chars, so a typo fails fast.

### Transaction fee (congestion)

`datalayer.fee` (mojos, default `0`) is attached to every `batch_update`. `0`
works when the mempool is quiet; if publishes/attests sit **unconfirmed** (the
post-write confirm keeps failing and the watermark won't advance), raise it,
e.g. `fee: 100000000` (0.0001 XCH), so the write makes it into a block.

## Preflight

```powershell
python -m orchard_chia.datalayer preflight
# without Chia RPC (oracle + config only):
python -m orchard_chia.datalayer preflight --skip-chia
```

Exit 0 = READY. Fix any FAIL line before enabling cron.

## Hot path (hourly)

Trees POST signed readings (firmware ≥ 0.4.8 with `device_reading`). Then:

```powershell
python -m orchard_chia.datalayer publish
python -m orchard_chia.datalayer publish --dry-run
python -m orchard_chia.datalayer publish --lookback-hours 72
```

- Only **closed** UTC hours are written.
- Progress is in `orchard_chia/data/publish_watermark.db`.
- Ops journal: `orchard_chia/data/ops/publish.jsonl`.

## Sealed path (daily / after season close)

```powershell
python -m orchard_chia.datalayer attest
```

Seals from published hour roots. **Seasons with no published readings are
SKIPPED, not sealed** — a placeholder attestation proves nothing, is unpayable,
and costs a fee to write permanently (185 of them are already on the store, and
the skip exists so there is never a 186th). To write placeholders anyway set
`ORCHARD_ATTEST_WRITE_PLACEHOLDERS=1`. Journal: `ops/attest.jsonl`.

Attest also **refuses**, by design, when: the oracle returns no Trees (an
unreadable oracle is not an empty network), a node_id is unrecognised or a
known test fixture, the store root cannot be read before writing (no baseline,
no write), or another publish/attest is running (shared writer lock,
`orchard_chia/data/datalayer-writer.lock`). Each refusal says why on stderr.

## Rewards (the ratified economics — daily)

The emission model (docs/token/EMISSION.md; fixed 85M pool, network-wide daily
ceiling, unearned rewards extend the runway):

```powershell
python -m orchard_chia.economics status               # pool, runway, backlog, stuck payments
python -m orchard_chia.economics audit                # the ledger proves itself (exit 1 on any contradiction)
python -m orchard_chia.economics report               # dry: next unsettled season
python -m orchard_chia.economics settle --season N    # dry until --yes
python -m orchard_chia.economics settle --all         # catch up every closed season, dry until --yes
python -m orchard_chia.economics pay                  # dry until two acts (below)
```

Worth knowing:

- Settlement includes **recently retired Trees** for the season being settled —
  retirement ends a Tree's future, not its history — and a sealed on-chain
  season **outranks the oracle's own count** when `ORCHARD_SETTLE_CHAIN=1`.
- `pay` **self-heals** the crash between "everything sent" and "ledger marked":
  a fully-sent cycle found unmarked is recorded, not refused forever.
- settle and live pay write ops journals (`ops/settle.jsonl`, `ops/pay.jsonl`)
  like publish and attest.
- Attest lookback defaults to **45 seasons**; `max_lookback_seasons: null`
  keeps unlimited. The legacy `orchard_chia.allocation` spender is disarmed
  like the legacy payout (`ORCHARD_ALLOCATION_SUPERSEDED_MODEL_ACK`).

- **settle** records a CLOSED season's per-Tree rewards in the pool ledger
  (`orchard_chia/data/pool_ledger.db`). The balance is derived, append-only,
  and a day settles exactly once.
- **pay** plans the oldest settled unpaid day through the spend planner.
  Going live requires **two deliberate acts** — `DRY_RUN=false` AND
  `--i-understand-this-spends-real-tokens` — plus external ceilings
  `ORCHARD_PAY_MAX_CYCLE_MOJOS` / `ORCHARD_PAY_MAX_WALLET_MOJOS`,
  `ORCHARD_ASSET_ID`, and `ORCHARD_PAY_WALLET_ID`.
- The legacy `python -m orchard_chia.payout` **refuses to spend** (superseded
  model); its reports remain for reconciling history.

## Verify

```powershell
python -m orchard_chia.cli.orchard_verify vectors orchard_chia/datalayer/testdata/vectors.json
python -m orchard_chia.cli.orchard_verify live --node-id <ID> --season <N>
# single hour (partial: season-level checks are skipped, result labelled partial):
python -m orchard_chia.cli.orchard_verify live --node-id <ID> --season <N> --hour <0-23>
# one reading by its device timestamp (SPEC §8 per-reading verify):
python -m orchard_chia.cli.orchard_verify reading --node-id <ID> --season <N> --hour <H> --ts-ms <T>
```

**Exit codes (both modes):**

| Code | Meaning | Operator action |
|------|---------|-----------------|
| `0` | VALID — every check passed | none |
| `1` | INVALID — a definitive contradiction (bad signature/Merkle/score/oracle-sig, or an on-chain value that differs from the record) | investigate tampering |
| `2` | CANNOT-VERIFY — transient/unprovable (RPC down, store root not yet **confirmed**, key not published yet, proof stale, an unsupported schema/scheme, or an **unanchored** reading — current firmware sends placeholder `block_anchor`) | retry later; **not** fraud |

`live` proves + value-binds every verdict-bearing key (`meta`/`node`/`attest`/
`readings`) against a **confirmed** store root via `get_proof` + `verify_proof`
(`current_root`). The anti-backdate anchor is checked for presence/format;
the on-chain anchor→block lookup is still a deferred live step.

`live --hour <H>` verifies a single hour: the season-level checks (season root,
verified hours, score) need every hour, so they are **skipped**, and the result
is labelled `(partial: hour NN)` — a partial slice never reports a bare VALID.
`reading` verifies one datum: device signature, Merkle membership in its hour
tree, and hour-root recompute (exit 0/1/2 as above).

## Honesty check

```powershell
python -m orchard_chia.datalayer reconcile
python -m orchard_chia.datalayer reconcile --season 12
```

Exit 1 if any oracle **overclaim** (hours_online > verified_hours).

## Env knobs

| Variable | Purpose |
|----------|---------|
| `ORCHARD_PUBLISH_LOOKBACK_HOURS` | Closed-hour catch-up window |
| `ORCHARD_PUBLISH_WATERMARK` | Override publish watermark DB path |
| `ORCHARD_DL_RPC_MAX_ATTEMPTS` | DataLayer RPC retry attempts (default 4) |
| `ORCHARD_DL_RPC_BASE_DELAY_S` | Retry base backoff seconds (default 0.5) |
| `ORCHARD_DL_RPC_MAX_DELAY_S` | Retry backoff cap seconds (default 30) |
| `ORCHARD_DL_RPC_JITTER` | Retry jitter fraction (default 0.25) |
| `ORCHARD_DL_CONFIRM_MAX` | Post-write inserts sampled per confirm (default 32) |
| `ORCHARD_OPS_LOG_DIR` | Override ops journal directory |
| `ORCHARD_SEASON_GENESIS` | `YYYY-MM-DD` season genesis (match oracle) |
| `ORCHARD_ATTEST_WRITE_PLACEHOLDERS` | Opt-in: seal no-reading seasons anyway |
| `ORCHARD_POOL_LEDGER` | Override pool ledger DB path |
| `ORCHARD_ASSET_ID` | $JUICE CAT asset id (required by `pay`, even dry) |
| `ORCHARD_PAY_MAX_CYCLE_MOJOS` / `_WALLET_MOJOS` | Live-payment ceilings (required live) |
| `ORCHARD_PAY_WALLET_ID` / `ORCHARD_PAY_FEE_MOJOS` | Wallet id / fee for live pay |
| `ORCHARD_BEACON_CACHE_TTL_S` | Oracle `/beacon` cache TTL |
| `ORCHARD_BEACON_BLOCK_ANCHOR` | Offline beacon for tests |

Retries now cover HTTP 408/429 (timeout/rate-limit) in addition to 5xx and
network errors; permanent 4xx are not retried — **except `batch_update`, which
is submitted exactly once**: an ambiguous timeout may already be in the
mempool, and a retry would pay the fee again. The post-write confirm is the
real retry. `get_keys` probes the page-index convention and pages through
large stores; an empty first page against a multi-page store raises instead of
reading as an empty dataset.

The oracle enforces two defaults worth knowing at the ingest side:
`require_seq` is **on** (replayed readings are rejected; firmware ≥0.5 sends a
monotonic NVS-persisted seq) and an hour needs **30 accepted readings** to
credit `hours_online` (half the 60s cadence — one ping an hour is no longer an
hour).

## Windows schedule (one command)

```powershell
powershell -ExecutionPolicy Bypass -File tools\schedule_windows.ps1
```

Registers four tasks: **Publish** hourly at :10, **Attest** daily 00:25,
**Settle** (`economics settle --all --yes`) daily 00:40, **Status** daily
08:00. Output appends to `orchard_chia\data\ops\scheduler-*.log`.
`-Unregister` removes them.

**Paying is deliberately NOT scheduled.** `economics pay` stays a human act —
it needs DRY_RUN=false, the explicit flag, external ceilings and a wallet id,
and no timer should launder that decision. The daily Status task shows the
unpaid backlog so you know when to act. A timer tick overlapping a manual run
is safe: the shared writer lock refuses the second entrant.

Publish and attest cannot overlap (shared lock); a run that finds the lock
held exits 64 with the holder's pid. `/network/stats` now exposes
`last_attestation_at` / `last_reading_at`, and the external heartbeat warns
when readings flow but nothing has reached the chain for 48h.

## Firmware

Use Tree firmware **0.4.8+** so each POST includes `device_reading` (secp256r1). Older trees only HMAC-auth to the oracle and cannot publish verifiable readings.


## Secrets

orchard_chia/data/oracle_signing_key.hex is the Season signer seed. Mode 0600 on POSIX; never commit. Compromise allows forged season attestations.


## Specs

- [SPEC](../datalayer/SPEC.md)
- [Chia DataLayer RPC/CLI reference](../datalayer/reference/CHIA_DATALAYER_RPC.md) — exact wire shapes we integrate against
- [ADR-0003](../decisions/0003-datalayer-verifiable-dataset.md)
- [ADR-0007 secp256r1](../decisions/) (if present in tree)


## Confirm lag

If post-write confirm fails intermittently, DataLayer may still be applying the batch. Re-run publish/attest; writers are convergent. Do not delete the watermark by hand unless recovering from corruption.

