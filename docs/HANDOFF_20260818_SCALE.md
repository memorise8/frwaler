# 인계 — 규모 문제와 진행 중인 작업 · 2026-08-18

compact 이후 이 문서 하나로 이어서 진행할 수 있게 정리했다.

---

## 0. 지금 상태 한눈에

```
브랜치   delivery-mvp-audit-20260812
원격     5bf0797 까지 push 됨
로컬     f10110f  (미푸시 3개: 6a074a1 · ab77d82 · f10110f)
작업트리 docs/superpowers/plans/2026-08-18-delivery-a2-truncation.md 수정됨(미커밋)
```

**진행 중:** A2(잘린 수집 표시) — 태스크 4개 중 **2개 완료·리뷰 통과**, Task 3·4 남음.
**새로 발견:** 용량 데이터가 틀렸고, 대형 사이트는 현재 구조로 수집 불가능하다.

---

## 1. 먼저 끝낼 것 — A2 (Task 3, 4)

작업 방식은 `superpowers:subagent-driven-development` 로 계속한다.

```
계획      docs/superpowers/plans/2026-08-18-delivery-a2-truncation.md
ledger    .superpowers/sdd/2026-08-18-delivery-a2-truncation/progress.md
브리프    같은 디렉터리에 task-N-brief.md (스크립트로 추출)
BASE      Task 3 의 BASE = f10110f
```

**주의: 계획 파일에 미커밋 수정이 있다.** Task 3 의 안내 문구를 고친 것이며, 브리프를
추출하기 전에 먼저 커밋해야 한다. 커밋 메시지 취지:

> A2 계획의 안내 문구가 사실이 아니었다. "다시 실행하면 이어서 수집합니다"라고
> 쓰게 되어 있었으나, 800개 중 이미 수집한 것을 건너뛰는 크롤러가 사실상 없다
> (has_blob 0개, `_last_save_created` 0개, `pdf_downloaded` 조회 10개). 재실행은
> 1페이지부터 다시 걸어 같은 지점에서 또 잘린다. 그대로 안내했다면 운영자가 무한히
> 재실행하며 진도를 못 뺐을 것이다.

### 완료분 (재작업 금지)

| | 내용 | 커밋 | 리뷰 |
|---|---|---|---|
| Task 1 | `crawl_jobs.truncated` 컬럼 + `verify_required_schema` 등록 | `ab77d82` | ✅ findings 0 |
| Task 2 | 워커가 **경과 시간**으로 판정, `finish_job(..., truncated=)` | `f10110f` | ✅ minor 1 이월 |

Task 2 리뷰가 직접 확인한 것: `time.monotonic()` 사용, 정상 반환 경로에서만 플래그,
실패 경로는 `fail_job` 이라 구조적으로 불가능, 임계 계산 1500→1440.0 / 2→1.8.

### 남은 것

- **Task 3** — FE 표시. `job-status.ts` 에 `TRUNCATED_BADGE = "부분 수집"`,
  `describeJobOutcome` 에 안내문, `job-dashboard.tsx` 배지, `globals.css`.
  **CSS 토큰은 아래 목록에만 있는 것을 쓸 것** (없는 이름을 쓰면 `css-tokens.test.ts` 실패):
  `--canvas --surface --ink --muted --rule --moss --moss-deep --moss-wash --sand-wash --bad --bad-wash --sans --serif --mono`
- **Task 4** — 실물 확인 + 문서. **추가 요구:** `LIBERTREE_MAX_WALL_S` 가
  `docker-compose.yml` 의 worker environment 에도 `.env.example` 에도 없다. 배지가
  문제를 알려주기만 하고 고칠 방법은 어디에도 없는 상태이므로, 이 값을 노출하고
  런북에 적어야 한다. 트레이드오프도 함께: 워커가 단일 직렬이라 값을 크게 잡으면
  한 사이트가 큐 전체를 독점한다 — 큰 사이트는 별도 워커로 돌리는 편이 낫다.

---

## 2. 새로 발견한 것 — 용량 데이터가 틀렸다

