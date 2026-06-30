# 회계 질의회신(fino_acct) deep-link 재수집 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** KASB·FSS 질의회신 크롤러가 목록 대신 **개별 질의회신 1글을 문서 1건으로** 본문·고유식별자·deep-link와 함께 수집하도록 `crawler/fino_acct/`를 수정한다.

**Architecture:** 목록 페이지는 상세 링크 추출용으로만 쓰고 저장하지 않는다. FSS는 목록의 `view.do?nttId=N`(GET)으로, KASB는 행의 `fn_Detail('seq','ctgCd')` → POST `View{ctgCd}.do`로 상세 진입. 기존 SQLite 스키마/upsert 재사용, 값만 per-문서로 올바르게 채움.

**Tech Stack:** Python 3.12, requests, BeautifulSoup4, sqlite3, pytest.

**Spec:** `docs/superpowers/specs/2026-06-30-fino-acct-qna-detail-recrawl-design.md`

---

## File Structure (수정 대상)

```
crawler/fino_acct/
  models.py        # ExtractedLinks 에 kasb_items 필드 추가
  parsers.py       # FSS 상세링크 추출 + KASB 상세(seq/ctgCd) 추출
  target_pages.py  # kasb_detail_request() (POST View{ctgCd}.do) 추가
  collect.py       # 목록 미저장 + 상세 단위 저장 (store_self 분기)
tests/
  test_fino_acct_collector.py   # 기존 파일에 테스트 추가
  fixtures/fino_acct/           # 정찰 HTML (신규)
    fss_list.html, fss_detail.html, kasb_list.html, kasb_detail.html
scripts/
  fino_acct_cleanup.py          # DB 백업 + priority 1~6 삭제 (신규)
```

---

## Task 1: 정찰 HTML fixture 저장 (네트워크)

**Files:**
- Create: `tests/fixtures/fino_acct/{fss_list,fss_detail,kasb_list,kasb_detail}.html`

- [ ] **Step 1: 실제 HTML을 fixture로 저장**

Run:
```bash
cd /data_raid/ruci_workspace/frwaler
mkdir -p tests/fixtures/fino_acct
curl -skL --max-time 25 "https://www.fss.or.kr/fss/bbs/B0000132/list.do?menuNo=200442" \
  -H "User-Agent: Mozilla/5.0" -o tests/fixtures/fino_acct/fss_list.html
curl -skL --max-time 25 "https://www.fss.or.kr/fss/bbs/B0000132/view.do?nttId=133043&menuNo=200442" \
  -H "User-Agent: Mozilla/5.0" -o tests/fixtures/fino_acct/fss_detail.html
curl -skL --max-time 25 -X POST "https://www.kasb.or.kr/front/board/allReplySummaryList.do" \
  -H "User-Agent: Mozilla/5.0" -H "Content-Type: application/x-www-form-urlencoded" \
  --data "siteCd=002000000000000&replySummary=Y&page=1" -o tests/fixtures/fino_acct/kasb_list.html
curl -skL --max-time 25 -X POST "https://www.kasb.or.kr/front/board/View016009.do" \
  -H "User-Agent: Mozilla/5.0" -H "Content-Type: application/x-www-form-urlencoded" \
  -H "Referer: https://www.kasb.or.kr/front/board/allReplySummaryList.do" \
  --data "seq=40533&ctgCd=016009&siteCd=002000000000000" -o tests/fixtures/fino_acct/kasb_detail.html
ls -l tests/fixtures/fino_acct/
```
Expected: 4개 파일, 각 수십~수백 KB.

- [ ] **Step 2: 핵심 패턴 존재 확인**

Run:
```bash
grep -c "view.do?nttId=" tests/fixtures/fino_acct/fss_list.html
grep -c "fn_Detail(" tests/fixtures/fino_acct/kasb_list.html
```
Expected: 둘 다 양수.

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/fino_acct/
git commit -m "test(fino_acct): FSS/KASB 목록·상세 정찰 HTML fixture"
```

---

## Task 2: FSS 상세링크 추출 + ExtractedLinks.kasb_items 필드

**Files:**
- Modify: `crawler/fino_acct/models.py` (ExtractedLinks)
- Modify: `crawler/fino_acct/parsers.py` (FSS detail 추출)
- Test: `tests/test_fino_acct_collector.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_acct_collector.py 에 추가
from pathlib import Path as _Path

