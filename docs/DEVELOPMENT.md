# 크롤러 시스템 개발 문서

## 1. 프로젝트 개요

### 목적
각국 정부/연구기관 사이트에서 문서를 자동 수집하는 다국가 웹 크롤링 플랫폼입니다. URL 입력만으로 모든 다운로드 가능 문서를 자동 발견하고, JSON 설정 기반 크롤러로 구조화된 데이터를 추출합니다.

### 기술 스택
- **백엔드**: Python + FastAPI (async/await)
- **프론트엔드**: Next.js 14 (TypeScript)
- **데이터베이스**: SQLite + aiosqlite
- **크롤링**: Playwright (브라우저), requests (HTTP), cloudscraper (WAF 우회)
- **파일 처리**: PDF, HWP, HWPX, XLSX, XLS, CSV, DOCX, DOC, PPTX, PPT 등
- **AI 분석**: OpenAI API (GPT-5.4-mini) - 선택적

### 핵심 특징
- **Smart Document Finder**: URL 하나로 전체 다운로드 파일 자동 발견
- **GenericCrawler**: JSON 설정 기반 유연한 크롤링
- **Intelligent Retry**: 429/5xx 자동 재시도 + 지수 백오프
- **robots.txt 준수**: 서버 부하 최소화
- **중복 감지**: URL + 콘텐츠 해시 기반
- **AJAX API 탐지**: XHR/fetch 요청 자동 분석
- **다국가 지원**: 한국, 독일, 영국, 호주, 미국

---

## 2. 시스템 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│                    사용자 (브라우저)                         │
└────────────────────────┬────────────────────────────────────┘
                         │
                    :30003 HTTP
                         │
        ┌────────────────▼─────────────────┐
        │   Next.js Frontend (FE)          │
        │  - 12개 페이지 (라우팅)           │
        │  - 실시간 UI 업데이트             │
        │  - API 클라이언트 (lib/api.ts)   │
        └────────────────┬─────────────────┘
                         │
                    :30004 HTTP
                    (Next.js rewrite)
                         │
        ┌────────────────▼─────────────────────────┐
        │    FastAPI Backend (BE)                  │
        │  - 13개 라우터 (API 엔드포인트)          │
        │  - 비동기 작업 관리 (JobManager)         │
        │  - 인증 및 rate limiting                │
        └────────────┬──────────────┬──────────────┘
                     │              │
           ┌─────────▼──────┐   ┌───▼──────────┐
           │  Crawler Engine │   │  SQLite DB   │
           │  ┌──────────┐   │   │              │
           │  │SmartFinder   │   │  papers.db   │
           │  ├──────────┤   │   │ (sites,      │
           │  │GenericCrawl  │   │  papers,     │
           │  ├──────────┤   │   │  jobs, ...)  │
           │  │BaseCrawler   │   │              │
           │  ├──────────┤   │   └──────────────┘
           │  │NetworkCapture│  (AJAX 탐지)
           │  └──────────┘   │
           └─────────────────┘
                     │
        ┌────────────▼────────────┐
        │  외부 리소스             │
        │  ├─ OpenAI API          │
        │  │  (Auto-Add 분석)     │
        │  ├─ 정부/연구기관 웹사이트
        │  └─ robots.txt/sitemap │
        └─────────────────────────┘
```

### 데이터 흐름
1. **사용자 입력** → FE 페이지 (`/smart-find`, `/sites/[id]`)
2. **API 호출** → BE 라우터 (FastAPI)
3. **크롤링 엔진** → URL 분석 및 문서 수집
4. **DB 저장** → SQLite (papers.db)
5. **결과 반환** → FE 실시간 업데이트

---

## 3. 주요 기능 상세 설명

### 3.1 Smart Document Finder (핵심 기능)

SmartDocumentFinder는 URL만으로 모든 다운로드 가능한 문서를 자동으로 발견합니다.

**작동 원리**:

```
1. URL 입력
    ↓
2. 초기 페이지 로드
    ├─ requests 시도 (빠름)
    ├─ cloudscraper 시도 (WAF 우회)
    └─ Playwright 브라우저 (JavaScript 렌더링)
    ↓
3. 다운로드 링크 감지 (3가지 패턴)
    ├─ 직접 파일 링크 (.pdf, .hwp, .xlsx 등)
    ├─ onclick 핸들러 (JavaScript 다운로드)
    └─ AJAX API 엔드포인트
    ↓
4. 재귀적 페이지 탐색
    ├─ 페이지네이션 자동 감지 (page, pageNo, offset 등)
    ├─ 깊이 설정 (1~5, 기본 3)
    └─ 방문한 URL 추적 (무한 루프 방지)
    ↓
5. 특수 패턴 감지
    ├─ iframe 내 PDF
    ├─ 숨겨진 다운로드 폼
    └─ JavaScript onclick 인터셉트
    ↓
6. 결과 반환
    └─ 발견된 파일 목록 (제목, URL, 확장자, 크기)
