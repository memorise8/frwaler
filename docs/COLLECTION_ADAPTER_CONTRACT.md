# 등록 수집기 어댑터 계약 v1 설계

기준: e2ebfc9. 오프라인 설계·개발이며 라이브 수집·DB·배포를 수행하지 않는다. 기존 동작은 계약 미지정 시 보존한다. 이 문서는 구현 전에 작성했으며 검증 후 결과를 별도 절에 추가한다.

## 실제 거부 24곳과 관련 패턴

같은 오류 24건은 해결 가능 24곳을 의미하지 않는다. 아래 행은 실제 등록 코드 조사다. Paper는 저장용 dict를 뜻하며 목록 요약 dict와 구분한다.

| site_id | 반환형·인자·변환 및 판단 | 코드 근거 |
|---|---|---|
| afp-gov-au-news-centre | tuple(items,next_url) → dict 행; detail(raw,url) → 부분 dict; _build_paper(item,detail). **계약 후보 아님**: 상세 파서가 파일명 없는 PDF 링크에서 `_curl_head_content_disposition`(subprocess curl)을 호출해 guard가 사이트 전체를 `config_error`로 멈춘다(리뷰 M1). 코드로 우회하지 않는다. | `crawler/sites/custom/afp-gov-au-news-centre.py` crawl:129, _parse_list_page:294, _parse_detail_page:371, _build_paper:414 |
| africamuseum-be-en | 5열 tuple 행(slug,title,listed_date,snippet,source_url); detail(html,slug,listed_date,snippet,source_url) → Paper. 연결 검증 대상(아래 최종 결과 참조). | `crawler/sites/custom/africamuseum-be-en.py` _parse_list_page:115, _parse_detail_page:186, crawl:257 |
| culture-gouv-fr-espace-documentation | tuple(items,has_next); detail(html,url) → 부분 dict; _to_paper(list_item,detail). 연결 검증 대상(아래 최종 결과 참조). | `crawler/sites/custom/culture-gouv-fr-espace-documentation.py` _parse_list_page:263, _parse_detail:333, _to_paper:558, crawl:629 |
| economie-fgov-be-nl | list[dict]; detail(html,item) → 부분 dict; Paper/ID 조립은 crawl 내부. 계약만으로 해결 불가. | `crawler/sites/custom/economie-fgov-be-nl.py` _parse_listing:113, _parse_detail:172, crawl:262 |
| health-ni-gov-uk-publications | tuple(items,has_next); detail(raw,card_summary) → 부분 dict; Paper 조립 crawl 내부. 해결 불가. | `crawler/sites/custom/health-ni-gov-uk-publications.py` _parse_list_page:75, _parse_detail:106, crawl:190 |
| mimit-gov-it-it | list[str URL]; detail(html,url) → 부분 dict; Paper/ID 조립 crawl 내부. URL 정규화만으로 해결 불가. | `crawler/sites/custom/mimit-gov-it-it.py` _parse_list_page:106, _parse_detail_page:128, crawl:234 |
| climateinstitute-ca-newsroom | JSON 목록과 Paper/ID 조립 모두 crawl 내부. 순수 메서드 추출 전 해결 불가. | `crawler/sites/custom/climateinstitute-ca-newsroom.py` crawl:60 |
| academie-sciences-fr-espace-presse | tuple(items,has_next), list(soup,page_num,list_url); PDF 본문/문서 조립 결합. page 인자 지원만으로 해결 불가. | `crawler/sites/custom/academie-sciences-fr-espace-presse.py` crawl:300, _parse_list_items:346 |
| ifremer-fr-fr | tuple(items,has_next), list(soup,page_index); detail(soup,raw,list_item) → 부분 dict; Paper 조립 crawl 내부. 해결 불가. | `crawler/sites/custom/ifremer-fr-fr.py` crawl:142, _parse_list_page:295, _parse_list_node:324, _parse_detail_page:448 |
| efk-admin-ch-fr | list[dict] JSON; detail(list_record,detail_json,soup,effective_url); _to_paper(parsed). JSON/HTML 다중 상세 응답이 필요하므로 단일 응답 계약으로 보류. | `crawler/sites/custom/efk-admin-ch-fr.py` crawl:43, _parse_list_records:319, _parse_detail:344, _to_paper:441 |
| earth-prints-org | JSON objects → _embedded.indexableObject; _paper_from_item(item,list_item=None) → Paper. 상세 보강/bitstream 조회 포함 여부와 URL 매핑은 별도 검증 필요. | `crawler/sites/custom/earth-prints-org.py` _paper_from_item:299, crawl:391 |
| kit-nl-institute | JSON 배열 → _parse_item(item) → Paper/None. 등록된 순수 변환 재사용 검증 대상. | `crawler/sites/custom/kit-nl-institute.py` _parse_item:199, crawl:260 |
| mi-se-arkiv | JSON 행과 Paper/ID 조립 crawl 내부. 해결 불가. | `crawler/sites/custom/mi-se-arkiv.py` crawl:220 |
| nasa-gov-2025-news-releases | JSON 행과 Paper/ID 조립 crawl 내부. 해결 불가. | `crawler/sites/custom/nasa-gov-2025-news-releases.py` crawl:61 |
| srnl-gov-newsroom | JSON 행과 Paper/ID 조립 crawl 내부; 추출 보조 함수만 존재. 해결 불가. | `crawler/sites/custom/srnl-gov-newsroom.py` crawl:149 |
| didaktorika-gr-eadd | DSpace 목록/상세/문서 조립 crawl 내부. 해결 불가. | `crawler/sites/custom/didaktorika-gr-eadd.py` crawl:243 |
| publichealthscotland-scot-publications | 목록 추출 crawl 내부; detail(soup,url,list_title,list_desc,listed_date,listed_date_raw,categories) → Paper. 개별 인자 지원만으로 목록 연결까지 해결 안 됨. | `crawler/sites/custom/publichealthscotland-scot-publications.py` crawl:46, _parse_detail:190 |
| finance-ni-gov-uk-publications | tuple(items,has_next), detail(raw,card_summary) → 부분 dict; Paper 조립 crawl 내부. sitemap 범위/목록 payload 문제도 남음. | `crawler/sites/custom/finance-ni-gov-uk-publications.py` _parse_list_page:73, _parse_detail:104, crawl:181 |
| nesc-ie-publications | list[dict] HTML; Paper 조립 crawl 내부. sitemap 상세 변환 없음. | `crawler/sites/custom/nesc-ie-publications.py` _parse_list_html:68, crawl:166 |
| cnl-ca-news-publications | JSON/Paper 조립 crawl 내부. sitemap 상세 변환 없음. | `crawler/sites/custom/cnl-ca-news-publications.py` crawl:379 |
| home-cern-news | _parse_rss_page(raw) → list[dict] 중간 행; crawl에서 필터/추가 필드/Paper 조립. RSS 파서만 Paper라고 간주할 수 없음. | `crawler/sites/custom/home-cern-news.py` _parse_rss_page:103, crawl:197 |
| ntrs-nasa-gov-search | API 목록/Paper 조립 crawl 내부; 기존 sitemap_url은 검색 UI. 해결 불가. | `crawler/sites/custom/ntrs-nasa-gov-search.py` crawl:61 |
| mentalhealthcommission-ca-resources | JSON/Paper 조립 crawl 내부. sitemap 상세 변환 없음. | `crawler/sites/custom/mentalhealthcommission-ca-resources.py` crawl:151 |
| nidi-nl-en | list(html,page) → dict 행; detail(item)이 PDF/외부/내부 추가 요청 수행; _paper_from_item은 keyword-only 필드 다수. 네트워크 혼합은 계약으로 우회하지 않음. | `crawler/sites/custom/nidi-nl-en.py` _parse_list_fragment:378, _parse_list_soup:384, _parse_detail:480, _paper_from_item:839, crawl:899 |

