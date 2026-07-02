# 세법 집행기준 크롤러 (fino_law exec_standard) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** taxlaw.nts.go.kr에서 세법집행기준(15세목, 조문형)을 재수집하는 크롤러를 `crawler/fino_law/`에 붙여, `fino_law.db`에 `source_kind=exec_standard`로 저장하고 조문별 citation deep-link를 확보한다.

**Architecture:** 기존 `crawler/fino_law/`(법령, law.go.kr) 확장. 집행기준은 taxlaw `action.do`(curl, `nts_taxlaw.py` 패턴) — `common_st.js`의 15세목 → `ASISTE001MR02`로 조문 목록(번호+제목+ntstExrBaseSn) → per-조문 본문 → 기존 `documents`/`articles` 테이블(source_kind=exec_standard) upsert. 법령과 export/스키마 공유.

**Tech Stack:** Python 3.12, curl(subprocess, `--tls-max 1.3`), sqlite3, pytest. taxlaw는 SSL 이슈로 httpx 대신 curl 사용(nts_taxlaw 검증됨).

**설계 근거/정찰:** 메모리 `fino-law-corpus-collection-endpoints` (2026-07-02 spike 성공 섹션). 기존 fino_law: `models.py`(DocumentRecord/ArticleRecord), `db.py`(upsert_document/upsert_article), `collect.py`(--law/--exec/--all, --exec는 현재 placeholder), `export_{markdown,ndjson}.py`(source_kind 무관).

---

## 확정된 taxlaw 엔드포인트 (spike 2026-07-02)
- 세목(15): `taxlaw.nts.go.kr/js/common/common_st.js` `exeBaseStttList` — `{ntstNm, ntstBscId, ntstPlcnBkId}`.
- 조문 목록: POST `action.do` `actionId=ASISTE001MR02`, paramData `{ntstBscId, rgtYr}` → `exeBaseDVOList` (법인세 2024=484건), 각 `{ntstExrBaseSn, ntstTextNm(" 2-0-1  제목"), srtOrdr}`. 본문(`ntstTextCntn`)은 리스트에 비어있음.
- 연도별 PDF(fallback): `ASISTE001MR03` `{ntstBscId, ntstPlcnBkId, rgtYr}` → fleId/fleSn → `downloadFile.do?fleId=&fleSn=`.
- deep-link: `taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId={ntstBscId}`.

---

## File Structure
```
crawler/fino_law/
  sources_exec.py    # EXEC_TARGETS: 15세목 (ntstNm, ntstBscId, ntstPlcnBkId, category)
  fetch_exec.py      # taxlaw action.do curl 클라이언트 (list/body/pdf)
  parsers_exec.py    # exeBaseDVOList → (집행기준번호, 제목, 본문) + citation url
  collect.py         # collect_exec() 추가, --exec/--all 활성화
tests/
  test_fino_law_exec.py
  fixtures/exec_법인세_list.json   # ASISTE001MR02 응답 (Task 2 저장)
```

---

## Task 1: per-조문 본문 소스 확정 (spike — 게이트)

> MR02는 조문 번호/제목/ntstExrBaseSn만 주고 본문은 비어있음. 본문을 주는 호출을 확정한다. 정적 분석 실패(번들 JS 델리게이트) → 브라우저 DevTools 캡처 or 후보 probe. **본문 소스 확정 후 Task 4 본문 파싱이 열림.** 실패 시 PDF fallback(Task 3의 downloadFile + 변환)로 전환하고 사용자에게 보고.

**Files:**
- Create: `docs/superpowers/specs/_recon-exec-body.md` (발견 기록)
- Create: `tests/fixtures/exec_법인세_body.json` (성공 시)

- [ ] **Step 1: DevTools 캡처 요청 or 후보 probe**

사용자에게: taxlaw.nts.go.kr `/st/USESTE001M.do?ntstBscId=100000000000001563` 접속 → 법인세 집행기준 좌측 트리에서 "2-0-1 내국법인과 외국법인의 구분" 클릭 → **DevTools Network 탭에서 `action.do` 요청의 `actionId`+`paramData` 캡처**.

