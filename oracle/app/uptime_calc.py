# SPDX-License-Identifier: Apache-2.0
"""Authoritative uptime computation.

Shared by the ``/uptime`` query and the attestation integrity cross-check so
both agree by construction — the oracle's answer to "how many hours was this
Tree online in this Season?" has exactly one implementation.

WHAT AN HOUR IS WORTH
=====================

An hour counts when it holds at least ``MIN_READINGS_PER_CREDITED_HOUR``
accepted readings. It used to count with ONE. Firmware samples every 60
seconds, so a healthy hour holds ~60 readings — under the old rule a Tree
reporting once an hour earned exactly what a continuously-reporting Tree
earned, a 60x overstatement of the thing being paid for.

The DataLayer side fixed this with a 30-reading signature quorum
(orchard_chia/datalayer/schema.py, MIN_VERIFIED_READINGS_PER_HOUR). This is
the same threshold applied to the oracle's own count, so the two accountings
agree about what an hour of sensing IS. They still differ in what they check —
the DataLayer quorum verifies device signatures, this counts accepted
readings — but they must never disagree about the quantity, or the payer and
the verifier would price the same day differently.

30 is half the expected cadence: a reboot, a Wi-Fi drop, or clock skew costs
an operator nothing, while an hour of near-silence cannot be sold as an hour
of sensing.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models, seasons

from .config import settings

# The production default (config.py) is numerically identical to
# orchard_chia.datalayer.schema's MIN_VERIFIED_READINGS_PER_HOUR — duplicated
# by value, not imported, because the oracle deploys without orchard_chia.
# test_uptime_quorum.py asserts the two match so drift is a red build, not a
# silent disagreement about money.


def _min_readings() -> int:
    return max(1, int(settings().min_readings_per_credited_hour))


def _spread_ok(mask: int) -> bool:
    """An hour must SPAN the hour, not just fill a burst. Hours recorded
    before the mask existed have slots_mask=0 and are exempt — the rule cannot
    be applied retroactively to data that never recorded spread."""
    if mask == 0:
        return True         # legacy row, spread unknown
    need = max(1, int(settings().min_slots_per_credited_hour))
    return bin(int(mask)).count("1") >= need


def hours_online_for(db: Session, node_id: str, season: int) -> tuple[int, list[str]]:
    """(hours_online, sorted hit buckets) for a node·season from uptime_hours.

    hours_online = number of distinct UTC-hour buckets within the Season whose
    ``reading_count`` meets the configured per-hour quorum.
    """
    season_buckets = set(seasons.hour_buckets_in_season(season))
    rows = db.execute(
        select(models.UptimeHour.hour_utc, models.UptimeHour.slots_mask).where(
            models.UptimeHour.node_id == node_id.upper(),
            models.UptimeHour.hour_utc.in_(season_buckets),
            models.UptimeHour.reading_count >= _min_readings(),
        )
    ).all()
    hit = sorted({bucket for bucket, mask in rows if _spread_ok(mask or 0)})
    return len(hit), hit
