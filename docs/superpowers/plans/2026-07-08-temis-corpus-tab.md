# TEMIS Ops 코퍼스 탭 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** fino-backend에 `/api/admin/corpus/*` 프록시 라우터를 추가하고, temis-ops에 Corpus 탭을 추가해 TEMIS admin 인증 아래에서 fino_ops(:8500)의 코퍼스 상태·수집을 관리한다.

**Architecture:** temis-ops(브라우저) → fino-backend `/api/admin/corpus/*`(admin token 인증, httpx 프록시) → 127.0.0.1:8500 fino_ops. frwaler/fino_ops는 무수정.

**Tech Stack:** FastAPI + httpx(BE, 기존 의존성), React 18 + TanStack Query + Vite + vitest(FE, 기존 스택).

**스펙:** `docs/superpowers/specs/2026-07-08-temis-corpus-tab-design.md` (frwaler)

## Global Constraints

- **대상 리포는 frwaler가 아님**: BE = `/data_raid/ruci_workspace/repo/finov2/fino-backend` (신규 브랜치 `temis-corpus-ops`, cbt에서 분기), FE = `/data_raid/ruci_workspace/repo/finov2/temis-ops` (신규 브랜치 `corpus-tab`, main에서 분기).
- **push 금지. 기존 브랜치(cbt/main)의 미푸시 커밋 불간섭. 가동 중인 서비스(:18080 uvicorn) 재시작 금지.**
- fino_ops 업스트림: env `TEMIS_FINO_OPS_URL` 기본 `http://127.0.0.1:8500`. 503 에러 포맷은 `{"detail": {"message": "..."}}` (temis-ops `isErrorEnvelope` 호환).
- BE 테스트: `cd fino-backend && .venv/bin/python -m pytest tests/test_admin_corpus_proxy.py -q` (venv는 fino-backend/.venv — 확인 후 없으면 `python3 -m pytest`나 리포 관례 확인). FE 테스트: `cd temis-ops && npm test`, `npm run lint`, 빌드는 `npm run build`(build:current가 있으면 그것).
- 커밋 스타일: BE `feat(temis): ...`, FE `feat(ops): ...` (각 리포 로그 관례 따름).
- frwaler 쪽 코드는 무수정 (스펙/계획 문서만 frwaler에 있음).

---

### Task 1: fino-backend — admin_corpus 프록시 라우터 + 테스트

**Files:** (리포: /data_raid/ruci_workspace/repo/finov2/fino-backend, 브랜치 `temis-corpus-ops` 생성 후)
- Create: `app/api/admin_corpus.py`
- Modify: `app/main.py` (import + include_router 2줄)
- Test: `tests/test_admin_corpus_proxy.py`

**Interfaces:**
- Consumes: `app.api.dependencies.get_admin_user`, httpx(requirements에 있음)
- Produces: `/api/admin/corpus/status`, `/api/admin/corpus/{key}/refresh`, `/api/admin/corpus/runs`, `/api/admin/corpus/runs/{run_id}/log` — Task 2의 corpusApi가 소비. `build_client()` 팩토리(테스트 주입 지점).

- [ ] **Step 0: 브랜치 생성**

```bash
cd /data_raid/ruci_workspace/repo/finov2/fino-backend
git status --short   # 클린 확인 — 더러우면 STOP, BLOCKED 보고
git checkout -b temis-corpus-ops
```

- [ ] **Step 1: Write the failing test**

`tests/test_admin_corpus_proxy.py`:

