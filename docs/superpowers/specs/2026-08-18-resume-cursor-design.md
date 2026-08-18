# 재개(cursor) 설계 — 대형 사이트 백필·증분·진도 표시

승인 경로: 2026-08-18 브레인스토밍 (접근안 A 선택, 자동 재큐잉 선택, ②③ 포함 전체 범위 확정).

## 목표

10만 건 이상 25개 사이트(`scripts/audit/capacity_corrected.csv` 기준)가 25분
예산(`LIBERTREE_MAX_WALL_S`)에 잘려도 **여러 조각에 걸쳐 결국 완주**하게 한다.
아울러 완주 후 신규분 수집이 전체 재순회 없이 끝나게 하고(②), 콘솔에서
백필 진도를 볼 수 있게 한다(③).

근거 조사: 25개 크롤러 전부 커서가 정수 하나로 환원된다 — `{"page": N}` 16개,
`{"offset": N}` 9개. OAI 토큰·스크롤 커서 등 불투명 상태 없음.

## 1. 데이터 모델

```sql
CREATE TABLE IF NOT EXISTS crawl_site_progress (
    site_id        TEXT PRIMARY KEY,
    cursor         JSONB,                      -- {"page": 4211} 또는 {"offset": 210500}
    items_done     BIGINT NOT NULL DEFAULT 0,  -- _advance_cursor 보고 누적
    total_estimate BIGINT,                     -- capacity_corrected.csv 에서 시드 (대상 외 NULL)
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ                 -- 백필 완주 시각, NULL=미완주
);
```

- `crawl_jobs.mode` 에 `"backfill"` 추가. **작업(job)에만 허용, 스케줄에는 불허**
  — 스케줄이 주기적으로 backfill 을 넣으면 자동 재큐잉과 이중 등록이 된다.
- 완주 시 `completed_at` 기록, **cursor 는 지우지 않는다** — oldest_first 사이트의
  증분이 커서를 이어 쓴다.

## 2. 워커 흐름 (mode=backfill)

> **2026-08-18 보정:** 최초안은 "잘리지 않고 정상 반환"을 완주 신호로 썼다.
> 전체 브랜치 리뷰가 이 판정을 doaj-org-search(약 1,337만 건)로 검증해 실패를
> 확인했다: 한 조각(약 200페이지, ~16분)이 25분 예산의 truncation 임계 아래라
> "안 잘렸다"로 보였고, 그 결과 첫 조각에서 곧바로 완주 처리되어 만 건도 못
> 걸은 채 체인이 죽었다(10,000/13,373,055). 아래 표는 그 오판을 없앤
> 교정판이다 — 완주는 이제 크롤러의 명시 신호(`_mark_exhausted` →
> `delivery_exhausted`) 하나로만 판정하며, 시간·예산(`truncated`)은 이 판정에
> 전혀 관여하지 않는다. 아울러 "정상 반환과 동시에 취소가 도착하는" 경로
> (`finish_job` 이 `running→done` 대신 `cancelling→cancelled` 로 떨어지는
> 경우)도 명시적 취소 경로와 동일하게 체인을 끊도록 고쳤다 — 이전에는 그
> 경로가 완주·재큐잉을 계속 허용해 운영자의 취소를 무시했다.

```
시작: cursor 읽기 → inst.delivery_cursor 주입 → crawl()

정상 반환 (예외 없음):
  finish_job 이 이 작업을 여전히 소유하는지 먼저 확인한다(반환값이 "done"인지
  "cancelled"인지 None인지) — lease 회수나 동시 취소로 소유권을 잃었으면
  아무것도 하지 않는다.
    소유권 상실(None):        아무것도 하지 않는다(다른 곳이 이 행을 넘겨받았다)
    "cancelled" (정상 반환과 동시에 취소 확정):
                               커서는 저장(cursor_enabled 일 때), 완주·재큐잉은 건너뛴다
    "done":
      inst.delivery_exhausted 가 참 (크롤러가 목록 끝에 도달했다고 명시):
                               진도 저장 + completed_at 기록, 재큐잉 없음 — 완주
      exhausted 아님, 커서 전진 AND 항목 목격(items_done>0 또는 실제 저장>0):
                               진도 저장 + 같은 (site, backfill) 작업을 큐 맨 뒤 재큐잉
                               (truncated 여부와 무관 — 잘렸든 안 잘렸든 전진이 있으면 계속)
      exhausted 아님, 위 조건 미충족:
                               저장·재큐잉 없음 (무한 루프 가드; job_events 에 stalled 기록)
취소(CrawlCancelled 예외):      커서는 저장, 재큐잉 없음 (체인 절단 — 운영자 의도 존중)
신규분 소진(CrawlUpToDate):     완료 신호로 취급 — 진도 저장 + completed_at 기록
실패(예외):                    커서 저장 없음 (마지막 성공 지점 유지), 기존 attempts 재시도 체계 적용
```

