# 제안: 수집기가 이미 아는 것을 기록하기

**대상:** `delivery/worker`, `delivery/db`, `delivery/be` 담당
**작성:** FE 작업 중 발견, 실측 근거 포함
**요약:** 워커가 이미 계산한 값 두 개를 버리고 있습니다. 저장만 하면 콘솔이 답하지 못하던 질문 두 개에 답할 수 있습니다.

---

## 배경

납품 콘솔에서 운영자가 크롤러 상세를 볼 때 지금 알 수 없는 것이 두 가지입니다.

1. **이번 수집이 실제로 무엇을 했는가** — 화면은 "신규 0건"까지만 말할 수 있습니다.
2. **이 사이트에서 얼마나 빠뜨렸는가** — 23건을 모았을 때 그게 전부인지 5%인지 아무도 모릅니다.

두 값 모두 **크롤 실행 중에는 존재합니다.** 저장되지 않을 뿐입니다.

---

## 1단계 — 크롤러의 처리 건수 기록 (작음, 권장)

### 문제

`delivery/worker/worker.py`의 `run_job()`:

```python
inst.crawl(limit=job.get("limit_n"))          # ← 반환값을 버림
...
saved = _count_site_docs(conn, site_id) - before
jobs.finish_job(conn, job["id"], saved_count=max(0, saved))
```

`crawl()`은 처리 건수를 반환합니다. 워커 컨테이너에서 직접 호출해 확인했습니다:

```
>>> inst = CRAWLERS['academie-sciences-fr-espace-presse'](db_conn=conn, delay=1.0)
>>> inst.crawl(limit=3)
3
```

그런데 `run_job`은 이 값을 무시하고 **해당 사이트 행 수의 순증분**만 기록합니다. 그래서 이미 수집된 문서를 재처리하면 `saved_count = 0`이 됩니다.

실측 사례 (`bfr-bund-de-en`, 2026-08-14):

| 출처 | 값 |
|---|---|
| 워커 컨테이너 로그 | `done. Total saved: 3` |
| `crawl_jobs.saved_count` | `0` |
| `documents` 행 수 변화 | 23 → 23 |

셋 다 맞습니다. 다만 **운영자에게 보이는 것은 `0` 하나뿐**이고, 그것이 "실패"인지 "새 문서 없음"인지 화면만 봐서는 구분되지 않습니다. 실제로 이 불일치 때문에 존재하지 않는 버그를 한동안 추적했습니다.

### 제안

`crawl()`의 반환값을 별도 컬럼에 함께 기록합니다.

```python
processed = inst.crawl(limit=job.get("limit_n"))
saved = _count_site_docs(conn, site_id) - before
jobs.finish_job(conn, job["id"],
                saved_count=max(0, saved),
                processed_count=processed)
```

- `crawl_jobs`에 `processed_count INTEGER` 추가 (nullable — 기존 행과 반환값이 없는 크롤러 대비)
- `BE /jobs`, `/jobs/{id}` 응답에 포함

**주의:** 800개 크롤러가 모두 정수를 반환한다는 보장은 확인하지 않았습니다. `None`이나 다른 타입을 돌려주는 구현이 있을 수 있으니 방어적으로 처리해야 합니다. nullable로 두고 값이 없으면 화면이 표시하지 않는 편이 안전합니다.

### 이것이 푸는 것

콘솔이 `처리 3건 · 신규 0건`이라고 말할 수 있게 됩니다. 지금은 `신규 0건`만 보이고, 왜 0인지는 컨테이너 로그를 봐야 압니다.

`delivery/README.md`의 "수집 결과 읽는 법" 절이 이 혼동을 문서로 덮고 있는데, 이 변경이 들어가면 그 설명 자체가 필요 없어집니다.

---

## 2단계 — 수집률 (큼, 조율 필요)

### 문제