```

**설정 가능 옵션**:
```python
finder = SmartDocumentFinder()
result = finder.find(
    url="https://example.go.kr/board/list.do",
    max_pages=10,        # 최대 탐색 페이지
    max_depth=3,         # 최대 깊이
    use_browser=False,   # Playwright 사용 여부
    filter_by_type=None  # 파일 타입 필터 (pdf, hwp, xlsx)
)
```

**지원 파일 확장자**:
```
문서: .pdf, .hwp, .hwpx, .docx, .doc, .txt, .rtf, .odt
스프레드시트: .xlsx, .xls, .csv, .ods
프레젠테이션: .pptx, .ppt
데이터: .json, .xml, .zip, .rar
```

**다운로드 링크 감지 패턴**:
```python
# 한글/영글 텍스트 패턴
DOWNLOAD_TEXT_PATTERNS = [
    '다운로드', '내려받기', '첨부파일', '보고서', '원문',
    'download', 'Download', 'DOWNLOAD', ...
]

# URL 패턴
DOWNLOAD_URL_PATTERNS = [
    r'download', r'attach', r'fileDown', r'getFile',
    r'cmm/fms/FileDown', r'pdfView', r'hwpView', ...
]

# 페이지네이션 매개변수
PAGE_PARAMS = [
    'page', 'pageIndex', 'pageNo', 'pg', 'p', 'offset', ...
]
```

### 3.2 GenericCrawler (설정 기반 크롤링)

JSON 설정 파일로 사이트별 크롤링 규칙을 정의합니다.

**설정 파일 구조** (`crawler/sites/configs/site_id.json`):

```json
{
  "site_id": "germany-bmbf-publications",
  "site_name": "BMBF Publications",
  "base_url": "https://www.bmbf.de/bmbf/de/forschung/publikationen/index.html",
  "type": "list-detail",
  "options": {
    "delay": 1.5,
    "respect_robots": true,
    "verify_ssl": true,
    "fetch_method": "requests"
  },
  "list_page": {
    "url": "https://www.bmbf.de/...",
    "selector": "div.publication-item",
    "pagination": {
      "next_button": "a.next-page",
      "param": "page",
      "start": 1
    }
  },
  "detail_page": {
    "selectors": {
      "title": "h1.title",
      "pdf_link": "a.download-pdf",
      "date": "span.publish-date",
      "description": "div.summary"
    }
  }
}
```

**지원하는 crawler 타입**:

1. **list-detail**: 목록 → 상세 페이지 패턴
   - 목록 페이지에서 항목 추출
   - 각 항목의 상세 페이지 방문
   - CSS 셀렉터로 데이터 추출

2. **single-page**: 단일 페이지 패턴
   - 한 페이지에 모든 데이터 존재
   - 페이지네이션 지원

**fetch_method 옵션**:
- `requests`: 일반 HTTP 요청 (기본, 빠름)
- `cloudscraper`: CloudFlare WAF 우회
- `browser`: Playwright 브라우저 (SPA, JavaScript 렌더링)

### 3.3 크롤링 인프라

#### robots.txt 준수
```python
# BaseCrawler에서 자동 처리
if not self._respect_robots:
    return  # 비활성화 가능

robot_parser = RobotFileParser()
robot_parser.set_url(f"{domain}/robots.txt")
robot_parser.read()

if not robot_parser.can_fetch(self.USER_AGENT, url):
    # 접근 거부 - 스킵
    continue
```

#### 지수 백오프 재시도
```python
# 자동 재시도 전략
- 429 (Too Many Requests): 즉시 재시도
- 5xx (Server Error): 최대 3회 재시도
- 백오프 계산: delay * (2 ^ attempt_count)

예: 1초 기본 delay
  시도 1: 1초 대기 후 재시도
  시도 2: 2초 대기 후 재시도
  시도 3: 4초 대기 후 재시도
```

#### 동적 Rate Limiting
```python
# 성공률에 따라 delay 자동 조정
- 연속 성공 시: delay 감소 (min_delay로 하한)
- 실패 시: delay 증가 (max_delay로 상한)
- min_delay: 초기 delay
- max_delay: min(delay * 10, 30초)
```

#### 중복 감지
```python
# 두 가지 방식으로 중복 감지
1. URL 해시: 동일 URL은 스킵
2. 콘텐츠 해시: 콘텐츠가 동일하면 스킵
   - SHA-256(content) 비교
   - 같은 파일이 다른 URL로 배포되는 경우 감지
```

#### AJAX API 탐지
```python
# NetworkCapture (network_capture.py)
- Playwright로 페이지 로드
- 모든 XHR/fetch 요청 인터셉트
- JSON/XML API 응답 분석
- API 엔드포인트 자동 추출

예:
  GET /api/publications?page=1&limit=20 → JSON 응답
  → GenericCrawler 설정에서 자동 이용 가능
```

### 3.4 파일 처리

#### 파일 다운로드 및 변환
```python
# summarizer.py에서 처리
1. URL에서 파일 다운로드
2. 확장자/Content-Type으로 파일 타입 감지
3. 텍스트 추출:
   - PDF → pdfplumber로 텍스트 추출
   - HWP/HWPX → python-pptx 또는 한글 API
   - XLSX/XLS → openpyxl/xlrd
   - DOCX/DOC → python-docx/python-docx2txt
   - 이미지 → OCR (선택적)
