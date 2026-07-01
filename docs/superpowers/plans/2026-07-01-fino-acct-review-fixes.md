# 회계 질의회신(fino_acct) Codex 리뷰 이슈 수정 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 2026-06-30 Codex 코드리뷰(REQUEST CHANGES)가 지적한 fino_acct 크롤러 이슈 5건(major 4·minor 1 중 유효분)을 TDD로 수정한다.

**Architecture:** 기존 `crawler/fino_acct/` 수정. 핵심은 early-stop 종료 신호를 "저장된 문서 수(`docs==0`)"에서 "목록 페이지의 상세 후보 식별자"로 바꿔, 상세 fetch 실패/키워드 필터/ wrap에 강건하게 만든다. 부수적으로 FSS 뷰어링크 첨부 오분류·백업 덮어쓰기·priority 기반 분기를 고친다.

**Tech Stack:** Python 3.12, requests, BeautifulSoup4, sqlite3, pytest.

**리뷰 출처:** `.omc/prompts/codex-response-code-review-request-085b50be.md`

---

## 이슈 ↔ Task 매핑
- **major** `docs==0` 종료 오류(상세 전부 실패 시 조기종료) → Task 2
- **major** FSC 키워드 필터 페이지 조기종료 → Task 2 (연속-빈-페이지 tolerance)
- **major** wrap 미감지(중복 재fetch) → Task 2 (seen-id wrap guard)
- **major** FSS 뷰어링크(`docView`) 첨부 오분류 → Task 1
- **major** cleanup `.bak` 덮어쓰기 → Task 3
- **minor** priority 숫자 기반 파서 분기 → Task 4 (선택)

---

## Task 1: FSS 뷰어 링크(docView) 첨부 오분류 제외

> 원인(실측): FSS 상세의 문서뷰어 링크 `.../etc/docView/view.do?...&fileName=....hwp`가 URL 안의 `.hwp`로 `looks_like_attachment`에 매칭돼 가짜 첨부로 저장됨. 실제 다운로드는 `.../cmmn/file/fileDown.do`.

**Files:**
- Modify: `crawler/fino_acct/parsers.py` (looks_like_attachment)
- Test: `tests/test_fino_acct_collector.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_acct_collector.py 에 추가
def test_fss_detail_excludes_docview_viewer_from_attachments() -> None:
    html = _Path("tests/fixtures/fino_acct/fss_detail.html").read_text(encoding="utf-8")
    links = extract_links("https://www.fss.or.kr/fss/bbs/B0000132/view.do", BeautifulSoup(html, "html.parser"))
    urls = [a.url for a in links.attachments]
    assert any("fileDown.do" in u for u in urls)          # 실제 첨부는 유지
    assert not any("docView" in u for u in urls)          # 뷰어는 제외
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py::test_fss_detail_excludes_docview_viewer_from_attachments -v`
Expected: FAIL (현재 docView 링크가 첨부에 포함됨)

- [ ] **Step 3: Write minimal implementation**

`parsers.py`의 `looks_like_attachment`를 아래로 교체 (뷰어 경로 우선 제외):
```python
def looks_like_attachment(href: str, text: str) -> bool:
    lowered = f"{href} {text}".lower()
    if "docview" in lowered:  # 문서뷰어 링크(fileName=..hwp 로 확장자 오매칭)는 첨부 아님
        return False
    return any(extension in lowered for extension in ATTACHMENT_EXTENSIONS) or "filedown" in lowered or "getfile" in lowered
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py -q`
Expected: PASS (신규 + 기존 전부)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_acct/parsers.py tests/test_fino_acct_collector.py
git commit -m "fix(fino_acct): FSS docView 뷰어 링크를 첨부에서 제외(가짜 .hwp 매칭)"
```

---

## Task 2: 견고한 early-stop 재설계 (상세후보 기반 + 연속빈페이지 + wrap guard)

> 현재 `docs==0`(저장된 문서 수)로 종료 → 상세 fetch 전부 실패 시 목록에 글이 있어도 조기종료. 수정: **목록 페이지에서 발견한 상세 후보 식별자**를 종료 신호로 사용. 연속 2회 빈 목록에서 종료(FSC 키워드 필터 1페이지 공백 허용), 이번 run에서 새 후보가 없으면(wrap/중복 루프) 종료.

**Files:**
- Modify: `crawler/fino_acct/collect.py` (collect_page 반환값 + collect_target LIST)
- Test: `tests/test_fino_acct_collector.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_fino_acct_collector.py 에 추가
def test_early_stop_continues_when_detail_fetches_fail(tmp_path, monkeypatch) -> None:
    """목록엔 글이 있으나 상세 fetch가 404여도, 뒤 페이지(글 있음)를 계속 수집해야 한다."""
    def fake_fetch(session, request, delay):
        if "list.do" in request.url:
            m = re.search(r"pageIndex=(\d+)", request.url)
            page = int(m.group(1)) if m else 1
            if page <= 3:
                body = f'<a href="/fss/bbs/B0000132/view.do?nttId={page}01&menuNo=200442">글{page}</a>'
            else:
                body = "<html>no items</html>"
            return FetchResult(url=request.url, status_code=200, content_type="text/html", content=body.encode())
        # 상세는 page1(nttId=101)만 실패(404), 나머지 성공
        code = 404 if "nttId=101" in request.url else 200
        return FetchResult(url=request.url, status_code=code, content_type="text/html",
                           content=b"<div class='bd-view'><h2 class='subject'>x</h2></div>")

    monkeypatch.setattr(collect_mod, "fetch_page_request", fake_fetch)
    db_path = tmp_path / "a.db"
    with connect_db(db_path) as conn:
        init_schema(conn)
        collect_mod.collect_target(
            conn=conn, session=None, target=TARGETS[1],
            download_dir=tmp_path / "dl", max_pages=100, delay_seconds=0, download=False,
        )
        ids = {r[0] for r in conn.execute("SELECT external_id FROM acct_documents WHERE source_priority=2")}
    # page1 상세가 404여도 page2,3 글은 수집돼야 함(조기종료 금지)
    assert "201" in ids and "301" in ids