원본 사이트에 문서가 몇 건 있는지 **어디에도 기록되지 않습니다.** `crawler/base_crawler.py`, `delivery/db/schema.py`, `delivery/worker/` 전체에서 `total_available`, `discovered`, `expected_total` 류의 식별자를 찾지 못했습니다. `documents` 테이블에도 커버리지 관련 컬럼이 없습니다.

그래서 답할 수 없습니다:

> `bfr-bund-de-en`에 23건이 있는데, 이것이 전부인가 일부인가?

**그런데 크롤러는 실행 중에 알고 있습니다.** 같은 실행의 로그입니다:

```
[bfr-bund-de-en] list endpoint: https://www.bfr.bund.de/api/v1/en/mini-suche
                 filters=['folder:117','folder:1179','folder:3746']
[bfr-bund-de-en] list page 1/4: discovered 6 records
```

`1/4` — 총 4페이지임을 알고 있습니다. 알아낸 뒤 버립니다.

### 왜 1단계보다 큰가

`crawl()`은 `base_crawler.py:61`에서 **추상 메서드**이고, 800개 사이트 크롤러가 각자 페이지네이션을 구현합니다. 공유 지점이 없습니다. 즉 한 곳을 고쳐서 전부 얻을 수 없습니다.

### 제안 방향

베이스 클래스에 선택적 훅을 두고 크롤러가 아는 경우에만 보고하게 합니다.

```python
# base_crawler.py
def _report_discovered(self, total: int) -> None:
    """목록에서 확인된 전체 건수를 보고한다. 알 수 없으면 호출하지 않는다."""
```

- 호출하지 않는 크롤러는 지금과 동일하게 동작 (`NULL` = 미상)
- `crawl_jobs.discovered_count INTEGER NULL`
- 페이지네이션 총계를 노출하는 크롤러부터 점진 적용

**미상과 0을 반드시 구분해야 합니다.** "확인 결과 0건"과 "확인하지 않음"이 같은 값이 되면, 콘솔이 지금 서킷 브레이커에서 겪었던 것과 같은 문제가 생깁니다 — 측정한 적 없는 상태를 정상으로 표시하는 것.

### 이것이 푸는 것

- 사이트별 수집률: `문서 23 / 확인 87건`
- "실패는 아닌데 절반만 모으는" 크롤러를 찾아낼 수 있음 — 현재 이 상태는 완전히 보이지 않습니다
- 804대 중 772대 정상이라는 숫자에 **깊이**가 생김. 지금은 "돌긴 하는가"만 알고 "제대로 모으는가"는 모릅니다

---

## 함께 검토할 만한 것

FE 작업 중 확인된, 백엔드 변경이 필요한 사항들입니다. 우선순위는 담당자 판단입니다.

| 항목 | 현재 | 영향 |
|---|---|---|
| 예약 삭제 | `GET`·`PUT /schedules`만 존재 | 잘못 만든 예약을 지울 수 없고, 중지만 가능 |
| 번역 provider 설정 상태 | 조회 수단 없음 | 미설정 사실을 작업이 실패한 뒤에야 알 수 있음. FE는 `error_code="configuration"`을 보고 역추정 중 |
| 조회 API 인증 | `GET` 계열 전부 무인증 | 프록시 전제로 보이나 확인 필요. 포트에 닿으면 문서 53만 건 열람 가능 |
| 크롤 상세 로그 | 컨테이너 로그에만 존재 | DB 작업 로그는 `queued`/`started`/`completed` 3줄. 어떤 엔드포인트를 어떤 필터로 긁는지 화면에서 알 수 없음 |

---

## 권장 순서

1. **1단계 먼저.** 변경이 작고, 실측으로 확인된 혼동을 없애며, 2단계와 독립적입니다.
2. 2단계는 훅만 먼저 넣고 크롤러 적용은 점진적으로. 전면 적용을 기다릴 필요가 없습니다.

두 단계 모두 FE는 값이 들어오는 즉시 표시할 수 있도록 준비돼 있습니다.
