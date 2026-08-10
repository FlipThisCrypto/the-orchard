# SPDX-License-Identifier: Apache-2.0
"""Canonical $JUICE economic constants. One source of truth.

Every number governing token economics lives here. Nothing downstream may
hardcode a rate, a cap, or a share — if a figure matters economically it is
defined once, in this file, and imported.

MODEL_VERSION exists so a payout record can state which economics produced it.
An amount computed under one model and audited under another is unfalsifiable,
and these numbers are expected to be governed rather than frozen.

EVERYTHING IS INTEGER MOJOS
===========================

$JUICE is a Chia CAT with 3 decimals, so 1 JUICE = 1000 mojos and the smallest
representable amount is 0.001 JUICE. Binary floating point cannot represent
0.1, let alone hold an invariant like "the sum of all rewards ever paid never
exceeds 85,000,000" across millions of additions. Token accounting here is
integer mojos throughout, and intermediate proportions use exact rationals.
"""
from __future__ import annotations

from fractions import Fraction

# Bump when any figure below changes. Written into payout records.
MODEL_VERSION = "2.0.0"

MOJOS_PER_JUICE = 1_000          # 3-decimal CAT

# --- fixed supply -----------------------------------------------------------
# Never minted beyond this. Not a target, a ceiling.
TOTAL_SUPPLY_JUICE = 100_000_000
TOTAL_SUPPLY_MOJOS = TOTAL_SUPPLY_JUICE * MOJOS_PER_JUICE

TREE_REWARDS_POOL_JUICE = 85_000_000        # 85%
LIQUIDITY_JUICE = 15_000_000                # 15%

TREE_REWARDS_POOL_MOJOS = TREE_REWARDS_POOL_JUICE * MOJOS_PER_JUICE
LIQUIDITY_MOJOS = LIQUIDITY_JUICE * MOJOS_PER_JUICE

# No founder/team allocation is carved from the fixed supply. Recorded as an
# explicit zero rather than an absence, so a future non-zero value is a visible
# change to this file and not an addition nobody has to justify.
FOUNDER_ALLOCATION_MOJOS = 0

# --- the eight-year base emission schedule ----------------------------------
#
# Maximum DAILY NETWORK emission, in mojos. Note "network": this is the whole
# Orchard's ceiling for the day, not a per-Tree rate. Adding Trees divides this
# pool further; it can never raise it. That is the entire early-adopter
# incentive, and it needs no separate multiplier to produce.
#
# Each year is ~20% below the last. The figures are stated exactly rather than
# derived by repeated multiplication, because 0.8 compounded in floating point
# does not reproduce them and a schedule that drifts by a mojo a year is a
# schedule two implementations will eventually disagree about.
DAILY_EMISSION_MOJOS_BY_YEAR: dict[int, int] = {
    1: 55_964_650,      # 55,964.65 JUICE/day
    2: 44_771_720,      # 44,771.72
    3: 35_817_380,      # 35,817.38
    4: 28_653_900,      # 28,653.90
    5: 22_923_120,      # 22,923.12
    6: 18_338_500,      # 18,338.50
    7: 14_670_800,      # 14,670.80
    8: 11_736_640,      # 11,736.64
}

SCHEDULE_YEARS = 8
DAYS_PER_EMISSION_YEAR = 365

# After year 8 the year-8 ceiling continues until the pool is empty. The
# schedule is a MINIMUM runway, not an expiry: unearned rewards stay in the
# pool, so the programme lasts longer than eight years by construction.
TERMINAL_DAILY_EMISSION_MOJOS = DAILY_EMISSION_MOJOS_BY_YEAR[SCHEDULE_YEARS]

# --- heartbeats -------------------------------------------------------------
# 24 reward windows per day, one per hour. A Tree's uptime factor is
# verified_heartbeats / 24, and only heartbeats that actually verify count.
HEARTBEATS_PER_DAY = 24

# --- sensor weighting -------------------------------------------------------
#
# Additional qualifying sensors raise a Tree's SHARE of the day's pool. They
# never raise the pool. Expressed in twentieths so the arithmetic stays exact:
# 1.05 is not representable in binary floating point, 21/20 is.
SENSOR_BONUS_PER_EXTRA = Fraction(1, 20)     # +5%
MAX_SENSOR_WEIGHT = Fraction(25, 20)         # 1.25x, reached at 6 sensors
BASE_SENSOR_WEIGHT = Fraction(20, 20)        # 1.00x

# A Tree with no qualifying sensor earns nothing. An ESP32 that only sends
# heartbeats is not environmental infrastructure, and rewarding it would make
# the cheapest way to earn the one that produces no data.
MIN_QUALIFYING_SENSORS = 1


def juice(mojos: int) -> Fraction:
    """Mojos as an exact JUICE amount. For display and reports only."""
    return Fraction(mojos, MOJOS_PER_JUICE)


def format_juice(mojos: int) -> str:
    return f"{float(juice(mojos)):,.3f}"
