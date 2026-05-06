# 프로젝트 현재 상태

> 마지막 업데이트: 2026-04-24

---

## 한 줄 요약

Finolaw는 URL 기반 크롤러 자동 생성과 SQLite 검색 UI를 제공하는 Next.js 16 앱이다. 현재 활성 프론트엔드는 `finolaw/` 하나이며, 레거시 `web/` 프론트엔드는 제거되었다.

---

## 현재 활성 구성

### finolaw 웹 앱

- 경로: `finolaw/`
- 포트: `3001`
- 프레임워크: Next.js 16, React 19
- 인증: HTTP Basic Auth (`ADMIN_USER`, `ADMIN_PASSWORD`)
- 주요 화면:
  - 대시보드 `/`
  - 검색 `/search`
  - 상세 `/search/[id]`
  - Smart Find `/smart-find`
  - Auto-Add `/auto-add`
  - 크롤러 관리 `/crawler`

### Python crawler

- 경로: `crawler/`
- CLI 진입점: `python -m crawler.main`
- 주요 명령:
  - `crawl <site_id>`
  - `smart-find <url>`
  - `auto-add <url>`
  - `auto-add-codex <url>`
  - `stats`, `list-sites`

### 데이터/로그

- DB 경로: `data/data.db`
- DB 경로 override: `FINOLAW_DB_PATH`
- 로그 경로: `.cache/`
- 현재 체크아웃에서는 `data/data.db`가 없을 수 있다. DB가 없으면 UI는 실행되지만 대시보드/검색은 빈 상태가 된다.

---

## Auto-Add 경로

| 구분 | 상태 | 진입점 | 산출물 |
|------|------|--------|--------|
| Tier 2 Codex CLI | 메인 경로 | `finolaw /auto-add`, `python -m crawler.main auto-add-codex` | `crawler/sites/custom/*.py` |
| Tier 1 AutoAddAgent | 보조/내부 경로 | `python -m crawler.main auto-add` | `crawler/sites/configs/*.json` |

`crawler/codex_runner.py`는 직접 실행 CLI가 아니라 `crawler.main`에서 호출되는 래퍼 모듈이다.

---

## 배포 상태

- `Dockerfile`, `docker-compose.yml`는 단일 컨테이너 배포 구조다.
- 컨테이너는 Next.js, Python crawler, Playwright, Codex CLI를 함께 포함한다.
- 데이터/로그/생성 크롤러는 bind mount로 유지된다.
- 회사별 배포는 같은 Git 코드를 쓰되 `data/`, `.cache/`, `crawler/sites/custom/`, `crawler/sites/configs/`를 독립 운영한다.
- `crawler/sites/custom/*.py`와 `crawler/sites/configs/*.json`은 백업 대상 런타임 생성물이며 Git 커밋 대상이 아니다.

---

## 정리된 항목

- 레거시 `web/` 프론트엔드 제거 완료
- `finolaw/README.md`를 활성 FE 기준으로 갱신
- 현재 사용 문서에서 `localhost:3000`/`web/src` 혼동 제거 완료

---

## 남은 작업

| 우선순위 | 항목 | 설명 |
|----------|------|------|
| 1 | Docker 빌드 검증 | `docker compose build && docker compose up -d` 실제 확인 |
| 2 | Preflight 체크 | `/api/health` + UI 배너로 DB/API key/Codex/Python 상태 표시 |
| 3 | DB 초기화 가이드 | DB가 없는 새 환경에서 샘플 수집/초기화 경로 보강 |
| 4 | Codex 비용 계측 | 생성 1회당 토큰/비용 추정 로그/표시 |
| 5 | 로그 로테이션 | `.cache/*.log` 자동 정리 |
| 6 | robots.txt/약관 확인 | 크롤 전 운영자 확인 또는 자동 체크 |

---

## 알려진 주의사항

- Next.js 16 변경 사항 확인 필요: `finolaw/node_modules/next/dist/docs/`
- Basic Auth는 HTTPS 없이 외부 노출하면 안전하지 않다. 외부 노출 시 reverse proxy + TLS 필요.
- Codex CLI는 sandbox/approval bypass로 실행된다. 운영 전 `docs/security-model.md` 확인 필요.
- `api/`는 레거시 FastAPI 코드로 남아 있으나 현재 활성 UI 경로는 `finolaw/`다.
