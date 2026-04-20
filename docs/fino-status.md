# Fino 크롤링 프로젝트 — 현재 상태 & 다음 작업

> 이 문서는 세션이 끊겨도 이어서 작업할 수 있도록 현재 상태와 남은 작업을 정리한 것.
> 마지막 업데이트: 2026-04-20 (MD export 완료, --incremental 구현)

---

## 1. 프로젝트 개요

국세법령정보시스템(`taxlaw.nts.go.kr`)의 **판례·해석례 전체**를 수집하여
Wiki + 벡터 검색 + Graph RAG 시스템을 구축하는 프로젝트.

- 전체 아키텍처: `docs/fino-rag-architecture.md`
- API 탐색 기록: `docs/fino-seach.md`, `docs/fino-seach-test.md`

---

## 2. 현재 상태 (Phase 1: 크롤링)

### 2-1. DB

| 항목 | 값 |
|------|-----|
| 파일 | `data/papers.db` (SQLite) |
| 크기 | **8.7GB** |
| 총 건수 | **269,768건** |
| 목표 대비 | **92.3%** (292,219건 중) |
| 날짜 범위 | 1953-05-21 ~ 2026-04-13 |

### 2-2. 카테고리별 수집률

| 유형 | 수집 | 목표 | 달성 | 미수집 |
|------|------|------|------|--------|
| ✅ 적부 (001_05) | 501 | 501 | 100% | 0 |
| ✅ 기준 (001_03) | 1,011 | 1,011 | 100% | 0 |
| ✅ 고시 (001_04) | 13 | 13 | 100% | 0 |
| ✅ 사전 (001_01) | 4,929 | 4,945 | 100% | 16 |
| ✅ 이의 (001_06) | 1,454 | 1,470 | 99% | 16 |
| ✅ 헌재 (001_10) | 353 | 355 | 99% | 2 |
| ✅ 판례 (001_09) | 52,133 | 54,522 | 96% | 2,389 |
| ✅ 심판 (001_08) | 66,807 | 70,661 | 95% | 3,854 |
| ✅ 질의 (001_02) | 121,568 | 132,454 | 92% | 10,886 |
| ⚠️ 심사 (001_07) | 19,974 | 22,203 | 90% | 2,229 |
| ℹ️ 정비 | 975 | - | - | - |
| **합계** | **269,768** | **292,219** | **92.3%** | **~19,392** |

### 2-3. 파일 저장소

| 위치 | 내용 | 수량 |
|------|------|------|
| `data/papers.db` | SQLite (메타+본문+metadata JSON) | 269,768건 |
| `data/exports/nts-taxlaw-pd/_html/` | 판례 원본 HTML (DOC_ID.html) | 141,130개 |
| `data/exports/nts-taxlaw-qt/_html/` | 해석례 원본 HTML | 127,498개 |
| `data/exports/nts-taxlaw-pd/*.md` | 판례 MD (검색용) | **141,222개** ✅ |
| `data/exports/nts-taxlaw-qt/*.md` | 해석례 MD | **128,496개** ✅ |
| 합계 (HTML) | | 268,628개 / 5.8GB |
| 합계 (MD) | | 269,718개 ✅ |

### 2-4. 크롤러 코드

| 파일 | 역할 |
|------|------|
| `crawler/sites/nts_taxlaw.py` | 크롤러 본체 (curl + JSON API) |
| `crawler/base_crawler.py` | 공통 베이스 (upsert, retry) |
| `crawler/main.py` | CLI (`crawl`, `test` 서브커맨드) |
| `crawler/db.py` | SQLite upsert 헬퍼 |
| `scripts/fino_search.py` | 문서번호 역검색 (document mode + keyword fallback) |
| `scripts/export_papers_md.py` | DB → MD/HTML(symlink) export |
| `scripts/report_incomplete.py` | 불완전 수집 리포트 생성 |

### 2-5. 크롤러 특이사항 (세션 이어받기용)

- **TLS 변경됨**: `--tlsv1.2` 제거, `-skL` 사용 (2026-04-17 사이트 점검 후 변경)
- **JSON 에러 재시도**: `crawl()` 메서드에 5회 retry + page skip 로직 추가 (기존엔 즉시 종료)
- **document mode 검색**: `ASEISA001MR01` (`searchType=document`) — 문서번호 전용 검색 API 발견 및 통합
  - `ntstTlawClCdList=[]` (비어있어야 함, `['111']`이면 0건)
  - `sortField='SCORE/DESC'`
- **정규화**: `normalize_input()`에서 trailing hyphen 보존 (헌법재판소 사건번호 호환)
- **raw HTML 저장**: 크롤링 시 `_html/{DOC_ID}.html`에 원본 HTML 자동 저장
- **불완전 수집 로그**: 크롤링 시 `_incomplete.jsonl`에 자동 기록

---

## 3. 남은 작업 (TODO)

### 3-1. 즉시 실행 가능

#### ☐ 미수집 19K gap fill
세션 재시작 시 페이지 슬립으로 빠진 ~19,392건 보충.
각 카테고리 전체 재크롤링 (upsert 안전):

