# FINO 세법 코퍼스 수집기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** law.go.kr OpenAPI로 세법 법령(법·령·규칙)을, taxlaw.nts.go.kr로 조문형 세법집행기준을 수집해 `fino_law.db`에 저장하고 조문별 원문 deep-link(citation)를 포함한 MD/NDJSON으로 export한다.

**Architecture:** `crawler/fino_acct/`와 동일 패턴의 신규 형제 모듈 `crawler/fino_law/`. 법령은 law.go.kr OpenAPI(httpx, JSON)로, 집행기준은 taxlaw.nts.go.kr `action.do`(curl, 기존 `nts_taxlaw.py` 메커니즘 재사용)로 수집. `documents`/`articles` 2테이블을 `source_kind`(law|exec_standard)로 공유.

**Tech Stack:** Python 3.12, httpx(이미 설치됨), sqlite3, pytest. 외부 키 OC=`fino-law-data`(law.go.kr).

**Spec:** `docs/superpowers/specs/2026-06-30-fino-law-corpus-collection-design.md`
**정찰 메모리:** `fino-law-corpus-collection-endpoints`

---

## File Structure

```
crawler/fino_law/
  __init__.py          # 빈 패키지 마커
  models.py            # LawTarget, ArticleRecord, DocumentRecord 데이터클래스
  db.py                # connect_db / init_schema / upsert_document / upsert_article
  sources.py           # LAW_TARGETS (국세+지방세 법령 리스트)
  fetch_law.py         # law.go.kr OpenAPI httpx 클라이언트
  parsers_law.py       # 법령 검색/본문 JSON → DocumentRecord + [ArticleRecord]
  collect.py           # CLI 오케스트레이션 (--law / --exec / --all)
  export_markdown.py   # 문서당 1 MD (exec_standard 호환 frontmatter)
  export_ndjson.py     # 조문당 1 NDJSON 레코드 + source_url
  fetch_exec.py        # (Phase 3 이후) taxlaw action.do 클라이언트
  parsers_exec.py      # (Phase 3 이후) 조문형 집행기준 파서
tests/
  test_fino_law_db.py
  test_fino_law_parsers.py
  test_fino_law_sources.py
  test_fino_law_export.py
  fixtures/
    law_lawinfo_법인세법.json     # Phase 1 spike에서 저장
    exec_jiphaeng_법인.json       # Phase 3 spike에서 저장
```

Tests run from repo root with `.venv/bin/python -m pytest`.

---

## Phase 1 — C-1 세법 법령 (law.go.kr OpenAPI)

### Task 1: 모듈 스캐폴드 + DB 스키마

**Files:**
- Create: `crawler/fino_law/__init__.py`
- Create: `crawler/fino_law/models.py`
- Create: `crawler/fino_law/db.py`
- Test: `tests/test_fino_law_db.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_law_db.py
from pathlib import Path
import sqlite3

from crawler.fino_law.db import connect_db, init_schema, upsert_document, upsert_article


def _dump(conn: sqlite3.Connection) -> str:
    return "\n".join(conn.iterdump())


def test_upsert_document_then_article_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "law.db"
    with connect_db(db_path) as conn:
        init_schema(conn)
        for _ in range(2):  # 두 번 실행해도 row 수 불변(멱등)
            doc_id = upsert_document(
                conn,
                source_kind="law",
                external_id="001563",
                title="법인세법",
                category="법률",
                org="기획재정부",
                promulgated_at="20251001",
                effective_at="20260102",
                version_code="현행",
                source_url="https://www.law.go.kr/법령/법인세법",
            )
            upsert_article(
                conn,
                document_id=doc_id,
                article_no="제1조",
                article_title="목적",
                body_text="이 법은 ...",
                clause_json="[]",
                source_url="https://www.law.go.kr/법령/법인세법#제1조",
                seq=1,
            )
        dump = _dump(conn)

    assert dump.count("'법인세법'") == 1
    assert dump.count("'제1조'") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_law_db.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'crawler.fino_law'`

- [ ] **Step 3: Write minimal implementation**

```python
# crawler/fino_law/__init__.py
```
(empty file)

