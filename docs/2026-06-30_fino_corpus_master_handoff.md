# FINO 코퍼스 통합 크롤러 — 마스터 인계 (frwaler 단일 거점)

> 작성 2026-06-30 (FINO 세션 c2f97790). **이 문서 = FINO RAG 7개 원본 코퍼스를 frwaler 하나로 수집/관리하기 위한 마스터 핸드오프.** 새 세션은 이 문서부터 읽으면 전체를 파악할 수 있다. frwaler 기존 문서(`status.md`, `next-session.md`, 2026-04-24)는 finolaw FE/일반 크롤러 관점이라 별개 — 이 문서가 FINO 코퍼스 미션의 상위 문서다.

---

## 0. TL;DR — "frwaler 하나로 전체 가능?" → 예
- frwaler는 **베이스 크롤러(`crawler/sites/nts_taxlaw.py` 등) + 회계 전용 `crawler/fino_acct/`** 를 모두 가진 superset이다. → 7개 소스 전부의 코드가 여기 있다.
- **회계(KASB·FSS·FSC)는 이미 수집됨**: `fino_acct.db`(acct_documents 2,216 / attachments 3,711) + MD export 3,711건(`/data_raid/ruci_workspace/acct_rag_data/`).
- **NTS/세법은 같은 리포의 다른 체크아웃(`crawler-poc`)이 보유**: `nts_taxlaw.py` + `data/papers.db`(**20.6GB**, papers 310,032). 이 데이터는 frwaler엔 없음(코드만 있음).
- 그래서 frwaler를 단일 작업 거점으로 삼되, **NTS 대용량 DB는 crawler-poc 체크아웃을 참조/이전**해야 "전체"가 한 곳에서 된다.

### ✅ 보존 상태 (2026-06-30 커밋 완료)
이전엔 `crawler/fino_acct/` 전체가 untracked였으나 **커밋 완료 → 유실 위험 해소, 트리 클린**:
- `304c2ec` feat(fino_acct): 회계 크롤러(KASB/FSS/FSC) + 큐레이션 계획 + 테스트
- `b0fcb63` feat(finolaw): sources viewer UI
- (이 문서) docs: 마스터 인계
branch `fino-crawler`. 미푸시일 수 있으니 새 세션은 `git -C /data_raid/ruci_workspace/frwaler log --oneline -5` 와 `git status`로 확인 후 필요시 `git push origin fino-crawler`. **새 세션은 커밋 걱정 없이 바로 개발 시작 가능.** (대용량 `data/`·`downloads/`는 `.gitignore`로 제외됨 — 코드만 버전관리.)

---

## 1. 리포 토폴로지 (혼동 주의)
세 디렉토리는 **모두 같은 GitHub 리포 `github.com/memorise8/frwaler` (branch `fino-crawler`)** 관련이다.

| 로컬 경로 | 정체 | 최신 | 고유 보유물 |
|---|---|---|---|
| `/data_raid/ruci_workspace/frwaler` | **메인 작업본** (newest) | 코드 06-25, 커밋 05-06 | `crawler/fino_acct/`(미커밋) + `data/fino_acct.db`(70MB) |
| `/data_raid/ruci_workspace/crawler-poc` | 같은 리포 구 체크아웃 | 커밋 05-04 | **`data/papers.db` 20.6GB (NTS 310k)** |
| `/data_raid/ruci_workspace/frwaler_job` | 납품/리포트 변형본 (06-18) | — | livertree, client_report.xlsx (메인선 아님) |

**권고**: frwaler를 정본으로. crawler-poc의 NTS `papers.db`는 (a) frwaler/data로 복사하거나 (b) 경로 참조로 연결. frwaler_job은 참고만.

---

## 2. FINO 7개 코퍼스 ↔ 크롤러 자산 매핑

| FINO 코퍼스 | 추정 출처 | 담당 크롤러(frwaler 내) | 수집 상태 |
|---|---|---|---|
| **NTS 예규·판례** | `taxlaw.nts.go.kr` | `crawler/sites/nts_taxlaw.py` | ✅ 완료(데이터는 crawler-poc/papers.db 310k) |
| **세법(법·령·규칙)** | `law.go.kr` / taxlaw | nts_taxlaw + **law.go.kr OpenAPI(권장)** | 🟡 부분 — API 조사 필요 |
| **집행기준** | `taxlaw.nts.go.kr` | nts_taxlaw category 확장 | 🟡 부분 |
| **회계 K-IFRS / GAAP** | `kasb.or.kr` | `crawler/fino_acct/` | 🟡 KASB 수집했으나 **버그(아래)** |
| **KASB 질의회신** | `kasb.or.kr/front/board/allReplySummaryList.do` | `fino_acct/`(priority 01) | 🔴 **버그: 217건 중 본문 unique 1개** |
| **FSS 질의회신** | `fss.or.kr/fss/bbs/B0000132~291` | `fino_acct/`(priority 02·03) | ✅ 강한 후보(수집됨) |
| (보조) FSC 보도/법령 | `fsc.go.kr/no010101` | `fino_acct/`(priority 08) | ⚪ 기본 제외(회계 관련성 약함) |

