# SPDX-License-Identifier: Apache-2.0
"""Nothing permanent gets written about a Tree that isn't real.

The production store holds three ``attest:`` records for
``5B9BB022649FA93D4091DA4BA40714B9``. That is not a Tree — it is the fixture
constant in ``_gen_vectors.py``, ``test_integration_e2e.py``, ``test_fetch.py``
and ``test_datalayer.py``. Test data reached mainnet during early development
and cannot be removed, because DataLayer is append-only by design.

Nothing would have stopped a fourth. Both write paths took the oracle's node
list on trust, and ``attest`` treated an empty list as a clean no-op — the same
shape as the ``get_keys`` bug, where an unreadable source and an empty one give
the same answer and the comfortable reading wins.
"""
from __future__ import annotations

import pytest

from orchard_chia.datalayer.provenance import (FIXTURE_NODE_IDS,
                                               ProvenanceError, check_writable,
                                               filter_writable,
                                               known_node_ids,
                                               require_live_network)

REAL = "D8641AD6CAE36977818499469F7E8C49"
OTHER = "E014926F4805D7D848E4EDC32D70E39F"
FIXTURE = "5B9BB022649FA93D4091DA4BA40714B9"


def nodes(*ids):
    return [{"node_id": i} for i in ids]


# --- the fixture that reached mainnet ---------------------------------------

def test_the_fixture_id_that_is_already_on_mainnet_is_refused():
    with pytest.raises(ProvenanceError, match="test fixture id"):
        check_writable(FIXTURE, {REAL, FIXTURE})


def test_it_is_refused_even_when_the_oracle_vouches_for_it():
    """Most likely how the existing three got there: a test pointed at the
    production oracle, which then genuinely knew the id. A gate that trusts the
    oracle completely would have let all three through."""
    known = known_node_ids(nodes(REAL, FIXTURE))
    assert FIXTURE in known
    with pytest.raises(ProvenanceError, match="test fixture"):
        check_writable(FIXTURE, known)


def test_the_refusal_says_not_to_relax_it():
    with pytest.raises(ProvenanceError, match="do not relax this check"):
        check_writable(FIXTURE, {FIXTURE})


def test_every_known_fixture_id_is_covered():
    for fid in FIXTURE_NODE_IDS:
        with pytest.raises(ProvenanceError):
            check_writable(fid, {fid})


def test_the_fixture_list_matches_what_the_tests_actually_use():
    """A stale allow-list is worse than none: it reads as protection."""
    import pathlib
    src = " ".join(
        p.read_text(encoding="utf-8", errors="ignore")
        for p in pathlib.Path(__file__).parent.glob("test_*.py"))
    for fid in FIXTURE_NODE_IDS:
        assert fid in src, (
            f"{fid} is listed as a fixture id but no test uses it — either the "
            f"list is stale or the id is real")


# --- unrecognised subjects ---------------------------------------------------

def test_an_unregistered_node_is_refused():
    with pytest.raises(ProvenanceError, match="does not|not a Tree this oracle"):
        check_writable(OTHER, {REAL})


def test_a_registered_node_passes():
    check_writable(REAL, {REAL, OTHER})


def test_matching_is_case_insensitive():
    check_writable(REAL.lower(), {REAL})


def test_an_empty_node_id_is_refused():
    with pytest.raises(ProvenanceError, match="empty node_id"):
        check_writable("", {REAL})


def test_the_refusal_explains_the_stakes():
    with pytest.raises(ProvenanceError, match="permanent, public and fee-bearing"):
        check_writable(OTHER, {REAL})


# --- an unread oracle is not an empty network -------------------------------

def test_an_empty_node_set_stops_the_run():
    """attest used to exit 0 here. A scheduled writer concluding 'no Trees' is
    reporting on the oracle, not on the network."""
    with pytest.raises(ProvenanceError, match="Refusing to treat that as an empty network"):
        require_live_network([], source="https://oracle.test")


def test_none_is_treated_the_same_as_empty():
    with pytest.raises(ProvenanceError):
        require_live_network(None, source="https://oracle.test")


def test_the_error_names_the_source_so_a_misconfigured_url_is_obvious():
    with pytest.raises(ProvenanceError, match="https://oracle.wrong"):
        require_live_network([], source="https://oracle.wrong")


def test_a_populated_network_returns_its_ids():
    assert require_live_network(nodes(REAL, OTHER), source="x") == {REAL, OTHER}


def test_nodes_without_ids_do_not_count_toward_a_live_network():
    with pytest.raises(ProvenanceError):
        require_live_network([{"label": "no id"}, {}], source="x")


# --- all-or-nothing ----------------------------------------------------------

def test_one_bad_node_stops_the_whole_batch():
    """Silently dropping it would produce a half-written season whose gaps are
    indistinguishable from Trees that were genuinely offline."""
    known = {REAL}
    with pytest.raises(ProvenanceError):
        filter_writable(nodes(REAL, OTHER), known)


def test_a_clean_batch_passes_through_unchanged():
    known = {REAL, OTHER}
    assert filter_writable(nodes(REAL, OTHER), known) == nodes(REAL, OTHER)


def test_an_empty_batch_is_not_an_error_here():
    """require_live_network is where emptiness is judged; this only filters."""
    assert filter_writable([], {REAL}) == []