4. 메타데이터 추출:
   - 파일명, 생성일, 수정일, 크기
```

#### 파일 타입 감지
```python
# 3단계 감지 프로세스
1. URL 확장자 확인 (.pdf, .hwp 등)
2. Content-Type 헤더 확인 (application/pdf)
3. 파일 매직 바이트 확인 (PDF: %PDF, ZIP: PK 등)

예:
  URL: https://example.go.kr/board/download.do?id=123
  → Content-Type: application/octet-stream (불명확)
  → 매직 바이트로 PDF 감지
```

---

## 4. 프론트엔드 페이지 구성

### 페이지 맵 및 기능

| 페이지 | 경로 | 설명 | 상태 |
|--------|------|------|------|
| 스마트 문서 찾기 | `/smart-find` | URL 입력 → 자동 문서 발견 (핵심) | ✅ |
| 대시보드 | `/` | 전체 통계, 나라별 성공률 | ✅ |
| 사이트 목록 | `/sites` | 나라별 카드뷰, 상태 필터 | ✅ |
| 사이트 상세 | `/sites/[id]` | 개별 사이트 정보, 문서 목록, 크롤링 실행 | ✅ |
| 크롤러 설정 편집 | `/sites/[id]/config` | JSON 설정 편집기 + 검증 | ✅ |
| 논문 검색 | `/papers` | 전체 문서 검색, 필터, 정렬 | ✅ |
| 작업 현황 | `/jobs` | 실시간 크롤링 작업 모니터링 | ✅ |
| URL 접근성 테스트 | `/url-test` | 3가지 방법 접근 테스트 (requests/cloudscraper/browser) | ✅ |
| 크롤링 미리보기 | `/crawl-preview` | CSS 셀렉터 테스트 + 자동 추천 | ✅ |
| GPT 기반 자동 추가 | `/auto-add` | 사이트 URL → 크롤러 설정 자동 생성 | ✅ |
| 일괄 추가 | `/batch-add` | 여러 사이트 한번에 처리 | ✅ |
| 리포트 | `/reports` | 나라별 수집 결과 + 실패 분석 | ✅ |
| 설정 | `/settings` | PRO 모드 설정, API 키 관리 | ✅ |

### 주요 페이지 상세

#### 1. Smart Find (`/smart-find`)
```
입력: https://example.go.kr/board/list.do
설정: Max Pages=10, Max Depth=3, Use Browser=false
↓
실행 (POST /api/smart-find)
↓
결과: 234개 파일 발견
  - 파일명
  - URL
  - 확장자
  - 크기
↓
액션: 다운로드, 상세보기, 재분석
```

#### 2. Sites (`/sites`)
```
나라별 카드:
  🇰🇷 한국: 255 사이트, 234 성공 (92%), 9,614 파일
  🇩🇪 독일: 55 사이트, 32 성공 (58%), ~500 파일
  🇬🇧 영국: 59 사이트, 55 성공 (93%), ~1,000 파일
  🇦🇺 호주: 65 사이트, 32 성공 (49%), 33개 지역 차단

필터: 모든 / 성공 / 실패
정렬: 이름 / 수집량 / 성공률
```

#### 3. Jobs (`/jobs`)
```
실시간 모니터링:
  Job ID | 사이트 | 상태 | 진행률 | 에러
  -------|--------|------|--------|----
  job-123 | BMBF | Running | 45% | -
  job-122 | BMBF | Completed | 100% | -
  job-121 | BMBF | Failed | 23% | Connection timeout

액션: 재시도, 로그 보기, 취소
```

#### 4. Auto-Add (`/auto-add`)
```
입력: https://publications.bmbf.de/
↓
GPT 분석:
  - 사이트 구조 파악
  - 목록/상세 페이지 구분
  - CSS 셀렉터 추천
  - 파일 타입 감지
↓
자동 생성 설정 (JSON)
↓
테스트 + 수동 조정 가능
```

---

## 5. API 엔드포인트 상세

### Smart Find API

```
POST /api/smart-find
요청:
{
  "url": "https://example.go.kr/board/list.do",
  "max_pages": 10,
  "max_depth": 3,
  "use_browser": false,
  "filter_by_type": "pdf"
}

응답:
{
  "id": "sf-12345",
  "url": "https://example.go.kr/board/list.do",
  "status": "completed",
  "discovered_count": 234,
  "created_at": "2026-04-07T10:30:00Z"
}
```

```
GET /api/smart-find/{id}/files
응답:
{
  "total": 234,
  "files": [
    {
      "title": "2026년 연구보고서.pdf",
      "url": "https://example.go.kr/board/download?id=123",
      "extension": "pdf",
      "size_bytes": 1048576,
      "detected_method": "direct_link"
    },
    ...
  ]
}
```

```
POST /api/smart-find/{id}/download
요청:
{
  "file_urls": [
    "https://example.go.kr/board/download?id=123",
    "https://example.go.kr/board/download?id=124"
  ]
}

응답:
{
  "job_id": "job-456",
  "download_count": 2,
  "status": "queued"
}
```

### 사이트 및 문서 API

```
GET /api/sites
쿼리 매개변수:
  ?country=KR          # 나라 코드 필터
  ?status=success      # 상태 필터 (success/failed/all)
  ?page=1
  ?limit=20

