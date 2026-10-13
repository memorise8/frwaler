# 대형 3곳 수집 준비 — 오프라인 13번 작업

**통과는 plan 형식·규칙과 합성 응답 시험 통과다. 실제 사이트에서 끝까지 수집할 수 있다는 증거가 아니다. 세 사이트 모두 판정은 `사이트 확인이 더 필요`이며 서버 전체 실행용으로 승인하지 않는다.**

기준 `ffc67dd8bbb58f9ec43a208d5db624610ace2e00`, 새 워크트리 `/mnt/raid/ruci_workspace/frwaler-large-sites-2`, 브랜치 `large-sites-2-20261013`. 기존 integration 및 다른 워크트리는 수정하지 않았다. 실제 사이트/패키지 네트워크 요청 0건, 운영·고객 DB 접속 0건, 외부 서버/temis 관련 접근·푸시·배포·병합 없음. 테스트는 `--rm --network none --name enum-test-large2-*` Docker 및 로컬 `/wheels`로 실행했다. 새 시험은 메모리 가짜 전송이며 E2E만 기존 폐기 가능한 SQLite fixture를 쓴다. 기존 전체 회귀에는 컨테이너 내부 loopback HTTP fixture가 포함된다. 새 DB 서버와 로컬 venv는 만들지 않았다.

## 1. 등록 코드에서 확인한 사실

아래 코드는 이 새 워크트리 기준 줄 번호다. 기록 규모는 지시 파일/기존 평가의 값이며 현재 총건수·등록 필터 범위·저장 가능 건수를 확인한 값이 아니다. 과거 확인기 JSON은 이번에 읽기만 했고 다시 요청하지 않았다.

|항목|e-stat-go-jp-stat-search|ots-at-pressemappe|etera-ee-browse|
|---|---|---|---|
|기록 N|1,709,118|1,561,267|210,234|
|목록|GET `https://www.e-stat.go.jp/retrieve/api_stat`|GET `https://core2023.ots.at/api/v1/search`|POST `https://www.etera.ee/api/browse`|
|범위|layout=dataset, metadata=1, data=1|빈 query, emittentId=54（Bundesministerium für Inneres）|-typ=page 제외; 빈 fields、favorite=false、layout=card、screen=browse|
|페이지|page=1부터；페이지 크기 코드/기존 증거에서 확인 못 함|page=1부터、maxCountPerPage=10|start=0,25,50…、limit=25|
|정렬|명시 sort 없음、DELIVERY_ORDER=arbitrary|명시 sort 없음、arbitrary|sort=[{key:date_created,asc:false}]、newest_first|
|목록 응답|JSON.items 안 HTML、article.stat-resource_list-item|JSON.result 배열；meta.count/totalPages는 코드 주석의 예상 schema|JSON.results.docs 배열、results.count는 등록 루프가 읽는 필드|
|상세|GET 목록의 detail URL、HTML 통계 표|GET /pressrelease/{key} JSON.result；정상적인 JSON 결과 부재 시 HTML 공개 상세 fallback|GET /api/item/{id}/meta JSON、오류 객체 거부|
|식별값|stat_infid/statInfId/data-value 문자열；external_id와 post_number；앞 0 보존|OTS_YYYYMMDD_OTSnnnn 복합 문자열 key；두 필드 모두 그대로|obj_id 또는 id를 문자열로 보존；Paper.external_id；공통 저장 변환에서 post_number로 사용, 임의 순번 추가 안 함|
|날짜|公開年月日時分→일 날짜, 없으면 등록 규칙상 조사월 fallback；일자 미확정 월은 YYYY-MM|JSON detail.date 또는 목록 date、DD.MM.YYYY/ISO 변환；posted_date 원문 목록 date+time|cf22/date_to 및 mask；YYYY/YYYYMM/YYYYMMDD에 해당하는 년/월/일 정밀도；created 정렬일과 published 날짜는 다름|
|초록|통계 개요+라벨 붙인 metadata 조립、100자 미만 제외|lead+text/structured article、50자 미만 제외|원문 description 우선、짧거나 없으면 catalog metadata 설명 생성、50자 미만 제외|
|PDF/첨부|첫 PDF download；HEAD Content-Disposition→URL 파일명→statInfId.pdf；본문 다운로드 없음|attachments/files/documents 중 PDF 후보；JSON 실패→HTML branch는 원래처럼 PDF 없음|rights cf13에 Vaba가 있으면 /api/oai2/download/{id}.pdf URL만 구성；수신 없음|

