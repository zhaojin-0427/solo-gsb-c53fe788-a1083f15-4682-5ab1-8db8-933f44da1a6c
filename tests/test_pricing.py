"""Unit tests for the pure pricing engine (no database required)."""

from decimal import Decimal

import pytest

from app.pricing import Tier, allocate, q2, tiered_total, validate_tiers

D = Decimal


def tiers(*spec):
    return [Tier(up_to=u, unit_price=p) for u, p in spec]


class TestValidateTiers:
    def test_valid(self):
        validate_tiers(tiers((D("100"), D("0.10")), (None, D("0.05"))))

    def test_requires_unbounded_final(self):
        with pytest.raises(ValueError, match="unbounded"):
            validate_tiers(tiers((D("100"), D("0.10"))))

    def test_rejects_two_unbounded(self):
        with pytest.raises(ValueError, match="unbounded"):
            validate_tiers(tiers((None, D("0.10")), (None, D("0.05"))))

    def test_rejects_duplicate_boundaries(self):
        with pytest.raises(ValueError, match="increasing"):
            validate_tiers(tiers((D("50"), D("0.10")), (D("50"), D("0.08")), (None, D("0.05"))))

    def test_unordered_input_is_accepted(self):
        # tiers are a set of brackets; the server assigns order by up_to
        validate_tiers(tiers((None, D("0.05")), (D("100"), D("0.10")), (D("50"), D("0.08"))))

    def test_rejects_zero_boundary(self):
        with pytest.raises(ValueError, match="increasing"):
            validate_tiers(tiers((D("0"), D("0.10")), (None, D("0.05"))))

    def test_rejects_negative_price(self):
        with pytest.raises(ValueError, match=">="):
            validate_tiers(tiers((None, D("-1"))))


class TestTieredTotal:
    def test_flat(self):
        assert tiered_total(D("5"), tiers((None, D("2")))) == D("10.00")

    def test_graduated(self):
        t = tiers((D("100"), D("0.10")), (D("1000"), D("0.08")), (None, D("0.05")))
        # 100*0.10 + 900*0.08 + 500*0.05 = 10 + 72 + 25
        assert tiered_total(D("1500"), t) == D("107.00")

    def test_exact_boundary(self):
        t = tiers((D("100"), D("0.10")), (None, D("0.05")))
        assert tiered_total(D("100"), t) == D("10.00")

    def test_zero_quantity(self):
        assert tiered_total(D("0"), tiers((None, D("0.10")))) == D("0.00")

    def test_fractional_quantity_and_price(self):
        t = tiers((None, D("0.333333")))
        assert tiered_total(D("3"), t) == q2(D("0.999999"))


class TestAllocate:
    def test_per_event_amounts_sum_to_total(self):
        t = tiers((D("100"), D("0.10")), (None, D("0.05")))
        events = [("a", (1,), D("60")), ("b", (2,), D("60")), ("c", (3,), D("30"))]
        allocs = allocate(events, t)
        assert sum(a.amount for a in allocs) == tiered_total(D("150"), t)

    def test_bracket_walk_order(self):
        t = tiers((D("100"), D("0.10")), (None, D("0.05")))
        events = [("first", (1,), D("100")), ("second", (2,), D("50"))]
        a_first, a_second = allocate(events, t)
        # first event fills the cheap bracket entirely
        assert a_first.amount == D("10.00")
        assert a_first.brackets == [
            {"from": "0", "to": "100", "unit_price": "0.10", "quantity": "100", "amount": "10.00"}
        ]
        # second event spills entirely into the next bracket
        assert a_second.amount == D("2.50")
        assert a_second.brackets[0]["from"] == "100"

    def test_event_straddling_boundary(self):
        t = tiers((D("100"), D("0.10")), (None, D("0.05")))
        (alloc,) = allocate([("x", (1,), D("150"))], t)
        assert len(alloc.brackets) == 2
        assert alloc.brackets[0]["quantity"] == "100"
        assert alloc.brackets[1]["quantity"] == "50"
        assert alloc.amount == D("12.50")
        # slice amounts sum exactly to the event amount
        assert sum(D(b["amount"]) for b in alloc.brackets) == alloc.amount

    def test_deterministic_regardless_of_input_order(self):
        t = tiers((D("10"), D("1")), (None, D("0.5")))
        e1 = [("a", (2,), D("5")), ("b", (1,), D("8"))]
        e2 = [("b", (1,), D("8")), ("a", (2,), D("5"))]
        r1 = {a.key: a.amount for a in allocate(e1, t)}
        r2 = {a.key: a.amount for a in allocate(e2, t)}
        assert r1 == r2 == {"b": D("8.00"), "a": D("3.50")}

    def test_zero_quantity_event_gets_empty_breakdown(self):
        t = tiers((None, D("1")))
        a_zero, a_rest = allocate([("z", (1,), D("0")), ("r", (2,), D("3"))], t)
        assert a_zero.amount == D("0") and a_zero.brackets == []
        assert a_rest.amount == D("3.00")

    def test_many_tiny_events_rounding_is_explicit(self):
        # 1000 sub-cent events: raw amounts round to 0.00 per event, but the
        # metric total (0.01) is quantized once and the residue is assigned
        # to exactly one event via largest-remainder — sum(lines) == total.
        t = tiers((None, D("0.01")))
        events = [(f"e{i}", (i,), D("0.001")) for i in range(1000)]
        allocs = allocate(events, t)
        assert sum(a.amount for a in allocs) == D("0.01") == tiered_total(D("1.000"), t)
        winners = [a for a in allocs if a.rounding_residual != 0]
        assert len(winners) == 1 and winners[0].rounding_residual == D("0.01")

    def test_cent_scale_events_sum_exactly_to_tiered_total(self):
        t = tiers((D("100"), D("0.10")), (None, D("0.05")))
        events = [(f"e{i}", (i,), D("1.25")) for i in range(120)]  # total 150
        allocs = allocate(events, t)
        assert sum(a.amount for a in allocs) == tiered_total(D("150"), t) == D("12.50")

    def test_largest_remainder_is_deterministic(self):
        t = tiers((None, D("0.001")))
        events = [(f"e{i}", (i,), D("1")) for i in range(7)]  # 7 x 0.001 -> total 0.01 (0.007 rounds up)
        allocs = allocate(events, t)
        assert sum(a.amount for a in allocs) == D("0.01")
        again = allocate(list(reversed(events)), t)
        assert [a.amount for a in allocs] == [a.amount for a in again]
