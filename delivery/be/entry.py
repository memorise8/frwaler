# -*- coding: utf-8 -*-
import os
from delivery.be.app import create_app


def app():
    dsn = os.environ["LIBERTREE_PG_DSN"]
    return create_app(dsn)
