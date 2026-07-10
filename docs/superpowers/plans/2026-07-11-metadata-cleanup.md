# 메타데이터 정제 + 가짜 PDF 정리 + 크롤러 보수 — 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** libertree.db 427,266건의 메타데이터 오염(HTML 엔티티·태그·CDATA·개행·비ISO 날짜·URL 이상)을 정제하고, 가짜 PDF ~3,458건을 리셋하며, 정보가 소실된 masaf(깨진 제목 154)·그리스 3사이트(날짜 0%, 4,581건)를 백필로 복구한다.

**Architecture:** ① 신규 `scripts/cleanup_metadata.py`가 순수함수 규칙(R1~R6)으로 DB를 정제(dry-run 기본, FTS는 `documents_au` 트리거가 자동 동기화). ② 기존 `scripts/scan_fake_pdfs.py`의 스캔 범위·판정 기준을 확장해 가짜 PDF를 리셋. ③ 신규 `scripts/backfill_site_fields.py`가 커스텀 크롤러를 스크래치 DB에 재실행한 뒤 dedup 키 `(site_id, post_number, meta_url)`로 본 DB의 지정 필드만 UPDATE (본 DB의 `insert_document`는 dedup 시 UPDATE하지 않으므로 재크롤만으로는 백필 불가 — 2026-07-11 확인).

**Tech Stack:** Python 3.12 (.venv), sqlite3, python-dateutil(신규 설치), unittest (repo 관행 — pytest 미설치)

**Spec:** `docs/superpowers/specs/2026-07-11-metadata-cleanup-design.md`

## Global Constraints

- DB는 운영 정책상 쓰기 금지 대상 — 이 계획의 스크립트들만 예외적으로 쓰기 (실행 전 `ps -ef | grep -E "promote_all|recover_pdfs|backfill|bulk_summarize" | grep -v grep`으로 idle 확인)
- `--apply` 전 반드시 DB 백업: `cp data/libertree.db "data/libertree.db.bak-$(date +%Y%m%d-%H%M)-precleanup"` (5.2GB, 최초 1회면 충분)
- 모든 정제 규칙은 멱등 (두 번째 실행 시 변경 0건)
- 테스트 실행: `.venv/bin/python -m unittest <module> -v` (pytest 아님)
- abstract==title 1,424건은 건드리지 않음 (사용자 결정: 유지)
- ftp:// pdf_url 76건은 유지 (리포트만)
- 파싱 불가 날짜는 원값 유지 + 리포트 (억지 변환 금지)
- 커밋 메시지 끝에 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`

---

### Task 1: 텍스트 정제 규칙 (R1 엔티티 / R2 태그·CDATA / R4 개행·공백)

**Files:**
- Create: `scripts/cleanup_metadata.py`
- Test: `tests/test_cleanup_metadata.py`

**Interfaces:**
- Produces: `clean_text(s: str|None) -> str|None` — CDATA 제거→태그 스트립→엔티티 디코드(반복)→공백 정리. title/abstract 공용.
- Produces: `clean_keywords(s: str|None) -> str|None` — clean_text 후 `,` 토큰 중복 제거(대소문자 무시).
- 두 함수 모두 변경 없으면 **원값 그대로 반환** (호출부가 `!=` 비교로 변경 감지).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_cleanup_metadata.py
# -*- coding: utf-8 -*-
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.cleanup_metadata import clean_text, clean_keywords


class TestCleanText(unittest.TestCase):
    def test_entity_decode(self):
        # esteri-it 실사례: &#8211; = en-dash, &#8217; = right quote
        self.assertEqual(
            clean_text("Nota di inquadramento &#8211; Conferenza"),
            "Nota di inquadramento – Conferenza",
        )
        self.assertEqual(clean_text("Blood Diseases &amp; Disorders"),
                         "Blood Diseases & Disorders")

    def test_double_encoded_entity(self):
        self.assertEqual(clean_text("A &amp;amp; B"), "A & B")

    def test_tag_strip(self):
        self.assertEqual(
            clean_text('intro <div class="x">body</div> <br/> end'),
            "intro body end",
        )

    def test_math_lt_preserved(self):
        # 태그가 아닌 부등호는 보존 (< 뒤가 영문자/슬래시일 때만 태그)
        self.assertEqual(clean_text("p<0.05, q>1"), "p<0.05, q>1")

    def test_newline_and_spaces(self):
        self.assertEqual(clean_text("Line one\n  Line two\t x"),
                         "Line one Line two x")

    def test_cdata_removed(self):
        self.assertEqual(clean_text("<![CDATA[Communiqué]]>"), "Communiqué")

    def test_none_and_empty_passthrough(self):
        self.assertIsNone(clean_text(None))
        self.assertEqual(clean_text(""), "")

    def test_clean_value_unchanged_identity(self):
        s = "Perfectly normal title 2024"
        self.assertEqual(clean_text(s), s)


class TestCleanKeywords(unittest.TestCase):
    def test_cdata_and_dupes(self):
        # presse-economie 실사례: CDATA 잔재 + 중복 토큰
        raw = ("Communiqué de presse, Bruno Le Maire, "
               "<![CDATA[Communiqué de presse]]>, <![CDATA[Bruno Le Maire]]>")
        self.assertEqual(clean_keywords(raw),
                         "Communiqué de presse, Bruno Le Maire")

    def test_sub_tag_stripped(self):
        self.assertEqual(clean_keywords("CO<sub>2</sub>, Carbon capture"),
                         "CO2, Carbon capture")

    def test_case_insensitive_dedupe_keeps_first(self):
        self.assertEqual(clean_keywords("Energy, energy, ENERGY, wind"),
                         "Energy, wind")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m unittest tests.test_cleanup_metadata -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.cleanup_metadata'` (또는 scripts에 `__init__.py` 없으면 import 오류 — 기존 `scripts/` 임포트 관행은 `sys.path` 삽입이므로 테스트처럼 경로 삽입 후 `from scripts.cleanup_metadata import` 가 되려면 `scripts/__init__.py` 존재 여부 확인. 없으면 `importlib` 대신 **테스트에서도 동일하게 `sys.path.insert` + `import cleanup_metadata`** 형태로 맞춘다. 기존 `tests/test_storage.py`의 임포트 스타일을 먼저 보고 따를 것.)

- [ ] **Step 3: 최소 구현**

