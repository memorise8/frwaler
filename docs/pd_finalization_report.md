# 판례(nts-taxlaw-pd) 마무리 리포트

작성: 2026-05-04 (md 메타데이터 비교 후속 정밀 검증)

전제: NTS taxlaw 정비 7일 작업(commit `8a7f9f7`) 완료 후, 사용자 제공 `세법MD_20260417_metadata.json`(471,619 records)으로 종합 비교를 수행하고, 그 과정에서 발견된 잔여 이슈 5종(A~E)을 정리한 결과.

---

## 0. 요약

| 항목 | 시작 | 종료 |
|---|---:|---:|
| nts-taxlaw-pd | 149,838 | **149,909** (+71) |
| nts-taxlaw-qt | 139,371 | **139,385** (+14) |
| 합계 | 289,209 | **289,344** (+85) |
| pd category empty | ? | **4** (618건 패치 완료) |
| pd published_date empty | ? | **4** |
| FTS5 인덱스 | 100% | **100%** |

**결론: 판례 데이터 정리 작업 종결.** 잔여 이슈는 모두 NTS API 한계(referencedCases 등) 또는 외부 데이터 노이즈(폴더 분류 오류)로, DB 측 추가 조치는 불필요.

---

## 1. 비교 결과 핵심 지표 (md metadata vs DB)

| 비교 | 일치율 |
|---|---|
| 문서번호 | MD 99.92%, DB 98.78% |
| 제목 | 99.79% (exact 213,130 + whitespace 71,523 / 285,239) |
| 생산일자 | 99.95% |
| folder2 ↔ DB doctype | 99.5% (disagree는 DB가 ground truth) |

상세 리포트: `docs/md_metadata_comparison_report.md`

---

## 2. A — 카테고리 disagree 정밀 분석

`folder2 ↔ DB documentTypeName` disagree 14쌍, 총 1,481건. **DB가 ground truth로 확인.**

### 가장 큰 disagree (DB가 정확)

| folder2 | DB doctype | 건수 | prefix 분포 |
|---|---|---:|---|
| 8. 심판청구 | 심사 | 1,156 | `심사법인` 972, `심사` 66, `심사소득` 39, `심사기타` 35... |
| 2. 질의회신 | 사전 | 205 | `사전-` 191, `법규재산` 6... |
| 9. 판례 | 질의 | 87 | `재산-` 82, `부가46015-` 4... |

documentNumber prefix가 DB doctype과 일치 → **폴더 작성자가 잘못 분류.** DB 보정 불필요.

산출물: `reports/disagree_largest.json`, `reports/disagree_all.json`

---

## 3. B — pd 데이터 완성도 점검

총 149,909건 (B 단계 패치 후).

### 양호 (≥99.5%)

| 필드 | empty | % |
|---|---:|---:|
| title, abstract, url, FTS5 | 0 | 0.00% |
| metadata.documentNumber | 0 | 0.00% |
| metadata.documentTypeName | 0 | 0.00% |
| metadata.sourceOrgCode | 0 | 0.00% |
| metadata.firstRegDtm/lastAltDtm | 95 | 0.06% |
| metadata.trialHistory | 95 | 0.06% |
| **published_date / category** | **4** | **0.003%** (B-fix 후) |

### 알려진 잔여 (NTS API 미제공 — 정상)

| 필드 | empty | 비고 |
|---|---:|---|
| metadata.replyReference | 100% | 판례에 안 쓰는 필드 |
| metadata.referencedCases | 100% | NTS detail API 미제공 |
| metadata.citedCases | 100% | NTS detail API 미제공 |
| metadata.attachedFiles | 100% | NTS detail API 미제공 |

### 부분 결손 (NTS가 일부만 제공)

| 필드 | empty | % |
|---|---:|---:|
| metadata.relatedLaws | 17,479 | 11.66% |
| metadata.relatedTopics | 31,255 | 20.85% |
| metadata.attrYr | 4,794 | 3.20% |

`relatedTopics`는 직전 7일 작업에서 197,909건 백필 완료. 남은 31K는 upstream API가 안 주는 케이스.

### B-fix 작업 결과

발견 시 622건이 `published_date`/`category` 비어있던 것을, `lookup_by_doc_number` API로 **618건 보충 (99.4%)**. 4건은 NTS도 lookup 실패.

### 추가 발견 (별도 이슈)

1. **중복 documentNumber 10건+** — 헌재 케이스 (`98-헌바-9-`, `2004-헌바-7-` 등) 4번 중복.
   - 원인: NTS 원본은 `98-헌바-9-1`, `98-헌바-9-2`처럼 끝자리 식별자로 구분. 우리 normalise가 trailing `-` 제거 시 합쳐버린 것.
   - 영향: 같은 dn으로 4건 별개 결정문이 저장되어 있어 데이터 손실은 없음. 단지 JOIN/검색 시 모호.
   - 처리: 별도 이슈로 기록. 다음 정비 사이클에 normalise 정책 재검토.

2. **pd 사이트의 `documentTypeName='질의'` 1건** (`전주부-2006-누-571`).
   - NTS 원본 분류 문제. 영향 미미.

---

## 4. C — DB only 930건 (판례) 분석

DB에는 있고 md 폴더엔 없는 판례 930건의 `published_date` 분포:

