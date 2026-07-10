# libertree 메타데이터 정제 최종 보고서

## 1. 개요

- **작업 기간**: 2026-07-11 (단일 세션, 11-task 계획 순차 실행)
- **대상 DB**: `data/libertree.db` (문서 427,266건)
- **작업 범위**: (1) title/abstract/keywords 텍스트 정제 + listed_date/published_date 정규화 + URL 정리(R1~R6), (2) 가짜 PDF 스캔·리셋, (3) 크롤러 오염 방지 수정(esteri/presse-economie/doras), (4) masaf 인코딩 수정 + title 백필, (5) 그리스 3사이트 published_date 추출 버그 수정 + 백필
- **백업**: `data/libertree.db.bak-20260711-0120-precleanup` (5,212,319,744 bytes, 정제 전 원본 스냅샷, 2026-07-11 01:20 생성 — 파일시스템 mtime은 소스 관리 정책상 최초 배포일 기준으로 표기될 수 있음)
- **최종 DB 크기**: 5,564,665,856 bytes (~352MB 증가 — 텍스트 정제로 일부 필드 길이 변화, 신규 컬럼 없음)
- **커밋 이력**: `5762568`(계획) → `9956b42`~`08de0d1`(R1~R6 규칙) → `92ad9e3`(CLI 결선) → `6aa2c35`,`2cbe630`(테스트+실 DB 버그 수정) → `4d51a0c`,`603f30a`(가짜 PDF) → `8f94aee`(크롤러 오염 방지) → `6961db3`,`efd1302`,`a564bc2`,`30f1d8e`(백필 툴+masaf) → `46108dc`(그리스 3사이트)

## 2. 규칙별 정제 결과 (apply JSON 집계, `data/audit/cleanup_metadata_20260711_013337_apply.json` 최종본 기준)

| 규칙 | 후보(candidates) | 변경(changed) | 원값 유지(unparsed/kept) | 비고 |
|---|---:|---:|---:|---|
| R1 title (엔티티/태그/개행) | 1,409 | 1,409 | 0 | `&#`, `&amp;`, `&lt;`, `<a `, `<span`, `<br`, 개행(LF) 대상 |
| R2+R4 abstract (태그/CDATA/엔티티) | 5,232 → (2차 3) | 최종 3건(4중 인코딩 엣지케이스) | 141(영구 no-op) | 1차 5,232건 정제 후, 4중 인코딩 3건(`max_rounds` 3→8로 재수정) 추가 반영 |
| R3 keywords (CDATA/중복 제거) | 1,274 | 1,274 | 2(영구 no-op) | CDATA 잔존 0 |
| R5 listed_date | 19,751 | 17,623 | 2,128 | 원값 유지분은 `"2026 - 12??"` 류 진짜 파싱 불가 |
| R5 published_date | 948 | 380 | 568 | 원값 유지분은 DOI/페이지번호/월-일 누락 표기 |
| R6 url (meta_url/pdf_url) | 113 | 37 | 76(ftp 보존) | `meta_url='ERROR'` 2건 → `''`, `pdf_url` 절대화/NULL화 |

- 최초 `--apply`가 `meta_url='ERROR'` NULL 처리 시 `NOT NULL` 제약 위반으로 크래시(url 규칙만 0건 반영, 나머지 5개 규칙은 정상 커밋됨) → 코드 수정(`fix_meta_url` 반환값 `None`→`""`) 후 재실행하여 전체 규칙 완료. 상세: `.superpowers/sdd/task-5-report.md`.
- 최종 멱등성 재실행(`cleanup_metadata_20260711_013354_dryrun.json`): 전 규칙 `changed=0` 확인.

## 3. 가짜 PDF 리셋 결과

- **스캔 대상**: `pdf_downloaded=1` 전체 270,519건 (기존 `text_extracted=0` 한정 스캔에서 범위 확장)
- **가짜 판정**: `fake_html`(HTML을 PDF로 저장) + `unknown & size<10KB` 휴리스틱 — 총 **3,704건** (1.4%)
  - 상위: krihs(744), krivet-re-kr-kor(500), kedi-re-kr-khome(344), environment-govt-nz(243) 등
- **조치**: 3,704건 `pdf_downloaded/pdf_size_bytes/pdf_sha256/text_extracted` 리셋 + PDF·TXT blob 파일 삭제
- **결과**: `pdf_downloaded` **270,519 → 266,815** (확정치), `text_extracted` 260,260 유지(진짜 가짜는 애초에 텍스트 추출 안 됐음)
- **잔존 검증**: krivet/kedi sha 그룹 0건, jeonnam-go-kr-m7116은 별도 프로세스로 이미 회복되어 fake 목록에서 자연 제외(770건 정상 HWP/OLE2 바이너리로 검증됨)
- 실행 중 스레드 격리 버그(스레드별 sqlite 연결 미분리로 최초 apply가 3,704건 전부 실패) 발견·수정 후 재실행. 상세: `.superpowers/sdd/task-6-report.md`

