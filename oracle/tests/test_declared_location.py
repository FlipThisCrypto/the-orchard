# SPDX-License-Identifier: Apache-2.0
"""An operator can say where their own Tree is — coarsely, and only their own.

Why this exists: most Trees have no GPS module. The oracle derives a cell from
a Tree's own GPS when it has one, which is the better answer because it is
measured — but a Tree without GPS then has no location at all. Not an unknown
position: nothing. That is literally how the globe went empty. Once the ghost
Trees were retired, the one live Tree turned out to have neither a GPS reading
nor an entry in worldview's hardcoded fallback table, so there was nothing to
draw and the map rendered empty rather than wrong.

The properties that matter, and that these tests pin:
  * only the owning wallet can declare, and a stranger cannot even tell the
    node exists (404, never 403)
  * precision is capped at the ~5 km cell the network publicly promises —
    enforced, not trusted
  * precise coordinates are NEVER stored, even when the caller sends them
  * a measured GPS fix always beats a declaration
  * it is reversible, and the response always says which kind of position
    it is handing you
"""
from __future__ import annotations

import datetime as dt

import pytest
from fastapi.testclient import TestClient

from oracle.app import db, models
from oracle.app.main import app

MINE = "D8641AD6CAE36977818499469F7E8C49"
THEIRS = "E014926F4805D7D848E4EDC32D70E39F"
MY_ADDR = "xch1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqmine"
THEIR_ADDR = "xch1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqtheirs"

# Somewhere real, at full device precision. Precision 5 of this is "dhwfx".
MIAMI_LAT, MIAMI_LON = 25.774300, -80.193600


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHARD_ORACLE_DB_URL", f"sqlite:///{(tmp_path/'t.db').as_posix()}")
    from oracle.app.config import reset_settings_for_tests
    from oracle.app.db import reset_for_tests
    reset_settings_for_tests()
    reset_for_tests()
    db.create_all()
    now = dt.datetime.now(dt.timezone.utc)
    s = db.session_factory()()
    for nid, addr in ((MINE, MY_ADDR), (THEIRS, THEIR_ADDR)):
        s.add(models.Node(node_id=nid, signing_key_hex="ab" * 32, last_seq=0,
                          registered_at=now, wallet_address=addr))
    s.commit(); s.close()
    return TestClient(app)


def _as(address: str) -> dict:
    """Authenticate as a wallet without walking the BLS challenge flow.

    The signature path has its own tests; what is under test here is the
    ownership rule, so we mint a session directly.
    """
    from oracle.app import sessions
    token, _ = sessions.issue(address)
    return {"Authorization": f"Bearer {token}"}


def _node(client, nid, headers=None):
    return client.get(f"/nodes/{nid}", headers=headers or {}).json()


# --- the coarsening is a real geohash --------------------------------------

def test_the_encoder_agrees_with_published_geohashes():
    """The whole privacy contract rests on this being a standard geohash.

    If the encoder drifted, every published cell would be a plausible-looking
    string that decodes somewhere else — silently, and permanently, since
    cells go to DataLayer. These are published reference vectors, not values
    read back out of our own implementation.
    """
    from oracle.app.routes.nodes import _geohash_encode as gh
    assert gh(57.64911, 10.40744, 11) == "u4pruydqqvj"   # the canonical example
    assert gh(42.6, -5.6, 5) == "ezs42"
    assert gh(51.5074, -0.1278, 5) == "gcpvj"            # London
    assert gh(-90.0, -180.0, 5) == "00000"               # corner of the world
    # A cell is a prefix of a finer cell in the same place — that property is
    # what makes precision-capping a real privacy control rather than a label.
    fine = gh(25.7743, -80.1936, 9)
    assert fine.startswith(gh(25.7743, -80.1936, 5))


def test_exact_cell_boundaries_stay_on_one_side():
    """(0, 0) lies exactly on a cell edge, where the tie-break is a convention.

    Pinned so it is a decision rather than an accident: we resolve toward the
    lower interval. The neighbouring convention would yield 's0000'; both name
    a cell touching the point. Real GPS never lands here, but a person typing
    coordinates by hand might.
    """
    from oracle.app.routes.nodes import _geohash_encode as gh
    assert gh(0.0, 0.0, 5) == "7zzzz"


# --- ownership -------------------------------------------------------------

def test_owner_can_declare(client):
    r = client.post(f"/nodes/{MINE}/location", json={"geohash": "dhwfx"},
                    headers=_as(MY_ADDR))
    assert r.status_code == 200, r.text
    assert r.json()["geohash"] == "dhwfx"


def test_a_stranger_cannot_declare_and_cannot_tell_it_exists(client):
    r = client.post(f"/nodes/{MINE}/location", json={"geohash": "dhwfx"},
                    headers=_as(THEIR_ADDR))
    assert r.status_code == 404, (
        "403 would confirm the node_id is real to someone who does not own it"
    )
    assert _node(client, MINE)["geohash"] is None, "and nothing may be written"


def test_no_session_is_rejected(client):
    r = client.post(f"/nodes/{MINE}/location", json={"geohash": "dhwfx"})
    assert r.status_code == 401
    assert _node(client, MINE)["geohash"] is None


# --- the ~5 km privacy contract -------------------------------------------

