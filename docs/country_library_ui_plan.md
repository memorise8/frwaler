# Libertree 나라별 라이브러리 UI 계획

**문서 작성**: 2026-05-25
**대상 세션**: 새 Claude Code 세션 (별도 자원으로 작업)
**상태**: 미착수 — 본 문서를 self-contained 사양으로 활용

---

## 1. 목적 (왜 만드는가)

현재 libertree 시스템은 **메타 + PDF + 요약** 약 25k+ docs (그리고 계속 증가 중) 보유. 사용자가 원하는 것:

> 대시보드(`/`)는 그대로 두고, **나라별로 수집된 문서를 열람·검색**하고 **요약을 깔끔하게 읽을 수 있는** "도서관" 같은 UI

즉:
- 메인 대시보드(통계)는 보존
- 신규: **나라 → 사이트 → 문서 → 요약** 깊이 탐색 흐름
- 사용자는 "프랑스 자료 보고 싶다" 같은 자연스러운 진입
- 한 paper에 대한 요약·메타·PDF/TXT 다운을 한 페이지에 깔끔히

---

## 2. 시스템 컨텍스트 (필수 사전 지식)

### 2.1 코드베이스 구조

```
/data_raid/ruci_workspace/frwaler_job/
├── data/libertree.db          # SQLite, WAL mode, ~3.4 GB
├── libertree/AAAA/BBBB/AAAABBBBNNNN.{pdf,txt}  # blob 트리, ~685 GB
├── finolaw/                   # Next.js 16.2.4 App Router 앱
│   ├── src/lib/db.ts          # DB 함수 (1500+ 줄, 핵심)
│   ├── src/lib/categories.ts  # sheet → country/continent/category 매핑
│   ├── src/lib/preflight.ts   # DB 경로 검증
│   ├── src/app/page.tsx       # 메인 대시보드 (그대로 유지)
│   ├── src/app/search/...
│   ├── src/app/admin/...
│   └── proxy.ts               # Basic Auth gate
└── data/audit/                # 분류·로그 CSV·JSON
```

### 2.2 DB 스키마 (documents 테이블)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| seq_id | INTEGER PK | 12자리 ID |
| collected_at | TIMESTAMP | 수집 시각 |
| site_id | TEXT NOT NULL | 사이트 식별자 (예: `ens-hal-science-search`) |
| post_number | TEXT | 사이트 내 식별번호 |
| meta_url | TEXT NOT NULL | 원문 페이지 URL |
| title | TEXT NOT NULL | 제목 |
| published_date | TEXT | 발행일 |
| listed_date | TEXT | 게시일 |
| authors | TEXT | 저자 (콤마 구분) |
| publisher | TEXT | 발행 기관 |
| journal | TEXT | 학술지 (있을 때만) |
| pdf_url | TEXT | PDF URL |
| keywords | TEXT | 키워드 |
| abstract | TEXT | 초록 |
| original_filename | TEXT | 원본 파일명 |
| pdf_downloaded | INTEGER | 0/1 |
| text_extracted | INTEGER | 0/1 |
| pdf_size_bytes | INTEGER | |
| pdf_sha256 | TEXT | |
| **summary** | TEXT | Gemma 한국어 요약 (3-5문장) |
| summary_model | TEXT | (UI 노출 금지 — 사용자 결정) |
| summary_at | TIMESTAMP | 요약 생성 시각 |

`sites` 테이블에는 site_id, site_name, sheet 등 메타데이터 있음. `sheet`로 categories.ts와 join하면 country/continent/category 추출 가능.

### 2.3 categories.ts (핵심 매핑)

```typescript
export interface SiteCategory {
  country: string;        // "프랑스", "한국", "USA" 등 한국어/영어 혼용
  continent: Continent;   // Asia/Europe/North America/...
  category: SiteFunctionCategory; // Government/Research/Academic/...
}
export function getCategoryForSheet(sheet: string): SiteCategory;
```

이미 모든 sheet 값에 매핑되어 있음. 새 sheet는 fallback {country:"기타", continent:"Other"}.

### 2.4 재사용 가능 db.ts 함수