응답:
{
  "total": 255,
  "items": [
    {
      "id": "site-123",
      "name": "Ministry of Science",
      "base_url": "https://example.go.kr",
      "country": "KR",
      "status": "success",
      "paper_count": 45,
      "success_rate": 0.92,
      "last_crawled": "2026-04-06T15:30:00Z"
    },
    ...
  ]
}
```

```
GET /api/sites/{id}/papers
응답:
{
  "site_id": "site-123",
  "site_name": "Ministry of Science",
  "total": 45,
  "papers": [
    {
      "id": "paper-456",
      "title": "2026 Research Report",
      "url": "https://example.go.kr/docs/report-2026.pdf",
      "extension": "pdf",
      "size_bytes": 2097152,
      "downloaded_at": "2026-04-06T14:20:00Z"
    },
    ...
  ]
}
```

```
GET /api/papers
쿼리:
  ?q=research               # 검색어
  ?country=KR              # 나라 필터
  ?extension=pdf           # 파일 타입 필터
  ?date_from=2026-01-01    # 기간 필터
  ?date_to=2026-04-07
  ?sort=date               # 정렬 (date/size/relevance)
  ?order=desc              # 순서 (asc/desc)
  ?page=1&limit=20

응답: papers 배열 (위와 동일 구조)
```

### 크롤링 실행 API

```
POST /api/crawl/{site_id}
요청:
{
  "force": false,          # 기존 데이터 무시하고 재크롤링
  "download_files": true,  # 파일 다운로드 여부
  "save_to_db": true       # DB 저장 여부
}

응답:
{
  "job_id": "job-789",
  "site_id": "site-123",
  "site_name": "Ministry of Science",
  "status": "queued",
  "created_at": "2026-04-07T10:45:00Z"
}
```

```
GET /api/jobs
쿼리:
  ?site_id=site-123        # 사이트별 필터
  ?status=running          # 상태 필터 (queued/running/completed/failed)
  ?page=1&limit=20

응답:
{
  "total": 150,
  "jobs": [
    {
      "id": "job-789",
      "site_id": "site-123",
      "site_name": "Ministry of Science",
      "status": "running",
      "progress": 45,
      "found_count": 45,
      "downloaded_count": 32,
      "error_count": 0,
      "created_at": "2026-04-07T10:45:00Z",
      "started_at": "2026-04-07T10:46:00Z",
      "estimated_completion": "2026-04-07T11:15:00Z"
    },
    ...
  ]
}
```

### 설정 편집 API

```
GET /api/config/{site_id}
응답: JSON 크롤러 설정 (위 3.2 참조)

PUT /api/config/{site_id}
요청: 수정된 JSON 설정
응답: { "success": true, "site_id": "site-123" }

POST /api/config/clone
요청:
{
  "source_site_id": "site-123",
  "target_site_id": "site-124",
  "override_config": { /* 선택적 오버라이드 */ }
}
응답: { "success": true, "cloned_from": "site-123" }
```

### URL 테스트 API

```
POST /api/url-test
요청:
{
  "url": "https://example.go.kr/board/list.do",
  "methods": ["requests", "cloudscraper", "browser"],
  "timeout": 15
}

응답:
{
  "url": "https://example.go.kr/board/list.do",
  "results": [
    {
      "method": "requests",
      "success": true,
      "status_code": 200,
      "response_time_ms": 234,
      "content_size": 45623
    },
    {
      "method": "cloudscraper",
      "success": true,
      "status_code": 200,
      "response_time_ms": 1234,
      "content_size": 45623
    },
    {
      "method": "browser",
      "success": true,
      "status_code": 200,
      "response_time_ms": 3456,
      "rendered_content_size": 89234
    }
  ]
}
```

### 셀렉터 추천 API

```
POST /api/crawl-preview
요청:
{
  "url": "https://example.go.kr/publications/list.do",
  "selector": "div.publication-item",
  "depth": 3,
  "fetch_method": "requests"
}

응답:
{
  "preview_items": [
    {
      "title": "2026 Research Report",
      "extracted_data": {
        "title": "2026 Research Report",
        "link": "https://example.go.kr/publications/detail/123",
        "date": "2026-04-01"
      }
    },
    ...
  ],
  "item_count": 15
}

POST /api/auto-selectors
요청:
{
  "url": "https://example.go.kr/publications/list.do",
  "fetch_method": "requests"
}

응답:
{
  "detected_patterns": [
    {
      "selector": "div.publication-item",
      "confidence": 0.95,
      "sample_count": 15,
      "fields": {
        "title": { "selector": "h2.title", "confidence": 0.98 },
        "link": { "selector": "a.publication-link", "confidence": 0.99 },
        "date": { "selector": "span.pub-date", "confidence": 0.87 }
      }
    },
    ...
  ]
}
```

### 리포트 API

```
GET /api/reports/country/{country_code}
응답:
{
  "country_code": "KR",
  "country_name": "South Korea",
  "total_sites": 255,
  "success_count": 234,
  "failed_count": 21,
  "success_rate": 0.9176,
  "total_files": 9614,
  "by_category": {
    "Government": { "sites": 120, "success": 115, "files": 5000 },
    "Research": { "sites": 80, "success": 78, "files": 3000 },
    "Other": { "sites": 55, "success": 41, "files": 1614 }
  },
  "file_type_distribution": {
    "pdf": { "count": 5000, "percentage": 52 },
    "hwp": { "count": 3000, "percentage": 31 },
    "xlsx": { "count": 1000, "percentage": 10 },
    "others": { "count": 614, "percentage": 7 }
  },
  "failure_analysis": {
    "javascript_required": 9,
    "no_documents": 10,
    "access_denied": 2
  }
}

