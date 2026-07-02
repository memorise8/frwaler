# 세법 집행기준 크롤러 (fino_law exec_standard, PDF 본문) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** taxlaw.nts.go.kr에서 세법집행기준 15세목(조문형)을 완전 자립(self-contained)으로 재수집한다 — 조문 구조(번호+제목+deep-link)는 action.do로, 본문은 원본 PDF를 `pdftotext`로 추출해 붙여 `fino_law.db`에 `source_kind=exec_standard`로 저장.

**Architecture:** 기존 `crawler/fino_law/`(법령) 확장. 세목당: `ASISTE001MR02`로 정본 조문목록(번호/제목/deep-link) → `ASISTE001MR03`로 최신연도 PDF(fleId) → `downloadFile.do` 다운로드 → `pdftotext -layout` → 조문 본문 분할 → 번호로 매칭 → 기존 `documents`/`articles` 테이블 upsert. **LLM 불필요**(pdftotext 실측 검증, 법인세 396조문 본문 추출 성공).

**Tech Stack:** Python 3.12, curl(subprocess, `--tls-max 1.3`), `pdftotext`(poppler, 설치됨), sqlite3, pytest.

**설계/검증 근거:** 메모리 `fino-law-corpus-collection-endpoints`. 실측(2026-07-02): 15세목 ntstBscId/ntstPlcnBkId 확보, ASISTE001MR02=조문목록(법인세 484행/401번호), downloadFile PDF=230p, pdftotext 파서로 396조문 본문 추출(2-0-1 등 MD와 일치).

---

## 확정 엔드포인트 (spike 2026-07-02)
- 15세목: `common_st.js` `exeBaseStttList` (아래 Task 1에 하드코딩).
- 조문목록: POST `action.do` `actionId=ASISTE001MR02` `{ntstBscId, rgtYr}` → `data.ASISTE001MR02.exeBaseDVOList`, 각 `{ntstTextNm(" 2-0-1  제목"), ntstExrBaseSn}`.
- 최신연도+PDF: `ASISTE001MR03` `{ntstBscId, ntstPlcnBkId, rgtYr:""}` → `exeBaseDVOList`, 각 `{rgtYr, fleId, fleSn, plcnDt}`.
- PDF 다운로드: `https://taxlaw.nts.go.kr/downloadFile.do?fleId={fleId}&fleSn={fleSn}`.
- deep-link: `https://taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId={ntstBscId}`.
- 공통: Referer `https://taxlaw.nts.go.kr/st/USESTE001M.do`, `--tls-max 1.3`.

## File Structure
```
crawler/fino_law/
  sources_exec.py    # EXEC_TARGETS: 15세목 (name, ntst_bsc_id, ntst_plcn_bk_id)
  fetch_exec.py      # action.do(list/year) + downloadFile(PDF) — curl
  pdf_exec.py        # pdftotext + 조문 본문 추출 (extract_bodies)
  parsers_exec.py    # MR02 목록 파싱 + PDF 본문 매칭 → DocumentRecord
  collect.py         # collect_exec() 추가, --exec/--all 활성화
tests/
  test_fino_law_exec.py
  fixtures/exec_법인세_list.json   # ASISTE001MR02 응답(이미 생성됨/재생성)
```

기존 fino_law 재사용: `models.py`(DocumentRecord/ArticleRecord: article_no/article_title/body_text/clause_json/seq/source_url), `db.py`(upsert_document/upsert_article), `collect.py`(--exec 현재 placeholder), `export_{markdown,ndjson}.py`(source_kind 무관).

---

## Task 1: 세목 레지스트리 sources_exec.py

**Files:**
- Create: `crawler/fino_law/sources_exec.py`
- Test: `tests/test_fino_law_exec.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_law_exec.py
from crawler.fino_law.sources_exec import EXEC_TARGETS


def test_exec_targets_15_with_ids() -> None:
    assert len(EXEC_TARGETS) == 15
    법인 = next(t for t in EXEC_TARGETS if t.name == "법인세 집행기준")
    assert 법인.ntst_bsc_id == "100000000000001563"
    assert 법인.ntst_plcn_bk_id == "511100000000000003"
    assert any(t.name == "국세기본법 집행기준" for t in EXEC_TARGETS)
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_fino_law_exec.py -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

```python
# crawler/fino_law/sources_exec.py
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ExecTarget:
    name: str
    ntst_bsc_id: str
    ntst_plcn_bk_id: str


