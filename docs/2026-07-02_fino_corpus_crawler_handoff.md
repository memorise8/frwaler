# FINO 코퍼스 크롤러 — 세션 인계 (2026-07-02)

> 목표: **NTS를 제외한 전체 코퍼스의 재수집 크롤러 제작 → 수집 → 그 후 NTS 최신화.** 이 문서는 다음 세션이 이어받기 위한 핸드오프. 브랜치 `fino-crawler` (전부 push됨, `decccdb` 기준).

---

## 0. TL;DR — 어디까지 왔나

**NTS 제외 코퍼스 7개 중 5개 완료, 2개(회계 기준서) 남음.**

| 코퍼스 | 크롤러 | 수집 | 상태 |
|---|---|---|---|
| 세법 법령(법·령·규칙) | `fino_law --law` | 6,507조문 | ✅ 완료 |
| 세법 집행기준 | `fino_law --exec` | 2,799조문 | ✅ 완료 |
| KASB 질의회신 | `fino_acct` p1 | 991건 | ✅ 완료 |
| FSS 질의회신 | `fino_acct` p2~6 | 2,635건 | ✅ 완료 |
| FSC 정책자료(보조) | `fino_acct` p8~17 | 기존 | ✅ |
| **회계 기준서 K-IFRS(전문)** | ❌ 없음 | — | 🔴 **다음 작업** |
| **회계 기준서 GAAP(일반기업회계기준 전문)** | ❌ 없음 | — | 🔴 **다음 작업** |

**다음 순서**: ① 회계 기준서(K-IFRS/GAAP) 크롤러 제작·수집 → ② NTS 최신화(papers.db 재가동·일원화).

---

## 1. 완료된 것 (이번 세션)

### 세법 = `crawler/fino_law/` (DB: `data/fino_law.db`, documents/articles)
- **법령**(`--law`, law.go.kr OpenAPI, OC=`fino-law-data`): 국세+지방세 55법령/6,507조문, 항·호 본문 플래튼, 조문별 citation deep-link(`law.go.kr/법령/{명}#제N조`).
- **집행기준**(`--exec`, taxlaw PDF): 15세목/2,799조문, 본문 99.9%. taxlaw `ASISTE001MR02`(조문목록)+`downloadFile.do`(PDF)→`pdftotext`→번호매칭. **LLM 없음.** taxlaw이 국세기본법 목록에 주류 조문 41개 오염시킨 것 → 오염필터(제목이 세목 PDF에 없으면 제외)로 정리.
- 실행: `.venv/bin/python -m crawler.fino_law.collect --law --exec`. 재실행=최신화.
- 상세 엔드포인트: 메모리 `fino-law-corpus-collection-endpoints`.

### 회계 질의회신 = `crawler/fino_acct/` (DB: `data/fino_acct.db`, acct_documents/acct_attachments)
- KASB(`allReplySummaryList.do`→`fn_Detail`→`View{ctgCd}.do`)+FSS(`list.do`→`view.do?nttId`) 개별 문서 재수집. 3,626건+첨부. 목록 대신 상세 저장, early-stop(docs 후보 기반), 제목 보강. Codex 리뷰 이슈 5건 수정 완료.
- 실행: `.venv/bin/python -m crawler.fino_acct.collect --priorities 1,2,3,4,5,6`.
- 상세: 메모리 `fino-acct-qna-crawler`.

---

## 2. 다음 작업 ① — 회계 기준서(K-IFRS/GAAP) 크롤러 (최우선)

**목표**: KASB의 실제 **회계기준 전문**(K-IFRS 기준서 + 일반기업회계기준(GAAP) 전문)을 재수집. FINO 기존 인덱스 fino-kifrs-20260625(17,444), fino-gaap-20260625(2,430)에 대응하는 원본.

**질의회신(fino_acct)과 별개** — 그건 Q&A, 이건 기준 조문 전문이다.