코드 근거:

- e-stat: `crawler/sites/custom/e-stat-go-jp-stat-search.py:136` 목록 URL, `:147` JSON/HTML, `:177` statInfId·목록 날짜, `:259` 상세 표, `:290` 원래 PDF HEAD, `:340` Paper/ID/원문 날짜, `:529` 200쪽 실행당 chunk.
- OTS: `crawler/sites/custom/ots-at-pressemappe.py:133` 목록 GET/54 범위, `:155` JSON 상세, `:161` HTML fallback/날짜, `:273` 재개 가능한 실행당 200쪽, `:306` 이후 ID/날짜/Paper 조립. meta.count 주석은 현재 서버의 schema 확인이 아니다.
- ETERA: `crawler/sites/custom/etera-ee-browse.py:197` POST payload, `:213` browse 전송, `:227` metadata, `:260` mask, `:375` Paper/권한/PDF, `:525` offset checkpoint.

**관찰 범위 구분**: 과거 platform-probes에서 e-stat은 GET 시작 HTML 200만 관측했고 API 총건수는 없었다. OTS search는 200이었지만 확인기가 HTML로 분류해 JSON 계약 미확정이다. ETERA GET /api/browse는 405였고 POST 차단/허용을 확인한 결과가 아니다. 마스터의 3행은 delivery=True, access_reasons=[]/restricted_hosts空이다. 과거 robots 200/404와 대장의 보류 없음은 현재 접근 허용을 의미하지 않는다. Crawl-delay의 현재 값도 확인 못 함.

## 2. 완전 열거 설계와 분할

첫 후보는 세 곳 모두 `last_page_backward`이며 등록 범위·페이지/오프셋을 보존한다. 끝 쪽은 새로 탐색/재확인하고 과거 코드의 200쪽/25분 제한을 소진 증거로 쓰지 않는다. 기본 요청/시간 예산은 기존 20,000건/6시간이며 중단은 incomplete·durable checkpoint로 남는다. 100만 건을 한 번의 명령에서 끝낼 계획이 아니다.

|사이트|깊이 상한 확인|구간 대안 및 키 타입|정렬/삽입 위험과 확인 조건|
|---|---|---|---|
|e-stat|서버 page/result window 상한 확인 못 함；코드 200은 실행당 chunk|공개/갱신일·통계 코드·statInfId 범위가 후보일 뿐 API 필터 이름/문법/누락값 처리 확인 못 함；statInfId는 숫자처럼 보여도 문자열 식별값이고 숫자/사전순 비교 여부 미확정|기본 정렬 유일성 미확정；같은 공개일 또는 조사월에 P개 초과 집중 가능；조사월 YYYY-MM와 공개일을 혼용하지 않음|
|OTS|서버 page/maxCountPerPage 상한 확인 못 함|지원된다면 날짜/시간 또는 native key 구간을검토；key는 복합 문자열；날짜 prefix가 있어도 API의 사전순 비교·범위 필터를 추정하지 않음；54 범위 고정 필수|동일 date/time에 10건 초과할 수 있음；기본 정렬/키 보조 정렬 확인 못 함；원문의 시간도 유일성 증거 아님|
|ETERA|POST offset 결과 창 상한 확인 못 함|dateSearch/dateFilter/dateInputs/dateCalendar 이름은 코드 확인、값은 모두 None；구간 문법/경계/의미 불명；obj_id/id를 문자열로 변환하는 것과 서버 비교 방식은 별개、숫자/사전순 미확정|date_created 내림차순 단독；일괄 등록 동률/삽입으로 25건 경계 밀림；published mask 년/월 날짜를 created 필터로 대체하지 않음|