연관 사례: libreria와 ncri의 tuple 반환은 list_result로 분리 가능하지만 범위/종료/URL 목록 파서 재검증이 남는다. igualdad/nida/repository-tno의 sitemap item 의존 거부는 유지한다. biblio/etera POST 목록, 경로 페이지 번호, 날짜 하한·목록 범위 판정은 이번 어댑터 계약으로 해결되지 않는다.

## 명시적 계약 제안

- `adapter.contract_version: 1`을 지정한 crawler 어댑터만 새 계약을 사용한다. 미지정은 기존 경로/거부 문자열/지문을 그대로 사용한다. 기존 등록 parser 재사용 규칙을 완화하지 않는다.
- 목록 입력은 기존 `list_method`(기존 토큰 + 필드 바인딩) 또는 `list_source: "json"`이다. JSON 직접 입력은 등록 `paper_method`가 있을 때만 허용한다. 원래 crawl이 JSON 행을 그 메서드로 넘기는 경우에만 사이트 연결로 검증한다. 인라인 문서 생성 코드를 자동 추출/복제하지 않는다.
- `list_result`는 반환값에서 목록까지의 명시 경로(문자열 dict 키/정수 배열 인덱스 배열)다. `(items,next)`는 `[0]`. `row_path`는 각 행의 API envelope를 벗기는 경로다. 추측·자동 tuple 첫 항목 선택을 하지 않는다.
- `list_row: {type:"dict"}`는 전체 필드를 보존한다. `{type:"tuple",fields:[...]}`은 정확한 길이·유일한 이름을 확인한다. `{type:"url"}`은 URL 문자열을 `{url:...}`로 만든다. list[Paper]는 dict 행이며 명시 `list_returns_paper`를 사용한다. 리스트가 아니거나 행이 다르면 전체 페이지를 거부하고 조용히 건너뛰지 않는다.
- 목록의 URL/식별값/날짜는 기존 listing.parser의 url_field/key_field/date_field로 지정한다. 원래 행을 payload에 보존한다. URL-only 목록에 DB 식별값을 합성하지 않는다. 내부 열거 key와 저장 post_number는 별개다. JSON 직렬화 불가/URL와 key가 모두 없는 행을 거부한다.
- 인자는 기존 html/raw/soup/json/url/item/resp, 새 `detail`(상세 파싱 결과), 또는 `{source:"item"|"detail"|"json"|"query",path:[...]}`다. query의 선택적 `type:"int"`는 페이지 번호처럼 명시된 정수만 변환한다. 누락·중복 쿼리 값·타입 불일치는 거부한다. 임의 표현식/eval/임포트/기본값/ID 템플릿을 허용하지 않는다. positional args와 명시 kwargs를 모두 지원하고 inspect.signature.bind로 호출 전 검증한다.
- 상세가 필요하면 기존 Fetcher.get(item.url)로 한 번 얻은 응답으로 detail_method를 호출한다. paper_method가 있으면 paper_args/paper_kwargs로 item과 detail을 넘겨 원래 변환을 수행한다. detail None은 paper 변환을 실행하지 않고 기존 실패 처리로 넘긴다. 두 번째 종류의 상세 응답이나 메서드 내부 네트워크는 자동으로 대신 실행하지 않는다.
- 모든 등록 메서드는 기존 guarded() 안에서 호출한다. 목록 또는 상세 변환 코드의 네트워크/프로세스 호출 차단을 유지한다. 스키마/서명 오류는 요청 전에 거부한다.

