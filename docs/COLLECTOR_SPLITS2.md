# 수집기 분리 2차 — 오프라인 15곳 검토

**통과는 plan 형식·틀 규칙과 합성 입력 시험 통과를 뜻한다. 실제 사이트에서 끝까지 수집할 수 있다는 증거가 아니다. 실제 목록 표지·끝 응답·총건수·정렬 안정성은 모두 확인 못 함.**

워크트리 `/mnt/raid/ruci_workspace/frwaler-collector-splits2`, 브랜치 `collector-splits2-20261013`, 기준 `01fa542093f9c5d544770c152ccdf6a51a6e8d26`. 사이트 요청 0건, 운영/고객 DB 접속 0건. 등록 `crawl()`·기존 파서·요청 코드·식별값·날짜 규칙은 변경하지 않았다. 공통 `crawler/enumeration`도 변경하지 않았다. 기존 워크트리는 읽기만 했다. 푸시·병합·배포·외부 발송·원격 서버 접근 없음.

## 선정과 결과

- 납품 대상 **15곳**, 작업대장 A형 2곳/B형 13곳. 이전 07의 10곳은 제외했다.
- **12곳**: 순수 목록/상세/Paper 변환과 plan 후보를 추가하고 모든 원래 Paper 필드·식별값 동등성과 실제 `SiteRunner`의 합성 E2E를 확인했다.
- **3곳**: 기존 분기/추가 응답/소스 소진을 보존하는 계약 근거가 없어 코드와 plan을 추가하지 않았다.
- 우선순위는 납품 여부, 기존 순수 파서/단일 상세 응답 재사용 가능성, 인라인 Paper 조립 분리 가능성이다. 수집량·복구량을 기준으로 성공을 추정하지 않았다. 최대 15곳 배치에서 멈춘다. 선택되지 않은 나머지 대장은 이번 완료 범위에 포함하지 않는다.
- 이전 B형 선정표 38곳 중 완료 10곳 이후 남은 28곳에서 13곳을 이번 배치로 검토했다. A형 2곳은 `collector-separation-worklist.csv`와 마스터 납품 여부를 근거로 추가했다.
- 이번 후보는 모두 `last_page_backward`. 기존 주소·필터·쪽/오프셋 이동을 보존하는 후보이지, 이전 마스터의 전략 충돌을 해소한 최종 전략이 아니다. 마스터 대장을 수정하지 않는다.

대장: [COLLECTOR_SPLITS2_SELECTION.csv](COLLECTOR_SPLITS2_SELECTION.csv). 15행에 납품 여부, A/B형, 결과, 미해결 코드, 다음 조치, 코드 근거, plan/fixture 경로, 실제 확인 여부, 이전 추정/접근 제한 사유를 보존했다. 실제 영향/회복 항목 수는 전부 `확인 못 함`이다.

## 분리한 12곳

각 `crawler/sites/custom/<site_id>.py`에는 `_enumeration_*` 메서드만 추가했다. 이전 모듈에서 이 새 메서드들을 제거하면 모듈 전체 AST가 기준 커밋과 일치하고, `crawl()` 원문 SHA-256도 일치한다. 원래 모듈 함수와 기존 클래스 메서드의 내부 분기는 그대로다.