지원 문법이 확인되기 전에는 날짜/ID/접두 분할 plan을 만들어 채우지 않았다. SONAR에서 확인된 문자열 사전순 pid 비교를 이 3곳의 숫자 비교/범위 지원 증거로 쓰지 않는다. 서버 상한이 나타나면 현재 페이지 후보를 실행 승인하지 않고 정상 API 날짜 구간·ID 범위·커서·스냅샷/공식 export 지원을 먼저 확인한다. 어떤 지원이 없는지는 아직 확인 못 함이다.

분할을 도입할 때 parent→children 구간은 빈 날짜/null/경계 포함 전체를 덮고 서로 겹치지 않아야 한다. 각 leaf의 source total↔고유 ID와 전체 total↔전체 고유 ID 대조를 반드시 켠다. 통계적으로 같아 보이는 count만으로 완전성을 인정하지 않는다.

**NTRS 함정 반영**: 같은 날 652건 날짜/offset 정렬에서 중복·누락 36건, 유일 ID 정렬에서 652건이었다는 사용자 제공 실측을 위험 근거로 사용한다. 이 사이트의 sort=id 지원 근거는 아니다. 동일 구간을 서로 다른 두 지원 정렬로 전 페이지 받아 raw count/고유 ID/중복/양쪽 차집합/보고 총량을 비교한다. 같은 날짜에 P개 초과인 구간과 1월 1일·일괄 적재 사례를 포함한다. 순서가 흔들리면 유일 보조 키·커서·동결 스냅샷 등 정상 지원이 있어야 한다. overlap 1페이지와 중복 저장 0은 누락 없음의 증거가 아니다.

## 3. 구현한 최소 틀 변경과 후보

후보 파일: `docs/examples/large-sites-batch2/<site_id>.json` 3개. 틀이 읽는 version 1/sites 형식 그대로이며 `_review.server_ready=false`를 표시했다. 설명 metadata는 실행을 막는 런타임 gate가 아니다. 사전 확인 담당자가 실제 실행 전에 승인 여부를 별도로 관리해야 한다.

- `crawler/enumeration/http.py:843`: 기존 get은 request(GET) wrapper. POST는 **etera.ee/www.etera.ee의 /api/browse만** 읽기 전용 검색 endpoint로 허용한다. JSON object, 64KiB 제한, NaN 거부, Content-Type 고정. 기존 GET/HEAD는 임의 쓰기 메서드로 확장하지 않는다. POST redirect는 모두 review 거부이며 body를 다른 endpoint로 재전송하거나 GET으로 바꾸지 않는다.
- `http.py:509`: production transport의 선택적 bounded body 전달. HEAD는 응답 본문을 읽지 않고 PDF의 큰 Content-Length를 다운로드 크기 실패로 오판하지 않는다. GET 본문 제한/TLS 검증/DNS pinning·사설주소 차단·hop robots/hold 검사·게이트·예산·거부/429 처리는 그대로다.
- `plan.py:132` 이후 및 `strategies.py:83`: opt-in listing.request POST template/page_field. 틀이 만든 page offset을 body.start로 넣고 wire URL에서 가상 start query를 제거한다. template를 복사하므로 checkpoint fingerprint/원문 범위가 변형되지 않는다. POST는 last_page_backward와 명시적 start만 지원한다. 날짜 split POST 문법은 확인되지 않아 구현/허용하지 않는다.
- `contracts.py:68`, `adapters.py:315`: opt-in followup_method/args/kwargs, paper의 followup 응답 인자. 등록 순수 메서드가 None 또는 `{method:GET/HEAD,url:…}` 한 개만 선언한다. 배열·POST·header/body override를 거부한다. 이 응답도 Fetcher를 거치며 401/403/429/robots/사설주소는 fallback으로 삼키지 않는다. methods/signatures/item binding도 검증한다.
- `strategies.py:343`: opt-in require_reported_total=True. 총건수가 없으면 `incomplete:reported_total_missing`이며 segment도 running으로 남긴다. 기존 opt-in 없는 plan의 fingerprint와 동작은 유지한다. 기존 overall/segment 총량·중복 점검은 끄지 않는다.