from crawler.fino_acct.parsers import extract_fss_details


def test_extract_fss_details_picks_view_do_nttid_links() -> None:
    html = _Path("tests/fixtures/fino_acct/fss_list.html").read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    details = extract_fss_details("https://www.fss.or.kr/fss/bbs/B0000132/list.do?menuNo=200442", soup)
    # 절대 URL, nttId 포함, 중복 제거
    assert details
    assert all("view.do?nttId=" in u for u in details)
    assert all(u.startswith("https://www.fss.or.kr") for u in details)
    assert len(details) == len(set(details))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py::test_extract_fss_details_picks_view_do_nttid_links -v`
Expected: FAIL with `ImportError: cannot import name 'extract_fss_details'`

- [ ] **Step 3: Write minimal implementation**

models.py — `ExtractedLinks`에 `kasb_items` 추가:
```python
# crawler/fino_acct/models.py 의 ExtractedLinks 를 아래로 교체
@dataclass(frozen=True, slots=True)
class ExtractedLinks:
    details: tuple[str, ...]
    attachments: tuple[AttachmentLink, ...]
    kasb_items: tuple[tuple[str, str], ...] = ()
```

parsers.py — FSS 상세 추출 함수 추가 (파일 상단 import에 `re`, `urljoin` 이미 있음):
```python
# crawler/fino_acct/parsers.py 에 추가
FSS_DETAIL_RE: Final[re.Pattern[str]] = re.compile(r"view\.do\?[^\"'>]*\bnttId=\d+")


