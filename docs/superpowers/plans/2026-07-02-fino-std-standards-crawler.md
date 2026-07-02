# FINO 회계 기준서(K-IFRS/GAAP) 크롤러 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** db.kasb.or.kr JSON API로 K-IFRS·GAAP 기준서 전문을 문단 단위로 `data/fino_std.db`에 수집하고 MD/NDJSON export를 제공한다.

**Architecture:** 신규 독립 모듈 `crawler/fino_std/` — fino_law의 documents/articles 패턴 미러링. 크롤 백본은 `api/title/{stdNum}`(big 섹션 목록) → `api/content/{stdNum}/{documentId}`(문단 스트림). big 섹션이 하위 문단을 전부 포함하므로 big만 순회하면 무중복 전체 수집(실측 확인).

**Tech Stack:** Python 3, httpx, sqlite3, pytest. LLM 없음.

**스펙:** `docs/superpowers/specs/2026-07-02-fino-std-standards-crawler-design.md`

## Global Constraints

- DB는 `data/fino_std.db` (신규). 기존 fino_law.db / fino_acct.db 무접촉. data/*.db는 .gitignore 유지.
- API 요청 지연 기본 0.4s, 재시도 3회(backoff), UA `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36`.
- 미지원/빈 기준서(91, 93, 1191, 1192, 1118, 10121)는 `[skip]` 로그 후 통과 — 실패로 처리하지 않는다.
- 재수집 = 문서 단위 교체(paragraphs DELETE 후 INSERT, 단일 트랜잭션). 재실행=최신화.
- 문단 deep-link: `https://db.kasb.or.kr/s/{std_num}/{para_num}`.
- 테스트 실행: `.venv/bin/python -m pytest tests/test_fino_std_*.py -q`.
- 커밋 메시지는 기존 스타일(`feat(fino_std): ...`, `test(fino_std): ...`) 유지.

---

### Task 1: 모듈 뼈대 — models.py + sources.py (시드 102종)

**Files:**
- Create: `crawler/fino_std/__init__.py` (빈 파일)
- Create: `crawler/fino_std/models.py`
- Create: `crawler/fino_std/sources.py`
- Test: `tests/test_fino_std_sources.py`

**Interfaces:**
- Produces: `StdTarget(std_num: int, title: str, std_type: str)`, `STD_SEEDS: tuple[StdTarget, ...]`(102종), `Section(document_id: str, title: str, ref: str)`, `ParagraphRecord(para_num, section_path, body_html, body_text, seq, source_url)`, `std_source_url(std_num) -> str`, `para_citation_url(std_num, para_num) -> str`

- [ ] **Step 1: Write the failing test**

`tests/test_fino_std_sources.py`:

```python
from crawler.fino_std.sources import STD_SEEDS, para_citation_url, std_source_url


def test_seed_counts_by_type() -> None:
    by_type: dict[str, int] = {}
    for t in STD_SEEDS:
        by_type[t.std_type] = by_type.get(t.std_type, 0) + 1
    assert by_type == {"kifrs": 43, "kifrs_interp": 19, "kifrs_etc": 3, "gaap": 37}
    assert len(STD_SEEDS) == 102


def test_seed_membership_and_exclusions() -> None:
    nums = {t.std_num for t in STD_SEEDS}
    assert {1000, 1001, 1116, 1117, 1118, 2010, 2123, 99, 1, 33, 60, 91, 93} <= nums
    # 구기준(KASB판 제외)과 주석 처리된 92는 시드에 없어야 한다
    assert not ({1011, 1017, 1018, 1104, 92} & nums)


def test_seed_titles() -> None:
    m = {t.std_num: t.title for t in STD_SEEDS}
    assert m[1001] == "재무제표 표시"
    assert m[99] == "재무회계개념체계"
    assert m[13] == "리스"
    assert m[2123] == "법인세 처리의 불확실성"


def test_citation_urls() -> None:
    assert std_source_url(1001) == "https://db.kasb.or.kr/s/1001"
    assert para_citation_url(1001, "한10.1") == "https://db.kasb.or.kr/s/1001/한10.1"
    assert para_citation_url(1001, "") == "https://db.kasb.or.kr/s/1001"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_std_sources.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'crawler.fino_std'`

- [ ] **Step 3: Write implementation**

`crawler/fino_std/__init__.py`: 빈 파일.

`crawler/fino_std/models.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StdTarget:
    std_num: int
    title: str
    std_type: str  # kifrs / kifrs_interp / kifrs_etc / gaap


@dataclass(frozen=True, slots=True)
class Section:
    document_id: str
    title: str
    ref: str


@dataclass(frozen=True, slots=True)
class ParagraphRecord:
    para_num: str        # "1", "한10.1", "82A", "BC1", "IG7", 없으면 ""
    section_path: str    # "재무제표 > 일반사항 > 계속기업"
    body_html: str
    body_text: str
    seq: int
    source_url: str = ""
```

`crawler/fino_std/sources.py` (시드는 db.kasb.or.kr SPA 번들 stdMap/typeStds에서 2026-07-02 추출, KASB판 표시 항목만):

```python
from typing import Final

from .models import StdTarget

_BASE = "https://db.kasb.or.kr"

# K-IFRS 기준서 + 개념체계 (KASB판 현행 43종; 구기준 1011/1017/1018/1104 제외)
_KIFRS: Final = (
    (1000, "재무보고를 위한 개념체계"),
    (1001, "재무제표 표시"), (1002, "재고자산"), (1007, "현금흐름표"),
    (1008, "회계정책, 회계추정치 변경과 오류"), (1010, "보고기간후사건"),
    (1012, "법인세"), (1016, "유형자산"), (1019, "종업원급여"),
    (1020, "정부보조금의 회계처리와 정부지원의 공시"), (1021, "환율변동효과"),
    (1023, "차입원가"), (1024, "특수관계자 공시"),
    (1026, "퇴직급여제도에 의한 회계처리와 보고"), (1027, "별도재무제표"),
    (1028, "관계기업과 공동기업에 대한 투자"), (1029, "초인플레이션 경제에서의 재무보고"),
    (1032, "금융상품: 표시"), (1033, "주당이익"), (1034, "중간재무보고"),
    (1036, "자산손상"), (1037, "충당부채, 우발부채, 우발자산"), (1038, "무형자산"),
    (1039, "금융상품: 인식과 측정"), (1040, "투자부동산"), (1041, "농림어업"),
    (1101, "한국채택국제회계기준의 최초채택"), (1102, "주식기준보상"),
    (1103, "사업결합"), (1105, "매각예정비유동자산과 중단영업"),
    (1106, "광물자원의 탐사와 평가"), (1107, "금융상품: 공시"), (1108, "영업부문"),
    (1109, "금융상품"), (1110, "연결재무제표"), (1111, "공동약정"),
    (1112, "타 기업에 대한 지분의 공시"), (1113, "공정가치 측정"),
    (1114, "규제이연계정"), (1115, "고객과의 계약에서 생기는 수익"),
    (1116, "리스"), (1117, "보험계약"),
    (1118, "재무제표 표시와 공시(조기적용가능)"),  # 현재 titles 빈 응답 → skip됨
)

# K-IFRS 해석서 19종
_KIFRS_INTERP: Final = (
    (2010, "정부지원: 영업활동과 특정한 관련이 없는 경우"),
    (2025, "법인세: 기업이나 주주의 납세지위 변동"),
    (2029, "민간투자사업: 공시"), (2032, "무형자산: 웹 사이트 원가"),
    (2101, "사후처리 및 복구관련 충당부채의 변경"),
    (2102, "조합원 지분과 유사 지분"),
    (2105, "사후처리, 복구 및 환경정화를 위한 기금의 지분에 대한 권리"),
    (2106, "특정 시장에 참여함에 따라 발생하는 부채: 폐전기·전자제품"),
    (2107, "기업회계기준서 제1029호의 적용"),
    (2110, "중간재무보고와 손상"), (2112, "민간투자사업"),
    (2114, "기업회계기준서 제1019호: 확정급여자산한도, 최소적립요건 및 그 상호작용"),
    (2116, "해외사업장순투자의 위험회피"), (2117, "소유주에 대한 비현금자산의 분배"),
    (2119, "지분상품에 의한 금융부채의 소멸"), (2120, "노천광산 생산단계의 박토원가"),
    (2121, "부담금"), (2122, "외화 거래와 선지급·선수취 대가"),
    (2123, "법인세 처리의 불확실성"),
)

# 번역서·적용의견서 (API 미지원/빈 응답이 많음 — skip 허용 시드)
_KIFRS_ETC: Final = (
    (1191, "경영진설명서 작성을 위한 개념체계 번역서"),
    (1192, "중요성에 대한 판단 번역서"),
    (10121, "회계기준 적용의견서"),
)

# 일반기업회계기준 37종 (92는 사이트에서 주석 처리 → 제외)
_GAAP: Final = (
    (99, "재무회계개념체계"),
    (1, "목적, 구성 및 적용"), (2, "재무제표의 작성과 표시Ⅰ"),
    (3, "재무제표의 작성과 표시Ⅱ"), (4, "연결재무제표"),
    (5, "회계정책, 회계추정의 변경 및 오류"), (6, "금융자산·금융부채"),
    (7, "재고자산"), (8, "지분법"), (9, "조인트벤처 투자"), (10, "유형자산"),
    (11, "무형자산"), (12, "사업결합"), (13, "리스"),
    (14, "충당부채, 우발부채 및 우발자산"), (15, "자본"), (16, "수익"),
    (17, "정부보조금의 회계처리"), (18, "차입원가자본화"), (19, "주식기준보상"),
    (20, "자산손상"), (21, "종업원급여"), (22, "법인세회계"), (23, "환율변동효과"),
    (24, "보고기간후사건"), (25, "특수관계자 공시"), (26, "기본주당이익"),
    (27, "특수활동"), (28, "중단사업"), (29, "중간재무제표"),
    (30, "일반기업회계기준의 최초채택"), (31, "중소기업 회계처리 특례"),
    (32, "동일지배거래"), (33, "온실가스 배출권과 배출부채"),
    (60, "시행일 및 경과규정"), (91, "보험업회계처리준칙"),
    (93, "일반기업회계기준 재무제표 영문양식"),
)


def _seeds() -> tuple[StdTarget, ...]:
    out: list[StdTarget] = []
    for nums, kind in ((_KIFRS, "kifrs"), (_KIFRS_INTERP, "kifrs_interp"),
                       (_KIFRS_ETC, "kifrs_etc"), (_GAAP, "gaap")):
        out.extend(StdTarget(n, t, kind) for n, t in nums)
    return tuple(out)


STD_SEEDS: Final[tuple[StdTarget, ...]] = _seeds()


def std_source_url(std_num: int) -> str:
    return f"{_BASE}/s/{std_num}"


def para_citation_url(std_num: int, para_num: str) -> str:
    return f"{_BASE}/s/{std_num}/{para_num}" if para_num else std_source_url(std_num)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_std_sources.py -q`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_std/__init__.py crawler/fino_std/models.py crawler/fino_std/sources.py tests/test_fino_std_sources.py
git commit -m "feat(fino_std): 기준서 크롤러 시드/모델 — K-IFRS 43+해석서 19+기타 3+GAAP 37"
```

---

### Task 2: fetch.py — API 클라이언트

**Files:**
- Create: `crawler/fino_std/fetch.py`
- Test: `tests/test_fino_std_fetch.py`

**Interfaces:**
- Consumes: 없음 (독립)
- Produces: `fetch_titles(client: httpx.Client, std_num: int, delay: float = 0.4) -> dict | None`, `fetch_content(client: httpx.Client, std_num: int, document_id: str, delay: float = 0.4) -> dict | None`, `is_unavailable(data: dict | None) -> bool` (None 또는 `{"message": ...}` 오류 페이로드 판정)

- [ ] **Step 1: Write the failing test** (순수 로직인 `is_unavailable`만 테스트 — 네트워크 함수는 fino_law fetch와 동일하게 픽스처 기반 상위 테스트로 커버)

`tests/test_fino_std_fetch.py`:

```python
from crawler.fino_std.fetch import is_unavailable


def test_is_unavailable_on_error_payload() -> None:
    assert is_unavailable(None)
    assert is_unavailable({"message": "Something went wrong!"})


def test_is_unavailable_false_on_real_payloads() -> None:
    assert not is_unavailable({"titles": [{"title": "목적"}]})
    assert not is_unavailable({"clauses": [], "status": 200})
    # titles가 비어 있어도 오류 페이로드는 아니다(빈 기준서는 호출부에서 skip)
    assert not is_unavailable({"titles": [], "titlesObj": {}})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_std_fetch.py -q`
Expected: FAIL — `ModuleNotFoundError` (fetch 모듈 없음)

- [ ] **Step 3: Write implementation**

`crawler/fino_std/fetch.py`:

```python
from __future__ import annotations

import time

import httpx

BASE = "https://db.kasb.or.kr"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def is_unavailable(data: dict | None) -> bool:
    """서버가 미지원 기준서에 주는 오류 페이로드({"message": ...}) 여부."""
    if data is None:
        return True
    return "message" in data and "titles" not in data and "clauses" not in data


def _get_json(client: httpx.Client, path: str, delay: float) -> dict | None:
    for attempt in range(3):
        if delay > 0:
            time.sleep(delay)
        try:
            resp = client.get(f"{BASE}{path}", headers={"User-Agent": _UA}, timeout=30)
        except httpx.HTTPError:
            time.sleep(1 + attempt)
            continue
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 500:  # 미지원 기준서 — 재시도 무의미
            return None
        time.sleep(1 + attempt)
    return None


def fetch_titles(client: httpx.Client, std_num: int, delay: float = 0.4) -> dict | None:
    return _get_json(client, f"/api/title/{std_num}", delay)


def fetch_content(client: httpx.Client, std_num: int, document_id: str, delay: float = 0.4) -> dict | None:
    return _get_json(client, f"/api/content/{std_num}/{document_id}", delay)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_std_fetch.py -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_std/fetch.py tests/test_fino_std_fetch.py
git commit -m "feat(fino_std): db.kasb.or.kr API 클라이언트(재시도/지연/미지원 판정)"
```

---

### Task 3: 픽스처 다운로드 + parsers.py

**Files:**
- Create: `tests/fixtures/fino_std/title_1001.json` (실 API 응답)
- Create: `tests/fixtures/fino_std/content_1001_1f0730.json` (실 API 응답: '전체 재무제표' mid 섹션, 문단 10~14 7개)
- Create: `crawler/fino_std/parsers.py`
- Test: `tests/test_fino_std_parsers.py`

**Interfaces:**
- Consumes: `Section`, `ParagraphRecord` (Task 1), `para_citation_url` (Task 1)
- Produces: `pick_big_sections(titles_json: dict) -> list[Section]`, `parse_content(content_json: dict, *, std_num: int, start_seq: int) -> list[ParagraphRecord]`, `html_to_text(raw: str) -> str`

- [ ] **Step 1: Download fixtures (실 API에서 1회)**

```bash
mkdir -p tests/fixtures/fino_std
curl -s -m 20 "https://db.kasb.or.kr/api/title/1001" -A "Mozilla/5.0" -o tests/fixtures/fino_std/title_1001.json
sleep 1
curl -s -m 20 "https://db.kasb.or.kr/api/content/1001/1f0730" -A "Mozilla/5.0" -o tests/fixtures/fino_std/content_1001_1f0730.json
.venv/bin/python -c "import json; t=json.load(open('tests/fixtures/fino_std/title_1001.json')); c=json.load(open('tests/fixtures/fino_std/content_1001_1f0730.json')); print(len(t['titles']), len(c['clauses']))"
```

Expected: `128 …`(titles 128, clauses 8±2 — 다운로드 시점의 실값을 확인하고 아래 테스트의 기대값을 실값으로 맞춘다. big 10개·문단 7개는 2026-07-02 실측)

- [ ] **Step 2: Write the failing test**

`tests/test_fino_std_parsers.py`:

```python
import json
from pathlib import Path

from crawler.fino_std.parsers import html_to_text, parse_content, pick_big_sections

_FX = Path("tests/fixtures/fino_std")


def test_html_to_text_strips_tags_and_entities() -> None:
    raw = '<div class="para-inner-para">이 기준서는 &#039;일반목적&#039;   재무제표<br/>를 다룬다.</div>'
    assert html_to_text(raw) == "이 기준서는 '일반목적' 재무제표 를 다룬다."


def test_pick_big_sections_from_fixture() -> None:
    titles = json.load(open(_FX / "title_1001.json"))
    bigs = pick_big_sections(titles)
    assert len(bigs) == 10                      # 본문 8 + 적용사례 + 결론도출근거
    assert bigs[0].title == "목적" and bigs[0].document_id == "c214c7"
    assert any(b.title.startswith("결론도출근거") for b in bigs)


def test_parse_content_extracts_paragraphs_with_numbers() -> None:
    content = json.load(open(_FX / "content_1001_1f0730.json"))
    paras = parse_content(content, std_num=1001, start_seq=0)
    nums = [p.para_num for p in paras]
    assert nums == ["10", "한10.1", "10A", "11", "12", "13", "14"]
    assert all(p.body_text and "<" not in p.body_text for p in paras)
    assert paras[0].source_url == "https://db.kasb.or.kr/s/1001/10"
    assert [p.seq for p in paras] == list(range(7))


def test_parse_content_builds_section_path() -> None:
    content = json.load(open(_FX / "content_1001_1f0730.json"))
    paras = parse_content(content, std_num=1001, start_seq=0)
    # title clause 스택으로 경로 구성 — '전체 재무제표' 섹션이 경로에 있어야 한다
    assert "전체 재무제표" in paras[0].section_path
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_std_parsers.py -q`
Expected: FAIL — `ModuleNotFoundError` (parsers 모듈 없음)

- [ ] **Step 4: Write implementation**

`crawler/fino_std/parsers.py`:

```python
from __future__ import annotations

import html as _html
import re

from .models import ParagraphRecord, Section
from .sources import para_citation_url

_TAG_RE = re.compile(r"<[^>]+>")


def html_to_text(raw: str) -> str:
    text = _TAG_RE.sub(" ", raw or "")
    text = _html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def pick_big_sections(titles_json: dict) -> list[Section]:
    out: list[Section] = []
    for t in titles_json.get("titles") or []:
        if t.get("type") != "big":
            continue
        out.append(Section(
            document_id=str(t.get("documentId") or ""),
            title=(t.get("title") or "").strip(),
            ref=str(t.get("ref") or ""),
        ))
    return [s for s in out if s.document_id]


def parse_content(content_json: dict, *, std_num: int, start_seq: int) -> list[ParagraphRecord]:
    """clauses 스트림 순회. title clause는 level 스택으로 섹션경로를 만들고,
    paragraph clause는 문단 레코드로 변환한다."""
    stack: dict[int, str] = {}
    out: list[ParagraphRecord] = []
    seq = start_seq
    for cl in content_json.get("clauses") or []:
        kind = cl.get("type")
        if kind == "title":
            level = int(cl.get("level") or 0)
            title = (cl.get("title") or "").strip()
            for deeper in [k for k in stack if k >= level]:
                del stack[deeper]
            if title:
                stack[level] = title
        elif kind == "paragraph":
            raw = cl.get("content") or ""
            text = html_to_text(raw)
            if not text:
                continue
            num = str(cl.get("number") or "").strip()
            titles = [stack[k] for k in sorted(stack)]
            dedup = [t for i, t in enumerate(titles) if i == 0 or t != titles[i - 1]]
            out.append(ParagraphRecord(
                para_num=num,
                section_path=" > ".join(dedup),
                body_html=raw,
                body_text=text,
                seq=seq,
                source_url=para_citation_url(std_num, num),
            ))
            seq += 1
    return out
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_std_parsers.py -q`
Expected: PASS (4 tests). 실패 시 픽스처 실값(문단 번호 목록 등)과 기대값을 대조해 테스트 기대값을 실값으로 수정(픽스처가 진실).

- [ ] **Step 6: Commit**

```bash
git add tests/fixtures/fino_std/ crawler/fino_std/parsers.py tests/test_fino_std_parsers.py
git commit -m "feat(fino_std): titles/content 파서 — big 섹션 필터, 문단 추출, 섹션경로 스택"
```

---

### Task 4: db.py — 스키마 + upsert/replace

**Files:**
- Create: `crawler/fino_std/db.py`
- Test: `tests/test_fino_std_db.py`

**Interfaces:**
- Consumes: `ParagraphRecord` (Task 1)
- Produces: `connect_db(db_path: Path) -> sqlite3.Connection`, `init_schema(conn) -> None`, `upsert_document(conn, *, std_num: int, std_type: str, title: str, source_url: str) -> int`, `replace_paragraphs(conn, document_id: int, records: list[ParagraphRecord]) -> int`

- [ ] **Step 1: Write the failing test**

`tests/test_fino_std_db.py`:

```python
from pathlib import Path

from crawler.fino_std.db import connect_db, init_schema, replace_paragraphs, upsert_document
from crawler.fino_std.models import ParagraphRecord


def _para(num: str, seq: int, text: str = "본문") -> ParagraphRecord:
    return ParagraphRecord(para_num=num, section_path="목적", body_html=f"<div>{text}</div>",
                           body_text=text, seq=seq, source_url=f"https://db.kasb.or.kr/s/1001/{num}")


def test_upsert_document_idempotent(tmp_path: Path) -> None:
    with connect_db(tmp_path / "t.db") as conn:
        init_schema(conn)
        a = upsert_document(conn, std_num=1001, std_type="kifrs", title="재무제표 표시",
                            source_url="https://db.kasb.or.kr/s/1001")
        b = upsert_document(conn, std_num=1001, std_type="kifrs", title="재무제표 표시(개정)",
                            source_url="https://db.kasb.or.kr/s/1001")
        assert a == b
        row = conn.execute("SELECT title FROM documents WHERE id = ?", (a,)).fetchone()
        assert row["title"] == "재무제표 표시(개정)"


def test_replace_paragraphs_swaps_cleanly(tmp_path: Path) -> None:
    with connect_db(tmp_path / "t.db") as conn:
        init_schema(conn)
        doc = upsert_document(conn, std_num=1001, std_type="kifrs", title="재무제표 표시",
                              source_url="https://db.kasb.or.kr/s/1001")
        assert replace_paragraphs(conn, doc, [_para("1", 0), _para("2", 1)]) == 2
        # 재수집: 문단 구성이 바뀌어도 잔재 없이 교체된다
        assert replace_paragraphs(conn, doc, [_para("1", 0, "개정 본문")]) == 1
        rows = conn.execute(
            "SELECT para_num, body_text FROM paragraphs WHERE document_id = ? ORDER BY seq", (doc,)
        ).fetchall()
        assert [(r["para_num"], r["body_text"]) for r in rows] == [("1", "개정 본문")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_std_db.py -q`
Expected: FAIL — `ModuleNotFoundError` (db 모듈 없음)

- [ ] **Step 3: Write implementation**

`crawler/fino_std/db.py`:

```python
from pathlib import Path
import sqlite3

from .models import ParagraphRecord


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
            std_num INTEGER NOT NULL UNIQUE,
            std_type TEXT NOT NULL,
            title TEXT NOT NULL,
            source_url TEXT NOT NULL,
            collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS paragraphs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            para_num TEXT NOT NULL,
            section_path TEXT NOT NULL,
            body_html TEXT NOT NULL,
            body_text TEXT NOT NULL,
            source_url TEXT NOT NULL,
            seq INTEGER NOT NULL,
            UNIQUE(document_id, seq)
        );

        CREATE INDEX IF NOT EXISTS idx_std_documents_type ON documents(std_type);
        CREATE INDEX IF NOT EXISTS idx_std_paragraphs_doc ON paragraphs(document_id);
        CREATE INDEX IF NOT EXISTS idx_std_paragraphs_num ON paragraphs(para_num);
        """
    )
    conn.commit()


def upsert_document(
    conn: sqlite3.Connection, *, std_num: int, std_type: str, title: str, source_url: str
) -> int:
    conn.execute(
        """
        INSERT INTO documents (std_num, std_type, title, source_url)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(std_num) DO UPDATE SET
            std_type = excluded.std_type,
            title = excluded.title,
            source_url = excluded.source_url,
            collected_at = CURRENT_TIMESTAMP
        """,
        (std_num, std_type, title, source_url),
    )
    conn.commit()
    row = conn.execute("SELECT id FROM documents WHERE std_num = ?", (std_num,)).fetchone()
    return int(row["id"])


def replace_paragraphs(
    conn: sqlite3.Connection, document_id: int, records: list[ParagraphRecord]
) -> int:
    with conn:  # 단일 트랜잭션: 삭제+삽입
        conn.execute("DELETE FROM paragraphs WHERE document_id = ?", (document_id,))
        conn.executemany(
            """
            INSERT INTO paragraphs (
                document_id, para_num, section_path, body_html, body_text, source_url, seq
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(document_id, r.para_num, r.section_path, r.body_html, r.body_text,
              r.source_url, r.seq) for r in records],
        )
    return len(records)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_std_db.py -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_std/db.py tests/test_fino_std_db.py
git commit -m "feat(fino_std): documents/paragraphs 스키마 — upsert + 문서 단위 교체"
```

---

### Task 5: collect.py — CLI + 수집 루프 (통합 테스트)

**Files:**
- Create: `crawler/fino_std/collect.py`
- Test: `tests/test_fino_std_collect.py`

**Interfaces:**
- Consumes: `STD_SEEDS`, `std_source_url` (Task 1), `fetch_titles`/`fetch_content`/`is_unavailable` (Task 2), `pick_big_sections`/`parse_content` (Task 3), `connect_db`/`init_schema`/`upsert_document`/`replace_paragraphs` (Task 4)
- Produces: CLI `python -m crawler.fino_std.collect [--types kifrs,kifrs_interp,kifrs_etc,gaap] [--std N] [--db-path P] [--delay-seconds F]`, `collect_standards(*, db_path, types, std_num, delay) -> tuple[int, int]` (docs, paras)

- [ ] **Step 1: Write the failing test** (fetch 모킹 — 픽스처 재사용)

`tests/test_fino_std_collect.py`:

```python
import json
from pathlib import Path

import crawler.fino_std.collect as collect_mod
from crawler.fino_std.collect import collect_standards
from crawler.fino_std.db import connect_db

_FX = Path("tests/fixtures/fino_std")


def _install_fake_fetch(monkeypatch) -> None:
    titles = json.load(open(_FX / "title_1001.json"))
    content = json.load(open(_FX / "content_1001_1f0730.json"))

    def fake_titles(client, std_num, delay=0.4):
        return titles if std_num == 1001 else None  # 1001 외 전부 미지원 시늉

    def fake_content(client, std_num, document_id, delay=0.4):
        return content

    monkeypatch.setattr(collect_mod, "fetch_titles", fake_titles)
    monkeypatch.setattr(collect_mod, "fetch_content", fake_content)


def test_collect_standards_end_to_end(tmp_path: Path, monkeypatch) -> None:
    _install_fake_fetch(monkeypatch)
    db = tmp_path / "std.db"
    docs, paras = collect_standards(db_path=db, types={"kifrs"}, std_num=None, delay=0)
    assert docs == 1                    # 1001만 성공, 나머지 kifrs는 skip
    assert paras == 10 * 7              # big 10개 × 픽스처 content 문단 7개
    with connect_db(db) as conn:
        row = conn.execute("SELECT std_type, title FROM documents WHERE std_num = 1001").fetchone()
        assert row["std_type"] == "kifrs" and row["title"] == "재무제표 표시"
        n = conn.execute("SELECT COUNT(*) AS c FROM paragraphs").fetchone()["c"]
        assert n == 70


def test_collect_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    _install_fake_fetch(monkeypatch)
    db = tmp_path / "std.db"
    collect_standards(db_path=db, types={"kifrs"}, std_num=1001, delay=0)
    collect_standards(db_path=db, types={"kifrs"}, std_num=1001, delay=0)  # 재실행
    with connect_db(db) as conn:
        assert conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"] == 1
        assert conn.execute("SELECT COUNT(*) AS c FROM paragraphs").fetchone()["c"] == 70


def test_collect_single_std_filter(tmp_path: Path, monkeypatch) -> None:
    _install_fake_fetch(monkeypatch)
    db = tmp_path / "std.db"
    docs, _ = collect_standards(db_path=db, types={"kifrs", "gaap"}, std_num=1001, delay=0)
    assert docs == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_std_collect.py -q`
Expected: FAIL — `ModuleNotFoundError` (collect 모듈 없음)

- [ ] **Step 3: Write implementation**

`crawler/fino_std/collect.py`:

```python
from __future__ import annotations

import argparse
from pathlib import Path

import httpx

from .db import connect_db, init_schema, replace_paragraphs, upsert_document
from .fetch import fetch_content, fetch_titles, is_unavailable
from .parsers import parse_content, pick_big_sections
from .sources import STD_SEEDS, std_source_url

DEFAULT_DB_PATH = Path("data/fino_std.db")
ALL_TYPES = ("kifrs", "kifrs_interp", "kifrs_etc", "gaap")


def collect_standards(
    *, db_path: Path, types: set[str], std_num: int | None, delay: float
) -> tuple[int, int]:
    docs = 0
    paras = 0
    targets = [t for t in STD_SEEDS if t.std_type in types
               and (std_num is None or t.std_num == std_num)]
    with connect_db(db_path) as conn, httpx.Client(timeout=30) as client:
        init_schema(conn)
        for t in targets:
            titles = fetch_titles(client, t.std_num, delay)
            if is_unavailable(titles):
                print(f"[skip] API 미지원: {t.std_num} {t.title}", flush=True)
                continue
            bigs = pick_big_sections(titles)
            if not bigs:
                print(f"[skip] 빈 목차: {t.std_num} {t.title}", flush=True)
                continue
            records = []
            for big in bigs:
                content = fetch_content(client, t.std_num, big.document_id, delay)
                if is_unavailable(content):
                    print(f"[warn] 섹션 실패: {t.std_num} {big.title}", flush=True)
                    continue
                records.extend(parse_content(content, std_num=t.std_num, start_seq=len(records)))
            doc_id = upsert_document(
                conn, std_num=t.std_num, std_type=t.std_type,
                title=t.title, source_url=std_source_url(t.std_num),
            )
            replace_paragraphs(conn, doc_id, records)
            docs += 1
            paras += len(records)
            print(f"[{t.std_type}] {t.std_num} {t.title}: 섹션 {len(bigs)} 문단 {len(records)}",
                  flush=True)
    return docs, paras


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m crawler.fino_std.collect")
    _ = p.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    _ = p.add_argument("--types", type=str, default=",".join(ALL_TYPES),
                       help="쉼표구분: kifrs,kifrs_interp,kifrs_etc,gaap")
    _ = p.add_argument("--std", type=int, default=None, help="단일 기준서 번호만")
    _ = p.add_argument("--delay-seconds", type=float, default=0.4)
    return p


def main() -> int:
    args = build_parser().parse_args()
    types = {s.strip() for s in args.types.split(",") if s.strip()}
    unknown = types - set(ALL_TYPES)
    if unknown:
        raise SystemExit(f"알 수 없는 타입: {sorted(unknown)}")
    d, a = collect_standards(db_path=args.db_path, types=types,
                             std_num=args.std, delay=args.delay_seconds)
    print(f"done std: documents={d} paragraphs={a}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_std_collect.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: Run full fino_std test suite**

Run: `.venv/bin/python -m pytest tests/test_fino_std_*.py -q`
Expected: 전부 PASS

- [ ] **Step 6: Commit**

```bash
git add crawler/fino_std/collect.py tests/test_fino_std_collect.py
git commit -m "feat(fino_std): 수집 CLI — 타입/단일 기준서 필터, graceful skip, 멱등 재수집"
```

---

### Task 6: export_markdown.py + export_ndjson.py

**Files:**
- Create: `crawler/fino_std/export_markdown.py`
- Create: `crawler/fino_std/export_ndjson.py`
- Test: `tests/test_fino_std_export.py`

**Interfaces:**
- Consumes: `connect_db` (Task 4)
- Produces: `export_markdown(*, db_path: Path, out_dir: Path) -> int` (문서당 1개 MD 파일, 반환=파일 수), `export_ndjson(*, db_path: Path, out_path: Path) -> int` (반환=행 수)

- [ ] **Step 1: Write the failing test**

`tests/test_fino_std_export.py`:

```python
from pathlib import Path

from crawler.fino_std.db import connect_db, init_schema, replace_paragraphs, upsert_document
from crawler.fino_std.export_markdown import export_markdown
from crawler.fino_std.export_ndjson import export_ndjson
from crawler.fino_std.models import ParagraphRecord


def _seed_db(db: Path) -> None:
    with connect_db(db) as conn:
        init_schema(conn)
        doc = upsert_document(conn, std_num=1001, std_type="kifrs", title="재무제표 표시",
                              source_url="https://db.kasb.or.kr/s/1001")
        replace_paragraphs(conn, doc, [
            ParagraphRecord(para_num="1", section_path="목적", body_html="<div>목적 본문</div>",
                            body_text="목적 본문", seq=0,
                            source_url="https://db.kasb.or.kr/s/1001/1"),
            ParagraphRecord(para_num="9", section_path="재무제표 > 재무제표의 목적",
                            body_html="<div>재무제표는…</div>", body_text="재무제표는…", seq=1,
                            source_url="https://db.kasb.or.kr/s/1001/9"),
        ])


def test_export_markdown(tmp_path: Path) -> None:
    db = tmp_path / "std.db"
    _seed_db(db)
    n = export_markdown(db_path=db, out_dir=tmp_path / "md")
    assert n == 1
    text = (tmp_path / "md" / "kifrs_1001_재무제표 표시.md").read_text(encoding="utf-8")
    assert "# [K-IFRS 1001] 재무제표 표시" in text
    assert "## 재무제표 > 재무제표의 목적" in text     # 섹션경로가 헤더로
    assert "**9** 재무제표는…" in text
    assert "https://db.kasb.or.kr/s/1001/9" in text


def test_export_ndjson(tmp_path: Path) -> None:
    import json
    db = tmp_path / "std.db"
    _seed_db(db)
    out = tmp_path / "std.ndjson"
    assert export_ndjson(db_path=db, out_path=out) == 2
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["std_num"] == 1001 and rows[0]["para_num"] == "1"
    assert rows[1]["source_url"].endswith("/s/1001/9")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_std_export.py -q`
Expected: FAIL — `ModuleNotFoundError` (export 모듈 없음)

- [ ] **Step 3: Write implementation**

`crawler/fino_std/export_ndjson.py`:

```python
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
            SELECT d.std_num, d.std_type, d.title AS doc_title,
                   p.para_num, p.section_path, p.body_text, p.source_url
            FROM paragraphs p JOIN documents d ON d.id = p.document_id
            ORDER BY d.std_num, p.seq
            """
        ).fetchall()
        for r in rows:
            fh.write(json.dumps(dict(r), ensure_ascii=False) + "\n")
            count += 1
    return count
```

`crawler/fino_std/export_markdown.py`:

```python
from __future__ import annotations

from pathlib import Path

from .db import connect_db

_TYPE_LABEL = {"kifrs": "K-IFRS", "kifrs_interp": "K-IFRS 해석서",
               "kifrs_etc": "K-IFRS 기타", "gaap": "일반기업회계기준"}


def export_markdown(*, db_path: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    count = 0
    with connect_db(db_path) as conn:
        docs = conn.execute("SELECT * FROM documents ORDER BY std_num").fetchall()
        for d in docs:
            paras = conn.execute(
                "SELECT * FROM paragraphs WHERE document_id = ? ORDER BY seq", (d["id"],)
            ).fetchall()
            label = _TYPE_LABEL.get(d["std_type"], d["std_type"])
            lines = [f"# [{label} {d['std_num']}] {d['title']}", "",
                     f"> 출처: {d['source_url']} (수집 {d['collected_at']})", ""]
            current_path = None
            for p in paras:
                if p["section_path"] != current_path:
                    current_path = p["section_path"]
                    lines += [f"## {current_path}", ""]
                head = f"**{p['para_num']}** " if p["para_num"] else ""
                lines += [f"{head}{p['body_text']}", f"[{p['source_url']}]({p['source_url']})", ""]
            name = f"{d['std_type']}_{d['std_num']}_{d['title'].replace('/', '·')}.md"
            (out_dir / name).write_text("\n".join(lines), encoding="utf-8")
            count += 1
    return count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_std_export.py -q`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_std/export_markdown.py crawler/fino_std/export_ndjson.py tests/test_fino_std_export.py
git commit -m "feat(fino_std): MD/NDJSON export — 섹션경로 헤더 + 문단 deep-link"
```

---

### Task 7: 실수집 + 검증 + 문서/메모리 갱신

**Files:**
- Modify: `docs/2026-07-02_fino_corpus_crawler_handoff.md` (현황 표 갱신)
- Create(외부): `data/fino_std.db` (gitignore — 커밋 안 함)

- [ ] **Step 1: 소규모 시험 수집 (기준서 1개)**

Run: `.venv/bin/python -m crawler.fino_std.collect --std 1001 --types kifrs`
Expected: `[kifrs] 1001 재무제표 표시: 섹션 10 문단 …` 후 `done std: documents=1 paragraphs=…` (문단 수백 개)

- [ ] **Step 2: 전체 수집** (백그라운드, ~10분)

Run: `.venv/bin/python -m crawler.fino_std.collect 2>&1 | tee data/fino_std_collect.log`
Expected: `[skip]` 6건 내외(91, 93, 1191, 1192, 1118, 10121), 오류 0, `done std: documents≈96 paragraphs=수만`

- [ ] **Step 3: 수집 품질 검증**

```bash
.venv/bin/python - <<'EOF'
import sqlite3
conn = sqlite3.connect("data/fino_std.db"); conn.row_factory = sqlite3.Row
print("타입별 문서:", {r["std_type"]: r["c"] for r in conn.execute(
    "SELECT std_type, COUNT(*) c FROM documents GROUP BY std_type")})
print("총 문단:", conn.execute("SELECT COUNT(*) FROM paragraphs").fetchone()[0])
print("빈 본문:", conn.execute("SELECT COUNT(*) FROM paragraphs WHERE body_text=''").fetchone()[0])
print("문단 0개 문서:", [r["std_num"] for r in conn.execute(
    "SELECT d.std_num FROM documents d LEFT JOIN paragraphs p ON p.document_id=d.id GROUP BY d.id HAVING COUNT(p.id)=0")])
EOF
```

Expected: kifrs 42(1118 skip), kifrs_interp 19, kifrs_etc 0~1, gaap 35(91·93 skip). 빈 본문 0, 문단 0개 문서 없음.

- [ ] **Step 4: deep-link 표본 대조 (API 문단 vs DB 본문)**

```bash
.venv/bin/python - <<'EOF'
import json, sqlite3, subprocess
conn = sqlite3.connect("data/fino_std.db"); conn.row_factory = sqlite3.Row
for std, num in [(1001, "5"), (1116, "22"), (13, "13.4")]:
    row = conn.execute(
        "SELECT p.body_text FROM paragraphs p JOIN documents d ON d.id=p.document_id "
        "WHERE d.std_num=? AND p.para_num=?", (std, num)).fetchone()
    api = json.loads(subprocess.run(
        ["curl", "-s", "-m", "15", "-A", "Mozilla/5.0",
         f"https://db.kasb.or.kr/api/paragraphs/content/{std}/{num}"],
        capture_output=True, text=True).stdout)
    api_text = api["paraContents"][0]["fullContent"][:40] if api.get("paraContents") else "(없음)"
    print(std, num, "DB:", (row["body_text"][:40] if row else "(없음)"), "| API:", api_text)
EOF
```

Expected: 각 표본에서 DB 본문 앞부분과 API fullContent 앞부분이 일치.

- [ ] **Step 5: 기존 share MD와 기준서 수 대사**

```bash
ls /data_raid/share/회계_KIFRS/*.md | wc -l    # 63 (참고치)
ls /data_raid/share/회계_GAAP기준서/*.md | wc -l  # 32 (참고치)
.venv/bin/python -c "
import sqlite3; c = sqlite3.connect('data/fino_std.db')
print('kifrs+interp:', c.execute(\"SELECT COUNT(*) FROM documents WHERE std_type IN ('kifrs','kifrs_interp')\").fetchone()[0])
print('gaap:', c.execute(\"SELECT COUNT(*) FROM documents WHERE std_type='gaap'\").fetchone()[0])"
```

Expected: kifrs+interp ≈ 61 (share 63에는 구기준·부록 포함), gaap ≈ 35 (share 32 + 60·91중 수집분 — 차이 사유를 눈으로 확인만).

- [ ] **Step 6: export 실행 확인**

```bash
.venv/bin/python -c "
from pathlib import Path
from crawler.fino_std.export_ndjson import export_ndjson
print(export_ndjson(db_path=Path('data/fino_std.db'), out_path=Path('data/export/fino_std.ndjson')))"
```

Expected: 총 문단 수와 동일한 행 수 출력.

- [ ] **Step 7: 전체 테스트 + 핸드오프 문서 갱신 + 커밋**

Run: `.venv/bin/python -m pytest tests/test_fino_std_*.py tests/test_fino_law_*.py tests/test_fino_acct_collector.py -q`
Expected: 전부 PASS

`docs/2026-07-02_fino_corpus_crawler_handoff.md`의 현황 표에서 회계 기준서 K-IFRS/GAAP 두 행을 `✅ 완료`로 바꾸고 수집 수치를 기입.

```bash
git add docs/2026-07-02_fino_corpus_crawler_handoff.md
git commit -m "chore(fino_std): 기준서 수집 완료 — 문서/문단 실측치 기록, NTS 최신화만 남음"
```

- [ ] **Step 8: 메모리 갱신**

`/home/ruci/.claude/projects/-data-raid-ruci-workspace-frwaler/memory/`에 `fino-std-standards-crawler.md` 신규 작성(엔드포인트·시드 규모·skip 목록·실행법) + `MEMORY.md` 인덱스에 한 줄 추가.

---

## Self-Review 결과

- 스펙 커버리지: 시드(§수집 범위)=Task 1, API 클라이언트(§소스)=Task 2, 파서(§수집 흐름)=Task 3, 스키마(§데이터 모델)=Task 4, 수집 루프+skip(§수집 흐름)=Task 5, export(§다운스트림)=Task 6, 검증(§검증·테스트)=Task 7. 갭 없음.
- Open Question(BC/IG deep-link 실효성)은 Task 7 Step 4 표본 대조에서 본문 문단으로 확인 — BC 문단 추가 표본은 수집 후 수동 1건 확인으로 충분(뷰어 라우팅 문제일 뿐 데이터 정합성과 무관).
- 타입/시그니처 일관성: `collect_standards(db_path, types: set[str], std_num, delay)` ↔ Task 5 테스트 호출 일치. `ParagraphRecord` 필드 순서 = models 정의와 db/export 사용처 일치. 픽스처 파일명 `title_1001.json`/`content_1001_1f0730.json` Task 3·5에서 동일.
- 플레이스홀더 없음(모든 코드 스텝에 전체 코드 포함).