| site_id | 목록 정규화·상세 연결 | 보존한 Paper/ID·필터 규칙 | 종료/총건수 후보 |
|---|---|---|---|
| finance-ni-gov-uk-publications | 기존 `(items,next)`에서 items; HTML + `card_summary`로 `_parse_detail` | node_id post_number, node_id 또는 slug external_id; 초록 100자 필터; topics/all_files/목록 날짜 | `page=0`, 빈 main 가정; 총건수 확인 못 함 |
| mimit-gov-it-it | URL 문자열 → `{url}`; `_parse_detail_page(html, 요청 URL)` | article_id 없으면 원래처럼 slug를 사용; title 필수/초록 100자; 날짜/PDF/metadata 그대로 | start 오프셋 10; 총건수 확인 못 함 |
| economy-ni-gov-uk-news | 모듈 URL 파서 → `{url}`; 모듈 `_extract_detail(요청 URL,html)` | node_id 또는 slug external_id, node_id post_number; title/초록 50자; 원래 News metadata | `page=0`; 총건수 확인 못 함 |
| education-ni-gov-uk-news | 모듈 카드 파서 + 정수 page payload; 모듈 상세 파서 | slug external_id/node_id post_number; snippet/meta-description fallback; 초록 50자; list_page 보존 | `page=0`; page 없는 첫 쪽 비교는 끔; 총건수 확인 못 함 |
| kit-nl-institute | WP 배열 검증 → URL/ID/date/원래 record; `_parse_item` | 원래 WP id external_id와 Research article taxonomy 34; 초록 50자; posted_date 유지, post_number 필드를 임의 추가하지 않음 | page 1, 400+정확한 WP 종료 코드, X-WP-Total |
| culture-gouv-fr-espace-documentation | 정수 page → 원래 `_build_list_url` → `(items,next)`; `_parse_detail` → `_to_paper` | location_id/slug/url 우선순위와 목록 원문 URL metadata; 초록 50자 | page 1, resource_type[0]=50; page 없는 첫 쪽 비교는 끔; 총건수 확인 못 함 |
| olympias-lib-uoi-gr-jspui | handle → 원래 `?mode=full` 상세 URL; 모듈 `_parse_full_record`/`_build_paper` | 원래 handle external_id/마지막 숫자 post_number; 표 metadata/날짜/PDF; 초록 100자 | start 오프셋 20, doctoralThesis/score 필터 보존; 총건수 확인 못 함 |
| helios-eie-gr-helios | URL과 원래 handle_path 분리; 쪽 내부 역순도 보존; 기존 `_parse_detail` | `10442/n` external_id/숫자 post_number; 날짜/언어/저널 metadata; 초록 50자 | start 오프셋 100; 원래 `_get_total`의 그리스어 총건수 정규식 |
| regulation-govt-nz-about-us | 모듈 목록/상세 함수 연결; crawl의 metadata 기반 Paper 조립 복사 | slug external_id/첫 다운로드 asset ID post_number; metadata 초록/posted_date/PDF 배열; 50자 필터 | start 오프셋 12; publication_type_id[110]=110; 총건수 확인 못 함 |
| nesc-ie-publications | success/data.pages/data.html envelope 검증; `_parse_list_html`; `_fetch_detail`의 요청 부분을 뺀 순수 상세 파서 | slug external_id/WP id 또는 slug post_number; list date/Type/Report No./Research Area; 50자 | paged=1; pages=0+빈 items만 정상 빈 목록; pages는 항목 총건수로 환산하지 않음 |
| edi-admin-ch-de | pageResults/items 검증; API 상세 URL과 원래 record 분리; JSON 상세 | 원래 item_id/숫자 꼬리 post_number, langGroupId 공개 URL; publisher/types/topics/PDF/원문 날짜; 50자 | offset 20; newsKinds 2개·publisherIDs=3·원래 날짜 범위; JSON pageResults 총건수 |
| publichealthscotland-scot-publications | 인라인 목록 필드를 순수 rows로 분리; 원래 HTML parser로 soup 구성 후 scalar 인자 연결 | slug/post_number, list_title/categories/list_desc fallback, published/posted date; 100자 | p=1/q/sort 원래 query; 총건수 확인 못 함 |

계약 파일은 `docs/examples/collector-splits/<site_id>.json`이다. 틀의 version 1 `sites` 형식을 유지한다. 모든 후보는 `respect_robots=true`, `verify_ssl=true`, `request_interval_s=1.5`, 겹침 1페이지다. 원래 요청의 TLS 비검증/직접 curl/브라우저를 새 계약에 가져오지 않았다. 새 메서드는 I/O를 하지 않고 Fetcher 응답만 처리한다.

EDI는 목록/상세 API 호스트가 `d-nsbc-p.admin.ch`이고 최종 Paper URL은 원래처럼 `www.edi.admin.ch`다. 상세를 공개 URL로 바꾸거나 최종 Paper URL을 API URL로 바꾸지 않는다. 같은 원문 식별값을 유지하기 위한 분리다.

## 리뷰 지적 반영과 거부

