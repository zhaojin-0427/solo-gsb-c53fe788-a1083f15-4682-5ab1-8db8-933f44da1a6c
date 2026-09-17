"""PostgreSQL 集成测试：仅当 DATABASE_URL 指向可用数据库时运行，否则整体跳过。

    DATABASE_URL=postgresql://billing:billing@localhost:5432/billing \
        python3 -m unittest tests.test_integration -v

测试库会被 DROP/CREATE 表，请使用专用数据库。
"""
import os
import unittest
from datetime import datetime, timezone
from decimal import Decimal

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool
    _PSYCOPG = True
except ImportError:
    psycopg = None
    ConnectionPool = None
    dict_row = None
    _PSYCOPG = False

DB_URL = os.environ.get("DATABASE_URL", "postgresql://billing:billing@localhost:5432/billing")
SCHEMA = os.path.join(os.path.dirname(__file__), "..", "..", "db", "schema.sql")

if _PSYCOPG:
    from app.billing import close_period, reconcile_bill, trial
    from app.deps import ApiError
    from app.events import ingest_event
    from app.periods import create_period, create_tenant
    from app.prices import create_price_version


def _db_available() -> bool:
    if psycopg is None:
        return False
    try:
        with psycopg.connect(DB_URL, connect_timeout=2) as conn:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


