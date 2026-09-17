"""关账、试算、账单明细与按固化版本重算。

截止线（cutoff_recv_seq）语义
-----------------------------
关账在单事务内：
  1. 锁定该租户 tenant_counters 行（与入账共用同一把锁），读取 last_recv_seq
     并固化为 cutoff_recv_seq —— 此刻尚未拿到序号的并发入账必须等待本事务，
     等待结束后其序号必然 > cutoff，于是它明确落在截止线“之后”，进入下期。
  2. 候选事件 = recv_seq <= cutoff、且从未进入任何账单的记录：
       - usage 且 occurred_at 属于本期 → 本期正常用量
       - usage 且 occurred_at 早于本期 → 迟到记录，本期调整
       - correction/cancellation：当其关联事件已在历史账单中，或被本账
         选中的事件带入时，作为调整行进入（同一张账单内“原量+差值”一起入账，
         净额正确；关闭后到达的修正进入下期调整）
  3. 账单与账单项写入后周期置 closed；历史账单自此不可变、不可重开。
"""
from __future__ import annotations

from decimal import Decimal

import psycopg
from psycopg.types.json import Jsonb

from .config import settings
from .deps import ApiError
from .pricing import PriceVersion, Tier, price_event
from .prices import resolve_price, get_price_version
from .periods import get_period

ZERO = Decimal("0")


def _load_price_version_object(conn: psycopg.Connection, version_id: str) -> PriceVersion:
    data = get_price_version(conn, version_id)
    return PriceVersion(
        id=data["id"],
        source=data["source"],
        currency=data["currency"],
        pricing_mode=data["pricing_mode"],
        effective_from=data["effective_from"],
        effective_to=data["effective_to"],
        tenant_id=data["tenant_id"],
        tiers=tuple(
            Tier(
                tier_index=t["tier_index"],
                from_qty=t["from_qty"],
                up_to_qty=t["up_to_qty"],
                unit_amount=t["unit_amount"],
                flat_amount=t["flat_amount"],
            )
            for t in data["tiers"]
        ),
    )


def _raw_candidates(
    cur: psycopg.Cursor,
    *,
    tenant_id: str,
    cutoff_seq: int,
    period_start,
    period_end,
    bill_id,
    exclude_billed: bool,
) -> list[dict]:
    """截止线内的原始事件行（含“已在本账单”的行，供重算精确还原）。"""
    if exclude_billed:
        billed_filter = (
            "AND NOT EXISTS (SELECT 1 FROM bill_lines bl WHERE bl.event_id = e.id)"
        )
        # 占位符物理顺序：tenant, cutoff, period_end(usage), period_start, period_end
        params = (
            tenant_id, cutoff_seq,
            period_end, period_start, period_end,
        )
    else:
        # 重算：本账单已有行始终纳入；已进“其它”账单的行排除
        billed_filter = (
            "AND (EXISTS (SELECT 1 FROM bill_lines bl0 "
            "      WHERE bl0.event_id = e.id AND bl0.bill_id = %s) "
            " OR NOT EXISTS (SELECT 1 FROM bill_lines bl WHERE bl.event_id = e.id))"
        )
        # 物理顺序：tenant, cutoff, bill_id(filter), period_end(usage),
        #          period_start, period_end
        params = (
            tenant_id, cutoff_seq, bill_id,
            period_end, period_start, period_end,
        )
    cur.execute(
        f"""
        WITH cutoff_events AS (
            SELECT * FROM usage_events
             WHERE tenant_id = %s
               AND recv_seq <= %s
               {billed_filter}
        ),
        usage_candidates AS (
            SELECT * FROM cutoff_events
             WHERE event_type = 'usage'
               AND occurred_at < %s   -- 本期内 + 更早的迟到用量
        ),
        adj_candidates AS (
            -- 截止线内全部未入账（或属于本账单）的修正/撤销，
            -- 是否入选取决于关联事件（见 Python 闭包）
            SELECT * FROM cutoff_events
             WHERE event_type IN ('correction','cancellation')
               AND linked_event_id IS NOT NULL
        )
        SELECT * FROM usage_candidates
        UNION ALL
        SELECT * FROM adj_candidates
        ORDER BY recv_seq ASC, id ASC
        """,
        params,
    )
    return list(cur.fetchall())


