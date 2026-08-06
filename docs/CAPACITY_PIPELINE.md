# 케파 산정 파이프라인 — 상태·재개 문서

> 최종 갱신: 2026-08-04 (compact 대비 영속 문서)
> 목적: "804개 사이트에서 받을 수 있는 전체 데이터(케파)"를 산정해 최종 보고서(`capacity_final_report.xlsx`)로 정리.
> 배경: 업체가 "50만 개가 전부가 아니다"라고 함 → 전량 규모 확인 후 "무조건 다 다운로드" 전 스토리지 산정.
> **전 과정 count-only(무저장), DB 읽기전용. 실제 다운로드는 스토리지 확보 후 별도 단계.**

---

## 1. 전체 그림: 3단계 파이프라인

| 단계 | 내용 | 상태 (2026-08-04) |
|---|---|---|
| **① sweep** (C 전수 측정) | 544개 HTML 사이트를 실제 크롤러로 돌려 규모 측정 + 크롤러 고장 검진 | ✅ **완료 544/544** (C 1.15M + E15 81k) |
| **② exact_probe** (정확값) | 544개 전체 probe — 실패 사유는 `tests.xlsx` 'exact_probe_실패' 시트 | ✅ **완료**: 정확값 293(2.15M, ots-at 1.56M 발견) / 실패 251(사유 기록됨). 청크판 `run_exact_chunked.py` 사용(단일프로세스 FD고갈 → 40개 청크 격리로 해결) |
| **③ 최종 보고서** | A+B+C+probe 병합(max 규칙) → `capacity_final_report.xlsx` (5시트) | ✅ **생성됨 (2026-08-04)**: **총 가용 하한 22,923,093건 / 84TB** (DOAJ 제외 9.56M). 802/804 측정. count_status: exact 591개 22.73M / 과소 211개 0.19M |

## 2. 804개 사이트 분류 (2026-08-04 파일 기준 확정)

| 분류 | 개수 | 상태 |
|---|---:|---|
| **A. API server_total 즉시** | 후보 93 → **61 확보** | ✅ **14.04M** (DOAJ 13.36M = 95%) |
| **B. 구조화 API 측정** (CKAN/DSpace/OAI/HAL) | 53 | ✅ **2.04M** |
| **A+B 정확측정 합계** | **114** | ✅ **16.08M** (DOAJ 제외 2.72M) |
| **C. HTML 페이징만 가능** | **529** (`html_list.csv`) | 🔄 sweep 진행중 |
| **E. 크롤러 있음·baseline 측정됨** | 141 | ✅ 값 있음, 추가작업 불필요 |
| **E. 크롤러 있음·미측정** | **15** | ✅ 2026-08-04 사용자 승인 → `html_list.csv`에 추가됨(총 544행). 현재 sweep 런 종료 후 `run_c_full.py` 재실행하면 재개 로직이 잔여 15개 자동 측정 |
| **D. 크롤러 없음** | **26** (datos.gob.mx CKAN 8 + gob.mx 프렌사 18) | ✅ **완료 (2026-08-06)**: 26곳 크롤러 제작+측정 = **72,667건** (CKAN 379 + gob.mx 72,288, 18곳 자연종료). gob.mx는 Akamai 봇방어 → Playwright 1회 솔브 후 쿠키 재사용. 커밋 `4dba783`, 상세 메모리 `project_crawler_share_20260806` |

- A 후보 중 실패 32개는 C 성격 → sweep/probe에서 커버.
- DOAJ 확정: `doaj-org-search` server_total=13,362,110 (사이트 표기 13,362,044와 일치). 메가 애그리게이터라 전체를 좌우.

## 3. sweep (C 전수 측정) — 작동 방식·재개법

