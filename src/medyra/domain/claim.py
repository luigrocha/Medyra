"""Entidades de dominio puro. Sin I/O, sin SQLAlchemy."""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Attribute(StrEnum):
    EMAIL = "email"
    PHONE = "phone"
    SPECIALTY = "specialty"
    AFFILIATION = "affiliation"
    SOCIAL = "social"


class ValidationStatus(StrEnum):
    UNVERIFIED = "unverified"
    PROBABLE = "probable"
    VERIFIED = "verified"
    REJECTED = "rejected"


class Claim(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    physician_id: UUID
    attribute: Attribute
    subkind: str | None = None
    value: str
    value_normalized: str
    confidence: float = Field(ge=0.0, le=1.0)
    is_inferred: bool = False
    inference_method: str | None = None
    validation_status: ValidationStatus = ValidationStatus.UNVERIFIED
    sources: list[UUID] = Field(default_factory=list)
    evidence_strength: dict = Field(default_factory=dict)
    contradicted_by: list[UUID] = Field(default_factory=list)
    explanation: str | None = None
    first_seen_at: datetime = Field(default_factory=datetime.utcnow)
    last_verified_at: datetime | None = None
    superseded_by: UUID | None = None


class PhysicianCore(BaseModel):
    """Fields que viajan entre capas; no es la entidad ORM."""
    id: UUID = Field(default_factory=uuid4)
    full_name: str
    family_name_1: str
    family_name_2: str | None = None
    given_name: str
    country: Literal["CR", "PA", "MX", "CO", "PE", "CL", "AR", "GT", "HN", "SV", "NI", "DO", "EC", "UY", "PY", "VE", "BO", "BR"]
    specialty_code: str | None = None
    assigned_to: str | None = None
    comment: str | None = None
