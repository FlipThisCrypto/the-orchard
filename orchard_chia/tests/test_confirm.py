# SPDX-License-Identifier: Apache-2.0
"""Post-write DataLayer confirmation tests."""
from __future__ import annotations

from orchard_chia.datalayer import confirm


def test_inserts_from_changelist_prefers_final_insert():
    cl = [
        {"action": "delete", "key": "aa"},
        {"action": "insert", "key": "aa", "value": "11"},
        {"action": "insert", "key": "bb", "value": "22"},
        {"action": "delete", "key": "bb"},
    ]
    pairs = dict(confirm.inserts_from_changelist(cl))
    assert pairs == {"aa": "11"}


def test_confirm_ok():
    store = {"aa": "11", "bb": "22"}

    class Fake:
        def get_value(self, store_id, key_hex):
            return store.get(key_hex)

    r = confirm.confirm_inserts(Fake(), "s", [("aa", "11"), ("bb", "22")])
    assert r.ok
    assert r.checked == 2


def test_confirm_missing_and_mismatch():
    store = {"aa": "99"}

    class Fake:
        def get_value(self, store_id, key_hex):
            return store.get(key_hex)

    r = confirm.confirm_inserts(
        Fake(), "s", [("aa", "11"), ("bb", "22")]
    )
    assert not r.ok
    assert len(r.mismatched) == 1
    assert len(r.missing) == 1


def test_confirm_empty_ok():
    class Fake:
        def get_value(self, store_id, key_hex):
            return None

    r = confirm.confirm_inserts(Fake(), "s", [])
    assert r.ok
    assert r.checked == 0
