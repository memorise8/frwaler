#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""libertree.db 메타데이터 정제 배치 (2026-07-11 설계 스펙 구현).

규칙: R1 엔티티, R2 태그/CDATA, R3 키워드 중복, R4 개행/공백,
      R5 날짜 정규화, R6 URL 정리.
기본 dry-run. --apply 시에만 UPDATE. FTS는 documents_au 트리거가 동기화.
"""
import argparse
import html
import json
import re
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

from dateutil import parser as _dateparser

# 태그는 <문자 또는 </문자 로 시작할 때만 (p<0.05 같은 부등호 보존)
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_WS_RE = re.compile(r"\s+")

_MONTHS_FR = {
    "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "décembre": 12,
    "decembre": 12,
}

# DD/MM 해석이 맞는 유럽식 표기 사이트 (진단에서 확인된 곳 위주; 필요 시 추가)
DAYFIRST_SITES = {
    "defense-gouv-fr-salle-de-presse",
    "caissedesdepots-fr-communiques-de-press",
    "presse-economie-gouv-fr",
    "hud-govt-nz-stats-and-insights",
}

_YMD_SEP_RE = re.compile(r"^(\d{4})[./](\d{1,2})[./](\d{1,2})$")
_TEXT_MONTH_RE = re.compile(r"^(\d{1,2})\s+([A-Za-zÀ-ÖØ-öø-ÿ]+)\.?\s+(\d{2,4})$")


def _unescape_repeat(s: str, max_rounds: int = 3) -> str:
    """이중 인코딩(&amp;#8211;)까지 풀되 무한루프 방지."""
    out = s
    for _ in range(max_rounds):
        nxt = html.unescape(out)
        if nxt == out:
            break
        out = nxt
    return out


def clean_text(s, tag_repl=" "):
    """R1+R2+R4: CDATA 제거 -> 태그 스트립 -> 엔티티 디코드 -> 공백 정리.

    tag_repl: 태그를 치환할 문자열. title/abstract는 공백(" "), 키워드는
    빈 문자열("")로 호출해 `CO<sub>2</sub>` 같은 인라인 태그가 토큰을
    쪼개지 않도록 한다.
    """
    if not s:
        return s
    out = _CDATA_RE.sub(r"\1", s)
    out = _TAG_RE.sub(tag_repl, out)
    out = _unescape_repeat(out)
    # 엔티티 디코드가 새 태그를 만들 수 있어 한 번 더
    out = _TAG_RE.sub(tag_repl, out)
    result = _WS_RE.sub(" ", out).strip()
    return result if result != s else s


def clean_keywords(s):
    """R3: clean_text 후 콤마 토큰 중복 제거(대소문자 무시, 첫 표기 유지)."""
    if not s:
        return s
    cleaned = clean_text(s, tag_repl="")
    seen, toks = set(), []
    for tok in cleaned.split(","):
        t = tok.strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            toks.append(t)
    result = ", ".join(toks)
    return result if result != s else s


def _fmt(y: int, m: int, d: int):
    try:
        dt = datetime(y, m, d)
    except ValueError:
        return None
    if not (1800 <= dt.year <= 2030):
        return None
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def normalize_date(value, dayfirst: bool = False):
    """R5: 다양한 날짜 표기를 YYYY-MM-DD로 정규화.

    파싱 불가/범위 밖(1800~2030)이면 None (호출부가 원값 유지 + 리포트).
    """
    v = (value or "").strip().rstrip(".").strip()
    if not v:
        return None
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", v)  # 이미 ISO(시각 붙은 것 포함)
    if m:
        return _fmt(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _YMD_SEP_RE.match(v)  # 2014.10.24 / 2025/11/24
    if m:
        return _fmt(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = _TEXT_MONTH_RE.match(v)  # "01 août 2025" / "1 Feb 24" / "18 March 2026"
    if m:
        day, mon_word, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        if year < 100:
            year += 2000
        mon = _MONTHS_FR.get(mon_word)
        if mon:
            return _fmt(year, mon, day)
        # 영어 월명은 dateutil에 위임 (아래 fallback)
    try:
        # 서로 다른 default(연/월/일이 각기 구별되는 두 값)로 두 번 파싱해
        # 결과가 다르면 입력에 없던 성분이 default로 채워진 것 → 불신(None).
        # 월/연도만 있는 입력(예: "01/2005", "August 2025")의 day=01 날조를 막는다.
        dt1 = _dateparser.parse(v, dayfirst=dayfirst, fuzzy=False,
                                default=datetime(1600, 1, 1))
        dt2 = _dateparser.parse(v, dayfirst=dayfirst, fuzzy=False,
                                default=datetime(1601, 2, 2))
        if (dt1.year, dt1.month, dt1.day) != (dt2.year, dt2.month, dt2.day):
            return None
        if dt1.year == 1600:  # 연도 없는 입력 가드(위 비교로도 걸리지만 안전망 유지)
            return None
        return _fmt(dt1.year, dt1.month, dt1.day)
    except (ValueError, OverflowError):
        return None


def fix_pdf_url(pdf_url, meta_url):
    """R6: pdf_url 정리.

    http(s) 그대로 유지("ok"), 상대경로는 meta_url 기준 절대화
    ("absolutized"), ftp는 보존만 하고 리포트("kept_ftp"), 그 외
    (인용/DOI 텍스트 등)는 None으로 null 처리("nulled").
    """
    v = (pdf_url or "").strip()
    if not v:
        return (pdf_url, "ok")
    if v.startswith(("http://", "https://")):
        return (v, "ok")
    if v.startswith("ftp://"):
        return (v, "kept_ftp")
    if v.startswith("/") and (meta_url or "").startswith(("http://", "https://")):
        return (urljoin(meta_url, v), "absolutized")
    return (None, "nulled")


def fix_meta_url(meta_url):
    """R6: meta_url 정리. 'ERROR'는 null 처리, 나머지 비http는 리포트만."""
    v = (meta_url or "").strip()
    if v == "ERROR":
        return (None, "nulled")
    return (meta_url, "kept" if not v.startswith(("http://", "https://")) else "ok")


_NON_ISO_WHERE = (
    "{col} IS NOT NULL AND {col} != '' "
    "AND {col} NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' "
    "AND {col} NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' "
    "AND {col} NOT GLOB '[0-9][0-9][0-9][0-9]'"
)


def _rule_text(conn, apply, col, where):
    rep = {"candidates": 0, "changed": 0, "samples": []}
    fn = clean_keywords if col == "keywords" else clean_text
    for seq_id, val in conn.execute(
            f"SELECT seq_id, {col} FROM documents WHERE {where}").fetchall():
        rep["candidates"] += 1
        new = fn(val)
        if new != val:
            rep["changed"] += 1
            if len(rep["samples"]) < 5:
                rep["samples"].append({"seq_id": seq_id, "old": val[:80], "new": (new or "")[:80]})
            if apply:
                conn.execute(f"UPDATE documents SET {col}=? WHERE seq_id=?", (new, seq_id))
    if apply:
        conn.commit()
    return rep


def _rule_date(conn, apply, col):
    rep = {"candidates": 0, "changed": 0, "unparsed": 0, "samples": []}
    where = _NON_ISO_WHERE.format(col=col)
    for seq_id, site_id, val in conn.execute(
            f"SELECT seq_id, site_id, {col} FROM documents WHERE {where}").fetchall():
        rep["candidates"] += 1
        new = normalize_date(val, dayfirst=site_id in DAYFIRST_SITES)
        if new is None:
            rep["unparsed"] += 1
            if len(rep["samples"]) < 10:
                rep["samples"].append({"seq_id": seq_id, "kept": val[:40]})
            continue
        if new != val:
            rep["changed"] += 1
            if apply:
                conn.execute(f"UPDATE documents SET {col}=? WHERE seq_id=?", (new, seq_id))
    if apply:
        conn.commit()
    return rep


def _rule_url(conn, apply):
    rep = {"candidates": 0, "changed": 0, "kept_ftp": 0, "samples": []}
    for seq_id, meta_url, pdf_url in conn.execute(
            "SELECT seq_id, meta_url, pdf_url FROM documents WHERE meta_url='ERROR'"
            " OR (pdf_url IS NOT NULL AND pdf_url != '' AND pdf_url NOT LIKE 'http%')"
    ).fetchall():
        rep["candidates"] += 1
        new_meta, meta_verdict = fix_meta_url(meta_url)
        new_pdf, pdf_verdict = fix_pdf_url(pdf_url, meta_url)
        if pdf_verdict == "kept_ftp":
            rep["kept_ftp"] += 1
        # 값 비교로 "changed"를 판단한다 (verdict 문자열만으로는 안 됨):
        # 예) http(s) pdf_url에 공백이 섞인 경우 fix_pdf_url은 strip된 값을
        # verdict="ok"로 돌려주는데, verdict만 보면 "안 바뀜"으로 오판해
        # 매번 WHERE에 다시 걸려 무한 재선택될 수 있다.
        changed = (new_meta != meta_url) or (new_pdf != pdf_url)
        if changed:
            rep["changed"] += 1
            if len(rep["samples"]) < 10:
                rep["samples"].append({"seq_id": seq_id, "pdf": pdf_verdict, "meta": meta_verdict})
            if apply:
                conn.execute("UPDATE documents SET meta_url=?, pdf_url=? WHERE seq_id=?",
                             (new_meta, new_pdf, seq_id))
    if apply:
        conn.commit()
    return rep


_TITLE_WHERE = ("title LIKE '%&#%' OR title LIKE '%&amp;%' OR title LIKE '%&lt;%'"
                " OR title LIKE '%<a %' OR title LIKE '%<span%' OR title LIKE '%<br%'"
                " OR title LIKE '%' || CHAR(10) || '%'")
_ABSTRACT_WHERE = ("abstract LIKE '%<div%' OR abstract LIKE '%<span%'"
                   " OR abstract LIKE '%<a href%' OR abstract LIKE '%<br%'"
                   " OR abstract LIKE '%<![CDATA[%' OR abstract LIKE '%&amp;%'"
                   " OR abstract LIKE '%&#%'")
_KEYWORDS_WHERE = "keywords LIKE '%<%' OR keywords LIKE '%&amp%'"


def run_cleanup(conn, apply: bool) -> dict:
    return {
        "title": _rule_text(conn, apply, "title", _TITLE_WHERE),
        "abstract": _rule_text(conn, apply, "abstract", _ABSTRACT_WHERE),
        "keywords": _rule_text(conn, apply, "keywords", _KEYWORDS_WHERE),
        "listed_date": _rule_date(conn, apply, "listed_date"),
        "published_date": _rule_date(conn, apply, "published_date"),
        "url": _rule_url(conn, apply),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="실제 UPDATE 수행 (기본 dry-run)")
    ap.add_argument("--db", default="data/libertree.db")
    args = ap.parse_args()

    db = Path(args.db)
    if args.apply:
        from datetime import datetime as _dt
        bak = db.with_name(db.name + f".bak-{_dt.now():%Y%m%d-%H%M}-precleanup")
        if not any(db.parent.glob(db.name + ".bak-*-precleanup")):
            print(f"backing up -> {bak}")
            shutil.copy2(db, bak)
    conn = sqlite3.connect(db)
    report = run_cleanup(conn, apply=args.apply)
    from datetime import datetime as _dt
    out = Path(f"data/audit/cleanup_metadata_{_dt.now():%Y%m%d_%H%M%S}"
               f"{'_apply' if args.apply else '_dryrun'}.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    for rule, r in report.items():
        print(f"{rule:16s} candidates={r['candidates']:6d} changed={r['changed']:6d}"
              + (f" unparsed={r['unparsed']}" if "unparsed" in r else ""))
    print(f"report -> {out}")


if __name__ == "__main__":
    main()
