"""SQLAlchemy 2.0 ORM models. Espejo del `schema.sql`.

Mantener sincronizados manualmente al inicio; migrar a Alembic cuando
el equipo cree que el churn de esquema lo justifique.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import ARRAY, CheckConstraint, DateTime, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Specialty(Base):
    __tablename__ = "specialty"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    code: Mapped[str] = mapped_column(Text, unique=True)
    name_es: Mapped[str] = mapped_column(Text)
    name_en: Mapped[str | None] = mapped_column(Text)


class Organization(Base):
    __tablename__ = "organization"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    country: Mapped[str] = mapped_column(String(2))
    website: Mapped[str | None] = mapped_column(Text)
    domain: Mapped[str | None] = mapped_column(Text)
    phones: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)


class Source(Base):
    __tablename__ = "source"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True)
    base_url: Mapped[str | None] = mapped_column(Text)
    tier: Mapped[int]
    is_active: Mapped[bool] = mapped_column(default=True)


class Physician(Base):
    __tablename__ = "physician"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    full_name: Mapped[str] = mapped_column(Text)
    family_name_1: Mapped[str] = mapped_column(Text)
    family_name_2: Mapped[str | None] = mapped_column(Text)
    given_name: Mapped[str] = mapped_column(Text)
    specialty_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("specialty.id"))
    country: Mapped[str] = mapped_column(String(2))
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=0)
    status: Mapped[str] = mapped_column(Text, default="draft")
    assigned_to: Mapped[str | None] = mapped_column(Text)
    comment: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    claims: Mapped[list[Claim]] = relationship(back_populates="physician", cascade="all, delete-orphan")
    __table_args__ = (
        Index("physician_family_idx", "family_name_1", "family_name_2"),
    )


class Observation(Base):
    __tablename__ = "observation"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    source_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("source.id"))
    source_url: Mapped[str | None] = mapped_column(Text)
    scraped_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    extractor_version: Mapped[str] = mapped_column(Text, default="v1")
    raw_payload: Mapped[dict] = mapped_column(JSONB)
    snapshot_path: Mapped[str | None] = mapped_column(Text)
    physician_id: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("physician.id"))
    status: Mapped[str] = mapped_column(Text, default="new")


class Claim(Base):
    __tablename__ = "claim"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    physician_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), ForeignKey("physician.id", ondelete="CASCADE"))
    attribute: Mapped[str] = mapped_column(Text)
    subkind: Mapped[str | None] = mapped_column(Text)
    value: Mapped[str] = mapped_column(Text)
    value_normalized: Mapped[str] = mapped_column(Text)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    is_inferred: Mapped[bool] = mapped_column(default=False)
    inference_method: Mapped[str | None] = mapped_column(Text)
    validation_status: Mapped[str] = mapped_column(Text, default="unverified")
    sources: Mapped[list[UUID]] = mapped_column(ARRAY(PgUUID(as_uuid=True)), default=list)
    evidence_strength: Mapped[dict] = mapped_column(JSONB, default=dict)
    contradicted_by: Mapped[list[UUID]] = mapped_column(ARRAY(PgUUID(as_uuid=True)), default=list)
    explanation: Mapped[str | None] = mapped_column(Text)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[UUID | None] = mapped_column(PgUUID(as_uuid=True), ForeignKey("claim.id"))

    physician: Mapped[Physician] = relationship(back_populates="claims")

    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="claim_conf_range"),
        Index("claim_val_norm", "value_normalized"),
    )


class ReviewTask(Base):
    __tablename__ = "review_task"
    id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict] = mapped_column(JSONB)
    priority: Mapped[int] = mapped_column(default=5)
    assigned_to: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="open")
    decision: Mapped[dict | None] = mapped_column(JSONB)
    resolved_by: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
