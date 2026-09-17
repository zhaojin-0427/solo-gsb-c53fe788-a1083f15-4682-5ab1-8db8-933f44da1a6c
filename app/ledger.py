"""Usage ledger: idempotent ingestion, append-only corrections/reversals, and
effective-state resolution as of a receive cutoff."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .errors import Conflict, NotFound
from .models import UsageRecord
from .schemas import record_dict


def next_recv_seq(session: Session, tenant_id: int) -> int:
    """Atomically allocate the next receive sequence number for a tenant.
    The row lock is held until commit, which is what makes the period-close
    cutoff deterministic under concurrency."""
    return session.execute(
        text(
            "INSERT INTO tenant_counters (tenant_id, last_seq) VALUES (:t, 1) "
            "ON CONFLICT (tenant_id) DO UPDATE "
            "SET last_seq = tenant_counters.last_seq + 1 "
            "RETURNING last_seq"
        ),
        {"t": tenant_id},
    ).scalar_one()


def _get_by_key(session: Session, tenant_id: int, source: str, event_id: str) -> UsageRecord | None:
    return session.scalars(
        select(UsageRecord).where(
            UsageRecord.tenant_id == tenant_id,
            UsageRecord.source == source,
            UsageRecord.event_id == event_id,
        )
    ).first()


def _same_payload(rec: UsageRecord, occurred_at: datetime, quantity: Decimal,
                  metric: str, dimensions: dict) -> bool:
    return (
        rec.occurred_at == occurred_at
        and rec.quantity == quantity
        and rec.metric == metric
        and (rec.dimensions or {}) == (dimensions or {})
    )


def _insert(session: Session, rec: UsageRecord) -> None:
    """Insert inside a savepoint so a unique-violation race can be handled."""
    with session.begin_nested():
        session.add(rec)
        session.flush()


def ingest_event(session: Session, tenant_id: int, payload) -> tuple[UsageRecord, bool]:
    """Book a source event exactly once.

    Returns (record, created). An identical replay returns the existing record
    (created=False); the same key with different content raises 409 Conflict.
    """
    existing = _get_by_key(session, tenant_id, payload.source, payload.event_id)
    if existing is not None:
        if existing.record_type == "event" and _same_payload(
            existing, payload.occurred_at, payload.quantity, payload.metric, payload.dimensions
        ):
            return existing, False
        raise Conflict(
            f"event {payload.source}/{payload.event_id} already recorded with different content",
            payload={"existing": record_dict(existing)},
        )
    rec = UsageRecord(
        tenant_id=tenant_id,
        source=payload.source,
        event_id=payload.event_id,
        record_type="event",
        occurred_at=payload.occurred_at,
        quantity=payload.quantity,
        metric=payload.metric,
        dimensions=payload.dimensions or {},
        recv_seq=next_recv_seq(session, tenant_id),
    )
    try:
        _insert(session, rec)
    except IntegrityError:
        # Lost a concurrent race on the same (tenant, source, event_id).
        existing = _get_by_key(session, tenant_id, payload.source, payload.event_id)
        if existing is not None and existing.record_type == "event" and _same_payload(
            existing, payload.occurred_at, payload.quantity, payload.metric, payload.dimensions
        ):
            return existing, False
        raise Conflict(
            f"event {payload.source}/{payload.event_id} already recorded with different content",
            payload={"existing": record_dict(existing) if existing else None},
        )
    return rec, True


def _chain(session: Session, root_id: int) -> list[UsageRecord]:
    return session.scalars(
        select(UsageRecord)
        .where((UsageRecord.id == root_id) | (UsageRecord.corrects_id == root_id))
        .order_by(UsageRecord.recv_seq)
    ).all()


def get_root_event(session: Session, tenant_id: int, source: str, event_id: str) -> UsageRecord:
    root = _get_by_key(session, tenant_id, source, event_id)
    if root is None or root.record_type != "event":
        raise NotFound(f"event {source}/{event_id} not found")
    return root


def append_correction(session: Session, tenant_id: int, source: str, event_id: str,
                      payload) -> tuple[UsageRecord, bool]:
    """Append a full-replacement correction linked to the original event.
    The original record is never modified."""
    root = get_root_event(session, tenant_id, source, event_id)

    if payload.correction_event_id:
        existing = _get_by_key(session, tenant_id, source, payload.correction_event_id)
        if existing is not None:
            if (
                existing.record_type == "correction"
                and existing.corrects_id == root.id
                and _same_payload(existing, payload.occurred_at, payload.quantity,
                                  payload.metric, payload.dimensions)
            ):
                return existing, False
            raise Conflict(
                f"correction id {payload.correction_event_id} already used with different content",
                payload={"existing": record_dict(existing)},
            )

    latest = max(_chain(session, root.id), key=lambda r: r.recv_seq)
    if latest.record_type == "reversal":
        raise Conflict(f"event {source}/{event_id} has been reversed and can no longer be corrected")

    rec = UsageRecord(
        tenant_id=tenant_id,
        source=source,
        event_id=payload.correction_event_id or f"corr-{uuid.uuid4().hex}",
        record_type="correction",
        corrects_id=root.id,
        occurred_at=payload.occurred_at,
        quantity=payload.quantity,
        metric=payload.metric,
        dimensions=payload.dimensions or {},
        recv_seq=next_recv_seq(session, tenant_id),
    )
    try:
        _insert(session, rec)
    except IntegrityError:
        raise Conflict(f"correction id {rec.event_id} already exists")
    return rec, True


def append_reversal(session: Session, tenant_id: int, source: str, event_id: str,
                    reason: str | None, reversal_event_id: str | None) -> tuple[UsageRecord, bool]:
    """Append a reversal that voids the event. Idempotent: reversing an
    already-reversed event returns the existing reversal record."""
    root = get_root_event(session, tenant_id, source, event_id)

    chain = _chain(session, root.id)
    for rec in chain:
        if rec.record_type == "reversal":
            return rec, False

    if reversal_event_id:
        existing = _get_by_key(session, tenant_id, source, reversal_event_id)
        if existing is not None:
            raise Conflict(
                f"reversal id {reversal_event_id} already used",
                payload={"existing": record_dict(existing)},
            )

    rec = UsageRecord(
        tenant_id=tenant_id,
        source=source,
        event_id=reversal_event_id or f"rev-{uuid.uuid4().hex}",
        record_type="reversal",
        corrects_id=root.id,
        occurred_at=None,
        quantity=None,
        metric=None,
        dimensions={"reason": reason} if reason else {},
        recv_seq=next_recv_seq(session, tenant_id),
    )
    try:
        _insert(session, rec)
    except IntegrityError:
        raise Conflict(f"reversal id {rec.event_id} already exists")
    return rec, True


@dataclass
class EffectiveEvent:
    """The effective (latest non-reversed) state of an event chain as of a
    receive cutoff."""

    root_id: int
    source: str
    event_id: str
    metric: str
    occurred_at: datetime
    quantity: Decimal
    dimensions: dict
    state_record_id: int  # ledger record that determines this state
    root_recv_seq: int


def effective_events(session: Session, tenant_id: int, start: datetime, end: datetime,
                     cutoff_seq: int) -> list[EffectiveEvent]:
    """Effective states of all events whose *effective* occurred_at falls in
    [start, end), considering only ledger records with recv_seq <= cutoff_seq.
    Append-only ledger + fixed cutoff => this function is stable forever,
    which is what makes bills reproducible."""
    rows = session.scalars(
        select(UsageRecord)
        .where(UsageRecord.tenant_id == tenant_id, UsageRecord.recv_seq <= cutoff_seq)
        .order_by(UsageRecord.recv_seq)
    ).all()
    chains: dict[int, list[UsageRecord]] = {}
    for r in rows:
        root_id = r.id if r.record_type == "event" else r.corrects_id
        chains.setdefault(root_id, []).append(r)

    events: list[EffectiveEvent] = []
    for root_id, recs in chains.items():
        root = next(r for r in recs if r.record_type == "event")
        latest = max(recs, key=lambda r: r.recv_seq)
        if latest.record_type == "reversal":
            continue
        if not (start <= latest.occurred_at < end):
            continue
        events.append(
            EffectiveEvent(
                root_id=root_id,
                source=root.source,
                event_id=root.event_id,
                metric=latest.metric,
                occurred_at=latest.occurred_at,
                quantity=latest.quantity,
                dimensions=latest.dimensions or {},
                state_record_id=latest.id,
                root_recv_seq=root.recv_seq,
            )
        )
    return events
