# -*- coding: utf-8 -*-
"""정확값 probe (count-only·무저장·읽기전용).

목적: sweep에서 2h 상한에 걸려 '하한값'으로만 남은 사이트, 그리고 counted<3 '의심'
사이트의 **정확한 총 게시물 수**를, 상세페이지를 하나도 받지 않고(=빠르게) 구한다.

원리 (2h 병목은 '항목마다 상세페이지 fetch'였음. 리스트만 세면 몇 분):
 1) 크롤러를 아주 잠깐 돌려 실제로 요청하는 URL들을 HTTP 계층 후킹으로 캡처
    → 리스트 URL 템플릿(증가하는 page 파라미터) + 상세 URL의 공통 경로를 자동 추출
 2) 리스트 page1 HTML에서 총량 신호 추출:
    a. '총 N건 / 전체 N / totalCount:N / N results' 등 총건수 텍스트  → 정확
    b. 페이지네이션의 최대 페이지 번호 → (maxpage-1)*per_page + 마지막페이지건수
       (per_page = 리스트 HTML에서 상세 경로 링크 개수)
 3) 신호가 전혀 없으면 method=none (하한값 유지, 별도 처리)

무저장: _save_paper·PDF 다운로드는 하지 않는다(캡처 단계에서 조기 중단, 이후엔 리스트 HTML만 GET).
"""
from __future__ import annotations
import csv, os, re, sys, time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

ROOT = Path("/data_raid/ruci_workspace/frwaler_job")
sys.path.insert(0, str(ROOT))

PAGE_PARAMS = ["pageIndex", "pageNo", "pageNum", "curPage", "currPage", "cpage",
               "nPage", "page", "pg", "p"]
# 총건수 텍스트 (한/영). 캡처 그룹 = 숫자(콤마 허용)
TOTAL_TEXT_RES = [
    re.compile(r"총\s*[:：]?\s*([\d,]{1,12})\s*건"),
    re.compile(r"전체\s*[:：]?\s*([\d,]{1,12})\s*건"),
    re.compile(r"게시물\s*[:：]?\s*([\d,]{1,12})\s*건"),
    re.compile(r"총\s*게시물[^0-9]{0,6}([\d,]{1,12})"),
    re.compile(r"total\s*Count[\"'\s:=]{0,6}([\d,]{1,12})", re.I),
    re.compile(r"totalCnt[\"'\s:=]{0,6}([\d,]{1,12})", re.I),
    re.compile(r"data-total[\"'\s:=]{0,6}([\d,]{1,12})", re.I),
    re.compile(r"([\d,]{2,12})\s*results", re.I),
    re.compile(r"([\d,]{2,12})\s*items", re.I),
]

# ---------------- HTTP 캡처 후킹 ----------------
import threading
# 캡처 상태는 스레드로컬 — 병렬 워커 간 교차오염 방지(전역 공유 금지)
_TL = threading.local()
CAP_N = 6  # 이만큼 캡처되면 조기중단


class _Stop(BaseException):
    # 크롤러 내부의 broad `except Exception`에 삼켜지지 않도록 BaseException 상속
    pass


def _caps():
    c = getattr(_TL, "caps", None)
    if c is None:
        c = _TL.caps = []
    return c


def _note(url):
    if getattr(_TL, "on", False) and isinstance(url, str) and url.startswith("http"):
        c = _caps()
        c.append(url)
        if len(c) >= CAP_N:
            raise _Stop()


_ORIG_RUN = None  # 패치 전 subprocess.run (fetch()가 후킹 우회용으로 사용)


