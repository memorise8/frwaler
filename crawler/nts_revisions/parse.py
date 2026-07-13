from __future__ import annotations

import re

from .models import Revision

_CASE_RE = re.compile(r"\s*(.*?)\s*(?:\((.*?)\))?\s*$")


def parse_case_cell(text: str | None) -> list[tuple[str, str]]:
    if not text:
        return []
    out: list[tuple[str, str]] = []
    for seg in str(text).split("|"):
        seg = seg.strip()
        if not seg:
            continue
        m = _CASE_RE.match(seg)
        number = (m.group(1) or "").strip() if m else seg
        date = (m.group(2) or "").strip() if m else ""
        if number:
            out.append((number, date))
    return out


def case_year(date: str) -> str:
    m = re.search(r"(19|20)\d\d", date or "")
    return m.group(0) if m else ""


def row_to_revision(row: tuple, source_file: str) -> Revision | None:
    # 컬럼: 번호, 요약정보(세목), 사유, 유지사례, 삭제사례, 정비사유, 등록일자
    if row[0] is None or str(row[0]).strip() == "":
        return None
    try:
        seq = int(str(row[0]).strip())
    except ValueError:
        return None
    return Revision(
        seq=seq,
        tax_category=str(row[1] or "").strip(),
        summary=str(row[2] or "").strip(),
        reason="",  # 스펙상 사유=요약. 별도 사유 컬럼 없음 → 요약을 summary로, reason은 예약(빈값)
        revision_reason=str(row[5] or "").strip(),
        registered_at=str(row[6] or "").strip(),
        keep_cases=parse_case_cell(row[3]),
        delete_cases=parse_case_cell(row[4]),
        source_file=source_file,
    )


def parse_workbook(path: str, source_file: str) -> list[Revision]:
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    next(rows, None)  # 헤더 skip
    out: list[Revision] = []
    for row in rows:
        rev = row_to_revision(row, source_file)
        if rev is not None:
            out.append(rev)
    wb.close()
    return out
