# Libertree 현재 상태 종합 (2026-07-17 갱신)

> ⚠️ **역사적 스냅샷 — 현재 운영 기준이 아닙니다.** 이 아래의 모든 레거시 안내(“가장 먼저 읽으세요” 같은 표현 포함)는 역사 보존용이며, 현재 상태와 다음 세션 운영 절차는 전부 [`docs/HANDOFF.md`](HANDOFF.md)에 의해 대체됩니다.

새 세션을 열었다면 이 문서를 **가장 먼저** 읽으세요. 컨텍스트 0에서 작업 가능하도록 self-contained로 작성.
(빠른 재개용 복사 프롬프트는 `docs/NEXT_SESSION_PROMPT.md` 참조)

---

## 1. 프로젝트 한 줄 요약

클라이언트가 준 `scroll_index.xlsx`의 **1,994개 글로벌 정부·연구·학술 사이트**(unique hosts 833)에서 메타데이터 + PDF + 텍스트 + 한국어 요약을 자동 수집·관리하는 시스템. 데이터는 `data/libertree.db` (SQLite) + `libertree/` blob 트리에 저장, `finolaw/` Next.js UI로 조회.

> **2026-07-07 리네이밍 완료**: 프로젝트 전체가 livertree → **libertree** 로 개명됨.
> 구 경로 호환 심링크 유지 중: `data/livertree.db` → `libertree.db`, `./livertree` → `./libertree`

---

## 2. 디렉토리 구조

```
/data_raid/ruci_workspace/frwaler_job/
├── data/
│   ├── libertree.db                             # 메인 DB (~5.2 GB)
│   └── audit/                                   # 분류/로그/보고 CSV·XLSX·JSON
├── libertree/AAAA/BBBB/AAAABBBBNNNN.{pdf,txt}   # blob 트리 (~1.6 TB)
├── crawler/                                     # Python 크롤러 인프라
│   ├── sites/configs/*.json                     # 437개 declarative 크롤러
│   ├── sites/custom/*.py                        # 719개 codex/claude 생성 크롤러
│   ├── db_libertree.py                          # 스키마 정의 (ground truth)
│   ├── blob_storage.py                          # 12자리 seq_id → blob 경로
│   └── summarizer.py                            # 요약 (gpt-5.4-mini / gemini / gemma)
├── finolaw/                                     # Next.js 16 UI 앱 (포트 3002)
│   ├── src/lib/db.ts                            # DB readonly 접근 (1700+ 줄)
│   └── src/lib/categories.ts                    # sheet → 국가/대륙/카테고리
├── scripts/                                     # promote_all.py, recover_pdfs.py 등
└── docs/                                        # 문서 (본 디렉토리)
```

---

## 3. 핵심 데이터 (2026-07-20 기준 — 수치는 반드시 DB로 재확인)

| 지표 | 값 |
|---|---|
| 입력 entries | 1,994 (unique hosts 833) |
| **수집 완료 hosts** | **694 / 832 (83.4%)** |
| documents | **481,669** |
| DB 내 unique site_id | 761 (한 host에 여러 site_id 가능) |
| PDF 다운로드 | **297,756** |
| 텍스트 추출 | **288,124** |
| 한국어 요약 | 36,548 (Gemma 트랙 일시중지 중, 마지막 5/26) |

**2026-07-18~20 대규모 수집 완료 — 시스템 idle.** 세부:
- 증분 재크롤 700사이트 (2달치 신규분) + 텍스트 백필 6,740건 전수
- 미수집 152곳 3단계 재실사 → 수집가능 51곳 확인 → 재개방 46곳 크롤러 생성·수집
- robots 금지 38곳 전원 기관 허가 획득(2026-07-19) → 전용 크롤러 37곳 생성·수집
- 신규 83곳 promote 결과: 82곳 수집 성공(items 25,474 / pdf 12,982 / text 10,933), archives-nationales 1곳만 items=0 실패
- **적재 hosts 635 → 694 (+59 순증)**. pdf=0 사이트(뉴스·보도자료 HTML본문형)도 abstract 100% 보유 → 검색 가능
- 자동 생성 실패 6곳(수동제작 대상): archives-nationales, nioz worldcat, data.ademe.fr, news.va.gov, observa.minciencia.gob.cl, compareschoolrankings.org, eia.gov

### 상태 확인 명령 (세션 시작 시 실행)
```bash
cd /data_raid/ruci_workspace/frwaler_job
.venv/bin/python -c "import sqlite3; c=sqlite3.connect('data/libertree.db'); \
print('docs', c.execute('SELECT COUNT(*) FROM documents').fetchone()[0], \
'pdf', c.execute('SELECT COUNT(*) FROM documents WHERE pdf_downloaded=1').fetchone()[0], \
'summary', c.execute(\"SELECT COUNT(*) FROM documents WHERE summary IS NOT NULL AND summary != ''\").fetchone()[0])"
ps -ef | grep -E "recover_pdfs|promote_all|backfill|bulk_summarize" | grep -v grep
```

