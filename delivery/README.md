# libertree-delivery 설치 가이드

## 전수 검증 실행 범위와 재개

`delivery.health.runner --all`은 납품 목록을, `--all-registered`는 추가 등록을 포함한
전체 수집기를 검사합니다. `--site SITE`도 추가 등록 사이트를 지정할 수 있습니다.
화면의 기본 납품 목록은 유지합니다. 등록 목록·코드 로딩 조사와 실제 사이트 검사는 별도입니다.

```bash
python -m delivery.health.runner --all-registered --timeout 60 --limit 3 --delay 1 --workers 4 \
  --report-dir /path/to/survey --profile-id FROZEN_ENVIRONMENT_ID --resume
```

검사 소스·컨테이너 이미지·브라우저·키 설정을 고정한 뒤 고유한 `--profile-id`를 부여합니다.
환경이나 키를 바꾸면 새 식별자와 별도 결과 폴더를 사용합니다. `--resume`은 같은 코드 해시와
실행 조건에서 기록된 성공·실패 모두를 유지합니다. 따라서 시간 초과나 접근 차단을 자동으로
재시도해 최초 실패를 덮어쓰지 않습니다. 원인을 조사한 후 별도 회차에서 대상만 재검사합니다.
`scan.json`에는 전체 대상·완료·재사용 수와 상태별 집계를 계속 기록합니다.

이 1차 검사의 범위는 소량 메타데이터 접속·파싱입니다. 실제 DB 저장, 신규 건수 반복,
중단·재개, PDF/TXT 처리, 원본 총량·전체 완전성은 별도 인수 항목으로 남습니다.
운영 DB·blob을 연결하지 않은 읽기 전용 컨테이너에서 실행하고 임시 파일만 허용합니다.
등록 목록 밖의 고객 DB 사이트는 고객 DB 범위를 확인한 후 추가 조사해야 합니다.

## 식별값·날짜·테스트 자료 원복 수정 (2026-10-08 후속 후보)

이번 후속 후보에는 확인된 결함 수정과 조사·보정·제한적 원복 도구의 격리 검증이 포함됩니다.
업체 회신 2026-10-08-r2에 따라 BUG-01의 목표는 누락 조사·보충이고, BUG-03의 범위는
실제 DB 전체 사이트·문서입니다. 원복 도구나 KRIVET 표본 성공만으로 이 확대 범위를 완료 처리하지 않습니다. 운영 DB의
기존 자료 보정·삭제와 고객 서버 배포는 수행하지 않았습니다. 이전 후보의 검증
기록은 아래에 보존하며, 아래 2026-10-08 초기 기록과 후속 결과를 구분합니다.

### KRIVET 식별값과 수집 재개

`krivet-re-kr`는 쿼리 순서에 관계없이 `pstNo`를 문서 식별값으로 사용합니다.
예시 두 URL의 값은 각각 `NH0000000791`, `NH0000000821`입니다.
`pstNo` 누락·중복·잘못된 호스트는 오류로 처리하며 메뉴 번호로 대체하지 않습니다.
별도 수집기 `krivet-re-kr-kor`의 규칙은 바꾸지 않았습니다.

기존 잘못된 ID가 남아 있거나 동일 ID의 문서가 여러 개면 재수집을 차단합니다.
아래 보정 도구의 충돌 보고서를 검토하고 기존 문서를 먼저 보정한 뒤 수집을 재개합니다.
보정은 `seq_id`와 파일·번역·요약 연결을 유지합니다. URL의 메뉴/쿼리 순서가 달라도
동일 `pstNo`의 기존 문서를 재사용합니다. 충돌 문서는 자동 병합·삭제하지 않습니다.

KRIVET는 매번 첫 페이지부터 중복을 제거하며 재탐색합니다. 신규 없는 두 페이지만으로
종료하지 않고, 신규 저장 건수만 제한에 포함합니다. 이 방식은 오래된 페이지를 다시
읽을 수 있습니다. 날짜순 이어받기와 변경 중인 원본 목록 전체의 완전성은 보장하지 않습니다.
일회용 PostgreSQL에 실사이트 자료를 3건씩 두 번 저장해 총 6건과 서로 다른 ID를 확인했습니다.
이 검사는 PDF·텍스트 확보나 사이트 전체 수집 완료를 뜻하지 않습니다.

### 날짜 저장·조회 계약과 업그레이드

먼저 기존 절차대로 백업 후 명시적 `python -m delivery.be.migrate`를 실행합니다.
마이그레이션은 `published_date`와 `listed_date` 각각에 `_raw`, `_status`, `_precision`
TEXT 컬럼을 추가합니다. 기존 행의 상태는 `legacy_unchecked`이며 기존 값은 자동 변환하지 않습니다.
새 버전 BE는 이 컬럼이 없으면 시작 검증에서 중단합니다. SQLite는 명시적 `init_db`로
같은 컬럼을 추가할 수 있습니다. 기존 SQLite→PostgreSQL 이관 도구도 날짜 메타데이터를 보존합니다.

공통 v2 SQLite/PostgreSQL 저장 경로에서 완전한 날짜는 `YYYY-MM-DD`로 저장합니다.
각 필드의 원문은 저장 함수에 들어온 문자열이며, 사이트 파서가 앞서 가공한 원문까지 복원하는
것은 아닙니다. `02 octobre 2026`은 `2026-10-02`로 변환합니다. `07.08.2026`처럼
숫자 일/월 순서가 확인되지 않은 값은 정규 날짜를 NULL로 두고 원문과 `ambiguous` 상태를 보존합니다.
연/월만 있으면 `partial` 및 `year`/`month` 정밀도를 기록하고 날짜를 임의로 채우지 않습니다.
상태는 `normalized`, `missing`, `partial`, `ambiguous`, `invalid`, `unrecognized`,
기존 자료의 `legacy_unchecked`로 구분합니다. 발행일과 게시일은 서로 복사하지 않습니다.

