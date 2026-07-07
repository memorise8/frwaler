# TEMIS Ops 코퍼스 탭 — fino_ops 연동 설계 스펙 (2026-07-08)

## 목표

frwaler의 fino_ops 대시보드 기능(6코퍼스 상태·수집 트리거·실행 이력·로그)을 **TEMIS 운영 콘솔(temis-ops)** 안으로 편입한다. TEMIS의 기존 admin token 인증 아래에서 코퍼스 최신화를 관리할 수 있게 한다. (승인된 A안)

## 대상 리포 (frwaler 외부 — 별도 git)

| 리포 | 브랜치 전략 | 변경 |
|---|---|---|
| `repo/finov2/fino-backend` (현재 cbt, ahead 1) | **신규 브랜치 `temis-corpus-ops`** (cbt에서 분기) | admin 프록시 라우터 1개 + 테스트 |
| `repo/finov2/temis-ops` (현재 main, ahead 1) | **신규 브랜치 `corpus-tab`** (main에서 분기) | Corpus 탭 (api/hook/type/component/테스트) |
| frwaler | fino-crawler | 무변경 (fino_ops 그대로) |

- **push 금지** — 두 리포 모두 로컬 미푸시 커밋(layer 1)이 있고 배포는 사용자의 private git handoff 절차. 브랜치 머지/푸시는 사용자 결정.
- 다른 Claude 세션(repo-finov2)이 상주하나 워킹트리 클린 확인(2026-07-08) — 신규 브랜치로 격리.

## 아키텍처

```
temis-ops 브라우저 ──Bearer admin token──▶ fino-backend /api/admin/corpus/* ──httpx──▶ 127.0.0.1:8500 (fino_ops)
```

- fino_ops는 loopback 유지(무수정, 외부 미노출). 백엔드가 같은 머신이라는 전제(현재 사실), 원격이면 env `TEMIS_FINO_OPS_URL`로 조정.
- 인증은 기존 `get_admin_user` dependency 재사용 — temis admin 체계 그대로.

## 백엔드 계약 — `/api/admin/corpus/*` (신규 admin_corpus.py)

| 엔드포인트 | 프록시 대상 (fino_ops) | 비고 |
|---|---|---|
| `GET /status` | `GET /api/corpora` | 6코퍼스 stats+busy |
| `POST /{key}/refresh` | `POST /api/corpora/{key}/refresh` | 202/409/404 status 그대로 passthrough |
| `GET /runs?limit` | `GET /api/runs` | |
| `GET /runs/{id}/log?tail` | `GET /api/runs/{id}/log` | text passthrough |

- fino_ops 미가동/연결 실패 → **503** + temis-ops 에러 포맷(`{"detail":{"message":"수집 서버(fino_ops)에 연결할 수 없습니다: ..."}}`).
- export(NDJSON 대용량)는 1단계 제외 — 인덱스 재생성 파이프라인 붙일 때 후속.
- 업스트림 주소: env `TEMIS_FINO_OPS_URL` (기본 `http://127.0.0.1:8500`). httpx(이미 requirements에 있음) 사용, timeout 10s.
- 테스트 훅: `build_client()` 팩토리를 monkeypatch해 MockTransport 주입.

## 프론트 — temis-ops Corpus 탭

기존 파일 패턴을 그대로 미러링(각 1파일):

- `src/types/corpus.ts` — `CorpusStatus`(key/label/total/last_collected/db_exists/last_run/busy), `CorpusRun`.
- `src/api/corpusApi.ts` — `OpsApiClient` 패턴 복제(`CorpusApiClient`): getStatus/refresh/getRuns/getRunLog(text). Bearer 헤더는 `adminSession.getStoredAdminToken()`, 에러는 `OpsApiError`.
- `src/hooks/useCorpus.ts` — `corpusKeys` + `useCorpusStatus`(refetchInterval 10s) + `useCorpusRuns` + `useRefreshCorpus`(mutation, 409 메시지 표면화) + `useCorpusRunLog`(선택 시 3s 폴링).
- `src/components/CorpusPage.tsx` — 코퍼스 카드 6개(총량·마지막 수집·신선도 배지·Refresh 버튼(busy 시 비활성)) + 실행 이력 테이블 + 로그 패널. 기존 다크 테마 토큰(bg-surface-dark 등) 사용.
- `src/App.tsx` — `views.corpus` 추가, NavButton "Corpus", AdminTokenControl invalidate에 corpusKeys 추가.
- `docs/api-contract.md` — Corpus API 계약 추가.
- 테스트: `CorpusPage.test.tsx` (SystemStatusPage.test.tsx 패턴 — fetch 모킹, 카드 렌더·refresh 클릭·503 배너).

## 오류 처리 UX

- 503(수집 서버 다운) → 카드 영역에 "수집 서버(fino_ops)가 꺼져 있습니다 — frwaler에서 uvicorn(:8500) 기동 필요" 안내.
- 409(수집 중) → 배너 "다른 수집이 실행 중입니다".
- 401/403(토큰 없음/무효) → 기존 temis-ops 관례(에러 표면화) 따름.

## 테스트/검증

- BE: pytest — 프록시 4엔드포인트(성공/409 passthrough/503/미인증 401·403), `get_admin_user` dependency_overrides + httpx.MockTransport. 기존 스위트 회귀 없음.
- FE: vitest(신규 컴포넌트/훅) + `npm run lint` + `npm run build:current 또는 build` 게이트.
- 통합: 로컬 fino_ops(:8500, 현재 가동 중) 대상 라이브 프록시 1회(TestClient + admin override, 모킹 없음) — 6코퍼스 실값 확인. **가동 중인 :18080 서비스 재시작은 하지 않음**(코드 반영은 사용자의 dev(38501)/배포 절차에서).

## Open Questions

- [ ] 운영(api.financenow.kr) 백엔드가 이 머신과 다른 호스트면 `TEMIS_FINO_OPS_URL` 설정 또는 네트워크 경로 필요 — 배포 시 확인.
- [ ] :3050 Next.js 대시보드 존치 여부 — temis 탭 안정화 후 결정.