또는 후보 probe (본문 담긴 응답 찾기):
```bash
.venv/bin/python - <<'PY'
import subprocess, json
def action(aid, param):
    cmd=["curl","-skL","--tls-max","1.3","--max-time","45","-X","POST",
         "-H","Content-Type: application/x-www-form-urlencoded; charset=UTF-8","-H","User-Agent: Mozilla/5.0",
         "-H","Origin: https://taxlaw.nts.go.kr","-H","Referer: https://taxlaw.nts.go.kr/st/USESTE001M.do",
         "-H","X-Requested-With: XMLHttpRequest",
         "--data-urlencode", f"paramData={json.dumps(param, ensure_ascii=False)}",
         "-d", f"actionId={aid}", "https://taxlaw.nts.go.kr/action.do"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=50).stdout
    return out
# 후보 actionId × 파라미터 조합
bsc="100000000000001563"; sn="2024552"
for aid in ["ASISTE001MR01","ASISTE002MR01","ASISTE001MR04","ASISTD001MR99"]:
    for param in ({"ntstBscId":bsc,"ntstExrBaseSn":sn}, {"ntstExrBaseSn":sn}):
        r=action(aid,param)
        hit = "주사무소" in r or "본점" in r
        print(aid, list(param.keys()), "len",len(r), "본문?",hit)
        if hit:
            open("tests/fixtures/exec_법인세_body.json","w").write(r); print("  → 저장"); break
PY
```
Expected: 본문("본점 또는 주사무소" 포함) 담긴 응답 발견 → fixture 저장, actionId/paramData 기록.

- [ ] **Step 2: 발견 기록**

`docs/superpowers/specs/_recon-exec-body.md`에 본문 actionId·paramData·응답 본문 필드명 기록. **판정: 조문형 본문 수집 가능 / 불가(→PDF fallback)**.

- [ ] **Step 3: Commit**

```bash
git add docs/superpowers/specs/_recon-exec-body.md tests/fixtures/exec_법인세_body.json 2>/dev/null || git add docs/superpowers/specs/_recon-exec-body.md
git commit -m "spike(fino_law): 집행기준 per-조문 본문 소스 확정"
```

> **STOP & REVIEW:** 본문 소스를 사용자에게 보고. "가능"이면 Task 2~ 진행(본문 포함), "불가"면 PDF fallback 경로로 계획 조정.

---

## Task 2: 세목 레지스트리 + 조문목록 fixture

**Files:**
- Create: `crawler/fino_law/sources_exec.py`
- Create: `tests/fixtures/exec_법인세_list.json` (네트워크)
- Test: `tests/test_fino_law_exec.py`

- [ ] **Step 1: 조문목록 fixture 저장 (네트워크)**

Run:
```bash
.venv/bin/python - <<'PY'
import subprocess, json
cmd=["curl","-skL","--tls-max","1.3","--max-time","50","-X","POST",
     "-H","Content-Type: application/x-www-form-urlencoded; charset=UTF-8","-H","User-Agent: Mozilla/5.0",
     "-H","Origin: https://taxlaw.nts.go.kr","-H","Referer: https://taxlaw.nts.go.kr/st/USESTE001M.do",
     "-H","X-Requested-With: XMLHttpRequest",
     "--data-urlencode", 'paramData={"ntstBscId":"100000000000001563","rgtYr":"2024"}',
     "-d","actionId=ASISTE001MR02","https://taxlaw.nts.go.kr/action.do"]
open("tests/fixtures/exec_법인세_list.json","w").write(subprocess.run(cmd,capture_output=True,text=True,timeout=55).stdout)
import json
d=json.load(open("tests/fixtures/exec_법인세_list.json"))
print("조문 수:", len(d["data"]["ASISTE001MR02"]["exeBaseDVOList"]))
PY
```
Expected: "조문 수: 484" 내외, fixture 생성.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_fino_law_exec.py
from crawler.fino_law.sources_exec import EXEC_TARGETS


def test_exec_targets_cover_15_tax_categories() -> None:
    names = {t.name for t in EXEC_TARGETS}
    assert "법인세 집행기준" in names
    assert "종합소득세 집행기준" in names
    assert "부가가치세 집행기준" in names
    assert len(EXEC_TARGETS) == 15
    법인 = next(t for t in EXEC_TARGETS if t.name == "법인세 집행기준")
    assert 법인.ntst_bsc_id == "100000000000001563"
    assert 법인.ntst_plcn_bk_id == "511100000000000003"
