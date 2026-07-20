# Libertree 프로젝트 종합 핸드오프 (2026-07-20)

> **새 세션/새 개발자용 단일 진입 문서.** 컨텍스트 0에서 이 문서만 읽으면 프로젝트 전체를
> 파악하고 이어서 개발할 수 있도록 self-contained로 작성. 수치는 빠르게 변하므로 항상 DB로 재확인.

---

## 1. 프로젝트 한 줄 요약

클라이언트가 준 `scroll_index.xlsx`의 **글로벌 정부·연구·학술 사이트 1,994 entries(고유 host 832~833)**에서
메타데이터 + PDF 원문 + 추출 텍스트 + (선택)한국어 요약을 **자동 수집·저장·조회**하는 시스템.

- 저장: `data/libertree.db`(SQLite, 5.2GB) + `libertree/` blob 트리(1.7TB)
- 조회: `finolaw/` Next.js 웹앱 (Docker, 포트 3001, Basic Auth)
- 수집: `crawler/` Python 인프라 + AI 생성 크롤러

> 2026-07-07 리네이밍: livertree → **libertree**. 구 경로 호환 심링크 유지(`data/livertree.db`, `./livertree`).
> git 브랜치: **libertree** (main 아님).

---

## 2. 현재 상태 (2026-07-20 실측)

| 지표 | 값 |
|---|---|
| 입력 entries | 1,994 (고유 host 832) |
| **수집 완료 hosts** | **694 / 832 (83.4%)** — 엄격 기준: 해당 host netloc에 문서 ≥ 1건 |
| documents | **481,669** |
| DB 내 unique site_id | 761 |
| PDF 다운로드 | **297,756** |
| 텍스트 추출 | **288,124** |
| 한국어 요약(Gemma) | 36,548 (트랙 일시중지, 마지막 5/26) |
| 미수집 hosts | 138 |

**상태 확인 명령 (세션 시작 시):**
```bash
cd /data_raid/ruci_workspace/frwaler_job
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/libertree.db'); \
print('docs', c.execute('SELECT COUNT(*) FROM documents').fetchone()[0], \
'pdf', c.execute('SELECT COUNT(*) FROM documents WHERE pdf_downloaded=1').fetchone()[0], \
'text', c.execute('SELECT COUNT(*) FROM documents WHERE text_extracted=1').fetchone()[0])"
ps -ef | grep -E "promote_all|recover_pdfs|backfill|summarize" | grep -v grep   # 실행 중 작업 확인
docker ps --filter name=crawler-poc                                             # 웹앱 상태
```

---

## 3. 디렉토리 구조

```
/data_raid/ruci_workspace/frwaler_job/
├── data/
│   ├── libertree.db                              # 메인 DB (5.2GB, SQLite WAL)
│   └── audit/                                     # 분류·로그·보고 CSV/XLSX/JSON (산출물 대부분 여기)
│       └── logs/                                  # 실행 로그
├── libertree/AAAA/BBBB/AAAABBBBNNNN.{pdf,txt}     # blob 트리 (1.7TB, 12자리 seq_id 4-4-4 분할)
├── crawler/                                       # 수집 인프라
│   ├── sites/configs/*.json                       # 437개 declarative 크롤러 (GenericCrawler가 실행)
│   ├── sites/custom/*.py                           # 782개 AI 생성 커스텀 크롤러 (BaseCrawler 상속)
│   ├── base_crawler.py                             # 커스텀 크롤러 베이스 클래스
│   ├── db_libertree.py                             # 스키마 정의 (ground truth)
│   ├── blob_storage.py                             # seq_id → blob 경로
│   ├── converter.py                                # PDF/HWP/HWPX → 텍스트 (extract_text_for)
│   ├── playwright_fetcher.py                        # JS/Cloudflare 사이트용 (fetch_html)
│   ├── claude_runner.py / codex_runner.py           # 크롤러 자동 생성 엔진
│   └── summarizer.py                                # 요약 (gpt-5.4-mini/gemini/gemma)
├── finolaw/                                        # Next.js 16 조회 웹앱 (포트 3001)
│   └── src/lib/db.ts                               # DB readonly 접근 (1700+줄)
├── scripts/                                        # 45개 운영 스크립트 (아래 §7)
├── docs/                                           # 문서 (본 디렉토리)
├── Dockerfile / docker-compose.yml                 # 배포 스택
└── .env                                            # ADMIN_USER/ADMIN_PASSWORD (Basic Auth)
```

