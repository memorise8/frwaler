"""Explicit, additive Delivery schema migration command."""
from __future__ import annotations

import os

from crawler import db_pg
from delivery.db.schema import init_catalogue_schema, init_delivery_schema, verify_required_schema


def migrate(dsn: str) -> None:
    conn = db_pg.open_db(dsn)
    try:
        db_pg.init_db(conn)
        init_catalogue_schema(conn)
        init_delivery_schema(conn)
        verify_required_schema(conn)
    finally:
        conn.close()


def main() -> int:
    dsn = os.environ.get("LIBERTREE_PG_DSN")
    if not dsn:
        raise SystemExit("LIBERTREE_PG_DSN is required")
    migrate(dsn)
    print("delivery schema migration: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
