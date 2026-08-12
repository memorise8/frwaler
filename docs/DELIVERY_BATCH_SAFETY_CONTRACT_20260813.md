# Libertree Delivery · Q1 배치 안전장치와 관측 계약

> 계약 버전: `batch-safety-v1`

## 목적

대량 처리 전에 모든 작업을 배치와 시도 단위로 추적하고 최근 오류율·지연·호스트 디스크를
근거로 신규 실행을 자동 중단한다. 원문, 번역, 요약, API key와 Authorization 값은 로그 및
관측 테이블에 저장하지 않는다.

## 저장 계약

- `translation_batches`: 요청 한 번의 설정, 대상/등록 수, 상태와 중단 사유
- `translation_jobs.batch_id`: 작업이 속한 배치. 기존 행과 호환하기 위해 nullable
- `translation_job_attempts`: 실제 Provider 호출 한 번마다 한 행. 상태, 표준 오류 코드,
  실제 token usage, finish reason, 지연만 저장
- `translation_worker_heartbeats`: worker ID, lease와 마지막 활동. hostname 원문 대신 명시적 ID 사용
- `translation_system_observations`: `endpoint_healthy`, `disk_free_bytes`, `gpu_memory_free_bytes`,
  `gpu_utilization_percent` 같은 숫자/boolean 표본. 상세 응답이나 credential 저장 금지

작업 claim은 `worker_id`와 `lease_expires_at`을 기록한다. lease가 만료된 `running` 작업은
`stale_lease` 시도로 닫고 최대 시도 횟수 안에서만 `pending`으로 회수한다.

## Provider 결과 계약

OpenAI 호환 응답의 `usage.prompt_tokens`, `usage.completion_tokens`와 `finish_reason`을 저장한다.
Ollama는 `prompt_eval_count`, `eval_count`, `done_reason`을 같은 필드로 정규화한다. 값이 없는
Provider는 null을 저장하며 문자 수로 위장하지 않는다.

## 초기 circuit breaker

신규 배치 enqueue 직전에 같은 provider/model/prompt의 최근 완료 시도 최대 50건과 15분 이내
시스템 표본을 평가한다.

- 표본 10건 이상에서 Provider 실패율 > 10%: 중단
- 표본 10건 이상에서 p95 latency > 60초: 중단
- 최신 endpoint 표본이 unhealthy: 중단
- 최신 disk free < 20 GiB: 중단
- 최신 GPU free < 1 GiB: 중단
- 최근 표본 없이 100건 초과 요청: 중단

중단은 HTTP 409와 안정적인 reason code로 반환한다. `translation_batches` 상태는
`queued`, `running`, `completed`, `paused`, `cancelled`이며 배치의 모든 작업이 종결되면 집계로
상태를 갱신한다.

## 단계별 확대

모델·prompt 조합마다 최대 요청 크기는 기본 100건이다. 100건의 종결 표본이 있고 Provider
실패율 1% 이하, rejected 0.1% 이하일 때 1,000건까지 허용한다. 10,000건 단계는 별도 명시적
환경 설정과 1,000건 검증 후에만 허용하며 이 계약의 기본 API 상한은 계속 1,000건이다.

## API

- 기존 `POST /translation/preview`: `estimated_input_chars`, `estimated_prompt_tokens`,
  `estimated_seconds`, `safety` 추가
- 기존 `POST /translation/jobs`: `batch_id`, `safety` 반환; breaker open이면 409
- `GET /translation/batches`: 배치 목록과 집계. 원문/결과 없음
- `GET /translation/batches/{id}`: 배치 집계, 오류/지연/token/품질 지표. 원문/결과 없음
- `GET /translation/operations`: worker heartbeat, breaker 상태, 최신 인프라 표본

## 로그

Worker stdout은 JSON lines로 `event`, `worker_id`, `batch_id`, `job_id`, `attempt_id`, `status`,
`error_code`, `latency_ms`, token counts만 기록한다. exception 문자열, 요청/응답 body, URL query,
headers는 기록하지 않는다. Docker 서비스는 `json-file`, `max-size=10m`, `max-file=5`로 회전한다.
LLM Nginx도 파일 대신 stdout/stderr에 기록해 같은 Docker 회전 정책을 적용한다.
