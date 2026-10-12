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


ISOLATED_PREFIX = "enum_"


def documents_target(db_path):
    """(backend, database name) the run would write documents to."""
    if os.environ.get("LIBERTREE_DB_BACKEND", "sqlite").lower() == "postgres":
        from psycopg.conninfo import conninfo_to_dict
        dsn = os.environ.get("LIBERTREE_PG_DSN") or ""
        return "postgres", (conninfo_to_dict(dsn).get("dbname") if dsn else None)
    return "sqlite", (Path(db_path).name if db_path else None)


def require_isolated(db_path, override=False):
    """G3: documents must go to a separate database named enum_* (or an explicit override)."""
    backend, name = documents_target(db_path)
    if not name:
        raise SystemExit(f"refusing to start: no isolated documents database given ({backend}); "
                         f"pass --db enum_<name>.db (or LIBERTREE_PG_DSN with dbname=enum_<name>)")
    if not name.startswith(ISOLATED_PREFIX) and not override:
        raise SystemExit(f"refusing to start: documents database '{name}' is not isolated "
                         f"(name must start with '{ISOLATED_PREFIX}', or pass --i-know-this-is-isolated)")
    return backend, name


def open_reference(path=None, dsn_env=None):
    """Read-only connection to the database holding the legacy crawlers' rows."""
    if dsn_env:
        from crawler.db_backend import get_backend
        conn = get_backend("postgres").open_db(os.environ[dsn_env])
        conn.read_only = True
        return conn
    if path:
        import sqlite3
        conn = sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    return None


def registered_site_ids():
    from crawler.sites import CRAWLERS
    return set(CRAWLERS)


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
             blob_root=None, today=None, crawler_factory=registered_crawler, log=print, reference_conn=None,
             identity_required=False, identity_sample=5, identity_threshold=1.0, allow_empty_reference=False):
    try:
        adapter = make_adapter(plan, conn, crawler_factory)
    except (AdapterConfigError, ValueError) as exc:
        store.start_site(plan.site_id, plan.fingerprint, plan.strategy)
        store.finish_site(plan.site_id, "config_error", str(exc))
        return {"site_id": plan.site_id, "status": "config_error", "stop_reason": str(exc)}
    try:
        result = SiteRunner(plan, store, make_fetcher(plan, state_dir, transport), adapter, today=today, log=log,
                            reference_conn=reference_conn, identity_required=identity_required,
                            identity_sample=identity_sample, identity_threshold=identity_threshold,
                            allow_empty_reference=allow_empty_reference).run(refresh=refresh, retry_only=retry_only)
    finally:
        adapter.close()
    if files:
        fetcher = make_fetcher(plan, state_dir, transport, max_bytes=plan.get("file_max_bytes"))
        result["files"] = FilePass(store, fetcher, blob_root=blob_root or Path(state_dir) / "files",
                                   doc_conn=conn, max_attempts=plan.get("file_max_attempts"),
                                   max_bytes=plan.get("file_max_bytes")).run(plan.site_id)
    return result


FAILED_STATUSES = ("failed", "failed_duplicates", "config_error")
REVIEW_STATUSES = ("complete_with_review", "review_pending", "access_blocked", "complete_with_mismatch",
                   "complete_unverified")


def exit_code(results):
    """1: a site failed or is misconfigured; 2: a site needs a person's review; 0 otherwise
    (incomplete runs are resumable and not an error)."""
    statuses = {r.get("status") for r in results}
    if statuses & set(FAILED_STATUSES):
        return 1
    if statuses & set(REVIEW_STATUSES):
        return 2
    return 0


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
    p.add_argument("--i-know-this-is-isolated", dest="isolated_override", action="store_true",
                   help="allow a documents database whose name does not start with enum_")
    p.add_argument("--reference-db", default=None,
                   help="read-only sqlite DB with the legacy crawlers' rows (identity precheck)")
    p.add_argument("--reference-pg-dsn-env", default=None,
                   help="name of an env var holding a read-only DSN with the legacy rows")
    p.add_argument("--allow-empty-reference", action="store_true",
                   help="let registered sites with no reference rows pass the identity check (warned in report)")
    p.add_argument("--identity-sample", type=int, default=5)
    p.add_argument("--identity-threshold", type=float, default=1.0)
    args = p.parse_args(argv)
    if not args.report_only:
        require_isolated(args.db, args.isolated_override)
    if not 1 <= args.workers <= 16:
        p.error("--workers must be within 1..16")
    plan_data = load_plan(args.plan)
    sites = args.site or sorted(plan_data["sites"])
    registered = registered_site_ids()
    try:
        plans = [site_plan(plan_data, s, registered=registered) for s in sites]
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
        try:
            return _one(plan)
        except Exception as exc:  # one site's crash must not hide the other sites' results
            return {"site_id": plan.site_id, "status": "failed", "stop_reason": f"runner_error:{type(exc).__name__}"}

    def _one(plan):
        conn = open_documents_db(args.db)
        try:
            if args.files_only:
                fetcher = make_fetcher(plan, state_dir, max_bytes=plan.get("file_max_bytes"))
                return {"site_id": plan.site_id, "files": FilePass(
                    store, fetcher, blob_root=args.blob_root or state_dir / "files", doc_conn=conn,
                    max_attempts=plan.get("file_max_attempts"), max_bytes=plan.get("file_max_bytes")).run(plan.site_id)}
            reference = open_reference(args.reference_db, args.reference_pg_dsn_env)
            try:
                return run_site(plan, store, conn, state_dir, refresh=args.refresh, retry_only=args.retry_failed,
                                files=args.files, blob_root=args.blob_root, reference_conn=reference,
                                identity_required=plan.site_id in registered, identity_sample=args.identity_sample,
                                identity_threshold=args.identity_threshold,
                                allow_empty_reference=args.allow_empty_reference)
            finally:
                if reference is not None:
                    reference.close()
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(one, plans))
    conn = open_documents_db(args.db)
    try:
        write_report(store, sites, report_dir, doc_conn=conn)
    finally:
        conn.close()
    print(json.dumps(results, ensure_ascii=False, default=str))
    summary = {}
    for r in results:
        summary[r.get("status")] = summary.get(r.get("status"), 0) + 1
    print(json.dumps({"summary_by_status": summary}, ensure_ascii=False))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
