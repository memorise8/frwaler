# 🤝 세션 핸드오프 — 2026-06-05

**용도**: 새 세션 시작 시 컨텍스트 0에서 작업 이어가기

새 세션에서 **이 문서 + `docs/CURRENT_STATE.md` 두 개만** 읽으면 즉시 작업 가능합니다.

---

## 1. 한 줄 요약

scroll_index.xlsx의 1,994 entries (833 unique hosts) 글로벌 정부·연구·학술 사이트를 자동 수집하는 libertree 시스템. 메타데이터 + PDF + 한국어 요약(Gemma).

---

## 2. 📊 현재 상태 (2026-06-05 11:02)

| 지표 | 값 |
|---|---|
| documents | **420,628건** |
| unique site_id | **674** |
| 적재 hosts | **595 / 833** |
| 전체 도달률 | **71.4%** |
| **회복 가능 700 대비** | **85.0%** ⭐ |
| Gemma summary | 34,611건 (요약 트랙은 일시 중지) |
| 디스크 (`/data_raid`) | 1.5T / 7.3T (충분) |

### 진행 흐름 누적 (5/22 ~ 6/5, 약 2주)
```
5/22  promote 자동 체인 종료    488 hosts / 353k docs
5/29  PoC Stealth Fetcher 60%   488 hosts (동일)
5/30  Static_list 178 시도       512 hosts / 373k docs
6/2   gen_unknown 117 재시도     557 hosts
6/3   promote 114 신규 적재      663 hosts
6/4   Static_list 49 재시도      669 hosts
6/5   promote 진행 중            674 hosts / 420k docs ← 현재
```

---

## 3. 🔥 지금 가동 중 작업

| PID | 작업 | 시작 | 상태 |
|---|---|---|---|
| **84927** | promote_all (49 신규 사이트 적재) | 11:00 | 🔄 진행 중 |
| 3598968 | Next.js port 3002 (FE) | 5/22 | 🟢 LISTEN |
| 1694887 | cloudflared quick tunnel | 5/15 | 🟢 alive |

### 현재 처리 중인 사이트 (마지막 확인 11:02)
- pubs-drdc-rddc-gc-ca-basis (캐나다 국방연구원) — 885건 적재 중
- gichd-org-publications-resourc (스위스 GICHD 지뢰제거) — 13건

### 예상 완료
- 49 사이트, 4 worker, 사이트당 15-30분 → **약 3-5시간 후 완료** (오늘 오후)
- 추가 예상: **+15-25 hosts, +수만 docs**

---

## 4. 🔍 작업 진행 상태 확인 방법

### 4.1 가장 빠른 점검 (3개 명령)
```bash
cd /data_raid/ruci_workspace/frwaler_job

# (1) 가동 중 프로세스
ps -ef | grep -E "promote_all|claude_required_runner|codex_required_runner|bulk_summarize" | grep -v grep

# (2) DB 통계
.venv/bin/python -c "
import sqlite3
c=sqlite3.connect('data/libertree.db')
docs = c.execute('SELECT COUNT(*) FROM documents').fetchone()[0]
sites = c.execute('SELECT COUNT(DISTINCT site_id) FROM documents').fetchone()[0]
print(f'docs: {docs:,}  sites: {sites}')
"

# (3) 최신 promote 로그
tail -10 $(ls -t data/audit/logs/promote_*.log | head -1)
```

### 4.2 적재율 정확히 계산
```bash
.venv/bin/python -c "
import sqlite3, csv
from urllib.parse import urlparse
c=sqlite3.connect('data/libertree.db')
db_hosts = set()
for (url,) in c.execute('SELECT DISTINCT meta_url FROM documents WHERE meta_url IS NOT NULL'):
    h = urlparse(url).hostname
    if h: db_hosts.add(h)
c.close()
input_hosts = set(r['host'] for r in csv.DictReader(open('data/audit/coverage_report.csv')) if r.get('host'))
mounted = input_hosts & db_hosts
print(f'적재 {len(mounted)}/833  전체대비 {100*len(mounted)/833:.1f}%  가능700대비 {100*len(mounted)/700:.1f}%')
"
```

