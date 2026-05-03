# API 명세서 — livertree

## 1. CLI API (`python -m crawler.main`)

모든 서브커맨드는 컨테이너 안에서 `docker compose exec app …` 으로 실행한다.

### `crawl`
| 항목 | 값 |
|---|---|
| 시그니처 | `crawl <site_id> [--limit N] [--incremental]` |
| 동작 | 사이트의 게시글 목록 + 상세 메타를 documents 테이블에 INSERT/UPDATE |
| 출력 | "Saved N papers" |
| 종료코드 | 0=성공, 1=설정 누락 |

### `download`
| 항목 | 값 |
|---|---|
| 시그니처 | `download [site_id] [--limit N] [--retry]` |
| 동작 | `pdf_url` 가 비지 않은 row 를 .tmp 로 받고 magic-byte 로 확장자 결정 후 정식 경로 (`data/AAAA/BBBB/N.{ext}`) 로 rename, `download_status='downloaded'` 기록 |
| 출력 | 진행 카운트 + 파일 크기 |

### `convert`
| 항목 | 값 |
|---|---|
| 시그니처 | `convert [site_id] [--limit N]` |
| 동작 | `download_status='downloaded'` AND `txt_path` NULL row 를 `pdf_path` 의 ext 로 추출기 분기 → `.txt` 출력, `txt_path` 채움 |
| 출력 | "Done. Converted: X, Skipped: Y, Failed: Z" |

### `summarize`
| 항목 | 값 |
|---|---|
| 시그니처 | `summarize [site_id] [--limit N] [--provider gpt|gemini] [--doc-id N]` |
| 동작 | `summary` IS NULL row 를 `summarize_text(text, title, provider)` 으로 한국어 3-5문장 요약, `summary` 컬럼에 UPDATE |
| 입력 | abstract → txt_path → title 우선순위 |
| 환경 | `OPENAI_API_KEY` / `GEMINI_API_KEY`, `LLM_PROVIDER` |

### `stats`
사이트별 문서 카운트 + 다운로드 진행률 (NULL/pending/downloaded/failed/no_file).

### 기타
| 명령 | 용도 |
|---|---|
| `list-sites` | 등록된 사이트 + 카운트 |
| `scan-index <site>` | doc_index gap 분석 |
| `test-config <site>` | generic JSON config 검증 |
| `smart-find <url>` | NDJSON 스트림으로 URL 분석 |
| `auto-add <url>` | GPT 에이전트로 사이트 config 생성 |
| `auto-add-codex <url>` | Codex CLI 기반 사이트 config 생성 |

## 2. HTTP API (Next.js Route Handlers)

기본 호스트: `http://<deploy-host>:3001`. 모든 응답은 `Content-Type: application/json` (별도 명시 시 제외).

### GET `/api/health`
운영 prefilght 체크 결과 JSON.

응답 예:
```json
{
  "status": "ok|warn|error",
  "checks": [{"id": "db", "level": "ok", "label": "DB", "message": "ready", "detail": null}, …]
}
```

### GET `/api/export/markdown`
| 파라미터 | 타입 | 기본 | 설명 |
|---|---|---|---|
| `site` | string? | (전체) | site_id 필터 |
| `limit` | int? | 200 | 최대 row 수 |

응답: `text/markdown; charset=utf-8` 첨부 다운로드.

### POST `/api/admin/summarize`
입력:
```json
{ "docId": 12345, "provider": "gpt" }
```
출력:
```json
{ "ok": true, "docId": 12345, "summary": "…", "changed": true, "log": "…" }
```
오류: `{ "ok": false, "error": "…" }` with HTTP 400/404/500. 90초 timeout.

### GET `/api/crawler`
백그라운드 크롤 작업 큐 + 상태.

### GET `/api/crawler/sites`
등록된 사이트 + 핸들러 메타.

### GET `/api/crawler/logs?jobId=<id>`
특정 작업의 NDJSON 로그 tail.

### POST `/api/auto-add`
GPT 에이전트로 신규 사이트 config 생성.
```json
{ "url": "https://...", "siteId": "optional", "browser": false, "dryRun": false }
```
출력: NDJSON 스트림 (`progress` / `result` / `error`).

### POST `/api/auto-add/codex`
Codex CLI 기반 사이트 config 생성. 입력은 동일 형태.

## 3. 인증

`finolaw/src/lib/preflight.ts` 의 admin auth 체크에 따라 환경변수
`ADMIN_BASIC_AUTH_USER` / `ADMIN_BASIC_AUTH_PASS` 가 설정되면 모든
`/admin/*` + `/api/admin/*` 경로에 Basic Auth 가 적용된다 (운영 시 필수).

## 4. 에러 응답 형식

| HTTP | 의미 | 본문 |
|---|---|---|
| 400 | 입력 검증 실패 | `{ "ok": false, "error": "…" }` |
| 401 | Basic Auth 실패 | (Next.js 표준) |
| 404 | 리소스 없음 | `{ "ok": false, "error": "no document with id=X" }` |
| 500 | 서버 오류 | `{ "ok": false, "error": "…", "stderr": "…" }` |

## 5. Rate / Timeout

- `/api/admin/summarize`: 90초 timeout (LLM 호출 포함)
- `/api/auto-add{/codex}`: 사이트별 다름, NDJSON 으로 진행 상황 송출
- 기타: Next.js 기본 30초 (조정 시 deploy 환경에서)
