# 회계 질의회신(fino_acct) deep-link 재수집 수정 — 설계 스펙

> 작성 2026-06-30. 브레인스토밍 합의안. 상위 맥락: `docs/2026-06-30_fino_corpus_master_handoff.md` 섹션 9(트랙 2), `plans/kasb-recrawl-repair.md`. 목표: 회계 질의회신 크롤러가 **개별 문서 단위로 본문+deep-link를 정상 수집**하도록 기존 `crawler/fino_acct/` 코드를 수정한다.

## 1. 목표 / 비목표

**목표** — KASB·FSS 질의회신 크롤러를 고쳐, **질의회신 1글 = 문서 1건**으로 개별 본문·고유식별자·원문 deep-link를 수집한다. 언제든 재실행 가능(최신화).

**비목표** — FSC(priority 8~17, 다른 사이트·이미 동작), 세법 집행기준(별도 스펙), 인덱스 빌드/FINO BE 연동.

## 2. 문제 (실측 2026-06-30, `data/fino_acct.db`)

현 크롤러는 **목록 페이지만 저장**하고 상세로 진입하지 않는다.
- **p1 KASB 217행 / 본문 고유 1** — 목록만 217번 저장(완전 중복).
- **p2~6 FSS 각 217행 / 본문 고유 217** — "목록 페이지 217장" 스냅샷일 뿐, 개별 질의회신 아님(`external_id`=`list.do` URL, 페이지당 수천 건 누락).
- p8~10 FSC는 개별 detail URL로 들어와 있음(이미 동작, 범위 밖).

## 3. 정찰로 확정된 수집 경로 (2026-06-30, 실호출 검증)

| 사이트 | 목록 | 상세 진입 | 고유 식별자 | deep-link |
|--------|------|-----------|------------|-----------|
| **FSS** | `Bxxxx/list.do?...&pageIndex=N` (정적 HTML) | 목록의 `Bxxxx/view.do?nttId=N` 링크 추출 → GET | `nttId` | `view.do?nttId=N` (GET, 깔끔) |
| **KASB** | `allReplySummaryList.do` POST(정적 HTML) | 행의 `onclick=fn_Detail('seq','ctgCd')` 파싱 → POST `/front/board/View{ctgCd}.do` | `(ctgCd, seq)` | `View{ctgCd}.do` (POST) |

- KASB JS: `fn_Detail(seq,ctgCd){ url="/front/board/View"+ctgCd+".do"; submit({seq,ctgCd}) }`. POST 파라미터 `seq`, `ctgCd`, `siteCd=002000000000000`.
- 실호출 확인: KASB View016009.do(seq=40533) → 본문(질의/회신) 50KB. FSS view.do?nttId=133043 → 본문 291KB.

## 4. 설계

### 4.1 상세 단위 저장 (③)
**문서 단위를 "목록 페이지" → "질의회신 1글"로 변경.** 목록 페이지는 **상세 링크 추출용으로만 사용하고 저장하지 않는다**(색인 ≠ 내용). 각 상세를 fetch → 제목/본문/첨부 파싱 → `acct_documents` 1행으로 upsert. `external_id`=문서별 식별자(FSS=nttId, KASB=`{ctgCd}-{seq}`), `detail_url`=원문 deep-link, `body_text`=질의/회신 본문.

### 4.2 깨진 행 정리 (④)
재크롤 행은 새 `external_id`로 들어와 기존 깨진 행을 덮어쓰지 않으므로 사전 정리 필요. **방법 A 채택**: `fino_acct.db` 백업 → `DELETE FROM acct_documents WHERE source_priority BETWEEN 1 AND 6`(첨부는 FK CASCADE) → priority 1~6 재크롤. FSC(8~17) 보존.

