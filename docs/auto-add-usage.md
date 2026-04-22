# Auto-Add 사용 가이드

> URL을 입력하면 자동으로 Python 크롤러가 생성되고, 즉시 수집이 시작됩니다.

---

## 경로 구분

현재 두 가지 경로가 존재합니다.

| 구분 | 이름 | 상태 | 진입점 |
|------|------|------|--------|
| **Tier 2** | Codex CLI | **메인 경로** (현재 기본) | finolaw `/auto-add` 페이지 또는 `codex_runner.py` 직접 호출 |
| Tier 1 | AutoAddAgent (GPT API) | 내부 경로 (UI 비노출) | CLI `python -m crawler.main auto-add <url>` |

Tier 2(Codex)가 검증된 메인 경로입니다. better.fsc.go.kr에서 5,394건 자동 수집 성공 사례가 있습니다.

---

## 1. Tier 2 — Codex CLI (메인 경로)

### 동작 방식

```
사용자 URL 입력
    → finolaw /auto-add 페이지
        → crawler/codex_runner.py
            → codex exec --dangerously-bypass-approvals-and-sandbox
                → Python 크롤러 자율 작성
                    → crawler/sites/custom/<site_id>.py 저장
                        → 즉시 크롤링 시작
```

### 전제 조건

```bash
# Codex CLI 설치 확인
codex --version

# OpenAI API 키 설정 (Codex CLI + AutoAddAgent 양쪽에서 사용)
export OPENAI_API_KEY="sk-..."
# 또는 crawler/.env 파일에 저장
echo "OPENAI_API_KEY=sk-..." > crawler/.env
```

### finolaw UI에서 사용 (권장)

1. 브라우저에서 `http://localhost:3001/auto-add` 접속
2. URL 입력란에 수집할 페이지 URL 입력
3. "크롤러 생성" 버튼 클릭
4. 실시간 진행 로그 확인
5. 완료 후 수집 결과 확인

### codex_runner.py 직접 호출

```bash
cd /data_raid/ruci_workspace/crawler-poc

# 기본 실행
.venv/bin/python crawler/codex_runner.py \
  --url "https://example.go.kr/list" \
  --site-id my-site

# 결과: crawler/sites/custom/my-site.py 생성
```

### 생성된 크롤러 확인 및 실행

```bash
# 생성된 크롤러 확인
cat crawler/sites/custom/my-site.py

# 테스트 실행 (3건)
.venv/bin/python -m crawler.main crawl my-site --limit 3

# 전체 수집
.venv/bin/python -m crawler.main crawl my-site
```

### 실제 검증 사례: FSC 사이트

```bash
# better.fsc.go.kr → 5,394건 자동 수집 성공
.venv/bin/python crawler/codex_runner.py \
  --url "https://better.fsc.go.kr/fsc_new/replyCase/PastReplyList.do?stNo=11" \
  --site-id better-fsc-go-kr-fsc_new

# DB 확인
.venv/bin/python -c "
import sqlite3
c = sqlite3.connect('data/papers.db')
count = c.execute(\"SELECT COUNT(*) FROM papers WHERE site_id='better-fsc-go-kr-fsc_new'\").fetchone()[0]
print(f'FSC: {count:,}건')
"
# 출력: FSC: 5,394건
```

### 주의사항: bypass flag

Codex CLI는 `--dangerously-bypass-approvals-and-sandbox` 플래그로 실행됩니다. 이 플래그는 `codex_runner.py` 내부에서 서브프로세스 호출 시 자동으로 전달됩니다.

Claude Code 세션 내에서 이 플래그를 직접 사용하려 하면 차단됩니다. **Codex 실행이 필요할 때는 사용자가 터미널에서 직접 실행해야 합니다.**

---

## 2. Tier 1 — AutoAddAgent (내부 경로)

AutoAddAgent는 GPT API를 직접 호출하는 방식으로, 현재 UI에 노출되지 않습니다. 코드는 `crawler/agent.py`에 유지되어 있으며 CLI로만 호출 가능합니다.

### CLI 사용법

```bash
python -m crawler.main auto-add <URL>
```

**예시:**
```bash
python -m crawler.main auto-add "https://better.fsc.go.kr/fsc_new/replyCase/PastReplyList.do?stNo=11"
```

**옵션:**

| 옵션 | 설명 | 예시 |
|------|------|------|
| `--site-id ID` | site_id 수동 지정 (생략 시 자동 생성) | `--site-id my-site` |
| `--browser` | 헤드리스 브라우저 강제 사용 (SPA 사이트용) | `--browser` |
| `--dry-run` | 실제 API 호출 없이 테스트 | `--dry-run` |

