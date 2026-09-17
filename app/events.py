"""用量事件入账：幂等、冲突检测、只追加修正/撤销、租户接收序号。

并发模型（单事务，Read Committed）：
  1. 对 tenant_counters 行加 FOR UPDATE 行锁 —— 同一租户的入账与“关账固化截止线”
     在此串行化，因此每条新记录的 recv_seq 必然明确落在某条截止线的一侧。
  2. usage 无链；correction/cancellation 锁定 root 事件行后读取全链，
     按 (recv_seq, id) 稳定排序计算当前累计量。
  3. recv_seq 先分配后插入；事务回滚不影响截止线正确性
     （序号可有空洞，但单调性与“先锁先得”的归属不变）。
"""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from .deps import ApiError
from .utils import content_hash, normalize_occurred_at

ZERO = Decimal("0")


def _chain_rows(cur: psycopg.Cursor, root_event_id: uuid.UUID) -> list[dict]:
    cur.execute(
        """
        SELECT * FROM usage_events
         WHERE root_event_id = %s
         ORDER BY recv_seq ASC, id ASC
        """,
        (root_event_id,),
    )
    return list(cur.fetchall())


def ingest_event(conn: psycopg.Connection, tenant_id: str, body: Any) -> tuple[dict, bool]:
    """返回 (事件行, 是否为幂等重放)。"""
    try:
        tenant_uuid = uuid.UUID(tenant_id)
    except ValueError:
        raise ApiError(404, f"tenant {tenant_id} not found")

    occurred_at = normalize_occurred_at(body.occurred_at)
    linked_uuid = uuid.UUID(body.linked_event_id) if body.linked_event_id else None

    if body.event_type in ("correction", "cancellation") and linked_uuid is None:
        raise ApiError(422, f"{body.event_type} requires linked_event_id")
    if body.event_type == "usage" and linked_uuid is not None:
        raise ApiError(422, "usage event must not carry linked_event_id")
    if body.event_type in ("usage", "correction") and body.quantity is None:
        raise ApiError(422, f"{body.event_type} requires quantity")
    quantity = ZERO if body.quantity is None else body.quantity
    if body.event_type in ("usage", "correction") and quantity < 0:
        raise ApiError(422, "quantity must be non-negative; corrections must be append records")

    h = content_hash(
        event_type=body.event_type,
        occurred_at=occurred_at,
        quantity=quantity,
        dimensions=body.dimensions,
        linked_event_id=str(linked_uuid) if linked_uuid else None,
    )

    with conn.cursor() as cur:
        # 步骤 1：租户串行化锁（入账-入账、入账-关账都在此排队）
        cur.execute(
            "SELECT last_recv_seq FROM tenant_counters WHERE tenant_id=%s FOR UPDATE",
            (tenant_uuid,),
        )
        counter = cur.fetchone()
        if counter is None:
            raise ApiError(404, f"tenant {tenant_id} not found")

        # 步骤 2：幂等 / 冲突判定
        cur.execute(
            """
            SELECT * FROM usage_events
             WHERE tenant_id=%s AND source=%s AND event_id=%s
            """,
            (tenant_uuid, body.source, body.event_id),
        )
        existing = cur.fetchone()
        if existing is not None:
            if existing["content_hash"] == h:
                return existing, True  # 幂等重放：原账不动
            raise ApiError(
                409,
                "conflict: same (source, event_id) already ingested with different content; "
                "issue a correction record instead of resubmitting",
                extra={
                    "source": body.source,
                    "event_id": body.event_id,
                    "existing_event_pk": str(existing["id"]),
                    "existing_content_hash": existing["content_hash"],
                    "incoming_content_hash": h,
                },
            )

        # 步骤 3：修正/撤销链路校验
        root_event_id = uuid.uuid4()
        billed_quantity = quantity  # 写入账册的“有符号计费量”
        if body.event_type != "usage":
            cur.execute(
                "SELECT * FROM usage_events WHERE id=%s AND tenant_id=%s FOR UPDATE",
                (linked_uuid, tenant_uuid),
            )
            linked = cur.fetchone()
            if linked is None:
                raise ApiError(404, "linked_event_id not found for this tenant")
            if linked["source"] != body.source:
                raise ApiError(422, "linked event belongs to a different source")
            root_event_id = linked["root_event_id"]

            # 对整条链加行锁，阻塞并发的同链修正/撤销；锁后重新读取避免陈旧快照
            cur.execute(
                "SELECT id FROM usage_events WHERE root_event_id=%s ORDER BY recv_seq FOR UPDATE",
                (root_event_id,),
            )
            if not cur.fetchall():
                raise ApiError(422, "linked event has no valid chain root")
            chain = _chain_rows(cur, root_event_id)

            if linked["event_type"] == "cancellation":
                raise ApiError(
                    422, "cannot link to a cancellation; the event chain is already revoked"
                )
            # 维度不可修正：必须与原账保持一致
            if linked["dimensions"] != (body.dimensions or {}):
                raise ApiError(
                    422, "dimensions are immutable; correction must repeat the original dimensions"
                )

            current_effective = sum((row["quantity"] for row in chain), ZERO)
            if body.event_type == "cancellation":
                billed_quantity = -current_effective
            else:  # correction：提交的是新的“累计用量”，差值才入账
                billed_quantity = quantity - current_effective

        # 步骤 4：分配接收序号并追加账册（原账永不变更）
        new_seq = counter["last_recv_seq"] + 1
        cur.execute(
            "UPDATE tenant_counters SET last_recv_seq=%s WHERE tenant_id=%s",
            (new_seq, tenant_uuid),
        )
        cur.execute(
            """
            INSERT INTO usage_events
              (tenant_id, source, event_id, event_type, occurred_at, recv_seq,
               quantity, dimensions, linked_event_id, root_event_id, content_hash)
            VALUES
              (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                tenant_uuid,
                body.source,
                body.event_id,
                body.event_type,
                occurred_at,
                new_seq,
                billed_quantity,
                Jsonb(body.dimensions or {}),
                linked_uuid,
                root_event_id,
                h,
            ),
        )
        row = cur.fetchone()
    return row, False


def list_events(
    conn: psycopg.Connection,
    tenant_id: str,
    *,
    source: str | None = None,
    limit: int = 100,
) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT * FROM usage_events
             WHERE tenant_id=%s
                   AND (%s IS NULL OR source = %s)
             ORDER BY recv_seq DESC, id DESC
             LIMIT %s
            """,
            (tenant_id, source, source, limit),
        )
        return list(cur.fetchall())


def serialize_event(row: dict, *, idempotent: bool = False) -> dict:
    return {
        "id": str(row["id"]),
        "source": row["source"],
        "event_id": row["event_id"],
        "event_type": row["event_type"],
        "occurred_at": row["occurred_at"],
        "received_at": row["received_at"],
        "recv_seq": row["recv_seq"],
        "quantity": row["quantity"],
        "dimensions": row["dimensions"],
        "linked_event_id": str(row["linked_event_id"]) if row["linked_event_id"] else None,
        "root_event_id": str(row["root_event_id"]),
        "idempotent": idempotent,
    }
