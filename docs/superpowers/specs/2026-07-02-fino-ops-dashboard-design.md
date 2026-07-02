# FINO Ops — 크롤러 통합 + 최신화 관리 대시보드 설계 스펙 (2026-07-02)

## 목표

frwaler의 크롤러 4종(fino_law, fino_acct, fino_std, NTS)을 **단일 진입점으로 통합**하고, 코퍼스별 **최신화 상태를 한눈에 보고 갱신을 트리거하는 웹 대시보드**를 만든다. FINO(서비스)는 1단계에서 export 산출물과 상태 API를 소비하고, 수집 트리거 API는 후속 연동에서 그대로 재사용한다.

**선택된 접근 = C(풀스택 UI) + 논리적 DB 통합.** Next.js 프론트 + FastAPI API 서버. DB 물리 통합(20GB papers.db 이관)은 제외 — 스키마 이질(문서형/조문형/판례형)과 20GB 재색인 리스크 대비 실익이 없고, 통합의 실제 요구(한눈 관리·단일 진입점)는 상태층으로 충족된다.

## 현황 (실측, 2026-07-02)

| 코퍼스 key | 소스 DB | 규모 | 기존 실행법 (제각각 — 이걸 흡수하는 것이 핵심) |
|---|---|---|---|
| `law` | data/fino_law.db (source_kind=law) | 6,507조문 | `python -m crawler.fino_law.collect --law` |
| `exec` | data/fino_law.db (source_kind=exec_standard) | 2,799조문 | `python -m crawler.fino_law.collect --exec` |
| `acct` | data/fino_acct.db | 4,540건 | `python -m crawler.fino_acct.collect --priorities 1,2,3,4,5,6` |
| `std` | data/fino_std.db | 21,375문단 | `python -m crawler.fino_std.collect` |
| `nts_qt` | crawler-poc/data/papers.db | 139,617행 | `FINOLAW_DB_PATH=… python -m crawler.main crawl nts-taxlaw-qt --incremental` |
| `nts_pd` | crawler-poc/data/papers.db | 151,041행 | 동일 (pd) |

모든 크롤러는 재실행=증분 최신화. 문제는 (1) 실행법이 6가지, (2) 최신화 이력·신선도를 보는 곳이 없음.

## 아키텍처

```
dashboard/            Next.js 15 (App Router, TS, Tailwind) — UI
   └─ /api/* rewrite → 127.0.0.1:8500 (FastAPI)
crawler/fino_ops/     Python 코어 + API 서버
   ├─ db.py           data/fino_ops.db — runs 테이블 (실행 이력·상태)
   ├─ corpora.py      6코퍼스 레지스트리 (stats SQL + refresh argv/env)
   ├─ runner.py       refresh 실행기 (subprocess, 락, new_count 산출)
   ├─ cli.py          python -m crawler.fino_ops {status,refresh}
   └─ api.py          FastAPI — /api/corpora, /api/runs, /api/export
data/fino_ops.db      상태 메타DB (신규)
data/ops_logs/        실행별 로그 파일
```

- 크롤러 코드는 **무수정**: runner가 기존 CLI를 subprocess로 호출(레지스트리에 argv+env 등록). 크롤러가 깨져도 대시보드는 error 상태로 기록하고 생존.
- 소비자 3종(CLI·대시보드·FINO)이 모두 같은 레지스트리+runs를 사용.

## 데이터 모델 — data/fino_ops.db

```sql
runs(id INTEGER PK, corpus TEXT, started_at TEXT, finished_at TEXT,
     status TEXT running|ok|error, new_count INTEGER, total_after INTEGER,
     log_path TEXT, error TEXT)
```

- `new_count` = refresh 전후 소스DB total의 차 (크롤러 stdout 파싱보다 견고).
- 서버 시작 시 잔존 `running` 행은 `error`(stale)로 마킹 (크래시 복구).

## 레지스트리 계약 (corpora.py)

```python
Corpus(key, label, db_path, count_sql, freshness_sql, argv, env)
corpus_stats(c) -> {total, last_collected, db_exists}   # 소스DB read-only 조회
```

- papers.db 경로는 `FINO_PAPERS_DB` env로 재정의 가능(기본 crawler-poc 경로).
- freshness = max(마지막 성공 run의 finished_at, 소스DB의 max collected/crawled/fetched_at).

## API (FastAPI, 127.0.0.1:8500 — 로컬 전용, 인증 없음)

| 엔드포인트 | 동작 |
|---|---|
| `GET /api/corpora` | 6코퍼스: label, total, last_collected, last_run(status·new_count·시각), busy |
| `POST /api/corpora/{key}/refresh` | 백그라운드 refresh 시작. **글로벌 동시 1개** — 실행 중이면 409 (소스 사이트 매너 + SQLite 단순성) |
| `GET /api/runs?limit=50` | 최근 실행 이력 |
| `GET /api/runs/{id}/log?tail=200` | 로그 tail (text) |
| `GET /api/export/{key}` | data/export/{key}.ndjson 파일 스트림 (있을 때; 없으면 404) — FINO 1단계 소비 지점 |

## 대시보드 UI (Next.js)

- **코퍼스 카드 6개**: label, 총 건수, 마지막 수집 시각, 신선도 배지(🟢 <7일, 🟡 <30일, 🔴 30일+), Refresh 버튼(busy 시 비활성+스피너).
- **실행 이력 테이블**: 최근 run들 (코퍼스·시각·상태·신규건수), 행 클릭 → 로그 뷰(3초 폴링 tail).
- 데이터는 5초 폴링(SSE 불필요 — YAGNI). 단일 페이지.
- `next.config.ts` rewrites로 `/api/*` → FastAPI. 배포 없음(로컬 `next dev`/`next start`).

## 오류 처리

- refresh subprocess 비정상 종료 → run status=error, stderr 포함 로그 보존, 카드에 마지막 실패 표시. 소스DB는 각 크롤러의 자체 보존 로직(멱등·문서단위 교체·부분실패 보존)에 위임.
- 소스DB 부재/잠김 → stats는 db_exists=false/total=null로 강등, API는 500 대신 정상 응답에 표시.

## 테스트

- pytest: runs 라이프사이클, 레지스트리 stats(임시 sqlite 픽스처), runner(가짜 argv로 성공/실패/락), API(TestClient, 409 포함). 실 크롤러 호출 없음.
- 프론트: `next build` + lint 게이트. E2E는 수동 체크리스트(카드 표시→std refresh→이력·로그 확인).

## FINO 연동 로드맵 (스펙 범위 밖, 기록용)

1단계(이 스펙): FINO가 `GET /api/export/{key}` + `GET /api/corpora`(신선도) 소비. 2단계(후속): FINO 에이전트가 `POST refresh` 직접 트리거 or MCP 래퍼. 3단계(선택): papers.db 물리 이전 여부 재검토.

## Open Questions

- [ ] 대시보드 상시 구동 방식(systemd/tmux) — 구현 후 운영에서 결정.
- [ ] acct 코퍼스의 last_collected 컬럼(fetched_at) 포맷 통일 여부 — 구현 중 실데이터로 확정.
