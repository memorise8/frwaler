# 케파(수집 가능 총량) 산정 — 상태 저장 (compact 재개용)

작성: 2026-08-03. 이 문서만 읽으면 compact 후 이어서 진행 가능.

## 배경 / 목표
- 시작: `test.xlsx`(1,995 URL) 수집 확인 → "받을 수 있는 전체 데이터가 얼마인가(케파)" 산정으로 확장.
- **사용자 확정 방침: "무조건 다 받는다"** → DOAJ 등 초대형 애그리게이터 **포함**해서 전량 계산.
- 전 과정 **count-only(무저장)** — 디스크 증가 0, DB 읽기전용. 실제 다운로드는 스토리지 확보 후 별도.

## 현재 확정 숫자 (다 받기 기준) — finalize 확정본 (2026-08-03)
- 보유(이미 수집): **525,570건 / held PDF 1,667GB (실디스크 ≈1.8TB)** (804 사이트)
- **측정 787/804 사이트, 가용 총계(하한) = 19,806,072건 (약 1,980만)**
  - exact(자연종료+API+server_total) 19,673,421 (474개)
  - 과소치(내부캡+외부상한, 실제 더 큼) 132,651 (313개)
  - 미측정 17개
- **=> 총 케파 하한 ≈ 19,806,072건 / 전량수집 필요용량 ≈ 72 TB** (보유대비 커버리지 2.7%)
- DOAJ 13,362,110 = 전체의 67% (server_total 정확값). HAL은 기관별 포털 개별집계(글로벌 1.5M 오염분 제외됨).
- 현재 디스크 여유 3.6TB → **약 20배 부족.** `/data_raid/share/gdrive`(1PB 마운트)가 대안 후보.
- 최종 산출물: `scripts/audit/capacity_final_report.xlsx`(5시트) + `.md`. finalize_capacity.py 재실행하면 갱신됨.
- ※ 이전 수기추정 18.06M/66TB는 C를 collected floor로 계산한 것; finalize는 C를 count-crawl 실측 하한으로 반영 → 1,980만이 더 정확.

## 사이트 분류 & 측정 상태 (804개)
| 분류 | 개수 | 측정 상태 |
|---|---:|---|
| **A. server_total(API즉시)** | 61/93후보 | ✅ 정확. 합 14,041,780 (DOAJ 13.36M). `count_only_totalscan.csv` |
| **B. 구조화 API(CKAN/DSpace/OAI/HAL)** | 93 | ✅ 정확. 합 3,926,070 |
| **C2. API가능한데 자동측정 실패** | 19 | ❌ 0/19 성공. floor(수집)만 13.8만(etera 100k가 대부분). `c2_sites.txt`,`c2_totals.csv` |
| **C1. 순수 HTML** | 510 | ❌ 미측정. count 하한값만. `html_list.csv` |
| D. 크롤러없음 | 18 | 측정불가 |
| E. 기타/불명 | ~ | count 하한 |

- **C(=C1+C2) 529개 = `html_list.csv`**. 이 중 자연종료(진짜끝) 213개만 신뢰, 나머지(캡걸림150+count0 75+불명91)는 하한이라 실제 더 큼.
- C에 대형이 섞여있음: **etera-ee 100,403**(이미수집), repository-tno 10,508, naturalis 9,681 등 → floor로 규모 앎.

## DOAJ (핵심)
- `doaj-org-search`, 실제 total **13,362,110** (사이트표시 13.36M과 일치). 우리 보유 1,417(0.01%).
- 메타 애그리게이터(전세계 OA논문). **다 받기로 확정** → 케파의 76%, 스토리지 51TB+.

## 만든 스크립트 (scripts/audit/)
- `count_only_harness.py` — 핵심. count-only 하네스. **개선완료**: ①neutralize_caps 정규식화(page+wall 캡 무력화, class/module/`__globals__`) ②server_total 포착(json.loads 감싸 total_count류 자동캡처, CSV에 server_total 컬럼) ③`--only`가 measured 사이트도 포함. `--only <ids> --site-timeout N --workers W --out-csv X`.
- `finalize_capacity.py` — 병합+보고서. 우선순위 uncapped>1h>초기>baseline, server_total>count면 채택. `capacity_final_report.xlsx`(5시트) 생성.
- `run_totalscan.py` — total_candidates.txt(93개) server_total 스캔 런처.
- `remeasure_c2.py`,`remeasure_c2_v2.py` — C2 API측정 시도(실패). 참고용.
- `total_candidates.txt`(93 A후보), `c2_sites.txt`(19), `capped_sites.txt`(283 상한사이트).
- 결과CSV: `count_only_totals.csv`(초기 586 count), `count_only_capped_1h.csv`(1h 재측정 부분), `count_only_totalscan.csv`(A server_total), `coverage_report.csv`(초기 API감사), `custom_crawler_totals.SNAPSHOT.csv`.
- 루트 `html_list.csv` — C 529개 목록(site_id,sheet,collected,count하한).

