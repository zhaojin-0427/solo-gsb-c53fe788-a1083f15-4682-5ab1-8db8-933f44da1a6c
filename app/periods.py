"""结算周期管理与关账。"""
from __future__ import annotations

import uuid
from datetime import datetime

import psycopg

from .deps import ApiError
from .utils import normalize_occurred_at


def _tenant_or_404(cur: psycopg.Cursor, tenant_id: str) -> uuid.UUID:
    try:
        t = uuid.UUID(tenant_id)
    except ValueError:
        raise ApiError(404, f"tenant {tenant_id} not found")
    cur.execute("SELECT 1 FROM tenants WHERE id=%s", (t,))
    if cur.fetchone() is None:
        raise ApiError(404, f"tenant {tenant_id} not found")
    return t


def create_tenant(conn: psycopg.Connection, name: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM tenants WHERE name=%s", (name,))
        if cur.fetchone() is not None:
            raise ApiError(409, f"tenant name {name!r} already exists")
        try:
            cur.execute(
                "INSERT INTO tenants (name) VALUES (%s) RETURNING *",
                (name,),
            )
        except psycopg.errors.UniqueViolation:
            raise ApiError(409, f"tenant name {name!r} already exists")
        tenant = cur.fetchone()
        cur.execute(
            "INSERT INTO tenant_counters (tenant_id, last_recv_seq) VALUES (%s, 0)",
            (tenant["id"],),
        )
    return tenant


def list_tenants(conn: psycopg.Connection) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM tenants ORDER BY created_at")
        return list(cur.fetchall())


def create_period(
    conn: psycopg.Connection, tenant_id: str, start: datetime, end: datetime
) -> dict:
    start = normalize_occurred_at(start)
    end = normalize_occurred_at(end)
    if end <= start:
        raise ApiError(422, "period_end must be after period_start")
    tenant_uuid = None
    with conn.cursor() as cur:
        tenant_uuid = _tenant_or_404(cur, tenant_id)
        try:
            cur.execute(
                """
                INSERT INTO billing_periods (tenant_id, period_start, period_end)
                VALUES (%s, %s, %s)
                RETURNING *
                """,
                (tenant_uuid, start, end),
            )
        except psycopg.errors.UniqueViolation:
            raise ApiError(409, "a period with the same tenant_id/period_start already exists")
        except psycopg.errors.ExclusionViolation:
            raise ApiError(
                409,
                "period overlaps an existing period for this tenant; periods must not overlap",
            )
        return cur.fetchone()


def list_periods(conn: psycopg.Connection, tenant_id: str) -> list[dict]:
    with conn.cursor() as cur:
        _tenant_or_404(cur, tenant_id)
        cur.execute(
            """
            SELECT * FROM billing_periods
             WHERE tenant_id=%s
             ORDER BY period_start
            """,
            (tenant_id,),
        )
        return list(cur.fetchall())


def get_period(conn: psycopg.Connection, period_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM billing_periods WHERE id=%s", (period_id,))
        row = cur.fetchone()
        if row is None:
            raise ApiError(404, f"period {period_id} not found")
        return row


def serialize_period(row: dict) -> dict:
    return {
        "id": str(row["id"]),
        "tenant_id": str(row["tenant_id"]),
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "status": row["status"],
        "cutoff_recv_seq": row["cutoff_recv_seq"],
        "closed_at": row["closed_at"],
        "bill_id": str(row["bill_id"]) if row["bill_id"] else None,
    }


def serialize_tenant(row: dict) -> dict:
    return {"id": str(row["id"]), "name": row["name"], "created_at": row["created_at"]}
