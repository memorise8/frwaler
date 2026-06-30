# FINO 세법 코퍼스 수집기 (`crawler/fino_law/`) — 설계 스펙

> 작성 2026-06-30. 브레인스토밍 합의안. 상위 맥락: `docs/2026-06-30_fino_corpus_master_handoff.md` 섹션 9(citation 원문링크 기능). 메모리: `fino-law-corpus-collection-endpoints`.

## 1. 목표 / 비목표

**목표** — FINO RAG 답변 근거에 **원문 문서번호 + 조문 단위 원문 deep-link**를 노출할 수 있도록, 세법 법령과 조문형 세법집행기준을 수집해 FINO BE 인덱스 빌드 입력(MD + NDJSON)으로 제공한다.

**비목표 (이 스펙 범위 밖)**
- 회계 질의회신(KASB/FSS) deep-link 재크롤 — 별도 트랙 A (`plans/kasb-recrawl-repair.md`).
- NTS papers.db 일원화 — 별도 트랙 B (파일 이전).
- 판례·예규 수집 — 기존 `nts_taxlaw.py` + papers.db가 보유.

## 2. 범위 (합의된 결정)

| 항목 | 결정 |
|------|------|
| 법령 범위 | 핵심 국세 + 지방세 (명시 리스트, §4) |
| 법령 버전 | **현행만** (연혁은 next-level, 스키마는 확장 대비) |
| 집행기준 | **조문형 세법집행기준만** = 권위 인덱스 (PDF 책자는 Phase 2 별도 tier) |
| 출력 | `fino_law.db`(정본) → **MD + NDJSON 둘 다** export, 조문별 `source_url` 포함 |
| PDF 책자 | 수집은 가능하나 **이 스펙 비포함**. Phase 2에서 별도 source tier로(권위 인덱스와 분리) |

## 3. 아키텍처

`crawler/fino_acct/`와 동일 패턴의 신규 형제 모듈 `crawler/fino_law/`.

```
crawler/fino_law/
  sources.py          # 수집 대상 레지스트리 (법령 리스트 + 집행기준 세목코드)
  models.py           # 데이터클래스 (LawDocument, LawArticle, ExecStandardDoc, ExecStandardArticle)
  db.py               # SQLite 스키마/연결 (fino_law.db)
  fetch_law.py        # law.go.kr OpenAPI 클라이언트 (httpx)
  fetch_exec.py       # taxlaw.nts.go.kr action.do 클라이언트 (curl, nts_taxlaw 패턴 재사용)
  parsers.py          # 조문 파싱 (law JSON 조문단위 / exec 뷰어 응답)
  collect.py          # 오케스트레이션 (CLI 진입점: --law / --exec / --all)
  export_markdown.py  # 법령/집행기준당 1 MD (exec_standard 호환)
  export_ndjson.py    # 조문당 1 레코드 + source_url
```

**재사용**: OC 키 `fino-law-data`, `law-search-mcp`의 httpx 호출 형태(참조만), `crawler/sites/nts_taxlaw.py`의 curl `action.do` POST 메커니즘(`--tls-max 1.3`, 3회 재시도).

## 4. C-1 — 세법 법령 (law.go.kr OpenAPI)

### 대상 레지스트리 (`sources.py`)
- **국세**: 국세기본법, 국세징수법, 법인세법, 소득세법, 부가가치세법, 상속세 및 증여세법, 조세특례제한법, 종합부동산세법, 국제조세조정에 관한 법률, 개별소비세법, 교육세법, 농어촌특별세법, 증권거래세법, 인지세법, 주세법 — **각 + 시행령 + 시행규칙**.
- **지방세**: 지방세기본법, 지방세징수법, 지방세법, 지방세특례제한법 — **각 + 시행령 + 시행규칙**.
- 각 항목은 `(법령명, 구분)` 으로 정의. 구분 = 법률/시행령/시행규칙.

### 수집 흐름
1. `lawSearch.do?target=law&query={법령명}` → 결과 중 `현행연혁코드=='현행'` & 법령명 정확 일치 1건 선택 → `법령ID`, `MST`, `소관부처`, `공포일자`, `시행일자`.
2. `lawService.do?target=law&MST={MST}` → `법령.조문.조문단위[]` 파싱 → 조문번호/조문제목/조문내용/(항·호).
3. `fino_law.db`에 저장 (§6).

### citation URL
- 법령 단위: `https://www.law.go.kr/법령/{법령명}`
- 조문 단위: 위 URL + 조문 앵커(가능 시). 최소 법령 URL + 조문번호 라벨 보장.

## 5. C-2 — 조문형 세법집행기준 (taxlaw.nts.go.kr)

