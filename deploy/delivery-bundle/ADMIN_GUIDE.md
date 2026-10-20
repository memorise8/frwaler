# libertree-delivery 통합 배포 번들: 관리자 안내

이 디렉터리(`deploy/delivery-bundle/`) 하나로 FE·BE·DB·워커·수집 실행기를 **한 compose 스택**
(프로젝트 이름 `libertree-delivery`)으로 띄웁니다. 기존 통합 DB(`ui-sci-20261013` 스택의 postgres)를
덤프·복원해 옮기고, 기존 운영 blob 디렉터리는 복사하지 않고 그대로 마운트합니다.

## 0. 구성

| 서비스 | 이미지(빌드 원본) | 호스트 포트 → 컨테이너 | 비고 |
|---|---|---|---|
| `postgres` | `postgres:16` | 기본 미공개 (선택: `127.0.0.1:53232`→5432) | 볼륨 `libertree-delivery_pgdata` |
| `be` | `delivery/Dockerfile.be` | `127.0.0.1:${BE_PORT:-8080}` → 3001 | blob 읽기 전용 |
| `fe` | `delivery/Dockerfile.fe` | `${FE_BIND:-0.0.0.0}:${FE_PORT:-3000}` → 3002 | |
| `worker` | `delivery/Dockerfile.worker` | 없음 | blob 읽기·쓰기 |
| `file-worker` | `delivery/Dockerfile.worker` | 없음 | PDF 다운로드·텍스트 추출, blob 읽기·쓰기 |
| `runner` | `delivery/Dockerfile.runner` | **없음(공개 금지)** | be→`http://runner:8793`, docker 소켓 |
| `migrate` | `Dockerfile.be` 이미지 | 없음 | 1회성(`--profile tools`) |

- 컨테이너 포트 3001(BE), 3002(FE), 8793(runner)은 각 Dockerfile의 `EXPOSE`/`CMD`, 기존
  `delivery/docker-compose.yml`과 같습니다.
- 컨테이너 이름: `libertree-delivery-<서비스>-1`. 이미지: `${IMAGE_PREFIX:-libertree-delivery}-<be|fe|worker|runner>:${IMAGE_TAG:-latest}`.
- 번들 밖에 두는 것: **nginx-blob(8181)**, **cloudflare-ddns**. 이 번들은 두 컨테이너를 만들거나
  멈추지 않습니다. 기존 그대로 운영하고, nginx-blob이 보는 blob 경로가 `BLOB_HOST_PATH`와 같은지만
  확인합니다.
- 파일
  - `docker-compose.yml`: 본 스택
  - `docker-compose.pg-port.yml`: (선택) postgres 호스트 포트 공개
  - `docker-compose.collect-status.yml`: (선택) 사이트 상태 CSV 폴더를 be에 연결
  - `.env.example`: 모든 변수 이름(값은 비어 있음)
  - `migrate/`: 덤프·복원·정렬 규칙 점검·건수 비교 스크립트 (`migrate/README.md`)

모든 `docker compose` 명령은 **이 디렉터리에서** 실행합니다(`.env`를 여기서 읽음).

## 1. 사전 준비

- Docker Engine **20.10 이상**: be의 `extra_hosts: host.docker.internal:host-gateway`가 20.10부터
  지원됩니다. `docker version --format '{{.Server.Version}}'`
- Docker Compose v2 (**v2.22 이상 권장**, `docker compose version`). `--wait` 옵션을 씁니다.
- 디스크 (`docker info -f '{{.DockerRootDir}}'` 위치 기준)
  - 이미지 약 5.1GB (worker 2.4GB, be·fe·runner 각 약 0.9GB) + 빌드 캐시
  - 새 postgres 볼륨: 원본 DB 크기(`01_dump.sh`가 출력) 이상. 인덱스 재생성 여유로 1.5배 권장
  - 덤프 파일: 출력 경로에 원본 DB 크기의 약 0.3~0.5배(압축). `--jobs`(병렬)를 쓰면 원본
    컨테이너 쓰기 계층에도 같은 크기가 잠시 필요
  - blob은 복사하지 않으므로 추가 공간 없음
