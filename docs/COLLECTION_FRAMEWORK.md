# 전체 수집 공통 틀 (crawler/enumeration)

목표: 등록된 수집기 사이트에서 **수집 가능한 전체 데이터를, 가장 오래된 항목부터, 빠짐없이** 수집한다.
기존 수집기 대부분은 "최신순으로 200페이지까지 읽고 멈춤" 구조라 이 목표를 만족하지 못한다.
이 틀은 사이트별 열거 전략(정찰/판정으로 정해진 4종)을 **설정(plan.json)으로 구동**하는 독립 모듈이다.

`crawler/incremental.py`(기존 영속 큐 엔진)에는 의존하지 않는다. 그 엔진의 검토 결함
(어댑터/계약 변경 시 영구 차단, 목록 밀림 시 영구 정지, 저장 불가 항목 하나가 큐를 막음,
상한에서 멈추면 발견한 항목이 저장되지 않음)을 아래 설계에서 각각 피한다(§9).

## 1. 모듈 구조

```
crawler/enumeration/
  __init__.py      공개 API
  urls.py          URL 조립(이중 인코딩 방지), 페이지 파라미터 치환
  http.py          Fetcher: 사이트별 요청 게이트(간격·예산·재시도·차단 판정·robots)
  parsing.py       목록 파서(html 선택자 / json 경로 / 어댑터 메서드), 총건수 추출
  store.py         SQLite 상태 저장소: 사이트 상태, 구간, 항목 대장(실패 포함), 파일 대장
  adapters.py      기존 수집기의 '상세 파싱/저장' 재사용 층 (generic 설정, 커스텀 파서 메서드)
  strategies.py    4개 열거기 (last_page_backward, date_range_split, oldest_first_sort, sitemap)
  runner.py        SiteRunner: 열거기 배치 → 항목 처리 → 체크포인트, 완료 판정
  files.py         파일(PDF/첨부) 별도 패스
  report.py        완전성 점검 보고(JSON/CSV)
  plan.py          plan.json 로드/검증
  legacy.py        기존 수집기 공통 결함을 설정으로 덮어쓰기(200페이지 상한 등)
  run.py           CLI: python -m crawler.enumeration.run --site <id> --plan <plan.json>
```

## 2. 실행 흐름

```
plan.json ─► SitePlan ─► Strategy.batches(progress)  ── yields Batch(segment, items[오래된→최신], checkpoint, segment_done?)
                               │                                   │
                           Fetcher(게이트)                     SiteRunner.process(batch)
                                                                  ├ 항목마다: 대장에 saved 이면 건너뜀
                                                                  ├ adapter.materialize(item) (상세 요청은 Fetcher 경유)
                                                                  ├ adapter.save(paper)  → 기존 BaseCrawler._save_paper 경로
                                                                  ├ 실패는 항목 대장에 사유와 함께 기록하고 계속
                                                                  └ 배치 끝: store.checkpoint(segment, cursor)  (페이지 단위 영속)
```

* 열거기는 **제너레이터**다. 배치(대개 한 목록 페이지)를 내보내면 러너가 그 배치의 항목을 **즉시 저장**하고
  나서야 체크포인트를 커밋한다. 따라서 "발견만 하고 저장 못 함"이 없다. 중간에 예산/시간이 끝나면
  마지막으로 커밋된 체크포인트에서 재개한다(이미 저장된 항목은 대장에서 즉시 건너뜀).
* 문서 저장(DB)과 체크포인트(상태 DB)는 서로 다른 DB라 원자적이지 않다. 순서를 "문서 저장 → 체크포인트"로
  고정했기 때문에 그 사이에서 죽으면 다음 실행이 같은 페이지를 다시 읽고, 기존 중복 판정
  (`find_by_dedup_key`: site_id+post_number+meta_url)과 항목 대장이 중복 저장을 막는다.

## 3. 요청 게이트 (http.py)

* 모든 요청은 `Fetcher.get()` 하나로만 나간다. 내부에서 `delivery.health.request_gate.RequestGate`
  (파일 잠금 + state.json, 정찰기가 쓰는 것과 같은 구현)를 **호스트별 루트**로 재사용한다.
  * 같은 호스트: 프로세스/스레드가 달라도 순차. 이전 응답 종료 후 최소 `request_interval_s`(기본 1.5초) 대기.
  * 다른 호스트: 서로 다른 게이트라 병렬 가능(CLI `--workers`).
* 예산: `max_requests`(사이트별 요청 수), `max_runtime_s`(벽시계). 초과 시 `BudgetExhausted` → 러너가
  완료 표시 없이 정상 중단(저장분 보존, 체크포인트 유지).
* 응답 분류
  | 상황 | 분류 | 처리 |
  |---|---|---|
  | 2xx | 성공 | |
  | 404, 410 | `PermanentFailure(http_404/http_410)` | 항목: 대장 기록 후 계속. 목록 탐색: 끝 판정 근거 |
  | 기타 4xx(400, 405, 422…) | `PermanentFailure(http_4xx)` | 같음 |
  | 전송 오류, DNS 해석 실패(`dns_unresolved`), 5xx, 429 | 재시도(`retries`, 지수 대기, 429와 503은 Retry-After 존중·상한) 후 `TransientFailure`. 503의 Retry-After가 `max_retry_wait_s`보다 길면 기다리지 않고 호스트 게이트에 쿨다운을 쓰고 `RateLimited(unavailable_retry_after)`로 멈춘다 | 항목: 대장(일시 실패, 시도 횟수) 후 계속, 다음 실행에서 `item_max_attempts`까지 재시도. 목록: 해당 실행 중단(미완료) |
  | 401/403/407/451, JS/봇 챌린지 본문(`access_challenge`), 로그인 경로 리다이렉트, robots 거부 | `AccessBlocked` | **우회하지 않는다**. 사이트 상태를 `access_blocked`로 기록하고 그 사이트 진행을 멈춘다. 이후 실행도 사람이 검토해 `--refresh`로 풀기 전까지 요청하지 않는다 |
* robots.txt: 호스트별 1회(같은 게이트 경유) 조회. 404/410 등 4xx는 "규칙 없음", 401/403/407/451은 호스트 보류(H09).
  5xx/429/전송 오류(재시도 후)는 허용을 추측하지 않고 `RobotsUnavailable`(`robots_unavailable:<세부>`)로 사이트 실행을
  멈추고(`incomplete`, 재개 가능, 호스트 보류 아님) 사이트 상태에 `cooldown_until`(지금+300초)을 남겨 다음 실행도 그동안 요청하지 않는다. robots.txt가 본문 상한을 넘어도 같은 정지. 같은 Fetcher 안에서 그 출처의 robots는 `ROBOTS_RETRY_S`(300초)
  동안 다시 요청하지 않고 같은 정지를 낸다(URL마다 robots 재요청 방지). 파일 패스도 같은 정지로 멈춘다(시도 횟수 미증가).
  `respect_robots: false`는 plan에서 명시할 때만.
* robots 규칙 해석은 표준 `urllib.robotparser`가 아니라 직접 구현한 RFC 9309 매처(`http.parse_robots`)다:
  우리 제품 토큰(User-Agent의 첫 제품명, 기본값이면 `Mozilla`, 대소문자 무시)과 **값 전체가 같은** 그룹이 있으면
  (`Mozilla/5.0 (compatible; Yeti/1.1)`처럼 다른 수집기의 UA 문자열 전체는 우리를 고르지 않는다) 그 그룹들(합침), 없으면 `*` 그룹들. `Chrome`·`Safari` 등 브라우저 UA의 다른 단어는 그룹을 고르지 않는다
  (더 느슨한 그룹이 엄격한 `*`를 대신하지 않게). Crawl-delay는 UA 부분 문자열이 일치하는 그룹과 `*` 그룹 값 중 **큰 값**을 쓴다(간격을 줄이지 않게).
  경로+질의에 대해 **가장 긴 패턴**이 이기고 길이가 같으면 Allow. `*`(임의 문자열)와 끝의 `$`(끝 고정) 지원.
  비교 전 퍼센트 인코딩 정규화(비ASCII는 UTF-8 `%XX`, 비예약 문자 `%XX`는 해제, 예약 문자 `%2F` 등은 유지).
  빈 `Disallow:`는 규칙 없음(허용). user-agent 줄 앞의 규칙은 어느 그룹에도 속하지 않는다.
  해석 중 예외가 나면 허용으로 보지 않고 `robots_unparseable`로 사이트를 멈춘다(호스트 보류는 아님).
  robots.txt가 다른 호스트로 리다이렉트돼도 Crawl-delay는 원래 호스트 간격에 적용한다.
* 호스트 정규화: 요청·리다이렉트 hop마다 국제화 도메인(IDN)은 IDNA(punycode)로 바꿔 게이트·robots·보류·요청에 쓴다.
  게이트가 다룰 수 없는 호스트(밑줄 등)는 요청하지 않고 `PermanentFailure(ungovernable_host)` — 항목이면 대장,
  목록이면 `listing_failed`(미완료). robots.txt가 그런 호스트로 리다이렉트되면 `robots_unavailable` 정지.
* 본문 크기 상한(`RequestsTransport.max_bytes`, 파일 패스는 `file_max_bytes`) 초과는 `PermanentFailure(body_too_large)`로
  재시도하지 않는다(같은 큰 본문을 반복 다운로드하지 않음). `Content-Length`가 상한을 넘으면 본문을 읽기 전에 끊는다.
* 전송 계층(`Transport`)은 주입 가능하다. 운영은 `RequestsTransport`(리다이렉트 추적, 본문 상한),
  테스트는 가짜 서버(인프로세스 라우터 또는 127.0.0.1 http.server)만 쓴다.

## 4. 상태 저장 스키마 (store.py, SQLite 파일 `<state-dir>/enumeration.sqlite`)