def test_over_precise_geohash_is_refused(client):
    r = client.post(f"/nodes/{MINE}/location", json={"geohash": "dhwfxm2j5"},
                    headers=_as(MY_ADDR))
    assert r.status_code == 422, "9 characters is metre-level — the contract is ~5 km"
    assert "dhwfx" in r.text, "the error should hand back a usable value"
    assert _node(client, MINE)["geohash"] is None


def test_coordinates_are_coarsened_and_the_precise_value_is_never_stored(client):
    """The convenience form must not become a second precision tier."""
    from sqlalchemy import text
    r = client.post(f"/nodes/{MINE}/location",
                    json={"lat": MIAMI_LAT, "lon": MIAMI_LON},
                    headers=_as(MY_ADDR))
    assert r.status_code == 200, r.text
    assert r.json()["geohash"] == "dhwfx"

    # Nothing anywhere in the node row may retain the precise position.
    with db.engine().connect() as conn:
        row = conn.execute(text("SELECT * FROM nodes WHERE node_id=:n"),
                           {"n": MINE}).mappings().one()
    blob = " ".join(str(v) for v in row.values())
    assert "25.7743" not in blob and "-80.1936" not in blob, (
        f"precise coordinates survived into storage: {dict(row)}"
    )


def test_the_audit_trail_does_not_carry_the_precise_position(client):
    """An audit row is written to be read later — by more people than the DB."""
    client.post(f"/nodes/{MINE}/location",
                json={"lat": MIAMI_LAT, "lon": MIAMI_LON}, headers=_as(MY_ADDR))
    events = client.get("/audit", headers=_as(MY_ADDR)).json()
    text_of = " ".join(str(e) for e in events)
    assert "25.7743" not in text_of and "-80.1936" not in text_of
    assert "dhwfx" in text_of, "the coarse cell itself is public — record it"


def test_garbage_is_not_a_geohash(client):
    r = client.post(f"/nodes/{MINE}/location", json={"geohash": "ail!!"},
                    headers=_as(MY_ADDR))
    assert r.status_code == 422
    assert _node(client, MINE)["geohash"] is None


def test_out_of_range_coordinates_are_refused(client):
    r = client.post(f"/nodes/{MINE}/location", json={"lat": 991.0, "lon": 0.0},
                    headers=_as(MY_ADDR))
    assert r.status_code == 422


def test_lat_without_lon_is_refused(client):
    r = client.post(f"/nodes/{MINE}/location", json={"lat": MIAMI_LAT},
                    headers=_as(MY_ADDR))
    assert r.status_code == 422, "half a coordinate is not a location"


def test_both_forms_at_once_is_refused(client):
    r = client.post(f"/nodes/{MINE}/location",
                    json={"geohash": "dhwfx", "lat": 1.0, "lon": 2.0},
                    headers=_as(MY_ADDR))
    assert r.status_code == 422, "ambiguous input must not be silently resolved"


# --- measured beats asserted ----------------------------------------------

def test_a_real_gps_fix_overrides_the_declaration(client):
    """Declaring is a stopgap for Trees without GPS, not a way to move one."""
    client.post(f"/nodes/{MINE}/location", json={"geohash": "u4pru"},
                headers=_as(MY_ADDR))          # somewhere in Norway
    assert _node(client, MINE)["geohash"] == "u4pru"

    s = db.session_factory()()
    s.add(models.Reading(node_id=MINE, received_at=dt.datetime.now(dt.timezone.utc),
                         payload_json="{}", sig_hex="ef" * 32,
                         gps_lat=MIAMI_LAT, gps_lon=MIAMI_LON))
    s.commit(); s.close()

    got = _node(client, MINE)
    assert got["geohash"] == "dhwfx", "a measurement must win over an assertion"
    assert got["location_source"] == "device"


def test_the_response_says_which_kind_of_position_it_is(client):
    assert _node(client, MINE)["location_source"] is None, "unplaced says so"
    client.post(f"/nodes/{MINE}/location", json={"geohash": "dhwfx"},
                headers=_as(MY_ADDR))
    assert _node(client, MINE)["location_source"] == "declared", (
        "a reader must never have to guess whether a dot was measured or claimed"
    )


# --- reversibility + reach -------------------------------------------------

def test_a_declaration_can_be_withdrawn(client):
    client.post(f"/nodes/{MINE}/location", json={"geohash": "dhwfx"},
                headers=_as(MY_ADDR))
    r = client.post(f"/nodes/{MINE}/location", json={"geohash": None},
                    headers=_as(MY_ADDR))
    assert r.status_code == 200
    got = _node(client, MINE)
    assert got["geohash"] is None and got["location_source"] is None


def test_the_declared_cell_reaches_the_public_list(client):
    """The globe reads /nodes, not /nodes/{id} — the point is that it renders."""
    client.post(f"/nodes/{MINE}/location", json={"geohash": "dhwfx"},
                headers=_as(MY_ADDR))
    placed = {n["node_id"]: n for n in client.get("/nodes").json()}
    assert placed[MINE]["geohash"] == "dhwfx"
    assert placed[MINE]["location_source"] == "declared"


def test_declaring_does_not_leak_the_owner_wallet_to_the_public(client):
    client.post(f"/nodes/{MINE}/location", json={"geohash": "dhwfx"},
                headers=_as(MY_ADDR))
    pub = {n["node_id"]: n for n in client.get("/nodes").json()}[MINE]
    assert pub["wallet_address"] is None, (
        "placing a Tree on a map must not tie it to its operator's wallet"
    )
