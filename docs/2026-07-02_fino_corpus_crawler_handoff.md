# FINO 코퍼스 크롤러 — 세션 인계 (2026-07-02)

> 목표: **NTS를 제외한 전체 코퍼스의 재수집 크롤러 제작 → 수집 → 그 후 NTS 최신화.** 이 문서는 다음 세션이 이어받기 위한 핸드오프. 브랜치 `fino-crawler` (전부 push됨, `decccdb` 기준).

---

## 0. TL;DR — 어디까지 왔나

**NTS 제외 코퍼스 7개 전부 완료 + NTS 최신화(2026-07-02)까지 완료. 원 미션의 수집 단계는 전부 끝.**

| 코퍼스 | 크롤러 | 수집 | 상태 |
|---|---|---|---|
| 세법 법령(법·령·규칙) | `fino_law --law` | 6,507조문 | ✅ 완료 |
| 세법 집행기준 | `fino_law --exec` | 2,799조문 | ✅ 완료 |
| KASB 질의회신 | `fino_acct` p1 | 991건 | ✅ 완료 |
| FSS 질의회신 | `fino_acct` p2~6 | 2,635건 | ✅ 완료 |
| FSC 정책자료(보조) | `fino_acct` p8~17 | 기존 | ✅ |
| **회계 기준서 K-IFRS(전문)** | `fino_std` | 61문서(kifrs 42+kifrs_interp 19)/18,550문단 | ✅ 완료 |
| **회계 기준서 GAAP(일반기업회계기준 전문)** | `fino_std` | 35문서/2,825문단 | ✅ 완료 |

**다음 순서**: 수집은 전부 완료(§2). 남은 후속은 통합 오케스트레이터/정리(§3, 보류 중)와 다운스트림 export→인덱스(§4).

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

### 회계 기준서(K-IFRS/GAAP) = `crawler/fino_std/` (DB: `data/fino_std.db`, documents/paragraphs)
- db.kasb.or.kr API(`api/title/{stdNum}`(목차), `api/content/{stdNum}/{documentId}`(본문); `/api/paragraphs/content/{std}/{para}`는 문단 단위 검증용)로 기준서 전문 수집. 102시드 중 96문서/21,375문단(kifrs 42+kifrs_interp 19=18,550문단, gaap 35/2,825문단), skip 6건(91·93·1191·1192·1118·10121, API 미지원/빈 목차), 빈 본문 0.
- 실행: `.venv/bin/python -m crawler.fino_std.collect`. 재실행=최신화.
- 상세: 메모리 `fino-std-standards-crawler`.

---

## 2. NTS 최신화 — ✅ 완료 (2026-07-02)

- 증분 재가동 완료: **예규(qt) +232건 → 139,617행, 판례(pd) +1,132건 → 151,041행** (papers.db 총 ~290.7k행, max crawled_at 2026-07-02). 기존 행 무접촉, 신규만 INSERT.
- 실행법(재현): frwaler에서 `FINOLAW_DB_PATH=/data_raid/ruci_workspace/crawler-poc/data/papers.db .venv/bin/python -m crawler.main crawl nts-taxlaw-qt --incremental` (pd 동일). 주의: env 미지정 시 기본 DB는 `frwaler/data/data.db`.
- 증분 안전 근거: 목록 API 등록일 내림차순(`DCM_RGT_DTM/DESC`) + "새 항목 0인 페이지에서 정지" + `(site_id, external_id)` 사전 중복 체크.
- **일원화**: 크롤러 코드는 frwaler가 정본(크롤러는 frwaler에서 실행, crawler-poc 사본은 구버전). DB 파일(20GB)은 crawler-poc/data/에 그대로 두고 `FINOLAW_DB_PATH`로 참조 — 파일 이전은 보류(사용자 합의: 기존 내용 기준 추가만).

---

## 3. 아키텍처 (현 구조 — 유지, 정리는 나중)

4개 독립 크롤러(소스 성격이 달라 분리가 자연스러움):
- `crawler/fino_law/` → `fino_law.db` (세법 법령+집행기준, documents/articles, source_kind=law|exec_standard)
- `crawler/fino_acct/` → `fino_acct.db` (회계 질의회신, acct_documents/acct_attachments)
- `crawler/fino_std/` → `fino_std.db` (회계 기준서 K-IFRS/GAAP, documents/paragraphs)
- `crawler/sites/*` + `crawler/main.py` → `papers.db` (NTS 판례/예규 + 기타 사이트)

**통합 오케스트레이터/정리는 전체 크롤러 완성 후로 미룸**(사용자 합의). data/*.db는 .gitignore(코드만 버전관리) — 데이터는 이 환경 로컬 디스크에만 있음(재수집으로 복구 가능).

---

## 4. 핵심 포인터
- **메모리**: `fino-law-corpus-collection-endpoints`(세법 법령·집행기준 엔드포인트/코드), `fino-acct-qna-crawler`(회계 질의회신), `fino-std-standards-crawler`(회계 기준서 K-IFRS/GAAP).
- **스펙/계획**: `docs/superpowers/{specs,plans}/2026-06-30-fino-acct-*`, `docs/superpowers/{specs,plans}/2026-07-02-fino-law-exec-standard-crawler.md`, `docs/superpowers/plans/2026-07-01-fino-acct-review-fixes.md`.
- **상위 문서**: `docs/2026-06-30_fino_corpus_master_handoff.md`(원 7코퍼스 매핑·citation 미션).
- **테스트**: `.venv/bin/python -m pytest tests/test_fino_law_*.py tests/test_fino_acct_collector.py tests/test_fino_std_*.py -q`.
- **다운스트림(FINO BE)**: fino_law.db/fino_acct.db/fino_std.db export(MD/NDJSON, 조문·문단별 source_url) → exec_standard 인덱스 → RAG citation.

## 5. 새 세션 액션 순서
1. 이 문서 + 메모리 3개 읽기.
2. (수집은 전부 완료) 필요 시 각 크롤러 재실행=최신화. NTS는 §2의 `FINOLAW_DB_PATH` 실행법 참고.
3. 후속 후보: 다운스트림 export→FINO 인덱스 재생성, 통합 오케스트레이터/정리(보류 중), papers.db 물리 이전 여부 결정.
4. 최신화 관리: `python -m crawler.fino_ops status|refresh` 또는 uvicorn(:8500)+dashboard(:3000) — 스펙 `2026-07-02-fino-ops-dashboard-design.md`.

## 6. Open Questions
- [ ] 법인세 집행기준 2조문(44-0-32/33) 본문 파서 miss — PDF 헤더 edge case 보정(소소, 선택).
- [ ] fino_std BC/IG(결론도출근거/적용지침) 문단의 뷰어 라우팅 — 데이터 정합성엔 무관, 수동 확인만 완료.
