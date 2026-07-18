# SPDX-License-Identifier: Apache-2.0
"""Smoke tests for Orchard View.

We don't actually open a serial port or talk to a real oracle —
both modules are monkeypatched in the fixture so the tests run
hermetically (and quickly).
"""
from __future__ import annotations


import pytest

from dashboard.app import config as dash_config
from dashboard.app import oracle_client, tree_serial
from dashboard.app.main import create_app


@pytest.fixture()
def client(monkeypatch):
    dash_config.reset_settings_for_tests()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    dash_config.reset_settings_for_tests()


@pytest.fixture()
def public_client(monkeypatch):
    """Same as `client`, but with public_mode=True so operator-only
    routes (provision, serial, oracle/register) get gated to 404."""
    monkeypatch.setenv("ORCHARD_VIEW_PUBLIC_MODE", "1")
    dash_config.reset_settings_for_tests()
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c
    dash_config.reset_settings_for_tests()


def test_index_with_oracle_down(client, monkeypatch):
    def boom():
        raise oracle_client.OracleError("connection refused")
    monkeypatch.setattr(oracle_client, "root", boom)
    monkeypatch.setattr(oracle_client, "list_nodes", lambda: [])
    r = client.get("/")
    assert r.status_code == 200
    assert b"Oracle unreachable" in r.data


def test_index_with_oracle_up_no_nodes(client, monkeypatch):
    monkeypatch.setattr(oracle_client, "root", lambda: {"version": "0.1.0", "current_season": 1, "now_utc": "2026-05-27T20:00:00+00:00"})
    monkeypatch.setattr(oracle_client, "list_nodes", lambda: [])
    r = client.get("/")
    assert r.status_code == 200
    assert b"Plant your first Tree" in r.data


def test_provision_page(client):
    r = client.get("/provision")
    assert r.status_code == 200
    assert b"Plant a Tree" in r.data
    assert b"Identify Tree" in r.data


def test_tree_page_unknown_node(client, monkeypatch):
    monkeypatch.setattr(oracle_client, "get_node", lambda node_id: None)
    r = client.get("/tree/ABCDEF0123456789ABCDEF0123456789")
    assert r.status_code == 404


def test_tree_page_known_node(client, monkeypatch):
    monkeypatch.setattr(oracle_client, "get_node", lambda node_id: {
        "node_id": node_id, "label": "test-tree", "fw_version": "0.1.0",
        "registered_at": "2026-05-27T20:00:00+00:00",
        "last_seen_at": None, "last_reading_at": None, "wallet_address": None,
    })
    r = client.get("/tree/ABCDEF0123456789ABCDEF0123456789")
    assert r.status_code == 200
    assert b"test-tree" in r.data


def test_api_oracle_status(client, monkeypatch):
    monkeypatch.setattr(oracle_client, "root", lambda: {"service": "the-orchard-oracle", "current_season": 7})
    r = client.get("/api/oracle/status")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["oracle"]["current_season"] == 7


def test_api_serial_ports(client, monkeypatch):
    monkeypatch.setattr(tree_serial, "list_ports", lambda: [
        tree_serial.PortInfo(device="COM4", description="USB Serial", hwid="VID:PID"),
    ])
    r = client.get("/api/serial/ports")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["ports"][0]["device"] == "COM4"


def test_api_serial_identify_missing_port(client):
    r = client.post("/api/serial/identify", json={})
    assert r.status_code == 400


