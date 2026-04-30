#!/usr/bin/env python3
"""Phase 1 verification: confirm parser fix hypotheses against live API.

Three sub-checks:
  1) detail API — share of ``dcmRltnStttMatrList[*].ntstTextMatrCntn`` that
     actually carries a non-empty value (the key the current parser misses).
     Also measures how often ``dcmDVO.ntstTlawClNm`` / ``dcmRgtDtm`` are null
     in detail responses (the gap_fill structural bug).
  2) list API — dump full ``dcm`` dict keys for first few items on page 1
     to confirm the real key names we must read during gap_fill.
  3) B-category cross-check — sample a few "deleted upstream" DOC_IDs and
     prove the detail endpoint still returns titles (page-drift not deletion).

Outputs:
    reports/parser_hypothesis_0423.json   — machine-readable summary
    stdout                                  — human-readable summary table
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "papers.db"
REPORT_DIR = ROOT / "reports"

sys.path.insert(0, str(ROOT))

from crawler import db as dbm  # noqa: E402
from crawler.sites.nts_taxlaw import (NTSTaxlawPdCrawler,  # noqa: E402
                                      NTSTaxlawQtCrawler)

SITE_CRAWLERS = {
    "nts-taxlaw-pd": NTSTaxlawPdCrawler,
    "nts-taxlaw-qt": NTSTaxlawQtCrawler,
}


def sample_doc_ids(conn: sqlite3.Connection, site_id: str, n: int,
                   seed: int) -> list[str]:
    rows = conn.execute(
        "SELECT external_id FROM papers WHERE site_id = ? AND external_id IS NOT NULL",
        (site_id,),
    ).fetchall()
    ids = [r[0] for r in rows if r[0]]
    rng = random.Random(seed)
    return rng.sample(ids, min(n, len(ids)))


def sample_b_ids(site_id: str, n: int, seed: int) -> list[str]:
    p = REPORT_DIR / f"gap_{site_id}.B_deleted_upstream.json"
    if not p.exists():
        return []
    items = json.loads(p.read_text(encoding="utf-8"))
    ids = [it["external_id"] for it in items if it.get("external_id")]
    rng = random.Random(seed)
    return rng.sample(ids, min(n, len(ids)))


def probe_detail(crawler, doc_ids: list[str]) -> dict:
    """For each doc_id fetch detail and collect the metrics we care about."""
    matr_total = 0       # total dcmRltnStttMatrList entries seen
    matr_with_cntn = 0   # entries whose ntstTextMatrCntn is non-empty
    matr_values: list[str] = []
    dvo_total = 0
    dvo_clnm_nonnull = 0
    dvo_dtm_nonnull = 0
    per_doc: list[dict] = []

    for i, did in enumerate(doc_ids, 1):
        detail = crawler._fetch_detail(did)
        if detail is None:
            per_doc.append({"doc_id": did, "status": "fail"})
            print(f"  [{i:>3}/{len(doc_ids)}] {did}: FAIL")
            continue
        dvo = detail.get("dcmDVO") or {}
        matr_list = detail.get("dcmRltnStttMatrList") or []
        local_cntn = []
        for m in matr_list:
            matr_total += 1
            v = (m.get("ntstTextMatrCntn") or "").strip()
            if v:
                matr_with_cntn += 1
                if len(matr_values) < 40:
                    matr_values.append(v)
                local_cntn.append(v)
        dvo_total += 1
        if (dvo.get("ntstTlawClNm") or "").strip():
            dvo_clnm_nonnull += 1
        if (dvo.get("dcmRgtDtm") or "").strip():
            dvo_dtm_nonnull += 1
        title = dvo.get("ntstDcmTtl") or ""
        per_doc.append({
            "doc_id": did, "status": "ok",
            "title": title[:40],
            "matr_count": len(matr_list),
            "matr_with_cntn": len(local_cntn),
            "cntn_sample": local_cntn[:4],
            "dvo_ntstTlawClNm": dvo.get("ntstTlawClNm"),
            "dvo_dcmRgtDtm": dvo.get("dcmRgtDtm"),
        })
        print(f"  [{i:>3}/{len(doc_ids)}] {did}: matr={len(matr_list):>2} "
              f"cntn={len(local_cntn):>2} "
              f"clnm={bool((dvo.get('ntstTlawClNm') or '').strip())} "
              f"dtm={bool((dvo.get('dcmRgtDtm') or '').strip())} "
              f"— {title[:40]}")

    return {
        "n_docs": len(doc_ids),
        "matr_entries_total": matr_total,
        "matr_entries_with_cntn": matr_with_cntn,
        "matr_cntn_rate": round(matr_with_cntn / matr_total, 4) if matr_total else None,
        "matr_value_samples": matr_values[:10],
        "dvo_total": dvo_total,
        "dvo_ntstTlawClNm_nonnull": dvo_clnm_nonnull,
        "dvo_dcmRgtDtm_nonnull": dvo_dtm_nonnull,
        "per_doc": per_doc,
    }


def probe_list_keys(crawler) -> dict:
    """Fetch page 1 and dump full dcm keys + sample values for first N items."""
    raw = crawler._fetch_list(page=1)
    if not raw:
        return {"error": "list fetch failed"}
    try:
        data = json.loads(raw)
        items = data["data"]["ASIPDI002PR01"]["body"] or []
    except Exception as e:  # noqa: BLE001
        return {"error": f"parse failed: {e}"}

    samples = []
    all_keys: set[str] = set()
    for it in items[:5]:
        dcm = it.get("dcm") or {}
        all_keys.update(dcm.keys())
        samples.append({
            "DOC_ID": dcm.get("DOC_ID"),
            "NTST_TLAW_CL_NM": dcm.get("NTST_TLAW_CL_NM"),
            "DCM_RGT_DTM": dcm.get("DCM_RGT_DTM"),
            "NTST_DCM_DSCM_CNTN": dcm.get("NTST_DCM_DSCM_CNTN"),
            "NTST_DCM_TTL": (dcm.get("NTST_DCM_TTL") or "")[:40],
            "all_keys": sorted(dcm.keys()),
        })
    return {
        "first_5_items": samples,
        "all_keys_union": sorted(all_keys),
    }


def probe_b(crawler, site_id: str, n: int, seed: int) -> dict:
    ids = sample_b_ids(site_id, n, seed)
    if not ids:
        return {"note": "no B list available", "ids_probed": []}
    results = []
    for did in ids:
        detail = crawler._fetch_detail(did)
        if detail is None:
            results.append({"doc_id": did, "status": "fail"})
            continue
        dvo = detail.get("dcmDVO") or {}
        title = dvo.get("ntstDcmTtl") or ""
        results.append({
            "doc_id": did,
            "status": "ok_has_title" if title else "empty",
            "title": title[:40],
        })
    ok = sum(1 for r in results if r.get("status") == "ok_has_title")
    return {"ids_probed": results, "ok_with_title": ok, "n": len(results)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-site", type=int, default=20)
    ap.add_argument("--b-per-site", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20260423)
    args = ap.parse_args()

    conn = dbm.get_db(str(DB_PATH))
    try:
        dbm.init_db(conn)
        result: dict = {}
        for site_id, cls in SITE_CRAWLERS.items():
            print(f"\n=== {site_id}: detail probe ({args.per_site} docs) ===")
            sample = sample_doc_ids(conn, site_id, args.per_site, args.seed)
            crawler = cls(db_conn=conn, delay=0.5)
            result[site_id] = {
                "detail": probe_detail(crawler, sample),
            }

            print(f"\n=== {site_id}: list key probe (page 1) ===")
            list_probe = probe_list_keys(crawler)
            result[site_id]["list"] = list_probe
            if "all_keys_union" in list_probe:
                print(f"  dcm key union ({len(list_probe['all_keys_union'])} keys):")
                for k in list_probe["all_keys_union"]:
                    print(f"    - {k}")
                for i, s in enumerate(list_probe["first_5_items"], 1):
                    print(f"  sample {i}: DOC_ID={s['DOC_ID']} "
                          f"CL_NM={s['NTST_TLAW_CL_NM']} "
                          f"DTM={s['DCM_RGT_DTM']} "
                          f"TTL={s['NTST_DCM_TTL']}")

            print(f"\n=== {site_id}: B cross-check ({args.b_per_site} docs) ===")
            result[site_id]["b_cross"] = probe_b(
                crawler, site_id, args.b_per_site, args.seed)
            for r in result[site_id]["b_cross"]["ids_probed"]:
                print(f"  {r['doc_id']}: {r['status']} "
                      f"— {r.get('title', '')}")

        # Summary table
        print("\n=== SUMMARY ===")
        print(f"{'site':<18} {'matr_rate':>10} {'clnm_null%':>12} "
              f"{'dtm_null%':>12} {'B_ok/n':>10}")
        for site_id in SITE_CRAWLERS:
            d = result[site_id]["detail"]
            b = result[site_id]["b_cross"]
            matr_rate = d["matr_cntn_rate"]
            dvo_t = d["dvo_total"] or 1
            clnm_null = 1 - d["dvo_ntstTlawClNm_nonnull"] / dvo_t
            dtm_null = 1 - d["dvo_dcmRgtDtm_nonnull"] / dvo_t
            print(f"{site_id:<18} {matr_rate!s:>10} "
                  f"{clnm_null*100:>11.1f}% {dtm_null*100:>11.1f}% "
                  f"{b.get('ok_with_title', 0)}/{b.get('n', 0):<8}")

        # Matr value samples
        print("\n=== ntstTextMatrCntn value samples (should be 주제어) ===")
        for site_id in SITE_CRAWLERS:
            samples = result[site_id]["detail"]["matr_value_samples"]
            print(f"  {site_id}: {samples}")

        out_path = REPORT_DIR / "parser_hypothesis_0423.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        print(f"\nwrote {out_path.relative_to(ROOT)}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