숫자 날짜의 일/월 순서는 사이트 증거 없이 추정하지 않습니다. 새 수집에 필요한 사이트 규칙은
`crawler/document_dates.py`의 `SITE_DATE_ORDERS`에 근거 fixture와 함께 추가해야 합니다.
현재 이 목록은 비어 있습니다. 날짜 보정 CLI의 `--date-order`는 해당 배치에만 적용됩니다.
구형 `crawler/db.py`의 독립 legacy DB는 이번 v2 날짜 계약 대상이 아닙니다.

날짜 범위 검색·정렬은 유효한 완전 ISO 날짜만 사용합니다. 미확정 날짜는 범위 검색에서
제외되고 정렬 시 뒤에 놓입니다. 화면에는 원문과 `날짜 확인 필요` 또는 `부분 날짜`를 표시합니다.
기존 비정규 날짜는 보정 전까지 날짜 조건을 해제해서 조회할 수 있습니다.

### 기존 자료 보정 도구

도구는 납품 대상 `delivery/scripts/repair_metadata.py`에 있습니다. 기본 연결 대상은 없으며,
별도로 설정한 `REPAIR_PG_DSN` 또는 `--sqlite`를 사용합니다. DSN에 운영 연결을 설정하는 것은
운영 변경 단계이므로 실제 DB 구조·대상·백업을 먼저 검토해야 합니다. 아래는 명령 형식입니다.

```bash
# 읽기 전용 미리보기: 마이그레이션 이후, 사이트별 최대 1,000건
python -m delivery.scripts.repair_metadata plan --kind krivet --site-id krivet-re-kr --plan krivet-plan.json
python -m delivery.scripts.repair_metadata plan --kind dates --site-id SITE --after-seq 0 --limit 500 --plan dates-plan.json
# 사이트가 DMY 형식임을 확인한 배치에만 추가:
# --date-order DMY --format-evidence '사이트 형식 근거 또는 검토 기록'

# 검토된 계획 적용 / 동일 계획으로 변경 취소
python -m delivery.scripts.repair_metadata apply --plan krivet-plan.json
python -m delivery.scripts.repair_metadata restore --plan krivet-plan.json
```

계획 JSON에는 변경 전/후 값·충돌·미해결 날짜·다음 `after_seq`와 체크섬이 있습니다.
계획 파일은 0600 권한으로 생성하며 기존 파일을 덮어쓰지 않습니다. 적용 전 이 파일을
별도 보관하세요. 적용/취소는 트랜잭션이며 미리보기 이후 관련 값이 바뀌면 중단합니다.
동일 계획의 재실행은 이미 반영된 행을 다시 변경하지 않습니다. 단계별 배치를 검토하고
모든 대상이 처리됐는지 별도로 집계해야 합니다. 테스트 성공은 운영 자료 보정 완료가 아닙니다.

### 실제 저장 데이터 전수 조사와 누락 대조

`delivery/scripts/audit_documents.py`는 납품 목록 밖을 포함해 실제 `documents`의 모든 행과
사이트를 읽습니다. ID NULL/빈 값/공백, 중복 키 그룹과 서로 다른 URL 수, 날짜 해석 상태,
URL·제목 누락, 알려진 참조 관계의 고아 행을 조사합니다. PostgreSQL은 일관된 읽기 전용
스냅샷과 서버 커서를 사용합니다. 현 DB의 외래키 목록도 남깁니다. 원본 PDF/TXT 내용은
이 도구에서 검사하지 않으며 DB 플래그를 파일 검증으로 간주하지 않습니다.

```bash
# 연결은 AUDIT_PG_DSN 환경변수로 별도 지정합니다. 기본 DB는 없습니다.
python -m delivery.scripts.audit_documents --out /path/to/new-private-audit
# SQLite 덤프 파일도 읽기 전용으로 조사할 수 있습니다.
python -m delivery.scripts.audit_documents --sqlite /path/to/copied.db --out /path/to/new-private-audit
# 확보한 원본 응답의 목록과 비교할 때:
python -m delivery.scripts.audit_documents --out /path/to/new-private-audit --source-manifest source-scope.json
```

결과는 사이트별 건수·상태를 담은 `summary.json`과 행별 문제/중복 후보/원본 대조 결과의
`findings.jsonl`입니다. 출력 디렉터리는 새 0700 디렉터리로 만들며 기존 결과를 덮어쓰지 않습니다.
원본 목록 JSON은 `site_id`, `observed_at`(시간대 포함 관측 시각), `scope`(페이지·기간·확보 범위),
`documents` 배열을 포함하고 각 문서는 `post_number`, `meta_url`을 갖습니다.
중복된 원본 ID 목록은 조용히 합치지 않고 검토를 요구합니다.

현재 원본 식별 규칙을 확인한 사이트는 KRIVET입니다. 다른 사이트의 중복 그룹은 게시판별
번호 재사용 등 정상 사례일 수 있어 `requires_site_rule_and_source_review`로 남깁니다.
KRIVET도 잘못된 키로 이미 저장된 문서는 누락과 구분합니다. 확인된 원본 범위 밖의 완전성은
판정하지 않습니다. 다른 사이트의 키/URL 차이는 미수집 확정으로 처리하지 않습니다.

고객 누락 조사는 실제 테스트 일시·시간대·사이트·작업·배포 버전과 전후 로그/커서를 먼저
확정해야 합니다. 이후 원본 목록 대조 → 누락 확정/의심/근거 부족 구분 → 기존 자료를
보존하는 겹치는 구간 재탐색·보충 → 원본 재대조 순으로 진행합니다. 현재 fixture에서는
KRIVET 기존 8건의 ID를 유지한 채 뒤쪽 누락 2건을 보충해 원본 10건과 일치함을 확인했습니다.
고객 DB 전체와 당시 원본 증거는 아직 조사하지 않았으므로 실제 누락 해소·기존 자료 전수 보정은
완료되지 않았습니다. 아래 원복 도구는 검토 결과 제한적 원복이 필요한 경우에만 사용합니다.

### 지정 테스트 자료의 안전 원복

