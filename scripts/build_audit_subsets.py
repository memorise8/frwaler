#!/usr/bin/env python3
"""
build_audit_subsets.py — Phase 2 정리: coverage_report.csv 에서 분리 CSV 4개 생성.

산출:
  data/audit/playwright_required.csv  — cloudflare_challenge 차단 사이트
  data/audit/blocked.csv              — CF/서버 403, 404, robots_blocked
  data/audit/dead.csv                 — render_class == 'dead'
  data/audit/analyzer_failed.csv     — analyzer_status == 'fail'
"""

import csv
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# 경로 설정
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
COVERAGE = ROOT / "data" / "audit" / "coverage_report.csv"
SCROLL_INDEX = ROOT / "data" / "audit" / "scroll_index.json"
OUT_DIR = ROOT / "data" / "audit"

# ---------------------------------------------------------------------------
# 비영어권 시트 목록 (analyzer_failed recommended_action 분류용)
# ---------------------------------------------------------------------------
NON_ENGLISH_SHEETS = {
    "한국완료", "중국 완료", "일본완료",
    "France Ministries", "French Research Institutes",
    "French Public Institutions", "French Public Universities",
    "Greece Ministries", "Greece Research Centers",
    "Italy Ministries", "Italy Research centers",
    "Spain Ministries", "Spanish Research Centers",
    "Mexico Ministries",
    "Norway Ministries", "Norway Research Centers",
    "Estonia Ministries", "Estonia Research Centers",
    "Polish Research Centers",
    "Israel Ministries",
    "Netherland Ministries", "Dutch Research Centers",
    "오스트리아완료", "핀란드완료", "덴마크완료", "벨기에완료", "독일완료",
    "Germany Ministries", "GER ResearchCenters(Unfinished)",
    "Slovenia Research Centers",
    "Switzerland Departments", "Switzerland Research Centers",
    "Portugal Ministries",
}

ENGLISH_SHEETS = {
    "USA Executive Departments", "American Research Institutes",
    "호주완료", "뉴질랜드완료", "영국완료",
    "Canada Ministries", "Canadian Research Centers",
    "Ireland Ministries", "Irish Research Centers",
    "Chile Ministries", "Chile Research Centers",
}


def load_rows() -> list[dict]:
    with COVERAGE.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ---------------------------------------------------------------------------
# CSV 1: playwright_required
# ---------------------------------------------------------------------------
def build_playwright_required(rows: list[dict]) -> list[dict]:
    target = [r for r in rows if r.get("block_reason") == "cloudflare_challenge"]
    # host_count 계산
    host_counts = Counter(r["host"] for r in target)
    result = []
    for r in target:
        result.append({
            "entry_id": r["entry_id"],
            "sheet": r["sheet"],
            "host": r["host"],
            "url": r["url"],
            "host_count": host_counts[r["host"]],
            "recommended_action": "Playwright 도입 후 자동 가능",
            "notes": "",
        })
    # 정렬: host_count desc, host asc
    result.sort(key=lambda x: (-x["host_count"], x["host"]))
    return result


# ---------------------------------------------------------------------------
# CSV 2: blocked
# ---------------------------------------------------------------------------
def build_blocked(rows: list[dict]) -> list[dict]:
    BLOCK_REASON_TARGETS = {"cloudflare_403", "still_403", "still_401", "other_404"}

    def categorize(r: dict) -> str | None:
        br = r.get("block_reason", "")
        rc = r.get("render_class", "")
        if br == "cloudflare_403":
            return "cf_ip_block"
        if br in ("still_403", "still_401"):
            return "server_403"
        if br == "other_404":
            return "not_found"
        if rc == "robots_blocked":
            return "robots_disallow"
        return None

    ACTION_MAP = {
        "cf_ip_block":    "운영 검토 — IP 정책 차단, residential proxy 또는 포기",
        "server_403":     "사이트 정책 — 자동화 제외 권장, 수동 등록 검토",
        "not_found":      "URL 변경 가능성 — 대체 URL 조사",
        "robots_disallow": "robots.txt 준수 — 수집 제외",
    }

    target = []
    for r in rows:
        cat = categorize(r)
        if cat is not None:
            target.append((r, cat))

    # host_count 계산
    host_counts = Counter(r["host"] for r, _ in target)
    result = []
    for r, cat in target:
        result.append({
            "entry_id": r["entry_id"],
            "sheet": r["sheet"],
            "host": r["host"],
            "url": r["url"],
            "category": cat,
            "http_status": r.get("http_status", ""),
            "host_count": host_counts[r["host"]],
            "recommended_action": ACTION_MAP[cat],
        })
    return result


