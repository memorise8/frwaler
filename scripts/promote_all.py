# -*- coding: utf-8 -*-
"""promote_all.py — Bulk-promote all codex/claude-generated custom crawlers
into libertree.db with PDF download + text extraction.

Usage
-----
    .venv/bin/python scripts/promote_all.py \\
        [--sources sample_runs codex_required_runs claude_required_runs] \\
        [--db data/libertree.db] \\
        [--blob-root libertree] \\
        [--per-site-limit 500] \\
        [--per-site-timeout 1800] \\
        [--concurrent-sites 4] \\
        [--concurrent-pdfs 8] \\
        [--skip-pdf] [--skip-text] \\
        [--resume] \\
        [--sanity-test 1]

``--sanity-test N`` processes only the first N sites (smoke test).
``--resume`` skips sites already recorded in data/audit/promoted_all.jsonl.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Project root on sys.path so crawler.* imports work when called from any cwd
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from crawler import db_libertree as _ldb
from crawler import pdf_downloader as _pdf
from crawler import converter as _conv

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROMOTED_LOG = _PROJECT_ROOT / "data" / "audit" / "promoted_all.jsonl"
RUN_LOG = _PROJECT_ROOT / "data" / "audit" / "promote_all_run.log"

_PRINT_LOCK = threading.Lock()

# Source directories and their success key
_SOURCE_CONFIGS = [
    ("data/audit/sample_runs",          "codex_gen_success"),
    ("data/audit/codex_required_runs",  "codex_gen_success"),
    ("data/audit/claude_required_runs", "claude_gen_success"),
]

# ---------------------------------------------------------------------------
# Logging setup — stderr + file
# ---------------------------------------------------------------------------

def _setup_logging() -> None:
    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
    fmt = "%(asctime)s %(levelname)s %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.StreamHandler(sys.stderr),
            logging.FileHandler(str(RUN_LOG), encoding="utf-8"),
        ],
    )


def _log(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, file=sys.stderr, flush=True)
        try:
            with open(str(RUN_LOG), "a", encoding="utf-8") as fh:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                fh.write(f"{ts} {msg}\n")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hms(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m {sec:02d}s"
    return f"{m}m {sec:02d}s"


# ---------------------------------------------------------------------------
# Collect sites from audit sources
# ---------------------------------------------------------------------------

def collect_sites(sources: list[str]) -> list[dict]:
    """Scan audit source dirs, return deduplicated list of promotable sites."""
    seen_site_ids: set[str] = set()
    sites: list[dict] = []

    # Build the list of (dir, success_key) to scan
    source_map = {cfg[0].split("/")[-1]: cfg for cfg in _SOURCE_CONFIGS}
    configs_to_scan = []
    for src in sources:
        if src in source_map:
            configs_to_scan.append(source_map[src])
        else:
            _log(f"[promote_all] WARNING: unknown source '{src}', skipping")

    for src_dir_rel, success_key in configs_to_scan:
        src_dir = _PROJECT_ROOT / src_dir_rel
        if not src_dir.exists():
            _log(f"[promote_all] WARNING: source dir not found: {src_dir}")
            continue

        for f in sorted(src_dir.glob("*.json")):
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except Exception as e:
                _log(f"[promote_all] WARNING: cannot parse {f}: {e}")
                continue

            if not d.get(success_key):
                continue

            site_id = d.get("site_id_generated") or d.get("site_id")
            if not site_id:
                continue

            # Deduplicate — first source wins
            if site_id in seen_site_ids:
                continue

            py_path = _PROJECT_ROOT / "crawler" / "sites" / "custom" / f"{site_id}.py"
            if not py_path.exists():
                _log(f"[promote_all] SKIP {site_id}: .py not found at {py_path}")
                continue

            seen_site_ids.add(site_id)
            sites.append({
                "site_id": site_id,
                "sheet": d.get("sheet", ""),
                "host": d.get("host", ""),
                "url": d.get("url", ""),
                "entry_id": d.get("entry_id", ""),
                "source": src_dir_rel.split("/")[-1],
                "py_path": py_path,
            })

    return sites


# ---------------------------------------------------------------------------
# Load / append promoted log
# ---------------------------------------------------------------------------

def load_promoted_log(log_path: Path) -> set[str]:
    """Return set of already-promoted site_ids."""
    result: set[str] = set()
    if not log_path.exists():
        return result
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            sid = rec.get("site_id")
            if sid:
                result.add(sid)
        except Exception:
            pass
    return result


def append_promoted_log(log_path: Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _PRINT_LOCK:
        with open(str(log_path), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Dynamic crawler loader
# ---------------------------------------------------------------------------

def _load_crawler_class(py_path: Path, site_id: str):
    """Dynamically import a custom crawler module and return the crawler class.

    Finds the first class that:
      - is defined in the module (not imported)
      - has a ``crawl`` method
      - its ``site_id`` class attribute (or instance attribute) matches site_id
        OR its name contains a CamelCase version of site_id tokens
    """
    import inspect
    from crawler.base_crawler import BaseCrawler

    spec = importlib.util.spec_from_file_location(f"custom_crawler_{site_id}", py_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    candidates = []
    for name, obj in inspect.getmembers(mod, inspect.isclass):
        if obj.__module__ != mod.__name__:
            continue  # imported, not defined here
        if not issubclass(obj, BaseCrawler):
            continue
        if inspect.isabstract(obj):
            continue
        candidates.append(obj)

    if not candidates:
        raise ImportError(f"No concrete BaseCrawler subclass found in {py_path}")

    # Prefer exact site_id match
    for cls in candidates:
        try:
            if getattr(cls, "site_id", None) == site_id:
                return cls
        except Exception:
            pass

    # Fallback: return first candidate
    return candidates[0]


# ---------------------------------------------------------------------------
# Per-site worker
# ---------------------------------------------------------------------------

def promote_one_site(
    site_meta: dict,
    *,
    db_path: Path,
    blob_root: Path,
    limit: int,
    timeout_s: int,
    skip_pdf: bool,
    skip_text: bool,
    concurrent_pdfs: int,
) -> dict:
    """Crawl one custom site, download PDFs, extract text.

    Returns a result dict suitable for promoted_all.jsonl.
    """
    site_id = site_meta["site_id"]
    t0 = time.time()
    errors: list[str] = []

    result = {
        "ts": _now_iso(),
        "site_id": site_id,
        "sheet": site_meta.get("sheet", ""),
        "host": site_meta.get("host", ""),
        "source": site_meta.get("source", ""),
        "items_collected": 0,
        "pdf_downloaded": 0,
        "pdf_failed": 0,
        "text_extracted": 0,
        "elapsed_seconds": 0.0,
        "error": None,
    }

    try:
        # Open per-thread libertree.db connection
        conn = _ldb.open_db(db_path)
        _ldb.init_db(conn)

        # Register/update site record
        _ldb.upsert_site(
            conn,
            site_id=site_id,
            site_name=f"Custom: {site_id}",
            site_url=site_meta.get("url", ""),
            sheet=site_meta.get("sheet"),
        )

        # Baseline for new rows detection
        pre_max = conn.execute(
            "SELECT COALESCE(MAX(seq_id), 0) FROM documents WHERE site_id = ?",
            (site_id,),
        ).fetchone()[0]

        # Dynamically load and instantiate crawler
        py_path = site_meta["py_path"]
        try:
            crawler_cls = _load_crawler_class(py_path, site_id)
            crawler = crawler_cls(db_conn=conn)
        except Exception as e:
            result["error"] = f"crawler load failed: {e}"
            conn.close()
            return result

        # Per-site timeout via threading.Event
        timed_out = threading.Event()
        timer = threading.Timer(timeout_s, timed_out.set)
        timer.daemon = True
        timer.start()

        try:
            saved = crawler.crawl(limit=limit)
            if timed_out.is_set():
                errors.append(f"timed out after {timeout_s}s")
        except Exception as e:
            errors.append(f"crawl error: {e}")
            saved = 0
        finally:
            timer.cancel()

        result["items_collected"] = saved or 0

        # Identify new rows inserted during this crawl
        post_max = conn.execute(
            "SELECT COALESCE(MAX(seq_id), 0) FROM documents WHERE site_id = ?",
            (site_id,),
        ).fetchone()[0]

        if post_max > pre_max:
            new_rows = conn.execute(
                "SELECT seq_id, pdf_url FROM documents "
                "WHERE site_id = ? AND seq_id > ? AND seq_id <= ?",
                (site_id, pre_max, post_max),
            ).fetchall()
        else:
            new_rows = []

        seq_ids = [(r[0], r[1]) for r in new_rows]

        # PDF download — each thread opens its own connection
        if not skip_pdf and seq_ids:
            pdf_ok = 0
            pdf_fail = 0

            def _download_one(seq_pdf):
                seq_id_, pdf_url_ = seq_pdf
                if not pdf_url_:
                    return False
                _tconn = _ldb.open_db(db_path)
                try:
                    res = _pdf.download_pdf_for(
                        _tconn, seq_id_, pdf_url_, blob_root=blob_root
                    )
                    return res.get("success", False)
                except Exception:
                    return False
                finally:
                    _tconn.close()

            with ThreadPoolExecutor(max_workers=concurrent_pdfs) as pex:
                pdf_futures = {pex.submit(_download_one, sp): sp for sp in seq_ids}
                for fut in as_completed(pdf_futures):
                    if fut.result():
                        pdf_ok += 1
                    else:
                        pdf_fail += 1

            result["pdf_downloaded"] = pdf_ok
            result["pdf_failed"] = pdf_fail
        else:
            result["pdf_downloaded"] = 0
            result["pdf_failed"] = 0

        # Text extraction (sequential, uses main conn)
        if not skip_text and seq_ids:
            text_ok = 0
            for seq_id_, _pdf_url in seq_ids:
                try:
                    res = _conv.extract_text_for(conn, seq_id_, blob_root=blob_root)
                    if res.get("success"):
                        text_ok += 1
                except Exception:
                    pass
            result["text_extracted"] = text_ok

        conn.close()

    except Exception as e:
        result["error"] = str(e)
        errors.append(str(e))

    result["elapsed_seconds"] = round(time.time() - t0, 1)
    if errors and not result["error"]:
        result["error"] = "; ".join(errors[:3])

    return result


# ---------------------------------------------------------------------------
# Progress reporter thread
# ---------------------------------------------------------------------------

class ProgressReporter:
    def __init__(self, total: int, interval_s: int = 300):
        self._total = total
        self._interval = interval_s
        self._completed: list[dict] = []
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def site_started(self, idx: int, site_meta: dict) -> None:
        with self._lock:
            self._active.add(site_meta["site_id"])
        _log(
            f"[promote_all] {idx}/{self._total} START "
            f"{site_meta['site_id']} ({site_meta.get('host', '')})"
        )

    def site_ended(self, idx: int, result: dict) -> None:
        sid = result["site_id"]
        with self._lock:
            self._active.discard(sid)
            self._completed.append(result)
        n = result["items_collected"]
        p = result["pdf_downloaded"]
        t = result["text_extracted"]
        e = result["elapsed_seconds"]
        err_suffix = f" ERROR={result['error']}" if result.get("error") else ""
        _log(
            f"[promote_all] {idx}/{self._total} END   {sid} "
            f"items={n} pdf={p}/{n} text={t} elapsed={int(e)}s{err_suffix}"
        )

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                completed = len(self._completed)
                active = list(self._active)
            _log(
                f"[promote_all] IN PROGRESS — completed={completed} "
                f"active_sites={active}"
            )

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bulk-promote codex/claude custom crawlers into libertree.db"
    )
    parser.add_argument(
        "--sources",
        nargs="+",
        default=["sample_runs", "codex_required_runs", "claude_required_runs"],
        metavar="SRC",
        help="Audit source dirs to scan (default: all three)",
    )
    parser.add_argument("--db", default="data/libertree.db")
    parser.add_argument("--blob-root", default="libertree")
    parser.add_argument("--per-site-limit", type=int, default=500)
    parser.add_argument("--per-site-timeout", type=int, default=1800)
    parser.add_argument("--concurrent-sites", type=int, default=4)
    parser.add_argument("--concurrent-pdfs", type=int, default=8)
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--skip-text", action="store_true")
    parser.add_argument("--resume", action="store_true",
                        help="Skip sites already in promoted_all.jsonl")
    parser.add_argument("--sanity-test", type=int, default=0,
                        help="Process only N sites (smoke test). 0 = all.")
    parser.add_argument("--site-offset", type=int, default=0,
                        help="Skip the first N sites (for batched runs)")
    parser.add_argument("--site-limit-count", type=int, default=0,
                        help="Process at most N sites starting from offset (0 = all remaining)")
    parser.add_argument("--only-site-ids", default="",
                        help="Comma-separated site_id list. If set, only these sites are processed.")
    parser.add_argument("--only-site-ids-file", default="",
                        help="Path to a newline-delimited file of site_ids to process (alternative to --only-site-ids).")
    args = parser.parse_args()

    RUN_LOG.parent.mkdir(parents=True, exist_ok=True)

    db_path = (_PROJECT_ROOT / args.db).resolve()
    blob_root = (_PROJECT_ROOT / args.blob_root).resolve()

    # 1. Collect all promotable sites
    sites = collect_sites(args.sources)
    _log(f"[promote_all] Found {len(sites)} unique crawlers from {len(args.sources)} sources")

    if not sites:
        _log("[promote_all] Nothing to promote. Exiting.")
        sys.exit(0)

    # 2. Resume filter
    if args.resume:
        already_done = load_promoted_log(PROMOTED_LOG)
        before = len(sites)
        sites = [s for s in sites if s["site_id"] not in already_done]
        _log(
            f"[promote_all] After resume filter: {len(sites)} remaining "
            f"(skipped {before - len(sites)} already promoted)"
        )

    # 2b. site_id whitelist (used for recollection of cap-reached sites)
    whitelist_ids: set[str] = set()
    if args.only_site_ids:
        whitelist_ids.update(s.strip() for s in args.only_site_ids.split(",") if s.strip())
    if args.only_site_ids_file:
        try:
            for line in open(args.only_site_ids_file, encoding="utf-8"):
                sid = line.strip()
                if sid and not sid.startswith("#"):
                    whitelist_ids.add(sid)
        except OSError as e:
            _log(f"[promote_all] WARN: cannot read --only-site-ids-file {args.only_site_ids_file}: {e}")
    if whitelist_ids:
        before = len(sites)
        sites = [s for s in sites if s["site_id"] in whitelist_ids]
        _log(
            f"[promote_all] only-site-ids filter: {len(sites)} of {before} site(s) "
            f"matched whitelist ({len(whitelist_ids)} ids)"
        )

    # 3. Sanity-test cap
    if args.sanity_test and args.sanity_test > 0:
        sites = sites[: args.sanity_test]
        _log(f"[promote_all] --sanity-test {args.sanity_test}: processing {len(sites)} site(s)")

    # 3b. Batch slicing (for memory-bounded sequential batches)
    if args.site_offset or args.site_limit_count:
        before = len(sites)
        start = args.site_offset
        stop = start + args.site_limit_count if args.site_limit_count > 0 else None
        sites = sites[start:stop]
        _log(
            f"[promote_all] Batch slice offset={args.site_offset} "
            f"limit={args.site_limit_count}: {len(sites)} of {before} site(s)"
        )
        if not sites:
            _log("[promote_all] Slice is empty — nothing to do, exiting.")
            sys.exit(0)

    total = len(sites)
    _log(
        f"[promote_all] Will promote {total} site(s) "
        f"(concurrent-sites={args.concurrent_sites}, "
        f"concurrent-pdfs={args.concurrent_pdfs}, "
        f"per-site-limit={args.per_site_limit}, "
        f"per-site-timeout={args.per_site_timeout}s)"
    )

    t_start = time.time()
    reporter = ProgressReporter(total, interval_s=300)

    completed_results: list[dict] = []
    idx_lock = threading.Lock()
    idx_counter = [0]

    def _worker(site_meta: dict) -> dict:
        with idx_lock:
            idx_counter[0] += 1
            idx = idx_counter[0]
        reporter.site_started(idx, site_meta)
        try:
            res = promote_one_site(
                site_meta,
                db_path=db_path,
                blob_root=blob_root,
                limit=args.per_site_limit,
                timeout_s=args.per_site_timeout,
                skip_pdf=args.skip_pdf,
                skip_text=args.skip_text,
                concurrent_pdfs=args.concurrent_pdfs,
            )
        except Exception as e:
            res = {
                "ts": _now_iso(),
                "site_id": site_meta["site_id"],
                "sheet": site_meta.get("sheet", ""),
                "host": site_meta.get("host", ""),
                "source": site_meta.get("source", ""),
                "items_collected": 0,
                "pdf_downloaded": 0,
                "pdf_failed": 0,
                "text_extracted": 0,
                "elapsed_seconds": 0.0,
                "error": str(e),
            }
        reporter.site_ended(idx, res)
        append_promoted_log(PROMOTED_LOG, res)
        # Force GC after each site to reclaim PDF/HTML parsing memory
        import gc
        gc.collect()
        return res

    try:
        with ThreadPoolExecutor(max_workers=args.concurrent_sites) as executor:
            futures = {executor.submit(_worker, s): s for s in sites}
            for fut in as_completed(futures):
                try:
                    completed_results.append(fut.result())
                except Exception as e:
                    site = futures[fut]
                    _log(f"[promote_all] FATAL for {site['site_id']}: {e}")
    except KeyboardInterrupt:
        _log("[promote_all] KeyboardInterrupt — waiting for running workers...")

    reporter.stop()

    # Summary
    elapsed = time.time() - t_start
    total_items = sum(r["items_collected"] for r in completed_results)
    total_pdf = sum(r["pdf_downloaded"] for r in completed_results)
    total_text = sum(r["text_extracted"] for r in completed_results)
    failures = [r for r in completed_results if r.get("error")]

    pdf_pct = (total_pdf / total_items * 100) if total_items else 0
    txt_pct = (total_text / max(total_pdf, 1) * 100) if total_pdf else 0

    failure_lines = ""
    if failures:
        failure_lines = "  failures by site:\n" + "\n".join(
            f"    {r['site_id']}: {r['error']}" for r in failures[:10]
        )
    else:
        failure_lines = "  failures: none"

    _log(
        f"\n[promote_all] Done. {len(completed_results)}/{total} sites processed.\n"
        f"  total items collected: {total_items:,}\n"
        f"  total pdf downloaded:  {total_pdf:,} / {total_items:,} ({pdf_pct:.1f}%)\n"
        f"  total text extracted:  {total_text:,} / {max(total_pdf, 1):,} ({txt_pct:.1f}%)\n"
        f"  elapsed: {_hms(elapsed)}\n"
        + failure_lines
    )


if __name__ == "__main__":
    main()
