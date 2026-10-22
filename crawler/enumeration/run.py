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
from .plan import PlanError, header_hosts, load_plan, resolve_headers, site_plan
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


def _has_documents_table(conn):
    import sqlite3
    if isinstance(conn, sqlite3.Connection):
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='documents'").fetchone() is not None
    row = conn.execute("SELECT to_regclass('public.documents') AS r").fetchone()
    return (row["r"] if hasattr(row, "keys") else row[0]) is not None


def ensure_documents_db(conn, site_id, site_name, base_url, *, db_name):
    """S0: make the documents DB usable from empty. The schema is created only in an
    isolated enum_* database; the site row (documents.site_id FK) is upserted the
    way the legacy crawlers do in crawl()."""
    import sqlite3
    from crawler.db_backend import get_backend
    backend = get_backend("sqlite" if isinstance(conn, sqlite3.Connection) else "postgres")
    isolated = bool(db_name) and str(db_name).startswith(ISOLATED_PREFIX)
    if not _has_documents_table(conn):
        if not isolated:
            raise SystemExit(f"refusing to initialise schema in non-isolated database '{db_name}'")
        backend.init_db(conn)
    elif isolated:
        backend.init_db(conn)  # idempotent: CREATE IF NOT EXISTS + additive date columns
    backend.upsert_site(conn, site_id, site_name or site_id, base_url or "")


class SiteLease:
    """One enumeration per (documents DB, site) at a time, across processes:
    a file lock next to a SQLite DB, a session advisory lock on PostgreSQL."""

    def __init__(self, conn, db_path, site_id):
        import sqlite3
        self.conn, self.site_id = conn, site_id
        self.sqlite = isinstance(conn, sqlite3.Connection)
        self.path = None
        if self.sqlite:
            import hashlib
            base = Path(db_path) if db_path else Path(".")
            self.path = base.with_name(base.name + f".enum-lease-{hashlib.sha256(site_id.encode()).hexdigest()[:16]}")
        self.handle = None

    def __enter__(self):
        if self.sqlite:
            import fcntl
            self.handle = open(self.path, "a")
            try:
                fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.handle.close()
                self.handle = None
                raise RuntimeError(f"site_lease_busy:{self.site_id}") from None
        else:
            row = self.conn.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS ok",
                                    (f"enum_site:{self.site_id}",)).fetchone()
            self.conn.commit()
            if not (row["ok"] if hasattr(row, "keys") else row[0]):
                raise RuntimeError(f"site_lease_busy:{self.site_id}")
        return self

    def __exit__(self, *exc):
        if self.sqlite and self.handle is not None:
            import fcntl
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()
        elif not self.sqlite:
            self.conn.rollback()
            self.conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (f"enum_site:{self.site_id}",))
            self.conn.commit()
        return False


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


def make_fetcher(plan, state_dir, transport=None, *, max_bytes=None, holds=None):
    headers = resolve_headers(plan.raw)  # PlanError when a named environment variable is missing
    if plan.raw.get("max_response_bytes"):
        # ext:response_size: a source whose listing responses are larger than the default cap.
        max_bytes = max(max_bytes or 0, int(plan.raw["max_response_bytes"]))
    transport = transport or RequestsTransport(verify=plan.get("verify_ssl"),
                                               **({"max_bytes": max_bytes} if max_bytes else {}))
    return Fetcher(plan.site_id, transport, Path(state_dir) / "gates", holds=holds,
                   interval=plan.get("request_interval_s"), max_requests=plan.get("max_requests"),
                   max_runtime_s=plan.get("max_runtime_s"), retries=plan.get("retries"),
                   backoff_s=plan.get("backoff_s"), max_retry_wait_s=plan.get("max_retry_wait_s"),
                   respect_robots=plan.get("respect_robots"), user_agent=plan.get("user_agent"),
                   encoding=plan.get("encoding"), extra_headers=headers,
                   header_hosts=header_hosts(plan.raw) if headers else None)


