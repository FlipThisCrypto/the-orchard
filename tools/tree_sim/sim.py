# SPDX-License-Identifier: Apache-2.0
"""Tree simulator — emulate virtual Trees posting to an oracle (HANDOVER T11).

Two modes:

  functional  one (or a few) Trees, verbose: register -> post a few signed
              readings -> print each oracle response. A quick "is the oracle
              alive and accepting correctly-signed readings?" check, and the
              backbone of the CI end-to-end integration test.

  load        N Trees posting concurrently for a duration; reports latency
              percentiles, status histogram, and error rate against a target
              oracle. Use it to answer "does the oracle hold up at 1,000
              Trees?" before shipping hardware.

A virtual Tree mirrors the firmware: a random node_id + 32-byte HMAC secret,
a monotonic `seq` (replay protection, T3), a real UTC `ts` (T6), and sensor
values that drift realistically each reading. Readings are signed exactly the
way the firmware signs them — HMAC-SHA256 of the raw JSON body, hex in the
`X-Orchard-Sig` header — so the oracle accepts them.

The transport is injectable: the CLI talks real HTTP (`requests`); the CI
integration test passes a FastAPI ``TestClient`` so the whole loop runs
in-process with no server to spawn.

Run from the repo root:
    python -m tools.tree_sim.sim --oracle http://127.0.0.1:8000 --mode functional
    python -m tools.tree_sim.sim --oracle https://oracle.theorchard.network \
        --mode load --trees 1000 --duration 60 --interval 60
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- #
# A virtual Tree                                                              #
# --------------------------------------------------------------------------- #
@dataclass
class VirtualTree:
    node_id: str
    secret_hex: str
    label: str = "sim-tree"
    seq: int = 0
    # Drifting sensor baselines (integer fixed-point-ish, like the fleet).
    _temp_mc: int = field(default_factory=lambda: random.randint(15_000, 28_000))
    _humidity: int = field(default_factory=lambda: random.randint(30_000, 70_000))
    _pressure: int = field(default_factory=lambda: random.randint(99_000, 103_000))
    _gas_adc: int = field(default_factory=lambda: random.randint(800, 2_400))
    _lat: float = field(default_factory=lambda: round(random.uniform(25.0, 49.0), 5))
    _lon: float = field(default_factory=lambda: round(random.uniform(-124.0, -67.0), 5))

    @classmethod
    def random(cls, i: int) -> "VirtualTree":
        return cls(
            node_id=os.urandom(16).hex().upper(),
            secret_hex=os.urandom(32).hex(),
            label=f"sim-tree-{i:04d}",
        )

    def _drift(self) -> None:
        self._temp_mc += random.randint(-150, 150)
        self._humidity = max(0, min(100_000, self._humidity + random.randint(-300, 300)))
        self._pressure += random.randint(-40, 40)
        self._gas_adc = max(0, self._gas_adc + random.randint(-25, 25))
        self._lat = round(self._lat + random.uniform(-0.00005, 0.00005), 5)
        self._lon = round(self._lon + random.uniform(-0.00005, 0.00005), 5)

    def next_body(self) -> bytes:
        """Build the next reading body (bytes) the way the firmware would."""
        self.seq += 1
        self._drift()
        body = {
            "node_id": self.node_id,
            "fw": "sim",
            "ts_ms": int(time.monotonic() * 1000) & 0xFFFFFFFF,
            "ts": int(time.time()),
            "seq": self.seq,
            "sensors": {
                "bme280": {
                    "temperature_mc": self._temp_mc,
                    "humidity_milli_pct": self._humidity,
                    "pressure_pa": self._pressure,
                },
                "mq135": {"gas_adc_raw": self._gas_adc},
                "gps": {
                    "fix": True,
                    "lat": self._lat,
                    "lon": self._lon,
                    "satellites": random.randint(5, 12),
                },
            },
        }
        return json.dumps(body, separators=(",", ":")).encode("utf-8")

    def sign(self, body: bytes) -> str:
        return hmac.new(bytes.fromhex(self.secret_hex), body, hashlib.sha256).hexdigest().upper()


# --------------------------------------------------------------------------- #
# Transport — real HTTP, or an injected in-process client (TestClient)        #
# --------------------------------------------------------------------------- #
class OracleClient:
    """Talks to the oracle. Pass `base_url` for real HTTP, or `client` (a
    FastAPI TestClient) for in-process calls — the sim code is identical."""

    def __init__(self, base_url: str | None = None, client=None, timeout: float = 10.0):
        if (base_url is None) == (client is None):
            raise ValueError("pass exactly one of base_url / client")
        self.base_url = base_url.rstrip("/") if base_url else None
        self._client = client
        self.timeout = timeout
        self._requests = None
        if base_url is not None:
            import requests  # lazy: not needed for in-process use
            self._requests = requests.Session()

    def register(self, tree: VirtualTree):
        payload = {"node_id": tree.node_id, "signing_key_hex": tree.secret_hex,
                   "label": tree.label}
        if self._client is not None:
            return self._client.post("/register", json=payload)
        return self._requests.post(f"{self.base_url}/register", json=payload,
                                   timeout=self.timeout)

    def post_reading(self, tree: VirtualTree):
        body = tree.next_body()
        headers = {
            "Content-Type": "application/json",
            "X-Orchard-Node": tree.node_id,
            "X-Orchard-Sig": tree.sign(body),
        }
        if self._client is not None:
            return self._client.post("/readings", content=body, headers=headers)
        return self._requests.post(f"{self.base_url}/readings", data=body,
                                   headers=headers, timeout=self.timeout)


# --------------------------------------------------------------------------- #
# Modes                                                                       #
# --------------------------------------------------------------------------- #
def run_functional(client: OracleClient, trees: int = 1, rounds: int = 3,
                   verbose: bool = True) -> dict:
    """Register `trees` Trees and post `rounds` readings each. Returns a stats
    dict; raises nothing — callers inspect accepted/failed counts."""
    fleet = [VirtualTree.random(i) for i in range(trees)]
    accepted = 0
    failures: list[str] = []

    for t in fleet:
        r = client.register(t)
        ok = r.status_code in (200, 201)
        if verbose:
            print(f"[register] {t.node_id[:8]}… -> {r.status_code}")
        if not ok:
            failures.append(f"register {t.node_id[:8]} -> {r.status_code}: {_body(r)}")

    for rnd in range(rounds):
        for t in fleet:
            r = client.post_reading(t)
            if r.status_code == 202:
                accepted += 1
            else:
                failures.append(f"reading {t.node_id[:8]} seq={t.seq} -> "
                                f"{r.status_code}: {_body(r)}")
            if verbose:
                print(f"[reading] {t.node_id[:8]}… round {rnd + 1} seq={t.seq} "
                      f"-> {r.status_code}")

    return {"trees": trees, "rounds": rounds, "accepted": accepted,
            "failures": failures}


def run_load(base_url: str, trees: int, duration_s: float, interval_s: float,
             workers: int = 32) -> dict:
    """Threaded load: each Tree posts every `interval_s` for `duration_s`.
    Reports latency percentiles + a status histogram. Real HTTP only."""
    fleet = [VirtualTree.random(i) for i in range(trees)]
    # Register first (sequentially is fine; it's not the thing under test).
    reg = OracleClient(base_url=base_url)
    for t in fleet:
        try:
            reg.register(t)
        except Exception as e:  # noqa: BLE001 — report, don't abort the run
            print(f"[load] register {t.node_id[:8]} failed: {e}", file=sys.stderr)

    latencies: list[float] = []
    status_hist: dict[str, int] = {}
    deadline = time.time() + duration_s

    def hammer(t: VirtualTree) -> None:
        c = OracleClient(base_url=base_url)
        while time.time() < deadline:
            start = time.perf_counter()
            try:
                r = c.post_reading(t)
                code = str(r.status_code)
            except Exception as e:  # noqa: BLE001
                code = type(e).__name__
            latencies.append(time.perf_counter() - start)
            status_hist[code] = status_hist.get(code, 0) + 1
            time.sleep(interval_s)

    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(as_completed(ex.submit(hammer, t) for t in fleet))

    total = sum(status_hist.values())
    ok = status_hist.get("202", 0)
    lat_sorted = sorted(latencies)
    pct = (lambda p: lat_sorted[min(len(lat_sorted) - 1, int(len(lat_sorted) * p))]
           if lat_sorted else 0.0)
    return {
        "trees": trees, "requests": total, "accepted": ok,
        "error_rate": round(1 - ok / total, 4) if total else None,
        "status_histogram": status_hist,
        "latency_ms": {
            "p50": round(pct(0.50) * 1000, 1),
            "p95": round(pct(0.95) * 1000, 1),
            "max": round((max(lat_sorted) if lat_sorted else 0) * 1000, 1),
            "mean": round(statistics.mean(latencies) * 1000, 1) if latencies else 0.0,
        },
    }


def _body(r) -> str:
    try:
        return json.dumps(r.json())[:200]
    except Exception:  # noqa: BLE001
        return getattr(r, "text", "")[:200]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m tools.tree_sim.sim",
                                description="Emulate virtual Trees against an oracle.")
    p.add_argument("--oracle", required=True, help="base URL, e.g. http://127.0.0.1:8000")
    p.add_argument("--mode", choices=["functional", "load"], default="functional")
    p.add_argument("--trees", type=int, default=1)
    p.add_argument("--rounds", type=int, default=3, help="functional: readings per Tree")
    p.add_argument("--duration", type=float, default=30.0, help="load: seconds")
    p.add_argument("--interval", type=float, default=60.0, help="load: seconds between a Tree's posts")
    p.add_argument("--workers", type=int, default=32, help="load: concurrent threads")
    p.add_argument("--seed", type=int, default=None, help="deterministic identities/drift")
    args = p.parse_args(argv)

    if args.seed is not None:
        random.seed(args.seed)

    if args.mode == "functional":
        stats = run_functional(OracleClient(base_url=args.oracle),
                               trees=args.trees, rounds=args.rounds, verbose=True)
        print(json.dumps({k: v for k, v in stats.items() if k != "failures"}, indent=2))
        if stats["failures"]:
            print("FAILURES:", file=sys.stderr)
            for f in stats["failures"]:
                print("  " + f, file=sys.stderr)
            return 1
        return 0

    stats = run_load(args.oracle, trees=args.trees, duration_s=args.duration,
                     interval_s=args.interval, workers=args.workers)
    print(json.dumps(stats, indent=2))
    return 0 if stats.get("error_rate") in (0, 0.0, None) else 1


if __name__ == "__main__":
    raise SystemExit(main())