def _billed_event_ids(cur: psycopg.Cursor, tenant_id) -> set:
    cur.execute(
        """
        SELECT bl.event_id AS event_id
          FROM bill_lines bl
          JOIN usage_events e ON e.id = bl.event_id
         WHERE e.tenant_id = %s
        """,
        (tenant_id,),
    )
    return {r["event_id"] for r in cur.fetchall()}


def _select_candidates(
    cur: psycopg.Cursor,
    *,
    tenant_id: str,
    cutoff_seq: int,
    period_start,
    period_end,
    exclude_billed: bool = True,
    bill_id: str | None = None,
) -> list[dict]:
    """选出本期账单应包含的事件集合。

    组成（均在截止线之前、且未被其它账单消费）：
      * usage：occurred_at 在本期（正常用量）或早于本期（迟到用量调整）；
      * correction/cancellation：其关联事件已经在某张历史账单中，
        或被本账的 usage 集合/先前入选的修正带入（单遍按 recv_seq 闭包）。

    这保证：① 同一业务链在同一张账单里“原量+差值”一起入账，净额正确；
    ② 关闭后到达的修正进入下一张账单作调整；
    ③ 每个账册事件恰好进入一张账单；④ 历史账单永不重开。
    """
    rows = _raw_candidates(
        cur,
        tenant_id=tenant_id,
        cutoff_seq=cutoff_seq,
        period_start=period_start,
        period_end=period_end,
        bill_id=bill_id,
        exclude_billed=exclude_billed,
    )
    billed_ids = _billed_event_ids(cur, tenant_id)

    chosen: list[dict] = []
    eligible_ids: set = set()
    for row in rows:  # 已按 recv_seq, id 排序
        if row["event_type"] == "usage":
            # 未来时间（>= 本期结束）的用量在 SQL 中已被 occurred_at < end 过滤
            eligible_ids.add(row["id"])
            chosen.append(row)
        else:
            linked = row["linked_event_id"]
            if linked in billed_ids or linked in eligible_ids:
                eligible_ids.add(row["id"])
                chosen.append(row)
    return chosen


def _classify(event: dict, period_start, period_end) -> tuple[str, str]:
    occurred = event["occurred_at"]
    if event["event_type"] == "usage":
        if period_start <= occurred < period_end:
            return "usage", "current"
        return "adjustment", "late"
    # correction / cancellation 全部作为调整行：
    # 发生时间在本期内属本期调整；其余（对更早账目的修正/撤销）属跨期调整。
    if period_start <= occurred < period_end:
        return "adjustment", "current"
    return "adjustment", "correction"


def _project(conn: psycopg.Connection, period: dict, *, cutoff_seq: int) -> dict:
    with conn.cursor() as cur:
        candidates = _select_candidates(
            cur,
            tenant_id=period["tenant_id"],
            cutoff_seq=cutoff_seq,
            period_start=period["period_start"],
            period_end=period["period_end"],
        )

    # 价格解析（无独立游标的写操作，复用连接即可）
    priced: list[dict] = []
    unpriceable: list[dict] = []
    currencies: set[str] = set()
    for event in candidates:
        price = resolve_price(
            conn,
            tenant_id=str(event["tenant_id"]),
            source=event["source"],
            at=event["occurred_at"],
        )
        if price is None:
            unpriceable.append(
                {
                    "event_pk": str(event["id"]),
                    "source": event["source"],
                    "event_id": event["event_id"],
                    "occurred_at": event["occurred_at"].isoformat(),
                }
            )
            continue
        currencies.add(price.currency)
        line_kind, attribution = _classify(
            event, period["period_start"], period["period_end"]
        )
        trace = price_event(event["quantity"], price, scale=settings.currency_scale)
        trace["line_kind"] = line_kind
        trace["period_attribution"] = attribution
        priced.append(
            {
                "event": event,
                "price": price,
                "line_kind": line_kind,
                "attribution": attribution,
                "amount": Decimal(trace["amount"]),
                "trace": trace,
            }
        )

    total = sum((p["amount"] for p in priced), ZERO)
    return {
        "period": period,
        "cutoff_recv_seq": cutoff_seq,
        "currency": next(iter(currencies)) if currencies else None,
        "total_amount": total,
        "lines": priced,
        "unpriceable": unpriceable,
    }