GET /api/reports/failure-analysis
응답:
{
  "by_country": {
    "KR": [
      { "reason": "No downloadable documents (news/text only)", "count": 10 },
      { "reason": "JavaScript onclick download only", "count": 9 },
      { "reason": "Page access failed", "count": 2 }
    ],
    ...
  }
}
```

---

## 6. 디렉토리 구조

```
crawler-poc/
├── api/                              # FastAPI 백엔드
│   ├── main.py                       # 앱 초기화 + 라우터 등록
│   ├── settings.py                   # 환경 설정 (pydantic-settings)
│   ├── database.py                   # SQLite 연결 풀
│   ├── schemas.py                    # Pydantic 요청/응답 모델
│   ├── country_map.py                # 나라 분류 (KR, DE, UK, AU 등)
│   │
│   ├── routers/                      # API 엔드포인트
│   │   ├── sites.py                  # GET /sites, /sites/{id}
│   │   ├── papers.py                 # GET /papers, /papers/{id}
│   │   ├── stats.py                  # GET /stats
│   │   ├── crawl.py                  # POST /crawl/{site_id}
│   │   ├── jobs.py                   # GET /jobs, /jobs/{id}
│   │   ├── smart_find.py             # POST /smart-find
│   │   ├── url_test.py               # POST /url-test
│   │   ├── crawl_preview.py          # POST /crawl-preview, /auto-selectors
│   │   ├── auto_add.py               # POST /auto-add (GPT 기반)
│   │   ├── config_editor.py          # GET/PUT /config/{site_id}
│   │   ├── site_status.py            # GET /site-status
│   │   ├── reports.py                # GET /reports/country/{code}
│   │   └── pro.py                    # PRO 모드 프록시
│   │
│   └── services/
│       ├── job_manager.py            # 백그라운드 크롤링 작업
│       └── pro_client.py             # PRO 서버 통신
│
├── crawler/                          # 크롤링 엔진
│   ├── smart_finder.py               # SmartDocumentFinder (핵심)
│   ├── generic_crawler.py            # GenericCrawler (JSON 설정 기반)
│   ├── base_crawler.py               # 기본 클래스 (retry, robots, rate limit)
│   ├── network_capture.py            # AJAX API 탐지
│   ├── agent.py                      # GPT Auto-Add 에이전트
│   ├── db.py                         # SQLite 데이터베이스 (models + queries)
│   ├── main.py                       # CLI 명령어
│   ├── summarizer.py                 # 다운로드 + 텍스트 추출
│   ├── converter.py                  # 파일 변환 (HWP/PDF)
│   │
│   └── sites/
│       ├── configs/                  # 200+ JSON 크롤러 설정
│       │   ├── korea-*.json
│       │   ├── germany-*.json
│       │   ├── uk-*.json
│       │   └── australia-*.json
│       │
│       └── custom/                   # 커스텀 Python 크롤러
│           ├── korea-*.py
│           └── germany-*.py
│
├── web/                              # Next.js 프론트엔드
│   ├── src/
│   │   ├── app/                      # 12개 페이지
│   │   │   ├── layout.tsx            # 메인 레이아웃 + 네비게이션
│   │   │   ├── page.tsx              # Dashboard
│   │   │   ├── smart-find/
│   │   │   ├── sites/
│   │   │   ├── papers/
│   │   │   ├── jobs/
│   │   │   ├── url-test/
│   │   │   ├── auto-add/
│   │   │   ├── batch-add/
│   │   │   ├── crawl-preview/
│   │   │   ├── reports/
│   │   │   └── settings/
│   │   │
│   │   ├── lib/
│   │   │   ├── api.ts                # API 클라이언트 (fetch wrapper)
│   │   │   └── types.ts              # TypeScript 타입
│   │   │
│   │   └── components/               # 공용 컴포넌트
│   │       ├── StatCard.tsx
│   │       ├── CountryCard.tsx
│   │       └── ...
│   │
│   ├── next.config.js                # Next.js 설정 (rewrite 프록시)
│   └── package.json
│
├── data/                             # 데이터
│   ├── papers.db                     # SQLite 데이터베이스
│   ├── crawl-state.json              # 크롤링 상태 (복구용)
│   └── cache/                        # HTTP 응답 캐시
│
├── downloads/                        # 다운로드된 파일
│   ├── korea/
│   ├── germany/
│   ├── uk/
│   └── australia/
│
├── reports/                          # 분석 리포트 (JSON)
│   ├── korea-results.json
│   ├── germany-results.json
│   ├── uk-results.json
│   └── australia-results.json
│
├── scripts/                          # 배치 스크립트
│   ├── smart_find_all_kr.py          # 한국 전체 Smart Finder 실행
│   ├── test_kr_sites.py              # URL 접근성 테스트
│   ├── batch_auto_add.py             # 대량 사이트 추가
│   └── report_generator.py           # 리포트 생성
│
├── docs/                             # 문서
│   ├── DEVELOPMENT.md                # 이 파일
│   ├── API.md                        # API 상세 명세 (OpenAPI 대체)
│   ├── DEPLOYMENT.md                 # 배포 가이드
│   └── TROUBLESHOOTING.md            # 문제 해결
│
├── .env                              # 환경 변수
├── requirements.txt                  # Python 의존성
├── package.json                      # Node.js 의존성
│
├── korea.md                          # 한국 수집 결과
├── germany.md                        # 독일 수집 결과
├── uk.md                             # 영국 수집 결과
├── australia.md                      # 호주 수집 결과
│
└── README.md                         # 프로젝트 개요
```

### 중요 파일 설명

**api/main.py** - FastAPI 앱 초기화
```python
- CORS 미들웨어 설정
- 13개 라우터 등록
- 헬스 체크 엔드포인트
```

**crawler/smart_finder.py** - SmartDocumentFinder 핵심
```python
- 초기 로드 (requests/cloudscraper/browser)
- 다운로드 링크 감지
- 재귀적 탐색 (페이지네이션, 깊이)
- AJAX/iframe 처리
```

**crawler/base_crawler.py** - 모든 크롤러의 기본
```python
- robots.txt 준수
- 재시도 + 백오프
- 동적 rate limiting
- 중복 감지
- 세션/쿠키 관리
```

**crawler/generic_crawler.py** - JSON 설정 기반 크롤러
```python
- list-detail 패턴
- single-page 패턴
- fetch_method 선택 (requests/cloudscraper/browser)
```

**web/src/lib/api.ts** - FE API 클라이언트
```typescript
export const api = {
  smartFind: (url, options) => POST /api/smart-find,
  getSites: (filters) => GET /api/sites,
  crawl: (siteId) => POST /api/crawl/{siteId},
  ...
}
```

---

## 7. 수집 현황 요약

### 나라별 결과 비교

| 지표 | 한국 (KR) | 독일 (DE) | 영국 (UK) | 호주 (AU) |
|------|-----------|-----------|-----------|----------|
| 테스트 사이트 수 | 255 | 55 | 59 | 65 |
| 성공 | 234 (92%) | 32 (58%) | 55 (93%) | 32 (49%) |
| 실패 | 21 (8%) | 23 (42%) | 4 (7%) | 33 (51%) |
| 수집 파일 수 | 9,614 | ~500 | ~1,000 | - |
| 평균 파일/사이트 | 41 | 16 | 18 | - |
| 크롤러 방식 | Smart Finder | GenericCrawler | GenericCrawler | GenericCrawler |
| 주요 실패 원인 | 파일 없음 (10개) | SPA (14개) | ArcGIS (2개) | 지역 차단 (33개) |

### 한국 상세 결과

**파일 타입 분포**:
```
- PDF: 4,800개 (50%)
- HWP: 3,000개 (31%)
- XLSX: 1,000개 (10%)
- 기타: 814개 (9%)
```

**실패 원인 분석** (GPT 검증):
```
E. 실제 파일 없음: 10개 (뉴스/텍스트만 제공)
A. JavaScript onclick: 9개 (동적 다운로드 필요)
G. 페이지 접근 실패: 2개 (WAF/차단)
```

**주요 성공 사이트**:
- 과학기술정보통신부 (MSIT): 450+ 문서
- 교육부 (MOE): 380+ 문서
- 보건복지부 (MOHW): 320+ 문서
- 각 부처 산하 연구기관: 200~500개

---

## 8. 실행 방법

### 환경 설정

```bash
# 저장소 클론
git clone https://github.com/your-org/crawler-poc.git
cd crawler-poc

