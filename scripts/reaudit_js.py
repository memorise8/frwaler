#!/usr/bin/env python3
"""미수집 152곳 재실사 — 3차 playwright 프로브 (2026-07-18).

대상: 재실사분류가 'JS렌더필요(playwright재확인)' 또는 '재확인필요'인 사이트.
방법: playwright로 목록을 렌더링 → 상세 후보 추출 → 상세 렌더링 → 본문/PDF 판정.
결과: data/audit/reaudit_tier3_20260718.csv + reaudit_final_20260718.csv 갱신.
"""
import csv
import re
import sys
from datetime import datetime, timezone
from urllib.parse import urljoin

sys.path.insert(0, "scripts")
sys.path.insert(0, ".")
from reaudit_deep import extract_detail_candidates, probe_pdf, TAG_RX, BLOCK_RX  # noqa: E402
from crawler.playwright_fetcher import fetch_html, is_cf_challenge  # noqa: E402

FINAL_CSV = "data/audit/reaudit_final_20260718.csv"
OUT_CSV = "data/audit/reaudit_tier3_20260718.csv"
TARGET_CLS = {"JS렌더필요(playwright재확인)", "재확인필요"}


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%SZ")
    print(f"{ts} {msg}", flush=True)


def probe(row):
    host = row["host"]
    listing_url = row["url"]
    out = {"host": host, "기존분류": row["기존분류"], "이전판정": row["재실사분류"],
           "listing_render": "", "n_candidates": 0, "detail_verdict": "",
           "pdf_verdict": "", "3차판정": ""}

    html = fetch_html(listing_url, timeout_seconds=40)
    if not html:
        out["listing_render"] = "render_fail"
        out["3차판정"] = "여전히차단(렌더실패)"
        log(f"[{host}] {out['3차판정']}")
        return out
    if is_cf_challenge(html):
        out["listing_render"] = "cf_challenge"
        out["3차판정"] = "여전히차단(봇차단)"
        log(f"[{host}] {out['3차판정']}")
        return out
    out["listing_render"] = "ok"

    cands = extract_detail_candidates(listing_url, html)
    out["n_candidates"] = len(cands)
    if not cands:
        out["3차판정"] = "JS후에도후보없음(수동확인)"
        log(f"[{host}] {out['3차판정']} bodylen={len(TAG_RX.sub(' ', html))}")
        return out

    for cand in cands[:2]:
        if cand.lower().endswith(".pdf"):
            out["pdf_verdict"] = probe_pdf(cand, listing_url)
            out["detail_verdict"] = "목록직결PDF"
            break
        dhtml = fetch_html(cand, timeout_seconds=40)
        if not dhtml:
            out["detail_verdict"] = "render_fail"
            continue
        if is_cf_challenge(dhtml) or BLOCK_RX.search(dhtml[:20000]):
            out["detail_verdict"] = "본문차단마커"
            continue
        if len(TAG_RX.sub(" ", dhtml)) < 500:
            out["detail_verdict"] = "본문빈약"
            continue
        out["detail_verdict"] = "ok"
        m = re.search(r"""href=["']([^"']+\.pdf[^"']*)["']""", dhtml, re.I)
        if m:
            out["pdf_verdict"] = probe_pdf(urljoin(cand, m.group(1)), cand)
        break

    if out["detail_verdict"] in ("ok", "목록직결PDF"):
        if out["pdf_verdict"].startswith("pdf_ok"):
            out["3차판정"] = "수집가능(JS렌더·PDF확인)"
        elif out["pdf_verdict"] in ("", "pdf_fake"):
            out["3차판정"] = "수집가능(JS렌더·HTML본문)"
        else:
            out["3차판정"] = "본문OK·PDF차단(JS)"
    elif out["detail_verdict"] == "본문차단마커":
        out["3차판정"] = "부분차단(본문차단)"
    else:
        out["3차판정"] = f"본문접근실패({out['detail_verdict']})"
    log(f"[{host}] {out['3차판정']} detail={out['detail_verdict']} pdf={out['pdf_verdict']}")
    return out


def main():
    rows = list(csv.DictReader(open(FINAL_CSV, encoding="utf-8-sig")))
    targets = [r for r in rows if r["재실사분류"] in TARGET_CLS]
    log(f"tier-3 targets: {len(targets)}")
    results = [probe(r) for r in targets]

    with open(OUT_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        w.writeheader()
        w.writerows(results)

    # final CSV 갱신 (robots금지 규칙 유지: 수집가능인데 robots disallowed → 정책 유지)
    res_by_host = {r["host"]: r for r in results}
    for r in rows:
        t3 = res_by_host.get(r["host"])
        if not t3:
            continue
        cls = t3["3차판정"]
        if cls.startswith("수집가능") and r.get("robots목록경로") == "disallowed":
            cls = "robots금지(수집자제유지)"
        r["재실사분류"] = cls
        r["2차판정"] = (r["2차판정"] + "→" + t3["3차판정"]) if r["2차판정"] else t3["3차판정"]
    with open(FINAL_CSV, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    from collections import Counter
    print("\n== 3차판정 분포 ==", flush=True)
    for k, v in Counter(r["3차판정"] for r in results).most_common():
        print(f"  {k}: {v}", flush=True)
    print("\n== 최종 재실사분류 합계 (152) ==", flush=True)
    for k, v in Counter(r["재실사분류"] for r in rows).most_common():
        print(f"  {k}: {v}", flush=True)
    print(f"\nsaved: {OUT_CSV}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