사이트 계약:

- e-stat: JSON.items/각 statInfId를 엄격히 확인하고 정상 빈 문자열과 오류 envelope를 구분. 상세 HTML `_parse_detail`은 재사용. 첫 PDF에 대해서만 gated HEAD를 선언하고 순수 `_enumeration_pdf_info`가 기존 파일명 규칙을 사용한다. HTML HEAD는 PDF 성공으로 읽지 않는다. `_build_record` Paper 조립을 새 순수 메서드로 옮겼다. **총건수 경로는 확인 못 했으므로 listing.total을 추측해 넣지 않았다.** 합성 2건이 저장돼도 incomplete로 남는 것을 시험한다.
- OTS: result/meta shape와 문자열 key 검증、API URL/공개 URL을 분리. 정상 200에서 JSON result가 없거나 읽을 수 없으면 선언된 공개 HTML GET fallback 한 개를 사용한다. 실제 4xx/5xx/접근 거부를 삼켜 다른 경로로 우회하지 않는다. native key/Paper/PDF branch를 보존하고 meta.count 대조 후보를 연결했다. 현재 JSON schema와 54 범위 count는 확인 못 함.
- ETERA: 원래 browse payload와 25 offset、-typ=page와 date_created desc를 유지. rows는 meta API URL+원래 item을 전달하고 `_paper_from_item`을 재사용한다. 오류 meta/불명 ID를 거부、원래 50자 제외 필터를 보존. results.count를 대조하고 POST 뒤 metadata GET도 같은 gate를 쓴다. Referer 등 native curl header의 필요 여부는 확인 못 했으며 이를 우회 목적으로 변형하지 않는다.

날짜 BUG-20261008-02 대응: e-stat `_survey_month_date`의 YYYY-MM-01을 YYYY-MM로 고치고 invalid month를 거부했다. 원문 조사월/公開年月日時分은 기존 metadata에 유지한다. 등록 native 변환과 새 계약 모두 같은 수정 helper를 쓴다. ETERA는 item.date_to fallback의 원문을 `source_date_to_raw`로 추가하고 원래 rawMeta/mask를 유지한다. OTS는 `source_published_date_raw`/`source_listed_date_raw`와 HTML fallback의 원문 date_raw를 추가했다. ID 및 날짜 선택 우선순위는 변경하지 않았다. partial 년/월은 공통 normalize_date가 raw/status/precision을 보존하며 검색 DATE 열에 임의 day를 저장하지 않는다.

기존 수집기의 허용 변경은 e-stat 월 정밀도 helper와 위 raw metadata뿐이다. AST 회귀는 신규 enumeration 메서드/명시한 날짜 변경을 제외한 모듈 전체가 기준과 같음을 확인한다. 실제 등록 crawl을 가짜 전송으로 실행해 저장 Paper 전체 필드와 새 계약 출력을 비교한다（OTS JSON/HTML 양쪽 branch 포함）. 기존 등록 `_curl/_get`을 새 계약에서 호출하지 않는다.

## 4. 서버 사전 확인 목록과 요청 비용

[LARGE_SITES_BATCH2_PREFLIGHT.csv](LARGE_SITES_BATCH2_PREFLIGHT.csv)는 3곳×8항목=24행이다. 무엇을/어디서/어떤 값을 확인하고 실패 시 어디서 멈추는지를 적었다. 모두 `확인 못 함(실사이트 요청 0건)`이다.