`scripts/audit/capacity_final.csv` 가 여러 사이트를 크게 과소평가한다. 다른 조사
파일(`count_only_totalscan.csv`, `crawler_health_probe*.csv`)과 대조한 결과:

```
site_id                          capacity_final       실제 스캔    배수
doaj-org-search                         35,582     13,373,055    376x
cds-cern-ch-collection                     121        110,861    916x
etis-ee-portal                               1        327,834      —
prism-go-kr-homepage                    10,521        385,683     37x
datacatalogue-adruk-org-browser              0        219,675      —

804개 합계   기존 문서 기재   6,109,256
             보정 후        ≈ 20,480,139
```

`doaj-org-search` 는 세 번 따로 조사됐고 값이 조금씩 늘어난다(13,362,110 →
13,362,368 → 13,373,055) — 실제로 계속 등록되는 살아있는 색인이라는 증거다.
현재 DB 보유 **1,417건**, 즉 **0.01%**.

**따라서 `docs/DELIVERY_READINESS_20260818.md` 와 납품 인계 문서의 "610만 건 /
17.7 TB" 는 최소 3배 이상 과소평가다.** 아직 고치지 않았다 — 재조사 결과가 나온
뒤에 한 번에 고치는 편이 낫다.

---

## 3. 구조적 한계 — 대형 사이트는 지금 구조로 수집 불가능

한 줄 요약: **작업 하나 = 사이트 하나를 처음부터 끝까지 한 번에 훑기.**

수만 건까지는 문제없다. 1,337만 건에서는 원리적으로 불가능하다:

1. 25분(`LIBERTREE_MAX_WALL_S`)에 잘린다 — 크롤러 716개가 `break` 후 정상 반환
2. 재실행하면 **1페이지부터 다시** 걷는다 — 어디까지 갔는지 저장하는 크롤러 **0개/800**
3. 결과: 매번 같은 앞부분만 반복. 100 Mbps 기준 회당 약 3,289건, 4,000회 이상 필요

### 빠진 것 세 가지

| | 내용 | 규모 |
|---|---|---|
| **재개(resume)** | 커서를 DB에 남기고 다음 실행이 이어받기. 커서 의미가 사이트마다 다름(페이지 번호·OAI 토큰·날짜 범위) | 스키마 1 + 워커 + **크롤러 약 20개** |
| **작업 분할** | 작업이 "사이트 전부"가 아니라 "다음 N건"이 되어야 25분이 자르는 칼이 아니라 조각 크기가 된다 | 워커 + BE |
| **규모 표시** | 콘솔이 "1,337만 중 0.01%"를 말해야 한다. 지금은 모르고 시작한다 | BE + FE |

### 전면 재작성은 필요 없다

```
100만 이상       1개 (doaj 1,337만)   재개 필수. 없으면 영구 불가
10만~100만      약 18개               재개 있으면 수월, 없으면 예산 크게 + 전용 실행
10만 미만       785개                 지금 그대로 문제없음
```

**785개는 지금도 몇 분에 끝난다.** 손봐야 할 크롤러는 800개가 아니라 20개 안쪽이다.

### 저장 공간

20.5M × 평균 5.7MB ≈ **100 TB 이상**. 현재 1.7 TB. 서버가 어디든 "전부 받기"는
현실적이지 않다 — **무엇을 받을지 고르는 것이 설계의 일부**가 되어야 한다.

---

## 4. 다음에 할 일 (권장 순서)

### 1) 용량 재조사 — **먼저 할 것**

5개만 틀렸는지, 804개를 다 다시 봐야 하는지 모른다. **이게 정해져야 나머지 설계가
선다.** 지금 숫자를 믿고 설계하면 또 틀린다.

- `capacity_final.csv` 의 `source`/`method` 컬럼별로 신뢰도를 나눠볼 것.
  doaj 는 `api_oai_resumption` 으로 세다 멈춘 값이었고, totalscan 은 `count_crawl`
  로 끝까지 센 값이다 — 방법이 다르면 값이 다르다.