**SPA 사이트 예시:**
```bash
python -m crawler.main auto-add "https://example.com/spa-page" --browser
```

### 출력 형식 (NDJSON 이벤트 스트림)

```json
{"type":"progress","message":"[fetch_page] https://... -> OK (45000 chars)"}
{"type":"progress","message":"[clean_html] 12000 chars"}
{"type":"progress","message":"[test_selector] 'tbody tr' -> 15 matches [OK]"}
{"type":"progress","message":"[save_config] site_id=my-site"}
{"type":"progress","message":"[test_crawl] 3 items crawled"}
{"type":"result","success":true,"site_id":"my-site","items_found":3,"config_path":"crawler/sites/configs/my-site.json","reason":"..."}
```

---

## 3. 동작 원리 (AutoAddAgent 내부)

### Phase 1: 페칭

1. **fetch_page(url)** — 표준 HTTP 요청으로 HTML 가져오기
2. 실패 시 → **curl_fetch()** 재시도 (TLS 문제 우회)
3. 403 Forbidden → **cloudscraper_fetch()** (Cloudflare 우회)
4. 매우 작은 HTML 또는 0개 링크 → **browser_fetch()** (JavaScript 렌더링)

### Phase 2: 분석

5. **clean_html(html)** — 불필요한 `<script>`, `<style>`, `<nav>` 등 제거 및 선택자 힌트 추출
6. 자동 발견된 선택자 힌트 확인 (클릭 횟수, 샘플 텍스트 포함)

### Phase 3: 선택자 테스트

7. **test_selector(html, css_selector)** — 각 선택자를 HTML에 대해 테스트
8. 매칭 수 및 샘플 텍스트 확인
9. 필요시 여러 선택자 병렬 테스트

### Phase 4: 설정 생성 및 테스트

10. **save_config(config)** — JSON 설정 파일 저장
11. **test_crawl(site_id, limit=3)** — 실제 크롤러가 잘 작동하는지 검증

### 도구 목록 (agent_tools.py)

| 도구 이름 | 기능 |
|----------|------|
| `fetch_page` | HTTP 요청으로 페이지 가져오기 |
| `curl_fetch` | curl 서브프로세스로 페칭 (SSL 우회) |
| `cloudscraper_fetch` | cloudscraper로 Cloudflare 우회 |
| `browser_fetch` | Playwright 헤드리스 브라우저 (JS 렌더링) |
| `clean_html` | 불필요한 태그 제거, 선택자 힌트 생성 |
| `test_selector` | CSS 선택자 테스트 |
| `extract_text` | 선택자와 매칭되는 텍스트 추출 |
| `save_config` | JSON 설정 저장 |
| `test_crawl` | 생성된 설정으로 테스트 크롤 |
| `write_crawler_file` | 커스텀 Python 크롤러 생성 (API 기반 사이트용) |

---

## 4. 생성된 설정 후 사용 흐름 (GenericCrawler 방식)

AutoAddAgent(Tier 1)는 JSON 설정 파일을 생성합니다. Codex(Tier 2)는 Python 파일을 생성합니다.

### JSON 설정 확인 (Tier 1 결과물)

```bash
cat crawler/sites/configs/my-site.json
```

출력 예시:
```json
{
  "site_id": "my-site",
  "site_name": "My Website",
  "base_url": "https://example.com",
  "crawl_type": "html",
  "list_page": {
    "url": "https://example.com/list",
    "pagination": {"type": "query_param", "param": "page", "start": 1},
    "selectors": {
      "item_container": "tbody tr",
      "item_link": "td a",
      "title": "td.name",
      "date": "td.date"
    }
  },
  "detail_page": {
    "selectors": {
      "title": "h1",
      "abstract": "div.content",
      "pdf_link": "a[href$='.pdf']"
    }
  },
  "options": {
    "delay": 1.5,
    "encoding": null,
    "fetch_method": null
  }
}
```

### 설정 검증

```bash
python -m crawler.main test-config my-site --limit 3
```

**기대 출력:**
```
Testing config 'my-site' with 3 items...
Test passed. 3 items crawled successfully.
```

만약 `0 items` 수집되면 → 설정의 선택자 수동 수정 필요.

### 실제 크롤링

```bash
python -m crawler.main crawl my-site
```

### 증분 크롤링

이미 수집된 항목은 스킵:

```bash
python -m crawler.main crawl my-site --incremental
```

---

## 5. 환경변수

| 변수 | 필수 | 설명 |
|------|------|------|
| `OPENAI_API_KEY` | 필수 | Codex CLI 및 AutoAddAgent 양쪽에서 사용 |

설정 방법:

