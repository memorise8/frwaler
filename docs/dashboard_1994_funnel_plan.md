# 메인 대시보드 1,994 → 532 Funnel 확장 계획

**문서 작성**: 2026-05-25
**대상 세션**: 새 Claude Code 세션 (UI 개발)
**관련 문서**: `docs/country_library_ui_plan.md` (도서관 UI는 별도)

---

## 1. 목적

사용자 요청: **"입력 1,994 entries 중 되는 것과 안 되는 것을 대시보드에 보이게 하고 싶다"**

즉 메인 대시보드(`/`)에 다음을 추가:
- 입력 funnel (1,994 → 833 → 977 → 532)
- 회복 카테고리별 분포 (5-6 카테고리)
- "왜 100% 수집 못 했는가"를 한눈에 설명

기존 KPI(documents 총수, PDF, 텍스트, 요약 등)는 그대로 유지하고 **새 섹션을 추가**.

---

## 2. 현재 데이터 (2026-05-25 기준)

### 2.1 입력 vs 실제

| 단위 | 수 | 비고 |
|---|---|---|
| 입력 URL entries | **1,994** | sheet 중복 포함 |
| unique hosts | **833** | 도메인 단위 |
| 시스템 등록 크롤러 | **977** | CRAWLERS dict — 한 host의 sub-section 분리 포함 |
| 적재된 unique site_id | **532** | libertree.db에 1건 이상 |

### 2.2 1,994 entries 회복 카테고리 분포

| 카테고리 | 건수 | 비율 | 회복 가능성 |
|---|---|---|---|
| 🔄 **자동회복중** | 1,093 | 54.8% | 진행 중 |
| ✅ **수집완료** | 301 | 15.1% | 완료 |
| 🔐 **사람개입필요-회원가입** | 246 | 12.3% | 수동 회복 가능 |
| 🚫 **정책상포기** (robots.txt) | 222 | 11.1% | 회복 불가 (법적) |
| ⚫ **절대불가** (dead/permaban) | 112 | 5.6% | 영구 불가 |
| ❓ **미분류** | 20 | 1.0% | 점검 필요 |

### 2.3 render_class 분포 (보조)

| 분류 | 건수 |
|---|---|
| `static_list` | 1,177 |
| `auth_blocked` | 248 |
| `robots_blocked` | 222 |
| `spa_likely` | 211 |
| `dead` | 116 |
| `unknown` | 20 |

---

## 3. 데이터 소스

### 3.1 CSV 파일 (이미 존재)

| 파일 | 경로 | 행 수 | 사용 |
|---|---|---|---|
| coverage_report.csv | `data/audit/coverage_report.csv` | 1,994 | host, render_class, recovery_status, block_reason |
| site_recovery_plan.csv | `data/audit/site_recovery_plan.csv` | 1,994 | recovery_category, recovery_method, effort |

CSV는 UTF-8 BOM 포함. Header 첫 라인.

### 3.2 DB (libertree.db)

- `documents` 테이블에서 `SELECT COUNT(DISTINCT site_id)` → 532
- 등록된 크롤러 수: `crawler/sites/custom/*.py` + `configs/*.json` + built-in 3 (= 977)

### 3.3 기존 함수 재사용

`finolaw/src/lib/db.ts` 의 `getCollectionReport()` 함수가 이미 이 데이터 일부 집계함:
```typescript
{
  inputEntries: 1994,
  inputUniqueHosts: 833,
  registeredCrawlers: 977,
  collectedSites: 532,
  ...
  recoveryCategories: [{ category, count }, ...]
}
```

→ **재사용 가능**. 메인 대시보드에서 호출하면 됨.

---

## 4. 표시 UI 사양

### 4.1 위치 — 메인 대시보드 `/`

`finolaw/src/app/page.tsx` 의 기존 KPI 카드 아래에 새 섹션 추가. **기존 KPI 보존**.

### 4.2 새 섹션 구조 (3 부분)

#### Part A — Funnel 시각화 (4단계)

```
┌────────────────────────────────────────────────────────────┐
│ 📥 입력 → 등록 → 수집 Funnel                                │
├────────────────────────────────────────────────────────────┤
│                                                              │
│  1,994 entries  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ │
│  833   hosts    ━━━━━━━━━━━━━━━━━━━━━━━━━━━                │
│  977   crawlers ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━              │
│  532   collected ━━━━━━━━━━━━━━━━━                          │
│                                                              │
│  → 입력 대비 27% 수집, 등록 대비 54%                          │
└────────────────────────────────────────────────────────────┘
```

각 단계 bar (Tailwind `bg-indigo-500/40/30/20` gradient)로 funnel 모양. 숫자와 함께.