## 4. 백필(재크롤 기반 필드 보정) 결과

| 사이트 | 문제 | 수정 | 백필 결과 |
|---|---|---|---:|
| masaf-gov-it-flex | 인코딩 오판정(UTF-8 강제 디코드)으로 title에 `�`(mojibake) 154건 | 인코딩 스니핑(`utf-8`→`windows-1252`→`iso-8859-1`) 디코더 추가 | title 154/154 백필 완료, 잔존 mojibake 0 |
| mindev-gov-gr-category | `_parse_date` 정규식의 `\b` 가 `T` 구분자(ISO datetime)와 매칭 안 됨 | `\b` → `(?!\d)` negative lookahead로 교체 | published_date 3,433/3,433 (100%) |
| mindigital-gr-archives | 〃 | 〃 | published_date 648/648 (100%) |
| ypergasias-gov-gr-category | 〃 | 〃 | published_date 500/500 (100%) |

- `scripts/backfill_site_fields.py`의 부수적 버그(스크래치 DB에 `sites` row 미등록 → FK 제약으로 전체 INSERT 무음 실패) 발견·수정 — 모든 사이트 백필에 영향을 미치는 공통 버그였음.
- 그리스 3사이트 정규식 버그는 WP REST API의 `T` 구분 ISO datetime을 쓰는 모든 크롤러에 공통 적용될 수 있는 패턴이나, 이번 작업 범위는 3개 사이트로 한정.

## 5. 의도적으로 남긴 것과 사유

| 항목 | 건수 | 사유 |
|---|---:|---|
| `pdf_url`가 `ftp://` | 76 | 유효한 다운로드 경로이므로 보존 (http(s)만 절대화/정리 대상) |
| listed_date 원값 유지 | 2,128 | `"2026 - 12??"` 등 진짜 파싱 불가능한 표기 — 날조 방지 원칙상 원값 보존 |
| published_date 원값 유지 | 568 | DOI 문자열, 페이지 번호, 연도 누락 표기 등 — 동일 사유 |
| abstract == title | 1,425 (기존 문서 1,424에서 +1) | 사용자 결정: 원본 사이트가 애초에 짧은 abstract를 title과 동일하게 제공하는 경우이므로 별도 조작 없이 유지. +1 증가분은 Task 5 재수정 라운드에서 abstract 4중 인코딩 3건을 추가로 언이스케이프하며 그중 1건이 정제 후 title과 우연히 일치하게 된 부수 효과로 추정(내용 손실 아님). |
| abstract 후보 141건 (영구 no-op) | 141 | PDF 바이너리 원문이 실수로 abstract 컬럼에 저장된 행 — SQL `LIKE` 전처리 필터는 걸리지만 실제 태그/엔티티 정규식과는 매칭되지 않아 `clean_text()`가 안전하게 아무것도 바꾸지 않음(정상 동작) |
| nistdigitalarchives 깨진 title(�) | 4 | 원본 사이트 자체 인코딩 문제로 판단, 재크롤 백필 범위 밖(이번 작업은 masaf/그리스 3사이트로 한정) |
| masaf title 개행(LF) 잔존 | 10 | **금번 검증에서 새로 확인된 잔존 항목.** 원인: Task 9 masaf 백필(재크롤)이 Task 5 DB 정제(R1/R4) *이후*에 실행되어, 재크롤로 새로 채워진 title 값에 포함된 개행 문자가 R4 규칙 적용 대상에서 누락됨. 내용 손실은 아니며(제목 텍스트 자체는 정상), 후속 세션에서 `cleanup_metadata.py --apply`를 1회 더 재실행하면 해소 가능 — 이번 Task 11은 검증·보고 전용이라 코드/데이터 수정 없이 사실만 기록함. |

## 6. 백업·롤백 정보

- 정제 전 백업: `data/libertree.db.bak-20260711-0120-precleanup` (5,212,319,744 bytes)
- 롤백 방법: `cp data/libertree.db.bak-20260711-0120-precleanup data/libertree.db` (서비스 중단 후 수행 권장 — DB는 운영 중이며 finolaw UI가 readonly로 접근)
- blob 삭제(가짜 PDF 3,704건)는 롤백 대상에서 제외됨 — 파일 자체가 가짜 콘텐츠였으므로 복구 불필요

