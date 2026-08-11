# SPDX-License-Identifier: Apache-2.0
"""Tests for orchard_chia.payout — calculator + watermark.

Hermetic — no DataLayer, no wallet, no oracle. Reader and orchestrator
get integration coverage when the user runs the actual payout against
their own DataLayer.
"""
from __future__ import annotations

import pytest

from orchard_chia.payout import calculator, watermark
from orchard_chia.payout.reader import _decode_key


# ----------------- calculator -----------------

def _proven(hours):
    """A record that can actually be checked: a real Merkle seal, signatures
    verified, and the hours to match. Bare hours_online is a claim, not
    evidence, and no longer buys anything."""
    return {"hours_online": hours, "verified_hours": hours,
            "seal_source": "readings", "sigs_verified": True}


def test_full_season_full_rate():
    attest = _proven(24)
    # 24/24 * 1.0 = 1.0 $JUICE = 1000 mojos
    assert calculator.juice_mojos_for_attestation(attest, daily_rate=1.0) == 1000


def test_half_season_full_rate():
    attest = _proven(12)
    # 12/24 * 1.0 = 0.5 $JUICE = 500 mojos
    assert calculator.juice_mojos_for_attestation(attest, daily_rate=1.0) == 500


def test_one_hour_full_rate():
    attest = _proven(1)
    # 1/24 * 1.0 = 0.04166... ≈ 0.042 $JUICE = 42 mojos
    assert calculator.juice_mojos_for_attestation(attest, daily_rate=1.0) == 42


def test_zero_hours_zero_payout():
    attest = {"hours_online": 0}
    assert calculator.juice_mojos_for_attestation(attest, daily_rate=1.0) == 0


def test_scaled_daily_rate():
    attest = _proven(24)
    assert calculator.juice_mojos_for_attestation(attest, daily_rate=2.5) == 2500


def test_invalid_hours_raises():
    with pytest.raises(ValueError):
        calculator.juice_mojos_for_attestation(
            _proven(-1), daily_rate=1.0)
    with pytest.raises(ValueError):
        calculator.juice_mojos_for_attestation(
            _proven(25), daily_rate=1.0)


def test_invalid_rate_raises():
    with pytest.raises(ValueError):
        calculator.juice_mojos_for_attestation(
            _proven(12), daily_rate=-1.0)


def test_prefers_verified_hours_when_present():
    # Oracle claims 24 but only 20 hours are publicly verifiable → pay on 20.
    attest = {"hours_online": 24, "verified_hours": 20}
    # 20/24 * 1.0 = 0.8333 $JUICE ≈ 833 mojos
    assert calculator.juice_mojos_for_attestation(attest, daily_rate=1.0) == 833


def test_overclaim_not_rewarded():
    honest = {"hours_online": 24, "verified_hours": 24}
    overclaim = {"hours_online": 24, "verified_hours": 12}
    assert calculator.juice_mojos_for_attestation(honest, daily_rate=1.0) == 1000
    assert calculator.juice_mojos_for_attestation(overclaim, daily_rate=1.0) == 500


def test_a_bare_hours_online_claim_is_not_paid():
    """Reversed policy, and the reason is measurable.

    This used to pay the full amount: a record with no verified_hours fell back
    to the oracle's own hours_online. Measured against the live store on
    2026-08-10, that rule would have paid all 188 attestations — 170.033 $JUICE
    — with not one published reading behind any of them.

    hours_online is the oracle's account of itself. It is reported, never paid.
    """
    attest = {"hours_online": 24}
    assert calculator.juice_mojos_for_attestation(attest, daily_rate=1.0) == 0
    hours, basis = calculator.paid_hours(attest)
    assert hours == 0 and basis == "unproven (no verified_hours field)"


def test_the_old_amounts_are_still_reachable_for_reconciliation():
    """Anyone reconciling historical figures can ask for them by name."""
    attest = {"hours_online": 24}
    assert calculator.juice_mojos_for_attestation(
        attest, daily_rate=1.0, pay_unproven=True) == 1000


def test_prefer_verified_can_be_disabled():
    attest = {"hours_online": 24, "verified_hours": 12}
    assert calculator.juice_mojos_for_attestation(
        attest, daily_rate=1.0, prefer_verified=False) == 1000


def test_verified_hours_out_of_range_raises():
    with pytest.raises(ValueError, match="verified_hours"):
        calculator.juice_mojos_for_attestation(
            {"hours_online": 10, "verified_hours": 99}, daily_rate=1.0)


def test_paid_hours_prefers_verified():
    # Legacy record (no seal_source): same AMOUNT as always; the label now says
    # the basis was never declared, so the payer can't print "verified" for a
    # record a third-party verifier can only call unproven.
    hours, basis = calculator.paid_hours({"hours_online": 24, "verified_hours": 12})
    assert hours == 12
    assert basis == "verified_hours (basis undeclared)"
    # A bare claim with no verified number is no longer a basis for payment.
    assert calculator.paid_hours({"hours_online": 24}) == (
        0, "unproven (no verified_hours field)")


