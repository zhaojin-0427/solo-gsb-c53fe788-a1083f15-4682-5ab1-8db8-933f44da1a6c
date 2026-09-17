#!/usr/bin/env python3
"""截止线并发竞争测试（标准库）：

N 个线程并发上报同一租户事件，同时关闭周期。
断言不变量：
  1. 每条事件的 recv_seq 唯一且恰好落在一张已关闭账单中（或仍在打开周期）；
  2. 账单中不存在 recv_seq > cutoff_recv_seq 的行；
  3. 每张已关闭账单 reconcile 后 matches=true。

用法：docker compose up -d 后
    python3 scripts/concurrency_check.py
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

BASE = os.environ.get("BASE_URL", "http://localhost:8000/api/v1").rstrip("/")
H = {"Content-Type": "application/json"}
N = int(os.environ.get("N_EVENTS", "24"))


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, headers=H, method=method)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def ts(y, m, d):
    return datetime(y, m, d, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    tenant = call("POST", "/tenants", {"name": f"race-{uuid.uuid4().hex[:8]}"})
    tid = tenant["id"]
    call(
        "POST", "/price-versions",
        {
            "tenant_id": None, "source": "s", "currency": "USD",
            "pricing_mode": "volume", "effective_from": ts(2025, 1, 1),
            "tiers": [
                {"tier_index": 0, "from_qty": "0", "up_to_qty": "1000",
                 "unit_amount": "1", "flat_amount": "0"},
                {"tier_index": 1, "from_qty": "1000", "up_to_qty": None,
                 "unit_amount": "0.5", "flat_amount": "0"},
            ],
        },
    )
    p1 = call("POST", f"/tenants/{tid}/periods",
              {"period_start": ts(2026, 1, 1), "period_end": ts(2026, 2, 1)})
    call("POST", f"/tenants/{tid}/periods",
         {"period_start": ts(2026, 2, 1), "period_end": ts(2026, 3, 1)})

    def send(i):
        return call(
            "POST", f"/tenants/{tid}/events",
            {"source": "s", "event_id": f"r-{i}", "event_type": "usage",
             "occurred_at": ts(2026, 1, min(1 + i, 27)), "quantity": str(i + 1),
             "dimensions": {}},
        )

    def close(pid):
        return call("POST", f"/periods/{pid}/close", None)

    results: dict = {}
    with ThreadPoolExecutor(max_workers=N + 1) as pool:
        futs = [pool.submit(send, i) for i in range(N)]
        # 让事件先飞一会再关账，提高交错概率
        close_fut = pool.submit(close, p1["id"])
        for f in as_completed(futs):
            r = f.result()
            results[r["event_id"]] = r
        bill1 = close_fut.result()["bill"]

    # 关闭第二期，兜底收回截止线之后的事件
    periods = call("GET", f"/tenants/{tid}/periods")
    p2 = next(p for p in periods if p["status"] == "open")
    bill2 = call("POST", f"/periods/{p2['id']}/close", None)["bill"]

    # 不变量 1：recv_seq 唯一
    seqs = [r["recv_seq"] for r in results.values()]
    assert len(seqs) == len(set(seqs)), "recv_seq must be unique"

    # 不变量 2：bill1 行序号 <= bill1 cutoff
    cutoff1 = bill1["cutoff_recv_seq"]
    b1_events = {l["event_id_business"]: l for l in bill1["lines"]}
    assert all(l["recv_seq"] <= cutoff1 for l in bill1["lines"]), "line beyond cutoff!"
    cutoff2 = bill2["cutoff_recv_seq"]
    assert all(l["recv_seq"] <= cutoff2 for l in bill2["lines"]), "line beyond cutoff2!"

    # 不变量 3：每条事件恰好出现一次
    b1_ids = set(b1_events)
    b2_ids = {l["event_id_business"] for l in bill2["lines"]}
    assert not (b1_ids & b2_ids), "event billed twice!"
    assert b1_ids | b2_ids == {f"r-{i}" for i in range(N)}, (b1_ids | b2_ids)

    # 截止线一侧：seq <= cutoff 的事件必须都在 bill1
    for eid, r in results.items():
        if r["recv_seq"] <= cutoff1:
            assert eid in b1_ids, f"{eid} seq {r['recv_seq']} <= {cutoff1} missing from bill1"
        else:
            assert eid in b2_ids, f"{eid} seq {r['recv_seq']} > cutoff missing from bill2"

    for bid in (bill1["id"], bill2["id"]):
        rec = call("GET", f"/bills/{bid}/reconcile")
        assert rec["matches"] is True, rec

    print(f"RACE OK: {len(b1_ids)} events in bill1 (cutoff={cutoff1}), "
          f"{len(b2_ids)} in bill2; all reconcile matches=true")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"RACE FAILED: {e}", file=sys.stderr)
        sys.exit(1)