```python
# scripts/cleanup_metadata.py
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


def clean_text(s):
    """R1+R2+R4: CDATA 제거 -> 태그 스트립 -> 엔티티 디코드 -> 공백 정리."""
    if not s:
        return s
    out = _CDATA_RE.sub(r"\1", s)
    out = _TAG_RE.sub(" ", out)
    out = _unescape_repeat(out)
    # 엔티티 디코드가 새 태그를 만들 수 있어 한 번 더
    out = _TAG_RE.sub(" ", out)
    return _WS_RE.sub(" ", out).strip()


def clean_keywords(s):
    """R3: clean_text 후 콤마 토큰 중복 제거(대소문자 무시, 첫 표기 유지)."""
    if not s:
        return s
    cleaned = clean_text(s)
    seen, toks = set(), []
    for tok in cleaned.split(","):
        t = tok.strip()
        key = t.lower()
        if t and key not in seen:
            seen.add(key)
            toks.append(t)
    return ", ".join(toks)
```

주의: `CO<sub>2</sub>` → 태그를 공백으로 치환하면 `CO 2` 가 됨. 테스트 기대값은 `CO2`. **키워드 경로에서는 태그를 빈 문자열로** 치환해야 한다. 구현 시 `clean_text(s, tag_repl=" ")` 파라미터를 두고 `clean_keywords`는 `tag_repl=""` 로 호출하도록 정리할 것 (테스트가 판정 기준).

- [ ] **Step 4: 통과 확인**

Run: `.venv/bin/python -m unittest tests.test_cleanup_metadata -v`
Expected: PASS (11 tests)

- [ ] **Step 5: 커밋**

```bash
git add scripts/cleanup_metadata.py tests/test_cleanup_metadata.py
git commit -m "feat(cleanup): text cleaning rules R1/R2/R3/R4 (entities, tags, CDATA, whitespace)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: 날짜 정규화 규칙 (R5)

**Files:**
- Modify: `scripts/cleanup_metadata.py` (함수 추가)
- Modify: `requirements.txt` (python-dateutil 추가)
- Test: `tests/test_cleanup_metadata.py` (클래스 추가)

**Interfaces:**
- Produces: `normalize_date(value: str|None, dayfirst: bool = False) -> str|None` — `YYYY-MM-DD` 반환, 파싱 불가/범위 밖(1800~2030)이면 **None** (호출부가 원값 유지 + 리포트).
- Produces: `DAYFIRST_SITES: set[str]` — 유럽식 DD/MM 사이트 화이트리스트.

- [ ] **Step 1: dateutil 설치**

```bash
.venv/bin/pip install python-dateutil
grep -q python-dateutil requirements.txt || echo "python-dateutil>=2.8" >> requirements.txt
```

- [ ] **Step 2: 실패하는 테스트 작성** (tests/test_cleanup_metadata.py에 추가)

```python
from scripts.cleanup_metadata import normalize_date


class TestNormalizeDate(unittest.TestCase):
    def test_already_iso_returns_none(self):
        # 호출부는 비ISO만 넘기지만 방어적으로: 동일값이면 변경 불필요 표시(None 아님)
        self.assertEqual(normalize_date("2025-08-01"), "2025-08-01")

    def test_french_month(self):
        self.assertEqual(normalize_date("01 août 2025"), "2025-08-01")
        self.assertEqual(normalize_date("01 avril 2021"), "2021-04-01")

    def test_dotted_korean_style(self):
        self.assertEqual(normalize_date("2014.10.24"), "2014-10-24")
        self.assertEqual(normalize_date("2025.09.15."), "2025-09-15")

    def test_rfc822(self):
        self.assertEqual(normalize_date("Fri, 01 Aug 2025 09:36:29 +0000"),
                         "2025-08-01")

    def test_short_year_english(self):
        self.assertEqual(normalize_date("1 Feb 24"), "2024-02-01")
        self.assertEqual(normalize_date("19 Nov 24"), "2024-11-19")

    def test_slash_us_default(self):
        self.assertEqual(normalize_date("09/30/2021"), "2021-09-30")

    def test_slash_dayfirst(self):
        self.assertEqual(normalize_date("06/02/2017", dayfirst=True), "2017-02-06")

    def test_slash_ymd(self):
        self.assertEqual(normalize_date("2025/11/24"), "2025-11-24")

    def test_long_english(self):
        self.assertEqual(normalize_date("March 17, 2026"), "2026-03-17")
        self.assertEqual(normalize_date("18 March 2026"), "2026-03-18")

    def test_unparseable_returns_none(self):
        self.assertIsNone(normalize_date("2026 - 12??"))
        self.assertIsNone(normalize_date(":"))
        self.assertIsNone(normalize_date(""))
        self.assertIsNone(normalize_date(None))

    def test_out_of_range_rejected(self):
        self.assertIsNone(normalize_date("0020-01-01"))
        self.assertIsNone(normalize_date("-001-11-30"))
```

- [ ] **Step 3: 실패 확인**

Run: `.venv/bin/python -m unittest tests.test_cleanup_metadata.TestNormalizeDate -v`
Expected: FAIL — `ImportError: cannot import name 'normalize_date'`

- [ ] **Step 4: 구현** (scripts/cleanup_metadata.py에 추가)

```python
from datetime import datetime

from dateutil import parser as _dateparser

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


def _fmt(y: int, m: int, d: int):
    try:
        dt = datetime(y, m, d)
    except ValueError:
        return None
    if not (1800 <= dt.year <= 2030):
        return None
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"


def normalize_date(value, dayfirst: bool = False):
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
```

- [ ] **Step 5: 통과 확인**

Run: `.venv/bin/python -m unittest tests.test_cleanup_metadata -v`
Expected: PASS (전체)

- [ ] **Step 6: 커밋**

```bash
git add scripts/cleanup_metadata.py tests/test_cleanup_metadata.py requirements.txt
git commit -m "feat(cleanup): date normalization rule R5 (locale-aware, dateutil fallback)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: URL 정리 규칙 (R6)

**Files:**
- Modify: `scripts/cleanup_metadata.py`
- Test: `tests/test_cleanup_metadata.py`

**Interfaces:**
- Produces: `fix_pdf_url(pdf_url: str|None, meta_url: str|None) -> tuple[str|None, str]` — `(새값, 판정)` 반환. 판정 ∈ `{"ok", "absolutized", "nulled", "kept_ftp"}`. `"ok"`면 변경 없음.
- Produces: `fix_meta_url(meta_url: str|None) -> tuple[str|None, str]` — `'ERROR'`→`(None,"nulled")`, 나머지 비http는 `(원값,"kept")` (리포트만).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
from scripts.cleanup_metadata import fix_pdf_url, fix_meta_url


