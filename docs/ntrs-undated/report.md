# NTRS 발행일 없는 레코드: 오프라인 분할 구현과 서버 인계

**합성 시험 통과는 실수집 성공 증거가 아니다. 이번 사이트 요청은 0건이다. 두 번째 plan은 서버에서 확인하지 못한 필터와 범주 목록 때문에 실행 차단된 초안이다. 648,013건을 실제로 모두 받을 수 있다고 확인하지 않았다.**

## 결과와 증거 범위

- 기준: `large-sites-2-20261013` HEAD `d76a5e542bb9a043b77313c4ffd3a17d7881455f`를 직접 확인하고 새 워크트리 `/mnt/raid/ruci_workspace/frwaler-ntrs-undated`, 브랜치 `ntrs-undated-20261014` 생성. 기존 워크트리는 수정하지 않았다.
- 입력: 상위 폴더의 `FINDINGS.md`, `plan-first-pass.json`. 서버 수치는 해당 기록을 인용한 것으로 이번에 재확인한 값이 아니다.
- 기준 시험 482개, 건너뜀 1개 → 변경 후 496개, 건너뜀 1개. 기존 482개 유지, 신규 14개 추가. Docker Python 3.12, `--network none`, `pip --no-index --find-links=/wheels`, 임시 SQLite만 사용했다. PostgreSQL 및 실제 데이터 폴더 시험은 하지 않았다.
- 39,954건의 합성 등록일 + 잔여 2건을 실제 전략·체크포인트 저장소에서 모두 열거하여 고유 ID 39,956개와 합계 일치를 확인했다. 이 큰 시험은 문서 저장 4만 회를 실행하지 않는다. 문서 저장·중복 회피·날짜 규격화는 별도 작은 end-to-end 합성 시험이다.
- 원본 첫 수집 산출물과 기존 고객/운영 DB는 읽거나 변경하지 않았다. 네트워크·원격 서버 접속·푸시·배포·병합은 하지 않았다.

## 먼저 숫자를 구분해야 하는 이유

| 항목 | 값 | 의미/한계 |
|---|---:|---|
| 서버 전체 보고 | 648,013 | 기록의 시점 수치 |
| 첫 저장 고유 행 | 590,634 | 중복 0, 실패 0, 종료 `complete_with_mismatch` |
| 전체 − 저장 | **57,379** | 다음 수집에서 추가될 수 있는 기준 차이. 전부 발행일 없음이라고 단정 불가 |
| published 범위 보고 | 590,670 | 1800–2026 범위 |
| 전체 − published 범위 | **57,343** | 그 필터 밖인 건수. 발행일 없음/범위 밖/시점 차이를 구별하지 못함 |
| published 범위 − 저장 | 36 | 누락 원인을 단정할 근거 아님 |
| 첫 leaf 보고 합계 − 저장 고유 | 58 | 590,692 − 590,634 |
| 여러 leaf에 나타난 공유 키 | 55 | 공유하는 **키 종류 수**, 중복 출현 횟수 합이 아님 |
| created 1900–2099 / ≤1899 | 648,012 / 1 | 합계는 전체와 같지만 NULL/빈 값 건수나 경계 의미를 증명하지 못함 |

기록은 동시에 “created 없는 레코드 1건 미확인”도 적고 있다. 초기 1건을 NULL로 간주하거나 반대로 NULL=0으로 확정하지 않는다. `0202-08-01`은 달력 파서에서 유효한 서기 202년일 수 있지만 데이터 의미상 이상값이다. 2020년 등으로 보정하지 않고 조기 구간에 원문 그대로 포함한다.

## 기존 틀의 범위와 상한 동작

줄 번호는 구현 커밋의 `crawler/enumeration/strategies.py` 기준이다. 아래 기존 분기는 opt-in tree를 제외하면 바뀌지 않았다.