```sql
enum_site(site_id PK, plan_fp, strategy, status, stop_reason, state JSON, started_at, updated_at, completed_at)
  -- status: running | incomplete | complete | complete_with_mismatch | access_blocked | config_error | failed
enum_segment(site_id, plan_fp, segment_key, parent_key, ordinal, status, cursor JSON,
             reported_total, listed, saved, evidence, detail JSON, updated_at,
             PRIMARY KEY(site_id, plan_fp, segment_key))
  -- status: pending | running | complete | mismatch | split | cap_unsplittable | failed
enum_item(site_id, item_key, status, seq_id, reason, attempts, url, segment_key, payload JSON, updated_at,
          PRIMARY KEY(site_id, item_key))
  -- status: saved | failed_permanent | failed_transient | failed_retries_exhausted
enum_file(site_id, seq_id, item_key, url, role, status, size, sha256, pages, content_type,
          attempts, reason, path, updated_at, PRIMARY KEY(site_id, seq_id, url))
  -- status: pending | done | failed_transient | failed | unavailable
enum_event(id PK, site_id, at, kind, detail JSON)   -- 접근 차단, 구간 불일치, 분할, 시작 페이지 불일치 등
```

* `plan_fp`: 전략·목록 정의의 지문. plan이 바뀌면 **새 구간 세대**로 다시 열거한다(차단하지 않음).
  항목 대장은 `plan_fp`와 무관하게 유지되므로 이미 저장된 항목은 상세 요청 없이 건너뛴다.
  (기존 엔진의 "계약 변경 → 명시 초기화 전까지 영구 차단" 결함을 피함)
* `enum_item`은 중복 제거 집합이자 **실패 대장**이다. 최종 보고의 "받지 못한 항목 수와 사유별 분포"는
  `status<>'saved'` 행을 `reason`별로 센 것이다.
* 완료 표시는 열거기가 **근거(evidence)** 와 함께 소스 소진/구간 대조를 보고할 때만 `complete`.
  예산·시간·페이지 상한으로 멈추면 `incomplete` + `stop_reason`.

## 5. 전략별 알고리즘

공통: 목록 페이지는 `listing` 설정으로 파싱해 `Item(key, url, date, title, payload)` 목록을 얻는다.
`key` 기본값은 상세 URL(프래그먼트 제거), `id_regex`/JSON 키로 바꿀 수 있다. 페이지 지문 = 키 목록의 해시.

### 5.1 last_page_backward (마지막 페이지부터 거꾸로)

1. 시작 페이지 `S`(`start_page`: 0 또는 1)를 읽는다. 비어 있으면 미완료(`empty_first_page_unverified`).
2. (첫 페이지 누락 방지) `check_unparameterized_first_page`가 켜져 있으면 페이지 파라미터 없는 기본 URL을
   읽어 `S` 페이지와 키 집합이 같은지 확인. 다르면 `start_page_mismatch` 이벤트 + `config_error`로 중단
   (예: 실제 첫 페이지가 0인데 1부터 읽는 설정).
3. 마지막 페이지 탐색 — 단조 술어 `beyond(p)`:
   `beyond(p) = 빈 페이지 ∨ 404/410 ∨ fp(p)=fp(S) (첫 페이지로 되돌아감) ∨ fp(p)=fp(p-1) (clamp)`
   * 실제 페이지 `p ≤ L`은 비어 있지 않고, 첫 페이지와 다르며, 앞 페이지와 다르다.
   * `p > L`이면 빈 페이지/404(empty), 첫 페이지(wrap), 또는 마지막 페이지 복사본(clamp: fp(p)=fp(p-1)=fp(L)).
   * 지수 탐색(S+1, S+2, S+4, …)으로 `beyond`인 상한을 찾고 이진 탐색으로 `L`을 찾는다. 요청 수 ≈ 2·log2(L).
     clamp 판정에 p-1 페이지가 필요하면 추가로 읽는다(캐시).
   * `L+1`의 관측으로 끝 종류(`empty|not_found|wrap|clamp`)를 기록한다.
   * `max_pages`가 설정되어 있고 상한까지 `beyond`가 아니면 `page_cap_reached`로 기록하고 **완료 아님**.
4. 거꾸로 읽기: `k = L … S`. 단계마다 **페이지 k를 읽은 다음 k+1을 다시 읽는다**(overlap_pages=1).
   최신순 목록에 새 글이 올라오면 항목이 높은 번호 쪽으로 밀리는데, k를 읽기 전에 k에서 k+1로 밀려난 항목은
   k+1 재독에서 잡히고, k를 읽은 뒤 밀려난 항목은 이미 읽은 것이다. 배치는 `미수집(k+1 재독) + 미수집(k)`을
   각 페이지 안에서 역순(오래된 것 먼저)으로 낸다. 재독한 k+1에 이미 본 항목이 하나도 없으면(한 단계에 한 페이지
   이상 밀림) 이미 본 항목이 나올 때까지 k+2, k+3…을 더 읽는다(`max_overlap_walk`). 삭제로 낮은 번호 쪽으로
   밀리는 경우는 이미 본 항목이 다시 나타날 뿐이라 키 중복 제거로 충분하다.
5. 체크포인트: `{phase, last_page, end_kind, next_page}`를 페이지마다 저장. 재개 시 `next_page+1`을 재독해
   실행 사이에 밀린 항목도 잡는다.
6. 완료: `S`까지 처리하면 `source_exhausted` 근거(`last_page_<end_kind>`)로 완료. 목록에 총건수가 있으면
   (`listing.total`) 고유 키 수와 대조해 불일치면 `complete_with_mismatch`.

### 5.2 date_range_split (연도/월/날짜 구간 분할)

1. 구간 생성: `from`~`to`(기본 오늘)를 `granularity`(year|month|day) 단위로, **오래된 구간부터**.
   `explicit` 모드는 plan에 적힌 구간(키 + 파라미터) 목록을 그대로 쓴다(분할 불가).
2. 필터 파라미터 매핑: `range`(from_param/to_param + 날짜 형식) 또는 `parts`(year_param/month_param/day_param).
   URL은 `urls.build_url`로 조립: 기본 URL의 기존 질의(`f%5B0%5D=...`)와 plan의 파라미터를 **한 번만 디코드한 뒤
   한 번만 인코드**한다. plan에 이미 인코딩된 키(`type%5B170%5D`)를 써도 `%255B`가 되지 않는다.
3. 구간 내부는 페이지(또는 offset)로 앞에서부터 끝(빈 페이지/404/총건수 도달/페이지 반복)까지 읽고 페이지마다
   저장·체크포인트. 구간 내부 순서는 소스 순서다(최신순이면 페이지 안에서 역순으로 낸다). 구간 간 순서는 오래된 순.
4. 대조: 구간 첫 페이지의 사이트 보고 총건수 `T`와 구간의 고유 목록 수 `N`.
   * `T > hidden_cap`(예: 10000) → 읽지 않고 즉시 하위 구간(year→month→day)으로 분할(`split`).
   * 읽은 뒤 `N < T`이고 더 쪼갤 수 있으면 분할(숨은 상한 의심). 이미 저장된 항목은 대장에서 건너뛰므로 비용은 목록 요청뿐.
   * `N == T` → `complete(total_matched)`. 총건수 없음 → 항목이 있으면 `complete(exhausted_no_total)`,
     빈 구간이면 `unverified`(필터 거부/파서 고장과 구분 불가 — 사이트는 `complete_unverified`, 보고서에 구간 목록).
   * 그 외 → `mismatch`(T, N 기록). 일 단위에서도 상한 초과면 `cap_unsplittable`.
5. `overall_total_check`가 켜져 있으면 구간을 다 읽은 뒤 필터 없는 목록의 총건수를 한 번 읽어 고유 목록 수와 비교
   (날짜 없는 항목, 구간 밖 항목, **구간 경계 누락** 탐지). 모든 구간이 complete/split이고 불일치가 없으면 사이트
   complete, 불일치가 있으면 `complete_with_mismatch`.
   * 기본값: `range` 필터(비 explicit) + `listing.total`이 있는 plan에서는 키가 없으면 **켜진 것으로** 본다
     (목록 URL에 `{segment}`가 있으면 필터 없는 목록을 만들 수 없어 끔). plan 지문은 바뀌지 않는다(진행 상태 유지).
     구간별 총건수는 사이트가 같은 필터로 센 값이라 경계에서 빠진 항목을 드러내지 못하기 때문이다.
   * 그런 plan에서 검사가 꺼져 있으면(`false` 명시 → `overall_total_check_disabled`, `{segment}` URL →
     `overall_total_not_checked`) 완료는 `complete_unverified`. 검사했는데 필터 없는 목록에서 총건수를 못 읽으면
     `complete_unverified(overall_total_unavailable)`.
   * 모든 구간이 비었으면 `overall_total_check: true`를 명시하고 전체 총건수가 0일 때만 완료로 본다(그 외 `incomplete`).
   * 구간 끝 경계: `segments.filter.to_inclusive`(기본 `true`) — `true`면 `to_param`=구간 마지막 날(양끝 포함),
     `false`면 `to_param`=다음 날(반열린 구간 `[from, to)`, 사이트가 끝을 배타로 셀 때). 사이트가 어느 쪽인지
     plan에 명시하라. 잘못 고르면 경계일 항목이 빠지고, 위 전체 총건수 검사가 `overall_total` 불일치로 드러낸다.
     `to`가 `now`가 아닌 고정 구간이면 그 뒤 항목도 불일치로 잡히므로 검사를 끄면(`false`) `complete_unverified`가 된다.
   * **문자열 접두 범위 트리**(`segments.filter.type: "query_prefix_range"`): 날짜로 나눌 수 없고 id 범위 질의가 되는
     소스용. 소스가 id를 **문자열 사전순**으로 비교하면(SONAR `pid`로 확인: `[0 TO 99999]`, `[100000 TO 999999]`,
     `[1000000 TO 9999999]`가 모두 거의 전체를 돌려줌) 정수 중점 이분은 구간이 겹쳐 쓸 수 없다. 대신 반개방 문자열 구간
     `[A, B)`를 쓴다(질의 `<param>=<field>:[A TO B}`, `:`는 `9` 다음 ASCII 문자). plan: `segments.hidden_cap`(양의 정수),
     `segments.max_depth`(1~18), `filter.param`/`field`, `listing.total`, `overall_total_check: true`
     (`from`/`to`/`granularity`/`split_to`는 쓰지 않는다).
     루트는 `<field>:0-:`(= `[0, :)`, 숫자로 시작하는 모든 id)이고 첫 자리 숫자로 10개(`[d, d+1)`, 9는 `[9, :)`)로 나뉜다.
     구간 키는 `<field>:<lo>-<hi>`라 재개해도 같은 구간이 나온다. 구간 총건수(`listing.total`)가 `hidden_cap` 이하면
     leaf, 초과면 `[p, hi)`를 `[p, p0), [p0, p1), …, [p8, p9), [p9, hi)` 11개로 나눈다(서로소, 합 = 부모, 마지막 자식은
     부모의 상한 유지). 첫 자식 `[p, p0)`에는 id가 정확히 `p`인 문서만 들어 있어 id가 유일하면 상한을 넘을 수 없다.
     총건수 0이면 첫 쪽 재확인 1회 뒤 `total_matched`로 완료. `lo`가 `max_depth` 자리에 이르렀거나 첫 자식인데 상한을
     넘으면 읽지 않고 `cap_unsplittable`. 숫자로 시작하지 않는 id는 어느 구간에도 안 잡혀 전체 총건수 대조(`overall_total`)에서
     불일치로 드러난다. 전체 총건수·leaf 합계·겹침·구간별 총건수↔고유 id 대조는 날짜 분할과 같다.
     분할되는 구간마다 첫 쪽 요청이 한 번 들고(그 쪽은 저장하지 않음), leaf마다 마지막 쪽 뒤의 끝 확인 읽기 3번
     (빈 쪽 2번 + 다음 쪽)이 더 든다.