### 4.3 핵심 로그 위치
| 로그 종류 | 경로 패턴 |
|---|---|
| Promote 실행 | `data/audit/logs/promote_*.log` |
| Runner (Claude/Codex) | `data/audit/logs/static49_*.log`, `retry2_*.log` 등 |
| Stealth PoC | `data/audit/logs/stealth_*.log` |
| Auto chain | `data/audit/logs/promote_transition.log` |

### 4.4 사이트별 결과 JSON 디렉토리
| 디렉토리 | 의미 |
|---|---|
| `data/audit/claude_required_runs/` | Claude runner 결과 (메인) |
| `data/audit/codex_required_runs/` | Codex runner 결과 (메인) |
| `data/audit/sample_runs/` | 초기 sample 결과 |
| `data/audit/static49_claude_runs/` | 6/4 49 사이트 retry |
| `data/audit/static49_codex_runs/` | 동일 |
| `data/audit/retry_claude_runs/` | 6/2 117 사이트 retry |
| `data/audit/retry_codex_runs/` | 동일 |

> retry 디렉토리는 별도였지만 6/3에 메인으로 cp 됨. 그래서 메인 디렉토리에 모두 통합됨.

---

## 5. 🎯 다음에 해야 할 일 (우선순위 순)

### 🥇 A. 49 사이트 promote 종료 대기 + 결과 확인 (자동)
**현재 진행 중** (PID 84927). 약 3-5시간 후 자동 종료.

종료 후 확인:
```bash
# 종료 감지
ps -ef | grep promote_all | grep -v grep && echo "still running" || echo "DONE"

# 적재 결과
tail -20 $(ls -t data/audit/logs/promote_static49_*.log | head -1)
```

### 🥈 B. spa_likely 35 사이트 Playwright 시도
**대상**: SPA(React/Vue) 사이트, JS 렌더링 필요
**기존 자원**: `crawler/playwright_fetcher.py` 이미 있음
**예상 추가**: +15~20 hosts

진행 방법:
```bash
cd /data_raid/ruci_workspace/frwaler_job

# 1) 35 사이트 list 추출
.venv/bin/python -c "
import csv, sqlite3
from urllib.parse import urlparse
c=sqlite3.connect('data/libertree.db')
db_hosts = set()
for (url,) in c.execute('SELECT DISTINCT meta_url FROM documents WHERE meta_url IS NOT NULL'):
    h = urlparse(url).hostname
    if h: db_hosts.add(h)
c.close()
seen=set(); chosen=[]
for r in csv.DictReader(open('data/audit/coverage_report.csv')):
    if r.get('render_class') != 'spa_likely': continue
    h = r.get('host')
    if not h or h in db_hosts or h in seen: continue
    seen.add(h); chosen.append(r)
import csv as csv_mod
with open('data/audit/spa_unmounted_35.csv','w',newline='') as f:
    w = csv_mod.DictWriter(f, fieldnames=list(chosen[0].keys()))
    w.writeheader(); w.writerows(chosen)
print(f'saved: {len(chosen)} sites')
"

# 2) Claude/Codex runner 호출 (별도 디렉토리)
mkdir -p data/audit/spa_claude_runs data/audit/spa_codex_runs
TS=$(date +%Y%m%d_%H%M%S)
nohup .venv/bin/python scripts/claude_required_runner.py \
    --input data/audit/spa_unmounted_35.csv \
    --out-dir data/audit/spa_claude_runs \
    --codex-out-dir data/audit/spa_codex_runs \
    --shard 0/2 > data/audit/logs/spa_claude_${TS}.log 2>&1 &
nohup .venv/bin/python scripts/codex_required_runner.py \
    --input data/audit/spa_unmounted_35.csv \
    --out-dir data/audit/spa_codex_runs \
    --shard 1/2 > data/audit/logs/spa_codex_${TS}.log 2>&1 &

# 3) 종료 후 메인 디렉토리로 cp + promote_all 실행 (위 A와 동일 패턴)
```

### 🥉 C. auth_blocked 51 사이트 (사람 회원가입 필요)
**대상**: 회원가입 필요한 학술 DB / 정부 통계 포털
**예상 추가**: +30-40 hosts (전수 시 50%)
**제약**: 사이트당 5-30분 사람 작업

진행 절차:
1. `data/audit/site_recovery_plan.csv` 에서 `category=🔐 사람개입필요-회원가입` 51개 추출
2. 가치 높은 사이트 선별 (50개 또는 일부)
3. 회원가입 → 브라우저 dev tools에서 쿠키 추출
4. `crawler/playwright_fetcher.py`의 cookies 옵션에 주입
5. promote_all 재실행

