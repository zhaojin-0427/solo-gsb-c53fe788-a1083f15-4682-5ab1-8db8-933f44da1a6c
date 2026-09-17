"""End-to-end API tests. Require a running stack:

    docker compose up --build -d
    docker compose exec app pytest tests/test_api.py -v

(or BASE_URL=http://localhost:8000 pytest tests/test_api.py from the host)
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from decimal import Decimal

import httpx
import pytest

BASE = os.environ.get("BASE_URL", "http://localhost:8000")
D = Decimal


def uid() -> str:
    return uuid.uuid4().hex[:10]


@pytest.fixture(scope="session")
def client():
    c = httpx.Client(base_url=BASE, timeout=30.0)
    for _ in range(60):
        try:
            if c.get("/health").status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(1)
    else:
        pytest.fail("API not reachable at " + BASE)
    yield c
    c.close()


def make_tenant(client, tiers=None, currency="USD"):
    """Create plan + one price version + tenant. Returns tenant code."""
    suffix = uid()
    plan = client.post("/plans", json={"name": f"plan-{suffix}", "currency": currency})
    assert plan.status_code == 201, plan.text
    plan_id = plan.json()["id"]
    tiers = tiers or [{"metric": "api_calls", "up_to": None, "unit_price": "0.10"}]
    ver = client.post(
        f"/plans/{plan_id}/versions",
        json={
            "version": 1,
            "effective_from": "2026-01-01T00:00:00Z",
            "effective_to": None,
            "tiers": tiers,
        },
    )
    assert ver.status_code == 201, ver.text
    code = f"tenant-{suffix}"
    t = client.post("/tenants", json={"code": code, "name": code, "plan_id": plan_id})
    assert t.status_code == 201, t.text
    return code


def ingest(client, tenant, event_id, occurred_at, quantity, metric="api_calls",
           source="meter", dimensions=None):
    return client.post(
        "/usage",
        json={
            "tenant_code": tenant,
            "source": source,
            "event_id": event_id,
            "occurred_at": occurred_at,
            "quantity": quantity,
            "metric": metric,
            "dimensions": dimensions or {},
        },
    )


def make_period(client, tenant, start, end):
    r = client.post("/periods", json={"tenant_code": tenant, "period_start": start, "period_end": end})
    assert r.status_code == 201, r.text
    return r.json()["id"]


# ------------------------------------------------------------------ ingestion
class TestIngestion:
    def test_idempotent_replay(self, client):
        tenant = make_tenant(client)
        r1 = ingest(client, tenant, "e1", "2026-01-10T10:00:00Z", "100")
        assert r1.status_code == 201 and r1.json()["created"] is True
        r2 = ingest(client, tenant, "e1", "2026-01-10T10:00:00Z", "100")
        assert r2.status_code == 200
        body = r2.json()
        assert body["created"] is False and body["deduplicated"] is True
        assert body["record"]["id"] == r1.json()["record"]["id"]

    def test_same_key_different_content_conflicts(self, client):
        tenant = make_tenant(client)
        ingest(client, tenant, "e1", "2026-01-10T10:00:00Z", "100")
        r = ingest(client, tenant, "e1", "2026-01-10T10:00:00Z", "999")
        assert r.status_code == 409
        assert r.json()["existing"]["quantity"] == "100.000000"

    def test_correction_appends_linked_record(self, client):
        tenant = make_tenant(client)
        ingest(client, tenant, "e1", "2026-01-10T10:00:00Z", "100")
        c = client.post(
            "/usage/meter/e1/corrections",
            json={
                "tenant_code": tenant,
                "occurred_at": "2026-01-10T10:00:00Z",
                "quantity": "150",
                "metric": "api_calls",
                "dimensions": {},
                "correction_event_id": "e1-corr-1",
            },
        )
        assert c.status_code == 201, c.text
        corr = c.json()["record"]
        assert corr["record_type"] == "correction" and corr["quantity"] == "150.000000"

        # correction itself is idempotent
        c2 = client.post(
            "/usage/meter/e1/corrections",
            json={
                "tenant_code": tenant,
                "occurred_at": "2026-01-10T10:00:00Z",
                "quantity": "150",
                "metric": "api_calls",
                "dimensions": {},
                "correction_event_id": "e1-corr-1",
            },
        )
        assert c2.status_code == 200 and c2.json()["record"]["id"] == corr["id"]

        # chain shows original + correction, original untouched
        chain = client.get("/usage/meter/e1/chain", params={"tenant_code": tenant}).json()
        assert [r["record_type"] for r in chain] == ["event", "correction"]
        assert chain[0]["quantity"] == "100.000000"

    def test_reversal_voids_event_and_is_idempotent(self, client):
        tenant = make_tenant(client)
        ingest(client, tenant, "e1", "2026-01-10T10:00:00Z", "100")
        r1 = client.post("/usage/meter/e1/reversals", json={"tenant_code": tenant, "reason": "duplicate"})
        assert r1.status_code == 201 and r1.json()["record"]["record_type"] == "reversal"
        r2 = client.post("/usage/meter/e1/reversals", json={"tenant_code": tenant})
        assert r2.status_code == 200 and r2.json()["record"]["id"] == r1.json()["record"]["id"]
        # a reversed event can no longer be corrected
        c = client.post(
            "/usage/meter/e1/corrections",
            json={
                "tenant_code": tenant,
                "occurred_at": "2026-01-10T10:00:00Z",
                "quantity": "1",
                "metric": "api_calls",
                "dimensions": {},
            },
        )
        assert c.status_code == 409


# ------------------------------------------------------------------ pricing admin
class TestPricing:
    def test_overlapping_versions_rejected(self, client):
        plan = client.post("/plans", json={"name": f"p-{uid()}", "currency": "USD"}).json()
        pid = plan["id"]
        v1 = client.post(
            f"/plans/{pid}/versions",
            json={
                "version": 1,
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": "2026-06-01T00:00:00Z",
                "tiers": [{"metric": "m", "up_to": None, "unit_price": "1"}],
            },
        )
        assert v1.status_code == 201
        overlap = client.post(
            f"/plans/{pid}/versions",
            json={
                "version": 2,
                "effective_from": "2026-05-01T00:00:00Z",
                "effective_to": None,
                "tiers": [{"metric": "m", "up_to": None, "unit_price": "2"}],
            },
        )
        assert overlap.status_code == 409
        adjacent = client.post(
            f"/plans/{pid}/versions",
            json={
                "version": 2,
                "effective_from": "2026-06-01T00:00:00Z",
                "effective_to": None,
                "tiers": [{"metric": "m", "up_to": None, "unit_price": "2"}],
            },
        )
        assert adjacent.status_code == 201

    def test_invalid_tiers_rejected(self, client):
        plan = client.post("/plans", json={"name": f"p-{uid()}", "currency": "USD"}).json()
        r = client.post(
            f"/plans/{plan['id']}/versions",
            json={
                "version": 1,
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
                "tiers": [{"metric": "m", "up_to": "100", "unit_price": "1"}],  # no unbounded tier
            },
        )
        assert r.status_code == 422


# ------------------------------------------------------------------ settlement
class TestSettlement:
    def test_preview_close_and_verify(self, client):
        tenant = make_tenant(
            client,
            tiers=[
                {"metric": "api_calls", "up_to": "100", "unit_price": "0.10"},
                {"metric": "api_calls", "up_to": None, "unit_price": "0.05"},
            ],
        )
        ingest(client, tenant, "e1", "2026-01-10T10:00:00Z", "120")  # 100*0.10 + 20*0.05 = 11.00
        ingest(client, tenant, "e2", "2026-01-11T10:00:00Z", "30")   # 30*0.05 = 1.50
        ingest(client, tenant, "late", "2026-03-01T00:00:00Z", "999")  # outside window
        pid = make_period(client, tenant, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")

        prev = client.post(f"/periods/{pid}/preview")
        assert prev.status_code == 200, prev.text
        assert D(prev.json()["total_amount"]) == D("12.50")

        # preview persists nothing
        assert client.get(f"/tenants/{tenant}/bills").json() == []

        bill = client.post(f"/periods/{pid}/close")
        assert bill.status_code == 200, bill.text
        body = bill.json()
        assert D(body["total_amount"]) == D("12.50")
        assert len(body["lines"]) == 2
        by_event = {l["event_id"]: l for l in body["lines"]}
        assert D(by_event["e1"]["amount"]) == D("11.00")
        assert D(by_event["e2"]["amount"]) == D("1.50")
        # trace shows the bracket walk
        brackets = by_event["e1"]["trace"]["brackets"]
        assert [(b["from"], b["to"]) for b in brackets] == [("0", "100"), ("100", "120")]

        # bill is reproducible from frozen inputs
        v = client.post(f"/bills/{body['id']}/verify").json()
        assert v["ok"] is True and v["mismatches"] == []

        # cannot close / reopen twice
        again = client.post(f"/periods/{pid}/close")
        assert again.status_code == 409

    def test_late_correction_and_reversal_become_next_period_adjustments(self, client):
        tenant = make_tenant(client)  # flat 0.10/unit
        ingest(client, tenant, "e1", "2026-01-10T10:00:00Z", "100")
        ingest(client, tenant, "e2", "2026-01-12T10:00:00Z", "50")
        p1 = make_period(client, tenant, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
        p2 = make_period(client, tenant, "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")

        bill1 = client.post(f"/periods/{p1}/close").json()
        assert D(bill1["total_amount"]) == D("15.00")

        # late correction (after cutoff of p1): 100 -> 150  => +5.00
        client.post(
            "/usage/meter/e1/corrections",
            json={
                "tenant_code": tenant,
                "occurred_at": "2026-01-10T10:00:00Z",
                "quantity": "150",
                "metric": "api_calls",
                "dimensions": {},
            },
        )
        # late reversal of e2 => -5.00
        client.post("/usage/meter/e2/reversals", json={"tenant_code": tenant})
        # fresh usage inside p2 window: 20 * 0.10 = 2.00
        ingest(client, tenant, "e3", "2026-02-05T10:00:00Z", "20")

        bill2 = client.post(f"/periods/{p2}/close").json()
        lines = bill2["lines"]
        adj = {l["event_id"]: l for l in lines if l["line_kind"] == "adjustment"}
        cur = {l["event_id"]: l for l in lines if l["line_kind"] == "current"}
        assert D(adj["e1"]["amount"]) == D("5.00")
        assert adj["e1"]["origin_period_id"] == p1
        assert D(adj["e2"]["amount"]) == D("-5.00")
        assert D(cur["e3"]["amount"]) == D("2.00")
        assert D(bill2["total_amount"]) == D("2.00")

        # history is immutable: bill1 unchanged, both bills verifiable
        assert D(client.get(f"/bills/{bill1['id']}").json()["total_amount"]) == D("15.00")
        assert client.post(f"/bills/{bill1['id']}/verify").json()["ok"] is True
        assert client.post(f"/bills/{bill2['id']}/verify").json()["ok"] is True

    def test_adjustments_use_origin_period_frozen_prices(self, client):
        # v1: 0.10/unit from 2026-01-01; v2: 0.20/unit from 2026-02-01
        suffix = uid()
        plan = client.post("/plans", json={"name": f"p-{suffix}", "currency": "USD"}).json()
        pid = plan["id"]
        for ver, frm, to, price in (
            (1, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z", "0.10"),
            (2, "2026-02-01T00:00:00Z", None, "0.20"),
        ):
            r = client.post(
                f"/plans/{pid}/versions",
                json={
                    "version": ver,
                    "effective_from": frm,
                    "effective_to": to,
                    "tiers": [{"metric": "api_calls", "up_to": None, "unit_price": price}],
                },
            )
            assert r.status_code == 201, r.text
        tenant = f"t-{suffix}"
        client.post("/tenants", json={"code": tenant, "name": tenant, "plan_id": pid})

        ingest(client, tenant, "e1", "2026-01-10T10:00:00Z", "100")
        p1 = make_period(client, tenant, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
        p2 = make_period(client, tenant, "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")
        client.post(f"/periods/{p1}/close")

        # late correction of a January event: repriced at January's frozen
        # version (0.10), not February's 0.20 => delta = 50 * 0.10 = 5.00
        client.post(
            "/usage/meter/e1/corrections",
            json={
                "tenant_code": tenant,
                "occurred_at": "2026-01-10T10:00:00Z",
                "quantity": "150",
                "metric": "api_calls",
                "dimensions": {},
            },
        )
        bill2 = client.post(f"/periods/{p2}/close").json()
        adj = [l for l in bill2["lines"] if l["line_kind"] == "adjustment"]
        assert len(adj) == 1 and D(adj[0]["amount"]) == D("5.00")
        assert client.post(f"/bills/{bill2['id']}/verify").json()["ok"] is True

    def test_close_requires_order(self, client):
        tenant = make_tenant(client)
        p1 = make_period(client, tenant, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")
        p2 = make_period(client, tenant, "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z")
        r = client.post(f"/periods/{p2}/close")
        assert r.status_code == 409
        assert client.post(f"/periods/{p1}/close").status_code == 200
        assert client.post(f"/periods/{p2}/close").status_code == 200


# ------------------------------------------------------------------ cutoff race
class TestCutoffDeterminism:
    def test_concurrent_ingest_lands_on_one_side_of_cutoff(self, client):
        tenant = make_tenant(client)
        pid = make_period(client, tenant, "2026-01-01T00:00:00Z", "2026-02-01T00:00:00Z")

        stop = threading.Event()
        errors: list[str] = []

        def hammer(worker: int) -> None:
            i = 0
            while not stop.is_set():
                i += 1
                try:
                    r = ingest(client, tenant, f"w{worker}-{i}", "2026-01-15T00:00:00Z", "1")
                    if r.status_code not in (200, 201):
                        errors.append(f"worker {worker}: {r.status_code} {r.text}")
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"worker {worker}: {exc}")

        threads = [threading.Thread(target=hammer, args=(w,)) for w in range(4)]
        for t in threads:
            t.start()
        time.sleep(0.5)  # let some records land before the close
        close_resp = client.post(f"/periods/{pid}/close")
        stop.set()
        for t in threads:
            t.join()

        assert not errors, errors
        assert close_resp.status_code == 200, close_resp.text
        bill = close_resp.json()
        cutoff = bill["cutoff_seq"]

        # every ledger record in the window is billed iff recv_seq <= cutoff
        records = client.get(f"/tenants/{tenant}/usage", params={"limit": 1000}).json()
        billed = {l["event_id"] for l in bill["lines"] if l["line_kind"] == "current"}
        for rec in records:
            expect_billed = rec["recv_seq"] <= cutoff
            assert (rec["event_id"] in billed) == expect_billed, (
                f"record {rec['event_id']} recv_seq={rec['recv_seq']} cutoff={cutoff} "
                f"billed={rec['event_id'] in billed}"
            )
        # and the bill still verifies
        assert client.post(f"/bills/{bill['id']}/verify").json()["ok"] is True