| 위치 | 확인한 동작 |
|---|---|
| 513, 516–534 | `year → month → day`; 시간/분/초 분할 없음 |
| 640–655 | `split_to`에 허용한 다음 단계만 분할. 기본 `[month, day]`. explicit 모드는 분할하지 못함 |
| 728–738 | `query_range`/`query_prefix_range`는 최소 단위에서도 `total > hidden_cap`이면 항목을 저장하기 전에 `cap_unsplittable` |
| 739–746 | 최초 페이지에서 cap 초과를 알면 부모 페이지를 저장하지 않고 분할 |
| 762–776 | 일반 range는 `listed == total` 판정이 cap 초과 판정보다 먼저다. 따라서 일 단위 cap 초과가 언제나 조기 거부되는 것은 아님. 목록 부족 시 더 나눌 수 있으면 늦은 분할 가능 |
| 807–838 | leaf 간 공유 키, leaf 합계↔전체 고유 ID, unfiltered 전체 총건수 대조 |

신규 `partition_tree`는 **leaf 총건수 < hidden_cap(9800)**를 요구한다(같아도 분할/중단). 첫 plan의 `> cap` 조건과 혼동하지 않는다. 실제 API 10,000건 창 상한은 제공 기록에서 plan의 가정으로만 언급되므로 이번에 확인했다고 쓰지 않는다.

## 새 분할 계약과 구현

기존 `date_range_split`에 `segments.mode=partition_tree`를 추가했다. 자식마다 필터를 명시하는 트리이며 새 전략이나 우회 전송기는 만들지 않았다.

1. `root`는 listing의 추가 필터가 없는 `params={}`이고 하나뿐이다. 노드는 고유 key, 절대적·누적 params, children으로 구성한다. 자식 params는 부모에서 자동 상속되지 않으므로 날짜+stiType+center를 모두 적어야 한다.
2. 서버에서 필터 문법·배타성·완전성을 확인한 뒤 `server_verified=true`로 명시해야 한다. `overall_total_check=true`, total 추출 규칙, cap, count size 매개변수가 필수다. overlap 허용, 순환·공유 자식·고아 노드, 페이지/크기/기존 listing params 덮어쓰기를 거부한다.
3. 각 활성 노드를 Fetcher로 size=1 조회한다. cap 이상이면 **모든 자식의 보고 총건수 합 = 부모**인지 먼저 확인하고, 맞을 때만 자식을 활성화한다. 부모 probe 항목은 저장하지 않는다. 재개 시도에서도 현재 노드를 다시 count한다.
4. 자식 합계 부족/초과, total 없음, 더 나눌 수 없는 cap 이상 leaf, 첫 목록/저장 cursor total이 count와 달라지면 `incomplete`로 멈춘다. tree에서 이미 저장한 부모를 나중에 자식으로 분할하는 동작은 하지 않는다.
5. 마지막에도 split 부모와 실제 자식 보고 합계를 다시 대조한다. 원래 leaf 합계↔고유 ID, leaf 겹침, 동일 첫 페이지 점검을 그대로 거친다. tree에서는 재개 후에도 unfiltered 전체 total을 새로 읽는다.
6. Fetcher·robots·주소 차단·요청 간격·저장 식별 규칙은 변경하지 않았다. 필터가 실제 배타적인지는 총건수만으로 입증할 수 없으므로 서버의 의미 검증과 최종 ID 점검이 모두 필요하다.

수치는 맞지만 서로 겹치는 두 자식도 있을 수 있다. 합계 검사만으로 완전한 분할을 보장하지 않는다. 합성 시험은 이 경우 최종 겹침/고유 ID 대조가 `complete`를 거부하는 것도 확인한다.

## NTRS에 적용할 분할 설계

기본 축은 publicationDate가 아닌 **created**다. publicationDate 없는 글을 배제하지 않는다. 유일 ID 정렬(`sort[field]=id`, asc)을 사용한다. 문자열 사전순으로 보여도 고유한 값이면 안정적인 쪽 이동에 유리하다. ID 범위 검색이 가능하다고 추정하거나 SONAR 문법을 가져오지 않는다.