### D. UI 작업 (선택)
3개 plan 문서 있음 (`docs/` 안):
- `country_library_ui_plan.md` — 나라별 도서관 (~6h)
- `dashboard_1994_funnel_plan.md` — 메인 funnel 추가 (~2h)
- `uncollected_sites_directory_plan.md` — 수집 불가 사이트 디렉토리 (~2h)

### E. Gemma 요약 재개 (GPU 1 사용)
**중지된 진행 상태**: 진짜끝 chain 95/185 완료 (5/27 중지)
**잔여**: 90 사이트, 약 1.5일

재개 명령:
```bash
cd /data_raid/ruci_workspace/frwaler_job

# 1) ruci ollama GPU 1 다시 띄움
CUDA_VISIBLE_DEVICES=1 OLLAMA_HOST=127.0.0.1:11436 \
  OLLAMA_MODELS=/data_raid/ruci_workspace/ollama_models \
  OLLAMA_NUM_PARALLEL=4 nohup ollama serve > /tmp/ollama_11436.log 2>&1 &

# 2) chain watcher 재시작 (resume-safe — 채워진 summary는 자동 SKIP)
setsid scripts/wait_then_summarize_done_sites.sh 1 < /dev/null > /dev/null 2>&1 &
```

---

## 6. 🌐 접속 정보 (변경 없음)

```
URL:  https://celebrity-annie-schedules-passing.trycloudflare.com
ID:   ruci
PW:   aKInNf7gljXlDHIj1P8t
```

브라우저로 접속 시 Basic Auth → 모든 페이지 (/admin/status, /search, /admin/collection-report 등) 접근 가능. 실시간 데이터 표시.

---

## 7. ⚠️ 주의 사항

| # | 내용 |
|---|---|
| 1 | **DB는 운영 중** — 모든 함수는 read-only 패턴, 쓰기는 promote_all/runner만 |
| 2 | **summary_model UI 노출 금지** (사용자 명시) |
| 3 | **DB 경로**: `data/libertree.db` (finolaw/libertree.db는 빈 stub) |
| 4 | **TypeScript strict** — `any` 금지 (Next.js 16) |
| 5 | **Codex 인증**: `.codex-a`, `.codex-b` 둘 다 활성. 이전 unknown 실패는 인증 만료가 원인이었음. 6/4 정상 작동 확인. |
| 6 | **별도 runner 디렉토리 → 메인으로 cp**: cross-skip 우회 후 promote_all이 인식하게 cp 필수 |
| 7 | **promote_all `_SOURCE_CONFIGS`** 하드코딩: sample_runs, codex_required_runs, claude_required_runs만 인식 |
| 8 | **libertree → libertree 리네이밍**: 모든 작업 종료 후 진행 합의됨. 아직 미실행 |
| 9 | `--resume` 옵션은 cross-skip 트리거 — fail 케이스 재시도 시 별도 디렉토리 사용 필수 |
| 10 | Codex/Claude OAuth 정책: `OPENAI_API_KEY` 필드는 항상 null 유지 |

---

## 8. 📁 핵심 파일 경로

