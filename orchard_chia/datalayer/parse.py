# SPDX-License-Identifier: Apache-2.0
"""Safe season number parsing for DataLayer keys and CLI args."""
from __future__ import annotations

def parse_season(value) -> int:
    """Parse a season number; raise ValueError if not a positive int."""
    if isinstance(value, bool):
        raise ValueError("season must be an integer")
    n = int(value)
    if n < 1:
        raise ValueError("season must be >= 1")
    return n


def parse_hour(value) -> int:
    """Parse UTC hour-of-day 0..23."""
    if isinstance(value, bool):
        raise ValueError("hour must be an integer")
    n = int(value)
    if n < 0 or n > 23:
        raise ValueError("hour must be in 0..23")
    return n
