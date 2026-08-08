# SPDX-License-Identifier: Apache-2.0
"""Season sealing must not require running a full node.

``attest`` stamps ``block_height_at_write`` — an anti-backdate anchor — and read
it from one full-node RPC (``peak_height`` on 8555) with no fallback. An
operator running wallet + data_layer, which is the whole requirement for
publishing, could not seal at all: readings landed on chain and the verifier
stopped at "attest missing from store", with a ~200 GB dependency in between.

A SYNCED wallet's height IS the peak, so it is an equivalent anchor. The sync
gate is what makes the substitution sound rather than convenient — an anchor is
a promise about time, and a syncing wallet's height is a guess.
"""
from __future__ import annotations

import pytest

from orchard_chia.wallet.rpc import WalletRpc, WalletRpcError


class _Wallet(WalletRpc):
    """WalletRpc with the transport replaced — no network, no config."""

    def __init__(self, responses):
        super().__init__("127.0.0.1", 9256, "", "")
        self._responses = responses
        self.calls: list[str] = []

    def _post(self, route, body, timeout=60):
        self.calls.append(route)
        return self._responses[route]


def test_a_synced_wallet_supplies_the_height():
    w = _Wallet({
        "get_sync_status": {"synced": True, "syncing": False},
        "get_height_info": {"height": 9122374},
    })
    assert w.synced_peak_height() == 9122374
    assert "get_sync_status" in w.calls, "sync must be checked, not assumed"


def test_a_syncing_wallet_is_refused():
    # Behind the chain -> its height is not the peak. Refuse rather than
    # quietly stamp a weaker anchor.
    w = _Wallet({
        "get_sync_status": {"synced": False, "syncing": True},
        "get_height_info": {"height": 8616588},
    })
    with pytest.raises(WalletRpcError, match="not synced"):
        w.synced_peak_height()


def test_a_wallet_claiming_synced_while_syncing_is_refused():
    w = _Wallet({
        "get_sync_status": {"synced": True, "syncing": True},
        "get_height_info": {"height": 9122374},
    })
    with pytest.raises(WalletRpcError, match="not synced"):
        w.synced_peak_height()


def test_a_zero_or_missing_height_is_refused():
    for info in ({"height": 0}, {}, {"height": None}):
        w = _Wallet({
            "get_sync_status": {"synced": True, "syncing": False},
            "get_height_info": info,
        })
        with pytest.raises(WalletRpcError, match="no usable height"):
            w.synced_peak_height()


def test_the_height_is_never_read_before_sync_is_confirmed():
    """Ordering matters: a height read from an unsynced wallet must not even
    be fetched, let alone stamped."""
    w = _Wallet({
        "get_sync_status": {"synced": False, "syncing": True},
        "get_height_info": {"height": 1},
    })
    with pytest.raises(WalletRpcError):
        w.synced_peak_height()
    assert w.calls == ["get_sync_status"], (
        f"expected to stop after the sync check, got {w.calls}"
    )