- **명령**: `PYTHONPATH=. .venv/bin/python scripts/audit/run_c_full.py > scripts/audit/run_c_full.run.log 2>&1 &`
- 실제 크롤러를 count-only로 실행(상세페이지까지 받아 세지만 저장 안 함). 사이트당 상한 **2h**, workers 12.
- **재개형 구조** (2026-08-04 추가 — 이전 "안 끝나던" 원인 해결):
  - harness `main()`은 `--out-csv`를 `mode="w"`로 덮어쓰므로, 런처가 2파일로 분리 관리:
    - **MASTER** `scripts/audit/count_only_c_full.csv` — 누적 (완료 사이트)
    - **PART** `scripts/audit/count_only_c_part.csv` — 이번 실행 임시
  - 시작 시 PART→MASTER fold(잔여 병합) 후 MASTER에 있는 site_id 건너뜀 → **중단돼도 `run_c_full.py` 재실행만 하면 이어짐**
  - fold = site_id 기준 병합(최신 우선), 원자적 재작성. 백업: `count_only_c_full.bak.csv`
- 타임아웃 강제종료는 harness `run_all()`의 terminate→kill (L594-601) — 무한대기 없음.
- **sweep 값의 한계** (그래서 ②가 필요):
  - `completed=False` = 2h에 잘림 → **하한값**
  - `completed=True & counted<3` = **의심** (크롤러 고장/얇음, 허위 저값. 예: afp=1인데 실제 2,025)
- 부수효과: **크롤러 건강검진** — 의심 목록은 전량 다운로드 전 수리해야 할 크롤러 목록.
- per-site 로그: `scripts/audit/out/count_only_logs/{site_id}.log`

## 4. exact_probe (정확값 측정기) — `scripts/audit/exact_probe.py`

**원리**: 크롤러를 잠깐 돌려 실제 요청 URL을 HTTP 후킹으로 캡처(subprocess/requests/curl_cffi/StealthSession) → 리스트 URL·page 파라미터·상세경로 자동 추출 → **상세페이지는 안 받고** 리스트 HTML만으로 총량 산출.

**방법 우선순위**:
1. `total_text` — "총 N건"/totalCount 등 텍스트 → 즉시 정확
2. `lastpage_verified` — 페이지네이션 끝페이지 링크 검증(마지막p 항목有 + 그 다음p 빈/반복) → `(maxpage-1)×per_page + last_cnt`, 3 fetch로 즉시
3. `list_walk` — 리스트만 배치병렬(6)로 끝까지 넘기며 합산 (무한루프는 내용 시그니처 반복으로 차단)

**검증 완료** (sweep 하한 → probe 정확값):
| 사이트 | sweep | probe | 방법 |
|---|---|---|---|
| ftc-go-kr-www | ≥8,313 | **9,716** | lastpage_verified |
| bok-or-kr-portal | ≥9,887 | **16,332** (1.65배!) | list_walk 1,634p 완주 |
| afp-gov-au-search | 1 (의심) | **2,025** | total_text → 크롤러 고장 확정 |

**해결한 구현 이슈**: ①`_Stop`은 BaseException(크롤러 broad except에 안 삼켜짐) ②캡처는 thread-local(병렬 교차오염 방지) ③`fetch()`는 원본 subprocess.run 사용(후킹 우회) ④크롤러 인스턴스화 `cls(db_conn=ro_conn, delay=0)`.

**한계**: page 파라미터 생략형(예: gnews-gg-go-kr 1페이지)은 리스트/상세 오인 → `no_per_page`/`no_list_url` 플래그로 수동확인 대상 분리.

**실행**(sweep 완료 후):
```
PYTHONPATH=. .venv/bin/python scripts/audit/run_exact_after_sweep.py
```
- 대상 자동선정: 캡(completed=False) + 의심(counted<3). 출력 `scripts/audit/exact_probe.csv` (--resume 지원).
- 단독 실행: `exact_probe.py --only <ids> --workers 6 --walk-wall 1200 --resume --out-csv ...`

## 5. 최종 보고서 — `scripts/audit/finalize_capacity.py`

- 병합 우선순위: uncapped > 1h > initial count > baseline. server_total > count면 채택(status=server_total).
- **TODO(③때 수정)**: `count_only_c_full.csv`(sweep)와 `exact_probe.csv`(정확값)를 입력에 추가.
  **병합 규칙 = 사이트별 max(sweep counted, probe exact_total, server_total)** — probe의 `list_walk total=1~2` 같은
  극소값은 페이지 파라미터 오인식 아티팩트이므로 max 로 하한 보호(작은 probe 값이 큰 sweep 값을 덮지 않게).