## ID 동일성과 거부 기준

post_number/external_id/metadata.post_number를 plan 매핑으로 새로 생성하거나 덮어쓰지 않는다. 등록된 기존 Paper 변환을 마지막 단계로 사용한다. 신규 계약의 최종 결과는 dict/None만 허용하며 site_id 불일치, URL 부재, paper_identity(clean_paper(...))의 빈 키/URL은 거부한다. 같은 합성 입력을 기존 호출 순서와 새 어댑터로 처리해 Paper와 paper_identity가 같은지 대조한다. 이는 표본 입력에서의 동등성 증거이며 모든 실제 ID의 정확성 증명이 아니다. 기존 save/중복/날짜 정규화 경로는 변경하지 않는다.

sitemap에서 item 전체/개별 필드 또는 list_returns_paper가 필요하면 거부한다. 새 계약을 통해 기존 sitemap 제약을 우회할 수 없다. 새 계약 내용이 바뀌면 새 계약 plan의 지문은 바뀌어 이전 열거 상태와 혼합되지 않는다. 계약 없는 기존 지문은 동일하다.

거부 코드는 `adapter_contract:*`로 필수 입력/반환형/행/필드/인자/문서 오류를 구별한다. 필요한 값이 없는 사이트는 생성기나 자동 기본값으로 통과시키지 않는다. 구버전에서 허용된 잘못된 값까지 일괄 강화하지 않고 opt-in 경계 안에서만 강화한다.