EXEC_TARGETS: Final[tuple[ExecTarget, ...]] = (
    ExecTarget("국세기본법 집행기준", "100000000000001586", "511100000000000001"),
    ExecTarget("국세징수법 집행기준", "100000000000001585", "511100000000000002"),
    ExecTarget("법인세 집행기준", "100000000000001563", "511100000000000003"),
    ExecTarget("국제조세 집행기준", "100000000000000603", "511100000000000004"),
    ExecTarget("종합소득세 집행기준", "100000000000001565", "511100000000000005"),
    ExecTarget("양도소득세 집행기준", "200000000000001565", "511100000000000006"),
    ExecTarget("종합부동산세 집행기준", "100000000000009873", "511100000000000007"),
    ExecTarget("상속증여세 집행기준", "100000000000001561", "511100000000000008"),
    ExecTarget("개별소비세 집행기준", "100000000000001570", "511100000000000009"),
    ExecTarget("인지세 집행기준", "100000000000001568", "511100000000000009"),
    ExecTarget("주세 집행기준", "100000000000001566", "511100000000000009"),
    ExecTarget("주류면허법 집행기준", "100000000000013931", "511100000000000009"),
    ExecTarget("증권거래세 집행기준", "100000000000000621", "511100000000000010"),
    ExecTarget("부가가치세 집행기준", "100000000000001571", "510000000000000448"),
    ExecTarget("조세특례제한법 집행기준", "100000000000001584", "510000000000000823"),
)
```

- [ ] **Step 4: Run test + commit**

Run: `.venv/bin/python -m pytest tests/test_fino_law_exec.py -v` → PASS
```bash
git add crawler/fino_law/sources_exec.py tests/test_fino_law_exec.py
git commit -m "feat(fino_law): 집행기준 15세목 레지스트리"
```

---

## Task 2: pdf_exec.py — PDF 본문 추출 (검증된 파서)

**Files:**
- Create: `crawler/fino_law/pdf_exec.py`
- Test: `tests/test_fino_law_exec.py`

> `extract_bodies(text, page_headers)`는 실측 검증됨(법인세 396조문). 조문 헤더="집행기준 N-N-N 제목"(목차 리더 ·/… 없음, 끝 페이지번호 없음), 페이지 노이즈(세목명·국세청·"- N -") 제거. 단위테스트는 합성 텍스트로(대용량 PDF fixture 불필요).

- [ ] **Step 1: Write the failing test (합성 pdftotext 텍스트)**

```python
# tests/test_fino_law_exec.py 에 추가
from crawler.fino_law.pdf_exec import extract_bodies

_SAMPLE = """법인세 집행기준
                    < 목  차 >
집행기준 2-0-1              【내국법인과 외국법인의 구분】 ··············· 1
집행기준 2-0-2              【비영리법인의 범위】 ··············· 2

- 1 -
법인세 집행기준
집행기준    2-0-1   내국법인과 외국법인의 구분

내국법인과 외국법인의 구분은 본점 또는 주사무소의 소재지를 기준으로 구분한다.
그 관리장소를 기준으로 구분한다.
- 2 -
법인세 집행기준
집행기준    2-0-2   비영리법인의 범위

① 비영리내국법인은 내국법인 중 다음에 해당하는 법인을 말한다.
국세청
"""


def test_extract_bodies_splits_and_strips_noise() -> None:
    bodies = extract_bodies(_SAMPLE, page_headers=("법인세 집행기준", "국세청"))
    assert set(bodies) == {"2-0-1", "2-0-2"}                     # 목차 아닌 본문만
    assert "본점 또는 주사무소" in bodies["2-0-1"]
    assert "법인세 집행기준" not in bodies["2-0-1"]              # 페이지 머리말 제거
    assert "- 1 -" not in bodies["2-0-1"] and "- 2 -" not in bodies["2-0-1"]
    assert bodies["2-0-2"].startswith("① 비영리내국법인")
    assert "국세청" not in bodies["2-0-2"]
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_fino_law_exec.py -k extract_bodies -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement (실측 검증된 코드)**

