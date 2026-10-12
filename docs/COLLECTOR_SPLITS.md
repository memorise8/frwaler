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

1. **실제 사이트 요청 0건**이다. 목록 순서, 마지막 쪽, API 현재 응답/필터, 총량, robots 허용, 전량 수집·실제 저장·중복 없음은 확인 못 함이다. 등록 코드의 URL/필터/페이지 시작값을 후보로 옮겼다. WordPress 6곳의 400 종료는 아래 '리뷰 수정 1'처럼 `json_code` 가 `rest_post_invalid_page_number` 인 응답으로만 한정했고 400 일반을 허용하지 않았다. 틀의 정책 코드는 약화하지 않았다.
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

## 리뷰 수정 (collect-merged-20261013 6b0da63 병합 후)

독립 리뷰(REQUEST CHANGES) 지적을 항목별로 수정했다. 각 항목은 실패하는 시험을 먼저 쓰고 수정 전 실패를 확인한 뒤 고쳤다. 기존 crawl()·기존 메서드·식별값·저장 필드와 `crawler/enumeration/` 틀은 바꾸지 않았다.

### 리뷰 수정 1 — WordPress 마지막 쪽 초과 400 종료와 X-WP-Total 대조

WordPress 6곳 plan 의 `listing` 에 다음 두 항목을 넣었다. 둘 다 틀의 `wordpress` preset(`crawler/enumeration/strategies.py` PRESETS)이 쓰는 형식 그대로이며, preset 자체는 정렬 파라미터(`order=asc`)를 바꾸므로 쓰지 않았다.

- `"end_statuses": [{"status": 400, "json_code": "rest_post_invalid_page_number"}]` — 본문 JSON 의 `code` 가 정확히 이 값일 때만 빈 끝 페이지로 본다. 다른 400(예: `rest_invalid_param`, HTML 400)은 `listing_failed:unverified_error_status_400` 로 incomplete 가 된다(시험으로 확인).
- `"total": {"type": "header", "name": "X-WP-Total"}` — last_page_backward 가 시작 쪽에서 읽은 총건수를 목록 건수와 대조하여, 다르면 `complete_with_mismatch`/`reported_total_mismatch` 가 된다. 헤더가 없으면 대조 없이 진행한다(틀 동작).

기존 crawl() 이 이 응답을 종료 신호로 쓰는 근거(코드 정적 확인, 실제 응답은 확인 못 함):

|사이트|기존 crawl() 의 처리|
|---|---|
|climateinstitute-ca-newsroom|`page > 1` 이고 `code == "rest_post_invalid_page_number"`, `data.status == 400` 일 때만 `_mark_exhausted()` (curl `-skL`, 상태코드 무시하고 본문 판독)|
|nasa-gov-2025-news-releases|curl `-sk`(-f 없음)로 400 본문 JSON 객체를 받고 `not isinstance(items, list)` 면 "No items ... Done" 으로 종료|
|research-imt-nord-europe-fr-news|같은 방식. 주석 "WP REST API returns a JSON object (not array) when page is out of range"|
|ukdataservice-ac-uk-2026|`resp.status_code == 400` 이면 "WP returns 400 when page > total pages" 로 종료(400 전체를 종료로 봄 — 새 plan 은 json_code 로 더 좁힘)|
|srnl-gov-newsroom|JSON 객체면 종료. 주석 "WP REST API returns {"code": "rest_post_invalid_page_number"} on overflow"|
|minfin-gov-gr-grafeio-typou|**근거 없음**: `_curl_get` 은 HTTP 400 이상을 실패로 보고 3회 재시도 뒤 None → "failed; stopping". 기존 crawl() 은 보통 `len(posts) < 100` (짧은 쪽)에서 먼저 끝나 이 응답에 닿지 않는다. 같은 WordPress REST 코어(`/wp-json/wp/v2/posts`)의 공통 동작에 근거해 넣었으며 실제 응답은 확인 못 함|

시험: `tests/test_enumeration_split_wordpress_end.py` — plan 값 검사, 실제 `SiteRunner.run` 합성 실행(400 종료 → complete, X-WP-Total 불일치 → complete_with_mismatch, 다른 400 → incomplete). 수정 전 26개 subTest 실패를 확인했다.

### 리뷰 수정 2 — minfin page 바인딩과 page 없는 첫 쪽 비교

minfin plan 의 목록 바인딩 `{"source":"query","path":["page"],"type":"int"}` 은 목록 URL 의 `page` 값을 읽는다(기존 crawl() 의 metadata `api_page` 를 같게 만들기 위함). last_page_backward 는 탐색 전에 page 파라미터를 뺀 URL 을 한 번 더 읽어 시작 쪽과 비교하는데, 그 URL 에는 `page` 가 없어 `adapter_contract:missing_path:["page"]` 로 config_error 가 되었다.

- 수정: minfin plan 에 `"last_page": {"check_unparameterized_first_page": false, ...}` 를 명시했다. WordPress REST 에서 `page=1` 은 정의상 첫 쪽이며, 기존 crawl() 도 항상 `page=` 를 붙여 요청한다. 대가로 이 사이트는 'page 없는 URL = 시작 쪽' 교차 확인을 하지 않는다.
- harness 원인: `tests/collector_split_helpers.py` 의 `pipeline()` 은 page 파라미터가 붙은 목록 URL 하나만 파싱하고 전략(첫 쪽 비교)을 실행하지 않았다. 이제 last_page_backward 이고 비교가 켜져 있으면 page 없는 URL 로도 같은 본문을 파싱하여 같은 키가 나오는지 확인한다. 수정 전 minfin 의 사이트별 시험 4개가 이 검사에서 오류로 재현되었다.
- 추가로 실제 `SiteRunner.run` 합성 실행(`run_split`)으로 minfin 이 complete 로 끝나는지 확인한다.

