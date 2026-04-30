#!/usr/bin/env python3
"""Phase 6: bulk-backfill ``metadata.relatedTopics`` for every NTS taxlaw
paper that still has an empty list (after Phase 4 finishes).

The script patches ONLY the relatedTopics field — every other field in
metadata is preserved via a merge — so a mid-run interruption cannot clear
any other value. Resumable via checkpoint.

Usage:
    # Dry-run a 100-doc sample first
    python scripts/enrich_related_topics.py --sample 100 --dry-run

    # Full run (takes ~24-76h depending on delay)
    python scripts/enrich_related_topics.py --delay 1.0 \\
        --checkpoint reports/enrich_ckpt.json \\
        --log reports/enrich_log.ndjson
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"

sys.path.insert(0, str(ROOT))

from crawler import db as dbm  # noqa: E402
from crawler.sites.nts_taxlaw import (NTSTaxlawPdCrawler,  # noqa: E402
                                      NTSTaxlawQtCrawler)

SITE_CRAWLERS = {
    "nts-taxlaw-pd": NTSTaxlawPdCrawler,
    "nts-taxlaw-qt": NTSTaxlawQtCrawler,
}


def select_targets(conn: sqlite3.Connection, sample: int | None) -> list[dict]:
    """Return (site_id, external_id) pairs for rows whose relatedTopics is
    empty or missing. json_array_length returns 0 for empty arrays and
    NULL for missing; both count as 'needs enrichment'."""
    sql = """
        SELECT site_id, external_id FROM papers
        WHERE site_id LIKE 'nts-taxlaw-%'
          AND external_id IS NOT NULL AND external_id != ''
          AND (
            json_extract(metadata, '$.relatedTopics') IS NULL
            OR json_array_length(json_extract(metadata, '$.relatedTopics')) = 0
          )
        ORDER BY site_id, external_id
    """
    rows = conn.execute(sql).fetchall()
    pairs = [{"site_id": r["site_id"], "doc_id": r["external_id"]}
             for r in rows]
    if sample:
        pairs = pairs[:sample]
    return pairs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--sample", type=int, default=None,
                    help="Process only first N matches (test mode)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--checkpoint", default="reports/enrich_ckpt.json")
    ap.add_argument("--log", default="reports/enrich_log.ndjson")
    args = ap.parse_args()

    conn = dbm.get_db(str(DB_PATH))
    dbm.init_db(conn)

    crawlers = {sid: cls(db_conn=conn, delay=args.delay)
                for sid, cls in SITE_CRAWLERS.items()}

    ckpt_path = Path(args.checkpoint)
    if not ckpt_path.is_absolute():
        ckpt_path = ROOT / ckpt_path
    processed_ids: set[str] = set()
    if ckpt_path.exists():
        ck = json.loads(ckpt_path.read_text(encoding="utf-8"))
        processed_ids = set(ck.get("processed", []))
        print(f"resumed from checkpoint: {len(processed_ids)} already done")

    log_path = Path(args.log)
    if not log_path.is_absolute():
        log_path = ROOT / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fh = log_path.open("a", encoding="utf-8")

    print("selecting targets...")
    targets = select_targets(conn, args.sample)
    targets = [t for t in targets if t["doc_id"] not in processed_ids]
    print(f"  {len(targets)} to process")

    empty_streak = 0
    effective_delay = args.delay
    written = 0
    failed = 0
    enriched = 0

    try:
        for i, t in enumerate(targets, 1):
            doc_id = t["doc_id"]
            site_id = t["site_id"]
            crawler = crawlers.get(site_id)
            if crawler is None:
                failed += 1
                continue

            time.sleep(effective_delay)
            detail = crawler._fetch_detail(doc_id)
            if detail is None:
                empty_streak += 1
                failed += 1
                if empty_streak >= 3:
                    effective_delay = min(effective_delay * 2, 30.0)
                    print(f"  streak={empty_streak}, "
                          f"raising delay to {effective_delay}s")
                continue
            empty_streak = 0

            related_topics = [
                v for m in (detail.get("dcmRltnStttMatrList") or [])
                if (v := (m.get("ntstTextMatrCntn") or "").strip())
            ]

            if not related_topics:
                log_fh.write(json.dumps({
                    "doc_id": doc_id, "site_id": site_id,
                    "action": "skip_empty",
                }, ensure_ascii=False) + "\n")
                processed_ids.add(doc_id)
                continue

            if args.dry_run:
                log_fh.write(json.dumps({
                    "doc_id": doc_id, "site_id": site_id,
                    "action": "dry_run",
                    "related_topics": related_topics,
                }, ensure_ascii=False) + "\n")
                enriched += 1
            else:
                row = conn.execute(
                    "SELECT metadata FROM papers "
                    "WHERE site_id=? AND external_id=?",
                    (site_id, doc_id),
                ).fetchone()
                if row is None:
                    failed += 1
                    continue
                try:
                    meta = json.loads(row["metadata"] or "{}")
                except json.JSONDecodeError:
                    meta = {}
                meta["relatedTopics"] = related_topics
                new_meta = json.dumps(meta, ensure_ascii=False)
                conn.execute(
                    "UPDATE papers SET metadata=? WHERE site_id=? AND external_id=?",
                    (new_meta, site_id, doc_id),
                )
                conn.commit()
                written += 1
                enriched += 1
                log_fh.write(json.dumps({
                    "doc_id": doc_id, "site_id": site_id,
                    "action": "patched",
                    "related_topics_count": len(related_topics),
                }, ensure_ascii=False) + "\n")
                log_fh.flush()

            processed_ids.add(doc_id)

            if i % 100 == 0:
                ckpt_path.parent.mkdir(parents=True, exist_ok=True)
                ckpt_path.write_text(
                    json.dumps({"processed": list(processed_ids)}),
                    encoding="utf-8")
                print(f"  [{i}/{len(targets)}] enriched={enriched} "
                      f"written={written} failed={failed}")
    finally:
        log_fh.close()
        ckpt_path.parent.mkdir(parents=True, exist_ok=True)
        ckpt_path.write_text(
            json.dumps({"processed": list(processed_ids)}),
            encoding="utf-8")
        conn.close()

    print(f"\ndone. enriched={enriched}  written={written}  failed={failed}")
    print(f"log: {log_path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
