# -*- coding: utf-8 -*-
"""Regenerate client-facing collection-status deliverables from the CURRENT DB.

DB (read-only): data/libertree.db  (documents / sites)
Host universe : data/audit/scroll_index.json -> entries[].url -> urlparse().netloc (strip www.)
Reasons       : reaudit_final_20260718.csv (latest verdicts)
                uncollected_sites_reasons.csv (master reason table)
                manual_verify_7_20260719.json (manual-check set)
                robots_allow_hosts.json (38 institutions permitted 2026-07-19)

Outputs:
  data/audit/수집현황_전체.xlsx      (sheets: 수집성공 / 미수집)
  data/audit/수집현황_요약.csv        (cp949)
  data/audit/미수집_사이트_사유.csv   (cp949)
  docs/미수집_사이트_설명.md          (non-developer, utf-8)
"""
from __future__ import annotations
import json, sqlite3, csv, io
from urllib.parse import urlparse
from collections import defaultdict, Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUD = ROOT / "data" / "audit"
DB = ROOT / "data" / "libertree.db"


def norm(u: str) -> str:
    net = urlparse(u or "").netloc.lower()
    return net[4:] if net.startswith("www.") else net


def sanitize(s):
    """Replace special dashes/arrows/symbols that break in EUC-KR/cp949."""
    if s is None:
        return ""
    s = str(s)
    repl = {
        "—": "-", "–": "-", "―": "-", "‒": "-",
        "→": "->", "←": "<-", "⇒": "=>", "•": "-",
        "·": "-", "●": "", "⚫": "", "⚪": "",
        "\U0001f512": "", "\U0001f6ab": "", "✅": "", "❓": "",
        "‘": "'", "’": "'", "“": '"', "”": '"',
        "…": "...", " ": " ", "️": "", "≡": "=",
        "∼": "~",
    }
    for k, v in repl.items():
        s = s.replace(k, v)
    # drop any remaining chars not encodable in cp949
    out = []
    for ch in s:
        try:
            ch.encode("cp949")
            out.append(ch)
        except UnicodeEncodeError:
            out.append("")
    return "".join(out)


# --------------------------------------------------------------------------
# sheet -> (country, field)
# --------------------------------------------------------------------------
SHEET_MAP = {
    "한국완료": ("한국", "혼합"),
    "뉴질랜드완료": ("뉴질랜드", "혼합"),
    "American Research Institutes": ("미국", "연구기관"),
    "중국 완료": ("중국", "혼합"),
    "French Research Institutes": ("프랑스", "연구기관"),
    "스웨덴 완료": ("스웨덴", "혼합"),
    "Switzerland Research Centers": ("스위스", "연구기관"),
    "오스트리아완료": ("오스트리아", "혼합"),
    "호주완료": ("호주", "혼합"),
    "French Public Institutions": ("프랑스", "공공기관"),
    "USA Executive Departments": ("미국", "정부·부처"),
    "Irish Research Centers": ("아일랜드", "연구기관"),
    "Canadian Research Centers": ("캐나다", "연구기관"),
    "Dutch Research Centers": ("네덜란드", "연구기관"),
    "영국완료": ("영국", "혼합"),
    "French Public Universities": ("프랑스", "대학"),
    "Norway Research Centers": ("노르웨이", "연구기관"),
    "Israel Ministries": ("이스라엘", "정부·부처"),
    "독일완료": ("독일", "혼합"),
    "Germany Ministries": ("독일", "정부·부처"),
    "Ireland Ministries": ("아일랜드", "정부·부처"),
    "Greece Research Centers": ("그리스", "연구기관"),
    "벨기에완료": ("벨기에", "혼합"),
    "Mexico Ministries": ("멕시코", "정부·부처"),
    "덴마크완료": ("덴마크", "혼합"),
    "France Ministries": ("프랑스", "정부·부처"),
    "Greece Ministries": ("그리스", "정부·부처"),
    "Italy Ministries": ("이탈리아", "정부·부처"),
    "핀란드완료": ("핀란드", "혼합"),
    "Switzerland Departments": ("스위스", "정부·부처"),
    "Spain Ministries": ("스페인", "정부·부처"),
    "Estonia Research Centers": ("에스토니아", "연구기관"),
    "GER ResearchCenters(Unfinished)": ("독일", "연구기관"),
    "Italy Research centers": ("이탈리아", "연구기관"),
    "Canada Ministries": ("캐나다", "정부·부처"),
    "Norway Ministries": ("노르웨이", "정부·부처"),
    "Slovenia Research Centers": ("슬로베니아", "연구기관"),
    "Estonia Ministries": ("에스토니아", "정부·부처"),
    "Spanish Research Centers": ("스페인", "연구기관"),
    "일본완료": ("일본", "혼합"),
    "Chile Ministries": ("칠레", "정부·부처"),
    "Chile Research Centers": ("칠레", "연구기관"),
    "Netherland Ministries": ("네덜란드", "정부·부처"),
    "Portugal Ministries": ("포르투갈", "정부·부처"),
    "Polish Research Centers": ("폴란드", "연구기관"),
}