```python
# crawler/fino_law/db.py
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
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_kind TEXT NOT NULL,
            external_id TEXT NOT NULL,
            title TEXT NOT NULL,
            category TEXT NOT NULL,
            org TEXT NOT NULL,
            promulgated_at TEXT NOT NULL,
            effective_at TEXT NOT NULL,
            version_code TEXT NOT NULL,
            source_url TEXT NOT NULL,
            collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_kind, external_id)
        );

        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            article_no TEXT NOT NULL,
            article_title TEXT NOT NULL,
            body_text TEXT NOT NULL,
            clause_json TEXT NOT NULL DEFAULT '[]',
            source_url TEXT NOT NULL,
            seq INTEGER NOT NULL,
            UNIQUE(document_id, article_no, seq)
        );

        CREATE INDEX IF NOT EXISTS idx_documents_kind ON documents(source_kind);
        CREATE INDEX IF NOT EXISTS idx_articles_document_id ON articles(document_id);
        """
    )
    conn.commit()


def upsert_document(
    conn: sqlite3.Connection,
    *,
    source_kind: str,
    external_id: str,
    title: str,
    category: str,
    org: str,
    promulgated_at: str,
    effective_at: str,
    version_code: str,
    source_url: str,
) -> int:
    conn.execute(
        """
        INSERT INTO documents (
            source_kind, external_id, title, category, org,
            promulgated_at, effective_at, version_code, source_url
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_kind, external_id) DO UPDATE SET
            title = excluded.title,
            category = excluded.category,
            org = excluded.org,
            promulgated_at = excluded.promulgated_at,
            effective_at = excluded.effective_at,
            version_code = excluded.version_code,
            source_url = excluded.source_url,
            collected_at = CURRENT_TIMESTAMP
        """,
        (source_kind, external_id, title, category, org,
         promulgated_at, effective_at, version_code, source_url),
    )
    conn.commit()
    row = conn.execute(
        "SELECT id FROM documents WHERE source_kind = ? AND external_id = ?",
        (source_kind, external_id),
    ).fetchone()
    return int(row["id"])


def upsert_article(
    conn: sqlite3.Connection,
    *,
    document_id: int,
    article_no: str,
    article_title: str,
    body_text: str,
    clause_json: str,
    source_url: str,
    seq: int,
) -> None:
    conn.execute(
        """
        INSERT INTO articles (
            document_id, article_no, article_title, body_text, clause_json, source_url, seq
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document_id, article_no, seq) DO UPDATE SET
            article_title = excluded.article_title,
            body_text = excluded.body_text,
            clause_json = excluded.clause_json,
            source_url = excluded.source_url
        """,
        (document_id, article_no, article_title, body_text, clause_json, source_url, seq),
    )
    conn.commit()
```

```python
# crawler/fino_law/models.py
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class LawTarget:
    name: str       # 법령명 정확명 (예: "법인세법")
    category: str   # 법률 / 시행령 / 시행규칙
    tax_scope: str  # 국세 / 지방세


@dataclass(frozen=True, slots=True)
class ArticleRecord:
    article_no: str
    article_title: str
    body_text: str
    clause_json: str
    seq: int
    source_url: str = ""


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    source_kind: str
    external_id: str
    title: str
    category: str
    org: str
    promulgated_at: str
    effective_at: str
    version_code: str
    source_url: str
    articles: tuple[ArticleRecord, ...] = field(default_factory=tuple)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_law_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_law/__init__.py crawler/fino_law/db.py crawler/fino_law/models.py tests/test_fino_law_db.py
git commit -m "feat(fino_law): module scaffold + documents/articles schema"
```

---

### Task 2: 법령 대상 레지스트리 (`sources.py`)

**Files:**
- Create: `crawler/fino_law/sources.py`
- Test: `tests/test_fino_law_sources.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_law_sources.py
from crawler.fino_law.sources import LAW_TARGETS


def test_law_targets_cover_core_national_and_local_taxes() -> None:
    names = {t.name for t in LAW_TARGETS}
    assert "법인세법" in names
    assert "소득세법" in names
    assert "부가가치세법" in names
    assert "지방세법" in names
    # 각 법령은 법률/시행령/시행규칙 3종이 있어야 함
    categories = {(t.name, t.category) for t in LAW_TARGETS}
    assert ("법인세법", "법률") in categories
    assert ("법인세법", "시행령") in categories
    assert ("법인세법", "시행규칙") in categories


def test_law_targets_are_unique() -> None:
    keys = [(t.name, t.category) for t in LAW_TARGETS]
    assert len(keys) == len(set(keys))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_law_sources.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crawler/fino_law/sources.py
from typing import Final

from .models import LawTarget

# (법률명, 국세/지방세). 각 항목은 법률/시행령/시행규칙 3종으로 전개된다.
_NATIONAL_BASES: Final[tuple[str, ...]] = (
    "국세기본법", "국세징수법", "법인세법", "소득세법", "부가가치세법",
    "상속세 및 증여세법", "조세특례제한법", "종합부동산세법",
    "국제조세조정에 관한 법률", "개별소비세법", "교육세법",
    "농어촌특별세법", "증권거래세법", "인지세법", "주세법",
)
_LOCAL_BASES: Final[tuple[str, ...]] = (
    "지방세기본법", "지방세징수법", "지방세법", "지방세특례제한법",
)

# 시행령/시행규칙 접미사 규칙: "X법" → "X법 시행령" / "X법 시행규칙",
#   "...에 관한 법률" → "...에 관한 법률 시행령" 등 단순 접미.
_SUFFIXES: Final[tuple[tuple[str, str], ...]] = (
    ("법률", ""),
    ("시행령", " 시행령"),
    ("시행규칙", " 시행규칙"),
)


def _expand(bases: tuple[str, ...], scope: str) -> list[LawTarget]:
    out: list[LawTarget] = []
    for base in bases:
        for category, suffix in _SUFFIXES:
            out.append(LawTarget(name=f"{base}{suffix}", category=category, tax_scope=scope))
    return out


LAW_TARGETS: Final[tuple[LawTarget, ...]] = tuple(
    _expand(_NATIONAL_BASES, "국세") + _expand(_LOCAL_BASES, "지방세")
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_law_sources.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_law/sources.py tests/test_fino_law_sources.py
git commit -m "feat(fino_law): 국세+지방세 법령 대상 레지스트리"
```

