# SPDX-License-Identifier: Apache-2.0
"""Uptime queries.

Per Season, count distinct hour buckets a Tree submitted at least one
reading in. v1 Seasons are UTC-day-aligned (00:00Z to next 00:00Z) —
see seasons.py for the rationale and the v2 swap path.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import models, seasons
from ..db import get_db
from ..uptime_calc import hours_online_for

router = APIRouter()


class UptimeResponse(BaseModel):
    node_id: str
    season: int
    season_start_utc: datetime
    season_end_utc: datetime
    hours_online: int
    hour_buckets: list[str]


@router.get("/uptime/{node_id}/{season}", response_model=UptimeResponse)
def uptime_for_season(node_id: str, season: int, db: Session = Depends(get_db)) -> UptimeResponse:
    node_id = node_id.upper()
    if db.get(models.Node, node_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown node_id")
    if season < 1:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="season must be >= 1")

    start, end = seasons.season_bounds(season)
    _, hit_buckets = hours_online_for(db, node_id, season)
    return UptimeResponse(
        node_id=node_id,
        season=season,
        season_start_utc=start,
        season_end_utc=end,
        hours_online=len(hit_buckets),
        hour_buckets=hit_buckets,
    )