def trial(conn: psycopg.Connection, period_id: str) -> dict:
    """对打开周期做只读试算：按“当前接收序号”投影账单，不固化任何数据。"""
    period = get_period(conn, period_id)
    if period["status"] != "open":
        return _closed_bill_projection(conn, period)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_recv_seq FROM tenant_counters WHERE tenant_id=%s",
            (period["tenant_id"],),
        )
        cutoff = cur.fetchone()["last_recv_seq"]
    return _project(conn, period, cutoff_seq=cutoff)


def _closed_bill_projection(conn: psycopg.Connection, period: dict) -> dict:
    bill = get_bill(conn, str(period["bill_id"]))
    return {
        "period": period,
        "cutoff_recv_seq": period["cutoff_recv_seq"],
        "currency": bill["currency"],
        "total_amount": bill["total_amount"],
        "lines": bill["lines"],
        "unpriceable": [],
        "already_closed": True,
    }


def close_period(conn: psycopg.Connection, period_id: str) -> dict:
    """关闭周期并生成不可变账单（单事务）。"""
    period = get_period(conn, period_id)
    if period["status"] != "open":
        raise ApiError(409, f"period {period_id} is already closed")

    with conn.cursor() as cur:
        # 关键：先固化截止线。FOR UPDATE 与入账共用租户计数器行锁，
        # 拿不到锁的并发入账 / 其它关账在本事务提交后才继续，
        # 其序号必然 > cutoff，且关账顺序在该锁上严格串行。
        cur.execute(
            "SELECT last_recv_seq FROM tenant_counters WHERE tenant_id=%s FOR UPDATE",
            (period["tenant_id"],),
        )
        cutoff_seq = cur.fetchone()["last_recv_seq"]

        # 锁当前周期行并复查（可能已被并发关账），FOR UPDATE 拿到的是最新已提交版本
        cur.execute(
            "SELECT * FROM billing_periods WHERE id=%s FOR UPDATE",
            (period["id"],),
        )
        period = cur.fetchone()
        if period["status"] != "open":
            raise ApiError(409, f"period {period_id} is already closed")

        # 顺序关账复查：必须在计数器锁之后（此时同租户不会有另一个关账在进行）
        cur.execute(
            """
            SELECT id FROM billing_periods
             WHERE tenant_id = %s AND status = 'open' AND period_start < %s
             LIMIT 1
            """,
            (period["tenant_id"], period["period_start"]),
        )
        earlier = cur.fetchone()
        if earlier is not None:
            raise ApiError(
                422,
                f"an earlier open period {earlier['id']} exists; close periods in order",
            )

        candidates = _select_candidates(
            cur,
            tenant_id=period["tenant_id"],
            cutoff_seq=cutoff_seq,
            period_start=period["period_start"],
            period_end=period["period_end"],
        )

    # 在同一事务/连接内计价（resolve_price 只做 SELECT）
    priced: list[dict] = []
    unpriceable: list[dict] = []
    currencies: set[str] = set()
    for event in candidates:
        price = resolve_price(
            conn,
            tenant_id=str(event["tenant_id"]),
            source=event["source"],
            at=event["occurred_at"],
        )
        if price is None:
            unpriceable.append(
                {
                    "event_pk": str(event["id"]),
                    "source": event["source"],
                    "event_id": event["event_id"],
                    "occurred_at": event["occurred_at"].isoformat(),
                }
            )
            continue
        currencies.add(price.currency)
        line_kind, attribution = _classify(
            event, period["period_start"], period["period_end"]
        )
        trace = price_event(event["quantity"], price, scale=settings.currency_scale)
        trace["line_kind"] = line_kind
        trace["period_attribution"] = attribution
        priced.append(
            {
                "event": event,
                "price": price,
                "line_kind": line_kind,
                "attribution": attribution,
                "amount": Decimal(trace["amount"]),
                "trace": trace,
            }
        )

    if unpriceable:
        raise ApiError(
            422,
            "cannot close period: some events have no effective price version; "
            "create a covering price (default tenant price allowed) and retry",
            extra={"unpriceable_events": unpriceable},
        )
    if len(currencies) > 1:
        raise ApiError(
            422,
            f"candidate events resolve to multiple currencies {sorted(currencies)}; "
            "a single bill must be in one currency",
        )

    currency = next(iter(currencies), "USD")
    total = sum((p["amount"] for p in priced), ZERO)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO bills (tenant_id, period_id, currency, cutoff_recv_seq, total_amount)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING *
            """,
            (period["tenant_id"], period["id"], currency, cutoff_seq, total),
        )
        bill = cur.fetchone()

        if priced:
            cur.executemany(
                """
                INSERT INTO bill_lines
                  (bill_id, event_id, line_kind, period_attribution, price_version_id,
                   quantity, amount, pricing_trace)
                VALUES
                  (%(bill_id)s, %(event_id)s, %(line_kind)s, %(period_attribution)s,
                   %(price_version_id)s, %(quantity)s, %(amount)s, %(trace)s)
                """,
                [
                    {
                        "bill_id": bill["id"],
                        "event_id": p["event"]["id"],
                        "line_kind": p["line_kind"],
                        "period_attribution": p["attribution"],
                        "price_version_id": p["price"].id,
                        "quantity": p["event"]["quantity"],
                        "amount": p["amount"],
                        "trace": Jsonb(p["trace"]),
                    }
                    for p in priced
                ],
            )

        cur.execute(
            """
            UPDATE billing_periods
               SET status='closed',
                   cutoff_recv_seq=%s,
                   closed_at=now(),
                   bill_id=%s
             WHERE id=%s
             RETURNING *
            """,
            (cutoff_seq, bill["id"], period["id"]),
        )
        period_closed = cur.fetchone()

    return {"bill": get_bill(conn, str(bill["id"])), "period": period_closed}


# ----------------------------------------------------------------- 账单查询 ----
def get_bill(conn: psycopg.Connection, bill_id: str) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM bills WHERE id=%s", (bill_id,))
        bill = cur.fetchone()
        if bill is None:
            raise ApiError(404, f"bill {bill_id} not found")
        cur.execute(
            """
            SELECT bl.*,
                   e.source       AS source,
                   e.event_id     AS event_id_business,
                   e.event_type   AS event_type,
                   e.occurred_at  AS event_occurred_at,
                   e.recv_seq     AS event_recv_seq
              FROM bill_lines bl
              JOIN usage_events e ON e.id = bl.event_id
             WHERE bl.bill_id=%s
             ORDER BY bl.created_at ASC, bl.id ASC
            """,
            (bill_id,),
        )
        lines = list(cur.fetchall())
    bill = dict(bill)
    bill["lines"] = lines
    return bill


def list_bills(conn: psycopg.Connection, tenant_id: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT b.* FROM bills b
             WHERE b.tenant_id=%s
             ORDER BY b.generated_at DESC
            """,
            (tenant_id,),
        )
        return list(cur.fetchall())