- 전체 → 조기 created(≤1899) / 정상 created(1900–2099) / 나머지(created 없음·빈 값·범위 밖).
- 정상 구간 → 연도, cap 이상 연도 → 월, cap 이상 월 → 일.
- cap 이상 일(2026-10-12: 39,954) → **전체 stiType 범주**. 05시대만 39,295이므로 시간 분할만 추가하는 것으로 해결되지 않는다.
- 범주도 cap 이상이면 같은 날짜+stiType 안에서 **배타적인 단일 center** 등으로 재분할한다. 모든 leaf는 9800 미만이어야 한다.
- stiType 또는 center가 다중값이면 단순 범주별 나눔은 겹친다. 지원되는 배타 필터/단일값 축을 확인해야 하며 임의 dedup으로 합계 검사를 끄지 않는다.
- created 잔여 필터를 API가 제공하지 않으면 전체를 덮었다고 표시할 수 없다. 정상 대안은 **전체 소스에 대한 배타·완전한 범주 트리로 계획 전체를 교체**하고 그 안에서 created/다른 축을 쓰는 것이다. 기존 날짜 leaf 옆에 unfiltered catch-all을 더하면 겹치므로 금지한다.

### 확인한 사실과 확인 못 한 문법

FINDINGS는 created `[gte]`/`[lte]`, 두 sort 필드가 동작했다고 보고한다. created 상한이 그 날짜 하루 전체를 포함하는지, 시각대 및 NULL 처리까지 확인한 기록은 없다.

openapi에서 **파라미터 이름** `stiType`, `center` 등이 보이고 응답 범주 문자열 `REPRINT` 등이 나온다는 사실과, `stiType=REPRINT`라는 요청 문법의 성공은 다르다. scalar/배열 인코딩, 대소문자 허용, AND/OR, center의 code/name 키, missing/empty/existence 및 반개방 범위 문법은 확인 못 했다. 다섯 제시 범주의 전체 합계는 517,857이며 전체보다 130,156 작다. 이 다섯 개만으로 범주 목록을 완성할 수 없고 해당 수치를 특정 날짜의 분포로 사용할 수도 없다.

## 서버 count-only 확인 목록

`count-requests.csv`에 257개 체크 행, 그중 **명시적인 후보 URL 252개**를 기록했다. 실제 실행은 하지 않았다. 5개 행은 문법/값을 확인하기 전 URL을 만들 수 없어 공란이다. 모든 URL은 `page[size]=1`, `page[from]=0`이다. 서버 담당자도 기존 Fetcher의 robots·사설주소 차단·호스트별 응답 후 1.5초 gate를 사용해야 하며, 거부·로그인·챌린지·429 또는 기존 host hold가 있으면 멈춘다. 응답 문서를 저장할 필요는 없다. URL이 있는 행도 범주 문법 등 미확인 상태 표시를 먼저 확인해야 하며 표를 무조건 전부 요청하는 실행기로 쓰면 안 된다.

| 순서 | 무엇을 확인 | 합격 기준 |
|---|---|---|
| 1 | unfiltered 전체와 시간, early/normal/잔여 | 같은 관측창에서 자식 합=전체. 이전 648,013을 하드코딩한 합격값으로 쓰지 않음 |
| 2 | created 경계 | 일자 lte의 하루 전체 포함, 인접 구간 배타성, 빈 값·NULL·0202 처리 확인. count-only 한 건으로 경계 의미가 불명확하면 미확인 유지 |
| 3 | 연도별 total; cap 이상만 월별, 다시 일별 | 모든 부모와 자식 합 일치. 미선언 cap 이상 leaf 없음 |
| 4 | 10-12의 전체 stiType, 빈 값/없는 값 | 전체 범주 manifest 확보, 합=39,954(재관측 값 기준), 배타성 확인 |
| 5 | cap 이상 범주의 center 등 | AND 필터 성공, 모든 자식 합=부모, 단일/다중값 구분, leaf <9800 |
| 6 | 실행 전 unfiltered 재조회 | 처음과 끝 global이 다르면 고정 snapshot 또는 재측정 후 재검증; 실패를 완료로 처리하지 않음 |
| 7 | 정렬 키 유일성 검증(본 수집 전 별도 승인 범위) | 같은 구간을 id 정렬과 기존 날짜 정렬로 받아 고유 ID 집합 비교. 날짜 동률·기본 1월1일 쏠림과 쪽 이동 반복을 확인 |

