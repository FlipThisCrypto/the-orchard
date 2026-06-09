# SPDX-License-Identifier: Apache-2.0
"""Verification engine for an Orchard DataLayer reading bundle.

Pure — no I/O, no printing. Reuses ``schema`` + ``merkle`` (NO duplicate crypto).
The CLI (``orchard_chia.cli.orchard_verify``) loads records and renders results.

This is the executable form of the project tenet — *don't trust the oracle,
verify it*. Given the published records, it independently re-derives every claim:
the device signed the reading, the reading is in the hour's Merkle tree, the hour
is in the season root, the uptime/score the oracle published is what the public
readings actually support, and the oracle's season signature checks out.

A **bundle** is the records needed to verify a season (or a slice of one):

    meta              — meta:schema           (units + oracle season_pubkey)
    node              — node:<NODE_ID>        (device pubkey)
    attest            — attest:<NODE_ID>:<S>  (uptime, roots, score, oracle_sig)
    readings_records  — [readings:<NODE_ID>:<S>:<H>, ...]  one or more hours

Offline mode loads it from ``testdata/vectors.json``; live mode (Phase 2) will
assemble the same shape from DataLayer reads.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import merkle, schema


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    node_id: str
    season: int
    hours: list[int] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)


def verify_bundle(
    *, meta: dict, node: dict, attest: dict, readings_records: list[dict]
) -> Report:
    """Run the seven checks (SPEC §7) over a bundle. Never raises on bad data —
    a failure is a failed Check, so tampering shows up as ``INVALID`` rather than
    a crash."""
    node = node or {}
    attest = attest or {}
    node_pub = node.get("pubkey", "")
    oracle_pub = ((meta or {}).get("signer") or {}).get("season_pubkey")

    by_hour: dict[int, list[dict]] = {
        int(rec["hour"]): rec.get("readings", []) for rec in readings_records
    }
    hours = sorted(by_hour)
    all_readings = [r for rec in readings_records for r in rec.get("readings", [])]

    checks: list[Check] = []

    # 1. Device provenance — every reading signed by the node's published key.
    bad_sig = [
        r for rec in readings_records for r in rec.get("readings", [])
        if not schema.verify_reading(r, node_pub)
    ]
    checks.append(Check(
        "Device signature verified",
        bool(all_readings) and not bad_sig,
        f"{len(all_readings)} reading(s) signed by node {node.get('node_id', '?')[:8]}…"
        if not bad_sig else f"{len(bad_sig)} reading(s) failed the signature check",
    ))

    # 2. Inclusion — prove one reading sits under its hour_root via a Merkle path.
    proof_ok, proof_detail = False, "no readings to prove"
    if readings_records:
        rec0 = readings_records[0]
        ordered = schema._sorted_readings(rec0.get("readings", []))
        if ordered:
            leaves = [schema.reading_leaf(r) for r in ordered]
            try:
                path = merkle.merkle_proof(leaves, 0)
                proof_ok = merkle.verify_proof(
                    leaves[0], path, bytes.fromhex(rec0["hour_root"])
                )
                proof_detail = f"leaf 0 of hour {int(rec0['hour']):02d} via {len(path)}-step path"
            except (ValueError, IndexError) as e:
                proof_detail = f"proof error: {e}"
    checks.append(Check("Reading Merkle proof verified", proof_ok, proof_detail))

    # 3. Hour roots — each record's stored hour_root equals a recompute.
    hr_bad = [
        int(rec["hour"]) for rec in readings_records
        if schema.hour_root(rec.get("readings", [])) != rec.get("hour_root")
    ]
    checks.append(Check(
        "Hour root verified", not hr_bad,
        f"{len(readings_records)} hour root(s) match recompute"
        if not hr_bad else f"mismatch at hour(s) {hr_bad}",
    ))

    # 4. Season root — recompute from the present hours; must equal attest.
    recomputed_sr = schema.season_root({h: schema.hour_root(by_hour[h]) for h in by_hour})
    sr_ok = recomputed_sr == attest.get("season_root") == attest.get("data_hash")
    checks.append(Check(
        "Season root verified", sr_ok,
        f"{recomputed_sr[:16]}… over hour(s) {hours}"
        if sr_ok else "season root mismatch (tampered, or a partial bundle)",
    ))

    # 5. Verified hours — recompute from public signed readings.
    vh = schema.verified_hours(by_hour, node_pub)
    checks.append(Check(
        "Verified hours recomputed", vh == attest.get("verified_hours"),
        f"recomputed={vh} claimed={attest.get('verified_hours')}",
    ))

    # 6. Season score — the verifiable reward metric.
    ss = schema.season_score(vh)
    checks.append(Check(
        "Season score recomputed", ss == attest.get("season_score"),
        f"recomputed={ss} claimed={attest.get('season_score')}",
    ))

    # 7. Oracle season signature — against the pubkey published in meta.
    if not oracle_pub:
        checks.append(Check(
            "Oracle season signature verified", False,
            "no season_pubkey in meta:schema.signer",
        ))
    else:
        checks.append(Check(
            "Oracle season signature verified",
            schema.verify_attest(attest, oracle_pub),
            f"ed25519 by oracle {oracle_pub[:8]}…",
        ))

    return Report(
        node_id=node.get("node_id", ""),
        season=int(attest.get("season", 0)),
        hours=hours,
        checks=checks,
    )
