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

## CI / integration test
`oracle/tests/test_tree_sim_integration.py` runs the simulator against the
real FastAPI app **in-process** (via `TestClient`) — register → sign → POST →
uptime credit. It's part of the normal `python -m pytest` run, so the
end-to-end wire contract is checked on every CI build with no server to spawn.
