# Libertree 납품 MVP — 개발 현황 및 다음 단계

> 작성일: 2026-08-12
> 작업 브랜치: `libertree`
> FE MVP 기준 커밋: `08a2c75` (`feat(delivery): add crawler operations frontend MVP`)
> 목적: DB와 블롭이 있는 기존 환경에서 다른 Codex 세션이 안전하게 개발을 이어가기 위한 짧은 핸드오프

## 1. 1차 납품 목표

고객사 서버에서 다음 흐름이 동작하는 최소 제품을 납품한다.

```text
FE에서 국가/자료 유형 선택
→ 크롤러 선택
→ 상태 확인 또는 수집 실행
→ BE가 작업 등록
→ Worker가 크롤러 실행
→ PostgreSQL에 문서 저장
→ FE에서 작업 결과·DB 상태·최신화 상태 확인
```

1차 범위는 크롤러 실행, 수집 DB 상태, 최신화 확인이다. 정교한 문서 검색, AI 요약, 신규 크롤러 AI 생성, 역할별 권한은 후속 범위로 둔다.

## 2. 현재 구현 상태

### 2.1 크롤러

- `crawler/sites/custom/`: 커스텀 크롤러 800개.
- 기본 크롤러와 레지스트리를 포함한 Python 파일은 Git에 추적되어 있다.
- `crawler/sites/configs/`: 설정 파일 436개. `noc-ac-uk.json` 1개는 과거 확인 시 JSON 문법 오류가 있었다.
- 과거 최종 감사 자료: `scripts/audit/crawler_status_final.csv`.
  - 전체 804개.
  - 2026-08-06 당시 실제 임시 DB 저장 성공 772개.
  - 실패·확인 필요 32개.
- 위 수치는 현재 실시간 상태가 아니라 과거 스냅샷이다.
- 공통 Health 실측 로직: `scripts/audit/delivery_health.py`.

### 2.2 납품 BE·Worker Phase 0

위치: `delivery/`

- PostgreSQL 백엔드: `crawler/db_pg.py`.
- SQLite/PostgreSQL 선택: `crawler/db_backend.py`.
- 작업 큐: `crawl_jobs` + `FOR UPDATE SKIP LOCKED`.
- Worker: `delivery/worker/worker.py`.
- FastAPI:
  - `GET /health`
  - `POST /jobs`
  - `GET /jobs/{job_id}`
  - `GET /documents/{seq_id}`
- Compose: `delivery/docker-compose.yml`.
- Phase 0 E2E: `delivery/scripts/e2e_phase0.sh`.
- 현재 Compose에는 `postgres`, `be`, `worker`만 있고 새 FE는 아직 합류하지 않았다.

### 2.3 새 납품 FE MVP

위치: `delivery/fe/`
개발 포트: `3002`

구현됨:

- E-CIP/Libertree 계열의 정보 포털 스타일.
- 과거 크롤러 상태 요약.
- 국가별 크롤러 분류.
- 자료 유형별 분류:
  - 논문
  - 보고서
  - 보도자료
  - 간행물
  - 공공데이터
  - 통계
  - 연구자료
  - PDF 검색 색인
  - 기타
- 국가 또는 자료 유형을 선택한 경우에만 해당 크롤러 목록 표시.
- 상태·실패 유형·국가·자료 유형 필터.
- 크롤러 상세 화면.
- 실행 프리셋:
  - 상태 확인: 최대 3건
  - 시험 수집: 최대 20건
  - 제한 수집: 최대 100건
- 증분/전체 모드 선택.
- FE `POST /api/jobs`가 `BE_URL`의 `POST /jobs`로 작업 등록을 프록시.
- BE 기본 주소: `http://127.0.0.1:8080`.
- Docker 내부에서는 `BE_URL=http://be:3001`로 설정해야 한다.

검증됨:

```bash
cd delivery/fe
npm run typecheck
npm run lint
npm run build
```

세 명령 모두 2026-08-12 기준 통과했다.

### 2.4 기존 관리자 FE 상태 MVP

초기에 `finolaw`에도 `/crawler-status` 페이지를 추가했다. 최종 납품 UI 개발은 `delivery/fe`를 기준으로 진행한다. `finolaw` 구현은 비교·참고용이며 새 기능을 양쪽에 중복 구현하지 않는다.

## 3. 현재 데이터 상태와 납품 결정

2026-08-12 운영 정본 SQLite를 PostgreSQL로 이관하고 실데이터 검증을 완료했다. 납품은 기존의 빈 시작 전제 대신 다음 구성을 사용한다.

- PostgreSQL: 이관된 실데이터를 dump/restore 방식으로 동봉.
- blob: 약 1.8TB이므로 Git이나 DB dump에 넣지 않고 FTP 등 별도 채널로 전달.
- SQLite: 원본 보관·감사 기준이며 납품 서비스의 운영 DB로 직접 사용하지 않음.
- 상세 측정값·해시·스냅샷 경로: `docs/DELIVERY_DATABASE_AUDIT_20260812.md`.