- 빌드 시 외부 접속: Docker Hub(base 이미지), npm·PyPI(패키지), Playwright Chromium 다운로드
- 호스트 디렉터리
  - `BLOB_HOST_PATH`: 기존 운영 blob 디렉터리(절대경로). 없으면 기동 실패(빈 폴더를 만들지 않음)
  - `COLLECT_RUNNER_HOME`, `COLLECT_RUNS_ROOT`: runner용. **호스트와 컨테이너에서 같은 경로**로
    마운트되며 `COLLECT_RUNNER_UID:COLLECT_RUNNER_GID` 소유여야 합니다. `DOCKER_GID`는
    `stat -c %g /var/run/docker.sock` 값입니다.

```bash
sudo mkdir -p /srv/collect-runner/units /srv/enum-pilot-run
sudo chown 1000:1000 /srv/collect-runner /srv/collect-runner/units /srv/enum-pilot-run   # COLLECT_RUNNER_UID:GID
stat -c %g /var/run/docker.sock                                                          # → DOCKER_GID
```

`be`도 runner 상태 디렉터리를 읽기 전용으로 붙여 토큰 파일을 읽으므로 `COLLECT_RUNNER_HOME`은
be보다 먼저 만들어 두어야 합니다.

## 2. .env 작성

```bash
cd deploy/delivery-bundle
cp .env.example .env && chmod 600 .env
openssl rand -hex 24   # → POSTGRES_PASSWORD
openssl rand -hex 32   # → DELIVERY_API_TOKEN
```

필수: `POSTGRES_PASSWORD`, `BLOB_HOST_PATH`, `DELIVERY_API_TOKEN`(32자 이상, BE가 짧거나 예시
문자열이면 기동 거부). runner를 쓰면 `COLLECT_RUNNER_UID`/`GID`, `DOCKER_GID`,
`COLLECT_RUNNER_HOME`, `COLLECT_RUNS_ROOT`. 나머지는 비워 두면 기본값입니다. 번역·검색 키·ScienceON은
현재 운영 `.env`의 값을 그대로 옮깁니다. **되돌리기를 위해 `IMAGE_TAG=20261020`처럼 날짜 태그를
권장합니다.**

postgres 포트를 현재처럼 53232로 유지해야 하면(외부 도구가 접속하는 경우만):

```bash
# .env
COMPOSE_FILE=docker-compose.yml:docker-compose.pg-port.yml
POSTGRES_HOST_PORT=53232
POSTGRES_BIND=127.0.0.1     # 0.0.0.0 금지 (7장)
```

`/site-collect`의 사이트 상태 CSV를 보이려면 `COMPOSE_FILE`에 `:docker-compose.collect-status.yml`을
덧붙이고 `COLLECT_STATUS_HOST_DIR`를 지정합니다. 설정하지 않으면 해당 칸은 "not_configured"로
나오고 나머지 화면은 정상입니다.

설정 확인: `docker compose config --quiet && echo OK`

## 3. 기동 순서

**postgres → 복원 → (migrate) → be/worker/file-worker → fe → runner**

