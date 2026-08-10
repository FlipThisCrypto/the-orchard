# SPDX-License-Identifier: Apache-2.0
"""ORM models for the Oracle.

Schema (v1):

    nodes           — every Tree that has registered
    readings        — every signed POST a Tree has submitted
    uptime_hours    — per-Tree, per-UTC-hour bucket (uniqueness keyed)
    seasons         — Season metadata (created lazily on first relevant write)
    attestations    — Phase 5: filled when we publish to DataLayer

In code we use technical names (`node`, `reading`, `season`). User-facing
copy uses brand names (Tree, Harvest, Season). See README Glossary.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Node(Base):
    """A registered Tree. Primary key is the hex node_id from firmware."""
    __tablename__ = "nodes"

    node_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    signing_key_hex: Mapped[str] = mapped_column(String(64), nullable=False)

    # ADR-0003 / ADR-0007: compressed secp256r1 public key (33 bytes → 66 hex,
    # lowercase). Published in DataLayer node:<id>.pubkey so anyone can verify
    # device-signed readings without trusting the oracle. Nullable so legacy
    # Trees registered before device keys remain loadable.
    device_pubkey: Mapped[str | None] = mapped_column(String(66), nullable=True)

    wallet_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fw_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # Retirement. NULL = live. A retired Tree stops counting anywhere that
    # describes the network — /nodes, trees_registered, trees_active_24h,
    # payout — but NOTHING is deleted: its readings, uptime, attestations and
    # claims all remain, and clearing this column brings it back exactly as it
    # was.
    #
    # This exists because re-flashing a board used to mint a NEW node_id (the
    # installer erased NVS), so the oracle accumulated ghost Trees that no
    # hardware would ever claim again — six registered ids for four physical
    # boards. Deleting them would have destroyed real history and, worse,
    # left DataLayer attestations pointing at a node_id the oracle denies
    # exists. Retiring says "this is not part of the living network" without
    # ever claiming it never was.
    #
    # A timestamp rather than a boolean: when something was retired is part of
    # the answer, and NULL/NOT NULL needs no separate "is it set" convention.
    # Operator-declared location, as a COARSE geohash cell — never coordinates.
    #
    # The oracle derives a cell from a Tree's own GPS when it has one, which is
    # the better answer because it is measured. Most Trees have no GPS module,
    # so without this they are simply unplaced and cannot appear on the map at
    # all. Declaring lets an operator say where their Tree is, on their own
    # authority, wallet-signed.
    #
    # Stored at the same ~5 km precision the privacy contract promises, and
    # coarsened at the boundary: a caller may send lat/lon for convenience, but
    # the precise value is never persisted. A cell cannot express a finer
    # position than itself, so over-precision is unrepresentable rather than
    # merely forbidden.
    declared_geohash: Mapped[str | None] = mapped_column(String(12), nullable=True)
    declared_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    retired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Free text, operator-supplied. A retirement without a reason is an
    # unexplained gap in the record six months from now.
    retired_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)

    # Replay protection: highest `seq` value accepted from this Tree.
    # The firmware persists a monotonic counter in NVS and includes it
    # in every signed body; /readings rejects anything <= this value
    # when settings().require_seq is on (tracked passively when off, so
    # flipping the flag never strands an up-to-date fleet). Reset to 0
    # on re-registration (the NVS-wipe recovery path).
    # Existing DBs (until Alembic, HANDOVER T4):
    #   sqlite3 oracle.db "ALTER TABLE nodes ADD COLUMN last_seq INTEGER NOT NULL DEFAULT 0;"
    last_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Phase 6.5: Orchard Pass NFT bound to this Tree.
    #
    # `pass_nft_id` is the bech32 nft1... identifier of an Orchard Pass
    # NFT that was verified on chain as owned by `wallet_address` at
    # registration time. `pass_verified_at` is when that verification
    # ran. Both nullable so legacy registrations (no wallet provided,
    # or pre-Phase-6.5 nodes) keep working unchanged.
    #
    # When pass_nft_id is set, /register has cryptographic evidence
    # (via the MintGarden indexer at the moment of registration) that
    # the operator at wallet_address controlled that NFT. The binding
    # does NOT auto-update if the Pass is later transferred — verify
    # again on every state change you care about (Phase 7 payout
    # re-checks before sending $JUICE).
    pass_nft_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    pass_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True)

    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_reading_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    readings: Mapped[list["Reading"]] = relationship(back_populates="node", cascade="all, delete-orphan")


class Reading(Base):
    """One signed POST from a Tree."""
    __tablename__ = "readings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), ForeignKey("nodes.node_id"), index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)

    # Tree-reported fields surfaced for indexing; full payload retained in payload_json.
    tree_ts_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fw_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Payload format version (ADR-0006/T14). NULL = pre-versioning firmware.
    schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_fix: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    # Indexed: POST /readings looks up (node_id, sig_hex) to drop exact
    # replays (anti-replay / idempotent retries) — see routes/readings.py.
    sig_hex: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    node: Mapped["Node"] = relationship(back_populates="readings")


class UptimeHour(Base):
    """One row per (Tree, UTC hour). Bumping the counter is how we
    detect Trees being alive during that hour for Season uptime math.
    """
    __tablename__ = "uptime_hours"
    __table_args__ = (UniqueConstraint("node_id", "hour_utc", name="uq_uptime_node_hour"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), ForeignKey("nodes.node_id"), index=True)
    # Bitmask of which ten-minute slots of the hour saw a reading (bit 0 =
    # :00-:09 ... bit 5 = :50-:59). reading_count says HOW MUCH arrived;
    # slots_mask says whether it SPANNED the hour — a 2-minute burst of 30
    # readings sets one bit and is not an hour of sensing.
    slots_mask: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    hour_utc: Mapped[str] = mapped_column(String(13), index=True)  # "YYYY-MM-DDTHH"
    reading_count: Mapped[int] = mapped_column(Integer, default=0)


class AuditEvent(Base):
    """Append-only record of a consequential action (node register/delete, …).

    Deliberately has NO foreign key to ``nodes`` — an audit row must outlive the
    node it references (a delete cascades the node away but the record of the
    deletion must remain). ``request_id`` correlates with the observability
    request log (X-Request-ID). ``detail_json`` holds non-secret context only.
    """
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow, index=True)
    action: Mapped[str] = mapped_column(String(64), index=True)
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(String(128), default="unknown")
    request_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    detail_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class Season(Base):
    """Season metadata. v1 = UTC-day-aligned; Phase 5 swaps to Chia blocks."""
    __tablename__ = "seasons"

    season_number: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    block_height_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    block_height_end: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Attestation(Base):
    """Per-(node, season) attestation written to DataLayer (Phase 5).

    Populated by the writer (``orchard_chia.datalayer.main``) after a
    successful ``batch_update`` — the writer POSTs back to the oracle's
    /attestations endpoint with the transaction id and the bytes-hex of
    the key it wrote, so the oracle's local DB tracks what's on chain
    without the dashboard having to round-trip DataLayer for every
    render. The signed body the writer pushed to DataLayer is included
    too (``oracle_sig`` plus data_hash) so anyone with the local DB
    can cross-check the chain copy without the writer running.
    """
    __tablename__ = "attestations"
    __table_args__ = (UniqueConstraint("node_id", "season_number", name="uq_attest_node_season"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[str] = mapped_column(String(64), ForeignKey("nodes.node_id"), index=True)
    season_number: Mapped[int] = mapped_column(Integer, ForeignKey("seasons.season_number"), index=True)
    hours_online: Mapped[int] = mapped_column(Integer, nullable=False)
    data_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # 128 hex chars fits secp256r1 r||s (64 bytes); legacy HMAC used 64 hex.
    oracle_sig: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Phase 5.5: chain tracking. dl_tx_id is the Chia DataLayer batch
    # transaction id (0x...64hex); dl_key_hex is the hex-encoded
    # `attest:<node>:<season:08d>` key the writer used (the same hex a
    # third-party verifier feeds to `chia data get_value --key`).
    # Both nullable so attestations created before this column was
    # added remain queryable.
    dl_tx_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    dl_key_hex: Mapped[str | None] = mapped_column(String(256), nullable=True)
    block_height_at_write: Mapped[int | None] = mapped_column(Integer, nullable=True)
    written_to_datalayer_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Claim(Base):
    """Remote claim-code provisioning (ADR-0005, T9).

    On first boot an unprovisioned Tree announces itself — creating its Node
    row (no wallet yet) and a Claim holding the human-readable code it shows
    over serial. The operator, authenticated with their wallet in a browser,
    submits that code to bind the Tree to their wallet (+ Orchard Pass). The
    Tree polls until the claim is consumed, then starts posting.

    The code (8 chars, normalized — no dash/ambiguous letters) is the primary
    key. Codes expire and are single-use (consumed_at set once, via a guarded
    UPDATE so a claim race can't double-bind).
    """
    __tablename__ = "claims"

    claim_code: Mapped[str] = mapped_column(String(16), primary_key=True)
    node_id: Mapped[str] = mapped_column(String(64), ForeignKey("nodes.node_id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Wallet that consumed the claim (the bound owner). Null while unclaimed.
    claimed_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
