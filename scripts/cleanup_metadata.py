#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""libertree.db 메타데이터 정제 배치 (2026-07-11 설계 스펙 구현).

규칙: R1 엔티티, R2 태그/CDATA, R3 키워드 중복, R4 개행/공백,
      R5 날짜 정규화, R6 URL 정리.
기본 dry-run. --apply 시에만 UPDATE. FTS는 documents_au 트리거가 동기화.
"""
import html
import re
from datetime import datetime

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
_TEXT_MONTH_RE = re.compile(r"^(\d{1,2})\s+([A-Za-zà-ÿ]+)\.?\s+(\d{2,4})$")


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
        dt = _dateparser.parse(v, dayfirst=dayfirst, fuzzy=False,
                               default=datetime(1600, 1, 1))
        if dt.year == 1600:  # 연도 없는 입력이 default로 채워진 것 → 불신
            return None
        return _fmt(dt.year, dt.month, dt.day)
    except (ValueError, OverflowError):
        return None