# 가상 환경 생성
python3 -m venv .venv
source .venv/bin/activate  # Linux/macOS
# or .venv\Scripts\activate  # Windows

# 의존성 설치
pip install -r requirements.txt
npm install --prefix web

# 환경 변수 설정
cp .env.example .env
# 필요시 OPENAI_API_KEY 추가
export $(cat .env | xargs)
```

### 서버 실행

**백엔드 시작 (포트 30004)**:
```bash
.venv/bin/python -m uvicorn api.main:app \
  --port 30004 \
  --host 0.0.0.0 \
  --reload
```

**프론트엔드 시작 (포트 30003)**:
```bash
cd web
npx next dev --port 30003
```

브라우저에서 `http://localhost:30003` 접속

### 배치 크롤링 실행

**Smart Document Finder로 한국 전체 수집**:
```bash
.venv/bin/python scripts/smart_find_all_kr.py \
  --max-pages 20 \
  --max-depth 3 \
  --parallel 5
```

**URL 접근성 테스트**:
```bash
.venv/bin/python scripts/test_kr_sites.py \
  --timeout 15 \
  --save-report reports/test-results.json
```

**GenericCrawler로 특정 사이트 크롤링**:
```bash
.venv/bin/python -m crawler.main crawl \
  --config crawler/sites/configs/korea-msit.json \
  --output data/papers.db
```