1. **WP 종료**: KIT은 400 전체를 끝으로 간주하지 않는다. `rest_post_invalid_page_number`일 때만 종료하고 X-WP-Total을 대조한다. 다른 400과 JSON 오류 객체는 incomplete. 원래 KIT crawl의 넓은 400 처리는 변경하지 않았고, 새 plan은 더 엄격하다.
2. **잘못된 목록**: HTML 타입/명시적 main 표지, JSON 배열·envelope·식별 필드를 검사한다. HTML main에 항목 없이 maintenance 텍스트가 남으면 `listing_parse_error:unconfirmed_empty_listing`로 중단한다. 일반 ValueError를 던져 runner_error로 끝내거나 오류 envelope를 빈 소스로 간주하지 않는다. NESC pages>0+빈 rows, pages=0+rows도 거부한다.
3. **짧은 초록**: 원래 저장 필터를 그대로 옮겨 None을 반환한다. 실제 runner에서는 `empty_item`으로 기록되고 2개의 합성 짧은 항목은 `complete_with_review`다. tolerance/비율을 바꿔 complete로 숨기지 않는다. 기존 연속 5건 계약 실패 중단 규칙은 유지된다. Regulation은 원래 metadata와 고정 publisher 문구로 초록을 구성하므로 topics를 줄여도 2건이 남는다. 이 예외도 원래 crawl과 새 Paper가 같음을 시험한다.
4. **바인딩**: Education/Culture은 page query를 정수로 받는다. 미제공·음수·문자열·중복 query는 `adapter_contract` 거부다. 해당 후보는 page 없는 첫 쪽 비교를 명시적으로 끄고 실제 매개변수를 쓰는 시작/끝 탐색은 유지한다. 상세 파서는 redirect 후 주소가 아닌 요청 당시 item URL/원래 handle을 사용한다.
5. **식별값 불확실**: KIT bool/문자열/0/누락 id, EDI 누락 id/langGroupId/불명 반환형을 거부한다. 기존 소스가 명시적으로 사용하는 slug/handle fallback은 보존하고 새 숫자 ID를 만들어 넣지 않는다. 중복/식별값 충돌 점검은 기존 어댑터/저장 경로를 그대로 쓴다.
6. **총건수**: KIT/EDI/Helios에 코드상 보고 필드를 연결했다. EDI `listing.total.path`는 추출기가 읽는 문자열 `pageResults`다(계약 binding path 배열과 혼동하지 않음). KIT/EDI의 총건수 3·관측 2 합성 사례는 `complete_with_mismatch:reported_total_mismatch`다. 보고 총건수가 없는 사이트는 임의로 total을 채우지 않는다.

## 미해결 3곳 — 코드/plan 미변경

| site_id | 사유 코드 | 확인된 코드 근거 | 다음 작업 |
|---|---|---|---|
| economie-fgov-be-nl | CONDITIONAL_DETAIL_UNSUPPORTED | crawl의 `len(teaser)<100`에서만 상세 요청/필드 보강. 긴 teaser에서는 authors/category/filename 등의 기본값을 유지. 현재 계약은 detail_method를 지정하면 모든 항목에 상세 요청 | 조건부 상세 계약의 안전한 설계가 필요. HTML을 Fetcher로 얻을 수 있는지도 확인 못 함. 무조건 상세 보강으로 의미를 바꾸지 않음 |
| ifremer-fr-fr | ADDITIONAL_HEAD_REQUIRED | `_process_item`에서 `_filename_from_url(pdf_url)` 실패 시 `_curl_head_filename`로 PDF HEAD 조회 후 originalFilename과 metadata에 반영 | 추가 응답을 명시적으로 게이트/robots/주소 검사 아래 전달하는 계약 필요. fallback 삭제로 완료 처리하지 않음 |
| doras-dcu-ie-view | WHOLE_EXPORT_STRATEGY_UNPROVEN | `_fetch_json_export` 전체 배열 다운로드, `_process_record` 저장과 중복 상태 결합; exporter의 paging/소진 근거 없음 | 단일 export 소진·크기 제한·재개·ID 대조 계약 검토. 가짜 page 또는 마지막 쪽을 넣지 않음 |

이 3곳이 코드 몇 줄 또는 틀 변경 하나로 반드시 풀린다는 숫자는 제시하지 않는다. 계약 설계·합성 시험·실제 소스 구조 확인이 각각 필요하다. 3곳은 기준 커밋과 파일 bytes가 일치한다.

## 시험과 커밋

- 기준 전체 enumeration: **477개, 실패 0, 건너뜀 1**.
- 신규 split2: **132개, 실패 0, 건너뜀 0**. 12사이트×10개(원래 Paper 모든 필드/ID, 짧은 초록, redirect, 입력 불변, 기존 메서드 SHA, 잘못된 목록, 빈 목록/오류 구분, 실제 runner 끝 응답, 실제 runner 오류, 짧은 초록 review) + 계약/총건수/거부 12개.
- 최종 전체 enumeration: **609개, 통과 608, 실패 0, 건너뜀 1** (84.186초). 기존 477개를 유지하고 신규 132개를 추가했다.
- 코드 커밋 1: `8056b6c` — Finance/Mimit/Economy/Education/KIT/Culture의 순수 계약·plan·fixture·시험.
- 코드 커밋 2: `94d3f2e` — Olympia/Helios/Regulation/NESC/EDI/PublicHealth 및 총건수/거부 시험.
- 이 문서와 선정 대장은 별도 docs 커밋으로 남긴다.

