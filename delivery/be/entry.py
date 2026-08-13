# -*- coding: utf-8 -*-
import os
from delivery.be.app import create_app


def app():
    dsn = os.environ["LIBERTREE_PG_DSN"]
    from delivery.be.access import validate_auth_config
    validate_auth_config()
    from crawler import db_pg
    from delivery.db.schema import verify_required_schema
    conn = db_pg.open_db(dsn)
    try:
        verify_required_schema(conn)
    finally:
        conn.close()
    return create_app(dsn)
