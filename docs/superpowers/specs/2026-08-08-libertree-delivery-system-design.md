# Libertree 납품 시스템 설계 (Delivery System Design)

> 작성 2026-08-08. 브레인스토밍 승인 완료. 이 문서는 납품용 자급자족 수집·열람 시스템의 단일 설계 진실(source of truth)이다.
> 관련 배경: `docs/CRAWLER_STATUS_FINAL.md`(크롤러 804곳 상태), 메모리 `project_crawler_status_final_20260806`.

## 1. 목적과 범위

클라이언트가 **자기 서버에서 Docker로 자체 호스팅**하며, 우리가 만든 크롤러들로 **직접 문서를 수집·열람·최신화**하는 시스템을 납품한다.

- **빈 시작(empty start)**: 우리 데이터를 동봉하지 않는다. 클라이언트가 자기 인프라에서 처음부터 수집한다. (데이터 동봉은 선택적 임포트 경로로만 지원.)
- **AI 없이 자급자족**: 코어(수집·저장·열람·스케줄·최신화)는 어떤 LLM도 필요 없다. 선택 모듈(문서 요약, 새 사이트용 크롤러 생성)만 OpenAI 키가 필요하며 기본 비활성.
- **역할 분리**: `operator`(열람·크롤 트리거·작업 현황)와 `technical`(크롤러 카탈로그 관리·스케줄·설정·새 사이트 추가).

### 범위 밖 (Non-goals)
- 콘텐츠 주제 분류(topic taxonomy) — 향후 과제.
- 대규모 데이터(1.7TB PDF) 전송 — 별도 결정/이메일 대기 중. 이 시스템은 빈 시작 기준.
- 크롤러 신규 제작(코드 수리 4곳 포함) — 별도 트랙.

## 2. 아키텍처 (4 컨테이너)

```
[fe: Next.js]  ──HTTP(REST)──▶  [be: FastAPI]  ──psycopg──▶  [postgres]
 (열람 + 운영/기술 콘솔)          (API + 오케스트레이션)              │
                                      │ APScheduler(예약)            │
                                      ▼ crawl_jobs 테이블 enqueue     │
                               [worker] ──크롤러 실행──▶ PG write ────┘
                                      │                       + PDF → [blob 볼륨]
                                      └── PDF 다운로드 ─────────────────┘
```

| 컨테이너 | 역할 |
|---|---|
| **postgres** | 메타데이터 단독 저장소(sites/documents/crawl_jobs/schedules/users/crawler_catalog). PDF는 저장 안 함. |
| **be (FastAPI)** | Postgres·크롤러 오케스트레이션 단독 소유. REST API 전량 제공. APScheduler가 예약 → `crawl_jobs`에 enqueue. |
| **worker** | `crawl_jobs`를 폴링 → 하나씩 크롤러 실행(기존 크롤러 코드 재사용, PG로 write) + PDF 다운로드(→ blob 볼륨). 동시성은 worker 레플리카 수로 조절. |
| **fe (Next.js)** | 기존 libertree-app의 디자인 시스템·라우트 골격 유지. 데이터층을 SQLite 직접읽기 → **BE API 호출**로 교체 + 운영/기술 콘솔 추가. |

**블롭(PDF)**: 파일시스템 유지. `blob_storage` 트리(`libertree/AAAA/BBBB/{12자리 seq_id}.pdf`)를 Docker named volume에 저장. be/worker/fe 모두 이 볼륨을 마운트(worker=write, be/fe=read).

## 3. 데이터 접근 아키텍처

- **BE API 단일 소스**: Postgres·블롭에 접근하는 주체는 be(+worker)뿐. FE는 BE REST API만 호출한다(SSR 서버컴포넌트에서 `fetch(BE_URL)`).
- 근거: DB 진실이 한 곳(be), 역할 인증을 API에서 일괄 처리, 쿼리 로직 이중관리 제거.
- 비용: 기존 `catalogue.ts`의 검색/집계/상세 쿼리를 FastAPI 엔드포인트로 이식해야 함(Phase 1).

## 4. 데이터 모델 (Postgres)

