# -*- coding: utf-8 -*-
"""claude_required_runner.py — Process Tier 1 fail entries via Claude Code CLI.

Reads codex_required.csv, runs each entry through run_claude_crawler_build(),
handles usage_limit detection.  Mirrors codex_required_runner.py but uses the
Claude Code CLI (Anthropic OAuth quota) instead of Codex (ChatGPT Plus quota).

Coordination with codex_required_runner
----------------------------------------
Both runners read the same input CSV but write to different output directories:
  codex → data/audit/codex_required_runs/
  claude → data/audit/claude_required_runs/

During resume, claude_required_runner checks BOTH directories and skips any
entry already processed by either runner.  This allows the two processes to
run concurrently and naturally divide the work — whichever runner finishes an
entry first "claims" it and the other skips it on next pass.

Usage
-----
.venv/bin/python scripts/claude_required_runner.py \\
    [--input data/audit/codex_required.csv] \\
    [--out-dir data/audit/claude_required_runs] \\
    [--effort high] \\
    [--max-runtime-seconds 1800] \\
    [--limit-crawl 3] \\
    [--resume] \\
    [--limit N]
"""

from __future__ import annotations

import argparse
import csv
import json
import signal
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Project root
# ---------------------------------------------------------------------------

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

sys.path.insert(0, PROJECT_ROOT)

from crawler.claude_runner import run_claude_crawler_build  # noqa: E402

# ---------------------------------------------------------------------------
# Graceful SIGINT
# ---------------------------------------------------------------------------

_STOP_REQUESTED = False


def _sigint_handler(signum, frame):
    global _STOP_REQUESTED
    print(
        "\n[claude-runner] SIGINT received — finishing current entry then exiting cleanly.",
        file=sys.stderr,
    )
    _STOP_REQUESTED = True


signal.signal(signal.SIGINT, _sigint_handler)

# ---------------------------------------------------------------------------
# Usage-limit markers (Claude Code quota exhaustion messages)
# ---------------------------------------------------------------------------

USAGE_LIMIT_MARKERS = (
    "rate limit",
    "too many requests",
    "weekly usage",
    "you've reached",
    "try again later",
    # codex-style markers also included as fallback
    "hit your usage limit",
    "usage limit",
    "try again at",
)

# Priority order (same as codex_required_runner)
PRIORITY_ORDER = {"partial": 0, "analyzer_fail": 1, "crawler_fail": 2}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify_error(result: dict) -> str:
    if result.get("success"):
        return "none"
    err = (result.get("error") or "").lower()
    if "timeout" in err:
        return "claude_timeout"
    if any(m in err for m in USAGE_LIMIT_MARKERS):
        return "usage_limit"
    if "test_crawl" in err or "quality check" in err:
        return "crawler_quality"
    if "not created" in err:
        return "claude_no_output"
    if "exit code" in err:
        return "claude_exit_error"
    return "unknown"


def write_json(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, ensure_ascii=False, indent=2)