| 함수 | 반환 | 용도 |
|---|---|---|
| `getDbStats()` | DbStats | 전체 카운트 |
| `getDashboardStats()` | DashboardStats | 메인 대시보드 KPI (그대로 사용) |
| `getCollectionProgress()` | SiteProgress[] | 사이트별 진행 |
| `searchPapers(filters)` | SearchResult | FTS5 검색 |
| `getPaper(id)` | Paper \| null | seq_id로 단건 조회 |
| `getDocumentDetail(seqId)` | DocumentDetail | 14필드 + 파일 정보 |
| `getDocumentBlobInfo(seqId)` | blob 파일 존재 검증 |
| `blobAbsolutePath()` / `blobPath()` | string | PDF/TXT 경로 |
| `parseMetadata()` | PaperMetadata | site_id에서 sheet 추출 |
| `getSiteOptionsRich()` | Site[] | 사이트 목록 + 메타 |

### 2.5 기존 페이지 패턴 (Next.js 16 App Router)

```typescript
// 모든 페이지 server component
export const dynamic = "force-dynamic";

export default async function Page({ params, searchParams }: Props) {
  const stats = getCollectionTotals();
  // ... DB 함수 호출 (모두 sync, sqlite-better-sqlite3)
  return <main>...</main>;
}
```

- Tailwind CSS (이미 설정)
- 다크 모드 지원 (`dark:` 변형 prefix)
- Korean 포맷팅 `n.toLocaleString("ko-KR")`
- Link from next/link, no Image
- PDF/TXT 다운로드는 `/api/blob/[seq_id]/[ext]` (이미 있음, streamiing)

### 2.6 인증 / 배포

- `proxy.ts` (구 middleware.ts): HTTP Basic Auth — `ADMIN_USER` / `ADMIN_PASSWORD` from `.env.local`
- Production: port 3002 (ruci 권한 `./node_modules/.bin/next start -p 3002 -H 0.0.0.0`)
- Cloudflare quick tunnel — URL은 동적, 재시작마다 변경 가능
- 빌드: `npm run build` (Turbopack)

---

## 3. 새 UI 사양

### 3.1 페이지 트리 (전체 5단계)

```
/library                            # 나라별 카드 그리드 (메인 진입점)
  /[country]                       # 한 국가의 사이트 카드 그리드
    /[site_id]                     # 한 사이트의 문서 리스트 (필터·페이지네이션)
      /doc/[seq_id]                # 문서 상세 (요약 위주, 책 페이지처럼)
```

기존 `/search`, `/search/[id]`는 그대로 두되, /library 트리는 "도서관 큐레이션" 경험을 제공.

### 3.2 `/library` — 나라별 카드 그리드

**보여줄 것**:
- 국가별 KPI (수집 사이트 수, 문서 수, 요약 비율, 최신 수집일)
- 대륙별로 그룹핑 (Asia / Europe / Americas / Oceania / Africa)
- 카드 클릭 → `/library/[country]`

**컴포넌트 스케치**:
```
[Europe]
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│ 🇫🇷 프랑스    │ │ 🇩🇪 독일      │ │ 🇨🇭 스위스    │
│ 23 sites     │ │ 18 sites     │ │ 9 sites      │
│ 18,453 docs  │ │ 4,210 docs   │ │ 5,802 docs   │
│ 요약 82%     │ │ 요약 15%     │ │ 요약 100%    │
└──────────────┘ ...
```

**필요 함수** (db.ts에 추가):
```typescript
export interface CountryStats {
  country: string;
  continent: string;
  flag?: string;   // 이모지 (선택)
  sites: number;
  docs: number;
  summarized: number;
  pdfDownloaded: number;
  lastCollected: string | null;
}
export function getCountryStats(): CountryStats[];
```

구현 힌트: `sites` JOIN `documents` → `categories.ts`의 `getCategoryForSheet(sheet)` 매핑 → GROUP BY country.

### 3.3 `/library/[country]` — 사이트 카드 그리드

**보여줄 것**:
- 국가 헤더 (국가명 + 통계)
- 사이트 카드 그리드 (사이트별 문서 수, 카테고리 아이콘, 요약 진행률)
- 카드 클릭 → `/library/[country]/[site_id]`

**필요 함수**:
```typescript
export interface SiteSummaryRow {
  site_id: string;
  site_name: string;
  sheet: string;
  category: string;    // Government/Research/...
  docs: number;
  summarized: number;
  pdfDownloaded: number;
  lastCollected: string | null;
}
export function getSitesByCountry(country: string): SiteSummaryRow[];
```

### 3.4 `/library/[country]/[site_id]` — 사이트별 문서 리스트