현재 정본은 사이트 805개, 문서 536,017건, PDF 확보 문서 303,245건, 텍스트 확보 문서 288,124건이다. FE의 실제 DB 현황 화면은 이 수치를 하드코딩하지 않고 BE 집계 API에서 조회해야 한다.

## 4. 국가·자료 유형 분류 현황

### 국가

- `scripts/audit/capacity_final.csv`의 `site_id → sheet`를 사용한다.
- `libertree-app/src/lib/categories.ts`가 `sheet → 국가·대륙`을 매핑한다.
- 804개 중 803개가 국가에 매핑된다.
- `Israel Ministries` 1개가 현재 `기타`로 처리된다.

### 자료 유형

- `libertree-app/src/lib/doc-type-map.generated.ts` 사용.
- 804개 중 762개가 명시적으로 매핑된다.
- 미매핑 42개는 현재 `기타`다.
- 현재 분류는 개별 문서가 아닌 사이트/크롤러 단위다.
- 실제 DB 확보 후 문서 메타데이터를 이용한 문서 단위 분류는 별도 개선한다.

## 5. DB가 있는 환경에서 시작하는 방법

운영 디렉터리에 바로 `git pull`하지 않는다.

### 5.1 현재 환경 보존

```bash
git status
git branch --show-current
git log -5 --oneline
```

다음을 먼저 백업한다.

```text
*.db
*.db-wal
*.db-shm
libertree/
crawler/sites/custom/
crawler/sites/configs/
.env
crawler/.env
```

실제 비밀 파일은 Git에 추가하지 않는다.

### 5.2 별도 worktree 권장

```bash
git fetch origin
git worktree add ../frwaler-delivery origin/libertree
```

- 운영 DB: 읽기 전용 분석.
- DB 스냅샷: 최신화·중복 재수집 테스트.
- 빈 임시 DB: Health 검사.
- 신규 PostgreSQL: 납품 시스템 통합 테스트.

운영 DB에 Health 검사나 시험 크롤을 직접 실행하지 않는다.

## 6. 다음 개발 계획

각 단계는 길게 이어가지 않고 독립 검증 후 커밋한다. 한 단계가 끝나기 전 다음 단계의 대규모 변경을 섞지 않는다.

### 단계 0 — 실제 DB 정본 확인

예상: 0.5일

작업:

1. DB 후보, WAL/SHM, 블롭 경로 확인.
2. 각 DB의 테이블·문서 수·최신 `collected_at` 비교.
3. 실제 운영 정본과 스냅샷 경로 확정.
4. 읽기 전용 DB 감사 결과를 JSON/Markdown으로 저장.

완료 조건:

- 정본 DB 경로가 문서화됨.
- 원본 해시·크기·최신 시각 기록.
- 원본을 수정하지 않고 분석 쿼리가 통과함.

권장 커밋:

```text
docs(delivery): identify canonical database and blob snapshot
```

2026-08-12 완료. 결과는 `docs/DELIVERY_DATABASE_AUDIT_20260812.md`를 기준으로 한다. 최종 PostgreSQL dump restore와 blob manifest 검증은 납품 패키징 단계에서 수행한다.

### 단계 1 — 실제 DB 현황 API·FE

예상: 0.5~1일

작업:

1. 전체 문서·사이트·PDF·텍스트 수 집계.
2. 국가별 문서 수 집계.
3. 자료 유형별 문서 수 집계.
4. 누락·중복·파일 정합성 요약.
5. `delivery/fe`에 `DB 현황` 화면 추가.

완료 조건:

- 화면 수치와 직접 SQL 결과 일치.
- 모든 수치에 기준 DB와 측정 시각 표시.
- DB가 없을 때 과거 스냅샷과 명확히 구분.

권장 커밋:

```text
feat(delivery): add measured database status dashboard
```

### 단계 2 — Compose에 FE 통합

예상: 0.5일

작업:

1. `delivery/Dockerfile.fe` 작성.
2. `delivery/docker-compose.yml`에 `fe` 추가.
3. `BE_URL=http://be:3001` 설정.
4. 외부 노출은 FE 포트만 기본으로 구성.
5. Healthcheck 추가.

완료 조건:

```bash
docker compose -f delivery/docker-compose.yml up --build
```

- PostgreSQL, BE, Worker, FE가 모두 healthy.
- FE에서 BE health 확인.

권장 커밋:

```text
feat(delivery): integrate frontend into compose stack
```

### 단계 3 — 대표 크롤러 실제 실행 E2E

예상: 0.5~1일

대표 유형:

- 일반 HTML.
- JSON/API.
- 논문 사이트.
- 보고서 사이트.
- Playwright 필요 사이트.

작업:

1. FE에서 최대 3건 작업 등록.
2. Worker가 실제 크롤러 실행.
3. PostgreSQL 문서 저장 확인.
4. 성공·실패·저장 건수·로그 표시.
5. Playwright Chromium/시스템 의존성 보완.