def make_stream_cb(log_dir: Path, entry_id: str):
    """Create a per-entry stream callback (mirrors codex_required_runner)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{entry_id}.log"
    fh = open(log_path, "w", encoding="utf-8", buffering=1)

    def cb(line: str) -> None:
        try:
            fh.write(line + "\n")
        except Exception:
            pass

    cb._fh = fh  # type: ignore[attr-defined]
    return cb, log_path


def print_summary(done: list[dict], total: int) -> None:
    n = len(done)
    if n == 0:
        return
    success = sum(1 for r in done if r["claude_gen_success"])
    elapsed_list = [r["elapsed_seconds"] for r in done]
    avg_elapsed = sum(elapsed_list) / len(elapsed_list) if elapsed_list else 0

    error_dist: dict[str, int] = defaultdict(int)
    for r in done:
        error_dist[r.get("error_class", "unknown")] += 1

    print(
        f"[claude-runner] --- Progress {n}/{total} | success={success} "
        f"avg_elapsed={avg_elapsed:.0f}s errors={dict(error_dist)}",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Main processing loop
# ---------------------------------------------------------------------------

def process_entries(
    entries: list[dict],
    *,
    out_dir: Path,
    codex_out_dir: Path,
    max_runtime_seconds: int,
    effort: Optional[str],
    resume: bool,
) -> list[dict]:
    log_dir = out_dir / "_logs"
    done: list[dict] = []
    total = len(entries)

    # Pre-compute set of already-done entry_ids from BOTH output directories
    # so the two runners naturally divide work without collision.
    def _done_set() -> set[str]:
        codex_done = {f.stem for f in codex_out_dir.glob("*.json")} if codex_out_dir.exists() else set()
        claude_done = {f.stem for f in out_dir.glob("*.json")} if out_dir.exists() else set()
        return codex_done | claude_done

    for idx, entry in enumerate(entries):
        if _STOP_REQUESTED:
            print("[claude-runner] Stop requested — exiting after current entry.", file=sys.stderr)
            break

        entry_id = entry["entry_id"]
        out_path = out_dir / f"{entry_id}.json"

        if resume:
            already_done = _done_set()
            if entry_id in already_done:
                print(
                    f"[claude-runner] {idx+1}/{total} SKIP (resume) entry_id={entry_id}",
                    file=sys.stderr,
                )
                continue

        # Per-entry stream log
        stream_cb, _log_file = make_stream_cb(log_dir, entry_id)

        started_ts = time.time()
        started_iso = _now_iso()

        print(
            f"[claude-runner] {idx+1}/{total} START entry_id={entry_id} "
            f"host={entry.get('host','?')} tier1_status={entry.get('tier1_status','?')} "
            f"effort={effort}",
            file=sys.stderr,
        )

        result = run_claude_crawler_build(
            url=entry["url"],
            site_id=None,
            site_name=None,
            project_root=PROJECT_ROOT,
            max_timeout_seconds=max_runtime_seconds,
            effort=effort,
            stream_cb=stream_cb,
            extra_notes=(entry.get("extra_notes") or "").strip() or None,
        )

        # Close log file
        try:
            stream_cb._fh.close()  # type: ignore[attr-defined]
        except Exception:
            pass

        elapsed = time.time() - started_ts
        ended_iso = _now_iso()

        # items_captured from test_crawl result
        test_crawl_data = result.get("test_crawl") or {}
        quality_data = test_crawl_data.get("quality") or {}
        items_captured: Optional[int] = quality_data.get("items_captured")
        if items_captured is None:
            items_captured = test_crawl_data.get("items_crawled")

        err_class = classify_error(result)

        record = {
            "entry_id": entry_id,
            "url": entry["url"],
            "sheet": entry.get("sheet", ""),
            "host": entry.get("host", ""),
            "tier1_status": entry.get("tier1_status", ""),
            "effort": effort,
            "started_at": started_iso,
            "ended_at": ended_iso,
            "elapsed_seconds": round(elapsed, 1),
            "claude_gen_success": result["success"],
            "site_id_generated": result.get("site_id"),
            "file_path": result.get("file_path"),
            "items_captured": items_captured,
            "error_class": err_class,
            "error_message": result.get("error"),
            "claude_log_path": result.get("claude_log_path"),
        }

        # Detect usage_limit — log and stop further processing
        err_text = (result.get("error") or "").lower()
        if (not result["success"]) and any(m in err_text for m in USAGE_LIMIT_MARKERS):
            print(
                f"[claude-runner] Claude quota limit reached on entry {entry_id}. "
                "Stopping — re-run with --resume after quota reset.",
                file=sys.stderr,
            )
            # Roll back JSON so --resume retries this entry
            try:
                out_path.unlink(missing_ok=True)
            except OSError:
                pass
            break

        # Save result
        write_json(out_path, record)
        done.append(record)

        status_char = "OK" if result["success"] else "FAIL"
        print(
            f"[claude-runner] {idx+1}/{total} {status_char} entry_id={entry_id} "
            f"host={entry.get('host','?')} success={result['success']} "
            f"items={items_captured} elapsed={round(elapsed,1)}s",
            file=sys.stderr,
        )

        if len(done) % 5 == 0:
            print_summary(done, total)

    return done


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="claude_required_runner",
        description="Process Tier 1 fail entries via Claude Code CLI (Tier 2 processing).",
    )
    p.add_argument(
        "--input",
        default="data/audit/codex_required.csv",
        metavar="PATH",
        help="Input CSV from list_codex_required.py",
    )
    p.add_argument(
        "--out-dir",
        default="data/audit/claude_required_runs",
        metavar="DIR",
        help="Output directory for per-entry JSON results",
    )
    p.add_argument(
        "--codex-out-dir",
        default="data/audit/codex_required_runs",
        metavar="DIR",
        help="Codex runner output dir — checked during resume to avoid re-processing",
    )
    p.add_argument(
        "--effort",
        default="high",
        metavar="LEVEL",
        help="Claude effort level: low/medium/high/xhigh/max (default: high)",
    )
    p.add_argument(
        "--max-runtime-seconds",
        type=int,
        default=1800,
        metavar="SECS",
        help="Wall-clock timeout per claude run (default: 1800 = 30 min)",
    )
    p.add_argument(
        "--limit-crawl",
        type=int,
        default=3,
        metavar="N",
        help="limit= passed to test_crawl (default: 3; currently informational)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Skip entries already processed by either claude or codex runner",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Process only the first N entries (sanity test mode)",
    )
    p.add_argument(
        "--shard",
        default=None,
        metavar="N/M",
        help="Process only shard N of M (e.g. 0/2 for first half, 1/2 for second; by index modulo M).",
    )
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    root = Path(PROJECT_ROOT)

    input_csv = Path(args.input)
    if not input_csv.is_absolute():
        input_csv = root / input_csv

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = root / out_dir

    codex_out_dir = Path(args.codex_out_dir)
    if not codex_out_dir.is_absolute():
        codex_out_dir = root / codex_out_dir

    out_dir.mkdir(parents=True, exist_ok=True)

    # Load entries
    if not input_csv.exists():
        print(f"[claude-runner] ERROR input not found: {input_csv}", file=sys.stderr)
        print("[claude-runner] Run list_codex_required.py first.", file=sys.stderr)
        return 1

    with open(input_csv, newline="", encoding="utf-8") as fh:
        entries = list(csv.DictReader(fh))

    # Sort by priority (same ordering as codex_required_runner)
    entries.sort(key=lambda e: (int(e.get("priority", 9)), e.get("entry_id", "")))

    total_available = len(entries)

    if args.shard is not None:
        try:
            n_str, m_str = args.shard.split("/")
            shard_n, shard_m = int(n_str), int(m_str)
            if shard_m <= 0 or not (0 <= shard_n < shard_m):
                raise ValueError
        except (ValueError, IndexError):
            print(f"[claude-runner] ERROR invalid --shard {args.shard!r} (expected N/M with 0<=N<M)", file=sys.stderr)
            return 1
        entries = [e for i, e in enumerate(entries) if i % shard_m == shard_n]
        print(
            f"[claude-runner] --shard {shard_n}/{shard_m}: processing {len(entries)} of "
            f"{total_available} entries (by index modulo {shard_m})",
            file=sys.stderr,
        )

    if args.limit is not None:
        entries = entries[: args.limit]
        print(
            f"[claude-runner] --limit {args.limit}: processing first {len(entries)} of "
            f"{total_available} entries",
            file=sys.stderr,
        )
    else:
        print(
            f"[claude-runner] Processing {len(entries)} entries from {input_csv}",
            file=sys.stderr,
        )

    print(f"[claude-runner] Out dir: {out_dir}", file=sys.stderr)
    print(f"[claude-runner] Effort: {args.effort}", file=sys.stderr)
    print(f"[claude-runner] Codex out dir (cross-skip): {codex_out_dir}", file=sys.stderr)

    if not entries:
        print("[claude-runner] No entries to process.", file=sys.stderr)
        return 0

    done = process_entries(
        entries,
        out_dir=out_dir,
        codex_out_dir=codex_out_dir,
        max_runtime_seconds=args.max_runtime_seconds,
        effort=args.effort if args.effort else None,
        resume=args.resume,
    )

    print_summary(done, len(entries))

    if args.limit is not None:
        print(
            f"\n[claude-runner] Sanity test complete: {len(done)}/{args.limit} processed.",
            file=sys.stderr,
        )
        success_count = sum(1 for r in done if r["claude_gen_success"])
        fail_count = len(done) - success_count
        print(f"  success={success_count} fail={fail_count}", file=sys.stderr)
        for r in done:
            print(
                f"  entry_id={r['entry_id']} host={r['host']} "
                f"tier1_status={r['tier1_status']} "
                f"effort={r['effort']} "
                f"success={r['claude_gen_success']} "
                f"elapsed={r['elapsed_seconds']}s "
                f"error_class={r['error_class']}",
                file=sys.stderr,
            )

        # Verify JSON outputs exist
        missing = []
        for r in done:
            p = out_dir / f"{r['entry_id']}.json"
            if not p.exists():
                missing.append(r["entry_id"])
        if missing:
            print(f"[claude-runner] ERROR missing JSON outputs: {missing}", file=sys.stderr)
            return 1
        print(
            f"[claude-runner] All {len(done)} JSON outputs verified in {out_dir}",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