시험은 이름이 `enum-test-splits2-`로 시작하는 `--rm --network none` Docker에서 로컬 `/wheels`의 의존성을 `--no-index`로 설치해 실행했다. 외부 네트워크·pip 요청 없음. 새 Paper 비교 시험은 생성자 없이 실제 등록 클래스를 불러오고 저장 인자만 메모리에 포착한다. socket/DNS/process/SQLite/PostgreSQL tripwire는 호출이 삼켜져도 실패하게 기록한다. E2E는 기존 `tests.enumeration_fakes.Env`의 폐기 가능한 임시 SQLite만 사용하며 고객/운영/외부 DB에 접속하지 않는다. 기존 전체 회귀에는 컨테이너 내부 loopback 가짜 HTTP fixture도 있으므로 '네트워크 0건'은 외부/사이트 요청 0건이다. 새 시험은 가짜 전송만 쓴다.

기존 framework 및 07의 10개 plan/fixture 35개 파일은 bytes 비교로 동일함을 확인했다. 이전 수집기/시험은 수정하지 않았다. 변경 파일 목록은 이 작업 커밋들의 `git diff --name-only 01fa542`와 최종 검증 JSON에 남긴다. source 파서와 crawl의 원문 해시는 각 fixture의 `original_method_source_sha256` 및 검증 JSON에 있다.

## 실행 전 확인과 알려진 한계

- 현재 URL·이전 접근 제한/호스트 보류·robots 상태를 먼저 확인한다. 이 후보들은 과거 제한을 해제하지 않는다. 실제 응답 구조와 마지막 쪽을 확인하지 않았으므로 즉시 서버 전체 수집용 확정 plan으로 취급하지 않는다.
- HTML `main`은 코드/합성 입력의 후보 표지다. 실제 빈 쪽이 heading/안내문을 포함하면 엄격한 빈 목록 검사가 중단할 수 있다. 실제 마지막 쪽의 명시적 빈 상태가 확인되기 전에는 검사 완화나 완료 표시는 하지 않는다.
- 실제 API의 X-WP-Total/pageResults/그리스어 숫자 의미와 현재 totals, deep paging/재정렬/검색창 상한은 확인 못 함. 최초 응답의 total과 소스가 변하는 사이의 일관성도 확인 못 함.
- 오프셋/score/date 정렬은 동률·삽입으로 중복/누락될 수 있다. 정렬 키의 유일성/보조 id 정렬 지원 여부는 확인 못 함. 서버 실행 전 **같은 구간을 두 정렬로 받아 고유 ID 수를 비교**하고, 같은 날짜 쏠림·구간/전체 총건수 대조를 검증해야 한다. overlap 1페이지나 중복 저장 방지만으로 누락 없음이 입증되지 않는다.
- 원래 목록의 `next`/`pages`를 순수 rows로 정규화하지만 마지막 쪽 전략은 원래 crawl의 200쪽/시간 상한을 소진 증거로 쓰지 않고 정상 빈 응답을 따로 찾는다. 실소스의 silent cap은 확인 못 함. 남은 불안정/반복 쪽을 complete로 추정하지 않는다.
- 원래 상세 실패 시 목록 fallback은 Fetcher 실패를 삼켜 구현하지 않았다. 정상 상세 응답의 Paper 동등성을 시험했고, 접근 거부/요청 실패는 기존 틀의 incomplete/access-blocked 규칙에 따른다. 고의로 요청 실패를 성공 Paper로 바꾸지 않는다.
- 원래 날짜 규칙을 보존했으므로 Olympia/Helios의 연도/월만 있는 날짜에 기본 1월/1일을 붙이는 기존 동작과 PublicHealth의 기존 월 파서는 여기서 고치지 않았다. 작업 11의 날짜 수정은 별도 기준/브랜치다. 변경을 자동 합치지 않았다.
- 파일(PDF)을 실제 수신하거나 DB에 운영 데이터를 넣지 않았다. fixture의 PDF URL은 메타데이터 비교에만 쓰며 요청하지 않는다. 중단 후 실소스 재실행, 실제 누락률/처리량/총용량/파일 성공률은 확인 못 함.
