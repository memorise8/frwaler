# -*- coding: utf-8 -*-
import os
from delivery.be.app import create_app


def app():
    dsn = os.environ["LIBERTREE_PG_DSN"]
    from crawler import db_pg
    from delivery.db import schema
    conn = db_pg.open_db(dsn)
    try:
        db_pg.init_db(conn)
        schema.init_delivery_schema(conn)
    finally:
        conn.close()
    return create_app(dsn)