def patch_http():
    global _ORIG_RUN
    import subprocess as sp
    _run = sp.run
    _ORIG_RUN = _run
    def run(cmd, *a, **k):
        try:
            toks = cmd if isinstance(cmd, (list, tuple)) else [cmd]
            for t in toks:
                if isinstance(t, str) and t.startswith("http"):
                    _note(t)
        except _Stop:
            raise
        except Exception:
            pass
        return _run(cmd, *a, **k)
    sp.run = run

    _popen = sp.Popen
    class Popen(_popen):
        def __init__(self, cmd, *a, **k):
            try:
                toks = cmd if isinstance(cmd, (list, tuple)) else [cmd]
                for t in toks:
                    if isinstance(t, str) and t.startswith("http"):
                        _note(t)
            except _Stop:
                raise
            except Exception:
                pass
            super().__init__(cmd, *a, **k)
    sp.Popen = Popen

    try:
        import requests
        _rq = requests.sessions.Session.request
        def rq(self, method, url, *a, **k):
            _note(url)
            return _rq(self, method, url, *a, **k)
        requests.sessions.Session.request = rq
    except Exception:
        pass

    try:
        import curl_cffi.requests as cc
        if hasattr(cc, "Session"):
            _ccr = cc.Session.request
            def ccr(self, method, url, *a, **k):
                _note(url)
                return _ccr(self, method, url, *a, **k)
            cc.Session.request = ccr
    except Exception:
        pass

    try:
        from crawler import stealth_fetcher as sf
        for mname in ("fetch", "get", "fetch_text", "fetch_html", "fetch_bytes", "request"):
            if hasattr(sf.StealthSession, mname):
                _o = getattr(sf.StealthSession, mname)
                def mk(_o):
                    def w(self, url, *a, **k):
                        _note(url)
                        return _o(self, url, *a, **k)
                    return w
                setattr(sf.StealthSession, mname, mk(_o))
    except Exception:
        pass


# ---------------- URL 분석 ----------------
def _qs(url):
    p = urlsplit(url)
    return p, dict(parse_qsl(p.query, keep_blank_values=True))


def _path_key(url):
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}{p.path}"


def analyze_captured(caps):
    """캡처 URL에서 (리스트 URL 템플릿, page 파라미터, 상세 공통경로) 추출."""
    from collections import Counter, defaultdict
    list_url = None
    page_param = None
    # 1) 알려진 page 파라미터를 값(숫자)으로 가진 URL = 리스트 후보
    for url in caps:
        p, q = _qs(url)
        for pp in PAGE_PARAMS:
            if pp in q and q[pp].isdigit():
                list_url, page_param = url, pp
                break
        if list_url:
            break
    # 2) 못 찾으면: 같은 path 로 2회 이상 호출된 URL 중, 숫자값이 서로 다른 키 = page 파라미터
    if not page_param:
        by_path = defaultdict(list)
        for u in caps:
            _, q = _qs(u)
            by_path[_path_key(u)].append(q)
        for pk, qs in by_path.items():
            if len(qs) < 2:
                continue
            keys = set().union(*[set(d) for d in qs])
            for k in keys:
                vals = [d.get(k) for d in qs]
                if all(v is not None and str(v).isdigit() for v in vals) and len(set(vals)) > 1:
                    page_param = k
                    for u in caps:
                        if _path_key(u) == pk:
                            list_url = u
                            break
                    break
            if page_param:
                break
    # 3) 그래도 없으면: 가장 많이 호출된 path = 리스트로 보고, 알려진 파라미터명을 기본 부여
    if not list_url and caps:
        pk = Counter(_path_key(u) for u in caps).most_common(1)[0][0]
        for u in caps:
            if _path_key(u) == pk:
                list_url = u
                break
        page_param = page_param or "pageIndex"
    list_path = _path_key(list_url) if list_url else None
    other = Counter(_path_key(u) for u in caps if _path_key(u) != list_path)
    detail_path = other.most_common(1)[0][0] if other else None
    return list_url, page_param, detail_path, list_path


def set_page(url, page_param, n):
    p, q = _qs(url)
    q[page_param] = str(n)
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), p.fragment))


# ---------------- HTML 신호 ----------------
def total_from_text(html):
    best = 0
    for rex in TOTAL_TEXT_RES:
        for m in rex.finditer(html):
            v = int(m.group(1).replace(",", ""))
            if v > best:
                best = v
    return best


_PAGE_FUNCS = ["goPage", "go_page", "linkPage", "fnLinkPage", "fnPage", "goPaging",
               "movePage", "fnMovePage", "pageMove", "gotoPage", "setPage", "fn_egov_link_page"]

def maxpage_from_html(html, page_param):
    """페이지네이션 최대 페이지 번호(참고신호). page_param= 및 흔한 JS 함수 인자에서 추출.
    주의: 한국식 '1~10블록' UI면 실제 마지막이 아닐 수 있어 참고용으로만 사용."""
    best = 0
    for m in re.finditer(re.escape(page_param) + r"['\"]?\s*[=:,(]\s*['\"]?(\d{1,7})", html):
        best = max(best, int(m.group(1)))
    for fn in _PAGE_FUNCS:
        for m in re.finditer(re.escape(fn) + r"\(\s*['\"]?(\d{1,7})", html):
            best = max(best, int(m.group(1)))
    return best


