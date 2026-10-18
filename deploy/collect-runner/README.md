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
- `POST /runner/runs {site_id, mode:"resume"|"trial", limit?, requested_by}` → `201 {run_id}` / `409 {reason, detail}`
- `POST /runner/runs/{run_id}/cancel {requested_by}`
- 409 이유: plan_not_registered, plan_not_ready, bad_limit, large_needs_approval, unit_reserved, site_busy, unit_busy, folder_busy, external_unknown(실행기 밖 컨테이너를 판정 못 함), docker_unavailable, runner_paused
- 등록 뒤 plan 파일이 바뀌면(sha256) 실행하지 않고 failed. 중단된 이어받기는 최대 3번까지만 자동 재대기.
- 시험 수집(trial)의 limit 은 "요청 상한"으로 바뀐다: plan 사본의 `max_requests = limit×1.2 + 목록 쪽수 + 10`, `max_pages = ceil(limit/10)`. 수집 틀에 건수 옵션이 없어서다. 결과 `incomplete/request_budget` 가 정상.

## 기록
`runner.sqlite`(runs, events) 와 `events.jsonl`. `runnerctl events`, `runnerctl runs`. 실행 로그는 `logs/<run_id>.log`.
