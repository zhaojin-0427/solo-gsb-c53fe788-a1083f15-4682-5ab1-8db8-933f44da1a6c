"""计价引擎：纯函数 + Decimal，可在试算 / 入账 / 重算三条路径复用。

两种阶梯模式：
- volume（总量定价）：总量 |x| 落入唯一一档，金额 = |x|*unit + flat，金额符号与 x 一致。
- graduated（分段累进）：对 |x| 逐档切片，每段 slice*unit + flat 之和，符号与 x 一致。

修正(correction)/撤销(cancellation)传入的是“相对上一累计量的有符号差值”，
因此撤销天然为负，价格变更产生的差额也会得到正确的正负调整。
"""
from __future__ import annotations
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True)
class Tier:
    tier_index: int
    from_qty: Decimal
    up_to_qty: Decimal | None  # None = 开区间
    unit_amount: Decimal
    flat_amount: Decimal


@dataclass(frozen=True)
class PriceVersion:
    id: str
    source: str
    currency: str
    pricing_mode: Literal["volume", "graduated"]
    effective_from: Any
    effective_to: Any | None
    tenant_id: str | None
    tiers: tuple[Tier, ...]


@dataclass(frozen=True)
class ChargeResult:
    raw_amount: Decimal
    tier_traces: tuple[dict[str, Any], ...]
    pricing_mode: str
    absolute_quantity: Decimal


def _find_tier(tiers: tuple[Tier, ...], abs_qty: Decimal) -> Tier:
    """volume 模式：qty 所在的唯一档。[from, up_to)，NULL up_to 为开区间。"""
    for tier in tiers:
        if abs_qty >= tier.from_qty and (tier.up_to_qty is None or abs_qty < tier.up_to_qty):
            return tier
    raise ValueError(f"quantity {abs_qty} is not covered by any price tier")


def charge_non_negative(qty: Decimal, price: PriceVersion) -> tuple[Decimal, tuple[dict, ...]]:
    """对非负数量计价，返回 (未取整金额, 各档轨迹)。"""
    if qty < 0:
        raise ValueError("charge_non_negative expects qty >= 0")

    if price.pricing_mode == "volume":
        tier = _find_tier(price.tiers, qty)
        amount = qty * tier.unit_amount + tier.flat_amount
        trace = (
            {
                "tier_index": tier.tier_index,
                "from_qty": str(tier.from_qty),
                "up_to_qty": str(tier.up_to_qty) if tier.up_to_qty is not None else None,
                "slice_quantity": str(qty),
                "unit_amount": str(tier.unit_amount),
                "flat_amount": str(tier.flat_amount),
                "slice_amount": str(amount),
            },
        )
        return amount, trace

    # graduated：从 0 起逐档切片；档 [from, up_to)，NULL up_to 为开区间
    traces: list[dict[str, Any]] = []
    total = ZERO
    consumed = ZERO
    for tier in price.tiers:
        if consumed >= qty:
            break
        lo = max(tier.from_qty, consumed)
        hi = qty if tier.up_to_qty is None else min(qty, tier.up_to_qty)
        slice_qty = hi - lo if hi > lo else ZERO
        if slice_qty <= 0:
            continue
        # 进入该档即收取一次 flat fee
        slice_amount = slice_qty * tier.unit_amount + tier.flat_amount
        total += slice_amount
        consumed = hi
        traces.append(
            {
                "tier_index": tier.tier_index,
                "from_qty": str(tier.from_qty),
                "up_to_qty": str(tier.up_to_qty) if tier.up_to_qty is not None else None,
                "slice_quantity": str(slice_qty),
                "unit_amount": str(tier.unit_amount),
                "flat_amount": str(tier.flat_amount),
                "slice_amount": str(slice_amount),
            }
        )
    if consumed != qty:
        raise ValueError(f"quantity {qty} is not fully covered by graduated tiers")
    return total, tuple(traces)


def price_event(
    signed_quantity: Decimal,
    price: PriceVersion,
    *,
    scale: int = 2,
) -> dict[str, Any]:
    """对一笔账册事件计价。signed_quantity：usage 为正量，修正/撤销为有符号差值。

    返回包含 raw_amount / amount(按货币精度固化) / 完整轨迹的 dict，
    轨迹将逐字写入 bill_lines.pricing_trace，供逐事件计价轨迹与重算使用。
    """
    abs_qty = abs(signed_quantity)
    positive_amount, tier_traces = charge_non_negative(abs_qty, price)
    sign = Decimal("1") if signed_quantity >= 0 else Decimal("-1")
    raw_amount = positive_amount * sign
    quant = Decimal(1).scaleb(-scale)  # 10^-scale
    amount = raw_amount.quantize(quant, rounding=ROUND_HALF_UP)

    return {
        "price_version_id": price.id,
        "pricing_mode": price.pricing_mode,
        "currency": price.currency,
        "input_quantity": str(signed_quantity),
        "absolute_quantity": str(abs_qty),
        "tiers": list(tier_traces),
        "raw_amount": str(raw_amount),
        "currency_scale": scale,
        "amount": str(amount),
    }