def count_detail_links(html, detail_path):
    if not detail_path:
        return 0
    # path 마지막 세그먼트(예: selectBbsNttView.do)로 카운트
    seg = detail_path.rstrip("/").split("/")[-1]
    if not seg:
        return 0
    return len(re.findall(re.escape(seg), html))


def _sig(html, detail_path):
    """페이지 내용 시그니처(무한루프/마지막페이지 반복 감지용): 상세링크 첫 등장 주변 문자열."""
    seg = detail_path.rstrip("/").split("/")[-1] if detail_path else ""
    i = html.find(seg) if seg else -1
    return html[i:i + 120] if i >= 0 else html[:120]


def list_walk(list_url, page_param, detail_path, wall_s=1800, cap_pages=30000, batch=6):
    """리스트만 끝까지 넘기며 페이지당 상세링크 수를 합산 = 정확한 총 게시물 수.
    상세페이지는 받지 않음. batch개 페이지를 동시 fetch 해 가속. 무한루프는 시그니처 반복으로 차단.
    반환: (total, completed, pages, note). completed=False면 wall/cap 도달(하한값)."""
    from concurrent.futures import ThreadPoolExecutor
    total = 0
    pages = 0
    prev_sig = None
    t0 = time.time()
    n = 1
    with ThreadPoolExecutor(max_workers=batch) as ex:
        while n <= cap_pages:
            if time.time() - t0 > wall_s:
                return total, False, pages, f"wall_{wall_s:.0f}s"
            bpages = list(range(n, min(n + batch, cap_pages + 1)))
            htmls = list(ex.map(lambda p: fetch(set_page(list_url, page_param, p)), bpages))
            for p, html in zip(bpages, htmls):
                if not html:
                    return total, True, pages, f"empty_p{p}"
                c = count_detail_links(html, detail_path)
                if c == 0:
                    return total, True, pages, f"no_items_p{p}"
                sig = _sig(html, detail_path)
                if sig == prev_sig:  # 마지막 페이지 이후 같은 내용 반복 → 종료
                    return total, True, pages, f"repeat_p{p}"
                prev_sig = sig
                total += c
                pages += 1
            n += batch
    return total, False, pages, f"cap_{cap_pages}p"


# ---------------- fetch (리스트 HTML만) ----------------
def fetch(url, timeout=30):
    import subprocess
    runner = _ORIG_RUN or subprocess.run  # 후킹 우회(캡처 안 됨)
    for ua in ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",):
        try:
            r = runner(
                ["curl", "-skL", "--max-time", str(timeout),
                 "-H", f"User-Agent: {ua}",
                 "-H", "Accept-Language: ko-KR,ko;q=0.9,en;q=0.8", url],
                capture_output=True, timeout=timeout + 5)
            if r.stdout:
                return r.stdout.decode("utf-8", errors="replace")
        except Exception:
            pass
    return ""