# ---------------------------------------------------------------------------
# CSV 3: dead
# ---------------------------------------------------------------------------
def build_dead(rows: list[dict]) -> list[dict]:
    def action(r: dict) -> str:
        err = r.get("error", "")
        status = r.get("http_status", "")
        if "timeout" in err:
            return "네트워크 재시도 권장 또는 사이트 응답 지연"
        if "dns" in err:
            return "도메인 변경/소멸 가능성 — URL 검증"
        if "ssl" in err:
            return "TLS 설정 이슈 — verify=False 재시도"
        if "connect_error" in err:
            return "호스트 다운 또는 차단 — 재확인"
        if err == "" and status:
            try:
                code = int(status)
                if 400 <= code < 600:
                    return f"http_{code} 응답 — 페이지 변경 가능성"
            except ValueError:
                pass
        if err == "":
            return "수동 점검 필요"
        return "수동 점검 필요"

    target = [r for r in rows if r.get("render_class") == "dead"]
    result = []
    for r in target:
        result.append({
            "entry_id": r["entry_id"],
            "sheet": r["sheet"],
            "host": r["host"],
            "url": r["url"],
            "http_status": r.get("http_status", ""),
            "error": r.get("error", ""),
            "recommended_action": action(r),
        })
    return result


# ---------------------------------------------------------------------------
# CSV 4: analyzer_failed
# ---------------------------------------------------------------------------
def build_analyzer_failed(rows: list[dict]) -> list[dict]:
    def action(sheet: str) -> str:
        if sheet in NON_ENGLISH_SHEETS:
            return "다국어 프롬프트 보강 후 재실행"
        if sheet in ENGLISH_SHEETS:
            return "사이트 구조 특이 — 검색 폼/SPA/인증 의심, 수동 검토"
        return "수동 검토"

    target = [r for r in rows if r.get("analyzer_status") == "fail"]
    result = []
    for r in target:
        result.append({
            "entry_id": r["entry_id"],
            "sheet": r["sheet"],
            "host": r["host"],
            "url": r["url"],
            "lang_hint": r.get("lang_hint", ""),
            "anchor_count": r.get("anchor_count", ""),
            "http_status": r.get("http_status", ""),
            "recommended_action": action(r["sheet"]),
        })
    return result


# ---------------------------------------------------------------------------
# CSV 저장 헬퍼
# ---------------------------------------------------------------------------
def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        print(f"  [경고] {path.name}: 행 없음")
        return
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"  {path.name}: {len(rows)} rows → {path}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] build_audit_subsets.py 시작")
    rows = load_rows()
    print(f"  coverage_report.csv: {len(rows)} rows")

    pw_rows = build_playwright_required(rows)
    write_csv(OUT_DIR / "playwright_required.csv", pw_rows)

    bl_rows = build_blocked(rows)
    write_csv(OUT_DIR / "blocked.csv", bl_rows)

    dead_rows = build_dead(rows)
    write_csv(OUT_DIR / "dead.csv", dead_rows)

    af_rows = build_analyzer_failed(rows)
    write_csv(OUT_DIR / "analyzer_failed.csv", af_rows)

    print(f"\n완료: CSV 4개 생성")


if __name__ == "__main__":
    main()
