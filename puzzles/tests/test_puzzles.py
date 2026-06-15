# SPDX-License-Identifier: Apache-2.0
"""Run the compiled Orchard puzzles under the consensus VM (HANDOVER T19).

Loads the committed bytecode from ``puzzles/hashes.json`` and executes it with
``chia_rs.run_chia_program`` (the same clvm_rs the mainnet uses). Also guards
against pin drift: a fresh compile must match the committed hashes, so a
``.clsp`` change can't land without regenerating ``hashes.json``.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from chia_rs import run_chia_program
from clvm import SExp

PUZZLES_DIR = Path(__file__).resolve().parents[1]
HASHES = json.loads((PUZZLES_DIR / "hashes.json").read_text(encoding="utf-8"))
MAX_COST = 11_000_000_000


def _prog(name: str) -> bytes:
    return bytes.fromhex(HASHES[name]["clvm_hex"])


def _solution(*args) -> bytes:
    # Serialize a Python list of args into a CLVM solution blob.
    return SExp.to(list(args)).as_bin()


def test_hashlock_accepts_correct_preimage():
    preimage = b"open-sesame"
    digest = hashlib.sha256(preimage).digest()
    marker = b"\xa5" * 8  # stands in for the returned conditions
    _cost, out = run_chia_program(
        _prog("hashlock"), _solution(digest, preimage, marker), MAX_COST, 0)
    assert out.atom == marker  # puzzle echoes the conditions on success


def test_hashlock_rejects_wrong_preimage():
    digest = hashlib.sha256(b"open-sesame").digest()
    with pytest.raises(ValueError):  # (x) raises in the VM -> spend aborts
        run_chia_program(
            _prog("hashlock"), _solution(digest, b"wrong", b"x"), MAX_COST, 0)


def test_hashes_json_matches_fresh_compile():
    # Drift guard: editing a .clsp without re-running build.py fails here.
    sys.path.insert(0, str(PUZZLES_DIR))
    import build  # puzzles/build.py
    assert build.build_all() == HASHES
