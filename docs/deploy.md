# 배포 가이드 — livertree (single-image)

## 개요

`Dockerfile` + `docker-compose.yml` 은 모든 구성요소를 **하나의 컨테이너**
로 실행하는 single-image 배포 (Playwright Python 베이스 + Node 22 + Codex
CLI). 두 개의 supervisord 프로그램이 컨테이너 안에서 돕는다:

- `finolaw` — Next.js 16 admin UI (포트 3001)
- `scheduler` — 매일 03:00 (Asia/Seoul) `cron_crawl.sh loop` 실행

크롤러 ad-hoc 명령은 `docker compose exec app python -m crawler.main …`
로 컨테이너 안에서 실행한다 (별도 컨테이너 불필요).

## 시스템 요구사항

| 자원 | 권장 |
|---|---|
| OS | Linux (Ubuntu 22.04 / Debian 12 등 systemd 기반) |
| Docker | 24.x 이상, `docker compose` v2 포함 |
| 디스크 | 50 GB 이상 — 1조 도큐먼트 한도 안에서 점진 확장 |
| 메모리 | 4 GB (Playwright + Next + 요약 LLM 호출 버퍼) |
| 네트워크 | 아웃바운드 HTTPS (수집 사이트 + OpenAI / Gemini API) |

## 첫 배포 절차

```bash
# 1. 코드 + 환경 변수 준비
git clone <repo> livertree && cd livertree
cp .env.example .env
cp crawler/.env.example crawler/.env
# 두 파일에 OPENAI_API_KEY 등 키를 채운다.

# 2. 빌드 + 실행 (이미지 빌드는 5-10분 소요)
docker compose build
docker compose up -d

# 3. DB 초기화 (한 번만)
docker compose exec app python -m scripts.migrate_livertree
docker compose exec app python -m scripts.migrate_documents_fts

# 4. 첫 수집 (사이트별)
docker compose exec app python -m crawler.main crawl ntrs --limit 50
docker compose exec app python -m crawler.main download ntrs
docker compose exec app python -m crawler.main convert ntrs
docker compose exec app python -m crawler.main summarize ntrs

# 5. UI 확인
xdg-open http://localhost:3001    # 또는 브라우저에서 직접
```

## 컨테이너 구조

| 디렉터리 (호스트) | 컨테이너 마운트 | 용도 |
|---|---|---|
| `./data/` | `/app/data` | `papers.db` + 12자리 파일 저장 |
| `./.cache/` | `/app/.cache` | Codex/크롤러 임시 로그 |
| `./crawler/sites/custom/` | 동일 | Codex-generated 크롤러 (Auto-Add) |
| `./crawler/sites/configs/` | 동일 | 사이트별 JSON config |

이미지는 약 1.8 GB (Playwright 브라우저 포함) 이며 빌드 시 layer 캐시
가 작동하도록 `requirements.txt` / `package-lock.json` 을 먼저 복사한다.

## 환경 변수 (`.env`, `crawler/.env`)

| 변수 | 용도 | 위치 |
|---|---|---|
| `OPENAI_API_KEY` | 요약 + Auto-Add | `crawler/.env` |
| `GEMINI_API_KEY` 또는 `GOOGLE_API_KEY` | 대체 LLM | `crawler/.env` |
| `LLM_PROVIDER` | `gpt` / `gemini` | `crawler/.env` (기본 `gpt`) |
| `LIVERTREE_DATA_ROOT` | 파일 저장 루트 | `crawler/.env` (compose 에서 `/app/data` 자동 설정) |
| `TZ` | 타임존 (스케줄러) | `.env` (`Asia/Seoul`) |
| `ADMIN_BASIC_AUTH_*` | finolaw 관리자 인증 | `.env` |

## 일상 운영

| 작업 | 명령 |
|---|---|
| 사이트 통계 | `docker compose exec app python -m crawler.main stats` |
| 단일 사이트 즉시 수집 | `docker compose exec app python -m crawler.main crawl <site> --limit 100` |
| 야간 배치 강제 실행 | `docker compose exec app bash /app/scripts/cron_crawl.sh once` |
| 스케줄러 켜기/끄기 | `docker compose exec app supervisorctl start/stop scheduler` |
| 크롤링 정확도 점검 | `docker compose exec app python -m scripts.qa_crawl_accuracy --limit 200 --xlsx /app/logs/qa.xlsx` |
| 요약 품질 점검 | `docker compose exec app python -m scripts.qa_summary_quality --sample 30 --judge gpt` |
| Next.js 로그 | `docker compose logs -f app` |
| 스케줄러 로그 | `tail -f logs/scheduler.log` (호스트 마운트) 또는 `docker compose exec app tail -f /app/logs/scheduler.log` |

## 백업

```bash
# 핵심 자산 = data/papers.db + data/AAAA/BBBB/*
tar -C data -czf backup-$(date +%Y%m%d).tgz papers.db .
```

WAL 모드라 hot copy 가 안전하지만 가능하면 스케줄러를 일시 중지
(`supervisorctl stop scheduler`) 후 백업을 권장한다.

## 트러블슈팅

| 증상 | 원인 / 조치 |
|---|---|
| `/admin/status` 가 0건만 표시 | 첫 수집 미실행. `crawl <site>` 실행 |
| 검색 결과가 LIKE 만 사용 | `documents_fts` 미생성. `migrate_documents_fts` 실행 |
| 재요약 버튼 500 | `OPENAI_API_KEY` 미설정 또는 `python3` 경로 문제. `crawler/.env` 확인 |
| 컨테이너 healthcheck 실패 | Next.js 부팅 30초+ 걸림. `docker compose logs app` 로 stack trace 확인 |
| 크롤러가 차단 | `playwright` 모드 / `--delay` 늘리기 |
| 디스크 full | `data/AAAA/BBBB/` 백업 후 정리, 또는 `LIVERTREE_DATA_ROOT` 별도 디스크로 |

## 클라우드 적응

Single-image 이미지 그대로 사용 가능:

- **AWS**: ECS Fargate task (1개) + EFS 마운트 (`./data` 위치). ALB 로 3001 포트 종단.
- **자체 호스팅**: 위 절차 그대로 + Caddy/Traefik 으로 HTTPS 종단.
- **Vercel + 별도 백엔드**: finolaw UI 만 Vercel 배포, 컨테이너는 별도 VM. UI 의 `better-sqlite3` 가 원격 DB 안 되므로 별도 read API 가 필요 (현 아키텍처에서는 single-image 권장).
