"""공표 PDF → 문단 파서 (KASB 조문 API가 없는 기준서용).

`fetch.py` 의 조문 API(db.kasb.or.kr `/api/title/{std_num}`)가 빈 응답을 주는
기준서가 있다. 2026-08 기준 제1118호 '재무제표 표시와 공시'(2025-12-18 공표,
2027 시행·2026 조기적용)가 그렇다. 목록 페이지에는 등재돼 있으나 조문 DB에는
아직 올라오지 않아 `collect.py` 가 skip 한다.

이 모듈은 그런 기준서를 KASB 공표 PDF에서 문단 단위로 복원한다. 출력은
`parsers.py` 와 같은 `ParagraphRecord` 라서 `db.py`/`export_ndjson.py` 를
그대로 탄다.

정확도는 API 판이 있는 기준서로 측정한다 (`tests/test_fino_std_pdf_parse.py`
및 제1117호 대조 기준 번호 리콜 99.3% / 본문 시작일치 99.7% / bleed 0.0%).

빈 줄로 나눈 '블록' 단위로 판정하는 것이 핵심이다. 줄 단위로 보면 문단 중간의
줄바꿈과 절 제목을 구분할 수 없어, 제목을 본문에 흡수하거나(bleed) 반대로
문단을 중간에서 잘라먹는다.
"""

from __future__ import annotations

import re
import subprocess
from collections import Counter
from pathlib import Path

from .models import ParagraphRecord

# 문단번호: 본문(1, 28A, 한3.1) · 부록B(B65) · 부록C(C1) · 결론도출근거(BC407) · 적용사례(IE93)
PARA_NUM = re.compile(
    r"^\s{0,8}("
    r"한?\d+[A-Z]{0,2}(?:\.\d+)?"
    r"|BC\d+[A-Z]{0,2}"
    r"|B\d+[A-Z]{0,2}"
    r"|IE\d+[A-Z]{0,2}"
    r"|C\d+[A-Z]{0,2}"
    r")\s{2,}(\S.*)$"
)

_PAGE_NO = re.compile(r"^\s*-\s*\d+\s*-\s*$")
_FOOTNOTE = re.compile(r"^\s*\d{1,3}\)\s*\S")
_APPENDIX = re.compile(r"^\s*부록\s*([A-Z])\s*[.·]?\s*(\S.*)?$")
_TABLE_ROW = re.compile(r"\S\s{3,}\S.*?\s{3,}\S")
_TABLE_UNIT = re.compile(r"^\s*\(단위\s*:")
_SUB_ITEM = re.compile(r"^\s*[⑴-⒇㈎-㈜①-⑳]")

# 문장이 끝난 것으로 보는 어미·부호. 이걸로 끝나지 않으면 다음 블록은 이어지는 중이다.
_SENT_END = re.compile(
    r"(다|함|음|것|참조|같다|한다|된다|이다)[.\)”’\"']*\s*$|[.:;][\)”’\"']*\s*$"
)

_HEADING_MAX = 40


def pdf_to_lines(pdf_path: str | Path) -> list[str]:
    """pdftotext -layout 결과를 줄 목록으로. 열 정렬이 유지돼야 표를 판별할 수 있다."""
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True,
        check=True,
        timeout=300,
    )
    return result.stdout.decode("utf-8", "ignore").split("\n")


def _blocks(lines: list[str]) -> list[list[str]]:
    """빈 줄과 페이지번호를 경계로 블록을 만든다."""
    out: list[list[str]] = []
    cur: list[str] = []
    for line in lines:
        if not line.strip() or _PAGE_NO.match(line):
            if cur:
                out.append(cur)
                cur = []
            continue
        cur.append(line)
    if cur:
        out.append(cur)
    return out


def _text(block: list[str]) -> str:
    return " ".join(l.strip() for l in block)


