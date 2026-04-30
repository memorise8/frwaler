#!/usr/bin/env python3
"""Quality snapshot: capture per-site field / metadata / abstract / filesystem
counts into JSON files that later runs can diff against.

Typical use:
    # Phase 0 baseline
    python scripts/snapshot_quality.py --out-prefix reports/baseline_0423 \
        --site-id nts-taxlaw-pd --site-id nts-taxlaw-qt

    # Phase 5 after a refetch, with diff table
    python scripts/snapshot_quality.py --out-prefix reports/after_0423 \
        --site-id nts-taxlaw-pd --site-id nts-taxlaw-qt \
        --compare-to reports/baseline_0423

Outputs (per --out-prefix):
    {prefix}_fields.json          # abstract / published_date / category / department misses
    {prefix}_metadata_keys.json   # per-key non-empty rate inside metadata JSON
    {prefix}_abstract_dist.json   # abstract length quantiles + bucket counts
    {prefix}_fs.json              # data/exports/<site>/ file counts
    {prefix}_diff.md              # only when --compare-to is given
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"
EXPORT_ROOT = ROOT / "data" / "exports"

# Metadata JSON keys to track. String-typed keys → track non_empty. List-typed
# keys → track non_empty (length > 0) and the total element count.
META_STR_KEYS = [
    "documentNumber", "documentTypeName", "replyReference", "fileId",
    "sourceOrgCode", "rawHtmlPath",
]
META_LIST_KEYS = [
    "relatedLaws", "trialHistory", "referencedCases", "citedCases",
    "relatedTopics", "attachedFiles",
]

ABS_BUCKETS = [100, 200, 500, 1000]


def _percentile(sorted_vals: list[int], p: float) -> int:
    if not sorted_vals:
        return 0
    k = max(0, min(len(sorted_vals) - 1, int(round((len(sorted_vals) - 1) * p))))
    return sorted_vals[k]


def collect_for_site(conn: sqlite3.Connection, site_id: str) -> dict:
    fields = {
        "total": 0,
        "abstract_empty": 0,
        "no_published_date": 0,
        "no_category": 0,
        "no_department": 0,
        "no_url": 0,
    }
    for b in ABS_BUCKETS:
        fields[f"abstract_lt_{b}"] = 0

    meta_keys: dict[str, dict] = {}
    for k in META_STR_KEYS + META_LIST_KEYS:
        meta_keys[k] = {"present": 0, "non_empty": 0}
        if k in META_LIST_KEYS:
            meta_keys[k]["total_elements"] = 0

    abs_lens: list[int] = []

    rows = conn.execute(
        "SELECT abstract, category, published_date, department, url, metadata "
        "FROM papers WHERE site_id = ?",
        (site_id,),
    )

    for r in rows:
        fields["total"] += 1
        abstract = r["abstract"] or ""
        alen = len(abstract)
        abs_lens.append(alen)
        if alen == 0:
            fields["abstract_empty"] += 1
        for b in ABS_BUCKETS:
            if alen < b:
                fields[f"abstract_lt_{b}"] += 1
        if not (r["published_date"] or "").strip():
            fields["no_published_date"] += 1
        if not (r["category"] or "").strip():
            fields["no_category"] += 1
        if not (r["department"] or "").strip():
            fields["no_department"] += 1
        if not (r["url"] or "").strip():
            fields["no_url"] += 1

        try:
            meta = json.loads(r["metadata"] or "{}")
        except json.JSONDecodeError:
            meta = {}

        for k in META_STR_KEYS:
            if k in meta:
                meta_keys[k]["present"] += 1
                if str(meta.get(k) or "").strip():
                    meta_keys[k]["non_empty"] += 1
        for k in META_LIST_KEYS:
            if k in meta:
                meta_keys[k]["present"] += 1
                lst = meta.get(k) or []
                if isinstance(lst, list) and len(lst) > 0:
                    meta_keys[k]["non_empty"] += 1
                    meta_keys[k]["total_elements"] += len(lst)

    abs_lens.sort()
    abs_dist = {
        "count": len(abs_lens),
        "min": abs_lens[0] if abs_lens else 0,
        "max": abs_lens[-1] if abs_lens else 0,
        "mean": int(statistics.fmean(abs_lens)) if abs_lens else 0,
        "p10": _percentile(abs_lens, 0.10),
        "p25": _percentile(abs_lens, 0.25),
        "p50": _percentile(abs_lens, 0.50),
        "p75": _percentile(abs_lens, 0.75),
        "p90": _percentile(abs_lens, 0.90),
        "p95": _percentile(abs_lens, 0.95),
    }

    out_dir = EXPORT_ROOT / site_id
    md_count = 0
    html_count = 0
    html_small_lt500 = 0
    if out_dir.is_dir():
        for p in out_dir.iterdir():
            if p.name.startswith("_") or p.is_dir():
                continue
            if p.suffix == ".md":
                md_count += 1
            elif p.suffix == ".html":
                html_count += 1
                try:
                    size = p.stat().st_size
                    if size < 500:
                        html_small_lt500 += 1
                except OSError:
                    pass
    fs = {
        "md_count": md_count,
        "html_count": html_count,
        "html_small_lt500": html_small_lt500,
    }

    return {
        "fields": fields,
        "metadata_keys": meta_keys,
        "abstract_dist": abs_dist,
        "fs": fs,
    }


def _write(prefix: Path, section: str, payload: dict) -> Path:
    p = prefix.parent / f"{prefix.name}_{section}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                 encoding="utf-8")
    return p


def write_snapshot(out_prefix: Path, site_to_data: dict) -> None:
    by_section: dict[str, dict] = {"fields": {}, "metadata_keys": {},
                                    "abstract_dist": {}, "fs": {}}
    for site, data in site_to_data.items():
        for section in by_section:
            by_section[section][site] = data[section]
    for section, payload in by_section.items():
        path = _write(out_prefix, section, payload)
        print(f"  wrote {path.relative_to(ROOT)}")


def _load(prefix: Path, section: str) -> dict:
    p = prefix.parent / f"{prefix.name}_{section}.json"
    return json.loads(p.read_text(encoding="utf-8"))


def _delta(before: int, after: int) -> str:
    if before == 0 and after == 0:
        return "=0"
    d = after - before
    if d == 0:
        return f"={after}"
    sign = "+" if d > 0 else ""
    pct = (d / before * 100) if before else 0.0
    return f"{after} ({sign}{d}, {sign}{pct:.1f}%)"


def render_diff(before_prefix: Path, after_prefix: Path) -> str:
    b_fields = _load(before_prefix, "fields")
    a_fields = _load(after_prefix, "fields")
    b_meta = _load(before_prefix, "metadata_keys")
    a_meta = _load(after_prefix, "metadata_keys")
    b_abs = _load(before_prefix, "abstract_dist")
    a_abs = _load(after_prefix, "abstract_dist")
    b_fs = _load(before_prefix, "fs")
    a_fs = _load(after_prefix, "fs")

    lines: list[str] = []
    lines.append(f"# Quality diff — {before_prefix.name} → {after_prefix.name}")
    lines.append("")

    sites = sorted(set(b_fields.keys()) | set(a_fields.keys()))

    for site in sites:
        lines.append(f"## {site}")
        lines.append("")
        lines.append("### fields")
        lines.append("")
        lines.append("| field | before | after (delta) |")
        lines.append("|---|---:|---|")
        for k in sorted(set(b_fields.get(site, {}).keys())
                        | set(a_fields.get(site, {}).keys())):
            bv = b_fields.get(site, {}).get(k, 0)
            av = a_fields.get(site, {}).get(k, 0)
            lines.append(f"| {k} | {bv} | {_delta(bv, av)} |")
        lines.append("")

        lines.append("### metadata_keys (non_empty / total_elements)")
        lines.append("")
        lines.append("| key | before non_empty | after non_empty | elements before → after |")
        lines.append("|---|---:|---|---|")
        for k in sorted(set(b_meta.get(site, {}).keys())
                        | set(a_meta.get(site, {}).keys())):
            bv = b_meta.get(site, {}).get(k, {})
            av = a_meta.get(site, {}).get(k, {})
            b_ne = bv.get("non_empty", 0)
            a_ne = av.get("non_empty", 0)
            b_el = bv.get("total_elements", "—")
            a_el = av.get("total_elements", "—")
            lines.append(f"| {k} | {b_ne} | {_delta(b_ne, a_ne)} | {b_el} → {a_el} |")
        lines.append("")

        lines.append("### abstract length distribution")
        lines.append("")
        lines.append("| stat | before | after |")
        lines.append("|---|---:|---:|")
        for k in ("count", "min", "p10", "p25", "p50", "p75", "p90", "p95",
                  "max", "mean"):
            lines.append(
                f"| {k} | {b_abs.get(site, {}).get(k, 0)} "
                f"| {a_abs.get(site, {}).get(k, 0)} |")
        lines.append("")

        lines.append("### filesystem")
        lines.append("")
        lines.append("| item | before | after (delta) |")
        lines.append("|---|---:|---|")
        for k in ("md_count", "html_count", "html_small_lt500"):
            bv = b_fs.get(site, {}).get(k, 0)
            av = a_fs.get(site, {}).get(k, 0)
            lines.append(f"| {k} | {bv} | {_delta(bv, av)} |")
        lines.append("")

    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-prefix", required=True,
                    help="Output path prefix, e.g. reports/baseline_0423")
    ap.add_argument("--site-id", action="append", required=True,
                    help="Site id to snapshot (repeatable)")
    ap.add_argument("--compare-to", default=None,
                    help="Prior snapshot prefix to diff against, e.g. "
                         "reports/baseline_0423")
    args = ap.parse_args()

    out_prefix = Path(args.out_prefix)
    if not out_prefix.is_absolute():
        out_prefix = ROOT / out_prefix

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    site_to_data: dict[str, dict] = {}
    for site in args.site_id:
        print(f"[{site}] collecting...")
        site_to_data[site] = collect_for_site(conn, site)

    print(f"\nwriting snapshot to {out_prefix}_*.json")
    write_snapshot(out_prefix, site_to_data)

    if args.compare_to:
        before_prefix = Path(args.compare_to)
        if not before_prefix.is_absolute():
            before_prefix = ROOT / before_prefix
        diff_md = render_diff(before_prefix, out_prefix)
        diff_path = out_prefix.parent / f"{out_prefix.name}_diff.md"
        diff_path.write_text(diff_md, encoding="utf-8")
        print(f"  wrote {diff_path.relative_to(ROOT)}")

    conn.close()


if __name__ == "__main__":
    main()