def make_adapter(plan, conn, crawler_factory=registered_crawler):
    cfg = plan.raw.get("adapter") or {"type": "list_only"}
    crawler = crawler_factory(plan.site_id, conn) if cfg.get("type") == "crawler" else None
    base = plan.base_url or (getattr(crawler, "base_url", "") if crawler is not None else "")
    return Adapter(plan.site_id, cfg, conn, crawler=crawler, base_url=base)


def run_site(plan, store, conn, state_dir, *, transport=None, refresh=False, retry_only=False, files=False,
             blob_root=None, today=None, crawler_factory=registered_crawler, log=print, reference_conn=None,
             identity_required=False, identity_sample=5, identity_threshold=1.0, allow_empty_reference=False,
             incremental=None, known_keys=None):
    """``incremental``: an IncrementalPolicy to collect only new items (front of a newest-first
    listing) instead of a full/resumed enumeration; ``known_keys``: optional KnownKeys seed."""
    try:
        adapter = make_adapter(plan, conn, crawler_factory)
    except (AdapterConfigError, ValueError) as exc:
        store.start_site(plan.site_id, plan.fingerprint, plan.strategy)
        store.finish_site(plan.site_id, "config_error", str(exc))
        return {"site_id": plan.site_id, "status": "config_error", "stop_reason": str(exc)}
    # One Fetcher (one budget, one set of holds) for the body run and the file pass.
    try:
        fetcher = make_fetcher(plan, state_dir, transport, holds=store,
                               max_bytes=plan.get("file_max_bytes") if files else None)
    except PlanError as exc:
        store.start_site(plan.site_id, plan.fingerprint, plan.strategy)
        store.finish_site(plan.site_id, "config_error", str(exc))
        return {"site_id": plan.site_id, "status": "config_error", "stop_reason": str(exc)}
    try:
        runner = SiteRunner(plan, store, fetcher, adapter, today=today, log=log,
                            reference_conn=reference_conn, identity_required=identity_required,
                            identity_sample=identity_sample, identity_threshold=identity_threshold,
                            allow_empty_reference=allow_empty_reference)
        if incremental is not None:
            result = runner.run_incremental(incremental, known_keys)
        else:
            result = runner.run(refresh=refresh, retry_only=retry_only)
    finally:
        adapter.close()
    if files:
        result["files"] = FilePass(store, fetcher, blob_root=blob_root or Path(state_dir) / "files",
                                   doc_conn=conn, max_attempts=plan.get("file_max_attempts"),
                                   max_bytes=plan.get("file_max_bytes")).run(plan.site_id)
    return result