def test_api_serial_identify_happy(client, monkeypatch):
    """Legacy firmware (<0.4.0): get_hw_info returns None. The route
    must still return 200 with hw_info=null — graceful fallback."""
    monkeypatch.setattr(tree_serial, "ping", lambda port: True)
    monkeypatch.setattr(tree_serial, "get_node_id", lambda port: "5B9BB022649FA93D4091DA4BA40714B9")
    monkeypatch.setattr(tree_serial, "get_signing_key", lambda port: "AA" * 32)
    monkeypatch.setattr(tree_serial, "get_status", lambda port: {"fw": "0.1.0", "wifi": "unconfigured", "oracle": ""})
    monkeypatch.setattr(tree_serial, "get_hw_info", lambda port: None)
    r = client.post("/api/serial/identify", json={"port": "COM4"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["node_id"] == "5B9BB022649FA93D4091DA4BA40714B9"
    assert body["status"]["fw"] == "0.1.0"
    assert body["hw_info"] is None


def test_api_serial_identify_includes_hw_info_when_present(client, monkeypatch):
    """Phase 9.0: when the Tree runs firmware 0.4.0+, /api/serial/identify
    surfaces the full board + sensor fingerprint to the wizard. The
    wizard renders this as the new "board" / "chip" / sensor-chip
    fields under the identify card."""
    hw_payload = {
        "fw": "0.4.0",
        "chip": "ESP32-S3",
        "board": "freenove-s3",
        "node_id": "5B9BB022649FA93D4091DA4BA40714B9",
        "sensors": [
            {"name": "bme280", "bus": "i2c",    "addr": 0x76, "active": True},
            {"name": "mq135",  "bus": "analog",                "active": True},
            {"name": "gps",    "bus": "uart",                  "active": False},
        ],
    }
    monkeypatch.setattr(tree_serial, "ping", lambda port: True)
    monkeypatch.setattr(tree_serial, "get_node_id", lambda port: hw_payload["node_id"])
    monkeypatch.setattr(tree_serial, "get_signing_key", lambda port: "AA" * 32)
    monkeypatch.setattr(tree_serial, "get_status",
                        lambda port: {"fw": "0.4.0", "wifi": "connected", "oracle": ""})
    monkeypatch.setattr(tree_serial, "get_hw_info", lambda port: dict(hw_payload))
    r = client.post("/api/serial/identify", json={"port": "COM4"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["hw_info"]["board"] == "freenove-s3"
    assert body["hw_info"]["chip"]  == "ESP32-S3"
    assert len(body["hw_info"]["sensors"]) == 3
    bme = next(s for s in body["hw_info"]["sensors"] if s["name"] == "bme280")
    assert bme["active"] is True
    assert bme["addr"]   == 0x76


def test_get_hw_info_returns_none_on_legacy_firmware(monkeypatch):
    """Direct test of the tree_serial helper: a Tree running pre-0.4.0
    firmware responds with 'ERR unknown' to HW_INFO. The helper must
    map that to None (NOT raise) so the wizard's identify flow keeps
    working against the field of existing already-deployed Trees."""
    monkeypatch.setattr(tree_serial, "_send_and_read_line",
                        lambda port, cmd: "ERR unknown")
    assert tree_serial.get_hw_info("COM4") is None


def test_get_hw_info_parses_json_on_modern_firmware(monkeypatch):
    """Direct test of the tree_serial helper: 0.4.0+ firmware emits
    `OK {json}` and the helper returns the parsed dict."""
    import json as _json
    sample = {
        "fw": "0.4.0", "chip": "ESP32-S3", "board": "freenove-s3",
        "node_id": "AB" * 16,
        "sensors": [{"name": "bme280", "bus": "i2c", "addr": 118, "active": True}],
    }
    monkeypatch.setattr(tree_serial, "_send_and_read_line",
                        lambda port, cmd: "OK " + _json.dumps(sample))
    out = tree_serial.get_hw_info("COM4")
    assert out is not None
    assert out["board"] == "freenove-s3"
    assert out["sensors"][0]["name"] == "bme280"


def test_get_hw_info_raises_on_garbage_response(monkeypatch):
    """A response that's neither 'ERR unknown' nor 'OK <valid json>' is
    a real problem (corrupted serial line, firmware bug, etc.); we
    surface it loudly instead of pretending it's a legacy fw response."""
    monkeypatch.setattr(tree_serial, "_send_and_read_line",
                        lambda port, cmd: "OK {garbage")
    with pytest.raises(tree_serial.TreeError):
        tree_serial.get_hw_info("COM4")


def test_api_oracle_register_passthrough(client, monkeypatch):
    monkeypatch.setattr(oracle_client, "register_node", lambda **kw: {"node_id": kw["node_id"], "new": True})
    r = client.post("/api/oracle/register", json={
        "node_id": "0123456789ABCDEF0123456789ABCDEF",
        "signing_key_hex": "AA" * 32,
        "label": "tree-A",
    })
    assert r.status_code == 200
    body = r.get_json()
    assert body["register"]["new"] is True


def test_api_oracle_register_forwards_authorization_header(client, monkeypatch):
    """The wizard's authFetch attaches Authorization: Bearer <session>.
    The Flask proxy must forward it so the oracle's require_wallet_session
    gate sees the token. Without this, every wizard-driven /register
    would 401 against a hardened oracle."""
    captured = {}

    def fake_register(**kw):
        captured.update(kw)
        return {"node_id": kw["node_id"], "new": True}

    monkeypatch.setattr(oracle_client, "register_node", fake_register)

    r = client.post(
        "/api/oracle/register",
        json={
            "node_id": "0123456789ABCDEF0123456789ABCDEF",
            "signing_key_hex": "AA" * 32,
        },
        headers={"Authorization": "Bearer eyJhbGc.fake.token"},
    )
    assert r.status_code == 200
    assert captured.get("authorization") == "Bearer eyJhbGc.fake.token"


def test_api_oracle_register_no_auth_header_passes_none(client, monkeypatch):
    """If no Authorization header is sent, the proxy passes None down —
    the oracle then applies its own gate (legacy fallback or 401).
    Closes the bug where a missing header would have been passed as a
    blank string and confused the downstream check."""
    captured = {}

    def fake_register(**kw):
        captured.update(kw)
        return {"node_id": kw["node_id"], "new": True}

    monkeypatch.setattr(oracle_client, "register_node", fake_register)

    r = client.post(
        "/api/oracle/register",
        json={
            "node_id": "0123456789ABCDEF0123456789ABCDEF",
            "signing_key_hex": "AA" * 32,
        },
    )
    assert r.status_code == 200
    assert captured.get("authorization") is None


def test_api_tree_latest_unknown(client, monkeypatch):
    monkeypatch.setattr(oracle_client, "get_node", lambda node_id: None)
    r = client.get("/api/tree/ABCDEF01/latest")
    assert r.status_code == 404


# ---------------- public mode ----------------

def test_public_mode_hides_plant_a_tree_in_nav(public_client, monkeypatch):
    """The nav link disappears so visitors don't see provisioning."""
    monkeypatch.setattr(oracle_client, "root", lambda: {"version": "0.1.0", "current_season": 1, "now_utc": "2026-05-27T20:00:00+00:00"})
    monkeypatch.setattr(oracle_client, "list_nodes", lambda: [])
    r = public_client.get("/")
    assert r.status_code == 200
    # The "Plant a Tree" CTA button is rendered inside the page body
    # when there are no nodes — it's an empty-state prompt — but the
    # nav-bar link is conditional. We check that the nav link doesn't
    # appear by looking for it inside the <nav> only. Simplest proxy:
    # the page rendered, but it should not contain the provision URL
    # twice (button + nav). In public mode the body's empty-state
    # button is still there but the nav link is suppressed.
    assert r.data.count(b'href="/provision"') <= 1


def test_public_mode_blocks_provision_page(public_client):
    r = public_client.get("/provision")
    assert r.status_code == 404


def test_public_mode_blocks_oracle_register(public_client):
    r = public_client.post("/api/oracle/register", json={
        "node_id": "0123456789ABCDEF0123456789ABCDEF",
        "signing_key_hex": "AA" * 32,
    })
    assert r.status_code == 404


def test_public_mode_blocks_serial_ports(public_client):
    r = public_client.get("/api/serial/ports")
    assert r.status_code == 404


def test_public_mode_blocks_serial_identify(public_client):
    r = public_client.post("/api/serial/identify", json={"port": "COM4"})
    assert r.status_code == 404


def test_public_mode_blocks_all_serial_routes(public_client):
    for path in ["/api/serial/wifi", "/api/serial/oracle", "/api/serial/sample"]:
        r = public_client.post(path, json={"port": "COM4"})
        assert r.status_code == 404, f"expected {path} to 404 in public mode"


def test_public_mode_still_serves_index_and_tree(public_client, monkeypatch):
    """Read-only endpoints must keep working — they're the whole point
    of public mode."""
    monkeypatch.setattr(oracle_client, "root", lambda: {"version": "0.1.0", "current_season": 1, "now_utc": "2026-05-27T20:00:00+00:00"})
    monkeypatch.setattr(oracle_client, "list_nodes", lambda: [])
    r = public_client.get("/")
    assert r.status_code == 200

    monkeypatch.setattr(oracle_client, "get_node", lambda node_id: {
        "node_id": node_id, "label": "demo-tree", "fw_version": "0.1.0",
        "registered_at": "2026-05-27T20:00:00+00:00",
        "last_seen_at": None, "last_reading_at": None, "wallet_address": None,
    })
    r = public_client.get("/tree/ABCDEF0123456789ABCDEF0123456789")
    assert r.status_code == 200


def test_public_mode_oracle_status_still_works(public_client, monkeypatch):
    monkeypatch.setattr(oracle_client, "root", lambda: {"current_season": 7})
    r = public_client.get("/api/oracle/status")
    assert r.status_code == 200
    assert r.get_json()["oracle"]["current_season"] == 7


# ----- public mode home-page pivot (Phase 6.6 #52) ------------------

_FAKE_STATS = {
    "trees_registered":   42,
    "trees_active_24h":   17,
    "readings_total":     123456,
    "readings_last_24h":  4321,
    "attestations_total": 8,
    "current_season":     11,
    "as_of_utc":          "2026-06-01T12:00:00+00:00",
}


def test_public_mode_home_renders_network_stats_card(public_client, monkeypatch):
    """Public-mode home page must show the aggregate Network card,
    NOT the per-Tree table. The card pulls from oracle /network/stats
    via oracle_client.network_stats()."""
    monkeypatch.setattr(oracle_client, "root",
                        lambda: {"version": "0.1.0", "current_season": 11,
                                 "now_utc": "2026-06-01T12:00:00+00:00"})
    monkeypatch.setattr(oracle_client, "network_stats", lambda: dict(_FAKE_STATS))

    # list_nodes MUST NOT be called in public mode — wire it to fail
    # if anyone accidentally re-enables it. Catches a regression where
    # the route would leak per-Tree info under the public surface.
    def _no_list_nodes():
        raise AssertionError("list_nodes called in public mode")
    monkeypatch.setattr(oracle_client, "list_nodes", _no_list_nodes)

    r = public_client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "The Network" in body
    assert "Trees online" in body
    assert "Attestations on chain" in body
    # Big numbers from the stats payload must surface.
    assert "17" in body     # trees_active_24h
    assert "4,321" in body  # readings_last_24h with thousands separator
    # The per-Tree table heading must NOT appear in public mode.
    assert "Registered Trees" not in body
    assert "Plant your first Tree" not in body


def test_public_mode_home_graceful_when_oracle_down(public_client, monkeypatch):
    """If the oracle's down, public-mode home should render the
    'unavailable' state cleanly, not 500."""
    def boom():
        raise oracle_client.OracleError("connection refused")
    monkeypatch.setattr(oracle_client, "root", boom)
    monkeypatch.setattr(oracle_client, "network_stats", boom)
    r = public_client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Oracle unreachable" in body


def test_private_mode_home_still_shows_per_tree_table(client, monkeypatch):
    """The private-mode home must continue to show the per-Tree table
    — public-mode pivot must NOT change operator UX."""
    monkeypatch.setattr(oracle_client, "root",
                        lambda: {"version": "0.1.0", "current_season": 1,
                                 "now_utc": "2026-06-01T12:00:00+00:00"})
    monkeypatch.setattr(oracle_client, "list_nodes", lambda: [
        {"node_id": "AB" * 16, "label": "backyard-1",
         "fw_version": "0.3.0", "last_reading_at": "2026-06-01T11:55:00+00:00"},
    ])
    # network_stats MUST NOT be called in private mode.
    def _no_network_stats():
        raise AssertionError("network_stats called in private mode")
    monkeypatch.setattr(oracle_client, "network_stats", _no_network_stats)

    r = client.get("/")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "Registered Trees" in body
    assert "backyard-1" in body
    assert "The Network" not in body


# ----- public mode response scrubbing (U1 + U2) ---------------------

_DOXX_WALLET = "xch1m3rvtj86wzzfjyk5mc7wzpr7h4zkaknm4wte7kg6afleu4f2tfxsr7nk3n"
_PRECISE_LAT = 38.054321  # 6 decimals — ~10 cm precision
_PRECISE_LON = -85.527834


def _node_with_secrets(node_id: str) -> dict:
    return {
        "node_id": node_id,
        "label": "backyard-1",
        "fw_version": "0.1.0",
        "wallet_address": _DOXX_WALLET,
        "registered_at": "2026-05-27T20:00:00+00:00",
        "last_seen_at": "2026-05-30T20:00:00+00:00",
        "last_reading_at": "2026-05-30T20:00:00+00:00",
    }


def _reading_with_gps() -> dict:
    return {
        "id": 1,
        "node_id": "ABCDEF0123456789ABCDEF0123456789",
        "received_at": "2026-05-30T20:00:00+00:00",
        "tree_ts_ms": 1234567,
        "fw_version": "0.1.0",
        "gps_lat": _PRECISE_LAT,
        "gps_lon": _PRECISE_LON,
        "gps_fix": True,
        "payload": {
            "sensors": {
                "mq135": {"adc_raw": 1234},
                "gps":   {"lat": _PRECISE_LAT, "lon": _PRECISE_LON, "fix": True},
            },
        },
    }


def test_public_mode_strips_wallet_address(public_client, monkeypatch):
    monkeypatch.setattr(oracle_client, "get_node",
                        lambda nid: _node_with_secrets(nid))
    monkeypatch.setattr(oracle_client, "list_readings",
                        lambda nid, limit: [_reading_with_gps()])
    monkeypatch.setattr(oracle_client, "root", lambda: {"current_season": 1})
    monkeypatch.setattr(oracle_client, "get_uptime",
                        lambda nid, season: {"hours": 1})

    r = public_client.get("/api/tree/ABCDEF0123456789ABCDEF0123456789/latest")
    assert r.status_code == 200
    body = r.get_json()
    # node must not include wallet_address.
    assert "wallet_address" not in body["node"]
    # raw payload of /latest must not include the literal address anywhere.
    assert _DOXX_WALLET not in r.get_data(as_text=True)


def test_public_mode_coarsens_gps_in_readings(public_client, monkeypatch):
    monkeypatch.setattr(oracle_client, "get_node",
                        lambda nid: _node_with_secrets(nid))
    monkeypatch.setattr(oracle_client, "list_readings",
                        lambda nid, limit: [_reading_with_gps()])
    monkeypatch.setattr(oracle_client, "root", lambda: {"current_season": 1})
    monkeypatch.setattr(oracle_client, "get_uptime",
                        lambda nid, season: None)

    r = public_client.get("/api/tree/ABCDEF0123456789ABCDEF0123456789/latest")
    assert r.status_code == 200
    body = r.get_json()
    reading = body["readings"][0]
    # Top-level GPS coarsened to 3 decimals (~111m).
    assert reading["gps_lat"] == round(_PRECISE_LAT, 3)
    assert reading["gps_lon"] == round(_PRECISE_LON, 3)
    # Nested payload.sensors.gps also coarsened.
    nested = reading["payload"]["sensors"]["gps"]
    assert nested["lat"] == round(_PRECISE_LAT, 3)
    assert nested["lon"] == round(_PRECISE_LON, 3)
    # "latest" mirrors readings[0] post-scrub.
    assert body["latest"]["gps_lat"] == round(_PRECISE_LAT, 3)
    # No 6-decimal coords anywhere in the raw JSON.
    assert "38.054321"  not in r.get_data(as_text=True)
    assert "-85.527834" not in r.get_data(as_text=True)


def test_private_mode_preserves_wallet_address_and_gps(client, monkeypatch):
    """Sanity: when public_mode is OFF (the operator's normal state),
    we DO want wallet_address and precise GPS to come through — that
    is the operator's own data on their own dashboard."""
    monkeypatch.setattr(oracle_client, "get_node",
                        lambda nid: _node_with_secrets(nid))
    monkeypatch.setattr(oracle_client, "list_readings",
                        lambda nid, limit: [_reading_with_gps()])
    monkeypatch.setattr(oracle_client, "root", lambda: {"current_season": 1})
    monkeypatch.setattr(oracle_client, "get_uptime",
                        lambda nid, season: None)

    r = client.get("/api/tree/ABCDEF0123456789ABCDEF0123456789/latest")
    assert r.status_code == 200
    body = r.get_json()
    assert body["node"]["wallet_address"] == _DOXX_WALLET
    assert body["readings"][0]["gps_lat"] == _PRECISE_LAT
    assert body["readings"][0]["payload"]["sensors"]["gps"]["lat"] == _PRECISE_LAT


# ---------------- Phase 6.5 verify_pass ----------------

def test_verify_pass_holder_returns_nft_id(client, monkeypatch):
    """Wallet that owns a Pass: endpoint returns has_pass=true plus
    the bound nft_id and a MintGarden link the wizard can show the
    operator."""
    from orchard_chia.nft import verify as nft_verify
    monkeypatch.setattr(nft_verify, "list_passes_by_address", lambda addr: [{
        "nft_coin_id":    "nft1n00ugdl737xc6ht4yjdc3cer047lcz9actdxfzpxyat3tsu72z0q46g56z",
        "launcher_id":    "9bdfe10dfe8fc6c6aebaa49370e3919f5fbf022f70b69912130dd71570f2854f",
        "name":           "Orchard Pass #0001",
        "edition_number": 1,
        "owner_address":  addr,
    }])
    r = client.post("/api/oracle/verify_pass",
                    json={"wallet_address": "xch1m3rvtj86wzzfjyk5mc7wzpr7h4zkaknm4wte7kg6afleu4f2tfxsr7nk3n"})
    assert r.status_code == 200, r.get_data(as_text=True)
    body = r.get_json()
    assert body["has_pass"] is True
    assert body["pass_nft_id"].startswith("nft1")
    assert body["edition_number"] == 1
    assert "mintgarden.io/nfts/nft1" in body["mintgarden_url"]


def test_verify_pass_non_holder_returns_has_pass_false(client, monkeypatch):
    """Wallet that doesn't hold a Pass: has_pass=false plus a buy_url
    the wizard can link the operator to."""
    from orchard_chia.nft import verify as nft_verify
    monkeypatch.setattr(nft_verify, "list_passes_by_address", lambda addr: [])
    r = client.post("/api/oracle/verify_pass",
                    json={"wallet_address": "xch1nobody00000000000000000000000000000000000000000000000000zz0t"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["has_pass"] is False
    assert body["pass_nft_id"] is None
    assert "mintgarden.io/collections/col1a56lp9" in body["buy_url"]


def test_verify_pass_missing_address_returns_400(client):
    r = client.post("/api/oracle/verify_pass", json={})
    assert r.status_code == 400


def test_verify_pass_indexer_error_returns_502(client, monkeypatch):
    from orchard_chia.nft import verify as nft_verify
    def boom(addr):
        raise nft_verify.IndexerError("MintGarden 500: bad gateway")
    monkeypatch.setattr(nft_verify, "list_passes_by_address", boom)
    r = client.post("/api/oracle/verify_pass",
                    json={"wallet_address": "xch1m3rvtj86wzzfjyk5mc7wzpr7h4zkaknm4wte7kg6afleu4f2tfxsr7nk3n"})
    assert r.status_code == 502
    assert "indexer error" in r.get_json()["error"]


def test_verify_pass_blocked_in_public_mode(public_client):
    """Operator-only endpoint must 404 in public mode (no leaking
    Pass-ownership queries through the public tunnel)."""
    r = public_client.post("/api/oracle/verify_pass",
                           json={"wallet_address": "xch1anything"})
    assert r.status_code == 404


# ---------------- Phase 6.6 connect ----------------

def test_api_auth_config_default_returns_unconfigured(monkeypatch):
    """Force an empty wc_project_id even if the operator's local
    dashboard/.env has a real one set. Exercises the empty-default
    branch deterministically."""
    monkeypatch.setenv("ORCHARD_VIEW_WC_PROJECT_ID", "")
    from dashboard.app import config as dash_config
    dash_config.reset_settings_for_tests()
    from dashboard.app.main import create_app
    app = create_app()
    with app.test_client() as c:
        r = c.get("/api/auth/config")
        assert r.status_code == 200
        body = r.get_json()
        assert body["wc_configured"] is False
        assert body["wc_project_id"] == ""
        assert body["metadata"]["name"] == "Orchard View"
    dash_config.reset_settings_for_tests()


def test_api_auth_config_picks_up_env_project_id(monkeypatch):
    """When ORCHARD_VIEW_WC_PROJECT_ID is set, the config endpoint
    surfaces it so the browser can init SignClient."""
    monkeypatch.setenv("ORCHARD_VIEW_WC_PROJECT_ID", "abc123def456")
    from dashboard.app import config as dash_config
    dash_config.reset_settings_for_tests()
    from dashboard.app.main import create_app
    app = create_app()
    with app.test_client() as c:
        r = c.get("/api/auth/config")
        assert r.status_code == 200
        body = r.get_json()
        assert body["wc_configured"] is True
        assert body["wc_project_id"] == "abc123def456"
    dash_config.reset_settings_for_tests()


def test_api_auth_challenge_forwards_to_oracle(client, monkeypatch):
    """POST /api/auth/challenge round-trips through the oracle proxy."""
    import requests
    class FakeResp:
        status_code = 200
        def json(self): return {"nonce": "deadbeef", "expires_at": 1, "message": "x"}
    def fake_post(url, timeout, **kw):
        assert url.endswith("/auth/challenge"), url
        return FakeResp()
    monkeypatch.setattr(requests, "post", fake_post)
    r = client.post("/api/auth/challenge")
    assert r.status_code == 200
    assert r.get_json()["nonce"] == "deadbeef"


def test_api_auth_verify_forwards_body(client, monkeypatch):
    """POST /api/auth/verify proxies the signed-challenge payload."""
    import requests
    captured = {}
    class FakeResp:
        status_code = 200
        def json(self): return {"session_token": "T", "address": "xch1...", "expires_at": 1}
    def fake_post(url, json=None, timeout=None, **kw):
        captured["url"] = url
        captured["body"] = json
        return FakeResp()
    monkeypatch.setattr(requests, "post", fake_post)
    r = client.post(
        "/api/auth/verify",
        json={"address": "xch1abc", "public_key": "ab"*48,
              "signature": "00"*96, "nonce": "deadbeef"},
    )
    assert r.status_code == 200
    assert captured["url"].endswith("/auth/verify")
    assert captured["body"]["nonce"] == "deadbeef"


def test_base_template_includes_connect_slot(client, monkeypatch):
    """The Connect Wallet area must be wired into base.html so every
    page picks it up via the script tag.

    Mock the oracle: this assertion is about the template chrome, not
    live oracle connectivity. Without the mock, a local process that
    answers on the configured oracle URL with a non-JSON body would
    crash the page (JSONDecodeError) and flake the suite.
    """
    monkeypatch.setattr(
        oracle_client, "root",
        lambda: {"version": "0.1.0", "current_season": 1, "now_utc": "2026-05-27T20:00:00+00:00"},
    )
    monkeypatch.setattr(oracle_client, "list_nodes", lambda: [])
    r = client.get("/")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert 'id="connect-slot"' in html
    assert "connect.js" in html


def test_root_non_json_becomes_oracle_error(monkeypatch):
    """A 200 with an empty/HTML body must not raise JSONDecodeError."""
    class _FakeResp:
        status_code = 200
        text = "<html>not json</html>"

        def json(self):
            raise ValueError("No JSON object could be decoded")

    monkeypatch.setattr(
        "dashboard.app.oracle_client.requests.get",
        lambda *a, **k: _FakeResp(),
    )
    with pytest.raises(oracle_client.OracleError) as ei:
        oracle_client.root()
    assert "non-JSON" in str(ei.value)


# ---------------- 2026-06-09 security hardening ----------------

def test_set_wifi_rejects_serial_injection(monkeypatch):
    """H3: a newline in the SSID or password would inject a second
    firmware command — must be rejected, clean values must pass."""
    monkeypatch.setattr(tree_serial, "_send_and_read_line", lambda *a, **k: "OK")
    with pytest.raises(tree_serial.TreeError):
        tree_serial.set_wifi("COM4", "MySSID", "pw\nORACLE_SET http://evil/readings")
    with pytest.raises(tree_serial.TreeError):
        tree_serial.set_wifi("COM4", "ssid\r\nREBOOT", "pw")
    tree_serial.set_wifi("COM4", "MySSID", "cleanpass")  # no raise


def test_set_oracle_url_rejects_injection_and_bad_scheme(monkeypatch):
    """H3: oracle URL must be a clean http(s) URL — no injected commands,
    no non-http schemes."""
    monkeypatch.setattr(tree_serial, "_send_and_read_line", lambda *a, **k: "OK")
    with pytest.raises(tree_serial.TreeError):
        tree_serial.set_oracle_url("COM4", "http://ok\nWIFI_CLEAR")
    with pytest.raises(tree_serial.TreeError):
        tree_serial.set_oracle_url("COM4", "ftp://nope")
    tree_serial.set_oracle_url("COM4", "http://192.168.0.5:8000/readings")  # no raise


def test_csrf_cross_origin_post_rejected(client):
    """H4: a state-changing POST from another origin is rejected."""
    r = client.post(
        "/api/serial/identify", json={"port": "COM4"},
        headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_csrf_same_origin_post_allowed(client):
    """H4: a same-origin POST passes the guard (then 400s on missing
    field — proving the guard let it through, not a 403)."""
    r = client.post(
        "/api/serial/identify", json={},
        headers={"Origin": "http://localhost"})
    assert r.status_code == 400  # not 403


def test_security_headers_present(client):
    """L1: clickjacking + sniffing + referrer headers on responses."""
    r = client.get("/api/auth/config")
    assert r.status_code == 200
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert r.headers.get("X-Content-Type-Options") == "nosniff"
    assert "frame-ancestors 'none'" in (r.headers.get("Content-Security-Policy") or "")


def test_public_mode_auth_config_hides_wc_project_id(monkeypatch):
    """L5: public mode must not expose the operator's WalletConnect
    project id even when one is configured."""
    monkeypatch.setenv("ORCHARD_VIEW_PUBLIC_MODE", "1")
    monkeypatch.setenv("ORCHARD_VIEW_WC_PROJECT_ID", "secretprojid")
    dash_config.reset_settings_for_tests()
    app = create_app()
    with app.test_client() as c:
        body = c.get("/api/auth/config").get_json()
        assert body["wc_project_id"] == ""
        assert body["wc_configured"] is False
    dash_config.reset_settings_for_tests()
