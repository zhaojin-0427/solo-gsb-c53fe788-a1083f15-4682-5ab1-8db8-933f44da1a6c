from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import ledger
from ..db import get_db
from ..errors import DomainError
from ..models import UsageRecord
from ..schemas import (
    CorrectionIn,
    IngestResult,
    ReversalIn,
    UsageIngest,
    UsageRecordOut,
)
from .tenants import get_tenant_or_404

router = APIRouter(tags=["usage"])


def _commit_or_rollback(db: Session, fn):
    try:
        result = fn()
        db.commit()
        return result
    except DomainError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


@router.post("/usage", response_model=IngestResult)
def ingest(payload: UsageIngest, response: Response, db: Session = Depends(get_db)):
    """Ingest one usage record. Idempotent on (tenant, source, event_id):
    an identical replay returns 200 with the original record; the same key
    with different content returns 409."""
    tenant = get_tenant_or_404(db, payload.tenant_code)
    rec, created = _commit_or_rollback(db, lambda: ledger.ingest_event(db, tenant.id, payload))
    response.status_code = 201 if created else 200
    return IngestResult(record=rec, created=created, deduplicated=not created)


@router.post("/usage/{source}/{event_id}/corrections", response_model=IngestResult)
def correct(source: str, event_id: str, payload: CorrectionIn, response: Response,
            db: Session = Depends(get_db)):
    """Append a full-replacement correction linked to the original event.
    The original ledger record is never modified."""
    tenant = get_tenant_or_404(db, payload.tenant_code)
    rec, created = _commit_or_rollback(
        db, lambda: ledger.append_correction(db, tenant.id, source, event_id, payload)
    )
    response.status_code = 201 if created else 200
    return IngestResult(record=rec, created=created, deduplicated=not created)


@router.post("/usage/{source}/{event_id}/reversals", response_model=IngestResult)
def reverse(source: str, event_id: str, payload: ReversalIn, response: Response,
            db: Session = Depends(get_db)):
    """Append a reversal that voids the event. Idempotent."""
    tenant = get_tenant_or_404(db, payload.tenant_code)
    rec, created = _commit_or_rollback(
        db,
        lambda: ledger.append_reversal(db, tenant.id, source, event_id,
                                       payload.reason, payload.reversal_event_id),
    )
    response.status_code = 201 if created else 200
    return IngestResult(record=rec, created=created, deduplicated=not created)


@router.get("/usage/{source}/{event_id}/chain", response_model=list[UsageRecordOut])
def chain(source: str, event_id: str, tenant_code: str, db: Session = Depends(get_db)):
    """The full append-only lineage of an event: original + corrections +
    reversal, ordered by receive sequence."""
    tenant = get_tenant_or_404(db, tenant_code)
    root = ledger.get_root_event(db, tenant.id, source, event_id)
    return db.scalars(
        select(UsageRecord)
        .where((UsageRecord.id == root.id) | (UsageRecord.corrects_id == root.id))
        .order_by(UsageRecord.recv_seq)
    ).all()