---

### Task 3: law.go.kr 응답 fixture 확보 (spike, 네트워크)

**Files:**
- Create: `tests/fixtures/law_search_법인세법.json`
- Create: `tests/fixtures/law_service_법인세법.json`

- [ ] **Step 1: 실제 API 응답을 fixture로 저장**

Run:
```bash
OC=fino-law-data
.venv/bin/python - <<'PY'
import httpx, json, os
OC = "fino-law-data"
with httpx.Client(timeout=30) as c:
    s = c.get("http://www.law.go.kr/DRF/lawSearch.do",
              params={"OC": OC, "target": "law", "type": "JSON", "query": "법인세법", "display": "5"})
    json.dump(s.json(), open("tests/fixtures/law_search_법인세법.json", "w"), ensure_ascii=False, indent=2)
    items = s.json()["LawSearch"]["law"]
    items = items if isinstance(items, list) else [items]
    # 법령명 정확 일치 + 현행
    mst = next(i["법령일련번호"] for i in items
               if i["법령명한글"] == "법인세법" and i["현행연혁코드"] == "현행")
    d = c.get("http://www.law.go.kr/DRF/lawService.do",
              params={"OC": OC, "target": "law", "type": "JSON", "MST": mst})
    json.dump(d.json(), open("tests/fixtures/law_service_법인세법.json", "w"), ensure_ascii=False, indent=2)
    art = d.json()["법령"]["조문"]["조문단위"]
    print("조문 수:", len(art) if isinstance(art, list) else 1)
PY
```
Expected: "조문 수: 255" (또는 유사 양수), fixture 2개 생성.

- [ ] **Step 2: 조문단위 실제 키 구조 확인**

