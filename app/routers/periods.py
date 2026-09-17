from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from .. import billing
from ..db import get_db
from ..errors import Conflict, DomainError, NotFound, Unprocessable
from ..models import Bill, BillingPeriod
from ..schemas import BillOut, PeriodCreate, PeriodOut, PreviewOut
from .tenants import get_tenant_or_404

router = APIRouter(tags=["periods"])


@router.post("/periods", response_model=PeriodOut, status_code=201)
def create_period(payload: PeriodCreate, db: Session = Depends(get_db)):
    tenant = get_tenant_or_404(db, payload.tenant_code)
    if payload.period_end <= payload.period_start:
        raise Unprocessable("period_end must be after period_start")
    overlap = db.scalars(
        select(BillingPeriod).where(
            BillingPeriod.tenant_id == tenant.id,
            BillingPeriod.period_start < payload.period_end,
            BillingPeriod.period_end > payload.period_start,
        )
    ).first()
    if overlap is not None:
        raise Conflict(f"period overlaps existing period {overlap.id}")
    period = BillingPeriod(
        tenant_id=tenant.id,
        period_start=payload.period_start,
        period_end=payload.period_end,
        status="open",
    )
    db.add(period)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict("a period with this start already exists for the tenant")
    return period


@router.get("/tenants/{code}/periods", response_model=list[PeriodOut])
def list_periods(code: str, db: Session = Depends(get_db)):
    tenant = get_tenant_or_404(db, code)
    return db.scalars(
        select(BillingPeriod)
        .where(BillingPeriod.tenant_id == tenant.id)
        .order_by(BillingPeriod.period_start)
    ).all()


@router.get("/periods/{period_id}", response_model=PeriodOut)
def get_period(period_id: int, db: Session = Depends(get_db)):
    period = db.get(BillingPeriod, period_id)
    if period is None:
        raise NotFound(f"billing period {period_id} not found")
    return period


@router.post("/periods/{period_id}/preview", response_model=PreviewOut)
def preview(period_id: int, db: Session = Depends(get_db)):
    """Dry-run （试算）: compute the bill this period would produce if closed
    right now. Nothing is persisted."""
    period, tenant, version, cutoff, specs = billing.preview_period(db, period_id)
    return PreviewOut(
        period_id=period.id,
        status=period.status,
        hypothetical_cutoff_seq=cutoff,
        price_version_id=version.id,
        currency=tenant.plan.currency,
        total_amount=sum((s.amount for s in specs), billing.ZERO2),
        lines=specs,
        note="dry-run only: the cutoff is hypothetical; concurrently arriving "
        "records may shift the result. Nothing was persisted.",
    )


@router.post("/periods/{period_id}/close", response_model=BillOut)
def close(period_id: int, db: Session = Depends(get_db)):
    """Close the period in one transaction: freeze the tenant receive cutoff,
    price current-period events received at/before the cutoff, carry late
    corrections of earlier periods as adjustments, and finalize an immutable
    bill. A closed period can never be reopened."""
    try:
        bill = billing.close_period(db, period_id)
        db.commit()
    except DomainError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise
    return db.scalars(
        select(Bill).where(Bill.id == bill.id).options(selectinload(Bill.lines))
    ).one()