def test_hours_cell_annotates_overclaim():
    from orchard_chia.payout.main import _hours_cell
    # Paid on verified 12 while oracle claimed 24 → surface the claim.
    assert _hours_cell(
        {"hours": 12, "hours_basis": "verified_hours", "claimed_hours": 24}
    ) == "12 (claim 24)"
    # Honest (equal) → no annotation.
    assert _hours_cell(
        {"hours": 24, "hours_basis": "verified_hours", "claimed_hours": 24}
    ) == "24"
    # Fallback basis → plain.
    assert _hours_cell({"hours": 24, "hours_basis": "hours_online"}) == "24"


def test_aggregate_by_wallet_sums_correctly():
    rows = [
        {"wallet_address": "xch1a", "mojos": 1000},
        {"wallet_address": "xch1b", "mojos": 500},
        {"wallet_address": "xch1a", "mojos": 250},
    ]
    out = calculator.aggregate_by_wallet(rows)
    assert out == {"xch1a": 1250, "xch1b": 500}


def test_aggregate_skips_empty_wallet():
    rows = [
        {"wallet_address": "",     "mojos": 1000},
        {"wallet_address": "xch1", "mojos":  500},
        {"wallet_address": None,   "mojos":  250},
    ]
    out = calculator.aggregate_by_wallet(rows)
    assert out == {"xch1": 500}


def test_mojos_to_juice():
    assert calculator.mojos_to_juice(1000) == 1.0
    assert calculator.mojos_to_juice(42) == 0.042
    assert calculator.mojos_to_juice(0) == 0.0


# ----------------- watermark -----------------

def test_watermark_records_and_reads(tmp_path):
    db = tmp_path / "wm.db"
    with watermark.Watermark(db) as wm:
        assert wm.is_paid("AAA", 1) is False
        wm.record_payment(
            node_id="AAA", season=1, wallet_address="xch1a",
            paid_mojos=42, tx_id="0xdead",
        )
        assert wm.is_paid("AAA", 1) is True
        assert wm.get_paid_amount("AAA", 1) == 42
        assert wm.total_paid_to_wallet("xch1a") == 42


def test_watermark_double_record_is_idempotent(tmp_path):
    db = tmp_path / "wm.db"
    with watermark.Watermark(db) as wm:
        wm.record_payment(
            node_id="AAA", season=1, wallet_address="xch1a",
            paid_mojos=42, tx_id="0xdead",
        )
        wm.record_payment(
            node_id="AAA", season=1, wallet_address="xch1a",
            paid_mojos=99, tx_id="0xbeef",
        )
        # INSERT OR IGNORE — original 42 stands.
        assert wm.get_paid_amount("AAA", 1) == 42
        assert wm.total_paid_to_wallet("xch1a") == 42


def test_watermark_per_wallet_totals(tmp_path):
    db = tmp_path / "wm.db"
    with watermark.Watermark(db) as wm:
        wm.record_payment(node_id="AAA", season=1,
                          wallet_address="xch1a", paid_mojos=100)
        wm.record_payment(node_id="AAA", season=2,
                          wallet_address="xch1a", paid_mojos=200)
        wm.record_payment(node_id="BBB", season=1,
                          wallet_address="xch1b", paid_mojos=300)
        assert wm.total_paid_to_wallet("xch1a") == 300
        assert wm.total_paid_to_wallet("xch1b") == 300
        assert wm.total_paid_to_wallet("xch1c") == 0


def test_watermark_persists_across_open(tmp_path):
    db = tmp_path / "wm.db"
    with watermark.Watermark(db) as wm:
        wm.record_payment(node_id="AAA", season=1,
                          wallet_address="xch1a", paid_mojos=42)
    with watermark.Watermark(db) as wm2:
        assert wm2.is_paid("AAA", 1)
        assert wm2.get_paid_amount("AAA", 1) == 42


def test_watermark_set_tx_updates_provisional_row(tmp_path):
    """M3: a provisional (tx_id=None) mark made before broadcasting is
    later confirmed with the real tx_id via set_tx."""
    db = tmp_path / "wm.db"
    with watermark.Watermark(db) as wm:
        wm.record_payment(node_id="AAA", season=1,
                          wallet_address="xch1a", paid_mojos=42, tx_id=None)
        assert wm.is_paid("AAA", 1) is True          # marked before send
        wm.set_tx("AAA", 1, "0xconfirmed")
        assert wm.all_paid()[0]["tx_id"] == "0xconfirmed"


def test_wallet_rpc_refuses_non_loopback_host():
    """L6: verify=False must not be used against a non-loopback host."""
    from orchard_chia.wallet.rpc import WalletRpc, WalletRpcError
    rpc = WalletRpc(host="10.0.0.9", port=9256, cert_path="", key_path="")
    with pytest.raises(WalletRpcError, match="non-loopback"):
        rpc.get_wallets()


# ----------------- reader._decode_key -----------------

def test_decode_key_round_trip():
    node = "5B9BB022649FA93D4091DA4BA40714B9"
    season = 42
    raw = f"attest:{node}:{season:08d}".encode("utf-8").hex()
    decoded = _decode_key(raw)
    assert decoded == (node, season)


def test_decode_key_rejects_non_orchard():
    other_key = "hello".encode("utf-8").hex()
    assert _decode_key(other_key) is None


def test_decode_key_rejects_invalid_hex():
    assert _decode_key("not-hex") is None


def test_decode_key_rejects_malformed_attest():
    bad = "attest:nope".encode("utf-8").hex()
    assert _decode_key(bad) is None