- 재큐잉은 새 `crawl_jobs` INSERT — `claim_next_job` 이 `ORDER BY created_at` 이므로
  자동으로 큐 맨 뒤가 되어 다른 사이트가 굶지 않는다.
- **"커서 전진 + 항목 목격"이 재큐잉의 유일한 면허, "exhausted 신호"가 완주의
  유일한 면허** — 어떤 결함도 무한 자동 크롤이나 오판된 완주로 이어질 수 없다.
- oldest_first 크롤러는 `mode=incremental` 에서도 커서를 주입받고, 정상 완료 시
  전진분을 저장한다(재큐잉·완주 판정은 backfill 전용).
- `save_progress` 는 커서를 갱신할 때 `completed_at` 을 NULL 로 되돌린다 — 완주
  후에도 oldest_first 증분이나 뒤늦은 백필 조각이 커서를 다시 전진시키면, 그
  사이트는 더 이상 "완주"가 아니므로 화면이 거짓을 말하지 않는다.

## 3. BaseCrawler 계약 (`crawler/base_crawler.py`)

추가만 하고 기존 메서드는 변경하지 않는다. 기본값에서 기존 785개 크롤러의 동작은
바이트 단위로 동일해야 하며, 이를 테스트로 고정한다.

```python
class CrawlUpToDate(RuntimeError):
    """incremental 이 이미 수집된 영역에 도달함 (newest_first 전용 조기 종료)."""

class BaseCrawler:
    DELIVERY_ORDER = "arbitrary"        # newest_first | oldest_first | arbitrary
    UP_TO_DATE_THRESHOLD = 50           # 연속 기보유 N건이면 CrawlUpToDate

    # __init__ 추가분
    self.delivery_cursor = None         # 워커 주입. 크롤러는 시작점으로 읽음
    self._pending_cursor = None         # _advance_cursor 가 기록, 워커가 회수
    self._cursor_items_done = 0
    self._consecutive_known = 0
    self._exhausted = False             # (2026-08-18 보정) _mark_exhausted 가 기록

    def _advance_cursor(self, cursor: dict, items_done: int = 0) -> None:
        # 메모리에만 기록. DB 저장·커밋은 워커의 몫 (크롤은 한 트랜잭션)

    def _mark_exhausted(self) -> None:
        # (2026-08-18 보정) 목록 끝까지 걸었다는 명시 신호. 백필 완주 판정의
        # 유일한 근거 — §2 참고. delivery_exhausted 프로퍼티로 워커가 회수한다.
```

`_save_paper_v2` 의 incremental-기보유 분기에서 `_consecutive_known` 을 세고,
`DELIVERY_ORDER == "newest_first"` 이고 임계에 닿으면 `CrawlUpToDate` 를 올린다.
새 문서 저장 시 0으로 리셋. `CrawlCancelled` 와 같은 제어 흐름 패턴이라 크롤러
루프를 고칠 필요가 없다.