```python
# crawler/fino_law/pdf_exec.py
from __future__ import annotations

import re
import subprocess
from pathlib import Path

_HEADER = re.compile(r"^집행기준\s+(\d+(?:-\d+)+)\s+(.*\S)")
_PAGE_NO = re.compile(r"^-?\s*\d+\s*-?$")


def pdf_to_text(pdf_path: Path) -> str:
    result = subprocess.run(
        ["pdftotext", "-layout", str(pdf_path), "-"],
        capture_output=True, text=True, timeout=180,
    )
    return result.stdout


def extract_bodies(text: str, page_headers: tuple[str, ...]) -> dict[str, str]:
    """pdftotext 결과 → {집행기준번호: 본문}. 목차/페이지노이즈 제거, 본문 헤더로 분할."""
    bodies: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            if current is not None:
                buf.append("")
            continue
        if s in page_headers or _PAGE_NO.match(s):
            continue  # 페이지 머리말/꼬리말/쪽번호
        m = _HEADER.match(s)
        # 본문 헤더 = 목차 리더(·/…)·끝 페이지번호 없음
        if m and "…" not in s and "·" not in s and not re.search(r"\d+$", s):
            if current is not None:
                bodies[current] = "\n".join(buf).strip()
            current = m.group(1)
            buf = []
            continue
        if current is not None:
            buf.append(line)
    if current is not None:
        bodies[current] = "\n".join(buf).strip()
    return {k: v for k, v in bodies.items() if len(v) > 5}
```

- [ ] **Step 4: Run test + commit**

Run: `.venv/bin/python -m pytest tests/test_fino_law_exec.py -q` → PASS
```bash
git add crawler/fino_law/pdf_exec.py tests/test_fino_law_exec.py
git commit -m "feat(fino_law): 집행기준 PDF 본문 추출 파서(pdftotext, 목차/노이즈 제거)"
```

---

## Task 3: fetch_exec.py — taxlaw action.do + PDF 다운로드

**Files:**
- Create: `crawler/fino_law/fetch_exec.py`

> 네트워크 클라이언트(단위테스트 없음). curl subprocess, `--tls-max 1.3`, 빈 응답 3회 재시도.

- [ ] **Step 1: Implement**

```python
# crawler/fino_law/fetch_exec.py
from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

_API = "https://taxlaw.nts.go.kr/action.do"
_DL = "https://taxlaw.nts.go.kr/downloadFile.do"
_REFERER = "https://taxlaw.nts.go.kr/st/USESTE001M.do"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def _post(action_id: str, param: dict, delay: float) -> str:
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "60", "-X", "POST",
        "-H", "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
        "-H", f"User-Agent: {_UA}", "-H", "Origin: https://taxlaw.nts.go.kr",
        "-H", f"Referer: {_REFERER}", "-H", "X-Requested-With: XMLHttpRequest",
        "--data-urlencode", f"paramData={json.dumps(param, ensure_ascii=False)}",
        "-d", f"actionId={action_id}", _API,
    ]
    for attempt in range(3):
        if delay > 0:
            time.sleep(delay)
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=65).stdout
        if out.strip():
            return out
        time.sleep(1 + attempt)
    raise RuntimeError(f"taxlaw empty response: {action_id}")


def fetch_article_list(ntst_bsc_id: str, rgt_year: str, delay: float = 0.4) -> dict:
    return json.loads(_post("ASISTE001MR02", {"ntstBscId": ntst_bsc_id, "rgtYr": rgt_year}, delay))


def latest_publication(ntst_bsc_id: str, ntst_plcn_bk_id: str, delay: float = 0.4) -> dict | None:
    """ASISTE001MR03 → 최신연도 발간본 {rgtYr, fleId, fleSn}."""
    data = json.loads(_post("ASISTE001MR03",
                            {"ntstBscId": ntst_bsc_id, "ntstPlcnBkId": ntst_plcn_bk_id, "rgtYr": ""}, delay))
    rows = data.get("data", {}).get("ASISTE001MR03", {}).get("exeBaseDVOList", [])
    rows = [r for r in rows if r.get("rgtYr") and r.get("fleId")]
    if not rows:
        return None
    best = max(rows, key=lambda r: str(r.get("rgtYr")))
    return {"rgt_year": str(best["rgtYr"]), "fle_id": str(best["fleId"]), "fle_sn": str(best.get("fleSn", "0"))}


def download_pdf(fle_id: str, fle_sn: str, dest: Path, delay: float = 0.4) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if delay > 0:
        time.sleep(delay)
    subprocess.run(
        ["curl", "-skL", "--tls-max", "1.3", "--max-time", "120",
         "-H", f"User-Agent: {_UA}", "-H", f"Referer: {_REFERER}",
         "-o", str(dest), f"{_DL}?fleId={fle_id}&fleSn={fle_sn}"],
        check=True, timeout=130,
    )
    return dest
```

