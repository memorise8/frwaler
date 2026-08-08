# -*- coding: utf-8 -*-
"""DB 백엔드 셀렉터.

LIBERTREE_DB_BACKEND 환경변수로 SQLite(기본) 또는 Postgres 백엔드를 고른다.
기본값이 'sqlite' 이므로 기존 크롤러 파이프라인과 crawlers-share 공유 패키지는
아무 변경 없이 SQLite 로 계속 동작한다.
"""
from __future__ import annotations

import os


def get_backend(name: str | None = None):
    """Return the db module for the requested backend.

    name is None -> read LIBERTREE_DB_BACKEND (default 'sqlite').
    'sqlite' -> crawler.db_libertree ; 'postgres' -> crawler.db_pg.
    """
    resolved = (name or os.environ.get("LIBERTREE_DB_BACKEND") or "sqlite").lower()
    if resolved == "sqlite":
        from . import db_libertree
        return db_libertree
    if resolved == "postgres":
        from . import db_pg
        return db_pg
    raise ValueError(f"unknown LIBERTREE_DB_BACKEND: {resolved!r}")