완료 조건:

- 최소 4개 유형에서 `queued → running → done` 확인.
- 실패 1건이 `failed`와 오류 메시지로 표시됨.
- 운영 DB가 변경되지 않음.

권장 커밋:

```text
feat(delivery): complete crawler run end-to-end flow
```

### 단계 4 — 작업 현황·로그·취소

예상: 0.5~1일

작업:

1. BE 작업 목록 API.
2. 작업 로그 저장·조회.
3. 대기 작업 취소.
4. 실패 작업 재시도.
5. FE `수집 작업` 화면.

완료 조건:

- 작업 목록이 새로고침 후 유지됨.
- 실행 상태와 저장 건수가 표시됨.
- 취소·재시도 동작 검증.

권장 커밋:

```text
feat(delivery): add job monitoring logs and retry controls
```

### 단계 5 — 현재 Crawler Health

예상: 1일 + 전체 검사 실행 시간

작업:

1. `delivery_health.py` 로직을 Worker 작업으로 분리.
2. 임시 DB·임시 블롭에 최대 3건 저장.
3. 사이트별 타임아웃 적용.
4. 현재 상태와 2026-08-06 상태 분리 표시.
5. 선택 검사부터 구현 후 전체 검사 확장.

상태:

```text
미검사 / 대기 / 실행 중 / 현재 정상 / 코드 오류 / 차단 / 키 필요 / 타임아웃 / 네트워크 오류
```

완료 조건:

- 대표 10개 검사 결과가 수동 실행과 일치.
- Health 데이터가 운영 문서 DB에 저장되지 않음.
- 전체 검사는 동시성 제한과 재개가 가능함.

권장 커밋:

```text
feat(delivery): add isolated crawler health checks
```

### 단계 6 — 최신화 검증·화면

예상: 0.5~1일

작업:

1. 사이트별 마지막 수집일·최신 게시일 집계.
2. 7일·30일·90일 최신성 분포.
3. 대표 5~10개 사이트 증분 재수집.
4. 중복 증가 여부와 신규 문서만 추가되는지 검사.
5. FE `최신화 현황` 화면.

완료 조건:

- DB 스냅샷에서 재수집 전후 비교 보고서 생성.
- 중복 방지와 신규 추가가 SQL로 확인됨.
- 운영 DB는 미변경.

권장 커밋:

```text
feat(delivery): add freshness audit and incremental verification
```

### 단계 7 — 납품 패키징

예상: 0.5~1일

작업:

1. `.env.example` 정리.
2. 설치·운영·백업·복구 문서.
3. 관리자 인증 최소 적용.
4. 빈 서버 설치 시험.
5. 버전·변경 이력·SHA256 생성.

완료 조건:

- 새 서버에서 Compose 기동.
- FE에서 대표 크롤러 실행·저장·결과 확인.
- 재부팅 후 데이터 유지.
- 비밀정보 미포함.

권장 커밋:

```text
chore(delivery): prepare versioned customer delivery package
```

## 7. 우선순위와 중단 기준

금주 납품 기준 우선순위:

1. 단계 0: 실제 DB 정본 확인.
2. 단계 2: Compose FE 통합.
3. 단계 3: 대표 크롤러 실제 실행.
4. 단계 1: DB 현황 화면.
5. 단계 4: 작업 현황.
6. 단계 6: 최신화 검증.
7. 단계 7: 패키징.
8. 단계 5 전체 804 Health는 시간이 부족하면 대표 검사 + 과거 스냅샷으로 납품하고 전체 검사는 후속 실행한다.

각 단계에서 다음 조건이면 멈추고 원인을 문서화한다.

- 운영 DB에 쓰기가 발생할 가능성.
- 정본 DB가 확정되지 않음.
- 비밀정보가 로그나 Git에 포함될 가능성.
- 전체 크롤 실행이 대상 사이트에 과도한 요청을 보낼 가능성.

## 8. 최종 납품 완료 기준

- Docker Compose로 PostgreSQL, BE, Worker, FE 기동.
- FE에서 국가·자료 유형별 크롤러 탐색.
- FE에서 대표 크롤러 실행.
- 작업 상태·로그·저장 건수 확인.
- 실제 DB 현황 표시.
- 최신화 및 증분 검증 결과 표시.
- 과거 스냅샷과 현재 실측을 구분.
- 설치·운영·백업·복구 문서 제공.
- 운영 DB·비밀정보 무손상.

## 9. 바로 시작할 때 확인할 명령

```bash
git status -sb
git log -3 --oneline
find . -type f \( -name '*.db' -o -name '*.db-wal' -o -name '*.db-shm' \) -not -path '*/node_modules/*'
find . -maxdepth 3 -type d -name libertree
cd delivery/fe && npm ci && npm run typecheck && npm run lint && npm run build
```

문서가 작성된 현재 작업공간에서는 실제 DB가 없으므로, 다음 세션의 첫 작업은 **단계 0 — 실제 DB 정본 확인**이다.