### CLI 사용

**Smart Finder 직접 사용**:
```bash
.venv/bin/python -c "
from crawler.smart_finder import SmartDocumentFinder

finder = SmartDocumentFinder()
result = finder.find(
    url='https://example.go.kr/board/list.do',
    max_pages=10,
    max_depth=3
)

print(f'발견된 파일: {len(result.documents)}')
for doc in result.documents[:5]:
    print(f'  - {doc.title} ({doc.extension})')
"
```

**GenericCrawler 직접 사용**:
```bash
.venv/bin/python -c "
from crawler.generic_crawler import GenericCrawler
from crawler.db import init_db

db = init_db('data/papers.db')
crawler = GenericCrawler(
    config_path='crawler/sites/configs/korea-msit.json',
    db_conn=db
)

results = crawler.crawl()
print(f'수집된 논문: {len(results)}')
"
```

---

## 9. 환경 변수

### 필수 설정

| 변수 | 예시 | 설명 |
|------|------|------|
| `CRAWLER_DATABASE_URL` | `/data_raid/ruci_workspace/crawler-poc/data/papers.db` | SQLite DB 경로 |
| `CRAWLER_CORS_ORIGINS` | `http://localhost:30003` | CORS 허용 도메인 (쉼표 구분) |

### 선택 설정

| 변수 | 예시 | 설명 |
|------|------|------|
| `OPENAI_API_KEY` | `sk-proj-xxxx` | GPT Auto-Add, 실패 분석용 (없으면 기능 비활성) |
| `CRAWLER_PRO_API_URL` | `https://pro.crawler-api.com` | PRO 모드 서버 URL |
| `CRAWLER_PRO_LICENSE_KEY` | `lic-12345` | PRO 모드 라이선스 |

### .env 파일 예시

```bash
# 데이터베이스
CRAWLER_DATABASE_URL=/data_raid/ruci_workspace/crawler-poc/data/papers.db

# CORS (개발: localhost, 배포: 도메인)
CRAWLER_CORS_ORIGINS=http://localhost:30003,http://localhost:3000

# OpenAI (선택)
OPENAI_API_KEY=sk-proj-your-key-here

# PRO 모드 (선택)
CRAWLER_PRO_API_URL=
CRAWLER_PRO_LICENSE_KEY=
```

---

## 10. 개발 이력

### 초기 단계

| 날짜 | 내용 | 상태 |
|------|------|------|
| 2026-03-30 | 호주/독일 URL 테스트 + GenericCrawler 설정 생성 (55 독일, 65 호주) | ✅ |
| 2026-03-31 | 영국 URL 테스트 + GenericCrawler 설정 (59 사이트, 55 성공) | ✅ |

### 플랫폼 구축

| 날짜 | 내용 | 상태 |
|------|------|------|
| 2026-04-01 | FastAPI 백엔드 + Next.js FE 초기 구축 | ✅ |
| 2026-04-02 | PRO/Normal 모드 분리 구조 (pro_client.py) | ✅ |
| 2026-04-04 | 나라별 카드뷰, URL 테스트 페이지, Auto-Add, 크롤링 미리보기 | ✅ |

### 크롤러 고도화

| 날짜 | 내용 | 상태 |
|------|------|------|
| 2026-04-04 | 크롤러 인프라 강화: robots.txt, retry, rate limit, 중복감지, AJAX탐지, iframe | ✅ |
| 2026-04-05 | 한국 256개 URL 자동 추가 배치 실행 (Auto-Add) | ✅ |
| 2026-04-05 | SmartDocumentFinder 개발 완료 (URL만으로 문서 발견) | ✅ |
| 2026-04-06 | Smart Finder 개선: 세션 유지, iframe PDF, onclick 인터셉트 | ✅ |
| 2026-04-06 | 한국 전체 수집: 234/255 성공 (92%), 9,614개 파일 | ✅ |
| 2026-04-06 | GPT-5.4-mini 실패 원인 분석 + 리포트 페이지 | ✅ |
| 2026-04-07 | Smart Finder 추가 개선: depth 확장, URL 패턴, Playwright 인터셉트 | ✅ |

---

## 11. 트러블슈팅

### 공통 문제

**Q: Smart Finder가 일부 파일을 놓치는 경우**

A: 다음을 확인하세요:
```
1. max_depth 증가 (기본 3 → 5)
2. max_pages 증가
3. use_browser=True 설정 (JavaScript 렌더링)
4. 특정 URL 패턴이 DOWNLOAD_URL_PATTERNS에 있는지 확인

# 예시
finder.find(url, max_depth=5, use_browser=True)
```

**Q: "429 Too Many Requests" 에러**

A: rate limiting 조정:
```python
# GenericCrawler 설정에서
{
  "options": {
    "delay": 3.0,           # 기본값 1.5 → 3.0으로 증가
    "respect_robots": true  # robots.txt 준수
  }
}
```

**Q: CloudFlare WAF 차단**

