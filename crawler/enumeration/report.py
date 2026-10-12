"""Completeness report: collected counts vs independent evidence, per site."""
from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from pathlib import Path

CSV_FIELDS = ("site_id", "strategy", "status", "stop_reason", "listed_unique", "saved", "failed_total",
              "failed_by_reason", "independent_evidence", "independent_total", "gap", "segments_total",
              "segments_incomplete", "segments_mismatch", "files_pending", "files_done", "files_failed",
              "oldest_item_date", "updated_at")


def site_report(store, site_id):
    site = store.site(site_id)
    if site is None:
        return {"site_id": site_id, "status": "not_started"}
    fp = site["plan_fp"]
    segments = store.segments(site_id, fp)
    listed = store.listed_count(site_id, fp)
    saved, failed = store.item_counts(site_id)
    evidence, independent = [], None
    strategy = site["strategy"]
    if strategy == "date_range_split":
        leaves = [s for s in segments if s["status"] != "split"]
        totals = [s["reported_total"] for s in leaves]
        if leaves and all(t is not None for t in totals):
            independent = sum(totals)
            evidence.append("segment_reported_total_sum")
        overall = site["state"].get("overall_total")
        if overall is not None:
            evidence.append(f"overall_reported_total={overall}")
    elif strategy == "sitemap":
        urlsets = [s for s in segments if s["evidence"] != "sitemap_index"]
        if urlsets and all(s["reported_total"] is not None for s in urlsets):
            independent = sum(s["reported_total"] for s in urlsets)
            evidence.append("sitemap_url_count")
    else:
        totals = [s["reported_total"] for s in segments if s["reported_total"] is not None]
        if totals:
            independent = totals[-1]
            evidence.append("source_reported_total")
    files = store.file_counts(site_id)
    oldest = store.oldest_saved(site_id)
    return {
        "site_id": site_id,
        "strategy": strategy,
        "status": site["status"],
        "stop_reason": site["stop_reason"],
        "listed_unique": listed,
        "saved": saved,
        "failed_total": sum(failed.values()),
        "failed_by_reason": failed,
        "independent_evidence": evidence or ["none"],
        "independent_total": independent,
        "gap": None if independent is None else independent - listed,
        "segments_total": len(segments),
        "segments_incomplete": sum(s["status"] in ("pending", "running", "failed") for s in segments),
        "segments_mismatch": sum(s["status"] in ("mismatch", "cap_unsplittable") for s in segments),
        "segments": [{k: s[k] for k in ("segment_key", "status", "reported_total", "listed", "evidence")}
                     for s in segments],
        "files_pending": files.get("pending", 0) + files.get("failed_transient", 0),
        "files_done": files.get("done", 0),
        "files_failed": files.get("failed", 0) + files.get("unavailable", 0),
        "oldest_item_date": oldest["item_date"] if oldest else None,
        "events": [{"kind": e["kind"], "detail": e["detail"], "at": e["at"]}
                   for e in store.events(site_id)[-50:]],
        "updated_at": site["updated_at"],
    }


def _atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write_report(store, site_ids, out_dir):
    rows = [site_report(store, s) for s in site_ids]
    out_dir = Path(out_dir)
    _atomic_text(out_dir / "completeness.json", json.dumps({"sites": rows}, ensure_ascii=False, indent=1))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        flat = dict(row)
        flat["failed_by_reason"] = ";".join(f"{k}={v}" for k, v in sorted((row.get("failed_by_reason") or {}).items()))
        flat["independent_evidence"] = ";".join(row.get("independent_evidence") or [])
        writer.writerow(flat)
    _atomic_text(out_dir / "completeness.csv", buf.getvalue())
    return rows
