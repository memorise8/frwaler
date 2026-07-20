# -*- coding: utf-8 -*-
"""list_codex_required.py — Extract Tier 1 fail entries that need Codex (Tier 2).

Usage
-----
.venv/bin/python scripts/list_codex_required.py [--out data/audit/codex_required.csv]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)

# ---------------------------------------------------------------------------
# Priority mapping: partial > analyzer_fail > crawler_fail
# ---------------------------------------------------------------------------

PRIORITY_ORDER = {
    "partial": 0,
    "analyzer_fail": 1,
    "crawler_fail": 2,
}

FAIL_STATUSES = {"partial", "analyzer_fail", "crawler_fail"}


def short_reason(entry: dict) -> str:
    """Extract a brief reason string from a tier1 run record."""
    status = entry.get("tier1_status", "")
    if status == "partial":
        analyzer = entry.get("analyzer") or {}
        crawler = entry.get("generic_crawler") or {}
        items = crawler.get("items_captured", 0) or 0
        sel = analyzer.get("selectors_inferred") or {}
        has_sel = any(v for v in sel.values() if v)
        if has_sel:
            return f"partial: selectors saved but items={items}"
        return f"partial: config_saved={analyzer.get('config_saved')} items={items}"
    elif status == "analyzer_fail":
        analyzer = entry.get("analyzer") or {}
        err = (analyzer.get("error") or "")[:80]
        return f"analyzer_fail: {err}"
    elif status == "crawler_fail":
        crawler = entry.get("generic_crawler") or {}
        err = (crawler.get("error") or "")[:80]
        return f"crawler_fail: {err}"
    return status


def load_tier1_fails(tier1_dir: Path) -> list[dict]:
    """Load all tier1 run JSONs with fail status."""
    results = []
    for f in sorted(tier1_dir.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("tier1_status") in FAIL_STATUSES:
            results.append(d)
    return results


def load_phase3_success_ids(sample_dir: Path) -> set[str]:
    """Load entry_ids from Phase 3 sample_runs where codex_gen_success=true."""
    ids: set[str] = set()
    for f in sample_dir.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("codex_gen_success"):
            ids.add(d["entry_id"])
    return ids


def load_coverage_meta(coverage_csv: Path) -> dict[str, dict]:
    """Build entry_id -> row dict from coverage_report.csv."""
    meta: dict[str, dict] = {}
    with open(coverage_csv, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            meta[row["entry_id"]] = row
    return meta


def build_codex_required(
    tier1_dir: Path,
    sample_dir: Path,
    coverage_csv: Path,
) -> list[dict]:
    fails = load_tier1_fails(tier1_dir)
    phase3_success = load_phase3_success_ids(sample_dir)
    meta = load_coverage_meta(coverage_csv)

    rows = []
    for entry in fails:
        eid = entry["entry_id"]
        if eid in phase3_success:
            continue
        cov = meta.get(eid, {})
        rows.append({
            "entry_id": eid,
            "sheet": entry.get("sheet") or cov.get("sheet", ""),
            "host": entry.get("host") or cov.get("host", ""),
            "url": entry.get("url") or cov.get("url", ""),
            "tier1_status": entry.get("tier1_status", ""),
            "tier1_reason": short_reason(entry),
            "priority": PRIORITY_ORDER.get(entry.get("tier1_status", ""), 9),
        })

    # Sort: priority asc, then entry_id for determinism
    rows.sort(key=lambda r: (r["priority"], r["entry_id"]))
    return rows


def write_csv(rows: list[dict], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = ["entry_id", "sheet", "host", "url", "tier1_status", "tier1_reason", "priority"]
    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def print_stats(
    tier1_dir: Path,
    sample_dir: Path,
    rows: list[dict],
    phase3_excluded: int,
) -> None:
    # Load full tier1 counts
    all_tier1: dict[str, int] = {}
    total_tier1 = 0
    for f in tier1_dir.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        total_tier1 += 1
        s = d.get("tier1_status", "?")
        all_tier1[s] = all_tier1.get(s, 0) + 1

    from collections import Counter
    dist: Counter = Counter(r["tier1_status"] for r in rows)

    print(f"[list-codex-required] Tier 1 진행: {total_tier1:,} / 1,304", file=sys.stderr)
    print(f"  → ok: {all_tier1.get('ok', 0)} (Tier 1 충분)", file=sys.stderr)
    print(f"  → partial: {all_tier1.get('partial', 0)} (codex 필요, 셀렉터 단서 있음)", file=sys.stderr)
    print(f"  → analyzer_fail: {all_tier1.get('analyzer_fail', 0)} (codex 필요)", file=sys.stderr)
    print(f"  → crawler_fail: {all_tier1.get('crawler_fail', 0)} (codex 필요)", file=sys.stderr)
    print(f"  → Phase 3 에서 이미 codex 성공: {phase3_excluded} (제외)", file=sys.stderr)
    print(f"", file=sys.stderr)
    print(f"→ Codex 처리 대기: {len(rows)}건 (data/audit/codex_required.csv)", file=sys.stderr)
    print(f"   partial={dist.get('partial', 0)}, "
          f"analyzer_fail={dist.get('analyzer_fail', 0)}, "
          f"crawler_fail={dist.get('crawler_fail', 0)}", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="list_codex_required",
        description="Extract Tier 1 fail entries requiring Codex (Tier 2) processing.",
    )
    p.add_argument(
        "--tier1-dir",
        default="data/audit/tier1_runs",
        metavar="DIR",
        help="Directory of per-entry tier1 run JSONs",
    )
    p.add_argument(
        "--sample-dir",
        default="data/audit/sample_runs",
        metavar="DIR",
        help="Directory of Phase 3 sample_runs JSONs (to exclude already-succeeded entries)",
    )
    p.add_argument(
        "--coverage-csv",
        default="data/audit/coverage_report.csv",
        metavar="PATH",
        help="Path to coverage_report.csv for metadata fallback",
    )
    p.add_argument(
        "--out",
        default="data/audit/codex_required.csv",
        metavar="PATH",
        help="Output CSV path",
    )
    return p


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    root = Path(PROJECT_ROOT)

    tier1_dir = Path(args.tier1_dir)
    if not tier1_dir.is_absolute():
        tier1_dir = root / tier1_dir

    sample_dir = Path(args.sample_dir)
    if not sample_dir.is_absolute():
        sample_dir = root / sample_dir

    coverage_csv = Path(args.coverage_csv)
    if not coverage_csv.is_absolute():
        coverage_csv = root / coverage_csv

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = root / out_path

    # Count phase3 excluded before filtering
    phase3_success = load_phase3_success_ids(sample_dir)
    fails_raw = load_tier1_fails(tier1_dir)
    phase3_excluded = sum(1 for e in fails_raw if e["entry_id"] in phase3_success)

    rows = build_codex_required(tier1_dir, sample_dir, coverage_csv)
    write_csv(rows, out_path)
    print_stats(tier1_dir, sample_dir, rows, phase3_excluded)
    print(f"[list-codex-required] Written: {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