```
/data_raid/ruci_workspace/frwaler_job/
├── data/
│   ├── libertree.db                              # 메인 DB (~3.4 GB)
│   ├── audit/
│   │   ├── coverage_report.csv                   # 1,994 entries 마스터 (변경 금지)
│   │   ├── site_recovery_plan.csv                # 회복 카테고리 분류
│   │   ├── sites_finalized.csv                   # 진짜끝/사실상끝/진행중 분류
│   │   ├── claude_required_runs/                 # Claude 결과 JSON
│   │   ├── codex_required_runs/                  # Codex 결과 JSON
│   │   ├── stealth_full_scan.json                # Stealth PoC 결과
│   │   ├── promoted_all.jsonl                    # promote 진행 기록
│   │   └── logs/                                 # 모든 실행 로그
│   └── archived_taxlaw/                          # 세법 잔재 (보존)
├── libertree/AAAA/BBBB/AAAABBBBNNNN.{pdf,txt}    # blob 트리 (~685 GB)
├── crawler/
│   ├── analyzer.py                               # Tier 1 분석
│   ├── base_crawler.py                           # 모든 크롤러 base
│   ├── promote_all.py 의 동일 위치               # crawler에는 없음, scripts/에 있음
│   ├── playwright_fetcher.py                     # SPA 사이트용
│   ├── stealth_fetcher.py                        # PoC stealth fetcher (6/2 생성)
│   ├── claude_runner.py                          # Claude CLI 호출
│   ├── codex_runner.py                           # Codex CLI 호출
│   ├── summarizer.py                             # gpt-5.4-mini / Gemma
│   ├── sites/configs/*.json                      # 437개 declarative
│   └── sites/custom/*.py                         # 553+개 generated
├── scripts/
│   ├── promote_all.py                            # 일괄 적재 (핵심)
│   ├── claude_required_runner.py                 # Claude runner
│   ├── codex_required_runner.py                  # Codex runner
│   ├── bulk_summarize_gemma.py                   # Gemma 일괄 요약
│   ├── poc_stealth_recovery.py                   # 6/2 PoC
│   ├── stealth_full_scan.py                      # 6/2 전수 스캔
│   ├── codex_quota.py                            # OAuth quota 모니터링
│   └── wait_then_*.sh                            # 자동 체인 watcher (모두 종료됨)
├── finolaw/                                      # Next.js 16.2.4 UI
│   ├── src/app/                                  # 9 페이지 (/, /search, /admin/* 등)
│   ├── src/lib/db.ts                             # DB 함수 1700+ 줄
│   └── src/lib/categories.ts                     # sheet → 국가/대륙/카테고리
└── docs/
    ├── README.md                                 # 인덱스
    ├── CURRENT_STATE.md                          # ⭐ 시스템 종합 상태
    ├── SESSION_HANDOFF_2026-06-05.md             # ⭐ 이 문서
    ├── country_library_ui_plan.md                # 신규 UI 계획 1
    ├── dashboard_1994_funnel_plan.md             # 신규 UI 계획 2
    ├── uncollected_sites_directory_plan.md       # 신규 UI 계획 3
    └── superpowers/specs/2026-05-29-stealth-fetcher-poc-design.md
```

---

## 9. 🖥 환경 정보

| 항목 | 값 |
|---|---|
| OS | Ubuntu Linux 6.17 |
| 사용자 | ruci (uid 1001, docker/sudo 그룹) |
| sudo | 비밀번호 필요 (자동 sudo 불가) |
| Python | 3.12, `.venv/bin/python` (pip은 `.venv/bin/pip3`) |
| Node.js | 22.x (nvm) |
| GPU | RTX 5090 × 2 (각 32GB) |
| RAM | 255GB |
| `/data_raid` | RAID md0, 7.3TB |
| 서버 IP | `112.217.198.42` (LG U+ ISP, 평판 양호) |

---

## 10. 📋 빠른 시작 — 새 세션에서 첫 5분

```bash
# 1. 컨텍스트 잡기
cd /data_raid/ruci_workspace/frwaler_job
cat docs/SESSION_HANDOFF_2026-06-05.md
cat docs/CURRENT_STATE.md

# 2. 현재 상태 확인 (위 §4 명령들)

# 3. 가장 가능성 높은 다음 작업
# - promote_all (PID 84927) 끝났으면 → spa_likely 35 시작
# - 안 끝났으면 → 그냥 대기, 다른 우선순위 작업 검토
```

---

## 11. 🎯 작업 우선순위 매트릭스 요약

| 작업 | 추가 hosts | 비용 | 시간 | 자동 |
|---|---|---|---|---|
| A. promote 종료 대기 | 진행 중 | 0 | 3-5h | ✅ |
| B. spa_likely 35 | +15-20 | 0 | 6-12h | ✅ |
| C. auth_blocked 51 (회원가입) | +30-40 | 일부 학술DB 구독 | 수십 시간 | ❌ 수동 |
| D. UI 3개 plan | 0 | 0 | 2-7h each | ✅ (별도 세션) |
| E. Gemma 재개 | 0 (summary만 늘림) | $0 | 1.5d | ✅ |

**A → B → (D 또는 E) → C** 순서가 가장 효율적.

---

문서 마지막 갱신: 2026-06-05 11:10
다음 세션 시작 시: 이 문서 + `docs/CURRENT_STATE.md` 두 개 읽기
