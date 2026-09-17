"""Pydantic 请求/响应模型。

Decimal 在响应中序列化为字符串以保证跨语言精度无损；
客户端提交时既可传字符串（推荐）也可传 JSON 数字。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field
from pydantic.functional_serializers import PlainSerializer
from typing_extensions import Annotated

DecimalStr = Annotated[
    Decimal,
    PlainSerializer(lambda v: format(v, "f"), return_type=str, when_used="json"),
]
TimestampStr = Annotated[
    datetime,
    PlainSerializer(lambda v: v.isoformat(), return_type=str, when_used="json"),
]


# ---------------------------------------------------------------- tenants ----
class TenantIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantOut(BaseModel):
    id: str
    name: str
    created_at: TimestampStr


# ----------------------------------------------------------------- events ----
class EventIn(BaseModel):
    # 业务侧全局幂等键由 (source, event_id) 组成
    source: str = Field(min_length=1, max_length=200)
    event_id: str = Field(min_length=1, max_length=200)
    event_type: Literal["usage", "correction", "cancellation"] = "usage"
    occurred_at: datetime
    # correction 必填，指向被修正/撤销的账内事件；cancellation 同样必填
    linked_event_id: str | None = None
    # usage/correction 给出新的累计用量；cancellation 忽略该字段（差值为 -旧累计）
    quantity: DecimalStr | None = None
    dimensions: dict[str, Any] = Field(default_factory=dict)


class EventOut(BaseModel):
    id: str
    source: str
    event_id: str
    event_type: Literal["usage", "correction", "cancellation"]
    occurred_at: TimestampStr
    received_at: TimestampStr
    recv_seq: int
    quantity: DecimalStr
    dimensions: dict[str, Any]
    linked_event_id: str | None
    root_event_id: str
    idempotent: bool = False  # True 表示本次请求命中了已有记录（重复提交）


# ------------------------------------------------------------- price list ----
class TierIn(BaseModel):
    tier_index: int = Field(ge=0)
    from_qty: DecimalStr = Field(ge=0)
    up_to_qty: DecimalStr | None = Field(default=None, ge=0)
    unit_amount: DecimalStr
    flat_amount: DecimalStr = Decimal("0")


class PriceVersionIn(BaseModel):
    # None = 该 source 的默认价格；否则为租户专属价格
    tenant_id: str | None = None
    source: str = Field(min_length=1, max_length=200)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    pricing_mode: Literal["volume", "graduated"] = "volume"
    effective_from: datetime
    effective_to: datetime | None = None
    tiers: list[TierIn] = Field(min_length=1)


class TierOut(TierIn):
    id: str


class PriceVersionOut(BaseModel):
    id: str
    tenant_id: str | None
    source: str
    currency: str
    pricing_mode: Literal["volume", "graduated"]
    effective_from: TimestampStr
    effective_to: TimestampStr | None
    tiers: list[TierOut]


# ---------------------------------------------------------------- periods ----
class PeriodIn(BaseModel):
    period_start: datetime
    period_end: datetime


class PeriodOut(BaseModel):
    id: str
    tenant_id: str
    period_start: TimestampStr
    period_end: TimestampStr
    status: Literal["open", "closed"]
    cutoff_recv_seq: int | None
    closed_at: TimestampStr | None
    bill_id: str | None


# ------------------------------------------------------------------ bills ----
class BillLineOut(BaseModel):
    id: str
    event_id: str
    source: str
    event_id_business: str
    event_type: str
    occurred_at: TimestampStr
    recv_seq: int
    line_kind: Literal["usage", "adjustment"]
    period_attribution: Literal["current", "late", "correction"]
    price_version_id: str
    quantity: DecimalStr
    amount: DecimalStr
    pricing_trace: dict[str, Any]


class BillOut(BaseModel):
    id: str
    tenant_id: str
    period_id: str
    currency: str
    cutoff_recv_seq: int
    total_amount: DecimalStr
    generated_at: TimestampStr
    lines: list[BillLineOut] = Field(default_factory=list)


class ReconcileOut(BaseModel):
    bill_id: str
    matches: bool
    stored_total_amount: DecimalStr
    recomputed_total_amount: DecimalStr
    cutoff_recv_seq: int
    line_count: int
    mismatches: list[dict[str, Any]] = Field(default_factory=list)
    note: str


class TrialLineOut(BaseModel):
    id: str | None = None
    event_id: str
    source: str | None = None
    event_id_business: str | None = None
    event_type: str | None = None
    occurred_at: TimestampStr | None = None
    recv_seq: int | None = None
    line_kind: Literal["usage", "adjustment"]
    period_attribution: Literal["current", "late", "correction"]
    price_version_id: str
    quantity: DecimalStr
    amount: DecimalStr
    pricing_trace: dict[str, Any]


class TrialOut(BaseModel):
    period_id: str
    period_start: TimestampStr
    period_end: TimestampStr
    status: Literal["open", "closed"]
    cutoff_recv_seq: int
    currency: str | None = None
    total_amount: DecimalStr
    unpriceable_events: list[dict[str, Any]] = Field(default_factory=list)
    already_closed: bool = False
    lines: list[TrialLineOut] = Field(default_factory=list)


class CloseResultOut(BaseModel):
    period: PeriodOut
    bill: BillOut
