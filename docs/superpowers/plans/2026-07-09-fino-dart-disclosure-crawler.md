# FINO DART 공시 수집기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** DART OpenAPI로 상장사 전체의 정기공시(A)+외부감사관련(F) 최근 12개월치를 목록+원문zip+재무제표로 `data/fino_dart.db`에 수집한다(일 20,000건 한도 하 재개 가능).

**Architecture:** 신규 독립 모듈 `crawler/fino_dart/` — fino_std 패턴 미러링. 4단계 파이프라인(corpcode→list→docs→financials), 각 단계는 "이미 있는 것 skip" 기반으로 재개·증분. 일일 API 쿼터를 `api_quota` 테이블로 추적해 한도 도달 시 정상 종료.

**Tech Stack:** Python 3, httpx 0.28, sqlite3, pytest. `.env`의 `DART_API_KEY`.

**스펙:** `docs/superpowers/specs/2026-07-09-fino-dart-disclosure-crawler-design.md`

## Global Constraints

- DB `data/fino_dart.db`(신규), 원문 `data/fino_dart_docs/`. 기존 크롤러 DB 무접촉. data/*는 .gitignore.
- API 키는 env `DART_API_KEY`(`.env`에 존재, 작동 확인됨). BASE = `https://opendart.fss.or.kr/api`.
- pblntf_ty: `A`(정기공시), `F`(외부감사관련). corp_cls: `Y`(유가), `K`(코스닥), `N`(코넥스). 상장사 = stock_code 있는 기업.
- **일일 한도 기본 19,500**(20,000에서 여유). 도달 시 `QuotaExhausted` → 파이프라인 정상 종료.
- list.json은 corp_code 없이 조회 시 **기간 ≤ 3개월** → 최근 12개월을 3개월 창 4개로 페이징. page_count ≤ 100.
- 재무제표(fnlttSinglAcntAll)는 정기공시(A)의 정형보고서만: 사업보고서→11011, 반기→11012, 1분기→11013, 3분기→11014. fs_div CFS·OFS 각각. F는 원문만.
- deep-link: `https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcp_no}`.
- 요청 지연 기본 0.3s, 재시도 3회. status 013(데이터 없음)=정상 skip, 020(한도초과)=QuotaExhausted, 010/011(키 오류)=즉시 중단.
- 테스트: `.venv/bin/python -m pytest tests/test_fino_dart_*.py -q`. 커밋 스타일 `feat(fino_dart): ...`. 실 API 호출은 유닛에 없음(최종 태스크 라이브 스모크만).

---

### Task 1: models.py + sources.py

**Files:**
- Create: `crawler/fino_dart/__init__.py` (빈 파일)
- Create: `crawler/fino_dart/models.py`
- Create: `crawler/fino_dart/sources.py`
- Test: `tests/test_fino_dart_sources.py`

**Interfaces:**
- Produces: `Corp(corp_code, corp_name, stock_code, modify_date)`, `Filing(rcp_no, corp_code, corp_name, stock_code, corp_cls, report_nm, pblntf_ty, rcept_dt, flr_nm, rm)`, `FinancialRec(rcp_no, bsns_year, reprt_code, fs_div, fs_json, status)`; `BASE`, `PBLNTF_TYPES=("A","F")`, `CORP_CLS=("Y","K","N")`, `REPRT_CODES`(dict), `report_to_reprt(report_nm) -> tuple[str,str] | None`, `filing_url(rcp_no) -> str`, `date_windows(months, today) -> list[tuple[str,str]]`

- [ ] **Step 1: Write the failing test**

`tests/test_fino_dart_sources.py`:

```python
from crawler.fino_dart.sources import (
    BASE, CORP_CLS, PBLNTF_TYPES, date_windows, filing_url, report_to_reprt,
)


def test_constants() -> None:
    assert BASE == "https://opendart.fss.or.kr/api"
    assert PBLNTF_TYPES == ("A", "F")
    assert CORP_CLS == ("Y", "K", "N")


def test_filing_url() -> None:
    assert filing_url("20260331000123") == "https://dart.fss.or.kr/dsaf001/main.do?rcpNo=20260331000123"


def test_report_to_reprt_maps_regular_reports() -> None:
    assert report_to_reprt("사업보고서 (2025.12)") == ("11011", "2025")
    assert report_to_reprt("반기보고서 (2025.06)") == ("11012", "2025")
    assert report_to_reprt("분기보고서 (2025.03)") == ("11013", "2025")
    assert report_to_reprt("분기보고서 (2025.09)") == ("11014", "2025")
    assert report_to_reprt("[기재정정]사업보고서 (2024.12)") == ("11011", "2024")


def test_report_to_reprt_returns_none_for_non_regular() -> None:
    assert report_to_reprt("감사보고서 (2025.12)") is None
    assert report_to_reprt("주요사항보고서(자기주식취득결정)") is None


def test_date_windows_splits_12_months_into_3month_chunks() -> None:
    wins = date_windows(12, "20260709")
    assert len(wins) == 4
    assert wins[0][1] == "20260709"          # 최신 창의 끝 = 오늘
    assert wins[-1][0] == "20250709"         # 가장 이른 창의 시작 = 12개월 전
    for start, end in wins:                  # 각 창은 YYYYMMDD 8자리
        assert len(start) == 8 and len(end) == 8
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_dart_sources.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'crawler.fino_dart'`

- [ ] **Step 3: Write implementation**

`crawler/fino_dart/__init__.py`: 빈 파일.

`crawler/fino_dart/models.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Corp:
    corp_code: str
    corp_name: str
    stock_code: str
    modify_date: str


@dataclass(frozen=True, slots=True)
class Filing:
    rcp_no: str
    corp_code: str
    corp_name: str
    stock_code: str
    corp_cls: str
    report_nm: str
    pblntf_ty: str
    rcept_dt: str
    flr_nm: str
    rm: str


@dataclass(frozen=True, slots=True)
class FinancialRec:
    rcp_no: str
    bsns_year: str
    reprt_code: str
    fs_div: str
    fs_json: str
    status: str
```

`crawler/fino_dart/sources.py`:

```python
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Final

BASE: Final = "https://opendart.fss.or.kr/api"
VIEWER: Final = "https://dart.fss.or.kr/dsaf001/main.do"
PBLNTF_TYPES: Final = ("A", "F")
CORP_CLS: Final = ("Y", "K", "N")

# 정기공시 정형보고서명 → (reprt_code, ...)
REPRT_CODES: Final = {
    "사업보고서": "11011",
    "반기보고서": "11012",
}
_QUARTER_MONTH_TO_CODE: Final = {"03": "11013", "09": "11014"}


def filing_url(rcp_no: str) -> str:
    return f"{VIEWER}?rcpNo={rcp_no}"


def report_to_reprt(report_nm: str) -> tuple[str, str] | None:
    """정형보고서명 → (reprt_code, bsns_year). 아니면 None."""
    m = re.search(r"\((\d{4})\.(\d{2})\)", report_nm)
    if not m:
        return None
    year, month = m.group(1), m.group(2)
    if "사업보고서" in report_nm:
        return "11011", year
    if "반기보고서" in report_nm:
        return "11012", year
    if "분기보고서" in report_nm:
        code = _QUARTER_MONTH_TO_CODE.get(month)
        return (code, year) if code else None
    return None


def date_windows(months: int, today: str) -> list[tuple[str, str]]:
    """오늘(YYYYMMDD)부터 months개월 전까지를 3개월 창으로 분할(최신순)."""
    end = datetime.strptime(today, "%Y%m%d")
    start_limit = end - timedelta(days=months * 30)
    windows: list[tuple[str, str]] = []
    cur_end = end
    while cur_end > start_limit:
        cur_start = max(cur_end - timedelta(days=90), start_limit)
        windows.append((cur_start.strftime("%Y%m%d"), cur_end.strftime("%Y%m%d")))
        cur_end = cur_start - timedelta(days=1)
    return windows
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_dart_sources.py -q`
Expected: PASS (5 tests). date_windows 테스트가 경계에서 실패하면 실제 반환값을 확인해 기대값을 실값에 맞춘다(로직이 3개월 창 4개·최신순·오늘 끝·12개월전 시작을 만족하면 정상).

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_dart/__init__.py crawler/fino_dart/models.py crawler/fino_dart/sources.py tests/test_fino_dart_sources.py
git commit -m "feat(fino_dart): 모델/상수 — pblntf/corp_cls, 보고서명→reprt_code, 3개월 창 분할"
```

---

### Task 2: db.py — 스키마 + upsert + 재개 쿼리 + 쿼터

**Files:**
- Create: `crawler/fino_dart/db.py`
- Test: `tests/test_fino_dart_db.py`

**Interfaces:**
- Consumes: `Corp`, `Filing`, `FinancialRec` (Task 1)
- Produces: `connect_db(path)`, `init_schema(conn)`, `upsert_corps(conn, corps)`, `upsert_filing(conn, f)`, `upsert_document(conn, rcp_no, *, local_path, bytes_, status)`, `upsert_financial(conn, fr)`, `filings_without_doc(conn) -> list[Row]`, `regular_filings_needing_fs(conn, fs_div) -> list[Row]`, `get_quota(conn, day) -> int`, `incr_quota(conn, day)`, `count_filings(conn) -> int`

- [ ] **Step 1: Write the failing test**

`tests/test_fino_dart_db.py`:

```python
from pathlib import Path

from crawler.fino_dart.db import (
    connect_db, count_filings, filings_without_doc, get_quota, incr_quota,
    init_schema, regular_filings_needing_fs, upsert_corps, upsert_document,
    upsert_filing, upsert_financial,
)
from crawler.fino_dart.models import Corp, Filing, FinancialRec


def _conn(tmp_path: Path):
    c = connect_db(tmp_path / "d.db")
    init_schema(c)
    return c


def _filing(rcp: str, pblntf: str = "A", report: str = "사업보고서 (2025.12)") -> Filing:
    return Filing(rcp_no=rcp, corp_code="00126380", corp_name="삼성전자", stock_code="005930",
                  corp_cls="Y", report_nm=report, pblntf_ty=pblntf, rcept_dt="20260331",
                  flr_nm="삼성전자", rm="")


def test_upsert_corps_and_filing_idempotent(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    upsert_corps(conn, [Corp("00126380", "삼성전자", "005930", "20260101")])
    upsert_filing(conn, _filing("R1"))
    upsert_filing(conn, _filing("R1", report="[기재정정]사업보고서 (2025.12)"))  # 같은 rcp_no
    assert count_filings(conn) == 1
    row = conn.execute("SELECT report_nm FROM filings WHERE rcp_no='R1'").fetchone()
    assert row["report_nm"].startswith("[기재정정]")


def test_filings_without_doc_excludes_ok(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    upsert_filing(conn, _filing("R1"))
    upsert_filing(conn, _filing("R2"))
    upsert_document(conn, "R1", local_path="x/R1.zip", bytes_=100, status="ok")
    pending = [r["rcp_no"] for r in filings_without_doc(conn)]
    assert pending == ["R2"]                       # ok는 제외, error는 재시도 대상
    upsert_document(conn, "R2", local_path="", bytes_=0, status="error")
    assert [r["rcp_no"] for r in filings_without_doc(conn)] == ["R2"]


def test_regular_filings_needing_fs(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    upsert_filing(conn, _filing("R1", pblntf="A", report="사업보고서 (2025.12)"))
    upsert_filing(conn, _filing("R2", pblntf="F", report="감사보고서 (2025.12)"))  # F 제외
    upsert_filing(conn, _filing("R3", pblntf="A", report="주요사항보고서"))          # 비정형 제외
    need = [r["rcp_no"] for r in regular_filings_needing_fs(conn, "CFS")]
    assert need == ["R1"]
    upsert_financial(conn, FinancialRec("R1", "2025", "11011", "CFS", "[]", "ok"))
    assert regular_filings_needing_fs(conn, "CFS") == []      # 이미 있으면 제외
    assert [r["rcp_no"] for r in regular_filings_needing_fs(conn, "OFS")] == ["R1"]  # fs_div별


def test_quota_counter(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    assert get_quota(conn, "20260709") == 0
    incr_quota(conn, "20260709")
    incr_quota(conn, "20260709")
    assert get_quota(conn, "20260709") == 2
    assert get_quota(conn, "20260710") == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_dart_db.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`crawler/fino_dart/db.py`:

```python
from __future__ import annotations

from pathlib import Path
import sqlite3

from .models import Corp, FinancialRec, Filing

# 정형보고서 판정용 SQL 조각(재무제표 대상): pblntf A + 사업/반기/분기 보고서
_REGULAR_WHERE = (
    "pblntf_ty = 'A' AND ("
    "report_nm LIKE '%사업보고서%' OR report_nm LIKE '%반기보고서%' "
    "OR report_nm LIKE '%분기보고서%')"
)


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
        CREATE TABLE IF NOT EXISTS corps (
            corp_code TEXT PRIMARY KEY, corp_name TEXT, stock_code TEXT,
            modify_date TEXT, collected_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS filings (
            rcp_no TEXT PRIMARY KEY, corp_code TEXT, corp_name TEXT, stock_code TEXT,
            corp_cls TEXT, report_nm TEXT, pblntf_ty TEXT, rcept_dt TEXT, flr_nm TEXT, rm TEXT,
            collected_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS documents (
            rcp_no TEXT PRIMARY KEY REFERENCES filings(rcp_no) ON DELETE CASCADE,
            local_path TEXT, bytes INTEGER, status TEXT,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
        );
        CREATE TABLE IF NOT EXISTS financials (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rcp_no TEXT REFERENCES filings(rcp_no) ON DELETE CASCADE,
            bsns_year TEXT, reprt_code TEXT, fs_div TEXT, fs_json TEXT, status TEXT,
            fetched_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
            UNIQUE(rcp_no, fs_div)
        );
        CREATE TABLE IF NOT EXISTS api_quota (day TEXT PRIMARY KEY, count INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_filings_pblntf ON filings(pblntf_ty);
        """
    )
    conn.commit()


def upsert_corps(conn: sqlite3.Connection, corps: list[Corp]) -> int:
    conn.executemany(
        """INSERT INTO corps (corp_code, corp_name, stock_code, modify_date)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(corp_code) DO UPDATE SET corp_name=excluded.corp_name,
             stock_code=excluded.stock_code, modify_date=excluded.modify_date,
             collected_at=datetime('now','localtime')""",
        [(c.corp_code, c.corp_name, c.stock_code, c.modify_date) for c in corps],
    )
    conn.commit()
    return len(corps)


def upsert_filing(conn: sqlite3.Connection, f: Filing) -> None:
    conn.execute(
        """INSERT INTO filings (rcp_no, corp_code, corp_name, stock_code, corp_cls,
             report_nm, pblntf_ty, rcept_dt, flr_nm, rm)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(rcp_no) DO UPDATE SET report_nm=excluded.report_nm,
             corp_name=excluded.corp_name, rm=excluded.rm,
             collected_at=datetime('now','localtime')""",
        (f.rcp_no, f.corp_code, f.corp_name, f.stock_code, f.corp_cls,
         f.report_nm, f.pblntf_ty, f.rcept_dt, f.flr_nm, f.rm),
    )
    conn.commit()


def upsert_document(conn: sqlite3.Connection, rcp_no: str, *,
                    local_path: str, bytes_: int, status: str) -> None:
    conn.execute(
        """INSERT INTO documents (rcp_no, local_path, bytes, status)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(rcp_no) DO UPDATE SET local_path=excluded.local_path,
             bytes=excluded.bytes, status=excluded.status,
             fetched_at=datetime('now','localtime')""",
        (rcp_no, local_path, bytes_, status),
    )
    conn.commit()


def upsert_financial(conn: sqlite3.Connection, fr: FinancialRec) -> None:
    conn.execute(
        """INSERT INTO financials (rcp_no, bsns_year, reprt_code, fs_div, fs_json, status)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(rcp_no, fs_div) DO UPDATE SET fs_json=excluded.fs_json,
             status=excluded.status, fetched_at=datetime('now','localtime')""",
        (fr.rcp_no, fr.bsns_year, fr.reprt_code, fr.fs_div, fr.fs_json, fr.status),
    )
    conn.commit()


def filings_without_doc(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT f.* FROM filings f LEFT JOIN documents d ON d.rcp_no = f.rcp_no
           WHERE d.rcp_no IS NULL OR d.status = 'error' ORDER BY f.rcp_no"""
    ).fetchall()


def regular_filings_needing_fs(conn: sqlite3.Connection, fs_div: str) -> list[sqlite3.Row]:
    return conn.execute(
        f"""SELECT f.* FROM filings f
            WHERE {_REGULAR_WHERE}
            AND NOT EXISTS (SELECT 1 FROM financials fin
                            WHERE fin.rcp_no = f.rcp_no AND fin.fs_div = ?)
            ORDER BY f.rcp_no""",
        (fs_div,),
    ).fetchall()


def get_quota(conn: sqlite3.Connection, day: str) -> int:
    row = conn.execute("SELECT count FROM api_quota WHERE day = ?", (day,)).fetchone()
    return int(row["count"]) if row else 0


def incr_quota(conn: sqlite3.Connection, day: str) -> None:
    conn.execute(
        """INSERT INTO api_quota (day, count) VALUES (?, 1)
           ON CONFLICT(day) DO UPDATE SET count = count + 1""",
        (day,),
    )
    conn.commit()


def count_filings(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_dart_db.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_dart/db.py tests/test_fino_dart_db.py
git commit -m "feat(fino_dart): 스키마(corps/filings/documents/financials/api_quota) + 재개 쿼리 + 쿼터 카운터"
```

---

### Task 3: parsers.py — list 행 / corpCode zip

**Files:**
- Create: `crawler/fino_dart/parsers.py`
- Test: `tests/test_fino_dart_parsers.py`

**Interfaces:**
- Consumes: `Corp`, `Filing` (Task 1)
- Produces: `parse_list_rows(data: dict, pblntf_ty: str) -> list[Filing]`, `parse_corpcode_zip(zip_bytes: bytes) -> list[Corp]` (상장사=stock_code 있는 것만)

- [ ] **Step 1: Write the failing test**

`tests/test_fino_dart_parsers.py`:

```python
import io
import zipfile

from crawler.fino_dart.parsers import parse_corpcode_zip, parse_list_rows


def test_parse_list_rows() -> None:
    data = {"status": "000", "list": [
        {"rcept_no": "20260331000123", "corp_code": "00126380", "corp_name": "삼성전자",
         "stock_code": "005930", "corp_cls": "Y", "report_nm": "사업보고서 (2025.12)",
         "rcept_dt": "20260331", "flr_nm": "삼성전자", "rm": "연"},
    ]}
    rows = parse_list_rows(data, "A")
    assert len(rows) == 1
    f = rows[0]
    assert f.rcp_no == "20260331000123" and f.pblntf_ty == "A"
    assert f.corp_name == "삼성전자" and f.corp_cls == "Y"


def test_parse_list_rows_empty_status_013() -> None:
    assert parse_list_rows({"status": "013", "message": "데이터 없음"}, "F") == []


def test_parse_corpcode_zip_keeps_only_listed() -> None:
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><result>'
        '<list><corp_code>00126380</corp_code><corp_name>삼성전자</corp_name>'
        '<stock_code>005930</stock_code><modify_date>20260101</modify_date></list>'
        '<list><corp_code>00999999</corp_code><corp_name>비상장회사</corp_name>'
        '<stock_code> </stock_code><modify_date>20260101</modify_date></list>'
        '</result>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("CORPCODE.xml", xml.encode("utf-8"))
    corps = parse_corpcode_zip(buf.getvalue())
    assert len(corps) == 1                       # 상장사(stock_code 있음)만
    assert corps[0].corp_code == "00126380" and corps[0].stock_code == "005930"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_dart_parsers.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`crawler/fino_dart/parsers.py`:

```python
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile

from .models import Corp, Filing


def parse_list_rows(data: dict, pblntf_ty: str) -> list[Filing]:
    if data.get("status") != "000":
        return []
    out: list[Filing] = []
    for r in data.get("list") or []:
        out.append(Filing(
            rcp_no=str(r.get("rcept_no", "")).strip(),
            corp_code=str(r.get("corp_code", "")).strip(),
            corp_name=str(r.get("corp_name", "")).strip(),
            stock_code=str(r.get("stock_code", "")).strip(),
            corp_cls=str(r.get("corp_cls", "")).strip(),
            report_nm=str(r.get("report_nm", "")).strip(),
            pblntf_ty=pblntf_ty,
            rcept_dt=str(r.get("rcept_dt", "")).strip(),
            flr_nm=str(r.get("flr_nm", "")).strip(),
            rm=str(r.get("rm", "")).strip(),
        ))
    return out


def parse_corpcode_zip(zip_bytes: bytes) -> list[Corp]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        name = next(n for n in z.namelist() if n.upper().endswith(".XML"))
        xml_bytes = z.read(name)
    root = ET.fromstring(xml_bytes)
    out: list[Corp] = []
    for el in root.findall("list"):
        stock = (el.findtext("stock_code") or "").strip()
        if not stock:                              # 상장사만(종목코드 존재)
            continue
        out.append(Corp(
            corp_code=(el.findtext("corp_code") or "").strip(),
            corp_name=(el.findtext("corp_name") or "").strip(),
            stock_code=stock,
            modify_date=(el.findtext("modify_date") or "").strip(),
        ))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_dart_parsers.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_dart/parsers.py tests/test_fino_dart_parsers.py
git commit -m "feat(fino_dart): list.json 행 파서 + corpCode.xml zip 파서(상장사 필터)"
```

---

### Task 4: client.py — DART 클라이언트 + 쿼터 + 재시도

**Files:**
- Create: `crawler/fino_dart/client.py`
- Test: `tests/test_fino_dart_client.py`

**Interfaces:**
- Consumes: `get_quota`, `incr_quota` (Task 2)
- Produces: `QuotaExhausted(RuntimeError)`, `DartAuthError(RuntimeError)`, `DartClient(api_key, conn, *, limit=19500, delay=0.3, transport=None)` with methods `get_json(path, params) -> dict`, `get_bytes(path, params) -> bytes`, `today() -> str`. get_json은 status 010/011→DartAuthError, 020→QuotaExhausted, 그 외(000/013 포함)는 dict 반환. 성공(013 포함 정상응답) 시 쿼터 +1. 한도 도달 시 호출 전에 QuotaExhausted.

- [ ] **Step 1: Write the failing test**

`tests/test_fino_dart_client.py`:

```python
from pathlib import Path

import httpx
import pytest

from crawler.fino_dart.client import DartAuthError, DartClient, QuotaExhausted
from crawler.fino_dart.db import connect_db, get_quota, incr_quota, init_schema


def _conn(tmp_path: Path):
    c = connect_db(tmp_path / "d.db")
    init_schema(c)
    return c


def _client(tmp_path, handler, **kw) -> DartClient:
    conn = _conn(tmp_path)
    return DartClient("KEY", conn, delay=0, transport=httpx.MockTransport(handler), **kw)


def test_get_json_success_consumes_quota(tmp_path: Path) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.params["crtfc_key"] == "KEY"
        return httpx.Response(200, json={"status": "000", "list": []})

    c = _client(tmp_path, handler)
    assert c.get_json("/list.json", {"pblntf_ty": "A"})["status"] == "000"
    assert get_quota(c.conn, c.today()) == 1


def test_get_json_013_is_returned_not_error(tmp_path: Path) -> None:
    c = _client(tmp_path, lambda r: httpx.Response(200, json={"status": "013", "message": "없음"}))
    assert c.get_json("/list.json", {})["status"] == "013"


def test_get_json_auth_error_raises(tmp_path: Path) -> None:
    c = _client(tmp_path, lambda r: httpx.Response(200, json={"status": "010", "message": "키오류"}))
    with pytest.raises(DartAuthError):
        c.get_json("/list.json", {})


def test_get_json_020_raises_quota(tmp_path: Path) -> None:
    c = _client(tmp_path, lambda r: httpx.Response(200, json={"status": "020", "message": "한도"}))
    with pytest.raises(QuotaExhausted):
        c.get_json("/list.json", {})


def test_quota_limit_blocks_before_call(tmp_path: Path) -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={"status": "000"})

    c = _client(tmp_path, handler, limit=2)
    for _ in range(2):
        c.get_json("/list.json", {})
    with pytest.raises(QuotaExhausted):
        c.get_json("/list.json", {})
    assert calls["n"] == 2                        # 3번째는 호출 전에 차단


def test_get_bytes_returns_content(tmp_path: Path) -> None:
    c = _client(tmp_path, lambda r: httpx.Response(200, content=b"PK\x03\x04zipdata"))
    assert c.get_bytes("/document.xml", {"rcept_no": "R1"}) == b"PK\x03\x04zipdata"
    assert get_quota(c.conn, c.today()) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_dart_client.py -q`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Write implementation**

`crawler/fino_dart/client.py`:

```python
from __future__ import annotations

import sqlite3
import time
from datetime import datetime

import httpx

from .db import get_quota, incr_quota
from .sources import BASE

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


class QuotaExhausted(RuntimeError):
    pass


class DartAuthError(RuntimeError):
    pass


class DartClient:
    def __init__(self, api_key: str, conn: sqlite3.Connection, *, limit: int = 19500,
                 delay: float = 0.3, transport: httpx.BaseTransport | None = None) -> None:
        self.api_key = api_key
        self.conn = conn
        self.limit = limit
        self.delay = delay
        self._client = httpx.Client(base_url=BASE, timeout=60.0,
                                    headers={"User-Agent": _UA}, transport=transport)

    def today(self) -> str:
        return datetime.now().strftime("%Y%m%d")

    def _check_quota(self) -> None:
        if get_quota(self.conn, self.today()) >= self.limit:
            raise QuotaExhausted(f"일일 한도({self.limit}) 도달 — 자정 이후 재실행하면 이어서 진행됩니다")

    def _request(self, path: str, params: dict) -> httpx.Response:
        full = {"crtfc_key": self.api_key, **params}
        last: Exception | None = None
        for attempt in range(3):
            if self.delay > 0:
                time.sleep(self.delay)
            try:
                resp = self._client.get(path, params=full)
                if resp.status_code == 200:
                    return resp
            except httpx.HTTPError as exc:
                last = exc
            time.sleep(1 + attempt)
        raise last or RuntimeError(f"DART 요청 실패: {path}")

    def get_json(self, path: str, params: dict) -> dict:
        self._check_quota()
        resp = self._request(path, params)
        data = resp.json()
        status = data.get("status")
        if status in ("010", "011"):
            raise DartAuthError(data.get("message", "인증키 오류"))
        if status == "020":
            raise QuotaExhausted("DART 사용한도 초과(020)")
        incr_quota(self.conn, self.today())
        return data

    def get_bytes(self, path: str, params: dict) -> bytes:
        self._check_quota()
        resp = self._request(path, params)
        incr_quota(self.conn, self.today())
        return resp.content
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_dart_client.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_dart/client.py tests/test_fino_dart_client.py
git commit -m "feat(fino_dart): DART 클라이언트 — 쿼터 게이트/소비, 재시도, 키·한도 에러 처리"
```

---

### Task 5: collect.py — 4단계 파이프라인 + CLI

**Files:**
- Create: `crawler/fino_dart/collect.py`
- Create: `crawler/fino_dart/__main__.py`
- Test: `tests/test_fino_dart_collect.py`

**Interfaces:**
- Consumes: Task 1-4 전부
- Produces: `collect(*, db_path, docs_dir, api_key, months, phases, corp_cls, limit, delay, today) -> dict` (단계별 카운트), CLI `python -m crawler.fino_dart.collect [--phase corpcode|list|docs|financials|all] [--months 12] [--corp-cls Y,K,N] [--limit 19500]`. 내부 함수: `phase_corpcode(client, conn)`, `phase_list(client, conn, months, corp_cls, today)`, `phase_docs(client, conn, docs_dir)`, `phase_financials(client, conn)`.

- [ ] **Step 1: Write the failing test** (client·fetch 모킹으로 파이프라인 검증)

`tests/test_fino_dart_collect.py`:

```python
import io
import zipfile
from pathlib import Path

import httpx

from crawler.fino_dart.client import DartClient
from crawler.fino_dart.collect import phase_docs, phase_financials, phase_list
from crawler.fino_dart.db import (
    connect_db, count_filings, init_schema, upsert_corps, upsert_filing,
)
from crawler.fino_dart.models import Corp, Filing


def _conn(tmp_path: Path):
    c = connect_db(tmp_path / "d.db")
    init_schema(c)
    return c


def _client(tmp_path, handler):
    return DartClient("KEY", _conn_shared, delay=0, transport=httpx.MockTransport(handler))


def test_phase_list_pages_and_upserts(tmp_path: Path, monkeypatch) -> None:
    conn = _conn(tmp_path)

    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params.get("page_no", "1"))
        if page == 1:
            return httpx.Response(200, json={"status": "000", "total_page": 1, "list": [
                {"rcept_no": "R1", "corp_code": "C1", "corp_name": "회사1", "stock_code": "001",
                 "corp_cls": "Y", "report_nm": "사업보고서 (2025.12)", "rcept_dt": "20260331",
                 "flr_nm": "회사1", "rm": ""}]})
        return httpx.Response(200, json={"status": "013"})

    client = DartClient("KEY", conn, delay=0, transport=httpx.MockTransport(handler))
    n = phase_list(client, conn, months=3, corp_cls=("Y",), today="20260709")
    assert count_filings(conn) >= 1
    assert n >= 1


def test_phase_docs_saves_zip(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    upsert_filing(conn, Filing("R1", "C1", "회사1", "001", "Y", "사업보고서 (2025.12)",
                               "A", "20260331", "회사1", ""))

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"PK\x03\x04FAKEZIP")

    client = DartClient("KEY", conn, delay=0, transport=httpx.MockTransport(handler))
    docs = tmp_path / "docs"
    n = phase_docs(client, conn, docs)
    assert n == 1
    row = conn.execute("SELECT local_path, status, bytes FROM documents WHERE rcp_no='R1'").fetchone()
    assert row["status"] == "ok" and row["bytes"] > 0
    assert Path(row["local_path"]).exists()


def test_phase_financials_only_regular_A(tmp_path: Path) -> None:
    conn = _conn(tmp_path)
    upsert_filing(conn, Filing("R1", "C1", "삼성", "001", "Y", "사업보고서 (2025.12)",
                               "A", "20260331", "삼성", ""))
    upsert_filing(conn, Filing("R2", "C2", "감사", "002", "Y", "감사보고서 (2025.12)",
                               "F", "20260331", "감사", ""))

    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.params.get("reprt_code"))
        return httpx.Response(200, json={"status": "000", "list": [{"account_nm": "자산총계"}]})

    client = DartClient("KEY", conn, delay=0, transport=httpx.MockTransport(handler))
    n = phase_financials(client, conn)
    # R1(사업보고서)만 대상, CFS+OFS 2회
    assert n == 2
    rows = conn.execute("SELECT DISTINCT rcp_no FROM financials").fetchall()
    assert [r["rcp_no"] for r in rows] == ["R1"]
    assert all(code == "11011" for code in seen)
```

Note: 첫 헬퍼 `_client`/`_conn_shared`는 사용하지 않으니 작성 시 제거하고, 각 테스트에서 client를 직접 만든다(위 3개 테스트가 정답 형태).

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_dart_collect.py -q`
Expected: FAIL — `ModuleNotFoundError` (collect 모듈 없음)

- [ ] **Step 3: Write implementation**

`crawler/fino_dart/collect.py`:

```python
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from pathlib import Path

from .client import DartClient, QuotaExhausted
from .db import (
    connect_db, count_filings, filings_without_doc, get_quota, incr_quota,
    init_schema, regular_filings_needing_fs, upsert_corps, upsert_document,
    upsert_filing, upsert_financial,
)
from .models import FinancialRec
from .parsers import parse_corpcode_zip, parse_list_rows
from .sources import CORP_CLS, PBLNTF_TYPES, date_windows, report_to_reprt

DEFAULT_DB = Path("data/fino_dart.db")
DEFAULT_DOCS = Path("data/fino_dart_docs")
FS_DIVS = ("CFS", "OFS")


def phase_corpcode(client: DartClient, conn) -> int:
    zip_bytes = client.get_bytes("/corpCode.xml", {})
    corps = parse_corpcode_zip(zip_bytes)
    upsert_corps(conn, corps)
    print(f"[corpcode] 상장사 {len(corps)}개", flush=True)
    return len(corps)


def phase_list(client: DartClient, conn, months: int, corp_cls, today: str) -> int:
    added = 0
    for pblntf in PBLNTF_TYPES:
        for cls in corp_cls:
            for start, end in date_windows(months, today):
                page = 1
                while True:
                    data = client.get_json("/list.json", {
                        "pblntf_ty": pblntf, "corp_cls": cls,
                        "bgn_de": start, "end_de": end,
                        "page_no": page, "page_count": 100,
                    })
                    rows = parse_list_rows(data, pblntf)
                    for f in rows:
                        upsert_filing(conn, f)
                    added += len(rows)
                    total_page = int(data.get("total_page") or 1)
                    if data.get("status") != "000" or page >= total_page:
                        break
                    page += 1
            print(f"[list] {pblntf}/{cls}: 누적 filings {count_filings(conn)}", flush=True)
    return added


def phase_docs(client: DartClient, conn, docs_dir: Path) -> int:
    done = 0
    for f in filings_without_doc(conn):
        rcp = f["rcp_no"]
        content = client.get_bytes("/document.xml", {"rcept_no": rcp})
        if content[:2] == b"PK" and len(content) > 0:      # zip 시그니처
            sub = docs_dir / rcp[:6]
            sub.mkdir(parents=True, exist_ok=True)
            path = sub / f"{rcp}.zip"
            path.write_bytes(content)
            upsert_document(conn, rcp, local_path=str(path), bytes_=len(content), status="ok")
        else:
            upsert_document(conn, rcp, local_path="", bytes_=len(content), status="empty")
        done += 1
    print(f"[docs] 원문 {done}건 처리", flush=True)
    return done


def phase_financials(client: DartClient, conn) -> int:
    done = 0
    for fs_div in FS_DIVS:
        for f in regular_filings_needing_fs(conn, fs_div):
            mapping = report_to_reprt(f["report_nm"])
            if mapping is None:
                continue
            reprt_code, bsns_year = mapping
            data = client.get_json("/fnlttSinglAcntAll.json", {
                "corp_code": f["corp_code"], "bsns_year": bsns_year,
                "reprt_code": reprt_code, "fs_div": fs_div,
            })
            status = data.get("status")
            fs_json = json.dumps(data.get("list") or [], ensure_ascii=False) if status == "000" else "[]"
            upsert_financial(conn, FinancialRec(
                rcp_no=f["rcp_no"], bsns_year=bsns_year, reprt_code=reprt_code,
                fs_div=fs_div, fs_json=fs_json, status=("ok" if status == "000" else "none"),
            ))
            done += 1
    print(f"[financials] 재무제표 {done}건 처리", flush=True)
    return done


def collect(*, db_path: Path, docs_dir: Path, api_key: str, months: int,
            phases: tuple, corp_cls: tuple, limit: int, delay: float, today: str) -> dict:
    result: dict = {}
    conn = connect_db(db_path)
    init_schema(conn)
    client = DartClient(api_key, conn, limit=limit, delay=delay)
    try:
        if "corpcode" in phases:
            result["corpcode"] = phase_corpcode(client, conn)
        if "list" in phases:
            result["list"] = phase_list(client, conn, months, corp_cls, today)
        if "docs" in phases:
            result["docs"] = phase_docs(client, conn, docs_dir)
        if "financials" in phases:
            result["financials"] = phase_financials(client, conn)
    except QuotaExhausted as exc:
        result["stopped"] = str(exc)
        print(f"[quota] {exc}", flush=True)
    result["today_calls"] = get_quota(conn, client.today())
    result["total_filings"] = count_filings(conn)
    conn.close()
    return result


_ALL_PHASES = ("corpcode", "list", "docs", "financials")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m crawler.fino_dart.collect")
    _ = p.add_argument("--db-path", type=Path, default=DEFAULT_DB)
    _ = p.add_argument("--docs-dir", type=Path, default=DEFAULT_DOCS)
    _ = p.add_argument("--phase", type=str, default="all")
    _ = p.add_argument("--months", type=int, default=12)
    _ = p.add_argument("--corp-cls", type=str, default=",".join(CORP_CLS))
    _ = p.add_argument("--limit", type=int, default=19500)
    _ = p.add_argument("--delay-seconds", type=float, default=0.3)
    return p


def main() -> int:
    args = build_parser().parse_args()
    api_key = os.environ.get("DART_API_KEY", "")
    if not api_key:
        raise SystemExit("DART_API_KEY 환경변수가 없습니다(.env 로드 필요)")
    phases = _ALL_PHASES if args.phase == "all" else tuple(args.phase.split(","))
    corp_cls = tuple(c.strip() for c in args.corp_cls.split(",") if c.strip())
    today = datetime.now().strftime("%Y%m%d")
    res = collect(db_path=args.db_path, docs_dir=args.docs_dir, api_key=api_key,
                  months=args.months, phases=phases, corp_cls=corp_cls,
                  limit=args.limit, delay=args.delay_seconds, today=today)
    print(f"done: {res}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

`crawler/fino_dart/__main__.py`:

```python
from .collect import main

raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_dart_collect.py -q`
Expected: PASS (3 tests). 이어서 전체 모듈: `.venv/bin/python -m pytest tests/test_fino_dart_*.py -q` 전부 PASS.

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_dart/collect.py crawler/fino_dart/__main__.py tests/test_fino_dart_collect.py
git commit -m "feat(fino_dart): 4단계 파이프라인(corpcode/list/docs/financials) + CLI — 재개·쿼터 종료"
```

---

### Task 6: 라이브 스모크 + fino_ops 편입 + 문서

**Files:**
- Modify: `crawler/fino_ops/corpora.py` (dart 코퍼스 1개 추가)
- Modify: `docs/2026-07-02_fino_corpus_crawler_handoff.md` (dart 항목)
- Test: 기존 `tests/test_fino_ops_corpora.py`가 6→7 코퍼스로 바뀌므로 해당 단언 갱신

**Interfaces:**
- Consumes: Task 5 collect CLI, fino_ops CORPORA 패턴

- [ ] **Step 1: 라이브 스모크** (소량 실제 수집 — .env 로드 후)

```bash
cd /data_raid/ruci_workspace/frwaler
set -a; . ./.env; set +a
# 최근 1개월·유가만·소량으로 list→docs→financials 각 단계 실동작 확인
.venv/bin/python -m crawler.fino_dart.collect --phase corpcode --limit 50
.venv/bin/python -m crawler.fino_dart.collect --phase list --months 1 --corp-cls Y --limit 200
.venv/bin/python -m crawler.fino_dart.collect --phase docs --limit 30
.venv/bin/python -m crawler.fino_dart.collect --phase financials --limit 30
```
Expected: corpcode 상장사 수천 개, list filings 수십~수백, docs 원문 zip 저장(data/fino_dart_docs/), financials 재무제표 저장. 각 `done: {...}`에 today_calls·total_filings 표시.

- [ ] **Step 2: 스모크 결과 검증**

```bash
.venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect("data/fino_dart.db"); c.row_factory = sqlite3.Row
print("corps:", c.execute("SELECT COUNT(*) FROM corps").fetchone()[0])
print("filings by pblntf:", {r[0]: r[1] for r in c.execute("SELECT pblntf_ty, COUNT(*) FROM filings GROUP BY pblntf_ty")})
print("docs ok:", c.execute("SELECT COUNT(*) FROM documents WHERE status='ok'").fetchone()[0])
print("financials:", c.execute("SELECT status, COUNT(*) FROM financials GROUP BY status").fetchall())
r = c.execute("SELECT rcp_no, corp_name, report_nm FROM filings LIMIT 3").fetchall()
for x in r: print(" ", x["rcp_no"], x["corp_name"], x["report_nm"][:30])
EOF
```
Expected: corps>0, filings 존재, docs ok>0, financials 존재. 이상 있으면 STOP·보고.

- [ ] **Step 3: fino_ops에 dart 코퍼스 추가**

`crawler/fino_ops/corpora.py`의 `CORPORA` dict에서 `"std"` 항목 다음, `"nts_qt"` 앞에 추가:

```python
    "dart": Corpus(
        key="dart", label="DART 공시(정기·외감)", db_path=REPO_ROOT / "data" / "fino_dart.db",
        count_sql="SELECT COUNT(*) FROM filings",
        freshness_sql="SELECT MAX(collected_at) FROM filings",
        argv=(_PY, "-m", "crawler.fino_dart.collect", "--phase", "all"),
        env={"DART_API_KEY": os.environ.get("DART_API_KEY", "")},
    ),
```
(파일 상단에 `import os`가 이미 있음 — 확인. env는 refresh subprocess에 키 전달용.)

- [ ] **Step 4: fino_ops 테스트 갱신**

`tests/test_fino_ops_corpora.py`의 `test_registry_has_six_corpora_with_expected_wiring`에서 키 집합 단언을 7개로 수정:

```python
    assert set(CORPORA) == {"law", "exec", "acct", "std", "dart", "nts_qt", "nts_pd"}
```
그리고 함수명이 six를 언급하면 `test_registry_corpora_with_expected_wiring`로 바꾸고, dart 배선 단언 추가:

```python
    assert "--phase" in CORPORA["dart"].argv and CORPORA["dart"].db_path.name == "fino_dart.db"
```

Run: `.venv/bin/python -m pytest tests/test_fino_ops_*.py -q`
Expected: 전부 PASS.

- [ ] **Step 5: 전체 회귀 + 문서 갱신 + 커밋**

Run: `.venv/bin/python -m pytest tests/test_fino_dart_*.py tests/test_fino_ops_*.py -q`
Expected: 전부 PASS.

`docs/2026-07-02_fino_corpus_crawler_handoff.md`에 dart 크롤러 한 줄 추가(§1 또는 §3 아키텍처에 `crawler/fino_dart/` → `data/fino_dart.db`, 상장사 A+F 공시, 스펙 포인터).

```bash
git add crawler/fino_ops/corpora.py tests/test_fino_ops_corpora.py docs/2026-07-02_fino_corpus_crawler_handoff.md
git commit -m "feat(fino_dart): fino_ops에 dart 코퍼스 편입 + 라이브 스모크 검증 + 핸드오프 갱신"
```

- [ ] **Step 6: 메모리 갱신 (컨트롤러 수행)**

`fino-dart-disclosure-crawler` 메모리 신규(엔드포인트·쿼터/재개·실행법·스키마) + MEMORY.md 인덱스 한 줄.

- [ ] **Step 7: 전체 수집 착수 결정** — 스모크·리뷰 통과 후, 전체 12개월 수집(`--phase all`, 3~4일 재개)을 백그라운드로 착수할지 사용자에게 확인.

---

## Self-Review 결과

- **스펙 커버리지**: 모델/상수(§소스·재무매핑)=Task 1, 스키마/재개/쿼터(§데이터모델·레이트리밋)=Task 2, 파서(§소스)=Task 3, 클라이언트/쿼터게이트(§레이트리밋)=Task 4, 4단계 파이프라인(§수집 파이프라인)=Task 5, 라이브 스모크·fino_ops편입(§검증·fino_ops)=Task 6. 갭 없음. 전체 수집 실행(Open Question)은 Task 6 Step 7에서 사용자 결정.
- **플레이스홀더 스캔**: 모든 코드 스텝 전체 코드 포함. Task 5 테스트의 미사용 헬퍼는 Note로 제거 지시 명시.
- **타입 일관성**: `Filing`/`Corp`/`FinancialRec` 필드가 models(T1)↔db(T2)↔parsers(T3)↔collect(T5) 일치. `DartClient(api_key, conn, *, limit, delay, transport)` 시그니처가 T4 정의·T5 사용처 일치. `report_to_reprt`(T1)→collect financials(T5), `date_windows`(T1)→phase_list(T5), `regular_filings_needing_fs(conn, fs_div)`(T2)→phase_financials(T5) 일치. `get_quota/incr_quota`(T2)→client(T4) 일치.
- **알려진 한계**: 원문 zip은 저장까지(텍스트 추출 후속, 스펙 Open Q). 전체 수집은 3~4일 재개 작업. corp_cls N 포함 기본(축소는 `--corp-cls Y,K`).
