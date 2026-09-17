"""Graduated tier pricing engine. Pure functions over Decimals — no I/O, no
floats, fully deterministic.

Allocation rule: events of one metric are ordered by
(occurred_at, root_recv_seq, root_id) and their quantities are walked through
the graduated brackets in that fixed order, producing a raw (unquantized)
amount per event. The metric total is quantized once to cents; per-event
amounts are then quantized and the rounding residue is distributed cent by
cent using the largest-remainder method (deterministic tie-break by sort
order). Hence, by construction:

    sum(per-event amounts) == tiered total == bill metric total

and the per-event breakdown stored in the bill line trace is exactly
reproducible from the frozen inputs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Hashable

CENT = Decimal("0.01")
ZERO = Decimal("0")


def q2(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def dec_str(value: Decimal) -> str:
    """Plain decimal string without trailing zeros or scientific notation
    (used for quantities/boundaries in traces; amounts keep their scale)."""
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


@dataclass(frozen=True)
class Tier:
    up_to: Decimal | None  # cumulative bracket boundary; None = unbounded
    unit_price: Decimal


def validate_tiers(tiers: list[Tier]) -> None:
    """Tiers of one metric must cover [0, +inf) exactly once."""
    if not tiers:
        raise ValueError("at least one tier is required")
    unbounded = [t for t in tiers if t.up_to is None]
    if len(unbounded) != 1:
        raise ValueError("exactly one tier must be unbounded (up_to = null)")
    prev = ZERO
    for t in sorted((t for t in tiers if t.up_to is not None), key=lambda t: t.up_to):
        if t.up_to <= prev:
            raise ValueError("tier boundaries must be strictly increasing and > 0")
        prev = t.up_to
    for t in tiers:
        if t.unit_price < 0:
            raise ValueError("unit_price must be >= 0")


@dataclass
class EventAllocation:
    key: Hashable
    quantity: Decimal
    amount: Decimal = ZERO
    brackets: list[dict[str, str]] = field(default_factory=list)
    rounding_residual: Decimal = ZERO


def allocate(
    events: list[tuple[Hashable, tuple, Decimal]],
    tiers: list[Tier],
) -> list[EventAllocation]:
    """Allocate a graduated tiered total back to individual events.

    events: (key, sort_key, quantity) triples. Returns one EventAllocation
    per event, in sorted order. Deterministic for a fixed input set.
    """
    validate_tiers(tiers)
    ordered_tiers = sorted(tiers, key=lambda t: (t.up_to is None, t.up_to))

    # 1) walk quantities through the brackets in a fixed order -> raw amounts
    result: list[EventAllocation] = []
    raws: list[Decimal] = []
    tier_idx = 0
    pos = ZERO  # cumulative quantity consumed so far
    for key, _sort_key, qty in sorted(events, key=lambda e: e[1]):
        alloc = EventAllocation(key=key, quantity=qty)
        raw = ZERO
        remaining = qty
        while remaining > 0:
            tier = ordered_tiers[tier_idx]
            take = remaining if tier.up_to is None else min(remaining, tier.up_to - pos)
            slice_raw = take * tier.unit_price
            alloc.brackets.append(
                {
                    "from": dec_str(pos),
                    "to": dec_str(pos + take),
                    "unit_price": str(tier.unit_price),
                    "quantity": dec_str(take),
                    "amount": str(slice_raw),
                }
            )
            raw += slice_raw
            pos += take
            remaining -= take
            if tier.up_to is not None and pos >= tier.up_to:
                tier_idx += 1
        raws.append(raw)
        result.append(alloc)

    # 2) quantize the total once, then apportion to events (largest remainder)
    total = q2(sum(raws, ZERO))
    quantized = [q2(r) for r in raws]
    residue_cents = int((total - sum(quantized, ZERO)) / CENT)
    if residue_cents != 0:
        # rounding error of each event: positive => rounded up, negative => down
        errors = [quantized[i] - raws[i] for i in range(len(raws))]
        if residue_cents > 0:
            # give cents to the events rounded down the most
            order = sorted(range(len(raws)), key=lambda i: (errors[i], i))
        else:
            # take cents back from the events rounded up the most
            order = sorted(range(len(raws)), key=lambda i: (errors[i], i), reverse=True)
        step = CENT if residue_cents > 0 else -CENT
        for k in range(abs(residue_cents)):
            idx = order[k % len(order)]
            quantized[idx] += step
            result[idx].rounding_residual += step

    for i, alloc in enumerate(result):
        alloc.amount = quantized[i]
    return result


def tiered_total(quantity: Decimal, tiers: list[Tier]) -> Decimal:
    """Total charge for a single aggregate quantity (convenience wrapper)."""
    return allocate([("total", (0,), quantity)], tiers)[0].amount
