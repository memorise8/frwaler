"""Explicit, additive Delivery schema migration command."""
from __future__ import annotations

import os

from crawler import db_pg
from delivery.db.schema import init_catalogue_schema, init_delivery_schema, verify_required_schema
from delivery.db.seed import seed_catalogue_sites


def migrate(dsn: str) -> None:
    conn = db_pg.open_db(dsn)
    try:
        db_pg.init_db(conn)
        init_catalogue_schema(conn)
        init_delivery_schema(conn)
        verify_required_schema(conn)
        # Without this a new install cannot start a single crawl from the
        # console: POST /jobs rejects any site missing from `sites`, and
        # nothing else fills that table before a crawler has already run.
        seeded = seed_catalogue_sites(conn)
        if seeded:
            print(f"delivery site catalogue: {seeded} sites registered")
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
