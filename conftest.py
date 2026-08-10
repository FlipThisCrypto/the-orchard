# SPDX-License-Identifier: Apache-2.0
"""Test isolation from production. Applies to every test in the repo.

WHY
===

Three ``attest:`` records for ``5B9BB022649FA93D4091DA4BA40714B9`` — this
repo's own fixture node_id — sit on the mainnet DataLayer store, permanently.
The most plausible route is a test process that reached the production oracle
or the production store during early development.

Iteration 4 added a gate that refuses those ids by name. That protects against
the three ids already known. It does nothing about the fourth: a new fixture
constant, added next month by someone with no reason to suspect a test can
touch mainnet, would pass every check.

The real defect is that nothing prevented it. Tests ran with unrestricted
sockets, against a repo whose ``config.yaml`` holds a live store_id and working
mTLS certificate paths. Every ingredient was present and only habit kept them
apart.

WHAT THIS ENFORCES
==================

  1. No test reaches off-box, and none reaches the local Chia daemons —
     the wallet (which spends) or data_layer (which writes permanently).
     Ordinary local sockets stay open: FastAPI's TestClient needs them, and a
     guard that fails honest work is a guard someone switches off.
  2. No test resolves the operator's real ``config.yaml``, which holds a live
     store_id, a wallet fingerprint and mTLS certificate paths.

Opting out is possible and deliberate: mark a test ``@pytest.mark.network``.
That mark is the entire audit trail — grep for it and you have the complete
list of tests permitted to touch the outside world.
"""
from __future__ import annotations

import socket

import pytest

# Kept for tests to assert against; the two controls above are what enforce
# isolation. An earlier version also failed any test whose source merely NAMED
# one of these, which flagged the tests written to prove the host is blocked —
# a guard that fails correct work is a guard someone switches off.
PRODUCTION_MARKERS = (
    "d0bb705e",                        # the live DataLayer store_id prefix
    "oracle.theorchard.network",       # the production oracle
)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: test may open real sockets. Use sparingly — this is the "
        "audit trail for everything allowed to touch the outside world.")


class BlockedNetworkAccess(RuntimeError):
    """A test tried to open a socket without asking for permission."""


# Loopback is NOT blanket-allowed. The wallet and DataLayer daemons listen on
# localhost, and they are the two things in reach that can spend money and
# write permanently — "it's only localhost" is precisely how a test reaches
# mainnet. These ports stay closed to tests even though the interface is local.
BLOCKED_LOCAL_PORTS = frozenset({
    8562,   # data_layer RPC — permanent writes
    9256,   # wallet RPC — spends
    8555,   # full node RPC
    55400,  # chia daemon
})
# Note what is deliberately NOT here: 8000, where a local oracle runs. It is
# also where the dashboard tests stand up their own server, and the production
# oracle is off-box and already blocked by the loopback rule. Blocking it would
# have bought nothing and broken five honest tests — and a guard that fails
# honest work is a guard people switch off.


def _is_loopback(host: object) -> bool:
    if not isinstance(host, str):
        return False
    return host in ("127.0.0.1", "::1", "localhost", "") or host.startswith("127.")


def _forbidden(address) -> str | None:
    """Why this address is off-limits, or None if a test may reach it."""
    if not isinstance(address, tuple) or len(address) < 2:
        return None                      # unix sockets, socketpair, etc.
    host, port = address[0], address[1]
    if not _is_loopback(host):
        return f"{host!r} is not loopback"
    if isinstance(port, int) and port in BLOCKED_LOCAL_PORTS:
        return (f"localhost:{port} is a Chia/oracle daemon — local does not "
                f"mean safe, it is the shortest path to a permanent write")
    return None


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    """Refuse the connections that can reach production, not all sockets.

    A blanket ban is wrong here: FastAPI's TestClient and anyio's portals use
    real local sockets, and failing those teaches people to disable the guard.
    What matters is that a test cannot reach anything off-box, and cannot reach
    the local daemons that write to the chain or hold keys.
    """
    if request.node.get_closest_marker("network"):
        return

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create = socket.create_connection

    def guard(fn):
        def wrapper(self_or_addr, *args, **kwargs):
            addr = args[0] if args else self_or_addr
            why = _forbidden(addr)
            if why:
                raise BlockedNetworkAccess(
                    f"test tried to connect to {addr!r}: {why}. Three fixture "
                    f"records are on mainnet permanently because something in "
                    f"this repo could do this. Add a fake, or mark the test "
                    f"@pytest.mark.network if it genuinely needs the outside "
                    f"world.")
            return fn(self_or_addr, *args, **kwargs)
        return wrapper

    monkeypatch.setattr(socket.socket, "connect", guard(real_connect), raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", guard(real_connect_ex), raising=False)
    monkeypatch.setattr(
        socket, "create_connection",
        lambda address, *a, **k: (
            _raise_blocked(address) if _forbidden(address)
            else real_create(address, *a, **k)),
        raising=False)


def _raise_blocked(address):
    raise BlockedNetworkAccess(
        f"test tried to connect to {address!r} — blocked. See conftest.py.")


@pytest.fixture(autouse=True)
def _sandbox_chia_config(tmp_path_factory, monkeypatch):
    """Point config resolution at a sandbox, never the operator's real file.

    ``orchard_chia/config.yaml`` is gitignored and holds a live store_id, a
    wallet fingerprint and certificate paths. A test that calls ``config.load()``
    without arranging its own file would otherwise read all of it — and, having
    read it, would be one RPC call from writing to the real store.
    """
    sandbox = tmp_path_factory.mktemp("chia-config-sandbox") / "config.yaml"
    monkeypatch.setenv("ORCHARD_CHIA_CONFIG", str(sandbox))
    # config.CONFIG_PATH is a module constant computed at import time with no
    # environment override, so setting the variable above is NOT sufficient —
    # the first version of this fixture set only the env var and isolated
    # nothing at all. Patch the constant the loader actually reads.
    from orchard_chia.datalayer import config as _dl_config
    monkeypatch.setattr(_dl_config, "CONFIG_PATH", sandbox, raising=False)
