from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db import get_db
from ..errors import Conflict, NotFound, Unprocessable
from ..models import PricePlan, Tenant, UsageRecord
from ..schemas import AssignPlan, TenantCreate, TenantOut, UsageRecordOut

router = APIRouter(tags=["tenants"])


@router.post("/tenants", response_model=TenantOut, status_code=201)
def create_tenant(payload: TenantCreate, db: Session = Depends(get_db)):
    if payload.plan_id is not None and db.get(PricePlan, payload.plan_id) is None:
        raise Unprocessable(f"price plan {payload.plan_id} does not exist")
    tenant = Tenant(code=payload.code, name=payload.name, plan_id=payload.plan_id)
    db.add(tenant)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise Conflict(f"tenant code '{payload.code}' already exists")
    return tenant


def get_tenant_or_404(db: Session, code: str) -> Tenant:
    tenant = db.scalars(select(Tenant).where(Tenant.code == code)).first()
    if tenant is None:
        raise NotFound(f"tenant '{code}' not found")
    return tenant


@router.get("/tenants/{code}", response_model=TenantOut)
def get_tenant(code: str, db: Session = Depends(get_db)):
    return get_tenant_or_404(db, code)


@router.put("/tenants/{code}/plan", response_model=TenantOut)
def assign_plan(code: str, payload: AssignPlan, db: Session = Depends(get_db)):
    tenant = get_tenant_or_404(db, code)
    if db.get(PricePlan, payload.plan_id) is None:
        raise Unprocessable(f"price plan {payload.plan_id} does not exist")
    tenant.plan_id = payload.plan_id
    db.commit()
    return tenant


@router.get("/tenants/{code}/usage", response_model=list[UsageRecordOut])
def list_usage(code: str, record_type: str | None = None, limit: int = 100,
               offset: int = 0, db: Session = Depends(get_db)):
    tenant = get_tenant_or_404(db, code)
    stmt = select(UsageRecord).where(UsageRecord.tenant_id == tenant.id)
    if record_type:
        stmt = stmt.where(UsageRecord.record_type == record_type)
    stmt = stmt.order_by(UsageRecord.recv_seq).limit(min(limit, 1000)).offset(offset)
    return db.scalars(stmt).all()