## 기술적 교훈 (재개시 주의)
- **zsh는 `$(cat file)` 단어분리 안 함** → `--only`에 파일 넘길 때 파이썬 런처(argv 직접 주입) 써야 함. `nohup &`+run_in_background 이중백그라운드 금지.
- 백그라운드 대기는 **PID 파일 신뢰금지(오래된 값)** → `os.kill(pid,0)` 또는 프로세스명(/proc cmdline, zsh래퍼 제외) 기준.
- 크롤러 캡이 **함수 로컬변수**면 무력화 불가(예: data-busan `max_pages=200`+25분). class/module 상수만 가능.
- C2 API 사이트들 quirk: EPrints OAI가 completeListSize 안줌 / CKAN 봇차단 / etera 커스텀SPA. 자동측정 난망 → 개별 수작업 필요.

## 다음 할 일 (우선순위)
1. **최종 보고서 확정**: `python3 scripts/audit/finalize_capacity.py` 재실행 → `capacity_final_report.xlsx` 최신화 (DOAJ 포함 총량 반영). ⚠️ 지금 report는 중간본일 수 있음.
2. (선택) **A의 32개 미포착** server_total 재시도 (45초 초과했던 것 상한 늘려).
3. (선택) **C1 510개** 처리 결정: 전량 크롤(며칠) vs 하한확정. 케파 대세엔 영향 작음(수십만).
4. (선택) C2 대형 3개(etera·tno·naturalis) 개별 정확 total.
5. **실제 재수집**: 스토리지(66TB+) 확보가 선결. 대형 우선(DOAJ·HAL·DSpace). 크롤러 캡(500/25분/MAX_PAGES) 상향 필요.

## ★ 재개 진입점 (2026-08-03 갱신 — compact 후 여기부터)
**핵심 need(사용자 반복 강조):** "그냥 전체 데이터를 다 수집하면 몇 건 / 몇 TB냐" — **중복제거(net) 안 함**, 딴 분석 안 함. 스토리지 구매 근거가 목적.

**현재 숫자(정직):**
- 레코드 gross(중복포함) ~1,830만 (HTML 미측정으로 흔들림, ±수십만)
- PDF 원문 ~576만 / **스토리지 ≈ 31TB** (실측 평균 5.63MB/건 — 이건 안 흔들림) → 구매권고 35~40TB
- DOAJ: 13.36M 중 저장필터 89.8%→12.0M 메타, PDF율 15.1%±0.9%(대규모샘플)→2.0M PDF/10.8TB

**방금 완료: 캡 롤아웃 (744개 크롤러 env화)**
- `LIBERTREE_MAX_PAGES`(기본200), `LIBERTREE_MAX_WALL_S`(기본1500=25분) 로 전 크롤러 캡 제어. env 미설정시 동작불변.
- 롤아웃 스크립트: `scripts/audit/raise_caps_rollout.py`(--apply함), 인라인벽시계 71개는 `fix_inline_wallclock.py`로 처리. py_compile 799개 에러0.
- **미결정 1개**: `repo-lib-duth-gr-jspui.py`의 `_WALL_SECONDS=300*60`(의도적 5시간) — env화할지 사용자 결정 대기.

**다음 할 일 (사용자 지시):**
1. HTML 하한 사이트 **깊이 재크롤 2시간 설정으로 재실행** — env: `LIBERTREE_MAX_PAGES`(크게, 예 100000) `LIBERTREE_MAX_WALL_S=7200`(2h), site-timeout 7200. 대상 `scripts/audit/lowerbound_sites.txt`(313개). ⚠️경고: 사이트당 최대 2h라 전체 수 시간~며칠. prism-go-kr·etis-ee는 이전 10분크롤서 38만·33만으로 튐(미검증-부풀림 의심, 검증필요). 스토리지엔 영향 거의 없음(HTML=저PDF).
2. (선택) 고장크롤러 수정: 73개 중 ~23확정 수정가능(셀렉터/도메인)+스텔스로 추가전환(andra·apra 등). `broken_fixability.csv`. 스텔스 재검증은 hung으로 미완.
3. (선택) 데이터손실 5개 수정(e-stat·apra·data-gov-au·datos-gob-cl·avoindata — 소스쿼리 PDF필터 제거).

**산출물 파일:** `capacity_final_report.xlsx`(레코드), `RECOLLECTION_PLAN.md`, `CRAWLER_REVIEW.md`(크롤러검수), `crawler_audit.csv`, `broken_fixability.csv`, `pdf_rate_sweep.csv`, `doaj_bigsample_result.txt`, `crawler_health_probe.csv`+`health_timeout_recheck.csv`(헬스 673ok/73고장/28느림).

**교훈(반복금지):** ①레코드≠PDF 구분 일관유지(스토리지 헤드라인으로 몰다가 DOAJ 12M 메타 빠뜨려 사용자가 지적). ②측정 자꾸 새로 벌이지 말 것(10분 재크롤 등 불필요 반복). ③etime은 mm:ss(hh:mm:ss)—시간 오독 주의. ④결론 억지로 내지 말고 상태 정직히.

## 한 줄 요약
**레코드 케파 ≈ 1,980만건(DOAJ 13.4M 포함). 단 PDF 실용량은 DOAJ 실체검증(2026-08-03)으로 72TB→~30~32TB 보정(DOAJ 직접PDF 12~18%뿐, 51TB아님). 재수집 준비=`RECOLLECTION_PLAN.md`. 저장타깃=로컬디스크 ~35TB. 다음: 캡 env화(b)+재개검증(d).**
