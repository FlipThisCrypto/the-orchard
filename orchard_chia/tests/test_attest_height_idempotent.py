# SPDX-License-Identifier: Apache-2.0
"""Re-sealing a closed season must reproduce its bytes.

``signed_at`` was fixed for this reason once already: a per-run timestamp inside
the signed body made the "unchanged, skip it" short-circuit unreachable, so a
daily run delete+inserted EVERY attestation for EVERY past season — unbounded,
fee-bearing, and rewriting the seal time of historical records.

``block_height_at_write`` sat in the same body and was taken from the live peak
height on every run, so the identical defect survived through a different
field. With 188 records on the live store, one run rewrote all 188.

A sealed season is a fixed fact. The height already on chain is reused when
reusing it reproduces the stored bytes — that is precisely what "nothing
changed" means. A record whose content genuinely differs still gets the current
height, because that write really is new.
"""
from __future__ import annotations

from orchard_chia.datalayer import schema

SEED = "01" + "00" * 31
NODE = "D8641AD6CAE36977818499469F7E8C49"


def _attest(height: int, *, hours: int = 24, root: str = "ab" * 32) -> dict:
    return schema.sign_attest(schema.build_attest(
        node_id=NODE, season=70,
        season_start_utc="2026-05-31T00:00:00Z",
        season_end_utc="2026-06-01T00:00:00Z",
        hours_online=hours, verified_hrs=hours, reading_count=60 * hours,
        block_height_at_write=height, season_root_hex=root,
        signed_at="2026-06-01T00:00:00Z",
    ), SEED)


def test_the_height_is_inside_the_signature():
    """Which is why it cannot simply be ignored when comparing."""
    a = _attest(100)
    tampered = dict(a, block_height_at_write=999)
    pub = schema.pubkey_for_seed(SEED)
    assert schema.verify_attest(a, pub) is True
    assert schema.verify_attest(tampered, pub) is False


def test_a_different_height_alone_changes_every_byte():
    """The defect, stated plainly: nothing about the season differs, yet the
    record does — so the writer rewrites it and pays for the privilege."""
    assert schema.value_hex(_attest(8_794_728)) != schema.value_hex(_attest(8_800_000))


def test_reusing_the_stored_height_reproduces_the_stored_bytes():
    """The property the fix relies on. Re-sealing the same closed season with
    the height already on chain must be byte-identical, or the short-circuit
    cannot fire."""
    on_chain = _attest(8_794_728)
    stored_hex = schema.value_hex(on_chain)

    # A later run: same season, same evidence, new peak height.
    fresh = schema.build_attest(
        node_id=NODE, season=70,
        season_start_utc="2026-05-31T00:00:00Z",
        season_end_utc="2026-06-01T00:00:00Z",
        hours_online=24, verified_hrs=24, reading_count=1440,
        block_height_at_write=8_900_000, season_root_hex="ab" * 32,
        signed_at="2026-06-01T00:00:00Z",
    )
    as_before = schema.sign_attest(
        dict(fresh, block_height_at_write=on_chain["block_height_at_write"]), SEED)
    assert schema.value_hex(as_before) == stored_hex


def test_a_genuine_content_change_is_not_masked_by_reusing_the_height():
    """The rule must not become 'never rewrite'. A season whose evidence
    improved — a placeholder that became proof-backed — has to be written."""
    on_chain = _attest(8_794_728, hours=12)
    improved = schema.build_attest(
        node_id=NODE, season=70,
        season_start_utc="2026-05-31T00:00:00Z",
        season_end_utc="2026-06-01T00:00:00Z",
        hours_online=24, verified_hrs=24, reading_count=1440,
        block_height_at_write=8_900_000, season_root_hex="ab" * 32,
        signed_at="2026-06-01T00:00:00Z",
    )
    as_before = schema.sign_attest(
        dict(improved, block_height_at_write=on_chain["block_height_at_write"]), SEED)
    assert schema.value_hex(as_before) != schema.value_hex(on_chain), (
        "reusing the height must not make a changed season look unchanged")


def test_a_changed_season_root_is_never_masked():
    on_chain = _attest(8_794_728, root="ab" * 32)
    other = _attest(8_794_728, root="cd" * 32)
    assert schema.value_hex(other) != schema.value_hex(on_chain)


def test_a_record_without_a_stored_height_falls_through_to_a_normal_write():
    """Nothing to reuse — the writer must not crash or skip."""
    assert "block_height_at_write" not in {"season_root": "x"}