|우선순위|어디서|확인 값/판정|
|---|---|---|
|접근|모든 목록/metadata/HTML fallback/HEAD 호스트 robots 및 기존 holds|현재 Allow/Disallow、UA、Crawl-delay、request-rate；보류/거부/로그인/챌린지/429이면 중단、우회 안 함|
|범위와 schema|e-stat retrieve/api_stat、OTS search emittentId=54、ETERA POST exact template|실제 page size/response type/필터 효과；source total 경로/타입/범위；OTS 전체 서비스 count와 54 count 구분|
|깊이|reported 마지막 쪽/offset 및 뒤 2쪽|IDs/raw count、오류·wrap/clamp/창 상한、silent cap；200 native chunk를 서버 cap으로 쓰지 않음|
|정렬 유일성|동결한 같은 범위、동일 날짜 P개 초과 구간|두 지원 정렬로 전 페이지、unique ID/중복/차집합/total 비교；API가 받는 유일 id 또는 (날짜,id) 보조 키/커서 확인|
|분할|실제 API 문서/프런트엔드의 date/ID control|경계 포함·null·숫자/사전순 비교·구간 합계/차집합 검증；지원 못 확인한 parameter는 plan에 넣지 않음|
|원문 품질|첫/중간/마지막 목록과 승인된 metadata 표본|n과 원문 초록/제목/날짜 없음、native short 제외、year/month/day/invalid 비율；ETERA 생성 catalog 초록과 원문 abstract를 분리 집계|
|처리량|권한이 확인된 요청 로그|RTT 분포、실효 gap、목록/상세/H/F/retry/overlap/재개 재탐색 요청 수；같은 공인 IP/호스트의 4서버 합산 제한|
|재개/완료|새 state/output의 작은 승인 시범|중단→이어받기、보고 total↔고유 IDs、원문 날짜·파일명、아무 mismatch/review/미확인도 전체 성공으로 승인하지 않음|

비용은 [LARGE_SITES_BATCH2_CAPACITY.csv](LARGE_SITES_BATCH2_CAPACITY.csv)에 5개 조건부 시나리오로 보존했다. 기록 N·페이지 P에 대한 정적인 소스 1회 전체 metadata 열거이며 실제 현재 총량이 아니다. L=ceil(N/P). 새 틀은 겹침 1페이지 때문에 목록을 대략 2L회 읽는다. 안정된 목록/끝 탐색 가산을 약 2ceil(log2 L)+6으로 놓았다（검증된 실제 요청 수가 아님）. robots、metadata N、조건부 HEAD H/HTML F를 별도 포함한다. 변동/실패/재시도/재개 후 재탐색·추가 ID 전수 점검·PDF 본문 비용은 가산 전이다.

**시간 단위**: 계획 초=R×평균 RTT+(R−1)×gap. 24시간 하루 요청 능력=86,400/(gap+평균 RTT). 가정한 RTT=1초에서 gap 1.5초는 약 34,560요청/일, gap 10초는 약 7,855요청/일이다. 다음 표는 RTT=1초라는 측정하지 않은 가정과 모든 요청 사이에 gap을 두는 단일 순차 스케줄이다. OTS의 두 호스트를 병렬화해 낙관적으로 줄이지 않았다. 8시간/일 운전이면 달력일을 대략 3배로 본다. 여러 서버로 나눠도 동일 호스트/공인 IP 총 속도를 4배로 잡지 않는다.

|사이트/시나리오|metadata 요청 추정|gap 1.5초+RTT1초、24h/day|gap 10초+RTT1초、24h/day|
|---|---:|---:|---:|
|e-stat|2ceil(N/P)+N+H+탐색/robots；P 확인 못 함、H=0…N|상세만 49.45일、상세+모든 PDF HEAD 98.91일에 목록/탐색 시간 추가|상세만 217.60일、상세+모든 HEAD 435.19일에 목록/탐색 추가|
|OTS JSON 성공、F=0|1,873,565|54.212일|238.532일|
|OTS 전부 HTML fallback、F=N|3,434,832|99.387일|437.305일|
|ETERA 목록+metadata|227,089|6.571일|28.912일|