```
2009:    6  ██
2010:    3  █
2011:    1  █
2012:    7  ██
2013:    6  ██
2014:    5  ██
2015:   18  ████
2016:    4  █
2017:    2  █
2018:    1  █
2020:    1  █
2022:    5  ██
2023:   42  ██████████
2024:  113  ████████████████████████
2025:  469  ████████████████████████████████████████████████
2026:  247  ███████████████████████████████████████████████████
```

**930건 모두 published_date < 2026-04-17** (md 스냅샷 일자).
- `crawled_at` 분포: 100% 2026년에 우리가 수집.
- 즉 **md 폴더가 일부를 빠뜨린 것** (2009~2026 전 기간). DB가 더 풍부.

산출물: `reports/db_only_pd_analysis.json`

---

## 5. D — xlsx 누락 7건 종결

이전 검증(xlsx 99.95%)에서 남은 7건의 현재 상태:

| documentNumber | 상태 |
|---|---|
| `대법원-2025-두-34754` | ✓ 추가 (md_only fetch에서) |
| `서면인터넷방문상담5팀-501` | ✓ 추가 (md_only fetch에서) |
| `서울고등법원-2011-누-29290` | ✓ 추가 (md_only fetch에서) |
| `서울고등법원-2011-누-42187` | ✓ 추가 (md_only fetch에서) |
| `서울고등법원-2013-누-14056` | ✓ 추가 (md_only fetch에서) |
| `법인세과-4278` | ✗ NTS lookup NOT FOUND |
| `서울고등법원-2024-누-71253` | ✗ NTS lookup NOT FOUND |

→ **5/7 자동 해결** (md_only 85건 추가 작업의 부산물).
→ **2건은 NTS에서 사라진 것으로 추정** (수집 불가).

---

## 6. E — Unavailable 59건 (NTS에서 사라진 문서)

md 폴더엔 있고 DB+NTS 모두 없는 진짜 누락. 폴더 분포:

| folder2 | 건수 |
|---|---:|
| 2. 질의회신 | 34 |
| 1. 사전답변 | 12 |
| 7. 심사청구 | 5 |
| 8. 심판청구 | 4 |
| 3. 과세기준자문 | 3 |
| 9. 판례 | 3 |
| 6. 이의신청 | 1 |

해석: md 폴더 작성 시점 이후 NTS가 삭제/비공개 처리한 것으로 추정. **NTS API로 수집 불가**.

### 처리 결정

- **본문 보존은 본 마무리 작업 범위 외**로 분리. 필요 시 별도 액션:
  1. 192.168.0.4 mount 또는 사용자가 본문 추출
  2. DB에 별도 컬럼/테이블로 저장 (예: `external_archive` 테이블)
  3. md 폴더의 본문 그대로를 `abstract`에 넣고 `crawled_at` 메모

- 현 시점에는 누락 리스트만 보존 (`reports/md_only_recovery.json` unavailable 항목).

---

## 7. 산출물 정리

| 파일 | 내용 |
|---|---|
| `docs/md_metadata_comparison_report.md` | metadata vs DB 종합 비교 |
| `docs/md_only_recovery.md` | md only 188건 수집 가능성 |
| `docs/pd_finalization_report.md` | (이 문서) 마무리 리포트 |
| `reports/md_compare.{summary,md_only,db_only}.json` | 1차 파일명 비교 결과 |
| `reports/md_metadata.{summary,md_only,db_only,title_diff,date_diff}.json` | 2차 metadata 비교 결과 |
| `reports/md_only_recovery.json` | 188건 NTS lookup 결과 (recoverable 85 / unavailable 59) |
| `reports/refetch_md_only_targets.json` | 85건 fetch 입력 |
| `reports/refetch_md_only_writes.ndjson` | 85건 변경 audit 로그 |
| `reports/refetch_md_only_ckpt.json` | checkpoint |
| `reports/disagree_largest.json` | 1,156건 disagree 상세 |
| `reports/disagree_all.json` | 14쌍 disagree 요약 |
| `reports/db_only_pd_analysis.json` | DB only 판례 930건 날짜 분포 |
| `scripts/compare_md.py` | 1차 비교 도구 (파일명 기반) |
| `scripts/compare_md_metadata.py` | 2차 비교 도구 (metadata 기반) |

---

## 8. 알려진 잔여 이슈 (다음 사이클로 이월)

| 이슈 | 영향 | 우선순위 |
|---|---|---|
| 헌재 trailing `-` normalise로 인한 4건 dn 합치기 | 검색 시 모호 | 낮음 |
| pd `질의` 1건 (`전주부-2006-누-571`) | 검색 분류 미세 영향 | 매우 낮음 |
| qt trialHistory 1,538개 동일 항목 | qt 별도 이슈 (이미 알려짐) | 중간 (qt 관련) |
| Unavailable 59건 본문 보존 결정 | 사용자 input 필요 | 낮음 |
| 폴더 분류 오류 1,400+ 건 사용자 피드백 | 외부 (md 작성자에게) | 정보용 |

---

## 9. 다음 단계 후보 (판례 외)

판례 마무리됨. 다음 세션에서 이어갈 후보:
1. qt trialHistory 1,538개 이슈 별도 조사
2. SQLite 뷰어 (요청 B) 진행
3. 다른 나라 사이트 (DE/UK/AU) Smart Finder
4. relatedTopics 활용 기능 (토픽 그래프, 검색 강화)
5. git commit (현 세션 변경: 신규 스크립트 + 리포트 + DB 추가)
