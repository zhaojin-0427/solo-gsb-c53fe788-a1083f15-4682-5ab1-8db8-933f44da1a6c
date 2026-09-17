"""Pydantic request/response schemas. All money and quantities are Decimal;
Pydantic v2 serializes Decimal as a JSON string, so no float ever leaks."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def _ensure_tz(v: datetime) -> datetime:
    """Naive timestamps are interpreted as UTC."""
    if v.tzinfo is None:
        return v.replace(tzinfo=timezone.utc)
    return v


def _quantity(v: Decimal) -> Decimal:
    if v < 0:
        raise ValueError("quantity must be >= 0 (use a reversal to void usage)")
    if v.as_tuple().exponent < -6:
        raise ValueError("quantity supports at most 6 decimal places")
    # normalize to the storage scale so API responses are consistent
    return v.quantize(Decimal("0.000001"))


def _unit_price(v: Decimal) -> Decimal:
    if v < 0:
        raise ValueError("unit_price must be >= 0")
    if v.as_tuple().exponent < -6:
        raise ValueError("unit_price supports at most 6 decimal places")
    return v


AwareDateTime = Annotated[datetime, AfterValidator(_ensure_tz)]
Quantity = Annotated[Decimal, AfterValidator(_quantity)]
UnitPrice = Annotated[Decimal, AfterValidator(_unit_price)]


# ---------------------------------------------------------------- tenants
class TenantCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str = Field(min_length=1, max_length=256)
    plan_id: int | None = None


class TenantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    code: str
    name: str
    plan_id: int | None
    created_at: datetime


class AssignPlan(BaseModel):
    plan_id: int


# ---------------------------------------------------------------- pricing
class PlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")


class PlanOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    currency: str
    created_at: datetime


class TierIn(BaseModel):
    metric: str = Field(min_length=1, max_length=64)
    up_to: Decimal | None = Field(
        default=None, description="cumulative bracket boundary; null = unbounded final tier"
    )
    unit_price: UnitPrice


class PriceVersionCreate(BaseModel):
    version: int = Field(ge=1)
    effective_from: AwareDateTime
    effective_to: AwareDateTime | None = None
    tiers: list[TierIn] = Field(min_length=1)


class TierOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    metric: str
    tier_order: int
    up_to: Decimal | None
    unit_price: Decimal


class PriceVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    plan_id: int
    version: int
    effective_from: datetime
    effective_to: datetime | None
    tiers: list[TierOut]


# ---------------------------------------------------------------- usage
class UsageIngest(BaseModel):
    tenant_code: str
    source: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=128)
    occurred_at: AwareDateTime
    quantity: Quantity
    metric: str = Field(min_length=1, max_length=64)
    dimensions: dict[str, Any] = Field(default_factory=dict)


class CorrectionIn(BaseModel):
    tenant_code: str
    occurred_at: AwareDateTime
    quantity: Quantity
    metric: str = Field(min_length=1, max_length=64)
    dimensions: dict[str, Any] = Field(default_factory=dict)
    correction_event_id: str | None = Field(
        default=None,
        description="idempotency key of the correction itself; generated if omitted",
    )


class ReversalIn(BaseModel):
    tenant_code: str
    reason: str | None = None
    reversal_event_id: str | None = Field(
        default=None,
        description="idempotency key of the reversal itself; generated if omitted",
    )


class UsageRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    source: str
    event_id: str
    record_type: str
    corrects_id: int | None
    occurred_at: datetime | None
    quantity: Decimal | None
    metric: str | None
    dimensions: dict[str, Any] | None
    recv_seq: int
    received_at: datetime


class IngestResult(BaseModel):
    record: UsageRecordOut
    created: bool
    deduplicated: bool


def record_dict(rec) -> dict[str, Any]:
    """JSON-safe snapshot of a UsageRecord (used in 409 conflict payloads)."""
    return {
        "id": rec.id,
        "source": rec.source,
        "event_id": rec.event_id,
        "record_type": rec.record_type,
        "corrects_id": rec.corrects_id,
        "occurred_at": rec.occurred_at.isoformat() if rec.occurred_at else None,
        "quantity": str(rec.quantity) if rec.quantity is not None else None,
        "metric": rec.metric,
        "dimensions": rec.dimensions or {},
        "recv_seq": rec.recv_seq,
    }


# ---------------------------------------------------------------- periods
class PeriodCreate(BaseModel):
    tenant_code: str
    period_start: AwareDateTime
    period_end: AwareDateTime


class PeriodOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    period_start: datetime
    period_end: datetime
    status: str
    cutoff_seq: int | None
    price_version_id: int | None
    closed_at: datetime | None


# ---------------------------------------------------------------- bills
class BillLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    line_kind: str
    origin_period_id: int
    root_record_id: int
    source: str
    event_id: str
    metric: str
    quantity: Decimal
    amount: Decimal
    trace: dict[str, Any]


class BillOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    period_id: int
    currency: str
    total_amount: Decimal
    cutoff_seq: int
    price_version_id: int
    created_at: datetime
    lines: list[BillLineOut] = []


class BillSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    tenant_id: int
    period_id: int
    currency: str
    total_amount: Decimal
    cutoff_seq: int
    price_version_id: int
    created_at: datetime


class PreviewLine(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    line_kind: str
    origin_period_id: int
    root_record_id: int
    source: str
    event_id: str
    metric: str
    quantity: Decimal
    amount: Decimal
    trace: dict[str, Any]


class PreviewOut(BaseModel):
    period_id: int
    status: str
    hypothetical_cutoff_seq: int
    price_version_id: int
    currency: str
    total_amount: Decimal
    lines: list[PreviewLine]
    note: str


class EventTrace(BaseModel):
    source: str
    event_id: str
    line_kind: str
    origin_period_id: int
    metric: str
    quantity: Decimal
    amount: Decimal
    trace: dict[str, Any]


class TraceOut(BaseModel):
    bill_id: int
    period_id: int
    cutoff_seq: int
    price_version_id: int
    currency: str
    events: list[EventTrace]


class VerifyOut(BaseModel):
    bill_id: int
    ok: bool
    stored_total: Decimal
    recomputed_total: Decimal
    stored_lines: int
    recomputed_lines: int
    mismatches: list[dict[str, Any]]
