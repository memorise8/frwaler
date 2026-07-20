#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 3.7 Tier 1 batch processor.

For each entry in coverage_report.csv where analyzer_status=ok and
recovery_status != still_blocked:
  1. Run analyzer.analyze_site  → infer selectors + track token cost
  2. Write JSON config to configs_dir/<site_id>.json
  3. Run GenericCrawler via subprocess isolation (limit=per_site_limit)
  4. Write per-entry result JSON to out_dir/<entry_id>.json
  5. Append cost record to cost_log JSONL
  6. Generate a summary Markdown report

Usage:
    .venv/bin/python scripts/tier1_batch.py --sanity-test 5
    .venv/bin/python scripts/tier1_batch.py   # full run
    .venv/bin/python scripts/tier1_batch.py --resume --concurrency 4 --per-site-timeout 300
"""

import argparse
import csv
import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ── project root on sys.path ─────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from crawler import db as dbm
from crawler.analyzer import analyze_site, _generate_site_id
from crawler.generic_crawler import GenericCrawler

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_COVERAGE_CSV = _ROOT / "data" / "audit" / "coverage_report.csv"
DEFAULT_OUT_DIR       = _ROOT / "data" / "audit" / "tier1_runs"
DEFAULT_CONFIGS_DIR   = _ROOT / "crawler" / "sites" / "configs"
DEFAULT_REPORT_MD     = _ROOT / "data" / "audit" / "phase37_summary.md"
DEFAULT_COST_LOG      = _ROOT / "data" / "audit" / "api_cost_log.jsonl"
DEFAULT_PER_SITE_LIMIT    = 20
DEFAULT_PER_SITE_TIMEOUT  = 300
DEFAULT_CONCURRENCY       = 4

# ── graceful shutdown ─────────────────────────────────────────────────────────
_SHUTDOWN = False

def _handle_sigint(sig, frame):
    global _SHUTDOWN
    print("\n[tier1] SIGINT received — will stop after current entry.", file=sys.stderr)
    _SHUTDOWN = True

signal.signal(signal.SIGINT, _handle_sigint)

# ── thread-safety locks ───────────────────────────────────────────────────────
_csv_lock     = threading.Lock()
_cost_lock    = threading.Lock()
_print_lock   = threading.Lock()
_results_lock = threading.Lock()
_counters_lock = threading.Lock()


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_entries(csv_path: Path):
    """Load coverage CSV rows where analyzer_status=ok AND not still_blocked."""
    entries = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("analyzer_status", "") == "ok" and row.get("recovery_status", "") != "still_blocked":
                entries.append(row)
    return entries


def _sanity_sample(entries, n: int):
    """Pick n entries spread across different sheets for diversity."""
    by_sheet: dict[str, list] = {}
    for e in entries:
        sheet = e.get("sheet", "")
        by_sheet.setdefault(sheet, []).append(e)

    sheets = sorted(by_sheet.keys())
    selected = []
    idx = 0
    while len(selected) < n:
        sheet = sheets[idx % len(sheets)]
        if by_sheet[sheet]:
            selected.append(by_sheet[sheet].pop(0))
        idx += 1
        if idx > len(sheets) * 100:
            break
    return selected[:n]


def _derive_site_id(url: str) -> str:
    return _generate_site_id(url)


def _write_config(config_path: Path, config: dict):
    """Write config JSON (without _analyzer_meta) to disk.

    Replaces None selectors with safe fallback values so GenericCrawler
    does not crash with NoneType.replace errors.
    """
    import copy
    clean = copy.deepcopy({k: v for k, v in config.items() if k != "_analyzer_meta"})
    # Ensure item_container and item_link are never None
    lp_sel = clean.get("list_page", {}).get("selectors", {})
    if not lp_sel.get("item_container"):
        lp_sel["item_container"] = "tr"
    if not lp_sel.get("item_link"):
        lp_sel["item_link"] = "a[href]"
    if not lp_sel.get("item_link_attr"):
        lp_sel["item_link_attr"] = "href"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(clean, f, ensure_ascii=False, indent=2)


def _append_cost_log(cost_log_path: Path, record: dict):
    cost_log_path.parent.mkdir(parents=True, exist_ok=True)
    with _cost_lock:
        with open(cost_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_crawler_subprocess(site_id: str, config_path: Path, site_name: str,
                            base_url: str, limit: int, timeout_sec: int = 300):
    """Spawn a Python subprocess to run GenericCrawler with hard timeout.

    Returns dict with keys: items_captured, items_with_abstract,
    elapsed_seconds, completed_naturally, error (str|None), timed_out (bool).
    """
    runner_script = f'''
import sys, sqlite3, json, time, traceback
sys.path.insert(0, {str(_ROOT)!r})

from crawler import db as dbm
from crawler.generic_crawler import GenericCrawler

start = time.time()
items_captured = 0
items_with_abstract = 0
error_str = None
completed_naturally = False

try:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbm.init_db(conn)
    dbm.register_site(conn, {site_id!r}, {site_name!r}, {base_url!r})

    crawler = GenericCrawler(config_path={str(config_path)!r}, db_conn=conn, delay=0.5)
    items_captured = crawler.crawl(limit={limit})
    completed_naturally = True

    rows = conn.execute("SELECT abstract FROM papers").fetchall()
    items_with_abstract = sum(1 for r in rows if r["abstract"] and len(r["abstract"]) >= 100)
    if items_with_abstract == 0:
        rows = conn.execute("SELECT abstract FROM documents").fetchall()
        items_with_abstract = sum(1 for r in rows if r["abstract"] and len(r["abstract"]) >= 100)
    conn.close()
except Exception as exc:
    error_str = type(exc).__name__ + ": " + str(exc)[:200]
    completed_naturally = False

elapsed = time.time() - start
print("__RESULT__:" + json.dumps({{
    "items_captured": items_captured or 0,
    "items_with_abstract": items_with_abstract,
    "elapsed_seconds": round(elapsed, 1),
    "completed_naturally": completed_naturally,
    "error": error_str,
    "timed_out": False,
}}))
'''

    start = time.time()
    proc = subprocess.Popen(
        [str(_ROOT / '.venv' / 'bin' / 'python'), '-c', runner_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors='replace',
    )
    try:
        stdout, _ = proc.communicate(timeout=timeout_sec)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        elapsed = time.time() - start
        return {
            'items_captured': 0,
            'items_with_abstract': 0,
            'elapsed_seconds': round(elapsed, 1),
            'completed_naturally': False,
            'error': f'subprocess timeout {timeout_sec}s',
            'timed_out': True,
        }

    for line in stdout.splitlines():
        if line.startswith('__RESULT__:'):
            try:
                return json.loads(line[len('__RESULT__:'):])
            except json.JSONDecodeError:
                pass

    elapsed = time.time() - start
    # Return stderr snippet for debugging
    stderr_snippet = stdout[-300:].replace('\n', ' ') if stdout else ''
    return {
        'items_captured': 0,
        'items_with_abstract': 0,
        'elapsed_seconds': round(elapsed, 1),
        'completed_naturally': False,
        'error': f'no __RESULT__ from subprocess. tail: {stderr_snippet}',
        'timed_out': False,
    }


def _classify_status(analyzer_ok: bool, analyzer_error: str | None,
                     items_captured: int, items_with_abstract: int,
                     crawler_error: str | None) -> str:
    if not analyzer_ok:
        return "analyzer_fail"
    if crawler_error:
        return "crawler_fail"
    if items_captured >= 1 and items_with_abstract >= 1:
        return "ok"
    if items_captured >= 1:
        return "partial"
    return "partial"  # analyzer ok but nothing crawled


def _write_entry_result(out_dir: Path, result: dict):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result['entry_id']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def _already_done(out_dir: Path, entry_id: str) -> bool:
    return (out_dir / f"{entry_id}.json").exists()


def _print_progress(done: int, total: int, start_ts: float, counters: dict, cost: float):
    elapsed = time.time() - start_ts
    pct = done / total * 100 if total else 0
    mins = elapsed / 60
    parts = " ".join(f"{k}={v}" for k, v in counters.items())
    with _print_lock:
        print(
            f"[tier1] {done}/{total} ({pct:.1f}%) — elapsed {mins:.1f}m — {parts} — cost=${cost:.2f}",
            file=sys.stderr,
            flush=True,
        )


def _write_summary(report_md: Path, entries_total: int, processed: int,
                   counters: dict, total_cost: float, results: list,
                   start_ts: float):
    elapsed = time.time() - start_ts
    report_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Phase 3.7 Tier 1 Batch Summary",
        f"",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        f"",
        f"## Overview",
        f"",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Eligible entries (analyzer_ok) | {entries_total} |",
        f"| Processed this run | {processed} |",
        f"| Elapsed | {elapsed/60:.1f} min |",
        f"| Total API cost | ${total_cost:.4f} |",
        f"",
        f"## Status Distribution",
        f"",
        f"| Status | Count |",
        f"|--------|-------|",
    ]
    for k, v in counters.items():
        lines.append(f"| {k} | {v} |")

    tier1_count = counters.get("ok", 0)
    tier2_count = processed - tier1_count
    lines += [
        f"",
        f"## Promotion",
        f"",
        f"| Tier | Count |",
        f"|------|-------|",
        f"| tier1 (ready for direct crawl) | {tier1_count} |",
        f"| tier2 (needs Codex) | {tier2_count} |",
        f"",
        f"## Per-Entry Results",
        f"",
        f"| entry_id | sheet | host | status | items | cost_usd | model |",
        f"|----------|-------|------|--------|-------|----------|-------|",
    ]
    for r in results:
        a = r.get("analyzer", {})
        gc = r.get("generic_crawler", {})
        lines.append(
            f"| {r['entry_id']} | {r.get('sheet','')} | {r.get('host','')} "
            f"| {r['tier1_status']} | {gc.get('items_captured',0)} "
            f"| ${a.get('estimated_cost_usd',0):.4f} | {a.get('model_used','')} |"
        )

    with open(report_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[tier1] Report written to {report_md}", file=sys.stderr)


# ── per-entry worker (called from thread pool) ────────────────────────────────

def _process_entry(entry: dict, idx: int, total: int,
                   out_dir: Path, configs_dir: Path, cost_log: Path,
                   per_site_limit: int, per_site_timeout: int,
                   thread_id: str) -> dict | None:
    """Process a single entry. Returns result dict or None on fatal error."""
    entry_id = entry.get("entry_id", f"entry_{idx}")
    url      = entry.get("url") or entry.get("final_url", "")
    host     = entry.get("host", "")
    sheet    = entry.get("sheet", "")

    with _print_lock:
        print(
            f"\n[tier1][{thread_id}] === {idx+1}/{total} entry_id={entry_id} url={url[:80]}",
            file=sys.stderr, flush=True,
        )

    started_at  = datetime.now(timezone.utc).isoformat()
    entry_start = time.time()

    # ── Step 1: analyzer ──────────────────────────────────────────────────────
    analyzer_ok    = False
    analyzer_error = None
    config         = None
    analyzer_meta  = {}

    try:
        site_id = _derive_site_id(url)
        config  = analyze_site(url, site_id=site_id, save_config=False)
        if config is None:
            analyzer_error = "analyze_site returned None (fetch failed)"
        else:
            analyzer_ok   = True
            analyzer_meta = config.get("_analyzer_meta", {})
    except Exception as exc:
        analyzer_error = f"{type(exc).__name__}: {str(exc)[:200]}"
        traceback.print_exc()

    cost_record = {
        "ts":             datetime.now(timezone.utc).isoformat(),
        "stage":          "analyzer",
        "model":          analyzer_meta.get("model_used", "unknown"),
        "input_tokens":   analyzer_meta.get("input_tokens", 0),
        "output_tokens":  analyzer_meta.get("output_tokens", 0),
        "cost_usd":       analyzer_meta.get("estimated_cost_usd", 0.0),
        "entry_id":       entry_id,
        "fallback_used":  analyzer_meta.get("fallback_used", False),
    }
    _append_cost_log(cost_log, cost_record)

    # ── Step 2: write config + run crawler (subprocess) ───────────────────────
    items_captured      = 0
    items_with_abstract = 0
    crawler_elapsed     = 0.0
    crawler_completed   = False
    crawler_error       = None
    timed_out           = False

    if analyzer_ok and config:
        site_id_safe = _derive_site_id(url)
        config_path  = configs_dir / f"{site_id_safe}.json"
        _write_config(config_path, config)
        analyzer_meta["config_saved"] = True

        site_name = config.get("site_name", host)
        base_url  = config.get("base_url", f"https://{host}")

        with _print_lock:
            print(
                f"[tier1][{thread_id}] Running crawler subprocess for {site_id_safe} "
                f"(timeout={per_site_timeout}s)",
                file=sys.stderr, flush=True,
            )

        cr = run_crawler_subprocess(
            site_id=site_id_safe,
            config_path=config_path,
            site_name=site_name,
            base_url=base_url,
            limit=per_site_limit,
            timeout_sec=per_site_timeout,
        )
        items_captured      = cr.get("items_captured", 0)
        items_with_abstract = cr.get("items_with_abstract", 0)
        crawler_elapsed     = cr.get("elapsed_seconds", 0.0)
        crawler_completed   = cr.get("completed_naturally", False)
        crawler_error       = cr.get("error")
        timed_out           = cr.get("timed_out", False)
    else:
        analyzer_meta["config_saved"] = False
        site_id_safe = _derive_site_id(url)

    # ── Step 3: classify ──────────────────────────────────────────────────────
    try:
        tier1_status = _classify_status(
            analyzer_ok=analyzer_ok,
            analyzer_error=analyzer_error,
            items_captured=items_captured,
            items_with_abstract=items_with_abstract,
            crawler_error=crawler_error,
        )
    except Exception:
        tier1_status = "fail"

    promotion       = "tier1" if tier1_status == "ok" else "tier2"
    ended_at        = datetime.now(timezone.utc).isoformat()
    elapsed_total   = time.time() - entry_start

    result = {
        "entry_id":  entry_id,
        "url":       url,
        "host":      host,
        "sheet":     sheet,
        "site_id":   site_id_safe,
        "started_at": started_at,
        "ended_at":  ended_at,
        "elapsed_seconds": round(elapsed_total, 1),
        "analyzer": {
            "model_used":          analyzer_meta.get("model_used", "unknown"),
            "input_tokens":        analyzer_meta.get("input_tokens", 0),
            "output_tokens":       analyzer_meta.get("output_tokens", 0),
            "estimated_cost_usd":  analyzer_meta.get("estimated_cost_usd", 0.0),
            "selectors_inferred":  config.get("list_page", {}).get("selectors") if config else None,
            "config_saved":        analyzer_meta.get("config_saved", False),
            "error":               analyzer_error,
        },
        "generic_crawler": {
            "items_captured":           items_captured,
            "items_with_full_abstract": items_with_abstract,
            "elapsed_seconds":          round(crawler_elapsed, 1),
            "completed_naturally":      crawler_completed,
            "timed_out":                timed_out,
            "error":                    crawler_error,
        },
        "tier1_status":          tier1_status,
        "promotion_recommended": promotion,
    }

    _write_entry_result(out_dir, result)

    with _print_lock:
        print(
            f"[tier1][{thread_id}] Done entry_id={entry_id} "
            f"status={tier1_status} items={items_captured} "
            f"elapsed={elapsed_total:.1f}s timed_out={timed_out}",
            file=sys.stderr, flush=True,
        )

    return result


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Phase 3.7 Tier 1 batch")
    parser.add_argument("--coverage-csv",    default=str(DEFAULT_COVERAGE_CSV))
    parser.add_argument("--out-dir",         default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--configs-dir",     default=str(DEFAULT_CONFIGS_DIR))
    parser.add_argument("--report-md",       default=str(DEFAULT_REPORT_MD))
    parser.add_argument("--cost-log",        default=str(DEFAULT_COST_LOG))
    parser.add_argument("--per-site-limit",  type=int, default=DEFAULT_PER_SITE_LIMIT)
    parser.add_argument("--per-site-timeout",type=int, default=DEFAULT_PER_SITE_TIMEOUT,
                        help="subprocess wallclock kill (seconds)")
    parser.add_argument("--concurrency",     type=int, default=DEFAULT_CONCURRENCY,
                        help="number of parallel worker threads")
    parser.add_argument("--resume",          action="store_true")
    parser.add_argument("--sanity-test",     type=int, default=0, metavar="N",
                        help="Process only N entries (diverse sheets) for smoke-test")
    args = parser.parse_args()

    coverage_csv  = Path(args.coverage_csv)
    out_dir       = Path(args.out_dir)
    configs_dir   = Path(args.configs_dir)
    report_md     = Path(args.report_md)
    cost_log      = Path(args.cost_log)

    print(f"[tier1] Loading coverage CSV: {coverage_csv}", file=sys.stderr)
    all_entries = _load_entries(coverage_csv)
    print(f"[tier1] Eligible entries: {len(all_entries)}", file=sys.stderr)

    if args.sanity_test > 0:
        entries = _sanity_sample(all_entries, args.sanity_test)
        print(f"[tier1] SANITY MODE: processing {len(entries)} entries", file=sys.stderr)
    else:
        entries = all_entries

    if args.resume:
        before = len(entries)
        entries = [e for e in entries if not _already_done(out_dir, e["entry_id"])]
        print(f"[tier1] Resume: skipping {before - len(entries)} already-done entries", file=sys.stderr)

    print(
        f"[tier1] Starting with concurrency={args.concurrency} "
        f"per_site_timeout={args.per_site_timeout}s "
        f"entries_to_process={len(entries)}",
        file=sys.stderr, flush=True,
    )

    counters     = {"ok": 0, "partial": 0, "analyzer_fail": 0, "crawler_fail": 0, "fail": 0}
    total_cost   = 0.0
    results_list = []
    done_count   = 0
    start_ts     = time.time()

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        futures = {}
        for idx, entry in enumerate(entries):
            if _SHUTDOWN:
                break
            thread_id = f"w{(idx % args.concurrency) + 1}"
            future = executor.submit(
                _process_entry,
                entry, idx, len(entries),
                out_dir, configs_dir, cost_log,
                args.per_site_limit, args.per_site_timeout,
                thread_id,
            )
            futures[future] = entry

        for future in as_completed(futures):
            if _SHUTDOWN:
                print("[tier1] Shutdown requested. Stopping cleanly.", file=sys.stderr)
                for f in futures:
                    f.cancel()
                break

            try:
                result = future.result()
            except Exception as exc:
                entry = futures[future]
                print(
                    f"[tier1] EXCEPTION for entry {entry.get('entry_id')}: {exc}",
                    file=sys.stderr, flush=True,
                )
                continue

            if result is None:
                continue

            with _results_lock:
                results_list.append(result)
                done_count += 1
                current_done = done_count

            analyzer_cost = result.get("analyzer", {}).get("estimated_cost_usd", 0.0)
            with _counters_lock:
                status = result.get("tier1_status", "fail")
                counters[status] = counters.get(status, 0) + 1
                total_cost += analyzer_cost
                current_counters = dict(counters)
                current_cost     = total_cost

            _print_progress(current_done, len(entries), start_ts, current_counters, current_cost)

    # ── Final report ──────────────────────────────────────────────────────────
    _write_summary(report_md, len(all_entries), len(results_list),
                   counters, total_cost, results_list, start_ts)

    print(f"\n[tier1] Done. Processed {len(results_list)} entries.", file=sys.stderr)
    print(f"[tier1] Status: {counters}", file=sys.stderr)
    print(f"[tier1] Total cost: ${total_cost:.4f}", file=sys.stderr)


if __name__ == "__main__":
    main()
