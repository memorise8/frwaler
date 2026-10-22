# 수집 실행기 (collect-runner)

수집 콘솔(`dashboard/server.py`)의 시작·이어받기·시험 수집 버튼이 부르는 서버 안 실행기.
등록된 plan 만 `deploy/enum-pilot/run-pilot.sh build|run` 으로 실행한다. 설계와 결정 기록은
`frwaler-delivery-out/plan-dev-20261008/dev-flow-20261018/status/B1.md`.

## 설치(서버, wise)
```
mkdir -p ~/collect-runner/units && cp runner.py runnerctl ~/collect-runner/
cp config.example.json ~/collect-runner/config.json        # 값 확인
cp <dev-flow>/lanes/*.csv ~/collect-runner/units/            # site_id → 등록 도메인(unit)
~/collect-runner/runnerctl ensure                            # 127.0.0.1:8793, 토큰 .token 자동 생성
```
자동 재시작은 아직 없다(사용자 결정 보류). 서버가 재부팅되거나 실행기가 죽으면 `runnerctl ensure`.
실행 중이던 이어받기 작업은 다시 뜰 때 대기열로 돌아가 같은 폴더에서 이어받는다(시험 수집은 `failed/interrupted`).

## 등록
```
runnerctl register --site <id> --plan <plan.json> --src <소스 폴더> --expected <예상 건수> [--folder YYYYMMDD-태그] [--approved-large]
runnerctl list | unregister --site <id>
```
- plan 에서 그 사이트만 복사해 `registry/plans/<id>.json` 으로 고정하고 소스 폴더의 plan.py 로 검증한다.
- 예상 1만 건 이상이거나 모르면 `--approved-large`(사용자 승인) 없이는 이어받기(resume)를 거절한다. 시험 수집은 된다.
- `--folder`: 이미 수집하던 폴더를 이어받을 때. 없으면 첫 이어받기 때 `YYYYMMDD-sN` 을 정한다.

## API (127.0.0.1, 머리글 `X-Runner-Token: <.token>`)
- `GET /runner/plans`, `GET /runner/runs[?site_id=]`, `GET /runner/health`
- `POST /runner/runs {site_id, mode:"resume"|"trial"|"incremental", limit?, requested_by}` → `201 {run_id}` / `409 {reason, detail}`
- 이어받기 선택 항목(`mode:"resume"` 에만, 다른 모드면 409):
  - `reprocess_failed: ["empty_item", ...]` — 그 이유로 실패한 항목을 다시 시도(`--reprocess-failed`, 같은 폴더). 허용 앞부분:
    `empty_item`, `parse_error`, `adapter_contract`, `transient`(상세 일시 실패가 시도 횟수를 다 씀), `save_transient`, `save_error`
    (뒤에 `:<세부>` 가능, 예 `adapter_contract:paper_title_missing`). 그 밖은 `409 bad_reprocess_failed`. 실행기가 이 값만으로
    `ENUM_EXTRA_ARGS` 를 만든다(임의 인자는 여전히 안 받음).
  - `auto_continue: {max_runs: 5, on: ["time_budget"]}` (max_runs 1~20, 기본 5; on 은 time_budget 만) — 이어받기가
    `incomplete/time_budget` 로 끝나면 실행기가 같은 폴더로 다음 이어받기를 대기열에 넣는다. `reprocess_failed` 는 묶음의
    첫 작업에만 넘긴다(그때 항목이 다시 대기열에 들어갔다; 다음 작업은 보통 이어받기, `chain.reprocess_failed_in` 에 기록). 다음 작업도 요청과 같은
    규칙(일시정지·등록·ready·대량 승인·예약 도메인)을 다시 보고, 사이트·도메인·폴더가 바쁘면 대기열에서 빌 때까지 기다린다.
    그 밖의 끝남(완료·차단·robots·config_error·rate limit·listing_transient·취소·실패)이나 max_runs 에 이르면 멈춘다.
    작업 행의 `chain {root, seq, end}` 와 events 의 `auto_continue`·`chain_end`(묶음의 run_id·결과 목록)에 남는다.
    `end`: `outcome:<상태>/<이유>`, `run_<cancelled|failed>`, `max_runs`, `refused:<이유>`.
- 목록 일시 실패 재시도(수집 틀, 기본 켜짐 — 이번 변경으로 동작이 바뀜): 목록 요청이 Fetcher 재시도 뒤에도 5xx·연결 오류면
  plan `listing_retry`(기본 `{"wait_s": 300, "max": 3}`, `max: 0` 이면 끔)대로 5분 기다렸다가 같은 요청을 실행당 최대 3번 다시 한다.
  대기가 `max_runtime_s` 안에 들지 않으면 기다리지 않고 `incomplete/listing_transient:<이유>` 로 끝난다(auto_continue 묶음도 멈춤).
  429·차단·robots·404·리다이렉트 정책 거절은 다시 하지 않는다. 새 수집 틀 이미지(등록 `--src` 소스)가 있어야 적용된다.
- `POST /runner/runs/{run_id}/cancel {requested_by}`
- 409 이유: plan_not_registered, plan_not_ready, bad_limit, large_needs_approval, unit_reserved, site_busy, unit_busy, folder_busy, external_unknown(실행기 밖 컨테이너를 판정 못 함), docker_unavailable, runner_paused
- 등록 뒤 plan 파일이 바뀌면(sha256) 실행하지 않고 failed. 중단된 이어받기는 최대 3번까지만 자동 재대기.
- 증분(incremental): `POST /runner/runs {site_id, mode:"incremental"}` (limit 없음). 그 사이트의 이어받기 폴더(상태 저장소·문서 DB)에서
  `run-pilot.sh` 를 `ENUM_INCREMENTAL=1` 로 돌려 목록 앞쪽부터 새 글만 받고, 이미 아는 항목이 plan 의 `incremental.known_run`(기본 50)건
  연속이면 멈춘다(`incremental.max_pages` 기본 50쪽 상한이라 큰 수집 승인 불필요). 최신순 목록(plan 이 last_page_backward 이거나
  `incremental.order: "newest_first"` 선언)만 받고, 그 밖은 `409 incremental_unsupported`. 사이트 하나·도메인 하나씩, plan ready 필수는 같다.
  config 의 `known_keys_dir` 에 `<site_id>.csv`(site_id,post_number,meta_url — 운영 DB 에서 운영자가 내보낸 것)가 있으면 `--known-keys` 로 넘긴다.
  결과는 `result.incremental {status, stop_reason, requests, saved_new}`. 받은 문서는 같은 폴더 enum_pilot.db 에 쌓여 가져오기가 그대로 옮긴다.
- 시험 수집(trial)의 limit 은 "요청 상한"으로 바뀐다: plan 사본의 `max_requests = limit×1.2 + 목록 쪽수 + 10`, `max_pages = ceil(limit/10)`. 수집 틀에 건수 옵션이 없어서다. 결과 `incomplete/request_budget` 가 정상.

## 기록
`runner.sqlite`(runs, events) 와 `events.jsonl`. `runnerctl events`, `runnerctl runs`. 실행 로그는 `logs/<run_id>.log`.