### 발견된 엔드포인트 (정찰 2026-06-30)
- 화면 `/st/USESTE002M.do` (세법집행기준), 뷰어 `/st/USESTA002P.do?ntstBscId=…&ntstBrkdId=…`.
- action: `ASISTZ001MR01`(법령/집행기준 select), `ASISTZ002MR01`(ntstBscId→ntstBrkdId).
- 세목 분류 `ntstSjtClCd` (action `ASIELA001MR02`): 법인=05, 부가=07, 소득=09 … 총 22개.

### Step 1 (필수 선행) — 조문 deep-link 실증
구현 착수 전, 조문형 세법집행기준 **1개 세목에서 조문 1건의 본문 + 안정적 deep-link(`USESTA002P.do?ntstBscId=…`)를 실제로 추출**해 확인한다. 이 결과로 `fetch_exec.py`/`parsers.py`의 정확한 paramData·응답 구조를 확정. (실패 시: 조문형 접근이 막히면 사용자에게 보고 후 C-2 보류, C-1만 진행.)

### 수집 흐름 (Step 1 확정 후)
1. 세목별(`ntstSjtClCd`) 집행기준 목록 조회 → 각 집행기준 문서의 `ntstBscId`.
2. 뷰어 action으로 조문 트리/본문 추출 → 조문번호·제목·본문·deep-link.
3. `fino_law.db` exec_standard 테이블에 저장 (§6, 법령과 동일 모양).

## 6. 데이터 모델 (`fino_law.db`)

citation 중심. 법령과 집행기준은 같은 모양(조문형)이라 동일 구조 공유, `source_kind`로 구분.

- `documents`: `id`, `source_kind`(law|exec_standard), `external_id`(법령ID 또는 ntstBscId), `title`(법령명/집행기준명), `category`(구분 또는 세목), `org`(소관부처), `promulgated_at`, `effective_at`, `version_code`(현행), `source_url`, `collected_at`.
- `articles`: `id`, `document_id`(FK), `article_no`(조문번호), `article_title`, `body_text`, `clause_json`(항·호), `source_url`(+조문 앵커), `seq`.
- 연혁 확장 대비: `version_code`/`effective_at` 보유 → 스키마 변경 없이 연혁 추가 가능.

## 7. 출력 (export)

- `export_markdown.py`: 문서(법령/집행기준)당 1 MD. 조문 섹션화, frontmatter에 `external_id`·`org`·`effective_at`·`source_url`. FINO BE `exec_standard_sources.py` 레지스트리 → `build_exec_standard_index.py` 입력 호환.
- `export_ndjson.py`: 조문당 1 레코드(`{doc_title, article_no, body_text, source_url, source_kind, ...}`). 인덱스 빌드 직접 입력용.
- **PDF 책자(Phase 2)는 별도 tier** — 권위 인덱스(법령+조문형 집행기준)와 동일 인덱스에 합치지 않음.

## 8. 에러 처리 / 견고성

- law.go.kr: 호출 간 delay, timeout 3회 재시도, 단일결과 `dict→list` 정규화(prec/law API 특성), 현행 미매칭 시 skip+로그.
- taxlaw: `nts_taxlaw.py`의 curl 재시도(빈 응답 3회) 패턴 재사용, `--tls-max 1.3`.
- 멱등성: `external_id` 기준 upsert (재실행 시 중복 생성 금지).

## 9. 테스트 / 수용 기준

- **단위 테스트**(오프라인 fixture, `tests/test_fino_law_*.py`): 법령 JSON·집행기준 응답 샘플로 파서 검증. `test_fino_acct_collector.py` 방식.
- **수용 기준**:
  - 각 대상 법령 → 조문 N>0, `source_url` 존재, 현행만.
  - 집행기준(C-2) → 세목별 조문 N>0, 조문별 deep-link 존재(목록 URL 아님).
  - export: MD/NDJSON 둘 다 생성, 모든 레코드에 `source_url`.
  - 멱등: 2회 실행 시 row 수 불변.

## 10. 단계 (구현 순서)

```
C-1.  세법 법령 수집 (law.go.kr OpenAPI)          ← 0 리스크, 먼저
C-2a. 조문형 집행기준 deep-link 실증 (Step 1)       ← 게이트
C-2b. 조문형 집행기준 수집 (게이트 통과 시)
EXP.  export MD + NDJSON (법령·집행기준 공통)
Phase2 (별도/후순위). PDF 책자 별도 tier 수집·인덱스 분리
```

## 11. Open Questions

- [ ] FINO BE `exec_standard_sources.py`가 받는 정확한 MD/NDJSON 스키마 — 최종 export 포맷은 BE와 합의 필요(현재는 handoff §5 기반 추정).
- [ ] 조문형 집행기준 조문 앵커가 deep-link로 직접 가능한지(아니면 문서 단위 링크 + 조문 라벨) — Step 1에서 확정.
- [ ] taxlaw.nts.go.kr robots.txt/약관 대량 수집 허용 범위.