### 5.3 oldest_first_sort (정렬 파라미터/API)

* 프리셋: `wordpress`(`/wp-json/wp/v2/<type>?orderby=date&order=asc&per_page=100&page=N`, 총건수 `X-WP-Total`,
  마지막 다음 페이지의 400 `rest_post_invalid_page_number`를 끝으로 인정), `ckan`(`package_search?sort=metadata_created asc&rows&start`,
  `result.count`), `dspace7`(`/server/api/discover/search/objects?sort=dc.date.accessioned,ASC&page&size`, `page.totalElements`, 시작 0),
  `generic`(직접 지정). 프리셋 값은 plan에서 덮어쓸 수 있다.
* 앞에서부터 읽는다. 끝 후보: 빈 페이지, 404/410, 설정된 끝 상태코드(`end_statuses`), 페이지 반복(clamp/wrap).
  끝 후보는 한 번 다시 읽고(일시적 빈 쪽이면 계속 진행), 다음 쪽도 비어 있어야 끝으로 확정한다. 다음 쪽에 항목이 있으면
  `empty_page_inside_range`로 미완료. 그 밖의 4xx는 끝이 아니라 실패(미완료)다.
* 정렬 무시 감지: 내보낸 항목의 날짜가 거꾸로 가면(`max_order_violations`, 기본 0) `sort_order_not_ascending`으로 멈춘다.
* `overlap_pages`(기본 0): 켜면 p를 읽은 뒤 p-1을 재독해 삭제로 앞당겨진 항목을 잡는다.
* 완료: 소진 + (총건수 없음 또는 고유 목록 수 = 총건수). 불일치면 `complete_with_mismatch`.

### 5.4 sitemap

* `sitemap_url`(인덱스 또는 urlset) 또는 robots.txt의 `Sitemap:`에서 시작. 인덱스를 따라가며 자식 sitemap 하나를
  구간 하나로 체크포인트한다. `.xml.gz`는 해제.
* `include`/`exclude` 경로 정규식(범위 필터), `index_include`(자식 sitemap 필터).
* **lastmod는 게시일이 아니므로 순서 근거로 쓰지 않는다**(payload에 참고로만 보관). 순서가 필요하면
  `order_by_url_number`(URL 속 숫자 id 정규식)로 자식 sitemap 안에서 오름차순 정렬한다. 아니면 문서 순서.
* 대조: 자식별 필터 통과 URL 수 = 구간 총건수. 모든 자식이 처리되면 complete.
* URL이 하나도 없는 urlset(`sitemap_empty_urlset`), 자식이 하나도 없는 인덱스(`sitemap_index_empty`)는 구간
  `unverified`이고, 사이트 전체에서 목록에 오른 항목이 0이면(필터로 전부 빠진 경우 포함) `complete_unverified`
  (`sitemaps_listed_nothing`). URL은 있지만 필터에 하나도 안 걸린 자식은 그 자체로는 `complete`.
* 200인데 XML이 아님/깨짐/잘린 gzip(`invalid_xml:*`, `unknown_xml_root`)은 그 실행에서 `failed`(사이트 `incomplete`)이고,
  다음 실행 시작에 다시 `pending`으로 돌린다(구간 `detail.attempts`, `sitemap.max_attempts` 기본 3회까지). 404 등
  `PermanentFailure`는 재시도하지 않는다.
* gzip 해제 크기 상한 `sitemap.max_uncompressed_bytes`(기본 64 MiB; 규격 상한 50 MB). 넘으면 `sitemap_too_large`로
  `failed`(재시도 안 함, 사이트 `incomplete`).

## 6. 항목별 실패 대장

| 원인 | 대장 상태 | 사유 코드 | 진행 |
|---|---|---|---|
| 상세 404/410 | failed_permanent | `http_404`, `http_410` | 계속 |
| 기타 4xx | failed_permanent | `http_4xx` | 계속 |
| 파서 예외 | failed_permanent | `parse_error:<예외형>` | 계속 |
| 파서가 빈 결과(None/제목·URL 없음) | failed_permanent | `empty_item` | 계속 |
| 저장 예외 | failed_permanent | `save_error:<예외형>` | 계속. 단 연속 `max_consecutive_save_errors`(기본 5)회면 저장 경로 고장으로 보고 사이트 중단(`config_error`) |
| 전송 오류/5xx/429 (재시도 후) | failed_transient → 다음 실행에서 재시도, `item_max_attempts` 도달 시 failed_retries_exhausted | `transient:<세부>` | 계속 |
| 상세 URL 하나가 robots 금지 | failed_permanent | `robots_disallowed` | 요청하지 않고 계속. 단 연속 `max_consecutive_robots_disallowed`(양의 정수, 기본 5)건이면 상세 페이지 전체가 금지된 것으로 보고 사이트 중단(`access_blocked:robots_disallowed_consecutive`, 호스트 보류 아님; 목록 URL 자체 금지는 `access_blocked:robots_disallowed`). 끝까지 돌아도 금지 항목 비율이 `max_robots_disallowed_ratio`(기본 0.0)를 넘으면 `complete_with_review`, 건수는 보고서 `robots_disallowed`와 warnings에 표시 |
| 401/403/챌린지/로그인, 목록·사이트맵 URL의 robots 금지 | (사이트 단위) | `access_blocked:<세부>` | **사이트 중단**, 우회 없음 |

* 한 실행 안에서 같은 항목은 한 번만 시도한다(겹쳐 읽기로 두 번 나와도 Fetcher가 이미 재시도했으므로).
  일시 실패 항목은 다음 실행 시작의 재시도 패스에서 다시 시도한다. 완료된 사이트를 다시 실행하면
  `--refresh`가 없는 한 재열거하지 않고 재시도 패스만 돈다(`--retry-failed`는 언제나 재시도 패스만).
* `post_number`(중복 판정 키) 규칙: 등록된 설정형 수집기(`adapter.type=crawler`) 또는 `adapter.id_regex`가 있으면
  기존 `_extract_id` 결과(기존 수집분과 같은 키), 커스텀 수집기는 그 파서가 만든 `external_id`, 그 외에는 목록 식별값.

## 7. 파일(PDF/첨부) 별도 대장 (files.py)

* 본문 단계에서 `paper.pdf_url`(및 `paper.attachments` 목록)을 `enum_file`에 `pending`으로 넣기만 한다(다운로드 안 함).
* 별도 패스 `FilePass.run(site)`가 `pending`/`failed_transient`(시도 < 상한)를 Fetcher(같은 게이트)로 받는다.
  * 검증: 빈 본문 실패, PDF 기대 시 `%PDF-` 머리 확인(HTML/로그인 페이지 차단), 크기 상한, sha256, 페이지 수(`/Type /Page` 계수, 근사).
  * 주 PDF(role=pdf): `blob_storage.save_pdf(seq_id, …)` + 백엔드 `update_document_pdf` — 기존 다운로더와 같은 저장 경로.
  * 첨부(role=attachment): `<blob_root>/attachments/<seq_id>/<sha256>.<ext>`에 저장, 대장에만 기록.
  * 접근 차단 → `unavailable`(사이트 파일 패스 중단), 404 → `failed`, 일시 실패 → `failed_transient`(재시도 횟수 증가).
  * 파일 URL 하나가 robots 금지 → 그 파일만 `failed`(`robots_disallowed`), 패스는 계속(요청 없음).
  * 본문 열거가 이미 완료(`complete*`)된 사이트에서 파일 패스가 차단을 만나면 사이트 상태는 그대로 두고
    `state.file_pass_hold`(사유·호스트)와 호스트 보류로 이후 파일 패스를 요청 전에 멈춘다(호스트 보류가 있으면 그것을 풀 때, 없으면(robots 해석 실패·사설 주소 등) `--refresh` 재열거 때 풀린다).
    완료 전 사이트는 이전처럼 `access_blocked`가 된다.

## 8. plan.json

