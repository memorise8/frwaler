# 등록 수집기 어댑터 계약 v1 설계

기준: e2ebfc9. 오프라인 설계·개발이며 라이브 수집·DB·배포를 수행하지 않는다. 기존 동작은 계약 미지정 시 보존한다. 이 문서는 구현 전에 작성했으며 검증 후 결과를 별도 절에 추가한다.

## 실제 거부 24곳과 관련 패턴

같은 오류 24건은 해결 가능 24곳을 의미하지 않는다. 아래 행은 실제 등록 코드 조사다. Paper는 저장용 dict를 뜻하며 목록 요약 dict와 구분한다.

| site_id | 반환형·인자·변환 및 판단 | 코드 근거 |
|---|---|---|
| afp-gov-au-news-centre | tuple(items,next_url) → dict 행; detail(raw,url) → 부분 dict; _build_paper(item,detail). 연결 검증 대상. | `crawler/sites/custom/afp-gov-au-news-centre.py` crawl:129, _parse_list_page:294, _parse_detail_page:371, _build_paper:414 |
| africamuseum-be-en | 5열 tuple 행(slug,title,listed_date,snippet,source_url); detail(html,slug,listed_date,snippet,source_url) → Paper. 연결 검증 대상. | `crawler/sites/custom/africamuseum-be-en.py` _parse_list_page:115, _parse_detail_page:186, crawl:257 |
| culture-gouv-fr-espace-documentation | tuple(items,has_next); detail(html,url) → 부분 dict; _to_paper(list_item,detail). 연결 검증 대상. | `crawler/sites/custom/culture-gouv-fr-espace-documentation.py` _parse_list_page:263, _parse_detail:333, _to_paper:558, crawl:629 |
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
4. 운영/고객/테스트 DB 연결, 서버 생성, 라이브 요청은 하지 않는다. 테스트는 메모리 응답과 conn=None으로 한다. 워크트리 collect-adapter-20261012에만 로컬 커밋한다.