```python
from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

import app.api.admin_corpus as admin_corpus
from app.api.dependencies import get_admin_user
from app.main import app


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    # 최소 env — app import/기동에 필요한 값 (기존 contract 테스트 fixture 관례)
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("ALLOWLIST_ENABLED", "false")
    app.dependency_overrides[get_admin_user] = lambda: SimpleNamespace(
        email="ops@test", is_admin=True
    )
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _install_upstream(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        admin_corpus, "build_client",
        lambda: httpx.Client(base_url="http://ops.test", transport=transport),
    )


def test_status_proxies_corpora(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/corpora"
        return httpx.Response(200, json=[{"key": "std", "total": 21375, "busy": False}])

    _install_upstream(monkeypatch, handler)
    r = client.get("/api/admin/corpus/status")
    assert r.status_code == 200
    assert r.json()[0]["key"] == "std"


def test_refresh_passes_through_status(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/corpora/nts_qt/refresh"
        return httpx.Response(409, json={"detail": "다른 수집이 실행 중입니다"})

    _install_upstream(monkeypatch, handler)
    assert client.post("/api/admin/corpus/nts_qt/refresh").status_code == 409


def test_runs_and_log_forward_params(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/runs":
            assert request.url.params["limit"] == "5"
            return httpx.Response(200, json=[{"id": 1, "corpus": "std", "status": "ok"}])
        assert request.url.path == "/api/runs/1/log"
        assert request.url.params["tail"] == "50"
        return httpx.Response(200, text="line1\nline2", headers={"content-type": "text/plain; charset=utf-8"})

    _install_upstream(monkeypatch, handler)
    assert client.get("/api/admin/corpus/runs?limit=5").json()[0]["id"] == 1
    log = client.get("/api/admin/corpus/runs/1/log?tail=50")
    assert log.status_code == 200 and "line1" in log.text


def test_upstream_down_returns_503(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_upstream(monkeypatch, handler)
    r = client.get("/api/admin/corpus/status")
    assert r.status_code == 503
    assert "fino_ops" in r.json()["detail"]["message"]


def test_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JWT_SECRET_KEY", "test-secret-key")
    with TestClient(app) as c:  # override 없음 → 인증 실패여야 함
        assert c.get("/api/admin/corpus/status").status_code in (401, 403)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /data_raid/ruci_workspace/repo/finov2/fino-backend && .venv/bin/python -m pytest tests/test_admin_corpus_proxy.py -q` (venv 경로가 다르면 리포 관례 확인 — uvicorn 프로세스가 `.venv/bin/python`을 쓰고 있음)
Expected: FAIL — `ModuleNotFoundError: No module named 'app.api.admin_corpus'`
주의: app import 자체가 다른 env를 요구해 collection 에러가 나면, 기존 `tests/test_accountant_verification_contract.py` fixture의 env 목록을 참고해 fixture에 추가하고 보고서에 기록.

- [ ] **Step 3: Write implementation**

`app/api/admin_corpus.py`:

```python
from __future__ import annotations

import os
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse

from app.api.dependencies import get_admin_user
from app.models import User

router = APIRouter(prefix="/api/admin/corpus", tags=["admin-corpus"])

AdminUserDep = Annotated[User, Depends(get_admin_user)]

DEFAULT_OPS_URL = "http://127.0.0.1:8500"


def ops_base_url() -> str:
    return os.environ.get("TEMIS_FINO_OPS_URL", DEFAULT_OPS_URL)


def build_client() -> httpx.Client:
    """fino_ops HTTP 클라이언트 — 테스트가 MockTransport를 주입하는 지점."""
    return httpx.Client(base_url=ops_base_url(), timeout=10.0)


def _forward(method: str, path: str, params: dict | None = None) -> Response:
    try:
        with build_client() as client:
            upstream = client.request(method, path, params=params)
    except httpx.HTTPError as exc:
        return JSONResponse(
            status_code=503,
            content={"detail": {"message": f"수집 서버(fino_ops)에 연결할 수 없습니다: {exc}"}},
        )
    media_type = upstream.headers.get("content-type", "application/json")
    return Response(content=upstream.content, status_code=upstream.status_code, media_type=media_type)


@router.get("/status")
def corpus_status(admin: AdminUserDep) -> Response:
    _ = admin
    return _forward("GET", "/api/corpora")


@router.post("/{key}/refresh")
def corpus_refresh(key: str, admin: AdminUserDep) -> Response:
    _ = admin
    return _forward("POST", f"/api/corpora/{key}/refresh")


@router.get("/runs")
def corpus_runs(admin: AdminUserDep, limit: int = 50) -> Response:
    _ = admin
    return _forward("GET", "/api/runs", params={"limit": limit})


@router.get("/runs/{run_id}/log")
def corpus_run_log(run_id: int, admin: AdminUserDep, tail: int = 200) -> Response:
    _ = admin
    return _forward("GET", f"/api/runs/{run_id}/log", params={"tail": tail})
```