def _is_table(block: list[str]) -> bool:
    if any(_TABLE_UNIT.match(l) for l in block):
        return True
    hits = sum(1 for l in block if _TABLE_ROW.search(l))
    return hits >= max(1, len(block) // 2)


def _is_heading(block: list[str]) -> bool:
    """절 제목: 한두 줄짜리 짧은 블록이며 서술문으로 끝나지 않는다."""
    if len(block) > 2:
        return False
    s = _text(block)
    if len(s) > _HEADING_MAX or not re.search(r"[가-힣]", s):
        return False
    return not _SENT_END.search(s)


def _has_wide_gap(line: str) -> bool:
    """넓은 열 간격이 있으면 2단 표, 없으면 산문."""
    return bool(re.search(r"\S\s{3,}\S", line))


def _definition_column(lines: list[str]) -> int:
    """용어정의 2단 표에서 '정의' 컬럼이 시작하는 열을 추정한다.

    이어지는 정의 줄은 모두 같은 열에서 시작하므로 들여쓰기 최빈값이 경계가 된다.
    정의가 한 줄로 끝나 이어지는 줄이 없으면 용어와 정의 사이 공백 자리를 쓴다.
    """
    indents: Counter[int] = Counter()
    for line in lines:
        stripped = line.rstrip()
        if not stripped.strip():
            continue
        indent = len(stripped) - len(stripped.lstrip())
        if indent > 2:
            indents[indent] += 1
    if indents:
        return indents.most_common(1)[0][0]

    gaps: Counter[int] = Counter()
    for line in lines:
        m = re.search(r"\S\s{2,}(?=\S)", line)
        if m:
            gaps[m.end()] += 1
    return gaps.most_common(1)[0][0] if gaps else 0


def _glossary_page(lines: list[str], section: str) -> list[tuple[str, str]]:
    """한 페이지 분량의 용어정의 표 → [(용어, 정의)]."""
    col = _definition_column(lines)
    if col < 4:
        return []

    out: list[tuple[str, str]] = []
    term_buf: list[str] = []
    body_buf: list[str] = []

    def flush() -> None:
        term = " ".join(term_buf).strip()
        body = " ".join(body_buf).strip()
        if term and body:
            out.append((term, body))
        term_buf.clear()
        body_buf.clear()

    for raw in lines:
        line = raw.replace("\x0c", " ")
        if not line.strip():
            continue
        left, right = line[:col].strip(), line[col:].strip()
        if left:
            # 좌측 컬럼에 글자가 있으면 새 용어의 시작(또는 여러 줄 용어의 연속)
            if body_buf:
                flush()
            term_buf.append(left)
        if right:
            body_buf.append(right)
    flush()
    return out


def _merge_wrapped(entries: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """페이지 경계에서 잘린 정의를 앞 항목에 되붙인다.

    용어 자리에 기호나 조사 조각만 남은 것은 독립 항목이 아니라 이어지는 정의다.
    """
    out: list[tuple[str, str]] = []
    for term, body in entries:
        if (len(term) < 3 or _SUB_ITEM.match(term)) and out:
            prev_term, prev_body = out[-1]
            out[-1] = (prev_term, f"{prev_body} {term} {body}".strip())
        else:
            out.append((term, body))
    return out


def parse_glossary(lines: list[str], section: str) -> list[tuple[str, str]]:
    """부록 A(용어의 정의) 2단 표 → [(용어, 정의)].

    문단번호가 없는 영역이라 빈 줄이 아니라 컬럼 위치로 항목 경계를 잡는다.
    컬럼 위치는 페이지마다 달라지므로 페이지(\\x0c) 단위로 추정한다.
    """
    pages: list[list[str]] = [[]]
    for line in lines:
        if "\x0c" in line:
            pages.append([])
        pages[-1].append(line)

    entries: list[tuple[str, str]] = []
    for page in pages:
        entries.extend(_glossary_page(page, section))
    return _merge_wrapped(entries)


def parse_pdf(pdf_path: str | Path, source_url: str = "") -> list[ParagraphRecord]:
    """공표 PDF → ParagraphRecord 목록 (문서 순서 보존)."""
    return parse_lines(pdf_to_lines(pdf_path), source_url=source_url)


def parse_lines(lines: list[str], source_url: str = "") -> list[ParagraphRecord]:
    """pdftotext -layout 줄 목록 → ParagraphRecord 목록.

    용어정의 항목은 para_num 이 비고, section_path 끝에 용어가 붙는다.
    """
    records: list[ParagraphRecord] = []
    section = ""
    glossary_section = ""
    glossary_lines: list[str] = []
    in_glossary = False
    cur_num: str | None = None
    cur_section = ""
    cur_body = ""

    def close() -> None:
        nonlocal cur_num, cur_body
        if cur_num is not None and cur_body.strip():
            records.append(
                ParagraphRecord(
                    para_num=cur_num,
                    section_path=cur_section,
                    body_html="",
                    body_text=re.sub(r"\s+", " ", cur_body).strip(),
                    seq=len(records),
                    source_url=source_url,
                )
            )
        cur_num = None
        cur_body = ""

    def close_glossary() -> None:
        nonlocal in_glossary
        if in_glossary and glossary_lines:
            for term, body in parse_glossary(glossary_lines, glossary_section):
                records.append(
                    ParagraphRecord(
                        para_num="",
                        section_path=f"{glossary_section} > {term}".strip(" >"),
                        body_html="",
                        body_text=re.sub(r"\s+", " ", body).strip(),
                        seq=len(records),
                        source_url=source_url,
                    )
                )
        glossary_lines.clear()
        in_glossary = False

    for block in _blocks(lines):
        head = block[0]

        appendix = _APPENDIX.match(head)
        if appendix and len(_text(block)) < _HEADING_MAX:
            close()
            close_glossary()
            section = _text(block)
            if appendix.group(1) == "A" and "용어" in (appendix.group(2) or ""):
                in_glossary = True
                glossary_section = section
            continue

        numbered = PARA_NUM.match(head)
        if numbered:
            close()
            close_glossary()
            cur_num = numbered.group(1)
            cur_section = section
            cur_body = " ".join([numbered.group(2).strip()] + [l.strip() for l in block[1:]])
            continue

        if in_glossary:
            # 부록 도입문("이 부록은 …")은 용어 표가 아니다.
            if len(block) == 1 and not _has_wide_gap(head) and _SENT_END.search(_text(block)):
                continue
            glossary_lines.extend(block)
            continue

        # 번호 없는 블록: 문단이 이어지는 중인지, 흡수하면 안 되는 요소인지 가른다.
        if _FOOTNOTE.match(head) or _is_table(block):
            close()
            continue

        # 직전 문단이 문장 중간에서 끊겼으면 페이지·단 넘김으로 보고 이어붙인다.
        if cur_num is not None and not _SENT_END.search(cur_body):
            cur_body += " " + _text(block)
            continue

        # 종결된 문단 뒤의 ⑴⑵… 블록은 같은 문단의 하위 항목이다.
        # 짧으면 제목으로 오인되므로 제목 판정보다 먼저 본다.
        if cur_num is not None and _SUB_ITEM.match(head):
            cur_body += " " + _text(block)
            continue

        if _is_heading(block):
            close()
            section = _text(block)
            continue

        close()

    close()
    close_glossary()
    return records
