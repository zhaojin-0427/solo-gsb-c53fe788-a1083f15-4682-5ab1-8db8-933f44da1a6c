"""内容指纹与时间规范化测试。"""
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from app.utils import canonical_payload, content_hash, normalize_occurred_at


class UtilsTest(unittest.TestCase):
    def test_naive_datetime_treated_as_utc(self):
        naive = datetime(2026, 1, 1, 12, 0, 0)
        aware = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(normalize_occurred_at(naive), normalize_occurred_at(aware))

    def test_canonical_payload_order_independent_dimensions(self):
        kw = dict(
            event_type="usage",
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            quantity=Decimal("10"),
            linked_event_id=None,
        )
        self.assertEqual(
            canonical_payload(dimensions={"a": 1, "b": 2}, **kw),
            canonical_payload(dimensions={"b": 2, "a": 1}, **kw),
        )
        self.assertEqual(
            content_hash(dimensions={"a": 1, "b": 2}, **kw),
            content_hash(dimensions={"b": 2, "a": 1}, **kw),
        )

    def test_different_content_different_hash(self):
        base = dict(
            event_type="usage",
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            dimensions={},
            linked_event_id=None,
        )
        self.assertNotEqual(
            content_hash(quantity=Decimal("10"), **base),
            content_hash(quantity=Decimal("11"), **base),
        )


if __name__ == "__main__":
    unittest.main()
