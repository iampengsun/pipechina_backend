"""Human-maintained asset identity, durable graph changes and impact reviews."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.shared.db import Base
from app.shared.model_mixins import TimestampMixin, UUIDPrimaryKeyMixin
from app.shared.types import JSON_DOCUMENT


class Station(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "operational_stations"
    __table_args__ = (UniqueConstraint("organization_unit_id", "code"),)
    organization_unit_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization_units.id"), index=True
    )
    code: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))


class Equipment(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "operational_equipment"
    __table_args__ = (UniqueConstraint("station_id", "code"),)
    station_id: Mapped[UUID] = mapped_column(ForeignKey("operational_stations.id"), index=True)
    organization_unit_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization_units.id"), index=True
    )
    code: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    equipment_type: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))


class EquipmentAlias(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "equipment_aliases"
    __table_args__ = (UniqueConstraint("station_id", "normalized"),)
    station_id: Mapped[UUID] = mapped_column(ForeignKey("operational_stations.id"))
    equipment_id: Mapped[UUID] = mapped_column(ForeignKey("operational_equipment.id"), index=True)
    alias: Mapped[str] = mapped_column(String(200))
    normalized: Mapped[str] = mapped_column(String(200))
    reason: Mapped[str] = mapped_column(Text)
    confirmed_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))


class FailureMode(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "failure_modes"
    __table_args__ = (UniqueConstraint("organization_unit_id", "code"),)
    organization_unit_id: Mapped[UUID] = mapped_column(
        ForeignKey("organization_units.id"), index=True
    )
    code: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))


class EquipmentAssociation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "equipment_associations"
    __table_args__ = (
        UniqueConstraint("equipment_id", "record_kind", "record_id", "failure_mode_id"),
        CheckConstraint(
            "record_kind IN ('event','hazard','work_order')", name="equipment_association_kind"
        ),
        CheckConstraint("version > 0", name="equipment_association_version"),
    )
    equipment_id: Mapped[UUID] = mapped_column(ForeignKey("operational_equipment.id"), index=True)
    failure_mode_id: Mapped[UUID] = mapped_column(ForeignKey("failure_modes.id"))
    record_kind: Mapped[str] = mapped_column(String(30))
    record_id: Mapped[UUID] = mapped_column(index=True)
    source_revision: Mapped[str] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    reason: Mapped[str] = mapped_column(Text)
    confirmed_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))


class GraphChange(Base):
    __tablename__ = "business_graph_outbox"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source_table: Mapped[str] = mapped_column(String(100))
    record_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GraphProjection(Base):
    __tablename__ = "business_graph_projection"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[str] = mapped_column(String(64), default="")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    counts: Mapped[dict] = mapped_column(JSON_DOCUMENT, default=dict)


class ImpactReview(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "graph_impact_reviews"
    __table_args__ = (
        UniqueConstraint("source_version_id", "target_key", "change_signature"),
        CheckConstraint(
            "status IN ('pending','needs_action','closed','superseded')", name="graph_review_status"
        ),
        CheckConstraint(
            "change_type IN ('withdrawal','expiry','new_revision')", name="graph_review_change_type"
        ),
    )
    source_version_id: Mapped[UUID] = mapped_column(ForeignKey("knowledge_versions.id"), index=True)
    replacement_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("knowledge_versions.id"))
    change_type: Mapped[str] = mapped_column(String(30))
    change_signature: Mapped[str] = mapped_column(String(64))
    target_key: Mapped[str] = mapped_column(String(100), index=True)
    target_revision: Mapped[str] = mapped_column(String(64))
    paths: Mapped[list] = mapped_column(JSON_DOCUMENT)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    version: Mapped[int] = mapped_column(Integer, default=1)
    history: Mapped[list] = mapped_column(JSON_DOCUMENT, default=list)
    reviewed_by: Mapped[UUID | None] = mapped_column(ForeignKey("user_accounts.id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)


class StationLocation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Append-only, human-confirmed WGS84 positions, including explicit withdrawal."""

    __tablename__ = "station_locations"
    __table_args__ = (
        UniqueConstraint("station_id", "version", name="uq_station_location_version"),
        CheckConstraint("version > 0", name="station_location_version"),
        CheckConstraint(
            "latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180",
            name="station_location_bounds",
        ),
        CheckConstraint(
            "NOT active OR (latitude IS NOT NULL AND longitude IS NOT NULL)",
            name="station_location_coordinates",
        ),
    )
    station_id: Mapped[UUID] = mapped_column(ForeignKey("operational_stations.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    address: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(500))
    reason: Mapped[str] = mapped_column(Text)
    created_by: Mapped[UUID] = mapped_column(ForeignKey("user_accounts.id"))
