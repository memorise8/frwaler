# -*- coding: utf-8 -*-
"""
Phase 2 — Analyzer dry-run: infer list/item selectors + pagination for Group A sites.

Usage:
    .venv/bin/python scripts/analyzer_dryrun.py \
        [--coverage-csv data/audit/coverage_report.csv] \
        [--outputs-dir data/audit/analyzer_outputs] \
        [--concurrency 10] [--limit N] [--resume]
"""

import argparse
import asyncio
import csv
import json
import os
import signal
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

# ---------------------------------------------------------------------------
# Bootstrap: load OPENAI_API_KEY from crawler/.env if not already set
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_ENV_PATH = _PROJECT_ROOT / "crawler" / ".env"
if _ENV_PATH.exists():
    from dotenv import load_dotenv
    load_dotenv(_ENV_PATH)

# ---------------------------------------------------------------------------
# Import internal helpers from crawler/analyzer.py (thin wrapper approach)
# ---------------------------------------------------------------------------
sys.path.insert(0, str(_PROJECT_ROOT))
from crawler.analyzer import _get_client, _fetch_page, _clean_html, _analyze_list_page  # noqa: E402

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COST_INPUT_PER_1M = 0.15   # USD / 1M input tokens  (gpt-4o-mini)
COST_OUTPUT_PER_1M = 0.60  # USD / 1M output tokens (gpt-4o-mini)

TARGET_RENDER_CLASSES = {"static_list", "spa_likely"}
EXCLUDED_RECOVERY = "still_blocked"

# New CSV columns
NEW_COLUMNS = [
    "analyzer_status",
    "list_selector_inferred",
    "item_selector_inferred",
    "next_page_inferred",
    "pagination_type",
]

# ---------------------------------------------------------------------------
# Global state for graceful shutdown
# ---------------------------------------------------------------------------
_shutdown = False


def _handle_sigint(sig, frame):
    global _shutdown
    print("\n[analyzer] SIGINT received — finishing in-flight calls then saving...", file=sys.stderr)
    _shutdown = True


signal.signal(signal.SIGINT, _handle_sigint)

# ---------------------------------------------------------------------------
# Core per-URL analysis (synchronous, called in thread pool)
# ---------------------------------------------------------------------------