## 검증 계획과 완료 기준

1. 합성 데이터로 dict/tuple envelope/tuple 행/URL 행/Paper/JSON 경로, 인자·kwargs·query·detail→paper, 잘못된 타입/누락/네트워크 차단/빈 목록/ID 보존을 검증한다.
2. AFP·AfricaMuseum·Culture·KIT의 실제 등록 메서드를 DB 생성자 없이 불러 직접 호출과 adapter 결과/식별값을 대조한다. 이것은 어댑터 연결 검증이며 plan 60곳 재통과 판정이나 사이트 수집 성공 판정이 아니다.
3. 보존한 plans-v2.json 12곳과 plans-codex.json 5곳을 모두 e2ebfc9 구현과 새 구현에 넣어 검증·지문·목록·문서/ID 동작을 오프라인에서 비교한다. 각 plan의 등록 코드/선택자는 그대로 쓴다. 커버하지 못한 경로를 명시한다.
4. 신규 어댑터 테스트는 메모리 응답과 conn=None을 사용한다. 사용자가 지정한 기존 전체 테스트에는 컨테이너 내부 임시 SQLite 가짜 데이터가 포함된다. 운영/고객 DB 및 PostgreSQL 인스턴스에는 접속하지 않으며 서버를 만들지 않는다. 사이트 라이브 요청은 하지 않는다. 워크트리 collect-adapter-20261012에만 로컬 커밋한다.


## 구현 결과와 검증 범위

**통과는 plan 형식·틀 규칙 및 등록 어댑터 연결 검증 통과이며, 끝까지 받을 수 있다는 증거가 아니다.**

새 계약은 `crawler/enumeration/contracts.py`로 분리했다. 기존 Adapter의 분기는 opt-in 지점에 한정했고, plan 검증·새 계약 지문과 ListingParser의 엄격한 행 검사/명시 JSON 빈 목록 처리만 추가했다. 개별 등록 수집기·식별 규칙·날짜 정규화·중복 점검·Fetcher·사설주소 검사·robots·요청 게이트 코드는 바꾸지 않았다. 계약 오류는 `AdapterConfigError` 하위 타입으로 전파되어 사이트가 `config_error`로 중단되며, 반환형 불명 항목을 건너뛰고 완료 처리하지 않는다.

### 24곳 재판정

이전의 거부 24곳 모두 **단순 site_plan 검사 자체는 통과했고**, Adapter 생성에서 같은 오류로 거부됐다. 따라서 이번 비교도 `site_plan(registered 포함) + 실제 등록 클래스의 Adapter 생성/서명 확인`을 한 묶음으로 사용한다. 이 기준으로 기존 0/24 → 신규 계약 후보 **3/24 통과, 21/24 거부 유지**다(af1df09의 4/24에서 AFP를 리뷰 M1로 제외). 두 집합은 같은 24개 사이트를 비교한 것이며 기타 관련 사이트 수와 합산하지 않는다.

