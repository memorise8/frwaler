#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Phase 3.5 — Full crawl validation script.

Runs each Phase-3 codex-generated crawler with a large limit to verify
it can collect real site-wide data (not just 3 sample items).

Usage:
    .venv/bin/python scripts/full_crawl_validate.py \
        [--custom-dir crawler/sites/custom] \
        [--out-dir data/audit/full_crawl_runs] \
        [--report-md data/audit/phase35_summary.md] \
        [--limit 500] \
        [--per-crawler-timeout 1800] \
        [--resume]
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Files that are NOT Phase-3 crawlers (pre-existing or non-crawler files)
# ---------------------------------------------------------------------------
EXCLUDED_FILES = {
    "better-fsc-go-kr-fsc_new.py",
    "fsc_better_testcodex.py",
    "__init__.py",
}

# ---------------------------------------------------------------------------
# Status classification thresholds
# ---------------------------------------------------------------------------
FULL_COMPLETE_MIN = 50
PARTIAL_COMPLETE_MIN = 10


# ---------------------------------------------------------------------------
# Inline runner script (executed in a subprocess per crawler)
# ---------------------------------------------------------------------------
RUNNER_SCRIPT = textwrap.dedent(r"""
import importlib.util, sqlite3, sys, time, json, traceback

sys.path.insert(0, ".")

from crawler import db as dbm
from crawler.base_crawler import BaseCrawler

site_id  = sys.argv[1]
py_path  = sys.argv[2]
limit_   = None if sys.argv[3] == "None" else int(sys.argv[3])

spec = importlib.util.spec_from_file_location(site_id, py_path)
m    = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

cls = next(
    (v for k, v in vars(m).items()
     if isinstance(v, type) and issubclass(v, BaseCrawler) and v is not BaseCrawler),
    None,
)
if cls is None:
    print("__RESULT__:" + json.dumps({"error": "no_crawler_class"}), flush=True)
    sys.exit(1)

conn = sqlite3.connect(":memory:")
conn.row_factory = sqlite3.Row
dbm.init_db(conn)

c = cls(db_conn=conn)
dbm.register_site(conn, c.site_id, c.site_name, c.base_url)

started = time.time()
try:
    n = c.crawl(limit=limit_)
    elapsed = time.time() - started

    try:
        rows = conn.execute("SELECT title, LENGTH(abstract) l FROM documents").fetchall()
    except Exception:
        rows = conn.execute("SELECT title, LENGTH(abstract) l FROM papers").fetchall()

    count = len(rows)
    sample_titles = [r["title"][:80] for r in rows[:5]]
    avg_abs = round(sum(r["l"] for r in rows) / count, 0) if count else 0

    print("__RESULT__:" + json.dumps({
        "items_saved": int(n) if n is not None else count,
        "actual_db_rows": count,
        "elapsed": round(elapsed, 1),
        "completed_naturally": True,
        "avg_abstract_chars": avg_abs,
        "sample_titles": sample_titles,
        "error": None,
        "stopped_by": None,
    }), flush=True)

except KeyboardInterrupt:
    elapsed = time.time() - started
    try:
        rows = conn.execute("SELECT title FROM documents").fetchall()
    except Exception:
        try:
            rows = conn.execute("SELECT title FROM papers").fetchall()
        except Exception:
            rows = []
    print("__RESULT__:" + json.dumps({
        "items_saved": len(rows),
        "actual_db_rows": len(rows),
        "elapsed": round(elapsed, 1),
        "completed_naturally": False,
        "avg_abstract_chars": 0,
        "sample_titles": [],
        "error": None,
        "stopped_by": "interrupt",
    }), flush=True)

except Exception as e:
    elapsed = time.time() - started
    tb = traceback.format_exc()
    try:
        rows = conn.execute("SELECT title FROM documents").fetchall()
    except Exception:
        try:
            rows = conn.execute("SELECT title FROM papers").fetchall()
        except Exception:
            rows = []
    print("__RESULT__:" + json.dumps({
        "items_saved": len(rows),
        "actual_db_rows": len(rows),
        "elapsed": round(elapsed, 1),
        "completed_naturally": False,
        "avg_abstract_chars": 0,
        "sample_titles": [],
        "error": (str(e) + "\n" + tb)[:500],
        "stopped_by": "exception",
    }), flush=True)
    sys.exit(1)
""")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def classify_status(result: dict) -> str:
    if result.get("error") == "no_crawler_class":
        return "no_class"
    if result.get("stopped_by") == "exception" or (
        result.get("error") and not result.get("completed_naturally")
    ):
        return "error"
    if not result.get("completed_naturally"):
        return "timeout"
    items = result.get("items_saved", 0) or 0
    if items >= FULL_COMPLETE_MIN:
        return "full_complete"
    if items >= PARTIAL_COMPLETE_MIN:
        return "partial_complete"
    return "tiny_complete"