class TestUrlRules(unittest.TestCase):
    def test_http_ok(self):
        self.assertEqual(fix_pdf_url("https://x.org/a.pdf", "https://x.org/p"),
                         ("https://x.org/a.pdf", "ok"))

    def test_relative_absolutized(self):
        self.assertEqual(
            fix_pdf_url("/globalassets/n.pdf", "https://www.sgu.se/en/page"),
            ("https://www.sgu.se/globalassets/n.pdf", "absolutized"),
        )

    def test_citation_nulled(self):
        self.assertEqual(fix_pdf_url("Brouwer2024", "https://nin.nl/p"),
                         (None, "nulled"))
        self.assertEqual(fix_pdf_url("DOI: 10.14207/ejsd.2019", "https://toi.no/p"),
                         (None, "nulled"))

    def test_ftp_kept(self):
        self.assertEqual(fix_pdf_url("ftp://ftp.asc-csa.gc.ca/a.pdf", "https://x/p"),
                         ("ftp://ftp.asc-csa.gc.ca/a.pdf", "kept_ftp"))

    def test_meta_error_nulled(self):
        self.assertEqual(fix_meta_url("ERROR"), (None, "nulled"))

    def test_meta_citation_kept(self):
        val = "Bączek-Kwinta, R. (2006). Reakcja..."
        self.assertEqual(fix_meta_url(val), (val, "kept"))
```

- [ ] **Step 2: 실패 확인**

Run: `.venv/bin/python -m unittest tests.test_cleanup_metadata.TestUrlRules -v`
Expected: FAIL — ImportError

- [ ] **Step 3: 구현**

```python
from urllib.parse import urljoin


def fix_pdf_url(pdf_url, meta_url):
    v = (pdf_url or "").strip()
    if not v:
        return (pdf_url, "ok")
    if v.startswith(("http://", "https://")):
        return (v if v == pdf_url else v, "ok")
    if v.startswith("ftp://"):
        return (v, "kept_ftp")
    if v.startswith("/") and (meta_url or "").startswith(("http://", "https://")):
        return (urljoin(meta_url, v), "absolutized")
    return (None, "nulled")


def fix_meta_url(meta_url):
    v = (meta_url or "").strip()
    if v == "ERROR":
        return (None, "nulled")
    return (meta_url, "kept" if not v.startswith(("http://", "https://")) else "ok")
```

- [ ] **Step 4: 통과 확인** — Run: `.venv/bin/python -m unittest tests.test_cleanup_metadata -v` / Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add scripts/cleanup_metadata.py tests/test_cleanup_metadata.py
git commit -m "feat(cleanup): URL repair rule R6 (absolutize, null citations, keep ftp)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: CLI 배선 (dry-run / --apply / 백업 / 리포트)

**Files:**
- Modify: `scripts/cleanup_metadata.py` (main + 규칙별 SELECT/UPDATE)
- Test: `tests/test_cleanup_metadata.py` (임시 DB 통합 테스트)

**Interfaces:**
- Consumes: Task 1~3의 `clean_text`, `clean_keywords`, `normalize_date`, `DAYFIRST_SITES`, `fix_pdf_url`, `fix_meta_url`
- Produces: `run_cleanup(conn, apply: bool) -> dict` — 규칙별 `{"candidates": int, "changed": int, "samples": list}` 집계 dict. CLI: `.venv/bin/python scripts/cleanup_metadata.py [--apply] [--db PATH]`

**규칙별 대상 SELECT (오탐 방지의 핵심 — WHERE로 대상을 좁힌 뒤 함수 적용):**

| 규칙 | SELECT WHERE | UPDATE 컬럼 |
|---|---|---|
| text(title) | `title LIKE '%&#%' OR title LIKE '%&amp;%' OR title LIKE '%&lt;%' OR title LIKE '%<a %' OR title LIKE '%<span%' OR title LIKE '%<br%' OR title LIKE '%'||CHAR(10)||'%'` | title |
| text(abstract) | `abstract LIKE '%<div%' OR abstract LIKE '%<span%' OR abstract LIKE '%<a href%' OR abstract LIKE '%<br%' OR abstract LIKE '%<![CDATA[%' OR abstract LIKE '%&amp;%' OR abstract LIKE '%&#%'` | abstract |
| keywords | `keywords LIKE '%<%' OR keywords LIKE '%&amp%'` | keywords |
| date(listed) | 비ISO GLOB 3종 부정 (진단 쿼리와 동일) | listed_date |
| date(published) | 동일 패턴 | published_date |
| url | `meta_url='ERROR' OR (pdf_url IS NOT NULL AND pdf_url!='' AND pdf_url NOT LIKE 'http%')` | meta_url, pdf_url |

- [ ] **Step 1: 통합 테스트 작성 (임시 DB)**

```python
import sqlite3
import tempfile

from scripts.cleanup_metadata import run_cleanup

MINI_SCHEMA = """
CREATE TABLE documents (
  seq_id INTEGER PRIMARY KEY, site_id TEXT, title TEXT, abstract TEXT,
  keywords TEXT, listed_date TEXT, published_date TEXT,
  meta_url TEXT, pdf_url TEXT
);
"""


