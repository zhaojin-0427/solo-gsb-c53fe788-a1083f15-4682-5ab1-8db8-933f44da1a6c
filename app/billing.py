"""Settlement engine: period close (transactional, with a frozen receive
cutoff), dry-run preview, and bill re-verification from frozen inputs.

Reproducibility contract: a bill depends only on
  * the append-only ledger rows with recv_seq <= bill.cutoff_seq,
  * the frozen price version bill.price_version_id,
  * the period window [period_start, period_end),
  * adjustment baselines = lines of bills with id < bill.id.
All of these are immutable, so verify_bill() always reproduces the same lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session

from .errors import Conflict, NotFound, Unprocessable
from .ledger import EffectiveEvent, effective_events
from .models import (
    Bill,
    BillLine,
    BillingPeriod,
    PriceTier,
    PriceVersion,
    Tenant,
    UsageRecord,
)
from .pricing import ZERO, Tier, allocate

ZERO2 = Decimal("0.00")


@dataclass
class LineSpec:
    line_kind: str  # current | adjustment
    origin_period_id: int
    root_record_id: int
    source: str
    event_id: str
    metric: str
    quantity: Decimal
    amount: Decimal
    trace: dict[str, Any]


# ------------------------------------------------------------------ helpers
def lock_and_read_seq(session: Session, tenant_id: int) -> int:
    """Freeze the receive cutoff: read the tenant counter under a row lock.
    Blocks until in-flight ingestion transactions commit/rollback, so every
    record ends up with recv_seq <= cutoff (billed now) or > cutoff (next
    period) — never ambiguous."""
    session.execute(
        text(
            "INSERT INTO tenant_counters (tenant_id, last_seq) VALUES (:t, 0) "
            "ON CONFLICT (tenant_id) DO NOTHING"
        ),
        {"t": tenant_id},
    )
    return session.execute(
        text("SELECT last_seq FROM tenant_counters WHERE tenant_id = :t FOR UPDATE"),
        {"t": tenant_id},
    ).scalar_one()


def current_seq(session: Session, tenant_id: int) -> int:
    row = session.execute(
        text("SELECT last_seq FROM tenant_counters WHERE tenant_id = :t"),
        {"t": tenant_id},
    ).first()
    return row[0] if row else 0


def resolve_price_version(session: Session, plan_id: int, at: datetime) -> PriceVersion | None:
    """The version effective at `at` (a period is priced by the version
    covering its start, then frozen on the bill)."""
    return session.scalars(
        select(PriceVersion)
        .where(
            PriceVersion.plan_id == plan_id,
            PriceVersion.effective_from <= at,
            or_(PriceVersion.effective_to.is_(None), PriceVersion.effective_to > at),
        )
        .order_by(PriceVersion.effective_from.desc())
        .limit(1)
    ).first()


def _tiers_by_metric(session: Session, version_id: int) -> dict[str, list[Tier]]:
    rows = session.scalars(
        select(PriceTier)
        .where(PriceTier.version_id == version_id)
        .order_by(PriceTier.metric, PriceTier.tier_order)
    ).all()
    out: dict[str, list[Tier]] = {}
    for r in rows:
        out.setdefault(r.metric, []).append(Tier(up_to=r.up_to, unit_price=r.unit_price))
    return out


def _allocate_events(
    events: list[EffectiveEvent], tiers_by_metric: dict[str, list[Tier]]
) -> dict[int, tuple[Decimal, dict[str, Any]]]:
    """root_id -> (amount, trace). Unpriced metrics yield 0-amount lines with
    an explicit trace flag instead of failing the whole close."""
    by_metric: dict[str, list[EffectiveEvent]] = {}
    for ev in events:
        by_metric.setdefault(ev.metric, []).append(ev)

    out: dict[int, tuple[Decimal, dict[str, Any]]] = {}
    for metric, evs in by_metric.items():
        tiers = tiers_by_metric.get(metric)
        if tiers is None:
            for ev in evs:
                out[ev.root_id] = (
                    ZERO2,
                    {
                        "unpriced_metric": metric,
                        "quantity": str(ev.quantity),
                        "state_record_id": ev.state_record_id,
                    },
                )
            continue
        by_id = {ev.root_id: ev for ev in evs}
        allocs = allocate(
            [(ev.root_id, (ev.occurred_at, ev.root_recv_seq, ev.root_id), ev.quantity) for ev in evs],
            tiers,
        )
        for a in allocs:
            ev = by_id[a.key]
            trace = {
                "metric": metric,
                "quantity": str(a.quantity),
                "state_record_id": ev.state_record_id,
                "brackets": a.brackets,
                "amount": str(a.amount),
            }
            if a.rounding_residual != ZERO:
                trace["rounding_residual"] = str(a.rounding_residual)
            out[a.key] = (a.amount, trace)
    return out


def _billed_by_root(session: Session, origin_period_id: int,
                    before_bill_id: int | None) -> dict[int, Decimal]:
    """Amounts already billed for an origin period, per event root.
    `before_bill_id` restricts to bills created before that bill, which is
    what makes verify_bill() see exactly the baseline the close saw."""
    stmt = (
        select(BillLine.root_record_id, func.coalesce(func.sum(BillLine.amount), 0))
        .join(Bill, Bill.id == BillLine.bill_id)
        .where(BillLine.origin_period_id == origin_period_id)
        .group_by(BillLine.root_record_id)
    )
    if before_bill_id is not None:
        stmt = stmt.where(Bill.id < before_bill_id)
    return {root: amount for root, amount in session.execute(stmt).all()}


# ------------------------------------------------------------------ compute
def compute_lines(
    session: Session,
    tenant: Tenant,
    period: BillingPeriod,
    cutoff_seq: int,
    version: PriceVersion,
    baseline_before_bill_id: int | None = None,
) -> list[LineSpec]:
    """All bill lines for `period` as of `cutoff_seq`, priced by `version`:
    current-period charges plus retro adjustments for earlier closed periods."""
    lines: list[LineSpec] = []

    # 1) current period: occurred in window AND received at/before the cutoff
    events = effective_events(session, tenant.id, period.period_start, period.period_end, cutoff_seq)
    alloc = _allocate_events(events, _tiers_by_metric(session, version.id))
    for ev in sorted(events, key=lambda e: (e.occurred_at, e.root_recv_seq, e.root_id)):
        amount, trace = alloc[ev.root_id]
        lines.append(
            LineSpec("current", period.id, ev.root_id, ev.source, ev.event_id,
                     ev.metric, ev.quantity, amount, trace)
        )

    # 2) adjustments: re-price every earlier closed period with the ledger
    #    state as of this cutoff (still at that period's own frozen prices)
    #    and bill the delta against what was already billed for it.
    prior_periods = session.scalars(
        select(BillingPeriod)
        .where(
            BillingPeriod.tenant_id == tenant.id,
            BillingPeriod.period_start < period.period_start,
            BillingPeriod.status == "closed",
        )
        .order_by(BillingPeriod.period_start)
    ).all()

    for pp in prior_periods:
        billed = _billed_by_root(session, pp.id, baseline_before_bill_id)
        pevents = effective_events(session, tenant.id, pp.period_start, pp.period_end, cutoff_seq)
        palloc = _allocate_events(pevents, _tiers_by_metric(session, pp.price_version_id))
        seen: set[int] = set()
        for ev in sorted(pevents, key=lambda e: (e.occurred_at, e.root_recv_seq, e.root_id)):
            seen.add(ev.root_id)
            amount, trace = palloc[ev.root_id]
            was = billed.get(ev.root_id, ZERO2)
            delta = amount - was
            if delta != ZERO2:
                lines.append(
                    LineSpec(
                        "adjustment", pp.id, ev.root_id, ev.source, ev.event_id,
                        ev.metric, ev.quantity, delta,
                        {
                            **trace,
                            "adjustment": {
                                "origin_period_id": pp.id,
                                "origin_price_version_id": pp.price_version_id,
                                "recomputed_amount": str(amount),
                                "previously_billed": str(was),
                                "delta": str(delta),
                            },
                        },
                    )
                )
        for root_id, was in billed.items():
            if root_id in seen or was == ZERO2:
                continue
            # event was billed before but is gone now (reversed, or its
            # correction moved occurred_at out of the origin window)
            rec = session.get(UsageRecord, root_id)
            lines.append(
                LineSpec(
                    "adjustment", pp.id, root_id, rec.source, rec.event_id,
                    rec.metric or "", Decimal("0"), -was,
                    {
                        "adjustment": {
                            "origin_period_id": pp.id,
                            "origin_price_version_id": pp.price_version_id,
                            "recomputed_amount": str(ZERO2),
                            "previously_billed": str(was),
                            "delta": str(-was),
                            "reason": "event reversed or moved out of the origin period",
                        }
                    },
                )
            )
    return lines


# ------------------------------------------------------------------ close
def close_period(session: Session, period_id: int) -> Bill:
    """Close a period and finalize its bill in one transaction. The cutoff is
    frozen under the tenant counter lock; the bill is immutable afterwards and
    the period can never be reopened."""
    period = session.get(BillingPeriod, period_id, with_for_update=True)
    if period is None:
        raise NotFound(f"billing period {period_id} not found")
    if period.status == "closed":
        existing = session.scalars(select(Bill).where(Bill.period_id == period.id)).first()
        raise Conflict(
            f"period {period_id} is already closed and cannot be reopened",
            payload={"bill_id": existing.id if existing else None},
        )

    tenant = session.get(Tenant, period.tenant_id)
    if tenant.plan_id is None:
        raise Unprocessable(f"tenant {tenant.code} has no price plan assigned")

    earlier_open = session.scalars(
        select(BillingPeriod.id).where(
            BillingPeriod.tenant_id == tenant.id,
            BillingPeriod.period_start < period.period_start,
            BillingPeriod.status == "open",
        )
    ).first()
    if earlier_open is not None:
        raise Conflict(f"earlier period {earlier_open} is still open; close periods in order")

    cutoff = lock_and_read_seq(session, tenant.id)
    version = resolve_price_version(session, tenant.plan_id, period.period_start)
    if version is None:
        raise Unprocessable(
            f"no price version of plan {tenant.plan_id} covers period start {period.period_start}"
        )

    specs = compute_lines(session, tenant, period, cutoff, version)
    bill = Bill(
        tenant_id=tenant.id,
        period_id=period.id,
        currency=tenant.plan.currency,
        total_amount=sum((s.amount for s in specs), ZERO2),
        cutoff_seq=cutoff,
        price_version_id=version.id,
    )
    session.add(bill)
    session.flush()
    for s in specs:
        session.add(BillLine(bill_id=bill.id, **vars(s)))

    period.status = "closed"
    period.cutoff_seq = cutoff
    period.price_version_id = version.id
    period.closed_at = datetime.now(timezone.utc)
    return bill


# ------------------------------------------------------------------ preview
def preview_period(session: Session, period_id: int):
    """Dry-run: what closing now would produce. Nothing is persisted; the
    cutoff is hypothetical (current counter value, no lock)."""
    period = session.get(BillingPeriod, period_id)
    if period is None:
        raise NotFound(f"billing period {period_id} not found")
    if period.status == "closed":
        raise Conflict(f"period {period_id} is already closed; fetch its bill instead")

    tenant = session.get(Tenant, period.tenant_id)
    if tenant.plan_id is None:
        raise Unprocessable(f"tenant {tenant.code} has no price plan assigned")
    version = resolve_price_version(session, tenant.plan_id, period.period_start)
    if version is None:
        raise Unprocessable(
            f"no price version of plan {tenant.plan_id} covers period start {period.period_start}"
        )

    cutoff = current_seq(session, tenant.id)
    specs = compute_lines(session, tenant, period, cutoff, version)
    return period, tenant, version, cutoff, specs


# ------------------------------------------------------------------ verify
def verify_bill(session: Session, bill_id: int) -> dict[str, Any]:
    """Recompute a finalized bill from its frozen inputs (cutoff + price
    version) and compare line by line. Must always match — the ledger is
    append-only and the price version is immutable."""
    bill = session.get(Bill, bill_id)
    if bill is None:
        raise NotFound(f"bill {bill_id} not found")
    period = session.get(BillingPeriod, bill.period_id)
    tenant = session.get(Tenant, bill.tenant_id)
    version = session.get(PriceVersion, bill.price_version_id)

    specs = compute_lines(
        session, tenant, period, bill.cutoff_seq, version,
        baseline_before_bill_id=bill.id,
    )
    stored = session.scalars(select(BillLine).where(BillLine.bill_id == bill.id)).all()

    stored_map = {(l.line_kind, l.origin_period_id, l.root_record_id): l for l in stored}
    recomputed_map = {(s.line_kind, s.origin_period_id, s.root_record_id): s for s in specs}

    mismatches: list[dict[str, Any]] = []
    for key in sorted(set(stored_map) | set(recomputed_map), key=str):
        s, r = stored_map.get(key), recomputed_map.get(key)
        if s is None:
            mismatches.append({"key": str(key), "issue": "missing in stored bill",
                               "recomputed_amount": str(r.amount)})
        elif r is None:
            mismatches.append({"key": str(key), "issue": "unexpected stored line",
                               "stored_amount": str(s.amount)})
        elif s.amount != r.amount or s.quantity != r.quantity or s.metric != r.metric:
            mismatches.append({
                "key": str(key),
                "issue": "content differs",
                "stored_amount": str(s.amount),
                "recomputed_amount": str(r.amount),
                "stored_quantity": str(s.quantity),
                "recomputed_quantity": str(r.quantity),
            })

    recomputed_total = sum((s.amount for s in specs), ZERO2)
    ok = not mismatches and recomputed_total == bill.total_amount
    return {
        "bill_id": bill.id,
        "ok": ok,
        "stored_total": bill.total_amount,
        "recomputed_total": recomputed_total,
        "stored_lines": len(stored),
        "recomputed_lines": len(specs),
        "mismatches": mismatches,
    }