- [ ] **Step 2: Commit**

```bash
git add crawler/fino_law/fetch_exec.py
git commit -m "feat(fino_law): taxlaw 집행기준 action.do + PDF 다운로드 클라이언트"
```

---

## Task 4: parsers_exec.py — 목록 파싱 + 본문 매칭

**Files:**
- Create: `crawler/fino_law/parsers_exec.py`
- Test: `tests/test_fino_law_exec.py`

> `ntstTextNm`(" 2-0-1  제목")에서 번호/제목 분리, `bodies`({번호:본문})와 번호로 매칭. deep-link = USESTE001M.do?ntstBscId=…#번호.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_law_exec.py 에 추가
import json
from pathlib import Path
from crawler.fino_law.parsers_exec import parse_exec, exec_citation_url


def test_parse_exec_matches_bodies_by_number() -> None:
    data = json.load(open(Path("tests/fixtures/exec_법인세_list.json")))
    bodies = {"2-0-1": "내국법인과 외국법인의 구분은 본점 또는...", "2-0-2": "① 비영리내국법인은..."}
    doc = parse_exec(data, name="법인세 집행기준", ntst_bsc_id="100000000000001563", bodies=bodies)
    assert doc.source_kind == "exec_standard"
    assert doc.source_url == "https://taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId=100000000000001563"
    first = next(a for a in doc.articles if a.article_no == "2-0-1")
    assert "내국법인과 외국법인의 구분" in first.article_title
    assert "본점 또는" in first.body_text
    assert first.source_url.endswith("ntstBscId=100000000000001563#2-0-1")


def test_exec_citation_url() -> None:
    assert exec_citation_url("100000000000001563") == \
        "https://taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId=100000000000001563"
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_fino_law_exec.py -k parse_exec -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement**

```python
# crawler/fino_law/parsers_exec.py
from __future__ import annotations

import re
from typing import Any

from .models import ArticleRecord, DocumentRecord

_NUM = re.compile(r"^\s*(\d+(?:-\d+)+)\s+(.*\S)")


def exec_citation_url(ntst_bsc_id: str) -> str:
    return f"https://taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId={ntst_bsc_id}"


def parse_exec(data: dict, *, name: str, ntst_bsc_id: str, bodies: dict[str, str]) -> DocumentRecord:
    rows: list[dict[str, Any]] = data.get("data", {}).get("ASISTE001MR02", {}).get("exeBaseDVOList", [])
    base = exec_citation_url(ntst_bsc_id)
    seen: set[str] = set()
    articles: list[ArticleRecord] = []
    seq = 0
    for row in rows:
        m = _NUM.match(str(row.get("ntstTextNm", "")))
        if not m:
            continue
        num, title = m.group(1), m.group(2).strip()
        if num in seen:
            continue
        seen.add(num)
        seq += 1
        articles.append(
            ArticleRecord(
                article_no=num,
                article_title=title,
                body_text=bodies.get(num, ""),
                clause_json="[]",
                seq=seq,
                source_url=f"{base}#{num}",
            )
        )
    return DocumentRecord(
        source_kind="exec_standard",
        external_id=ntst_bsc_id,
        title=name,
        category="집행기준",
        org="국세청",
        promulgated_at="",
        effective_at=str(rows[0].get("rgtYr", "")) if rows else "",
        version_code="현행",
        source_url=base,
        articles=tuple(articles),
    )
```

