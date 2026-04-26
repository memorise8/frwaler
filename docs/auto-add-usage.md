# Auto-Add 사용 가이드

> URL을 입력하면 Codex CLI 또는 내부 AutoAddAgent가 크롤러를 생성하고 수집 흐름으로 연결합니다.

---

## 경로 구분

| 구분 | 이름 | 상태 | 진입점 | 산출물 |
|------|------|------|--------|--------|
| **Tier 2** | Codex CLI | **메인 경로** | finolaw `/auto-add` 또는 `python -m crawler.main auto-add-codex` | `crawler/sites/custom/<site_id>.py` |
| Tier 1 | AutoAddAgent | 내부/보조 경로 | `python -m crawler.main auto-add` | `crawler/sites/configs/<site_id>.json` |

Tier 2(Codex CLI)가 현재 기본 경로입니다. `crawler/codex_runner.py`는 라이브러리 모듈이며 직접 실행용 CLI가 아닙니다. 직접 실행할 때는 반드시 `python -m crawler.main auto-add-codex ...`를 사용하세요.

---

## 1. Tier 2 — Codex CLI 메인 경로

### 동작 방식

```
사용자 URL 입력
    → finolaw /auto-add 페이지
        → /api/auto-add/codex
            → .venv/bin/python -m crawler.main auto-add-codex <url>
                → crawler/codex_runner.py
                    → codex exec --dangerously-bypass-approvals-and-sandbox
                        → crawler/sites/custom/<site_id>.py 저장
                        → test_crawl 검증
```

### 전제 조건

```bash
# 저장소 루트에서 실행한다고 가정
cd <repo-root>

# Python 가상환경과 Codex CLI 확인
.venv/bin/python --version
codex --version

# OpenAI API 키 설정: 환경변수 또는 crawler/.env
printf 'OPENAI_API_KEY=sk-...\nLLM_PROVIDER=gpt\n' > crawler/.env
chmod 600 crawler/.env
```

### finolaw UI에서 사용 권장

```bash
cd <repo-root>/finolaw
npm run dev
# http://localhost:3001/auto-add 접속
```

1. `/auto-add` 페이지로 이동
2. 수집할 URL 입력
3. 필요하면 `site_id` 지정
4. 크롤러 생성 시작
5. 실시간 로그 확인
6. 완료 후 `/crawler?site=<site_id>`에서 실행/상태 확인

### CLI에서 직접 실행

```bash
cd <repo-root>

.venv/bin/python -m crawler.main auto-add-codex \
  "https://example.go.kr/list" \
  --site-id my-site \
  --timeout-seconds 1200
```

결과:

- 생성 파일: `crawler/sites/custom/my-site.py`
- UI 로그: `.cache/ui_codex-*.log`
- Codex 내부 로그: `.cache/codex_<site_id>_*.log`
- DB 저장 위치: `data/papers.db`

### 생성된 크롤러 확인 및 실행

```bash
cd <repo-root>

cat crawler/sites/custom/my-site.py

# 소량 테스트
.venv/bin/python -m crawler.main crawl my-site --limit 3

# 전체 또는 증분 수집
.venv/bin/python -m crawler.main crawl my-site
.venv/bin/python -m crawler.main crawl my-site --incremental
```

---

## 2. Tier 1 — AutoAddAgent 내부 경로

AutoAddAgent는 GPT API를 직접 호출해 JSON 설정을 만드는 보조 경로입니다. 현재 UI 기본 경로는 아닙니다.

```bash
cd <repo-root>

.venv/bin/python -m crawler.main auto-add \
  "https://example.go.kr/list" \
  --site-id my-site
```

옵션:

| 옵션 | 설명 |
|------|------|
| `--site-id ID` | site_id 수동 지정 |
| `--browser` | SPA/JS 렌더링 사이트용 브라우저 페치 강제 |
| `--dry-run` | 실제 API 호출 없이 테스트 |
| `--max-iterations N` | 복잡한 사이트 분석 반복 횟수 증가 |

결과는 `crawler/sites/configs/<site_id>.json`에 저장됩니다.

---

