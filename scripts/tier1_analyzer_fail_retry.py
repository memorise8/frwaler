#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 3.9 — Retry analyzer on coverage_report entries where analyzer_status == 'fail'.

Phase 2 used gpt-4o-mini for analysis; this retry uses gpt-5.5 (default) which may
recover a portion of the 85 previously-failed entries.

Reuses tier1_batch helpers directly (same flow: analyze_site → write config → crawl).
Only the entry selection differs: filter coverage_report.csv for analyzer_status == 'fail'.

Usage:
    .venv/bin/python scripts/tier1_analyzer_fail_retry.py --sanity-test 2
    .venv/bin/python scripts/tier1_analyzer_fail_retry.py   # full run (after tier1_batch.py finishes)
"""

import argparse
import csv
import json
import os
import signal
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Reuse helpers from tier1_batch directly
from scripts.tier1_batch import (
    _write_config,
    _append_cost_log,
    _run_crawler,
    _classify_status,
    _write_entry_result,
    _already_done,
    _derive_site_id,
    _print_progress,
)
from crawler.analyzer import analyze_site

# ── defaults ──────────────────────────────────────────────────────────────────
DEFAULT_COVERAGE_CSV     = _ROOT / "data" / "audit" / "coverage_report.csv"
DEFAULT_OUT_DIR          = _ROOT / "data" / "audit" / "tier1_fail_retry_runs"
DEFAULT_CONFIGS_DIR      = _ROOT / "crawler" / "sites" / "configs"
DEFAULT_COST_LOG         = _ROOT / "data" / "audit" / "api_cost_log.jsonl"
DEFAULT_REPORT_MD        = _ROOT / "data" / "audit" / "phase39_summary.md"
DEFAULT_PER_SITE_LIMIT   = 20
DEFAULT_PER_SITE_TIMEOUT = 600

# ── graceful shutdown ──────────────────────────────────────────────────────────
_SHUTDOWN = False

def _handle_sigint(sig, frame):
    global _SHUTDOWN
    print("\n[fail_retry] SIGINT — stopping after current entry.", file=sys.stderr)
    _SHUTDOWN = True

signal.signal(signal.SIGINT, _handle_sigint)


# ── entry loading ─────────────────────────────────────────────────────────────

def _load_fail_entries(csv_path: Path) -> list[dict]:
    """Return rows from coverage_report.csv where analyzer_status == 'fail'."""
    entries = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("analyzer_status", "") == "fail":
                url = row.get("url") or row.get("final_url", "")
                if url:
                    entries.append(row)
    return entries


def _write_summary(report_md: Path, total_input: int, processed: int,
                   counters: dict, total_cost: float, results: list, start_ts: float):
    elapsed = time.time() - start_ts
    report_md.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Phase 3.9 Analyzer-Fail Retry Summary",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Overview",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| analyzer_fail entries in coverage_report | {total_input} |",
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

    recovered = counters.get("ok", 0) + counters.get("partial", 0)
    lines += [
        "",
        f"## Recovery: {recovered}/{processed} entries promoted out of analyzer_fail",
        "",
        "## Per-Entry Results",
        "",
        "| entry_id | sheet | host | tier1_status | items | cost_usd | model |",
        "|----------|-------|------|--------------|-------|----------|-------|",
    ]
    for r in results:
        a  = r.get("analyzer", {})
        gc = r.get("generic_crawler", {})
        lines.append(
            f"| {r['entry_id']} | {r.get('sheet','')} | {r.get('host','')} "
            f"| {r['tier1_status']} | {gc.get('items_captured',0)} "
            f"| ${a.get('estimated_cost_usd',0):.4f} | {a.get('model_used','')} |"
        )
    with open(report_md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[fail_retry] Report: {report_md}", file=sys.stderr)


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Phase 3.9 — Retry analyzer on analyzer_fail entries")
    parser.add_argument("--coverage-csv",     default=str(DEFAULT_COVERAGE_CSV))
    parser.add_argument("--out-dir",          default=str(DEFAULT_OUT_DIR))
    parser.add_argument("--configs-dir",      default=str(DEFAULT_CONFIGS_DIR))
    parser.add_argument("--cost-log",         default=str(DEFAULT_COST_LOG))
    parser.add_argument("--report-md",        default=str(DEFAULT_REPORT_MD))
    parser.add_argument("--per-site-limit",   type=int, default=DEFAULT_PER_SITE_LIMIT)
    parser.add_argument("--per-site-timeout", type=int, default=DEFAULT_PER_SITE_TIMEOUT)
    parser.add_argument("--resume",           action="store_true")
    parser.add_argument("--sanity-test",      type=int, default=0, metavar="N",
                        help="Process only N entries (smoke-test)")
    args = parser.parse_args()

    coverage_csv = Path(args.coverage_csv)
    out_dir      = Path(args.out_dir)
    configs_dir  = Path(args.configs_dir)
    cost_log     = Path(args.cost_log)
    report_md    = Path(args.report_md)

    print(f"[fail_retry] Loading analyzer_fail entries from {coverage_csv}", file=sys.stderr)
    all_entries = _load_fail_entries(coverage_csv)
    print(f"[fail_retry] Found {len(all_entries)} analyzer_fail entries", file=sys.stderr)

    entries = all_entries
    if args.sanity_test > 0:
        entries = entries[:args.sanity_test]
        print(f"[fail_retry] SANITY MODE: {len(entries)} entries", file=sys.stderr)

    if args.resume:
        before = len(entries)
        entries = [e for e in entries if not _already_done(out_dir, e["entry_id"])]
        print(f"[fail_retry] Resume: skipping {before - len(entries)} done", file=sys.stderr)

    counters  = {"ok": 0, "partial": 0, "analyzer_fail": 0, "crawler_fail": 0, "fail": 0}
    total_cost = 0.0
    results_list = []
    start_ts = time.time()

    for idx, entry in enumerate(entries):
        if _SHUTDOWN:
            print("[fail_retry] Shutdown. Stopping.", file=sys.stderr)
            break

        entry_id = entry.get("entry_id", f"entry_{idx}")
        url      = entry.get("url") or entry.get("final_url", "")
        host     = entry.get("host", "")
        sheet    = entry.get("sheet", "")

        print(f"\n[fail_retry] === {idx+1}/{len(entries)} entry_id={entry_id} url={url[:80]}",
              file=sys.stderr)

        started_at  = datetime.now(timezone.utc).isoformat()
        entry_start = time.time()

        # ── Step 1: analyzer (gpt-5.5 via ANALYZER_MODEL default) ──────────
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
            "ts": datetime.now(timezone.utc).isoformat(),
            "stage": "analyzer_fail_retry",
            "model": analyzer_meta.get("model_used", "unknown"),
            "input_tokens": analyzer_meta.get("input_tokens", 0),
            "output_tokens": analyzer_meta.get("output_tokens", 0),
            "cost_usd": analyzer_meta.get("estimated_cost_usd", 0.0),
            "entry_id": entry_id,
            "fallback_used": analyzer_meta.get("fallback_used", False),
        }
        _append_cost_log(cost_log, cost_record)
        total_cost += cost_record["cost_usd"]

        # ── Step 2: write config + crawl ────────────────────────────────────
        items_captured      = 0
        items_with_abstract = 0
        crawler_elapsed     = 0.0
        crawler_completed   = False
        crawler_error       = None

        if analyzer_ok and config:
            config_path = configs_dir / f"{site_id}.json"
            _write_config(config_path, config)
            analyzer_meta["config_saved"] = True

            site_name = config.get("site_name", host)
            base_url  = config.get("base_url", f"https://{host}")

            items_captured, items_with_abstract, crawler_elapsed, crawler_completed, crawler_error = \
                _run_crawler(config_path, site_id, site_name, base_url,
                             limit=args.per_site_limit,
                             timeout_seconds=args.per_site_timeout)
        else:
            analyzer_meta["config_saved"] = False

        # ── Step 3: classify ────────────────────────────────────────────────
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

        promotion = "tier1" if tier1_status == "ok" else "tier2"
        counters[tier1_status] = counters.get(tier1_status, 0) + 1

        ended_at      = datetime.now(timezone.utc).isoformat()
        elapsed_total = time.time() - entry_start

        result = {
            "entry_id":  entry_id,
            "url":       url,
            "host":      host,
            "sheet":     sheet,
            "site_id":   site_id if analyzer_ok else _derive_site_id(url),
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
                "items_captured":          items_captured,
                "items_with_full_abstract": items_with_abstract,
                "elapsed_seconds":         round(crawler_elapsed, 1),
                "completed_naturally":     crawler_completed,
                "error":                   crawler_error,
            },
            "tier1_status":          tier1_status,
            "promotion_recommended": promotion,
            "phase": "3.9_fail_retry",
        }

        _write_entry_result(out_dir, result)
        results_list.append(result)

        _print_progress(idx + 1, len(entries), start_ts, counters, total_cost)

        # Sanity: stop after N entries
        if args.sanity_test > 0 and len(results_list) >= args.sanity_test:
            print(f"[fail_retry] Sanity test done ({args.sanity_test} entries). Exiting.",
                  file=sys.stderr)
            break

    _write_summary(report_md, len(all_entries), len(results_list),
                   counters, total_cost, results_list, start_ts)

    print(f"\n[fail_retry] Done. Processed={len(results_list)} "
          f"counters={counters} cost=${total_cost:.4f}", file=sys.stderr)


if __name__ == "__main__":
    main()