**size=1 count-only 요청만으로 구간 간 ID 겹침 0, 정렬 키의 전 구간 유일성, 집합의 완전성을 직접 확인할 수 없다.** 7번은 이번 count-only 표의 실행 범위가 아니며 추후 서버가 목록 ID를 검증할 때 할 일이다. 1–6의 건수·의미 검증에 이어 실제 수집 중 leaf 고유 ID 대조/겹침 점검까지 통과해야 한다. 고정 snapshot이 없고 실시간 유입이 지속되면 건수가 맞아도 완전한 원자적 스냅샷을 보장하지 못한다.

### 요청 수와 시간 예산

- 현재 URL 252개를 모두 측정하는 경우 간격만 최소 `(252−1)×1.5 = 376.5초`(6분16.5초), 평균 응답 0.5초 가정 시 약 8분22.5초. robots 최초 1회 및 미확인 문법 확인/추가 분할은 별도다. 적응형 측정은 불필요한 하위 구간을 생략한다.
- 실측 전 확정 요청 수는 알 수 없다. 총건수 T=648,013, page size=100이면 데이터 페이지 하한 6,481. leaf L개라면 페이지 반올림 상한 `6481 + L−1`이다. 내부 split 노드 I개에 대해 count probe 상한 `2(I+L)−1`, 종료 확인 약 `3L`, 최종 total 1회와 robots 1회를 더한다(전송 오류/재개 제외).
- **추정 예시** I=10, L=250이면 약 8,001요청 이내. 이는 확인된 범주 수/실측값이 아니다. 간격만 약 3시간20분, 평균 응답 0.5초면 약 4시간27분에 저장·파싱·gate 부가비용이 추가된다. 계수 합산 반올림의 느슨한 상한이다.
- 초안 `max_requests=50000`, `max_runtime_s=345600`(96시간), `max_pages=null`. 예시보다 여유를 두었지만 무제한 완료 보장은 아니다. 평균 응답 5초라면 50,000요청만으로 약 90시간이므로 backoff/DB/파일까지 더하면 runtime이 먼저 닿을 수 있다. retries=3으로 인한 추가 시도와 재개 때의 probe도 요청 예산에 포함된다.
- 한도 도달 시 그 checkpoint에서 `incomplete`로 끝내고 같은 검증된 plan/state로 이어받는다. 이전 두 번의 한도 소진을 피하려면 서버가 실측 I/L/응답시간으로 예산을 다시 계산해야 한다. 파일 수신 비용은 이 목록 수집 예산에 포함하지 않고 `--files`를 사용하지 않는다.

## plan 초안과 이어받기

`plan-second-pass.draft.json`: 253개 노드의 **불완전한 서버 검증용 골격**. 200개 연도와 알려진 2026년 10월 12일 분할 경로를 명시했다. 다른 연도도 cap 이상이면 월/일/범주 자식을 더 선언해야 한다. 범주 미완성 및 created 잔여 노드의 `params=null`은 의도적인 미해결 표시다. 실행 검증은 `partition_tree_requires_server_verification`으로 거부한다. flag만 바꿔도 null params 검증에 걸린다. 확인 못 한 값을 추측으로 채워 실행 가능하다고 만들지 않았다.

서버 담당자는 count 표와 의미 검증으로 노드를 완성하고 모든 params를 검증한 뒤에만 flag를 켠다. 모든 자식은 부모 범위를 포함한 누적 필터를 가져야 한다. 빈 잔여 구간을 삭제하려면 같은 snapshot에서 그 구간이 0이고 전체 분할이 배타·완전하다는 근거를 남긴다.