def serialize_bill(bill: dict, *, include_lines: bool = True) -> dict:
    out = {
        "id": str(bill["id"]),
        "tenant_id": str(bill["tenant_id"]),
        "period_id": str(bill["period_id"]),
        "currency": bill["currency"],
        "cutoff_recv_seq": bill["cutoff_recv_seq"],
        "total_amount": bill["total_amount"],
        "generated_at": bill["generated_at"],
    }
    if include_lines:
        out["lines"] = [_serialize_line(l) for l in bill.get("lines", [])]
    return out


def _serialize_line(line: dict) -> dict:
    line_id = line.get("id")
    return {
        "id": str(line_id) if line_id is not None else None,
        "event_id": str(line["event_id"]),
        "source": line.get("source"),
        "event_id_business": line.get("event_id_business"),
        "event_type": line.get("event_type"),
        "occurred_at": line.get("event_occurred_at"),
        "recv_seq": line.get("event_recv_seq"),
        "line_kind": line["line_kind"],
        "period_attribution": line["period_attribution"],
        "price_version_id": str(line["price_version_id"]),
        "quantity": line["quantity"],
        "amount": line["amount"],
        "pricing_trace": line["pricing_trace"],
    }


def serialize_projection(proj: dict) -> dict:
    payload = {
        "period_id": str(proj["period"]["id"]),
        "period_start": proj["period"]["period_start"],
        "period_end": proj["period"]["period_end"],
        "status": proj["period"]["status"],
        "cutoff_recv_seq": proj["cutoff_recv_seq"],
        "currency": proj["currency"],
        "total_amount": proj["total_amount"],
        "unpriceable_events": proj["unpriceable"],
        "already_closed": proj.get("already_closed", False),
        "lines": [
            _serialize_line(
                {
                    "event_id": p["event"]["id"],
                    "line_kind": p["line_kind"],
                    "period_attribution": p["attribution"],
                    "price_version_id": p["price"].id,
                    "quantity": p["event"]["quantity"],
                    "amount": p["amount"],
                    "pricing_trace": p["trace"],
                    "source": p["event"]["source"],
                    "event_id_business": p["event"]["event_id"],
                    "event_type": p["event"]["event_type"],
                    "event_occurred_at": p["event"]["occurred_at"],
                    "event_recv_seq": p["event"]["recv_seq"],
                }
            )
            for p in proj["lines"]
        ],
    }
    return payload


