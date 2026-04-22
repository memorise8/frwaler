# 다음 세션 핸드오프

> 이 파일을 먼저 읽으세요. 5분 안에 어디까지 왔는지 파악하고 작업을 이어받을 수 있습니다.

---

## 빠른 시작

```bash
cd /data_raid/ruci_workspace/crawler-poc

# 1. 미커밋 변경 확인
git status

# 2. finolaw dev 서버 시작 (이미 켜져 있으면 스킵)
cd finolaw && npm run dev &
# → http://localhost:3001

# 3. DB 상태 확인
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('data/papers.db')
total = c.execute('SELECT COUNT(*) FROM papers').fetchone()[0]
print(f'총 {total:,}건')
for row in c.execute('SELECT site_id, COUNT(*) FROM papers GROUP BY site_id ORDER BY 2 DESC LIMIT 5'):
    print(row)
"

# 4. 실행 중인 크롤러 확인
ps aux | grep crawler | grep -v grep
```

---

## 현재 위치

Month 1 Foundation이 완료된 상태. Codex CLI 기반 Auto-Add 흐름(URL → 자율 크롤러 생성)이 작동 중이며, finolaw Next.js 앱(port 3001)이 메인 UI다. DB에는 308,623건이 수집되어 있고, FTS5 전문검색이 동작한다. better.fsc.go.kr 5,394건 자동 수집으로 Tier 2(Codex) 경로가 검증되었다. `api/`, `pro_server/`, `web/` 레거시 디렉토리는 정리 대기 중.

---

## 바로 이어 할 수 있는 작업 (우선순위 순)

### 1. Docker 빌드 검증 (Month 1 마무리) — 약 20분

Month 1 Dockerfile은 작성되었지만 실제 빌드 테스트가 남아있다.

```bash
cd /data_raid/ruci_workspace/crawler-poc
docker build -t crawler-poc:test .
# 성공하면 docker-compose up -d 로 전체 스택 확인
```

네트워크 환경에 따라 20분 소요. 빌드 오류가 나면 `Dockerfile` 수정 후 재시도.

### 2. 레거시 디렉토리 정리

`api/`, `pro_server/`, `web/` 디렉토리는 현재 사용하지 않음.

```bash
# 내용 확인 후 archive 또는 삭제 결정
ls api/ pro_server/ web/
# git rm -r 또는 mv 처리
```

### 3. Preflight 체크 구현 (Month 2 첫 아이템)

`OPENAI_API_KEY` 등 필수 환경변수 누락 시 finolaw UI에 빨간 배너 표시.

- 백엔드: `GET /api/health` 엔드포인트 → 환경변수 체크 결과 반환
- 프론트: 대시보드 상단에 경고 배너 컴포넌트 추가

### 4. Codex 비용 계측

크롤러 자동 생성 1회당 토큰 수와 추정 비용을 DB 또는 로그에 기록.

- `crawler/codex_runner.py`에 비용 로깅 추가
- finolaw 크롤러 관리 페이지에 비용 컬럼 표시

---

## 주요 파일 빠른 참조

| 파일 | 역할 |
|------|------|
| `crawler/codex_runner.py` | Codex CLI 서브프로세스 실행 + 크롤러 저장 (Tier 2 메인 경로) |
| `crawler/agent.py` | AutoAddAgent GPT API 구현 (Tier 1 내부 경로) |
| `crawler/agent_tools.py` | AutoAddAgent 도구 11개 구현 |
| `crawler/main.py` | CLI 진입점 (`crawl`, `auto-add`, `test-config`) |
| `crawler/base_crawler.py` | 공통 베이스 (upsert, retry) |
| `crawler/sites/nts_taxlaw.py` | NTS 국세법령 크롤러 |
| `crawler/sites/custom/` | Codex가 자동 생성한 Python 크롤러 저장 위치 |
| `finolaw/src/app/` | Next.js App Router 페이지 |
| `finolaw/src/lib/` | DB 접근, 유틸리티 |
| `data/papers.db` | SQLite DB (308,623건) |

---

## 계획 파일 경로

```
/home/ruci/.claude/plans/elegant-herding-goblet.md
```

3개월 MVP 로드맵. Month 1 완료, Month 2 진행 중.

---

## 알려진 블로커 / 주의사항

**Codex bypass flag 이슈**

Codex CLI는 `--dangerously-bypass-approvals-and-sandbox` 플래그로 실행해야 자율 작업이 가능하다. 이 플래그는 `crawler/codex_runner.py`에서 서브프로세스 호출 시 전달된다.

Claude Code 세션 내에서 Codex를 직접 이 플래그로 호출하려 하면 Claude Code 자체적으로 차단된다. 이는 의도된 보안 동작이므로, Codex 실행이 필요할 때는 **사용자가 터미널에서 직접 실행**해야 한다.

**git 미커밋 변경**

세션 시작 시 `git status` 확인 필수. `web/package.json`, `web/src/`, `web/package-lock.json` 등 변경 사항이 스테이지 밖에 있을 수 있다.

---

## 재개 체크리스트

- [ ] `finolaw` dev server가 port 3001에서 응답하는지 확인 (`curl -s http://localhost:3001 | head -3`)
- [ ] `git status` 확인 — 커밋 안 된 변경 있으면 처리 또는 기록
- [ ] Month 1 Foundation 파일 자리에 있는지 확인 (`ls Dockerfile docker-compose.yml README-install.md`)
- [ ] 계획 파일 열기 (`cat /home/ruci/.claude/plans/elegant-herding-goblet.md`)
- [ ] 위 "바로 이어 할 수 있는 작업" 목록에서 첫 아이템 집어들기
