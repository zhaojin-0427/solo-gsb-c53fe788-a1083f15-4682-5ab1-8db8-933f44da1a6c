from __future__ import annotations

from typing import Iterator

import psycopg

from .db import get_pool


def get_conn() -> Iterator[psycopg.Connection]:
    """从连接池取连接。默认事务隔离级别 RC；服务层自行管理 commit/rollback。"""
    with get_pool().connection() as conn:
        yield conn


class ApiError(Exception):
    def __init__(self, status_code: int, detail: str, extra: dict | None = None):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.extra = extra or {}