def extract_fss_details(base_url: str, soup: BeautifulSoup) -> tuple[str, ...]:
    out: list[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href", ""))
        if FSS_DETAIL_RE.search(href):
            out.append(urljoin(base_url, href))
    return tuple(dict.fromkeys(out))


def _is_fss_board(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.netloc.endswith("fss.or.kr") and "/fss/bbs/" in parsed.path
```

parsers.py — `extract_links_for_target`가 FSS 게시판을 `extract_fss_details`로 라우팅하도록 수정 (KASB·FSC 분기는 유지). 파일 상단 import에 `urlparse` 추가(`from urllib.parse import ... urlparse`는 이미 있음):
```python
# crawler/fino_acct/parsers.py 의 extract_links_for_target 시작부에 FSS 분기 추가
def extract_links_for_target(target: Target, base_url: str, soup: BeautifulSoup) -> ExtractedLinks:
    if target.priority == 1:
        return _extract_kasb_list_links(base_url, soup)
    if _is_fss_board(target.url):
        return ExtractedLinks(
            details=extract_fss_details(base_url, soup),
            attachments=extract_links(base_url, soup).attachments,
        )
    if target.priority != 8:
        return extract_links(base_url, soup)
    # (이하 기존 priority==8 FSC 로직 그대로 유지)
```

- [ ] **Step 4: Write a second failing→passing test (FSS routing)**

```python
# tests/test_fino_acct_collector.py 에 추가
def test_extract_links_for_target_routes_fss_board_to_view_details() -> None:
    html = _Path("tests/fixtures/fino_acct/fss_list.html").read_text(encoding="utf-8")
    links = extract_links_for_target(
        TARGETS[1], "https://www.fss.or.kr/fss/bbs/B0000132/list.do?menuNo=200442",
        BeautifulSoup(html, "html.parser"),
    )
    assert links.details
    assert all("view.do?nttId=" in u for u in links.details)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py -v`
Expected: PASS (FSS 직접 추출 + 라우팅 테스트 + 기존 전부)

- [ ] **Step 6: Commit**

```bash
git add crawler/fino_acct/models.py crawler/fino_acct/parsers.py tests/test_fino_acct_collector.py
git commit -m "feat(fino_acct): FSS view.do?nttId 상세링크 추출 + 라우팅 + kasb_items 필드"
```

---

## Task 3: KASB 상세 추출(seq/ctgCd) + POST 상세 요청 빌더

**Files:**
- Modify: `crawler/fino_acct/parsers.py` (KASB fn_Detail 파싱, `_extract_kasb_list_links` 확장)
- Modify: `crawler/fino_acct/target_pages.py` (`kasb_detail_request`)
- Test: `tests/test_fino_acct_collector.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_acct_collector.py 에 추가
from crawler.fino_acct.target_pages import kasb_detail_request


def test_extract_kasb_list_returns_seq_ctgcd_items() -> None:
    html = _Path("tests/fixtures/fino_acct/kasb_list.html").read_text(encoding="utf-8")
    links = extract_links_for_target(
        TARGETS[0], "https://www.kasb.or.kr/front/board/allReplySummaryList.do",
        BeautifulSoup(html, "html.parser"),
    )
    assert ("40533", "016009") in links.kasb_items
    assert len(links.kasb_items) == len(set(links.kasb_items))


def test_kasb_detail_request_builds_post_view_url() -> None:
    req = kasb_detail_request("40533", "016009")
    assert req.url == "https://www.kasb.or.kr/front/board/View016009.do"
    assert req.method == "POST"
    assert req.data == {"seq": "40533", "ctgCd": "016009", "siteCd": "002000000000000"}
    assert req.external_id == "016009-40533"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py::test_extract_kasb_list_returns_seq_ctgcd_items tests/test_fino_acct_collector.py::test_kasb_detail_request_builds_post_view_url -v`
Expected: FAIL (`kasb_items` 비어있음 / `kasb_detail_request` 없음)

- [ ] **Step 3: Write minimal implementation**

parsers.py — KASB 목록에서 fn_Detail 파싱 (`_extract_kasb_list_links` 교체, 첨부 추출은 유지):
```python
# crawler/fino_acct/parsers.py 상단 정규식 추가
KASB_FN_DETAIL_RE: Final[re.Pattern[str]] = re.compile(
    r"fn_Detail\('([^']+)'\s*,\s*'([^']+)'\)"
)


# crawler/fino_acct/parsers.py 의 _extract_kasb_list_links 를 아래로 교체
def _extract_kasb_list_links(base_url: str, soup: BeautifulSoup) -> ExtractedLinks:
    attachments: list[AttachmentLink] = []
    items: list[tuple[str, str]] = []
    for row in soup.select("tbody tr"):
        row_soup = BeautifulSoup(str(row), "html.parser")
        row_links = extract_links(base_url, row_soup)
        attachments.extend(row_links.attachments)
        for anchor in row_soup.find_all("a"):
            match = KASB_FN_DETAIL_RE.search(str(anchor.get("onclick", "")))
            if match is not None:
                items.append((match.group(1), match.group(2)))
    return ExtractedLinks(
        details=(),
        attachments=tuple(_dedupe_attachments(attachments)),
        kasb_items=tuple(dict.fromkeys(items)),
    )
```

target_pages.py — KASB 상세 POST 요청 빌더:
```python
# crawler/fino_acct/target_pages.py 에 추가
def kasb_detail_request(seq: str, ctg_cd: str, site_cd: str = "002000000000000") -> PageRequest:
    return PageRequest(
        url=f"https://www.kasb.or.kr/front/board/View{ctg_cd}.do",
        external_id=f"{ctg_cd}-{seq}",
        method="POST",
        data={"seq": seq, "ctgCd": ctg_cd, "siteCd": site_cd},
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py -v`
Expected: PASS (기존 KASB 첨부 테스트 포함 전부 통과)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_acct/parsers.py crawler/fino_acct/target_pages.py tests/test_fino_acct_collector.py
git commit -m "feat(fino_acct): KASB fn_Detail(seq,ctgCd) 추출 + View{ctgCd}.do POST 상세요청"
```

---

## Task 4: collect.py — 목록 미저장 + 상세 단위 저장

**Files:**
- Modify: `crawler/fino_acct/collect.py`
- Test: `tests/test_fino_acct_collector.py` (FSS 상세 저장 통합 테스트, 네트워크 없이 monkeypatch)

> 핵심 변경: `collect_page`에 `store_self` 파라미터 추가. LIST 페이지는 `store_self=False`(저장 안 함, 상세만 추출·진행), 상세 페이지는 `store_self=True`. FSS 상세 external_id=nttId, KASB 상세 external_id=`{ctgCd}-{seq}`(kasb_detail_request가 부여).

- [ ] **Step 1: Write the failing test (네트워크 없는 통합)**

```python
# tests/test_fino_acct_collector.py 에 추가
from crawler.fino_acct import collect as collect_mod
from crawler.fino_acct.db import connect_db, init_schema
from crawler.fino_acct.models import FetchResult
from crawler.fino_acct.target_pages import PageRequest


def test_collect_list_stores_details_not_list_page(tmp_path, monkeypatch) -> None:
    list_html = (
        '<a href="/fss/bbs/B0000132/view.do?nttId=111&menuNo=200442">질의응답 A</a>'
        '<a href="/fss/bbs/B0000132/view.do?nttId=222&menuNo=200442">질의응답 B</a>'
    )
    detail_html = "<h2>제목</h2><div>질의: ... 회신: 내용</div>"

    def fake_fetch(session, request: PageRequest, delay):
        body = list_html if "list.do" in request.url else detail_html
        return FetchResult(url=request.url, status_code=200, content_type="text/html", content=body.encode())

    monkeypatch.setattr(collect_mod, "fetch_page_request", fake_fetch)

    db_path = tmp_path / "acct.db"
    with connect_db(db_path) as conn:
        init_schema(conn)
        docs, _ = collect_mod.collect_target(
            conn=conn, session=None, target=TARGETS[1],  # FSS B0000132
            download_dir=tmp_path / "dl", max_pages=1, delay_seconds=0, download=False,
        )
        rows = conn.execute("SELECT external_id, detail_url FROM acct_documents ORDER BY external_id").fetchall()

    eids = [r["external_id"] for r in rows]
    assert "111" in eids and "222" in eids           # 상세가 문서로 저장됨
    assert all("list.do" not in r["detail_url"] for r in rows)  # 목록 페이지는 저장 안 됨
    assert all("view.do?nttId=" in r["detail_url"] for r in rows)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py::test_collect_list_stores_details_not_list_page -v`
Expected: FAIL (현재는 목록 페이지가 저장되고 상세 미진입)

- [ ] **Step 3: Write minimal implementation**

`collect.py`에서 `direct_page_request`, `kasb_detail_request`, `page_request_for_target` import 확인 후, `collect_target`/`collect_page` 교체:

```python
# crawler/fino_acct/collect.py — import 에 추가
from urllib.parse import parse_qs, urlparse
from .target_pages import PageRequest, direct_page_request, kasb_detail_request, page_request_for_target


def _fss_detail_external_id(url: str) -> str:
    qs = parse_qs(urlparse(url).query)
    ntt = qs.get("nttId", [])
    return ntt[0] if ntt else url


# collect_target 의 LIST 분기를 아래로 교체
        case TargetKind.LIST:
            documents = 0
            attachments = 0
            for page in range(1, max_pages + 1):
                docs, files = collect_page(
                    conn=conn, session=session, target=target,
                    request=page_request_for_target(target, page),
                    download_dir=download_dir, delay_seconds=delay_seconds,
                    download=download, store_self=False, follow_details=True,
                )
                documents += docs
                attachments += files
            return documents, attachments


# DETAIL/META 분기는 store_self=True, follow_details=False 로 호출
        case TargetKind.DETAIL | TargetKind.META:
            return collect_page(
                conn=conn, session=session, target=target,
                request=direct_page_request(target.url, target.url),
                download_dir=download_dir, delay_seconds=delay_seconds,
                download=download, store_self=True, follow_details=False,
            )
```

```python
# crawler/fino_acct/collect.py — collect_page 전체 교체
def collect_page(
    *,
    conn: sqlite3.Connection,
    session: requests.Session,
    target: Target,
    request: PageRequest,
    download_dir: Path,
    delay_seconds: float,
    download: bool,
    store_self: bool,
    follow_details: bool,
) -> tuple[int, int]:
    result = fetch_page_request(session, request, delay_seconds)
    if result.status_code >= 400:
        return 0, 0
    soup = BeautifulSoup(result.content, "html.parser")
    links = extract_links_for_target(target, result.url, soup)
    documents = 0
    attachment_count = 0
    if store_self:
        title = page_title(soup, target.target_name)
        document_id = upsert_document(
            conn,
            source_priority=target.priority,
            agency=target.agency,
            target_name=target.target_name,
            source_url=target.url,
            source_type=target.source_type,
            source_subtype=target.source_subtype,
            index_name=target.index_name,
            external_id=request.external_id,
            title=title,
            detail_url=result.url,
            published_date="",
            body_text=page_body(soup),
        )
        documents = 1
        attachment_count = store_attachments(
            conn=conn, session=session, document_id=document_id,
            links=links.attachments, download_dir=download_dir / f"{target.priority:02d}",
            prefix=str(document_id), delay_seconds=delay_seconds, download=download,
        )
    if follow_details:
        for detail_url in links.details:
            req = direct_page_request(detail_url, _fss_detail_external_id(detail_url))
            d, f = collect_page(
                conn=conn, session=session, target=target, request=req,
                download_dir=download_dir, delay_seconds=delay_seconds,
                download=download, store_self=True, follow_details=False,
            )
            documents += d
            attachment_count += f
        for seq, ctg in links.kasb_items:
            d, f = collect_page(
                conn=conn, session=session, target=target,
                request=kasb_detail_request(seq, ctg),
                download_dir=download_dir, delay_seconds=delay_seconds,
                download=download, store_self=True, follow_details=False,
            )
            documents += d
            attachment_count += f
    return documents, attachment_count
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py -v`
Expected: PASS (전체)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_acct/collect.py tests/test_fino_acct_collector.py
git commit -m "feat(fino_acct): 목록 미저장 + 상세 단위 저장(store_self), FSS/KASB 상세 진입"
```

---

## Task 5: DB 정리 스크립트 (백업 + priority 1~6 삭제)

**Files:**
- Create: `scripts/fino_acct_cleanup.py`

- [ ] **Step 1: Write implementation**

```python
# scripts/fino_acct_cleanup.py
"""백업 후 회계 질의회신 재크롤 대상(priority 1~6) 행 삭제."""
from __future__ import annotations

import argparse
import shutil
import sqlite3
from pathlib import Path


def cleanup(db_path: Path) -> tuple[int, int]:
    backup = db_path.with_suffix(db_path.suffix + ".bak")
    shutil.copy2(db_path, backup)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    before = conn.execute("SELECT COUNT(*) FROM acct_documents").fetchone()[0]
    conn.execute("DELETE FROM acct_documents WHERE source_priority BETWEEN 1 AND 6")
    conn.commit()
    after = conn.execute("SELECT COUNT(*) FROM acct_documents").fetchone()[0]
    conn.close()
    print(f"backup={backup} deleted={before - after} remaining={after}")
    return before, after


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    _ = p.add_argument("--db-path", type=Path, default=Path("data/fino_acct.db"))
    cleanup(p.parse_args().db_path)
```

- [ ] **Step 2: 동작 확인 (실 DB는 Task 6에서 실행)**

Run (임시 DB로 smoke):
```bash
.venv/bin/python - <<'PY'
import sqlite3, tempfile, os
from pathlib import Path
from crawler.fino_acct.db import connect_db, init_schema, upsert_document
d = Path(tempfile.mkdtemp()) / "t.db"
with connect_db(d) as c:
    init_schema(c)
    for p in (1, 5, 8):
        upsert_document(c, source_priority=p, agency="a", target_name="t", source_url="u",
            source_type="qna", source_subtype="s", index_name="i", external_id=f"e{p}",
            title="t", detail_url="u", published_date="", body_text="b")
import scripts.fino_acct_cleanup as cl
cl.cleanup(d)
import sqlite3
left = [r[0] for r in sqlite3.connect(d).execute("SELECT source_priority FROM acct_documents")]
print("remaining priorities:", left)
assert left == [8]
PY
```
Expected: `remaining priorities: [8]` (1·5 삭제, 8 보존).

- [ ] **Step 3: Commit**

```bash
git add scripts/fino_acct_cleanup.py
git commit -m "feat(fino_acct): cleanup 스크립트(백업 + priority 1~6 삭제)"
```

---

## Task 6: 재크롤 + 수용기준 검증

- [ ] **Step 1: 전체 테스트**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py -q`
Expected: 전부 PASS.

- [ ] **Step 2: DB 백업 + priority 1~6 삭제**

Run: `.venv/bin/python -m scripts.fino_acct_cleanup --db-path data/fino_acct.db`
Expected: `backup=data/fino_acct.db.bak deleted=<약 1302> remaining=<나머지>` (FSC 등 보존).

- [ ] **Step 3: KASB(1) + FSS(2~6) 재크롤**

Run: `.venv/bin/python -m crawler.fino_acct.collect --priorities 1,2,3,4,5,6 --max-pages 50 --no-download --delay-seconds 0.5`
Expected: 각 priority 상세 진입 로그. (시간 소요 — `--max-pages`로 범위 조절, 첫 검증은 작게)

- [ ] **Step 4: 수용 기준 검증**

Run:
```bash
.venv/bin/python - <<'PY'
import sqlite3, hashlib
c = sqlite3.connect("data/fino_acct.db"); c.row_factory = sqlite3.Row
for p in (1, 2, 3):
    rows = c.execute("SELECT body_text, detail_url, external_id FROM acct_documents WHERE source_priority=?", (p,)).fetchall()
    uniq = len({hashlib.md5((r["body_text"] or "").encode()).hexdigest() for r in rows})
    list_url = sum(1 for r in rows if "list.do" in r["detail_url"])
    print(f"p{p}: docs={len(rows)} body_uniq={uniq} detail_url_is_list={list_url}")
    assert len(rows) == 0 or (uniq > 1 and list_url == 0), f"p{p} 수용기준 미달"
print("수용기준 통과")
PY
```
Expected: KASB(p1) `body_uniq`가 1이 아니라 다수, `detail_url_is_list=0`. `수용기준 통과` 출력.

- [ ] **Step 5: 멱등 확인 + Commit**

Run (재실행 후 row 수 불변 확인): `--priorities 1` 한 번 더 실행 → docs 수 동일.
```bash
git commit --allow-empty -m "chore(fino_acct): QnA 재크롤 검증 완료 (KASB 본문 고유 다수, FSS 상세 진입)"
```

---

## Self-Review 메모 (작성자)
- Spec §4.1(상세 저장)→Task4, §4.2(정리)→Task5/6, §4.3(코드 단위)→Task2/3/4, §7(테스트/수용)→Task2/3/4/6. 커버 완료.
- 타입 일관: `kasb_items: tuple[tuple[str,str],...]`(models) ↔ `kasb_detail_request(seq,ctg)`(target_pages) ↔ collect의 `for seq,ctg in links.kasb_items` 일치. `external_id` = FSS:nttId / KASB:`{ctgCd}-{seq}` 일관.

## Open Questions (구현 중 확인)
- [ ] KASB 상세 GET 가능 여부(`View{ctgCd}.do?seq=&ctgCd=`) → 되면 detail_url을 GET 링크로(citation 개선). 안 되면 현 POST 식별자 유지.
- [ ] FSS/KASB 상세 본문 영역 셀렉터 정제(현재 `page_body`=전체 텍스트, 노이즈 포함) → RAG 품질 위해 후속 정제 가능(별도).
- [ ] 게시판별 총 페이지수 → `--max-pages` 적정값(전수 수집 시).