def load_universe():
    d = json.load(open(AUD / "scroll_index.json"))
    seen = set()
    hosts = []
    host_sheet = {}
    host_name = {}
    host_slug = {}
    for e in d["entries"]:
        h = norm(e.get("url") or "")
        if not h:
            continue
        if h not in seen:
            seen.add(h)
            hosts.append(h)
            host_sheet[h] = e["sheet"]
            host_name[h] = e.get("name") or ""
            host_slug[h] = e.get("host_slug") or ""
    return hosts, host_sheet, host_name, host_slug


def load_db_counts():
    con = sqlite3.connect(f"file:{DB}?mode=ro&immutable=1", uri=True)
    host_sites = defaultdict(list)
    for sid, url in con.execute("select site_id, site_url from sites"):
        host_sites[norm(url or "")].append(sid)
    docs = dict(con.execute("select site_id, count(*) from documents group by site_id"))
    pdfs = dict(con.execute("select site_id, count(*) from documents where pdf_downloaded=1 group by site_id"))
    txts = dict(con.execute("select site_id, count(*) from documents where text_extracted=1 group by site_id"))
    con.close()
    dcount, pcount, tcount = {}, {}, {}
    for h, sids in host_sites.items():
        dcount[h] = sum(docs.get(s, 0) for s in sids)
        pcount[h] = sum(pdfs.get(s, 0) for s in sids)
        tcount[h] = sum(txts.get(s, 0) for s in sids)
    return dcount, pcount, tcount