| 새 계약 후보 | 실제로 재사용한 경로 | 확인된 근거 | 남은 제한 |
|---|---|---|---|
| africamuseum-be-en | 5열 tuple을 이름 붙인 dict로 변경 → 상세 메서드에 원래 scalar 인자 전달 | 실제 목록 tuple/상세 변환의 Paper 전체/identity 동일, 합성 항목 1건 | 마지막 쪽·원천 총량·실제 콘텐츠 미검증 |
| culture-gouv-fr-espace-documentation | 목록 tuple의 items → `_parse_detail(html,url)` → `_to_paper(list_item,detail)` | 실제 메서드 직접 호출과 Paper 전체/identity 동일, 합성 항목 1건 | 후보 marker가 item selector라 실제 빈 페이지의 종료 판정은 추가 확인 필요 |
| kit-nl-institute | JSON 목록 배열 → 등록 `_parse_item(item)` | 원래 JSON 행 변환과 Paper 전체/identity 동일, 합성 항목 1건; 상세 요청 없음 | 실제 API 필터 범위·정렬/경계·원천 총량 미검증 |

위 숫자는 `tests.test_enumeration_adapter_sites.ActualSites.test_rejected_24_reclassification`가 원래 24개 입력 전체를 검증해 계산하며, 단순한 효과 추정이 아니다. 실제 메서드 대조는 같은 테스트 모듈의 세 사이트 테스트이며, AFP 제외 근거는 `test_afp_not_a_candidate`가 검사한다. 구체 후보는 `docs/examples/adapter-contract-plans.json`, 원래 거부 입력은 `tests/fixtures/enumeration_adapter/rejected-24.json`에 보존했다. 후보를 원격 시범·운영 plan에 적용하지 않았다.

나머지 21곳(AFP 포함, AFP는 후보 plan에서 빠져 원래 입력으로 검사)의 공통 반환 사유는 여전히 `custom crawler adapter needs detail_method, paper_method or list_returns_paper`다. 첫 표에 실제 반환형·인자·변환 위치와 미해결 사유를 각각 적었다. 특히 다음을 억지로 통과시키지 않았다.

- Paper/ID 조립이 crawl 안에 있는 사이트: 등록 메서드가 없으므로 raw JSON/RSS 행을 Paper라고 선언하지 않았다. CERN RSS도 category/department/metadata 조립이 crawl에 있어 그대로 목록을 저장하는 것은 동등하지 않다.
- Earth-Prints는 `_fetch_item_detail(uuid)`로 `embed=bundles/bitstreams` 상세를 받은 뒤 `_paper_from_item(item,list_item)`을 실행한다(442~446행). 목록 내 객체만 전달하는 방식은 첨부·메타데이터 보강을 생략하므로 통과 수에 넣지 않았다.
- EFK는 JSON·HTML의 여러 응답, NIDI는 상세 함수 내부의 추가 네트워크/PDF 처리가 필요하다. 선언적 인자 바인딩으로 네트워크를 우회하지 않는다.
- Public Health Scotland는 상세의 개별 필드 인자는 표현할 수 있지만 순수 목록 메서드가 없다. 서명만 연결되는 것을 전체 어댑터 통과로 세지 않는다.
- sitemap item payload 의존, 날짜 하한, POST 목록과 경로 페이지 방식, 사이트 범위 문제는 별도다. 이를 고치면 몇 곳이 더 통과할지는 확인하지 않았다.

### 기존 plan 호환성

12곳+5곳 모두 보존한 원본 plan을 e2ebfc9 구현과 새 구현에 넣어 **plan 검증 결과와 지문, 실제 등록 클래스에서의 어댑터 호출 인자·반환값·최종 Paper/identity, 상세 요청 호출 수**가 같은지 대조한다. 기준 구현은 e2ebfc9의 adapters/parsing/plan 파일을 테스트 fixture로 보존했다. 등록 파서 호출에는 동일한 합성 반환값을 주는 spy를 사용하여 plan별 어댑터 경계 동작을 비교한다. 이는 17개 사이트 실제 HTML의 파싱 정확성이나 전체 수집 성공 회귀검증은 아니다. 기존 전체 테스트와 신규 네 사이트 실제 파서 대조를 별도로 실행한다.

