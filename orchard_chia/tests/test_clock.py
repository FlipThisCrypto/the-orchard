# SPDX-License-Identifier: Apache-2.0
from orchard_chia.datalayer.clock import utc_now, utc_now_iso


def test_utc_now_timezone_aware():
    n = utc_now()
    assert n.tzinfo is not None
    assert utc_now_iso().endswith("Z")