```

- [ ] **Step 3: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_fino_law_exec.py::test_exec_targets_cover_15_tax_categories -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 4: Implement sources_exec.py**

```python
# crawler/fino_law/sources_exec.py
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ExecTarget:
    name: str            # 예: "법인세 집행기준"
    ntst_bsc_id: str
    ntst_plcn_bk_id: str


# common_st.js exeBaseStttList (2026-07-02 추출)
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

- [ ] **Step 5: Run test + commit**

Run: `.venv/bin/python -m pytest tests/test_fino_law_exec.py -v` → PASS
```bash
git add crawler/fino_law/sources_exec.py tests/fixtures/exec_법인세_list.json tests/test_fino_law_exec.py
git commit -m "feat(fino_law): 집행기준 15세목 레지스트리 + 조문목록 fixture"
```

---

## Task 3: fetch_exec.py — taxlaw action.do 클라이언트

**Files:**
- Create: `crawler/fino_law/fetch_exec.py`

> 네트워크 클라이언트는 단위 테스트 안 함(파서가 fixture로 검증). curl subprocess 사용(nts_taxlaw 패턴, `--tls-max 1.3`, 빈 응답 3회 재시도).

- [ ] **Step 1: Implement**

```python
# crawler/fino_law/fetch_exec.py
from __future__ import annotations

import json
import subprocess
import time

_API = "https://taxlaw.nts.go.kr/action.do"
_REFERER = "https://taxlaw.nts.go.kr/st/USESTE001M.do"
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36"


def _post(action_id: str, param_data: dict, delay: float) -> str:
    body = json.dumps(param_data, ensure_ascii=False)
    cmd = [
        "curl", "-skL", "--tls-max", "1.3", "--max-time", "50", "-X", "POST",
        "-H", "Content-Type: application/x-www-form-urlencoded; charset=UTF-8",
        "-H", f"User-Agent: {_UA}",
        "-H", "Origin: https://taxlaw.nts.go.kr",
        "-H", f"Referer: {_REFERER}",
        "-H", "X-Requested-With: XMLHttpRequest",
        "--data-urlencode", f"paramData={body}",
        "-d", f"actionId={action_id}", _API,
    ]
    for attempt in range(3):
        if delay > 0:
            time.sleep(delay)
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=55).stdout
        if out.strip():
            return out
        time.sleep(1 + attempt)
    raise RuntimeError(f"taxlaw empty response: {action_id}")


def fetch_article_list(ntst_bsc_id: str, rgt_year: str, delay: float = 0.4) -> dict:
    return json.loads(_post("ASISTE001MR02", {"ntstBscId": ntst_bsc_id, "rgtYr": rgt_year}, delay))


def latest_year(ntst_bsc_id: str, ntst_plcn_bk_id: str, delay: float = 0.4) -> str:
    """ASISTE001MR03 발간본 목록에서 최신 rgtYr."""
    data = json.loads(_post("ASISTE001MR03",
                            {"ntstBscId": ntst_bsc_id, "ntstPlcnBkId": ntst_plcn_bk_id, "rgtYr": ""}, delay))
    rows = data.get("data", {}).get("ASISTE001MR03", {}).get("exeBaseDVOList", [])
    years = sorted((str(r.get("rgtYr", "")) for r in rows if r.get("rgtYr")), reverse=True)
    return years[0] if years else ""
```

> Task 1이 본문 action을 확정했다면, 그 호출을 `fetch_article_body(...)`로 여기 추가한다(Task 1 발견 기반, actionId/paramData 대입). 본문이 PDF fallback이면 `download_pdf(fle_id, fle_sn)`를 추가.

- [ ] **Step 2: Commit**

```bash
git add crawler/fino_law/fetch_exec.py
git commit -m "feat(fino_law): taxlaw 집행기준 action.do curl 클라이언트"
```

---

## Task 4: parsers_exec.py — 조문 파싱 + citation

**Files:**
- Create: `crawler/fino_law/parsers_exec.py`
- Test: `tests/test_fino_law_exec.py`

> `ntstTextNm`(" 2-0-1  내국법인과 외국법인의 구분")에서 **집행기준번호("2-0-1")와 제목**을 분리. 본문은 Task 1 확정 소스에서 채운다(미확정이면 빈 문자열로 두고 Task 1 재개 시 채움 — 번호/제목/deep-link는 확정).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_law_exec.py 에 추가
import json
from pathlib import Path
from crawler.fino_law.parsers_exec import parse_exec_list, exec_citation_url