def collect_crawlers(custom_dir: Path) -> list[Path]:
    """Return sorted list of Phase-3 crawler .py files (excluding pre-existing ones)."""
    files = sorted(
        p for p in custom_dir.glob("*.py")
        if p.name not in EXCLUDED_FILES and not p.name.startswith("_")
    )
    return files


def run_crawler(
    py_path: Path,
    site_id: str,
    limit: int | None,
    timeout_sec: int,
    log_path: Path,
    project_root: Path,
) -> dict:
    """Run one crawler in a subprocess. Return parsed result dict."""

    # Write the runner script to a temp file so we don't hit shell arg length limits
    runner_path = project_root / ".omc" / "_phase35_runner.py"
    runner_path.parent.mkdir(parents=True, exist_ok=True)
    runner_path.write_text(RUNNER_SCRIPT, encoding="utf-8")

    python_exe = str(project_root / ".venv" / "bin" / "python")
    if not Path(python_exe).exists():
        python_exe = sys.executable

    cmd = [
        python_exe,
        str(runner_path),
        site_id,
        str(py_path),
        str(limit) if limit is not None else "None",
    ]

    log_path.parent.mkdir(parents=True, exist_ok=True)

    raw_result_line: str | None = None
    process = None

    with open(log_path, "w", encoding="utf-8") as log_fh:
        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(project_root),
                text=True,
                encoding="utf-8",
                errors="replace",
            )

            deadline = time.time() + timeout_sec
            timed_out = False

            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    timed_out = True
                    break

                try:
                    line = process.stdout.readline()
                except Exception:
                    break

                if line == "" and process.poll() is not None:
                    break

                if line:
                    log_fh.write(line)
                    log_fh.flush()
                    if line.startswith("__RESULT__:"):
                        raw_result_line = line[len("__RESULT__:"):].strip()

            if timed_out and process.poll() is None:
                # Drain remaining stdout briefly then kill
                process.send_signal(signal.SIGTERM)
                time.sleep(3)
                if process.poll() is None:
                    process.kill()
                # Drain remaining
                try:
                    remaining_out, _ = process.communicate(timeout=10)
                    if remaining_out:
                        log_fh.write(remaining_out)
                        for line in remaining_out.splitlines():
                            if line.startswith("__RESULT__:"):
                                raw_result_line = line[len("__RESULT__:"):].strip()
                except Exception:
                    pass
            else:
                # Process ended; drain remaining output
                try:
                    remaining_out, _ = process.communicate(timeout=30)
                    if remaining_out:
                        log_fh.write(remaining_out)
                        for line in remaining_out.splitlines():
                            if line.startswith("__RESULT__:"):
                                raw_result_line = line[len("__RESULT__:"):].strip()
                except Exception:
                    pass

        except KeyboardInterrupt:
            if process and process.poll() is None:
                process.send_signal(signal.SIGTERM)
                time.sleep(2)
                if process.poll() is None:
                    process.kill()
            raise

    if raw_result_line:
        try:
            parsed = json.loads(raw_result_line)
            if timed_out:
                parsed["completed_naturally"] = False
                parsed["stopped_by"] = "timeout"
            return parsed
        except json.JSONDecodeError:
            pass

    # Fallback: no __RESULT__ line found
    return {
        "items_saved": 0,
        "actual_db_rows": 0,
        "elapsed": timeout_sec if timed_out else 0,
        "completed_naturally": False,
        "avg_abstract_chars": 0,
        "sample_titles": [],
        "error": "no __RESULT__ line in subprocess output",
        "stopped_by": "timeout" if timed_out else "no_output",
    }


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------

