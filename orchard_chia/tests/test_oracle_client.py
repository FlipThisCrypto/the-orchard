# SPDX-License-Identifier: Apache-2.0
"""OracleClient robustness — malformed/absent responses become OracleError."""
from __future__ import annotations

import pytest

from orchard_chia.datalayer.oracle import OracleClient, OracleError


def _client(get_impl) -> OracleClient:
    c = OracleClient("http://oracle.local")
    c._get = get_impl  # type: ignore[assignment]
    return c


def test_current_season_ok():
    c = _client(lambda path, **kw: {"current_season": "7"})
    assert c.current_season() == 7


def test_current_season_missing_field_raises_oracle_error():
    c = _client(lambda path, **kw: {"unrelated": 1})
    with pytest.raises(OracleError):
        c.current_season()


def test_current_season_non_int_raises_oracle_error():
    c = _client(lambda path, **kw: {"current_season": "not-a-number"})
    with pytest.raises(OracleError):
        c.current_season()


def test_root_404_raises_oracle_error():
    c = _client(lambda path, **kw: None)  # _get returns None on 404
    with pytest.raises(OracleError):
        c.current_season()


def test_root_non_dict_raises_oracle_error():
    c = _client(lambda path, **kw: ["not", "a", "dict"])
    with pytest.raises(OracleError):
        c.root()