> fino_acct 타깃 전체는 `crawler/fino_acct/sources.py` 참조(KASB·FSS 게시판 다수·FSC·data.go.kr).

---

## 3. 회계(fino_acct) 현황 상세
- 파이프라인: `sources.py`(타깃) → `collect.py`/`fetch.py`/`parsers.py`(수집·파싱) → `fino_acct.db` → `export_markdown.py` → `/data_raid/ruci_workspace/acct_rag_data/documents/{priority}/*.md`(3,711) → `curate_markdown.py`(큐레이션).
- priority 분포(= 소스 구분): 03(FSS) 1405, 10 936, 09 522, 06 272, 05 230, 01(KASB) 217 …
- **큐레이션 계획 존재**: `plans/acct-rag-curation.md` — include=FSS(02·03), secondary=05/06/09/10/13~17, quarantine=KASB(01), exclude=FSC(08). 산출물은 `/data_raid/ruci_workspace/acct_rag_data_curated/<run_id>/`(raw 불변).
- **알려진 버그(미해결)**: KASB priority 01 = 217 파일인데 **본문 해시 unique 1개** → 첨부 POST 다운로드 식별자(`post_file_no/seq`)가 URL 유일성에 반영 안 됨. 같은 PDF가 전 레코드에 중복. 수리계획 `plans/kasb-recrawl-repair.md`(작성 예정, 미작성).

### 실행 예시
```bash
cd /data_raid/ruci_workspace/frwaler
# 회계 수집
.venv/bin/python -m crawler.fino_acct.collect --help
# MD 큐레이션(raw 읽기전용, 산출 분리)
.venv/bin/python -m crawler.fino_acct.curate_markdown \
  --input-dir /data_raid/ruci_workspace/acct_rag_data \
  --output-root /data_raid/ruci_workspace/acct_rag_data_curated
# 테스트
.venv/bin/python -m pytest tests/test_fino_acct_collector.py -q
```

---

## 4. NTS/세법(base crawler) 현황
- `crawler/sites/nts_taxlaw.py`(38KB)가 `taxlaw.nts.go.kr`를 doc_type/category로 크롤(법령·판례·예규). 산출 `data/papers.db`(crawler-poc 체크아웃, 310,032행).
- 실행: `.venv/bin/python -m crawler.main crawl <site_id>` / `stats` / `list-sites`.
- 자동 신규 사이트: `auto-add-codex <url>`(Codex CLI Tier 2) → `crawler/sites/custom/*.py`.

---

## 5. FINO BE 연결(다운스트림 — 다른 리포)
크롤 산출물은 FINO BE(`/data_raid/ruci_workspace/repo/finov2/fino-backend-dev`)의 색인 빌드 입력이 된다:
- 법/회계 MD → `app/services/exec_standard_sources.py` 레지스트리 → `build_exec_standard_index.py` / `build_accounting_standard_ndjson.py`
- QnA PDF → `scripts/qna_corpus/parse_*_pdf.py` → `build_qna_index.py`
- NTS(papers.db + nts_summaries.jsonl) → `scripts/nts_full_embed_index.py`
- **참고**: FINO 측 별도 인계문서 `crawler-poc/docs/2026-06-29_fino_corpus_crawler_plan.md`(회계는 신규필요로 적혀 있으나 **실제로는 frwaler/fino_acct에 이미 구현됨** — 본 문서가 정정).

---

## 6. 새 세션 액션 순서
0. (선택) `git -C /data_raid/ruci_workspace/frwaler push origin fino-crawler` — 304c2ec/b0fcb63/문서 원격 백업.
1. **NTS papers.db 일원화** — crawler-poc/data/papers.db(20.6GB)를 frwaler에서 참조하거나 이전. 신선도(마지막 크롤일) 확인.
2. **KASB 버그 수리** — `plans/acct-rag-curation.md` Task 6 + 수리계획 작성 → POST 첨부 식별자 보존 재크롤.
3. **세법 = law.go.kr/data.go.kr OpenAPI 조사** → 크롤 대체 가능 시 전환.
4. **회계 큐레이션 실행** → FINO BE 빌드 입력 포맷 합의.
5. (정리) crawler-poc·frwaler_job 중 정본을 frwaler로 확정, 나머지 아카이브.

## 7. Open Questions (확인 필요)
- [ ] nts_taxlaw가 법령·집행기준까지 커버하는 정확한 doc_type/category 범위
- [ ] law.go.kr / data.go.kr 공식 OpenAPI 존재·키 발급
- [ ] kasb.or.kr·fss.or.kr 로그인/SPA/CAPTCHA 유무
- [ ] KASB priority 01 첨부 다운로드 식별자 버그 근본원인
- [ ] frwaler ↔ crawler-poc ↔ frwaler_job 중 정본 확정 + 나머지 정리
- [ ] 각 출처 robots.txt/약관 (대량 수집 합법성)

