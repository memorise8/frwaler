# KASB 질의회신 재크롤 수리계획 (조사·설계 — 코드 미포함)

> 작성 2026-06-30. `acct-rag-curation.md` Task 6의 후속 설계문서. **트랙 2(회계 질의회신 원문링크) 선행 필수.** 이 문서는 조사 항목·수리 방향·수용 기준만 담는다. 실제 크롤러 코드는 다음 세션에서 작성. 상위 맥락은 `docs/2026-06-30_fino_corpus_master_handoff.md` 섹션 9 참조.

---

## 1. 문제 (실측 2026-06-30, `data/fino_acct.db`)
- KASB 질의회신 수집 **217행**인데:
  - **`body_text` 고유 해시 = 1개** → 217건 전부 같은 본문(4,267자)의 복제. 실질 수집 내용 = 0.
  - **제목 전부 "질의회신 요약 전체"** (개별 제목 없음).
  - **`detail_url` = `allReplySummaryList.do#page=1..217`** — 목록 페이지의 page 앵커만 다른 같은 URL. 개별 문서 deep-link 아님.
  - `external_id`도 위 목록 URL 문자열 그대로.
- 즉 크롤러가 **개별 질의회신 문서에 도달하지 못하고 목록 페이지만 217번 저장**했다.
- `acct-rag-curation.md`에서 이 217건은 priority-01 = **quarantine(격리)** 로 분류됨.

## 2. 원인 가설
- KASB 질의회신 페이지 `https://www.kasb.or.kr/front/board/allReplySummaryList.do` 는 **SPA(JS 렌더 게시판)**.
  - 목록·상세가 서버 HTML이 아니라 JS/AJAX로 그려질 가능성 → 정적 `requests`+BS4로는 목록 1장만 잡힘.
  - 개별 항목 클릭이 **POST/AJAX 호출**(예: 문서 id, `post_file_no`/`post_file_seq` 류 파라미터)로 상세를 불러오는데, 그 식별자가 URL 유일성에 반영되지 않아 전부 같은 레코드로 저장됨.
- 첨부(PDF/HWP) 다운로드도 같은 식별자 문제로 같은 파일이 반복될 수 있음.

## 3. 조사 항목 (코드 작성 전 확인)
- [ ] `allReplySummaryList.do` 가 SPA인지 확인 — playwright로 렌더 전/후 DOM 비교, 목록 항목이 JS로 채워지는지.
- [ ] 목록 → 개별 항목의 **실제 호출**: 브라우저 DevTools/`crawler/network_capture.py` 로 AJAX 엔드포인트·요청 본문(POST 파라미터·문서 id) 캡처.
- [ ] 개별 질의회신의 **고유 식별자**(문서번호/일련번호)와 **상세 URL 패턴**(deep-link 가능한지, 아니면 POST 전용인지).
- [ ] 첨부 다운로드 식별자(`post_file_no`/`post_file_seq` 등)와 파일-문서 매핑.
- [ ] 페이지네이션 실제 파라미터(현 `#page=N`는 가짜 — 서버가 실제로 받는 page 키 확인).
- [ ] KASB 질의회신 카테고리 구분: `K_IFRS_QNA|GAAP_QNA|IFRS_IC_KR|FAST_QNA|TF_SUPPORT` (sources.py Target 1) 가 어떻게 필터되는지.

## 4. 수리 방향 (설계)
- **playwright 기반 렌더링 크롤** — `crawler/`의 playwright 자산 재사용. 목록 렌더 → 각 항목 진입 → 상세 본문·문서번호·원문 URL·첨부 추출.
- 또는 **3번에서 발견한 AJAX 엔드포인트를 직접 호출**(더 안정적이면). `network_capture.py`로 찾은 요청을 재현.
- `fino_acct.db` 저장 시 **유일성 키를 개별 문서 식별자로** 교정(현재는 목록 URL이라 충돌). `detail_url`에 **개별 deep-link(또는 재현 가능한 상세 호출 정보)** 저장.
- 본문은 개별 질의/회신 텍스트로 저장(현재 목록 요약 1건 → 개별 N건).
- 산출은 `export_markdown.py` 경로를 그대로 타도록(우선순위/메타 일관).

## 5. 수용 기준 (Acceptance)
- 재크롤 후 KASB `body_text` **고유 해시 다수**(목표: 항목 수에 근접, 1 아님).
- 각 레코드에 **개별 문서번호 + 원문 URL** 존재.
- 같은 PDF/본문이 전 레코드에 반복되지 않음.
- `detail_url` 이 목록 URL이 아닌 **항목별로 구별**.
- (연계) 이 산출이 FINO 회계 질의회신 인덱스 `source_url` 재빌드 입력으로 매핑 가능 — 매핑 키(문서번호/title) 확인.

## 6. 주의 / 전제
- **로그인/약관**: KASB 자료 접근에 로그인 필요한지, robots.txt/이용약관 확인.
- **CAPTCHA**: SPA에 봇 차단(reCAPTCHA) 있으면 난이도 급상승 — 발견 시 별도 보고.
- **FSS와 분리**: FSS(651건)는 목록은 페이지별로 받았으나 역시 개별 deep-link 없음 — 같은 "상세 진입" 보강이 FSS에도 필요(`view.do?nttId=...` 류 추정). KASB가 더 심각(내용 자체가 1건)이므로 우선순위 높음.

## 7. 산출물
- 본 조사 결과(엔드포인트·식별자·페이지네이션) 기록 → 그다음 크롤러 코드 수정(`crawler/fino_acct/{sources,fetch,parsers,collect}.py`) + 재크롤 → `fino_acct.db` 검증(수용 기준) → FINO 인덱스 `source_url` 재빌드.