`app/main.py`: import 블록의 `from app.api.admin_ops import router as admin_ops_router` 옆에 `from app.api.admin_corpus import router as admin_corpus_router` 추가, include 블록의 `app.include_router(admin_ops_router)` 옆에 `app.include_router(admin_corpus_router)` 추가 (기존 정렬/그룹 관례 유지).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/test_admin_corpus_proxy.py -q`
Expected: 5 passed. 이어서 기존 스위트 회귀 스팟체크: `.venv/bin/python -m pytest tests/test_accountant_verification_contract.py -q` (전체 스위트는 무겁고 이 변경과 무관 — 라우터 추가는 계약 테스트 1개로 충분).

- [ ] **Step 5: Commit** (fino-backend 리포에, push 금지)

```bash
git add app/api/admin_corpus.py app/main.py tests/test_admin_corpus_proxy.py
git commit -m "feat(temis): admin corpus proxy — fino_ops(:8500) 상태/수집/이력/로그 중계"
```

---

### Task 2: temis-ops — corpus 타입/API/훅 + 테스트

**Files:** (리포: /data_raid/ruci_workspace/repo/finov2/temis-ops, 브랜치 `corpus-tab` 생성 후)
- Create: `src/types/corpus.ts`
- Create: `src/api/corpusApi.ts`
- Create: `src/hooks/useCorpus.ts`
- Test: `src/api/corpusApi.test.ts`

**Interfaces:**
- Consumes: `adminSession.getStoredAdminToken`, `OpsApiError`(evidenceDebugApi), Task 1의 `/api/admin/corpus/*`
- Produces: `CorpusStatus`/`CorpusRun` 타입, `corpusApi`(getStatus/refresh/getRuns/getRunLog), `corpusKeys`/`useCorpusStatus`/`useCorpusRuns`/`useRefreshCorpus`/`useCorpusRunLog` — Task 3의 CorpusPage가 소비

- [ ] **Step 0: 브랜치 생성**

```bash
cd /data_raid/ruci_workspace/repo/finov2/temis-ops
git status --short   # 클린 확인 — 더러우면 STOP, BLOCKED 보고
git checkout -b corpus-tab
npm install          # node_modules 없으면
```

- [ ] **Step 1: Write the failing test**

`src/api/corpusApi.test.ts`:

```typescript
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { corpusApi } from './corpusApi';

const fetchMock = vi.fn();

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  window.sessionStorage.setItem('temis_ops_admin_token', 'test-token');
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
  fetchMock.mockReset();
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

describe('corpusApi', () => {
  it('getStatus는 Bearer 토큰과 함께 /api/admin/corpus/status를 호출한다', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse([{ key: 'std', label: '회계 기준서', total: 21375, last_collected: null, db_exists: true, last_run: null, busy: false }]));
    const rows = await corpusApi.getStatus();
    expect(rows[0].key).toBe('std');
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/api/admin/corpus/status');
    expect((init?.headers as Record<string, string>).Authorization).toBe('Bearer test-token');
  });

  it('refresh는 409에서 OpsApiError(status 409)를 던진다', async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: '다른 수집이 실행 중입니다' }, 409));
    await expect(corpusApi.refresh('nts_qt')).rejects.toMatchObject({ status: 409 });
  });

  it('getRunLog는 text를 반환한다', async () => {
    fetchMock.mockResolvedValueOnce(new Response('line1\nline2', { status: 200, headers: { 'Content-Type': 'text/plain' } }));
    await expect(corpusApi.getRunLog(1, 50)).resolves.toContain('line1');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- corpusApi`
Expected: FAIL — corpusApi 모듈 없음

- [ ] **Step 3: Write implementation**

`src/types/corpus.ts`:

```typescript
export interface CorpusLastRun {
  readonly status: string;
  readonly new_count: number | null;
  readonly total_after: number | null;
  readonly finished_at: string | null;
}

export interface CorpusStatus {
  readonly key: string;
  readonly label: string;
  readonly total: number | null;
  readonly last_collected: string | null;
  readonly db_exists: boolean;
  readonly last_run: CorpusLastRun | null;
  readonly busy: boolean;
}

export interface CorpusRun {
  readonly id: number;
  readonly corpus: string;
  readonly started_at: string;
  readonly finished_at: string | null;
  readonly status: string;
  readonly new_count: number | null;
  readonly total_after: number | null;
}

export interface CorpusRefreshResponse {
  readonly started: boolean;
  readonly corpus: string;
}
```

`src/api/corpusApi.ts` (opsApi.ts 패턴 미러):

```typescript
import { getStoredAdminToken } from './adminSession';
import { OpsApiError } from './evidenceDebugApi';
import type { CorpusRefreshResponse, CorpusRun, CorpusStatus } from '../types/corpus';

const API_BASE = import.meta.env.VITE_API_URL || '';

class CorpusApiClient {
  private readonly baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = baseUrl;
  }

  async getStatus(): Promise<readonly CorpusStatus[]> {
    return this.requestJson('/api/admin/corpus/status');
  }

  async refresh(key: string): Promise<CorpusRefreshResponse> {
    return this.requestJson(`/api/admin/corpus/${key}/refresh`, { method: 'POST' });
  }

  async getRuns(limit = 30): Promise<readonly CorpusRun[]> {
    return this.requestJson(`/api/admin/corpus/runs?limit=${limit}`);
  }

  async getRunLog(runId: number, tail = 200): Promise<string> {
    const response = await this.rawRequest(`/api/admin/corpus/runs/${runId}/log?tail=${tail}`, {});
    return response.text();
  }

  private async requestJson<T>(endpoint: string, options: RequestInit = {}): Promise<T> {
    const response = await this.rawRequest(endpoint, options);
    return response.json();
  }

  private async rawRequest(endpoint: string, options: RequestInit): Promise<Response> {
    const response = await fetch(`${this.baseUrl}${endpoint}`, {
      ...options,
      credentials: 'include',
      headers: requestHeaders(options),
    });
    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({ message: 'Request failed' }));
      throw new OpsApiError(response.status, errorMessage(errorBody), errorBody);
    }
    return response;
  }
}

function requestHeaders(options: RequestInit): HeadersInit {
  const nextHeaders: Record<string, string> = {};
  if (options.body !== undefined) nextHeaders['Content-Type'] = 'application/json';
  const token = getStoredAdminToken();
  if (token.length > 0) nextHeaders.Authorization = `Bearer ${token}`;
  return { ...nextHeaders, ...options.headers };
}

function errorMessage(errorBody: unknown): string {
  if (typeof errorBody === 'object' && errorBody !== null && 'detail' in errorBody) {
    const detail = (errorBody as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (typeof detail === 'object' && detail !== null && 'message' in detail
      && typeof (detail as { message: unknown }).message === 'string') {
      return (detail as { message: string }).message;
    }
  }
  if (typeof errorBody === 'object' && errorBody !== null && 'message' in errorBody
    && typeof (errorBody as { message: unknown }).message === 'string') {
    return (errorBody as { message: string }).message;
  }
  return 'Request failed';
}

export const corpusApi = new CorpusApiClient(API_BASE);
```

`src/hooks/useCorpus.ts`:

```typescript
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { corpusApi } from '../api/corpusApi';
import type { CorpusRefreshResponse, CorpusRun, CorpusStatus } from '../types/corpus';

export const corpusKeys = {
  all: ['corpus'] as const,
  status: () => [...corpusKeys.all, 'status'] as const,
  runs: () => [...corpusKeys.all, 'runs'] as const,
  runLog: (runId: number) => [...corpusKeys.all, 'run-log', runId] as const,
};

export function useCorpusStatus() {
  return useQuery<readonly CorpusStatus[]>({
    queryKey: corpusKeys.status(),
    queryFn: () => corpusApi.getStatus(),
    refetchInterval: 10 * 1000,
  });
}

export function useCorpusRuns(limit = 30) {
  return useQuery<readonly CorpusRun[]>({
    queryKey: [...corpusKeys.runs(), limit],
    queryFn: () => corpusApi.getRuns(limit),
    refetchInterval: 10 * 1000,
  });
}

export function useRefreshCorpus() {
  const queryClient = useQueryClient();
  return useMutation<CorpusRefreshResponse, Error, string>({
    mutationFn: (key: string) => corpusApi.refresh(key),
    onSettled: () => {
      void queryClient.invalidateQueries({ queryKey: corpusKeys.all });
    },
  });
}

export function useCorpusRunLog(runId: number | null) {
  return useQuery<string>({
    queryKey: corpusKeys.runLog(runId ?? -1),
    queryFn: () => corpusApi.getRunLog(runId as number),
    enabled: runId !== null,
    refetchInterval: 3 * 1000,
  });
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `npm test -- corpusApi`
Expected: 3 passed. `npm run lint`도 클린.

- [ ] **Step 5: Commit** (temis-ops 리포에, push 금지)

```bash
git add src/types/corpus.ts src/api/corpusApi.ts src/hooks/useCorpus.ts src/api/corpusApi.test.ts
git commit -m "feat(ops): corpus API 클라이언트/훅 — /api/admin/corpus 소비 계층"
```

---

### Task 3: temis-ops — CorpusPage + 탭 통합 + 계약 문서

**Files:** (리포: temis-ops, 브랜치 corpus-tab)
- Create: `src/components/CorpusPage.tsx`
- Modify: `src/App.tsx` (views.corpus + NavButton + 렌더 분기 + AdminTokenControl invalidate)
- Modify: `docs/api-contract.md` (Corpus API 섹션)
- Test: `src/components/CorpusPage.test.tsx`

**Interfaces:**
- Consumes: Task 2의 useCorpusStatus/useCorpusRuns/useRefreshCorpus/useCorpusRunLog, corpusKeys

- [ ] **Step 1: Write the failing test**

`src/components/CorpusPage.test.tsx` (SystemStatusPage.test.tsx 패턴):

```typescript
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { CorpusPage } from './CorpusPage';

const fetchMock = vi.fn();

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function renderCorpusPage() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  function Wrapper({ children }: { readonly children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  }
  return render(<CorpusPage />, { wrapper: Wrapper });
}

const statusRow = {
  key: 'std', label: '회계 기준서(K-IFRS/GAAP)', total: 21375,
  last_collected: '2026-07-02 05:36:49', db_exists: true,
  last_run: { status: 'ok', new_count: 0, total_after: 21375, finished_at: '2026-07-02 19:12:01' },
  busy: false,
};
const runRow = {
  id: 1, corpus: 'nts_qt', started_at: '2026-07-02 19:11:57',
  finished_at: '2026-07-02 19:12:01', status: 'ok', new_count: 0, total_after: 139617,
};

beforeEach(() => {
  vi.stubGlobal('fetch', fetchMock);
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes('/api/admin/corpus/status')) return Promise.resolve(jsonResponse([statusRow]));
    if (url.includes('/api/admin/corpus/runs')) return Promise.resolve(jsonResponse([runRow]));
    return Promise.resolve(jsonResponse({}, 404));
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  fetchMock.mockReset();
});

describe('CorpusPage', () => {
  it('코퍼스 카드와 실행 이력을 렌더링한다', async () => {
    renderCorpusPage();
    await waitFor(() => expect(screen.getByText('회계 기준서(K-IFRS/GAAP)')).toBeTruthy());
    expect(screen.getByText('21,375')).toBeTruthy();
    expect(screen.getByText('nts_qt')).toBeTruthy();
  });

  it('Refresh 클릭 시 refresh API를 호출한다', async () => {
    renderCorpusPage();
    await waitFor(() => expect(screen.getByText('회계 기준서(K-IFRS/GAAP)')).toBeTruthy());
    fetchMock.mockResolvedValueOnce(jsonResponse({ started: true, corpus: 'std' }, 202));
    fireEvent.click(screen.getByRole('button', { name: /refresh/i }));
    await waitFor(() => {
      const called = fetchMock.mock.calls.some(([u]) => String(u).includes('/api/admin/corpus/std/refresh'));
      expect(called).toBe(true);
    });
  });

  it('수집 서버 다운(503)이면 안내 배너를 보여준다', async () => {
    fetchMock.mockImplementation(() => Promise.resolve(
      jsonResponse({ detail: { message: '수집 서버(fino_ops)에 연결할 수 없습니다: refused' } }, 503),
    ));
    renderCorpusPage();
    await waitFor(() => expect(screen.getByText(/fino_ops/)).toBeTruthy());
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `npm test -- CorpusPage`
Expected: FAIL — CorpusPage 모듈 없음

- [ ] **Step 3: Write implementation**

`src/components/CorpusPage.tsx`:

```tsx
import { useState } from 'react';
import { useCorpusRunLog, useCorpusRuns, useCorpusStatus, useRefreshCorpus } from '../hooks/useCorpus';
import type { CorpusStatus } from '../types/corpus';

function freshnessBadge(lastCollected: string | null): { label: string; className: string } {
  if (!lastCollected) return { label: '이력 없음', className: 'bg-surface-highlight text-text-muted' };
  const ms = Date.now() - new Date(lastCollected.replace(' ', 'T')).getTime();
  const days = ms / 86_400_000;
  if (Number.isNaN(days)) return { label: '알 수 없음', className: 'bg-surface-highlight text-text-muted' };
  if (days < 7) return { label: '최신', className: 'bg-primary/10 text-primary' };
  if (days < 30) return { label: `${Math.floor(days)}일 경과`, className: 'bg-amber-500/10 text-amber-400' };
  return { label: `${Math.floor(days)}일 경과`, className: 'bg-red-500/10 text-red-400' };
}

export function CorpusPage() {
  const status = useCorpusStatus();
  const runs = useCorpusRuns();
  const refresh = useRefreshCorpus();
  const [logRunId, setLogRunId] = useState<number | null>(null);
  const log = useCorpusRunLog(logRunId);

  const anyBusy = (status.data ?? []).some((c) => c.busy);
  const errorMessage = status.error instanceof Error ? status.error.message : null;

  return (
    <div className="space-y-6">
      {errorMessage && (
        <div className="rounded-lg border border-red-500/40 bg-red-500/10 px-4 py-3 text-sm text-red-300">
          {errorMessage.includes('fino_ops')
            ? `${errorMessage} — frwaler 서버에서 uvicorn(:8500) 기동이 필요합니다.`
            : errorMessage}
        </div>
      )}
      {refresh.error instanceof Error && (
        <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-4 py-3 text-sm text-amber-300">
          {refresh.error.message}
        </div>
      )}

      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
        {(status.data ?? []).map((c: CorpusStatus) => {
          const badge = freshnessBadge(c.last_collected);
          return (
            <div key={c.key} className="rounded-xl border border-border-dark bg-surface-dark p-4">
              <div className="flex items-center justify-between gap-2">
                <p className="text-sm font-semibold text-text-primary">{c.label}</p>
                <span className={`rounded-full px-2 py-0.5 text-xs font-semibold ${badge.className}`}>{badge.label}</span>
              </div>
              <p className="mt-2 text-2xl font-bold text-text-primary">{c.total?.toLocaleString() ?? '—'}</p>
              <p className="mt-1 text-xs text-text-muted">마지막 수집 {c.last_collected ?? '—'}</p>
              <p className="text-xs text-text-muted">
                최근 실행 {c.last_run ? `${c.last_run.status} (+${c.last_run.new_count ?? 0})` : '—'}
              </p>
              <button
                type="button"
                onClick={() => refresh.mutate(c.key)}
                disabled={anyBusy || refresh.isPending}
                className="mt-3 w-full rounded-lg bg-primary px-3 py-1.5 text-sm font-semibold text-stone-950 hover:bg-primary-dark disabled:cursor-not-allowed disabled:bg-surface-highlight disabled:text-text-muted"
              >
                {anyBusy ? '수집 실행 중…' : 'Refresh'}
              </button>
            </div>
          );
        })}
      </div>

      <section>
        <h2 className="mb-2 text-sm font-semibold uppercase tracking-wide text-text-tertiary">실행 이력</h2>
        <div className="overflow-x-auto rounded-xl border border-border-dark bg-surface-dark">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-border-dark text-xs uppercase text-text-muted">
              <tr>
                <th className="px-3 py-2">#</th><th className="px-3 py-2">코퍼스</th>
                <th className="px-3 py-2">시작</th><th className="px-3 py-2">상태</th>
                <th className="px-3 py-2">신규</th><th className="px-3 py-2">총계</th>
                <th className="px-3 py-2">로그</th>
              </tr>
            </thead>
            <tbody>
              {(runs.data ?? []).map((r) => (
                <tr key={r.id} className="border-b border-border-dark/60">
                  <td className="px-3 py-2 text-text-muted">{r.id}</td>
                  <td className="px-3 py-2 text-text-primary">{r.corpus}</td>
                  <td className="px-3 py-2 text-text-secondary">{r.started_at}</td>
                  <td className={`px-3 py-2 font-semibold ${r.status === 'ok' ? 'text-primary' : r.status === 'running' ? 'text-sky-400' : 'text-red-400'}`}>{r.status}</td>
                  <td className="px-3 py-2">{r.new_count ?? '—'}</td>
                  <td className="px-3 py-2">{r.total_after?.toLocaleString() ?? '—'}</td>
                  <td className="px-3 py-2">
                    <button type="button" className="text-primary underline" onClick={() => setLogRunId(r.id)}>보기</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      {logRunId !== null && (
        <section className="rounded-xl border border-border-dark bg-surface-deep p-4">
          <div className="mb-2 flex items-center justify-between text-xs text-text-muted">
            <span>run #{logRunId} 로그 (3초 갱신)</span>
            <button type="button" onClick={() => setLogRunId(null)} className="hover:text-text-primary">닫기 ✕</button>
          </div>
          <pre className="max-h-80 overflow-auto whitespace-pre-wrap text-xs text-emerald-300">{log.data ?? '(로그 로드 중…)'}</pre>
        </section>
      )}
    </div>
  );
}
```

`src/App.tsx` 수정 3곳:
1. import 추가: `import { CorpusPage } from './components/CorpusPage';`, `import { corpusKeys } from './hooks/useCorpus';`
2. views에 `corpus: 'corpus'` 추가, Nav에 `<NavButton active={view === views.corpus} onClick={() => setView(views.corpus)}>Corpus</NavButton>` 추가, 렌더 분기를

```tsx
{view === views.status ? <SystemStatusPage /> : view === views.evidence ? <EvidenceDebugDashboard /> : <CorpusPage />}
```

3. AdminTokenControl의 saveToken/clearToken 각각에 `void queryClientInstance.invalidateQueries({ queryKey: corpusKeys.all });` 추가.

`docs/api-contract.md`에 섹션 추가 (기존 스타일에 맞춰):

```markdown
## Corpus APIs (fino_ops proxy)

수집 서버(fino_ops, frwaler 리포)의 상태를 backend가 중계합니다. backend env `TEMIS_FINO_OPS_URL`(기본 `http://127.0.0.1:8500`)로 위치를 지정하며, 수집 서버 미가동 시 503과 `detail.message`를 반환합니다.

### `GET /api/admin/corpus/status`
6개 코퍼스의 `{key, label, total, last_collected, db_exists, last_run, busy}` 배열.

### `POST /api/admin/corpus/{key}/refresh`
수집 시작(202). 이미 실행 중이면 409.

### `GET /api/admin/corpus/runs?limit=50`
최근 실행 이력.

### `GET /api/admin/corpus/runs/{id}/log?tail=200`
실행 로그 tail (text/plain).
```

- [ ] **Step 4: Run tests + lint + build**

Run: `npm test` → 전체 통과(기존 + 신규 6). `npm run lint` → 클린. `npm run build`(package.json에 build:current 있으면 그것) → exit 0.

- [ ] **Step 5: Commit**

```bash
git add src/components/CorpusPage.tsx src/components/CorpusPage.test.tsx src/App.tsx docs/api-contract.md
git commit -m "feat(ops): Corpus 탭 — 6코퍼스 신선도/수집 트리거/실행 이력/로그"
```

---

### Task 4: 라이브 프록시 통합 검증 + frwaler 문서 마감

**Files:**
- Modify(frwaler): `docs/2026-07-02_fino_corpus_crawler_handoff.md` §5에 temis 연동 한 줄
- 검증 스크립트는 임시 실행만(커밋 없음)

- [ ] **Step 1: fino_ops 가동 확인** (frwaler에서)

```bash
curl -s -m 3 -o /dev/null -w "%{http_code}" http://127.0.0.1:8500/api/corpora
```
Expected: 200. 아니면 `cd /data_raid/ruci_workspace/frwaler && nohup .venv/bin/uvicorn crawler.fino_ops.api:app --host 127.0.0.1 --port 8500 > data/ops_logs/api_server.log 2>&1 &` 후 재확인.

- [ ] **Step 2: 라이브 프록시 검증** (fino-backend에서, MockTransport 없이 실제 :8500 — 가동 중 서비스 재시작 없음)

```bash
cd /data_raid/ruci_workspace/repo/finov2/fino-backend
.venv/bin/python - <<'EOF'
from types import SimpleNamespace
from fastapi.testclient import TestClient
import os
os.environ.setdefault("JWT_SECRET_KEY", "live-check")
from app.api.dependencies import get_admin_user
from app.main import app

app.dependency_overrides[get_admin_user] = lambda: SimpleNamespace(email="live@check", is_admin=True)
with TestClient(app) as c:
    r = c.get("/api/admin/corpus/status")
    rows = r.json()
    print("status:", r.status_code, "corpora:", len(rows))
    for row in rows:
        print(f"  {row['key']:<7} total={row['total']}")
    runs = c.get("/api/admin/corpus/runs?limit=2")
    print("runs:", runs.status_code, len(runs.json()))
app.dependency_overrides.clear()
EOF
```
Expected: `status: 200 corpora: 6` + law 6507 등 실값, runs 200.

- [ ] **Step 3: frwaler 핸드오프 갱신 + 커밋**

`docs/2026-07-02_fino_corpus_crawler_handoff.md` §5에 추가: "5. temis 연동: temis-ops Corpus 탭(브랜치 corpus-tab) + fino-backend `/api/admin/corpus/*`(브랜치 temis-corpus-ops) — 스펙 `2026-07-08-temis-corpus-tab-design.md`. 두 브랜치는 미푸시, 머지/배포는 temis 절차."

```bash
cd /data_raid/ruci_workspace/frwaler
git add docs/2026-07-02_fino_corpus_crawler_handoff.md
git commit -m "docs(temis): temis-ops 코퍼스 탭 연동 완료 기록 — finov2 두 리포 브랜치 포인터"
```

---

## Self-Review 결과

- 스펙 커버리지: BE 계약 4엔드포인트+503 포맷=Task 1, FE 소비계층=Task 2, 탭/UX/계약문서=Task 3, 라이브 검증+기록=Task 4. 갭 없음.
- 타입 일관성: corpusApi 반환 타입 ↔ types/corpus.ts ↔ BE passthrough(fino_ops 응답 형태) ↔ CorpusPage 사용처 일치. `OpsApiError(status, message, body)` 시그니처는 evidenceDebugApi 기존 정의 사용(Task 2 구현자가 실제 시그니처 확인 후 인자 순서 맞출 것 — 브리프 주의사항).
- 플레이스홀더 없음. 리스크 주의사항(브랜치 클린 체크, app import env, 실행 중 서비스 불간섭)을 해당 태스크 스텝에 명시.
- 알려진 한계: 운영 백엔드가 타 호스트면 `TEMIS_FINO_OPS_URL` 필요(스펙 Open Question), 배포는 사용자 handoff 절차.