```json
{
  "version": 1,
  "defaults": {
    "request_interval_s": 1.5, "max_requests": 20000, "max_runtime_s": 21600,
    "max_pages": null, "retries": 3, "item_max_attempts": 3,
    "respect_robots": true, "user_agent": "Mozilla/5.0 ... Chrome/120.0.0.0 Safari/537.36"
  },
  "sites": {
    "afd-fr-fr": {
      "strategy": "last_page_backward",
      "adapter": {"type": "crawler", "detail": "generic_selectors"},
      "listing": {
        "url": "https://www.afd.fr/fr/ressources/liste?type%5B170%5D=170",
        "page_param": "page", "start_page": 0,
        "parser": {"type": "html", "item_container": ".views-row", "item_link": "h3 a[href]", "date": "time"}
      },
      "last_page": {"overlap_pages": 1, "check_unparameterized_first_page": true}
    },
    "ga-gov-au-data-pubs": {
      "strategy": "date_range_split",
      "adapter": {"type": "list_only"},
      "listing": {"url": "https://www.ga.gov.au/data-pubs/library/legacy-publications/records/{segment}",
                  "parser": {"type": "html", "item_container": "table tbody tr", "item_link": "a[href]"}},
      "segments": {"mode": "explicit", "items": [{"key": "1940s"}, {"key": "1950s"}, {"key": "1960s"}]}
    },
    "keei-re-kr-boardes": {
      "strategy": "date_range_split",
      "listing": {"url": "https://www.keei.re.kr/board.es?mid=a10101000000&bid=0001",
                  "page_param": "nPage", "start_page": 1,
                  "parser": {"type": "html", "item_container": "table tbody tr", "item_link": "a[href]"},
                  "total": {"type": "regex", "pattern": "총\\s*([\\d,]+)\\s*건"}},
      "segments": {"mode": "range", "from": "1990", "to": "now", "granularity": "year",
                   "filter": {"type": "range", "from_param": "sdate", "to_param": "edate", "date_format": "%Y-%m-%d",
                              "to_inclusive": true},
                   "hidden_cap": 10000, "split_to": ["month", "day"]},
      "overall_total_check": true
    },
    "climateinstitute-ca-newsroom": {
      "strategy": "oldest_first_sort",
      "listing": {"preset": "wordpress", "url": "https://climateinstitute.ca/wp-json/wp/v2/posts"}
    },
    "cedelft-eu-reports": {
      "strategy": "sitemap",
      "sitemap": {"sitemap_url": "https://cedelft.eu/sitemap_index.xml",
                  "index_include": ["report-sitemap\\d*\\.xml"], "include": ["/publications/"]}
    }
  }
}
```

* `max_pages`: 기본 `null`(상한 없음) — 대신 `max_requests`/`max_runtime_s`로 제한한다.
* 사이트 설정은 `defaults`를 덮어쓴다. `adapter`:
  * `{"type": "list_only"}` — 목록 정보만으로 문서 생성(상세 요청 없음).
  * `{"type": "selectors", "selectors": {...}}` — 상세 페이지를 받아 GenericCrawler와 같은 선택자 규칙으로 파싱.
  * `{"type": "crawler"}` — 등록된 수집기를 사용. GenericCrawler 설정 사이트면 목록 선택자·상세 파싱(`_extract_list_items`,
    `_fetch_detail`)을 재사용한다(상세 요청은 Fetcher가 미리 받아 넘김). 커스텀 수집기는
    `"list_method"`/`"detail_method"`와 인자 규약(`html`, `soup`, `url`, `item`, `json`)을 지정한다.
    저장은 언제나 해당 수집기 인스턴스의 `_save_paper`(기존 저장·중복 판정 경로)다.

## 9. 기존 엔진 결함과 대응

| 기존 결함 (incremental.py) | 이 틀의 대응 |
|---|---|
| 어댑터/계약이 나중에 바뀌면 상태 불일치로 영구 차단 | plan 지문이 바뀌면 새 구간 세대로 재열거, 항목 대장은 유지 |
| 목록이 밀리면(커서 불일치) 영구 정지 | 겹쳐 읽기 + 키 중복 제거로 밀림을 정상 상황으로 처리, 커서는 페이지 번호/구간 키 |
| 저장 불가 항목 하나가 큐를 막음 | 항목 실패는 대장에 기록하고 진행. 연속 저장 실패만 사이트 중단 |
| 상한에서 멈추면 발견분이 저장 안 됨 | 페이지 단위로 즉시 저장 후 체크포인트. 상한/예산 정지는 완료 표시 없이 저장분 보존 |

## 10. 기존 수집기 공통 결함의 설정화 (legacy.py, generic_crawler.py)

* 200페이지 하드코딩 상한: 클래스/인스턴스 속성 `MAX_PAGES`/`max_pages`/`_MAX_PAGES` 및 모듈 상수를
  `legacy.apply_overrides(crawler, {"page_cap": N|null})`로 덮어쓴다. 기본값은 그대로(기존 동작 보존).
  `null`이면 사실상 무제한(10^9)이고 수집기 자체의 시간 예산이 남는다.
* 페이지 시작 번호: GenericCrawler는 기존 `pagination.start`를 쓰고, plan의 `legacy.start_page`로 덮어쓸 수 있다.
* 필터 인코딩: GenericCrawler `params`에 이미 인코딩된 키(`type%5B170%5D`)가 있으면 requests가 `%255B`로
  이중 인코딩하던 문제는 **plan 플래그로만** 고친다: `legacy: {"decode_preencoded_params": true}`이면
  `options.decode_preencoded_params`가 켜지고 `%XX`가 있는 키/값만 `unquote_plus`로 한 번 디코드한다. 플래그가 없으면 기존 URL 그대로
  (`tests/test_enumeration_review.py`가 설정 전체의 이전/이후 URL을 비교).
* 첫 페이지 누락: 새 틀은 `check_unparameterized_first_page`로 검증. 기존 수집기는 plan의 `start_page` 덮어쓰기로 교정.
* 적용 경로: 배달 워커(`delivery/worker/worker.py`)가 수집기 인스턴스를 만든 직후
  `legacy.apply_overrides_from_env`를 호출한다. 환경변수 `LIBERTREE_ENUMERATION_PLAN`이 없으면 아무것도 하지 않는다.
  plan 예: `"gri-re-kr-web": {"strategy": "...", "legacy": {"page_cap": null}}`.

## 11. 완전성 점검 보고 (report.py)

사이트별 행: `site_id, strategy, status, stop_reason, listed_unique, saved, failed_total, failed_by_reason,
source_reported_total, segment_sum_total, sitemap_url_count, independent_evidence, gap, segments_incomplete,
segments_mismatch, files_{pending,done,failed}, oldest_item`. JSON(상세 포함)과 CSV(평평한 열)로 쓴다.
`gap = 독립 근거 총건수 - listed_unique`(근거가 없으면 null — 근거 없음 자체를 보고).

요청 시간 진단: Fetcher가 호스트별로 요청 수, 게이트 대기(간격·Crawl-delay 대기 포함) 평균/최대, 응답 시간 평균/최대,
적용된 Crawl-delay와 실효 간격(`effective_gap_s`)을 모으고, 게이트 대기+응답이 5초 이상인 요청 중 가장 느린 10건을
표본으로 남긴다. 실행마다 사이트당 `request_timing` 이벤트 1건(파일 패스는 `scope: file_pass`로 1건)과
`state.request_timing`(최근 실행)에 기록하고, 보고서 JSON에 `request_timing`, CSV에 `request_count`,
`gate_wait_avg_s`, `response_avg_s`, `crawl_delay_s`를 낸다. 예: 요청당 11초인 사이트에서 `gate_wait_avg_s`≈10,
`crawl_delay_s`=10이면 Crawl-delay 때문이고, `response_avg_s`가 크면 사이트 응답이 느린 것이다.

## 12. 알려진 한계

* 날짜 구간 분할은 날짜 필터에 걸리지 않는 항목(날짜 없음)이나 경계일 항목을 놓칠 수 있다 → `overall_total_check`(range 필터+총건수 plan은 기본 켬)로 탐지만 한다.
* 구간 내부/사이트맵 순서는 소스 순서에 의존한다(사이트맵 lastmod 미사용). "오래된 순"은 구간 단위로 보장.
* wrap이 첫 페이지가 아닌 `p mod N` 페이지로 되돌아가는 사이트는 `beyond` 술어가 단조가 아니다(미지원, 키 반복으로 탐지 시 중단).
* 커스텀 수집기 메서드가 내부에서 curl/requests를 직접 호출하면 게이트를 우회할 수 있다. 어댑터는 해당 수집기 모듈의
  호출 구간을 `guard.guarded()`(스레드 지역)로 감싸 socket 연결, 프로세스 실행(curl/브라우저), `os.system`을
  **오류로 드러낸다**(`adapter_ungated_network`). 파이썬 socket을 거치지 않는 네이티브 코드(curl_cffi 내장 libcurl 등)는 잡지 못한다.

## 13. 코드 검토 반영 (2026-10-12)