### 4.1 기존 SQLite 스키마 이식 (`db_libertree.py` → Postgres)
- `sites(site_id PK, site_name, site_url, sheet, created_at)`.
- `documents`: 기존 컬럼 전부. `seq_id` = `BIGINT GENERATED ALWAYS AS IDENTITY`(기존 INTEGER AUTOINCREMENT 대응). dedup = `UNIQUE (site_id, post_number, meta_url)`. **주의**: Postgres UNIQUE는 NULL을 서로 구별하므로 SQLite와 동일 시맨틱(post_number NULL 행 여러 개 허용). insert 시 `find_by_dedup_key`로 선조회 후 삽입(기존 동작 보존).
- `document_translations`, `document_lang`: 번역 파이프라인 테이블(존재 시 이식; 코어 동작에는 선택).

### 4.2 신규 테이블
- `crawl_jobs(id BIGSERIAL PK, site_id TEXT, mode TEXT[full|incremental], status TEXT[queued|running|done|failed|canceled], limit_n INT NULL, saved_count INT DEFAULT 0, error TEXT, requested_by TEXT, created_at, started_at, finished_at)`.
- `schedules(id BIGSERIAL PK, site_id TEXT NULL, group_key TEXT NULL, cron TEXT, mode TEXT, enabled BOOL, last_run TIMESTAMPTZ NULL, next_run TIMESTAMPTZ NULL)`.
- `users(username PK, password_hash TEXT, role TEXT[operator|technical], created_at)`.
- `crawler_catalog(site_id PK, site_name, country, category, status TEXT[된다|안된다], reason TEXT)` — `scripts/audit/crawler_status_final.csv`(804행)로 시드.

## 5. 크롤러 → Postgres 이식 전략

기존 크롤러 코드를 **수정 없이 재사용**하는 것이 목표(회귀 방지 + crawlers-share 공유 패키지 SQLite 단독 실행 유지).

- 신규 `crawler/db_pg.py`: `db_libertree.py`와 **동일한 공개 함수 시그니처**로 Postgres 구현. `open_db(dsn)`, `init_db(conn)`, `upsert_site(conn, site_id, site_name, site_url, sheet=None)`, `insert_document(conn, doc) -> int`, `update_document_pdf(...)`, `update_document_text(...)`, `update_document_summary(...)`, `get_max_post_number`, `get_last_meta_url`, `find_by_dedup_key`, `get_document`.
- 신규 `crawler/db_backend.py`: `get_backend()`가 `LIBERTREE_DB_BACKEND` 환경변수(`sqlite` 기본 / `postgres`)에 따라 `db_libertree` 또는 `db_pg` 모듈을 반환.
- `base_crawler.py` 수정(최소): `_save_paper_v2`가 `db_backend.get_backend().insert_document(...)`를 쓰고, `_save_paper`가 `LIBERTREE_DB_BACKEND == "postgres"`일 때 v2 경로를 타도록 라우팅(`_conn_is_libertree`의 SQLite pragma 검사에 의존하지 않음). **기본값 sqlite → 기존 동작·crawlers-share 무영향.**
- 블롭: `blob_storage`/`pdf_downloader`는 파일시스템 그대로. `LIBERTREE_BLOB_ROOT`만 볼륨 경로로.

## 6. 워커 실행 모델

- **큐**: `crawl_jobs` 테이블(별도 브로커 없음). 워커가 `SELECT ... FOR UPDATE SKIP LOCKED`로 `queued` 작업 1건을 원자적으로 집어 `running`으로 전이.
- **실행**: `crawlers-share/run.py`와 동일 패턴 — `CRAWLERS[site_id](db_conn=pg_conn, delay=...)` → `crawl(limit)`. 저장은 `_save_paper` 경로로 PG에 dedup insert. 크롤 후 PDF 미다운로드 문서에 대해 `pdf_downloader.download_pdf_for`로 blob 볼륨에 저장.
- **진행/종료**: 저장 건수를 주기적으로 `crawl_jobs.saved_count`에 반영. 완료 시 `done`, 예외 시 `failed`+error, 취소 요청 시 `canceled`.
- **예약**: be의 APScheduler가 `schedules`의 cron을 읽어 만기 시 `crawl_jobs`에 `mode=incremental` 작업 enqueue. 실행은 항상 워커가.
- **동시성**: `docker compose up --scale worker=N`. 각 워커는 SKIP LOCKED로 서로 다른 작업을 집으므로 충돌 없음.

## 7. 최신화(freshness)·증분