def main():
    hosts, host_sheet, host_name, host_slug = load_universe()
    dcount, pcount, tcount = load_db_counts()
    db_hosts = set(h for h in dcount if dcount[h] > 0)

    collected = [h for h in hosts if h in db_hosts]
    missing = [h for h in hosts if h not in db_hosts]

    # reason sources
    reaudit = {}
    for r in csv.DictReader(open(AUD / "reaudit_final_20260718.csv", encoding="utf-8-sig")):
        reaudit[r["host"].strip().lower()] = r
    master = {}
    with open(AUD / "uncollected_sites_reasons.csv", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            master[r["host"].strip().lower()] = r
    mv7_slugs = list(json.load(open(AUD / "manual_verify_7_20260719.json")).keys())
    slug2host = {}
    for h in hosts:
        s = host_slug.get(h)
        if s:
            slug2host.setdefault(s, h)
    mv7 = set()
    for k in mv7_slugs:
        if k in slug2host:
            mv7.add(slug2host[k]); continue
        parts = k.split("-")
        for i in range(len(parts), 0, -1):
            c = "-".join(parts[:i])
            if c in slug2host:
                mv7.add(slug2host[c]); break
    robots = set(norm("http://" + r) for r in json.load(open(AUD / "robots_allow_hosts.json")))

    def parents(h):
        p = h.split(".")
        return [".".join(p[i:]) for i in range(1, len(p) - 1)]

    # ---- classify a missing host into a client bucket + reason texts ----
    def classify(h):
        # bucket, why, howfix, chance
        if h in mv7:
            return ("수동확인 필요",
                    "재실사에서 크롤러가 목록/본문을 0건으로 반환해 사람이 직접 페이지 구조를 확인해야 함",
                    "담당자가 목록/상세 셀렉터를 수동 점검하거나 JS 렌더링(playwright) 적용",
                    "중간")
        r = reaudit.get(h)
        if r:
            v = r.get("재실사분류", "")
            if v.startswith("부분차단"):
                return ("부분차단(목록/본문 일부)",
                        "일부 페이지(목록 또는 본문/PDF)만 차단되어 전체 수집이 막힘",
                        "차단되지 않는 경로만 부분 수집하거나 쿠키/렌더링 보강",
                        "중간")
            if v == "여전히불가(접속불능)":
                return ("접속불능(소멸·차단)",
                        "재실사에서도 접속 불가(도메인 소멸/서버 차단/타임아웃) 상태 유지",
                        "사이트 복구 시 재시도 또는 새 URL 확보",
                        "낮음")
            if v == "여전히차단(봇차단)":
                return ("봇차단(로그인·봇검증)",
                        "재실사에서도 봇 차단(Cloudflare/차단 정책) 유지로 자동 접근 불가",
                        "사람이 챌린지 통과 후 쿠키를 브라우저에서 추출해 주입",
                        "중간")
            if v.startswith("JS후") or v.startswith("본문접근실패"):
                return ("수동확인 필요",
                        "JS 렌더링 후에도 유효 후보가 없어 사람이 직접 확인 필요",
                        "담당자가 페이지 구조/내부 API를 수동 점검",
                        "중간")
            if v == "robots금지(수집자제유지)":
                return ("접속불능(소멸·차단)",
                        "robots.txt 자동수집 거부(허가 확인 전) 상태",
                        "기관 허가 확인 후 수집 재개",
                        "낮음")
            if v.startswith("수집가능"):
                return ("자동수집 미완(0건)",
                        "재실사에서는 수집 가능으로 판정됐으나 크롤러가 아직 0건 -- 재생성 대기",
                        "목록/본문 셀렉터 보강 또는 JS 렌더링으로 크롤러 재생성",
                        "높음")
        m = master.get(h)
        if m:
            cat = m.get("분류", "")
            why = m.get("왜_수집이_안되었나", "") or ""
            fix = m.get("남은_회복_방법", "") or ""
            ch = m.get("회복_가능성", "") or ""
            if "사람개입필요" in cat:
                return ("봇차단(로그인·봇검증)", why, fix, ch)
            if "절대불가" in cat:
                return ("접속불능(소멸·차단)", why, fix, ch)
            if "재시도실패" in cat:
                return ("자동수집 미완(0건)", why or "자동 크롤러 생성이 0건으로 실패", fix, ch)
            if "정책상포기" in cat:
                return ("봇차단(로그인·봇검증)", why or "봇검증/정책으로 자동수집 보류", fix, ch)
            if "미분류" in cat:
                return ("수동확인 필요", why or "분류 미확정 -- 수동 확인 필요", fix, ch)
            if "수집완료" in cat:
                return ("자동수집 미완(0건)", "이전 수집완료로 표시됐으나 현재 DB에 문서 0건 -- 재수집 대기", fix, ch)
        # no record: subdomain of collected portal vs orphan
        if any(p in db_hosts for p in parents(h)):
            par = next(p for p in parents(h) if p in db_hosts)
            return ("상위도메인 통합수집",
                    f"이 호스트의 콘텐츠는 상위도메인({par})에 통합 수집되어 있어 개별 호스트로는 0건",
                    "중복 -- 상위도메인 수집분으로 커버됨(추가 작업 불필요)",
                    "해당없음")
        return ("자동수집 미완(0건)",
                "완료 대상 사이트이나 현재 DB에 문서 0건 -- 크롤러 미생성/재생성 대기",
                "목록/본문 셀렉터로 크롤러 생성 또는 JS 렌더링 적용",
                "높음")

    miss_rows = []
    for h in missing:
        country, field = SHEET_MAP.get(host_sheet.get(h, ""), ("기타", "기타"))
        bucket, why, fix, chance = classify(h)
        miss_rows.append({
            "host": h, "country": country, "field": field, "sheet": host_sheet.get(h, ""),
            "bucket": bucket, "why": why, "fix": fix, "chance": chance,
        })

    bucket_counts = Counter(r["bucket"] for r in miss_rows)

    # ---------------- XLSX ----------------
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    hdr_font = Font(bold=True, color="FFFFFF")
    hdr_fill = PatternFill("solid", fgColor="2F5496")

    ws1 = wb.active
    ws1.title = f"수집성공({len(collected)})"
    cols1 = ["순번", "사이트주소", "국가", "분야", "수집문서수", "PDF수", "본문추출수", "어떻게 수집되었나"]
    ws1.append(cols1)
    coll_sorted = sorted(collected, key=lambda h: dcount[h], reverse=True)
    for i, h in enumerate(coll_sorted, 1):
        country, field = SHEET_MAP.get(host_sheet.get(h, ""), ("기타", "기타"))
        how = "자동 수집 로봇으로 정상 수집됨"
        if h in robots:
            how = "기관 허가(2026-07-19) 후 자동 수집됨"
        ws1.append([i, h, country, field, dcount.get(h, 0), pcount.get(h, 0), tcount.get(h, 0), how])

    ws2 = wb.create_sheet(f"미수집({len(missing)})")
    cols2 = ["순번", "사이트주소", "국가", "분류", "왜 수집이 안되었나", "해결 방법", "해결 가능성"]
    ws2.append(cols2)
    bucket_order = ["봇차단(로그인·봇검증)", "접속불능(소멸·차단)", "부분차단(목록/본문 일부)",
                    "수동확인 필요", "자동수집 미완(0건)", "상위도메인 통합수집"]
    miss_sorted = sorted(miss_rows, key=lambda r: (bucket_order.index(r["bucket"]) if r["bucket"] in bucket_order else 99, r["host"]))
    for i, r in enumerate(miss_sorted, 1):
        ws2.append([i, r["host"], r["country"], r["bucket"], r["why"], r["fix"], r["chance"]])

    for ws in (ws1, ws2):
        for c in range(1, ws.max_column + 1):
            cell = ws.cell(row=1, column=c)
            cell.font = hdr_font
            cell.fill = hdr_fill
            cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        for c in range(1, ws.max_column + 1):
            L = get_column_letter(c)
            width = max((len(str(ws.cell(row=r, column=c).value or "")) for r in range(1, min(ws.max_row, 200) + 1)), default=8)
            ws.column_dimensions[L].width = min(max(width + 2, 10), 60)

    xlsx_path = AUD / "수집현황_전체.xlsx"
    wb.save(xlsx_path)

    # ---------------- 미수집_사이트_사유.csv (cp949) ----------------
    reason_path = AUD / "미수집_사이트_사유.csv"
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["순번", "분류", "host", "국가", "분야", "왜_수집이_안되었나", "해결_방법", "해결_가능성"])
    for i, r in enumerate(miss_sorted, 1):
        w.writerow([i, sanitize(r["bucket"]), r["host"], sanitize(r["country"]), sanitize(r["field"]),
                    sanitize(r["why"]), sanitize(r["fix"]), sanitize(r["chance"])])
    reason_path.write_bytes(buf.getvalue().encode("cp949", errors="replace"))

    # ---------------- 수집현황_요약.csv (cp949) ----------------
    # per-field collected stats
    field_stat = defaultdict(lambda: [0, 0, 0, 0])  # sites, docs, pdf, text
    for h in collected:
        _, field = SHEET_MAP.get(host_sheet.get(h, ""), ("기타", "기타"))
        s = field_stat[field]
        s[0] += 1; s[1] += dcount.get(h, 0); s[2] += pcount.get(h, 0); s[3] += tcount.get(h, 0)

    buf2 = io.StringIO()
    w2 = csv.writer(buf2)
    total = len(hosts)
    rate = f"{len(collected) / total * 100:.1f}%"
    w2.writerow(["[ 전체 요약 ]"])
    w2.writerow(["전체 사이트", "수집 성공", "미수집", "수집률"])
    w2.writerow([total, len(collected), len(missing), rate])
    w2.writerow([])
    w2.writerow(["[ 수집 성공 - 분야별 ]"])
    w2.writerow(["분야", "사이트수", "수집문서수", "PDF수", "본문추출수"])
    tot = [0, 0, 0, 0]
    for f_, s in sorted(field_stat.items(), key=lambda kv: kv[1][1], reverse=True):
        w2.writerow([sanitize(f_), s[0], s[1], s[2], s[3]])
        for j in range(4):
            tot[j] += s[j]
    w2.writerow(["합계", tot[0], tot[1], tot[2], tot[3]])
    w2.writerow([])
    w2.writerow(["[ 미수집 - 분류별 (현재 DB 기준) ]"])
    w2.writerow(["분류", "사이트수", "설명"])
    bucket_desc = {
        "봇차단(로그인·봇검증)": "Cloudflare/로그인/봇검증 벽으로 자동 접근 불가",
        "접속불능(소멸·차단)": "도메인 소멸/서버 차단/타임아웃",
        "부분차단(목록/본문 일부)": "목록 또는 본문/PDF 일부만 차단",
        "수동확인 필요": "크롤러 0건 반환 -- 사람 직접 확인 필요",
        "자동수집 미완(0건)": "완료 대상이나 현재 DB 문서 0건 -- 크롤러 재생성 대기",
        "상위도메인 통합수집": "상위도메인 수집분에 콘텐츠 포함(중복 -- 추가작업 불필요)",
    }
    for b in bucket_order:
        if bucket_counts.get(b):
            w2.writerow([sanitize(b), bucket_counts[b], sanitize(bucket_desc.get(b, ""))])
    w2.writerow(["합계", sum(bucket_counts.values()), ""])
    w2.writerow([])
    w2.writerow(["[ 참고: robots 허가(2026-07-19) ]"])
    robots_collected = len([h for h in robots if h in db_hosts])
    w2.writerow(["허가 기관", "수집 완료", "잔여"])
    w2.writerow([len(robots), robots_collected, len(robots) - robots_collected])
    buf2.getvalue()
    reason2 = buf2.getvalue().encode("cp949", errors="replace")
    (AUD / "수집현황_요약.csv").write_bytes(reason2)

    # ---------------- print summary (for report) ----------------
    print("COLLECTED", len(collected))
    print("MISSING", len(missing))
    print("BUCKETS", dict(bucket_counts))
    print("ROBOTS collected", robots_collected, "/", len(robots))
    print("FIELD_STAT", {k: v for k, v in field_stat.items()})
    # persist small json for md builder
    (AUD / "_regen_stats.json").write_text(json.dumps({
        "total": total, "collected": len(collected), "missing": len(missing),
        "rate": rate, "buckets": dict(bucket_counts),
        "robots_total": len(robots), "robots_collected": robots_collected,
        "field_stat": {k: v for k, v in field_stat.items()},
        "doc_total": sum(dcount.get(h, 0) for h in collected),
        "pdf_total": sum(pcount.get(h, 0) for h in collected),
        "text_total": sum(tcount.get(h, 0) for h in collected),
    }, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
