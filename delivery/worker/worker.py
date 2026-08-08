# -*- coding: utf-8 -*-
"""워커: crawl_jobs 를 소비해 크롤러를 실행한다.

기본 백엔드는 postgres 로 강제(LIBERTREE_DB_BACKEND). crawlers-share 와 동일한
호출 패턴: CRAWLERS[site_id](db_conn=conn, delay=...).crawl(limit).
"""
from __future__ import annotations

import os
import time

from . import jobs


def _registry(crawler_registry):
    if crawler_registry is not None:
        return crawler_registry
    from crawler.sites import CRAWLERS
    return CRAWLERS


def _count_site_docs(conn, site_id) -> int:
    row = conn.execute("SELECT count(*) AS n FROM documents WHERE site_id=%s", (site_id,)).fetchone()
    return int(row["n"])


def run_job(conn, job, crawler_registry=None, delay=1.0) -> int:
    """Run one claimed job. Returns saved doc count (0 on failure)."""
    os.environ.setdefault("LIBERTREE_DB_BACKEND", "postgres")
    site_id = job["site_id"]
    registry = _registry(crawler_registry)
    cls = registry.get(site_id)
    if cls is None:
        jobs.fail_job(conn, job["id"], f"no crawler for site_id={site_id!r}")
        return 0
    before = _count_site_docs(conn, site_id)
    try:
        inst = cls(db_conn=conn, delay=delay)
        inst.crawl(limit=job.get("limit_n"))
    except Exception as exc:  # noqa: BLE001
        jobs.fail_job(conn, job["id"], f"{type(exc).__name__}: {exc}")
        return 0
    saved = _count_site_docs(conn, site_id) - before
    jobs.finish_job(conn, job["id"], saved_count=max(0, saved))
    return max(0, saved)


def run_once(conn, crawler_registry=None, delay=1.0) -> bool:
    """Claim+run one job. False if the queue was empty."""
    job = jobs.claim_next_job(conn)
    if job is None:
        return False
    run_job(conn, job, crawler_registry=crawler_registry, delay=delay)
    return True


def main() -> int:
    import argparse
    from crawler import db_pg

    os.environ.setdefault("LIBERTREE_DB_BACKEND", "postgres")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dsn", default=os.environ.get("LIBERTREE_PG_DSN"))
    ap.add_argument("--once", action="store_true", help="한 작업만 처리하고 종료")
    ap.add_argument("--poll", type=float, default=5.0, help="빈 큐 폴링 간격(초)")
    ap.add_argument("--delay", type=float, default=1.0)
    args = ap.parse_args()
    if not args.dsn:
        raise SystemExit("LIBERTREE_PG_DSN or --dsn required")

    conn = db_pg.open_db(args.dsn)
    try:
        if args.once:
            run_once(conn, delay=args.delay)
            return 0
        while True:
            if not run_once(conn, delay=args.delay):
                time.sleep(args.poll)
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
