# SPDX-License-Identifier: Apache-2.0
"""Reading submission + retrieval.

POST /readings is the hottest path — the Tree calls it every sample
interval. It's also the security boundary: nobody but the Tree's
registered HMAC secret can submit a valid reading.

Authentication:
    Header X-Orchard-Node: <hex node_id>
    Header X-Orchard-Sig:  <hex HMAC-SHA256 of raw body>

The raw body is the JSON the Tree firmware produced; we never reparse
and re-serialize before checking the signature (that would change the
bytes and break HMAC).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Header, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .. import auth, models, seasons, sessions
from ..config import settings
from ..db import get_db
from ..session_deps import maybe_session

router = APIRouter()


class ReadingResponse(BaseModel):
    id: int
    node_id: str
    received_at: datetime
    tree_ts_ms: int | None
    fw_version: str | None
    gps_lat: float | None
    gps_lon: float | None
    gps_fix: bool | None
    payload: dict


def _bump_uptime_hour(db: Session, node_id: str, when: datetime) -> None:
    bucket = seasons.hour_bucket_for(when)
    row = (
        db.execute(
            select(models.UptimeHour).where(
                models.UptimeHour.node_id == node_id,
                models.UptimeHour.hour_utc == bucket,
            )
        )
        .scalar_one_or_none()
    )
    if row is None:
        row = models.UptimeHour(node_id=node_id, hour_utc=bucket, reading_count=1)
        db.add(row)
    else:
        row.reading_count += 1


@router.post("/readings", status_code=status.HTTP_202_ACCEPTED)
async def post_reading(
    request: Request,
    x_orchard_node: str | None = Header(default=None, alias="X-Orchard-Node"),
    x_orchard_sig: str | None = Header(default=None, alias="X-Orchard-Sig"),
    db: Session = Depends(get_db),
) -> dict:
    body_bytes = await request.body()

    try:
        node = auth.verify_reading_sig(db, x_orchard_node, x_orchard_sig, body_bytes)
    except auth.SignatureError as e:
        # Distinguish "unknown node" (404) from "bad signature" (401) for
        # operator clarity; both leak some info but the data path is local.
        msg = str(e)
        code = status.HTTP_404_NOT_FOUND if "unregistered" in msg else status.HTTP_401_UNAUTHORIZED
        raise HTTPException(status_code=code, detail=msg)

    # Anti-replay: an exact replay re-sends the identical body, so the
    # HMAC signature is identical. Drop duplicates WITHOUT inserting or
    # bumping uptime — otherwise a captured reading could be replayed to
    # inflate Season uptime (and therefore $JUICE). This also makes a
    # client's network-retry of the same POST idempotent.
    sig_hex_upper = x_orchard_sig.upper() if x_orchard_sig else ""
    if sig_hex_upper:
        dup = db.execute(
            select(models.Reading.id, models.Reading.received_at)
            .where(
                models.Reading.node_id == node.node_id,
                models.Reading.sig_hex == sig_hex_upper,
            )
            .limit(1)
        ).first()
        if dup is not None:
            return {
                "id": dup[0],
                "received_at": dup[1].isoformat() if dup[1] else None,
                "season": seasons.current_season(),
                "duplicate": True,
            }

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid JSON: {e}")

    # ---- Replay protection (docs/replay-protection.md, T3) ----------
    # `seq` lives inside the HMAC-covered body, so it can't be forged
    # or bumped on a captured packet. Strictly increasing per Tree.
    # Exact duplicates never reach here (the sig-dedup above returns
    # them as idempotent 202s), so a 409 here means a *distinct* stale
    # or out-of-order body — i.e. a captured-and-held replay.
    #
    # The bump is a GUARDED UPDATE (`... WHERE last_seq < :seq`) rather
    # than a read-compare-set, so it stays correct with multiple workers
    # on Postgres: the row lock serializes concurrent posts and only the
    # strictly-greater seq wins. rowcount==0 means "not greater" — a
    # stale/duplicate seq. (On SQLite the single writer makes it moot,
    # but the same code path works there too.)
    seq = payload.get("seq") if isinstance(payload, dict) else None
    seq_valid = isinstance(seq, int) and not isinstance(seq, bool) and seq > 0
    if settings().require_seq and not seq_valid:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing or invalid 'seq' (firmware too old? reflash)",
        )
    if seq_valid:
        last_accepted = node.last_seq
        advanced = db.execute(
            update(models.Node)
            .where(models.Node.node_id == node.node_id, models.Node.last_seq < seq)
            .values(last_seq=seq)
        ).rowcount
        if settings().require_seq and not advanced:
            # Signature was VALID; the content is just stale -> 409,
            # no uptime credit, no stored row (the failed UPDATE and
            # everything after roll back when we raise).
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"replayed or out-of-order seq {seq} (last accepted {last_accepted})",
            )
    # ------------------------------------------------------------------

    now = datetime.now(timezone.utc)

    # ---- Reading freshness (D6/T6) ----------------------------------
    # Optional data-quality guard: reject a reading whose signed UTC `ts`
    # (epoch seconds, set once the Tree's SNTP clock syncs) is older than
    # max_reading_age_seconds. Only enforced when a ts is present, so
    # pre-SNTP firmware (no ts) is never rejected by it. Off by default.
    max_age = settings().max_reading_age_seconds
    if max_age > 0:
        ts = payload.get("ts") if isinstance(payload, dict) else None
        if isinstance(ts, int) and not isinstance(ts, bool) and ts > 0:
            age = int(now.timestamp()) - ts
            if age > max_age:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=f"stale reading: ts {ts} is {age}s old (max {max_age}s)",
                )
    # ------------------------------------------------------------------

    sensors_obj = payload.get("sensors", {}) if isinstance(payload, dict) else {}
    gps = sensors_obj.get("gps", {}) if isinstance(sensors_obj, dict) else {}

    reading = models.Reading(
        node_id=node.node_id,
        received_at=now,
        tree_ts_ms=payload.get("ts_ms") if isinstance(payload, dict) else None,
        fw_version=payload.get("fw") if isinstance(payload, dict) else None,
        gps_lat=gps.get("lat") if isinstance(gps, dict) else None,
        gps_lon=gps.get("lon") if isinstance(gps, dict) else None,
        gps_fix=gps.get("fix") if isinstance(gps, dict) else None,
        payload_json=body_bytes.decode("utf-8"),
        sig_hex=sig_hex_upper,
    )
    db.add(reading)

    # Touch Node state.
    node.last_reading_at = now
    node.last_seen_at = now
    if reading.fw_version:
        node.fw_version = reading.fw_version

    _bump_uptime_hour(db, node.node_id, now)

    db.commit()
    db.refresh(reading)
    return {"id": reading.id, "received_at": reading.received_at.isoformat(),
            "season": seasons.current_season()}


def _scrub_payload_gps(payload: dict) -> dict:
    """Strip precise lat/lon from a reading payload (keep fix/sats).
    Returns a copy; leaves non-dict / GPS-less payloads untouched."""
    if not isinstance(payload, dict):
        return payload
    sensors = payload.get("sensors")
    if not isinstance(sensors, dict):
        return payload
    gps = sensors.get("gps")
    if not isinstance(gps, dict):
        return payload
    clean_gps = {k: v for k, v in gps.items() if k not in ("lat", "lon")}
    return {**payload, "sensors": {**sensors, "gps": clean_gps}}


@router.get("/readings/{node_id}", response_model=list[ReadingResponse])
def list_readings(
    node_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
    sess: sessions.Session | None = Depends(maybe_session),
) -> list[ReadingResponse]:
    node_id = node_id.upper()
    node = db.get(models.Node, node_id)
    if node is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="unknown node_id")

    # Precise GPS locates the operator's home, so it is operator-private:
    # return coords only to the owner (session wallet == node wallet), or
    # for legacy/unowned nodes with no wallet bound. Everyone else —
    # including a LAN peer hitting the oracle directly — gets fix/sats but
    # not lat/lon. (Uptime + attestations stay public by design; they're
    # the verifiable data, and they carry no location.)
    owner = node.wallet_address is None or (
        sess is not None and sess.address == node.wallet_address
    )

    rows = (
        db.execute(
            select(models.Reading)
            .where(models.Reading.node_id == node_id)
            .order_by(models.Reading.received_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )
    out: list[ReadingResponse] = []
    for r in rows:
        try:
            payload = json.loads(r.payload_json)
        except Exception:
            payload = {}
        if not owner:
            payload = _scrub_payload_gps(payload)
        out.append(
            ReadingResponse(
                id=r.id,
                node_id=r.node_id,
                received_at=r.received_at,
                tree_ts_ms=r.tree_ts_ms,
                fw_version=r.fw_version,
                gps_lat=r.gps_lat if owner else None,
                gps_lon=r.gps_lon if owner else None,
                gps_fix=r.gps_fix,
                payload=payload,
            )
        )
    return out