`delivery/scripts/recover_test_data.py`는 PostgreSQL 전용입니다. 오늘 날짜 전체를 선택하지 않습니다.
확인한 단일 `site_id`, 명시적 `seq_id` JSON 배열, 시간대가 포함된 수집 구간
(`[시작, 종료)`), 명시적 blob 루트가 모두 필요합니다. 원본 DB/파일 전체의 별도 백업도 유지하세요.

1. 수집·예약·번역·파일 Worker를 중지하고 해당 사이트의 활성 예약과 대기/실행 작업을 정리합니다.
   도구는 실행 중 작업·활성 예약·워커 소유 번역을 발견하면 중단합니다. 서비스 중지는 자동화하지 않습니다.
2. 고객 실제 스키마와 대상 목록을 대조합니다. 새 외래키·사용자 트리거·지원하지 않는 참조 구조는
   자동 처리하지 않습니다. 테스트 전 커서가 있으면 전체 행을 JSON 배열로 제공하며, 없었던 경우는 `[]`입니다.
   KRIVET에 한해 검증한 재탐색 정책 `--krivet-rescan`을 선택할 수 있습니다. 다른 사이트의 커서를 추정하지 않습니다.
3. 읽기 전용 계획을 만들고 문서·연결 행·커서·파일 목록을 검토합니다. 계획 작성은 일관성을 위해 잠깐 테이블 잠금을 잡습니다.
4. `--workers-stopped`는 실제 서비스 중지를 확인한 뒤만 사용합니다. 파일 백업은 blob 루트 밖의
   새 0700 디렉터리에 보관합니다. 공간과 무결성 검사가 실패하면 처리를 중단합니다.
5. 적용은 대상 번역의 RESTRICT와 연결 행의 CASCADE를 확인해 트랜잭션으로 처리하며 수집 작업 이력은 보존합니다.
   DB 반영 후 원본 파일을 백업 보관소로 격리합니다. 중간 실패 시 같은 명령을 재실행하면 남은 파일 처리를 이어갑니다.
6. 취소는 백업에서 같은 `seq_id`, 연결 행, 이전 커서, 파일을 복원합니다. 복원 중 실패도 재실행 가능합니다.
   재수집한 동등 문서나 변경된 파일이 있으면 덮어쓰지 않고 검토를 요구합니다.

```bash
# RECOVERY_PG_DSN을 검토 대상 DB로 별도 설정한 뒤 실행합니다.
python -m delivery.scripts.recover_test_data plan --plan recovery-plan.json --site-id krivet-re-kr   --seq-ids confirmed-seq-ids.json --collected-from 2026-10-08T09:00:00+09:00   --collected-to 2026-10-08T10:00:00+09:00 --blob-root /path/to/blobs --krivet-rescan
# 다른 사이트에서는 --krivet-rescan 대신 --cursor-before pre-test-progress.json을 사용합니다.
python -m delivery.scripts.recover_test_data apply --plan recovery-plan.json   --blob-root /path/to/blobs --backup-dir /path/to/private-backup --workers-stopped
python -m delivery.scripts.recover_test_data restore --plan recovery-plan.json   --blob-root /path/to/blobs --backup-dir /path/to/private-backup --workers-stopped
```

삭제 후 새로 수집하면 새 `seq_id`가 발급됩니다. 옛 파일을 새 ID에 자동 연결하지 않으므로
파일 작업을 다시 실행해야 합니다. 백업 복원은 기존 ID와 파일 연결을 되살립니다.
고객 DB의 실제 구조·대상은 아직 확인하지 않았으므로 고객 운영 삭제 가능 여부는 판정하지 않았습니다.

전체 등록 1,245개(납품 806개·추가 439개)의 정적 조사표를 갱신했습니다. 신규 건수 검토 대상은
KRIVET를 포함한 10개이며 나머지 1,235개를 정상으로 판정하지 않습니다. 다중 쿼리 ID 추출
추가 검토 후보 8개가 있으나 확인된 버그 수는 아닙니다. 실인증·전수 실사이트·전체 수집 완전성·
전체 배포 설치/복구 인수는 별도 잔여 항목입니다.


## 이번 배포 후보의 수집 규칙과 제한 (2026-10-08)

이번 버전은 배포 후보입니다. 실제 사이트 인증, PostgreSQL 신규 설치·업그레이드,
브라우저 연동 검증이 남아 있으므로 운영 인수 완료 버전으로 간주하지 않습니다.

- DOAJ: 중복 문서를 건수 제한에서 제외했습니다. 이전 실행으로 기존 문서가 50건
  이상 쌓인 경우의 조기 종료도 해제했습니다. 등록일 내림차순이며 날짜순 이어받기는 아닙니다.
- HAL 6개(ANR, AMU, CEA, EHESS, ENS-LYON, INSERM): 페이지에서 실제 처리한
  위치를 저장합니다. 실패한 페이지는 재탐색합니다. 기존 커서는 버전 2로 전환하면서
  시작점부터 중복 제거 재탐색하므로 처음 실행이 오래 걸릴 수 있습니다. 순서는 문서 ID
  오름차순이며 페이지 위치를 사용하므로 원본 목록 변경 시 완전성을 보장하지 않습니다.
- ScienceON 국내·해외는 실제 신규 저장 기준을 격리 테스트로 확인했습니다. 연도·자료 유형별
  조회이며 전역 최신순을 보장하지 않습니다. `full`은 제한하고 지원하는 모드를 사용합니다.
- 등록 1,245개 중 위 9개만 신규 건수 기준을 검토했습니다. 나머지 1,236개는 미검증입니다.
  806개 납품 목록과 추가 등록 439개를 구분한 결과표를 제공합니다.
- `GET /sites/{site_id}/capabilities`는 수집 순서·지원 모드·키 설정 유무·제한을 반환합니다.
  키 값은 반환하지 않습니다. 값의 존재는 실제 인증 성공을 뜻하지 않습니다.
- FE에서 시험 실행 확인이 필요합니다. 직접 `POST /jobs`, 활성 예약 등록, 작업 재시도 시
  `trial: true`를 명시해야 합니다. 미지원 모드와 필수 키 미설정은 BE에서도 거부합니다.
  기존 예약은 남아 있으므로 업그레이드 전 예약을 점검하세요. Worker에서도 모드·키를 검사합니다.