- 출력: `capacity_final_report.xlsx` 5시트(요약/사이트별/국가별_롤업/크롤러캡_상향필요/상한재측정_대상) + `.md`. 의심 크롤러 suspect 플래그 추가 예정.
- 용량 환산: 3.83MB/건. 전량수집 시 최소 수십TB(DOAJ만 ~51TB), 현재 여유 3.6TB, gdrive 1PB 마운트(`/data_raid/share/gdrive`) 후보.

## 6. 다음 작업 (compact 후 재개 체크리스트)

**①②③ 완료 (2026-08-04). 최종 수치: 가용 하한 22,923,093건 (DOAJ 13.36M / e-stat 1.71M / ots-at 1.56M[API 검증] 3대 대형), 84TB.**
**+ 2026-08-05: 크롤러 전체 건강검진(정상 718/778) + broken 44개 수리(27 수리/17 인프라차단). 상세 = `docs/PROGRESS_20260805.md`, `scripts/audit/CRAWLER_REPAIR_20260805.md`, `crawler_health_full.csv`.**
남은 것:
1. ~~sweep→probe→보고서~~ ✅ 전부 완료
2. ~~E-미측정 15개 sweep 추가~~ ✅ 완료(2026-08-04): html_list.csv 544행으로 확장됨.
   추가된 15개: acma-gov-au-publications, ag-gov-au-publications, data-e-gov-go-jp-data, defence-gov-au-publications, dfat-gov-au-publications, earth-prints-org, flore-unifi-it, government-se-publications, health-gov-au-publications, health-govt-nz-publications, iris-unitn-it, justice-govt-nz-publications, nhmrc-gov-au-publications, openaccess-inaf-it, treasury-govt-nz-publications
3. ~~**D 26개** 크롤러 제작+측정~~ ✅ 완료(2026-08-06): CKAN 8 + gob.mx 18 = 72,667건. 커밋 `4dba783`. 크롤러 공유 패키지 `crawlers-share/`도 제작(독립 실행, 비밀키 제외).
4. finalize_capacity.py에 C+probe 입력 추가 → ③ 최종 보고서 생성
5. 보고서에 포함: 총 케파(하한/정확 구분), DOAJ 포함/제외 두 버전, 국가별, 수리필요 크롤러 목록, 스토리지 소요

## 7. 핵심 파일 맵

| 파일 | 내용 |
|---|---|
| `html_list.csv` (루트) | C 529개 목록 |
| `scripts/audit/count_only_c_full.csv` | sweep MASTER (누적) |
| `scripts/audit/count_only_c_part.csv` | sweep PART (임시, 실행중에만) |
| `scripts/audit/run_c_full.py` | sweep 런처 (재개형) |
| `scripts/audit/exact_probe.py` | 정확값 측정기 |
| `scripts/audit/run_exact_after_sweep.py` | ② 자동 러너 (캡+의심 선정) |
| `scripts/audit/exact_probe.csv` | ② 결과 (누적) |
| `scripts/audit/count_only_harness.py` | count 하네스 (캡무력화+server_total 포착) |
| `scripts/audit/count_only_totalscan.csv` | A 93개 server_total 결과 |
| `scripts/audit/coverage_report.csv`, `custom_crawler_totals.SNAPSHOT.csv` | B/baseline |
| `scripts/audit/finalize_capacity.py` | ③ 병합+보고서 |
| `scripts/audit/total_candidates.txt` | A 후보 93 |
| `scripts/audit/out/count_only_logs/` | per-site 로그 |

## 8. 주의사항

- zsh에서 `$(cat file)`은 단어분리 안 됨 → **python 런처로 sys.argv 직접 전달** (run_c_full.py / run_exact_after_sweep.py 방식 유지)
- 로컬변수 캡(data-busan `max_pages=200` 등)은 무력화 불가 → server_total/probe로 우회
- 크롤러 727개는 `LIBERTREE_MAX_PAGES`/`LIBERTREE_MAX_WALL_S` env로 캡 조절 가능(기본 200p/25분) — 전량 다운로드 때 사용. count 하네스는 이미 자체 무력화하므로 env 불필요
- 크롤러 생성기는 OAuth, analyzer/summarizer는 API key (변경 금지)
