# 프로젝트 현재 상태

> 마지막 업데이트: 2026-04-22

---

## 한 줄 요약

URL을 입력하면 Codex CLI가 자율적으로 Python 크롤러를 작성하고, 수집된 문서는 SQLite DB + 전문검색(FTS5)으로 즉시 조회 가능한 시스템.

---

## 주요 성과

### finolaw 웹 앱 (Next.js, port 3001)

`finolaw/` 디렉토리. 주요 기능:

- **대시보드** — DB 통계, 최근 수집 문서 요약
- **전문 검색** — FTS5 기반, 308,623건 인덱스
- **URL 크롤링** (`/auto-add`) — URL 입력 → Codex CLI 자동 호출 → Python 크롤러 생성 → 즉시 수집
- **크롤러 관리** — 등록된 크롤러 목록, 수집 상태 확인

### AutoAddAgent (Tier 1 — 내부 경로)

- `crawler/agent.py` 구현, GPT 모델(11개 도구 사용) 기반
- 현재 **UI 비노출**: CLI `python -m crawler.main auto-add <url>`로만 호출
- Codex CLI 방식(Tier 2)이 메인 경로로 자리 잡음

### Codex CLI 통합 (Tier 2 — 메인 경로)

- `crawler/codex_runner.py` → `codex exec` 서브프로세스 → `crawler/sites/custom/<site_id>.py` 저장
- `/auto-add` UI 페이지에서 호출
- 검증 사례: **better.fsc.go.kr** 5,394건 자동 수집 성공

### Month 1 Foundation 완료

- `Dockerfile` + `docker-compose.yml` 정비
- Admin 인증 (세션 기반)
- 설치 문서 (`README-install.md`)

---

## DB 현황 (2026-04-22 기준)

```
site_id                       건수
-----------------------------  ------
nts-taxlaw-pd                 149,348
nts-taxlaw-qt                 138,537
ntrs                            8,153
better-fsc-go-kr-fsc_new        5,394
fsc                             2,510
statistischebibliothek          1,472
mohw                              495
forest-press                      280
daten-berlin                      247
nora-nerc                         237
(기타)                           2,950
-----------------------------  ------
합계                          308,623
```

DB 파일: `data/papers.db` (SQLite)

---

## 실행 중인 프로세스 (참고용 — 세션 재시작 시 직접 확인)

| 서비스 | 명령 | 포트 |
|--------|------|------|
| finolaw dev | `cd finolaw && npm run dev` | 3001 |
| 레거시 web/ | `cd web && npm run dev` | 3000 |
| 고립 FastAPI | `uvicorn api.main:app` | 8000 |

> finolaw(3001)만 활성 개발 대상. web/(3000)과 api/(8000)은 레거시.

---

## 미완 항목

### Month 2 (다음 단계)

| 항목 | 설명 |
|------|------|
| Preflight 체크 | `/api/health` 엔드포인트 + UI 빨간 배너 (환경변수 누락 시) |
| Codex 비용 계측 | 크롤러 생성 1회당 토큰/비용 로깅 |
| 로그 로테이션 | `.cache/*.log` 자동 정리 |
| crawler metadata | `site_id`, 생성일시, 수집 건수 DB 저장 |
| robots.txt 준수 | 크롤 전 robots.txt 체크 로직 |

### Month 3

- 파일럿 고객 온보딩
- 문서 완비
- v0.1.0 릴리스

---

## 알려진 이슈

| 이슈 | 내용 |
|------|------|
| 레거시 디렉토리 | `api/`, `pro_server/`, `web/` 정리 필요 |
| nts-taxlaw gap | ~19,392건 미수집 (92% 완료). upsert 안전하므로 재크롤 가능 |
| Codex bypass flag | 서브프로세스에서 `--dangerously-bypass-approvals-and-sandbox` 필요. Claude Code 내 직접 호출 시 차단됨 (의도된 동작) |