### 알려진 한계

- 실제 사이트 HTTP 요청은 0건이다. 설치 단계의 pip 패키지 내려받기는 사용자 지정 Docker 명령에 포함된다. 테스트의 전송은 합성 응답/가짜 전송이다.
- 신규 테스트는 DB 연결 없이 실행하며 네 사이트 실메서드 테스트에서 socket/DNS/SQLite 연결 시도를 실패하도록 막았다. 기존 전체 테스트의 임시 SQLite는 컨테이너 내부의 가짜 데이터다. PostgreSQL 전용 테스트는 TEST_PG_ADMIN_DSN 없이 건너뛴다.
- 새 계약은 등록 코드의 순수 파싱/문서 변환 메서드를 재사용한다. crawl 바깥으로 필터·상태·네트워크·문서 조립 코드를 자동 이식하지 않는다. 이미 등록된 메서드 자체의 파싱 누락이나 ID 의미 오류까지 교정하지 않는다.
- `guarded()`는 기존 네트워크 tripwire이며 보안 샌드박스가 아니다. 기존에 연결된 소켓/native I/O 등 기존 한계도 유지된다. 임의의 비검토 메서드를 plan에서 호출해도 안전하다고 보증하지 않는다.
- 실제 빈 페이지/총건수/전체 납품 scope/첨부/증분/저장 동등성을 확인하지 않았고 수집 가능 사이트 수로 일반화하지 않는다. 이번 산출물은 로컬 설계·코드·오프라인 근거다.

### 최종 실행 결과

- Docker 전체 검사: **236개, OK, 건너뜀 1개**(55.755초). 기준 186개 유지 + 신규 50개. 건너뜀은 `TEST_PG_ADMIN_DSN` 없는 PostgreSQL 전용 검사다.
- 로컬 커밋: 설계/기존 plan 보존 `6e6c008`, 계약 구현/테스트 `84b8ca0`, 계약 오류 즉시 중단 및 추가 검사 `b87b7f5`. 최종 검사는 `b87b7f5` 코드로 실행했다. 이후 변경은 이 결과 문서·JSON·로컬 로그 제외 설정뿐이다.
- 기준 워크트리 HEAD는 `e2ebfc9`이며 해당 enumeration/설계 파일의 미커밋 변경이 없다. 새 브랜치의 개별 수집기/ID 구현 변경도 없다.
- 사용한 네 `enum-test-` 컨테이너는 모두 `--rm`으로 제거되었고 개별 이름으로 부재를 확인했다. 로컬 venv, 배포·푸시·병합은 사용하지 않았다.
- 기계 판독용 결과와 사이트별 판정은 `docs/examples/adapter-contract-audit.json`에 기록했다. 최종 원문 로그는 이 워크트리의 `tests/fixtures/enumeration_adapter/test-run-verified.log`에 남겼으며 반복 실행 로그는 git 제외 대상이다. JSON에 최종 로그 SHA-256을 기록했다.

실행 명령(호스트 shell에서 로그만 리다이렉트):

```sh
docker run --rm --name enum-test-adapter-20261012-verified \
  -v /mnt/raid/ruci_workspace/frwaler-collect-adapter:/src:ro \
  -e PYTHONDONTWRITEBYTECODE=1 python:3.12-slim bash -c \
  'cp -r /src /w && cd /w && pip install -q -r delivery/requirements.txt -r delivery/vendor/crawler-runtime-requirements.txt && python -m unittest discover -s tests -p "test_enumeration_*.py" -t .'
```

## 리뷰 결함 수정 (브랜치 collect-adapter-fix-20261012, 기준 af1df09)

