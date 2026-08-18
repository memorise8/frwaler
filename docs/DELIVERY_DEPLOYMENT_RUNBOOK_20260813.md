# Libertree Delivery 배포·운영 Runbook

## 배포 전 필수 순서

1. PostgreSQL dump와 blob manifest를 생성하고 별도 위치에서 복구 가능성을 확인한다.
2. 실데이터 DB를 복제한 이름에 `snapshot`, `rehearsal`, `staging`이 포함된 DB를 준비한다.
3. `rehearse_snapshot_schema.sh`로 원본 읽기 전용 대조와 스키마 적용을 검증한다.
4. 스냅샷 DB에서 대표 크롤러 E2E와 Qwen 100건 canary를 수행한다.
5. 오류율·품질 거부율·p95 지연·디스크·GPU 기준을 확인한다.
6. 운영 Worker를 중지하고 백업 시각을 기록한 뒤에만 아래 명시적 migration을 실행한다.

   ```bash
   docker compose -f delivery/docker-compose.yml --profile tools run --rm --build migrate
   ```

   `--build`가 필요한 이유: `migrate`는 프로파일 뒤에 있어 평소의 전체 빌드가
   건너뛰므로, 이 플래그가 없으면 캐시된 옛 이미지가 옛 스키마 코드로 조용히
   `PASS`를 찍을 수 있습니다(아래 「주 경로」 절의 설명 참조).

7. BE read-only smoke 후 Worker를 시작하되 작업은 자동 등록하지 않는다.
8. 고객이 요청한 소량 배치부터 시작한다.

## 보안

- `POSTGRES_PASSWORD`, `DELIVERY_API_TOKEN`, LLM key는 `.env` mode 0600에만 둔다.
- `DELIVERY_AUTH_MODE=token`을 유지하며, 32자 이상의 `DELIVERY_API_TOKEN`이 없으면 BE는 기동하지 않는다. `disabled`는 격리된 로컬 개발 전용이다.
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

## 노출 범위 — 반드시 지켜야 할 전제 (2026-08-18 확정)

**BE를 인터넷이나 사내망에 직접 노출하지 마세요.** 프록시나 방화벽 뒤에 두거나,
같은 호스트의 FE만 접근하게 두어야 합니다.

조회 API 18개에는 운영자 토큰이 걸려 있지 않습니다. 쓰기 10개 중 9개가 토큰을
요구합니다(`POST /translation/preview` 는 동사만 POST일 뿐 집계 조회만 수행합니다
(count·sum·avg, 쓰기 없음)). 즉 **포트에 닿을 수 있는 사람은 문서 전체를 읽을 수 있습니다.**

이 개수는 2026-08-18 기준입니다. 같은 날 `GET /jobs/summary` 와
`DELETE /schedules/{site_id}` 가 추가되어 직전 문서의 17개·9개에서 늘었습니다.
라우트를 추가하면 이 숫자를 함께 갱신하세요.

지금 이것을 막고 있는 것은 `delivery/docker-compose.yml` 의 다음 두 줄뿐입니다.

    ports:
      - "127.0.0.1:${BE_PORT:-8080}:3001"
      - "127.0.0.1:${FE_PORT:-3000}:3002"

`127.0.0.1:` 을 지우거나 `0.0.0.0:` 으로 바꾸면 이 전제는 즉시 깨집니다.
`tests/test_delivery_compose.py` 가 그 변경을 실패로 잡습니다.

노출이 꼭 필요하다면 조회 API에도 토큰을 걸어야 하며, 그때는 FE의 서버 사이드
호출(`getRecentJobs`, `getDatabaseStats`, `getFreshnessStats`,
`getVerificationStats`, `/schedules`)이 전부 토큰 없이 나가고 있으므로 함께
고쳐야 합니다. 이들은 실패 시 화면이 조용히 비는 방식이라 빠뜨리면 눈에 띄지
않습니다.

## 2026-08-18 수정 반영

이 런북이 작성된 뒤 납품을 막는 결함 2건이 수정되었습니다.

- **FE 이미지 빌드 실패** (`4897df6`): `/schedules` 에 `force-dynamic` 이 없어
  `next build` 가 실패했습니다. 이 런북의 빌드 절차는 그대로 유효하지만,
  `4897df6` 이전 커밋으로는 FE 이미지를 만들 수 없습니다.
- **예시 API 토큰** (`e502494`): `.env.example` 은 이제 `DELIVERY_API_TOKEN` 을
  빈 값으로 배포합니다. **`openssl rand -hex 32` 로 직접 생성해서 채우세요.**
  비워 두거나 옛 예시 문자열을 그대로 쓰면 BE가 시작을 거부합니다.

## 납품 경로 — 주 경로와 나중 경로 (2026-08-18 확정)