```bash
cd deploy/delivery-bundle

# 3-1. 빌드 (기존 서비스에 영향 없음)
docker compose --profile tools build

# 3-2. 기존 스택 이름 확인 후 쓰기 주체만 멈춤 (postgres는 덤프를 위해 켜 둠, 삭제하지 않음)
docker ps --filter label=com.docker.compose.project=ui-sci-20261013 --format '{{.Names}}\t{{.Ports}}'
docker stop <기존-be> <기존-worker> <기존-file-worker> <기존-fe>    # 포트 3000/8080도 이때 비워짐
#   호스트에서 별도로 도는 collect-runner(~/collect-runner)가 있으면 그것도 멈춥니다(두 실행기가
#   같은 실행 폴더를 쓰지 않도록).

# 3-3. 새 postgres만 기동
docker compose up -d --wait postgres

# 3-4. 데이터 이전 (자세한 설명은 migrate/README.md)
SRC=ui-sci-20261013-postgres-1                 # 3-2에서 확인한 실제 이름
DST=libertree-delivery-postgres-1
DUMP=/srv/backup/libertree-$(date +%Y%m%d-%H%M).dump
migrate/01_dump.sh "$SRC" "$DUMP"              # 계획 출력 → yes 입력
migrate/02_restore.sh "$DST" "$DUMP"
migrate/03_postcheck.sh "$DST"
migrate/04_verify.sh "$SRC" "$DST" /srv/backup/verify-$(date +%Y%m%d-%H%M)   # "(차이 없음)" 확인

# 3-5. 스키마를 현재 코드 버전으로 (추가형, 기존 행 유지)
docker compose run --rm migrate                # 마지막 줄 "delivery schema migration: PASS"

# 3-6. BE·워커
docker compose up -d --wait be worker file-worker

# 3-7. FE
docker compose up -d --wait fe

# 3-8. runner (1장의 디렉터리 준비가 끝난 뒤)
#   기존 호스트 runner의 units/*.csv(site_id→unit)와 등록 정보를 옮기려면 그 폴더 내용을
#   COLLECT_RUNNER_HOME으로 복사하고 소유자를 COLLECT_RUNNER_UID로 맞춥니다.
docker compose up -d runner
#   최초 기동 때 COLLECT_RUNNER_HOME/config.json과 .token이 생깁니다. COLLECT_RUNS_ROOT를
#   /srv/enum-pilot-run 이 아닌 곳으로 바꿨다면 config.json의 runs_root, seed_holds,
#   reference_db를 고친 뒤 `docker compose restart runner`.
#   plan 등록: docker compose exec runner runnerctl register --src /opt/frwaler-src --site ... (deploy/collect-runner/README.md)

docker compose ps
```

기존 DB 없이 새로 시작하는 경우에는 3-2, 3-4를 건너뛰고 3-5의 `migrate`가 빈 스키마를 만듭니다.

## 4. 점검

```bash
TOKEN=$(grep '^DELIVERY_API_TOKEN=' .env | cut -d= -f2)
curl -s http://127.0.0.1:8080/health     # {"status":"ok"}    DB 연결
curl -s http://127.0.0.1:8080/ready      # {"status":"ready"} 필수 스키마 확인 (컨테이너 healthcheck도 이것)
curl -s -H "X-Delivery-Token: $TOKEN" http://127.0.0.1:8080/collect/runner/health
#   {"runner_ok":true,...}   runner 연결·토큰 확인
#   runner_unconfigured: .token 미생성(runner를 아직 안 띄움) / runner_unavailable: runner 미기동
curl -s -H "X-Delivery-Token: $TOKEN" http://127.0.0.1:8080/health/system   # 운영자용 상세
docker compose ps                         # be, fe, worker, file-worker 가 (healthy)
docker compose logs --since 10m worker file-worker | tail -50
```

BE 경로 근거: `delivery/be/app.py`의 `/health`, `/ready`, `/health/crawlers`, `/health/system`,
`delivery/be/collect.py`의 `/collect/runner/health`. 운영자 인증 헤더는 `X-Delivery-Token`
(`delivery/be/access.py`, 값은 `DELIVERY_API_TOKEN`)입니다.

FE 화면(`http://<서버>:3000`)에서 확인할 것:

- `/documents`: 문서 목록 건수가 이전과 같은지, 문서 하나를 열어 PDF·텍스트가 열리는지(blob 연결과
  seq_id 확인)
- `/crawlers`: 수집기 목록과 사이트별 건수
- `/site-collect` (사이트 수집): runner 상태가 정상으로 표시되는지, 사이트 목록·건수가 보이는지

## 5. 되돌리기

원칙: **이전 스택은 멈추기만 하고 지우지 않습니다**(`docker stop`만 사용, `down`/`rm`/`volume rm` 금지).
새 스택의 볼륨도 `docker compose down`은 볼륨을 남기지만 `down -v`는 지우므로 쓰지 않습니다.

