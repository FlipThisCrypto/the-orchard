<!-- SPDX-License-Identifier: Apache-2.0 -->
# Tree simulator (`tools/tree_sim`)

Emulates virtual Trees posting to an oracle (HANDOVER T11). Each virtual Tree
mirrors the firmware: a random `node_id` + 32-byte HMAC secret, a monotonic
`seq` (replay protection, T3), a real UTC `ts` (T6), and sensor values that
drift each reading. Readings are HMAC-signed exactly like the firmware, so the
oracle accepts them.

Run from the repo root.

## Functional (1 Tree, verbose)
```bash
python -m tools.tree_sim.sim --oracle http://127.0.0.1:8000 --mode functional
```
Registers, posts a few signed readings, prints each oracle response, exits
non-zero if anything was rejected. Needs `requests` (`pip install requests`).

## Load (N Trees, report latency + error rate)
```bash
python -m tools.tree_sim.sim --oracle https://oracle.theorchard.network \
    --mode load --trees 1000 --duration 60 --interval 60 --workers 64
```
Each Tree posts every `--interval` seconds for `--duration`; prints latency
p50/p95/max, a status histogram, and the error rate. Use it to answer "does
the oracle hold up at 1,000 Trees?" before shipping hardware.

> Point load runs at a **staging/dev oracle**, not production — 1,000 Trees ×
> frequent posts will trip the production rate limits (and pollute the real
> uptime ledger). Use a throwaway DB.

## Negative / adversarial modes
```bash
python -m tools.tree_sim.sim --oracle http://127.0.0.1:8000 --mode negative
# or a subset:
python -m tools.tree_sim.sim --oracle http://127.0.0.1:8000 --mode negative \
    --attacks duplicate_seq,invalid_sig,unknown_node,oversized
```
Registers one Tree, posts a good baseline reading, then each attack mode.
Useful statuses (with `ORCHARD_ORACLE_REQUIRE_SEQ=true` and default body/
future-skew limits):

| Mode | Typical status |
|------|----------------|
| `duplicate_seq` / `decreasing_seq` | 409 |
| `invalid_sig` / `wrong_key` | 401 |
| `unknown_node` | 404 |
| `missing_seq` | 400 |
| `oversized` | 413 |
| `stale_ts` / `future_ts` | 422 (when age/future limits enabled) |
| `malformed` | 400 |
| `missing_sensors` | 202 (empty sensors still accepted) |

## CI / integration test
`oracle/tests/test_tree_sim_integration.py` runs the simulator against the
real FastAPI app **in-process** (via `TestClient`) — register → sign → POST →
uptime credit, **plus** the full negative-mode suite under `require_seq=true`.
It's part of the normal `python -m pytest` run, so the end-to-end wire contract
is checked on every CI build with no server to spawn.
