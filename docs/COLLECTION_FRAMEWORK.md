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
  | 전송 오류, 5xx, 429 | 재시도(`retries`, 지수 대기, 429는 Retry-After 존중·상한) 후 `TransientFailure` | 항목: 대장(일시 실패, 시도 횟수) 후 계속, 다음 실행에서 `item_max_attempts`까지 재시도. 목록: 해당 실행 중단(미완료) |
  | 401/403/407/451, JS/봇 챌린지 본문(`access_challenge`), 로그인 폼, robots 거부 | `AccessBlocked` | **우회하지 않는다**. 사이트 상태를 `access_blocked`로 기록하고 그 사이트 진행을 멈춘다 |
* robots.txt: 호스트별 1회(같은 게이트 경유) 조회. RFC 9309에 따라 4xx(401/403 포함)는 "규칙 없음"으로 보고
  (페이지 자체의 401/403은 별도로 접근 차단 처리), 5xx/전송 오류는 허용을 추측하지 않고 일시 실패로 진행을 멈춘다.
  `respect_robots: false`는 plan에서 명시할 때만.
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
   * `N == T` → `complete(total_matched)`. 총건수 없음 → `complete(exhausted_no_total)`.
   * 그 외 → `mismatch`(T, N 기록). 일 단위에서도 상한 초과면 `cap_unsplittable`.
5. `overall_total_check`가 켜져 있으면 필터 없는 목록의 총건수를 한 번 읽어 구간 합계와 비교(날짜 없는 항목
   누락 탐지). 모든 구간이 complete/split이고 불일치가 없으면 사이트 complete, 불일치가 있으면 `complete_with_mismatch`.

### 5.3 oldest_first_sort (정렬 파라미터/API)

* 프리셋: `wordpress`(`/wp-json/wp/v2/<type>?orderby=date&order=asc&per_page=100&page=N`, 총건수 `X-WP-Total`,
  마지막 다음 페이지의 400 `rest_post_invalid_page_number`를 끝으로 인정), `ckan`(`package_search?sort=metadata_created asc&rows&start`,
  `result.count`), `dspace7`(`/server/api/discover/search/objects?sort=dc.date.accessioned,ASC&page&size`, `page.totalElements`, 시작 0),
  `generic`(직접 지정). 프리셋 값은 plan에서 덮어쓸 수 있다.
* 앞에서부터 읽는다. 끝 판정: 빈 페이지, 설정된 끝 상태코드(`end_statuses`), 페이지 반복(clamp/wrap), 총건수 도달.
* `overlap_pages`(기본 0): 켜면 p를 읽은 뒤 p-1을 재독해 삭제로 앞당겨진 항목을 잡는다.
* 완료: 소진 + (총건수 없음 또는 고유 목록 수 = 총건수). 불일치면 `complete_with_mismatch`.

### 5.4 sitemap

* `sitemap_url`(인덱스 또는 urlset) 또는 robots.txt의 `Sitemap:`에서 시작. 인덱스를 따라가며 자식 sitemap 하나를
  구간 하나로 체크포인트한다. `.xml.gz`는 해제.
* `include`/`exclude` 경로 정규식(범위 필터), `index_include`(자식 sitemap 필터).
* **lastmod는 게시일이 아니므로 순서 근거로 쓰지 않는다**(payload에 참고로만 보관). 순서가 필요하면
  `order_by_url_number`(URL 속 숫자 id 정규식)로 자식 sitemap 안에서 오름차순 정렬한다. 아니면 문서 순서.
* 대조: 자식별 필터 통과 URL 수 = 구간 총건수. 모든 자식이 처리되면 complete.

## 6. 항목별 실패 대장

| 원인 | 대장 상태 | 사유 코드 | 진행 |
|---|---|---|---|
| 상세 404/410 | failed_permanent | `http_404`, `http_410` | 계속 |
| 기타 4xx | failed_permanent | `http_4xx` | 계속 |
| 파서 예외 | failed_permanent | `parse_error:<예외형>` | 계속 |
| 파서가 빈 결과(None/제목·URL 없음) | failed_permanent | `empty_item` | 계속 |
| 저장 예외 | failed_permanent | `save_error:<예외형>` | 계속. 단 연속 `max_consecutive_save_errors`(기본 5)회면 저장 경로 고장으로 보고 사이트 중단(`config_error`) |
| 전송 오류/5xx/429 (재시도 후) | failed_transient → 다음 실행에서 재시도, `item_max_attempts` 도달 시 failed_retries_exhausted | `transient:<세부>` | 계속 |
| 401/403/챌린지/로그인/robots | (사이트 단위) | `access_blocked:<세부>` | **사이트 중단**, 우회 없음 |

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
                   "filter": {"type": "range", "from_param": "sdate", "to_param": "edate", "date_format": "%Y-%m-%d"},
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
  이중 인코딩하던 문제를 `urls.decode_preencoded`로 고쳤다(이미 인코딩된 형태만 한 번 디코드).
* 첫 페이지 누락: 새 틀은 `check_unparameterized_first_page`로 검증. 기존 수집기는 plan의 `start_page` 덮어쓰기로 교정.
* 적용 경로: 배달 워커(`delivery/worker/worker.py`)가 수집기 인스턴스를 만든 직후
  `legacy.apply_overrides_from_env`를 호출한다. 환경변수 `LIBERTREE_ENUMERATION_PLAN`이 없으면 아무것도 하지 않는다.
  plan 예: `"gri-re-kr-web": {"strategy": "...", "legacy": {"page_cap": null}}`.

## 11. 완전성 점검 보고 (report.py)

사이트별 행: `site_id, strategy, status, stop_reason, listed_unique, saved, failed_total, failed_by_reason,
source_reported_total, segment_sum_total, sitemap_url_count, independent_evidence, gap, segments_incomplete,
segments_mismatch, files_{pending,done,failed}, oldest_item`. JSON(상세 포함)과 CSV(평평한 열)로 쓴다.
`gap = 독립 근거 총건수 - listed_unique`(근거가 없으면 null — 근거 없음 자체를 보고).

## 12. 알려진 한계

* 날짜 구간 분할은 날짜 필터에 걸리지 않는 항목(날짜 없음)을 놓칠 수 있다 → `overall_total_check`로 탐지만 한다.
* 구간 내부/사이트맵 순서는 소스 순서에 의존한다(사이트맵 lastmod 미사용). "오래된 순"은 구간 단위로 보장.
* wrap이 첫 페이지가 아닌 `p mod N` 페이지로 되돌아가는 사이트는 `beyond` 술어가 단조가 아니다(미지원, 키 반복으로 탐지 시 중단).
* 커스텀 수집기 메서드가 내부에서 curl/requests를 직접 호출하면 게이트를 우회할 수 있다. 어댑터는 해당 수집기 모듈의
  `subprocess`/`requests`를 호출 차단 프록시로 바꿔 우회를 **오류로 드러낸다**(`adapter_ungated_network`).
