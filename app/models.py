"""SQLAlchemy models.

Design invariants enforced at the database level:
  * usage_records / bills / bill_lines / price_versions / price_tiers are
    append-only (BEFORE UPDATE OR DELETE triggers installed in db.init_db).
  * (tenant_id, source, event_id) is unique  -> a source event is booked once.
  * (tenant_id, recv_seq) is unique          -> one deterministic receive order.
  * price_versions of one plan never overlap (GiST exclusion constraint).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
    column,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, ExcludeConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256))
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("price_plans.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    plan: Mapped["PricePlan | None"] = relationship()


class TenantCounter(Base):
    """Per-tenant receive sequence. Exactly one row per tenant; the row lock
    serializes ingestion against period close so every record lands on a
    deterministic side of the cutoff."""

    __tablename__ = "tenant_counters"

    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), primary_key=True)
    last_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class UsageRecord(Base):
    """Append-only usage ledger. Corrections and reversals are new rows linked
    to the root event via corrects_id; the original row is never modified."""

    __tablename__ = "usage_records"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    source: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(128))
    record_type: Mapped[str] = mapped_column(String(16))  # event | correction | reversal
    corrects_id: Mapped[int | None] = mapped_column(ForeignKey("usage_records.id"))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(28, 6))
    metric: Mapped[str | None] = mapped_column(String(64))
    dimensions: Mapped[dict | None] = mapped_column(JSONB)
    recv_seq: Mapped[int] = mapped_column(BigInteger)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "event_id", name="uq_usage_source_event"),
        UniqueConstraint("tenant_id", "recv_seq", name="uq_usage_recv_seq"),
        Index("ix_usage_tenant_recv", "tenant_id", "recv_seq"),
    )


class PricePlan(Base):
    __tablename__ = "price_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(3))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    versions: Mapped[list["PriceVersion"]] = relationship(back_populates="plan")


class PriceVersion(Base):
    """Versioned price table, effective over [effective_from, effective_to).
    Ranges of one plan must not overlap (exclusion constraint + app check)."""

    __tablename__ = "price_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("price_plans.id"), index=True)
    version: Mapped[int] = mapped_column()
    effective_from: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    plan: Mapped[PricePlan] = relationship(back_populates="versions")
    tiers: Mapped[list["PriceTier"]] = relationship(back_populates="version")

    __table_args__ = (
        UniqueConstraint("plan_id", "version", name="uq_price_version"),
        ExcludeConstraint(
            (column("plan_id"), "="),
            (
                func.tstzrange(
                    column("effective_from"), column("effective_to"), text("'[)'")
                ),
                "&&",
            ),
            name="excl_price_versions_no_overlap",
            using="gist",
        ),
    )


class PriceTier(Base):
    """Graduated tier: quantities in (prev up_to, up_to] are charged unit_price.
    The tier with up_to = NULL is the unbounded final bracket."""

    __tablename__ = "price_tiers"

    id: Mapped[int] = mapped_column(primary_key=True)
    version_id: Mapped[int] = mapped_column(ForeignKey("price_versions.id"), index=True)
    metric: Mapped[str] = mapped_column(String(64))
    tier_order: Mapped[int] = mapped_column()
    up_to: Mapped[Decimal | None] = mapped_column(Numeric(28, 6))
    unit_price: Mapped[Decimal] = mapped_column(Numeric(20, 6))

    version: Mapped[PriceVersion] = relationship(back_populates="tiers")

    __table_args__ = (
        UniqueConstraint("version_id", "metric", "tier_order", name="uq_price_tier"),
    )


class BillingPeriod(Base):
    __tablename__ = "billing_periods"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | closed
    cutoff_seq: Mapped[int | None] = mapped_column(BigInteger)
    price_version_id: Mapped[int | None] = mapped_column(ForeignKey("price_versions.id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("tenant_id", "period_start", name="uq_period_start"),
    )


class Bill(Base):
    """Immutable finalized bill. total_amount is the exact sum of its lines."""

    __tablename__ = "bills"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), index=True)
    period_id: Mapped[int] = mapped_column(ForeignKey("billing_periods.id"), unique=True)
    currency: Mapped[str] = mapped_column(String(3))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    cutoff_seq: Mapped[int] = mapped_column(BigInteger)
    price_version_id: Mapped[int] = mapped_column(ForeignKey("price_versions.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    lines: Mapped[list["BillLine"]] = relationship(order_by="BillLine.id")


class BillLine(Base):
    """One priced event. line_kind='current' bills the bill's own period;
    line_kind='adjustment' carries a late correction/reversal for the period
    referenced by origin_period_id. trace holds the full pricing derivation."""

    __tablename__ = "bill_lines"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), index=True)
    line_kind: Mapped[str] = mapped_column(String(16))  # current | adjustment
    origin_period_id: Mapped[int] = mapped_column(ForeignKey("billing_periods.id"))
    root_record_id: Mapped[int] = mapped_column(ForeignKey("usage_records.id"))
    source: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(String(128))
    metric: Mapped[str] = mapped_column(String(64))
    quantity: Mapped[Decimal] = mapped_column(Numeric(28, 6))
    amount: Mapped[Decimal] = mapped_column(Numeric(20, 2))
    trace: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        Index("ix_bill_lines_origin_root", "origin_period_id", "root_record_id"),
    )