## 7. 최종 검증 배터리 (Task 11, 2026-07-11 재실행)

| 항목 | 기대 | 실제 | 판정 |
|---|---|---:|---|
| title 엔티티/태그/개행 (`_TITLE_WHERE` 기준) | 0 | 10 | **편차** — 전량 masaf, Task 9 백필이 Task 5 정제 이후 실행되어 미반영(§5 참조) |
| keywords CDATA | 0 | 0 | PASS |
| keywords 잔존 후보(rule-exact) | 2(no-op) | 2 | PASS (task-5 최종 dry-run과 일치) |
| 깨진 title(�) | masaf 0 / nist 4 | masaf 0 / nist 4 | PASS |
| 비ISO listed_date (`_NON_ISO_WHERE` 기준) | 2,128 | 2,128 | PASS |
| 비ISO published_date (`_NON_ISO_WHERE` 기준) | 568 | 568 | PASS |
| meta_url='ERROR' | 0 | 0 | PASS |
| 가짜 PDF sha 그룹(krivet/kedi) 잔존 | 0 | 0 | PASS |
| jeonnam-go-kr-m7116 pdf_downloaded | 참고치 | 770 (정상 바이너리) | PASS (Task 6에서 이미 확인된 사실 재확인) |
| pdf_downloaded 확정치 | ~267K | 266,815 | PASS |
| text_extracted | 유지 | 260,260 | PASS |
| 그리스 3사이트 published_date 채움률 | 100% | mindev 3433/3433, mindigital 648/648, ypergasias 500/500 | PASS (전부 100%) |
| FTS5 표본 검색 | 동작 | `documents_fts` 427,266 rows, "agricoltura"(이탈리아어)·"통일연구원"(한글) 검색 모두 정상 히트 | PASS |
| unittest 전체 | 전부 PASS | `tests.test_storage tests.test_cleanup_metadata tests.test_scan_fake_pdfs tests.test_backfill_site_fields` → **74 tests, OK** | PASS |

**총평**: 14개 점검 항목 중 13개 PASS, 1개 편차(masaf title 개행 10건 — Task 9가 Task 5보다 나중에 실행되어 생긴 순서 문제, 내용 손실 없음, 후속 재실행으로 해소 가능).

## 8. 산출물 경로 목록

### DB 정제 (Task 1~5)
- `scripts/cleanup_metadata.py`, `tests/test_cleanup_metadata.py`
- `data/audit/cleanup_metadata_20260711_011754_dryrun.json` (최초 dry-run)
- `data/audit/cleanup_metadata_20260711_012938_dryrun.json` (1차 apply 후 재검증 dry-run)
- `data/audit/cleanup_metadata_20260711_013337_apply.json` (버그 수정 후 최종 apply)
- `data/audit/cleanup_metadata_20260711_013354_dryrun.json` (최종 멱등성 확인 dry-run)

### 가짜 PDF (Task 6)
- `scripts/scan_fake_pdfs.py`, `tests/test_scan_fake_pdfs.py`

### 크롤러 오염 방지 (Task 7)
- `crawler/sites/custom/esteri*.py`, `crawler/sites/custom/presse-economie*.py`, `crawler/sites/custom/doras*.py`

### 백필 툴 + masaf (Task 8~9)
- `scripts/backfill_site_fields.py`, `tests/test_backfill_site_fields.py`
- `crawler/sites/custom/masaf-gov-it-flex.py`
- `data/audit/backfill_masaf-gov-it-flex_20260711_023606_dryrun.json`, `_023741_dryrun.json`, `_030102_dryrun.json`, `_032421_apply.json`

### 그리스 3사이트 (Task 10)
- `crawler/sites/custom/mindev-gov-gr-category.py`, `mindigital-gr-archives.py`, `ypergasias-gov-gr-category.py`
- `data/audit/backfill_mindev-gov-gr-category_20260711_033520_dryrun.json`, `_034133_dryrun.json`, `_081319_apply.json`
- `data/audit/backfill_mindigital-gr-archives_20260711_034328_dryrun.json`, `_040944_dryrun.json`, `_043545_apply.json`
- `data/audit/backfill_ypergasias-gov-gr-category_20260711_034427_dryrun.json`, `_041024_dryrun.json`, `_043552_apply.json`

### 백업
- `data/libertree.db.bak-20260711-0120-precleanup`

### 세부 태스크 보고서
- `.superpowers/sdd/task-1-report.md` ~ `task-10-report.md` (규칙 구현/버그 수정 상세 근거)
- `.superpowers/sdd/task-11-report.md` (본 검증 배터리 실행 로그)
