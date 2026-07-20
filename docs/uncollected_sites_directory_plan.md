# 수집 불가 사이트 디렉토리 UI 계획

**문서 작성**: 2026-05-25
**대상 세션**: 새 Claude Code 세션 (UI 개발)
**관련 문서**: `country_library_ui_plan.md`, `dashboard_1994_funnel_plan.md`

---

## 1. 목적

수집 불가/대기 사이트들도 **사용자가 그 URL을 직접 클릭해서 볼 수 있게** 디렉토리 형태로 노출. 약 1,462 entries (전체 1,994 중 73%)에 해당.

> "수집은 못 하지만 사용자가 그 사이트에 직접 가서 자료를 보고 싶을 수 있다"

---

## 2. 분류 대상 (1,994 entries 중 수집 불가/대기)

| 카테고리 | 건수 | 사용자가 볼 수 있는 정보 |
|---|---|---|
| 🚫 정책상포기 (robots) | 222 | 사이트는 살아있음. URL 클릭 가능 |
| 🔐 회원가입 필요 (auth) | 246 | 사이트 살아있음. 가입 후 접근 |
| 🌐 차단 우회 필요 (cloudflare) | 246 (still_blocked) | 사이트 살아있음. VPN 필요 가능 |
| ⚫ 절대불가 (dead) | 112 | URL 자체 만료. archive.org 시도 가능 |
| ❓ 미분류 | 20 | 점검 필요 |
| **합계** | **약 850** | — |

(자동회복중 1093은 곧 수집되니 별도. 수집완료 301은 이미 적재됨.)

---

## 3. 데이터 소스

### 3.1 coverage_report.csv (이미 존재)
- 경로: `data/audit/coverage_report.csv`
- UTF-8, 1,994 rows
- 컬럼: entry_id, sheet, host, url, http_status, final_url, redirect_count, content_type, render_class, recovery_status, block_reason, analyzer_status, ...

### 3.2 site_recovery_plan.csv (이미 존재)
- 경로: `data/audit/site_recovery_plan.csv`
- UTF-8 BOM, 1,994 rows
- 컬럼: entry_id, sheet, host, url, recovery_category, recovery_method, effort, cost, notes

→ **두 CSV의 entry_id로 JOIN**해서 host, url, category, method 모두 한 행에 표시.

---

## 4. UI 사양

### 4.1 페이지 위치

**옵션 A (권장)**: 새 페이지 `/library/unreachable` — 도서관 트리 안에 자연스럽게 포함
**옵션 B**: `/admin/blocked-sites` — 관리자 전용
**옵션 C**: `/admin/collection-report` 페이지에 새 섹션 추가

→ 사용자 의도(직접 사이트 방문)는 일반 사용자 행동이므로 **옵션 A** 추천.

### 4.2 페이지 구조

```
┌────────────────────────────────────────────────────────────┐
│ 🔗 수집 안 된 사이트 디렉토리                                  │
│                                                              │
│ 입력 1,994 사이트 중 850개는 자동 수집이 어렵습니다.            │
│ 아래 링크를 통해 직접 방문하실 수 있습니다.                     │
│                                                              │
│ 카테고리 필터: [전체] [robots] [회원가입] [차단] [dead]      │
│ 국가 필터:   [전체] [한국] [프랑스] [USA] ...                │
│ 검색:        [_______________________]                       │
└────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────┐
│ 🚫 정책상 포기 (robots.txt) — 222 사이트                     │
├────────────────────────────────────────────────────────────┤
│ 🇰🇷 한국 (75건)                                              │
│   ┌────────────────────────────────────────────────────┐  │
│   │ www.bok.or.kr / 한국은행 보도자료                     │  │
│   │ 🔗 https://www.bok.or.kr/portal/singl/newsData/...   │  │
│   │ 사유: robots.txt 거부                                 │  │
│   │ 처리법: 사이트 운영자에게 데이터셋 요청 또는 공식 API   │  │
│   └────────────────────────────────────────────────────┘  │
│   ...                                                        │
└────────────────────────────────────────────────────────────┘
```

### 4.3 핵심 UI 요소

- **카드/리스트 형태**: 각 entry에 호스트 + sheet (사이트명) + URL 링크 + 사유 + 처리법
- **URL은 `target="_blank" rel="noopener"`** — 새 탭에서 열림
- **카테고리 색상**: dashboard_1994_funnel_plan.md의 색상 매핑 재사용
- **카테고리/국가 필터** — URL searchParams로 동기화
- **검색**: 호스트 또는 sheet명으로 간단 textmatch
- **페이지네이션 또는 무한 스크롤** — 850건이라 한 페이지에 다 표시도 가능 (성능 OK)

---

## 5. 필요 함수 (db.ts 또는 별도 lib)

### 5.1 새 인터페이스
```typescript
export interface UnreachableEntry {
  entry_id: string;
  sheet: string;
  host: string;
  url: string;
  category: string;           // recovery_category from CSV
  method: string;             // recovery_method from CSV
  reason_detail: string;      // notes
  country?: string;           // categories.ts로 sheet → country 매핑
}

export interface UnreachableReport {
  total: number;
  byCategory: Record<string, number>;
  byCountry: Record<string, number>;
  entries: UnreachableEntry[];
}

export function getUnreachableSites(): UnreachableReport;
```