- `backfill`의 건수 제한은 한 작업의 한도이며 후속 작업이 자동 등록됩니다. 전체 체인을
  멈추려면 취소가 필요합니다. 무제한은 건수 제한 해제이며 전체 사이트 완주 보장이 아닙니다.

### 기존 문서 PDF·텍스트만 처리

사이트의 수집 실행 화면 아래에서 기존 문서 파일 이어받기를 선택합니다. 사이트,
DB 수집 기간(UTC, 종료일 포함), 문서 번호 다음부터, 1~1,000건 조건으로 미리보기 후
등록합니다. 자료 발행일 기준이 아니라 DB에 수집된 날짜 기준입니다. 빈 기간은 전체 기간입니다.
기본은 DB 파일 미완료 표시와 PDF URL을 기준으로 고르며, 완료 문서 재확인을 선택하면
실제 파일이 없거나 손상된 경우도 점검합니다. 활성 파일 작업이 있는 문서는 제외합니다.
미리보기와 등록 사이 상태가 바뀔 수 있어 등록 시 재선택합니다.

API는 `POST /files/backfill`입니다. `site_id`, `after_seq`(기본 0), `limit_n`(기본 100),
`collected_from`, `collected_to`(YYYY-MM-DD 또는 null), `include_existing`(기본 false),
`dry_run`(기본 true)을 받습니다. 실제 등록은 `dry_run: false`입니다.
문서 정보 재수집 없이 파일 작업을 등록하고, 작업 현황에서 PDF·텍스트 상태를 별도로 봅니다.
파일 작업의 문서 수집 상태 `done`은 큐 등록 완료이며 파일 처리 완료가 아닙니다.

### 검색 API 키 설정

[검색 키 안내](../docs/deliverables/수집기_검색키_설정안내_20261008.html)를 참고해
`delivery/.env`에 필요한 값을 입력합니다. 이번 Compose는 8개 키를 BE·수집 Worker·health에
전달하며 health 하위 검사 프로세스에도 허용합니다. 파일 Worker에는 전달하지 않습니다.
키 변경 후 BE·Worker와 사용 중인 health 컨테이너를 재생성해야 합니다.
별도 `docker-compose.keys.yml`은 필요하지 않습니다. 실제 `.env`와 키 값은 번들에 없습니다.

FE 빌드는 Google Fonts 다운로드를 사용하지 않습니다. 접속한 PC의 한글 시스템 글꼴을
사용하므로 PC에 따라 글꼴 모양이 다를 수 있습니다.

## FE에서 PDF 다운로드·텍스트 추출 (2026-10-08)

사이트별 **수집 실행** 화면에서 `PDF 다운로드·텍스트 추출도 실행`을 선택합니다
(기본 선택). `크롤러 실행`을 누르면 문서 정보 수집 후 별도 `file-worker`가
해당 작업의 PDF를 내려받고 텍스트를 추출합니다. 화면을 닫아도 계속 처리됩니다.
기존 일괄 실행·예약 실행은 문서 정보 수집 설정을 유지합니다. 파일 처리가 필요하면
사이트별 실행을 사용하거나 `POST /jobs`에 `process_files: true`를 명시합니다.

- 범위: 이번 실행에서 공통 저장 경로로 처리한 신규/기존 문서. 공통 경로를 쓰지 않는
  수집기는 이번 실행에서 새로 저장한 문서가 대상입니다. DB 전체의 과거 미다운로드
  자료를 자동으로 일괄 처리하지 않습니다.
- 파일 상태는 문서 수집 상태와 별개입니다. 문서 수집의 `완료`가 파일 처리 완료를
  뜻하지 않습니다. PDF 확보·텍스트 확보·대기·실패·주소 없음·중지 건수를 따로 표시합니다.
- **최근 작업 현황 → 작업 번호 또는 PDF·텍스트 확인**에서도 결과를 볼 수 있습니다.
  문서 수집 종료 후 `실패·중지한 파일 재시도`, `파일 처리 중지`를 사용할 수 있습니다.
- URL이 없으면 `PDF 주소 없음`으로 건너뜁니다. 공개 HTTP(S) 주소만 처리하고,
  HTML·크기 초과는 다운로드 실패로 처리합니다. PDF 헤더 검사는 전체 구조 검증과
  다르며, PDF를 저장했어도 텍스트 파싱이 실패하면 파일 작업은 실패로 표시합니다. 현재 한 문서의 `pdf_url`
  하나를 처리하며 HWP 등 다른 첨부 형식은 이 파일 워커의 대상이 아닙니다.
- PDF 헤더와 저장된 해시를 확인해 기존 파일을 재사용합니다. 텍스트 추출 실패 시
  이미 받은 PDF는 보존합니다. 텍스트 레이어가 없는 PDF는 확인 필요 상태이며 OCR은
  포함하지 않습니다. 기존 파일의 잘못된 다운로드 플래그를 전수 교정하는 기능은 아닙니다.
- 일시적인 다운로드 실패·시간 초과·공간 부족은 최대 3회 시도합니다. 각 단계는
  `FILE_STAGE_TIMEOUT_SECONDS`(기본 180초, 10~600초), PDF는 최대 200MB입니다.
  파일 처리 전 최소 1GiB의 여유 공간을 요구합니다.
- 문서 수집 취소 시 대기 파일도 중지합니다. 파일 처리 중지 시 아직 반영하지 않은
  임시 파일은 버립니다. 이미 저장 완료된 PDF·텍스트는 삭제하지 않습니다.
- 파일 워커가 응답하지 않으면 화면에서 안내하고 대기열을 보존합니다. 워커 재시작 시
  만료된 작업을 회수합니다. 파일 처리는 현재 한 워커에서 순차 진행합니다.

기존 DB에는 명시적 마이그레이션이 필요합니다. 백업을 확보한 후 저장소 루트에서:

```bash
docker compose --env-file delivery/.env -f delivery/docker-compose.yml \
  --profile tools run --rm --build migrate
docker compose --env-file delivery/.env -f delivery/docker-compose.yml \
  up -d --build be fe worker file-worker
```