- [ ] **Step 4: Run test + commit**

Run: `.venv/bin/python -m pytest tests/test_fino_law_exec.py -q` → PASS
```bash
git add crawler/fino_law/parsers_exec.py tests/test_fino_law_exec.py
git commit -m "feat(fino_law): 집행기준 목록 파싱 + PDF 본문 번호매칭 + deep-link"
```

---

## Task 5: collect_exec — 세목 순회 오케스트레이션

**Files:**
- Modify: `crawler/fino_law/collect.py`

- [ ] **Step 1: Implement collect_exec + --exec 배선**

```python
# crawler/fino_law/collect.py — import 추가
from pathlib import Path as _Path  # (기존 Path 있으면 생략)
from .fetch_exec import download_pdf, fetch_article_list, latest_publication
from .pdf_exec import extract_bodies, pdf_to_text
from .parsers_exec import parse_exec
from .sources_exec import EXEC_TARGETS

DEFAULT_PDF_DIR = Path("data/fino_law_exec_pdf")


def collect_exec(*, db_path: Path, pdf_dir: Path, delay: float) -> tuple[int, int]:
    docs = 0
    arts = 0
    with connect_db(db_path) as conn:
        init_schema(conn)
        for t in EXEC_TARGETS:
            pub = latest_publication(t.ntst_bsc_id, t.ntst_plcn_bk_id, delay)
            if pub is None:
                print(f"[skip] 발간본 없음: {t.name}", flush=True)
                continue
            listing = fetch_article_list(t.ntst_bsc_id, pub["rgt_year"], delay)
            pdf_path = pdf_dir / f"{t.name}_{pub['rgt_year']}.pdf"
            try:
                download_pdf(pub["fle_id"], pub["fle_sn"], pdf_path, delay)
                bodies = extract_bodies(pdf_to_text(pdf_path), page_headers=(t.name, "국세청"))
            except Exception as exc:  # PDF 실패해도 구조는 저장
                print(f"[warn] PDF 실패 {t.name}: {exc}", flush=True)
                bodies = {}
            doc = parse_exec(listing, name=t.name, ntst_bsc_id=t.ntst_bsc_id, bodies=bodies)
            doc_id = upsert_document(
                conn, source_kind=doc.source_kind, external_id=doc.external_id,
                title=doc.title, category=doc.category, org=doc.org,
                promulgated_at=doc.promulgated_at, effective_at=doc.effective_at,
                version_code=doc.version_code, source_url=doc.source_url,
            )
            filled = 0
            for a in doc.articles:
                upsert_article(
                    conn, document_id=doc_id, article_no=a.article_no,
                    article_title=a.article_title, body_text=a.body_text,
                    clause_json=a.clause_json, source_url=a.source_url, seq=a.seq,
                )
                if a.body_text:
                    filled += 1
            docs += 1
            arts += len(doc.articles)
            print(f"[exec] {t.name}({pub['rgt_year']}): 조문 {len(doc.articles)} 본문 {filled}", flush=True)
    return docs, arts
```
그리고 `build_parser`에 `--pdf-dir` 추가 + `main()` exec 분기 교체:
```python
    _ = parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    # main():
    if args.exec or args.all:
        d, a = collect_exec(db_path=args.db_path, pdf_dir=args.pdf_dir, delay=args.delay_seconds)
        print(f"done exec: documents={d} articles={a}")
```

- [ ] **Step 2: 소규모 실수집 smoke (법인세 1세목, 임시 DB)**

Run:
```bash
.venv/bin/python -c "
from pathlib import Path
from crawler.fino_law.sources_exec import EXEC_TARGETS
import crawler.fino_law.collect as C
# 법인세만 임시로
C.EXEC_TARGETS = tuple(t for t in EXEC_TARGETS if t.name=='법인세 집행기준')
print(C.collect_exec(db_path=Path('/tmp/exec_smoke.db'), pdf_dir=Path('/tmp/exec_pdf'), delay=0.3))
"
```
Expected: `[exec] 법인세 집행기준(2024): 조문 ~401 본문 ~396` 후 `(1, ~401)`.

