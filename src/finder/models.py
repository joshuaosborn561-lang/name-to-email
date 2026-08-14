from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON, Uuid


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


# JSONB on Postgres, JSON elsewhere.
JSONType = JSON().with_variant(JSONB, "postgresql")


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    cost_ceiling: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    stats: Mapped[dict] = mapped_column(JSONType, default=dict)
    source: Mapped[str] = mapped_column(String(255), default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    people: Mapped[list["Person"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class Person(Base):
    __tablename__ = "people"
    __table_args__ = (
        Index("ix_people_run_domain", "run_id", "norm_domain"),
        Index("ix_people_run_status", "run_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    first: Mapped[str] = mapped_column(String(255), default="")
    last: Mapped[str] = mapped_column(String(255), default="")
    domain: Mapped[str] = mapped_column(String(255), default="")
    norm_first: Mapped[str] = mapped_column(String(255), default="")
    norm_last: Mapped[str] = mapped_column(String(255), default="")
    norm_domain: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    pattern_used: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    verifier: Mapped[str | None] = mapped_column(String(64), nullable=True)
    domain_is_catchall: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[str | None] = mapped_column(String(16), nullable=True)
    passthrough: Mapped[dict] = mapped_column(JSONType, default=dict)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0)
    pattern_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sighted: Mapped[bool] = mapped_column(Boolean, default=False)
    hunter_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)

    run: Mapped[Run] = relationship(back_populates="people")


class DomainPattern(Base):
    __tablename__ = "domain_patterns"

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    pattern: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[int] = mapped_column(Integer, default=0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    is_catchall: Mapped[bool] = mapped_column(Boolean, default=False)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    catchall_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class DomainEmailPattern(Base):
    """Permanent Hunter domain pattern cache, local mirror of the Supabase table."""

    __tablename__ = "domain_email_patterns"

    domain: Mapped[str] = mapped_column(String(255), primary_key=True)
    pattern: Mapped[str | None] = mapped_column(String(64), nullable=True)
    organization: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sighted_emails: Mapped[list] = mapped_column(JSONType, default=list)
    accept_all: Mapped[bool] = mapped_column(Boolean, default=False)
    webmail: Mapped[bool] = mapped_column(Boolean, default=False)
    hunter_confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source: Mapped[str] = mapped_column(String(32), default="hunter")


class Verification(Base):
    __tablename__ = "verifications"

    email: Mapped[str] = mapped_column(String(320), primary_key=True)
    verdict: Mapped[str] = mapped_column(String(32), index=True)
    verifier: Mapped[str] = mapped_column(String(64))
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 6), default=Decimal("0"))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    raw: Mapped[dict | None] = mapped_column(JSONType, nullable=True)
