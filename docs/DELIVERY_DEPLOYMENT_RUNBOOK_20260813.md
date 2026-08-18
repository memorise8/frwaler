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

이 플래그는 업그레이드 이후에 실행된 수집에만 적용됩니다 — 그 이전에 끝난 작업은
잘렸더라도 모두 truncated=false 로 남아 있습니다.

## 백필 운영 — 대형 사이트 이어받기 (2026-08-18 확정)

재개(resume) 계약을 갖춘 대형 사이트(예: doaj-org-search, 약 1,337만 건)를
`LIBERTREE_MAX_WALL_S` 예산 안에서 여러 번의 작업으로 나눠, 매번 1페이지부터
다시 걷지 않고 저장된 커서에서 이어받는 모드입니다. 25개 대형 사이트 크롤러가
이 계약을 구현합니다.

### 시작

```bash
curl -X POST "http://127.0.0.1:${BE_PORT:-8080}/jobs" \
  -H "Authorization: Bearer $DELIVERY_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"site_id": "doaj-org-search", "mode": "backfill"}'
```

운영자 토큰이 필요합니다(쓰기 10개 중 하나). 그 site_id에 이미 활성 작업이 있으면
409를 반환합니다.

### 자동 재큐잉과 멈추는 조건

작업 하나가 정상 반환하면(예산에 잘렸든, 안 잘렸든) 워커가 커서 전진 여부와
항목 목격 여부(크롤러가 보고한 `items_done` 또는 실제 저장된 문서 수)를
확인합니다. 둘 다 있어야만(그리고 아래 "완주" 신호가 없어야만) 같은
site_id·`mode=backfill` 작업을 큐 맨 뒤에 자동으로 다시 넣습니다
(`requested_by="auto-backfill"`) — 운영자가 반복해서 `POST /jobs`를 부를
필요가 없습니다. 전진이 재큐잉의 유일한 면허이며, 이 체인은 다음 세 조건 중
하나로만 멈춥니다.

> **2026-08-18 보정:** 완주 판정은 더 이상 시간·예산과 무관합니다. 이전에는
> "예산 안에 끝났다(잘리지 않았다)"는 것 자체를 완주 신호로 썼는데, doaj 처럼
> 목록이 1,337만 건인 사이트는 한 조각(약 200페이지, ~16분)이 25분 예산의
> truncation 임계 아래라 "안 잘렸다"로 보였고, 그 첫 조각에서 곧바로 완주
> 처리되어 만 건도 못 걸은 채 체인이 죽었습니다. 지금은 크롤러가 목록의
> 자연스러운 끝(빈 결과 페이지, `has_next=false`, `totalPages` 도달 등)에
> 닿았을 때만 명시적으로 `self._mark_exhausted()`를 부르고, 워커는 오직 이
> 신호(`delivery_exhausted`)만 완주 판정에 씁니다. 25개 대형 사이트 크롤러
> 전부가 이 신호를 자연 종료 지점마다 보고합니다.

- **완주**: 크롤러가 `_mark_exhausted()`로 "목록 끝까지 걸었다"고 명시했을
  때만 `crawl_site_progress.completed_at`을 기록하고 체인을 끝냅니다. 잘렸는지
  안 잘렸는지는 이 판정과 무관합니다. 크롤러가 신규분 소진(`up-to-date`)을
  보고해도 동일하게 완주로 처리합니다. 완주해도 `cursor` 자체는 지우지 않습니다
  — 이후 증분 수집(oldest_first)이나 뒤늦은 재개가 그 커서를 다시 전진시키면
  `completed_at`은 자동으로 지워집니다(그 사이트가 아직 끝나지 않았다는 뜻이므로).
- **무전진**: exhausted 신호 없이 커서가 전진하지 않았거나 항목을 하나도
  목격하지 못했으면(잘렸든 안 잘렸든) "stalled"로 기록하고 재큐잉을 멈춥니다.
  운영자가 원인을 살피고 다시 `POST /jobs`해야 이어집니다.
- **취소**: `POST /jobs/{job_id}/cancel`로 취소를 요청하면(`cancel_requested_at`)
  그때까지 걸은 커서는 저장하지만, 다음 작업은 자동으로 큐에 넣지 않습니다.
  크롤러가 취소 확인 지점에서 멈추는 경우뿐 아니라, 취소가 마지막 안전 경계
  이후에 도착해 크롤이 정상 반환과 동시에 취소 확정되는 경우에도 동일하게
  체인이 끊깁니다 — 두 경로 모두에서 완주·재큐잉은 절대 일어나지 않습니다.
  이어가려면 운영자가 다시 `POST /jobs`합니다.

### 진행 확인 — `/backfill` 화면

FE 내비게이션의 "백필" 메뉴(`/backfill`)가 `GET /progress`를 그대로 렌더합니다.
조회 API라 토큰이 필요 없습니다. 사이트별로 완주/진행 중/시작 전 배지와
`items_done / total_estimate` 기준 진행률(%)을 보여주며, `total_estimate`가
비어 있으면(시딩 전) 퍼센트를 표시하지 않습니다.

### 시드 스크립트 — `total_estimate` 채우기

`total_estimate`는 `delivery/scripts/seed_backfill_estimates.py`로 채웁니다.
`scripts/audit/capacity_corrected.csv`의 `corrected_max`가 10만 건 이상인
사이트만 업서트하며(현재 25개), 재실행해도 무해합니다(업서트) — 단 이미 진행
중인 행의 `cursor`·`items_done`·`completed_at`은 절대 건드리지 않고
`total_estimate`만 갱신합니다.

```bash
LIBERTREE_PG_DSN='postgresql://libertree:<password>@<host>:5432/libertree' \
  python3 delivery/scripts/seed_backfill_estimates.py
```

### `LIBERTREE_MAX_WALL_S`와의 관계 — 백필의 조각 크기

위 「크롤 작업 시간 예산」 절의 `LIBERTREE_MAX_WALL_S`가 백필의 조각 크기를
그대로 정합니다 — 별도의 조각 크기 파라미터는 없습니다. 값을 키우면 한 작업이
더 많이 걷어 자동 재큐잉 횟수가 줄지만, 워커가 단일 직렬 처리기라 그만큼 다른
사이트의 시작이 늦어집니다. 각 크롤러의 페이지 캡도 이제 이번 실행에서 걸을
페이지 수(커서로 재개한 지점 기준 상대값)이지 사이트 전체에 대한 절대 상한이
아닙니다 — 재개한 작업이 큰 페이지 번호에서 시작해도 매번 정해진 만큼은
반드시 걷습니다.

`LIBERTREE_MAX_WALL_S`가 조각 크기를 정한다는 이 문장은 이제 25개 대형 사이트
크롤러 전부에 대해 사실입니다(2026-08-18 보정) — `etera-ee-browse`와
`repositorio-uchile-cl-discover`는 원래 무한 `while True` 루프에 시간 가드가
없어, 백필 작업 하나가 단일 직렬 워커를 몇 시간이고 붙잡을 수 있었습니다. 두
파일 모두 나머지 23개가 이미 쓰던 것과 같은 `LIBERTREE_MAX_WALL_S` 가드를
받았습니다.