#### Part B — 회복 카테고리 도넛 또는 stacked bar

```
┌────────────────────────────────────────────────────────────┐
│ 🛠 1,994 entries 회복 분류                                  │
├────────────────────────────────────────────────────────────┤
│  ┌──────────────────────────────────────────────────────┐  │
│  │ 자동회복중 1093   ████████████████████████████████  │  │
│  │ 수집완료    301   ████████                          │  │
│  │ 회원가입    246   ███████                           │  │
│  │ 정책포기    222   ██████                            │  │
│  │ 절대불가    112   ███                               │  │
│  │ 미분류       20   ▌                                │  │
│  └──────────────────────────────────────────────────────┘  │
│                                                              │
│  ⓘ 각 카테고리 설명 펼치기 (details/summary 토글)            │
└────────────────────────────────────────────────────────────┘
```

또는 카드 그리드로 6개 카테고리 표시 (이미 `/admin/collection-report`에 있는 패턴 재사용).

#### Part C — "되는 것 / 안 되는 것" 단순화 요약

```
┌──────────────────────┬──────────────────────┐
│ ✅ 수집 가능          │ ❌ 회복 불가          │
│                       │                       │
│  1,394건 (70%)       │   334건 (17%)         │
│                       │                       │
│  • 자동 1,093        │   • robots 222       │
│  • 완료    301       │   • dead    112      │
│                       │                       │
└──────────────────────┴──────────────────────┘
       (266 회원가입 시 추가 회복 가능)
```

3분류 정리:
- ✅ **수집 가능** = 자동회복중 + 수집완료 = 1,394
- 🔐 **수동 회복 가능** = 회원가입 = 246
- ❌ **회복 불가** = 정책포기 + 절대불가 = 334
- ❓ 미분류 = 20

---

## 5. 카테고리 의미 (사용자 설명용)

| 카테고리 | 의미 | 예시 사이트 |
|---|---|---|
| 🔄 자동회복중 | 시스템이 자동 batch에서 처리 중. 사용자 개입 불필요 | static_list, spa_likely 사이트 |
| ✅ 수집완료 | libertree.db에 적재됨. 일부는 cap 도달로 추가 분 대기 | 532 사이트 |
| 🔐 회원가입 필요 | auth_blocked. 회원 가입 + 쿠키/토큰 추출 → playwright_fetcher.py에 주입 | Web of Science, 일부 정부 통계 포털 |
| 🚫 정책상포기 | robots.txt 거부. 우회는 법적/윤리적 위험. 사이트 운영자에게 데이터 셋 요청이 정도 | 일부 한국 정부 사이트 |
| ⚫ 절대불가 | dead 페이지 / 영구 IP 차단. archive.org 외에 회복 수단 없음 | URL 자체가 만료된 사이트 |
| ❓ 미분류 | 분류 실패. 수동 점검 필요 | unknown render_class 20건 |

---

## 6. 구현 단계

### Step 1 (15m) — 메인 페이지 import 추가
```typescript
// finolaw/src/app/page.tsx
import { getCollectionReport } from "@/lib/db";

export default async function Home() {
  const stats = getDashboardStats();   // 기존 그대로
  const report = getCollectionReport(); // 신규
  // ...
}
```

### Step 2 (45m) — Funnel 섹션 컴포넌트
```typescript
<section className="mt-10">
  <h2 className="text-lg font-semibold mb-3">📥 입력 → 등록 → 수집 Funnel</h2>
  <FunnelBar items={[
    { label: "입력 entries", count: report.inputEntries, color: "bg-indigo-500" },
    { label: "unique hosts", count: report.inputUniqueHosts, color: "bg-indigo-400" },
    { label: "등록 크롤러", count: report.registeredCrawlers, color: "bg-indigo-300" },
    { label: "수집 완료 사이트", count: report.collectedSites, color: "bg-emerald-500" },
  ]} />
</section>
```

`FunnelBar` 컴포넌트는 각 행 width를 `count / inputEntries * 100`%로 비례.

### Step 3 (30m) — 회복 카테고리 차트
- 옵션 A: 카드 그리드 (간단)
- 옵션 B: stacked bar (시각적)
- 옵션 C: 도넛 차트 (recharts 같은 라이브러리 도입 필요 — 비추)

→ 옵션 A 추천 (의존성 추가 없이). `/admin/collection-report`의 카테고리 카드 그리드 코드 그대로 복사 가능.

### Step 4 (15m) — 단순 요약 (Part C)
3 카테고리 (가능 / 수동 / 불가) 큰 숫자 카드 3개.