Run:
```bash
.venv/bin/python - <<'PY'
import json
d = json.load(open("tests/fixtures/law_service_법인세법.json"))
art = d["법령"]["조문"]["조문단위"]
art = art if isinstance(art, list) else [art]
sample = next(a for a in art if a.get("조문여부") == "조문")
print("조문단위 keys:", list(sample.keys()))
for k in ("조문번호", "조문제목", "조문내용", "조문여부"):
    print(f"  {k} = {str(sample.get(k))[:60]}")
PY
```
Expected: `조문번호`, `조문제목`, `조문내용`, `조문여부`(='조문' vs '전문'/편장절) 등 키 출력. **이 출력으로 Task 4 파서 필드명을 확정한다.**

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/law_search_법인세법.json tests/fixtures/law_service_법인세법.json
git commit -m "test(fino_law): law.go.kr 법인세법 응답 fixture"
```

---

### Task 4: 법령 파서 (`parsers_law.py`)

**Files:**
- Create: `crawler/fino_law/parsers_law.py`
- Test: `tests/test_fino_law_parsers.py`

> 파서는 Task 3 Step 2에서 확인한 실제 키(`조문번호`/`조문제목`/`조문내용`/`조문여부`)를 사용한다. `조문단위`는 단일 결과면 dict, 복수면 list로 오므로 정규화한다. `조문여부 != '조문'`(편/장/절 헤더, 부칙)은 건너뛴다.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_law_parsers.py
import json
from pathlib import Path

from crawler.fino_law.parsers_law import parse_search_for_current, parse_law_service


FIX = Path("tests/fixtures")


def test_parse_search_picks_current_exact_match() -> None:
    data = json.load(open(FIX / "law_search_법인세법.json"))
    hit = parse_search_for_current(data, "법인세법")
    assert hit is not None
    assert hit["법령명한글"] == "법인세법"
    assert hit["현행연혁코드"] == "현행"
    assert hit["법령일련번호"]  # MST 존재


def test_parse_law_service_extracts_articles_with_citation_url() -> None:
    data = json.load(open(FIX / "law_service_법인세법.json"))
    doc = parse_law_service(data, name="법인세법", category="법률", external_id="001563")
    assert doc.source_kind == "law"
    assert doc.source_url == "https://www.law.go.kr/법령/법인세법"
    assert len(doc.articles) > 100
    first = doc.articles[0]
    assert first.article_no.startswith("제")
    assert first.source_url.startswith("https://www.law.go.kr/법령/법인세법")
    assert first.body_text  # 본문 비어있지 않음
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_law_parsers.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crawler/fino_law/parsers_law.py
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote

from .models import ArticleRecord, DocumentRecord


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def law_citation_url(name: str) -> str:
    return f"https://www.law.go.kr/법령/{name}"


def parse_search_for_current(data: dict, name: str) -> dict | None:
    """lawSearch 응답에서 법령명 정확 일치 + 현행 1건 반환."""
    items = _as_list(data.get("LawSearch", {}).get("law"))
    for item in items:
        if item.get("법령명한글") == name and item.get("현행연혁코드") == "현행":
            return item
    return None


def parse_law_service(data: dict, *, name: str, category: str, external_id: str) -> DocumentRecord:
    """lawService 응답 → DocumentRecord(+articles). 조문만 추출."""
    root = data.get("법령", {})
    basic = root.get("기본정보", {})
    org = ""
    dept = basic.get("소관부처")
    if isinstance(dept, dict):
        org = dept.get("content", "") or dept.get("소관부처명", "")
    elif isinstance(dept, str):
        org = dept
    promulgated = str(basic.get("공포일자", "") or "")
    effective = str(basic.get("시행일자", "") or "")

    base_url = law_citation_url(name)
    articles: list[ArticleRecord] = []
    seq = 0
    for unit in _as_list(root.get("조문", {}).get("조문단위")):
        if unit.get("조문여부") != "조문":
            continue
        seq += 1
        article_no = str(unit.get("조문번호", "") or "").strip()
        # API의 조문번호는 "1" 형태일 수 있어 "제N조"로 정규화
        label = article_no if article_no.startswith("제") else f"제{article_no}조"
        clauses = _as_list(unit.get("항"))
        articles.append(
            ArticleRecord(
                article_no=label,
                article_title=str(unit.get("조문제목", "") or "").strip(),
                body_text=str(unit.get("조문내용", "") or "").strip(),
                clause_json=json.dumps(clauses, ensure_ascii=False),
                seq=seq,
                source_url=f"{base_url}#{label}",
            )
        )

    return DocumentRecord(
        source_kind="law",
        external_id=external_id,
        title=name,
        category=category,
        org=org,
        promulgated_at=promulgated,
        effective_at=effective,
        version_code="현행",
        source_url=base_url,
        articles=tuple(articles),
    )
```