### 4.3 코드 변경 단위 (기존 fino_acct **수정**)
- `parsers.py`: FSS 상세링크 추출기(`view.do?nttId=`)와 KASB 상세 추출기(`fn_Detail('seq','ctgCd')` 파싱)를 `extract_links_for_target`에 사이트별 분기로 추가. 기존 KASB는 첨부만 긁고 `details=()` 반환 → 상세 진입(seq/ctgCd) 반환하도록 교정.
- `target_pages.py`: KASB 상세용 POST `PageRequest` 빌더 추가(`View{ctgCd}.do`, data=`{seq,ctgCd,siteCd}`, external_id=`{ctgCd}-{seq}`). FSS 상세는 기존 `direct_page_request`(GET) 재사용.
- `collect.py`: FSS/KASB LIST 타깃 처리에서 **목록 페이지를 upsert하지 않고**, 추출한 상세 요청들을 순회해 상세만 upsert. 페이지네이션은 목록에서 다음 페이지로 진행.
- `models.py`: 필요 시 상세 요청 표현용 경량 dataclass(또는 기존 `ExtractedLinks.details`에 `PageRequest` 수용). 스키마 변경 없음.

### 4.4 deep-link (citation)
- FSS: `view.do?nttId=N` GET → 그대로 detail_url(클릭 가능) ✅
- KASB: 상세가 POST(`View{ctgCd}.do`)라 클릭형 링크는 best-effort. 1순위는 본문/식별자 정상 수집. detail_url에는 식별 가능한 형태(예: `View{ctgCd}.do?seq={seq}&ctgCd={ctgCd}` GET 시도, 실패 시 식별자 보존) 저장 — 구현 시 GET 가능 여부 확인.

## 5. 데이터 모델
**스키마 변경 없음.** 기존 `acct_documents`(`external_id`,`detail_url`,`title`,`body_text`,`UNIQUE(source_priority,external_id)`)와 `acct_attachments`(FK CASCADE) 재사용. 값만 per-문서로 올바르게 채움.

## 6. 에러 처리 / 견고성
- POST 재시도(`fetch.py` 3회), 멱등 upsert(기존). 상세 fetch 실패 시 해당 글 skip+로그, 목록 진행 지속.
- 페이지네이션 종료: 목록에서 더 이상 상세 링크가 없으면 중단(또는 `max_pages`).
- KASB 첨부: 기존 `kasb_attachment`(post_file_no/seq) 재사용 — 상세 페이지의 첨부는 문서별로 정상 분리됨.

## 7. 테스트 / 수용 기준
- **단위 테스트**(오프라인 fixture, `tests/test_fino_acct_*.py` 확장): 정찰 때 저장한 실제 HTML(FSS 목록·상세, KASB 목록·상세)로 추출기 검증.
  - FSS 목록 → `view.do?nttId=` 추출 정확.
  - KASB 목록 → `fn_Detail('40533','016009')` → (seq=40533, ctgCd=016009) 파싱.
  - KASB 상세 PageRequest = POST `View016009.do`, data 포함.
- **수용 기준**(재크롤 후 DB):
  - KASB 본문 **고유 해시 다수**(1 아님), 각 행 개별 식별자(`{ctgCd}-{seq}`)+deep-link.
  - FSS 각 게시판 → 개별 질의회신 N건(목록 스냅샷 아님), detail_url=`view.do?nttId=`.
  - 같은 PDF/본문이 전 레코드에 반복되지 않음.
  - 멱등: 2회 실행 시 row 수 불변.

## 8. 단계
```
1. 정찰 HTML fixture 저장 (FSS/KASB 목록·상세)
2. FSS 상세링크 추출기 + 테스트
3. KASB 상세 추출기(seq/ctgCd) + POST PageRequest + 테스트
4. collect.py: 상세 단위 저장 배선(목록 미저장)
5. DB 백업 + priority 1~6 삭제
6. 재크롤 + 수용기준 검증
```

## 9. Open Questions
- [ ] KASB 상세가 GET(`View{ctgCd}.do?seq=&ctgCd=`)로도 열리는지 → 열리면 citation 링크 깔끔.
- [ ] FSS 게시판별 본문 영역 셀렉터 차이(질의/회신 구조) — 구현 시 상세 HTML로 확정.
- [ ] kasb.or.kr/fss.or.kr 대량 수집 robots/약관, 적정 delay.