def test_early_stop_halts_on_wrap_when_no_new_candidates(tmp_path, monkeypatch) -> None:
    """모든 페이지가 동일 글(wrap)을 반환하면 max_pages 전에 종료해야 한다."""
    calls = {"list": 0}

    def fake_fetch(session, request, delay):
        if "list.do" in request.url:
            calls["list"] += 1
            body = '<a href="/fss/bbs/B0000132/view.do?nttId=999&menuNo=200442">동일글</a>'  # 항상 같은 글
            return FetchResult(url=request.url, status_code=200, content_type="text/html", content=body.encode())
        return FetchResult(url=request.url, status_code=200, content_type="text/html",
                           content=b"<div class='bd-view'><h2 class='subject'>x</h2></div>")

    monkeypatch.setattr(collect_mod, "fetch_page_request", fake_fetch)
    db_path = tmp_path / "a.db"
    with connect_db(db_path) as conn:
        init_schema(conn)
        collect_mod.collect_target(
            conn=conn, session=None, target=TARGETS[1],
            download_dir=tmp_path / "dl", max_pages=1000, delay_seconds=0, download=False,
        )
    assert calls["list"] <= 2  # page2에서 새 후보 없음 감지 → 종료(1000 전부 X)
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py -k "early_stop_continues or early_stop_halts_on_wrap" -v`
Expected: FAIL (현재 로직은 상세 404 시 docs=0으로 조기종료 / wrap 시 max_pages까지 진행)

- [ ] **Step 3: Implement — collect_page가 상세 후보 식별자 반환**

`collect.py`의 `collect_page` 반환 타입을 `tuple[int, int, tuple[str, ...]]`로 바꾸고, 목록에서 찾은 후보 식별자를 계산·반환. 내부 재귀 호출은 3번째 값 무시.

`collect_page` 전체를 아래로 교체:
```python
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
    title_override: str = "",
) -> tuple[int, int, tuple[str, ...]]:
    result = fetch_page_request(session, request, delay_seconds)
    if result.status_code >= 400:
        return 0, 0, ()
    soup = BeautifulSoup(result.content, "html.parser")
    links = extract_links_for_target(target, result.url, soup)
    candidate_ids = tuple(
        [_fss_detail_external_id(u) for u in links.details]
        + [f"{ctg}-{seq}" for seq, ctg in links.kasb_items]
    )
    documents = 0
    attachment_count = 0
    if store_self:
        title = detail_title(target, soup) or title_override or page_title(soup, target.target_name)
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
            ext = _fss_detail_external_id(detail_url)
            d, f, _ = collect_page(
                conn=conn, session=session, target=target,
                request=direct_page_request(detail_url, ext),
                download_dir=download_dir, delay_seconds=delay_seconds,
                download=download, store_self=True, follow_details=False,
                title_override=links.title_by_id.get(ext, ""),
            )
            documents += d
            attachment_count += f
        for seq, ctg in links.kasb_items:
            ext = f"{ctg}-{seq}"
            d, f, _ = collect_page(
                conn=conn, session=session, target=target,
                request=kasb_detail_request(seq, ctg),
                download_dir=download_dir, delay_seconds=delay_seconds,
                download=download, store_self=True, follow_details=False,
                title_override=links.title_by_id.get(ext, ""),
            )
            documents += d
            attachment_count += f
    return documents, attachment_count, candidate_ids
