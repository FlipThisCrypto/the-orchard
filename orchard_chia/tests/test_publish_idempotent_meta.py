# SPDX-License-Identifier: Apache-2.0
"""A quiet hour must be free.

``meta:schema`` carried a wall-clock ``created_at``, so the record differed on
every run purely because time had passed. ``_upsert`` saw a change, planned a
delete+insert, and the publisher submitted a fee-bearing spend — on every run,
including the ones with nothing new to say. Observed on 2026-08-09: a real run
reporting ``batches: 0`` still performed a batch_update.

Under a scheduler that is most runs. An hourly job publishing a Tree that has
been offline all week would pay a fee every hour to re-record that its schema
version is still the same schema version.
"""
from __future__ import annotations

from orchard_chia.datalayer import publish, schema

PUB = schema.pubkey_for_seed("01" + "00" * 31)


def _plan(existing=None, created_at="2026-08-10T00:00:00Z", pubkey=PUB,
          writer_version=publish.WRITER_VERSION):
    return publish.plan_publish(
        batches=[], season_pubkey=pubkey, writer_version=writer_version,
        created_at=created_at, existing_values=existing or {})


def test_a_first_publish_writes_meta():
    p = _plan()
    assert p.meta_written and p.changelist


def test_a_second_run_with_nothing_new_writes_nothing():
    """The whole point: no changelist means no spend, means no fee."""
    first = _plan(created_at="2026-08-10T00:00:00Z")
    mk = schema.meta_key()
    on_chain = {mk: next(i["value"] for i in first.changelist
                         if i["action"] == "insert" and i["key"] == mk)}

    later = _plan(existing=on_chain, created_at="2026-08-10T09:00:00Z")
    assert later.meta_written is False
    assert later.changelist == [], (
        "nine hours passing is not a change worth paying a blockchain fee for")


def test_the_stored_timestamp_is_preserved_verbatim():
    prior = schema.build_meta(writer_version=publish.WRITER_VERSION,
                              created_at="2026-06-09T00:00:00Z", season_pubkey=PUB)
    got = publish._stable_meta_created_at(
        prior, publish.WRITER_VERSION, PUB, "2026-08-10T09:00:00Z")
    assert got == "2026-06-09T00:00:00Z"


def test_any_change_anywhere_in_the_record_forces_a_rewrite():
    """Comparing the whole record, not named fields — the first version of this
    helper compared field names the record does not use, so it silently did
    nothing at all."""
    prior = schema.build_meta(writer_version=publish.WRITER_VERSION,
                              created_at="2026-06-09T00:00:00Z", season_pubkey=PUB)
    mutated = dict(prior, units=dict(prior["units"], newly_added={"display": "x",
                                                                  "pow10": 0}))
    assert publish._stable_meta_created_at(
        mutated, publish.WRITER_VERSION, PUB, "NEW") == "NEW"


def test_a_new_writer_version_is_a_real_change_and_is_recorded():
    first = _plan(created_at="2026-08-10T00:00:00Z")
    mk = schema.meta_key()
    on_chain = {mk: next(i["value"] for i in first.changelist
                         if i["action"] == "insert" and i["key"] == mk)}

    later = _plan(existing=on_chain, created_at="2026-08-10T09:00:00Z",
                  writer_version="9.9.9")
    assert later.meta_written is True, "a genuine change must still be written"


def test_a_new_season_pubkey_is_a_real_change():
    other = schema.pubkey_for_seed("02" + "00" * 31)
    first = _plan(created_at="2026-08-10T00:00:00Z")
    mk = schema.meta_key()
    on_chain = {mk: next(i["value"] for i in first.changelist
                         if i["action"] == "insert" and i["key"] == mk)}

    later = _plan(existing=on_chain, created_at="2026-08-10T09:00:00Z",
                  pubkey=other)
    assert later.meta_written is True, (
        "the key that signs seasons changing is exactly what meta exists to say")


def test_an_unreadable_prior_falls_back_rather_than_crashing():
    assert publish._stable_meta_created_at(None, "v", PUB, "T") == "T"
    assert publish._stable_meta_created_at({}, "v", PUB, "T") == "T"
    assert publish._stable_meta_created_at(
        {"created_at": ""}, "v", PUB, "T") == "T"


def test_a_prior_from_a_different_schema_version_is_not_preserved():
    prior = schema.build_meta(writer_version=publish.WRITER_VERSION,
                              created_at="2026-01-01T00:00:00Z", season_pubkey=PUB)
    prior["orchard_schema"] = "0.9.0"
    assert publish._stable_meta_created_at(
        prior, publish.WRITER_VERSION, PUB, "NEW") == "NEW"
