from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from .. import billing
from ..db import get_db
from ..errors import NotFound
from ..models import Bill
from ..schemas import BillOut, BillSummary, TraceOut, VerifyOut
from .tenants import get_tenant_or_404

router = APIRouter(tags=["bills"])


def _get_bill_or_404(db: Session, bill_id: int) -> Bill:
    bill = db.scalars(
        select(Bill).where(Bill.id == bill_id).options(selectinload(Bill.lines))
    ).first()
    if bill is None:
        raise NotFound(f"bill {bill_id} not found")
    return bill


@router.get("/tenants/{code}/bills", response_model=list[BillSummary])
def list_bills(code: str, db: Session = Depends(get_db)):
    tenant = get_tenant_or_404(db, code)
    return db.scalars(
        select(Bill).where(Bill.tenant_id == tenant.id).order_by(Bill.id)
    ).all()


@router.get("/bills/{bill_id}", response_model=BillOut)
def get_bill(bill_id: int, db: Session = Depends(get_db)):
    """Bill detail: header + all immutable lines (current charges and
    retro adjustments), each with its pricing trace."""
    return _get_bill_or_404(db, bill_id)


@router.get("/bills/{bill_id}/trace", response_model=TraceOut)
def bill_trace(bill_id: int, db: Session = Depends(get_db)):
    """Per-event pricing trace （逐事件计价轨迹）: for every event, the exact
    bracket slices that produced its amount."""
    bill = _get_bill_or_404(db, bill_id)
    return TraceOut(
        bill_id=bill.id,
        period_id=bill.period_id,
        cutoff_seq=bill.cutoff_seq,
        price_version_id=bill.price_version_id,
        currency=bill.currency,
        events=[
            {
                "source": l.source,
                "event_id": l.event_id,
                "line_kind": l.line_kind,
                "origin_period_id": l.origin_period_id,
                "metric": l.metric,
                "quantity": l.quantity,
                "amount": l.amount,
                "trace": l.trace,
            }
            for l in bill.lines
        ],
    )


@router.post("/bills/{bill_id}/verify", response_model=VerifyOut)
def verify(bill_id: int, db: Session = Depends(get_db)):
    """Recompute the bill from its frozen inputs (cutoff sequence + frozen
    price version + append-only ledger) and compare line by line. Proves any
    bill can be independently reproduced."""
    return billing.verify_bill(db, bill_id)
