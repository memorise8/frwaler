"""Bounded PostgreSQL pool used by request-serving backend processes."""
from __future__ import annotations

import atexit
import os
import threading

from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


class _BorrowedConnection:
    def __init__(self, pool: ConnectionPool):
        self._pool = pool
        self._conn = pool.getconn(timeout=5)
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def close(self):
        if not self._closed:
            self._closed = True
            # A SELECT starts a transaction with psycopg's default settings.
            # Return only idle connections so the pool need not repair them.
            if self._conn.info.transaction_status != TransactionStatus.IDLE:
                self._conn.rollback()
            self._pool.putconn(self._conn)


class Database:
    def __init__(self, dsn: str):
        minimum = max(0, min(int(os.environ.get("DELIVERY_DB_POOL_MIN", "1")), 10))
        maximum = max(
            minimum or 1,
            min(int(os.environ.get("DELIVERY_DB_POOL_MAX", "10")), 50),
        )
        self.pool = ConnectionPool(
            conninfo=dsn,
            min_size=minimum,
            max_size=maximum,
            timeout=5,
            kwargs={
                "row_factory": dict_row,
                "connect_timeout": 5,
                "options": (
                    "-c statement_timeout=30000 "
                    "-c idle_in_transaction_session_timeout=30000"
                ),
            },
            open=False,
        )
        self.pool.open()

    def connection(self):
        return _BorrowedConnection(self.pool)

    def close(self):
        self.pool.close()


_lock = threading.Lock()
_databases: dict[str, Database] = {}


def get_database(dsn: str) -> Database:
    with _lock:
        if dsn not in _databases:
            _databases[dsn]=Database(dsn)
        return _databases[dsn]


def close_all() -> None:
    with _lock:
        for database in _databases.values():
            database.close()
        _databases.clear()


atexit.register(close_all)