기존 파일을 `BLOB_HOST_PATH`로 연결한 환경에서는 **두 명령 모두** 기존과 동일하게
`-f delivery/docker-compose.blob.yml`을 추가합니다. `be`, `worker`, `file-worker`가
같은 파일 저장소를 보도록 해당 오버라이드에도 새 서비스가 포함되어 있습니다.
마이그레이션은 기존 작업의 `process_files`를 false로 설정하므로 과거 작업이
갑자기 파일 다운로드를 시작하지 않습니다. 운영 서버와 납품 번들은 코드 수정만으로
갱신되지 않습니다.

이번 세션 검증: FE 테스트·타입·린트·빌드와 로컬 PDF/파일 처리 테스트를 수행합니다.
초기 후보 검증 환경에서는 Docker·소켓 접근 제한으로 PostgreSQL 검사가 미실행이었습니다.
후속 후보는 별도 일회용 PostgreSQL에서 회귀 검증했습니다. 고객 환경 인수와 구분합니다.


## 수집 결과 정확성 보강 (2026-10-07)

- DOAJ는 요청 실패·잘못된 JSON/목록·항목 처리 실패를 작업 실패로 기록합니다.
  실패 전 저장한 문서 수와 마지막 완료 커서는 보존하고 자동 백필 체인을 멈춥니다.
  취소 요청이 동시에 도착하면 취소 상태가 우선합니다. 이 명시적 실패 계약은
  현재 DOAJ에 적용했으며, 다른 수집기는 단계적으로 적용해야 합니다.
- DOAJ의 실행 건수 제한이 페이지 중간에 걸리면 같은 페이지의 항목 위치를
  커서에 저장합니다. 재개할 때 그 페이지의 남은 항목부터 처리합니다.
- 공통 PDF 다운로드 함수는 SQLite/PostgreSQL 연결을 구분하고 PDF 헤더를
  확인합니다. HTML·빈 응답·크기 초과·HTTP 오류는 저장 성공으로 처리하지 않습니다.
  실패한 재다운로드는 기존 파일과 DB 다운로드 정보를 덮어쓰지 않습니다.
  헤더 검사는 PDF 전체 구조 검증이 아니며, 개별 수집기의 독자 다운로드 경로와
  기존에 저장된 파일까지 소급 검사하지 않습니다. 메타데이터 수집 이후의 자동
  PDF 다운로드·텍스트 추출은 위의 파일 워커 기능을 사용합니다.
- 백필 화면은 활성 백필 작업과 저장 커서를 함께 읽어 대기·실행·취소 처리·중단·완주를
  구분합니다. 커서가 저장되기 전의 새 백필 작업도 표시합니다.
- 신규 저장 0건은 수집 성공을 의미하지 않습니다. 작업 로그와 건강 점검을 함께 확인합니다.

이번 변경과 미검증 범위는 [개발·검증 결과](../docs/deliverables/개발_및_검증결과_20261008.html)에 정리했습니다.

## 수집기 건강 점검 (2026-10-07)

원문 범위 조사에는 별도의 읽기 전용 도구를 사용할 수 있습니다.
DB·blob·비밀 설정을 마운트하지 않은 조사 환경에서 실행합니다.

```sh
python -m delivery.health.scope_inventory --out /reports/source-inventory.json
python -m delivery.health.source_observer --inventory /reports/source-inventory.json \
  --out /reports/entry-observation-attempt-1 --workers 4 --timeout 20
```

첫 도구는 등록된 모든 수집기의 정적 원문 시작점 후보·목록 설정·제한 신호를
조사합니다. 두 번째 도구는 후보 URL의 응답을 검사하며 수집기를 실행하지 않습니다.
새 결과 폴더만 허용하고, 같은 프로세스 내 호스트별 동시 요청을 1개로 제한합니다.
리다이렉트도 같은 제한을 따르며 429/Retry-After는 해당 호스트의 후속 요청을
보류합니다. 여러 observer 프로세스의 호스트 제한은 공유하지 않으므로 동시에
별도 observer를 실행하지 않습니다. DNS·요청·응답 크기를 제한하고 인증 URL 및
내부 주소는 조사하지 않습니다. 응답 본문·쿠키·오류 메시지는 보고서에 저장하지
않으며 숫자 후보·선택자 일치 건수·본문 해시·HTTP 상태만 기록합니다.

이 결과는 실제 수집기의 접속/파싱 성공, 완전한 원문 범위 또는 총량 인증이 아닙니다.
홈페이지 fallback과 미검토 API 시작점을 구분하며, 총건수 후보가 나와도 현재 총량은
미확인으로 유지합니다. 상세 파싱·DB 저장·재개·파일 검증은 별도로 수행해야 합니다.

`/health` 화면은 전체 카탈로그의 코드 로딩 상태, 최근 소량 검사 결과,
DB 구조, 워커 응답, 원본 PDF 표본을 보여줍니다. 화면 조회·새로고침은 외부
사이트에 요청하거나 수집 작업을 등록하지 않습니다.

기본 배포에 자동 검사는 포함되지 않습니다. 변경된 BE·FE·워커를 빌드하고
기존 최초 설치/업데이트 절차로 기동한 뒤, 아래 별도 서비스를 켭니다.
기존 데이터가 있는 환경에서는 먼저 백업하고 명시적으로 migration을 실행합니다.
현재 운영 컨테이너를 그대로 두고 코드만 수정하면 새 화면/API는 반영되지 않습니다.

```bash
# 코드 전수 점검만: 외부 통신 없음, inventory.json / inventory.csv 생성
docker compose -f delivery/docker-compose.yml -f delivery/docker-compose.health.yml \
  --profile health run --rm --build health \
  python -m delivery.health.runner --audit

# 특정 사이트만 실제 접속·파싱 검사 (최대 3개 표본, 60초 제한)
docker compose -f delivery/docker-compose.yml -f delivery/docker-compose.health.yml \
  --profile health run --rm health \
  python -m delivery.health.runner --site doaj-org-search --timeout 60 --limit 3

# 전체 사이트 검사 후 24시간 쉬고 반복: 2개 병렬, 사이트당 최대 60초
docker compose -f delivery/docker-compose.yml -f delivery/docker-compose.health.yml \
  --profile health up -d --build health

# 중지 (수집 워커·DB에는 영향 없음)
docker compose -f delivery/docker-compose.yml -f delivery/docker-compose.health.yml \
  --profile health stop health
```