---

## 4. 데이터 모델

**documents 테이블 주요 컬럼:**
`seq_id`(PK), `collected_at`, `site_id`, `post_number`(증분수집 기준), `meta_url`, `title`,
`published_date`, `listed_date`, `authors`, `publisher`, `journal`, `pdf_url`, `keywords`,
`abstract`, `original_filename`, `pdf_downloaded`(0/1), `text_extracted`(0/1), `pdf_size_bytes`,
`pdf_sha256`, `summary`, `summary_model`, `summary_at`

- **dedup 키**: `(site_id, post_number, meta_url)` — insert 시 중복이면 UPDATE 안 함
- **FTS5**: `documents_fts` (trigram) — title·abstract·summary·keywords 인덱싱. 본문 전문은 미인덱싱(.txt 파일에만 존재)
- **sites 테이블**: `site_id`(PK), `site_name`, `site_url`, `sheet`, `created_at`
- **Blob 경로**: `libertree/{seq[:4]}/{seq[4:8]}/{seq:012d}.{pdf|txt}` — `crawler/blob_storage.py`, TS는 `finolaw/src/lib/db.ts blobPath()`

---

## 5. 완료된 작업 (What's Done)

### 5.1 이번 세션 (2026-07-18~20) 대규모 수집
- **증분 재크롤 700사이트**: 기존 크롤러로 2달치 신규 게시물 재수집
- **텍스트 백필 6,740건 전수**: 과거 추출 실패분 재시도 (결과는 §6.3 참조)
- **미수집 152곳 3단계 재실사**: requests 프로브 → 목록·본문·PDF 심층 → playwright 렌더 → 수집가능 51곳 발굴
- **재개방 46곳 크롤러 생성·수집**: 재실사로 살아난 사이트
- **robots 금지 38곳 전원 기관 허가 획득(7/19)** → **전용 크롤러 37곳 생성·수집** (nioz worldcat 1곳만 실패)
- **신규 83곳 promote: 82곳 성공** (신규 25,474건 / PDF 12,982 / 텍스트 10,933)
- **finolaw 웹앱 Docker 배포** (포트 3001, Basic Auth)
- 순증: 문서 +46,260 / 적재 hosts 실질 대폭 증가

### 5.2 이전 작업 (2026-06~07)
- **PDF 회복 프로젝트**: recover_pdfs.py로 UA/Referer/verify=False/GET 결손 보완 → +21K PDF, +29K 텍스트
- **fake-HTML 블록 정리** + converter에 HWP/HWPX 매직바이트 감지 추가(한국 문서 추출 가능해짐)
- **메타데이터 정제**(11-task SDD): 정제 규칙 R1~R6, 가짜 PDF 3,704건 리셋
- **라벨오류 18개 정정**(7/7) + livertree→libertree 리네이밍
- **로그인 실사 2회**: "회원가입 필요 51"을 봇차단으로 재분류

---

## 6. 실패·한계·미완 (What Failed / Limitations)

### 6.1 미수집 138곳 (전부 사람 손 필요 또는 크롤러 재생성 대기)
| 분류 | 개수 | 회복 경로 |
|---|---|---|
| 봇차단(로그인·봇검증) | 38 | 사람이 브라우저에서 Cloudflare/로그인 통과 → 쿠키 전달 |
| **자동수집 미완(0건)** | **60** | 정부·연구 포털인데 현재 문서 0건 — **크롤러 (재)생성 시 대부분 수집 가능** (최대 회수 여지) |
| 상위도메인 통합수집 | 19 | 콘텐츠가 이미 상위도메인(hal.science 등)에 수집됨 — 중복, 사실상 확보 |
| 수동확인 필요 | 11 | 크롤러 0건 반환 — 사람 직접 분석 |
| 접속불능(소멸·차단) | 6 | Wayback Machine 정도 (성공률 낮음) |
| 부분차단 | 4 | 쿠키 작업 유사 |

> ⚠️ **"60곳 자동수집 미완" 주의**: scroll_index의 host가 포털(예 `www.admin.ch`, `www.usda.gov`)이면
> 그 정확한 netloc엔 0건이지만, 형제 서브도메인(bfe.admin.ch, search.nal.usda.gov 등)엔 데이터가 있는
> 경우가 섞임. **데이터 손실 아님** — host 집계 방식(정확 netloc 매칭)의 문제. 크롤러 재생성으로 회수 가능.