### DB 스키마 (documents 주요 컬럼)
seq_id(PK), collected_at, site_id, post_number, meta_url, title, published_date, listed_date, authors, publisher, journal, pdf_url, keywords, abstract, original_filename, pdf_downloaded, text_extracted, pdf_size_bytes, pdf_sha256, summary, summary_model, summary_at
- FTS5: `documents_fts` (trigram)
- `sites` 테이블: site_id(PK), site_name, site_url, sheet, created_at

### Blob 규칙
`libertree/AAAA/BBBB/AAAABBBBNNNN.{pdf,txt}` — 12자리 zero-padded seq_id를 4-4-4 분할. 유틸: `crawler/blob_storage.py get_blob_path()`, TS는 `finolaw/src/lib/db.ts blobPath()`.

---

## 4. 미수집 198 사이트 분류 (2026-07-14 실사 반영)

| 분류 | 개수 | 남은 경로 (전부 수동) |
|---|---|---|
| 정책상 포기 | 63 | 운영 기관 직접 컨택 |
| 봇차단 (전면37+부분9) | 46 | 사람이 브라우저에서 차단 통과 → 쿠키 전달 (7/13 실사로 "회원가입 필요"에서 정정 — 로그인 확인된 곳은 5곳뿐) |
| 확인 중 | 2 | ots.at(언론전용 가능성)·culture.gov.gr(지역차단 의심) |
| 재시도 실패 | 48 | 수동 분석 또는 Wayback |
| 절대 불가 | 38 | Wayback Machine (성공률 10~20%) |
| 미분류 | 1 | gob.mx — 수집 범위 정의 필요 (oka.go.kr는 7/14 수집 완료) |

> 자동 회복 수단은 소진됨 (6월에 3회 자동 재시도 완료).
> ⚠️ 종전 "라벨오류 18" 분류는 2026-07-07에 정정 완료 — 실제로는 수집돼 있어 수집성공 명단으로 이동 (613→631).

### 보고 파일 (비개발자 전달용, `data/audit/`)
- **`수집현황_전체.xlsx`** ⭐ 대표본 — 시트: 수집성공(635) / 미수집(198)
- `수집현황_요약.csv`, `미수집_사이트_사유.csv`, `미수집_사이트_분류설명.csv` (모두 EUC-KR)
- `site_collection_status.csv` — 833 hosts 원본 소스 (UTF-8, 재생성용)
- `docs/미수집_사이트_설명.md` — 비개발자용 설명 문서
- 정정 전 원본 백업: `data/audit/backup_20260707/`

---

## 5. 최근 이력 (5월 말 이후)

| 시기 | 내용 |
|---|---|
| ~5/27 | 자동 수집 체인 완료 (promote cap500 → cap30k → cap100k → unprocessed, QA 3회) |
| 5/27 | Gemma 요약 트랙 일시중지 (GPU 반납, 34.6K건 시점) — 이후 재개 안 됨 |
| 6/9~11 | PDF 회복 프로젝트: `recover_pdfs.py`(UA/Referer/verify=False/GET 보완)로 **+21K PDF, +29K text** |
| 6/9~11 | fake-HTML 블록 정리, converter.py에 HWP/HWPX 매직바이트 감지 추가 |
| 6/18 | 미수집 사이트 사유별 분류 + 비개발자 보고 파일 작성 |
| **7/7** | **라벨오류 18개 정정** (수집률 613→631/833) + **livertree→libertree 전면 리네이밍** (61개 파일 치환, DB/blob/브랜치 개명, 호환 심링크 유지) |
| **7/11** | **메타데이터 정제 완료**: title/abstract/keywords 엔티티·태그·CDATA 정리(~24k행) + 날짜 정규화(listed 17,623건/published 380건, 원값 유지분은 진짜 파싱불가) + URL 정리 · **가짜 PDF 정리**: 3,704건 리셋(PDF 270,519→**266,815**) · **백필**: masaf 인코딩 수정(title 154건 100% 복구), 그리스 3사이트(mindev/mindigital/ypergasias) `_parse_date` 정규식 버그 수정으로 published_date 4,581건 100% 백필. 상세: `data/audit/metadata_cleanup_final_report.md` |
| **7/13~14** | **"회원가입 필요 51" 전수 실사** — 로그인 확인 5곳뿐, 대부분 봇차단으로 정정 (`data/audit/로그인사이트_실사_20260713.csv`) · **재개방 4사이트 크롤러 생성·수집**: mentalhealthcommission.ca(500) + ameslab.gov(392) + repo.lib.duth.gr(500) + oka.go.kr(30) = **+1,422 문서, hosts 631→635** |

