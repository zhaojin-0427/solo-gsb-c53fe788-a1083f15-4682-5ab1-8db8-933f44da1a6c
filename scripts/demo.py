#!/usr/bin/env python3
"""端到端演示：幂等/冲突 -> 阶梯价 -> 试算 -> 关账固化截止线 ->
迟到与关闭后修正进入下期调整 -> 重算一致性。

仅使用标准库。先 `docker compose up -d`，再：
    BASE_URL=http://localhost:8000/api/v1 python3 scripts/demo.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("BASE_URL", "http://localhost:8000/api/v1").rstrip("/")
H = {"Content-Type": "application/json"}


def call(method: str, path: str, body: dict | None = None, *, expect: int | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=H, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            payload = json.loads(resp.read())
            if expect is not None and resp.status != expect:
                raise AssertionError(f"{method} {path}: expected {expect}, got {resp.status}")
            return payload
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        if expect is not None and e.code == expect:
            return json.loads(detail)
        raise AssertionError(f"{method} {path} -> {e.code}: {detail}") from None


def ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    now = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    p1_start, p1_end = datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 2, 1, tzinfo=timezone.utc)
    p2_end = datetime(2026, 3, 1, tzinfo=timezone.utc)

    # 1) 租户
    tenant = call("POST", "/tenants", {"name": f"demo-{uuid.uuid4().hex[:8]}"}, expect=201)
    tid = tenant["id"]
    print(f"tenant: {tid}")

    # 2) 默认阶梯价格（graduated）：0-100 @0.10，100-1000 @0.05，1000+ @0.02
    pv = call(
        "POST",
        "/price-versions",
        {
            "tenant_id": None,
            "source": "api_calls",
            "pricing_mode": "graduated",
            "currency": "USD",
            "effective_from": ts(datetime(2025, 1, 1, tzinfo=timezone.utc)),
            "tiers": [
                {"tier_index": 0, "from_qty": "0", "up_to_qty": "100",
                 "unit_amount": "0.10", "flat_amount": "0"},
                {"tier_index": 1, "from_qty": "100", "up_to_qty": "1000",
                 "unit_amount": "0.05", "flat_amount": "0"},
                {"tier_index": 2, "from_qty": "1000", "up_to_qty": None,
                 "unit_amount": "0.02", "flat_amount": "0"},
            ],
        },
        expect=201,
    )
    print(f"price version: {pv['id']}")

    # 3) 两个周期
    per1 = call("POST", f"/tenants/{tid}/periods",
                {"period_start": ts(p1_start), "period_end": ts(p1_end)}, expect=201)
    call("POST", f"/tenants/{tid}/periods",
         {"period_start": ts(p1_end), "period_end": ts(p2_end)}, expect=201)

    def event(eid, etype, qty, linked=None, occurred=None):
        return call(
            "POST", f"/tenants/{tid}/events",
            {
                "source": "api_calls", "event_id": eid, "event_type": etype,
                "occurred_at": ts(occurred or now),
                "linked_event_id": linked,
                "quantity": qty,
                "dimensions": {"region": "us-east"},
            },
        )

    # 本期 1500 次 -> 65.00
    ev1 = event("evt-1", "usage", "1500")
    assert ev1["recv_seq"] == 1
    print(f"usage evt-1 recv_seq={ev1['recv_seq']}")

    # 幂等重放
    again = event("evt-1", "usage", "1500")
    assert again["idempotent"] is True and again["recv_seq"] == 1
    # 同键异内容 -> 409
    conflict = event("evt-1", "usage", "1501") if False else call(
        "POST", f"/tenants/{tid}/events",
        {"source": "api_calls", "event_id": "evt-1", "event_type": "usage",
         "occurred_at": ts(now), "quantity": "1501", "dimensions": {"region": "us-east"}},
        expect=409,
    )
    assert "incoming_content_hash" in json.dumps(conflict)

    # 追加式修正：1500 -> 1600，差值 100（第二档 0.05）= +5
    ev_fix = event("evt-1-fix", "correction", "1600", linked=ev1["id"])
    print(f"correction delta qty stored: {ev_fix['quantity']} (expected 100)")

    # 试算：65 + 5 = 70
    trial = call("GET", f"/periods/{per1['id']}/trial")
    assert trial["total_amount"] == "70.00", trial["total_amount"]
    print(f"trial total: {trial['total_amount']}")

    # 4) 并发：关账的同时有一条“迟到的 1 月用量”到达。
    #    先让迟到事件在服务端排队于关账窗口——直接并发提交即可，
    #    截止线锁定保证它要么进本期要么进下期调整，绝不会两边都算。
    late_event_body = {
        "source": "api_calls", "event_id": "evt-late", "event_type": "usage",
        "occurred_at": ts(datetime(2026, 1, 20, tzinfo=timezone.utc)),
        "quantity": "200", "dimensions": {"region": "us-east"},
    }

    def post_late():
        return call("POST", f"/tenants/{tid}/events", late_event_body)

    with ThreadPoolExecutor(max_workers=2) as pool:
        f_close = pool.submit(call, "POST", f"/periods/{per1['id']}/close", None)
        f_late = pool.submit(post_late)
        closed = f_close.result()
        late = f_late.result()

    bill1 = closed["bill"]
    print(f"bill1 cutoff={bill1['cutoff_recv_seq']} total={bill1['total_amount']}")
    print(f"late event recv_seq={late['recv_seq']}")
    # 截止线不变式：迟到记录的序号要么 <= cutoff（必在 bill1），
    # 要么 > cutoff（必在 bill2 调整行）；不会两边都算或两边都不算。
    bill1_event_ids = {l["event_id"] for l in bill1["lines"]}
    if late["recv_seq"] <= bill1["cutoff_recv_seq"]:
        assert late["id"] in bill1_event_ids, "late event inside cutoff must be in bill1"
        in_bill1 = True
    else:
        assert late["id"] not in bill1_event_ids, "late event after cutoff must not be in bill1"
        in_bill1 = False

    # 重算账单 1 必须完全一致
    rec1 = call("GET", f"/bills/{bill1['id']}/reconcile")
    assert rec1["matches"] is True, rec1
    print(f"reconcile bill1: matches={rec1['matches']}")

    # 5) 关闭后的撤销（occurred_at 在 2 月）-> 下期调整
    cancel = event("evt-1-cancel", "cancellation", None, linked=ev1["id"],
                   occurred=datetime(2026, 2, 10, tzinfo=timezone.utc))
    print(f"cancellation delta qty stored: {cancel['quantity']} (expected -1600)")

    periods_ = call("GET", f"/tenants/{tid}/periods")
    per2 = next(p for p in periods_ if p["period_start"].startswith("2026-02"))
    close2 = call("POST", f"/periods/{per2['id']}/close", None)
    bill2 = close2["bill"]
    kinds = {(l["period_attribution"], l["event_type"]) for l in bill2["lines"]}
    print(f"bill2 total={bill2['total_amount']} line kinds={kinds}")
    # 撤销一定是下期的跨期调整；迟到 usage 若在关账后拿序号则同在此处
    assert ("correction", "cancellation") in kinds
    if not in_bill1:
        assert ("late", "usage") in kinds
    else:
        assert ("late", "usage") not in kinds
    # 明细中可逐事件查看计价轨迹
    for line in bill2["lines"]:
        tr = call("GET", f"/bills/{bill2['id']}/lines/{line['id']}/trace")
        assert tr["pricing_trace"]["amount"] == line["amount"]

    rec2 = call("GET", f"/bills/{bill2['id']}/reconcile")
    assert rec2["matches"] is True, rec2
    print(f"reconcile bill2: matches={rec2['matches']}")
    print("DEMO OK")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as exc:
        print(f"DEMO FAILED: {exc}", file=sys.stderr)
        sys.exit(1)
