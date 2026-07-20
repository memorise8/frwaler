# -*- coding: utf-8 -*-
"""Phase B — promote Tier-1 ok sites into libertree.db.

Usage
-----
    .venv/bin/python scripts/promote_tier1.py \\
        [--tier1-runs-dir data/audit/tier1_runs] \\
        [--configs-dir crawler/sites/configs] \\
        [--db data/libertree.db] \\
        [--blob-root libertree] \\
        [--per-site-limit 5000] \\
        [--per-site-timeout 3600] \\
        [--concurrent-sites 4] \\
        [--concurrent-pdfs 8] \\
        [--skip-pdf] \\
        [--skip-text] \\
        [--resume] \\
        [--sanity-test 1]

``--sanity-test N`` processes only the first N ok sites (for smoke-testing).
``--resume`` skips sites already recorded in data/audit/promoted.jsonl.
"""

from __future__ import annotations

import argparse
import json
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
from crawler.generic_crawler import GenericCrawler

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
PROMOTED_LOG = _PROJECT_ROOT / "data" / "audit" / "promoted.jsonl"
_PRINT_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    with _PRINT_LOCK:
        print(msg, file=sys.stderr, flush=True)


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
# Load ok sites from tier1_runs directory
# ---------------------------------------------------------------------------

def load_ok_sites(tier1_runs_dir: Path, configs_dir: Path) -> list[dict]:
    """Return list of site dicts with confirmed config files."""
    seen_site_ids: set[str] = set()
    sites: list[dict] = []

    for f in sorted(tier1_runs_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue

        if d.get("tier1_status") != "ok":
            continue

        site_id = d.get("site_id") or d.get("analyzer", {}).get("site_id_inferred")
        if not site_id:
            continue

        # Deduplicate by site_id
        if site_id in seen_site_ids:
            continue
        seen_site_ids.add(site_id)

        config_path = configs_dir / f"{site_id}.json"
        if not config_path.exists():
            _log(f"[promote] SKIP {site_id}: config not found at {config_path}")
            continue

        sites.append({
            "site_id": site_id,
            "sheet": d.get("sheet", ""),
            "host": d.get("host", ""),
            "url": d.get("url", ""),
            "entry_id": d.get("entry_id", ""),
            "config_path": config_path,
        })

    return sites


# ---------------------------------------------------------------------------
# Load / append promoted log
# ---------------------------------------------------------------------------

def load_promoted_log(log_path: Path) -> dict[str, dict]:
    """Return {site_id: record} for already-promoted sites."""
    result: dict[str, dict] = {}
    if not log_path.exists():
        return result
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            if rec.get("site_id"):
                result[rec["site_id"]] = rec
        except Exception:
            pass
    return result


def append_promoted_log(log_path: Path, record: dict) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with _PRINT_LOCK:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


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
    """Crawl one site, download PDFs, extract text.

    Returns a result dict suitable for the promoted.jsonl log.
    """
    site_id = site_meta["site_id"]
    t0 = time.time()
    errors: list[str] = []

    result = {
        "ts": _now_iso(),
        "site_id": site_id,
        "sheet": site_meta.get("sheet", ""),
        "host": site_meta.get("host", ""),
        "items_collected": 0,
        "pdf_downloaded": 0,
        "pdf_failed": 0,
        "text_extracted": 0,
        "elapsed_seconds": 0.0,
        "error": None,
    }

    try:
        conn = _ldb.open_db(db_path)
        _ldb.init_db(conn)

        # Register the site
        config_path = site_meta["config_path"]
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception as e:
            result["error"] = f"config read failed: {e}"
            return result

        _ldb.upsert_site(
            conn,
            site_id=site_id,
            site_name=cfg.get("site_name", site_id),
            site_url=cfg.get("base_url", site_meta.get("url", "")),
            sheet=site_meta.get("sheet"),
        )

        # Record max seq_id before crawl to identify new rows
        pre_max = conn.execute("SELECT COALESCE(MAX(seq_id), 0) FROM documents WHERE site_id = ?", (site_id,)).fetchone()[0]

        # Instantiate and run crawler
        crawler = GenericCrawler(str(config_path), conn)

        # Per-site timeout via a threading.Event + timer
        timed_out = threading.Event()
        timer = threading.Timer(timeout_s, timed_out.set)
        timer.daemon = True
        timer.start()

        try:
            saved = crawler.crawl(limit=limit)
        except Exception as e:
            errors.append(f"crawl error: {e}")
            saved = 0
        finally:
            timer.cancel()

        result["items_collected"] = saved or 0

        # Collect seq_ids of newly inserted documents for this site
        post_max = conn.execute("SELECT COALESCE(MAX(seq_id), 0) FROM documents WHERE site_id = ?", (site_id,)).fetchone()[0]

        if post_max > pre_max:
            new_rows = conn.execute(
                "SELECT seq_id, pdf_url FROM documents WHERE site_id = ? AND seq_id > ? AND seq_id <= ?",
                (site_id, pre_max, post_max),
            ).fetchall()
        else:
            new_rows = []

        seq_ids = [(r[0], r[1]) for r in new_rows]

        # PDF download (threaded — each thread opens its own connection)
        if not skip_pdf and seq_ids:
            pdf_ok = 0
            pdf_fail = 0

            def _download_one(seq_pdf):
                seq_id_, pdf_url_ = seq_pdf
                if not pdf_url_:
                    return False
                # Open a per-thread connection (SQLite connections are not thread-safe)
                _tconn = _ldb.open_db(db_path)
                try:
                    res = _pdf.download_pdf_for(_tconn, seq_id_, pdf_url_, blob_root=blob_root)
                    return res.get("success", False)
                except Exception:
                    return False
                finally:
                    _tconn.close()

            with ThreadPoolExecutor(max_workers=concurrent_pdfs) as pex:
                futures = {pex.submit(_download_one, sp): sp for sp in seq_ids}
                for fut in as_completed(futures):
                    if fut.result():
                        pdf_ok += 1
                    else:
                        pdf_fail += 1

            result["pdf_downloaded"] = pdf_ok
            result["pdf_failed"] = pdf_fail
        else:
            result["pdf_downloaded"] = 0
            result["pdf_failed"] = 0

        # Text extraction
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
        self._promoted: list[dict] = []
        self._active: set[str] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def site_started(self, idx: int, site_meta: dict) -> None:
        with self._lock:
            self._active.add(site_meta["site_id"])
        _log(f"[promote] {idx}/{self._total} START {site_meta['site_id']} ({site_meta.get('host','')})")

    def site_ended(self, idx: int, result: dict) -> None:
        sid = result["site_id"]
        with self._lock:
            self._active.discard(sid)
            self._promoted.append(result)
        n = result["items_collected"]
        p = result["pdf_downloaded"]
        t = result["text_extracted"]
        e = result["elapsed_seconds"]
        _log(
            f"[promote] {idx}/{self._total} END   {sid} "
            f"items={n} pdf={p}/{n} text={t}/{max(p,1)} elapsed={int(e)}s"
            + (f" ERROR={result['error']}" if result.get("error") else "")
        )

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                promoted = len(self._promoted)
                active = list(self._active)
            _log(
                f"[promote] IN PROGRESS — promoted={promoted} active_sites={active}"
            )

    def stop(self) -> None:
        self._stop.set()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Promote Tier-1 ok sites into libertree.db"
    )
    parser.add_argument("--tier1-runs-dir", default="data/audit/tier1_runs")
    parser.add_argument("--configs-dir", default="crawler/sites/configs")
    parser.add_argument("--db", default="data/libertree.db")
    parser.add_argument("--blob-root", default="libertree")
    parser.add_argument("--per-site-limit", type=int, default=5000)
    parser.add_argument("--per-site-timeout", type=int, default=3600)
    parser.add_argument("--concurrent-sites", type=int, default=4)
    parser.add_argument("--concurrent-pdfs", type=int, default=8)
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--skip-text", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sanity-test", type=int, default=0,
                        help="Process only N sites (smoke test). 0 = all.")
    args = parser.parse_args()

    root = _PROJECT_ROOT
    tier1_runs_dir = (root / args.tier1_runs_dir).resolve()
    configs_dir = (root / args.configs_dir).resolve()
    db_path = (root / args.db).resolve()
    blob_root = (root / args.blob_root).resolve()

    # Load ok sites
    sites = load_ok_sites(tier1_runs_dir, configs_dir)
    if not sites:
        _log("[promote] No ok sites found with matching config files. Nothing to do.")
        sys.exit(0)

    # Resume filter
    if args.resume:
        promoted_log = load_promoted_log(PROMOTED_LOG)
        before = len(sites)
        sites = [s for s in sites if s["site_id"] not in promoted_log]
        _log(f"[promote] Resume: skipping {before - len(sites)} already-promoted sites")

    # Sanity-test cap
    if args.sanity_test and args.sanity_test > 0:
        sites = sites[: args.sanity_test]
        _log(f"[promote] --sanity-test {args.sanity_test}: processing {len(sites)} site(s)")

    total = len(sites)
    _log(f"[promote] Will promote {total} site(s) "
         f"(concurrent={args.concurrent_sites}, pdf-workers={args.concurrent_pdfs})")

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
        reporter.site_ended(idx, res)
        append_promoted_log(PROMOTED_LOG, res)
        return res

    try:
        with ThreadPoolExecutor(max_workers=args.concurrent_sites) as executor:
            futures = {executor.submit(_worker, s): s for s in sites}
            for fut in as_completed(futures):
                try:
                    completed_results.append(fut.result())
                except Exception as e:
                    site = futures[fut]
                    _log(f"[promote] FATAL for {site['site_id']}: {e}")
    except KeyboardInterrupt:
        _log("[promote] KeyboardInterrupt — waiting for running workers to finish...")

    reporter.stop()

    # Summary
    elapsed = time.time() - t_start
    total_items = sum(r["items_collected"] for r in completed_results)
    total_pdf = sum(r["pdf_downloaded"] for r in completed_results)
    total_text = sum(r["text_extracted"] for r in completed_results)
    failures = [r for r in completed_results if r.get("error")]
    failures_sorted = sorted(failures, key=lambda r: r.get("items_collected", 0))[:5]

    pdf_pct = (total_pdf / total_items * 100) if total_items else 0
    txt_pct = (total_text / total_pdf * 100) if total_pdf else 0

    _log(
        f"\n[promote] Done. {len(completed_results)}/{total} sites processed.\n"
        f"  total items collected: {total_items:,}\n"
        f"  total pdf downloaded:  {total_pdf:,} / {total_items:,} ({pdf_pct:.1f}%)\n"
        f"  total text extracted:  {total_text:,} / {max(total_pdf, 1):,} ({txt_pct:.1f}%)\n"
        f"  elapsed: {_hms(elapsed)}\n"
        + (
            "  failures by site:\n" +
            "\n".join(f"    {r['site_id']}: {r['error']}" for r in failures_sorted)
            if failures_sorted else "  failures: none"
        )
    )


if __name__ == "__main__":
    main()