- 문서 대상: 첫 run과 **동일한 격리 문서 DB/파일 backend 및 blob 폴더**를 명시한다. 기존 590,634행을 읽거나 수정한 시험은 하지 않았다. 따라서 실데이터 중복 0은 아직 확인 못 했다.
- 상태: 기존 상태를 보존하고 새 sibling `enumeration-ntrs-created-v2/`를 권장한다. 목록을 다시 읽어 전체 집합을 검증하되 같은 `site_id`, 원본 `str(id)` post_number/external_id, `/citations/{id}` URL을 유지한다. 기존행은 저장 경로의 dedup으로 재삽입하지 않고 부족분만 새로 저장한다. 이는 **이미 저장된 목록에 대한 HTTP 요청까지 생략한다는 뜻은 아니다**.
- `plan.py:42, 77–84`: listing·segments·adapter 계약이 fingerprint에 반영되므로 이전 plan과 다르다. `store.py:100–120`은 plan별 segment 세대를 분리하지만 site 상태행은 한 개이므로 다른 plan을 같은 state에서 번갈아 실행하지 않는다. 기존 state를 복사하거나 조작하지 않는다.
- `adapters.py:398–430`: 같은 키의 저장행을 찾고 post_number가 다른 문서와 충돌하면 저장 거부한다. 기존 dedup 및 날짜 규격화 경로를 그대로 사용한다. 합성 시험에서 첫 10행→새 state/다른 fingerprint/동일 DB에 새 1행을 추가해 총 11행, post_number 중복 0을 확인했다. NTRS 등록 어댑터도 새 state/동일 DB 재실행 10→10행을 확인했다.
- 새 state가 요청 gate/host hold를 잃지 않도록 기존 보류 목록을 **동일하게 seed**하고 기존 차단을 유지해야 한다. 원래 run과 동시 실행하지 않고 NTRS 호스트를 서버들까지 하나의 gate로 직렬화한다. 새 상태 폴더로 요청 간격이나 접근 보류를 우회하면 안 된다. 본 작업에서는 서버 실행 명령을 수행하지 않았다.
- 실제 590,634행과 총건수가 관측 중 바뀌면 신규 건수는 57,379로 고정되지 않는다. 기존 결함행/식별 충돌은 수동 확인 대상으로 보고하며 삭제·보정하지 않는다.

## 날짜 원문과 규격화

기존 `_parse_search_results`와 `_paper_from_result`는 그대로 유지했다. 새 초안만 opt-in `_paper_from_result_with_source_dates`를 사용한다(`crawler/sites/custom/ntrs-nasa-gov-search.py:411–432`). 선택 순서는 원래대로 첫 publications.publicationDate → submittedDate, listed는 distributionDate → submittedDate다. created를 published_date로 대체하지 않는다. submittedDate가 실제 발행일이라는 의미는 **확인 못 함**이다.

새 메서드는 선택한 전체 원문을 공통 정규화기로 넘긴다. SQLite의 `published_date_raw/status/precision`, `listed_date_raw/status/precision`에 원문/상태/정밀도를 보존하고 날짜 컬럼에는 완전하고 유효한 날짜만 넣는다. 연도만 있으면 임의 1월1일을 채우지 않는다. 원문 timestamp를 하루로 자르던 기존 NTRS 메서드는 이전 plan 호환을 위해 수정하지 않았다.

`metadata.source_dates`에는 publicationDate/submittedDate/distributionDate/created와 원본 publications를 제공한다. **현 v2 SQLite/PG 저장 경로는 metadata 전체를 문서 컬럼에 저장하지 않는다.** 선택한 raw 날짜는 위 전용 컬럼으로 보존되지만, 선택되지 않은 원문 전체가 DB에 영속된다고 주장하지 않는다. 이를 영속 감사할 필요가 있으면 metadata 지원 backend 또는 별도 승인된 원문 감사 저장 설계가 필요하다. 저장 backend와 PG 실데이터는 이번에 확인하지 않았다.

## 첫 mismatch 원인 가설과 두 번째 수집에서의 처리