`--all`은 모든 사이트에 실제 요청을 보냅니다. 주기는 `--interval`, 동시 실행은
`--workers`(1~4), 요청 간격은 `--delay`로 조절합니다. 한 번 실행한 CLI의 종료
코드 0은 선택한 사이트 모두 표본 정상, 1은 확인 필요 항목 있음, 2는 잘못된
인자 또는 다른 검사 실행 중입니다. 주기 서비스는 점검 실패가 있어도 다음 주기에 재검사합니다.
동일 결과 볼륨에서는 파일 잠금으로 중복 검사를 막으므로, 수동 검사 전 주기 서비스를 중지하세요.

검사 컨테이너에는 운영 DB 연결정보·문서 볼륨을 전달하지 않고 별도 네트워크를
사용합니다. 내부에서 메모리 DB와 표본 저장 대체 함수를 사용하고, 운영 문서를
추가·수정하지 않습니다. 인라인 PDF 파싱 등 크롤러가 필요로 하는 임시 다운로드는
발생할 수 있으며 `/tmp`에만 남습니다. 프로세스 시간 제한·임시 공간·메모리 제한을
적용합니다. 별도 DB/파일 쓰기를 요구하는 수집기는 `별도 점검 필요`로 분류합니다.

검사 결과는 `health-reports` 볼륨에 사이트별 JSON으로 원자적으로 교체되며 BE에는
읽기 전용으로 연결됩니다. 문서 본문·제목·원문 URL·원시 오류 로그·인증정보는
결과에 저장하지 않습니다. ScienceON은 `.env`의 인증정보가 없으면 `설정 필요`입니다.

상태의 의미:

- **표본 수집 정상**: 이번 검사에서 제목·HTTP(S) 링크가 있는 표본을 읽었습니다.
  실제 DB 저장, PDF 원본 다운로드, 모든 페이지의 완주를 검증한 것은 아닙니다.
- **0건·판정 보류**: 신규 문서 없음과 파싱 오류를 구분하지 못했으므로 정상으로 표시하지 않습니다.
- **접근 제한 / 접속 오류 / 시간 초과 / 필수 정보 누락**: 이번 검사에서 관측한 결과입니다.
  모든 네트워크 구현이 공통 계측을 사용하는 것은 아니므로, 계측되지 않은 실패는 판정 보류일 수 있습니다.
- **미검사 / 검사 오래됨 / 코드 변경·재검사 필요**: 현재의 정상 증거가 없습니다.
  `CRAWLER_HEALTH_STALE_HOURS` 기본값은 48시간입니다.
- 코드 점검표의 이어받기·시간 제한은 **정적 구현 신호**이며 실제 동작 보장이 아닙니다.

운영자 토큰을 사용하는 읽기 전용 API는 `GET /health/crawlers`, `GET /health/system`입니다.
`x-delivery-token` 헤더가 필요합니다. 기존 `/health`·`/ready`는 서버·DB 점검 용도로 유지합니다.
FE의 사용자 접근 인증은 기존 배포 경계(인증 프록시 등)를 계속 사용합니다.
워커 Docker healthcheck는 최근 heartbeat 또는 유효한 작업 lease를 확인합니다.
이는 프로세스 응답 증거이며 수집 진행률 검사가 아니고, Docker의 `unhealthy`만으로
컨테이너가 자동 재시작되지는 않습니다.

원본 파일 점검은 DB에서 `pdf_downloaded=1`인 최근 최대 20건의 존재·PDF 헤더를
확인하는 표본 검사입니다. 전수 무결성·해시 검증과 다릅니다. 최신 스키마가 빠진
환경은 `DB 업데이트 필요`, 파일이 연결되지 않은 환경은 `누락`으로 표시됩니다.

---

이 디렉터리는 `delivery/docker-compose.yml` 로 빌드/실행하는 **납품용 패키지**입니다.
프런트엔드(FE), 백엔드(BE), 수집 워커, 파일 워커, Postgres로 구성됩니다.

| 서비스 | 역할 | 이미지 빌드 | 노출 포트(호스트) |
|--------|------|-------------|--------------------|
| `postgres` | Postgres 16, 데이터 저장소 | `postgres:16` (빌드 없음) | 미노출 (컨테이너 네트워크 내부용) |
| `be` | 백엔드 API | `Dockerfile.be` | `127.0.0.1:${BE_PORT:-8080}` → 컨테이너 3001 |
| `fe` | 프런트엔드(Next.js) | `Dockerfile.fe` | `127.0.0.1:${FE_PORT:-3000}` → 컨테이너 3002 |
| `worker` | 번역/수집 백그라운드 워커 | `Dockerfile.worker` | 미노출 |
| `file-worker` | PDF 다운로드·텍스트 추출 워커 | `Dockerfile.worker` | 미노출 |

`migrate` 서비스(`profiles: ["tools", "bootstrap"]`)는 스키마 마이그레이션 전용이며, 평소 `up`에는
포함되지 않습니다. 스키마가 전혀 없는 최초 설치에서는 `--profile bootstrap`으로, 이미 떠 있는
환경에서 스키마만 바꿀 때는 `--profile tools`로 `run --rm migrate` 를 명시적으로 실행합니다(둘 다
같은 서비스를 가리키며, 프로파일 이름만 "지금이 최초 설치냐 아니냐"를 구분하기 위한 것입니다).
자세한 최초 실행 순서는 3장을 참고하세요.

`postgres`/`be`/`fe`/`worker`/`file-worker`는 모두 `restart: unless-stopped` 입니다 — 호스트 재부팅이나 Docker
데몬 재시작 후에도 사람 개입 없이 다시 올라오고, `worker`가 일시적으로 실패하더라도(예: 최초
부트스트랩 순서를 잘못 실행해 스키마가 아직 없는 상태로 시작한 경우) 백오프를 두고 계속
재시도하며 죽은 채로 방치되지 않습니다. `migrate`/`postgres-password-guard`는 1회성 작업이므로
`restart: "no"` 그대로입니다.

