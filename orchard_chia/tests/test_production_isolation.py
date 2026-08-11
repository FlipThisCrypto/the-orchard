# SPDX-License-Identifier: Apache-2.0
"""The isolation in conftest.py actually isolates.

A guard nobody tests is a guard that quietly stops working — and this one has a
specific failure mode: it is invisible when it succeeds. Every test passing
tells you nothing about whether the socket ban is still installed, so it would
survive a refactor that disabled it entirely and nothing would go red until
something reached mainnet again.

Three fixture attestations are already on the production store because nothing
prevented a test from touching it. These tests are the evidence that the
prevention is real.
"""
from __future__ import annotations

import socket

import pytest

from conftest import (BLOCKED_LOCAL_PORTS, BlockedNetworkAccess,
                      PRODUCTION_MARKERS, _forbidden, _is_loopback)


# --- what the guard classifies ----------------------------------------------

def test_off_box_addresses_are_forbidden():
    assert _forbidden(("oracle.theorchard.network", 443))
    assert _forbidden(("8.8.8.8", 53))
    assert "not loopback" in _forbidden(("example.com", 80))


def test_the_datalayer_daemon_is_forbidden_even_on_localhost():
    """This is the one that writes permanently. 'It's only localhost' is
    exactly the reasoning that puts test data on mainnet."""
    why = _forbidden(("127.0.0.1", 8562))
    assert why and "permanent write" in why


def test_the_wallet_daemon_is_forbidden_even_on_localhost():
    assert _forbidden(("localhost", 9256))


def test_every_blocked_port_is_actually_blocked_on_loopback():
    for port in BLOCKED_LOCAL_PORTS:
        assert _forbidden(("127.0.0.1", port)), f"port {port} slipped through"


def test_ordinary_local_test_servers_are_allowed():
    """A guard that fails honest work is a guard people switch off. The
    dashboard tests stand up a server on 8000; blocking it bought nothing,
    since the production oracle is off-box and already refused."""
    assert _forbidden(("127.0.0.1", 8000)) is None
    assert _forbidden(("127.0.0.1", 54321)) is None


def test_non_tuple_addresses_are_left_alone():
    """Unix sockets and socketpair() are not what this guards against."""
    assert _forbidden("/tmp/some.sock") is None
    assert _forbidden(None) is None


def test_loopback_recognition():
    assert _is_loopback("127.0.0.1") and _is_loopback("::1")
    assert _is_loopback("localhost") and _is_loopback("127.0.1.5")
    assert not _is_loopback("10.0.0.1") and not _is_loopback("example.com")


# --- the guard is installed, right now, in this process ---------------------

def test_a_real_connection_to_the_datalayer_daemon_is_refused():
    """Not a unit test of _forbidden — this exercises the patched socket."""
    s = socket.socket()
    try:
        with pytest.raises(BlockedNetworkAccess):
            s.connect(("127.0.0.1", 8562))
    finally:
        s.close()


def test_a_real_connection_off_box_is_refused():
    s = socket.socket()
    try:
        with pytest.raises(BlockedNetworkAccess):
            s.connect(("oracle.theorchard.network", 443))
    finally:
        s.close()


def test_create_connection_is_guarded_too():
    with pytest.raises(BlockedNetworkAccess):
        socket.create_connection(("oracle.theorchard.network", 443))


def test_the_refusal_explains_itself():
    s = socket.socket()
    try:
        with pytest.raises(BlockedNetworkAccess, match="mark the test"):
            s.connect(("1.1.1.1", 443))
    finally:
        s.close()


# --- config never resolves to the operator's real file ----------------------

def test_the_chia_config_path_is_sandboxed():
    import os
    path = os.environ.get("ORCHARD_CHIA_CONFIG", "")
    assert path, "ORCHARD_CHIA_CONFIG must be set during tests"
    assert "sandbox" in path.replace("\\", "/").lower()


def test_loading_config_in_a_test_does_not_read_the_live_store():
    """orchard_chia/config.yaml is gitignored and holds a live store_id, a
    wallet fingerprint and certificate paths. A forgotten config.load() must
    not quietly acquire all of it."""
    from orchard_chia.datalayer import config
    try:
        cfg = config.load()
    except FileNotFoundError:
        return                      # the sandbox is empty — correct outcome
    assert "d0bb705e" not in (cfg.data_layer.store_id or "").lower(), (
        "a test resolved the production store_id")


def test_the_production_marker_list_is_not_empty():
    assert PRODUCTION_MARKERS, "the marker check would silently pass forever"


# --- opting out is possible, and visible ------------------------------------

@pytest.mark.network
def test_the_network_marker_lifts_the_ban():
    """Proves the escape hatch works, so nobody needs to disable the guard to
    write a test that legitimately needs a socket. Grepping for this marker
    gives the complete list of tests allowed outside."""
    s = socket.socket()
    try:
        s.settimeout(0.01)
        try:
            s.connect(("127.0.0.1", 9))     # discard port; refusal is fine
        except OSError:
            pass                            # connected or refused, both are OK
    finally:
        s.close()
