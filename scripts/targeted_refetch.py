#!/usr/bin/env python3
"""Targeted refetch for NTS taxlaw documents.

Two primary use cases:
  - **Small sample** (1-100 docs): give an explicit doc id list, skip the
    list-API cache build. Enough to populate relatedTopics from the fixed
    parser; preserves existing fields (published_date, category etc.) when
    detail API returns None.
  - **Bulk refetch** (~15K docs, Phase 4): pass ``--list-cache-file`` so the
    script can also fill published_date / category / documentNumber /
    rawHtmlPath from list-API data that detail omits.

Each run writes an NDJSON diff log suitable for after-the-fact audit:

    {"doc_id": ..., "site_id": ...,
     "mode": "dry-run"|"write",
     "changes": {
        "category": {"before": "...", "after": "...", "status": "changed"|...},
        "metadata.relatedTopics": {"before_len": 0, "after_len": 3, ...},
        ...
     }}

Usage examples:
    # Phase 2 dry-run for 5 random documents
    python scripts/targeted_refetch.py --site-id nts-taxlaw-pd --random 5 \\
        --dry-run --log-updates reports/refetch_dryrun_pd.ndjson

    # Phase 3-a single live write
    python scripts/targeted_refetch.py --doc-id 200000000000014713 \\
        --site-id nts-taxlaw-pd \\
        --log-updates reports/refetch_phase3a.ndjson

    # Phase 4 bulk run
    python scripts/targeted_refetch.py \\
        --doc-id-file reports/refetch_targets_0423.json \\
        --list-cache-dir reports/list_cache \\
        --checkpoint reports/refetch_ckpt_0423.json \\
        --log-updates reports/refetch_updates_0423.ndjson \\
        --delay 1.0
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"

sys.path.insert(0, str(ROOT))

from crawler import db as dbm  # noqa: E402
from crawler.sites.nts_taxlaw import (NTSTaxlawPdCrawler,  # noqa: E402
                                      NTSTaxlawQtCrawler,
                                      _HTML_EXPORT_ROOT,
                                      _ROOT as _NTS_ROOT)

SITE_CRAWLERS = {
    "nts-taxlaw-pd": NTSTaxlawPdCrawler,
    "nts-taxlaw-qt": NTSTaxlawQtCrawler,
}

_DCM_CL_NAMES = {
    "01": "사전", "02": "질의", "03": "기준", "04": "고시",
    "05": "적부", "06": "이의", "07": "심사", "08": "심판",
    "09": "판례", "10": "헌재",
}

METADATA_LIST_KEYS = [
    "relatedLaws", "trialHistory", "referencedCases", "citedCases",
    "relatedTopics", "attachedFiles",
]
METADATA_STR_KEYS = [
    "documentNumber", "documentTypeName", "replyReference", "fileId",
    "sourceOrgCode", "rawHtmlPath",
]
TOP_FIELDS = ["title", "category", "published_date", "url"]


def build_paper(crawler, site_id: str, doc_id: str, detail: dict,
                list_item: dict) -> dict:
    """Mirror of ``_NTSTaxlawBase.gap_fill`` Phase 3 write path, but taking
    an optional list_item for fallback values. Keeps behaviour identical so
    the write path stays single-sourced in the crawler; this function only
    exists to let us diff before/after without writing."""
    dvo = detail.get("dcmDVO") or {}
    title = dvo.get("ntstDcmTtl") or dvo.get("TTL") or dvo.get("ttl") or ""
    doc_number = (dvo.get("ntstDcmDscmCntn")
                  or list_item.get("NTST_DCM_DSCM_CNTN") or "")
    tax_category = (dvo.get("ntstTlawClNm")
                    or list_item.get("NTST_TLAW_CL_NM") or "")
    doc_type_code = dvo.get("ntstDcmClCd") or ""
    doc_type_name = (_DCM_CL_NAMES.get(doc_type_code, "")
                     or list_item.get("NTST_DCM_CL_NM") or "")
    raw_date = dvo.get("dcmRgtDtm") or list_item.get("DCM_RGT_DTM") or ""
    file_id = dvo.get("ntstFleId") or list_item.get("NTST_FLE_ID") or ""
    src_org_cd = (dvo.get("ntstDcmSrcsOrgnClCd")
                  or list_item.get("NTST_DCM_SRCS_ORGN_CL_CD") or "")
    reply_ref = (dvo.get("ntstDcmRplyCntn")
                 or list_item.get("NTST_DCM_RPLY_CNTN") or "")
    detail_gist = dvo.get("ntstDcmGistCntn") or ""
    detail_content = dvo.get("ntstDcmCntn") or ""
    keywords_raw = dvo.get("ntstDcmMatrCntn") or ""
    keyword_list = [k.strip() for k in keywords_raw.split(",") if k.strip()] \
        if keywords_raw else []

    published_date = crawler._parse_date(raw_date)

    detail_extra_raw = {
        "attrYr": dvo.get("attrYr"),
        "decisionClassCd": dvo.get("ntstDcmDcsClCd"),
        "reviewResultCd": dvo.get("ntstDcmInveRsltCd"),
        "reviewReason": dvo.get("ntstDcmInveRsn"),
        "supremeCourtAllAgmt": dvo.get("sprcJdgmAllAgmtYn"),
        "caseNumber": dvo.get("dsbdHpnnNo"),
        "attachedFileId": dvo.get("ntstWpFleId"),
        "firstRegDtm": dvo.get("frsRgtDtm"),
        "lastAltDtm": dvo.get("lstAltDtm"),
        "inputOrgCd": dvo.get("inptOptrTxhfOgzCd"),
    }
    detail_extra = {k: v for k, v in detail_extra_raw.items()
                    if v not in (None, "", "ZZ", "ZZZ", "ZZZZ",
                                 "ZZZZZ", "ZZZZZZ", "ZZZZZZZ")}

    html_body = ""
    raw_html_content = ""
    for editor_item in (detail.get("dcmHwpEditorDVOList") or []):
        if editor_item.get("dcmFleTy") == "html":
            raw_html = editor_item.get("dcmFleByte", "") or ""
            if raw_html:
                raw_html_content = raw_html
                html_body = crawler._strip_tags(raw_html)
                break

    raw_html_path = ""
    if raw_html_content and doc_id:
        html_dir = _HTML_EXPORT_ROOT / site_id / "_html"
        html_dir.mkdir(parents=True, exist_ok=True)
        html_file = html_dir / f"{doc_id}.html"
        try:
            html_file.write_text(raw_html_content, encoding="utf-8")
            raw_html_path = str(html_file.relative_to(_NTS_ROOT))
        except Exception as e:  # noqa: BLE001
            print(f"[{site_id}] raw HTML save failed ({doc_id}): {e}")

    related_laws = [law.get("ntstTextNm", "")
                    for law in (detail.get("dcmRltnStttList") or [])
                    if law.get("ntstTextNm")]
    trial_history = [t.get("ntstDcmDscmCntn", "")
                     for t in (detail.get("trilPsagList") or [])
                     if t.get("ntstDcmDscmCntn")]
    referenced_cases = [p.get("ntstDcmDscmCntn", "")
                        for p in (detail.get("dcmRfrnPrtsList") or [])
                        if p.get("ntstDcmDscmCntn")]
    cited_cases = [p.get("ntstDcmDscmCntn", "")
                   for p in (detail.get("dcmQutPrtsList") or [])
                   if p.get("ntstDcmDscmCntn")]
    related_topics = [v for m in (detail.get("dcmRltnStttMatrList") or [])
                      if (v := (m.get("ntstTextMatrCntn") or "").strip())]
    attached_files = [{"name": f.get("fleOrgNm") or f.get("fleNm") or "",
                       "fileId": f.get("fleId") or f.get("ntstFleId") or ""}
                      for f in (detail.get("fleDVOList") or [])
                      if (f.get("fleOrgNm") or f.get("fleNm")
                          or f.get("fleId") or f.get("ntstFleId"))]

    abstract_parts = []
    for part in [detail_gist, detail_content, html_body]:
        if part and part not in abstract_parts:
            abstract_parts.append(part)
    abstract = "\n\n".join(abstract_parts)

    paper = {
        "id": None,
        "site_id": site_id,
        "external_id": doc_id,
        "title": title,
        "authors": json.dumps([], ensure_ascii=False),
        "abstract": abstract,
        "category": tax_category,
        "keywords": json.dumps(keyword_list, ensure_ascii=False),
        "published_date": published_date,
        "url": f"https://taxlaw.nts.go.kr/pd/USEPDA002P.do?ntstDcmId={doc_id}",
        "pdf_url": "",
        "doi": "",
        "department": "",
        "metadata": json.dumps({
            "documentNumber": doc_number,
            "documentTypeName": doc_type_name,
            "replyReference": reply_ref,
            "fileId": file_id,
            "sourceOrgCode": src_org_cd,
            "relatedLaws": related_laws,
            "trialHistory": trial_history,
            "referencedCases": referenced_cases,
            "citedCases": cited_cases,
            "relatedTopics": related_topics,
            "attachedFiles": attached_files,
            "rawHtmlPath": raw_html_path,
            **detail_extra,
        }, ensure_ascii=False),
    }
    return paper


def merge_paper(before_row: sqlite3.Row | None, new_paper: dict) -> dict:
    """Merge the fresh detail-API payload with the existing DB row so a
    refetch never clears a previously-populated field. Applies to top-level
    fields (title/category/published_date/abstract) and every metadata key.
    This is essential when list_cache is unavailable: detail API returns
    None for ntstTlawClNm / dcmRgtDtm / fileId on most records, so a blind
    rewrite would wipe those values out."""
    if before_row is None:
        return new_paper
    before = dict(before_row)

    for field in ["title", "category", "published_date", "abstract"]:
        if not (new_paper.get(field) or "").strip():
            new_paper[field] = before.get(field) or ""

    try:
        before_meta = json.loads(before.get("metadata") or "{}")
    except json.JSONDecodeError:
        before_meta = {}
    try:
        new_meta = json.loads(new_paper.get("metadata") or "{}")
    except json.JSONDecodeError:
        new_meta = {}

    merged_meta = dict(before_meta)
    for k, nv in new_meta.items():
        if nv in (None, "", [], {}):
            continue  # keep existing
        merged_meta[k] = nv

    new_paper["metadata"] = json.dumps(merged_meta, ensure_ascii=False)
    return new_paper


def diff_paper(before_row: sqlite3.Row | None, after_paper: dict) -> dict:
    """Build a structured diff between existing DB row and prospective write."""
    changes: dict[str, dict] = {}

    if before_row is None:
        changes["_status"] = {"status": "new_insert"}
    else:
        changes["_status"] = {"status": "update"}

    before = dict(before_row) if before_row is not None else {}
    # Top-level fields
    for field in TOP_FIELDS:
        bv = (before.get(field) or "") if before else ""
        av = after_paper.get(field) or ""
        if bv == av:
            continue
        status = _value_status(bv, av)
        changes[field] = {"before": _truncate(bv), "after": _truncate(av),
                          "status": status}

    # Abstract (compare by length to keep log small)
    b_abs = before.get("abstract") or "" if before else ""
    a_abs = after_paper.get("abstract") or ""
    if len(b_abs) != len(a_abs) or (b_abs and a_abs and b_abs[:200] != a_abs[:200]):
        changes["abstract"] = {
            "before_len": len(b_abs),
            "after_len": len(a_abs),
            "status": _value_status(b_abs, a_abs),
        }

    try:
        before_meta = json.loads(before.get("metadata") or "{}") if before else {}
    except json.JSONDecodeError:
        before_meta = {}
    try:
        after_meta = json.loads(after_paper.get("metadata") or "{}")
    except json.JSONDecodeError:
        after_meta = {}

    for k in METADATA_STR_KEYS:
        bv = str(before_meta.get(k) or "")
        av = str(after_meta.get(k) or "")
        if bv != av:
            changes[f"metadata.{k}"] = {"before": _truncate(bv),
                                        "after": _truncate(av),
                                        "status": _value_status(bv, av)}
    for k in METADATA_LIST_KEYS:
        bv = before_meta.get(k) or []
        av = after_meta.get(k) or []
        if not isinstance(bv, list):
            bv = []
        if not isinstance(av, list):
            av = []
        if len(bv) != len(av) or bv[:3] != av[:3]:
            changes[f"metadata.{k}"] = {
                "before_len": len(bv),
                "after_len": len(av),
                "after_sample": av[:3],
                "status": _value_status_list(bv, av),
            }
    return changes


def _truncate(s: str, n: int = 120) -> str:
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "…"


def _value_status(before: str, after: str) -> str:
    before = (before or "").strip()
    after = (after or "").strip()
    if before == after:
        return "preserved"
    if not before and after:
        return "added"
    if before and not after:
        return "cleared"
    return "changed"


def _value_status_list(before: list, after: list) -> str:
    if not before and not after:
        return "preserved"
    if not before and after:
        return "added"
    if before and not after:
        return "cleared"
    if before == after:
        return "preserved"
    return "changed"


def classify_site(doc_id: str) -> str:
    """DOC_ID prefix tells us pd vs qt. pd ids begin with '2', qt with '01'."""
    if doc_id.startswith("01"):
        return "nts-taxlaw-qt"
    if doc_id.startswith("2") or doc_id.startswith("00"):
        return "nts-taxlaw-pd"
    return ""


def load_doc_ids(args) -> list[dict]:
    """Return list of {doc_id, site_id}. site_id inferred if not explicit."""
    pairs: list[dict] = []
    if args.doc_id:
        for d in args.doc_id:
            sid = args.site_id or classify_site(d)
            pairs.append({"doc_id": d, "site_id": sid})
    if args.doc_id_file:
        path = Path(args.doc_id_file)
        if not path.is_absolute():
            path = ROOT / path
        data = json.loads(path.read_text(encoding="utf-8"))
        for entry in data:
            if isinstance(entry, str):
                d = entry
                sid = args.site_id or classify_site(d)
            else:
                d = entry.get("doc_id") or entry.get("external_id") or ""
                sid = entry.get("site_id") or args.site_id or classify_site(d)
            if d:
                pairs.append({"doc_id": d, "site_id": sid})
    if args.random:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        site = args.site_id or "nts-taxlaw-pd"
        rows = conn.execute(
            "SELECT external_id FROM papers WHERE site_id = ? "
            "ORDER BY RANDOM() LIMIT ?",
            (site, args.random),
        ).fetchall()
        conn.close()
        pairs.extend({"doc_id": r["external_id"], "site_id": site}
                     for r in rows)
    return pairs


def load_list_cache(args) -> dict[str, dict[str, dict]]:
    """Optionally load reports/list_cache_{site}.json per site. Shape:
    {doc_id: dcm_dict}."""
    cache: dict[str, dict[str, dict]] = {}
    if not args.list_cache_dir:
        return cache
    cache_dir = Path(args.list_cache_dir)
    if not cache_dir.is_absolute():
        cache_dir = ROOT / cache_dir
    if not cache_dir.is_dir():
        return cache
    for site in SITE_CRAWLERS:
        p = cache_dir / f"list_cache_{site}.json"
        if p.exists():
            cache[site] = json.loads(p.read_text(encoding="utf-8"))
            print(f"  loaded list cache for {site}: {len(cache[site])} items")
    return cache


def run(args) -> int:
    pairs = load_doc_ids(args)
    if args.limit:
        pairs = pairs[: args.limit]
    if not pairs:
        print("No doc ids to process.")
        return 0
    print(f"Processing {len(pairs)} documents...")

    list_cache = load_list_cache(args)

    conn = dbm.get_db(str(DB_PATH))
    dbm.init_db(conn)

    crawlers = {sid: cls(db_conn=conn, delay=args.delay)
                for sid, cls in SITE_CRAWLERS.items()}

    ckpt_path: Path | None = None
    processed_ids: set[str] = set()
    if args.checkpoint:
        ckpt_path = Path(args.checkpoint)
        if not ckpt_path.is_absolute():
            ckpt_path = ROOT / ckpt_path
        if ckpt_path.exists():
            ck = json.loads(ckpt_path.read_text(encoding="utf-8"))
            processed_ids = set(ck.get("processed", []))
            print(f"  resumed from checkpoint: {len(processed_ids)} already processed")

    log_path: Path | None = None
    log_fh = None
    if args.log_updates:
        log_path = Path(args.log_updates)
        if not log_path.is_absolute():
            log_path = ROOT / log_path
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_fh = log_path.open("a", encoding="utf-8")

    empty_streak = 0
    effective_delay = args.delay
    saved = 0
    skipped = 0
    changed = 0
    new_insert = 0
    failures = 0

    summary: dict[str, Any] = {
        "total": len(pairs),
        "dry_run": args.dry_run,
        "per_status": {"added": 0, "changed": 0, "preserved": 0, "cleared": 0},
    }

    try:
        for i, pair in enumerate(pairs, 1):
            doc_id = pair["doc_id"]
            site_id = pair["site_id"]
            if not site_id:
                print(f"  [{i}] {doc_id}: cannot infer site_id, skipping")
                failures += 1
                continue
            if doc_id in processed_ids:
                skipped += 1
                continue
            if site_id not in crawlers:
                print(f"  [{i}] {doc_id}: unknown site {site_id}, skipping")
                failures += 1
                continue

            crawler = crawlers[site_id]
            time.sleep(effective_delay)
            detail = crawler._fetch_detail(doc_id)
            if detail is None:
                empty_streak += 1
                failures += 1
                print(f"  [{i}/{len(pairs)}] {doc_id}: detail fetch FAILED "
                      f"(streak={empty_streak})")
                if empty_streak >= 3:
                    effective_delay = min(effective_delay * 2, 30.0)
                    print(f"  → raising delay to {effective_delay}s")
                continue
            empty_streak = 0

            list_item = list_cache.get(site_id, {}).get(doc_id, {})
            paper = build_paper(crawler, site_id, doc_id, detail, list_item)

            before_row = conn.execute(
                "SELECT * FROM papers WHERE site_id = ? AND external_id = ?",
                (site_id, doc_id),
            ).fetchone()
            if not args.no_merge:
                paper = merge_paper(before_row, paper)
            changes = diff_paper(before_row, paper)
            entry_status = changes.get("_status", {}).get("status")
            if entry_status == "new_insert":
                new_insert += 1
            elif len(changes) > 1:  # has real changes beyond _status
                changed += 1

            for k, v in changes.items():
                s = v.get("status")
                if s in summary["per_status"]:
                    summary["per_status"][s] += 1

            if not args.dry_run:
                if before_row is None:
                    import uuid
                    paper["id"] = str(uuid.uuid4())
                else:
                    paper["id"] = before_row["id"]
                dbm.upsert_paper(conn, paper)
                saved += 1

            if log_fh:
                log_fh.write(json.dumps({
                    "doc_id": doc_id,
                    "site_id": site_id,
                    "mode": "dry-run" if args.dry_run else "write",
                    "changes": changes,
                }, ensure_ascii=False) + "\n")
                log_fh.flush()

            processed_ids.add(doc_id)
            if ckpt_path and (i % 100 == 0):
                ckpt_path.parent.mkdir(parents=True, exist_ok=True)
                ckpt_path.write_text(json.dumps(
                    {"processed": list(processed_ids)}, ensure_ascii=False),
                    encoding="utf-8")

            if i % 50 == 0 or i == len(pairs) or i <= 10:
                status = "DRY" if args.dry_run else "WRITE"
                print(f"  [{i:>5}/{len(pairs)}] {status} {doc_id} ({site_id}) "
                      f"— changes: {len(changes) - 1}")
    finally:
        if log_fh:
            log_fh.close()
        if ckpt_path:
            ckpt_path.parent.mkdir(parents=True, exist_ok=True)
            ckpt_path.write_text(json.dumps(
                {"processed": list(processed_ids)}, ensure_ascii=False),
                encoding="utf-8")
        conn.close()

    print("\n=== done ===")
    print(f"  saved (written):   {saved}")
    print(f"  new inserts:       {new_insert}")
    print(f"  changed records:   {changed}")
    print(f"  already processed: {skipped}")
    print(f"  failures:          {failures}")
    print(f"  status counts:     {summary['per_status']}")
    if log_path:
        print(f"  diff log:          {log_path.relative_to(ROOT)}")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc-id", action="append",
                    help="Explicit DOC_ID (repeatable)")
    ap.add_argument("--doc-id-file",
                    help="JSON file: either list of strings or list of "
                         "{doc_id, site_id} objects")
    ap.add_argument("--random", type=int, default=0,
                    help="Pick N random existing doc ids from DB")
    ap.add_argument("--site-id", default=None,
                    help="Default site id when not embedded in the input")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true",
                    help="Do not write to DB; only compute diffs")
    ap.add_argument("--no-merge", action="store_true",
                    help="Disable merge-with-existing. When set, an empty "
                         "field in the new detail overwrites the stored "
                         "value. Use for auditing only.")
    ap.add_argument("--list-cache-dir",
                    help="Directory containing list_cache_<site_id>.json "
                         "files for published_date/category fallback")
    ap.add_argument("--checkpoint", default=None,
                    help="Path for processed-id checkpoint JSON")
    ap.add_argument("--log-updates", default=None,
                    help="NDJSON diff log path (appended to)")
    ap.add_argument("--delay", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=20260423)
    args = ap.parse_args()

    random.seed(args.seed)
    sys.exit(run(args))


if __name__ == "__main__":
    main()