- **증분 = insert 계층 dedup 내장**: 재크롤 시 `(site_id, post_number, meta_url)` 중복은 자동 스킵 → 새 문서만 추가. 전 크롤러 공통 적용(이미 검증됨).
- **base 공통 조기종료(신규)**: `base_crawler`에 옵션 훅 — 한 리스트 페이지의 항목이 **전부 기존 dedup 히트**면 페이징 중단(증분 모드에서만). 전 사이트가 앞부분만 순회하게 되어 최신화가 빨라짐.
- **능동 최신화 확인**: 리스트 1페이지 최신 항목 vs DB 최신 대조(초 단위, LLM 없음). be가 `crawler_catalog`/`documents` 기준으로 "새 문서 있음/없음"을 보고.

## 8. 인증·역할

- `users` 테이블 + 서버 세션(쿠키). 2역할:
  - `operator`: 열람 전 라우트 + 크롤 트리거 + 작업 현황 조회.
  - `technical`: operator 전부 + 크롤러 카탈로그 관리 + 스케줄 CRUD + 설정 + 새 사이트 추가(선택 AI).
- BE가 라우트별 역할을 강제. FE는 역할에 따라 콘솔 메뉴를 노출/숨김.
- 초기 계정: `.env`의 부트스트랩 자격증명으로 최초 `technical` 계정 생성.

## 9. AI-free 정책 (변경 금지: 메모리 `feedback_cost_model`)
- 코어는 LLM 0. 선택 모듈만:
  - **문서 요약**: OpenAI API 키(있을 때만 UI 노출).
  - **새 사이트 크롤러 생성**: 생성기는 OAuth(기존 방침 유지), analyzer/summarizer는 API 키.
- 키 미설정 시 해당 기능은 UI에서 비활성, 코어는 완전 동작.

## 10. 단계별 빌드 (각 단계 = 독립 실행·검증 가능한 소프트웨어)

| Phase | 산출물 | 완료 검증 |
|---|---|---|
| **0. 기반** | PG 스키마 + `db_pg.py`/`db_backend.py` 이식 + base_crawler 라우팅 + docker-compose 4컨테이너 골격 + 빈 시작 기동 | 워커가 크롤러 1곳을 PG에 write, be `/health` 200, 문서 조회 API가 그 문서 반환 |
| **1. 열람 API+FE** | be 카탈로그 API(검색/브라우즈/상세/블롭) + FE 데이터층 API 교체 | 브라우즈/검색/상세/PDF가 현재 앱과 기능 동등 |
| **2. 수집 제어** | 크롤러 카탈로그 API + 작업 트리거/현황/취소 + 운영자 콘솔 | UI에서 실제 크롤 트리거 → 문서 수집·현황 표시 |
| **3. 스케줄·최신화·역할** | 스케줄(APScheduler) + 증분/조기종료 + 최신화 확인 + 기술자 콘솔 + 역할 인증 | 예약 증분이 새 문서만 추가, 역할별 접근 제어 동작 |
| **4. 패키징** | `.env.example`·시드·매뉴얼(운영/기술)·export·선택 데이터임포트·선택 AI모듈 | 새 머신 `docker compose up` → 빈 시작 → 수집 → 열람 end-to-end |

각 Phase는 별도 구현 계획 문서(`docs/superpowers/plans/2026-08-08-libertree-delivery-phaseN.md`)로 작성한다.

## 11. 보안·운영 제약 (필수 준수)
- **비밀 무유출**: `crawler/.env`는 gitignore 유지. 납품 패키지에 실제 키 절대 미포함(`.env.example`는 키 이름만).
- **운영 DB 무손상**: 개발·검증은 임시/별도 PG 인스턴스에. 우리 SQLite 운영본은 건드리지 않음.
- **크롤러 회귀 방지**: `LIBERTREE_DB_BACKEND` 기본 sqlite → 기존 파이프라인·crawlers-share 무영향.

## 12. 검증 전략
- 각 Phase 끝에 통합 검증(위 표). 단위 테스트는 db_pg 이식(dedup·insert·조회), 백엔드 셀렉터, API 계약, 워커 큐 원자성(SKIP LOCKED)에 집중.
- 최종: 완전 초기화된 머신에서 `docker compose up` → technical 로그인 → 크롤 트리거 → 문서·PDF 수집 → operator 열람 → 예약 증분까지 무개입 통과.