class TestRunCleanup(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(MINI_SCHEMA)
        rows = [
            (1, "esteri-it-it", "A &#8211; B", "x <div>y</div>", None,
             None, "2024-01-01", "https://e.it/p1", None),
            (2, "presse-economie-gouv-fr", "OK", "ok",
             "K1, <![CDATA[K1]]>", "01 août 2025", "2024-01-01",
             "https://p.fr/p2", None),
            (3, "sgu-se-en", "OK", "ok", None, None, "2024-01-01",
             "https://sgu.se/p3", "/globalassets/n.pdf"),
            (4, "directives-doe-gov-guidance", "OK", "ok", None, None,
             "2024-01-01", "ERROR", None),
            (5, "search-nal-usda-gov-discovery", "OK", "ok", None,
             "2026 - 12??", "2024-01-01", "https://u.gov/p5", None),
        ]
        self.conn.executemany("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?)", rows)

    def test_dry_run_changes_nothing(self):
        report = run_cleanup(self.conn, apply=False)
        self.assertEqual(
            self.conn.execute("SELECT title FROM documents WHERE seq_id=1").fetchone()[0],
            "A &#8211; B")
        self.assertGreaterEqual(report["title"]["changed"], 1)

    def test_apply_fixes_and_is_idempotent(self):
        run_cleanup(self.conn, apply=True)
        got = {r[0]: r for r in self.conn.execute(
            "SELECT seq_id, title, keywords, listed_date, meta_url, pdf_url"
            " FROM documents").fetchall()}
        self.assertEqual(got[1][1], "A – B")
        self.assertEqual(got[2][2], "K1")
        self.assertEqual(got[2][3], "2025-08-01")
        self.assertEqual(got[3][5], "https://www.sgu.se/globalassets/n.pdf")
        self.assertIsNone(got[4][4])
        self.assertEqual(got[5][3], "2026 - 12??")  # 파싱불가 → 원값 유지
        report2 = run_cleanup(self.conn, apply=True)  # 멱등
        self.assertTrue(all(v["changed"] == 0 for v in report2.values()))
```

- [ ] **Step 2: 실패 확인** — Run: `.venv/bin/python -m unittest tests.test_cleanup_metadata.TestRunCleanup -v` / Expected: FAIL (run_cleanup 없음)

- [ ] **Step 3: run_cleanup + main 구현**

```python
import argparse
import json
import shutil
import sqlite3
from pathlib import Path

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
        changed = (meta_verdict == "nulled") or pdf_verdict in ("absolutized", "nulled")
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
```

- [ ] **Step 4: 통과 확인** — Run: `.venv/bin/python -m unittest tests.test_cleanup_metadata -v` / Expected: 전체 PASS

- [ ] **Step 5: 커밋**

```bash
git add scripts/cleanup_metadata.py tests/test_cleanup_metadata.py
git commit -m "feat(cleanup): wire rules into dry-run/apply CLI with backup and JSON report

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 실 DB 정제 실행 (dry-run 대조 → apply → 사후 검증)

**Files:** 실행만 (코드 변경 없음). 산출물: `data/audit/cleanup_metadata_*_dryrun.json`, `*_apply.json`

- [ ] **Step 1: idle 확인 + dry-run**

```bash
ps -ef | grep -E "promote_all|recover_pdfs|backfill|bulk_summarize" | grep -v grep  # 출력 없어야 함
.venv/bin/python scripts/cleanup_metadata.py --db data/libertree.db
```

Expected (2026-07-10 진단 수치와 대조 — 크게 어긋나면 중단하고 원인 파악):
- title candidates ≈ 894+515 (중복 제외 ~1,300±)
- keywords candidates ≈ 1,274
- listed_date candidates = 19,751 / published_date = 948
- url candidates ≈ 115 (ERROR 2 + 비http 113)

- [ ] **Step 2: dry-run 리포트의 unparsed 표본 검토**

`data/audit/cleanup_metadata_*_dryrun.json`의 `listed_date.samples` / `published_date.samples`를 열어 "파싱 불가로 원값 유지"가 정당한지(usda `2026 - 12??` 류) 확인. 파싱 가능해야 할 포맷이 unparsed에 있으면 normalize_date에 규칙 추가 후 Task 2 테스트부터 재실행.

- [ ] **Step 3: apply**

```bash
.venv/bin/python scripts/cleanup_metadata.py --db data/libertree.db --apply
```

Expected: 백업 파일 생성 로그 + changed 수치가 dry-run과 동일

- [ ] **Step 4: 사후 검증 쿼리**

```bash
.venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect('data/libertree.db')
q1 = lambda s: c.execute(s).fetchone()[0]
assert q1("SELECT COUNT(*) FROM documents WHERE title LIKE '%&#%' OR title LIKE '%&amp;%'") == 0
assert q1("SELECT COUNT(*) FROM documents WHERE keywords LIKE '%<![CDATA[%'") == 0
assert q1("SELECT COUNT(*) FROM documents WHERE meta_url='ERROR'") == 0
print("잔여 비ISO listed_date:", q1("""SELECT COUNT(*) FROM documents WHERE listed_date IS NOT NULL AND listed_date != ''
 AND listed_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'
 AND listed_date NOT GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]' AND listed_date NOT GLOB '[0-9][0-9][0-9][0-9]'"""))
# FTS 동기화 표본: 정제된 문서가 새 제목으로 검색되는지
row = c.execute("SELECT seq_id, title FROM documents WHERE site_id='esteri-it-it' LIMIT 1").fetchone()
print("FTS hit:", c.execute("SELECT COUNT(*) FROM documents_fts WHERE documents_fts MATCH ?", (f'"{row[1][:20]}"',)).fetchone()[0] >= 1)
print("OK")
EOF
```

Expected: assert 통과, 잔여 비ISO는 unparsed 건수와 일치, FTS hit True

- [ ] **Step 5: 멱등 확인 + 리포트 커밋 없음(데이터 산출물은 audit에만)**

```bash
.venv/bin/python scripts/cleanup_metadata.py --db data/libertree.db  # 재-dry-run: changed 전부 0이어야 함
```

---

### Task 6: scan_fake_pdfs.py 확장 + 가짜 PDF 리셋 실행

**Files:**
- Modify: `scripts/scan_fake_pdfs.py`
- Test: `tests/test_scan_fake_pdfs.py` (신규 — classify/reset 단위)

**Interfaces:**
- Consumes: 기존 `classify_blob`, `Scanner`, `reset_pdf_row`
- Produces: 변경된 판정 — fake = `fake_html` 또는 (`unknown` AND size<10KB). 스캔 범위 = `pdf_downloaded=1` 전체 (기존은 `AND text_extracted=0`이라 텍스트까지 추출된 가짜 871건을 놓쳤음). 리셋 시 `text_extracted=0` 포함 + `.txt` blob도 삭제.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_scan_fake_pdfs.py
# -*- coding: utf-8 -*-
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.scan_fake_pdfs import classify_blob, is_fake, reset_pdf_row


class TestIsFake(unittest.TestCase):
    def _blob(self, content: bytes) -> Path:
        f = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
        f.write(content); f.close()
        return Path(f.name)

    def test_html_is_fake(self):
        p = self._blob(b"<html><body>login required</body></html>")
        self.assertTrue(is_fake(classify_blob(p), p.stat().st_size))

    def test_small_unknown_is_fake(self):
        p = self._blob(b"\r\n\r\n\r\nsome error text")  # kedi 실사례 패턴
        self.assertTrue(is_fake(classify_blob(p), p.stat().st_size))

    def test_https_text_is_fake(self):
        p = self._blob(b"https://redirected.example.org/real.pdf")
        self.assertTrue(is_fake(classify_blob(p), p.stat().st_size))

    def test_real_pdf_not_fake(self):
        p = self._blob(b"%PDF-1.7 rest-of-file")
        self.assertFalse(is_fake(classify_blob(p), p.stat().st_size))

    def test_hwp_ole_not_fake(self):
        p = self._blob(b"\xd0\xcf\x11\xe0" + b"\x00" * 100)  # OLE(HWP) 정상 바이너리
        self.assertFalse(is_fake(classify_blob(p), p.stat().st_size))

    def test_large_unknown_not_fake(self):
        p = self._blob(b"\x00" * 20480)  # 20KB unknown — 보수적으로 유지
        self.assertFalse(is_fake(classify_blob(p), p.stat().st_size))


class TestResetIncludesText(unittest.TestCase):
    def test_reset_clears_text_extracted(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE documents (seq_id INTEGER PRIMARY KEY,"
                     " pdf_downloaded INT, pdf_size_bytes INT, pdf_sha256 TEXT,"
                     " text_extracted INT)")
        conn.execute("INSERT INTO documents VALUES (1, 1, 999, 'abc', 1)")
        reset_pdf_row(conn, 1)
        row = conn.execute("SELECT pdf_downloaded, pdf_size_bytes, pdf_sha256,"
                           " text_extracted FROM documents WHERE seq_id=1").fetchone()
        self.assertEqual(row, (0, 0, "", 0))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인** — Run: `.venv/bin/python -m unittest tests.test_scan_fake_pdfs -v` / Expected: FAIL (`is_fake` 없음, reset은 text_extracted 미포함)

- [ ] **Step 3: 구현 수정**

scripts/scan_fake_pdfs.py 변경 4곳:

```python
FAKE_SMALL_BYTES = 10240  # 10KB 미만 unknown은 가짜로 판정


def is_fake(classification: str, size_bytes: int) -> bool:
    """fake 판정: HTML이거나, 10KB 미만의 unknown(에러 텍스트/리다이렉트 등)."""
    if classification == "fake_html":
        return True
    if classification == "unknown" and size_bytes < FAKE_SMALL_BYTES:
        return True
    return False
```

`reset_pdf_row`의 UPDATE에 `text_extracted = 0` 추가:

```python
        UPDATE documents
           SET pdf_downloaded = 0,
               pdf_size_bytes = 0,
               pdf_sha256 = '',
               text_extracted = 0
         WHERE seq_id = ?
```

`Scanner.fetch_rows`의 WHERE를 `pdf_downloaded = 1` 로 변경 (text_extracted=0 조건 삭제).

`Scanner.process_row`: classify 후 사이즈를 읽어 `is_fake(classification, size)`로 판정하고, fake이면 (reset 시) `.txt` sibling도 삭제:

```python
        blob_path = blob_path_for(seq_id)
        classification = classify_blob(blob_path)
        size = blob_path.stat().st_size if blob_path.exists() else 0
        fake = is_fake(classification, size) or classification == "missing"
        ...
        if fake:
            if self.reset:
                with self.db_lock:
                    reset_pdf_row(self.conn, seq_id)
            if self.delete_blob:
                delete_blob_file(blob_path)
                delete_blob_file(blob_path.with_suffix(".txt"))
```

(stats 집계의 `fake_html` 하드코딩도 `fake` 불리언 기준으로 갱신)

- [ ] **Step 4: 통과 확인** — Run: `.venv/bin/python -m unittest tests.test_scan_fake_pdfs -v` / Expected: PASS. 회귀: `.venv/bin/python -m unittest tests.test_storage -v` PASS 유지.

- [ ] **Step 5: 실 DB dry-run (270K blob 스캔, 수 분 소요)**

```bash
.venv/bin/python scripts/scan_fake_pdfs.py --workers 16
```

Expected: `count_fake_html + count_unknown(소형)` 합계가 ~3,400±500 범위. 사이트 상위에 jeonnam/krivet/kedi/environment-govt-nz 등장 (2026-07-10 진단과 일치). 크게 다르면 중단.

- [ ] **Step 6: apply**

```bash
.venv/bin/python scripts/scan_fake_pdfs.py --workers 16 --reset --delete-blob
```

- [ ] **Step 7: 사후 검증**

```bash
.venv/bin/python - <<'EOF'
import sqlite3
c = sqlite3.connect('data/libertree.db')
q1 = lambda s: c.execute(s).fetchone()[0]
print("pdf_downloaded:", q1("SELECT COUNT(*) FROM documents WHERE pdf_downloaded=1"))
print("text_extracted:", q1("SELECT COUNT(*) FROM documents WHERE text_extracted=1"))
# 대표 오염원 잔존 확인 (krivet 500건 그룹 등 소멸했는지)
print("krivet fake sha 잔존:", q1("SELECT COUNT(*) FROM documents WHERE pdf_sha256 LIKE 'e343cd674626ff95%'"))
EOF
```

Expected: pdf_downloaded ≈ 267,000±, krivet 잔존 0

- [ ] **Step 8: 커밋**

```bash
git add scripts/scan_fake_pdfs.py tests/test_scan_fake_pdfs.py
git commit -m "feat(fake-pdf): widen scan to all pdf_downloaded rows, small-unknown heuristic, reset text_extracted and delete txt blobs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: 오염원 크롤러 3곳 코드 수정 (재수집 없음)

**Files:**
- Modify: `crawler/sites/custom/esteri-it-it.py` (`_strip_html` — 태그만 제거하고 엔티티 미해제가 원인)
- Modify: `crawler/sites/custom/presse-economie-gouv-fr.py` (category 추출부 — line 133~ 부근에서 CDATA-매치와 plain-매치를 **둘 다** 수집해 중복 유입)
- Modify: `crawler/sites/custom/doras-dcu-ie-view.py` (title strip — line 153 `.strip()`만으로는 내부 개행 미처리)

**Interfaces:**
- Consumes: 각 크롤러의 기존 헬퍼 (`_strip_html`, `_extract_tag` 류)
- Produces: 파싱 결과에 오염이 없는 크롤러 (향후 재크롤 시 청정 데이터). DB의 기존 오염은 Task 5가 이미 정제.

- [ ] **Step 1: esteri — `_strip_html`에 엔티티 디코드 추가**

파일에서 `_strip_html` 정의를 찾아 (grep `-n "def _strip_html" crawler/sites/custom/esteri-it-it.py`) 반환 직전에 적용:

```python
from html import unescape as _html_unescape

def _strip_html(value):
    ...  # 기존 태그 제거 로직 유지
    text = _html_unescape(_html_unescape(text))  # &amp;#8211; 이중 인코딩 대응
    return " ".join(text.split())
```

검증 (인라인):
```bash
.venv/bin/python -c "
import importlib.util
spec = importlib.util.spec_from_file_location('m', 'crawler/sites/custom/esteri-it-it.py')
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
assert m._strip_html('A &#8211; B') == 'A – B', m._strip_html('A &#8211; B')
print('esteri OK')"
```

- [ ] **Step 2: presse-economie — category 중복 유입 제거**

line 130~140 부근의 두 findall 결과를 합치는 로직을 "CDATA 우선, plain은 CDATA 미존재 시만"으로 수정하고 최종적으로 dict.fromkeys 로 순서 보존 중복 제거:

```python
            cats = re.findall(
                r"<category><!\[CDATA\[(.*?)\]\]></category>", raw, re.DOTALL
            )
            if not cats:
                cats = re.findall(r"<category>(.*?)</category>", raw, re.DOTALL)
            cats = [" ".join(c.split()) for c in cats]
            cats = list(dict.fromkeys(c for c in cats if c and "CDATA" not in c))
```

검증: 위와 같은 동적 import로 category 파싱 함수에 CDATA 포함 표본 RSS 문자열을 넣어 `<![CDATA[` 부재 + 중복 부재 assert.

- [ ] **Step 3: doras — title 개행 정규화**

line 153: `title = (rec.get("title") or "").strip()` →

```python
        title = " ".join((rec.get("title") or "").split())
```

- [ ] **Step 4: 회귀 확인 + 커밋**

```bash
.venv/bin/python -m py_compile crawler/sites/custom/esteri-it-it.py crawler/sites/custom/presse-economie-gouv-fr.py crawler/sites/custom/doras-dcu-ie-view.py
git add crawler/sites/custom/esteri-it-it.py crawler/sites/custom/presse-economie-gouv-fr.py crawler/sites/custom/doras-dcu-ie-view.py
git commit -m "fix(crawlers): entity decode in esteri, CDATA-dedup categories in presse-economie, newline-safe titles in doras

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: backfill_site_fields.py (크롤러 재실행 → 지정 필드만 UPDATE)

**Files:**
- Create: `scripts/backfill_site_fields.py`
- Test: `tests/test_backfill_site_fields.py`

**Interfaces:**
- Consumes: `crawler.db_libertree.open_db/init_db/find_by_dedup_key`, 커스텀 크롤러 규약(클래스가 `BaseCrawler` 상속, `crawl(limit=)`, 생성자 `(db_conn, delay=)` — promote_all.py:203의 로더와 동일 방식의 동적 import)
- Produces: CLI `.venv/bin/python scripts/backfill_site_fields.py --site <site_id> --fields published_date[,title] [--apply] [--limit N]`
- Produces: `backfill(main_conn, scratch_conn, site_id, fields, apply) -> dict` — `{"scratch_docs": n, "matched": n, "updated": {field: n}, "unmatched": n}`
- 갱신 조건: dedup 키 `(site_id, post_number, meta_url)` 매치 AND 새값 non-empty AND 기존값과 다름 AND (title 백필의 경우) **기존값에 '�' 포함 시에만** (멀쩡한 제목 덮어쓰기 방지)

- [ ] **Step 1: 실패하는 테스트 작성**

```python
# tests/test_backfill_site_fields.py
# -*- coding: utf-8 -*-
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler.db_libertree import init_db
from scripts.backfill_site_fields import backfill


def _mkdb():
    conn = sqlite3.connect(":memory:")
    init_db(conn)
    return conn


def _ins(conn, site_id, post, url, **kw):
    cols = {"site_id": site_id, "post_number": post, "meta_url": url,
            "title": kw.get("title", "t"),
            "published_date": kw.get("published_date")}
    conn.execute(
        "INSERT INTO documents (site_id, post_number, meta_url, title, published_date)"
        " VALUES (:site_id, :post_number, :meta_url, :title, :published_date)", cols)
    conn.commit()


class TestBackfill(unittest.TestCase):
    def test_date_backfilled_only_when_empty_target_has_value(self):
        main, scratch = _mkdb(), _mkdb()
        _ins(main, "s", "1", "https://x/1", published_date="")
        _ins(scratch, "s", "1", "https://x/1", published_date="2025-03-01")
        rep = backfill(main, scratch, "s", ["published_date"], apply=True)
        self.assertEqual(rep["updated"]["published_date"], 1)
        got = main.execute("SELECT published_date FROM documents").fetchone()[0]
        self.assertEqual(got, "2025-03-01")

    def test_title_only_replaced_when_mojibake(self):
        main, scratch = _mkdb(), _mkdb()
        _ins(main, "s", "1", "https://x/1", title="dann�ggiat�")
        _ins(main, "s", "2", "https://x/2", title="fine title")
        _ins(scratch, "s", "1", "https://x/1", title="danneggiatà")
        _ins(scratch, "s", "2", "https://x/2", title="DIFFERENT")
        rep = backfill(main, scratch, "s", ["title"], apply=True)
        self.assertEqual(rep["updated"]["title"], 1)
        titles = [r[0] for r in main.execute(
            "SELECT title FROM documents ORDER BY seq_id").fetchall()]
        self.assertEqual(titles, ["danneggiatà", "fine title"])

    def test_dry_run_no_write(self):
        main, scratch = _mkdb(), _mkdb()
        _ins(main, "s", "1", "https://x/1", published_date="")
        _ins(scratch, "s", "1", "https://x/1", published_date="2025-03-01")
        backfill(main, scratch, "s", ["published_date"], apply=False)
        self.assertEqual(
            main.execute("SELECT published_date FROM documents").fetchone()[0], "")

    def test_unmatched_counted(self):
        main, scratch = _mkdb(), _mkdb()
        _ins(scratch, "s", "9", "https://x/9", published_date="2025-01-01")
        rep = backfill(main, scratch, "s", ["published_date"], apply=True)
        self.assertEqual(rep["unmatched"], 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 실패 확인** — Run: `.venv/bin/python -m unittest tests.test_backfill_site_fields -v` / Expected: FAIL (모듈 없음)

- [ ] **Step 3: 구현**

```python
# scripts/backfill_site_fields.py
#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""커스텀 크롤러를 스크래치 DB에 재실행한 뒤, dedup 키로 본 DB의 지정
필드만 UPDATE 하는 백필 도구.

insert_document 는 dedup 히트 시 UPDATE 하지 않으므로(2026-07-11 확인)
단순 재크롤로는 기존 행이 갱신되지 않는다 — 이 도구가 그 간극을 메운다.

사용:
  .venv/bin/python scripts/backfill_site_fields.py \
      --site mindev-gov-gr-category --fields published_date --apply
"""
import argparse
import importlib.util
import json
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PROJECT_ROOT))

from crawler.db_libertree import find_by_dedup_key, init_db, open_db  # noqa: E402

ALLOWED_FIELDS = {"published_date", "title", "listed_date", "authors", "keywords"}


def load_crawler_class(site_id: str):
    py_path = _PROJECT_ROOT / "crawler" / "sites" / "custom" / f"{site_id}.py"
    if not py_path.exists():
        raise FileNotFoundError(py_path)
    spec = importlib.util.spec_from_file_location(f"backfill_{site_id}", py_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    from crawler.base_crawler import BaseCrawler
    for obj in vars(mod).values():
        if (isinstance(obj, type) and issubclass(obj, BaseCrawler)
                and obj is not BaseCrawler):
            return obj
    raise RuntimeError(f"no BaseCrawler subclass in {py_path}")


def _should_update(field: str, old, new) -> bool:
    if not new or str(new).strip() == "":
        return False
    if str(new) == str(old or ""):
        return False
    if field == "title":
        return "�" in (old or "")  # 깨진 제목만 교체
    return not (old or "").strip()      # 그 외: 비어있는 것만 채움


def backfill(main_conn, scratch_conn, site_id, fields, apply: bool) -> dict:
    rep = {"scratch_docs": 0, "matched": 0, "unmatched": 0,
           "updated": {f: 0 for f in fields}}
    rows = scratch_conn.execute(
        f"SELECT post_number, meta_url, {', '.join(fields)} FROM documents"
        " WHERE site_id = ?", (site_id,)).fetchall()
    for row in rows:
        rep["scratch_docs"] += 1
        post_number, meta_url = row[0], row[1]
        seq_id = find_by_dedup_key(main_conn, site_id, post_number, meta_url)
        if seq_id is None:
            rep["unmatched"] += 1
            continue
        rep["matched"] += 1
        current = main_conn.execute(
            f"SELECT {', '.join(fields)} FROM documents WHERE seq_id=?",
            (seq_id,)).fetchone()
        for i, field in enumerate(fields):
            new = row[2 + i]
            if _should_update(field, current[i], new):
                rep["updated"][field] += 1
                if apply:
                    main_conn.execute(
                        f"UPDATE documents SET {field}=? WHERE seq_id=?",
                        (new, seq_id))
    if apply:
        main_conn.commit()
    return rep


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--site", required=True)
    ap.add_argument("--fields", required=True,
                    help="콤마 구분 (예: published_date,title)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--db", default="data/libertree.db")
    args = ap.parse_args()

    fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    bad = set(fields) - ALLOWED_FIELDS
    if bad:
        raise SystemExit(f"not allowed fields: {bad}")

    scratch_path = Path(tempfile.mkstemp(prefix=f"backfill_{args.site}_",
                                         suffix=".db")[1])
    scratch = open_db(scratch_path)
    init_db(scratch)
    cls = load_crawler_class(args.site)
    crawler = cls(db_conn=scratch, delay=1.0)
    print(f"[backfill] crawling {args.site} into scratch {scratch_path} ...")
    crawler.crawl(limit=args.limit)

    main_conn = open_db(Path(args.db))
    rep = backfill(main_conn, scratch, args.site, fields, apply=args.apply)
    out = Path(f"data/audit/backfill_{args.site}_{datetime.now():%Y%m%d_%H%M%S}"
               f"{'_apply' if args.apply else '_dryrun'}.json")
    out.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    print(f"report -> {out}")


if __name__ == "__main__":
    main()
```

주의: `find_by_dedup_key`의 실제 시그니처/반환(seq_id int인지 row인지)을 구현 전에 `grep -n "def find_by_dedup_key" -A 15 crawler/db_libertree.py`로 확인하고 테스트·코드를 맞출 것.

- [ ] **Step 4: 통과 확인** — Run: `.venv/bin/python -m unittest tests.test_backfill_site_fields -v` / Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add scripts/backfill_site_fields.py tests/test_backfill_site_fields.py
git commit -m "feat(backfill): re-run custom crawler into scratch DB and update selected fields by dedup key

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: masaf 인코딩 수정 + title 백필 (154건)

**Files:**
- Modify: `crawler/sites/custom/masaf-gov-it-flex.py`
- 실행: `scripts/backfill_site_fields.py --site masaf-gov-it-flex --fields title`

- [ ] **Step 1: 인코딩 원인 진단**

masaf 크롤러의 fetch 경로 확인 (requests 미사용 — playwright_fetcher 또는 base 헬퍼 사용 가능성):

```bash
grep -n "fetch\|get_html\|curl\|playwright\|decode\|\.content\|\.text" crawler/sites/custom/masaf-gov-it-flex.py | head -20
curl -s "https://www.masaf.gov.it/flex/cm/pages/ServeBLOB.php/L/IT/IDPagina/1" -o /tmp/masaf_probe.html 2>/dev/null; file /tmp/masaf_probe.html; grep -io "charset=[a-z0-9-]*" /tmp/masaf_probe.html | head -2
```

판정: 응답이 `charset=iso-8859-1`(또는 windows-1252)인데 UTF-8로 디코드했다면 → fetch 후 디코드 지점에 명시적 인코딩 지정. 반대로 UTF-8인데 latin-1 디코드였다면 그에 맞춤. (진단 결과에 따라 아래 Step 2 코드의 encoding 값을 결정)

- [ ] **Step 2: 수정 적용**

디코드 지점(예: `resp.text` 사용부)을 다음 패턴으로 교체:

```python
        raw = resp.content  # bytes
        # masaf는 header/meta의 charset 선언과 실제 인코딩이 어긋남 (2026-07 확인)
        for enc in ("utf-8", "windows-1252", "iso-8859-1"):
            try:
                html_text = raw.decode(enc)
                if "�" not in html_text:
                    break
            except UnicodeDecodeError:
                continue
        else:
            html_text = raw.decode("utf-8", errors="replace")
```

검증: 크롤러를 limit=5로 스크래치 실행해 title에 `�` 없는지 확인:

```bash
.venv/bin/python scripts/backfill_site_fields.py --site masaf-gov-it-flex --fields title --limit 5
```

Expected: 리포트 `scratch_docs=5`, 표본 title 정상 (dry-run이므로 본 DB 무변경)

- [ ] **Step 3: 전체 백필**

```bash
.venv/bin/python scripts/backfill_site_fields.py --site masaf-gov-it-flex --fields title          # dry-run
.venv/bin/python scripts/backfill_site_fields.py --site masaf-gov-it-flex --fields title --apply
```

Expected: `updated.title` ≈ 154 (깨진 제목만 교체 — `_should_update`가 보장)

- [ ] **Step 4: 사후 검증 + 커밋**

```bash
.venv/bin/python -c "
import sqlite3; c = sqlite3.connect('data/libertree.db')
print('masaf 깨진 title 잔여:', c.execute(\"SELECT COUNT(*) FROM documents WHERE site_id='masaf-gov-it-flex' AND title LIKE '%�%'\").fetchone()[0])"
git add crawler/sites/custom/masaf-gov-it-flex.py
git commit -m "fix(masaf): encoding-sniffing decode to repair mojibake titles

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

Expected: 잔여 0 (일부 남으면 사이트에서 사라진 글 — 리포트의 unmatched로 설명 가능해야 함)

---

### Task 10: 그리스 3사이트 published_date 백필 (4,581건)

**Files:**
- 실행: `scripts/backfill_site_fields.py` × 3
- Modify(조건부): `crawler/sites/custom/mindigital-gr-archives.py`, `crawler/sites/custom/ypergasias-gov-gr-category.py` — dry-run에서 날짜 추출이 안 되는 경우만

**참고:** `mindev-gov-gr-category.py`는 현재 코드가 이미 WP REST API의 `date` 필드에서 `published_date`를 추출한다 (line 315). DB에 0%인 것은 수집 당시 구버전/폴백 경로였기 때문일 가능성 — 즉 **크롤러 수정 없이 백필만으로 해결될 수 있다**. dry-run으로 먼저 판정한다.

- [ ] **Step 1: mindev dry-run 판정**

```bash
.venv/bin/python scripts/backfill_site_fields.py --site mindev-gov-gr-category --fields published_date --limit 50
```

Expected: `updated.published_date` > 0 이면 크롤러 수정 불필요 → Step 2로. 0이면 크롤러의 `_parse_date` 반환을 표본 디버깅 (스크래치 DB의 published_date 직접 조회) 후 수정.

- [ ] **Step 2: mindev 전체 백필**

```bash
.venv/bin/python scripts/backfill_site_fields.py --site mindev-gov-gr-category --fields published_date          # dry-run 전체
.venv/bin/python scripts/backfill_site_fields.py --site mindev-gov-gr-category --fields published_date --apply
```

Expected: updated ≈ 3,000+ (WP API가 옛 글을 전부 노출하는 한 3,433에 근접; unmatched/미노출分은 리포트로 설명)

- [ ] **Step 3: mindigital, ypergasias 동일 절차**

```bash
for s in mindigital-gr-archives ypergasias-gov-gr-category; do
  .venv/bin/python scripts/backfill_site_fields.py --site "$s" --fields published_date --limit 50
done
```

각각 dry-run 판정 → (추출 0이면) 해당 크롤러의 날짜 추출부에 mindev와 같은 패턴(WP REST `date` 필드 또는 `article:published_time` meta) 추가 → 전체 dry-run → apply. 수정이 생기면:

```bash
git add crawler/sites/custom/mindigital-gr-archives.py crawler/sites/custom/ypergasias-gov-gr-category.py
git commit -m "fix(gr-crawlers): extract published_date (WP REST date / article:published_time)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

- [ ] **Step 4: 사후 검증**

```bash
.venv/bin/python -c "
import sqlite3; c = sqlite3.connect('data/libertree.db')
for s in ['mindev-gov-gr-category','mindigital-gr-archives','ypergasias-gov-gr-category']:
    tot, filled = c.execute(\"SELECT COUNT(*), SUM(CASE WHEN published_date IS NOT NULL AND published_date!='' THEN 1 ELSE 0 END) FROM documents WHERE site_id=?\", (s,)).fetchone()
    print(f'{s:35s} {filled}/{tot}')"
```

Expected: 각 사이트 채움률이 0% → 대부분(사이트가 노출하는 범위)으로 상승. 남는 미채움은 백필 리포트의 unmatched 수와 합치.

---

### Task 11: 최종 검증 배터리 + 보고

**Files:**
- Create: `data/audit/metadata_cleanup_final_report.md` (결과 요약)
- Modify: `docs/CURRENT_STATE.md` (수치 갱신)

- [ ] **Step 1: 점검 배터리 재실행**

2026-07-10 진단과 동일한 쿼리 세트 실행 (title HTML/개행/깨짐, keywords CDATA, 비ISO 날짜, URL 이상, 가짜 PDF sha 그룹, pdf/text 카운트). 각 항목 기대값:

| 항목 | 기대 |
|---|---|
| title 엔티티/태그/개행 | 0 |
| keywords CDATA | 0 |
| 깨진 title(�) | ~0 (masaf 외 nistdigitalarchives 4건은 잔존 허용 — 리포트 명시) |
| 비ISO listed_date | unparsed 리포트 수치와 일치 (수백 건, 원값 유지 정책) |
| meta_url='ERROR' | 0 |
| 가짜 PDF (krivet/kedi/jeonnam sha 그룹) | 0 |
| pdf_downloaded | ~267K (확정치 기록) |
| 그리스 3곳 published_date | 백필 리포트와 합치 |
| unittest 전체 | `tests.test_storage`, `tests.test_cleanup_metadata`, `tests.test_scan_fake_pdfs`, `tests.test_backfill_site_fields` 전부 PASS |

- [ ] **Step 2: 최종 보고서 작성**

`data/audit/metadata_cleanup_final_report.md`에: 규칙별 변경 건수(각 apply JSON 집계), 가짜 PDF 리셋 건수와 새 유효 PDF 확정치, 백필 결과(masaf/그리스), 의도적으로 남긴 것(ftp 76, unparsed 날짜, abstract==title 1,424, nistdigitalarchives 4)과 사유.

- [ ] **Step 3: 문서 수치 갱신 + 커밋**

`docs/CURRENT_STATE.md`의 핵심 데이터 표(PDF/텍스트 수치)와 "최근 이력"에 이번 작업 한 줄 추가.

```bash
git add data/audit/metadata_cleanup_final_report.md docs/CURRENT_STATE.md
git commit -m "docs: metadata cleanup final report and state refresh

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review 결과 (계획 작성 후 점검)

- **스펙 커버리지**: R1~R6 → Task 1~5 / 가짜 PDF → Task 6 / 크롤러 코드 수정(esteri·presse·doras) → Task 7 / masaf 재수집 → Task 9 / 그리스 3곳 → Task 10 / 검증·보고 → Task 11. 스펙의 "실행 순서" 7단계 전부 대응. 갭 없음.
- **타입 일관성**: `clean_text/clean_keywords`는 원값 반환 규약(Task 1 정의 = Task 4 사용), `normalize_date`는 None=파싱불가(Task 2 정의 = Task 4 `_rule_date` 사용), `backfill` 리포트 dict 키(Task 8 정의 = Task 9·10 기대값 표기) 일치 확인.
- **알려진 확인 포인트(placeholder 아님, 명시적 분기)**: ① `find_by_dedup_key` 시그니처 (Task 8 Step 3 주의사항) ② scripts 패키지 임포트 방식 (Task 1 Step 2) ③ masaf 인코딩 방향 (Task 9 Step 1 진단으로 결정) ④ 그리스 2곳 크롤러 수정 필요 여부 (Task 10 dry-run으로 판정).
