# SPDX-License-Identifier: Apache-2.0
"""The gate every permanent write passes through: is this Tree real?

WHY THIS EXISTS, WITH EVIDENCE
==============================

The production store holds three ``attest:`` records for node_id
``5B9BB022649FA93D4091DA4BA40714B9``. That id is not a Tree. It is the fixture
constant in ``_gen_vectors.py``, ``test_integration_e2e.py``, ``test_fetch.py``
and ``test_datalayer.py`` — test data, written to mainnet during early
development, permanently, and it cannot be removed because DataLayer is
append-only by design.

Nothing in the writer would stop it happening again. Both write paths take the
oracle's node list on trust, and ``attest`` additionally treats an empty list as
a benign no-op — which is the same failure shape as the ``get_keys`` bug: an
oracle that cannot be read looks exactly like a network with no Trees, and
"nothing to do" is a comfortable thing for a scheduled job to conclude.

WHAT THIS GATE ASSERTS
======================

  * every node_id about to be written is one the oracle currently recognises
  * an empty node set stops the run rather than completing it successfully
  * no known test fixture id is ever written, whatever the oracle says

The last one is belt-and-braces on purpose. If a fixture id were ever
registered against the production oracle — by a test pointed at the wrong URL,
which is very likely how the existing three got there — the first two checks
would happily pass it.
"""
from __future__ import annotations

# node_ids that exist only in this repo's fixtures. A write carrying one of
# these is a test that escaped, never a Tree.
#
# Kept as an explicit list rather than a pattern: these ids are indistinguishable
# from real ones by shape, which is precisely why the first three reached
# mainnet unremarked.
FIXTURE_NODE_IDS = frozenset({
    "5B9BB022649FA93D4091DA4BA40714B9",   # _gen_vectors.py, test_integration_e2e.py,
                                          # test_fetch.py, test_datalayer.py
    "AABBCCDDEEFF0011AABBCCDDEEFF0011",   # test_reconcile.py, test_seal.py, and others
})


class ProvenanceError(RuntimeError):
    """A write was refused because its subject could not be shown to be real."""


def known_node_ids(nodes: list[dict]) -> set[str]:
    out = set()
    for n in nodes or []:
        nid = str((n or {}).get("node_id") or "").strip().upper()
        if nid:
            out.add(nid)
    return out


def require_live_network(nodes: list[dict], *, source: str) -> set[str]:
    """The node set a run is allowed to write for.

    Raises rather than returning an empty set. A scheduled writer that finds no
    Trees has either been pointed at the wrong oracle or is reading one that is
    failing quietly, and neither is a reason to exit successfully. There is no
    legitimate state in which this network has zero Trees AND something worth
    writing about them.
    """
    known = known_node_ids(nodes)
    if not known:
        raise ProvenanceError(
            f"{source} returned no Trees. Refusing to treat that as an empty "
            f"network: an oracle that cannot be read looks identical to one "
            f"with nothing registered, and a writer must not conclude 'nothing "
            f"to do' from a failed read. Check the oracle URL and its /nodes "
            f"response before re-running."
        )
    return known


def check_writable(node_id: str, known: set[str]) -> None:
    """Refuse a node_id that is not a currently-registered Tree.

    Raises ``ProvenanceError``. Callers should let it stop the run rather than
    skipping the node: if the writer's idea of the network disagrees with the
    oracle's, the safe assumption is that the writer is wrong about all of it,
    not just this one.
    """
    nid = str(node_id or "").strip().upper()
    if not nid:
        raise ProvenanceError("refusing to write a record with an empty node_id")
    if nid in FIXTURE_NODE_IDS:
        raise ProvenanceError(
            f"{nid} is a test fixture id, not a Tree. Three such records are "
            f"already on mainnet permanently; this refusal exists so there are "
            f"never four. If a fixture id has genuinely been registered against "
            f"this oracle, un-register it — do not relax this check."
        )
    if nid not in known:
        raise ProvenanceError(
            f"{nid} is not a Tree this oracle currently recognises. DataLayer "
            f"writes are permanent, public and fee-bearing, so an unrecognised "
            f"subject stops the run. If this Tree is real, confirm it is "
            f"registered and not retired."
        )


def filter_writable(nodes: list[dict], known: set[str]) -> list[dict]:
    """Every node in ``nodes``, once all of them have been checked.

    Deliberately all-or-nothing. Silently dropping the unrecognised ones would
    let a half-wrong node list produce a half-written season, which is worse
    than not writing: the gaps are indistinguishable from Trees that were
    genuinely offline.
    """
    for n in nodes or []:
        check_writable((n or {}).get("node_id", ""), known)
    return list(nodes or [])
