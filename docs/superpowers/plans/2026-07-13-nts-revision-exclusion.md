# NTS 정비내역 적재 + 검색 제외목록 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** NTS 정비내역 xlsx를 `fino_nts.db`에 적재하고, 삭제된 해석사례를 doc_index로 매칭해 검색 제외목록(external_id)과 삭제 이력을 산출한다(기존 테이블 무수정).

**Architecture:** 신규 모듈 `crawler/nts_revisions/` — xlsx 파싱 → 삭제사례를 `doc_index.doc_number` 정확일치(다건은 연도로 disambiguation)로 `doc_id(=papers.external_id)` 매칭 → 새 테이블 3종(nts_revisions/nts_revision_cases/nts_excluded_docs)에 적재 + ndjson export. `papers`·`doc_index`는 읽기만.

**Tech Stack:** Python 3, openpyxl 3.1, sqlite3, pytest.

**스펙:** `docs/superpowers/specs/2026-07-13-nts-revision-exclusion-design.md`

## Global Constraints

- 대상 DB `data/fino_nts.db`. 새 테이블 3종만 추가, **기존 papers·doc_index 무수정**(읽기 전용 SELECT).
- 소스 xlsx: `nts_new.xlsx`(Sheet1, 996행). 컬럼 순서: 번호/요약정보(세목)/사유/유지사례/삭제사례/정비사유/등록일자.
- 사례 셀: `번호(YYYY.MM.DD.)`, 다중은 `|` 구분. 매칭키 = 번호(정확일치), disambiguation = 날짜 연도 vs `doc_index.published_date`(`YYYY-MM-DD`) 앞 4자리.
- `doc_index.doc_id` = `papers.external_id`. 매칭 다건이 연도로도 1건으로 안 좁혀지면 **전부 제외 등재**(보수적).
- 삭제 문서를 RAG 검색 소스로 export하지 않음. export 2종: `data/export/nts_excluded_ids.ndjson`(제외목록), `data/export/nts_deletion_history.ndjson`(이력).
- 멱등: 같은 source_file 재적재 시 기존 정비분 교체.
- 테스트: `.venv/bin/python -m pytest tests/test_nts_revisions_*.py -q`. 커밋 스타일 `feat(nts_revisions): ...`. data/*는 .gitignore.

---

### Task 1: models.py + parse.py — xlsx 파싱

**Files:**
- Create: `crawler/nts_revisions/__init__.py` (빈 파일)
- Create: `crawler/nts_revisions/models.py`
- Create: `crawler/nts_revisions/parse.py`
- Modify: `requirements.txt` (openpyxl 추가)
- Test: `tests/test_nts_revisions_parse.py`

**Interfaces:**
- Produces: `Revision(seq, tax_category, summary, reason, revision_reason, registered_at, keep_cases, delete_cases, source_file)` (keep/delete_cases = `list[tuple[str,str]]` = (번호,날짜)), `parse_case_cell(text) -> list[tuple[str,str]]`, `case_year(date) -> str`, `row_to_revision(row, source_file) -> Revision | None`, `parse_workbook(path, source_file) -> list[Revision]`

- [ ] **Step 1: Write the failing test**

`tests/test_nts_revisions_parse.py`:

```python
from crawler.nts_revisions.parse import case_year, parse_case_cell, row_to_revision


def test_parse_case_cell_single_and_multi() -> None:
    assert parse_case_cell("재산세과-271(2010.05.04.)") == [("재산세과-271", "2010.05.04.")]
    assert parse_case_cell("법인46012-1784(2000.08.19.)|법인46012-379(2000.02.10.)") == [
        ("법인46012-1784", "2000.08.19."), ("법인46012-379", "2000.02.10.")]


def test_parse_case_cell_edge() -> None:
    assert parse_case_cell(None) == []
    assert parse_case_cell("") == []
    # 괄호 없는 조문(법령)도 번호만 뽑고 날짜는 빈값
    assert parse_case_cell("소득세법 시행령 제155조의3") == [("소득세법 시행령 제155조의3", "")]


def test_case_year() -> None:
    assert case_year("2010.05.04.") == "2010"
    assert case_year("") == ""


def test_row_to_revision() -> None:
    row = ("996", "상증", "사망보험금을 협의분할...", "사전-2014-법령해석재산-20405(2015.07.13.)",
           "재산세과-271(2010.05.04.)", "국세법령해석심의위원회...", "2026.06.26.")
    rev = row_to_revision(row, "nts_new.xlsx")
    assert rev is not None
    assert rev.seq == 996 and rev.tax_category == "상증"
    assert rev.keep_cases == [("사전-2014-법령해석재산-20405", "2015.07.13.")]
    assert rev.delete_cases == [("재산세과-271", "2010.05.04.")]
    assert rev.registered_at == "2026.06.26." and rev.source_file == "nts_new.xlsx"


def test_row_to_revision_skips_empty() -> None:
    assert row_to_revision((None, None, None, None, None, None, None), "x") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nts_revisions_parse.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'crawler.nts_revisions'`

- [ ] **Step 3: Write implementation**

`crawler/nts_revisions/__init__.py`: 빈 파일.

`crawler/nts_revisions/models.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Revision:
    seq: int
    tax_category: str
    summary: str
    reason: str
    revision_reason: str
    registered_at: str
    keep_cases: tuple[tuple[str, str], ...]    # (번호, 날짜)
    delete_cases: tuple[tuple[str, str], ...]
    source_file: str
```

`crawler/nts_revisions/parse.py`:

```python
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
        keep_cases=tuple(parse_case_cell(row[3])),
        delete_cases=tuple(parse_case_cell(row[4])),
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
```

Note: 스펙의 nts_revisions.summary/reason 매핑 — xlsx의 `사유` 컬럼(row[2])이 해석 요약이므로 이를 `summary`에 넣고, `reason`은 사용 안 함(빈 문자열). DB에는 summary만 저장(Task 2).

- [ ] **Step 4: Add openpyxl to requirements**

`requirements.txt` 끝에 한 줄 추가(이미 있으면 skip):

```
openpyxl>=3.1
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_nts_revisions_parse.py -q`
Expected: PASS (5 tests)

- [ ] **Step 6: Commit**

```bash
git add crawler/nts_revisions/__init__.py crawler/nts_revisions/models.py crawler/nts_revisions/parse.py requirements.txt tests/test_nts_revisions_parse.py
git commit -m "feat(nts_revisions): xlsx 파서 — 사례 셀(번호/날짜) 분리, 행→Revision"
```

---

### Task 2: db.py — 스키마 3종 + 멱등 적재 + 조회

**Files:**
- Create: `crawler/nts_revisions/db.py`
- Test: `tests/test_nts_revisions_db.py`

**Interfaces:**
- Consumes: 없음(원시값)
- Produces: `connect_db(path)`, `init_schema(conn)`, `delete_by_source(conn, source_file)`, `insert_revision(conn, *, seq, tax_category, summary, revision_reason, registered_at, source_file) -> int`, `insert_case(conn, *, revision_id, role, case_number, case_date, matched_doc_id)`, `upsert_excluded(conn, *, external_id, revision_id, doc_number, title, revision_reason, registered_at)`, `iter_excluded(conn) -> list[Row]`, `iter_history(conn) -> list[Row]`, `count_excluded(conn) -> int`

- [ ] **Step 1: Write the failing test**

`tests/test_nts_revisions_db.py`:

```python
from pathlib import Path

from crawler.nts_revisions.db import (
    connect_db, count_excluded, delete_by_source, init_schema, insert_case,
    insert_revision, iter_excluded, iter_history, upsert_excluded,
)


def _conn(tmp_path: Path):
    c = connect_db(tmp_path / "d.db")
    init_schema(c)
    return c


def test_insert_and_history(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    rid = insert_revision(conn, seq=996, tax_category="상증", summary="사망보험금...",
                          revision_reason="심의 변경", registered_at="2026.06.26.",
                          source_file="nts_new.xlsx")
    insert_case(conn, revision_id=rid, role="delete", case_number="재산세과-271",
                case_date="2010.05.04.", matched_doc_id="010000000000058132")
    insert_case(conn, revision_id=rid, role="keep", case_number="사전-2014-법령해석재산-20405",
                case_date="2015.07.13.", matched_doc_id=None)
    hist = iter_history(conn)
    assert len(hist) == 1 and hist[0]["seq"] == 996 and hist[0]["tax_category"] == "상증"


def test_upsert_excluded_idempotent(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    rid = insert_revision(conn, seq=1, tax_category="법인", summary="x",
                          revision_reason="y", registered_at="2026.01.01.", source_file="a.xlsx")
    upsert_excluded(conn, external_id="E1", revision_id=rid, doc_number="법인46012-1784",
                    title="업무무관가지급금...", revision_reason="y", registered_at="2026.01.01.")
    upsert_excluded(conn, external_id="E1", revision_id=rid, doc_number="법인46012-1784",
                    title="업무무관가지급금...", revision_reason="y", registered_at="2026.01.01.")
    assert count_excluded(conn) == 1                       # 같은 external_id 재등재 → 1건
    rows = iter_excluded(conn)
    assert rows[0]["external_id"] == "E1" and rows[0]["doc_number"] == "법인46012-1784"


def test_delete_by_source_replaces(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    rid = insert_revision(conn, seq=1, tax_category="법인", summary="x", revision_reason="y",
                          registered_at="2026.01.01.", source_file="a.xlsx")
    upsert_excluded(conn, external_id="E1", revision_id=rid, doc_number="D1", title="t",
                    revision_reason="y", registered_at="2026.01.01.")
    delete_by_source(conn, "a.xlsx")
    assert count_excluded(conn) == 0
    assert iter_history(conn) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nts_revisions_db.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`crawler/nts_revisions/db.py`:

```python
from __future__ import annotations

from pathlib import Path
import sqlite3


def connect_db(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS nts_revisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seq INTEGER, tax_category TEXT, summary TEXT,
            revision_reason TEXT, registered_at TEXT,
            source_file TEXT, loaded_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS nts_revision_cases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            revision_id INTEGER REFERENCES nts_revisions(id) ON DELETE CASCADE,
            role TEXT, case_number TEXT, case_date TEXT, matched_doc_id TEXT
        );
        CREATE TABLE IF NOT EXISTS nts_excluded_docs (
            external_id TEXT PRIMARY KEY,
            revision_id INTEGER REFERENCES nts_revisions(id) ON DELETE CASCADE,
            doc_number TEXT, title TEXT, revision_reason TEXT, registered_at TEXT,
            excluded_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE INDEX IF NOT EXISTS idx_rev_source ON nts_revisions(source_file);
        CREATE INDEX IF NOT EXISTS idx_case_rev ON nts_revision_cases(revision_id);
        """
    )
    conn.commit()


def delete_by_source(conn: sqlite3.Connection, source_file: str) -> None:
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM nts_revisions WHERE source_file = ?", (source_file,))]
    if ids:
        qs = ",".join("?" for _ in ids)
        conn.execute(f"DELETE FROM nts_excluded_docs WHERE revision_id IN ({qs})", ids)
        conn.execute(f"DELETE FROM nts_revision_cases WHERE revision_id IN ({qs})", ids)
        conn.execute(f"DELETE FROM nts_revisions WHERE id IN ({qs})", ids)
    conn.commit()


def insert_revision(conn: sqlite3.Connection, *, seq: int, tax_category: str, summary: str,
                    revision_reason: str, registered_at: str, source_file: str) -> int:
    cur = conn.execute(
        """INSERT INTO nts_revisions (seq, tax_category, summary, revision_reason,
             registered_at, source_file) VALUES (?, ?, ?, ?, ?, ?)""",
        (seq, tax_category, summary, revision_reason, registered_at, source_file),
    )
    conn.commit()
    return int(cur.lastrowid)


def insert_case(conn: sqlite3.Connection, *, revision_id: int, role: str,
                case_number: str, case_date: str, matched_doc_id: str | None) -> None:
    conn.execute(
        """INSERT INTO nts_revision_cases (revision_id, role, case_number, case_date, matched_doc_id)
           VALUES (?, ?, ?, ?, ?)""",
        (revision_id, role, case_number, case_date, matched_doc_id),
    )
    conn.commit()


def upsert_excluded(conn: sqlite3.Connection, *, external_id: str, revision_id: int,
                    doc_number: str, title: str, revision_reason: str, registered_at: str) -> None:
    conn.execute(
        """INSERT INTO nts_excluded_docs (external_id, revision_id, doc_number, title,
             revision_reason, registered_at) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(external_id) DO UPDATE SET revision_id=excluded.revision_id,
             doc_number=excluded.doc_number, title=excluded.title,
             revision_reason=excluded.revision_reason, registered_at=excluded.registered_at,
             excluded_at=datetime('now','localtime')""",
        (external_id, revision_id, doc_number, title, revision_reason, registered_at),
    )
    conn.commit()


def iter_excluded(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT external_id, doc_number, title, revision_reason, registered_at "
        "FROM nts_excluded_docs ORDER BY external_id").fetchall()


def iter_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM nts_revisions ORDER BY seq DESC").fetchall()


def count_excluded(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM nts_excluded_docs").fetchone()[0])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_nts_revisions_db.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add crawler/nts_revisions/db.py tests/test_nts_revisions_db.py
git commit -m "feat(nts_revisions): 스키마 3종(revisions/cases/excluded) + 멱등 적재/조회"
```

---

### Task 3: match.py — 삭제사례 → doc_index 매칭

**Files:**
- Create: `crawler/nts_revisions/match.py`
- Test: `tests/test_nts_revisions_match.py`

**Interfaces:**
- Consumes: doc_index 테이블(같은 conn), `case_year` (Task 1)
- Produces: `resolve_delete_case(conn, case_number, case_date) -> list[tuple[str, str]]` (매칭된 [(external_id, title), ...]. 없으면 []. 다건은 연도로 좁히되 안 좁혀지면 전부 반환)

- [ ] **Step 1: Write the failing test**

`tests/test_nts_revisions_match.py`:

```python
from pathlib import Path

from crawler.nts_revisions.db import connect_db
from crawler.nts_revisions.match import resolve_delete_case


def _conn_with_docindex(tmp_path: Path):
    conn = connect_db(tmp_path / "d.db")
    conn.executescript(
        """CREATE TABLE doc_index (doc_id TEXT, doc_number TEXT, title TEXT, published_date TEXT);
           INSERT INTO doc_index VALUES
             ('D-2000','법인46012-1784','2000년 해석','2000-08-19'),
             ('D-1998','법인46012-1784','1998년 해석','1998-07-02'),
             ('D-1994','법인46012-1784','1994년 해석','1994-06-21'),
             ('D-uniq','재산세과-271','유일 해석','2010-05-04');""")
    conn.commit()
    return conn


def test_single_match(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    assert resolve_delete_case(conn, "재산세과-271", "2010.05.04.") == [("D-uniq", "유일 해석")]


def test_year_disambiguation(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    # 3건 중 2000년만 선택
    assert resolve_delete_case(conn, "법인46012-1784", "2000.08.19.") == [("D-2000", "2000년 해석")]


def test_no_year_returns_all_conservative(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    got = resolve_delete_case(conn, "법인46012-1784", "")
    assert {e for e, _ in got} == {"D-2000", "D-1998", "D-1994"}   # 연도 없으면 전부(보수적)


def test_no_match(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    assert resolve_delete_case(conn, "없는번호-999", "2020.01.01.") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nts_revisions_match.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`crawler/nts_revisions/match.py`:

```python
from __future__ import annotations

import sqlite3

from .parse import case_year


def resolve_delete_case(conn: sqlite3.Connection, case_number: str,
                        case_date: str) -> list[tuple[str, str]]:
    """삭제사례 번호를 doc_index.doc_number 정확일치로 매칭.
    다건이면 case_date 연도로 좁히고, 안 좁혀지면 전부 반환(보수적 제외)."""
    rows = conn.execute(
        "SELECT doc_id, title, published_date FROM doc_index WHERE doc_number = ?",
        (case_number,),
    ).fetchall()
    if not rows:
        return []
    if len(rows) > 1:
        year = case_year(case_date)
        if year:
            narrowed = [r for r in rows if str(r["published_date"] or "")[:4] == year]
            if narrowed:
                rows = narrowed
    return [(r["doc_id"], r["title"]) for r in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_nts_revisions_match.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add crawler/nts_revisions/match.py tests/test_nts_revisions_match.py
git commit -m "feat(nts_revisions): 삭제사례 doc_index 매칭 — 정확일치 + 연도 disambiguation(보수적)"
```

---

### Task 4: load.py — 오케스트레이션 + export + CLI

**Files:**
- Create: `crawler/nts_revisions/load.py`
- Create: `crawler/nts_revisions/__main__.py`
- Test: `tests/test_nts_revisions_load.py`

**Interfaces:**
- Consumes: Task 1-3 전부
- Produces: `load(conn, revisions) -> dict`(적재 통계: revisions/cases/excluded), `export_excluded(conn, out_path) -> int`, `export_history(conn, out_path) -> int`, CLI `python -m crawler.nts_revisions.load --xlsx PATH [--db-path P] [--export]`

- [ ] **Step 1: Write the failing test**

`tests/test_nts_revisions_load.py`:

```python
import json
from pathlib import Path

from crawler.nts_revisions.db import connect_db, count_excluded, init_schema
from crawler.nts_revisions.load import export_excluded, export_history, load
from crawler.nts_revisions.models import Revision


def _conn_with_docindex(tmp_path: Path):
    conn = connect_db(tmp_path / "d.db")
    init_schema(conn)
    conn.executescript(
        """CREATE TABLE doc_index (doc_id TEXT, doc_number TEXT, title TEXT, published_date TEXT);
           INSERT INTO doc_index VALUES
             ('D-2000','법인46012-1784','2000년 해석','2000-08-19'),
             ('D-uniq','재산세과-271','유일 해석','2010-05-04');""")
    conn.commit()
    return conn


def _rev() -> Revision:
    return Revision(seq=996, tax_category="상증", summary="사망보험금...", reason="",
                    revision_reason="심의 변경", registered_at="2026.06.26.",
                    keep_cases=(("사전-2014-법령해석재산-20405", "2015.07.13."),),
                    delete_cases=(("재산세과-271", "2010.05.04."), ("법인46012-1784", "2000.08.19.")),
                    source_file="nts_new.xlsx")


def test_load_populates_and_matches(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    stats = load(conn, [_rev()])
    assert stats["revisions"] == 1
    assert stats["excluded"] == 2                     # 두 삭제사례 모두 매칭
    assert count_excluded(conn) == 2
    ids = {r["external_id"] for r in conn.execute("SELECT external_id FROM nts_excluded_docs")}
    assert ids == {"D-uniq", "D-2000"}


def test_load_idempotent_by_source(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    load(conn, [_rev()])
    load(conn, [_rev()])                              # 재적재
    assert count_excluded(conn) == 2                  # 중복 안 쌓임
    assert conn.execute("SELECT COUNT(*) FROM nts_revisions").fetchone()[0] == 1


def test_export(tmp_path: Path) -> None:
    conn = _conn_with_docindex(tmp_path)
    load(conn, [_rev()])
    ex = tmp_path / "excluded.ndjson"
    hi = tmp_path / "history.ndjson"
    assert export_excluded(conn, ex) == 2
    assert export_history(conn, hi) == 1
    first = json.loads(ex.read_text(encoding="utf-8").splitlines()[0])
    assert "external_id" in first and "revision_reason" in first
    h0 = json.loads(hi.read_text(encoding="utf-8").splitlines()[0])
    assert h0["seq"] == 996 and h0["tax_category"] == "상증"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_nts_revisions_load.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`crawler/nts_revisions/load.py`:

```python
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .db import (
    connect_db, count_excluded, delete_by_source, init_schema, insert_case,
    insert_revision, iter_excluded, iter_history, upsert_excluded,
)
from .match import resolve_delete_case
from .models import Revision
from .parse import parse_workbook

DEFAULT_DB = Path("data/fino_nts.db")
DEFAULT_EXCLUDED = Path("data/export/nts_excluded_ids.ndjson")
DEFAULT_HISTORY = Path("data/export/nts_deletion_history.ndjson")


def load(conn, revisions: list[Revision]) -> dict:
    if revisions:
        delete_by_source(conn, revisions[0].source_file)   # 멱등: 해당 source 교체
    n_rev = n_case = n_excl = 0
    for rev in revisions:
        rid = insert_revision(conn, seq=rev.seq, tax_category=rev.tax_category,
                              summary=rev.summary, revision_reason=rev.revision_reason,
                              registered_at=rev.registered_at, source_file=rev.source_file)
        n_rev += 1
        for number, date in rev.keep_cases:
            insert_case(conn, revision_id=rid, role="keep", case_number=number,
                        case_date=date, matched_doc_id=None)
            n_case += 1
        for number, date in rev.delete_cases:
            matches = resolve_delete_case(conn, number, date)
            doc_id = matches[0][0] if matches else None
            insert_case(conn, revision_id=rid, role="delete", case_number=number,
                        case_date=date, matched_doc_id=doc_id)
            n_case += 1
            for external_id, title in matches:
                upsert_excluded(conn, external_id=external_id, revision_id=rid,
                                doc_number=number, title=title,
                                revision_reason=rev.revision_reason, registered_at=rev.registered_at)
                n_excl += 1
    return {"revisions": n_rev, "cases": n_case, "excluded": count_excluded(conn)}


def export_excluded(conn, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for r in iter_excluded(conn):
            fh.write(json.dumps({
                "external_id": r["external_id"], "doc_number": r["doc_number"],
                "title": r["title"], "revision_reason": r["revision_reason"],
                "registered_at": r["registered_at"],
            }, ensure_ascii=False) + "\n")
            n += 1
    return n


def export_history(conn, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for r in iter_history(conn):
            fh.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
            n += 1
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m crawler.nts_revisions.load")
    _ = p.add_argument("--xlsx", type=str, required=True)
    _ = p.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    _ = p.add_argument("--export", action="store_true")
    return p


def main() -> int:
    args = build_parser().parse_args()
    source_file = Path(args.xlsx).name
    revisions = parse_workbook(args.xlsx, source_file)
    conn = connect_db(args.db_path)
    init_schema(conn)
    stats = load(conn, revisions)
    print(f"load: {stats}")
    if args.export:
        ne = export_excluded(conn, DEFAULT_EXCLUDED)
        nh = export_history(conn, DEFAULT_HISTORY)
        print(f"export: excluded={ne} history={nh}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`crawler/nts_revisions/__main__.py`:

```python
from .load import main

raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_nts_revisions_load.py -q`
Expected: PASS (3 tests). 이어서 전체 모듈: `.venv/bin/python -m pytest tests/test_nts_revisions_*.py -q` 전부 PASS.

- [ ] **Step 5: Commit**

```bash
git add crawler/nts_revisions/load.py crawler/nts_revisions/__main__.py tests/test_nts_revisions_load.py
git commit -m "feat(nts_revisions): 적재 오케스트레이션 + 제외목록/이력 export + CLI(멱등)"
```

---

### Task 5: 라이브 적재 검증 + 문서/메모리

**Files:**
- Modify: `docs/2026-07-02_fino_corpus_crawler_handoff.md` (정비/제외목록 한 줄)

- [ ] **Step 1: 실 xlsx 적재** (fino_nts.db 대상 — 새 테이블만 추가되므로 안전)

```bash
cd /data_raid/ruci_workspace/frwaler
.venv/bin/python -m crawler.nts_revisions.load --xlsx nts_new.xlsx --export
```
Expected: `load: {'revisions': 996, 'cases': ~3400, 'excluded': N}` 후 `export: excluded=N history=996`. N은 매칭된 삭제사례 문서 수(삭제 27% 커버리지 기준 수백 건대).

- [ ] **Step 2: 커버리지·무수정 검증**

```bash
.venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect("data/fino_nts.db"); c.row_factory=sqlite3.Row
print("nts_revisions:", c.execute("SELECT COUNT(*) FROM nts_revisions").fetchone()[0])
print("삭제사례 case:", c.execute("SELECT COUNT(*) FROM nts_revision_cases WHERE role='delete'").fetchone()[0])
print("  그 중 매칭:", c.execute("SELECT COUNT(*) FROM nts_revision_cases WHERE role='delete' AND matched_doc_id IS NOT NULL").fetchone()[0])
print("nts_excluded_docs:", c.execute("SELECT COUNT(*) FROM nts_excluded_docs").fetchone()[0])
# 제외 external_id가 papers에 실재하는지 표본
row = c.execute("SELECT external_id, doc_number, title FROM nts_excluded_docs LIMIT 3").fetchall()
for r in row:
    n = c.execute("SELECT COUNT(*) FROM papers WHERE external_id=?", (r["external_id"],)).fetchone()[0]
    print(f"  {r['external_id']} ({r['doc_number']}): papers 실재={n} | {r['title'][:30]}")
# 기존 테이블 무수정 확인: papers/doc_index 행수는 변함없어야(참고 출력)
print("papers 총:", c.execute("SELECT COUNT(*) FROM papers").fetchone()[0])
EOF
ls -la data/export/nts_excluded_ids.ndjson data/export/nts_deletion_history.ndjson
```
Expected: nts_revisions=996, 삭제사례 매칭률 ≈27%, 제외목록 수백건, 표본 external_id가 papers에 실재(=1). 이상 있으면 STOP·보고.

- [ ] **Step 3: 재실행 멱등 확인**

```bash
.venv/bin/python -m crawler.nts_revisions.load --xlsx nts_new.xlsx
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/fino_nts.db'); print('revisions(재실행후):', c.execute('SELECT COUNT(*) FROM nts_revisions').fetchone()[0])"
```
Expected: 996 (누적 안 됨).

- [ ] **Step 4: 전체 테스트 + 문서 갱신 + 커밋**

Run: `.venv/bin/python -m pytest tests/test_nts_revisions_*.py -q`
Expected: 전부 PASS.

`docs/2026-07-02_fino_corpus_crawler_handoff.md`에 한 줄 추가(§ NTS 관련): "NTS 해석사례 정비: `crawler/nts_revisions/`가 nts_new.xlsx→fino_nts.db 3테이블(정비내역/사례/제외목록) 적재, `data/export/nts_excluded_ids.ndjson`(검색 제외목록)·`nts_deletion_history.ndjson` 산출. 스펙 `2026-07-13-nts-revision-exclusion-design.md`. 제외 실현(ES 재색인 필터)은 후속."

```bash
git add docs/2026-07-02_fino_corpus_crawler_handoff.md
git commit -m "feat(nts_revisions): 실 xlsx 적재 검증(996 정비·제외목록 N) + 핸드오프 갱신"
```

- [ ] **Step 5: 메모리 갱신 (컨트롤러 수행)**

`nts-revision-exclusion` 메모리 신규(정비 xlsx→fino_nts.db 3테이블·매칭체인·제외목록 export·후속 ES 필터) + MEMORY.md 인덱스 한 줄.

---

## Self-Review 결과

- **스펙 커버리지**: 파서(§소스·처리1)=Task 1, 스키마 3종·멱등(§데이터모델·처리3)=Task 2, 매칭(§연결체인·처리2)=Task 3, 적재+export(§처리4·export)=Task 4, 라이브검증·이력보존(§검증)=Task 5. 후속 ES 필터는 스펙상 범위 밖(문서화). 갭 없음.
- **플레이스홀더 스캔**: 모든 코드 스텝 전체 코드 포함. `reason` 미사용 필드는 Note로 명시(요약을 summary에 저장).
- **타입 일관성**: `Revision`(T1) 필드 → load(T4) 사용 일치. `resolve_delete_case(conn, number, date) -> list[tuple[external_id,title]]`(T3) → load(T4) 소비 일치. db insert/upsert 키워드 인자(T2) → load(T4) 호출 일치. `case_year`(T1) → match(T3) 일치.
- **알려진 한계**: 삭제 27%만 매칭(옛사례 미크롤 — 정상). 제외 실현은 후속 ES 스펙. models.Revision에 `reason` 필드는 예약(현재 빈값) — YAGNI상 제거 가능하나 스펙 nts_revisions 매핑 주석 유지 위해 존치.