**착수 방법 (권장 = 집행기준과 동일 패턴)**:
1. **소스 정찰**: `kasb.or.kr`에서 K-IFRS/일반기업회계기준 **기준서 게시판/뷰어** 찾기. `fino_acct`의 KASB 경로(`/front/board/...`)와 유사할 것. 기준서가 HTML인지 PDF인지 확인.
2. **기존 MD 위치 확인**: FINO에 이미 있는 kifrs/gaap MD가 어디 있는지(`/data_raid/share/` 계열 추정, tax_data/current엔 없음 — 별도 경로). 있으면 집행기준처럼 "구조는 크롤 + 본문은 MD/PDF" 하이브리드 가능.
3. **구현**: 집행기준(`fino_law` exec)과 동일 골격 재사용 가능하면 재사용. 아니면 `fino_acct`에 기준서 타깃 추가 or 신규 모듈.
4. 브레인스토밍 → 스펙 → 계획 → subagent 구현 (이번 세션과 동일 플로우).

**주의**: 소스가 SPA/POST면 DevTools 캡처가 가장 확실(집행기준에서 그렇게 뚫음). blind probe는 한계.

---

## 3. 다음 작업 ② — NTS 최신화 (①완료 후)

- `crawler/sites/nts_taxlaw.py`(taxlaw action.do, curl) — 판례/예규 크롤러. 작동·증분 지원.
- 데이터 `crawler-poc/data/papers.db`(20GB, 289k행, ~2026-04 기준, ~2개월+ stale).
- 할 일: **증분 재가동으로 최신화** + frwaler로 **일원화**(참조/이전). 신선도 확인 후.

---

## 4. 아키텍처 (현 구조 — 유지, 정리는 나중)

3개 독립 크롤러(소스 성격이 달라 분리가 자연스러움):
- `crawler/fino_law/` → `fino_law.db` (세법 법령+집행기준, documents/articles, source_kind=law|exec_standard)
- `crawler/fino_acct/` → `fino_acct.db` (회계 질의회신, acct_documents/acct_attachments)
- `crawler/sites/*` + `crawler/main.py` → `papers.db` (NTS 판례/예규 + 기타 사이트)

**통합 오케스트레이터/정리는 전체 크롤러 완성 후로 미룸**(사용자 합의). data/*.db는 .gitignore(코드만 버전관리) — 데이터는 이 환경 로컬 디스크에만 있음(재수집으로 복구 가능).

---

## 5. 핵심 포인터
- **메모리**: `fino-law-corpus-collection-endpoints`(세법 법령·집행기준 엔드포인트/코드), `fino-acct-qna-crawler`(회계 질의회신).
- **스펙/계획**: `docs/superpowers/{specs,plans}/2026-06-30-fino-acct-*`, `docs/superpowers/{specs,plans}/2026-07-02-fino-law-exec-standard-crawler.md`, `docs/superpowers/plans/2026-07-01-fino-acct-review-fixes.md`.
- **상위 문서**: `docs/2026-06-30_fino_corpus_master_handoff.md`(원 7코퍼스 매핑·citation 미션).
- **테스트**: `.venv/bin/python -m pytest tests/test_fino_law_*.py tests/test_fino_acct_collector.py -q`.
- **다운스트림(FINO BE)**: fino_law.db/fino_acct.db export(MD/NDJSON, 조문별 source_url) → exec_standard 인덱스 → RAG citation.

## 6. 새 세션 액션 순서
1. 이 문서 + 메모리 2개 읽기.
2. **회계 기준서(K-IFRS/GAAP) 소스 정찰** (kasb.or.kr 기준서 게시판/뷰어, 기존 MD 위치).
3. 브레인스토밍 → 스펙 → 계획 → subagent 구현 → 수집·검증.
4. (①완료 후) NTS papers.db 재가동·최신화·일원화.

## 7. Open Questions
- [ ] 회계 기준서 소스: kasb.or.kr 기준서 뷰어 URL/구조(HTML vs PDF), 기존 kifrs/gaap MD 정확 위치.
- [ ] 기준서를 fino_law(조문형) 골격에 넣을지 fino_acct(문서형)에 넣을지 신규 모듈일지.
- [ ] 법인세 집행기준 2조문(44-0-32/33) 본문 파서 miss — PDF 헤더 edge case 보정(소소, 선택).