```bash
# 4 병렬
nohup .venv/bin/python -m crawler.main crawl nts-taxlaw-pd --doc-type 001_08 > .cache/fill_08.log 2>&1 & disown
nohup .venv/bin/python -m crawler.main crawl nts-taxlaw-pd --doc-type 001_09 > .cache/fill_09.log 2>&1 & disown
nohup .venv/bin/python -m crawler.main crawl nts-taxlaw-pd --doc-type 001_07 > .cache/fill_07.log 2>&1 & disown
nohup .venv/bin/python -m crawler.main crawl nts-taxlaw-qt --doc-type 001_02 > .cache/fill_02.log 2>&1 & disown
```

예상: ~20시간 (대부분 upsert skip, 신규만 insert)

#### ✅ 증분 크롤링 기능 (`--incremental`) — 구현 완료
`crawler/sites/nts_taxlaw.py` `crawl()` + `crawler/main.py` CLI 옵션 추가 완료.

```bash
# 사용법
.venv/bin/python -m crawler.main crawl nts-taxlaw-pd --incremental
.venv/bin/python -m crawler.main crawl nts-taxlaw-qt --incremental
```

로직:
1. 최신순 API 조회 (기존 `DCM_RGT_DTM/DESC` 정렬 활용)
2. 페이지 단위 DB 존재 여부 체크
3. 한 페이지(50건) 전부 기존 → STOP
4. 새 문서만 상세 fetch + 저장

#### ☐ 증분 크롤링 스케줄 (cron)

```bash
# /etc/crontab 또는 crontab -e
# 매일 새벽 2시 증분 크롤링
0 2 * * * cd /data_raid/ruci_workspace/crawler-poc && .venv/bin/python -m crawler.main crawl nts-taxlaw-pd --incremental >> .cache/incremental_pd.log 2>&1
0 2 * * * cd /data_raid/ruci_workspace/crawler-poc && .venv/bin/python -m crawler.main crawl nts-taxlaw-qt --incremental >> .cache/incremental_qt.log 2>&1
```

#### ✅ 전체 MD export — 완료
- pd: 141,222개 MD / qt: 128,496개 MD (총 269,718개)

#### ✅ 불완전 수집 리포트 갱신 — 완료
- 33,298건 불완전 감지 (pd: 16,650 / qt: 16,598 / 기타: 50)
- 결과: `docs/fino-incomplete-report.md`

---

### 3-2. Phase 2~7 (아키텍처 문서 참조: `docs/fino-rag-architecture.md`)

| Phase | 작업 | 상태 |
|-------|------|------|
| **Phase 2** | 위키 렌더링 (MD/HTML export) | ✅ MD export 완료 (269,718건) |
| **Phase 3** | LLM 요약 + 구조화 추출 | 미시작 |
| **Phase 4** | 임베딩 (sqlite-vec) | 미시작 |
| **Phase 5** | Graph RAG 인덱스 | 미시작 |
| **Phase 6** | 하이브리드 검색 API | 미시작 |
| **Phase 7** | Wiki UI (Next.js) | 미시작 |

각 Phase 상세: `docs/fino-rag-architecture.md` 참조.

---

## 4. DB 스키마 (papers 테이블)

```sql
CREATE TABLE papers (
    id              TEXT PRIMARY KEY,
    site_id         TEXT NOT NULL,       -- 'nts-taxlaw-pd' / 'nts-taxlaw-qt'
    external_id     TEXT,                -- DOC_ID
    title           TEXT,
    authors         TEXT,                -- JSON []
    abstract        TEXT,                -- gist + content + HTML stripped body
    category        TEXT,                -- 세법분류 (법인세, 양도소득세, …)
    keywords        TEXT,                -- JSON array
    published_date  TEXT,
    url             TEXT,
    pdf_url         TEXT,
    doi             TEXT,
    department      TEXT,
    metadata        TEXT,                -- JSON (아래 상세)
    crawled_at      TIMESTAMP,
    summary         TEXT,                -- Phase 3에서 채울 LLM 요약
    download_status TEXT,
    download_path   TEXT,
    UNIQUE(site_id, external_id)
);
```

### metadata JSON 필드 (21개)

```json
{
  "documentNumber": "대법원-2026-두-30102",
  "documentTypeName": "판례",
  "replyReference": "",
  "fileId": "300000000001180709",
  "sourceOrgCode": "54",
  "relatedLaws": ["국세기본법 제39조"],
  "trialHistory": ["수원지방법원-2024-구합-63374", "…", "대법원-2026-두-30023"],
  "referencedCases": [],
  "citedCases": [],
  "relatedTopics": [],
  "attachedFiles": [],
  "rawHtmlPath": "data/exports/nts-taxlaw-pd/_html/200000000000019630.html",
  "attrYr": 2017,
  "decisionClassCd": "10",
  "reviewResultCd": "04",
  "reviewReason": "승인완료",
  "supremeCourtAllAgmt": "N",
  "caseNumber": "대법원-2026-두-30023",
  "attachedFileId": "300000000001180710",
  "firstRegDtm": "20260406094227",
  "lastAltDtm": "20260406",
  "inputOrgCd": "28137"
}
```

