#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 3.8 — Tier-1 analysis for Playwright-recovered sites.

Collects entries with status=='success' from:
  - data/audit/playwright_runs/*.json
  - data/audit/playwright_runs_stealth/*.json   (takes priority on dup entry_id)

For each recovered entry:
  1. Re-fetch the URL via plain requests (fast). If CF-blocked, skip.
  2. Pass prefetched HTML to analyzer.analyze_html() → infer selectors.
  3. Write JSON config to configs_dir/<site_id>.json.
  4. Run GenericCrawler with in-memory SQLite (limit=per_site_limit).
  5. Write per-entry result JSON to out_dir/<entry_id>.json.
  6. Append cost record to api_cost_log.jsonl.

Usage:
    .venv/bin/python scripts/tier1_recovered_html.py --sanity-test 2
    .venv/bin/python scripts/tier1_recovered_html.py            # full run (do NOT launch while tier1_batch.py is running)
"""

import argparse
import glob
import json
import os
import signal
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from crawler import db as dbm
from crawler.analyzer import analyze_html, _generate_site_id
from crawler.generic_crawler import GenericCrawler
from crawler.playwright_fetcher import is_cf_challenge

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_PW_RUNS_DIR     = _ROOT / "data" / "audit" / "playwright_runs"
DEFAULT_PW_STEALTH_DIR  = _ROOT / "data" / "audit" / "playwright_runs_stealth"
DEFAULT_OUT_DIR         = _ROOT / "data" / "audit" / "tier1_recovered_runs"
DEFAULT_CONFIGS_DIR     = _ROOT / "crawler" / "sites" / "configs"
DEFAULT_COST_LOG        = _ROOT / "data" / "audit" / "api_cost_log.jsonl"
DEFAULT_REPORT_MD       = _ROOT / "data" / "audit" / "phase38_summary.md"
DEFAULT_PER_SITE_LIMIT  = 20
DEFAULT_PER_SITE_TIMEOUT = 600
DEFAULT_FETCH_TIMEOUT   = 30

# ── graceful shutdown ──────────────────────────────────────────────────────────
_SHUTDOWN = False

def _handle_sigint(sig, frame):
    global _SHUTDOWN
    print("\n[tier1_recovered] SIGINT — stopping after current entry.", file=sys.stderr)
    _SHUTDOWN = True

signal.signal(signal.SIGINT, _handle_sigint)


# ── helpers ───────────────────────────────────────────────────────────────────

def _load_recovered_entries(pw_runs_dir: Path, pw_stealth_dir: Path) -> list[dict]:
    """Collect status=='success' entries. Stealth takes priority on duplicate entry_id."""
    by_id: dict[str, dict] = {}

    def _load_dir(d: Path, is_stealth: bool):
        for p in sorted(glob.glob(str(d / "*.json"))):
            try:
                with open(p, encoding="utf-8") as f:
                    entry = json.load(f)
            except Exception:
                continue
            if entry.get("status") != "success":
                continue
            eid = entry["entry_id"]
            if is_stealth or eid not in by_id:
                by_id[eid] = entry

    _load_dir(pw_runs_dir, is_stealth=False)
    _load_dir(pw_stealth_dir, is_stealth=True)
    return list(by_id.values())


def _fetch_html_requests(url: str, timeout: int = 30) -> tuple[str | None, str]:
    """Fetch HTML via plain requests with browser-like headers.

    Returns (html, method) where method is 'requests' or 'playwright'.
    Falls back to Playwright fetch when plain requests returns 403/empty.
    """
    import urllib3
    import requests as _requests
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    }
    html = None
    for verify in (True, False):
        try:
            r = _requests.get(url, headers=headers, timeout=timeout, verify=verify)
            r.raise_for_status()
            html = r.text
            break
        except Exception as exc:
            if verify:
                continue  # retry without SSL verify
            print(f"  [fetch] requests failed: {exc}", file=sys.stderr)

    if html and len(html) >= 500 and not is_cf_challenge(html):
        return html, "requests"

    # Fall back to Playwright (these sites were recovered via Playwright originally)
    print(f"  [fetch] requests insufficient (len={len(html) if html else 0}), trying Playwright...",
          file=sys.stderr)
    try:
        from crawler.playwright_fetcher import fetch_html as pw_fetch_html
        pw_html = pw_fetch_html(url, timeout_seconds=timeout, extra_wait_seconds=3.0)
        if pw_html and len(pw_html) >= 500:
            return pw_html, "playwright"
        print(f"  [fetch] playwright also returned insufficient HTML", file=sys.stderr)
    except Exception as exc:
        print(f"  [fetch] playwright failed: {exc}", file=sys.stderr)

    return None, "failed"


def _already_done(out_dir: Path, entry_id: str) -> bool:
    return (out_dir / f"{entry_id}.json").exists()


def _write_config(config_path: Path, config: dict):
    import copy
    clean = copy.deepcopy({k: v for k, v in config.items() if k != "_analyzer_meta"})
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
    with open(cost_log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _run_crawler(config_path: Path, site_id: str, site_name: str, base_url: str,
                 limit: int, timeout_seconds: int):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    dbm.init_db(conn)
    dbm.register_site(conn, site_id, site_name, base_url)

    start = time.time()
    items_captured = 0
    items_with_abstract = 0
    error_str = None
    completed_naturally = False

    try:
        crawler = GenericCrawler(config_path=str(config_path), db_conn=conn, delay=0.5)

        def _timeout_handler(sig, frame):
            raise TimeoutError(f"crawler exceeded {timeout_seconds}s")

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(timeout_seconds)
        try:
            items_captured = crawler.crawl(limit=limit)
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        completed_naturally = True

        rows = conn.execute("SELECT abstract FROM papers").fetchall()
        items_with_abstract = sum(1 for r in rows if r["abstract"] and len(r["abstract"]) >= 100)
        if items_with_abstract == 0:
            rows = conn.execute("SELECT abstract FROM documents").fetchall()
            items_with_abstract = sum(1 for r in rows if r["abstract"] and len(r["abstract"]) >= 100)

    except TimeoutError as exc:
        error_str = str(exc)
    except Exception as exc:
        error_str = f"{type(exc).__name__}: {str(exc)[:200]}"
    finally:
        conn.close()

    elapsed = time.time() - start
    return items_captured, items_with_abstract, elapsed, completed_naturally, error_str


def _classify_status(analyzer_ok, items_captured, items_with_abstract, crawler_error):
    if not analyzer_ok:
        return "analyzer_fail"
    if crawler_error:
        return "crawler_fail"
    if items_captured >= 1 and items_with_abstract >= 1:
        return "ok"
    return "partial"


def _write_entry_result(out_dir: Path, result: dict):
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{result['entry_id']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def _write_summary(report_md: Path, total_input: int, processed: int,
                   counters: dict, total_cost: float, results: list, start_ts: float):
    elapsed = time.time() - start_ts
    report_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Phase 3.8 Tier1-Recovered Summary",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Overview",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Playwright-recovered entries | {total_input} |",
        f"| Processed this run | {processed} |",
        f"| Elapsed | {elapsed/60:.1f} min |",
        f"| Total API cost | ${total_cost:.4f} |",
        "",
        "## Status Distribution",
        "",
        "| Status | Count |",
        "|--------|-------|",
    ]
    for k, v in counters.items():
        lines.append(f"| {k} | {v} |")
    lines += [
        "",
        "## Per-Entry Results",
        "",
        "| entry_id | host | tier1_status | items | cost_usd | model |",
        "|----------|------|--------------|-------|----------|-------|",
    ]
    for r in results:
        a = r.get("analyzer", {})
        gc = r.get("generic_crawler", {})
        lines.append(
            f"| {r['entry_id']} | {r.get('host','')} | {r['tier1_status']} "
            f"| {gc.get('items_captured',0)} | ${a.get('estimated_cost_usd',0):.4f} "
            f"| {a.get('model_used','')} |"
        )
    with open(report_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[tier1_recovered] Report: {report_md}", file=sys.stderr)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Phase 3.8 Tier-1 batch for Playwright-recovered sites")
    parser.add_argument("--pw-runs-dir",      default=str(DEFAULT_PW_RUNS_DIR))
    parser.add_argument("--pw-stealth-dir",   default=str(DEFAULT_PW_STEALTH_DIR))
    parser.add_argument("--out-dir",          default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--configs-dir",      default=str(DEFAULT_CONFIGS_DIR))
    parser.add_argument("--cost-log",         default=str(DEFAULT_COST_LOG))
    parser.add_argument("--report-md",        default=str(DEFAULT_REPORT_MD))
    parser.add_argument("--limit",            type=int, default=DEFAULT_PER_SITE_LIMIT,
                        help="Max items per site for generic_crawler")
    parser.add_argument("--per-site-timeout", type=int, default=DEFAULT_PER_SITE_TIMEOUT)
    parser.add_argument("--fetch-timeout",    type=int, default=DEFAULT_FETCH_TIMEOUT,
                        help="HTTP fetch timeout seconds")
    parser.add_argument("--resume",           action="store_true")
    parser.add_argument("--sanity-test",      type=int, default=0, metavar="N",
                        help="Process only N entries and exit")
    args = parser.parse_args()

    pw_runs_dir    = Path(args.pw_runs_dir)
    pw_stealth_dir = Path(args.pw_stealth_dir)
    out_dir        = Path(args.out_dir)
    configs_dir    = Path(args.configs_dir)
    cost_log       = Path(args.cost_log)
    report_md      = Path(args.report_md)

    print(f"[tier1_recovered] Loading playwright success entries...", file=sys.stderr)
    all_entries = _load_recovered_entries(pw_runs_dir, pw_stealth_dir)
    print(f"[tier1_recovered] Found {len(all_entries)} success entries", file=sys.stderr)

    entries = all_entries
    if args.sanity_test > 0:
        entries = entries[:args.sanity_test]
        print(f"[tier1_recovered] SANITY MODE: {len(entries)} entries", file=sys.stderr)

    if args.resume:
        before = len(entries)
        entries = [e for e in entries if not _already_done(out_dir, e["entry_id"])]
        print(f"[tier1_recovered] Resume: skipping {before - len(entries)} done", file=sys.stderr)

    counters = {"ok": 0, "partial": 0, "analyzer_fail": 0, "crawler_fail": 0,
                "fetch_fail": 0, "cf_blocked": 0}
    total_cost = 0.0
    results_list = []
    start_ts = time.time()

    for idx, entry in enumerate(entries):
        if _SHUTDOWN:
            print("[tier1_recovered] Shutdown. Stopping.", file=sys.stderr)
            break

        entry_id = entry["entry_id"]
        url      = entry["url"]
        host     = entry.get("host", "")
        stealth  = entry.get("stealth", False)

        print(f"\n[tier1_recovered] === {idx+1}/{len(entries)} entry_id={entry_id} url={url[:80]}",
              file=sys.stderr)

        started_at  = datetime.now(timezone.utc).isoformat()
        entry_start = time.time()

        # ── Step 1: fetch HTML (requests → Playwright fallback) ────────────
        html, fetch_method = _fetch_html_requests(url, timeout=args.fetch_timeout)

        if not html:
            print(f"  [fetch] all methods failed, skipping", file=sys.stderr)
            result = {
                "entry_id": entry_id, "url": url, "host": host,
                "started_at": started_at,
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.time() - entry_start, 1),
                "fetch_method": fetch_method, "stealth_recovered": stealth,
                "analyzer": {}, "generic_crawler": {},
                "tier1_status": "fetch_fail", "promotion_recommended": "tier2",
            }
            counters["fetch_fail"] = counters.get("fetch_fail", 0) + 1
            _write_entry_result(out_dir, result)
            results_list.append(result)
            continue

        if is_cf_challenge(html):
            print(f"  [fetch] CF challenge detected after fetch ({fetch_method}), skipping", file=sys.stderr)
            result = {
                "entry_id": entry_id, "url": url, "host": host,
                "started_at": started_at,
                "ended_at": datetime.now(timezone.utc).isoformat(),
                "elapsed_seconds": round(time.time() - entry_start, 1),
                "fetch_method": fetch_method, "stealth_recovered": stealth,
                "analyzer": {}, "generic_crawler": {},
                "tier1_status": "cf_blocked", "promotion_recommended": "tier2",
            }
            counters["cf_blocked"] = counters.get("cf_blocked", 0) + 1
            _write_entry_result(out_dir, result)
            results_list.append(result)
            continue

        # ── Step 2: analyze HTML ────────────────────────────────────────────
        analyzer_ok    = False
        analyzer_error = None
        config         = None
        analyzer_meta  = {}
        site_id        = _generate_site_id(url)

        try:
            config = analyze_html(html, url=url, site_id=site_id, save_config=False)
            if config is None:
                analyzer_error = "analyze_html returned None"
            else:
                analyzer_ok   = True
                analyzer_meta = config.get("_analyzer_meta", {})
        except Exception as exc:
            analyzer_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            traceback.print_exc()

        # cost log
        cost_record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "stage": "analyzer_recovered",
            "model": analyzer_meta.get("model_used", "unknown"),
            "input_tokens": analyzer_meta.get("input_tokens", 0),
            "output_tokens": analyzer_meta.get("output_tokens", 0),
            "cost_usd": analyzer_meta.get("estimated_cost_usd", 0.0),
            "entry_id": entry_id,
            "fallback_used": analyzer_meta.get("fallback_used", False),
        }
        _append_cost_log(cost_log, cost_record)
        total_cost += cost_record["cost_usd"]

        # ── Step 3: write config + crawl ────────────────────────────────────
        items_captured = 0
        items_with_abstract = 0
        crawler_elapsed = 0.0
        crawler_completed = False
        crawler_error = None

        if analyzer_ok and config:
            config_path = configs_dir / f"{site_id}.json"
            _write_config(config_path, config)
            site_name = config.get("site_name", host)
            base_url  = config.get("base_url", f"https://{host}")

            items_captured, items_with_abstract, crawler_elapsed, crawler_completed, crawler_error = \
                _run_crawler(config_path, site_id, site_name, base_url,
                             limit=args.limit,
                             timeout_seconds=args.per_site_timeout)

        # ── Step 4: classify ────────────────────────────────────────────────
        tier1_status = _classify_status(analyzer_ok, items_captured, items_with_abstract, crawler_error)
        promotion    = "tier1" if tier1_status == "ok" else "tier2"
        counters[tier1_status] = counters.get(tier1_status, 0) + 1

        ended_at      = datetime.now(timezone.utc).isoformat()
        elapsed_total = time.time() - entry_start

        result = {
            "entry_id":  entry_id,
            "url":       url,
            "host":      host,
            "site_id":   site_id,
            "started_at": started_at,
            "ended_at":  ended_at,
            "elapsed_seconds": round(elapsed_total, 1),
            "fetch_method": fetch_method,
            "stealth_recovered": stealth,
            "analyzer": {
                "model_used":         analyzer_meta.get("model_used", "unknown"),
                "input_tokens":       analyzer_meta.get("input_tokens", 0),
                "output_tokens":      analyzer_meta.get("output_tokens", 0),
                "estimated_cost_usd": analyzer_meta.get("estimated_cost_usd", 0.0),
                "config_saved":       True if (analyzer_ok and config) else False,
                "error":              analyzer_error,
            },
            "generic_crawler": {
                "items_captured":          items_captured,
                "items_with_full_abstract": items_with_abstract,
                "elapsed_seconds":         round(crawler_elapsed, 1),
                "completed_naturally":     crawler_completed,
                "error":                   crawler_error,
            },
            "tier1_status":          tier1_status,
            "promotion_recommended": promotion,
        }

        _write_entry_result(out_dir, result)
        results_list.append(result)

        elapsed = time.time() - start_ts
        pct = (idx + 1) / len(entries) * 100
        parts = " ".join(f"{k}={v}" for k, v in counters.items() if v)
        print(f"[tier1_recovered] {idx+1}/{len(entries)} ({pct:.1f}%) "
              f"elapsed={elapsed/60:.1f}m {parts} cost=${total_cost:.3f}",
              file=sys.stderr, flush=True)

        # Sanity: exit immediately after processing requested count
        if args.sanity_test > 0 and len(results_list) >= args.sanity_test:
            print(f"[tier1_recovered] Sanity test done ({args.sanity_test} entries). Exiting.",
                  file=sys.stderr)
            break

    _write_summary(report_md, len(all_entries), len(results_list),
                   counters, total_cost, results_list, start_ts)

    print(f"\n[tier1_recovered] Done. Processed={len(results_list)} "
          f"counters={counters} cost=${total_cost:.4f}", file=sys.stderr)


if __name__ == "__main__":
    main()