## 8. 핵심 파일 포인터
**frwaler**: `crawler/fino_acct/{sources,collect,fetch,parsers,export_markdown,curate_markdown}.py`, `crawler/sites/nts_taxlaw.py`, `crawler/{agent,smart_finder,converter,generic_crawler}.py`, `data/fino_acct.db`, `plans/acct-rag-curation.md`, `docs/{status,next-session,rag-architecture,security-model}.md`.
**crawler-poc**: `data/papers.db`(NTS 20.6GB), `scripts/export_papers_md.py`.
**FINO BE**: `scripts/{build_exec_standard_index,build_accounting_standard_ndjson,nts_full_embed_index}.py`, `scripts/qna_corpus/*`.
**원본 디스크**: 회계 MD `/data_raid/ruci_workspace/acct_rag_data/`, 법/회계기준 MD `/data_raid/share/tax_data/current/`, QnA PDF `~/qna_corpus_work/`, NTS `crawler-poc/data/papers.db`.

---

## 9. 원문 링크 / 문서번호 citation 기능 (사용자 요청 2026-06-30)

> 사용자 요청: 답변 근거에 **원문 문서번호(예: "조심-2017-서-2912") + 원문 링크**를 노출. 사용자가 회계기준원 홈피에서 매번 재검색하거나 원문링크를 일일이 여는 불편 해소. **소비자: FINO FE + TEMIS**(TEMIS는 (a) FINO BE API 호출, 또는 (b) 회계 질의회신 데이터를 DB/인덱스에서 직접 취득 — 둘 다 확인됨). **공통 전제 = 원문 URL이 실제 DB/인덱스에 존재해야 함.**

### 트랙 1 — 판례·예규·심판례 (NTS): ✅ 즉시 가능 (크롤러 불필요)
- `fino-nts-v2`에 **이미 보유**: `document_number`(="심사-부가-2024-0049"), `reference_no`, `document_type`(판례), 작동 deep-link `url`(`https://taxlaw.nts.go.kr/pd/USEPDA002P.do?ntstDcmId=...`).
- 작업: FINO BE가 NTS 근거를 citation으로 낼 때 `document_number`(제목/라벨)+`url`(링크) 포함 → FE/TEMIS 렌더. **크롤러 작업 없음.**
- 전제 점검: 세무 답변이 실제로 NTS를 근거로 쓰는지 배선 확인(과거 우회 이력, FINO 메모리 `project_fino_tax_retriever_bypass_0615`).

### 트랙 2 — 회계 질의회신 (KASB/FSS): 🔴 frwaler 크롤러 보강이 선행 필수
- **현재 원문 URL 없음**(실측): `fino_acct.db` 회계 qna 868행의 `detail_url`이 **개별 deep-link가 아니라 목록 페이지 URL**(`fss.or.kr/.../list.do?...&pageIndex=N`, `#page=N`만 다름). 제목도 "금융감독원"으로 비어있음. KASB 217행 = 깨진 priority-01 배치(본문 unique 1개).
- 따라서 **"DB에 있으니 보강"이 아니라, 크롤러를 고쳐 개별 문서 deep-link를 새로 수집(재크롤)** 해야 함.
- 데이터 흐름(이 순서대로 해야 TEMIS/FE가 링크를 받음):
  ```
  frwaler fino_acct 크롤러 보강(목록→상세 진입, 개별 원문 URL 추출)
    → fino_acct.db detail_url 정상 기록(현재 목록URL)
    → FINO 회계/질의회신 ES 인덱스에 source_url 필드 추가 재빌드
    → FINO BE citation에 url 포함 → FINO FE + TEMIS 표시
  ```
- 크롤러 보강 구체:
  - FSS: 게시판 목록(`list.do`)에서 각 글 상세(`view.do?nttId=...` 류) URL 추출해 detail_url에 저장.
  - KASB: `allReplySummaryList.do` SPA에서 개별 항목 식별자/상세 URL 추출(+ priority-01 첨부 식별자 버그 동시 수리, `plans/acct-rag-curation.md` Task 6 / `plans/kasb-recrawl-repair.md`).
  - 매핑 키: 현 FINO QnA 인덱스의 `doc_no`/`title` ↔ 크롤 `external_id`/`title` 정합 확인(매칭 가능해야 기존 인덱스에 URL 주입 가능; 불가 시 크롤 산출로 QnA 코퍼스 재소싱 검토).

### 작업 순서 (권장)
1. **판례(NTS) 링크/번호 노출** — FINO BE+FE, 즉효. (frwaler 무관)
2. **회계 질의회신**: ① frwaler 크롤러 deep-link 수집 보강 → ② 인덱스 `source_url` 재빌드 → ③ FINO BE+FE/TEMIS 노출.

> 주의: **이 문서 추가 ≠ 링크 생성.** 실제 링크는 위 코드 작업으로 생기며, 문서는 다음 세션 인계용 계획임.