**보여줄 것**:
- 사이트 헤더 (이름, 도메인, 총 문서 수)
- 검색·필터 박스 (제목, 날짜 범위, 요약 있음 only 등)
- 문서 리스트 (페이지네이션, 무한 스크롤 또는 50/page)
  - 각 행: 제목 + 발행일 + 저자 + 요약 첫 1문장
  - 클릭 → `/library/[country]/[site_id]/doc/[seq_id]`

**필요 함수**:
```typescript
export interface DocListItem {
  seq_id: number;
  title: string;
  published_date: string | null;
  authors: string | null;
  summary_excerpt: string | null;  // 첫 100자
  pdf_available: boolean;
}
export interface DocListResult {
  items: DocListItem[];
  total: number;
  page: number;
  pageSize: number;
}
export function getDocsBySite(
  siteId: string,
  opts: { q?: string; from?: string; to?: string; hasSummary?: boolean; page?: number; pageSize?: number }
): DocListResult;
```

### 3.5 `/library/[country]/[site_id]/doc/[seq_id]` — "책 페이지" 문서 상세

**디자인 원칙**:
- 종이책 같은 typography (sans-serif 본문, 큰 여백)
- 본문 영역에 요약을 크게 (Hero 섹션)
- 메타데이터는 사이드바 또는 접힘 카드
- PDF/TXT 다운로드 버튼은 큰 액션 버튼

**페이지 레이아웃**:
```
┌────────────────────────────────────────────────────────────┐
│ ← /library/[country]/[site_id]                              │
├────────────────────────────────────────────────────────────┤
│                                                              │
│    📄 [Title 24pt bold]                                      │
│    [Author] · [Published date] · [Site name]                 │
│                                                              │
│    ┌────────────────────────────────────────────────────┐   │
│    │ 📌 요약                                              │   │
│    │   (Gemma summary 3-5문장, 18pt regular, 줄간격 1.7) │   │
│    └────────────────────────────────────────────────────┘   │
│                                                              │
│    📖 초록 (Abstract)                                       │
│    [original abstract text]                                  │
│                                                              │
│    [PDF 다운로드]  [텍스트 다운로드]  [원문 보기]            │
│                                                              │
│    🏷 키워드: [tag] [tag] [tag]                              │
│                                                              │
└────────────────────────────────────────────────────────────┘
```

**필요 함수**: 기존 `getDocumentDetail(seqId)` 그대로 사용 가능. 단 URL params에서 country/site_id로 navigation 검증만 추가.

---

## 4. 구현 단계

### Step 1 (1-2h): db.ts 함수 추가
- [ ] `getCountryStats()` — sites + documents JOIN + categories.ts 매핑
- [ ] `getSitesByCountry(country)`
- [ ] `getDocsBySite(siteId, opts)` — FTS5 fallback 검색 포함
- [ ] 캐시 keys 추가 (5분 TTL)

### Step 2 (1h): `/library` 페이지
- [ ] page.tsx + 대륙별 그룹핑
- [ ] 국가 카드 컴포넌트
- [ ] 이모지 flag 매핑 (한국=🇰🇷, 프랑스=🇫🇷 등 — `categories.ts`에 추가 가능)

### Step 3 (1h): `/library/[country]` 페이지
- [ ] 사이트 카드 그리드
- [ ] 카테고리별 색상/아이콘

### Step 4 (1.5h): `/library/[country]/[site_id]` 페이지
- [ ] 문서 리스트 + 페이지네이션
- [ ] 클라이언트 필터 (제목 검색)
- [ ] URL searchParams 동기화

### Step 5 (1h): `/library/[country]/[site_id]/doc/[seq_id]` 페이지
- [ ] 책 페이지 레이아웃
- [ ] 요약 hero
- [ ] 메타데이터 사이드바 또는 카드
- [ ] PDF/TXT 다운로드 액션

### Step 6 (30m): 진입점 통합
- [ ] 메인 대시보드(`/`)에 "도서관 둘러보기" 버튼 추가 (→ `/library`)
- [ ] `app/layout.tsx`의 nav에 `Library` 메뉴 추가

### Step 7 (15m): 빌드 + 배포
- [ ] `cd finolaw && npm run build`
- [ ] port 3002 재시작
- [ ] cloudflared tunnel은 그대로 사용 — URL 동일

---

## 5. 디자인 가이드