### 6.2 자동 크롤러 생성 실패 6곳 (수동 제작 대상)
`archives-nationales.culture.gouv.fr`, `nioz.on.worldcat.org`, `data.ademe.fr`, `news.va.gov`,
`observa.minciencia.gob.cl`, `compareschoolrankings.org`, `eia.gov`
→ AI가 2회(힌트+xhigh) 시도해도 실패. 사람이 개발자도구로 직접 분석해 크롤러를 짜야 함.

### 6.3 텍스트 미추출 잔여 7,903건 (OCR 없이 회복 불가)
전수 스캔 결과: 스캔형 PDF 3,410(OCR 후보) / 이미지파일 1,924 / 한글·오피스 1,371 / HTML·기타 1,198.
전체 문서의 ~1.7%. 상세: `docs/텍스트미추출_잔여분_설명.md`. OCR 트랙은 미착수(로컬 GPU로 가능).

### 6.4 pdf=0 사이트 (문제 아님)
신규 27곳이 pdf=0 — 대부분 뉴스·보도자료 **HTML 본문형**이라 원래 PDF 없음. abstract 100% 보유 → 검색 정상.

---

## 7. 핵심 스크립트 (scripts/)

| 스크립트 | 역할 | 주요 옵션 |
|---|---|---|
| `promote_all.py` | AI 생성 크롤러를 실제 수집(DB 적재) | `--sources claude_required_runs` `--only-site-ids-file` `--per-site-limit` `--per-site-timeout` `--concurrent-sites`. **`--resume` 재수집 시 금지, cp -n 금지** |
| `claude_required_runner.py` | 크롤러 자동 생성 (입력 CSV) | `--input` `--effort high/xhigh` `--max-runtime-seconds`. 입력 컬럼: entry_id,sheet,host,url,tier1_status,tier1_reason,priority,**extra_notes**(사이트별 힌트 주입) |
| `recover_pdfs.py` | PDF 회복 (UA/Referer/verify 보완) | `--throttle-sec` |
| `backfill_text_extraction.py` | 텍스트 추출 backfill | `--all` `--site-ids` `--max-workers` |
| `scan_fake_pdfs.py` | 가짜 HTML PDF 스캐너 | `--reset` `--delete-blob` |
| `reaudit_{probe,deep,js}.py` | 미수집 3단계 재실사 (이번 세션 생성) | - |
| `robots_listing_harvest.py` | 목록+URL 인벤토리 수집(DB 미기록) | - |
| `regen_client_report_20260720.py` | 수집현황 스프레드시트 재생성 | - |

**실행 패턴 (백그라운드 장기작업):**
```bash
setsid nohup .venv/bin/python scripts/<script>.py <args> > data/audit/logs/<name>.log 2>&1 < /dev/null &
```
> ⚠️ 하니스 백그라운드 태스크가 이유 불명 kill되는 이슈가 있어 **setsid nohup 분리 실행 필수**.

---

## 8. 비용 정책 (변경 금지)

| 구성요소 | 인증/모델 | 비고 |
|---|---|---|
| 크롤러 생성기 (claude_runner) | **OAuth (Claude 구독)** | `claude --model sonnet` 내장, 추가 과금 없음 |
| analyzer / summarizer | **API key** | 토큰 과금 |
| Gemma 요약 | 로컬 GPU **port 11436** (11434 금지) | 현재 일시중지 |

DB 쓰기는 **promote_all / runner / recover_pdfs / backfill 계열만**. summary_model은 UI 노출 금지.

---

## 9. 웹앱 / 배포 (finolaw)