| 가설 | 근거 | 확인 못 한 것/조치 |
|---|---|---|
| 날짜 동률 정렬로 페이지 이동 중 중복/누락 | 첫 plan은 `sort[field]=publications.publicationDate`; 이전 NTRS 동일 날짜 652건 함정과 같은 위험 | 이번 12구간의 실제 ID/원응답은 없음. id 정렬과 구간 합계 대조로 재열거; 서버에서 두 정렬 집합 비교 |
| publicationDate가 다중 publications 값과 매칭되어 서로 다른 날짜 leaf에 같은 ID 등장 | native 변환은 첫 publications 날짜만 선택, query는 다중값일 가능성 | 실제 NTRS 필터의 다중값 매칭은 미확인. created의 단일값/경계 의미부터 검증 |
| lte 경계, 잘못된 필터, 시점 차이 | leaf 합 590,692가 별도 published 범위 보고 590,670보다 22 많음 | 스냅샷 및 실제 요청 로그가 없어 어느 원인인지 확정 못 함 |
| 부모를 읽다가 뒤늦게 분할하면서 이미 본 ID를 다시 봄 | 현 코드의 초기 proactive split은 부모 항목을 저장하지 않음. 반면 listed<total 이후 late split은 이미 저장한 부모를 자식이 다시 읽을 수 있음 | skipped_known 82,317을 부모 split 97개/133,484와 직접 동일시할 근거 없음. 기존 DB/ledger 재사용 등도 가능. tree는 저장 전 count/split하므로 이 경로를 차단 |
| 유입·삭제 등 실행 중 source 변경 | 장시간 열거, snapshot 고정 확인 없음 | 시작/끝 및 부모·자식 total 대조; 변동 시 미완료/불일치. same-count 내용 교체까지 count로 검출할 수 없음 |

공유 키 55가 중복 출현 횟수 55와 같지 않으므로 `58−55=3건 추가 누락`이라고 계산하면 안 된다. 같은 키가 세 구간 이상에 나타날 수 있고, 복합 원인도 가능하다.

12개 mismatch 날짜: 1963-05-01, 1964-01-01, 1965-01-01, 1965-04-01, 1965-06-01, 1965-07-01, 1965-08-01, 1974-01-01, 1975-01-01, 1975-10-01, 1976-01-01, 1983-08-01. 두 번째 run은 이 published 구간만 재시도하는 것이 아니라 전체 created/잔여 집합을 고유 id 정렬로 다시 열거한다. 기존 저장행을 dedup하고 모든 부족분을 함께 메우는 설계다. 최종 전체 unique/global/leaf sum/overlap, 저장행 post_number 중복 및 실패 0을 확인한 뒤에만 실제 완료로 인수한다. 기존 저장행 날짜를 소급 보정하지 않는다.

## 변경 파일

- `crawler/enumeration/partitions.py`: 명시적 검증 분할 트리의 plan 검증.
- `crawler/enumeration/plan.py`: opt-in 모드 연결.
- `crawler/enumeration/strategies.py`: count-before-split, strict cap, 부모·자식 및 기존 ID 대조 연결.
- `crawler/sites/custom/ntrs-nasa-gov-search.py`: 기존 메서드 유지, 새 plan 전용 원문 날짜 메서드 추가.
- `tests/test_enumeration_partition_tree.py`: 네트워크 없는 신규 14개 시험.
- `docs/ntrs-undated/report.md`, `plan-second-pass.draft.json`, `count-requests.csv`: 설계와 인계 자료.

## 인계 파일과 커밋

- `plan-second-pass.draft.json`: 의도적으로 실행 거부되는 서버 검증용 초안.
- `count-requests.csv`: 개수만 확인할 후보 요청/미확인 체크 목록.
- `validation.json`: 오프라인 plan 거부 코드와 fingerprint 비교.
- `baseline.log`, `tests.log`: 네트워크 없는 Docker 시험 전체 로그.
- `commits.txt`: 기준 및 단계별 로컬 커밋.

구현 커밋: `2cb5091` (partitions.py, plan.py, strategies.py, NTRS opt-in 메서드, 신규 테스트). 문서 커밋은 인접 commits.txt에 기록한다. 알려진 미완료는 실사이트 필터 문법·범주 완전성·실제 648,013건 수집 및 실DB 중복 검증이며, 이번 오프라인 작업의 확인 범위 밖이다.