## 3. DB와 검색 반영

크롤러가 데이터를 저장하면 `data/papers.db`에 `papers`/`sites` 테이블이 생성 또는 갱신됩니다.

```bash
cd <repo-root>

.venv/bin/python -m crawler.main stats
.venv/bin/python -m crawler.main list-sites
```

`data/papers.db`가 없으면 finolaw UI는 실행되지만 대시보드/검색 결과는 비어 보입니다. 먼저 위 Auto-Add 또는 기존 크롤러로 데이터를 수집하세요.

---

## 4. 환경변수

| 변수 | 필수 | 설명 |
|------|------|------|
| `OPENAI_API_KEY` | Auto-Add/Codex 사용 시 필수 | Codex CLI 및 AutoAddAgent에서 사용 |
| `LLM_PROVIDER` | 선택 | Smart Finder AI fallback provider. 기본값은 `gpt` |
| `GEMINI_API_KEY` | 선택 | `LLM_PROVIDER=gemini`일 때 사용 |

`crawler/.env` 예시:

```env
OPENAI_API_KEY=sk-...
LLM_PROVIDER=gpt
# GEMINI_API_KEY=...
```

---

## 5. 한계 및 주의사항

- Codex CLI는 OpenAI API 비용을 사용합니다. 계정의 월별 사용량 상한을 설정하세요.
- 로그인/세션이 필요한 사이트는 자동 생성이 실패할 수 있습니다.
- SPA 사이트는 Tier 1에서 `--browser`가 필요할 수 있습니다. Tier 2는 생성된 코드에 따라 별도 브라우저/네트워크 처리가 필요할 수 있습니다.
- 생성된 Python 크롤러는 실행 전 `crawler/sites/custom/*.py`를 검토하는 것을 권장합니다.
- 대상 사이트의 robots.txt, 이용약관, 저작권/개인정보 규정을 운영자가 확인해야 합니다.

---

## 6. 트러블슈팅

### `OPENAI_API_KEY not set`

```bash
grep OPENAI_API_KEY crawler/.env
env | grep OPENAI_API_KEY
```

컨테이너 환경이면 `.env` 수정 후 재시작하세요.

```bash
docker compose restart
```

### `codex CLI not found`

로컬 개발 환경에서는 Codex CLI가 PATH에 있어야 합니다.

```bash
codex --version
```

Docker 배포 환경은 Dockerfile에서 `@openai/codex`를 설치합니다.

### 생성은 됐지만 0건 수집

```bash
.venv/bin/python -m crawler.main crawl <site_id> --limit 1
cat .cache/ui_*.log | tail -200
```

선택자/API 추론 실패, 로그인 필요, 차단, 인코딩 문제일 수 있습니다. 생성된 `crawler/sites/custom/<site_id>.py` 또는 `crawler/sites/configs/<site_id>.json`을 확인하세요.

### Codex bypass flag 관련

`crawler/codex_runner.py`는 내부적으로 `codex exec --dangerously-bypass-approvals-and-sandbox`를 사용합니다. 이 플래그는 보안상 위험하므로 자세한 위험과 완화책은 `docs/security-model.md`를 확인하세요.

---

## 7. 관련 파일

| 파일 | 역할 |
|------|------|
| `finolaw/src/app/auto-add/page.tsx` | Auto-Add UI |
| `finolaw/src/app/api/auto-add/codex/route.ts` | Tier 2 API route |
| `finolaw/src/lib/auto-add-codex.ts` | Python subprocess 실행 |
| `crawler/main.py` | CLI 진입점 |
| `crawler/codex_runner.py` | Codex CLI 서브프로세스 래퍼 |
| `crawler/agent.py` | Tier 1 AutoAddAgent |
| `crawler/agent_tools.py` | Tier 1 도구 |
| `crawler/sites/custom/` | Codex 생성 Python 크롤러 |
| `crawler/sites/configs/` | AutoAddAgent 생성 JSON 설정 |
| `data/papers.db` | SQLite DB |
| `.cache/` | UI/Codex/크롤러 로그 |

---

최종 갱신: 2026-04-24
