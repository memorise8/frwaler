# -*- coding: utf-8 -*-
"""sample_crawl.py — Phase 3: sample-based crawl using codex_runner.

Usage
-----
.venv/bin/python scripts/sample_crawl.py \
    [--coverage-csv data/audit/coverage_report.csv] \
    [--sample-dir data/audit/sample_runs] \
    [--per-sheet 3] [--seed 42] \
    [--codex-homes ~/.codex-a ~/.codex-b] \
    [--limit-crawl 3] \
    [--max-runtime-seconds 1200] \
    [--resume] \
    [--sanity-test N]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Project root — two levels up from scripts/
# ---------------------------------------------------------------------------

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

# ---------------------------------------------------------------------------
# Imports from project
# ---------------------------------------------------------------------------

sys.path.insert(0, PROJECT_ROOT)

from scripts.codex_quota import (  # noqa: E402
    QuotaExhausted,
    QuotaSnapshot,
    pick_account,
    read_latest_quota,
    wait_until_reset,
)
from crawler.codex_runner import run_codex_crawler_build  # noqa: E402


# ---------------------------------------------------------------------------
# Globals for graceful SIGINT
# ---------------------------------------------------------------------------

_STOP_REQUESTED = False


def _sigint_handler(signum, frame):
    global _STOP_REQUESTED
    print("\n[crawl] SIGINT received — finishing current sample then exiting cleanly.",
          file=sys.stderr)
    _STOP_REQUESTED = True


signal.signal(signal.SIGINT, _sigint_handler)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _stderr_warn(snap: QuotaSnapshot) -> None:
    pct = snap.primary_used_percent
    home_name = snap.home.name
    print(f"[crawl] WARN account {snap.account_email} ({home_name}) at {pct:.1f}% primary",
          file=sys.stderr)


def classify_error(result: dict) -> str:
    if result.get("success"):
        return "none"
    err = (result.get("error") or "").lower()
    if "timeout" in err:
        return "codex_timeout"
    if "test_crawl" in err or "quality check" in err:
        return "crawler_quality"
    if "not created" in err:
        return "codex_no_output"
    if "exit code" in err:
        return "codex_exit_error"
    return "unknown"


def write_json(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)


def read_coverage_csv(csv_path: Path) -> list[dict]:
    with open(csv_path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def update_csv_phase3_columns(csv_path: Path, entry_id: str, record: dict) -> None:
    """Add/update phase3 columns for the given entry_id in the CSV (in-place)."""
    rows = []
    fieldnames = None

    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    phase3_cols = ["phase3_sampled", "phase3_codex_success",
                   "phase3_items_captured", "phase3_error_class"]
    for col in phase3_cols:
        if col not in fieldnames:
            fieldnames.append(col)

    for row in rows:
        if row["entry_id"] == entry_id:
            row["phase3_sampled"] = "true"
            row["phase3_codex_success"] = "true" if record["codex_gen_success"] else "false"
            items = record.get("items_captured")
            row["phase3_items_captured"] = str(items) if items is not None else ""
            row["phase3_error_class"] = record.get("error_class", "")

    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def select_samples(rows: list[dict], per_sheet: int, seed: int) -> list[dict]:
    """Deterministic per-sheet sampling."""
    rng = random.Random(seed)
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["analyzer_status"] in ("ok", "partial"):
            groups[row["sheet"]].append(row)

    samples: list[dict] = []
    for sheet, sheet_rows in sorted(groups.items()):
        pool = sorted(sheet_rows, key=lambda r: r["entry_id"])
        chosen = rng.sample(pool, min(per_sheet, len(pool)))
        samples.extend(chosen)
    return samples


# ---------------------------------------------------------------------------
# Per-sample stream logger
# ---------------------------------------------------------------------------

def make_stream_cb(log_dir: Path, entry_id: str):
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{entry_id}.log"
    fh = open(log_path, "w", encoding="utf-8", buffering=1)

    def cb(line: str) -> None:
        try:
            fh.write(line + "\n")
        except Exception:
            pass

    # attach handle for cleanup
    cb._fh = fh  # type: ignore[attr-defined]
    return cb, log_path


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------

def print_summary(done: list[dict], total: int) -> None:
    n = len(done)
    if n == 0:
        return
    success = sum(1 for r in done if r["codex_gen_success"])
    items_list = [r["items_captured"] for r in done if r["items_captured"] is not None]
    avg_items = sum(items_list) / len(items_list) if items_list else 0
    elapsed_list = [r["elapsed_seconds"] for r in done]
    avg_elapsed = sum(elapsed_list) / len(elapsed_list) if elapsed_list else 0

    error_dist: dict[str, int] = defaultdict(int)
    for r in done:
        error_dist[r.get("error_class", "unknown")] += 1

    print(
        f"[crawl] --- Progress {n}/{total} | success={success} "
        f"avg_items={avg_items:.1f} avg_elapsed={avg_elapsed:.0f}s "
        f"errors={dict(error_dist)}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------

def process_samples(
    samples: list[dict],
    *,
    sample_dir: Path,
    coverage_csv: Path,
    codex_homes: list[Path],
    max_runtime_seconds: int,
    resume: bool,
    model: Optional[str] = None,
) -> list[dict]:
    log_dir = sample_dir / "_logs"
    done: list[dict] = []
    total = len(samples)

    # In-process tracking of homes that returned "usage limit" — codex's
    # logs_2.sqlite isn't updated when usage_limit fires (the turn never
    # completes), so we cannot rely on read_latest_quota to flip the home
    # into 95%+ territory. Track exhaustion at the script level.
    live_homes = list(codex_homes)
    USAGE_LIMIT_MARKERS = (
        "hit your usage limit",
        "usage limit",
        "try again at",
    )

    for idx, sample in enumerate(samples):
        if _STOP_REQUESTED:
            print("[crawl] Stop requested — exiting after current sample.", file=sys.stderr)
            break

        if not live_homes:
            print(
                "[crawl] All codex accounts exhausted (usage_limit on each). "
                "Stopping early — re-run with --resume after quota reset.",
                file=sys.stderr,
            )
            break

        entry_id = sample["entry_id"]
        out_path = sample_dir / f"{entry_id}.json"

        # Resume: skip if output already exists
        if resume and out_path.exists():
            print(f"[crawl] {idx+1}/{total} SKIP (resume) entry_id={entry_id}", file=sys.stderr)
            continue

        # Pick codex account (auto-sleep if all exhausted)
        while True:
            try:
                home, snap = pick_account(
                    live_homes,
                    max_percent=95,
                    warn_percent=90,
                    on_warn=_stderr_warn,
                )
                break
            except QuotaExhausted as exc:
                print(
                    f"[crawl] all accounts exhausted; sleeping {exc.min_reset_in_seconds}s",
                    file=sys.stderr,
                )
                wait_until_reset(live_homes)

        # Build stream callback (per-sample log)
        stream_cb, _log_file = make_stream_cb(log_dir, entry_id)

        started_ts = time.time()
        started_iso = _now_iso()

        result = run_codex_crawler_build(
            url=sample["url"],
            site_id=None,
            site_name=None,
            project_root=PROJECT_ROOT,
            max_timeout_seconds=max_runtime_seconds,
            model=model,
            codex_home=str(home),
            stream_cb=stream_cb,
        )

        # Close log file handle
        try:
            stream_cb._fh.close()  # type: ignore[attr-defined]
        except Exception:
            pass

        elapsed = time.time() - started_ts
        ended_iso = _now_iso()

        # Extract items_captured from test_crawl result
        test_crawl_data = result.get("test_crawl") or {}
        quality_data = test_crawl_data.get("quality") or {}
        items_captured: Optional[int] = quality_data.get("items_captured")
        if items_captured is None:
            # fallback: items_crawled field
            items_captured = test_crawl_data.get("items_crawled")

        # Quota snapshot after run
        try:
            post_snap = read_latest_quota(home)
            quota_after = {
                "primary_used_percent": post_snap.primary_used_percent,
                "secondary_used_percent": post_snap.secondary_used_percent,
                "limit_reached": post_snap.limit_reached,
                "measured_at": post_snap.measured_at,
            }
        except Exception:
            quota_after = {}

        record = {
            "entry_id": entry_id,
            "url": sample["url"],
            "sheet": sample["sheet"],
            "host": sample["host"],
            "codex_home_used": str(home),
            "account_email": snap.account_email,
            "started_at": started_iso,
            "ended_at": ended_iso,
            "elapsed_seconds": round(elapsed, 1),
            "codex_gen_success": result["success"],
            "site_id_generated": result.get("site_id"),
            "file_path": result.get("file_path"),
            "test_crawl_quality": quality_data.get("quality") if quality_data else None,
            "items_captured": items_captured,
            "pdf_download_success": 0,
            "summary_success": 0,
            "error_class": classify_error(result),
            "error_message": result.get("error"),
            "codex_log_path": result.get("codex_log_path"),
            "quota_after": quota_after,
        }

        # Immediately save to disk
        write_json(out_path, record)

        # Update coverage CSV in-place
        try:
            update_csv_phase3_columns(coverage_csv, entry_id, record)
        except Exception as exc:
            print(f"[crawl] WARN CSV update failed for {entry_id}: {exc}", file=sys.stderr)

        # Detect ChatGPT usage_limit response (turn doesn't write rate_limits
        # to logs_2.sqlite, so quota reader can't see exhaustion). Drop the
        # offending home from live_homes for the rest of this run.
        err_text = (result.get("error") or "").lower()
        if (not result["success"]) and any(m in err_text for m in USAGE_LIMIT_MARKERS):
            if home in live_homes:
                live_homes.remove(home)
                print(
                    f"[crawl] {home.name} marked exhausted (usage_limit detected). "
                    f"Remaining live homes: {[h.name for h in live_homes]}",
                    file=sys.stderr,
                )
            # Also retry-roll-back: delete the JSON we just wrote so --resume
            # picks this entry up again next session.
            try:
                out_path.unlink()
                print(f"[crawl] Rolled back {entry_id}.json (will retry next session).",
                      file=sys.stderr)
            except OSError:
                pass
            # Don't append to done; skip to next sample
            continue

        done.append(record)

        # Progress line
        sheet_short = sample["sheet"][:20]
        host_short = sample["host"][:25]
        print(
            f"[crawl] {idx+1}/{total} sheet={sheet_short:<20} host={host_short:<25} "
            f"home={home.name} success={record['codex_gen_success']} "
            f"items={record['items_captured']} elapsed={record['elapsed_seconds']:.0f}s",
            file=sys.stderr,
        )

        # Summary every 5 items
        if len(done) % 5 == 0:
            print_summary(done, total)

    return done


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sample_crawl",
        description="Phase 3 sample crawl using codex_runner.",
    )
    p.add_argument(
        "--coverage-csv",
        default="data/audit/coverage_report.csv",
        metavar="PATH",
        help="Path to coverage_report.csv",
    )
    p.add_argument(
        "--sample-dir",
        default="data/audit/sample_runs",
        metavar="DIR",
        help="Output directory for per-sample JSON and logs",
    )
    p.add_argument(
        "--per-sheet",
        type=int,
        default=3,
        metavar="N",
        help="Max samples per sheet (default: 3)",
    )
    p.add_argument(
        "--seed",
        type=int,
        default=42,
        metavar="N",
        help="Random seed for reproducible sampling (default: 42)",
    )
    p.add_argument(
        "--codex-homes",
        nargs="+",
        default=["~/.codex-a", "~/.codex-b"],
        metavar="DIR",
        help="CODEX_HOME directories for account rotation",
    )
    p.add_argument(
        "--limit-crawl",
        type=int,
        default=3,
        metavar="N",
        help="limit= passed to test_crawl inside run_codex_crawler_build (default: 3)",
    )
    p.add_argument(
        "--max-runtime-seconds",
        type=int,
        default=1200,
        metavar="SECS",
        help="Wall-clock timeout per codex exec (default: 1200)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip entries that already have a JSON in sample-dir",
    )
    p.add_argument(
        "--sanity-test",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N samples and exit (for sanity testing)",
    )
    p.add_argument(
        "--model",
        default=None,
        metavar="NAME",
        help="Model passed to `codex exec -m <model>` (default: codex CLI default)",
    )
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Resolve paths relative to project root if not absolute
    coverage_csv = Path(args.coverage_csv)
    if not coverage_csv.is_absolute():
        coverage_csv = Path(PROJECT_ROOT) / coverage_csv
    sample_dir = Path(args.sample_dir)
    if not sample_dir.is_absolute():
        sample_dir = Path(PROJECT_ROOT) / sample_dir
    codex_homes = [Path(h).expanduser().resolve() for h in args.codex_homes]

    sample_dir.mkdir(parents=True, exist_ok=True)

    # Load coverage CSV
    print(f"[crawl] Loading coverage CSV: {coverage_csv}", file=sys.stderr)
    all_rows = read_coverage_csv(coverage_csv)
    ok_rows = [r for r in all_rows if r["analyzer_status"] in ("ok", "partial")]
    print(f"[crawl] ok/partial rows: {len(ok_rows)}", file=sys.stderr)

    # Select samples (deterministic)
    samples = select_samples(ok_rows, per_sheet=args.per_sheet, seed=args.seed)
    print(f"[crawl] Sampled {len(samples)} entries across sheets", file=sys.stderr)

    # Sanity-test mode: only first N
    if args.sanity_test is not None:
        samples = samples[: args.sanity_test]
        print(f"[crawl] --sanity-test {args.sanity_test}: processing first {len(samples)} only",
              file=sys.stderr)

    if not samples:
        print("[crawl] No samples to process.", file=sys.stderr)
        return 0

    # Codex account availability check
    print(f"[crawl] Codex homes: {[str(h) for h in codex_homes]}", file=sys.stderr)
    for home in codex_homes:
        if not home.exists():
            print(f"[crawl] WARN codex home not found: {home}", file=sys.stderr)

    # Run
    done = process_samples(
        samples,
        sample_dir=sample_dir,
        coverage_csv=coverage_csv,
        codex_homes=codex_homes,
        max_runtime_seconds=args.max_runtime_seconds,
        resume=args.resume,
        model=args.model,
    )

    # Final summary
    print_summary(done, len(samples))

    if args.sanity_test is not None:
        print(f"\n[crawl] Sanity test complete: {len(done)}/{args.sanity_test} processed.",
              file=sys.stderr)
        # Verify output files
        missing = []
        for record in done:
            p = sample_dir / f"{record['entry_id']}.json"
            if not p.exists():
                missing.append(record["entry_id"])
        if missing:
            print(f"[crawl] ERROR missing JSON outputs: {missing}", file=sys.stderr)
            return 1
        print(f"[crawl] All {len(done)} JSON outputs verified in {sample_dir}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
