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

Paste the returned `id` into `datalayer.store_id`.

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
```

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
| `ORCHARD_DL_RPC_MAX_ATTEMPTS` | DataLayer RPC retries |
| `ORCHARD_OPS_LOG_DIR` | Override ops journal directory |
| `ORCHARD_BEACON_CACHE_TTL_S` | Oracle `/beacon` cache TTL |
| `ORCHARD_BEACON_BLOCK_ANCHOR` | Offline beacon for tests |

## Suggested Windows schedule

1. Preflight at boot (alert if NOT READY).
2. Publish every hour at :05.
3. Attest once daily after 00:10 UTC.
4. Reconcile daily; page on exit 1.

## Firmware

Use Tree firmware **0.4.8+** so each POST includes `device_reading` (secp256r1). Older trees only HMAC-auth to the oracle and cannot publish verifiable readings.


## Secrets

orchard_chia/data/oracle_signing_key.hex is the Season signer seed. Mode 0600 on POSIX; never commit. Compromise allows forged season attestations.