(`ArticleRecord.source_url`은 Task 1 models.py에 이미 정의됨 — 여기서 `{base_url}#{label}`로 채운다.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_law_parsers.py tests/test_fino_law_db.py -v`
Expected: PASS (db 테스트의 upsert_article 호출과 필드 정합 확인)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_law/parsers_law.py crawler/fino_law/models.py tests/test_fino_law_parsers.py
git commit -m "feat(fino_law): 법령 검색/본문 파서 + 조문 citation URL"
```

---

### Task 5: law.go.kr fetch 클라이언트 (`fetch_law.py`)

**Files:**
- Create: `crawler/fino_law/fetch_law.py`

> 네트워크 클라이언트는 단위 테스트하지 않는다(통합은 Task 7 CLI smoke). 파서가 분리돼 있어 로직 테스트는 Task 4가 커버.

- [ ] **Step 1: Write implementation**

```python
# crawler/fino_law/fetch_law.py
from __future__ import annotations

import os
import time

import httpx

_OC = os.environ.get("LAW_API_OC", "fino-law-data")
_SEARCH = "http://www.law.go.kr/DRF/lawSearch.do"
_SERVICE = "http://www.law.go.kr/DRF/lawService.do"


def _get(client: httpx.Client, url: str, params: dict, delay: float) -> dict:
    last: Exception | None = None
    for attempt in range(3):
        if delay > 0:
            time.sleep(delay)
        try:
            r = client.get(url, params=params)
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ValueError) as exc:
            last = exc
            time.sleep(1 + attempt)
    raise RuntimeError(f"law.go.kr fetch failed: {url}") from last


def search_law(client: httpx.Client, name: str, delay: float = 0.3) -> dict:
    return _get(client, _SEARCH,
                {"OC": _OC, "target": "law", "type": "JSON", "query": name, "display": "10"}, delay)


def fetch_law_service(client: httpx.Client, mst: str, delay: float = 0.3) -> dict:
    return _get(client, _SERVICE,
                {"OC": _OC, "target": "law", "type": "JSON", "MST": mst}, delay)
```

- [ ] **Step 2: Commit**

```bash
git add crawler/fino_law/fetch_law.py
git commit -m "feat(fino_law): law.go.kr OpenAPI httpx 클라이언트"
```

---

### Task 6: collect 오케스트레이션 + CLI (`collect.py`)

**Files:**
- Create: `crawler/fino_law/collect.py`
- Test: extend `tests/test_fino_law_db.py` (CLI --help)

- [ ] **Step 1: Write the failing test (CLI help)**

```python
# tests/test_fino_law_db.py 에 추가
import subprocess
import sys


def test_cli_help_when_invoked_as_module() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "crawler.fino_law.collect", "--help"],
        check=False, capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "--db-path" in result.stdout
    assert "--law" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_law_db.py::test_cli_help_when_invoked_as_module -v`
Expected: FAIL with `No module named crawler.fino_law.collect`

- [ ] **Step 3: Write minimal implementation**

```python
# crawler/fino_law/collect.py
from __future__ import annotations

import argparse
from pathlib import Path

import httpx

from .db import connect_db, init_schema, upsert_article, upsert_document
from .fetch_law import fetch_law_service, search_law
from .parsers_law import parse_law_service, parse_search_for_current
from .sources import LAW_TARGETS

DEFAULT_DB_PATH = Path("data/fino_law.db")


def collect_law(*, db_path: Path, delay: float) -> tuple[int, int]:
    docs = 0
    arts = 0
    with connect_db(db_path) as conn, httpx.Client(timeout=30) as client:
        init_schema(conn)
        for target in LAW_TARGETS:
            search = search_law(client, target.name, delay)
            hit = parse_search_for_current(search, target.name)
            if hit is None:
                print(f"[skip] 현행 미매칭: {target.name}", flush=True)
                continue
            service = fetch_law_service(client, hit["법령일련번호"], delay)
            doc = parse_law_service(
                service, name=target.name, category=target.category,
                external_id=str(hit["법령ID"]),
            )
            doc_id = upsert_document(
                conn, source_kind=doc.source_kind, external_id=doc.external_id,
                title=doc.title, category=doc.category, org=doc.org,
                promulgated_at=doc.promulgated_at, effective_at=doc.effective_at,
                version_code=doc.version_code, source_url=doc.source_url,
            )
            for a in doc.articles:
                upsert_article(
                    conn, document_id=doc_id, article_no=a.article_no,
                    article_title=a.article_title, body_text=a.body_text,
                    clause_json=a.clause_json, source_url=a.source_url, seq=a.seq,
                )
            docs += 1
            arts += len(doc.articles)
            print(f"[law] {target.name}: 조문 {len(doc.articles)}", flush=True)
    return docs, arts


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m crawler.fino_law.collect")
    _ = p.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    _ = p.add_argument("--law", action="store_true", help="법령 수집(law.go.kr)")
    _ = p.add_argument("--exec", action="store_true", help="조문형 집행기준 수집(taxlaw)")
    _ = p.add_argument("--all", action="store_true", help="법령+집행기준 모두")
    _ = p.add_argument("--delay-seconds", type=float, default=0.3)
    return p


def main() -> int:
    args = build_parser().parse_args()
    run_law = args.law or args.all
    if run_law:
        d, a = collect_law(db_path=args.db_path, delay=args.delay_seconds)
        print(f"done law: documents={d} articles={a}")
    if args.exec or args.all:
        print("exec(집행기준) 수집은 Phase 3 이후 활성화됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test + 소규모 실수집 smoke**

Run: `.venv/bin/python -m pytest tests/test_fino_law_db.py -v`
Expected: PASS

Smoke (네트워크, 1개 법령만 — sources를 일시 축소하지 말고 Ctrl-C로 첫 건 확인):
Run: `.venv/bin/python -m crawler.fino_law.collect --law --db-path /tmp/law_smoke.db --delay-seconds 0.2` (몇 건 출력되면 중단)
Expected: `[law] 국세기본법: 조문 N` 형태 로그.

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_law/collect.py tests/test_fino_law_db.py
git commit -m "feat(fino_law): collect 오케스트레이션 + CLI(--law/--exec/--all)"
```

---

## Phase 2 — Export (MD + NDJSON)

### Task 7: export_markdown + export_ndjson

**Files:**
- Create: `crawler/fino_law/export_markdown.py`
- Create: `crawler/fino_law/export_ndjson.py`
- Test: `tests/test_fino_law_export.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_law_export.py
import json
from pathlib import Path

from crawler.fino_law.db import connect_db, init_schema, upsert_article, upsert_document
from crawler.fino_law.export_markdown import export_markdown
from crawler.fino_law.export_ndjson import export_ndjson


def _seed(db_path: Path) -> None:
    with connect_db(db_path) as conn:
        init_schema(conn)
        doc_id = upsert_document(
            conn, source_kind="law", external_id="001563", title="법인세법",
            category="법률", org="기획재정부", promulgated_at="20251001",
            effective_at="20260102", version_code="현행",
            source_url="https://www.law.go.kr/법령/법인세법",
        )
        upsert_article(
            conn, document_id=doc_id, article_no="제1조", article_title="목적",
            body_text="이 법은 ...", clause_json="[]",
            source_url="https://www.law.go.kr/법령/법인세법#제1조", seq=1,
        )


def test_export_markdown_writes_one_file_per_document(tmp_path: Path) -> None:
    db_path = tmp_path / "law.db"
    _seed(db_path)
    out = tmp_path / "md"
    count = export_markdown(db_path=db_path, out_dir=out)
    assert count == 1
    files = list(out.rglob("*.md"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "source_url:" in text  # frontmatter
    assert "제1조" in text


def test_export_ndjson_writes_one_record_per_article(tmp_path: Path) -> None:
    db_path = tmp_path / "law.db"
    _seed(db_path)
    out = tmp_path / "law.ndjson"
    count = export_ndjson(db_path=db_path, out_path=out)
    assert count == 1
    line = out.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["source_url"] == "https://www.law.go.kr/법령/법인세법#제1조"
    assert rec["source_kind"] == "law"
    assert rec["doc_title"] == "법인세법"
    assert rec["article_no"] == "제1조"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_law_export.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# crawler/fino_law/export_markdown.py
from __future__ import annotations

from pathlib import Path

from .db import connect_db


def _safe(name: str) -> str:
    return "".join(c for c in name if c.isalnum() or c in " _-()가-힣").strip().replace(" ", "_")


def export_markdown(*, db_path: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with connect_db(db_path) as conn:
        for doc in conn.execute("SELECT * FROM documents ORDER BY id").fetchall():
            arts = conn.execute(
                "SELECT * FROM articles WHERE document_id = ? ORDER BY seq", (doc["id"],)
            ).fetchall()
            lines = [
                "---",
                f"title: {doc['title']}",
                f"source_kind: {doc['source_kind']}",
                f"external_id: {doc['external_id']}",
                f"category: {doc['category']}",
                f"org: {doc['org']}",
                f"effective_at: {doc['effective_at']}",
                f"source_url: {doc['source_url']}",
                "---",
                "",
                f"# {doc['title']}",
                "",
            ]
            for a in arts:
                heading = f"## {a['article_no']}"
                if a["article_title"]:
                    heading += f"({a['article_title']})"
                lines.append(heading)
                lines.append("")
                lines.append(a["body_text"])
                lines.append("")
            sub = out_dir / doc["source_kind"]
            sub.mkdir(parents=True, exist_ok=True)
            (sub / f"{_safe(doc['title'])}.md").write_text("\n".join(lines), encoding="utf-8")
            count += 1
    return count
```

```python
# crawler/fino_law/export_ndjson.py
from __future__ import annotations

import json
from pathlib import Path

from .db import connect_db


def export_ndjson(*, db_path: Path, out_path: Path) -> int:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with connect_db(db_path) as conn, out_path.open("w", encoding="utf-8") as fh:
        rows = conn.execute(
            """
            SELECT d.source_kind, d.external_id, d.title AS doc_title, d.org,
                   d.effective_at, a.article_no, a.article_title, a.body_text, a.source_url
            FROM articles a JOIN documents d ON d.id = a.document_id
            ORDER BY d.id, a.seq
            """
        ).fetchall()
        for r in rows:
            fh.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
            count += 1
    return count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_law_export.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_law/export_markdown.py crawler/fino_law/export_ndjson.py tests/test_fino_law_export.py
git commit -m "feat(fino_law): MD + NDJSON export with per-article citation source_url"
```

---

### Task 8: Phase 1+2 전체 회귀 + 실수집 검증

- [ ] **Step 1: 전체 테스트**

Run: `.venv/bin/python -m pytest tests/test_fino_law_*.py -q`
Expected: 전부 PASS.

- [ ] **Step 2: 전체 법령 실수집 (네트워크, 시간 소요)**

Run: `.venv/bin/python -m crawler.fino_law.collect --law --delay-seconds 0.3`
Expected: 국세+지방세 각 법/령/규칙 수집 로그, 일부 "현행 미매칭" 스킵 허용.

- [ ] **Step 3: 수용 기준 검증**

Run:
```bash
.venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect("data/fino_law.db"); c.row_factory = sqlite3.Row
docs = c.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"]
arts = c.execute("SELECT COUNT(*) n FROM articles").fetchone()["n"]
no_url = c.execute("SELECT COUNT(*) n FROM articles WHERE source_url = ''").fetchone()["n"]
print(f"documents={docs} articles={arts} articles_without_url={no_url}")
assert docs > 0 and arts > 0 and no_url == 0
PY
```
Expected: `articles_without_url=0`, docs/arts 양수.

- [ ] **Step 4: export 실행 + 커밋(코드만)**

Run:
```bash
.venv/bin/python -m crawler.fino_law.export_markdown 2>/dev/null || true
.venv/bin/python -c "from pathlib import Path; from crawler.fino_law.export_markdown import export_markdown; from crawler.fino_law.export_ndjson import export_ndjson; print('md', export_markdown(db_path=Path('data/fino_law.db'), out_dir=Path('data/fino_law_md'))); print('ndjson', export_ndjson(db_path=Path('data/fino_law.db'), out_path=Path('data/fino_law.ndjson')))"
```
Expected: md/ndjson 건수 출력. (`data/`는 .gitignore이므로 산출물은 커밋 안 함.)

```bash
git commit --allow-empty -m "chore(fino_law): Phase 1+2 (법령 수집+export) 검증 완료"
```

---

## Phase 3 — C-2 조문형 세법집행기준 (spike-gated)

> **게이트:** Task 9 spike가 조문 본문 + deep-link 추출에 성공해야 Task 10~11을 진행한다. 실패 시(SPA 차단/CAPTCHA/구조 불명) 사용자에게 보고하고 집행기준은 보류, Phase 1+2(법령)만으로 마감한다.

### Task 9: 조문형 집행기준 deep-link 실증 (spike)

**Files:**
- Create: `tests/fixtures/exec_jiphaeng_법인.json` (성공 시)
- Create: `docs/superpowers/specs/_recon-exec-standard.md` (발견 기록)

- [ ] **Step 1: 세법집행기준 화면의 조문 목록/뷰어 호출 재현**

발견된 사실(메모리 `fino-law-corpus-collection-endpoints`):
- 화면 `/st/USESTE002M.do`, action `ASISTZ001MR01`, 뷰어 `/st/USESTA002P.do?ntstBscId=…&ntstBrkdId=…`
- 세목코드 `ntstSjtClCd`: 법인=05, 부가=07, 소득=09 (action `ASIELA001MR02`)

Run (탐색 — 실제 paramData를 찾는다):
```bash
# /st/USESTE002M.do 의 JS에서 ASISTZ001MR01 호출 paramData와 조문 진입(USESTA002P) 흐름을 추출
curl -skL --tls-max 1.3 "https://taxlaw.nts.go.kr/st/USESTE002M.do" \
  -H "User-Agent: Mozilla/5.0" -H "Referer: https://taxlaw.nts.go.kr/" -o /tmp/ste.html
.venv/bin/python - <<'PY'
import re
h = open('/tmp/ste.html', encoding='utf-8', errors='ignore').read()
for aid in ['ASISTZ001MR01', 'ASISTZ002MR01']:
    m = re.search(aid, h)
    if m:
        print(f"=== {aid} ===")
        print(re.sub(r'\s+', ' ', h[max(0, m.start()-400):m.end()+300])[:700])
PY
```
목표: `ASISTZ001MR01`/`ASISTZ002MR01` 의 paramData 키와, 조문 목록을 반환하는 action(목록→ntstBscId→조문 본문) 체인을 확정.

- [ ] **Step 2: 조문 1건 본문 + deep-link 추출 성공 확인**

조문 목록 action을 호출해 `ntstBscId`를 얻고, 뷰어/본문 action으로 조문 본문을 받아 fixture로 저장. (정확한 actionId/paramData는 Step 1 결과로 채운다.)

Run (구조 확정 후):
```bash
.venv/bin/python - <<'PY'
import subprocess, json
def action(aid, param):
    cmd = ["curl","-skL","--tls-max","1.3","--max-time","30","-X","POST",
           "-H","Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
           "-H","User-Agent: Mozilla/5.0",
           "-H","Origin: https://taxlaw.nts.go.kr",
           "-H","Referer: https://taxlaw.nts.go.kr/st/USESTE002M.do",
           "-H","X-Requested-With: XMLHttpRequest",
           "--data-urlencode", f"paramData={json.dumps(param, ensure_ascii=False)}",
           "-d", f"actionId={aid}", "https://taxlaw.nts.go.kr/action.do"]
    return json.loads(subprocess.run(cmd, capture_output=True, text=True, timeout=35).stdout)
# Step 1에서 확정한 목록 action/param으로 법인(05) 집행기준 목록 → 첫 ntstBscId → 본문
# (실제 키는 Step 1 결과로 대체)
print("TODO: Step 1 결과로 목록 action 호출 후 ntstBscId 확보 → 본문 action 호출")
PY
```
**성공 기준:** 조문 본문 텍스트(비어있지 않음) + `USESTA002P.do?ntstBscId=…` deep-link 1건 확보 → fixture 저장.

- [ ] **Step 3: 발견 기록 + 판정**

`docs/superpowers/specs/_recon-exec-standard.md`에 다음을 기록:
- 목록 action id + paramData(세목코드 사용법)
- 조문 본문 action id + paramData(ntstBscId/ntstBrkdId)
- 조문별 deep-link URL 패턴
- 페이지네이션 키
- **판정: 수집 가능 / 불가(사유)**

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/exec_jiphaeng_법인.json docs/superpowers/specs/_recon-exec-standard.md
git commit -m "test(fino_law): 조문형 집행기준 deep-link 실증(spike) + 발견 기록"
```

> **STOP & REVIEW:** Step 3 판정을 사용자에게 보고. "가능"이면 Task 10 진행, "불가"면 여기서 Phase 3 종료.

### Task 10: 집행기준 fetch + 파서 (`fetch_exec.py` / `parsers_exec.py`)

**Files:**
- Create: `crawler/fino_law/fetch_exec.py`
- Create: `crawler/fino_law/parsers_exec.py`
- Test: `tests/test_fino_law_exec_parsers.py`

- [ ] **Step 1: Write the failing test (fixture 기반)**

Task 9에서 저장한 `tests/fixtures/exec_jiphaeng_법인.json`을 입력으로, `parse_exec_articles(data, name, external_id)` 가 `DocumentRecord(source_kind="exec_standard", ...)`와 조문 리스트(각 `source_url`에 `USESTA002P.do?ntstBscId=…` 포함)를 반환하는지 검증. (정확한 assert 값은 fixture 실제 내용으로 작성.)

- [ ] **Step 2~5:** Task 4와 동일 TDD 사이클로 `parsers_exec.py`(fixture 구조 기반 파서) + `fetch_exec.py`(curl action.do, `nts_taxlaw._curl_post` 패턴 재사용) 구현 → 테스트 PASS → 커밋.

```bash
git commit -m "feat(fino_law): 조문형 집행기준 fetch + 파서 (taxlaw action.do)"
```

### Task 11: collect에 exec 배선 + 검증

**Files:**
- Modify: `crawler/fino_law/collect.py` (`collect_exec()` 추가, `main()`의 exec 분기 활성화)

- [ ] **Step 1:** `collect_exec(db_path, delay)` 추가 — 세목코드 순회 → 목록 → 조문 본문 → `upsert_document(source_kind="exec_standard")` + `upsert_article`. `--exec`/`--all`에서 호출.
- [ ] **Step 2:** 소규모 실수집 smoke (법인 세목 1개) → `documents`에 `source_kind='exec_standard'` row 생성, 조문별 source_url 존재 확인.
- [ ] **Step 3:** export(MD/NDJSON)가 exec_standard도 함께 출력하는지 확인(코드 재사용 — 별도 수정 불필요).
- [ ] **Step 4: Commit**

```bash
git commit -m "feat(fino_law): collect_exec 배선 + 집행기준 수집 검증"
```

---

## Phase 4 (후순위, 별도) — PDF 책자 별도 tier

> 스펙 §7: PDF 책자(`USEELA001M.do`, action `ASIELA001MR01`)는 **권위 인덱스와 분리된 별도 source tier**. 본 plan 범위 밖. 별도 spec/plan에서 다룬다(수집은 가능 확인됨: 법인 42건). 권위 인덱스 출시 후 검색 갭이 측정되면 착수.

---

## Open Questions (구현 중 확인)

- [ ] FINO BE `exec_standard_sources.py`가 받는 정확한 MD/NDJSON 필드 스키마 — export 포맷 최종 합의(현재 추정 기반).
- [ ] law.go.kr `소관부처` 필드가 service 응답에서 dict/str 어느 형태인지 — Task 3 Step 2에서 확정 후 `parse_law_service` 보정.
- [ ] 조문형 집행기준 deep-link가 조문 단위로 직접 열리는지 — Task 9 spike.
- [ ] taxlaw.nts.go.kr 대량 수집 robots/약관.
```