데이터는 소스 번들과 별도로 전달됩니다. **DB 덤프와 PDF blob은 소스 번들에
포함하지 않기로 결정되었습니다.** 두 경로를 분명히 구분하세요.

### 주 경로 (지금 바로) — 소스 번들만, DB는 비어 있음

지금 전달하는 패키지는 소스 코드만 담고 있고 DB는 빈 상태입니다. 위
`## 배포 전 필수 순서`와 `delivery/README.md` 3장에 있는 두 단계 부트스트랩만으로
바로 기동됩니다.

```bash
docker compose -f delivery/docker-compose.yml --profile bootstrap run --rm --build migrate && \
docker compose -f delivery/docker-compose.yml up -d
```

`migrate`는 `tools`/`bootstrap` 프로파일 뒤에 있어서 평소의 `docker compose ... build`
(프로파일 지정 없음)로는 다시 빌드되지 않습니다. `--build`가 없으면 예전에 캐시된
`migrate` 이미지가 옛 스키마 코드로 조용히 `PASS`를 찍을 수 있습니다 — 이번 리허설에서
실제로 겪은 문제입니다.

이것만으로도 완결된 시스템입니다 — 크롤러 804대가 등록되어 바로 수집을 시작할
수 있습니다. 실측: 기동 7.2초, 전 서비스 healthy 도달까지 17초.

### 나중 경로 — DB 덤프·blob이 별도로 도착했을 때

DB 덤프와 PDF blob은 이후 별도 납품으로 전달됩니다. 송신측(생성) 절차는
`docs/DELIVERY_PACKAGING.md`를 참고하세요. 수신측은 다음을 수행합니다.

- **DB 덤프**: 이미 기동 중인 Postgres 컨테이너에 restore합니다.
- **blob**: 검증된 절대경로를 `BLOB_HOST_PATH`에 설정하고, compose 실행에
  `-f delivery/docker-compose.blob.yml`을 추가합니다. 자세한 절차는
  `docs/DELIVERY_BLOB_MOUNT.md`를 참고하세요.

blob을 아직 연결하지 않은 상태에서 "PDF 열기" 링크는 **404를 반환합니다. 이것은
결함이 아니라 설계된 동작입니다.**

blob을 연결하기 전에 반드시 송신측과 수신측이 blob manifest를 대조해야 합니다.
대조가 끝나기 전에는 Worker가 blob에 쓰기 작업을 하도록 허용하지 마세요.

## 소스 번들 생성 방법과 주의할 함정 (2026-08-18 확정)

소스 번들은 `delivery/scripts/build_release_bundle.sh`로 생성합니다. 이 스크립트는
내부적으로 `git archive HEAD`를 사용하므로 **커밋되고 추적된 파일만** 번들에
들어갑니다 — `.env`, `.git`, 커밋 히스토리는 포함되지 않습니다. 실측 결과: 약
5.5MB, 크롤러 모듈 800개 포함.

**주의할 함정:** 이 저장소는 linked git worktree입니다 — `.git`이 디렉터리가
아니라 실제 gitdir을 가리키는 포인터 파일입니다. 이 디렉터리를 `tar`로 직접
압축하면 수신측에서 깨진 저장소가 만들어집니다. 반드시
`build_release_bundle.sh`를 사용하고, 디렉터리를 통째로 `tar`로 압축하는 방식으로
대체하지 마세요.

## 크롤 작업 시간 예산 — `LIBERTREE_MAX_WALL_S` (2026-08-18 확정)

Worker가 크롤 작업 하나에 쓸 수 있는 최대 경과 시간(초)입니다. 기본값은
**1500(25분)**이며, `delivery/docker-compose.yml`의 `worker` 서비스가
`${LIBERTREE_MAX_WALL_S:-1500}`으로 주입합니다.

이 예산에 근접한 시간을 쓰고 정상 종료한 수집은 콘솔 작업 목록에 「부분 수집」
배지로 표시됩니다 — 그 사이트를 끝까지 받지 못하고 시간이 다 되어 끊겼다는
뜻입니다. 크롤러 자체는 고쳐지지 않았습니다.

값을 올리려면 `delivery/.env`에 `LIBERTREE_MAX_WALL_S`를 설정하고
`docker compose -f delivery/docker-compose.yml up -d worker`로 적용하세요.

**주의:** Worker는 단일 직렬 처리기입니다. 이 값을 크게 잡으면 큰 사이트 하나가
큐 전체를 오래 독점합니다 — 그 뒤에 대기 중인 다른 사이트들이 그만큼 늦게
시작됩니다. 큰 사이트는 값을 전역으로 올리기보다 별도 워커나 별도 시간대에
돌리는 편이 낫습니다.
