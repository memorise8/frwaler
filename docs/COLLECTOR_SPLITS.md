# 수집기 순수 메서드 분리 — 완료

통과는 합성 응답·plan 규칙·어댑터 연결 확인이며 실제 사이트 전체 수집 성공을 뜻하지 않는다.

기준 브랜치 collect-merged-20261013, 커밋 584fe39903986475588ebb8f8d33d9f5b6097d10. 원본의 미커밋 변경은 포함하거나 수정하지 않았다. 새 워크트리 frwaler-collector-splits / collector-splits-20261013에만 작성한다.

## 선정

직전 B 38곳 중 10곳, 모두 납품 대상이다. 선정 우선순위는 (1) 다중 API/POST/내부 조회 없는 순수 변환으로 표현 가능 (2) 이미 분리된 파서 재사용 또는 crawl의 문서 조립 블록 복사만 필요 (3) 기존 저장 기록/총량 자료가 있어 회귀 효과를 검토할 수 있는 곳이다. 절대적인 난이도 최저 순위가 측정된 것은 아니며 정적 코드 검토에 따른 상대적 선정이다. 실제 영향 건수는 확인 못 함이다.

`COLLECTOR_SPLITS_SELECTION.csv`에 B 38곳 전부의 선정/후순위 사유와 납품 여부·기록 규모를 보존한다. NASA의 전체 posts 추정 35,278건을 2025 press-release 범위 회복 건수로 사용하지 않는다. Minfin의 미측정 0을 데이터 없음으로 해석하지 않는다. UK Data Service의 마스터 576과 용량표 560도 이번 새 측정이 아니다.

AFP는 B가 아닌 C이며 내부 curl 때문에 제외한다. MI는 B지만 3개 유형과 PDF 조회 분기가 있어 제외한다. CPB/PBL/HRB/NIDI 등 내부 요청 결합 사이트는 선택하지 않았다. HTML 상세 한 번은 틀 Fetcher가 수행하고 새 메서드는 그 응답만 처리하는 SRNL/Health/BRGM/MfE와 구별한다.

## 범위

기존 crawl()과 기존 메서드/상수는 변경하지 않는다. 짧은 초록 필터를 새 Paper 메서드에도 복사하여 저장 대상 확대를 피한다. 다만 반환 None은 현재 틀에서 의도된 skip이 아니라 empty_item 실패로 처리되어 연속 실패/완료 검토 대상이 될 수 있다. 틀을 고치거나 필터를 생략하지 않는다.

사이트 요청과 DB 연결 없이 원본 crawl의 _save_paper를 메모리 캡처로 교체하여 모든 필드 및 paper_identity를 대조한다. 지정된 전체 틀 시험에 원래 들어 있는 컨테이너 임시 SQLite만 사용하며 새 회귀 시험은 sqlite/psycopg/socket/subprocess 호출을 차단한다.

기준 전체 시험: 361개 실행, 실패 0, 건너뜀 1(PostgreSQL 전용). 최종 결과는 작업 후 추가한다.

## 결과

10/10 사이트의 plan 규칙 검사와 실제 등록 클래스를 주입한 Adapter 생성을 통과했다. `docs/examples/collector-splits-plans.json`에 10개 후보를 모았다. 개별 plan은 `docs/examples/collector-splits/`, 사이트별 기계 판독 결과는 `COLLECTOR_SPLITS_RESULTS.csv`에 있다. 후보의 정상적인 실제 사이트 응답/페이지 종료/전량 저장을 검증했다는 뜻은 아니다.

|사이트|로컬 커밋|추가 메서드|사이트별 시험|
|---|---|---|---:|
|climateinstitute-ca-newsroom|`86b39a2c1e39`|_enumeration_items, _enumeration_paper|7 통과|
|nasa-gov-2025-news-releases|`70cf211ec8af`|_enumeration_items, _enumeration_paper|7 통과|
|ukdataservice-ac-uk-2026|`6732847d8c4d`|_enumeration_items, _enumeration_paper|7 통과|
|minfin-gov-gr-grafeio-typou|`de4e5150caca`|_enumeration_items, _enumeration_paper|7 통과|
|research-imt-nord-europe-fr-news|`d4174ac8f90d`|_enumeration_items, _enumeration_paper|7 통과|
|home-cern-news|`a636b4fd990a`|_enumeration_items, _enumeration_paper|7 통과|
|health-ni-gov-uk-publications|`efae0f785fd5`|_enumeration_paper|7 통과|
|brgm-fr-fr|`e4fe3b54c83c` + `a03b66c`|_enumeration_items|8 통과|
|srnl-gov-newsroom|`76e055879106`|_enumeration_items, _enumeration_paper|7 통과|
|data-mfe-govt-nz-documents|`8190942a9b12`|_enumeration_items, _enumeration_paper|7 통과|