# ----------------------------------------------------------------- 重算核对 ----
def reconcile_bill(conn: psycopg.Connection, bill_id: str) -> dict:
    """按固化的价格版本与截止线重新计价，比对已存储账单。

    任何账单在任何时候调用都应得到完全一致的金额：
    事件账册只追加、价格版本与阶梯不可变、截止线已固化。
    """
    bill = get_bill(conn, bill_id)
    period = get_period(conn, str(bill["period_id"]))
    cutoff_seq = bill["cutoff_recv_seq"]

    with conn.cursor() as cur:
        candidates = _select_candidates(
            cur,
            tenant_id=bill["tenant_id"],
            cutoff_seq=cutoff_seq,
            period_start=period["period_start"],
            period_end=period["period_end"],
            exclude_billed=False,
            bill_id=bill["id"],
        )
    stored_by_event = {line["event_id"]: line for line in bill["lines"]}
    candidate_ids = {e["id"] for e in candidates}
    stored_ids = set(stored_by_event.keys())

    mismatches: list[dict] = []
    for eid in candidate_ids - stored_ids:
        mismatches.append({"event_id": str(eid), "reason": "in recomputation but missing from stored bill"})
    for eid in stored_ids - candidate_ids:
        mismatches.append({"event_id": str(eid), "reason": "in stored bill but absent from recomputation"})

    recomputed_total = ZERO
    for event in candidates:
        stored = stored_by_event.get(event["id"])
        # 分类在“事件被放入哪张账单”时确定；重算时存储行是权威，
        # 否则迟到事件在更晚的周期重算中会被重新归类而误报。
        if stored is not None:
            line_kind = stored["line_kind"]
            attribution = stored["period_attribution"]
        else:
            line_kind, attribution = _classify(
                event, period["period_start"], period["period_end"]
            )
        # 关键：使用账单项上固化的 price_version_id，而非“当前生效价格”
        version_id = stored["price_version_id"] if stored is not None else None
        if version_id is None:
            mismatches.append({"event_id": str(event["id"]), "reason": "no stored line"})
            continue
        price = _load_price_version_object(conn, str(version_id))
        trace = price_event(event["quantity"], price, scale=settings.currency_scale)
        recomputed_total += Decimal(trace["amount"])
        if stored is not None and Decimal(stored["amount"]) != Decimal(trace["amount"]):
            mismatches.append(
                {
                    "event_id": str(event["id"]),
                    "reason": "amount differs",
                    "stored_amount": str(stored["amount"]),
                    "recomputed_amount": trace["amount"],
                }
            )

    return {
        "bill_id": bill_id,
        "matches": len(mismatches) == 0 and recomputed_total == bill["total_amount"],
        "stored_total_amount": bill["total_amount"],
        "recomputed_total_amount": recomputed_total,
        "cutoff_recv_seq": cutoff_seq,
        "line_count": len(bill["lines"]),
        "mismatches": mismatches,
        "note": (
            "recomputed from immutable events, frozen price versions "
            "(bill_lines.price_version_id) and the frozen cutoff_recv_seq"
        ),
    }
