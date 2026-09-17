"""HTTP 路由层。

写接口成功时提交事务；抛出 ApiError 时连接池上下文自动回滚。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from psycopg import Connection

from .. import billing, events, periods, prices
from ..deps import ApiError, get_conn
from ..models import (
    BillOut,
    CloseResultOut,
    EventIn,
    EventOut,
    PeriodIn,
    PeriodOut,
    PriceVersionIn,
    PriceVersionOut,
    ReconcileOut,
    TenantIn,
    TenantOut,
    TrialOut,
)

router = APIRouter()


# ----------------------------------------------------------------- tenants ----
@router.post("/tenants", response_model=TenantOut, status_code=201, tags=["tenants"])
def create_tenant(body: TenantIn, conn: Connection = Depends(get_conn)):
    return periods.serialize_tenant(periods.create_tenant(conn, body.name))


@router.get("/tenants", response_model=list[TenantOut], tags=["tenants"])
def list_tenants(conn: Connection = Depends(get_conn)):
    return [periods.serialize_tenant(t) for t in periods.list_tenants(conn)]


# ------------------------------------------------------------------ events ----
@router.post(
    "/tenants/{tenant_id}/events",
    response_model=EventOut,
    status_code=202,
    tags=["events"],
)
def ingest_event(tenant_id: str, body: EventIn, conn: Connection = Depends(get_conn)):
    row, idempotent = events.ingest_event(conn, tenant_id, body)
    return events.serialize_event(row, idempotent=idempotent)


@router.get(
    "/tenants/{tenant_id}/events",
    response_model=list[EventOut],
    tags=["events"],
)
def list_events(
    tenant_id: str,
    source: str | None = None,
    limit: int = 100,
    conn: Connection = Depends(get_conn),
):
    rows = events.list_events(conn, tenant_id, source=source, limit=limit)
    return [events.serialize_event(r) for r in rows]


# ------------------------------------------------------------------ prices ----
@router.post(
    "/price-versions",
    response_model=PriceVersionOut,
    status_code=201,
    tags=["prices"],
)
def create_price_version(body: PriceVersionIn, conn: Connection = Depends(get_conn)):
    return prices.create_price_version(conn, body)


@router.get("/price-versions", response_model=list[PriceVersionOut], tags=["prices"])
def list_price_versions(
    source: str | None = None,
    tenant_id: str | None = None,
    conn: Connection = Depends(get_conn),
):
    return prices.list_price_versions(conn, source=source, tenant_id=tenant_id)


@router.get(
    "/price-versions/{version_id}", response_model=PriceVersionOut, tags=["prices"]
)
def get_price_version(version_id: str, conn: Connection = Depends(get_conn)):
    return prices.get_price_version(conn, version_id)


# ----------------------------------------------------------------- periods ----
@router.post(
    "/tenants/{tenant_id}/periods",
    response_model=PeriodOut,
    status_code=201,
    tags=["billing"],
)
def create_period(tenant_id: str, body: PeriodIn, conn: Connection = Depends(get_conn)):
    row = periods.create_period(conn, tenant_id, body.period_start, body.period_end)
    return periods.serialize_period(row)


@router.get(
    "/tenants/{tenant_id}/periods",
    response_model=list[PeriodOut],
    tags=["billing"],
)
def list_periods(tenant_id: str, conn: Connection = Depends(get_conn)):
    return [periods.serialize_period(p) for p in periods.list_periods(conn, tenant_id)]


@router.post(
    "/periods/{period_id}/close",
    response_model=CloseResultOut,
    status_code=200,
    tags=["billing"],
)
def close_period(period_id: str, conn: Connection = Depends(get_conn)):
    result = billing.close_period(conn, period_id)
    return {
        "period": periods.serialize_period(result["period"]),
        "bill": billing.serialize_bill(result["bill"]),
    }


@router.get("/periods/{period_id}/trial", response_model=TrialOut, tags=["billing"])
def trial_period(period_id: str, conn: Connection = Depends(get_conn)):
    """只读试算：open 周期投影当前账单；closed 周期返回已固化账单。"""
    return billing.serialize_projection(billing.trial(conn, period_id))


# ------------------------------------------------------------------- bills ----
@router.get("/tenants/{tenant_id}/bills", response_model=list[BillOut], tags=["bills"])
def list_bills(tenant_id: str, conn: Connection = Depends(get_conn)):
    out = []
    for head in billing.list_bills(conn, tenant_id):
        bill = billing.get_bill(conn, str(head["id"]))
        out.append(billing.serialize_bill(bill))
    return out


@router.get("/bills/{bill_id}", response_model=BillOut, tags=["bills"])
def get_bill(bill_id: str, conn: Connection = Depends(get_conn)):
    return billing.serialize_bill(billing.get_bill(conn, bill_id))


@router.get("/bills/{bill_id}/reconcile", response_model=ReconcileOut, tags=["billing"])
def reconcile_bill(bill_id: str, conn: Connection = Depends(get_conn)):
    """按固化价格版本与截止线重算并比对账单，验证可重复性。"""
    return billing.reconcile_bill(conn, bill_id)


@router.get("/bills/{bill_id}/lines/{line_id}/trace", tags=["billing"])
def get_line_trace(bill_id: str, line_id: str, conn: Connection = Depends(get_conn)):
    """逐事件计价轨迹：档位切片、单价、flat、原始/固化金额。"""
    bill = billing.get_bill(conn, bill_id)
    for line in bill["lines"]:
        if str(line["id"]) == line_id:
            return {"bill_id": bill_id, **_line_trace(line)}
    raise ApiError(404, f"line {line_id} not found in bill {bill_id}")


def _line_trace(line: dict) -> dict:
    occurred = line.get("event_occurred_at")
    return {
        "line_id": str(line["id"]),
        "event_id": str(line["event_id"]),
        "source": line.get("source"),
        "event_id_business": line.get("event_id_business"),
        "event_type": line.get("event_type"),
        "occurred_at": occurred.isoformat() if occurred else None,
        "recv_seq": line.get("event_recv_seq"),
        "line_kind": line["line_kind"],
        "period_attribution": line["period_attribution"],
        "price_version_id": str(line["price_version_id"]),
        "quantity": str(line["quantity"]),
        "amount": str(line["amount"]),
        "pricing_trace": line["pricing_trace"],
    }
