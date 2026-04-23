"""
Shared DB helper for every query service.

Why this base class:
  - DictCursor + conn management is the same for every read; repeating
    the try/finally everywhere is noise.
  - One place to attach observability later (slow-query log, metrics).
  - conn_factory injection keeps the services testable without mocking
    `get_mysql_conn` module-globally.

No transactions: queries are SELECTs only. No ORM: pymysql + raw SQL is
fast and the schema is stable enough that abstraction tax > benefit.
"""
from typing import Any, Callable, List, Optional, Sequence

import pymysql


class ReadQueryService:
    """Base for every module in services.query.

    Subclasses only write SQL. The helpers below own cursor setup,
    DictCursor fetching, and connection cleanup. Each call grabs a
    short-lived connection from the factory; there is no connection
    pool yet because pymysql doesn't ship one and the query rate is
    low enough that it isn't the bottleneck.
    """

    def __init__(self, conn_factory: Callable):
        self._conn_factory = conn_factory

    # ------------------------------------------------------------------
    # Low-level fetchers — DictCursor so callers get {col: val} dicts.
    # ------------------------------------------------------------------
    def _fetch_all(
        self, sql: str, params: Sequence[Any] = ()
    ) -> List[dict]:
        conn = self._conn_factory()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, params)
                return list(cur.fetchall())
        finally:
            conn.close()

    def _fetch_one(
        self, sql: str, params: Sequence[Any] = ()
    ) -> Optional[dict]:
        conn = self._conn_factory()
        try:
            with conn.cursor(pymysql.cursors.DictCursor) as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                return dict(row) if row else None
        finally:
            conn.close()

    def _fetch_scalar(
        self, sql: str, params: Sequence[Any] = (), default: Any = None
    ) -> Any:
        """Single-column single-row. Used for COUNT/AVG aggregates."""
        conn = self._conn_factory()
        try:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                row = cur.fetchone()
                if not row:
                    return default
                return row[0] if row[0] is not None else default
        finally:
            conn.close()
