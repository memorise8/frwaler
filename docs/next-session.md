# 다음 세션 핸드오프

> 이 파일을 먼저 읽으세요. 5분 안에 현재 작업 상태와 다음 액션을 파악하는 용도입니다.

---

## 빠른 시작

```bash
cd <repo-root>

# 1. 미커밋 변경 확인
git status --short

# 2. finolaw dev 서버 시작
cd finolaw
npm run dev
# → http://localhost:3001
```

DB가 있는지 확인:

```bash
cd <repo-root>

if [ -f data/papers.db ]; then
  .venv/bin/python - <<'PY'
import sqlite3
c = sqlite3.connect('data/papers.db')
print('papers:', c.execute('SELECT COUNT(*) FROM papers').fetchone()[0])
for row in c.execute('SELECT site_id, COUNT(*) FROM papers GROUP BY site_id ORDER BY 2 DESC LIMIT 5'):
    print(row)
PY
else
  echo 'data/papers.db missing: dashboard/search will be empty until a crawl creates data.'
fi
```

실행 중인 크롤러 확인:

```bash
ps aux | grep crawler | grep -v grep || true
```

---

## 현재 위치

- 활성 FE는 `finolaw/` 하나다. 레거시 `web/` 프론트엔드는 제거되었다.
- `finolaw`는 Next.js 16 앱이며 포트는 3001이다.
- `finolaw`는 상위 디렉터리의 `crawler/`, `.venv/`, `data/`, `.cache/`에 의존한다.
- 현재 체크아웃 기준 `data/papers.db`가 없을 수 있다. 이 경우 앱은 뜨지만 대시보드/검색 데이터는 비어 보인다.
- Auto-Add 메인 경로는 Codex CLI 기반 Tier 2다.

---

## 바로 이어 할 수 있는 작업

### 1. 문서/상태 검증 마무리

```bash
cd <repo-root>
rg -n "localhost:3000|port 3000|세션 기반|codex_runner.py 직접|web/src" docs README-install.md finolaw/README.md
```

아카이브 문서 외에 현재 사용 문서에서 오래된 표현이 나오면 수정한다.

### 2. Docker 빌드 검증

```bash
cd <repo-root>
docker compose build
docker compose up -d
docker compose logs -f --tail=200
```

빌드가 성공하면 `http://localhost:3001` 접속과 Basic Auth 동작을 확인한다.

### 3. DB 초기화/샘플 수집 경로 확인

DB가 없는 환경에서는 아래 중 하나로 초기 데이터를 만든다.

```bash
cd <repo-root>

# 기존 크롤러 소량 실행 예시
.venv/bin/python -m crawler.main crawl ntrs --limit 3

# 또는 Auto-Add Codex 경로
.venv/bin/python -m crawler.main auto-add-codex \
  "https://example.go.kr/list" \
  --site-id example-site \
  --timeout-seconds 1200
```

### 4. Preflight 체크 구현

`OPENAI_API_KEY`, `data/papers.db`, `.venv/bin/python`, `codex` 유무를 확인하는 `/api/health`와 UI 배너를 추가한다.

### 5. Codex 비용 계측

`crawler/codex_runner.py` 또는 실행 로그에서 크롤러 생성 1회당 토큰/비용 추정치를 남기고 UI에 표시한다.

---

## 주요 파일 빠른 참조

| 파일 | 역할 |
|------|------|
| `finolaw/README.md` | 활성 FE 개발 가이드 |
| `finolaw/src/app/` | Next.js App Router 페이지/API |
| `finolaw/src/lib/db.ts` | `data/papers.db` readonly 조회 |
| `finolaw/src/lib/auto-add-codex.ts` | `python -m crawler.main auto-add-codex` 실행 |
| `finolaw/src/proxy.ts` | HTTP Basic Auth |
| `crawler/main.py` | Python CLI 진입점 |
| `crawler/codex_runner.py` | Codex CLI 래퍼 |
| `crawler/sites/custom/` | Codex 생성 Python 크롤러 |
| `crawler/sites/configs/` | AutoAddAgent JSON 설정 |
| `data/papers.db` | SQLite DB |
| `.cache/` | UI/Codex/크롤러 로그 |
| `Dockerfile`, `docker-compose.yml` | 단일 컨테이너 배포 |

---

## 주의사항

- Next.js 16은 기존 Next.js 지식과 다를 수 있다. 프레임워크 API/파일 convention 수정 전 `finolaw/node_modules/next/dist/docs/`를 확인한다.
- Admin 인증은 세션 기반이 아니라 HTTP Basic Auth다. `ADMIN_USER`와 `ADMIN_PASSWORD`가 모두 있어야 켜진다.
- Codex CLI는 내부적으로 sandbox/approval bypass 플래그를 사용한다. 운영 전 `docs/security-model.md`를 확인한다.
- 세션 시작 시 `git status --short`로 사용자 미커밋 변경을 먼저 확인한다.

---

## 재개 체크리스트

- [ ] `git status --short` 확인
- [ ] `finolaw` dev server가 port 3001에서 응답하는지 확인
- [ ] `data/papers.db` 존재 여부 확인
- [ ] `crawler/.env`의 `OPENAI_API_KEY` 확인
- [ ] `docs/README.md`의 읽기 순서에 따라 필요한 문서 확인

최종 갱신: 2026-04-24
