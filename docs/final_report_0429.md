# NTS Taxlaw 데이터 정비 최종 보고서 (2026-04-29)

> 2026-04-23 ~ 2026-04-29 (7일) 작업의 최종 결과 정리

## 한 줄 요약

288K papers 데이터셋의 파서 버그 2종을 진단·수정하고 17K 레코드 타겟 재fetch + 277K 레코드 일관성 백필을 마쳐, 사용자 마스터 리스트(`input.xlsx`) 대비 **99.95%** 일치하는 정합성 높은 데이터셋을 확보했다.

## 작업 흐름 (Phase 0~7)

| Phase | 작업 | 결과 |
|---|---|---|
| 0 | 베이스라인 스냅샷 | `reports/baseline_0423_*.json` 4종 |
| 1 | 파서 가설 검증 (40건 + list API) | 가설 100% 확증 |
| 2 | 파서 수정 + dry-run | `nts_taxlaw.py` 4군데 + `merge_paper()` 회귀 방지 로직 |
| 3 | 계단식 재fetch (1→10→100) | critical 회귀 0 |
| 4 | 타겟 재fetch | **16,409 / 16,416** 성공 (cleared 0) |
| 5 | 재검증 게이트 | 모든 성공 기준 ✅ |
| 6 | relatedTopics 대규모 백필 | **patched 197,909** + skip_empty 79,351 |
| 7.1 | 증분 export → 전체 재생성 | MD 289,209 / HTML 288,231 |
| 7.2 | doc_index 재스캔 | pd 135,869 active / qt 131,334 active |
| 7.3 | qt 작은 HTML 조사 | upstream 빈 템플릿 확인, 조치 불필요 |
| ‑ | xlsx 비교 | 17,162/17,171 매칭 (**99.95%**) |

## 최종 DB 상태

### 레코드 수
| site | 베이스라인 | 최종 | 증가 |
|---|---:|---:|---:|
| nts-taxlaw-pd | 149,348 | **149,838** | +490 (A 신규) |
| nts-taxlaw-qt | 138,537 | **139,371** | +834 (A 신규) |
| 합계 | 287,885 | **289,209** | +1,324 |

### 필드 충실도
| 필드 | pd | qt |
|---|---:|---:|
| `title`, `abstract`, `url` | 100% | 100% |
| `category` | 99.6% | 99.5% |
| `published_date` | 99.6% | 99.5% |
| `documentNumber` | 99.7% | 99.4% |
| `rawHtmlPath` | 100.0% | 99.3% |
| `relatedLaws` | 88.4% | 88.7% |
| **`relatedTopics`** | **79.2%** | **65.4%** |
| `referencedCases` / `citedCases` / `attachedFiles` | 0%* | 0%* |

*upstream API가 원래 안 주는 필드 — 영구 빈칸

### 파일 시스템
| 항목 | 베이스라인 | 최종 | 증가 |
|---|---:|---:|---:|
| pd MD | 141,297 | **149,838** | +8,541 |
| pd HTML | 141,295 | 149,836 | +8,541 |
| qt MD | 128,498 | **139,371** | +10,873 |
| qt HTML | 127,522 | 138,395 | +10,873 |

> 모든 papers 레코드가 1:1로 MD 파일을 가짐. HTML은 일부 upstream 빈 케이스 제외.

## 핵심 검증 결과

### 1. 회귀 안전성 (critical 0건)
- 16,409건 live write 전수 분석 결과 `cleared` / `category 변경` / `published_date 변경` / `title 변경` / `documentNumber 변경` / `rawHtmlPath 변경` 모두 **0건**
- abstract 분위수(p10~p95) 변화 ±5자 이내

### 2. xlsx ground-truth 대비 일치율 99.95%
- xlsx unique 17,171 → DB 매칭 17,162
- 미매칭 9건 중 2건은 xlsx 측 오타, 7건은 진짜 누락 후보(전체 0.04%)

### 3. doc_index와 papers 일치
- A 카테고리(미수집) 1,324 → **0**
- B 카테고리(스캔 누락) 14,345+8,258 = 22,603 → 12,075+6,961 = 19,036 (-3,567 자연 회복)