아래 규칙은 모두 `adapter.contract_version`이 있는 사이트에만 적용된다. 계약 없는 plan의 경로·상태·지문은 바뀌지 않는다.

### 계약 단계 실패의 사이트 상태 (H1)

- 계약 사이트에서 등록 상세/Paper 메서드가 예외를 내거나(`failed_permanent:parse_error:<예외형>`), 결과가 비어(`failed_permanent:empty_item`) 항목이 원장에 남으면 `adapter_contract_item_failed` 이벤트와 함께 "계약 단계 실패"로 센다. 정상 항목이 나오면 연속 횟수는 0으로 돌아간다.
- 연속 `max_consecutive_contract_failures`(기본 5, 양의 정수)건이면 `StrategyStop("config_error", "adapter_contract_failures_consecutive:<마지막 사유>")`로 사이트를 멈춘다. 바인딩/필드명 불일치처럼 모든 항목이 실패하는 경우가 이 규칙에 걸린다. 연속 저장 오류(`max_consecutive_save_errors` → `config_error`)와 같은 방식이다. 멈춘 배치는 체크포인트(mark_listed/update_segment)를 남기지 않는다.
- 연속은 아니지만 원장의 계약 단계 실패가 (saved+실패 전체) 대비 `max_contract_failure_ratio`(기본 0.0, [0,1))를 넘으면 열거가 끝나도 `complete`가 아니라 `complete_with_review`(`adapter_contract_failures:<건수>`)로 강등한다. 기본값 0.0이면 한 건이라도 있으면 검토 대상이다.

### 항목 데이터 오류와 스키마 오류 구분 (M4)

- `ContractError`는 `code`를 가진다. 항목 처리(materialize) 중 나온 코드 중 데이터성 코드 `missing_path`, `paper_identity_missing`, `paper_url_missing`만 그 항목을 `failed_permanent:adapter_contract:<코드>`로 원장에 남기고 계약 단계 실패로 센다(H1 연속/비율 규칙 적용 → 연속이면 `config_error`, 산발이면 `complete_with_review`). 식별값·URL을 대신 만들지 않는다.
- 그 밖의 코드(`paper_not_dict`, `paper_site_mismatch`, `paper_identity_type`, 인자/서명/스키마 오류 등)와 계약이 아닌 `AdapterConfigError`는 종전대로 즉시 사이트를 `config_error`로 멈추고 원장·체크포인트를 남기지 않는다.
- 목록 단계(`parse_list`/ListingParser)의 계약 오류는 페이지 단위 판단이므로 이 구분 없이 종전대로 사이트를 멈춘다.

### `url` 토큰의 의미 (H2)

- 계약 사이트의 상세/Paper 단계에서 `url`은 **요청 전 item URL**(`Item.url`, 등록 crawl()이 상세 파서와 `_build_paper`/`_to_paper`에 넘기는 `item_url`과 같은 값)이다. 리다이렉트 뒤 최종 URL로 바뀌지 않는다. 같은 값을 명시하려면 `request_url`을 쓴다.
- 리다이렉트 뒤 응답 URL(`resp.url`)이 필요하면 `final_url`을 명시한다. 기본 후보 plan은 등록 수집기와 같도록 `url`(=item URL)을 사용한다.
- 목록 단계에는 item이 없으므로 `request_url`은 거부(`adapter_contract:invalid_argument_token:request_url`)하고, `url`/`final_url`은 목록 응답 URL(`resp.url`)이다. 목록 페이지가 리다이렉트되면 등록 crawl()이 넘기는 요청 페이지 URL과 다를 수 있다(상대 링크 해석 기준만 달라짐). 이 차이는 남은 제한이다.
- 계약 없는 기존 어댑터 경로의 `url` 토큰(`resp.url`)은 그대로다.

### 상세 메서드 내부 프로세스 호출 사이트 제외 (M1)

