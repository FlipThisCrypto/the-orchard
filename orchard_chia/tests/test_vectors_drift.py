# SPDX-License-Identifier: Apache-2.0
"""Golden-vectors drift guard.

vectors.json is the byte-pinned cross-language contract firmware verifies
against. If the schema/signing/Merkle code changes but the committed vectors
are not regenerated (or vice versa), cross-language verification breaks
silently. This test fails whenever the committed file diverges from what the
current code produces — forcing a deliberate regeneration:

    python -m orchard_chia.datalayer._gen_vectors
"""
from __future__ import annotations

import json
from pathlib import Path

from orchard_chia.datalayer import _gen_vectors

VPATH = (
    Path(__file__).resolve().parents[1] / "datalayer" / "testdata" / "vectors.json"
)


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def test_generator_is_deterministic():
    # RFC 6979 signing + deterministic Merkle → identical output every run.
    assert _canon(_gen_vectors.build()) == _canon(_gen_vectors.build())


def test_committed_vectors_match_generator():
    committed = json.loads(VPATH.read_text(encoding="utf-8"))
    regenerated = _gen_vectors.build()
    assert _canon(regenerated) == _canon(committed), (
        "vectors.json is stale — regenerate with "
        "`python -m orchard_chia.datalayer._gen_vectors`"
    )