### 5.2 구현 (req-only)
```typescript
export function getUnreachableSites(): UnreachableReport {
  return cached("unreachableSites", TTL, () => {
    const path = require("node:path");
    const fs = require("node:fs");
    const projectRoot = path.resolve(process.cwd(), "..");

    // site_recovery_plan.csv 읽음 (UTF-8 BOM)
    const csv = fs.readFileSync(
      path.join(projectRoot, "data/audit/site_recovery_plan.csv"),
      "utf-8"
    ).replace(/^﻿/, "");
    const lines = csv.split(/\r?\n/).filter(Boolean);
    const header = lines[0].split(",");
    const colIdx = (name: string) => header.indexOf(name);

    const EXCLUDE_CATEGORIES = new Set(["✅ 수집완료", "🔄 자동회복중"]);
    const entries: UnreachableEntry[] = [];

    for (let i = 1; i < lines.length; i++) {
      const cols = lines[i].split(",");
      const category = cols[colIdx("recovery_category")];
      if (EXCLUDE_CATEGORIES.has(category)) continue;
      const sheet = cols[colIdx("sheet")];
      entries.push({
        entry_id: cols[colIdx("entry_id")],
        sheet,
        host: cols[colIdx("host")],
        url: cols[colIdx("url")],
        category,
        method: cols[colIdx("recovery_method")],
        reason_detail: cols[colIdx("notes")] || "",
        country: getCategoryForSheet(sheet).country,
      });
    }

    // aggregations
    const byCategory: Record<string, number> = {};
    const byCountry: Record<string, number> = {};
    for (const e of entries) {
      byCategory[e.category] = (byCategory[e.category] || 0) + 1;
      if (e.country) byCountry[e.country] = (byCountry[e.country] || 0) + 1;
    }

    return { total: entries.length, byCategory, byCountry, entries };
  });
}
```

---

## 6. 구현 단계

### Step 1 (30m): db.ts 함수 추가
- `getUnreachableSites()` 함수 + 인터페이스
- categories.ts에서 country 매핑 사용

### Step 2 (1h): `/library/unreachable` 페이지
- searchParams로 카테고리/국가 필터
- URL 검색 박스 (client-side filter or server fetch)
- 카드 그리드 또는 표 (사용자 선택)

### Step 3 (30m): 메인 대시보드에 진입점 링크
- `/` 페이지의 funnel 섹션에 "수집 불가 사이트 보기 →" 링크
- `/library`에도 진입점 카드

### Step 4 (15m): 빌드 + 재시작

**총 소요 시간: 약 2시간**

---

## 7. 디자인 가이드

### 카테고리 색상 (dashboard_1994_funnel_plan.md와 일치)
| 카테고리 | Tailwind |
|---|---|
| 🚫 robots | `border-orange-300 bg-orange-50/30` |
| 🔐 회원가입 | `border-amber-300 bg-amber-50/30` |
| 🌐 차단 | `border-cyan-300 bg-cyan-50/30` |
| ⚫ dead | `border-rose-300 bg-rose-50/30` |
| ❓ 미분류 | `border-slate-300` |

### 카드 구조
```tsx
<a href={entry.url} target="_blank" rel="noopener noreferrer"
   className="block border rounded p-3 hover:shadow-md hover:border-indigo-500 transition">
  <div className="font-mono text-xs text-slate-500">{entry.host}</div>
  <div className="font-medium mt-1">{entry.sheet}</div>
  <div className="text-xs text-slate-600 dark:text-slate-400 mt-2">
    <span className="inline-block px-2 py-0.5 rounded bg-{color}-100 text-{color}-800">
      {entry.category}
    </span>
  </div>
  <div className="text-xs text-slate-500 mt-1 line-clamp-2">{entry.method}</div>
</a>
```

### 외부 링크 보안
- `target="_blank" rel="noopener noreferrer"` — 필수
- 외부 URL 클릭 시 user warning은 불필요 (사용자가 직접 의도한 행동)

---

## 8. 검증 시나리오

1. `/library/unreachable` 접속 → 약 850 entries 표시
2. 카테고리 필터 "robots" 클릭 → 222건만 표시
3. 국가 필터 "한국" 클릭 → 한국 사이트만 표시
4. 검색 박스에 "kosis" 입력 → 매치 entries만 표시
5. URL 클릭 → 새 탭에서 사이트 열림 (현재 페이지 유지)
6. 카드에 사유 설명 표시 (사용자가 왜 수집 안 됐는지 즉시 이해)
7. 모바일 화면에서도 카드 정렬 OK

---

## 9. 데이터 동결 여부

`coverage_report.csv` 와 `site_recovery_plan.csv`는 정적 CSV. 새 분석 돌릴 때만 갱신. 캐시 TTL은 5분으로 충분 (데이터가 자주 변하지 않음).

만약 promote_all이 새 사이트를 추가하면 그 사이트는 자동 회복으로 분류되어 이 페이지에서 자동 제외됨 (`✅ 수집완료` 카테고리 변경 후).

---

## 10. 주의 사항

- **외부 URL을 그대로 노출**하므로 사용자가 자기 책임 하에 방문하는 것
- robots/auth 사이트는 사용자가 직접 방문해도 일부 접근 제한 있을 수 있음 (이건 사이트 측 결정)
- archive.org fallback URL을 dead 카테고리에 함께 표시하면 회복률 ↑ (선택)

---

## 11. 새 세션 시작 가이드

```bash
cd /data_raid/ruci_workspace/frwaler_job

# 본 문서 + 관련 문서 같이 읽기
cat docs/uncollected_sites_directory_plan.md
cat docs/country_library_ui_plan.md       # 도서관 트리 (옵션 A 위치 선택 시)
cat docs/CURRENT_STATE.md                  # 시스템 상태

# CSV 구조 검증
head -2 data/audit/site_recovery_plan.csv
wc -l data/audit/site_recovery_plan.csv
```

---

문서 작성: 2026-05-25