A: fetch_method 변경:
```python
{
  "options": {
    "fetch_method": "cloudscraper"  # requests 대신 cloudscraper
  }
}

# 또는
{
  "options": {
    "fetch_method": "browser"  # Playwright 브라우저
  }
}
```

**Q: PDF/HWP 텍스트 추출 실패**

A: converter.py 확인:
```bash
# 필요 패키지 설치 확인
pip install pdfplumber python-pptx openpyxl

# HWP는 추가 라이브러리 필요
pip install hwppy  # HWP 한글 문서
```

---

## 12. 성능 최적화 팁

### 대량 크롤링 시

```python
# 병렬 처리 (async)
import asyncio

async def crawl_multiple(site_ids):
    tasks = [
        asyncio.create_task(crawl_site(site_id))
        for site_id in site_ids
    ]
    return await asyncio.gather(*tasks)

# 메모리 효율 (스트리밍)
with open('output.jsonl', 'w') as f:
    for paper in db.get_papers_stream():
        f.write(json.dumps(paper) + '\n')
```

### 네트워크 최적화

```python
# HTTP 연결 재사용 (세션)
session = requests.Session()
session.headers.update({'User-Agent': USER_AGENT})

# 컨텐츠 압축
headers = {
    'Accept-Encoding': 'gzip, deflate, br',
}
```

### 데이터베이스 최적화

```python
# 인덱스 생성
CREATE INDEX idx_papers_site ON papers(site_id);
CREATE INDEX idx_papers_url ON papers(url);
CREATE INDEX idx_papers_created ON papers(created_at);

# 배치 삽입
cursor.executemany(
    'INSERT INTO papers (...) VALUES (...)',
    batch_of_100_papers
)
```

---

## 13. 기여 가이드

### 새로운 기능 추가 순서

1. **DB 스키마 변경** (필요시)
   ```python
   # crawler/db.py에 migration 추가
   def migrate_v2():
       conn.execute("ALTER TABLE papers ADD COLUMN new_field TEXT")
   ```

2. **백엔드 구현** (api/routers/*.py)
   ```python
   @router.post("/api/new-feature")
   async def new_feature(request: NewFeatureRequest):
       # 구현
       return response
   ```

3. **프론트엔드 추가** (web/src/app/*)
   ```typescript
   export default function NewPage() {
       const [data, setData] = useState(null);
       useEffect(() => {
           api.newFeature().then(setData);
       }, []);
       return <div>{/* UI */}</div>;
   }
   ```

4. **테스트**
   ```bash
   pytest tests/ -v
   npm test --prefix web
   ```

5. **문서 업데이트** (docs/DEVELOPMENT.md)

### 코드 스타일

- **Python**: PEP 8 준수, 타입 힌팅 사용
- **TypeScript**: ESLint + Prettier 설정 준수
- **커밋**: "feat(module): description" 형식

---

## 14. 라이선스 및 연락처

- **라이선스**: MIT
- **저장소**: https://github.com/your-org/crawler-poc
- **이슈 트래킹**: GitHub Issues
- **문서**: /docs 디렉토리

---

## 부록: 주요 클래스 및 함수

### SmartDocumentFinder 메인 메서드

```python
class SmartDocumentFinder:
    def find(
        self,
        url: str,
        max_pages: int = 5,
        max_depth: int = 3,
        use_browser: bool = False,
        filter_by_type: Optional[str] = None
    ) -> FindResult:
        """
        URL에서 모든 다운로드 가능 문서를 자동 발견.

        Args:
            url: 시작 URL
            max_pages: 최대 탐색 페이지 수
            max_depth: 최대 탐색 깊이
            use_browser: Playwright 브라우저 사용 여부
            filter_by_type: 파일 타입 필터 (pdf, hwp 등)

        Returns:
            FindResult: 발견된 문서 목록
                - documents: Document 객체 배열
                - total: 발견된 총 파일 수
                - errors: 발생한 에러 목록
        """
```

### GenericCrawler 메인 메서드

```python
class GenericCrawler(BaseCrawler):
    def crawl(self) -> List[Paper]:
        """
        JSON 설정 기반 크롤링 실행.

        Returns:
            수집된 Paper 객체 목록
        """

    def crawl_list_detail(self) -> List[Paper]:
        """목록 → 상세 페이지 패턴"""

    def crawl_single_page(self) -> List[Paper]:
        """단일 페이지 패턴"""
```

### BaseCrawler 헬퍼 메서드

```python
class BaseCrawler(ABC):
    def _fetch_with_retry(
        self,
        url: str,
        method: str = "GET",
        max_retries: int = 3
    ) -> Optional[Response]:
        """재시도 로직 포함 HTTP 요청"""

    def _check_robots(self, url: str) -> bool:
        """robots.txt 준수 확인"""

    def _is_duplicate(self, url: str, content: str) -> bool:
        """URL/콘텐츠 중복 감지"""
```

---

이 문서는 `crawler-poc` 프로젝트의 전체 구조와 기능을 포괄적으로 설명합니다. 최신 정보는 GitHub repository를 참조하세요.

**마지막 업데이트**: 2026-04-07
**문서 버전**: 1.0.0