FAILED_STATUSES = ("failed", "failed_duplicates", "config_error", "private_destination_blocked")
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
    p.add_argument("--release-host", action="append", default=[],
                   help="release a reviewed host hold (refusals are otherwise permanent)")
    p.add_argument("--seed-holds", default=None, metavar="CSV",
                   help="CSV (columns host, reason) of access-restricted hosts to hold before any request; "
                        "'*.example.org' holds every subdomain")
    p.add_argument("--reprocess-failed", action="append", default=[], metavar="REASON_PREFIX",
                   help="give failed items whose reason starts with this prefix a fresh retry (e.g. parse_error:)")
    p.add_argument("--incremental", action="store_true",
                   help="collect only new items: read a newest-first listing from the front and stop after "
                        "known_run consecutive known items (plan 'incremental' section; never a full pass)")
    p.add_argument("--known-keys", default=None, metavar="FILE",
                   help="incremental: CSV/JSONL (site_id, post_number, meta_url) of documents already delivered")
    p.add_argument("--known-run", type=int, default=None, help="incremental: override the plan's known_run")
    p.add_argument("--incremental-max-pages", type=int, default=None,
                   help="incremental: override the plan's incremental max_pages")
    p.add_argument("--identity-sample", type=int, default=5)
    p.add_argument("--identity-threshold", type=float, default=1.0)
    args = p.parse_args(argv)
    if not args.report_only:
        require_isolated(args.db, args.isolated_override)
    if not 1 <= args.workers <= 16:
        p.error("--workers must be within 1..16")
    if args.site and len(set(args.site)) != len(args.site):
        p.error("each --site may be given once (one enumeration per site at a time)")
    if args.incremental and (args.refresh or args.retry_failed or args.files_only or args.report_only):
        p.error("--incremental cannot be combined with --refresh, --retry-failed, --files-only or --report-only")
    if not args.incremental and (args.known_keys or args.known_run is not None
                                 or args.incremental_max_pages is not None):
        p.error("--known-keys, --known-run and --incremental-max-pages need --incremental")
    plan_data = load_plan(args.plan)
    sites = args.site or sorted(plan_data["sites"])
    registered = registered_site_ids()
    try:
        plans = [site_plan(plan_data, s, registered=registered) for s in sites]
    except PlanError as exc:
        p.error(str(exc))
    policies, seeds = {}, {}
    if args.incremental:
        from .incremental import IncrementalPolicy, KnownKeys
        for plan in plans:
            try:
                policies[plan.site_id] = IncrementalPolicy.from_plan(plan, known_run=args.known_run,
                                                                     max_pages=args.incremental_max_pages)
            except (TypeError, ValueError) as exc:
                p.error(f"{plan.site_id}: {exc}")
            if args.known_keys:
                try:
                    seeds[plan.site_id] = KnownKeys.load(args.known_keys, plan.site_id)
                except (OSError, ValueError) as exc:
                    p.error(f"--known-keys: {exc}")
    state_dir = Path(args.state_dir)
    store = Store(state_dir / "enumeration.sqlite")
    if args.seed_holds:
        import csv
        with open(args.seed_holds, newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))
        if rows and "host" not in rows[0]:
            p.error("--seed-holds CSV needs a 'host' column")
        for row in rows:
            host = (row.get("host") or "").strip()
            if host and store.seed_hold(host, (row.get("reason") or "").strip() or "access_restricted"):
                store.event("*", "host_hold_seeded", {"host": host.lower(), "reason": row.get("reason")})
    for host in args.release_host:
        store.release_host(host)
        store.event("*", "host_hold_released", {"host": host})
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

    _backend, db_name = documents_target(args.db)

    def _one(plan):
        conn = open_documents_db(args.db)
        try:
            from crawler.sites import CRAWLERS
            cls = CRAWLERS.get(plan.site_id)
            ensure_documents_db(conn, plan.site_id,
                                getattr(cls, "site_name", None) or plan.raw.get("site_name") or plan.site_id,
                                getattr(cls, "base_url", None) or plan.base_url, db_name=db_name)
            with SiteLease(conn, args.db, plan.site_id):
                return _leased(plan, conn)
        except RuntimeError as exc:
            if str(exc).startswith("site_lease_busy"):
                return {"site_id": plan.site_id, "status": "locked", "stop_reason": str(exc)}
            raise
        finally:
            conn.close()

    def _leased(plan, conn):
        if args.reprocess_failed:
            store.requeue_failed(plan.site_id, args.reprocess_failed)
        if args.files_only:
            try:
                fetcher = make_fetcher(plan, state_dir, max_bytes=plan.get("file_max_bytes"), holds=store)
            except PlanError as exc:
                return {"site_id": plan.site_id, "status": "config_error", "stop_reason": str(exc)}
            return {"site_id": plan.site_id, "files": FilePass(
                store, fetcher, blob_root=args.blob_root or state_dir / "files", doc_conn=conn,
                max_attempts=plan.get("file_max_attempts"), max_bytes=plan.get("file_max_bytes")).run(plan.site_id)}
        reference = open_reference(args.reference_db, args.reference_pg_dsn_env)
        try:
            return run_site(plan, store, conn, state_dir, refresh=args.refresh, retry_only=args.retry_failed,
                            files=args.files, blob_root=args.blob_root, reference_conn=reference,
                            identity_required=plan.site_id in registered, identity_sample=args.identity_sample,
                            identity_threshold=args.identity_threshold,
                            allow_empty_reference=args.allow_empty_reference,
                            incremental=policies.get(plan.site_id), known_keys=seeds.get(plan.site_id))
        finally:
            if reference is not None:
                reference.close()

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