@unittest.skipUnless(_db_available(), "PostgreSQL not available; skipping integration tests")
class BillingFlowTest(unittest.TestCase):
    pool = None

    @classmethod
    def setUpClass(cls):
        # 此时数据库确认可用，再导入依赖 psycopg 的应用模块
        from app.billing import close_period, reconcile_bill, trial  # noqa: F401
        from app.events import ingest_event  # noqa: F401
        from app.periods import create_period, create_tenant  # noqa: F401
        from app.prices import create_price_version  # noqa: F401

        cls.pool = ConnectionPool(
            DB_URL, min_size=1, max_size=5,
            kwargs={"row_factory": dict_row}, open=True,
        )
        with open(SCHEMA, encoding="utf-8") as f:
            ddl = f.read()
        # 清理旧数据（触发器禁删，故用 TRUNCATE ... CASCADE）并重建结构
        with psycopg.connect(DB_URL, autocommit=True) as admin:
            with admin.cursor() as cur:
                for tbl in (
                    "bill_lines", "bills", "billing_periods", "usage_events",
                    "price_tiers", "price_versions", "tenant_counters", "tenants",
                ):
                    cur.execute(f"TRUNCATE TABLE {tbl} RESTART IDENTITY CASCADE")
                cur.execute(ddl)

    @classmethod
    def tearDownClass(cls):
        if cls.pool is not None:
            cls.pool.close()

    def _conn(self):
        return self.pool.connection()

    def _bootstrap(self):
        class Body:
            pass

        with self._conn() as conn:
            t = create_tenant(conn, f"t-{os.getpid()}-{id(self)}")
            tid = str(t["id"])

            pv_body = Body()
            pv_body.tenant_id = None
            pv_body.source = "api_calls"
            pv_body.currency = "USD"
            pv_body.pricing_mode = "graduated"
            pv_body.effective_from = datetime(2025, 1, 1, tzinfo=timezone.utc)
            pv_body.effective_to = None

            class T:
                def __init__(self, i, lo, hi, unit, flat="0"):
                    self.tier_index, self.from_qty, self.up_to_qty = i, Decimal(lo), (Decimal(hi) if hi else None)
                    self.unit_amount, self.flat_amount = Decimal(unit), Decimal(flat)

            pv_body.tiers = [T(0, "0", "100", "0.10"), T(1, "100", "1000", "0.05"), T(2, "1000", None, "0.02")]
            create_price_version(conn, pv_body)

            p1 = create_period(conn, tid,
                               datetime(2026, 1, 1, tzinfo=timezone.utc),
                               datetime(2026, 2, 1, tzinfo=timezone.utc))
            p2 = create_period(conn, tid,
                               datetime(2026, 2, 1, tzinfo=timezone.utc),
                               datetime(2026, 3, 1, tzinfo=timezone.utc))
            return tid, str(p1["id"]), str(p2["id"])

    def _event(self, conn, tid, eid, etype, qty, linked=None, occurred=None):
        class E:
            pass
        b = E()
        b.source = "api_calls"
        b.event_id = eid
        b.event_type = etype
        b.linked_event_id = linked
        b.quantity = None if qty is None else Decimal(qty)
        b.dimensions = {}
        b.occurred_at = occurred or datetime(2026, 1, 15, tzinfo=timezone.utc)
        row, idempotent = ingest_event(conn, tid, b)
        return row, idempotent

    def test_full_flow_idempotency_conflict_cutoff_adjustments(self):
        tid, p1, p2 = self._bootstrap()

        with self._conn() as conn:
            ev1, _ = self._event(conn, tid, "e1", "usage", "1500",
                                 occurred=datetime(2026, 1, 10, tzinfo=timezone.utc))
            # 幂等
            _, idem = self._event(conn, tid, "e1", "usage", "1500",
                                  occurred=datetime(2026, 1, 10, tzinfo=timezone.utc))
            self.assertTrue(idem)
            # 同键异内容
            from app.deps import ApiError
            with self.assertRaises(ApiError) as cm:
                self._event(conn, tid, "e1", "usage", "1501",
                            occurred=datetime(2026, 1, 10, tzinfo=timezone.utc))
            self.assertEqual(cm.exception.status_code, 409)
            # 追加修正 1500 -> 1600
            fix, _ = self._event(conn, tid, "e1-fix", "correction", "1600",
                                 linked=str(ev1["id"]),
                                 occurred=datetime(2026, 1, 12, tzinfo=timezone.utc))
            self.assertEqual(fix["quantity"], Decimal("100"))

            proj = trial(conn, p1)
            self.assertEqual(proj["total_amount"], Decimal("70.00"))

            result = close_period(conn, p1)
        bill1 = result["bill"]
        self.assertEqual(bill1["total_amount"], Decimal("70.00"))
        self.assertEqual(bill1["cutoff_recv_seq"], 2)

        with self._conn() as conn:
            rec = reconcile_bill(conn, str(bill1["id"]))
        self.assertTrue(rec["matches"], rec)

        # 迟到的 1 月用量 + 2 月撤销
        with self._conn() as conn:
            self._event(conn, tid, "e-late", "usage", "200",
                        occurred=datetime(2026, 1, 20, tzinfo=timezone.utc))
            self._event(conn, tid, "e1-cancel", "cancellation", None,
                        linked=str(ev1["id"]),
                        occurred=datetime(2026, 2, 10, tzinfo=timezone.utc))
            result2 = close_period(conn, p2)
        bill2 = result2["bill"]
        attrs = sorted((l["period_attribution"], l["event_type"]) for l in bill2["lines"])
        self.assertIn(("correction", "cancellation"), attrs)
        self.assertIn(("late", "usage"), attrs)

        with self._conn() as conn:
            rec2 = reconcile_bill(conn, str(bill2["id"]))
        self.assertTrue(rec2["matches"], rec2)

        # 重开已关闭周期必须失败
        from app.deps import ApiError
        with self._conn() as conn:
            with self.assertRaises(ApiError):
                close_period(conn, p1)

        # 直接 UPDATE 账单/事件应被数据库触发器拒绝
        with self.assertRaises(Exception):
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE bills SET total_amount = 0 WHERE id=%s", (bill1["id"],))

    def test_append_only_event_trigger(self):
        tid, p1, _ = self._bootstrap()
        with self._conn() as conn:
            ev, _ = self._event(conn, tid, "imm", "usage", "10")
        with self.assertRaises(Exception):
            with self._conn() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE usage_events SET quantity = 1 WHERE id=%s", (ev["id"],))


if __name__ == "__main__":
    unittest.main()