def _analyze_url_sync(entry_id: str, url: str, output_path: Path) -> dict:
    """
    Fetch URL and call GPT-4o-mini for list page structure.
    Returns a result dict with keys:
        analyzer_status, list_selector_inferred, item_selector_inferred,
        next_page_inferred, pagination_type, raw (full LLM output),
        usage_input_tokens, usage_output_tokens, error
    """
    result = {
        "entry_id": entry_id,
        "url": url,
        "analyzer_status": "fail",
        "list_selector_inferred": "",
        "item_selector_inferred": "",
        "next_page_inferred": "",
        "pagination_type": "",
        "usage_input_tokens": 0,
        "usage_output_tokens": 0,
        "error": None,
        "raw": None,
    }

    max_retries = 3
    backoff = 1.0

    for attempt in range(max_retries + 1):
        try:
            # 1. Fetch page
            html = _fetch_page(url)
            cleaned = _clean_html(html)

            # 2. Call GPT-4o-mini (need raw response for usage)
            client = _get_client()
            response = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "웹 스크래핑 전문가. JSON만 응답합니다."},
                    {
                        "role": "user",
                        "content": _build_prompt(cleaned, url),
                    },
                ],
                response_format={"type": "json_object"},
                max_tokens=800,
                temperature=0.1,
                timeout=60,
            )

            # 3. Parse
            raw = json.loads(response.choices[0].message.content)
            result["raw"] = raw
            result["usage_input_tokens"] = getattr(response.usage, "prompt_tokens", 0)
            result["usage_output_tokens"] = getattr(response.usage, "completion_tokens", 0)

            # 4. Extract key selectors
            item_container = raw.get("item_container") or ""
            item_link = raw.get("item_link") or ""
            title = raw.get("title") or ""
            pagination_param = raw.get("pagination_param") or ""
            pagination_type_raw = raw.get("pagination_type") or ""

            result["list_selector_inferred"] = item_container
            result["item_selector_inferred"] = item_link or title
            result["next_page_inferred"] = pagination_param
            result["pagination_type"] = _normalize_pagination_type(pagination_type_raw)

            # 5. Classify status
            selectors = [s for s in [item_container, item_link, title] if s.strip()]
            if len(selectors) >= 2:
                result["analyzer_status"] = "ok"
            elif len(selectors) == 1:
                result["analyzer_status"] = "partial"
            else:
                result["analyzer_status"] = "fail"
                result["error"] = "no_selectors"

            break  # success

        except Exception as exc:
            err_str = str(exc)
            is_rate_limit = "429" in err_str or "rate_limit" in err_str.lower() or "RateLimitError" in type(exc).__name__

            if is_rate_limit and attempt < max_retries:
                wait = backoff * (3 ** attempt)
                print(
                    f"[analyzer] 429 rate limit for {url[:60]} — waiting {wait:.0f}s (attempt {attempt+1}/{max_retries})",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue

            result["analyzer_status"] = "fail"
            result["error"] = _classify_error(exc)
            result["raw"] = {"error": err_str}
            break

    # 6. Write raw output immediately
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    return result


def _build_prompt(html: str, url: str) -> str:
    """Build the GPT prompt (same format as analyzer._analyze_list_page but inline)."""
    return f"""당신은 웹 스크래핑 전문가입니다. 아래 HTML은 게시판/목록 페이지입니다.
이 페이지에서 반복되는 아이템(게시글, 논문, 보고서 등)의 구조를 분석해주세요.

URL: {url}

HTML:
{html}

다음 JSON 형식으로 응답해주세요:
{{
  "item_container": "반복 아이템의 CSS 셀렉터 (예: table tbody tr, div.list-item)",
  "item_link": "아이템 내 상세 페이지 링크의 CSS 셀렉터 (예: a.title, td a[href])",
  "item_link_attr": "링크 URL이 있는 속성 (보통 href)",
  "title": "아이템 내 제목 CSS 셀렉터 (예: a.title, td.subject a)",
  "date": "아이템 내 날짜 CSS 셀렉터 (예: td.date, span.date) 또는 null",
  "category": "아이템 내 카테고리 CSS 셀렉터 또는 null",
  "pagination_type": "query_param 또는 none",
  "pagination_param": "페이지네이션 파라미터명 (예: page, nPage, pageNo) 또는 null",
  "pagination_start": 1,
  "items_found": "페이지에서 발견된 아이템 수 (숫자)"
}}

규칙:
- CSS 셀렉터는 구체적으로 작성하세요 (table.boardList tbody tr 처럼)
- 클래스명이 있으면 사용하세요
- 아이템이 없으면 items_found를 0으로 설정하세요
- JSON만 응답하세요"""


def _normalize_pagination_type(raw: str) -> str:
    """Map raw LLM pagination values to canonical set."""
    raw = (raw or "").lower().strip()
    if raw in ("query_param", "query", "numeric", "page"):
        return "numeric"
    if raw in ("cursor", "offset", "scroll"):
        return "cursor"
    if "load" in raw or "more" in raw or "infinite" in raw:
        return "load-more"
    if raw in ("none", ""):
        return "none"
    return raw or "none"


def _classify_error(exc: Exception) -> str:
    """Classify exception into a short error label."""
    err = str(exc).lower()
    exc_name = type(exc).__name__

    if "429" in err or "rate_limit" in err:
        return "rate_limit"
    if "timeout" in err or "timed out" in err:
        return "timeout"
    if "ssl" in err or "certificate" in err:
        return "ssl_error"
    if "connection" in err or "refused" in err or "resolver" in err:
        return "connection_error"
    if "403" in err or "forbidden" in err:
        return "auth_blocked"
    if "404" in err or "not found" in err:
        return "dead_url"
    if "redirect" in err:
        return "redirect_error"
    if "json" in err or "parse" in err:
        return "parse_error"
    return f"error_{exc_name}"


# ---------------------------------------------------------------------------
# Async orchestrator
# ---------------------------------------------------------------------------

async def _run_batch(
    targets: list,
    outputs_dir: Path,
    concurrency: int,
) -> list:
    """Run all targets with bounded concurrency. Returns list of result dicts."""
    semaphore = asyncio.Semaphore(concurrency)
    loop = asyncio.get_event_loop()
    results = []
    total = len(targets)
    start_time = time.time()
    status_counts = Counter()
    total_input_tokens = 0
    total_output_tokens = 0
    done_count = 0

    async def process_one(entry_id, url, output_path):
        nonlocal done_count, total_input_tokens, total_output_tokens
        async with semaphore:
            if _shutdown:
                return None
            result = await loop.run_in_executor(
                None, _analyze_url_sync, entry_id, url, output_path
            )
            return result

    tasks = []
    for row in targets:
        entry_id = row["entry_id"]
        url = row["url"]
        output_path = outputs_dir / f"{entry_id}.json"
        tasks.append(process_one(entry_id, url, output_path))

    for coro in asyncio.as_completed(tasks):
        result = await coro
        if result is None:
            continue

        results.append(result)
        done_count += 1
        status_counts[result["analyzer_status"]] += 1
        total_input_tokens += result["usage_input_tokens"]
        total_output_tokens += result["usage_output_tokens"]

        if done_count % 100 == 0 or done_count == total:
            elapsed = time.time() - start_time
            elapsed_str = _fmt_elapsed(elapsed)
            cost = _calc_cost(total_input_tokens, total_output_tokens)
            pct = done_count / total * 100
            print(
                f"[analyzer] {done_count}/{total} ({pct:.1f}%) — elapsed {elapsed_str} — "
                f"status: ok={status_counts['ok']} partial={status_counts['partial']} fail={status_counts['fail']} — "
                f"tokens={_fmt_tokens(total_input_tokens + total_output_tokens)} cost=${cost:.2f}",
                file=sys.stderr,
            )

    return results, total_input_tokens, total_output_tokens


def _fmt_elapsed(seconds: float) -> str:
    m = int(seconds // 60)
    s = int(seconds % 60)
    return f"{m}m{s:02d}s"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1000:
        return f"{n/1000:.0f}k"
    return str(n)


def _calc_cost(input_tokens: int, output_tokens: int) -> float:
    return (input_tokens / 1_000_000) * COST_INPUT_PER_1M + (output_tokens / 1_000_000) * COST_OUTPUT_PER_1M


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def _load_csv(path: Path) -> tuple[list[dict], list[str]]:
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = list(reader.fieldnames or [])
    return rows, fieldnames


def _save_csv(path: Path, rows: list[dict], fieldnames: list[str]):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _ensure_new_columns(rows: list[dict], fieldnames: list[str]) -> list[str]:
    """Add new columns to fieldnames and ensure each row has them (default empty)."""
    for col in NEW_COLUMNS:
        if col not in fieldnames:
            fieldnames.append(col)
    for row in rows:
        for col in NEW_COLUMNS:
            if col not in row:
                row[col] = ""
    return fieldnames


def _apply_results_to_csv(rows: list[dict], results_by_id: dict):
    """Update rows in-place with analyzer results."""
    for row in rows:
        entry_id = row["entry_id"]
        if entry_id in results_by_id:
            r = results_by_id[entry_id]
            row["analyzer_status"] = r["analyzer_status"]
            row["list_selector_inferred"] = r["list_selector_inferred"]
            row["item_selector_inferred"] = r["item_selector_inferred"]
            row["next_page_inferred"] = r["next_page_inferred"]
            row["pagination_type"] = r["pagination_type"]


# ---------------------------------------------------------------------------
# Summary report
# ---------------------------------------------------------------------------

def _write_summary(
    summary_path: Path,
    rows: list[dict],
    target_count: int,
    processed_count: int,
    elapsed_seconds: float,
    concurrency: int,
    total_input_tokens: int,
    total_output_tokens: int,
    run_start: datetime,
):
    processed_rows = [r for r in rows if r.get("analyzer_status")]
    status_counter = Counter(r["analyzer_status"] for r in processed_rows)
    total_proc = sum(status_counter.values())

    ok_n = status_counter.get("ok", 0)
    partial_n = status_counter.get("partial", 0)
    fail_n = status_counter.get("fail", 0)

    fail_pct = fail_n / max(total_proc, 1) * 100

    if fail_pct < 30:
        gate = "Phase 3 진입 가능 (fail < 30%)"
    elif fail_pct <= 50:
        gate = "다국어 보강 검토 권장 (30% ≤ fail ≤ 50%)"
    else:
        gate = "다국어 보강 후 재실행 필수 (fail > 50%)"

    # Sheet ok rate top 10
    sheet_ok = Counter()
    sheet_total = Counter()
    for r in processed_rows:
        sheet = r.get("sheet", "unknown")
        sheet_total[sheet] += 1
        if r["analyzer_status"] == "ok":
            sheet_ok[sheet] += 1
    sheet_rates = {s: sheet_ok[s] / max(sheet_total[s], 1) for s in sheet_total}
    top_sheets = sorted(sheet_rates.items(), key=lambda x: -x[1])[:10]

    # Selector top 10
    list_sel_counter = Counter()
    for r in processed_rows:
        sel = r.get("list_selector_inferred", "").strip()
        if sel:
            list_sel_counter[sel.split(" ")[0]] += 1  # normalize: first token

    # Pagination type distribution
    pag_counter = Counter(r.get("pagination_type", "") for r in processed_rows if r.get("pagination_type"))

    # Fail error analysis — load from JSON output files
    error_counter = Counter()
    host_fail_counter = Counter()
    for r in processed_rows:
        if r["analyzer_status"] == "fail":
            host = r.get("host", urlparse(r.get("url", "")).hostname or "unknown")
            host_fail_counter[host] += 1

    # Try to get error labels from JSON files
    outputs_dir = summary_path.parent / "analyzer_outputs"
    if outputs_dir.exists():
        for entry_id in [r["entry_id"] for r in processed_rows if r["analyzer_status"] == "fail"]:
            jf = outputs_dir / f"{entry_id}.json"
            if jf.exists():
                try:
                    with open(jf) as f:
                        d = json.load(f)
                    err = d.get("error") or "unknown"
                    error_counter[err] += 1
                except Exception:
                    error_counter["unknown"] += 1

    cost = _calc_cost(total_input_tokens, total_output_tokens)

    lines = [
        "# Phase 2 — Analyzer Dry-run Summary",
        "",
        f"생성일: {run_start.isoformat()}",
        "",
        "## 실행",
        f"- 대상: {target_count}건",
        f"- 처리: {processed_count}건 (resume 포함)",
        f"- 소요 시간: {_fmt_elapsed(elapsed_seconds)}",
        f"- 동시성: {concurrency}",
        f"- 총 토큰: {total_input_tokens:,} in / {total_output_tokens:,} out",
        f"- 추정 비용: ${cost:.2f}",
        "",
        "## analyzer_status 분포",
        "| status | 건수 | 비율 |",
        "|--------|------|------|",
        f"| ok | {ok_n} | {ok_n/max(total_proc,1)*100:.1f}% |",
        f"| partial | {partial_n} | {partial_n/max(total_proc,1)*100:.1f}% |",
        f"| fail | {fail_n} | {fail_n/max(total_proc,1)*100:.1f}% |",
        "",
        "## sheet 별 ok 비율 (상위 10)",
        "| sheet | ok건 | 전체 | ok율 |",
        "|-------|------|------|------|",
    ]
    for sheet, rate in top_sheets:
        lines.append(f"| {sheet} | {sheet_ok[sheet]} | {sheet_total[sheet]} | {rate*100:.1f}% |")

    lines += [
        "",
        "## 셀렉터 패턴 분포",
        "",
        "### 가장 흔한 list_selector top 10 (정규화 — 첫 번째 토큰)",
        "| selector | 건수 |",
        "|----------|------|",
    ]
    for sel, cnt in list_sel_counter.most_common(10):
        lines.append(f"| `{sel}` | {cnt} |")

    lines += [
        "",
        "### pagination_type 분포",
        "| type | 건수 |",
        "|------|------|",
    ]
    for pt, cnt in pag_counter.most_common():
        lines.append(f"| {pt} | {cnt} |")

    lines += [
        "",
        "## 게이트 평가",
        f"- analyzer_status=fail 비율: {fail_pct:.1f}%",
        "- 30% 미만 → Phase 3 진입 가능",
        "- 30~50% → 다국어 보강 검토",
        "- 50% 초과 → 다국어 보강 후 재실행 필수",
        f"- **판정: {gate}**",
        "",
        "## fail 사유 상위 분류",
        "| 오류 유형 | 건수 |",
        "|-----------|------|",
    ]
    for err, cnt in error_counter.most_common(15):
        lines.append(f"| {err} | {cnt} |")

    lines += [
        "",
        "## host 별 fail 상위 10",
        "| host | fail 건수 |",
        "|------|-----------|",
    ]
    for host, cnt in host_fail_counter.most_common(10):
        lines.append(f"| {host} | {cnt} |")

    summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[analyzer] Summary written to {summary_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Phase 2 analyzer dry-run")
    parser.add_argument(
        "--coverage-csv",
        default="data/audit/coverage_report.csv",
        help="Path to coverage_report.csv",
    )
    parser.add_argument(
        "--outputs-dir",
        default="data/audit/analyzer_outputs",
        help="Directory to store per-entry JSON outputs",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=10,
        help="Max concurrent OpenAI API calls",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only first N target rows (for testing)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Skip entries that already have a JSON output file",
    )
    args = parser.parse_args()

    coverage_path = _PROJECT_ROOT / args.coverage_csv
    outputs_dir = _PROJECT_ROOT / args.outputs_dir
    outputs_dir.mkdir(parents=True, exist_ok=True)

    print(f"[analyzer] Loading CSV: {coverage_path}", file=sys.stderr)
    rows, fieldnames = _load_csv(coverage_path)
    fieldnames = _ensure_new_columns(rows, fieldnames)

    # Build target list
    targets = [
        r for r in rows
        if r.get("render_class") in TARGET_RENDER_CLASSES
        and r.get("recovery_status") != EXCLUDED_RECOVERY
    ]
    print(f"[analyzer] Target rows: {len(targets)}", file=sys.stderr)

    if args.resume:
        already_done = {r["entry_id"] for r in rows if r.get("analyzer_status") in ("ok", "partial", "fail")}
        # Also check JSON files for any that were processed but CSV not yet updated
        for p in outputs_dir.glob("*.json"):
            already_done.add(p.stem)
        targets = [t for t in targets if t["entry_id"] not in already_done]
        print(f"[analyzer] After resume filter: {len(targets)} remaining", file=sys.stderr)

    if args.limit:
        targets = targets[:args.limit]
        print(f"[analyzer] Limit applied: {len(targets)} rows", file=sys.stderr)

    total_targets = len(targets)
    if total_targets == 0:
        print("[analyzer] Nothing to process.", file=sys.stderr)
        return

    run_start = datetime.now(timezone.utc)
    wall_start = time.time()

    # Run async batch
    results, total_input_tokens, total_output_tokens = asyncio.run(
        _run_batch(targets, outputs_dir, args.concurrency)
    )

    elapsed = time.time() - wall_start

    # Map results by entry_id
    results_by_id = {r["entry_id"]: r for r in results}

    # Apply to CSV rows
    _apply_results_to_csv(rows, results_by_id)

    # Final CSV save
    _save_csv(coverage_path, rows, fieldnames)
    print(f"[analyzer] CSV saved: {coverage_path}", file=sys.stderr)

    # Print final stats
    cost = _calc_cost(total_input_tokens, total_output_tokens)
    status_counts = Counter(r["analyzer_status"] for r in results)
    print(
        f"[analyzer] DONE — processed={len(results)} elapsed={_fmt_elapsed(elapsed)} "
        f"ok={status_counts['ok']} partial={status_counts['partial']} fail={status_counts['fail']} "
        f"tokens_in={total_input_tokens:,} tokens_out={total_output_tokens:,} cost=${cost:.4f}",
        file=sys.stderr,
    )

    # Write summary report
    summary_path = _PROJECT_ROOT / "data" / "audit" / "phase2_summary.md"
    _write_summary(
        summary_path=summary_path,
        rows=rows,
        target_count=len(
            [r for r in rows if r.get("render_class") in TARGET_RENDER_CLASSES
             and r.get("recovery_status") != EXCLUDED_RECOVERY]
        ),
        processed_count=len(results),
        elapsed_seconds=elapsed,
        concurrency=args.concurrency,
        total_input_tokens=total_input_tokens,
        total_output_tokens=total_output_tokens,
        run_start=run_start,
    )


if __name__ == "__main__":
    main()