| **7/16~17** | **cap 확장 재수집**: duth 500→**7,132** 문서(+PDF 5,438, 그리스 학위논문 저장소), mentalhealth 500→589 · 크롤러 내부 25분 예산이 병목이라 5시간으로 확장 · 텍스트 추출 잔여분은 backfill로 완료(스캔본 170건은 텍스트 레이어 없음, seq 431199는 추출 불능 known-problem) |

상세 개발 이력: `libertree_crawler.md` (페이즈별 변경 로그)

---

## 6. 데이터 정책 (변경 금지)

1. **비용 트랙 분리** (절대 유지)
   - OAuth(구독): 크롤러 생성 — codex_runner / claude_runner
   - OpenAI API key: analyzer.py, summarizer.py (gpt-5.4-mini)
   - 로컬 GPU(무료): Qwen3 8B 요약 — port **11436** Ollama (11434는 CPU fallback, 사용 금지)
2. **summary_model 컬럼 UI 노출 금지** (관리자 디버그 뷰만 허용)
3. **DB는 운영 중** — 쓰기는 promote_all / runner / recover_pdfs 계열만, UI는 readonly
4. **runner 결과 cp 시 `cp -n` 금지** — 옛 실패 JSON이 새 성공 결과를 가려 promote 누락됨 (6/11 실제 사고)
5. EUC-KR CSV 재생성 시 한글 이모지·특수문자(—·↔) 텍스트 치환 필수

---

## 7. 다음 작업 후보 (우선순위)

1. **봇차단 46 사이트 수동 쿠키 작업** — 가장 큰 잔여. 브라우저에서 차단 통과 후 쿠키 전달 (순서표: `data/audit/로그인사이트_실사_20260713.csv`)
1-2. (완료 7/17) 신규 4사이트 cap 확장 재수집 — duth 7,132·mentalhealth 589로 전량 수집
2. **Qwen3 8B 요약 트랙 재개** — resume-safe (채워진 summary 자동 skip). 재개 명령:
   ```bash
   CUDA_VISIBLE_DEVICES=1 OLLAMA_HOST=127.0.0.1:11436 \
     OLLAMA_MODELS=/data_raid/ruci_workspace/ollama_models \
     OLLAMA_NUM_PARALLEL=1 nohup ollama serve > /tmp/ollama_11436.log 2>&1 &
   setsid scripts/wait_then_summarize_done_sites.sh 1 < /dev/null > /dev/null 2>&1 &
   ```
3. UI 확장 (계획 문서만 존재): `country_library_ui_plan.md`, `dashboard_1994_funnel_plan.md`, `uncollected_sites_directory_plan.md`
4. 워킹트리 커밋 정리 — 7/7 리네이밍 + 라벨오류 정정 변경분이 미커밋 상태 (git mv 2건은 스테이징됨)
5. (해소됨 7/11) masaf title 개행 10건 — cleanup 재실행으로 0건 확인, masaf 크롤러에 제목 정규화 추가(67d8c27)로 재발 경로 차단
6. (후속 후보) 타 크롤러 20곳에 동일한 `\b` 날짜 정규식 버그 잔존 (그리스 3곳만 수정됨) — 재크롤 계획 시 함께 수정 권장. 상세: `.superpowers/sdd/final-review.md` m3

---

## 8. UI (finolaw, 포트 3002)

| 경로 | 용도 |
|---|---|
| `/` | 메인 대시보드 (KPI) |
| `/search`, `/search/[seq_id]` | FTS5 검색 / 문서 상세 |
| `/admin/status` | 사이트별 진행도 |
| `/admin/collection-report` | 수집 funnel + 누락 사유 |
| `/admin/summary` | 요약 생성·검토 |

빌드+재시작:
```bash
cd finolaw && npm run build
pkill -f "next start -p 3002"; sleep 2
nohup ./node_modules/.bin/next start -p 3002 -H 0.0.0.0 > /tmp/next3002.log 2>&1 &
```

외부 접속: cloudflared quick tunnel (PID 1694887, 5/15부터 가동).
URL이 죽었으면: `ls -t data/audit/logs/cloudflared_quick_*.log | head -1 | xargs grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" | head -1`

---

## 9. 환경

- Ubuntu (커널 6.17), zsh / Python 3.12 (`.venv/`) / Node 22.x (nvm)
- GPU: RTX 5090 × 2 (32GB) / RAM 255GB / /data_raid RAID 7.3TB
- git: 브랜치 `libertree` (로컬 전용, 원격 origin=github.com/memorise8/frwaler)
- 메모리: `~/.claude/projects/-data-raid-ruci-workspace-frwaler-job/memory/` (세션 간 자동 로드)