- AFP(`afp-gov-au-news-centre`)의 등록 `_parse_detail_page`는 PDF 링크에 파일명이 없으면 `_curl_head_content_disposition`으로 curl을 실행한다. 계약 경로에서 이 호출은 guard에 막혀 `AdapterUngatedNetwork` → 사이트 전체 `config_error`가 된다. 따라서 AFP는 통과 후보에서 빼고 `docs/examples/adapter-contract-plans.json`에서 삭제했다. 재판정은 3/24 통과, 21/24 거부다(`adapter-contract-audit.json` 갱신).
- guard를 완화하거나 HEAD 요청을 계약 경로가 대신 수행하는 우회는 하지 않았다. AFP를 계약으로 연결하려면 등록 수집기 쪽에서 네트워크 없는 상세 파서를 분리해야 한다(이 작업 범위 밖).
- 남은 후보 중 AfricaMuseum·Culture·KIT의 등록 목록/상세/Paper 메서드에서는 subprocess/세션 호출을 찾지 못했다(코드 검색 기준, 모든 입력에 대한 증명은 아님).

### 남은 제한: crawl() 내부의 짧은 초록 필터 (M2, 코드 변경 없음)

- Culture(`MIN_ABSTRACT_CHARS`=50)·KIT(`_ABSTRACT_MIN_CHARS`=50)·AFP(`_MIN_ABSTRACT_CHARS`=50)의 짧은 초록 건너뛰기는 등록 crawl() 루프 안에 인라인으로 있고 분리된 메서드가 없다. 계약 경로는 crawl()을 이식하지 않으므로 이 필터가 없고, 초록이 짧은 문서도 저장 대상이 된다. 즉 **계약 경로의 수집 범위는 등록 수집기보다 넓을 수 있다.** 필터를 계약 쪽에 복제하지 않았다(`test_short_abstract_not_filtered_by_contract`가 이 차이를 고정해 둔다).
- AfricaMuseum은 같은 필터(`_MIN_ABSTRACT`=100)가 등록 상세 메서드 안에 있어 계약 경로도 `None`을 받는다. 다만 계약 경로는 의도된 건너뜀과 파서 실패를 구별할 수 없으므로 이 항목은 `empty_item`으로 원장에 남고 H1 규칙의 계약 단계 실패로 센다(연속 5건이면 `config_error`, 산발이면 `complete_with_review`).
- 필터를 맞추려면 등록 수집기에서 필터를 메서드로 분리한 뒤 Paper 단계에 연결해야 한다(이 작업 범위 밖).

### 식별값 사전 점검과 계약 오류 (M3)

- `identity_precheck`는 표본 재계산 중 나온 `AdapterConfigError`(계약 `ContractError` 포함)를 다른 ValueError보다 먼저 잡아, 스키마/서명/반환형 오류이면 다시 raise해 사이트를 `config_error(adapter_contract:<코드>)`로 멈춘다. 오류 개수로만 세고 나머지 표본으로 일치율 100%가 나오는 일이 없다.
- 데이터성 계약 코드(M4)는: 기준 DB 행 표본이고 식별값이 목록 필드에 의존하는 계약(`identity_uses_list_fields`)이면 URL만으로 목록 필드를 재구성할 수 없으므로 `errors`로 센다(종전 설계대로 비교 0건이면 목록 첫 페이지 표본으로 넘어감). 그 밖(목록 첫 페이지 표본 등)에서는 비교 대상의 불일치로 세어 일치율을 낮춘다. 결과에 `contract_data_errors`를 기록한다(계약 사이트만).

### 복사도 guard 안에서 (L1)

- 계약 인자 구성(`item`/`detail`/필드 바인딩의 deepcopy 포함)과 목록 행 정규화(행 deepcopy·JSON 직렬화 검사)를 `guarded()` 안에서 수행한다. 등록 메서드가 돌려준 객체의 `__deepcopy__` 같은 코드도 네트워크/프로세스 tripwire 아래에서 실행된다. Paper 반환 뒤의 `clean_paper`/`paper_identity` 검사는 종전대로 guard 밖이다(dict 사본에 대한 검사).