### 리뷰 수정 3 — 목록 메서드 예외가 runner_error 로 끝나던 문제

새 `_enumeration_items` 들이 `ValueError`/`ET.ParseError` 를 던져 runner 가 분류하지 못하고 `runner_error` 로 끝났다. 수정 후 결과(실제 `SiteRunner.run` 합성 실행, `tests/collector_split_helpers.py` 의 `SplitRunChecks`):

|경우|사이트|수정 후 결과|
|---|---|---|
|점검 HTML 페이지(200)|JSON 7곳|`incomplete` / `listing_failed:listing_parse_error:invalid_json` (틀의 기존 판정)|
|점검 HTML 페이지(200)|brgm-fr-fr, health-ni|`incomplete` / `listing_failed:listing_parse_error:listing_structure_missing` (plan 의 `listing_marker: "main"` 판정)|
|점검 HTML 페이지(200)|home-cern-news|`incomplete` / `listing_failed:listing_parse_error:invalid_xml`|
|200 JSON 오류 객체(`{"code":"rest_no_route",...}`)|WordPress 6곳, data-mfe|`incomplete` / `listing_failed:listing_parse_error:items_not_a_list_of_objects`|
|XML 선언 앞 빈 줄|home-cern-news|`incomplete` / `listing_failed:listing_parse_error:invalid_xml`|

- brgm-fr-fr: 카드가 없을 때의 자체 `main` 검사·예외를 없애고 빈 목록을 돌려주어 plan 의 `listing_marker: "main"` 판정에 맡겼다. `_make_soup` 이 None 이면 `PermanentFailure("listing_parse_error:html_unparseable")`.
- WordPress 6곳·data-mfe·home-cern-news: 모양이 틀리면 `crawler.enumeration.http.PermanentFailure("listing_parse_error:...", url)` 를 던진다(메서드 안에서 지연 import, 모듈 최상위 import 는 추가하지 않음). 목록 URL 을 이유 기록에 남기려고 plan 의 `list_args` 끝에 `"url"` 을 추가했다.
- **ContractError 를 쓰지 않은 이유**: 틀(`runner._stop_status`)은 `ContractError`(= `AdapterConfigError`)를 `config_error` 로 분류한다. 그대로 두면 200 JSON 오류 객체는 `config_error` / `adapter_contract:list_result_not_list` 가 되고, CERN 의 빈 줄 XML 은 `[]` 를 돌려도 html.parser 가 `<channel>` 표지를 찾아 '정상 빈 쪽'(끝 후보)이 된다. 요구한 incomplete(listing_parse_error 계열)는 틀이 목록 파싱 오류에 쓰는 `PermanentFailure("listing_parse_error:...")` 와 같은 분류로만 얻을 수 있어 그 형식을 썼다. 틀은 수정하지 않았다.
- data-mfe-govt-nz-documents 는 리뷰 목록에 없었으나 같은 `raise ValueError` 결함이 있어 같은 방식으로 고쳤다.
- 시험: `SplitChecks.test_valid_empty_page_and_error_page_are_distinct` 가 `ValueError/TypeError` 를 정상으로 받아들이던 부분(이전 193행)을 없애고 `PermanentFailure(listing_parse_error:...)` 만 허용한다. 새 `SplitRunChecks.test_error_listing_ends_incomplete_as_listing_parse_error` 가 세 경우를 실제 runner 로 확인한다. 수정 전 21개 오류(ValueError/ParseError)를 확인했다.

### 리뷰 수정 4 — BRGM 상세 파서 일치(html5lib)

brgm-fr-fr plan 은 `detail_method: "_build_paper"` 에 틀의 `"soup"` 토큰(html.parser)을 넘겼으나, 기존 crawl() 은 `self._make_soup`(html5lib → lxml → html.parser 순)으로 파싱한 soup 을 `_build_paper` 에 넘긴다. 손상된 HTML 에서는 결과가 달라진다.

- 얇은 메서드 `_enumeration_detail(raw_html, list_meta, detail_url)` 를 추가했다: `self._make_soup(raw_html, context=f"detail {detail_url}")` 로 파싱하고, None 이면 crawl() 처럼 건너뛰며(None), 아니면 기존 `_build_paper` 를 그대로 호출한다. 기존 메서드는 바꾸지 않았다.
- plan: `"detail_method": "_enumeration_detail", "detail_args": ["html", "item", "url"]` (`url` 은 요청한 상세 URL = crawl() 의 detail_url).
- 시험 `test_malformed_detail_html_is_parsed_like_crawl`: 닫히지 않은 `<p>` 가 있는 상세 fixture 로 html5lib/html.parser 의 초록이 실제로 다름을 먼저 확인하고, 원본 crawl() 저장 인자와 새 경로의 정제 전·후 Paper 가 같음을 대조한다. 수정 전 abstract 차이(`\n\n` 대 공백 연결)로 실패를 확인했다.