def test_parse_exec_list_splits_number_and_title() -> None:
    data = json.load(open(Path("tests/fixtures/exec_법인세_list.json")))
    doc = parse_exec_list(data, name="법인세 집행기준", ntst_bsc_id="100000000000001563")
    assert doc.source_kind == "exec_standard"
    assert doc.source_url == "https://taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId=100000000000001563"
    assert len(doc.articles) > 100
    first = doc.articles[0]
    assert first.article_no == "2-0-1"
    assert "내국법인과 외국법인의 구분" in first.article_title
    assert first.source_url.endswith("ntstBscId=100000000000001563#2-0-1")


def test_exec_citation_url() -> None:
    assert exec_citation_url("100000000000001563") == \
        "https://taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId=100000000000001563"
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_fino_law_exec.py -k parse_exec -v`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement parsers_exec.py**

```python
# crawler/fino_law/parsers_exec.py
from __future__ import annotations

import re
from typing import Any

from .models import ArticleRecord, DocumentRecord

_NUM_RE = re.compile(r"^\s*([\d]+[-\d]*-[\d]+)\s+(.*)$")


def exec_citation_url(ntst_bsc_id: str) -> str:
    return f"https://taxlaw.nts.go.kr/st/USESTE001M.do?ntstBscId={ntst_bsc_id}"


def _split_num_title(ntst_text_nm: str) -> tuple[str, str]:
    m = _NUM_RE.match(ntst_text_nm or "")
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return "", (ntst_text_nm or "").strip()


