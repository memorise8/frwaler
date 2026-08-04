# 케파 전수조사 — 상태 & 재개 핸드오프 (2026-08-01)

## ✅ 완료 (2026-08-02 17:01) — 전수조사 789/789 완주
- **최종 케파**: CONFIRMED 6,018,334 (30M의 20.1%) / MINIMUM 6,109,256 / 스토리지 ~17.6TB / 수집완료 525,570
- **최종 보고서**: `docs/deliverables/크롤러_케파_및_스토리지_산정보고_20260802.md` (최종본)
- **test.xlsx 대조**: `docs/deliverables/test_수집현황반영_20260802.xlsx` (수집률 91.4%), `의심사이트_업체확인_20260802.xlsx`
- **신규 크롤러 15개** 제작+수집 완료(스웨덴/이스라엘/호주6/뉴질2/이탈4). iris(10,521)·flore(14,038) 메타완료·PDF는 Cloudflare차단. justice.govt.nz/aihw/finance/mur 등 차단보류.
- 재집계: `python3 scripts/audit/consolidate_capacity.py` + `build_suspect_report.py` (언제든 재실행 가능). 남은 백그라운드 프로세스 없음.

---


## 목표 (사용자 확정)
각 크롤러가 **최대 몇 건 수집해야 하는지(=소스 총량)** 를 CSV에 확정하고,
그 합이 사람이 예상한 **~3000만 건**에 맞는지 검증. (스토리지 산정 + 크롤러 수집목표 설정)

## 계획 = A
1. **지금 전수조사(full_survey) 완주 대기** (롱테일 + 라벨 확보)
2. **대형/capped/failed 사이트 타깃 정밀 재측정**: `--retry-failed` + **API 총량 우선** + 상한 대폭↑(사실상 무제한)
   - ⚠️ 핵심: 3000만의 대부분은 대형 저장소(HAL/DSpace/OAI/CKAN)에 있는데, 지금 20분 상한(1200s)에 걸려 `site_timeout` 하한값으로만 기록됨 → 대형은 **총량 API(numFound/totalElements/completeListSize/count) 1회 호출**로 정확·즉시 측정해야 함 (페이징 아님)
3. **finalize CSV에 크롤러별 컬럼 확정**: `site_id, sheet, collected(현재수집), max_to_collect(소스총량=수집목표), method, exact_or_lowerbound, gap`
   - `max_to_collect` 합계 = 검증 총량 → 3000만과 대조

## 현재 실행 상태 (스냅샷 시점 done=202/789)
- survey: `python3 scripts/audit/full_survey.py --resume --workers 6 --site-timeout 1200` (PID 715392, ALIVE)
- watchdog: `bash scripts/audit/survey_watchdog.sh` (PID 715348, ALIVE) — PID파일 방식, 죽으면 auto-resume, 정체(2900s)감지 kill, 789 완료시 종료
- 이 프로세스들은 **detached OS 프로세스라 대화 compact와 무관하게 계속 돎**

## 파일 경로
- 하네스: `scripts/audit/full_survey.py` (stealth주입/resume/저장0/캡완화/관측성)
- 워치독: `scripts/audit/survey_watchdog.sh`
- 결과 CSV(append, resume): `scripts/audit/full_survey_totals.csv`
  - 컬럼: site_id, sheet, collected, counted, completed, elapsed_s, method, outcome, note
  - outcome: ok / zero_parsed / fetch_fail / site_timeout / no_crawler / error
- 상태 JSON(실시간): `scripts/audit/out/full_survey_status.json`
- 하트비트: `scripts/audit/out/full_survey_heartbeat.log`
- 워치독 로그: `scripts/audit/out/watchdog.log`
- PID파일: `scripts/audit/out/full_survey.pid`, `scripts/audit/out/watchdog.pid`
- finalize(개선 필요): `scripts/audit/finalize_capacity_health.py` (현재 count_only 기준 → full_survey 컬럼+max_to_collect로 조정 필요)

## 진행 확인 명령
```
source .venv/bin/activate
tail -n +2 scripts/audit/full_survey_totals.csv | cut -d, -f1 | sort -u | wc -l   # done/789
tail -1 scripts/audit/out/full_survey_heartbeat.log                               # rate/eta/inflight
kill -0 $(cat scripts/audit/out/full_survey.pid) && echo alive                    # survey 생존
kill -0 $(cat scripts/audit/out/watchdog.pid) && echo wd-alive                    # 워치독 생존
```

## 완료 감지
워치독(715348)이 종료되면 전수조사 완료. 완료 후:
1. `--retry-failed` 대형 정밀 재측정 (API총량+무상한) — 2단계
2. finalize에 max_to_collect 컬럼 넣어 최종 CSV + 3000만 대조