- [ ] **Step 3: Commit**

```bash
git add crawler/fino_law/collect.py
git commit -m "feat(fino_law): collect_exec 집행기준 수집 배선(구조+PDF본문, --exec/--all)"
```

---

## Task 6: export + 전체 실수집 + 수용기준

- [ ] **Step 1: 전체 테스트**

Run: `.venv/bin/python -m pytest tests/test_fino_law_*.py -q`
Expected: 전부 PASS.

- [ ] **Step 2: 15세목 실수집 (네트워크, PDF 다운로드 — 시간 소요)**

Run: `.venv/bin/python -m crawler.fino_law.collect --exec --delay-seconds 0.4`
Expected: 세목별 `[exec] … 조문 N 본문 M` 로그. 일부 세목 본문 매칭률 낮으면(레이아웃 편차) 경고.

- [ ] **Step 3: 수용 기준**

Run:
```bash
.venv/bin/python - <<'PY'
import sqlite3
c=sqlite3.connect("data/fino_law.db"); c.row_factory=sqlite3.Row
ex=c.execute("SELECT COUNT(*) n FROM documents WHERE source_kind='exec_standard'").fetchone()["n"]
art=c.execute("SELECT COUNT(*) n FROM articles a JOIN documents d ON d.id=a.document_id WHERE d.source_kind='exec_standard'").fetchone()["n"]
body=c.execute("SELECT COUNT(*) n FROM articles a JOIN documents d ON d.id=a.document_id WHERE d.source_kind='exec_standard' AND a.body_text!=''").fetchone()["n"]
nourl=c.execute("SELECT COUNT(*) n FROM articles a JOIN documents d ON d.id=a.document_id WHERE d.source_kind='exec_standard' AND a.source_url=''").fetchone()["n"]
print(f"exec 세목={ex} 조문={art} 본문있음={body}({100*body//max(art,1)}%) url없음={nourl}")
assert ex>=13 and art>1000 and nourl==0 and body >= art*0.8
PY
```
Expected: 세목≈15, 조문 수천, 본문 ≥80%, url없음=0. (세목별 레이아웃으로 본문<100%는 허용; 낮은 세목은 로그로 파악해 Task 2 page_headers 보정.)

- [ ] **Step 4: export 확인 + commit**

Run:
```bash
.venv/bin/python -c "from pathlib import Path; from crawler.fino_law.export_ndjson import export_ndjson; print('ndjson', export_ndjson(db_path=Path('data/fino_law.db'), out_path=Path('data/fino_law.ndjson')))"
```
Expected: 법령+집행기준 조문 합계 출력.
```bash
git commit --allow-empty -m "chore(fino_law): 집행기준 15세목 수집 검증 완료"
```

---

## Self-Review 메모 (작성자)
- 스펙 커버: 세목 enum→T1, PDF파서→T2, fetch/PDF→T3, 목록+본문매칭→T4, 수집→T5, export/검증→T6. 본문=PDF(실측 검증, LLM 없음).
- 타입 일관: `parse_exec`가 기존 `DocumentRecord`/`ArticleRecord` 반환(필드 동일), `upsert_document`/`upsert_article` 시그니처=fino_law db.py와 일치(source_url 포함). `extract_bodies`→`{번호:본문}`→`parse_exec(bodies=)` 키=번호 일관.
- 견고성: PDF 실패 시 구조만 저장(본문 빈값), 세목별 page_headers=세목명. 본문 매칭률<100% 허용(레이아웃 편차 세목은 보정 대상).

## Open Questions
- [ ] 세목별 PDF 레이아웃 편차 → 본문 매칭률 낮은 세목은 Task 6 로그로 식별 후 `extract_bodies` page_headers/헤더정규식 보정.
- [ ] 인지세/주세/주류면허 3세목 ntst_plcn_bk_id 동일(…009) → `latest_publication`(MR03)이 세목 구분되는지 확인, 안 되면 조문목록(MR02, ntstBscId기준)의 연도 사용.
- [ ] 최신본만 수집(현행). 연혁 next level.
