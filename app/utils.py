"""内容指纹与序列化辅助。

同一 (tenant, source, event_id) 的重复入账必须体内容完全一致；
规范化 JSON 保证字段顺序/空格差异不会误报冲突。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def normalize_occurred_at(value: datetime) -> datetime:
    """无时区的时间戳按 UTC 处理；有时区的统一换算到 UTC。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _canonical_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        # 1.20 -> "1.20" 保留提交时的字面精度；JSON 数字位本身已去空格
        return format(obj, "f")
    if isinstance(obj, datetime):
        return normalize_occurred_at(obj).isoformat()
    raise TypeError(f"unsupported type for canonical hash: {type(obj)}")


def canonical_payload(
    *,
    event_type: str,
    occurred_at: datetime,
    quantity: Decimal,
    dimensions: dict[str, Any],
    linked_event_id: str | None,
) -> str:
    body = {
        "event_type": event_type,
        "occurred_at": normalize_occurred_at(occurred_at).isoformat(),
        "quantity": format(quantity, "f"),
        "dimensions": dimensions or {},
        "linked_event_id": linked_event_id,
    }
    return json.dumps(
        body,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=_canonical_default,
    )


def content_hash(
    *,
    event_type: str,
    occurred_at: datetime,
    quantity: Decimal,
    dimensions: dict[str, Any],
    linked_event_id: str | None,
) -> str:
    return hashlib.sha256(
        canonical_payload(
            event_type=event_type,
            occurred_at=occurred_at,
            quantity=quantity,
            dimensions=dimensions,
            linked_event_id=linked_event_id,
        ).encode("utf-8")
    ).hexdigest()
