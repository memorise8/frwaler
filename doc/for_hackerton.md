# Hackerton - 우주 부품 탐색기 (Space Components Explorer)

## 프로젝트 개요

우주선 소재/소자 관련 반도체 부품 정보를 수집하고 탐색하는 시스템.
기존 논문 크롤러(crawler-poc)에 **제품 크롤링 + 해커톤 UI**를 추가한 프로젝트.

- **브랜치**: `hackerton`
- **프론트엔드**: https://web-alpha-two-66.vercel.app (Vercel)
- **백엔드 API**: Cloudflare 임시 터널 (재시작 시 URL 변경됨)

---

## 수집 현황 (2026-04-14 기준)

| 사이트 | 수집 수 | HTML 저장 | 상태 |
|--------|---------|-----------|------|
| **TI (Texas Instruments)** | 21,974 | 21,966 | 완료 |
| **Infineon** | 20,264 | 20,264 | 완료 |
| **Nexperia** | 7,563 | 7,563 | 완료 |
| **Vishay** | 6,263 | 6,263 | 완료 |
| **총합** | **56,064** | **56,056** | |

### 미진행 사이트 (Akamai/TLS 차단)
- ST Microelectronics — TLS 핑거프린트 차단
- Analog Devices — 연결 타임아웃
- onsemi — Akamai Bot Manager 403
- Microchip — Akamai Bot Manager 403

> stealth 플러그인(playwright-extra) + 프록시 필요. 추후 진행 가능.

---

## 아키텍처

```
[Vercel] ← HTTPS → [Cloudflare Tunnel] ← localhost:8000 → [FastAPI Backend]
                                                              ↓
                                                        [SQLite DB: data/papers.db]
                                                        [HTML Archive: html_archive/]
```

### Backend (Python)
- **FastAPI** — `api/main.py` (포트 8000)
- **SQLite** — `data/papers.db` (papers + products 테이블)
- **크롤러** — `crawler/` 디렉토리

### Frontend (Next.js 14)
- **위치**: `web/`
- **테마**: 다크 네이비 우주 테마 (DM Sans/Mono)
- **페이지**: 홈, 제품 검색, 카테고리, 통계, 제품 상세

---

## 실행 방법

### 1. 백엔드 서버 시작

```bash
cd /data_raid/ruci_workspace/crawler-poc
.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

### 2. Cloudflare 임시 터널 (외부 접근 필요 시)

```bash
# 기존 config 무시 필수 (fino-tunnel 충돌 방지)
cloudflared tunnel --config /dev/null --url http://127.0.0.1:8000 --no-autoupdate
```
> 출력에서 `https://xxx.trycloudflare.com` URL 확인

### 3. 프론트엔드 (로컬 개발)

```bash
cd web
npm run dev
# http://localhost:3000
```

### 4. 프론트엔드 (Vercel 재배포)

```bash
cd web
# .env.production에서 NEXT_PUBLIC_API_URL을 새 터널 URL로 변경
npx vercel --prod --yes
```

### 5. 크롤링 실행

```bash
# 특정 사이트
.venv/bin/python -m crawler.main crawl vishay --limit 10
.venv/bin/python -m crawler.main crawl ti --limit 10
.venv/bin/python -m crawler.main crawl infineon --limit 10
.venv/bin/python -m crawler.main crawl nexperia --limit 10

# 전체 (limit 없이)
.venv/bin/python -m crawler.main crawl vishay

# 통계 확인
.venv/bin/python -m crawler.main product-stats
```

---

## 주요 파일 구조

### 신규 파일 (hackerton 브랜치에서 추가)

```
crawler/
  product_crawler.py          # 범용 제품 크롤러 (JSON config 기반)
  sites/
    vishay.py                 # Vishay 전용 (Next.js JSON API)
    nexperia.py               # Nexperia 전용 (REST API + HTML)
    ti.py                     # TI 전용 (sitemap + productmodel JSON API)
    infineon.py               # Infineon 전용 (sitemap + HTML + dataApi)
    configs/
      product-example-generic.json  # 범용 제품 config 템플릿

api/
  routers/
    products.py               # 제품 API (목록/상세/HTML/카테고리/통계)

web/
  .env.production             # API URL 설정
  src/
    components/NavBar.tsx     # 해커톤 네비게이션
    app/
      layout.tsx              # 해커톤 레이아웃
      page.tsx                # 홈 (히어로 검색 + 통계)
      products/
        page.tsx              # 제품 검색 (그리드 카드)
        [productId]/page.tsx  # 제품 상세 (스펙 + HTML 뷰어)
      categories/page.tsx     # 카테고리 목록
      stats/page.tsx          # 통계 대시보드
```

### 수정된 기존 파일

```
crawler/db.py                 # products 테이블 + helper 함수 추가
crawler/main.py               # product-stats CLI 명령어 추가
crawler/sites/__init__.py     # 커스텀 크롤러 등록
api/main.py                   # products 라우터 + CORS(vercel.app) 추가
api/schemas.py                # ProductOut, ProductListOut 모델 추가
api/settings.py               # CORS origin 설정
web/src/lib/api.ts            # 제품 API 클라이언트 함수 추가
```