| 항목 | 반영 |
|---|---|
| C1 기존 행과 식별값 | 등록 수집기가 있는 사이트는 `adapter.type=crawler` + 목록 `parser.type=adapter`만 허용(plan 검증에서 거부). GenericCrawler 설정 사이트는 `_extract_list_items`(=`_make_absolute`)와 `_extract_id`를 그대로 쓴다. 실행 전 **식별값 사전 점검**: 시작 목록 쪽(사이트맵은 기존 행 URL) 표본 N개(기본 5)의 (post_number, meta_url)을 틀의 실제 경로로 계산해 읽기 전용 기존 DB(`--reference-db` 또는 `--reference-pg-dsn-env`)와 대조하고, 일치율이 기준(기본 1.0) 미만이거나 비교 가능한 행이 없으면 `config_error`로 실행 거부. 기존 행이 없는 사이트는 `no_reference_rows`로 통과. |
| C2 끝 오판 | `fetch_page`는 404/410·`end_statuses`만 빈 쪽으로 바꾸고 다른 4xx는 실패로 올린다. 마지막 쪽 후보 L은 L, L+1, L+2를 새로 읽어 확인(실패 시 탐색 1회 재시도, 그래도 실패면 `last_page_unconfirmed`). 실제 쪽의 내용이 다른 쪽 번호에서 반복되면(`page mod N`) `repeated_page_content`. 범위 안 빈 쪽은 한 번 다시 읽고 여전히 비면 미완료. 겹쳐 읽기가 `max_overlap_walk`에 닿으면 `overlap_walk_limit`(미완료). |
| H1 로그인 | 로그인 폼은 차단이 아니라 의심 표시(`login_suspect`): 페이지 제목/주 제목이 로그인이거나 폼 밖에 링크가 하나도 없을 때만. 목록에서 항목이 나오면 무시, 항목 0개면 `review_pending`(이벤트 `login_form_review`, 영구 보류 아님), 상세면 항목 실패 `login_required_suspect`. |
| H2 저장 안전 | 저장 실패 시 `conn.rollback()`, `external_id`는 `str`, 문자열의 NUL 제거. `tests/test_enumeration_pg.py`가 SQLite와 임시 PG DB(`enum_test_<시각>`, 끝나면 삭제)에서 같은 시나리오를 돈다. |
| H3 | 위 5.2 참고(`unverified`, `complete_unverified`). |
| H4 | `guard.py` 스레드 지역 감시(위 12 참고). |
| H5 | 위 10 참고(플래그 뒤로 이동, `unquote_plus`). |
| M1 | 파일 대기열 기록 → 항목 `saved` 기록 순서. |
| M2 | 429가 연속 3회(`rate_limit_stop`)거나 Retry-After가 `max_retry_wait_s`보다 길면 사이트를 `incomplete(rate_limited_cooldown)`로 멈추고 `cooldown_until`을 상태에 기록. 그 전에는 다시 실행해도 요청하지 않는다(`cooling_down`). |
| M4 | 5.3 참고. |
| M5 | 사이트맵 URL 수는 `sitemap_url_count_not_independent`로 표시하고 gap을 계산하지 않는다. |
| M7 | 모듈 상수 덮어쓰기는 `legacy.restore_all()`로 되돌리며 워커가 매 작업 시작 전에 호출한다. |
| G1 중복 0 | 저장 직전 (site_id, post_number) 행이 있으면 새 행을 만들지 않고 `existing_post_number`로 대장 기록 + `duplicate_post_number` 이벤트. 실행 전/후로 `group by site_id, post_number having count(*)>1`의 초과 행 수를 세어 결과/보고서에 `duplicates{before,after,new}`; `new>0`이면 `failed_duplicates`. |
| G2 날짜 | 저장은 언제나 수집기 `_save_paper` → `insert_document`의 `normalize_document_dates`(값/원문/상태/정밀도)를 거친다. 목록/JSON의 **원문 날짜 문자열**을 넘기고, 없으면 빈 값(사이트맵 lastmod는 쓰지 않음). 보고서에 `published_date_fill`, `listed_date_fill`. |
| G3 격리 DB | CLI는 문서 DB가 지정되지 않았거나 이름이 `enum_`으로 시작하지 않으면 시작을 거부(`--i-know-this-is-isolated`로만 예외). PG는 `LIBERTREE_PG_DSN`의 dbname으로 판정. |

새 사이트 상태: `complete_unverified`, `review_pending`, `failed_duplicates`.

## 14. 재검토 반영 (2026-10-12, 2차)

| 항목 | 반영 |
|---|---|
| HIGH-1 | 저장 직전 판정 순서: ① 백엔드 `find_by_dedup_key`(KRIVET/KEI/CSO 규칙 포함)로 같은 문서면 일반 저장 경로를 그대로 탄다(이미 있는 행이면 `existing_document`로 대장 기록, **파일 대기열은 다시 넣는다**). 그래서 `_save_paper` 커밋 직후·파일 대기열 전에 죽어도 재실행에서 PDF가 들어간다. |
| HIGH-2 | ② dedup 규칙으로는 다른 문서인데 (site_id, post_number) 행이 이미 있으면 `failed_permanent:identity_collision`(이벤트 `identity_collision`), 기존 행은 건드리지 않는다. dedup 규칙 자체가 검토를 요구하면(`*_requires_review` 등) 이 점검보다 먼저 `failed_permanent:identity_review:<규칙>` + 이벤트 `identity_review_required`. 보고서에 `skipped_existing_post_number`, `identity_collisions`, `identity_reviews` 열. `saved`와 `date_fill`은 실제로 그 항목의 문서인 행만 센다. |
| MEDIUM-3 | 정렬 검사는 직전 항목과 비교(고정 공지 하나는 위반 1회). JSON 파서 `exclude_if_true`(예: WordPress `sticky`)와 HTML `exclude` 선택자로 고정 항목 제외. |
| MEDIUM-1 | 식별값 사전 점검 표본은 참조 DB의 최신 행 N개(meta_url)에서 뽑아 틀의 계산 결과를 `find_by_dedup_key`로 그 행에 대응시키는지 본다. URL만으로 식별값을 만들 수 없는 수집기(목록 필드가 필요한 커스텀)는 시작 목록 쪽 표본으로 대체. |
| MEDIUM-2 | 등록 사이트인데 참조 행이 0건이면 `identity_reference_empty`로 거부. `--allow-empty-reference`일 때만 통과하고 보고서 `warnings`에 `identity_reference_empty_allowed`. |
| MEDIUM-4 | 상세가 로그인 의심인 항목이 연속 `max_consecutive_login_details`(기본 5)건이면 `review_pending(login_required_details)`. |
| L-1 | 이어받기 첫 쪽이 비어 있으면(삭제로 마지막 쪽이 사라짐) 마지막 쪽을 다시 찾고(`last_page_reprobed`) 더 작은 새 끝에서 계속한다. |
| L-2 | 규칙 유지·명시: plan 파라미터 값은 `%XX`가 있을 때만 "이미 인코딩된 폼 문자열"로 보고 `unquote_plus`(그때 `+`는 공백), `%XX`가 없으면 `+`를 포함해 글자 그대로. 기본 URL의 질의는 항상 폼 인코딩으로 해석. |
| L-4 | 사전 점검은 실행마다 최대 표본 수(기본 5)만큼 상세 요청(+대체 시 목록 1회)을 더 쓴다. |

## 15. 재검토 반영 (3차)

| 항목 | 반영 |
|---|---|
| M-1 | 열거는 끝났지만 `identity_collision`/`identity_review` 항목이 1건 이상이면 사이트 상태는 `complete_with_review`(사유 `identity_collisions:N;identity_reviews:M`), 보고서 `warnings`에도 건수. 재실행(재시도 패스만)은 상태를 덮어쓰지 않는다. CLI 종료 코드: 1 = `failed`/`failed_duplicates`/`config_error`, 2 = 사람이 볼 상태(`complete_with_review`, `review_pending`, `access_blocked`, `complete_with_mismatch`, `complete_unverified`), 0 = 그 외(`incomplete`는 이어받기 가능). 마지막 줄에 `summary_by_status`. |
| M-2 | 백엔드 식별 규칙의 `ValueError`는 `*requires_review*`, `*repair_required*`, `ambiguous_*`만 항목 검토(`identity_review`)로, 나머지(예: `krivet_post_number_must_match_pstNo`, `kei_scoped_post_number_required`, `invalid_cso_release_identity`)는 설정 오류로 사이트를 `config_error(identity_rule_config:<규칙>)`로 멈춘다. 항목 검토가 연속 `max_consecutive_identity_reviews`(기본 5)건이면 `review_pending(identity_reviews_consecutive)`. |
| L-1/L-2 | 사전 점검에서 dedup 결과가 실제 행 id(int)일 때만 일치. 목록 대체 경로는 dedup 일치가 없어도 같은 meta_url 또는 post_number 행이 있으면 불일치로 센다. 결과에 `sampled_reference`, `sampled_listing`, `sampled`(합계). |
| M-3 (한계) | 식별값이 목록 필드에 의존하는 커스텀 수집기(`paper_method`, `list_returns_paper`, `detail_args`에 `item`)는 참조 행 URL만으로 식별값을 정확히 재계산하지 못할 수 있다. 이런 사이트의 불일치 정지는 사유가 `identity_mismatch:sample_recompute_may_be_wrong`, 결과에 `list_field_identity: true`로 표시되어 오탐 여부를 사람이 가려야 한다. 근본 해결(목록 필드 재현)은 하지 않았다. |

## 16. 교차 검토(Codex) 반영 — 1차 배치