```bash
# 새 스택 멈춤 (컨테이너·볼륨 유지)
cd deploy/delivery-bundle && docker compose stop

# 이전 스택 재기동
docker start $(docker ps -aq --filter label=com.docker.compose.project=ui-sci-20261013)
```

주의: 전환 후 새 스택에서 문서가 추가되었다면 그 행은 이전 DB에 없지만 blob 파일은 같은
디렉터리에 남습니다. 이전 DB의 seq_id 시퀀스가 같은 번호를 다시 발급하면 다른 문서가 그
파일에 연결됩니다. 이전 worker를 다시 켜기 전에 둘 중 하나를 하십시오.

- 새 DB를 이전 쪽으로 다시 옮긴다(같은 스크립트, 방향만 반대, `--force`).
- 또는 이전 DB의 시퀀스를 새 DB의 최대값 이상으로 올린다:
  `SELECT setval(pg_get_serial_sequence('documents','seq_id'), <새 DB의 max(seq_id)>);`

이전 스택과 새 스택의 worker/file-worker를 **동시에 켜지 마십시오**.

다시 새 스택으로 가려면 `docker compose start`. 이미지 롤백은 이전 `IMAGE_TAG`로 바꾸고
`docker compose up -d`.

## 6. 운영 명령

```bash
docker compose logs -f be                 # 로그
docker compose --profile tools run --rm --build migrate   # 코드 갱신 후 스키마
docker compose up -d --build be fe worker file-worker     # 코드 갱신 반영
docker compose stop / start                # 정지·재개(데이터 유지)
```

## 7. 보안

- **postgres(53232)를 외부에 공개하지 마십시오.** 현재 운영은 53232를 공개하고 있습니다. DB 비밀번호가
  유출되거나 약하면 모든 문서·작업 기록을 읽고 지울 수 있고, `libertree`는 superuser라서
  `COPY ... PROGRAM` 등으로 DB 컨테이너 안에서 명령 실행까지 가능합니다. 이 번들은 기본으로 포트를
  공개하지 않으며, 필요하면 `docker-compose.pg-port.yml`로 `127.0.0.1`에만 엽니다. 원격 접속은 SSH
  터널(`ssh -L 53232:127.0.0.1:53232 서버`)을 씁니다.
- **BE·runner API 포트를 공개하지 마십시오.** 현재 운영은 8792를 공개하고 있습니다. BE는
  `127.0.0.1:8080`에만 바인드되고, runner는 호스트 포트가 없습니다. 운영 API가 열려 있으면 토큰을 대입
  공격당하거나 수집 실행·취소를 외부에서 호출할 수 있습니다. 8792를 쓰던 서비스가 이 스택으로
  넘어오면 그 포트는 닫습니다.
- 방화벽: 외부에는 FE(3000, 가능하면 TLS를 종단하는 리버스 프록시 뒤)와 nginx-blob(8181, 필요한
  경우)만 허용합니다. 주의: Docker가 공개한 포트는 `ufw` 규칙을 우회합니다(DOCKER 체인). 포트 제한은
  **바인드 주소(127.0.0.1)로 하는 것이 확실하며**, 방화벽으로 막으려면 `DOCKER-USER` 체인에 규칙을
  넣습니다. 확인: `ss -ltnp | grep -E ':(53232|8792|8793|8080)'`, 외부 호스트에서 `nc -zv 서버 53232`.
- FE를 리버스 프록시 뒤에만 둘 경우 `FE_BIND=127.0.0.1`로 바꿉니다.
- **runner의 docker 소켓 = 호스트 root 권한.** 소켓에 접근할 수 있으면 `/`를 마운트한 특권
  컨테이너를 띄울 수 있습니다. runner는 비root(uid `COLLECT_RUNNER_UID`)로 돌지만 docker 그룹에
  들어 있으므로 사실상 root와 같습니다. 그래서 등록된 plan만 실행하고, `X-Runner-Token`을 요구하며,
  호스트 포트를 열지 않습니다. `ports:`를 추가하지 마십시오. runner 상태 폴더(`.token`)는 0600이며
  runner uid만 읽습니다(be는 컨테이너 root로 읽기 전용 마운트).