공통 시험 기반 커밋: `3f7cf58`(메모리 저장 캡처/선정 근거), `56c65cd`(정제 전 Paper 전체 필드 및 정상 빈 목록/오류 페이지 구분). 위 사이트별 10개 커밋에는 해당 수집기 추가 메서드·개별 plan·합성 fixture·시험 모듈만 포함했다. 마지막 문서 커밋은 이 보고서와 합본 plan·결과 CSV/JSON을 기록한다. 푸시·병합·배포하지 않았다.

## 시험 근거

- 수정 전 기준: **361개 실행, 실패 0, 건너뜀 1**. 현재 통합 브랜치의 실제 기준이며 과거 291/189개 수치를 재사용하지 않았다.
- 신규 시험을 먼저 작성한 후 최종 red: 60개 실행, 기존 메서드 보존 10개 통과, 새 메서드 부재로 50개 오류. 최초 harness의 Python 버전별 AST 직렬화 차이는 원문 SHA-256 대조로 고쳤고 이 최종 red는 그 수정 후 결과다.
- 구현 후 최초 신규 60개 통과. 원본 저장 직전 Paper와 **정제 전** 새 메서드 Paper 전체 dict도 대조하도록 보강했고, 정상 빈 목록과 오류 페이지를 구별하는 10개 시험을 추가했다.
- 최종 전체: **432개 실행, 실패 0, 건너뜀 1** = 기존 361 + 신규 71. 마지막 실행 76.696초. 건너뜀은 TEST_PG_ADMIN_DSN 없는 PostgreSQL 전용 시험이다.
- 사이트마다 합성 2항목(총 20항목)에서 기존 crawl이 `_save_paper`에 넘기는 모든 필드와 새 Paper를 비교하고 `paper_identity`도 대조했다. 제목/날짜/PDF/메타데이터가 포함되며 날짜 없음·일부 fallback은 fixture로 다뤘다. 모든 실제 입력 변형을 증명한 것은 아니다.
- 사이트별 기본 7개(BRGM은 추가 1개, 총 8개): Paper/식별값 동등성, 짧은 초록 거부, 상세 리다이렉트 시 동일성, 호출자 입력 불변, 모든 기존 클래스 메서드 원문 보존, 잘못된 목록/식별 입력 거부, 정상 빈 목록과 오류 페이지 구분. 상세 요청 없는 사이트의 리다이렉트 시험은 추가 요청이 생기지 않는 것도 확인한다.
- 신규 시험은 생성자와 DB 없이 객체를 만들고 기존 crawl의 HTTP를 메모리 응답, 저장을 메모리 캡처로 바꾼다. 실제 socket/DNS/subprocess/SQLite/psycopg 연결 시도는 실패시키고, 예외가 내부에서 삼켜져도 시도 기록으로 시험을 실패시킨다. Adapter의 상세 전송 대역은 선언된 fixture URL 이외 요청을 거부한다.
- 기준 커밋과 비교하여 추가 `_enumeration_*` 메서드만 AST에서 제거하면 **모듈 전체 AST가 원본과 동일**하다. 기존 메서드 원문 SHA-256 대조와 함께 crawl·기존 파서·상수·모듈 보조 함수 불변을 확인했다. `crawler/enumeration/`, 저장·날짜 규격·ID·요청 게이트·robots·사설주소 차단 코드는 수정하지 않았다.

로그는 `.audit/logs/`, SHA-256과 결과는 `docs/COLLECTOR_SPLITS_AUDIT.json`에 기록했다. 신규 함수에는 외부 조회/DB/저장/대기 호출이 없다. 기존 전체 시험의 원래 컨테이너 내부 임시 SQLite fixture만 사용했다. 운영·고객 DB 및 PostgreSQL 서버 접속, 새 DB 서버 생성은 없었다.

## 남은 제한