### 색상 (Tailwind)
- Primary: `indigo-500` (이미 사용 중)
- 국가별 카드 hover: `hover:border-indigo-500 hover:shadow-md`
- 요약 hero box: `bg-amber-50/50 dark:bg-amber-950/30 border-amber-200`
- Category 색상:
  - Government: `bg-blue-100 text-blue-800`
  - Research: `bg-emerald-100 text-emerald-800`
  - Academic: `bg-purple-100 text-purple-800`
  - Statistics: `bg-amber-100 text-amber-800`

### Typography
- 책 페이지: `font-serif`로 본문 (Noto Serif KR 추천)
- 메타데이터: `font-mono text-xs`
- 제목: `text-3xl font-bold`

### Responsive
- 카드 그리드: `grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4`
- 문서 상세: `max-w-3xl mx-auto` (가독성 우선)

---

## 6. 검증 시나리오

새 페이지 만든 후 다음 시나리오 통과 확인:

1. **국가 목록 진입**: `/library` 접속 → 6대륙 그룹 + 각 국가 카드 표시
2. **국가 진입**: 프랑스 카드 클릭 → `/library/프랑스`에 프랑스 사이트들 (HAL 계열 다수)
3. **사이트 진입**: ens-hal-science-search 카드 → 문서 리스트 (요약 있는 것 18k+)
4. **요약 표시**: 한 문서 클릭 → 책 페이지 → Gemma 한국어 요약 hero에 표시
5. **PDF 다운로드**: PDF 버튼 → `/api/blob/[seq_id]/pdf` 통해 다운
6. **검색**: 사이트 페이지에서 "기후" 검색 → 일치 문서만 필터
7. **빈 국가**: 데이터 없는 국가 카드는 비활성 또는 안 보임
8. **국가명 한국어/영어 혼용 처리**: categories.ts의 country 값 그대로 사용 (한국, 프랑스, USA 등)

---

## 7. 기존 자원 위치 참조

| 자원 | 경로 |
|---|---|
| 메인 대시보드 (변경 금지) | `finolaw/src/app/page.tsx` |
| 기존 검색 페이지 (참고용) | `finolaw/src/app/search/page.tsx` |
| 기존 문서 상세 (참고용) | `finolaw/src/app/search/[id]/page.tsx` |
| Admin 디버그 뷰 | `finolaw/src/app/admin/document/[seq_id]/page.tsx` |
| 사이트별 진행 | `finolaw/src/app/admin/status/page.tsx` |
| 수집 리포트 | `finolaw/src/app/admin/collection-report/page.tsx` |
| DB 함수 (1500+ 줄) | `finolaw/src/lib/db.ts` |
| 카테고리 매핑 | `finolaw/src/lib/categories.ts` |
| Blob 스트리밍 API | `finolaw/src/app/api/blob/[seq_id]/[ext]/route.ts` |
| Layout (nav 수정) | `finolaw/src/app/layout.tsx` |
| Basic Auth | `finolaw/src/proxy.ts` |

---

## 8. 다른 세션 시작 시 첫 명령

```bash
cd /data_raid/ruci_workspace/frwaler_job/finolaw

# DB 상태 빠른 확인
sqlite3 ../data/libertree.db "SELECT COUNT(*) FROM documents; SELECT COUNT(DISTINCT site_id) FROM documents;"

# 기존 dev 서버 안 띄우고 prod build로 검증 (이미 가동 중이므로):
# 1. 코드 작업 → npm run build
# 2. kill <기존 next-server PID>
# 3. ./node_modules/.bin/next start -p 3002 -H 0.0.0.0
```

`docs/country_library_ui_plan.md` 본 문서 참조하면 컨텍스트 거의 다 잡힙니다.

---

## 9. 주의 사항

- **메인 대시보드(`/`)는 그대로 유지** (사용자 명시)
- DB는 운영 중 — 쓰기 충돌 없도록 read-only 함수만 사용
- 검색 시 `searchPapers`/`sanitizeFtsQuery` 패턴 따르기 (FTS5 한국어 trigram)
- `dynamic = "force-dynamic"`로 SSR 캐시 회피 (live data 표시)
- Tailwind는 Tailwind 4 (Next.js 16 기본). 기존 클래스 사용법 그대로
- TypeScript strict 모드 — `any` 사용 금지
- 모델명(gemma4)은 UI 노출 금지 (사용자 결정사항)

---

문서 마지막 갱신: 2026-05-25