**(2026-08-18 보정)** 25개 대형 사이트 크롤러는 각자의 자연스러운 목록 종료
지점(빈 결과 페이지, `has_next=false`, `totalPages` 도달 등) 바로 앞에서
`self._mark_exhausted()` 를 호출한다. fetch 실패·JSON 파싱 오류·이번 실행의
예산/페이지 캡 소진·`limit` 컷은 목록이 끝났다는 뜻이 아니므로 호출하지
않는다 — 이 구분이 §2 보정의 전제다. 이 구분이 실제로 유효하려면 목록을
가져오는 헬퍼 자체가 실패(반환값 `None`)와 빈 목록(반환값 `[]`/`{}` 이지만
성공한 응답)을 서로 다른 값으로 돌려줘야 한다 — 둘을 같은 falsy 값으로
합쳐 반환하면 자연 종료 지점의 호출부가 일시적 fetch 실패를 목록 끝으로
오판해 `_mark_exhausted()` 를 잘못 부르게 된다(2026-08-18 Probe G, 5개 파일
에서 재현: data-gov-au-data, etis-ee-portal, ots-at-pressemappe,
e-stat-go-jp-stat-search, inserm-hal-science-search).

## 4. 크롤러 패치 (25개)

패턴: 루프 시작값을 `delivery_cursor` 에서 읽고, 페이지 끝마다
`self._advance_cursor({...}, len(items))` 한 줄. 가족별:

| 가족 | 파일 수 | 커서 | DELIVERY_ORDER | 비고 |
|---|--:|---|---|---|
| HAL Solr | 6 | `{"offset": start}` | oldest_first | 무정렬 4개(anr·amu·ehess·ens-lyon)에 `sort=docid asc` 추가 — inserm·cea 와 동일한 안정 정렬 |
| DSpace7 REST | 4 | `{"page": n}` (0-기준) | newest_first | anu·ethz·ut-ee 동일 패치, ostrnrcan 변형 |
| DSpace XMLUI | 1 | `{"page": n}` | newest_first | uchile |
| Invenio | 2 | `{"page": n}` | newest_first | sonar, cds-cern |
| CKAN | 1 | `{"offset": start}` | arbitrary | data-gov-au |
| 개별 | 11 | page 또는 offset | 표별 지정 | doaj·e-stat·ots·ntrs·prism·etis·adruk·etera·data-gv-at·mof·pergamos |

## 5. ② 신규분 증분

- newest_first (~12개): incremental 중 연속 50건 기보유 → `CrawlUpToDate` → 워커가
  정상 `done` 처리. 크롤러당 변경은 `DELIVERY_ORDER` 한 줄.
- oldest_first (HAL 6개): 신규분이 끝에 붙으므로 커서가 증분 그 자체.
- arbitrary: 조기 종료 불가(순서 보장이 없어 미수집 구간을 건너뛸 위험). 현행 유지.

## 6. ③ 콘솔 진도

- 시드: `delivery/scripts/seed_backfill_estimates.py` 가 `capacity_corrected.csv` 의
  `corrected_max` 를 25개 사이트의 `total_estimate` 로 업서트 (1회성, 재실행 무해).
- BE: 인증 필요 `GET /progress` — 진도 행 + 사이트별 보유 문서 수.
- FE: 새 라우트 `(shell)/backfill` — 사이트/진행률(items_done÷total_estimate)/상태
  (시작 전·진행 중·완주)/마지막 전진 시각. 계산은 순수 함수 lib (`backfill-progress.ts`)
  로 분리해 vitest (jsdom 없음 규약 준수).

## 7. 검증 정책

- **실사이트 크롤 금지.** 모든 테스트는 가짜 크롤러(레지스트리 주입)와 일회용
  postgres 컨테이너. 리허설 스택 확인도 가짜 site_id + 주입 크롤러만.
- 고정할 성질: 잘림→저장→재큐잉→이어받기→완주 사이클, 무전진 가드, 취소 체인 절단,
  실패 시 커서 불변, CrawlUpToDate 조기 종료와 그 후 done 처리, 기본값에서 기존
  동작 불변, 25개 파일의 패턴 준수(정적 소스 검사 — 하이픈 파일명이라 import 불가).

## 범위 밖

- 작업 분할(조각 크기 지정), 스케줄의 backfill, 진도 알림, 크롤러 25개 외 확장.
- 실측 재검증 (ots-at-pressemappe 1.56M 은 single_source — 백필이 곧 실검증이 된다).