- **접속**: `http://<서버IP>:3001` — Basic Auth 자격증명은 로컬 `.env`의 `ADMIN_USER`·`ADMIN_PASSWORD` 사용
- **컨테이너**: `crawler-poc:mvp`, `docker compose up -d` (현재 32시간+ healthy 가동 중)
- **구성**: Next.js 16 (포트 3001) + Python/Playwright 런타임. 볼륨: `./data`(DB rw), `./libertree`(blob ro), 크롤러 디렉토리
- **인증 게이트**: `finolaw/src/proxy.ts` (Next.js 16 proxy = 구 middleware). ADMIN_USER/PASSWORD 비면 auth 비활성(로컬 전용)
- 수집이 계속돼도 웹앱은 같은 DB를 읽어 **자동 최신화** (읽기전용, WAL이라 쓰기와 충돌 없음)
- **끄기/켜기** (프로젝트 루트에서 실행):
  ```bash
  docker compose stop      # 끄기 (컨테이너 유지, 데이터 보존)
  docker compose start     # 다시 켜기 (기존 컨테이너 재가동)
  docker compose down      # 컨테이너 제거 (이미지·볼륨은 유지, up -d로 재생성)
  docker compose up -d     # 처음부터 다시 기동 (빌드 이미지 crawler-poc:mvp 재사용)
  ```
  DB·blob은 호스트 디렉토리(`./data`, `./libertree`)에 있어 **컨테이너를 껐다 켜도/제거해도 데이터는 안전**하다.
  `restart: unless-stopped` 설정이라 서버 재부팅 시 자동 복구되지만, `docker compose stop`으로 명시적으로 끈 상태는 유지된다.

---

## 10. 납품 (진행 예정 — 태스크 미완)

- **방식**: 외장 디스크(2TB+) 물리 전달 (사용자 확정)
- **패키지**: libertree.db(5.2GB) + libertree/(1.7TB) + finolaw+docker-compose+Dockerfile + crawler/scripts + Phase G 문서 + sha256 manifest
- **순서**: 수집 완료 후 → DB WAL 체크포인트 → 스냅샷 확정 → sha256 manifest 생성 → 디스크 rsync 복사·검증
- **현재**: 스냅샷·manifest 미착수. 외장 디스크 연결되면 시작.

---

## 11. 알려진 함정 (Gotchas)

- **setsid nohup 필수** — 하니스 백그라운드 kill 이슈
- **promote_all `--resume` 금지** (재수집 시), 결과 cp 시 **`cp -n` 금지** (옛 실패 JSON이 새 성공 가림)
- **cp949(EUC-KR) CSV**: em-dash(—)·화살표(→) 등 특수문자 깨짐 → 텍스트 치환 필수
- **커스텀 크롤러 내부 예산**: `MAX_SECONDS`/`_WALL_SECONDS` 상수(기본 25분) — 대량 사이트는 상수 수정 필요
- **meta_url NOT NULL 제약** — NULL 대신 ''(빈문자열)
- **거대 PDF 추출 행(seq 431199, 10MB)**: pdfplumber 2h+ 지연 — 개별 처리 시 타임아웃 걸 것
- **PostToolUse 훅의 "Command failed"/"Write failed"**: 세션 내내 오탐, 무시
- **날짜 정규식**: 구 `\b((?:19|20)\d{2})` 패턴은 T-접미 ISO에서 매치 실패 — 그리스 크롤러 등에서 수정됨

---

## 12. 새 세션에서 이어서 할 수 있는 작업 (우선순위)

1. **자동수집 미완 60곳 크롤러 (재)생성** — 정부·연구 포털, 자동화로 상당수 회수 가능 (robots 38곳과 같은 방식)
   → `data/audit/미수집_사이트_사유.csv`에서 대상 추출 → claude_required_runner
2. **납품 패키지 완성** (태스크 미완): DB 스냅샷 + manifest + 외장 디스크 복사
3. **봇차단 38곳 쿠키 수작업** — 사람이 브라우저 통과 후 쿠키 전달 (가장 큰 수동 회복분)
4. **자동생성 실패 6곳 수동 크롤러 제작** — 개발자 직접 분석
5. **OCR 트랙 신설** — 스캔 PDF 3,410건 (로컬 GPU, API 비용 0)
6. **Gemma 요약 재개** — 36.5K/288K, API key 트랙

**참고 문서:**
- `docs/CURRENT_STATE.md` — 상태 종합 (수치·스키마·미수집 분류)
- `docs/NEXT_SESSION_PROMPT.md` — 빠른 재개용 복사 프롬프트
- `docs/텍스트미추출_잔여분_설명.md` — OCR 검토 자료
- `data/audit/reaudit_final_20260718.csv` — 미수집 152곳 재실사 최종 판정
- `data/audit/robots_허가확인.csv` — robots 38곳 기관 허가 결과
- `data/audit/수집현황_전체.xlsx` — 클라이언트용 대표 산출물