---

## 5. API 정보 (taxlaw.nts.go.kr)

### 엔드포인트

| API | Action ID | 용도 |
|-----|-----------|------|
| 리스트 검색 | `ASIPDI002PR01` | 키워드/날짜/카테고리 필터 페이지네이션 |
| 상세 조회 | `ASIQTB002PR01` | DOC_ID → 전체 메타+본문+심급이력 |
| 문서번호 검색 (document mode) | `ASEISA001MR01` | `searchType=document` → 문서번호 필드만 매치 |

### 중요 파라미터 (document mode)

```json
{
  "schVcb": "검색어",
  "searchType": "document",
  "sortField": "SCORE/DESC",
  "ntstTlawClCdList": [],      // 반드시 빈 배열!
  "prtsAttrYrCtl": [],         // 반드시 빈 배열!
  "prtsSprcChiefJdgmYn": "",   // 빈 문자열!
  "collection": "question"     // 또는 "precedent"
}
```

### TLS 주의

```bash
# ❌ 2026-04-17 이후 작동 안 함
curl --tlsv1.2 ...

# ✅ 현재 동작
curl -skL ...    # 기본 TLS + redirect follow
```

### 카테고리 코드

```
PD (nts-taxlaw-pd):
  001_05: 과세적부  001_06: 이의신청  001_07: 심사청구
  001_08: 심판청구  001_09: 판례      001_10: 감사원 심사
  003_01: 기타 판결례

QT (nts-taxlaw-qt):
  001_01: 사전답변   001_02: 질의회신  001_03: 과세기준자문
  001_04: 고시서면질의  002_01: 기타
```

---

## 6. 크롤링 CLI 사용법

```bash
# 기본 크롤링 (전체)
.venv/bin/python -m crawler.main crawl nts-taxlaw-pd
.venv/bin/python -m crawler.main crawl nts-taxlaw-qt

# 카테고리 지정
.venv/bin/python -m crawler.main crawl nts-taxlaw-pd --doc-type 001_08

# 날짜 범위 지정
.venv/bin/python -m crawler.main crawl nts-taxlaw-qt --doc-type 001_02 --date-from 1999-01-01 --date-to 2006-12-31

# 건수 제한
.venv/bin/python -m crawler.main crawl nts-taxlaw-pd --limit 100

# 증분 크롤링 (TODO: 구현 필요)
.venv/bin/python -m crawler.main crawl nts-taxlaw-pd --incremental
```

---

## 7. 문서번호 검색 사용법

```python
# 크롤러 내장 메서드
from crawler.sites.nts_taxlaw import NTSTaxlawPdCrawler, NTSTaxlawQtCrawler
import sqlite3
conn = sqlite3.connect('data/papers.db')
c = NTSTaxlawPdCrawler(db_conn=conn)
result = c.lookup_by_doc_number('감심-1994-0168')
# → {'doc_id': '...', 'title': '...', 'type': '심사', 'category': '부가가치세', 'published_date': '1994-10-04'}

# 배치 검색 (scripts/fino_search.py)
.venv/bin/python scripts/fino_search.py --reset --workers 4
# Pass0 (document mode) → Pass1 (keyword) → Pass2 (pagination) → Pass3 (deep)
```

---

## 8. 관련 문서 목록

| 문서 | 내용 |
|------|------|
| `docs/fino-rag-architecture.md` | 전체 RAG 아키텍처 (Phase 1~7, 비용, 기술 스택) |
| `docs/fino-status.md` | **이 문서** — 현재 상태 & TODO |
| `docs/fino-seach.md` | API 탐색 기록 (감심 판례 검색) |
| `docs/fino-seach-test.md` | 가능성 테스트 (4건 샘플) |
| `docs/fino-seach-results.md` | 문서번호 역검색 결과 (16,962건) |
| `docs/fino-seach-unmatched.md` | 역검색 미매치 (209건) |
| `docs/fino-incomplete-report.md` | 불완전 수집 리포트 (품질 점검) |

---

## 9. 빠른 시작 (세션 이어받기)

```bash
cd /data_raid/ruci_workspace/crawler-poc

# 1. 현재 상태 확인
.venv/bin/python -c "
import sqlite3
c=sqlite3.connect('data/papers.db').cursor()
total = c.execute(\"SELECT COUNT(*) FROM papers WHERE site_id LIKE 'nts-taxlaw%'\").fetchone()[0]
print(f'DB: {total:,}건')
"

# 2. 실행 중인 크롤러 확인
ps aux | grep "crawler.main crawl" | grep -v grep

# 3. 사이트 상태 확인
curl -sk --max-time 10 -o /dev/null -w "HTTP %{http_code}\n" "https://taxlaw.nts.go.kr/"

# 4. 이 문서 읽기
cat docs/fino-status.md
```