def write_report(
    report_path: Path,
    results: list[dict],
    limit: int | None,
    timeout_sec: int,
    total_elapsed: float,
) -> None:
    status_order = ["full_complete", "partial_complete", "tiny_complete", "timeout", "error", "no_class"]
    counts: dict[str, int] = {s: 0 for s in status_order}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    total = len(results)

    lines: list[str] = [
        "# Phase 3.5 — Full Crawl Validation Summary",
        "",
        f"생성일: {now_iso()}",
        "",
        "## 실행",
        f"- 검증 대상 {total}개 크롤러 (Phase 3 에서 codex 가 생성)",
        f"- limit={limit}, 각 크롤러 {timeout_sec}초({timeout_sec//60}분) timeout",
        f"- 총 소요 시간: {total_elapsed/60:.1f}분",
        "",
        "## status 분포",
        "",
        "| status | 건수 | 비율 |",
        "|---|---|---|",
    ]
    for s in status_order:
        c = counts.get(s, 0)
        pct = f"{c/total*100:.0f}%" if total else "0%"
        lines.append(f"| {s} | {c} | {pct} |")

    lines += [
        "",
        "## 사이트별 결과",
        "",
        "| site_id | items_saved | elapsed | status | sample_title |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        st = r.get("sample_titles") or []
        sample = (st[0][:60] if st else "-").replace("|", "｜")
        elapsed_s = f"{r.get('elapsed_seconds', 0):.0f}s"
        lines.append(
            f"| {r['site_id']} | {r.get('items_saved', 0)} | {elapsed_s} | {r['status']} | {sample} |"
        )

    lines += [
        "",
        "## 권장 조치",
        "",
        "- **full_complete**: 운영 DB 에 포함. cron 스케줄 등록 권장.",
        "- **partial_complete**: 사이트 자체 규모가 작거나 페이지네이션 일부만 동작. 수동 확인 후 포함 결정.",
        "- **tiny_complete**: 데이터 극소. 사이트 상태 재확인 필요.",
        "- **timeout**: 크롤러 로직 최적화 또는 rate-limit 완화 필요. 로그 확인.",
        "- **error**: 예외 스택 트레이스 로그 확인. 수정 후 재실행.",
        "- **no_class**: BaseCrawler 서브클래스 없음. 파일 구조 확인.",
        "",
        "## 결론 — Phase 3 성공 크롤러의 진짜 의미",
        "",
    ]

    full = counts.get("full_complete", 0)
    partial = counts.get("partial_complete", 0)
    tiny = counts.get("tiny_complete", 0)
    timeout = counts.get("timeout", 0)
    error = counts.get("error", 0)

    lines += [
        f"- 검증 대상 {total}건 중 limit={limit} 도 통과(full+partial): {full + partial}건",
        f"- 진짜 풀 크롤 가능 사이트 (≥{FULL_COMPLETE_MIN}건): {full}건",
        f"- 소규모 사이트 (10~{FULL_COMPLETE_MIN-1}건): {partial}건",
        f"- 데이터 극소 (<10건): {tiny}건",
        f"- 페이지네이션 미완 / timeout: {timeout}건",
        f"- 오류 발생: {error}건",
    ]

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3.5 full crawl validation")
    parser.add_argument("--custom-dir",         default="crawler/sites/custom")
    parser.add_argument("--out-dir",            default="data/audit/full_crawl_runs")
    parser.add_argument("--report-md",          default="data/audit/phase35_summary.md")
    parser.add_argument("--limit",              type=int, default=500,
                        help="Max items per crawler (0 = unlimited)")
    parser.add_argument("--per-crawler-timeout", type=int, default=1800,
                        help="Wall-clock timeout per crawler in seconds (default 1800 = 30min)")
    parser.add_argument("--resume",             action="store_true",
                        help="Skip crawlers whose output JSON already exists")
    args = parser.parse_args()

    limit: int | None = args.limit if args.limit > 0 else None
    timeout_sec: int  = args.per_crawler_timeout

    project_root = Path(__file__).resolve().parent.parent
    custom_dir   = project_root / args.custom_dir
    out_dir      = project_root / args.out_dir
    logs_dir     = out_dir / "_logs"
    report_path  = project_root / args.report_md

    out_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    crawlers = collect_crawlers(custom_dir)
    if not crawlers:
        print("[fullcrawl] No crawlers found in", custom_dir, file=sys.stderr)
        sys.exit(1)

    total = len(crawlers)
    print(f"[fullcrawl] Found {total} Phase-3 crawlers in {custom_dir}", file=sys.stderr)
    print(f"[fullcrawl] limit={limit}  timeout={timeout_sec}s  resume={args.resume}", file=sys.stderr)

    results: list[dict] = []
    wall_start = time.time()

    # Load any already-completed results if --resume
    completed_ids: set[str] = set()
    if args.resume:
        for jf in out_dir.glob("*.json"):
            if jf.stem.startswith("_"):
                continue
            try:
                existing = json.loads(jf.read_text(encoding="utf-8"))
                results.append(existing)
                completed_ids.add(existing["site_id"])
            except Exception:
                pass
        if completed_ids:
            print(f"[fullcrawl] --resume: skipping {len(completed_ids)} already-done sites", file=sys.stderr)

    interrupted = False

    for idx, py_path in enumerate(crawlers, start=1):
        site_id = py_path.stem   # filename without .py
        out_json  = out_dir / f"{site_id}.json"
        log_path  = logs_dir / f"{site_id}.stdout.log"

        if args.resume and site_id in completed_ids:
            print(f"[fullcrawl] {idx}/{total} skipping {site_id} (already done)", file=sys.stderr)
            continue

        print(f"[fullcrawl] {idx}/{total} starting {site_id} ...", file=sys.stderr, flush=True)
        started_at = now_iso()
        t0 = time.time()

        try:
            raw = run_crawler(
                py_path=py_path,
                site_id=site_id,
                limit=limit,
                timeout_sec=timeout_sec,
                log_path=log_path,
                project_root=project_root,
            )
        except KeyboardInterrupt:
            print(f"\n[fullcrawl] SIGINT received — stopping after {idx-1}/{total}", file=sys.stderr)
            interrupted = True
            break

        elapsed = time.time() - t0
        ended_at = now_iso()

        status = classify_status(raw)
        items  = raw.get("items_saved", 0) or 0

        record = {
            "site_id":            site_id,
            "py_path":            str(py_path.relative_to(project_root)),
            "started_at":         started_at,
            "ended_at":           ended_at,
            "elapsed_seconds":    round(elapsed, 1),
            "status":             status,
            "items_saved":        items,
            "actual_db_rows":     raw.get("actual_db_rows", items),
            "completed_naturally": raw.get("completed_naturally", False),
            "avg_abstract_chars": raw.get("avg_abstract_chars", 0),
            "error":              raw.get("error"),
            "stopped_by":        raw.get("stopped_by"),
            "sample_titles":     raw.get("sample_titles", []),
            "stdout_log_path":   str(log_path.relative_to(project_root)),
        }

        # Save per-crawler JSON immediately
        out_json.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        results.append(record)

        print(
            f"[fullcrawl] {idx}/{total} done {site_id} "
            f"status={status} items={items} elapsed={elapsed:.0f}s",
            file=sys.stderr,
            flush=True,
        )

    total_elapsed = time.time() - wall_start

    # Write summary report
    write_report(report_path, results, limit, timeout_sec, total_elapsed)
    print(f"\n[fullcrawl] Report written to {report_path}", file=sys.stderr)
    print(f"[fullcrawl] Total wall time: {total_elapsed/60:.1f}min", file=sys.stderr)

    # Print quick summary to stdout
    status_counts: dict[str, int] = {}
    for r in results:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
    print("\n=== Phase 3.5 Quick Summary ===")
    for s, c in sorted(status_counts.items()):
        print(f"  {s}: {c}")
    print(f"  TOTAL: {len(results)}")

    if interrupted:
        sys.exit(130)


if __name__ == "__main__":
    main()