1. **실제 사이트 요청 0건**이다. 목록 순서, 마지막 쪽, API 현재 응답/필터, 총량, robots 허용, 전량 수집·실제 저장·중복 없음은 확인 못 함이다. 등록 코드의 URL/필터/페이지 시작값을 후보로 옮겼다. API가 WordPress라는 사실만으로 400을 정상 종료로 허용하지 않았으며 정책 코드를 약화하지 않았다.
2. 짧은 초록 필터는 10곳 모두 등록 변환과 같은 기준으로 보존한다(BRGM은 기존 `_build_paper` 내부 필터 재사용). **None을 틀이 empty_item 실패로 처리하는 차이**가 남는다. 연속 실패 중단/검토 완료 규칙이 작동할 수 있어 원본 crawl의 조용한 skip과 실행 상태까지 같다고 주장하지 않는다.
3. 새 메서드는 기존 문서 조립을 복사한다. 원본 crawl은 그대로이므로 향후 원본 로직만 변경하면 둘이 어긋날 수 있다. 원문 보존·메모리 저장 대조 시험을 함께 갱신해야 한다. 날짜 추정이나 결함 보정은 이번에 하지 않았다.
4. NASA의 2025 기간과 press-release 타입, Minfin category 62, IMT lang=en/category 75, BRGM Communiqué de presse 필터를 그대로 유지한다. 전체 사이트 데이터로 범위를 넓히지 않았다. MfE는 목록 ID로 원래 API 상세 URL을 만들고 Paper에는 원래 공개 문서 URL을 유지한다.
5. Health/BRGM의 `main`, CERN RSS의 `channel` 표지는 합성 구조에서만 시험했다. 실제 빈 페이지/오류 페이지 표지로 충분한지 확인 못 함이다. 단순 HTML main 존재만으로 실제 종료를 보증하지 않는다. 목록 리다이렉트와 사이트 측 정렬 변동도 미검증이다.
6. 상세 리다이렉트 후 응답 URL을 ID로 바꾸지 않는다. 등록 코드와 같은 목록 URL/명시 ID를 사용해 합성 응답의 동일성을 확인했지만, 실제 사이트의 모든 redirect/ID 충돌 가능성을 검증한 것은 아니다.
7. 최초 기준 워크트리의 미커밋 변경 및 작업 중 추가된 원격/다른 브랜치 변경은 포함하지 않았다. `584fe39` 기준의 격리된 로컬 변경이다.

## 오프라인 재현

워크트리 `.audit/run-tests.sh`는 Python 3.12 slim 임시 `enum-test-` 컨테이너에 소스를 읽기 전용으로 마운트하고 임시 `/w`에 복사한다. 금지된 이름의 경로와 `.git`/`.audit`는 복사에서 제외한다. 기존 wheelhouse를 읽기 전용으로 마운트하여 `--network none`, `pip --no-index`로 의존성을 설치한다. 실행은 다음과 같다.

```sh
python -m unittest discover -s tests -p 'test_enumeration_*.py' -t .
```

사용 컨테이너는 모두 `--rm`이며 잔존 여부를 해당 enum-test 이름에 한해 확인한다. 로컬 venv, 실제 수집, DB 보정, 외부 발송은 수행하지 않았다.

## 최종 대조에서 추가한 BRGM 회귀

기존 BRGM은 main 밖 카드도 읽으므로 새 목록 메서드도 그 동작을 보존한다. 본문 카드는 그대로 있으나 main wrapper가 없는 합성 목록을 원본 crawl과 비교하는 시험을 먼저 추가해 8개 중 1개 오류를 재현했다(`.audit/logs/brgm-marker-red.log`). 새 목록 메서드를 보정한 로컬 커밋은 `a03b66c`다. 카드가 없는 페이지의 marker 검사는 유지한다. 최종 전체 432개에서 이 추가 회귀도 통과했다. 기존 crawl/파서는 계속 원문 그대로다.

중간 문서 커밋 `c533096`에는 431개 기준 결과가 있고, 이 최종 문서 갱신은 BRGM 추가 회귀를 반영한다. 기준 361개, 신규 71개, 합계 432개가 최종 수치다. 새 메서드의 로컬 함수 호출 연결도 정적으로 따라가 네트워크/프로세스/DB/저장/대기 호출이 없음을 대조했다(실행 시 차단 시험과 함께 보는 근거이며 일반적인 Python 보안 증명은 아니다).
