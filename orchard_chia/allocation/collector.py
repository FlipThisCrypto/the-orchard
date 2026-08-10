# SPDX-License-Identifier: Apache-2.0
"""Uptime collector — turns what the oracle knows into allocation records.

WHAT THE ORACLE ACTUALLY MEASURES, AND WHY THIS FILE IS SHAPED LIKE THIS
=======================================================================

The specification asks for uptime per (tree, sensor) pair. The oracle does not
have that. ``uptime_hours`` holds one row per (node_id, UTC hour) with a
``reading_count``, bumped by ``_bump_uptime_hour()`` on any accepted reading
(oracle/app/routes/readings.py:48-66). ``hours_online`` is then the count of
distinct hour buckets containing at least one reading
(oracle/app/uptime_calc.py:16-34). It is node-level reading PRESENCE. Nothing
records which sensor produced the reading, so nothing can say that the DS18B20
was up and the GPS was down.

The tempting move is to emit one record per declared sensor and give each the
node's uptime. That is wrong in a way that would be very hard to spot later: a
node declaring two sensors would appear twice in its wallet's mean, so a wallet
would raise its own weight by declaring more sensors on the same hardware. The
average would stop meaning "how reliable is this operator's fleet" and start
meaning "how many sensor names did they list".

So this collector emits ONE record per node, ``sensor_id`` names the node's
declared sensor set, and ``uptime_basis`` records that the number is
node-level. When per-sensor uptime genuinely exists, ``PerSensorSource`` can be
implemented and the basis label changes with it. Until then the system says
what it measured rather than what was asked for.

WHAT ELIGIBILITY MEANS HERE
===========================

A pair is excluded — never silently dropped — when it is retired, unclaimed,
stale, or below the uptime floor. Excluded records travel with the result so
the audit shows what was considered and rejected. Dropping them would shrink
the denominator and quietly pay everyone else more.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .engine import BASIS_POINTS_FULL, UptimeRecord

USER_AGENT = "TheOrchard-Allocation/1.0 (+https://theorchard.network)"

# Cloudflare 403s urllib's default User-Agent on this origin, which surfaces as
# a bare HTTP 403 and reads exactly like an auth failure. Learned the hard way
# on the MintGarden indexer; the fix is the same and belongs on every client.


class CollectorError(RuntimeError):
    pass


@dataclass(frozen=True)
class CollectionResult:
    records: tuple[UptimeRecord, ...]
    period_start: datetime
    period_end: datetime
    uptime_basis: str
    source: str
    # Trees the oracle returned that produced no usable record, with the reason.
    skipped: tuple[tuple[str, str], ...] = ()

    @property
    def eligible(self) -> tuple[UptimeRecord, ...]:
        return tuple(r for r in self.records if r.eligible)


class OracleUptimeSource:
    """Reads uptime from a running oracle over HTTP.

    Deliberately read-only and side-effect free: collection must be safe to run
    at any time, including by an operator poking at production, because the
    thing it feeds is a spend.
    """

    def __init__(self, base_url: str, *, timeout: int = 30,
                 writer_token: str | None = None):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self._token = writer_token

    def _get(self, path: str) -> object:
        req = urllib.request.Request(self.base + path,
                                     headers={"User-Agent": USER_AGENT})
        if self._token:
            # Never logged, never echoed, never placed in a URL.
            req.add_header("Authorization", f"Bearer {self._token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                body = r.read()
        except urllib.error.HTTPError as e:
            raise CollectorError(
                f"GET {path} -> HTTP {e.code}. If this is 403 from a script "
                f"client, check the origin's WAF rules before assuming auth."
            ) from e
        except urllib.error.URLError as e:
            raise CollectorError(f"GET {path} unreachable: {e.reason}") from e
        try:
            return json.loads(body)
        except ValueError as e:
            head = body[:200].decode("utf-8", "replace").replace("\n", " ")
            raise CollectorError(
                f"GET {path} -> 200 but the body is not JSON ({e}). "
                f"Is {self.base} really the oracle? First bytes: {head!r}"
            ) from e

    def nodes(self) -> list[dict]:
        got = self._get("/nodes")
        if not isinstance(got, list):
            raise CollectorError(f"/nodes did not return a list, got {type(got).__name__}")
        return got

    def uptime(self, node_id: str, season: int) -> dict:
        got = self._get(f"/uptime/{node_id}/{season}")
        if not isinstance(got, dict):
            raise CollectorError(f"/uptime/{node_id}/{season} did not return an object")
        return got

    def current_season(self) -> int:
        stats = self._get("/network/stats")
        if not isinstance(stats, dict) or "current_season" not in stats:
            raise CollectorError("/network/stats did not carry current_season")
        return int(stats["current_season"])


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def collect(
    source: OracleUptimeSource,
    *,
    season: int | None = None,
    now: datetime | None = None,
    period_hours: int = 24,
    stale_after_hours: int = 3,
    min_uptime_bp: int = 0,
    require_wallet: bool = True,
) -> CollectionResult:
    """Build allocation records for one cycle.

    ``stale_after_hours`` is the sensor-liveness guard: a Tree whose last
    reading predates it is excluded regardless of the hours it accumulated
    earlier in the period. A Tree that died eighteen hours ago should not be
    paid for the six hours before it did, in a cycle that is meant to describe
    the present.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise CollectorError("`now` must be timezone-aware; naive times cause "
                             "silent hour-bucket drift against the oracle")
    if period_hours <= 0:
        raise CollectorError(f"period_hours must be positive, got {period_hours}")

    season = source.current_season() if season is None else season
    period_end = now
    period_start = now - timedelta(hours=period_hours)
    stale_before = now - timedelta(hours=stale_after_hours)

    records: list[UptimeRecord] = []
    skipped: list[tuple[str, str]] = []

    for node in source.nodes():
        node_id = str(node.get("node_id") or "")
        if not node_id:
            skipped.append(("<no node_id>", "oracle returned a node without an id"))
            continue

        sensors = node.get("sensors") or []
        sensor_id = "+".join(sorted(str(s) for s in sensors)) if sensors else "none"

        wallet = node.get("wallet_address")
        if require_wallet and not wallet:
            # Not an error: /nodes scrubs wallet_address for unauthenticated
            # callers by design. Without a session this is the expected state,
            # and paying an address we cannot see is not an option.
            skipped.append((node_id, "no wallet_address visible to this caller"))
            continue

        try:
            up = source.uptime(node_id, season)
        except CollectorError as e:
            skipped.append((node_id, f"uptime unavailable: {e}"))
            continue

        hours = int(up.get("hours_online", 0) or 0)
        # Uptime as a proportion of the period, clamped. A Tree cannot be up for
        # more hours than the period contains; if the oracle says otherwise the
        # season and the period disagree and we must not pay on the excess.
        capped = min(hours, period_hours)
        uptime_bp = (capped * BASIS_POINTS_FULL) // period_hours

        last_seen = _parse_ts(node.get("last_reading_at")) or _parse_ts(node.get("last_seen_at"))

        eligible, reason = True, None
        if last_seen is None:
            eligible, reason = False, "never reported a reading"
        elif last_seen < stale_before:
            age = (now - last_seen).total_seconds() / 3600.0
            eligible, reason = False, f"stale: last reading {age:.1f}h ago"
        elif uptime_bp < min_uptime_bp:
            eligible, reason = False, (
                f"below the {min_uptime_bp / 100:.2f}% floor "
                f"({uptime_bp / 100:.2f}%)")

        records.append(UptimeRecord(
            tree_id=node_id, sensor_id=sensor_id,
            wallet_address=str(wallet) if wallet else "unknown",
            uptime_bp=uptime_bp, eligible=eligible, exclusion_reason=reason,
        ))

    return CollectionResult(
        records=tuple(records), period_start=period_start, period_end=period_end,
        uptime_basis="node-hours-present",
        source=source.base, skipped=tuple(skipped),
    )