---

## 1. 빌드 전 준비사항 (Prerequisites)

### 네트워크 접근

빌드 시 아래 호스트에 대한 아웃바운드 네트워크 접근이 필요합니다.

| 호스트 | 용도 | 시점 |
|--------|------|------|
| `registry.npmjs.org` | FE의 npm 의존성 설치 (`npm ci`) | 빌드 시 |

FE는 한글 시스템 글꼴을 사용하며 Google Fonts 다운로드에 의존하지 않습니다.

### 필수 소프트웨어

- Docker Engine (Compose v2.22 이상, `docker compose run --build` 지원 버전)

---

## 2. 환경 변수

`delivery/.env.example` 을 복사해 `delivery/.env` 를 만들고 값을 채웁니다.

```bash
cp delivery/.env.example delivery/.env
```

`delivery/docker-compose.yml` 이 실제로 참조하는 변수는 다음과 같습니다.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `POSTGRES_USER` | `libertree` | Postgres 계정 |
| `POSTGRES_PASSWORD` | *(없음, 필수)* | Postgres 비밀번호. 비어 있으면 `postgres` 컨테이너가 **기동을 거부**합니다 (공식 postgres 이미지의 안전장치). |
| `POSTGRES_DB` | `libertree` | Postgres 데이터베이스명 |
| `BE_PORT` | `8080` | BE를 노출할 호스트 포트 (`127.0.0.1`에만 바인딩) |
| `FE_PORT` | `3000` | FE를 노출할 호스트 포트 (`127.0.0.1`에만 바인딩) |
| `DELIVERY_API_TOKEN` | *(빈 값)* | BE/FE 간 API 인증 토큰. `openssl rand -hex 32` 로 직접 생성합니다. `DELIVERY_AUTH_MODE=token`(기본값)일 때 32자 미만이거나 `.env.example`의 예시 문자열 그대로면 BE가 시작을 거부합니다. |
| `DELIVERY_AUTH_MODE` | `token` | `token` 또는 `disabled`. 로컬 격리 개발에서만 `disabled`를 명시적으로 사용하세요. |
| `DELIVERY_DB_POOL_MIN` | `1` | BE의 DB 커넥션 풀 최소 크기 |
| `DELIVERY_DB_POOL_MAX` | `10` | BE의 DB 커넥션 풀 최대 크기 |
| `DELIVERY_AGGREGATE_CACHE_SECONDS` | `30` | BE 집계 캐시 TTL(초) |
| `TRANSLATION_INTERNAL_MODEL` | *(빈 값)* | 내부(Ollama 호환) 번역 모델명 |
| `TRANSLATION_INTERNAL_ENDPOINT` | *(빈 값)* | 내부 번역 엔드포인트 (컨테이너에서 접근 가능한 주소) |
| `TRANSLATION_EXTERNAL_MODEL` | *(빈 값)* | 외부(OpenAI 호환) 번역 모델명 |
| `TRANSLATION_EXTERNAL_ENDPOINT` | *(빈 값)* | 외부 번역 엔드포인트 |
| `TRANSLATION_EXTERNAL_API_KEY` | *(빈 값)* | 외부 번역 API 키 |
| `TRANSLATION_WORKER_ID` | `delivery-worker-1` | 워커 인스턴스 식별자 |

번역 Provider(내부/외부) 변수를 설정하지 않으면 해당 Provider를 쓰는 작업만 안전하게 실패하며,
나머지 기능에는 영향이 없습니다.

### 사이트별 검색 API 키

일부 수집기는 GPT 번역 키와 별개로 대상 사이트의 Algolia 또는 Azure Search 키를 사용합니다.
8개 변수는 `crawler/.env.example`에 있지만, 기존 전달 Compose에는 Worker로 전달하는 설정이
없으므로 `.env`에 값만 입력해서는 적용되지 않습니다. 키별 용도, 미설정 영향, Docker 추가
설정 예시와 검증 절차는 [수집기 검색 키 설정 안내](../docs/deliverables/수집기_검색키_설정안내_20261008.html)를
참고하십시오. 실제 키 값은 문서에 포함하지 않습니다. 키 보유 확인과 API 유효성 검증은 별개이며,
이 안내가 서버 설정 적용 완료를 의미하지는 않습니다.

수집 기준·이어받기와 검색 키 설정의 확인 결과 및 수정 기준안은
[납품 이슈 현황 및 개선 계획](../docs/deliverables/납품_이슈현황_및_개선계획_20261008.html)에
정리되어 있습니다. 검증된 현상과 수정 예정 사항을 구분하며, 실제 키 값은 포함하지 않습니다.

### 포트 충돌 해결

`BE_PORT`/`FE_PORT`는 모두 `127.0.0.1`에만 바인딩되지만, 같은 호스트에서 다른 프로젝트가 이미
그 포트를 쓰고 있으면 (예: `127.0.0.1:8080`을 다른 컨테이너가 선점) `up -d`가 아래와 비슷한
오류로 실패합니다.

```
Error response from daemon: driver failed programming external connectivity ... address already in use
```

이때 기본값을 바꿀 필요는 없습니다 — 먼저 무엇이 그 포트를 쓰고 있는지 확인하고, 비어 있는
포트를 골라 `.env`의 `BE_PORT`/`FE_PORT`만 바꾸면 됩니다.

```bash
# 무엇이 8080을 쓰고 있는지 확인 (다른 프로젝트의 컨테이너일 수 있으니 함부로 내리지 않습니다)
docker ps --format '{{.Names}}\t{{.Ports}}' | grep 8080
ss -ltn | grep 127.0.0.1:8080

# .env에서 충돌하지 않는 포트로 변경 후 재기동
# 예: BE_PORT=18080
docker compose -f delivery/docker-compose.yml up -d
```