---

## API 엔드포인트

### 제품 API

| Endpoint | Method | 설명 |
|----------|--------|------|
| `/api/products?q=&site_id=&page=1&limit=20` | GET | 제품 목록 (검색, 페이지네이션) |
| `/api/products/{id}` | GET | 제품 상세 |
| `/api/products/{id}/html` | GET | 저장된 HTML 원본 |
| `/api/products/stats` | GET | 수집 통계 |
| `/api/products/categories?site_id=` | GET | 카테고리 목록 + 개수 |

### 기존 API (논문/크롤러 관리)

| Endpoint | Method | 설명 |
|----------|--------|------|
| `/api/smart-find` | POST | URL에서 PDF 파일 자동 탐지 (GPT 불필요) |
| `/api/auto-add` | POST | URL → 크롤러 config 자동 생성 (GPT 필요) |
| `/api/crawl/{site_id}` | POST | 크롤링 실행 |
| `/api/health` | GET | 서버 상태 |

---

## DB 스키마 (products 테이블)

```sql
CREATE TABLE products (
    id              TEXT PRIMARY KEY,
    site_id         TEXT NOT NULL,      -- vishay, ti, infineon, nexperia
    external_id     TEXT,               -- 제품 고유 ID (docid, part number)
    name            TEXT,               -- 제품명
    price           TEXT,               -- 가격 문자열
    price_value     REAL,               -- 숫자 가격
    currency        TEXT,               -- KRW, USD 등
    brand           TEXT,               -- 제조사
    category        TEXT,               -- 카테고리 (> 구분)
    description     TEXT,               -- 제품 설명
    image_url       TEXT,               -- 대표 이미지
    image_urls      TEXT,               -- JSON array (복수 이미지)
    specs           TEXT,               -- JSON object (사양/스펙)
    rating          REAL,
    review_count    INTEGER,
    availability    TEXT,               -- 재고/생산 상태
    url             TEXT,               -- 원본 페이지 URL
    html_path       TEXT,               -- 저장된 HTML 파일 경로
    metadata        TEXT,               -- JSON (datasheet_url 등)
    crawled_at      TIMESTAMP,
    UNIQUE(site_id, external_id)
);
```

---

## 크롤러별 데이터 소스

| 사이트 | 방식 | 특징 |
|--------|------|------|
| **Vishay** | Next.js JSON API (`/_next/data/`) | namespace 없음, 카테고리별 전체 docid 제공 |
| **TI** | sitemap XML + `/productmodel/{PART}` JSON API | 가장 풍부한 데이터 (스펙, 가격, 문서) |
| **Infineon** | sitemap XML + HTML 파싱 + `/dataApi/` | AEM CMS, User-Agent 필수 |
| **Nexperia** | 카테고리 HTML + `product-data.nexperia.com` REST API | robots.txt 403 → respect_robots=False |

---

## 새 사이트 추가 방법

### 방법 1: JSON config (간단한 사이트)

```bash
# 템플릿 복사
cp crawler/sites/configs/product-example-generic.json crawler/sites/configs/새사이트.json
# site_id, base_url, CSS 셀렉터 수정
# 자동으로 등록됨 (data_type: "product" 필수)
```

### 방법 2: 커스텀 크롤러 (API가 있는 사이트)

```python
# crawler/sites/새사이트.py 생성
class NewCrawler(BaseCrawler):
    site_id = "새사이트"
    ...

# crawler/sites/__init__.py에 등록
from .새사이트 import NewCrawler
CRAWLERS['새사이트'] = NewCrawler
```

---

## 향후 TODO

- [ ] 차단된 4개 사이트 (ST, Analog, onsemi, Microchip) — stealth 플러그인 적용
- [ ] 용도/응용분야 기반 카테고리 재분류 (TI applications 필드 활용)
- [ ] Verra 방법론 PDF 수집 (smart-find 활용 가능, GPT 불필요)
- [ ] 프론트에서 URL 입력 → 제품 크롤링 자동화 UI
- [ ] 고정 도메인 Cloudflare Tunnel 설정 (현재 임시 URL)
- [ ] Swagger UI 비활성화 (프로덕션 보안)
- [ ] HTML 아카이브를 S3/R2로 이관 (디스크 용량 관리)

---

## 주의사항

- 임시 터널 URL(`*.trycloudflare.com`)은 서버 재시작 시 변경됨
- 터널 실행 시 `--config /dev/null` 필수 (기존 fino-tunnel 충돌 방지)
- `.env.production`의 `NEXT_PUBLIC_API_URL`을 새 터널 URL로 변경 후 Vercel 재배포 필요
- TI robots.txt에 crawl-delay 1초 — 현재 1.5초로 설정됨
- Nexperia robots.txt 자체가 403 → respect_robots=False 사용
- Vercel 계정: `memorise8` (memorise8s-projects)
