"""价格版本领域服务：版本化价格表、区间不重叠、生效价格解析。"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg

from .deps import ApiError
from .pricing import PriceVersion, Tier
from .utils import normalize_occurred_at


def validate_tiers(tiers: list[Any]) -> list[dict]:
    """阶梯必须：索引连续、从 0 起、首尾相接、仅最后一档开口。"""
    if not tiers:
        raise ApiError(422, "at least one price tier is required")
    ordered = sorted(tiers, key=lambda t: t.tier_index)
    expected = 0
    prev_end: Decimal | None = None
    out: list[dict] = []
    for t in ordered:
        if t.tier_index != expected:
            raise ApiError(422, f"tier_index must be contiguous from 0; expected {expected}")
        if expected == 0 and t.from_qty != 0:
            raise ApiError(422, "first tier must start at from_qty=0")
        if prev_end is not None and t.from_qty != prev_end:
            raise ApiError(
                422,
                f"tier {expected} must start where previous tier ended ({prev_end})",
            )
        if t.up_to_qty is not None and t.up_to_qty <= t.from_qty:
            raise ApiError(422, f"tier {expected} requires up_to_qty > from_qty")
        out.append(
            {
                "tier_index": t.tier_index,
                "from_qty": t.from_qty,
                "up_to_qty": t.up_to_qty,
                "unit_amount": t.unit_amount,
                "flat_amount": t.flat_amount,
            }
        )
        prev_end = t.up_to_qty
        expected += 1
    if prev_end is not None:
        raise ApiError(422, "last tier must be open-ended: set up_to_qty to null")
    return out


def create_price_version(conn: psycopg.Connection, body: Any) -> dict:
    effective_from = normalize_occurred_at(body.effective_from)
    effective_to = (
        normalize_occurred_at(body.effective_to) if body.effective_to else None
    )
    if effective_to is not None and effective_to <= effective_from:
        raise ApiError(422, "effective_to must be after effective_from")
    tiers = validate_tiers(body.tiers)

    with conn.cursor() as cur:
        if body.tenant_id is not None:
            cur.execute("SELECT 1 FROM tenants WHERE id=%s", (body.tenant_id,))
            if cur.fetchone() is None:
                raise ApiError(404, f"tenant {body.tenant_id} not found")
            scope_sql = "tenant_id = %s"
            scope_params: tuple[Any, ...] = (body.tenant_id,)
        else:
            scope_sql = "tenant_id IS NULL"
            scope_params = ()

        # 新版本开启时，自动把同一 (租户, source) 上仍开口的旧版本在新版本起点收尾。
        cur.execute(
            f"""
            UPDATE price_versions
               SET effective_to = %s
             WHERE source = %s
               AND {scope_sql}
               AND effective_to IS NULL
               AND effective_from < %s
            RETURNING id
            """,
            (effective_from, body.source, *scope_params, effective_from),
        )

        try:
            cur.execute(
                """
                INSERT INTO price_versions
                    (tenant_id, source, currency, pricing_mode, effective_from, effective_to)
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    body.tenant_id,
                    body.source,
                    body.currency,
                    body.pricing_mode,
                    effective_from,
                    effective_to,
                ),
            )
        except psycopg.errors.ExclusionViolation:
            raise ApiError(
                409,
                "price version interval overlaps an existing version for the same "
                "tenant/source; price intervals must not overlap",
            )
        pv = cur.fetchone()

        cur.executemany(
            """
            INSERT INTO price_tiers
                (price_version_id, tier_index, from_qty, up_to_qty, unit_amount, flat_amount)
            VALUES (%(price_version_id)s, %(tier_index)s, %(from_qty)s, %(up_to_qty)s,
                    %(unit_amount)s, %(flat_amount)s)
            """,
            [{**t, "price_version_id": pv["id"]} for t in tiers],
        )

    return get_price_version(conn, pv["id"])


def get_price_version(conn: psycopg.Connection, version_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM price_versions WHERE id=%s", (version_id,))
        pv = cur.fetchone()
        if pv is None:
            raise ApiError(404, f"price version {version_id} not found")
        cur.execute(
            "SELECT * FROM price_tiers WHERE price_version_id=%s ORDER BY tier_index",
            (version_id,),
        )
        tiers = cur.fetchall()
    return _serialize_version(pv, tiers)


def list_price_versions(
    conn: psycopg.Connection, *, source: str | None, tenant_id: str | None
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if source is not None:
        where.append("source = %s")
        params.append(source)
    if tenant_id is not None:
        where.append("(tenant_id = %s OR tenant_id IS NULL)")
        params.append(tenant_id)
    sql = "SELECT * FROM price_versions"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY effective_from, source, tenant_id"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        pvs = cur.fetchall()
        result = []
        for pv in pvs:
            cur.execute(
                "SELECT * FROM price_tiers WHERE price_version_id=%s ORDER BY tier_index",
                (pv["id"],),
            )
            result.append(_serialize_version(pv, cur.fetchall()))
    return result


def resolve_price(
    conn: psycopg.Connection, *, tenant_id: str, source: str, at: datetime
) -> PriceVersion | None:
    """取某时刻生效价格：租户专属优先，其次默认价格。"""
    at = normalize_occurred_at(at)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM price_versions
             WHERE source = %s
               AND tenant_id = %s
               AND effective_from <= %s
               AND (effective_to IS NULL OR %s < effective_to)
             ORDER BY effective_from DESC
             LIMIT 1
            """,
            (source, tenant_id, at, at),
        )
        pv = cur.fetchone()
        if pv is None:
            cur.execute(
                """
                SELECT * FROM price_versions
                 WHERE source = %s
                   AND tenant_id IS NULL
                   AND effective_from <= %s
                   AND (effective_to IS NULL OR %s < effective_to)
                 ORDER BY effective_from DESC
                 LIMIT 1
                """,
                (source, at, at),
            )
            pv = cur.fetchone()
        if pv is None:
            return None
        cur.execute(
            "SELECT * FROM price_tiers WHERE price_version_id=%s ORDER BY tier_index",
            (pv["id"],),
        )
        tiers = cur.fetchall()

    return PriceVersion(
        id=str(pv["id"]),
        source=pv["source"],
        currency=pv["currency"],
        pricing_mode=pv["pricing_mode"],
        effective_from=pv["effective_from"],
        effective_to=pv["effective_to"],
        tenant_id=None if pv["tenant_id"] is None else str(pv["tenant_id"]),
        tiers=tuple(
            Tier(
                tier_index=t["tier_index"],
                from_qty=t["from_qty"],
                up_to_qty=t["up_to_qty"],
                unit_amount=t["unit_amount"],
                flat_amount=t["flat_amount"],
            )
            for t in tiers
        ),
    )


def _serialize_version(pv: dict, tiers: list[dict]) -> dict:
    return {
        "id": str(pv["id"]),
        "tenant_id": str(pv["tenant_id"]) if pv["tenant_id"] else None,
        "source": pv["source"],
        "currency": pv["currency"],
        "pricing_mode": pv["pricing_mode"],
        "effective_from": pv["effective_from"],
        "effective_to": pv["effective_to"],
        "tiers": [
            {
                "id": str(t["id"]),
                "tier_index": t["tier_index"],
                "from_qty": t["from_qty"],
                "up_to_qty": t["up_to_qty"],
                "unit_amount": t["unit_amount"],
                "flat_amount": t["flat_amount"],
            }
            for t in tiers
        ],
    }