- **토큰·비밀번호는 `.env`에만** 둡니다(`chmod 600 .env`, 커밋 금지). 운영자 인증은
  `DELIVERY_API_TOKEN`(헤더 `X-Delivery-Token`)이며 `DELIVERY_AUTH_MODE=disabled`는 운영에서 쓰지
  않습니다. runner 토큰은 runner가 생성한 `.token` 파일을 be가 읽으므로 `.env`에 넣지 않아도 됩니다.
  토큰 교체: `.env` 수정 후 `docker compose up -d be fe`.

## 8. 현재 구성과의 차이

`delivery/docker-compose.yml`(+ `docker-compose.blob.yml`, `docker-compose.collect.yml`) 대비:

| 항목 | 기존 | 번들 |
|---|---|---|
| FE 바인드 | `127.0.0.1:3000` | `0.0.0.0:3000` (현재 운영 서버와 같게, `FE_BIND`로 변경 가능) |
| BE 바인드 | `127.0.0.1:8080`→3001 | 같음 |
| blob | 기본 named volume, blob.yml 사용 시 bind | **항상 `BLOB_HOST_PATH` bind**(필수). be 읽기 전용, 경로가 없으면 기동 실패 |
| runner | `--profile runner` | 기본 스택에 포함(포트 없음) |
| be의 runner 토큰 | collect.yml: `.token` 파일 단독 bind(runner를 먼저 띄워야 함) | runner 상태 볼륨을 읽기 전용으로 붙임 → be를 runner보다 먼저 띄워도 됨 |
| site-status CSV | collect.yml에서 필수 | 선택 오버라이드(`docker-compose.collect-status.yml`) |
| migrate | `tools`/`bootstrap` 프로파일 | `tools`만(복원 후 실행) |
| postgres 호스트 포트 | 없음 | 기본 없음, 선택 오버라이드 |
| 이미지 이름 | compose 기본(`libertree-delivery-<서비스>`) | `IMAGE_PREFIX`/`IMAGE_TAG`로 지정, worker·file-worker가 한 이미지 공유 |
| health 서비스 | docker-compose.health.yml | 포함하지 않음(필요 시 기존 파일 사용) |

현재 운영 서버(`ui-sci-20261013` 스택)와 비교: 프로젝트·컨테이너 이름이 `libertree-delivery-*`로
바뀌고, postgres 53232와 8792는 기본으로 공개하지 않습니다. 운영 서버에 이미 `libertree-delivery`라는
compose 프로젝트가 있으면(`docker compose ls`) 같은 볼륨 이름(`libertree-delivery_pgdata`)을 쓰게
되므로, 먼저 확인하고 겹치면 `.env`에 `COMPOSE_PROJECT_NAME`을 다른 이름으로 지정하십시오
(`02_restore.sh`는 documents가 있는 DB를 덮어쓰지 않습니다).

## 9. 문제 해결

- `required variable BLOB_HOST_PATH is missing a value`: `.env`에 `BLOB_HOST_PATH` 지정
- `POSTGRES_PASSWORD is not set`: guard 컨테이너 메시지. `.env`에 비밀번호 지정
- be가 계속 재시작하고 `/ready`가 503: 스키마 부족 → `docker compose run --rm migrate`
- runner 기동 실패 `no such file or directory`: `COLLECT_RUNNER_HOME` 디렉터리 없음 → 1장
- runner 로그 `Permission denied`: 디렉터리 소유자가 `COLLECT_RUNNER_UID`가 아님
- `/collect/runner/health`가 `docker_unavailable` 등으로 실행 거절: `DOCKER_GID`가 소켓 그룹과 다름
- 포트 충돌 `address already in use`: 이전 FE/BE가 아직 떠 있음(3-2) 또는 `.env`의 포트 변경