`LIBERTREE_PG_DSN`은 직접 설정하지 않습니다 — `docker-compose.yml`이
`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`로부터 자동으로 조합합니다
(비밀번호는 `POSTGRES_PASSWORD` 하나로만 관리되는 단일 소스입니다).

실제 blob(PDF) 스토리지를 호스트 디렉터리에 연결하려면 `delivery/docker-compose.blob.yml`을
함께 사용하고 `BLOB_HOST_PATH`(검증된 절대경로)를 설정하세요. 이 파일을 쓰지 않으면 blob은
Docker 볼륨(`blob`)에 저장됩니다.

---

## 3. 빌드 및 실행

리포지토리 루트에서 실행합니다.

```bash
# 전체 빌드
docker compose -f delivery/docker-compose.yml build

# 특정 서비스만 빌드 (예: FE만) — Postgres를 건드리지 않으므로 .env 없이도 가능
docker compose -f delivery/docker-compose.yml build fe
```

### 최초 실행 (부트스트랩) — 스키마가 전혀 없는 새 볼륨

새로 만든 Postgres 볼륨처럼 스키마가 전혀 없는 상태에서는 아래 두 명령을 **이 순서 그대로**
실행합니다. 셸의 `&&`로 묶으면 첫 명령이 성공(종료 코드 0)했을 때만 두 번째 명령이 실행되므로,
사실상 복사-붙여넣기 한 번으로 끝나는 하나의 절차입니다.

```bash
docker compose -f delivery/docker-compose.yml --profile bootstrap run --rm --build migrate && \
docker compose -f delivery/docker-compose.yml up -d
```

`migrate`는 `tools`/`bootstrap` 프로파일 뒤에 있어서 위 3장의 `docker compose ... build`
(프로파일 지정 없음)로는 다시 빌드되지 않습니다. `--build`가 없으면 예전에 캐시된 `migrate`
이미지가 옛 스키마 코드로 조용히 `PASS`를 찍을 수 있습니다 — 이번 리허설에서 실제로 겪은
문제입니다.

**왜 `be`가 `depends_on`으로 `migrate`를 기다리게 하지 않는가:** 더 간단해 보이지만 실제로는
동작하지 않습니다. `migrate`는 `tools`/`bootstrap` 프로파일에만 속해 있고, 평소의
`docker compose up -d`(프로파일 지정 없음)에는 포함되지 않습니다. 여기서 `be`가 `migrate`를
`depends_on`으로 가리키게 하면, Compose는 활성화되지 않은 프로파일에 속한 서비스를 참조하는
것 자체를 프로젝트 해석 오류로 취급하고 **즉시 실패**합니다 — 직접 재현해 확인한 동작입니다:

```
service "main" depends on undefined service "dep": invalid compose project
```

즉 `depends_on` 방식을 쓰면 이미 마이그레이션이 끝난 기존 환경에서도 평소의 `up -d`가 깨져
버립니다. 그래서 순서 보장은 Compose 그래프가 아니라 **명령 실행 순서**로 합니다:
`run --rm`은 컨테이너가 끝날 때까지 블로킹하므로, 두 번째 명령이 시작되는 시점에는 스키마가
이미 만들어져 있음이 보장됩니다.

부가적인 안전망으로, `be`/`worker`의 재시작 정책이 `unless-stopped`이기 때문에 혹시 두 명령을
잘못된 순서로(또는 동시에) 실행하더라도 `be`/`worker`는 스키마 부재로 한 번 죽었다가 백오프를
두고 계속 재시도하며, `migrate`가 끝나는 순간 스스로 정상화됩니다. 다만 이는 어디까지나
안전망이고, 위 두 단계 순서만이 매번 보장되는 방법이므로 항상 그 순서로 실행하세요.

### 이후 실행 — 스키마가 이미 있는 환경 (재기동, 재배포 등)

```bash
# 스키마를 바꿀 때만 명시적으로 실행 (평소 up에는 포함되지 않음)
# --build 없이는 캐시된 옛 migrate 이미지가 재사용될 수 있다
docker compose -f delivery/docker-compose.yml --profile tools run --rm --build migrate

# 기동
docker compose -f delivery/docker-compose.yml up -d

# 로그 확인
docker compose -f delivery/docker-compose.yml logs -f

# 중지
docker compose -f delivery/docker-compose.yml down
```

blob을 호스트 디렉터리에 연결해 실행하려면:

```bash
docker compose -f delivery/docker-compose.yml -f delivery/docker-compose.blob.yml up -d
```

## 4. 수집 결과 읽는 법

크롤러 실행 결과의 **"저장 N건"은 DB에 새로 추가된 문서 수**입니다. 크롤러가 실제로
처리한 문서 수와 다를 수 있습니다.

이미 수집된 문서를 다시 처리하면 기존 행이 갱신될 뿐 행 수가 늘지 않으므로 **0건으로
표시될 수 있습니다. 0건만으로 정상·실패 또는 "새 문서 없음"을 확정하지 않습니다.** 기존 로그 예:

```
워커 로그  [bfr-bund-de-en] done. Total saved: 3     ← 크롤러가 처리한 건수
작업 기록  saved_count = 0                            ← 새로 늘어난 행 수
```

크롤러가 실제로 무엇을 처리했는지는 워커 로그로 확인합니다:

```bash
docker compose -f delivery/docker-compose.yml logs --since 10m worker | grep -v worker_heartbeat
```

로그를 볼 때는 `-t`(타임스탬프)를 함께 쓰는 것을 권장합니다. `docker compose up -d`는
종료된 기존 컨테이너를 **재시작**하는 경우가 있어, 이전 실행의 로그가 버퍼에 남아 방금
실행한 작업의 로그처럼 보일 수 있습니다.

`fe`/`be` 포트는 `127.0.0.1`에만 바인딩됩니다(외부 인터페이스에 노출되지 않음). 기동 후
서버에서 직접, 또는 SSH 포트포워딩 등을 통해 `http://127.0.0.1:${FE_PORT:-3000}` 으로 접속합니다.
외부에 노출하려면 앞단에 reverse proxy(nginx, Caddy 등)를 두고 TLS를 종단하는 것을 권장합니다.