def parse_exec_list(data: dict, *, name: str, ntst_bsc_id: str,
                    bodies: dict[str, str] | None = None) -> DocumentRecord:
    """ASISTE001MR02 응답 → 집행기준 DocumentRecord(+articles).

    bodies: {ntstExrBaseSn: 본문} 매핑(Task 1 확정 시). None이면 본문 빈 문자열.
    """
    rows: list[dict[str, Any]] = data.get("data", {}).get("ASISTE001MR02", {}).get("exeBaseDVOList", [])
    base_url = exec_citation_url(ntst_bsc_id)
    bodies = bodies or {}
    articles: list[ArticleRecord] = []
    seq = 0
    for row in rows:
        article_no, title = _split_num_title(str(row.get("ntstTextNm", "")))
        if not article_no:
            continue
        seq += 1
        sn = str(row.get("ntstExrBaseSn", ""))
        articles.append(
            ArticleRecord(
                article_no=article_no,
                article_title=title,
                body_text=bodies.get(sn, ""),
                clause_json="[]",
                seq=seq,
                source_url=f"{base_url}#{article_no}",
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
        source_url=base_url,
        articles=tuple(articles),
    )
```

- [ ] **Step 4: Run test + commit**

Run: `.venv/bin/python -m pytest tests/test_fino_law_exec.py -q` → PASS
```bash
git add crawler/fino_law/parsers_exec.py tests/test_fino_law_exec.py
git commit -m "feat(fino_law): 집행기준 조문 파서(번호/제목 분리 + citation deep-link)"
```

---

## Task 5: collect_exec — 세목 순회 → 저장

**Files:**
- Modify: `crawler/fino_law/collect.py` (collect_exec 추가, --exec/--all 활성화)

> Task 1이 본문 소스를 확정했으면 `bodies` 매핑을 채워 넘긴다. 미확정이면 본문 없이 번호/제목/deep-link만 저장(후속 보강). 저장은 기존 `upsert_document`/`upsert_article` 재사용(source_kind=exec_standard).

- [ ] **Step 1: Implement collect_exec + 배선**

```python
# crawler/fino_law/collect.py — import 추가
from .fetch_exec import fetch_article_list, latest_year
from .parsers_exec import parse_exec_list
from .sources_exec import EXEC_TARGETS


def collect_exec(*, db_path: Path, delay: float) -> tuple[int, int]:
    docs = 0
    arts = 0
    with connect_db(db_path) as conn:
        init_schema(conn)
        for target in EXEC_TARGETS:
            year = latest_year(target.ntst_bsc_id, target.ntst_plcn_bk_id, delay)
            if not year:
                print(f"[skip] 발간연도 없음: {target.name}", flush=True)
                continue
            data = fetch_article_list(target.ntst_bsc_id, year, delay)
            doc = parse_exec_list(data, name=target.name, ntst_bsc_id=target.ntst_bsc_id)
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
            print(f"[exec] {target.name}({year}): 조문 {len(doc.articles)}", flush=True)
    return docs, arts
```
그리고 `main()`의 exec 분기 교체:
```python
    if args.exec or args.all:
        d, a = collect_exec(db_path=args.db_path, delay=args.delay_seconds)
        print(f"done exec: documents={d} articles={a}")
```

- [ ] **Step 2: 소규모 실수집 smoke (법인세만, 임시 DB)**

Run: `.venv/bin/python -c "from pathlib import Path; from crawler.fino_law.collect import collect_exec; print(collect_exec(db_path=Path('/tmp/exec_smoke.db'), delay=0.3))"` (Ctrl-C로 몇 세목 확인)
Expected: `[exec] 법인세 집행기준(2024): 조문 484` 형태.

- [ ] **Step 3: Commit**

```bash
git add crawler/fino_law/collect.py
git commit -m "feat(fino_law): collect_exec 집행기준 수집 배선(--exec/--all)"
```

---

## Task 6: export + 실수집 검증

- [ ] **Step 1: 전체 테스트**

Run: `.venv/bin/python -m pytest tests/test_fino_law_*.py -q`
Expected: 전부 PASS.

- [ ] **Step 2: 집행기준 실수집**

Run: `.venv/bin/python -m crawler.fino_law.collect --exec --delay-seconds 0.4`
Expected: 15세목(일부 skip 허용) 조문 수집 로그.

- [ ] **Step 3: 수용 기준**

Run:
```bash
.venv/bin/python - <<'PY'
import sqlite3
c=sqlite3.connect("data/fino_law.db"); c.row_factory=sqlite3.Row
ex=c.execute("SELECT COUNT(*) n FROM documents WHERE source_kind='exec_standard'").fetchone()["n"]
art=c.execute("SELECT COUNT(*) n FROM articles a JOIN documents d ON d.id=a.document_id WHERE d.source_kind='exec_standard'").fetchone()["n"]
nourl=c.execute("SELECT COUNT(*) n FROM articles a JOIN documents d ON d.id=a.document_id WHERE d.source_kind='exec_standard' AND a.source_url=''").fetchone()["n"]
print(f"exec 문서={ex} 조문={art} url없음={nourl}")
assert ex>0 and art>0 and nourl==0
PY
```
Expected: 문서≈15, 조문 수천, `url없음=0`.

- [ ] **Step 4: export (법령+집행기준 한 세트) 확인**

Run:
```bash
.venv/bin/python -c "from pathlib import Path; from crawler.fino_law.export_ndjson import export_ndjson; print('ndjson', export_ndjson(db_path=Path('data/fino_law.db'), out_path=Path('data/fino_law.ndjson')))"
```
Expected: 법령+집행기준 조문 합계 라인 수 출력. (`data/`는 gitignore.)

- [ ] **Step 5: Commit**

```bash
git commit --allow-empty -m "chore(fino_law): 집행기준 수집 검증 완료"
```

---

## Self-Review 메모 (작성자)
- 스펙 커버: 세목 enum→T2, 조문목록/번호·제목/deep-link→T4, 수집→T5, export/검증→T6. **본문**은 Task 1 게이트(미확정 위험 명시, PDF fallback).
- 타입 일관: `parse_exec_list`가 반환하는 `DocumentRecord`/`ArticleRecord`는 기존 fino_law `models.py` 재사용(필드 동일). `upsert_document`/`upsert_article` 시그니처 = fino_law db.py와 일치(source_url 포함).
- YAGNI: 본문 미확정 시에도 번호/제목/deep-link는 확보 → citation 목표 부분 달성. 본문은 Task 1 결과로 보강.

## Open Questions
- [ ] per-조문 본문 action (Task 1). 미확정 시 PDF fallback(downloadFile + 변환)로 본문 확보.
- [ ] 인지세/주세/주류면허 3세목의 ntstPlcnBkId가 동일(…009) — MR03 발간연도 조회 시 구분되는지(구현 중 확인, 안 되면 ntstBscId 기준).
- [ ] rgtYr는 최신본만(현행) — 연혁은 next level.
