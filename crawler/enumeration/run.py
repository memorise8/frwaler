"""CLI: python -m crawler.enumeration.run --site <id> --plan <plan.json> [...]

Documents go to the existing store: ``--db`` (libertree sqlite file) or
``LIBERTREE_DB_BACKEND=postgres`` with ``LIBERTREE_PG_DSN``. Enumeration
state, ledgers and request gates live under ``--state-dir``. Different sites
may run in parallel (``--workers``); one site is always sequential.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .adapters import Adapter, AdapterConfigError
from .files import FilePass
from .http import Fetcher, RequestsTransport
from .plan import PlanError, load_plan, site_plan
from .report import write_report
from .runner import SiteRunner
from .store import Store


def open_documents_db(db_path):
    from crawler.db_backend import get_backend
    if os.environ.get("LIBERTREE_DB_BACKEND", "sqlite").lower() == "postgres":
        dsn = os.environ.get("LIBERTREE_PG_DSN")
        if not dsn:
            raise SystemExit("LIBERTREE_PG_DSN is required for the postgres backend")
        return get_backend("postgres").open_db(dsn)
    return get_backend("sqlite").open_db(db_path)


def registered_crawler(site_id, conn):
    from crawler.sites import CRAWLERS
    cls = CRAWLERS.get(site_id)
    if cls is None:
        raise AdapterConfigError(f"no registered crawler for {site_id}")
    return cls(db_conn=conn)


def make_fetcher(plan, state_dir, transport=None, *, max_bytes=None):
    transport = transport or RequestsTransport(verify=plan.get("verify_ssl"),
                                               **({"max_bytes": max_bytes} if max_bytes else {}))
    return Fetcher(plan.site_id, transport, Path(state_dir) / "gates",
                   interval=plan.get("request_interval_s"), max_requests=plan.get("max_requests"),
                   max_runtime_s=plan.get("max_runtime_s"), retries=plan.get("retries"),
                   backoff_s=plan.get("backoff_s"), max_retry_wait_s=plan.get("max_retry_wait_s"),
                   respect_robots=plan.get("respect_robots"), user_agent=plan.get("user_agent"),
                   encoding=plan.get("encoding"))


def make_adapter(plan, conn, crawler_factory=registered_crawler):
    cfg = plan.raw.get("adapter") or {"type": "list_only"}
    crawler = crawler_factory(plan.site_id, conn) if cfg.get("type") == "crawler" else None
    base = plan.base_url or (getattr(crawler, "base_url", "") if crawler is not None else "")
    return Adapter(plan.site_id, cfg, conn, crawler=crawler, base_url=base)


def run_site(plan, store, conn, state_dir, *, transport=None, refresh=False, retry_only=False, files=False,
             blob_root=None, today=None, crawler_factory=registered_crawler, log=print):
    try:
        adapter = make_adapter(plan, conn, crawler_factory)
    except (AdapterConfigError, ValueError) as exc:
        store.start_site(plan.site_id, plan.fingerprint, plan.strategy)
        store.finish_site(plan.site_id, "config_error", str(exc))
        return {"site_id": plan.site_id, "status": "config_error", "stop_reason": str(exc)}
    try:
        result = SiteRunner(plan, store, make_fetcher(plan, state_dir, transport), adapter,
                            today=today, log=log).run(refresh=refresh, retry_only=retry_only)
    finally:
        adapter.close()
    if files:
        fetcher = make_fetcher(plan, state_dir, transport, max_bytes=plan.get("file_max_bytes"))
        result["files"] = FilePass(store, fetcher, blob_root=blob_root or Path(state_dir) / "files",
                                   doc_conn=conn, max_attempts=plan.get("file_max_attempts"),
                                   max_bytes=plan.get("file_max_bytes")).run(plan.site_id)
    return result


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m crawler.enumeration.run")
    p.add_argument("--plan", required=True)
    p.add_argument("--site", action="append", help="site id (repeatable); default: every site in the plan")
    p.add_argument("--state-dir", default="data/enumeration")
    p.add_argument("--db", default=None, help="libertree sqlite path (sqlite backend)")
    p.add_argument("--report-dir", default=None)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--files", action="store_true", help="run the separate file pass after enumeration")
    p.add_argument("--files-only", action="store_true")
    p.add_argument("--blob-root", default=None)
    p.add_argument("--refresh", action="store_true", help="re-enumerate sites already complete")
    p.add_argument("--retry-failed", action="store_true", help="only retry transient item failures")
    p.add_argument("--report-only", action="store_true")
    args = p.parse_args(argv)
    if not 1 <= args.workers <= 16:
        p.error("--workers must be within 1..16")
    plan_data = load_plan(args.plan)
    sites = args.site or sorted(plan_data["sites"])
    try:
        plans = [site_plan(plan_data, s) for s in sites]
    except PlanError as exc:
        p.error(str(exc))
    state_dir = Path(args.state_dir)
    store = Store(state_dir / "enumeration.sqlite")
    report_dir = Path(args.report_dir or state_dir / "reports")
    if args.report_only:
        rows = write_report(store, sites, report_dir)
        print(json.dumps([{k: r.get(k) for k in ("site_id", "status", "listed_unique", "saved", "gap")} for r in rows],
                         ensure_ascii=False))
        return 0

    def one(plan):
        conn = open_documents_db(args.db)
        try:
            if args.files_only:
                fetcher = make_fetcher(plan, state_dir, max_bytes=plan.get("file_max_bytes"))
                return {"site_id": plan.site_id, "files": FilePass(
                    store, fetcher, blob_root=args.blob_root or state_dir / "files", doc_conn=conn,
                    max_attempts=plan.get("file_max_attempts"), max_bytes=plan.get("file_max_bytes")).run(plan.site_id)}
            return run_site(plan, store, conn, state_dir, refresh=args.refresh, retry_only=args.retry_failed,
                            files=args.files, blob_root=args.blob_root)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(one, plans))
    write_report(store, sites, report_dir)
    print(json.dumps(results, ensure_ascii=False, default=str))
    return 0 if all(r.get("status") not in ("failed",) for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
