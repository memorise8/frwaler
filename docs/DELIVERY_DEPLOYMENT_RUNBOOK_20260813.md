# Libertree Delivery 배포·운영 Runbook

## 배포 전 필수 순서

1. PostgreSQL dump와 blob manifest를 생성하고 별도 위치에서 복구 가능성을 확인한다.
2. 실데이터 DB를 복제한 이름에 `snapshot`, `rehearsal`, `staging`이 포함된 DB를 준비한다.
3. `rehearse_snapshot_schema.sh`로 원본 읽기 전용 대조와 스키마 적용을 검증한다.
4. 스냅샷 DB에서 대표 크롤러 E2E와 Qwen 100건 canary를 수행한다.
5. 오류율·품질 거부율·p95 지연·디스크·GPU 기준을 확인한다.
6. 운영 Worker를 중지하고 백업 시각을 기록한 뒤에만 아래 명시적 migration을 실행한다.

   ```bash
   docker compose --profile tools run --rm migrate
   ```

7. BE read-only smoke 후 Worker를 시작하되 작업은 자동 등록하지 않는다.
8. 고객이 요청한 소량 배치부터 시작한다.

## 보안

- `POSTGRES_PASSWORD`, `DELIVERY_API_TOKEN`, LLM key는 `.env` mode 0600에만 둔다.
- FE만 고객에게 공개하고 PostgreSQL·BE·GPU endpoint는 사설망/loopback으로 제한한다.
- Cloudflare Access에서 고객 한 명의 계정만 허용한다.
- 고객 브라우저에는 Delivery API token과 LLM key를 전달하지 않는다.
- 모델·prompt는 FE 서버 설정으로 고정한다.

## 백업과 복구

- DB: 매일 custom-format `pg_dump`, 주 1회 빈 임시 DB restore 검증.
- blob: 변경 불가능 원본으로 취급하고 manifest SHA-256/크기 대조 후 증분 동기화.
- 로그: Docker `10m × 5`, 민감 본문 비기록.
- 복구 순서: PostgreSQL → blob read-only mount → BE → FE → Worker.

## 장애 대응

- circuit open: 원인 코드를 확인하고 신규 배치를 중단한다. 승인 결과는 재처리하지 않는다.
- Worker heartbeat 만료: lease 회수 후 실패 시도만 재대기한다.
- disk 20GiB 미만: Worker 중지, 로그/임시 파일 점검. blob 원본은 삭제하지 않는다.
- GPU endpoint unhealthy: Delivery는 계속 문서 검색/수집을 제공하고 번역 배치만 중단한다.
- GPU 0 호스트에서는 `delivery/llm/scripts/report_metrics.sh`를 1분 주기로 실행해 숫자 표본만 전송한다.
- rollback: 새 컨테이너 중지, 이전 digest로 기동. schema는 additive이므로 테이블 삭제 rollback은 하지 않는다.

## 금지

- 운영 SQLite 쓰기, 운영 DB에서 파괴적 테스트, WAL 본체만 분리 복사
- 53만 건 일괄 등록
- circuit breaker 우회
- GPU 1 사용 또는 기존 Elasticsearch/Cloudflare route 임의 변경