### Step 5 (15m) — 카테고리 설명 details 토글
```html
<details className="mt-4">
  <summary className="cursor-pointer text-sm font-medium">카테고리 의미 설명</summary>
  <div className="mt-3 text-sm space-y-2 text-slate-600 dark:text-slate-400">
    ...카테고리별 설명...
  </div>
</details>
```

### Step 6 (10m) — 빌드 + 재시작
```bash
cd /data_raid/ruci_workspace/frwaler_job/finolaw
npm run build
kill <prev next-server PID on 3002>
./node_modules/.bin/next start -p 3002 -H 0.0.0.0
```

**총 소요 시간: 약 2시간**

---

## 7. 디자인 가이드

### 색상 매핑
| 카테고리 | Tailwind 클래스 |
|---|---|
| 자동회복중 | `bg-indigo-100 text-indigo-800 border-indigo-200` |
| 수집완료 | `bg-emerald-100 text-emerald-800 border-emerald-200` |
| 회원가입 | `bg-amber-100 text-amber-800 border-amber-200` |
| 정책포기 | `bg-orange-100 text-orange-800 border-orange-200` |
| 절대불가 | `bg-rose-100 text-rose-800 border-rose-200` |
| 미분류 | `bg-slate-100 text-slate-800 border-slate-200` |

### Responsive
- Funnel bars: full-width
- 카테고리 카드: `grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6`
- 단순 요약 (Part C): `grid grid-cols-1 md:grid-cols-3`

### 다크 모드
- 모든 `bg-*-100` 에 대응 `dark:bg-*-950/30` 추가
- `text-*-800` 에 대응 `dark:text-*-300` 추가

---

## 8. 검증 시나리오

새 섹션 추가 후 다음 통과 확인:

1. 메인 페이지(`/`) 로딩 → 기존 KPI **그대로** 표시 (회귀 없음)
2. 신규 funnel 섹션 표시: 4단계 bar + 정확한 숫자 (1994, 833, 977, 532)
3. 회복 카테고리 6개 카드 표시 + 합계 1,994 확인
4. 단순 요약 (가능/수동/불가) 카드 3개 표시 + 합계 검증
5. 다크 모드 토글 시 색상 적절히 변경
6. 모바일 화면 (sm) 에서 1열, 데스크탑에서 다열 정렬
7. 데이터 갱신 — `data/libertree.db`의 `documents.site_id` count가 변하면 `collectedSites`도 자동 변경 (5분 캐시)

---

## 9. 기존 자원 참조

| 자원 | 경로 |
|---|---|
| 메인 대시보드 (수정 대상) | `finolaw/src/app/page.tsx` |
| 수집 리포트 페이지 (UI 패턴 참고) | `finolaw/src/app/admin/collection-report/page.tsx` |
| `getCollectionReport()` | `finolaw/src/lib/db.ts` (이미 존재, 1380+ line 근처) |
| `CollectionReport` 타입 | `finolaw/src/lib/db.ts` |
| coverage_report.csv | `data/audit/coverage_report.csv` |
| site_recovery_plan.csv | `data/audit/site_recovery_plan.csv` |

---

## 10. 주의 사항

- **기존 메인 대시보드 KPI 보존** — 위쪽 KPI 4-5개 (총 문서, PDF, 텍스트, 요약, 사이트 수) 그대로 두고 **새 섹션은 아래에 추가**
- `getCollectionReport()`는 이미 5분 캐시 적용됨 — 부담 없음
- 카테고리 이름(이모지 포함)은 CSV의 `recovery_category` 컬럼 값과 정확히 일치 — 수정 금지
- TypeScript strict — `any` 사용 금지
- `dynamic = "force-dynamic"` 패턴 유지

---

## 11. 다른 세션 시작 시 첫 명령

```bash
cd /data_raid/ruci_workspace/frwaler_job

# 1. 본 문서 + country_library_ui_plan.md 둘 다 읽기
cat docs/dashboard_1994_funnel_plan.md
cat docs/country_library_ui_plan.md  # 도서관 UI는 별도

# 2. 현재 데이터 상태 확인
head -1 data/audit/coverage_report.csv
wc -l data/audit/coverage_report.csv data/audit/site_recovery_plan.csv

# 3. 메인 페이지 코드 확인
cat finolaw/src/app/page.tsx | head -100

# 4. 기존 getCollectionReport 함수 확인
grep -n "getCollectionReport\|recoveryCategories" finolaw/src/lib/db.ts
```

이 문서와 함께 `country_library_ui_plan.md`도 같이 보면 전체 UI 작업 방향 이해 가능.

---

문서 작성: 2026-05-25
