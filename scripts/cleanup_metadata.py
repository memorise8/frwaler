#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""libertree.db 메타데이터 정제 배치 (2026-07-11 설계 스펙 구현).

규칙: R1 엔티티, R2 태그/CDATA, R3 키워드 중복, R4 개행/공백,
      R5 날짜 정규화, R6 URL 정리.
기본 dry-run. --apply 시에만 UPDATE. FTS는 documents_au 트리거가 동기화.
"""
import html
import re

# 태그는 <문자 또는 </문자 로 시작할 때만 (p<0.05 같은 부등호 보존)
_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_WS_RE = re.compile(r"\s+")


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