## 야간 자율진행 로그 (2026-08-01 밤, 사용자 취침)
사용자 지시: "e-stat 값이랑 대형들 검증 진행해줘, 난 자러 갈테니 쭉 진행해줘"
- **HAL 교정 완료**: `scripts/audit/remeasure_hal.py` → `hal_totals.csv` (28 ok, 합 539,919). 전역버그(460만) 해결, 포털별 필터복제(submitType_s=file/docType_s/collCode). 1개 실패: artsetmetiers(포털 alias가 HTML 반환).
- **통합 스크립트 완성**: `scripts/audit/consolidate_capacity.py` → `capacity_final.csv` + `capacity_final.md`. max_to_collect 우선순위: big_totals(예정)>hal_totals>coverage(hal제외)>custom>full_survey(ok<500 exact / ok>=500·site_timeout=하한 / 기타=미측정). PDF율·평균크기로 projected_pdf_count·projected_storage_gb 산출.
- **부분 결과(서베이 진행중)**: 확정 4,828,118 / 최소 4,856,649 / 스토리지 확정≈9.75TB. 미측정 472.
- **e-stat 검증 결과**: 1,709,118은 라이브 페이지에 `件`로 실재(진짜). 단 **통계표(Excel/CSV) 레코드**로 이미 수집분 PDF율 8%·평균0.63MB → 스토리지 기여 작음. "30M 문서"에 데이터셋/통계표 포함 여부는 사용자 판단 필요.
- **진행중**: `remeasure_big.py`(executor aa653bb9) 대형/미측정 사이트 API총량 재측정(CKAN/DSpace/OAI/EPrints/Invenio/WP), e-stat·gov-scot·gov-uk html_regex 재검증. 완료시 consolidate에 big_api 최우선소스로 편입 후 재실행.
- **다음(자율)**: ① big_totals 완료 → consolidate 재실행 ② survey 완주 대기 → consolidate 재실행 ③ 아침 최종보고서 파일화(docs/deliverables/). 30M은 현재 추세상 논문PDF로는 크게 미달, 데이터셋/통계표 포함시에만 근접 가능 — 세 층위(총레코드/전체PDF/논문PDF)로 분리 보고 예정.

## 야간 진행 갱신 #2 (2026-08-02 새벽)
- **대형 재측정 2차(집중) 완료**: etera-ee **210,234**(커스텀 JSON API), sonar-ch **309,727**(Invenio), naturalis 17,309, tno 13,609/69,032, par-nsf 5,134, doras 4,902, ga-gov 31,979 확보. noaa(WAF)·eprints-imtlucca(호스트死) 실패. → `big_totals_retry.csv`, big_totals.csv 병합됨.
- **현재 헤드라인**: 확정 6,003,305 (30M의 20%) / 최소 6,030,861 / 스토리지 ~17.6TB (ntrs 4.9TB 최대). exact 282·lower 61·미측정 446.
- **잠정 최종보고서 작성**: `docs/deliverables/크롤러_케파_및_스토리지_산정보고_20260802.md` (3층위 프레이밍+스토리지+30M대조). survey 완주 시 숫자만 갱신하면 됨.
- **결론 확정**: 789개 크롤러 authoritative 상한 ~6M(완주시 ~6.5-7M), 3000만은 데이터셋/통계표 하위레코드 포함하거나 소스확장 필요. 스토리지 실구매 기준 ~20TB급.
- **남은 것**: survey(~260/789) 완주 대기 → consolidate 재실행 → 보고서 수치 갱신. 그 외 측정작업 불필요(짜낼만큼 짜냄).

## 야간 진행 갱신 #3 (2026-08-02 오전) — test.xlsx URL 대조 + 신규 크롤러
- **test.xlsx 1,995 URL 수집대조**: `docs/deliverables/test_URL_수집대조_20260802.xlsx` (3시트: 전체/미수집_내용확인/시트명별요약). 수집됨 1,645(82.5%), 미수집 350. 미수집 라이브 프로빙(`scripts/audit/probe_uncollected.py`→`uncollected_probe.csv`): 크롤가능 194·차단102·깨짐/죽음 여러. 매칭기 결과 pkl: /home/ruci/.claude/jobs/c789169f/tmp/test_url_match.pkl.
- **신규 크롤러 2개 제작+소량검증 완료** (전량수집은 미실행):
  - `crawler/sites/custom/government-se-publications.py` (스웨덴 50섹션, Cloudflare→Stealth, 테스트 8/8 PDF). 러너 `scripts/collect_government_se.py`.
  - `crawler/sites/custom/gov-il-collectors.py` (이스라엘 23부처×2 API. API: openapi-gc.digital.gov.il, x-client-id 헤더, blobfolder 소문자 우회. 테스트 13행/6PDF). 러너 `scripts/collect_gov_il.py`.
- **다음**: 전수조사 완주 후 두 러너로 전량수집 → capacity/대조 재생성. 미수집 나머지(호주·뉴질랜드·이탈리아·핀란드 등)도 동일 방식으로 크롤러 제작 가능.

## 이력/주의
- 1회차(count_only, 6분·10병렬·stealth無) = 완주했으나 봇차단·캡으로 **데이터 오염** → 폐기, 2회차로 재실행
- 2회차 초기 워치독 self-match 버그 → PID방식으로 수정 완료
- 문서당 평균 3.83MB. 현재 여유 3.6TB, gdrive 1PB(`/data_raid/share/gdrive`)
- 러프 스토리지 견적: 하한 ~22TB, 3000만이면 ~115TB
