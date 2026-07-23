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


def _anchor_wellformed(anchor: object) -> bool:
    """A ``block_anchor`` is the first 16 hex chars (8 bytes) of a recent Chia
    header hash (SPEC §2.3 / §4.2). Well-formed = a 16-char hex string that is
    not the all-zero placeholder (all-zero ⇒ the reading was never anchored).

    This validates the anchor is *present and parseable*; confirming it against
    a real block whose ``timestamp ≤ ts_ms`` requires a full node and is the
    live-only half of SPEC §7 check 4.
    """
    if not isinstance(anchor, str) or len(anchor) != 16:
        return False
    try:
        return int(anchor, 16) != 0
    except ValueError:
        return False


@dataclass
class Report:
    node_id: str
    season: int
    hours: list[int] = field(default_factory=list)
    checks: list[Check] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return bool(self.checks) and all(c.ok for c in self.checks)

    def as_dict(self) -> dict:
        """JSON-serializable summary for automation / --json CLIs."""
        return {
            "node_id": self.node_id,
            "season": self.season,
            "hours": list(self.hours),
            "valid": self.valid,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail}
                for c in self.checks
            ],
        }


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

    # Sanitize: a readings record must have an integer hour to be placed in the
    # season tree. Malformed records are flagged as a failed check below rather
    # than crashing (verify_bundle never raises on bad data).
    valid_records: list[dict] = []
    malformed_hours = 0
    for rec in readings_records:
        try:
            int(rec["hour"])
        except (KeyError, TypeError, ValueError):
            malformed_hours += 1
            continue
        valid_records.append(rec)

    by_hour: dict[int, list[dict]] = {
        int(rec["hour"]): rec.get("readings", []) for rec in valid_records
    }
    hours = sorted(by_hour)
    all_readings = [r for rec in readings_records for r in rec.get("readings", [])]

    checks: list[Check] = []

    checks.append(Check(
        "Readings records well-formed",
        malformed_hours == 0,
        "all records have an integer hour" if malformed_hours == 0
        else f"{malformed_hours} readings record(s) with a missing/invalid hour",
    ))

    # 0. Bundle consistency — every record must be about the SAME node·season,
    # so a bundle can't be stitched from a node card, attest, and readings that
    # actually belong to different nodes/seasons.
    nid = str(node.get("node_id", "")).upper()
    consistency: list[str] = []
    if not nid:
        consistency.append("node card has no node_id")
    if str(attest.get("node_id", "")).upper() != nid:
        consistency.append("attest node_id != node card")
    a_season = attest.get("season")
    for rec in readings_records:
        if str(rec.get("node_id", "")).upper() != nid:
            consistency.append(f"readings hour {rec.get('hour')} node_id != node card")
        if a_season is not None and rec.get("season") != a_season:
            consistency.append(f"readings hour {rec.get('hour')} season != attest")
        if any(str(r.get("node_id", "")).upper() != nid for r in rec.get("readings", [])):
            consistency.append(f"a reading in hour {rec.get('hour')} has a foreign node_id")
    checks.append(Check(
        "Records agree on node and season",
        bool(nid) and not consistency,
        "all records share one node·season"
        if not consistency else "; ".join(consistency[:3]),
    ))

    # 0b. Schema/scheme compatibility — this verifier implements schema 1.x with
    # secp256r1 device + season signatures. A store declaring a different major
    # version or a different signer scheme cannot be meaningfully verified here
    # (its sigs would read as failures), so flag it rather than mislead.
    signer = (meta or {}).get("signer") or {}
    schema_v = str((meta or {}).get("orchard_schema", ""))
    want_major = schema.SCHEMA_VERSION.split(".")[0]
    got_major = schema_v.split(".")[0] if schema_v else ""
    compat: list[str] = []
    if got_major != want_major:
        compat.append(f"schema {schema_v or '?'} != {schema.SCHEMA_VERSION}")
    if signer.get("device_sig") not in (None, "secp256r1"):
        compat.append(f"device_sig {signer.get('device_sig')!r} unsupported")
    if signer.get("season_sig") not in (None, "secp256r1"):
        compat.append(f"season_sig {signer.get('season_sig')!r} unsupported")
    checks.append(Check(
        "Schema and signer scheme supported",
        not compat,
        f"schema {schema_v or '?'}, secp256r1" if not compat else "; ".join(compat),
    ))

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

    # 1b. Anti-backdate anchor (SPEC §7 check 4, offline half). Every reading
    # must carry a well-formed, non-placeholder block anchor. Confirming it
    # against a real block (timestamp ≤ ts_ms) is the live-only chain lookup.
    bad_anchor = [
        r for r in all_readings if not _anchor_wellformed(r.get("block_anchor"))
    ]
    checks.append(Check(
        "Anti-backdate anchor present",
        bool(all_readings) and not bad_anchor,
        f"{len(all_readings)} reading(s) carry a 16-hex block anchor "
        f"(chain lookup is a live-only step)"
        if not bad_anchor
        else f"{len(bad_anchor)} reading(s) have a missing, placeholder, or "
             f"malformed block anchor",
    ))

    # 2. Inclusion — prove EVERY reading sits under its hour_root via its own
    #    Merkle path, so any single reading is provably in the tree (not just a
    #    sampled leaf). Exercises odd-level promotion and ordering for real data.
    proof_ok, proof_detail = False, "no readings to prove"
    if valid_records:
        proven = 0
        failures: list[str] = []
        for rec in valid_records:
            ordered = schema._sorted_readings(rec.get("readings", []))
            if not ordered:
                continue
            try:
                root = bytes.fromhex(rec["hour_root"])
            except (ValueError, KeyError, TypeError):
                failures.append(f"hour {rec.get('hour')}: bad hour_root")
                continue
            leaves = [schema.reading_leaf(r) for r in ordered]
            for i in range(len(leaves)):
                try:
                    path = merkle.merkle_proof(leaves, i)
                    if merkle.verify_proof(leaves[i], path, root):
                        proven += 1
                    else:
                        failures.append(f"hour {int(rec['hour']):02d} leaf {i}")
                except (ValueError, IndexError) as e:
                    failures.append(f"hour {rec.get('hour')} leaf {i}: {e}")
        proof_ok = proven > 0 and not failures
        if proof_ok:
            proof_detail = f"{proven} reading(s) each proven to their hour_root"
        elif failures:
            proof_detail = (
                f"{len(failures)} Merkle path failure(s): {failures[:3]}"
            )
    checks.append(Check("Reading Merkle proof verified", proof_ok, proof_detail))

    # 3. Hour roots — each record's stored hour_root equals a recompute.
    hr_bad = [
        int(rec["hour"]) for rec in valid_records
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
            f"secp256r1 by oracle {oracle_pub[:8]}…",
        ))

    return Report(
        node_id=node.get("node_id", ""),
        season=int(attest.get("season", 0)),
        hours=hours,
        checks=checks,
    )
