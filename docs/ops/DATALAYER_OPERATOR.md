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

Uses published hour roots when present; otherwise uptime placeholder. Journal: `ops/attest.jsonl`.

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
| `2` | CANNOT-VERIFY — transient/unprovable (RPC down, store root not yet **confirmed**, key not published yet, proof stale) | retry later; **not** fraud |

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
| `ORCHARD_BEACON_CACHE_TTL_S` | Oracle `/beacon` cache TTL |
| `ORCHARD_BEACON_BLOCK_ANCHOR` | Offline beacon for tests |

Retries now cover HTTP 408/429 (timeout/rate-limit) in addition to 5xx and
network errors; permanent 4xx are not retried. `get_keys` pages through large
stores automatically (no silent truncation).

## Suggested Windows schedule

1. Preflight at boot (alert if NOT READY).
2. Publish every hour at :05.
3. Attest once daily after 00:10 UTC.
4. Reconcile daily; page on exit 1.

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

