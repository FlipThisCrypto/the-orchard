# SPDX-License-Identifier: Apache-2.0
"""Cross-season replay: a reading seals only into the season it was taken in.

A signed reading body carries no season field, and the hour a record files it
under is chosen by the WRITER. Readings replayed from a season the Tree was
dark for carry genuine signatures and sealed as verified_hours in a season the
Tree never saw — rated fatal by the adversarial review.

ts_ms is inside the device signature. The seal and the verifier now both drop
readings whose ts_ms falls outside the season's declared bounds (with a 5 min
skew grace), using the bounds from the attest record itself so both sides
agree by construction. hour_root stays a commitment to the published batch
as-is; only what an hour is WORTH applies the window.

This check also caught the golden vectors themselves: their readings were
stamped June 2025 inside a season declared May 2026. Fixtures are now
internally consistent.
"""
from __future__ import annotations

from orchard_chia.datalayer import schema, seal

SEED = "01" + "00" * 31
PUB = schema.pubkey_for_seed(SEED)
NODE = "AABBCCDDEEFF0011AABBCCDDEEFF0011"

# Season: 2026-05-31 UTC. In-window noon; out-of-window is 30 days earlier.
WINDOW = schema.window_ms_from_utc("2026-05-31T00:00:00Z", "2026-06-01T00:00:00Z")
IN_TS = 1_780_232_400_000
OUT_TS = IN_TS - 30 * 24 * 3600 * 1000


def _hour(ts0: int, n: int) -> list[dict]:
    return [schema.sign_reading({
        "node_id": NODE, "ts_ms": ts0 + i * 60_000, "block_anchor": "a" * 16,
        "metrics": {"temperature_mc": 20000},
    }, SEED) for i in range(n)]


def test_in_window_readings_count():
    got = schema.verified_hours({13: _hour(IN_TS, 30)}, PUB, window_ms=WINDOW)
    assert got == 1


def test_replayed_readings_from_another_season_do_not():
    """The attack: real signatures, wrong day."""
    got = schema.verified_hours({13: _hour(OUT_TS, 30)}, PUB, window_ms=WINDOW)
    assert got == 0


def test_padding_a_thin_hour_with_replays_fails_the_quorum():
    mixed = _hour(IN_TS, 10) + _hour(OUT_TS, 40)
    assert schema.verified_hours({13: mixed}, PUB, window_ms=WINDOW) == 0


def test_clock_skew_at_the_edge_is_forgiven():
    just_before = WINDOW[0] + 1000     # inside the grace margin
    got = schema.verified_hours({0: _hour(just_before, 30)}, PUB,
                                window_ms=WINDOW)
    assert got == 1


def test_a_reading_with_no_timestamp_cannot_prove_when_it_was():
    rs = _hour(IN_TS, 30)
    stripped = []
    for r in rs:
        r2 = {k: v for k, v in r.items() if k != "ts_ms"}
        stripped.append(r2)
    assert schema.verified_hours({13: stripped}, PUB, window_ms=WINDOW) == 0


def test_no_window_preserves_old_behaviour():
    """Pre-window records verify exactly as written."""
    assert schema.verified_hours({13: _hour(OUT_TS, 30)}, PUB) == 1


def test_the_seal_threads_the_window_through():
    batch = schema.build_readings_batch(node_id=NODE, season=5, hour=13,
                                        readings=_hour(OUT_TS, 30))
    out = seal.seal_from_readings([batch], device_pubkey=PUB, window_ms=WINDOW)
    assert out is not None
    assert out.verified_hours == 0, "replays root, but are worth nothing"


def test_unparseable_bounds_mean_no_window():
    assert schema.window_ms_from_utc("garbage", "alsogarbage") is None