```

- [ ] **Step 4: Implement — collect_target LIST가 후보 기반 종료**

`collect.py`의 `collect_target` LIST 분기를 아래로 교체:
```python
        case TargetKind.LIST:
            documents = 0
            attachments = 0
            seen: set[str] = set()
            empty_streak = 0
            for page in range(1, max_pages + 1):
                docs, files, candidate_ids = collect_page(
                    conn=conn, session=session, target=target,
                    request=page_request_for_target(target, page),
                    download_dir=download_dir, delay_seconds=delay_seconds,
                    download=download, store_self=False, follow_details=True,
                )
                documents += docs
                attachments += files
                if not candidate_ids:
                    empty_streak += 1
                    if empty_streak >= 2:  # 연속 2회 빈 목록 → 끝(FSC 필터 1페이지 공백 허용)
                        break
                    continue
                empty_streak = 0
                new_ids = set(candidate_ids) - seen
                if not new_ids:  # 이번 run에서 새 후보 없음(wrap/중복 루프) → 종료
                    break
                seen |= new_ids
            return documents, attachments
```

그리고 `collect_target`의 DETAIL/META 분기를 3-tuple 언패킹으로 수정:
```python
        case TargetKind.DETAIL | TargetKind.META:
            docs, files, _ = collect_page(
                conn=conn, session=session, target=target,
                request=direct_page_request(target.url, target.url),
                download_dir=download_dir, delay_seconds=delay_seconds,
                download=download, store_self=True, follow_details=False,
            )
            return docs, files
```

- [ ] **Step 5: 기존 early-stop 테스트 어서션 완화**

`test_collect_target_early_stops_when_page_has_no_new_docs`는 연속 2회 빈 페이지에서 종료하므로 목록 fetch가 4회까지 될 수 있다. 어서션을 `assert calls["list"] <= 5`로 변경.

- [ ] **Step 6: Run tests to verify all pass**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py -q`
Expected: PASS (신규 2 + 완화된 기존 + 나머지)

- [ ] **Step 7: 임시 DB로 실사이트 회귀 확인**

Run: `.venv/bin/python -m crawler.fino_acct.collect --priorities 1,2 --no-download --delay-seconds 0.3 --db-path /tmp/acct_fix.db`
그리고:
```bash
.venv/bin/python -c "
import sqlite3,hashlib
c=sqlite3.connect('/tmp/acct_fix.db')
for p in (1,2):
    n=c.execute('SELECT COUNT(*) FROM acct_documents WHERE source_priority=?',(p,)).fetchone()[0]
    print(f'p{p}: {n}건')
"
```
Expected: p1(KASB)·p2(FSS) 정상 다건 수집(조기종료 없이), 실행이 자동 종료.

- [ ] **Step 8: Commit**

```bash
git add crawler/fino_acct/collect.py tests/test_fino_acct_collector.py
git commit -m "fix(fino_acct): early-stop을 상세후보 기반으로 재설계(fetch실패/필터/wrap 강건)"
```

---

## Task 3: cleanup 백업 타임스탬프 (덮어쓰기 방지)

> `.bak` 고정 경로 → 두 번째 실행 시 이미 삭제된 DB로 원본 백업 덮어씀. 타임스탬프(마이크로초 포함) 백업으로 유일 파일 생성.

**Files:**
- Modify: `scripts/fino_acct_cleanup.py`
- Test: `tests/test_fino_acct_collector.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_acct_collector.py 에 추가
import scripts.fino_acct_cleanup as cleanup_mod


def test_cleanup_backup_does_not_overwrite_original(tmp_path) -> None:
    db_path = tmp_path / "acct.db"
    with connect_db(db_path) as conn:
        init_schema(conn)
        for p in (1, 8):
            upsert_document(conn, source_priority=p, agency="a", target_name="t", source_url="u",
                source_type="qna", source_subtype="s", index_name="i", external_id=f"e{p}",
                title="t", detail_url="u", published_date="", body_text="b")
    cleanup_mod.cleanup(db_path)   # 1회: 백업A(1·8 모두 보유), db에서 p1 삭제
    cleanup_mod.cleanup(db_path)   # 2회: 백업B(이미 p1 없음)
    backups = list(tmp_path.glob("acct.db*.bak"))
    assert len(backups) == 2  # 서로 다른 백업 2개(덮어쓰기 없음)
    # 최소 하나의 백업엔 p1(e1)이 남아있어야 함(원본 보존)
    import sqlite3
    has_e1 = any(
        sqlite3.connect(b).execute("SELECT COUNT(*) FROM acct_documents WHERE external_id='e1'").fetchone()[0] > 0
        for b in backups
    )
    assert has_e1
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py::test_cleanup_backup_does_not_overwrite_original -v`
Expected: FAIL (현재는 같은 `.bak` 1개만, 2회차가 덮어써 p1 소실)

- [ ] **Step 3: Implement**

