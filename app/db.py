"""PostgreSQL 连接池与 schema 初始化。

入账与关账都依赖显式事务 + 行锁，这里只提供连接池，不做隐式提交。
psycopg 的 `pool.connection()` 上下文在正常退出时自动 COMMIT，
抛出异常时自动 ROLLBACK。
"""
from __future__ import annotations

import time
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

pool: ConnectionPool | None = None


def get_pool() -> ConnectionPool:
    if pool is None:
        raise RuntimeError("connection pool not initialized")
    return pool


def init_pool() -> None:
    global pool
    # 等待数据库就绪（compose healthcheck 之外的双保险）
    deadline = time.monotonic() + 30
    while True:
        try:
            pool = ConnectionPool(
                settings.database_url,
                min_size=2,
                max_size=10,
                kwargs={"row_factory": dict_row},
                open=True,
            )
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            break
        except Exception:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.5)
    if settings.init_schema:
        apply_schema()


def apply_schema() -> None:
    sql = Path(settings.schema_file).read_text(encoding="utf-8")
    with get_pool().connection() as conn:
        conn.execute(sql)
    # 退出 with 块即自动提交


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None
