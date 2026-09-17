"""计价引擎单元测试：不依赖 PostgreSQL。

    python3 -m unittest discover -s tests -v
（安装 pytest 后 `pytest` 也可直接运行。）
"""
import unittest
from decimal import Decimal

from app.pricing import PriceVersion, Tier, price_event

V_GRADUATED = PriceVersion(
    id="pv-1",
    source="api_calls",
    currency="USD",
    pricing_mode="graduated",
    effective_from=None,
    effective_to=None,
    tenant_id=None,
    tiers=(
        Tier(0, Decimal("0"), Decimal("100"), Decimal("0.10"), Decimal("0")),
        Tier(1, Decimal("100"), Decimal("1000"), Decimal("0.05"), Decimal("0")),
        Tier(2, Decimal("1000"), None, Decimal("0.02"), Decimal("0")),
    ),
)

V_VOLUME = PriceVersion(
    id="pv-2",
    source="storage_gb",
    currency="USD",
    pricing_mode="volume",
    effective_from=None,
    effective_to=None,
    tenant_id=None,
    tiers=(
        Tier(0, Decimal("0"), Decimal("10"), Decimal("1.00"), Decimal("5")),
        Tier(1, Decimal("10"), Decimal("100"), Decimal("0.80"), Decimal("5")),
        Tier(2, Decimal("100"), None, Decimal("0.50"), Decimal("5")),
    ),
)


class PricingTest(unittest.TestCase):
    def test_graduated_slices_across_tiers(self):
        r = price_event(Decimal("1500"), V_GRADUATED)
        # 100*0.10 + 900*0.05 + 500*0.02 = 10 + 45 + 10 = 65
        self.assertEqual(Decimal(r["raw_amount"]), Decimal("65.00"))
        self.assertEqual(Decimal(r["amount"]), Decimal("65.00"))
        self.assertEqual(len(r["tiers"]), 3)
        self.assertEqual(r["tiers"][0]["slice_quantity"], "100")
        self.assertEqual(r["tiers"][2]["slice_quantity"], "500")

    def test_graduated_boundary_qty_100(self):
        r = price_event(Decimal("100"), V_GRADUATED)
        self.assertEqual(Decimal(r["raw_amount"]), Decimal("10.00"))

    def test_volume_uses_single_tier_with_flat(self):
        r = price_event(Decimal("50"), V_VOLUME)
        self.assertEqual(Decimal(r["raw_amount"]), Decimal("45.00"))
        self.assertEqual(len(r["tiers"]), 1)

    def test_negative_cancellation_is_mirror(self):
        positive = price_event(Decimal("1500"), V_GRADUATED)
        negative = price_event(Decimal("-1500"), V_GRADUATED)
        self.assertEqual(
            Decimal(negative["raw_amount"]), -Decimal(positive["raw_amount"])
        )
        self.assertEqual(Decimal(negative["amount"]), Decimal("-65.00"))

    def test_half_up_quantization(self):
        pv = PriceVersion(
            id="pv-3",
            source="x",
            currency="USD",
            pricing_mode="volume",
            effective_from=None,
            effective_to=None,
            tenant_id=None,
            tiers=(
                Tier(0, Decimal("0"), Decimal("10"), Decimal("0.005"), Decimal("0")),
                Tier(1, Decimal("10"), None, Decimal("0.005"), Decimal("0")),
            ),
        )
        r = price_event(Decimal("1"), pv)
        self.assertEqual(r["raw_amount"], "0.005")
        self.assertEqual(r["amount"], "0.01")
        self.assertEqual(r["currency_scale"], 2)

    def test_correction_delta_small(self):
        r = price_event(Decimal("3"), V_VOLUME)
        self.assertEqual(Decimal(r["amount"]), Decimal("8.00"))

    def test_graduated_flat_charged_per_entered_tier(self):
        pv = PriceVersion(
            id="pv-4",
            source="y",
            currency="USD",
            pricing_mode="graduated",
            effective_from=None,
            effective_to=None,
            tenant_id=None,
            tiers=(
                Tier(0, Decimal("0"), Decimal("10"), Decimal("1"), Decimal("2")),
                Tier(1, Decimal("10"), None, Decimal("1"), Decimal("3")),
            ),
        )
        r = price_event(Decimal("15"), pv)
        self.assertEqual(Decimal(r["raw_amount"]), Decimal("20.00"))


if __name__ == "__main__":
    unittest.main()
