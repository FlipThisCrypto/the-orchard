# oracle/ — Sensor data oracle

A FastAPI service that Trees POST signed readings to. Stores raw data in SQLite locally and exposes the endpoints the dashboard and the attestation writer use.

> **Vocabulary:** the code uses `node`, `season`, `reading`. User-facing copy says **Tree, Season, Harvest**. See the [Glossary](../README.md#glossary).

## Why this is its own component

The oracle is the single trust boundary in v1 between "what Trees claim" and "what the system records." It verifies HMAC signatures, tracks per-hour uptime, and stores the raw data the daily attestations (Phase 5) and reward payouts (Phase 7) are computed from. It is deliberately small and replaceable — v2+ may decentralize it (e.g., Keepers cross-validate submissions).

## Endpoints

| Method | Path                              | Purpose                                                                |
|--------|-----------------------------------|------------------------------------------------------------------------|
| GET    | `/`                               | Liveness + current Season + public `flags`                             |
| GET    | `/health`                         | `{"ok": true, "flags": {...}, "metrics": {...}}` (no secrets/payloads) |
| POST   | `/register`                       | Register a new Tree (`node_id`, `signing_key_hex`, optional wallet/label) |
| POST   | `/readings`                       | Tree submits a signed reading (HMAC-SHA256 over the raw body)          |
| GET    | `/readings/{node_id}?limit=50`    | Most recent readings for a Tree                                        |
| GET    | `/nodes`                          | List all registered Trees                                              |
| GET    | `/nodes/{node_id}`                | Single Tree status (last seen, last reading, fw, etc.)                 |
| GET    | `/uptime/{node_id}/{season}`      | Hour-buckets a Tree was alive in for a given Season (0-24)             |

OpenAPI docs are automatic at `/docs` and `/redoc` when the server is running.

## Authentication

Every `/readings` POST must carry:

```
X-Orchard-Node: <hex node_id, 32 chars>
X-Orchard-Sig:  <hex HMAC-SHA256 of the raw request body, 64 chars>
```

The HMAC secret is the 32-byte signing key the Tree generated on first boot and the dashboard pushed to `/register`. The oracle recomputes the HMAC over the bytes it received (no JSON re-parsing) and constant-time compares.

`/register` requires a wallet session by default (`require_wallet_session=true`);
claim-code onboarding uses `/provision/*` instead. Remote callers are rate-limited
on `/auth/*`, `/readings`, `/provision/*`, and `/register` (loopback exempt).

Reading rejections (replay, bad sig, oversized body, future `ts`, etc.) increment
process counters on `GET /health` → `metrics` — counters only, never payloads.

## Storage

SQLite via SQLAlchemy 2.x. Tables:

- `nodes` — every registered Tree.
- `readings` — every signed POST. Full payload kept as JSON text.
- `uptime_hours` — `(node_id, hour_utc)` bucket counts.
- `seasons` — Season metadata (filled lazily; Phase 5 adds block heights).
- `attestations` — per-`(node_id, season)` Season attestations (Phase 5 writes these to DataLayer).

DB lives at `oracle/data/orchard.db` by default. The `oracle/data/` directory is gitignored.

## v1 Season math (simplified)

`seasons.py` aligns Seasons to **UTC days** (00:00Z to next 00:00Z), with Season 1 starting at `ORCHARD_ORACLE_SEASON_GENESIS_DATE` (default `2026-05-27`). Uptime within a Season is the count of distinct UTC hours a Tree submitted at least one reading in (0-24).

Phase 5 will swap this for **Chia-block-aligned Seasons** (4608 blocks ≈ 24h). The Season-bounds helpers in `seasons.py` are the only thing that changes — the rest of the oracle treats `(node_id, season_number)` as an opaque pair.

## Quick start

```bash
# from the repo root
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # macOS / Linux
pip install -r oracle/requirements.txt

# (Optional) customize:
cp oracle/.env.example oracle/.env
# edit oracle/.env

# Run (binds loopback by default)
python -m oracle.app.main

# Or if you need real Trees on your LAN to reach it directly:
# (Security note: /register has no auth — anyone on the same network
# can claim a node_id. Only use 0.0.0.0 on a trusted LAN.)
ORCHARD_ORACLE_HOST=0.0.0.0 python -m oracle.app.main
```

Open `http://localhost:8000/` to confirm it's up, and `http://localhost:8000/docs` for interactive OpenAPI.

## Smoke tests

```bash
pytest oracle/tests/
```

Tests cover: service identification, registration (happy + duplicate + conflict), unknown node, bad signature, end-to-end signed reading + retrieve + uptime bucket increment.

## Talking to it from a real Tree

1. **Register the Tree once** (the dashboard will do this in Phase 4; meanwhile by hand):
   ```bash
   curl -X POST http://localhost:8000/register \
        -H "Content-Type: application/json" \
        -d '{"node_id":"5B9BB022649FA93D4091DA4BA40714B9",
             "signing_key_hex":"<64 hex chars from `KEY` command>",
             "label":"My first Tree"}'
   ```
2. **Provision the Tree** over USB-serial:
   ```
   WIFI_SET <ssid> <pass>
   ORACLE_SET http://<your-pc-lan-ip>:8000/readings
   ```
3. **Force an immediate sample** with `SAMPLE_NOW` or just wait for the next cadence tick. The Tree will POST; the oracle will verify the signature, store the reading, and bump the uptime hour.

## Status

Phase 3 implemented: register, signed readings, retrieve, uptime by Season, all tested. Phase 5 will add the DataLayer attestation writer; Phase 6 will gate `/register` on Orchard Pass ownership.