```bash
# A. 환경변수로 설정 (권장)
export OPENAI_API_KEY="sk-..."

# B. .env 파일로 설정
echo "OPENAI_API_KEY=sk-..." > crawler/.env
```

API 키 취득: https://platform.openai.com → 계정 → API keys → "Create new secret key"

---

## 6. 설치 필요 패키지

기본:
```bash
pip install openai requests beautifulsoup4 python-dotenv
```

선택사항:
```bash
pip install cloudscraper          # Cloudflare 우회
pip install playwright && playwright install chromium  # SPA 사이트용
```

---

## 7. 한계 및 주의사항

### API 토큰 비용

- Codex CLI 또는 GPT API 호출마다 토큰 소비
- 사이트 복잡도에 따라 비용 차이 발생
  - 단순 테이블 사이트: 낮음 (~$0.01–0.05)
  - 복잡한 다단계 구조: 높음 (~$0.20–0.50)

실행 전 OpenAI 크레딧 잔량 확인 권장.

### SPA(Single Page Application) 사이트 필수 사항

Tier 1(AutoAddAgent) 사용 시 `--browser` 플래그 없으면 빈 HTML 수집:

```bash
# 틀림
python -m crawler.main auto-add "https://spa-site.com/list"

# 맞음
python -m crawler.main auto-add "https://spa-site.com/list" --browser
```

**SPA 판별 기준:**
- HTML에 `<div id="root">`, `<div id="app">`, `<app-root>` 등 루트 엘리먼트만 있음
- 실제 콘텐츠는 JavaScript 실행 후 생성
- clean_html 출력에서 선택자 힌트 0개

### 인증 필요 사이트 불가

로그인/세션이 필요한 사이트는 자동 생성 불가:

```bash
# 불가능 (로그인 필요)
python -m crawler.main auto-add "https://members-only.com/list"
```

해결: 수동 커스텀 크롤러 작성 (`write_crawler_file` 도구 또는 직접 코딩)

### 선택자 실패 (Tier 1)

LLM이 잘못된 선택자를 선택하면 `test_crawl`에서 0건 수집:

```
Test failed. No items could be crawled. Check the config.
```

해결: 설정 파일 수동 수정

```bash
nano crawler/sites/configs/my-site.json
python -m crawler.main test-config my-site --limit 3
```

### 인코딩 문제

텍스트가 깨져 보이면 (예: EUC-KR 한국어):

```json
{
  "options": {
    "encoding": "euc-kr"
  }
}
```

---

## 8. 트러블슈팅

### Q. "OPENAI_API_KEY not set"

```bash
echo $OPENAI_API_KEY
cat crawler/.env | grep OPENAI_API_KEY
```

환경변수 또는 `crawler/.env` 파일 확인.

### Q. "Max iterations (15) reached" (AutoAddAgent)

사이트가 너무 복잡하거나 구조가 동적임. Codex CLI(Tier 2) 방식으로 전환하거나 수동 수정 필요.

### Q. "Playwright not installed"

```bash
pip install playwright
playwright install chromium
```

### Q. "cloudscraper_fetch 403 여전히 발생"

```bash
pip install --upgrade cloudscraper
```

### Q. 생성된 config가 0건 수집

```bash
# 1건으로 테스트
python -m crawler.main test-config my-site --limit 1

# HTML 직접 확인
curl "https://example.com/list" | grep -o "선택자" | head -5
```

### Q. Codex 실행이 Claude Code 내에서 차단됨

의도된 동작입니다. `--dangerously-bypass-approvals-and-sandbox` 플래그는 Claude Code 세션 내에서 직접 사용할 수 없습니다. 터미널에서 직접 실행하거나 finolaw UI(/auto-add)를 통해 호출하세요.

---

## 9. 관련 파일

| 파일 | 역할 |
|------|------|
| `crawler/codex_runner.py` | Codex CLI 서브프로세스 실행 (Tier 2 메인 경로) |
| `crawler/agent.py` | AutoAddAgent GPT API 구현 (Tier 1) |
| `crawler/agent_tools.py` | AutoAddAgent 도구 11개 구현 |
| `crawler/main.py` | CLI 진입점 |
| `crawler/sites/custom/` | Codex 생성 Python 크롤러 저장 위치 |
| `crawler/sites/configs/` | AutoAddAgent 생성 JSON 설정 저장 위치 |
| `crawler/sites/generic_crawler.py` | GenericCrawler (JSON 설정 기반) |
| `docs/security-model.md` | Codex 샌드박스 우회 설계 및 완화책 |
| `docs/rag-architecture.md` | Phase 1~7 RAG 아키텍처 (미래 계획) |

---

최종 갱신: 2026-04-22
