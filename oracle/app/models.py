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

    wallet_address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fw_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

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
    hour_utc: Mapped[str] = mapped_column(String(13), index=True)  # "YYYY-MM-DDTHH"
    reading_count: Mapped[int] = mapped_column(Integer, default=0)


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