| 항목 | 반영 |
|---|---|
| S0 | CLI는 사이트마다 `ensure_documents_db`: 문서 DB에 스키마가 없고 이름이 `enum_`이면 `init_db`(SQLite/PG) 자동 적용, `enum_`이 아니면 스키마를 만들지 않고 거부. 이어서 등록 수집기 클래스(`CRAWLERS`)의 `site_name`/`base_url`(없으면 plan)로 `sites` 행을 idempotent upsert — 기존 수집기가 `crawl()` 안에서 하던 일. 빈 DB 파일에서 시작하는 CLI 종단 테스트 추가. |
| H08 | 전송 계층은 리다이렉트를 따르지 않는다(`allow_redirects=False`). `Fetcher.get`이 hop마다 hold 확인 → 목적지 robots → 호스트 게이트 → 예산을 거친다. Location이 로그인/인증/챌린지 경로(`login`, `signin`, `sso`, `auth`, `oauth`, `saml`, `captcha`, `challenge` 등)면 따라가지 않고 `login_redirect`로 정지. 최대 5 hop. |
| H09 | robots 응답 401/403/407/451, 로그인 경로 리다이렉트, 챌린지/로그인 페이지는 **보류**(이전의 RFC 9309 "4xx=규칙 없음"은 401/403에 대해 폐기). 404/410 등 그 밖의 4xx만 "미게시". `Crawl-delay`(소수 허용, 직접 파싱)와 `Request-rate`를 호스트 간격에 반영. 게이트 자체 간격 상한은 10초이므로 그보다 긴 값은 게이트 안에서(호스트 잠금을 쥔 채) `이전 응답 끝 + Crawl-delay - 지금`만큼 더 기다려, 이전 응답 끝부터 다음 요청까지의 실제 간격이 Crawl-delay 이상이 된다(그 기다림이 `max_runtime_s`를 넘기면 자지 않고 `time_budget`으로 멈춘다). 무한대/NaN 값은 무시하고, Crawl-delay/Request-rate 간격이 `MAX_CRAWL_DELAY_S`(300초)를 넘으면 요청마다 기다리지 않고 `access_blocked:robots_crawl_delay_too_long`(호스트 보류 아님)으로 멈춰 사람이 판단한다. plan 검증: `respect_robots=false` 거부, 끝 상태에 401/403/407/429/451·2xx 거부, 404/410 외 상태는 `json_code`/`body_regex` 조건이 있어야 함. Fetcher는 거부 상태를 plan 허용 상태보다 먼저 분류. |
| H07 | 차단은 사이트 상태(`access_blocked`)와 **호스트 hold 대장**(`enum_host_hold`, 같은 호스트의 모든 site ID가 공유)에 영속. 429 cooldown은 `RequestGate.defer`로 호스트 게이트에 기록되어 다른 site ID/실행/패스도 `host_cooldown`으로 멈춘다. retry-only도 본 실행과 같은 상태 전이를 하고, 파일 패스(`--files`, `--files-only`)는 사이트 hold/cooldown을 먼저 확인하며 새 차단을 사이트·호스트에 기록한다. `--files`는 본 실행과 같은 Fetcher(같은 예산)를 쓴다. hold 해제는 사람이 검토 후 `--refresh`(사이트)와 `--release-host HOST`(호스트)로만. |
| H01 | 파서는 (키, URL)이 완전히 같은 반복만 버린다. 같은 목록 키에 다른 문서 URL(목록 상태 파라미터 page 등은 무시하고 비교)이 오면 같은 쪽·다른 쪽·재실행 모두 `failed_permanent:listing_identity_conflict`로 별도 대장 행(`<키>#conflict:<해시>`)에 보존, 사이트는 `complete_with_review`. |
| H02 | 사이트 lease: SQLite는 문서 DB 옆 파일 잠금, PG는 세션 advisory lock(`pg_try_advisory_lock`). 잡혀 있으면 `locked`. 같은 `--site` 중복은 CLI 거부. post_number가 없는 문서는 저장하지 않고 `identity_missing`(검토). 중복 집계는 업체 쿼리처럼 NULL을 한 그룹으로 센다. DB 유일 제약 `UNIQUE(site_id, post_number)`는 **제안만**(기존 데이터 정리가 먼저 필요). |
| H03 | 파서가 상태를 구분: 깨진 JSON·items 경로 없음·목록 구조 없는 HTML은 `parse_error` → 목록 실패(미완료). 빈 HTML 목록은 목록 표지(`listing_marker` 또는 항목 선택자의 조상 부분, 예: `tbody tr` → `tbody`)가 있을 때만 "명시적 빈 결과". 표지가 없는 설정은 빈 쪽을 끝으로 쓸 수 없다(plan에 `listing_marker` 필요). WordPress 프리셋은 `rest_post_invalid_page_number`인 400만 끝. 정방향의 반복 쪽은 재확인 후 다음 쪽에 새 키가 있으면 `repeated_page_inside_range`(미완료). |
| H04 | 날짜 구간·사이트맵 루트 manifest를 매 실행 전체 upsert(멱등). 분할 자식도 부모를 `split`으로 바꾸기 전에 모두 upsert. |
| H05 | 사이트맵 커서 = (offset, 필터 후 URL 목록 지문). 지문이 바뀌면 그 사이트맵을 처음부터 재열거(`sitemap_changed_restart`). 끝에 현재 URL 전체가 열거됐는지 대조(아니면 `mismatch`). |
| H06 | 구간 간 같은 키(겹침, `allow_overlap`이 아니면 `segment_overlap`), leaf 보고 합계 ≠ 고유 열거 수(`leaf_total_sum_mismatch`)면 `complete_with_mismatch`. 서로 다른 구간의 첫 쪽이 같으면 필터 무효 의심(`filter_not_effective`) → 해당 구간 `unverified`. |
| H11 | 가드는 **보안 경계가 아니라 우회 탐지용 tripwire**로 정정. 가드 안에서 시작된 스레드는 가드를 물려받는다(테스트). 남은 한계: 이미 연결된 소켓 재사용, native 소켓(curl_cffi 등), 가드 이전에 만들어진 스레드/실행기, 수집기 생성자와 `_save_paper`(가드 밖). 신뢰가 필요한 경우 수집기 메서드를 실행하지 않는 방식(순수 파서 계약 또는 네트워크 없는 별도 실행 환경)이 필요하다. |

### 16.1 접근 제한 호스트 사전 보류 (`--seed-holds`)

새 상태 디렉터리의 `enum_host_hold`는 비어 있으므로, 이전 조사에서 접근 제한으로 분류된 호스트는 `--seed-holds <csv>`(열 `host`, `reason`; 다른 열은 무시)로 시작 시 보류 대장에 넣는다. 사유는 `seeded:<reason>`으로 기록되고(이미 있는 보류는 덮어쓰지 않음), 이벤트 `host_hold_seeded`가 남는다.

- 사이트 시작 시(식별값 사전 점검보다 먼저) plan의 `listing.url`, `base_url`, `sitemap.sitemap_url` 호스트가 보류 중이면 **요청 0건**으로 `access_blocked(host_access_hold:seeded:<reason>)`. 실행 중 다른 호스트(상세·리다이렉트·파일)는 기존처럼 요청 직전 `Fetcher` 확인에서 멈춘다.
- 호스트 일치 규칙: URL의 hostname(소문자)과 **정확히 같은 항목**, 또는 상위 도메인의 `*.<접미>` 항목. `*.hal.science`는 `x.hal.science`, `a.b.hal.science`를 막지만 `hal.science` 자체는 막지 않는다(그건 별도 행). `www.` 유무도 서로 다른 호스트이므로 CSV에 둘 다 적는다.
- 해제는 기존대로 사람이 `--release-host HOST`(+ 사이트는 `--refresh`)로만. 해제 이벤트가 있는 호스트는 같은 CSV를 다시 넘겨도 다시 보류하지 않는다. 같은 실행에서 둘 다 주면 보류 주입 → 해제 순서.

### 16.2 목적지 주소 검사 (서버 내부망 보호)

- 모든 요청(목록·상세·robots·파일)과 모든 리다이렉트 hop 직전에 `Fetcher._once`가 호스트를 해석해 결과 주소를 **전부** 검사한다. 하나라도 사설(10/8, 172.16/12, 192.168/16), 루프백(127/8, ::1), 링크로컬(169.254/16 — 메타데이터 169.254.169.254 포함, fe80::/10), 고유 로컬(fc00::/7), 미지정(0.0.0.0, ::), 멀티캐스트, 예약·공유·문서용 대역(240/4, 100.64/10, 192.0.2/24 등, `ipaddress`의 비공용 판정)이면 요청하지 않는다. IPv4-mapped IPv6(::ffff:10.0.0.1)는 풀어서 본다. IP 리터럴 URL도 같은 검사.
- 결과: 사이트 상태 `private_destination_blocked`, 사유 `private_destination_blocked:<호스트>:<주소>`, 이벤트 `private_destination_blocked`(host, address, url). 호스트 보류(`enum_host_hold`)에는 넣지 않는다. CLI 종료 코드 1. 파일 패스는 그 파일을 `unavailable`로 두고 패스를 멈춘다.
- 해석 실패(이름 없음 등)는 요청 없이 `TransientFailure(dns_unresolved)` → 목록이면 `incomplete`.
- DNS 재바인딩: 운영 전송(`RequestsTransport`)은 새 연결마다 호스트를 한 번 해석·검사하고 **검사한 주소로 연결**한다(urllib3 연결의 `_dns_host` 고정). Host 헤더·SNI·인증서 검증은 원래 이름. 한계: (1) Fetcher 사전 검사와 연결 시 검사는 각각 해석하므로 두 결과가 달라도 연결은 항상 검사를 통과한 주소로만 간다. (2) 고정은 urllib3의 비공개 속성 `_dns_host`(urllib3 1.26/2.x)에 의존한다. (3) 프록시는 쓰지 않는다(`trust_env=False`). (4) 틀 밖의 네트워크(수집기 내부 curl 등)는 가드(tripwire)로만 막히며 이 검사 대상이 아니다.
- plan으로 끄거나 완화할 수 없다: `allow_private_destinations`, `destination_check`, `resolver` 등과 이름에 `private`가 들어간 키는 plan 검증이 거부한다. 테스트의 루프백 서버만 테스트 코드에서 127.0.0.1을 한정 허용한다.

## 17. 교차 검토(Codex) 반영 — 2차 배치

| 항목 | 반영 |
|---|---|
| H10 | 저장 중 DB 쪽 일시 장애(SQLite `OperationalError`, psycopg `OperationalError`/`InterfaceError`/deadlock/serialization/lock)는 `failed_transient:save_transient:<형>`(다음 실행 재시도), 연속 `max_consecutive_save_errors`회면 `incomplete(database_unavailable)`. 문서 자체의 저장 오류만 `save_error`(영구). 어댑터 설정 오류(없는 메서드, 모르는 인자 토큰)는 어댑터 생성 시 검사해 사이트 `config_error`로 멈추고 항목 대장에 남기지 않는다. 명시적 재처리: `--reprocess-failed <사유 접두>`(예: `parse_error:`)가 해당 실패 항목의 시도 횟수를 초기화해 재시도 패스에 넣는다. saved 항목은 건드리지 않고, `identity_*`/`listing_identity_conflict`는 데이터 판단이 필요하므로 재처리 대상에서 제외. 어댑터 변경은 plan 지문에 넣지 않는다(진행 보존) — 재처리는 이 명령으로. |
| H12 | 기본 URL 질의는 순서 있는 다중값 쌍으로 보존(`f=a&f=b`, `fq=..&fq=..`, 빈 값). plan/페이지 파라미터로 주어진 키만 그 키의 모든 값을 첫 위치에서 교체(리스트 값은 여러 쌍). |
| M01 | 파일 패스의 blob 쓰기(`OSError`)와 문서 DB 갱신 오류는 파일 대장에 `transient:storage_error:<형>`/`db_error:<형>`으로 기록하고 시도 횟수 상한 후 `failed(retries_exhausted:…)`. blob·DB 갱신 후 대장 기록 전 크래시는 다음 패스에서 blob 해시와 `documents.pdf_sha256`이 같으면 다운로드 없이 `done(reconciled_existing_blob)`. `--files`는 본 실행과 같은 Fetcher(예산)를 쓰고, `--files-only`는 plan 예산으로 별도 실행. |
| M02 | 어댑터 생성 시 문서 연결의 종류와 저장 경로(`LIBERTREE_DB_BACKEND`)가 다르면 `config_error(backend_mismatch:…)`. (전역 환경변수를 바꾸지 않고 검증으로 일치를 강제) |
| M03 | 보고서에 `saved_current_generation`(현재 plan 세대에 열거된 항목 중 저장된 수, `listed_unique`와 같은 범위)과 `saved_all_time`(= `saved`) 분리. `date_fill`은 `DISTINCT seq_id` 기준. gap=0은 항목 실패 0건을 뜻하지 않는다. |