- 조사 스크립트는 `scripts/audit/` 에 있다(`consolidate_capacity.py`,
  `finalize_capacity.py`, `finish_capacity_pipeline.py`).

### 2) 재개 설계

커서를 어디에 어떤 모양으로 둘지. 후보: `crawl_sites_progress(site_id, cursor JSONB,
updated_at)` 또는 `sites` 에 컬럼 추가. 크롤러가 커서를 읽고 쓰는 계약을
`BaseCrawler` 에 두면 799/800이 이미 `self._save` 를 쓰므로 접점은 있다.

### 3) 수집 대상 선택

100 TB를 다 받지 않을 거라면 콘솔이 "이 사이트는 상위 N건만" 또는 "이 날짜 이후만"
을 지원해야 한다. 지금 `limit` 은 있으나 어디부터 N건인지는 정할 수 없다.

---

## 5. 지켜야 할 것 (변함없음)

- **워커는 살아 있다.** 실존 `site_id` 로 작업을 넣으면 실제로 제3자 사이트를 크롤한다.
  검증에는 존재하지 않는 site_id 나 주입한 가짜 크롤러를 쓴다.
- **`git add -A` / `git add .` 금지.** 다른 세션이 같은 브랜치에 파이썬을 커밋한다.
- **`delivery/fe/tsconfig.json` · `next-env.d.ts` 스테이징 금지.**
- **운영 DB(`libertree-delivery`, 문서 536,056건)에 테스트를 겨누지 않는다.** 테스트는
  `DROP SCHEMA` 를 한다. 일회성 `--tmpfs --rm` postgres 컨테이너를 쓴다.
- **운영 스택에 `down -v` 금지.** 리허설 스택(`delivery-coldstart`, 8091/3091)은 안전.
- 리허설 스택 이미지는 **코드가 바뀔 때마다 다시 빌드**해야 실물 검증이 의미를 갖는다.

### 테스트 명령

```bash
cd /mnt/raid/ruci_workspace/frwaler-delivery
docker rm -f a2-testpg >/dev/null 2>&1
docker run -d --rm --name a2-testpg --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_PASSWORD=devpw -e POSTGRES_USER=libertree -e POSTGRES_DB=libertree postgres:16 >/dev/null
until docker exec a2-testpg pg_isready -U libertree -d libertree >/dev/null 2>&1; do sleep 1; done
docker run --rm --network container:a2-testpg \
  -v /mnt/raid/ruci_workspace/frwaler-delivery:/app -w /app \
  -e TEST_PG_DSN='postgresql://libertree:devpw@127.0.0.1:5432/libertree' \
  -e DELIVERY_AUTH_MODE=disabled \
  libertree-delivery-worker:latest python -m unittest tests.test_worker_run tests.test_worker_jobs tests.test_be_app tests.test_delivery_migration -v
docker rm -f a2-testpg >/dev/null 2>&1

cd delivery/fe && npm test && npm run typecheck && npm run lint && npm run build
```

기준선: FE 281 / 28 파일 · Python 104 (A2 Task 1·2 로 늘어남).

---

## 6. 납품 상태 (이 발견 이전 기준)

- 소스 번들은 만들 수 있다: `delivery/scripts/build_release_bundle.sh` → 5.5 MB.
  검사 완료(크롤러 800개, `.env` 없음, `.git` 없음, 비밀값 없음).
- 데이터는 **소스와 별도로 전달**하기로 결정됨.
- 인계 문서: https://claude.ai/code/artifact/998ec5e6-fbc9-4b2b-9764-816433301f31
- 남은 납품 후 항목: **A1**(전부 차단당해도 `done`) — 745개 크롤러의 반환 계약을
  바꿔야 해서, 운영 로그가 쌓인 뒤 실제 차단 사이트만 손보는 편이 낫다.

**단, 위 3절의 규모 문제는 납품 문서에 아직 반영돼 있지 않다.** 납품처가 17.7 TB와
14개 사이트를 알고 시작한다고 적혀 있는데, 실제로는 100 TB 이상이고 doaj 하나가
1,337만 건이다. 재조사 후 반드시 갱신할 것.
