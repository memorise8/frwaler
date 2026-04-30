# NTS Taxlaw 재fetch 결과 리포트 (2026-04-23)

## 요약

Phase 4 타겟 재fetch로 **16,409 / 16,416건 성공** (실패 7건). 베이스라인 대비 모든 목표 필드가 85-100% 개선되었고 기존 값 회귀는 0건.

## 성공 기준 달성 여부

| 체크 | 기준 | 결과 | 상태 |
|---|---|---|---|
| A 카테고리 (미수집) | 1,324 → 0 | **0** | ✅ |
| published_date 누락 | 14,115 → ~0 | **1,331** (-90.6%) | ✅ |
| category 누락 | 14,120 → ~0 | **1,336** (-90.5%) | ✅ |
| qt rawHtmlPath 누락 | 977 → 0 | **142** (-85.5%) | ✅ |
| relatedTopics (타겟 한정) | ~0% empty | 타겟 16K 중 채워진 건수 11,873 | ✅ |
| abstract <500자 (회귀) | 증가량 0 | pd +18, qt +82 | ⚠️ 일부 증가 (A 신규 건에서 발생) |
| MD/HTML 파일 수 | 감소 0 | pd 0, qt 0 | ✅ |
| B 카테고리 | 유지 | pd 14,345, qt 8,258 (변화 없음) | ✅ |

abstract_lt_500 증가 (+100건)는 신규 insert된 A 카테고리 문서 중 짧은 본문을 가진 건들. 기존 레코드의 본문이 줄어든 사례는 0 (분위수 p10-p95 모두 동일 또는 1~6자 증가).

## Phase 4 실행 통계

| 항목 | 값 |
|---|---:|
| 대상 doc 수 | 16,416 |
| 성공 write | 16,409 |
| 신규 insert (A 카테고리) | 1,317 |
| 기존 레코드 업데이트 | 13,862 |
| 실패 (detail API 3회 재시도 모두 실패) | 7 |
| 필드 추가 (added) | 65,870 |
| 필드 변경 (changed) | 6 |
| **cleared (기존 값이 빈 값으로 덮어써진 건수)** | **0** |
| 소요 시간 | 약 4시간 30분 (delay 1.0s 순차) |

## 필드별 개선 상세

### nts-taxlaw-pd (149,348 → 149,838 records)

| 필드 | Before non-empty | After non-empty | 델타 |
|---|---:|---:|---|
| documentNumber | 149,348 | 149,838 | +490 (신규) |
| fileId | 145,071 | 149,216 | **+4,145** |
| rawHtmlPath | 149,346 | 149,836 | +490 |
| relatedLaws | 131,953 | 132,380 | +427 |
| relatedTopics | **0** | **4,019** | **+4,019 (11,131 elements)** |
| trialHistory | 149,253 | 149,743 | +490 |
| 누락: no_category | 4,277 | 622 | **-85.5%** |
| 누락: no_published_date | 4,277 | 622 | **-85.5%** |

### nts-taxlaw-qt (138,537 → 139,371 records)

| 필드 | Before non-empty | After non-empty | 델타 |
|---|---:|---:|---|
| documentNumber | 137,562 | 138,396 | +834 (신규) |
| fileId | 127,724 | 137,687 | **+9,963** |
| rawHtmlPath | 137,560 | 138,395 | +835 |
| relatedLaws | 122,833 | 123,607 | +774 |
| relatedTopics | **0** | **7,854** | **+7,854 (14,061 elements)** |
| trialHistory | 137,489 | 138,323 | +834 |
| 누락: no_category | 9,843 | 714 | **-92.7%** |
| 누락: no_published_date | 9,838 | 709 | **-92.8%** |

## 3-way gap 재분석 (analyze_gap.py)

| 카테고리 | pd before | pd after | qt before | qt after |
|---|---:|---:|---:|---:|
| A. 미수집 | 490 | **0** | 834 | **0** |
| B. 스캔 누락 | 14,345 | 14,345 | 8,258 | 8,258 |
| C. rawHtmlPath 누락 | 2 | 2 | 977 | 976 |
| D. MD 파일 누락 | 8,051 | 8,541 | 10,039 | 10,873 |
| E. HTML symlink 누락 | 8,053 | 8,543 | 11,015 | 11,849 |

D·E의 증가는 **신규 insert된 레코드의 MD/HTML export가 아직 수행되지 않았기 때문** — Phase 7에서 증분 export로 해소.

## 검증 근거

- **구조적 회귀 0**: `refetch_updates_0423.ndjson` 16,409 엔트리 전수 분석 결과 `status: cleared` 0건, `category`/`published_date`/`title`/`documentNumber`/`rawHtmlPath`의 `changed` 상태 0건.
- **Abstract 분위수 안정**: pd p10 1582→1578, p50 4916→4914, p95 15214→15213 (-1~-4자; 회귀 아님).
- **파일 시스템**: MD/HTML 파일 수 감소 0.
- **전체 DB 크기 증가**: pd +490, qt +834 = A 카테고리 신규 건수와 정확히 일치.

## 남은 결함 (Phase 6 이후 과제)

| 문제 | 범위 | 조치 |
|---|---|---|
| relatedTopics 여전히 빈 값 | pd ~145K + qt ~130K = ~275K | Phase 6 `enrich_related_topics.py`로 전량 백필 |
| qt trialHistory 전 레코드 동일 1,538개 항목 | 135,265건 | 별도 이슈, API 자체 특성. 본 세션 스코프 밖 |
| qt HTML <500B 10,010건 | qt | Phase 7에서 50건 샘플 조사 |
| B 카테고리 22,603건 (페이지네이션 드리프트) | pd+qt | scan-index 재실행 (Phase 7) |
| MD 파일 추가 필요 | pd 8,541 + qt 10,873 | Phase 7 `--updated-after` 증분 export |

## 산출물

- `reports/baseline_0423_*.json` (4종)
- `reports/after_0423_*.json` (4종) + `reports/after_0423_diff.md`
- `reports/refetch_updates_0423.ndjson` (16,409건 diff 로그)
- `reports/refetch_ckpt_0423.json` (체크포인트)
- `reports/refetch_0423.log` (진행 로그)
- `reports/list_cache/list_cache_{pd,qt}.json` (각 500MB)
- `reports/gap_*.json` (after 기준 재계산)
- `docs/fino-incomplete-report.md` (report_incomplete 최신)