## 18. 틀 확장 (X1, 2026-10-18)

사이트 구역 세션이 "틀이 지원하지 않아 needs_dev" 로 남긴 공통 유형을 plan 키로 지원한다. 사유 표기(site-status `detail`)는 `ext:<이름>`.
모든 새 요청은 기존 요청 게이트(호스트별 간격·예산·robots·호스트 보류·목적지 주소 검사·거절 분류)를 그대로 거친다.
**새 키가 없는 plan 은 동작과 plan 지문이 바뀌지 않는다**(테스트 `FingerprintTest` 가 기존 지문 값을 고정).
머리글(`request_headers*`)과 `adapter.second_step` 은 지문에 들어가지 않는다(토큰을 바꿔도 진행이 이어진다).
테스트: `tests/test_enumeration_extensions.py`.

| 표기 | plan 키 | 동작 | 막는 것 |
|---|---|---|---|
| `ext:headers` | `request_headers: {"Accept": "application/json"}`, `request_headers_env: {"Authorization": {"env": "SITE_API_TOKEN", "format": "Bearer {}"}}`, 선택 `request_headers_hosts: ["api.example.org"]` | plan 의 호스트로 가는 요청(robots·목록·상세·파일)에만 머리글 추가: `request_headers_hosts`(없으면 listing 호스트 하나). 리다이렉트로 다른 호스트·PDF/CDN 호스트로 가면 붙지 않음. 비밀값은 **환경변수 이름만** plan 에 적고 실행 때 읽는다(`plan.resolve_headers`) | Host·Cookie(2)·User-Agent·Content-*·Forwarded·X-Forwarded-*·Proxy-*·X-HTTP-Method(-Override) 금지(세션 쿠키 = 로그인 우회, 메서드 바꿔치기 금지). Authorization·이름에 token/key/secret/auth/session/password 가 든 머리글은 리터럴 금지(환경변수로만). 줄바꿈 값 금지. 환경변수가 없으면 그 사이트는 `config_error`(빈 값으로 보내지 않음) |
| `ext:post_listing` | `listing.method: "POST"`, `listing.idempotent: true`, `listing.body_type: "form"\|"json"` | 목록 요청을 POST 로. `params`·쪽 번호·구간 필터가 모두 본문으로 간다. `Fetcher.post` 가 GET 과 같은 게이트·재시도·robots(URL 경로 기준)·보류·목적지 검사를 거침 | `idempotent: true` 선언 필수(읽기 전용 검색 API 만). 리다이렉트는 따라가지 않음(`post_redirect_not_followed`; 로그인 경로로 가면 접근 거절·호스트 보류). 경로형 쪽 넘김과 같이 못 씀 |
| `ext:path_paging` | `listing.url` 에 `{page}` (예 `https://x/news/page/{page}/`), 선택 `listing.first_page_url` | 쪽 번호를 경로에 넣음. 첫 쪽은 `first_page_url`(보통 `/page/1/` 없는 주소). `page_mode: offset` 과 같이 쓸 수 있음 | `first_page_url` 은 절대 주소·`{page}` 없음, 구간(`{segment}`·date_range_split)과 같이 못 씀. last_page_backward 의 "쪽 번호 없는 첫 쪽 비교" 는 건너뜀(첫 쪽이 곧 그 주소) |
| `ext:json_page` (커서) | `listing.next_cursor: {"path": "meta.next", "param": "after"}` | JSON 응답의 다음 커서를 다음 요청 매개변수로. 커서가 비면 끝(`exhausted_cursor_end`). 상태 저장소 커서에 다음 토큰을 저장해 이어받음 | oldest_first_sort·date_range_split 만(앞으로만 읽힘), `overlap_pages` 금지. 첫 쪽에 커서 칸 자체가 없으면 `config_error`(경로 오타·API 변경). 같은 쪽·최근 200개 토큰 반복은 `cursor_loop` 로 멈춤(완료 아님). 토큰으로 간 쪽이 비면(만료된 토큰 등) 보고된 총건수와 목록 수가 같을 때만 끝, 아니면 `empty_page_by_cursor` 로 멈춤(완료 아님) |
| `ext:jsonp` | `listing.parser.jsonp: true`(JSON 파서) 또는 `adapter.jsonp: true`(수집기 메서드의 `json` 인자·`list_source: "json"`) | 본문이 `콜백(…)` 한 번(앞에 `/**/`, 끝 `;` 허용)이면 괄호 안을 JSON 으로 읽는다. 빈 배열이면 다른 JSON 목록처럼 끝 후보 | 콜백 이름은 점으로 이은 식별자만. 콜백 하나가 아닌 본문(뒤에 다른 문장, HTML 오류 쪽)은 `invalid_json`(끝이 아님). 값은 true/false 만. 커서·총건수(`next_cursor`·`total`)는 JSONP 를 읽지 않음 |
| `ext:json_page` (마지막 쪽) | `listing.last_page_path: "meta.last_page"` | last_page_backward 가 JSON 이 알려 준 마지막 쪽을 후보로 씀(탐색 요청 절약) | 후보도 기존과 같은 확인(L 실제, L+1·L+2 끝)을 통과해야 함. 실패하면 `reported_last_page_unconfirmed` 이벤트 후 원래 탐색. `max_pages` 상한 이상이면 상한 쪽까지만(`page_cap`, 완료 아님) |
| `ext:http204_end` | `listing.end_on_204: true` | 204 No Content 를 목록 끝으로(`end_status_204`) | 선언 없으면 204 는 끝이 아님(오류). 본문이 있는 204 는 오류(`http_204_with_body`) |
| `ext:two_step_detail` | `adapter.second_step: {"use": "detail_page"\|"pdf_url", "selector": "a.next", "attr": "href", "required": true, "allow_hosts": []}` | `detail_page`: 상세 페이지의 링크를 한 번 더 받아(같은 fetcher) 그 페이지에서 상세를 뽑음. `pdf_url`: 추출에 pdf_url 이 없으면 링크를 pdf_url 로(요청 없음, 파일 단계가 게이트로 받음) | 식별값(key·meta_url)은 1단계 항목 그대로. 다른 호스트 링크는 `allow_hosts` 에 없으면 무시(요청 안 함), 링크가 없으면 `second_step_link_missing` 항목 실패(`required: false` 면 1단계 페이지 사용). list_only 어댑터에는 못 씀 |

서버 배포는 아직(2026-10-18 사용자 방침: 이 컴퓨터에서 먼저 확인). 배포 목록은 `frwaler-delivery-out/plan-dev-20261008/dev-flow-20261018/status/DEPLOY.md`.

## 19. 틀 확장 (X2, 2026-10-18)

X1 다음으로 구역 세션들이 모은 요구. 원칙은 X1 과 같다: 새 요청은 모두 기존 게이트를 거치고, **새 키가 없는 plan 은 동작과 지문이 그대로**이며, 식별값(post_number·meta_url) 규칙은 바꾸지 않는다(수집기 메서드가 그대로 만든다).
`adapter.*` 키(`pdf_text`, 인자 토큰, `second_step.url_template`)는 지문에 들어가지 않고, `listing.*`·`segments.*` 키는 들어간다.
테스트: `tests/test_enumeration_extensions_x2.py`.

