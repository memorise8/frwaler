# 메타데이터 정제 + 가짜 PDF 정리 + 크롤러 보수 — 설계

날짜: 2026-07-11 · 상태: 사용자 승인됨 · 대상 DB: `data/libertree.db` (427,266 documents)

## 배경

2026-07-10~11 데이터 품질 점검에서 발견:

| # | 문제 | 규모 | 복원 방법 |
|---|---|---|---|
| 1 | 가짜 PDF (HTML 에러페이지가 .pdf로 저장, pdf_downloaded=1) | ≥3,458건 (+오염 txt 871건) | 플래그 리셋 + blob 삭제 |
| 2 | listed_date/published_date 비ISO 포맷 | 19,751 + 948건 | DB 정제 (로케일 규칙 파싱) |
| 3 | title/abstract/keywords HTML 엔티티·태그·CDATA·개행 | ~4,200건 (esteri-it, presse-economie 집중) | DB 정제 (기계적 복원) |
| 4 | masaf-gov-it title 인코딩 깨짐(�) | 154건 | **재수집만 가능** (정보 소실) |
| 5 | 그리스 3사이트 published_date 추출 0% | 4,581건 | **재수집만 가능** (미추출) |
| 6 | URL 이상 (`'ERROR'` 2, 상대경로 ~19, 인용문/DOI 오입력 소수) | ~25건 | DB 정제 |

재검증에서 **오탐으로 판정**되어 제외된 것: 그리스 장문 abstract(속기록 전문 = 스펙상 "본문" 허용), HAL 계열 42K자 authors(실제 컨소시엄 저자명단), 미래 날짜 137건(저널 발행예정일 원데이터), meta_url==pdf_url 18,102건(직링크 게시물 패턴).

## 사용자 결정 사항

- 범위: DB 정제 + 가짜 PDF 포함 + 오염원 크롤러 수정 + masaf/그리스 재수집 (전체)
- abstract==title 복제 1,424건: **그대로 유지** (원문에 초록 없음, 스펙상 본문 허용)
- 가짜 PDF 리셋 후 재다운로드 시도 안 함 (에러페이지 = 차단 사이트, 자동회복 소진)

## 컴포넌트

### A. `scripts/cleanup_metadata.py` (신규 — DB 정제 배치)

- dry-run 기본 / `--apply`로 반영. apply 전 DB 파일 백업(`data/libertree.db.bak-<ts>-precleanup`)
- 규칙별 변경 건수·표본을 `data/audit/cleanup_metadata_<ts>.json`에 기록
- FTS는 기존 `documents_au` 트리거가 자동 동기화 (2026-07-10 검증 완료)
- 멱등: 재실행 시 변경 0건이어야 함

| 규칙 | 대상 필드 | 처리 |
|---|---|---|
| R1 | title/abstract/keywords | HTML 엔티티 디코드 (`html.unescape`) |
| R2 | abstract (실태그 보유분) | 태그 스트립 + 공백 정리 |
| R3 | keywords | `<![CDATA[...]]>` 제거, 구분자 기준 중복 토큰 제거 |
| R4 | title | 개행→공백, 연속 공백 정리, 트림 |
| R5 | listed_date, published_date | 비ISO → `YYYY-MM-DD` 정규화. 프랑스어 월명·dotted(`2014.10.24`)·RFC822·`1 Feb 24`·슬래시(DD/MM vs MM/DD는 사이트 로케일로 판정). 파싱 불가는 원값 유지 + 리포트 |
| R6 | meta_url, pdf_url | `'ERROR'`→NULL, 상대경로→절대화(사이트 base), 비URL 값(인용문·DOI)→NULL + 리포트. `ftp://` 76건은 유지(리포트만) |

### B. `scripts/scan_fake_pdfs.py` 확장 (가짜 PDF)

기존 도구(`--reset --delete-blob` 보유)에 탐지 기준 추가:
1. sha256 중복그룹(≥2) 대표 blob의 매직바이트가 `%PDF` 아님 → 그룹 전체
2. 단독 소형(<10KB) blob 매직바이트 비-`%PDF`

처리: `pdf_downloaded=0, text_extracted=0, pdf_sha256=NULL, pdf_size_bytes=NULL` + `.pdf` blob 삭제 + 오염된 `.txt`(871건) 삭제. 예상 규모 ~3,458건.

### C. 크롤러 수정 (코드만 — 기존 데이터는 A가 정제)

- `esteri-it-it`: 파싱 결과에 엔티티 디코드·태그 스트립 적용
- `presse-economie-gouv-fr`: RSS CDATA 처리
- `doras-dcu-ie`: title 개행 정리

### D. 크롤러 수정 + 재수집 (정보 소실분)

- `masaf-gov-it-flex`: 응답 인코딩 오감지 수정 → 재크롤 → `(site_id, post_number)` upsert로 title 154건 갱신
- 그리스 3곳 `mindev-gov-gr-category`(3,433) / `mindigital-gr-archives`(648) / `ypergasias-gov-gr-category`(500): 날짜 추출 로직 추가 → 재크롤 → published_date 백필
- 실행은 기존 runner 경로 사용 (DB 쓰기 정책: promote_all/runner 계열만 쓰기 가능). 신규 문서 추가 유입은 정상 동작으로 허용

## 실행 순서

```
1. DB 백업
2. cleanup_metadata dry-run → 리포트 검토 (진단 수치와 대조)
3. cleanup_metadata --apply
4. scan_fake_pdfs 확장 실행 (dry-run → apply)
5. 크롤러 수정 커밋 (C, D)
6. masaf + 그리스 3곳 재수집 → upsert
7. 점검 쿼리 배터리 재실행 → 최종 보고
```

## 롤백·안전장치

- apply 전 백업 파일로 복원 가능. 규칙 전부 멱등이라 중단 후 재실행 안전
- 요약 정합성: 정제 대상 중 summary 보유는 날짜류 2,147건(날짜 변경은 요약과 무관), abstract 정제 대상 summary 0건 — 요약-원문 불일치 없음
- DB는 운영 중 정책: 정제·재수집 중 다른 쓰기 작업 없음(idle) 확인 후 실행

## 검증 (성공 기준)

1. 규칙별 유닛 테스트: 대표 입력→기대 출력 (tests/에 추가)
2. dry-run 건수 == 2026-07-10 진단 수치 대조
3. apply 후 점검 쿼리 배터리 잔여 0 (정당한 예외는 리포트 명시)
4. FTS 표본 검색: 정제된 제목으로 `/search` 히트 확인
5. 유효 PDF 수치 확정 (~267K) → 클라이언트 보고 수치로 사용