`scripts/fino_acct_cleanup.py`의 `cleanup` 백업 라인을 교체 (상단 import에 `from datetime import datetime` 추가):
```python
def cleanup(db_path: Path) -> tuple[int, int]:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup = db_path.with_name(f"{db_path.name}.{stamp}.bak")
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py::test_cleanup_backup_does_not_overwrite_original -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add scripts/fino_acct_cleanup.py tests/test_fino_acct_collector.py
git commit -m "fix(fino_acct): cleanup 백업 타임스탬프(원본 덮어쓰기 방지)"
```

---

## Task 4 (선택, minor): priority 숫자 대신 URL 기반 파서 분기

> `extract_links_for_target`/`detail_title`이 `target.priority == 1/8`로 사이트를 식별 → 타깃 추가/번호 변경 시 취약. netloc 기반 헬퍼로 교체. (동작 동일, 유지보수성만 개선. 시간이 없으면 후순위.)

**Files:**
- Modify: `crawler/fino_acct/parsers.py`
- Test: `tests/test_fino_acct_collector.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_fino_acct_collector.py 에 추가
from crawler.fino_acct.parsers import _is_kasb_board


def test_is_kasb_board_by_url() -> None:
    assert _is_kasb_board("https://www.kasb.or.kr/front/board/allReplySummaryList.do")
    assert not _is_kasb_board("https://www.fss.or.kr/fss/bbs/B0000132/list.do")
```

- [ ] **Step 2: Run to verify fail**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py::test_is_kasb_board_by_url -v`
Expected: FAIL (`_is_kasb_board` 없음)

- [ ] **Step 3: Implement**

`parsers.py`에 헬퍼 추가하고, `extract_links_for_target`/`detail_title`의 `priority == 1` 분기를 `_is_kasb_board(target.url)`로, `priority != 8` 앞의 FSC 처리를 `_is_fsc(target.url)`로 교체:
```python
def _is_kasb_board(url: str) -> bool:
    return urlparse(url).netloc.endswith("kasb.or.kr")


def _is_fsc(url: str) -> bool:
    return urlparse(url).netloc.endswith("fsc.go.kr")
```
`extract_links_for_target` 시작부:
```python
def extract_links_for_target(target: Target, base_url: str, soup: BeautifulSoup) -> ExtractedLinks:
    if _is_kasb_board(target.url):
        return _extract_kasb_list_links(base_url, soup)
    if _is_fss_board(target.url):
        details: list[str] = []
        titles: dict[str, str] = {}
        for anchor in soup.find_all("a", href=True):
            href = str(anchor.get("href", ""))
            if FSS_DETAIL_RE.search(href):
                url = urljoin(base_url, href)
                details.append(url)
                ntt = _nttid(href)
                if ntt:
                    titles[ntt] = clean_text(anchor.get_text(" ", strip=True))
        return ExtractedLinks(
            details=tuple(dict.fromkeys(details)),
            attachments=extract_links(base_url, soup).attachments,
            title_by_id=titles,
        )
    if not _is_fsc(target.url):
        return extract_links(base_url, soup)
    # (이하 기존 FSC 행 필터 로직 유지)
```
`detail_title` 시작부의 `if target.priority == 1` → `if _is_kasb_board(target.url)` 로 교체.

- [ ] **Step 4: Run tests**

Run: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py -q`
Expected: PASS (전부 — 분기 동작 동일)

- [ ] **Step 5: Commit**

```bash
git add crawler/fino_acct/parsers.py tests/test_fino_acct_collector.py
git commit -m "refactor(fino_acct): 파서 사이트 분기를 priority숫자→URL 기반으로"
```

---

## 최종 검증

- [ ] 전체 테스트: `.venv/bin/python -m pytest tests/test_fino_acct_collector.py -q` → 전부 PASS.
- [ ] (선택) p3 등 실 게시판 소규모 재크롤로 회귀 없음 확인 후 push.

## Self-Review 메모 (작성자)
- Task 매핑: 이슈 major4 → Task1(뷰어), Task2(docs==0/FSC필터/wrap 3건), Task3(백업). minor1 → Task4. 커버 완료.
- 타입 일관: `collect_page` 반환 `tuple[int,int,tuple[str,...]]` — collect_target LIST(3언패킹)/DETAIL·META(3언패킹 후 2반환)/재귀(`d,f,_`) 모두 일치. candidate_id 키 = FSS `_fss_detail_external_id`(nttId), KASB `{ctg}-{seq}` — Task2 파서/수집과 동일.
- YAGNI: Task4는 minor라 선택으로 표시.

## Open Questions
- [ ] 연속-빈-페이지 tolerance 2가 FSC(키워드필터)에 충분한가 — FSC 재크롤 시 필요하면 3으로 상향(현재 FSC 범위 밖).