| 표기 | plan 키 | 동작 | 막는 것 |
|---|---|---|---|
| `ext:two_step_keep_first` (`two_step_both`) | 인자 토큰 `first_html`·`first_soup`·`first_json`·`first_url`·`first_resp` (계약: `first_final_url`) | `second_step.use: detail_page` 에서 2단계 응답과 함께 1단계 상세 쪽을 메서드에 넘김. 링크가 없고 `required: false` 면 1단계 = 그 쪽 | `second_step.use: detail_page` 없이 쓰면 plan 오류. 목록 단계에는 못 씀 |
| `ext:pdf_text` | `adapter.pdf_text: {"args": ["-layout"], "timeout_s": 30, "min_bytes": 0}` + 인자 `pdf_text` | 상세 응답(보통 2단계로 받은 PDF) 본문에 **틀이** pdftotext 를 실행(어댑터 감시 밖; 메서드는 여전히 감시 안). 결과 = stdout(utf-8, 깨진 글자 치환, strip 안 함) → 메서드가 원래 수집기의 strip·최소 길이 규칙을 그대로 적용 | 옵션은 `-layout -raw -nopgbrk -f N -l N -enc UTF-8` 만. 셸·네트워크 없음, 시간 제한(≤120초), 임시 파일. 짧은 본문·비정상 종료·시간 초과는 `None`. 이미지에 pdftotext 가 없으면 `config_error`. crawler 어댑터의 detail_method 에만 |
| `ext:post_listing_nested_page` | `listing.body: {"search": {"page": "{page}"}, "filters": []}`, 선택 `listing.content_type: "versus/callback; charset=UTF-8"` | POST JSON 본문 틀. 값이 정확히 `"{page}"` 인 자리에 쪽 값(정수, offset 모드면 offset). 구간·커서 매개변수는 최상위 키로 더함 | `{page}` 는 정확히 하나(커서면 없음), 값 전체여야 함. `params` 와 같이 못 씀. content_type 은 JSON 본문용만(form·multipart 금지) |
| `ext:dynamic_token` / `ext:dynamic_ref` | `listing.dynamic: {"url": <목록 호스트의 공개 쪽>, "values": {"nonce": {"regex": "..."}, "ref": {"json_path": "refs.master", "public": true}}, "header": {"name": "{dyn:h}", "value": "{dyn:t}"}}`, 값 자리 `"{dyn:이름}"` | 실행마다 그 쪽을 1번(게이트로) 읽어 값을 뽑고 POST 본문·머리글 1개(POST 만)·GET 매개변수에 넣음. nonce·CSRF·쪽에 들어 있는 hash 처럼 **사이트가 공개로 내주는 값만**(계산·재현 아님) | 값은 상태 DB·이벤트·중단 사유에 남지 않음(이름만). GET 매개변수(=URL, 기록됨)에는 `public: true` 이고 비밀처럼 보이지 않는 이름(token·key·secret·auth·session·password 아님)만. 받은 머리글 이름이 금지 머리글이면 `config_error`. 값이 없으면 `config_error: dynamic_value_missing:<이름>`. 로그인·챌린지·발급 키가 필요하면 쓰지 않음(협의) |
| `ext:multi_listing` | 명시 구간 `items: [{"key", "params", "url", "first_page_url", "label"}]`, `listing.first_page_url` 의 `{segment}`, `segments.mode: "discover"` (`hub_url`, `selector`, `attr`, `key_regex`, `params: {"columnid": "{key}"}`, `label: "text"\|"attr:<이름>"\|null`, `min_segments`, `retired`), 인자 `segment` | 구간마다 자기 목록(주소·첫 쪽). discover: 매 실행 허브 쪽에서 구간을 찾음. 메서드에 `{key, label, params}` (항목이 실린 구간) | 자기 `url` 이 있는 구간은 목록의 첫 쪽을 빌리지 않음. 예전에 찾았는데 허브에서 사라진 구간도 계속 읽고 결과는 `complete_unverified`(`segments_not_rediscovered`), plan 이 바뀌어 매개변수를 모르는 구간도 같음 → 사람이 확인하면 `segments.retired` 에 적음. 찾은 구간이 `min_segments` 보다 적으면 `incomplete`. 알 수 없는 `segments.mode` 는 plan 오류. 식별값 점검 표본에는 구간 정보가 없음(`{key: null}`) |
| `ext:next_link` | `listing.next_link: {"selector": "a.load-more", "attr": "href"}` 또는 `{"json_path": "links.next"}` | 응답 안 다음 쪽 주소(서버 서명 "더 보기" 링크 등)를 따라감. 커서(`ext:json_page`)와 같은 저장·이어받기·반복 감시 | oldest_first_sort·date_range_split 만, GET 만, `next_cursor`·경로형·`overlap_pages` 와 같이 못 씀. 다른 호스트나 https→http 링크면 `config_error`. 첫 쪽에 링크 자리가 없으면 `config_error`(한 쪽짜리 목록은 next_link 를 쓰지 않음) |
| `ext:detail_aux_request` | `adapter.second_step: {"use": "detail_page", "url_template": "https://host/api?pageId={item:page_id}"}` (selector 대신) | 상세 쪽 링크 대신 항목의 목록 필드로 만든 주소를 한 번 더 요청. first_* 와 같이 쓰면 두 응답을 모두 사용 | 값은 URL 인코딩, `.`·`..`·빈 값·객체는 링크 없음(→ `required` 규칙). 호스트는 리터럴이고 상세 호스트 또는 `allow_hosts` 만 |

남긴 요구(개발 필요·협의): 다른 호스트 행만 있는 쪽·끝 뒤 홈페이지 리다이렉트(neac), 계정이 필요한 조건부 상세(github)·Basic auth(vlaanderen), 구간별로 다른 동적 값(em-dk 월 쪽), POST 로 받는 PDF(kepco), 3단계 상세(ehesp), 상세 1쪽→여러 문서(dhs-congressional-approp, cnl).

## 20. 틀 확장 (X3, 2026-10-18)

X2 적용 중 나온 요구. 원칙은 같다(게이트 그대로, 새 키 없는 plan 불변, 식별값 규칙 불변). 테스트: `tests/test_enumeration_extensions_x3.py`.

| 표기 | plan 키 | 동작 | 막는 것 |
|---|---|---|---|
| `ext:blank_body_end` | `listing.end_on_blank_body: true` | 200 + 공백 본문을 목록 끝으로 | 선언 없으면 공백 본문은 파싱 오류(끝 아님) |
| `ext:second_step_link_params` | `adapter.second_step.add_params: {"_embed": "1"}` | 찾은 2단계 링크에 고정 매개변수를 더함(수집기가 붙이던 것) | 이름은 리터럴, 값은 문자열. 링크에 같은 이름이 이미 있거나 `#` 경로면 링크 없음(덮어쓰지 않음). 호스트 검사는 그대로 |
| `ext:second_step_link_rewrite` | `adapter.second_step.rewrite: {"regex": "/record/(\\d+)$", "template": "https://host/record/{1}?of=xm"}` | 찾은 링크를 정규식 그룹으로 새 주소(예 MARCXML 내보내기)로 바꿈 | template 은 절대 주소·`{1}`..`{n}` 만, 쓰는 그룹 값은 한 번만 URL 인코딩(이미 인코딩된 값은 풀고 다시)·`.`/`..` 거절, 맞지 않으면 링크 없음. https 상세에서 http 주소로는 안 바꿈. 바꾼 주소도 상세 호스트 또는 allow_hosts 만 |
| `ext:list_page_arg` (`list_position`) | 인자 `list_position` (detail/paper) | `{page: 보낸 쪽 값, n: 쪽 번호, position: 그 쪽 안 순번(1부터, 응답 순서)}`. 항목 장부에 같이 저장돼 다시 시도 때도 같음 | 목록 메서드에는 못 씀. 식별값 점검 표본은 None(그래서 이 인자를 쓰는 계약은 "목록 값에 기댐"으로 봄). 한계: 커서·next_link 에서는 page 도 쪽 번호 n, position 은 파서가 남긴 항목 기준, 다시 시도한 항목은 처음 저장한 위치 |
| `ext:multi_level_discover` | `segments.follow: {"selector": "a.journal, a.year", "attr": "href", "max_depth": 2, "max_pages": 100}` (discover 와 함께) | 허브 → 같은 호스트의 하위 허브를 너비 우선으로 따라가며 구간 링크를 찾음. 구간에 `path`(따라간 링크 글자들) 저장 → `segment` 인자로 전달 | 깊이 1..5, 쪽 1..1000, 허브 호스트만·https 허브에서 http 링크 안 따라감·같은 주소(조각·스킴 무시) 한 번만. 하위 허브 404/410 은 건너뛰고 찾은 구간은 읽되 `complete_unverified: sub_hub_failed:<상태>`. 쪽 상한·깊이 상한에 닿으면 마찬가지로 `hub_pages_cap`·`hub_depth_limit`(완료 아님). 허브 자체 실패는 실행 실패 |
| `ext:identity_post_number_multi_url` | `adapter.allow_shared_post_number: true` (crawler 어댑터) | 등록 수집기가 post_number 하나에 여러 행(언어별 PDF 등, meta_url 다름)을 저장하던 규칙을 그대로: IdentityCollision 으로 거절하지 않음(정확 일치 dedup 은 그대로) | 구매자 중복 검사(site_id·post_number 묶음)에는 걸리므로 결과는 `complete_with_review: shared_post_number_rows:N`(N = 그 사이트 전체 공유 행, 이번 실행분만이 아님; 다른 완료 상태면 경고로). 가져오기(import_enum_pilot.py)는 `SHARED_POST_NUMBER` 목록 사이트만: 가장 최근 폴더에 없는 옛 주소는 빼고, 대상에 같은 번호의 다른 주소가 있으면 넣지 않고 `shared_post_new` 로 검토 |
| `ext:pdf_text` 재시도 | `adapter.pdf_text.retry_args: ["-raw"]` | 첫 옵션 결과가 비면 두 번째 옵션으로 다시(수집기의 "-layout 안 되면 -raw") | 같은 허용 옵션 목록. 최악 시간은 timeout_s × 2 |
| `ext:response_size` | `max_response_bytes: 83886080` (사이트) | 목록·상세 응답 크기 상한을 올림(누적 "더 보기" 응답 등) | 기본 상한(50 MiB) ~ `file_max_bytes` (올리기만, 모든 전략에서 검사) |
| `ext:none_is_rule_skip` | `adapter.none_is_rule_skip: true` | 메서드가 None 을 돌려주면 수집기 규칙에 따른 건너뛰기(예 초록 50자 미만): `failed_permanent: skipped_by_rule` 로 기록·건수 보고, 계약 실패·검토로 세지 않음 | 메서드가 실제로 None 을 돌려준 경우만(dict 아닌 값·빈 dict·제목 없음은 그대로 실패). 건너뛴 몫이 `adapter.max_rule_skip_ratio`(기본 0.2)를 넘으면 `complete_with_review: skipped_by_rule:N`, 저장 없이 연속 `max_consecutive_rule_skips`(기본 20)번이면 `config_error`(고장 난 파서). 건수는 늘 경고로 표시. 선언 없으면 계약 사이트는 계약 실패로 셈 |

X3 후속(2026-10-18):

| 표기 | plan 키 | 동작 | 막는 것 |
|---|---|---|---|
| `ext:detail_missing_as_no_text` | `adapter.pdf_text.missing_statuses: [404]` (404·410 만) | pdf_text 가 읽는 요청(2단계가 있으면 2단계, 없으면 상세)이 그 상태면 항목 실패 대신 `pdf_text = None` 으로 메서드 호출 → 수집기의 대체 규칙(예 목록 설명을 초록으로) 그대로. 건수는 경고 `pdf_missing_as_no_text:N` | 그 요청만(1단계 상세 404 는 여전히 실패), 선언한 상태만 |
| `ext:discover_segment_url` | `segments.link_as_url: true` (discover) | 찾은 구간 링크 자체를 그 구간의 목록 주소로(쪽은 page_param) | listing.url 에 `{page}`·`{segment}` 없음, 허브가 목록 호스트, 다른 호스트·https→http 링크는 구간으로 안 씀 |