10초는 현재 Crawl-delay로 확인한 값이 아니라 느린 허용 속도 시나리오다. 1.5초도 현재 robots가 허용하는 속도로 확인한 값이 아니다. e-stat page size를 임의로 20/100으로 채우지 않았으므로 현재 전체 예상 일수는 산정 못 함이다. 예산 20,000/6시간 내 시범/재개와 장기간 전체 소요를 구분해야 한다. PDF byte 전송·렌더링·저장량/디스크 용량·품질 보완 시간을 metadata 시간으로 대체하지 않는다. 세 사이트의 일수를 합산해 납품 일정으로 약속하지 않는다.

## 5. 사이트별 판정과 시험

|사이트|판정|코드/과거 증거 근거와 선행 확인|
|---|---|---|
|e-stat-go-jp-stat-search|사이트 확인이 더 필요|136/147/177/290/340행의 GET/HTML/HEAD 계약은 합성 구현；P·실제 total path·깊이/정렬·부분 날짜 의미 미확정；require_total True에서 total 미제공이면 incomplete|
|ots-at-pressemappe|사이트 확인이 더 필요|133/155/161/273행의 GET+JSON/HTML branch 구현；과거 200은 JSON 계약 미확정、1,561,267이 54의 count인지 미확정、깊이/정렬 미확정|
|etera-ee-browse|사이트 확인이 더 필요|197/213/227/260/375행의 POST+meta+mask 계약 구현；과거 GET405는 POST 권한 증거 아님、created 보조 정렬/창 상한/날짜 필터 미확정|

세 곳 모두 '지금 수집 가능한 설계'로 승인하지 않았으며、현재 접근 거부로 확인된 것이 없어 '접근·정책상 보류'도 새로 단정하지 않았다.

- 기준 전체 enumeration **440개、실패 0、건너뜀 1**（91.355초）.
- 신규 시험 **39개**: native ID/모든 Paper 필드、e-stat HEAD 파일명/월 정밀도、OTS JSON/HTML/raw 날짜、ETERA POST/body/마스크、원문 보존、잘못된 목록/오류/식별값、total 미제공/불일치、동일 created일 50건 중 25건 반복 누락 탐지、budget/403、POST/HEAD robots/private/redirect/401/429、real gate 응답 후 1.5초、production HEAD 무본문 및 POST body 전달、기존 fingerprint/AST 보존.
- 최종 전체 enumeration **479개: 통과 478, 실패 0, 건너뜀 1** (91.522초). 기존 440개 유지 + 신규 39개. PostgreSQL 전용 1개는 TEST_PG_ADMIN_DSN 없음으로 정상 건너뜀.
- 코드 커밋 **a207b16** — 3곳의 계약/후보, governed POST/단일 GET·HEAD followup, 날짜 원문·정밀도, 회귀 시험.
- 문서/사전 확인표/조건부 비용 표는 별도 docs 커밋으로 보존한다.

알려진 한계: 실제 데이터/총량·날짜/초록 비율·정렬·API 키/Referer 필요성/robots/Crawl-delay·응답 헤더·창 제한·partition syntax는 확인 못 함. fallback은 성공 응답의 미사용 payload에서만 선언하고 HTTP 실패/거부는 보존한다. HEAD redirect는 최종 헤더를 사용하므로 curl의 여러 hop 헤더를 섞어 읽는 방식과 파일명 결과가 다를 수 있다. 실제 파일 수신/고객 DB 데이터 대조/운영 저장 원자성/장시간 실제 요청량은 시험하지 않았다. 통과는 합성 시험 통과이며 성공 회복 건수나 실제 납품량 추정이 아니다.
