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

```
시작: cursor 읽기 → inst.delivery_cursor 주입 → crawl()
잘림(truncated): 커서 전진했으면 저장 + 같은 (site, backfill) 작업을 큐 맨 뒤 재큐잉
                 전진 없으면 저장·재큐잉 없음 (무한 루프 가드; job_events 에 stalled 기록)
완주:            진도 저장 + completed_at 기록, 재큐잉 없음
취소:            커서는 저장, 재큐잉 없음 (체인 절단 — 운영자 의도 존중)
실패(예외):      커서 저장 없음 (마지막 성공 지점 유지), 기존 attempts 재시도 체계 적용
```

- 재큐잉은 새 `crawl_jobs` INSERT — `claim_next_job` 이 `ORDER BY created_at` 이므로
  자동으로 큐 맨 뒤가 되어 다른 사이트가 굶지 않는다.
- **"커서 전진"이 재큐잉의 유일한 면허** — 어떤 결함도 무한 자동 크롤로 이어질 수 없다.
- oldest_first 크롤러는 `mode=incremental` 에서도 커서를 주입받고, 정상 완료 시
  전진분을 저장한다(재큐잉은 backfill 전용).

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

    def _advance_cursor(self, cursor: dict, items_done: int = 0) -> None:
        # 메모리에만 기록. DB 저장·커밋은 워커의 몫 (크롤은 한 트랜잭션)
```

`_save_paper_v2` 의 incremental-기보유 분기에서 `_consecutive_known` 을 세고,
`DELIVERY_ORDER == "newest_first"` 이고 임계에 닿으면 `CrawlUpToDate` 를 올린다.
새 문서 저장 시 0으로 리셋. `CrawlCancelled` 와 같은 제어 흐름 패턴이라 크롤러
루프를 고칠 필요가 없다.

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
