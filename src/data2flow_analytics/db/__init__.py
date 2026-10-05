"""PostgreSQL 연결 풀. 세션 시간대는 UTC(conventions.md §4)."""

from __future__ import annotations

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def create_pool(conninfo: str, max_size: int = 10, min_size: int = 1) -> ConnectionPool:
    pool = ConnectionPool(conninfo, min_size=min_size, max_size=max_size, kwargs={"row_factory": dict_row, "autocommit": False},
                          open=False, name="data2flow-analytics")
    pool.open(wait=False)
    return pool