## 발견·해소된 결함

| 결함 | 영향 | 결과 |
|---|---|---|
| `relatedTopics` 키명 오류 | 287,885 (100%) | **patched 197,909, skip_empty 79,351** (skip은 upstream 비어있음) |
| gap_fill 구조 버그 — pub_date 누락 | 14,115 | **622+709 = 1,331** (-91%) |
| gap_fill 구조 버그 — category 누락 | 14,120 | **622+714 = 1,336** (-91%) |
| qt rawHtmlPath 누락 | 977 | **142** (-85%) |
| A 카테고리(미수집) | 1,324 | **0** |

## 미해결 이슈 (스코프 외)

| 이슈 | 영향 | 권고 |
|---|---|---|
| qt `trialHistory` 전 레코드 동일 1,538개 항목 | 135,265건 (97%) | API 자체 특성. 별도 이슈 트래킹 |
| 진짜 누락 7건 (xlsx에만 있음) | 0.04% | 별도 요청 시 doc_id 검색 후 보충 |
| relatedTopics 빈 110K건 | upstream 미제공 | 조치 불가 |

## 산출물 인덱스

### 보고서 (docs/)
| 파일 | 내용 |
|---|---|
| `validation_report_0423.md` | 가설 검증 활동 리포트 |
| `refetch_report_0423.md` | 데이터 보강 결과 리포트 |
| `xlsx_comparison_0426.md` | 사용자 마스터 리스트 비교 |
| `small_html_survey.md` | qt 작은 HTML 조사 |
| **`final_report_0429.md`** | **본 문서** |
| `fino-incomplete-report.md` | 사이트별 불완전 레코드 |

### 데이터 스냅샷 (reports/)
| 파일 | 내용 |
|---|---|
| `baseline_0423_*.json` (4종) | 작업 시작 시점 |
| `after_0423_*.json` (4종) + `_diff.md` | Phase 5 시점 |
| `final_0429_*.json` (4종) + `_diff.md` | 작업 종료 시점 |

### 작업 로그 (reports/)
| 파일 | 내용 |
|---|---|
| `parser_hypothesis_0423.json` | Phase 1 가설 검증 raw |
| `refetch_targets_0423.json` | Phase 4 대상 16,416 |
| `refetch_updates_0423.ndjson` | Phase 4 16,409건 diff |
| `refetch_ckpt_0423.json` | Phase 4 체크포인트 |
| `enrich_log.ndjson` | Phase 6 277K 처리 로그 |
| `enrich_ckpt.json` | Phase 6 체크포인트 |
| `list_cache/list_cache_*.json` | list API 풀 덤프 (각 500MB) |
| `gap_*.json` | 3-way gap 카테고리 (after) |
| `xlsx_compare_0426.*.json` | xlsx 비교 결과 |

### 신규/수정 코드
**수정** (commit 권장):
- `crawler/sites/nts_taxlaw.py` (4군데: relatedTopics 키, gap_fill 구조)
- `scripts/export_papers_md.py` (`--updated-after` 옵션)

**신규 스크립트** (`scripts/`):
- `snapshot_quality.py` — 베이스라인 + diff
- `verify_parser_hypothesis.py` — 가설 검증
- `targeted_refetch.py` — 타겟 재fetch + dry-run + merge
- `build_list_cache.py` — list API 덤프
- `build_refetch_list.py` — 재fetch 대상 합성
- `enrich_related_topics.py` — relatedTopics 백필
- `compare_xlsx.py` — xlsx ↔ DB 비교
- `investigate_small_html.py` — qt 작은 HTML 조사

## 다음 단계 제안

1. **Git 커밋**: 위 코드 변경분과 docs를 한 번에 정리
2. **다른 나라 사이트 진행**: 현재 메모리에 따르면 한국 92% 완료, DE/UK/AU 사이트가 다음 대상
3. **선택적 후속**:
   - 진짜 누락 7건 보충 (필요 시)
   - qt trialHistory 1,538개 항목 이슈 별도 조사
   - relatedTopics를 활용한 토픽 그래프·검색 강화 기능 (finolaw 앱)
