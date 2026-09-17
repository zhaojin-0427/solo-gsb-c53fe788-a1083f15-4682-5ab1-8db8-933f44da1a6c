from __future__ import annotations

from collections import defaultdict

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..errors import Conflict, NotFound, Unprocessable
from ..models import PricePlan, PriceTier, PriceVersion
from ..pricing import Tier, validate_tiers
from ..schemas import PlanCreate, PlanOut, PriceVersionCreate, PriceVersionOut

router = APIRouter(tags=["pricing"])


@router.post("/plans", response_model=PlanOut, status_code=201)
def create_plan(payload: PlanCreate, db: Session = Depends(get_db)):
    plan = PricePlan(name=payload.name, currency=payload.currency)
    db.add(plan)
    db.commit()
    return plan


@router.get("/plans/{plan_id}", response_model=PlanOut)
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    plan = db.get(PricePlan, plan_id)
    if plan is None:
        raise NotFound(f"price plan {plan_id} not found")
    return plan


@router.post("/plans/{plan_id}/versions", response_model=PriceVersionOut, status_code=201)
def create_version(plan_id: int, payload: PriceVersionCreate, db: Session = Depends(get_db)):
    # Lock the plan row: serializes concurrent version creation so the
    # non-overlap check below cannot be raced (the GiST exclusion constraint
    # is the database-level backstop).
    plan = db.get(PricePlan, plan_id, with_for_update=True)
    if plan is None:
        raise NotFound(f"price plan {plan_id} not found")

    if payload.effective_to is not None and payload.effective_to <= payload.effective_from:
        raise Unprocessable("effective_to must be after effective_from")

    tiers_by_metric: dict[str, list[Tier]] = defaultdict(list)
    for t in payload.tiers:
        if t.up_to is not None and t.up_to <= 0:
            raise Unprocessable("tier up_to must be > 0 (or null for the unbounded tier)")
        tiers_by_metric[t.metric].append(Tier(up_to=t.up_to, unit_price=t.unit_price))
    for metric, tiers in tiers_by_metric.items():
        try:
            validate_tiers(tiers)
        except ValueError as exc:
            raise Unprocessable(f"invalid tiers for metric '{metric}': {exc}")

    # overlap iff existing.from < new.to (new open-ended => always true)
    #         AND (existing.to is open OR existing.to > new.from)
    conditions = []
    if payload.effective_to is not None:
        conditions.append(PriceVersion.effective_from < payload.effective_to)
    conditions.append(
        or_(PriceVersion.effective_to.is_(None), PriceVersion.effective_to > payload.effective_from)
    )
    overlap = db.scalars(
        select(PriceVersion).where(PriceVersion.plan_id == plan_id, *conditions)
    ).first()
    if overlap is not None:
        raise Conflict(
            f"effective range overlaps existing version {overlap.version} "
            f"[{overlap.effective_from}, {overlap.effective_to})"
        )

    version = PriceVersion(
        plan_id=plan_id,
        version=payload.version,
        effective_from=payload.effective_from,
        effective_to=payload.effective_to,
    )
    db.add(version)
    db.flush()
    for metric, tiers in tiers_by_metric.items():
        ordered = sorted(tiers, key=lambda t: (t.up_to is None, t.up_to))
        for i, t in enumerate(ordered, start=1):
            db.add(PriceTier(version_id=version.id, metric=metric, tier_order=i,
                             up_to=t.up_to, unit_price=t.unit_price))
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        diag = getattr(getattr(exc.orig, "diag", None), "message_detail", None)
        raise Conflict(f"version conflicts with existing data: {diag or exc.orig}")
    return db.get(PriceVersion, version.id, options=[selectinload(PriceVersion.tiers)])


@router.get("/plans/{plan_id}/versions", response_model=list[PriceVersionOut])
def list_versions(plan_id: int, db: Session = Depends(get_db)):
    if db.get(PricePlan, plan_id) is None:
        raise NotFound(f"price plan {plan_id} not found")
    return db.scalars(
        select(PriceVersion)
        .where(PriceVersion.plan_id == plan_id)
        .options(selectinload(PriceVersion.tiers))
        .order_by(PriceVersion.effective_from)
    ).all()
