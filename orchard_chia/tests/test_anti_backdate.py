# SPDX-License-Identifier: Apache-2.0
"""Anti-backdate anchor check (SPEC §7 check 4, offline half).

Verifies block_anchor presence/format. The chain lookup (anchor vs a real
block with timestamp <= ts_ms) is the live-only half and is not exercised here.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

from orchard_chia.datalayer import verify

VPATH = Path(__file__).resolve().parents[1] / "datalayer" / "testdata" / "vectors.json"
VEC = json.loads(VPATH.read_text(encoding="utf-8"))


def _bundle() -> dict:
    rec = copy.deepcopy(VEC["records"])
    return {
        "meta": rec["meta"],
        "node": rec["node"],
        "attest": rec["attest"],
        "readings_records": [rec["readings"]],
    }


def _anchor_check(rep):
    return next(c for c in rep.checks if c.name == "Anti-backdate anchor present")


def test_wellformed_helper():
    assert verify._anchor_wellformed("a1b2c3d4e5f60718") is True
    assert verify._anchor_wellformed("A1B2C3D4E5F60718") is True  # case-insensitive
    assert verify._anchor_wellformed("0000000000000000") is False  # placeholder
    assert verify._anchor_wellformed("a1b2") is False              # too short
    assert verify._anchor_wellformed("zzzzzzzzzzzzzzzz") is False   # non-hex
    assert verify._anchor_wellformed(None) is False
    assert verify._anchor_wellformed(12345678) is False


def test_vectors_have_valid_anchor():
    rep = verify.verify_bundle(**_bundle())
    assert _anchor_check(rep).ok is True


def test_placeholder_anchor_fails():
    b = _bundle()
    b["readings_records"][0]["readings"][0]["block_anchor"] = "0000000000000000"
    rep = verify.verify_bundle(**b)
    c = _anchor_check(rep)
    assert c.ok is False
    assert rep.valid is False


def test_malformed_anchor_fails():
    b = _bundle()
    b["readings_records"][0]["readings"][0]["block_anchor"] = "not-hex!"
    rep = verify.verify_bundle(**b)
    assert _anchor_check(rep).ok is False


def test_missing_anchor_fails():
    b = _bundle()
    del b["readings_records"][0]["readings"][0]["block_anchor"]
    rep = verify.verify_bundle(**b)
    assert _anchor_check(rep).ok is False
