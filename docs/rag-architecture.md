# Fino 법률 RAG 시스템 — 전체 아키텍처

국세법령정보시스템(taxlaw.nts.go.kr)의 판례·해석례 약 **292,219건**을 수집하여,
위키 형태로 열람 가능하게 만들고, LLM 요약 기반 벡터 검색 + Graph RAG를 결합한
**하이브리드 검색 시스템**을 구축한다.

> 참고 패턴: [Karpathy의 LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
> — "LLM이 원본을 반복 retrieval 하지 않고 정리해서 저장한다"는 아이디어를
> 법률 도메인 규모(30만 건)에 맞게 **벡터 검색 + 지식 그래프**로 확장.

---

## 0. 목차

1. [Vision & 목표](#1-vision--목표)
2. [전체 데이터 흐름](#2-전체-데이터-흐름)
3. [파일 시스템 구조](#3-파일-시스템-구조)
4. [DB 스키마](#4-db-스키마)
5. [Phase 1 — 크롤링](#5-phase-1--크롤링)
6. [Phase 2 — 위키 렌더링](#6-phase-2--위키-렌더링)
7. [Phase 3 — LLM 요약 & 구조화 추출](#7-phase-3--llm-요약--구조화-추출)
8. [Phase 4 — 임베딩](#8-phase-4--임베딩)
9. [Phase 5 — Graph RAG](#9-phase-5--graph-rag)
10. [Phase 6 — 하이브리드 검색 파이프라인](#10-phase-6--하이브리드-검색-파이프라인)
11. [Phase 7 — Wiki UI](#11-phase-7--wiki-ui)
12. [비용·시간 추산](#12-비용시간-추산)
13. [실행 순서](#13-실행-순서)
14. [오픈 이슈](#14-오픈-이슈)
15. [레퍼런스](#15-레퍼런스)

---

## 1. Vision & 목표

### 최종 형태

```
 사용자 질문: "1세대 1주택 비과세 요건에서 양도 당시 오피스텔 거주가 문제된 사례?"
   │
   ├─ 관련 판례 top-5 (정확도↑)
   ├─ 각 판례 → 위키 페이지 (원본 판결 전문 + 메타데이터 + 관련 법령 그래프)
   ├─ 법령 조문(소득세법 제89조) → 조문별 판례 타임라인 + 해석례 클러스터
   └─ 쟁점별 판례 군집 + 법리 변천 시각화
```

### 3개 축의 조합

| 축 | 역할 |
|----|------|
| **Wiki (원본 보존)** | 사이트에서 보던 그대로 — 표·이미지·스타일까지 |
| **Vector Search (의미 검색)** | "이런 질문" → LLM 요약 임베딩에서 찾기 |
| **Graph RAG (관계 탐색)** | 법령 ↔ 판례 ↔ 심급이력 ↔ 쟁점 주제어 |

---

## 2. 전체 데이터 흐름

```
                  [taxlaw.nts.go.kr 사이트]
                           │
                           │  (curl + JSON API)
                           ▼
      ┌────────────────────────────────────────┐
      │  Phase 1: 크롤링                       │
      │  (crawler/sites/nts_taxlaw.py)         │
      └────────────────────────────────────────┘
                           │
            ┌──────────────┼──────────────┐
            ▼              ▼              ▼
      [papers.db]     [_html/]      [_html/]
      (메타+본문)    판례 HTML      해석례 HTML
                     (DOC_ID명)     (DOC_ID명)
                           │
                           │  (export_papers_md.py)
                           ▼
      ┌────────────────────────────────────────┐
      │  Phase 2: 위키 파일 생성                │
      │   ├─ {문서번호}.md   (검색용)          │
      │   └─ {문서번호}.html (렌더링용, symlink)│
      └────────────────────────────────────────┘
                           │
                           │  (Gemini Flash / Haiku)
                           ▼
      ┌────────────────────────────────────────┐
      │  Phase 3: LLM 요약 & 구조화 추출        │
      │   ├─ summary_issue  (쟁점·결론·법리)    │
      │   └─ extracted_meta (당사자/조문/금액)  │
      └────────────────────────────────────────┘
                           │
                           │  (OpenAI / Cohere / BGE)
                           ▼
      ┌────────────────────────────────────────┐
      │  Phase 4: 임베딩                        │
      │   ├─ V1 요약 임베딩                     │
      │   ├─ V2 "이유" 섹션 청크 임베딩         │
      │   └─ V3 제목 임베딩                     │
      │          ↓                              │
      │       [sqlite-vec 가상 테이블]          │
      └────────────────────────────────────────┘
                           │
                           │  (엣지 추출)
                           ▼
      ┌────────────────────────────────────────┐
      │  Phase 5: Graph RAG 인덱스              │
      │   ├─ laws 테이블 (조문 노드)            │
      │   ├─ paper_law_edges (적용)             │
      │   ├─ paper_paper_edges (참조/심급)      │
      │   └─ topics 테이블 (주제어 노드)        │
      └────────────────────────────────────────┘
                           │
                           ▼
      ┌────────────────────────────────────────┐
      │  Phase 6: 검색 API                      │
      │   FTS5 + Vector + Graph + Reranker     │
      │    (api/main.py 확장)                  │
      └────────────────────────────────────────┘
                           │
                           ▼
      ┌────────────────────────────────────────┐
      │  Phase 7: Wiki UI (Next.js)             │
      │   ├─ 검색 페이지                        │
      │   ├─ 판례 상세 (HTML 원본 렌더)         │
      │   ├─ 법령 조문 뷰 (연관 판례 그래프)    │
      │   └─ 쟁점별 지식 지도                   │
      └────────────────────────────────────────┘
```

---

## 3. 파일 시스템 구조

### 디렉토리 레이아웃

```
crawler-poc/
├── data/
│   ├── papers.db                        # SQLite: 메타데이터·검색 인덱스·그래프
│   └── exports/
│       ├── nts-taxlaw-pd/               # 판례/결정례 (152,808건 예상)
│       │   ├── _html/                   # 원본 HTML (크롤러 직접 저장)
│       │   │   ├── 200000000000019651.html
│       │   │   └── …
│       │   ├── 대법원-2026-두-30102.md    # 검색용 (MD)
│       │   ├── 대법원-2026-두-30102.html  # → _html/…html (symlink)
│       │   └── …
│       └── nts-taxlaw-qt/               # 세법해석례 (139,411건 예상)
│           ├── _html/
│           ├── 기준-2024-법규소득-0211.md
│           ├── 기준-2024-법규소득-0211.html
│           └── …
├── crawler/
│   ├── sites/nts_taxlaw.py              # 크롤러 (curl + JSON API)
│   └── …
├── scripts/
│   ├── export_papers_md.py              # DB → MD/HTML export
│   ├── summarize_papers.py              # (Phase 3) LLM 요약
│   ├── embed_papers.py                  # (Phase 4) 임베딩
│   ├── build_graph.py                   # (Phase 5) 엣지 추출
│   └── …
├── api/main.py                          # FastAPI (검색/그래프 엔드포인트)
├── web/                                  # Next.js Wiki UI
└── docs/
    └── fino-rag-architecture.md (이 문서)
```

### 파일 역할 분담

| 파일 | 포맷 | 대상 | 용도 |
|------|------|------|------|
| `papers.db` | SQLite | 30만 rows | 메타·검색 인덱스·그래프·벡터 |
| `_html/{DOC_ID}.html` | HTML (원본) | 30만 파일 | 위키 렌더링용 원본, 표·이미지 보존 |
| `{문서번호}.md` | Markdown | 30만 파일 | 구조화 메타 + 플레인 본문 (임베딩/FTS5 입력) |
| `{문서번호}.html` | symlink → `_html/{DOC_ID}.html` | 30만 링크 | 사람이 읽기 쉬운 이름 |

**설계 원칙:**
- **HTML은 위키용**, **MD는 검색용** — 두 목적을 물리적으로 분리
- 크롤러는 DOC_ID 기반으로 저장 (일관성)
- export 스크립트가 사용자 친화적 이름(문서번호)으로 symlink 생성

---

## 4. DB 스키마

### 기존 (이미 있음)

```sql
CREATE TABLE papers (
    id TEXT PRIMARY KEY,              -- UUID
    site_id TEXT,                     -- 'nts-taxlaw-pd' / 'nts-taxlaw-qt'
    external_id TEXT,                 -- DOC_ID (예: 200000000000019651)
    title TEXT,
    authors TEXT,
    abstract TEXT,                    -- gist + content + html_body (플레인)
    category TEXT,                    -- 세법 분류 (예: '법인세')
    keywords TEXT,                    -- JSON array
    published_date TEXT,
    url TEXT,
    pdf_url TEXT,
    doi TEXT,
    department TEXT,
    metadata TEXT,                    -- JSON (문서번호, 관련법령, 심급이력, …)
    crawled_at TEXT,
    summary TEXT,                     -- ⚠️ 스키마만 존재, Phase 3에서 채움
    download_status TEXT,
    download_path TEXT
);
```

### 추가 (Phase 1~5에서 단계별 확장)

```sql
-- ============================================================
-- Phase 1~2: 크롤링 후 바로 추가할 인덱스
-- ============================================================

-- 문서번호 인덱스 (지금은 metadata JSON 안에 있어서 풀스캔)
ALTER TABLE papers ADD COLUMN doc_number TEXT
  GENERATED ALWAYS AS (json_extract(metadata, '$.documentNumber')) VIRTUAL;
CREATE INDEX idx_papers_doc_number ON papers(doc_number);
CREATE INDEX idx_papers_site_pub   ON papers(site_id, published_date);
CREATE INDEX idx_papers_category   ON papers(category);

-- 전문검색 (FTS5) — 키워드 검색용
CREATE VIRTUAL TABLE papers_fts USING fts5(
    title,
    abstract,
    summary,               -- Phase 3 채운 뒤 re-index
    content='papers',
    content_rowid='rowid',
    tokenize='unicode61 remove_diacritics 1'  -- 한국어 기본
);

-- 한국어 정확도 필요하면 별도 확장:
-- CREATE VIRTUAL TABLE papers_fts USING fts5(..., tokenize='icu ko');

-- ============================================================
-- Phase 3: LLM 요약·구조화
-- ============================================================

ALTER TABLE papers ADD COLUMN summary_issue TEXT;   -- 쟁점·결론·법리 (V1 임베딩 대상)
ALTER TABLE papers ADD COLUMN extracted_meta TEXT;  -- JSON: {당사자, 세목, 금액대, 조문, 쟁점카테고리}

-- ============================================================
-- Phase 4: 임베딩 (sqlite-vec 확장)
-- ============================================================

-- 요약 임베딩 (V1) - 개념 검색
CREATE VIRTUAL TABLE paper_vec_summary USING vec0(
    paper_id TEXT PRIMARY KEY,
    embedding FLOAT[1536]              -- OpenAI text-embedding-3-small 차원
);

-- 이유 섹션 청크 임베딩 (V2) - 세부 사실관계
CREATE TABLE paper_chunks (
    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id TEXT,
    chunk_idx INTEGER,
    text TEXT,
    FOREIGN KEY (paper_id) REFERENCES papers(id)
);
CREATE VIRTUAL TABLE chunk_vec USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding FLOAT[1536]
);
CREATE INDEX idx_chunks_paper ON paper_chunks(paper_id);

-- 제목 임베딩 (V3) - 짧은 쿼리
CREATE VIRTUAL TABLE paper_vec_title USING vec0(
    paper_id TEXT PRIMARY KEY,
    embedding FLOAT[1536]
);

-- ============================================================
-- Phase 5: Graph RAG
-- ============================================================

-- 법령 조문 노드 (중복 제거)
CREATE TABLE laws (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    law_name TEXT,                    -- "소득세법"
    article TEXT,                     -- "제89조"
    full_name TEXT UNIQUE             -- "소득세법 제89조"
);

-- 판례-법령 엣지 (APPLIES_TO)
CREATE TABLE paper_law_edges (
    paper_id TEXT,
    law_id INTEGER,
    PRIMARY KEY (paper_id, law_id),
    FOREIGN KEY (paper_id) REFERENCES papers(id),
    FOREIGN KEY (law_id)   REFERENCES laws(id)
);

-- 판례-판례 엣지 (REFERENCES / CITED_BY / SAME_CASE)
CREATE TABLE paper_paper_edges (
    from_id TEXT,                     -- 출발 판례
    to_id TEXT,                       -- 도착 판례
    edge_type TEXT,                   -- 'references' | 'cited_by' | 'same_case'
    PRIMARY KEY (from_id, to_id, edge_type)
);
CREATE INDEX idx_ppedge_to ON paper_paper_edges(to_id, edge_type);

-- 주제어 노드 (DISCUSSES)
CREATE TABLE topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE
);
CREATE TABLE paper_topic_edges (
    paper_id TEXT,
    topic_id INTEGER,
    PRIMARY KEY (paper_id, topic_id)
);
```

---

## 5. Phase 1 — 크롤링

### 5-1. 크롤러 구조 (`crawler/sites/nts_taxlaw.py`)

```
ASIPDI002PR01 (리스트 API)
  ↓ 페이지당 50건
  dcm 필드만 받음 (제목/문서번호/DOC_ID/등록일)
ASIQTB002PR01 (상세 API)
  ↓ DOC_ID 단위로 호출
  dcmDVO (54 필드) + dcmRltnStttList (관련법령) + trilPsagList (심급) + dcmHwpEditorDVOList (HTML 본문)
  ↓
  papers 테이블 upsert + _html/{DOC_ID}.html 파일 저장
```

### 5-2. 수집 항목 (현재 완료된 수준)

사이트 상세화면의 **거의 모든 데이터**를 수집:

| 필드 | papers 컬럼 | metadata JSON |
|------|-------------|---------------|
| 제목 | `title` | |
| 문서번호 | | `documentNumber` |
| 사건번호 | | `caseNumber` |
| DOC_ID | `external_id` | |
| 유형 (판례/심판/심사) | | `documentTypeName` |
| 결정유형 코드 | | `decisionClassCd` |
| 세법 분류 | `category` | |
| 생산일자 | `published_date` | |
| 귀속연도 | | `attrYr` |
| 심리결과 | | `reviewReason` |
| 전원합의체 여부 | | `supremeCourtAllAgmt` |
| 등록/수정일시 | | `firstRegDtm`, `lastAltDtm` |
| 등록기관 | | `inputOrgCd` |
| 키워드(주제어) | `keywords` | |
| 관련 법령 (조문 리스트) | | `relatedLaws` |
| 관련법령 주제어 | | `relatedTopics` |
| 심급 이력 | | `trialHistory` |
| 참조 판례 | | `referencedCases` |
| 인용 판례 | | `citedCases` |
| 첨부파일 | | `attachedFiles` |
| 요지 + 본문 (플레인) | `abstract` | |
| 원본 HTML 경로 | | `rawHtmlPath` |
| 상세 URL | `url` | |

**수집 안 함** (의도적 생략):
- HWP 바이너리 원본 — 필요시 `attachedFiles[].fileId`로 나중에 별도 다운로드
- PDF/인쇄 생성 — 사이트 기능일 뿐 데이터 아님

### 5-3. 문서번호 정확 조회 (별개 기능)

`ASEISA001MR01` (`searchType=document`) API로 문서번호 → DOC_ID 정확 조회. 
`lookup_by_doc_number()` 메서드로 크롤러에 통합됨.

`scripts/fino_search.py`의 `search_pass0()`이 같은 방식으로 `input.xlsx` 검색에 활용.

### 5-4. 크롤링 규모·시간·IP 차단 대응

| 항목 | 값 |
|------|-----|
| 판례/결정례 | 152,808건 |
| 세법해석례 | 139,411건 |
| **총 대상** | **292,219건** |
| 1건당 API 호출 | 2회 (list 1회 공유 + detail 1회) |
| 1건당 실소요 (delay=1s) | ~2~3초 |
| 병렬 2 프로세스 (pd+qt 동시) | **약 40~50시간** 예상 |
| 병렬 5 프로세스 (세목별 분할) | 약 10~20시간 |

**IP 차단 방지 전략:**
- delay=1.0s 기본값 유지 (초당 2건 이하)
- 병렬은 최대 2~5 프로세스 (동시 10 req/s 이하)
- 에러 3회 연속 시 exponential backoff (2→4→8→16s)
- 429/503/빈응답 패턴 감지 시 5분 pause
- 심야 시간대 활용 (23:00~06:00)

현재(2026-04-15) 10,000건 테스트 중 — 초기 관찰: 에러율 0.4%, 403/429 없음 → **2 병렬은 안전 확인**.

### 5-5. 분할 전략 옵션

**a) 세목별 분할 (추천)**

| 세목 | 코드 | 건수 |
|------|------|------|
| 원천세 | 307 | 34,811 |
| 양도소득세 | 306 | 31,380 |
| 부가가치세 | 308 | 21,913 |
| 종합소득세 | 305 | 21,549 |
| 법인세 | 303 | 19,595 |
| 국세징수 | 302 | 9,557 |
| 국세기본 | 301 | 4,231 |
| 인지세 | 311 | 3,234 |
| 기타 | 999 | 5,052 |
| 상속증여세 | 313 | 515 |
| 기타 소형 세목 | | 합 ~1,500 |

→ CLI 옵션 `--tax-law` 필요. 현재 코드에 아직 없음 (`--doc-type`만 있음).

**b) 문서유형별 분할**

- 001_08 (심판청구) 70K
- 001_09 (판례 법원) 54K
- 001_02 (질의회신) 132K
- 나머지 소분류

**c) 기간 분할**

- `--date-from`, `--date-to` 기존 옵션으로 연도 범위 쪼개기
- 최대 병렬도 높음 (10~20 batches 가능)

---

## 6. Phase 2 — 위키 렌더링

### 6-1. MD 파일 구조 (`scripts/export_papers_md.py`)

각 문서별로 섹션화된 MD 생성:

```markdown
# {제목}

## 메타데이터
- **문서번호**: ...
- **사건번호**: ...
- **DOC_ID**: ...
- **유형**: ...
- **결정유형코드**: ...
- **세법 분류**: ...
- **생산일자**: ...
- **귀속연도**: ...
- **심리결과**: ...
- **전원합의체 여부**: ...
- **상세 URL**: ...

## 키워드
...

## 관련 법령
- 소득세법 제88조
- 소득세법 제89조

## 관련법령 주제어
...

## 심급 이력
1. 조심-2022-서-2359
2. 서울행정법원-2022-구합-78968
3. 서울고등법원-2023-누-52958

## 참조 판례
...

## 인용 판례
...

## 첨부파일
...

## 본문
{gist} {content} {html_stripped_body}
```

→ 구조화되어 있어 LLM 프롬프트 투입·임베딩·FTS5 모두 재료로 활용.

### 6-2. HTML 파일 (위키 렌더링용)

- 크롤러가 `_html/{DOC_ID}.html` 에 원본 그대로 저장
- export 시 `{문서번호}.html` symlink 생성
- 표 (`<table class="sebeop_t">`), 인라인 이미지 (`data:image/png;base64,...`), 스타일 모두 보존
- Next.js에서 `dangerouslySetInnerHTML`로 sanitize 후 직접 렌더

### 6-3. Wiki URL 설계

```
/papers/{external_id}                   # 판례 상세 (HTML 원본 + 메타 카드)
/papers/doc/{문서번호}                  # 문서번호로 진입
/laws/{law_name}/{article}              # 법령 조문별 연관 판례
/topics/{topic_id}                      # 주제어별 판례 클러스터
/search?q=...                           # 하이브리드 검색
```

---

## 7. Phase 3 — LLM 요약 & 구조화 추출

### 7-1. 왜 LLM 요약인가

원본 전문을 그대로 임베딩하면 법률 도메인 특성상 노이즈가 많음:

- 판결문의 40%+ 는 boilerplate (사건/원고/피고/변론종결/판결선고/주문/청구취지)
- 개인정보 마스킹으로 `AAA`, `○○세무서장` 등이 반복
- 실질 판단은 "이 유" 섹션 안의 특정 문단에 집중

→ LLM이 "쟁점·결론·적용법리"를 추출해서 **고신호 요약**을 만들면 벡터 공간에서 구분력 폭증.

### 7-2. 추출 스키마

**(A) `summary_issue`** — 서술형 요약 (~300토큰, V1 임베딩 대상)

```
형식: 쟁점 / 결론 / 근거법리
예시:
  쟁점: 오피스텔의 사용 상태가 1세대 1주택 비과세 적용에 영향을 주는지 여부
  결론: 종전 주택 양도 당시 쟁점 오피스텔이 주거용으로 사용 가능한 상태였으면,
        1세대 1주택 요건을 충족하지 않음
  법리: 소득세법 제89조 및 시행령 제154조에 따른 "사실상 주거용" 판단
```

**(B) `extracted_meta`** — 구조화 JSON (V4 메타필터 대상)

```json
{
  "party_type": ["개인"],          // 개인 / 법인 / 비영리 / 외국인
  "tax_category": "소득세",        // 세목 분류 재확인
  "issue_category": ["비과세요건", "주택 판단"],
  "dispute_amount_krw": 103559530, // 분쟁 금액 (있으면)
  "period_years": [2021],          // 귀속 기간
  "applied_articles": [
    "소득세법 제88조", "소득세법 제89조",
    "소득세법 시행령 제154조"
  ],
  "outcome": "기각",               // 기각 / 인용 / 파기 / 환송 / 취하
  "is_precedent_citing_case": true,
  "is_decision_changed": false    // 선례 변경 여부
}
```

### 7-3. 프롬프트 전략

- **Few-shot**: 10건 정도 예시를 제공 (다양한 세목·유형 커버)
- **구조 강제**: JSON mode (OpenAI) 또는 schema-constrained 출력 (Gemini)
- **사실 보존 지침**: "중요 숫자·날짜·당사자 역할은 요약에 반드시 포함하라"
- **개인정보 처리**: "AAA, ○○세무서장 같은 마스킹 유지, 구체 금액은 보존"

### 7-4. 배치 실행

- **Gemini 2.0 Flash** 권장 — 한국어 법률 강함, 저렴
- OpenAI Batch API 같은 async 지원 모델이면 50% 할인
- 속도: 분당 ~100건 → 30만 건 = **약 2~3일** (병렬 rate limit 내에서)

---

## 8. Phase 4 — 임베딩

### 8-1. 다관점 임베딩 (누락 위험 최소화)

| 뷰 | 대상 텍스트 | 저장 테이블 | 검색 용도 |
|----|-------------|-------------|-----------|
| **V1** | `summary_issue` (쟁점·결론·법리) | `paper_vec_summary` | 개념·질문 검색 primary |
| **V2** | `abstract` 중 "주문 + 이유" 섹션 500토큰 청크 | `chunk_vec` + `paper_chunks` | 세부 사실관계 |
| **V3** | `title` | `paper_vec_title` | 짧은 쿼리 매칭 |

### 8-2. 모델 선택

| 모델 | 차원 | 한국어 | 가격 | 비고 |
|------|------|--------|------|------|
| OpenAI `text-embedding-3-small` | 1536 | 양호 | $0.02/1M tokens | 기본 선택 |
| OpenAI `text-embedding-3-large` | 3072 | 우수 | $0.13/1M tokens | 정확도 중시 |
| Cohere `embed-multilingual-v3` | 1024 | **우수** | $0.10/1M tokens | 한국어 벤치 1위권 |
| BAAI `bge-m3` | 1024 | 우수 | 무료 (로컬) | GPU 있으면 최선 |

### 8-3. 청크 전략 (V2 전용)

- 대상: `abstract` 중 **"주 문"** 이후부터 "결 론" 또는 끝까지
- 크기: 500 토큰 (~200~400 한국어 글자)
- 슬라이딩: overlap 100 토큰
- 평균 청크 수: 판결당 2~5개

### 8-4. 벡터 저장: `sqlite-vec`

```python
import sqlite_vec

conn = sqlite3.connect('data/papers.db')
conn.enable_load_extension(True)
sqlite_vec.load(conn)

# 검색 예시
cur.execute("""
  SELECT paper_id, distance
  FROM paper_vec_summary
  WHERE embedding MATCH ?
  ORDER BY distance
  LIMIT 50
""", [query_embedding.tobytes()])
```

### 8-5. 대안 (로컬 / 비-OpenAI)

- `BAAI/bge-m3` + `sentence-transformers` → 완전 로컬 실행 ($0, GPU 필수)
- `jhgan/ko-sroberta-multitask` → 한국어 특화 RoBERTa

---

## 9. Phase 5 — Graph RAG

### 9-1. 그래프 구조

**노드:**
- 판례 (papers 테이블)
- 법령 조문 (laws 테이블, 예: "소득세법 제89조")
- 주제어 (topics 테이블, 예: "1세대 1주택 비과세")
- 세목 (기존 category 필드로 표현)

**엣지:**

| 엣지 | 출발 | 도착 | 소스 |
|------|------|------|------|
| `APPLIES_TO` | 판례 | 법령 조문 | `metadata.relatedLaws` |
| `DISCUSSES` | 판례 | 주제어 | `metadata.relatedTopics`, `keywords` |
| `REFERENCES` | 판례 | 판례 | `metadata.referencedCases` |
| `CITED_BY` | 판례 | 판례 | `metadata.citedCases` (역방향) |
| `SAME_CASE` | 판례 | 판례 | `metadata.trialHistory` 체인 |
| `SAME_TAX_CATEGORY` | 판례 | 세목 | `papers.category` |

### 9-2. 엣지 추출 스크립트 (`scripts/build_graph.py`)

```python
# 의사 코드
for paper in papers:
    meta = json.loads(paper.metadata)
    
    # APPLIES_TO (관련 법령)
    for law_str in meta.get('relatedLaws', []):
        law_id = ensure_law(law_str)
        add_edge(paper.id, law_id, 'APPLIES_TO')
    
    # SAME_CASE (심급 이력)
    history = meta.get('trialHistory', [])
    for a, b in zip(history[:-1], history[1:]):
        a_id = lookup_paper_by_doc_number(a)
        b_id = lookup_paper_by_doc_number(b)
        if a_id and b_id:
            add_edge(a_id, b_id, 'SAME_CASE')
    
    # REFERENCES (참조 판례)
    for ref in meta.get('referencedCases', []):
        ref_id = lookup_paper_by_doc_number(ref)
        if ref_id:
            add_edge(paper.id, ref_id, 'REFERENCES')
    
    # DISCUSSES (주제어)
    for topic in meta.get('relatedTopics', []):
        topic_id = ensure_topic(topic)
        add_edge(paper.id, topic_id, 'DISCUSSES')
```

### 9-3. 그래프 활용 쿼리

**쿼리 1. 조문 → 관련 판례 연대표**
```sql
SELECT p.*, p.published_date
FROM papers p
JOIN paper_law_edges ple ON ple.paper_id = p.id
JOIN laws l ON l.id = ple.law_id
WHERE l.full_name = '소득세법 제89조'
ORDER BY p.published_date DESC;
```

**쿼리 2. 판례 → 심급 체인 (Recursive CTE)**
```sql
WITH RECURSIVE chain AS (
    SELECT from_id, to_id, 1 AS depth
    FROM paper_paper_edges WHERE from_id = ? AND edge_type='same_case'
    UNION ALL
    SELECT e.from_id, e.to_id, c.depth+1
    FROM paper_paper_edges e
    JOIN chain c ON e.from_id = c.to_id
    WHERE e.edge_type='same_case' AND c.depth < 10
)
SELECT * FROM chain;
```

**쿼리 3. 쟁점 → 판례 클러스터 + 법령 교차**
```sql
SELECT p.*, COUNT(DISTINCT l.id) AS law_count
FROM topics t
JOIN paper_topic_edges pte ON pte.topic_id = t.id
JOIN papers p ON p.id = pte.paper_id
LEFT JOIN paper_law_edges ple ON ple.paper_id = p.id
LEFT JOIN laws l ON l.id = ple.law_id
WHERE t.name = '1세대 1주택 비과세'
GROUP BY p.id
ORDER BY p.published_date DESC;
```

### 9-4. 고도화 (L4)

**Community Detection + Community Summary (Microsoft GraphRAG 스타일)**

1. 법령/주제어 기준으로 Louvain/Leiden 커뮤니티 탐지
2. 각 커뮤니티(예: "소득세법 제89조 + 1세대1주택 쟁점") 내 판례들을 LLM이 통합 요약
3. 커뮤니티 요약도 임베딩 → "이 쟁점 영역에 대한 전반적 입장"을 한 번에 검색

---

## 10. Phase 6 — 하이브리드 검색 파이프라인

### 10-1. 검색 흐름

```
사용자 쿼리
  │
  ├─ (선택) Query Expansion: LLM이 유사 질문 2~3개 생성
  ├─ (선택) HyDE: LLM이 "이런 판례라면 어떻게 쓰였을 것"이라고 가상 판례문 생성
  │
  ▼
  병렬 검색 4-way
  ┌─────────────────────┬─────────────────────┬─────────────────────┬─────────────────────┐
  │  (A) FTS5           │  (B) Vector V1+V2+V3 │  (C) Graph lookup   │  (D) Metadata filter│
  │  원본 키워드 매칭   │  요약 + 청크 + 제목  │  법령·조문·주제어   │  세목/연도/유형    │
  │  → top-20           │  → top-50           │  → 확정된 ID set    │  → 조건 교집합     │
  └─────────────────────┴─────────────────────┴─────────────────────┴─────────────────────┘
  │
  ▼
  Reciprocal Rank Fusion (RRF)
  → top-50
  │
  ▼
  Cross-Encoder Reranker (bge-reranker-v2-m3)
  쿼리 × 원본 본문 pair로 재평가 → 요약에서 놓친 내용 회수
  → top-10
```

### 10-2. RRF 구현

```python
def rrf_fusion(ranked_lists, k=60):
    scores = defaultdict(float)
    for lst in ranked_lists:
        for rank, doc_id in enumerate(lst):
            scores[doc_id] += 1 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])
```

### 10-3. Reranker

- `BAAI/bge-reranker-v2-m3` — 다국어 포함 한국어 우수
- CPU에서도 ms 단위 (top-50 재평가)
- GPU 있으면 더 빠름

### 10-4. API 엔드포인트 (`api/main.py` 확장)

```
GET  /search?q=...&mode=hybrid           # 하이브리드 검색
GET  /papers/{id}                        # 판례 상세 (메타 + 본문)
GET  /papers/{id}/html                   # 원본 HTML
GET  /papers/doc/{doc_number}            # 문서번호로 진입
GET  /papers/{id}/related?type=...       # 관계 기반 탐색

GET  /laws/{law_id}/papers               # 조문 → 관련 판례
GET  /topics/{topic_id}/papers           # 주제어 → 판례 클러스터
GET  /papers/{id}/trial_chain            # 심급 체인

POST /search/hyde                        # HyDE 기반 검색
POST /search/expand                      # Query expansion
```

---

## 11. Phase 7 — Wiki UI

### 11-1. 페이지 구성 (`web/`)

```
/                                          홈: 최신 판례, 인기 쟁점, 통계
/search?q=...                              검색 결과 (하이브리드 + 필터)
/papers/{id}                               판례 상세
    ├─ 메타 카드 (문서번호, 세법, 생산일자, 심리결과…)
    ├─ 본문 (HTML 원본 렌더 — 표·스타일 보존)
    ├─ 관련 법령 (조문 링크 → /laws/...)
    ├─ 심급 이력 (타임라인 시각화)
    ├─ 참조 판례 (링크 리스트)
    └─ 인용된 곳 (이 판례를 인용한 후속 판례)
/laws/{law_name}/{article}                 법령 조문 페이지
    ├─ 조문 원문 (나중에 법령 원문 크롤링 시)
    ├─ 이 조문을 적용한 판례 타임라인
    └─ 주요 해석례
/topics/{topic_id}                         쟁점별 지식 지도
    ├─ 쟁점 정의 (LLM 생성 개요)
    ├─ 주요 판례 클러스터
    ├─ 법리 변천 타임라인
    └─ 관련 조문
```

### 11-2. 판례 상세 페이지 렌더링

```tsx
// web/src/app/papers/[id]/page.tsx
import DOMPurify from 'isomorphic-dompurify';

export default async function PaperPage({ params }) {
  const paper = await api.getPaper(params.id);
  const rawHtml = await api.getPaperHtml(params.id);
  const safeHtml = DOMPurify.sanitize(rawHtml);

  return (
    <article>
      <h1>{paper.title}</h1>
      <MetadataCard data={paper.metadata} />
      <RelatedLaws laws={paper.metadata.relatedLaws} />
      <TrialHistoryTimeline chain={paper.metadata.trialHistory} />
      
      {/* 원본 HTML 그대로 렌더 */}
      <div 
        className="legal-doc"
        dangerouslySetInnerHTML={{ __html: safeHtml }}
      />
      
      <CitedBySection paperId={params.id} />
    </article>
  );
}
```

### 11-3. CSS 스타일링

```css
/* 판결문 원본 표 스타일 유지 */
.legal-doc table.sebeop_t {
  border-collapse: collapse;
  margin: 1em 0;
}
.legal-doc table.sebeop_t td {
  border: 1px solid #ccc;
  padding: 0.5em;
}

/* base64 이미지 최대 폭 제한 */
.legal-doc img {
  max-width: 100%;
  height: auto;
}
```

### 11-4. 검색 UI

- 상단: 하이브리드 검색창 (자동완성)
- 필터 사이드바: 세목, 기간, 유형(판례/심판/심사), 법원 등급
- 결과 카드별:
  - 제목 (하이라이트)
  - 문서번호, 생산일자, 세법
  - 쟁점 요약 (`summary_issue` 첫 줄)
  - 매칭 근거 (FTS/V1/V2 어디서 왔는지 뱃지)
  - 관련 법령 뱃지

---

## 12. 비용·시간 추산

### 12-1. 크롤링 (Phase 1)

| 항목 | 값 |
|------|-----|
| 대상 | 292,219건 |
| LLM 사용 | ❌ 없음 |
| API 비용 | ❌ 없음 (curl만) |
| 시간 | 2 병렬 ~40h / 5 병렬 ~15h |
| DB 크기 증가 | 약 1.5~2GB |
| HTML 파일 크기 | 약 2~3GB |

### 12-2. LLM 요약·구조화 (Phase 3)

| 모델 | 일회성 비용 (30만 건) |
|------|----------------------|
| Gemini 2.0 Flash | **~$40** |
| GPT-4o mini | ~$150 |
| Claude Haiku 4.5 | ~$250 |

- 입력 평균 2500토큰 (판결 전문 일부만 샘플링 가능) × 300K
- 출력 평균 300토큰 × 300K

### 12-3. 임베딩 (Phase 4)

| 모델 | 일회성 비용 |
|------|-------------|
| text-embedding-3-small (V1+V2+V3) | ~$13 |
| text-embedding-3-large | ~$80 |
| Cohere embed-multilingual-v3 | ~$65 |
| BGE-M3 로컬 | $0 (GPU 필요) |

### 12-4. 검색 런타임 (Phase 6)

| 항목 | 쿼리당 비용 |
|------|-------------|
| Query Embedding | ~$0.00004 |
| Query Expansion (Haiku) | ~$0.0003 |
| HyDE (Haiku) | ~$0.0005 |
| Reranker (로컬 CPU) | $0 |
| **쿼리당 총합** | **<$0.001** |

→ 월간 100,000 쿼리 가정해도 **$100 이하** 유지.

### 12-5. 총 구축 비용 (Gemini + OpenAI small 기준)

**~$55** (일회성) + 월 API 운영비 (쿼리 수 따라)

---

## 13. 실행 순서

### 완료 (2026-04-15 기준)

- [x] 크롤러 기본 구현 (`crawler/sites/nts_taxlaw.py`)
- [x] 문서번호 역검색 (17,171건 중 98.8% 복원, `fino_search.py`)
- [x] `document mode` API 통합 (`ASEISA001MR01`)
- [x] 크롤러 metadata 확장 (21개 필드)
- [x] Raw HTML 원본 파일 저장 (`_html/{DOC_ID}.html`)
- [x] MD/HTML 이중 export (`export_papers_md.py`)
- [x] 10K 테스트 크롤링 (진행 중) — 2병렬 안전 확인

### Phase 1 완료까지

- [ ] 1만 건 테스트 완료 후 차단 없음 재확인
- [ ] 크롤러에 `--tax-law` CLI 옵션 추가 (세목별 분할)
- [ ] 전체 29만 건 크롤링 실행 (세목별 5 병렬 권장)
- [ ] 인덱스 구축 (`doc_number` 가상 컬럼 + FTS5 vtable)

### Phase 2

- [ ] 전체 30만 건 MD/HTML export 실행
- [ ] 파일 크기/구조 검증

### Phase 3

- [ ] 샘플 100건으로 요약 프롬프트 튜닝 (다양한 세목·유형)
- [ ] JSON 스키마 확정 (`extracted_meta`)
- [ ] Gemini Batch API로 전체 요약
- [ ] `summary_issue`, `extracted_meta` 컬럼 적재
- [ ] FTS5 재인덱싱 (summary 포함)

### Phase 4

- [ ] `sqlite-vec` 설치 + 테이블 생성
- [ ] V1 요약 임베딩 배치
- [ ] V2 이유 섹션 청크 생성 + 임베딩
- [ ] V3 제목 임베딩
- [ ] 벡터 검색 성능 벤치 (latency, top-K 정확도)

### Phase 5

- [ ] `build_graph.py` 작성 — 엣지 추출
- [ ] `laws`, `topics` 노드 테이블 populate
- [ ] 엣지 테이블 populate
- [ ] 그래프 쿼리 검증 (조문→판례, 심급체인, 쟁점 클러스터)

### Phase 6

- [ ] FTS5 + Vector + Graph + Reranker 통합 파이프라인
- [ ] RRF 튜닝
- [ ] bge-reranker-v2-m3 로컬 로드
- [ ] API 엔드포인트 구현
- [ ] HyDE / Query Expansion 옵션

### Phase 7

- [ ] Next.js 상세 페이지 (HTML 렌더)
- [ ] 하이브리드 검색 UI
- [ ] 법령 조문 페이지
- [ ] 쟁점 지식 지도

---

## 14. 오픈 이슈

| 이슈 | 메모 |
|------|------|
| 한국어 FTS5 tokenizer | 기본(unicode61) 한계 있으면 `mecab-ko` / `icu` 확장 |
| 임베딩 모델 한국어 벤치 | 3-small vs Cohere v3 vs BGE-M3 실측 비교 필요 |
| HyDE on/off | 쿼리당 지연/비용 vs 정확도 향상 trade-off |
| 요약 프롬프트 | 개인정보 마스킹 유지, 숫자 보존, 법리 인용 형식 |
| 재크롤링 주기 | 신규 판례 일일 증분 (판례는 매일 1~10건 정도 갱신) |
| HWP 원본 | `attachedFiles[].fileId`로 배치 다운로드 + `hwp5txt` 변환 (이미 venv에 hwp5 있음) |
| 법령 원문 | 현재는 조문 이름만 수집. 법령 전문 따로 크롤링 여부 |
| 개인정보 마스킹 강화 | 판결문에 남아있는 부분 마스킹 재점검 |
| 소송비용/벌금 숫자 보존 | LLM 요약이 숫자 포기 않도록 프롬프트 보강 |
| GPU 가능성 | 로컬 reranker/embedding 속도 개선 |

---

## 15. 레퍼런스

### 외부 자료
- [Karpathy LLM Wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f)
- [sqlite-vec (Alex Garcia)](https://github.com/asg017/sqlite-vec)
- [bge-reranker-v2-m3](https://huggingface.co/BAAI/bge-reranker-v2-m3)
- [SQLite FTS5](https://www.sqlite.org/fts5.html)
- [HyDE paper](https://arxiv.org/abs/2212.10496)
- [Microsoft GraphRAG](https://microsoft.github.io/graphrag/)
- [Reciprocal Rank Fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)

### 내부 문서
- 문서번호 복원 실험 기록: `docs/fino-seach.md`
- 복원 결과: `docs/fino-seach-results.md`
- 미매치 분석: `docs/fino-seach-unmatched.md`
- API 탐색 기록: `docs/fino-seach-test.md`

### 코드 위치
- 크롤러: `crawler/sites/nts_taxlaw.py`
- 문서번호 검색: `scripts/fino_search.py`
- MD export: `scripts/export_papers_md.py`
- DB: `data/papers.db`
- FastAPI: `api/main.py`
- Next.js UI: `web/src/`