# ---------------- per-site probe ----------------
def probe_site(sid, walk_wall=900):
    import sqlite3
    from crawler.sites import CRAWLERS
    if sid not in CRAWLERS:
        return {"site_id": sid, "method": "no_crawler", "exact_total": "", "signal": "", "note": ""}
    DB = str(ROOT / "libertree-app/data/libertree.db")
    cls = CRAWLERS[sid]
    # 1) 동적 URL 캡처 (스레드로컬 — 워커 간 격리)
    _TL.caps = []
    try:
        conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        inst = cls(db_conn=conn, delay=0)
    except Exception as e:
        return {"site_id": sid, "method": "instantiate_fail", "exact_total": "", "signal": "", "note": str(e)[:80]}
    _TL.on = True
    try:
        if hasattr(inst, "_delay"):
            try:
                inst._delay = 0
            except Exception:
                pass
        inst.crawl(limit=None)
    except _Stop:
        pass
    except Exception as e:
        if not _caps():
            _TL.on = False
            return {"site_id": sid, "method": "capture_fail", "exact_total": "", "signal": "",
                    "note": f"{type(e).__name__}:{str(e)[:60]}"}
    finally:
        _TL.on = False
    caps = list(_caps())
    list_url, page_param, detail_path, list_path = analyze_captured(caps)
    if not list_url or not page_param:
        return {"site_id": sid, "method": "no_list_url", "exact_total": "", "signal": "",
                "note": f"caps={len(caps)}"}

    # 2) page1 HTML → 총량 신호
    url1 = set_page(list_url, page_param, 1)
    html1 = fetch(url1)
    if not html1:
        return {"site_id": sid, "method": "fetch_fail", "exact_total": "", "signal": "",
                "note": "list page1 empty"}

    ttext = total_from_text(html1)
    per_page = count_detail_links(html1, detail_path)
    maxpage = maxpage_from_html(html1, page_param)
    sig = f"text={ttext} per_page={per_page} maxpage={maxpage}"

    # a. 총건수 텍스트가 있고 per_page 이상이면 정확값으로 채택(지름길)
    if ttext and per_page and ttext >= per_page:
        return {"site_id": sid, "method": "total_text", "exact_total": ttext,
                "signal": sig, "note": f"list={list_path}"}

    # b. per_page 를 못 세면(상세경로 매칭 실패) walk 불가 → 텍스트/신호만
    if per_page == 0:
        if ttext:
            return {"site_id": sid, "method": "total_text_only", "exact_total": ttext,
                    "signal": sig, "note": f"list={list_path} detail={detail_path}"}
        return {"site_id": sid, "method": "no_per_page", "exact_total": "",
                "signal": sig, "note": f"list={list_path} detail={detail_path}"}

    # b2. maxpage 가 진짜 '마지막 페이지 링크'면 검증 후 역산(walk 회피, 3 fetch)
    #     검증: page=maxpage 는 항목 있고, page=maxpage+1 은 비었거나(=끝) 같은내용 반복.
    if maxpage >= 2 and per_page > 0:
        last_html = fetch(set_page(list_url, page_param, maxpage))
        last_cnt = count_detail_links(last_html, detail_path)
        beyond = fetch(set_page(list_url, page_param, maxpage + 1))
        beyond_cnt = count_detail_links(beyond, detail_path)
        if last_cnt > 0 and (beyond_cnt == 0 or _sig(beyond, detail_path) == _sig(last_html, detail_path)):
            total = (maxpage - 1) * per_page + last_cnt
            return {"site_id": sid, "method": "lastpage_verified", "exact_total": total,
                    "signal": f"maxpage={maxpage} per_page={per_page} last={last_cnt}",
                    "note": f"list={list_path}"}

    # c. 리스트 전용 walk = 정확 총량(상세 안 받음)
    total, completed, pages, wnote = list_walk(list_url, page_param, detail_path, wall_s=walk_wall)
    method = "list_walk" if completed else "list_walk_partial"
    return {"site_id": sid, "method": method, "exact_total": total,
            "signal": f"pages={pages} {sig} {wnote}",
            "note": f"list={list_path}"}


def main():
    import argparse
    from concurrent.futures import ThreadPoolExecutor, as_completed
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", required=True)
    ap.add_argument("--out-csv", default=str(ROOT / "scripts/audit/exact_probe.csv"))
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--walk-wall", type=float, default=900, help="사이트별 walk 최대 시간(s)")
    ap.add_argument("--resume", action="store_true", help="이미 값 확보된 site_id 건너뜀")
    args = ap.parse_args()

    patch_http()
    fields = ["site_id", "method", "exact_total", "signal", "note"]
    out = Path(args.out_csv)
    write_lock = threading.Lock()
    existing = {}
    if out.exists():
        for r in csv.DictReader(open(out, encoding="utf-8")):
            existing[r["site_id"]] = r

    # 성공 판정: exact_total 이 숫자면 확보됨
    def has_value(sid):
        r = existing.get(sid)
        return r and str(r.get("exact_total", "")).strip().isdigit()

    todo = [s for s in dict.fromkeys(args.only) if not (args.resume and has_value(s))]
    print(f"[exact] 대상 {len(todo)} / 전체 {len(args.only)} "
          f"(workers {args.workers}, walk_wall {args.walk_wall}s)", flush=True)

    def flush_csv():
        tmp = out.with_suffix(".csv.tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            for rr in existing.values():
                w.writerow({k: rr.get(k, "") for k in fields})
        os.replace(tmp, out)

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(probe_site, sid, args.walk_wall): sid for sid in todo}
        for fut in as_completed(futs):
            sid = futs[fut]
            try:
                row = fut.result()
            except Exception as e:
                row = {"site_id": sid, "method": "error", "exact_total": "", "signal": "",
                       "note": f"{type(e).__name__}:{str(e)[:80]}"}
            done += 1
            with write_lock:
                existing[sid] = row
                flush_csv()
            print(f"[{done}/{len(todo)}] {sid:<34} {row['method']:<16} "
                  f"total={row['exact_total']}  {row['signal']}", flush=True)


if __name__ == "__main__":
    main()
