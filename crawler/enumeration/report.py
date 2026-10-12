"""Completeness report: collected counts vs independent evidence, per site."""
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path

CSV_FIELDS = ("site_id", "strategy", "status", "stop_reason", "listed_unique", "saved_current_generation",
              "saved_all_time", "saved", "failed_total",
              "failed_by_reason", "independent_evidence", "independent_total", "gap", "segments_total",
              "segments_incomplete", "segments_mismatch", "segments_unverified", "new_duplicates",
              "skipped_existing_post_number", "identity_collisions", "identity_reviews", "robots_disallowed",
              "warnings",
              "identity_match_rate", "published_date_fill", "listed_date_fill", "files_pending", "files_done",
              "files_failed", "oldest_item_date", "request_count", "gate_wait_avg_s", "response_avg_s",
              "crawl_delay_s", "updated_at")


def date_fill(store, site_id, doc_conn):
    """Share of documents saved through this framework whose date fields are filled."""
    # Documents, not ledger rows: several item keys can resolve to one seq_id.
    seq_ids = [r["seq_id"] for r in store._all("SELECT DISTINCT seq_id FROM enum_item WHERE site_id=? "
                                                "AND status='saved' AND seq_id IS NOT NULL", (site_id,))]
    if doc_conn is None or not seq_ids:
        return {"documents": len(seq_ids), "published_date": None, "listed_date": None}
    filled = {"published_date": 0, "listed_date": 0}
    mark = "?" if isinstance(doc_conn, sqlite3.Connection) else "%s"
    for start in range(0, len(seq_ids), 500):
        chunk = seq_ids[start:start + 500]
        sql = (f"SELECT published_date, listed_date FROM documents WHERE seq_id IN ({','.join([mark] * len(chunk))})")
        for row in doc_conn.execute(sql, chunk).fetchall():
            values = tuple(row[k] for k in row.keys()) if hasattr(row, "keys") else tuple(row)
            filled["published_date"] += bool(values[0])
            filled["listed_date"] += bool(values[1])
    total = len(seq_ids)
    return {"documents": total, **{k: round(v / total, 4) for k, v in filled.items()}}


def _weighted(hosts, field):
    total = sum(h["requests"] for h in hosts.values())
    return round(sum(h[field] * h["requests"] for h in hosts.values()) / total, 3) if total else None


def site_report(store, site_id, doc_conn=None):
    site = store.site(site_id)
    if site is None:
        return {"site_id": site_id, "status": "not_started"}
    fp = site["plan_fp"]
    segments = store.segments(site_id, fp)
    listed = store.listed_count(site_id, fp)
    saved, failed = store.item_counts(site_id)
    evidence, independent, sitemap_count = [], None, None
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
        # The sitemap is the enumeration source itself: its URL count cannot be
        # independent evidence of its own completeness, so no gap is computed.
        urlsets = [s for s in segments if s["evidence"] != "sitemap_index"]
        if urlsets and all(s["reported_total"] is not None for s in urlsets):
            evidence.append("sitemap_url_count_not_independent")
            sitemap_count = sum(s["reported_total"] for s in urlsets)
    else:
        totals = [s["reported_total"] for s in segments if s["reported_total"] is not None]
        if totals:
            independent = totals[-1]
            evidence.append("source_reported_total")
    files = store.file_counts(site_id)
    oldest = store.oldest_saved(site_id)
    saved_current = store._one(
        "SELECT COUNT(DISTINCT i.item_key) AS n FROM enum_item i JOIN enum_listed l ON l.site_id=i.site_id "
        "AND l.item_key=i.item_key AND l.plan_fp=? WHERE i.site_id=? AND i.status='saved'", (fp, site_id))["n"]
    fill = date_fill(store, site_id, doc_conn)
    timing = site["state"].get("request_timing")
    hosts = (timing or {}).get("hosts") or {}
    return {
        "site_id": site_id,
        "strategy": strategy,
        "status": site["status"],
        "stop_reason": site["stop_reason"],
        "listed_unique": listed,
        # saved/saved_all_time: every generation; saved_current_generation: items of
        # the current plan's enumeration (comparable with listed_unique). gap=0 does not
        # mean every listed item was saved.
        "saved": saved,
        "saved_all_time": saved,
        "saved_current_generation": saved_current,
        "failed_total": sum(failed.values()),
        "failed_by_reason": failed,
        "independent_evidence": evidence or ["none"],
        "independent_total": independent,
        "gap": None if independent is None else independent - listed,
        "sitemap_url_count": sitemap_count,
        "segments_total": len(segments),
        "segments_incomplete": sum(s["status"] in ("pending", "running", "failed") for s in segments),
        "segments_mismatch": sum(s["status"] in ("mismatch", "cap_unsplittable") for s in segments),
        "segments_unverified": sum(s["status"] == "unverified" for s in segments),
        # Same document met again (dedup rule matched an existing row): counted, not new.
        "skipped_existing_post_number": store._one(
            "SELECT COUNT(*) AS n FROM enum_item WHERE site_id=? AND status='saved' AND reason='existing_document'",
            (site_id,))["n"],
        # Same post_number, different document: never saved, listed for review.
        "identity_collisions": failed.get("failed_permanent:identity_collision", 0),
        "identity_reviews": sum(v for k, v in failed.items() if k.startswith("failed_permanent:identity_review:")),
        # Detail URLs robots.txt excludes: listed but never requested or saved.
        "robots_disallowed": failed.get("failed_permanent:robots_disallowed", 0),
        "warnings": site["state"].get("warnings") or [],
        "unverified_segments": [s["segment_key"] for s in segments if s["status"] == "unverified"],
        "duplicates": site["state"].get("duplicates"),
        "new_duplicates": (site["state"].get("duplicates") or {}).get("new"),
        "identity": site["state"].get("identity"),
        "identity_match_rate": (site["state"].get("identity") or {}).get("match_rate"),
        "date_fill": fill,
        "published_date_fill": fill["published_date"],
        "listed_date_fill": fill["listed_date"],
        "segments": [{k: s[k] for k in ("segment_key", "status", "reported_total", "listed", "evidence")}
                     for s in segments],
        "files_pending": files.get("pending", 0) + files.get("failed_transient", 0),
        "files_done": files.get("done", 0),
        "files_failed": files.get("failed", 0) + files.get("unavailable", 0),
        "oldest_item_date": oldest["item_date"] if oldest else None,
        # Latest run's request timing (per host: gate wait incl. Crawl-delay vs response time).
        "request_timing": timing,
        "request_count": sum(h["requests"] for h in hosts.values()) if hosts else None,
        "gate_wait_avg_s": _weighted(hosts, "gate_wait_avg_s"),
        "response_avg_s": _weighted(hosts, "response_avg_s"),
        "crawl_delay_s": max((h["crawl_delay_s"] for h in hosts.values() if h.get("crawl_delay_s")), default=None),
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


def write_report(store, site_ids, out_dir, doc_conn=None):
    rows = [site_report(store, s, doc_conn=doc_conn) for s in site_ids]
    out_dir = Path(out_dir)
    _atomic_text(out_dir / "completeness.json", json.dumps({"sites": rows}, ensure_ascii=False, indent=1))
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        flat = dict(row)
        flat["failed_by_reason"] = ";".join(f"{k}={v}" for k, v in sorted((row.get("failed_by_reason") or {}).items()))
        flat["independent_evidence"] = ";".join(row.get("independent_evidence") or [])
        flat["warnings"] = ";".join(row.get("warnings") or [])
        writer.writerow(flat)
    _atomic_text(out_dir / "completeness.csv", buf.getvalue())
    return rows
