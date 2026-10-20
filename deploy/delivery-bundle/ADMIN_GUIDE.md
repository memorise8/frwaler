# libertree-delivery 관리자 인계 안내

이 문서는 두 가지를 다룹니다.

- **A. 현재 운영 구성(2026-10-20 교체)**: 운영 서버에서 지금 실제로 돌고 있는 구성과 운영·되돌리기
  명령입니다. 운영 서버는 이 번들의 v2 프로젝트가 아니라, 기존 compose 프로젝트에 override 파일
  하나를 더하는 더 단순한 방식으로 교체했습니다.
- **B. 번들 경로(0~10장)**: 새 서버나 다른 서버에 같은 방식(기존 DB를 그대로 두고 앱 이미지만 교체)을
  적용할 때 쓰는 `deploy/delivery-bundle` 절차입니다.

## A. 현재 운영 구성(2026-10-20 교체)

사용자 승인 하에 2026-10-20에 교체했습니다. 아래 경로·이름은 운영 서버 기준입니다.

### A-1. compose 파일: 항상 세 개를 함께 씁니다

- 프로젝트: 기존 그대로 `libertree-delivery`
- 디렉터리: `/mnt/apps/apps/docker/libertree-delivery/delivery/`
- 파일:
  - `docker-compose.yml`, `docker-compose.blob.yml`: **원본 그대로**
  - `docker-compose.swap-20261020.yml`: **이번에 추가한 override**
- **모든 명령에 `-f` 세 개를 붙입니다.** 두 개(원본+blob)만 쓰면 서비스가 `:latest`, 즉 **교체 전
  이미지**로 돌아갑니다. 이것이 곧 되돌리기 방법이기도 하므로 실수로 두 개만 쓰지 않도록 주의합니다.

```bash
cd /mnt/apps/apps/docker/libertree-delivery/delivery
DC="docker compose -f docker-compose.yml -f docker-compose.blob.yml -f docker-compose.swap-20261020.yml"
$DC ps
$DC logs --since 10m be worker file-worker
$DC up -d --no-deps be          # 서비스 하나만 다시 띄울 때. --no-deps 로 postgres 를 건드리지 않음
```

`--remove-orphans`와 `down`은 쓰지 않습니다. 원하면 `delivery/.env`에
`COMPOSE_FILE=docker-compose.yml:docker-compose.blob.yml:docker-compose.swap-20261020.yml`를 넣어 세
파일을 기본값으로 둘 수 있습니다. 이때도 `-f`를 직접 주면 `COMPOSE_FILE`보다 우선하므로 되돌리기
명령은 그대로 동작합니다.

### A-2. 이미지와 서비스

| 서비스 | 이미지 | 비고 |
|---|---|---|
| be | `libertree-delivery-be:20261020-865e5612` | 유닉스 소켓으로 runner 접속 |
| fe | `libertree-delivery-fe:20261020-4e75c48b` | `0.0.0.0:3000` 공개(A-6) |
| worker | `libertree-delivery-worker:20261020-4e75c48b` | |
| file-worker | worker 이미지(`:20261020-4e75c48b`) | **새 서비스**. `python -m delivery.files.worker`, blob bind |
| postgres | 변경 없음 | 컨테이너를 다시 만들지 않음(A-4) |

교체 전 이미지는 `:prev` 태그로 남아 있고, `:latest`도 여전히 교체 전 이미지를 가리킵니다. 교체
전 이미지를 지우거나 `:latest`를 다시 태그하지 마십시오. 되돌리기에 필요합니다.

### A-3. 수집 실행기(runner): 호스트 프로세스 + 유닉스 소켓

- runner는 컨테이너가 아니라 서버 사용자 홈의 **프로세스** `~/collect-runner`입니다.
- 호스트 방화벽이 컨테이너→호스트 TCP를 막기 때문에, be는 TCP 대신 **유닉스 소켓**으로 접속합니다.
  - be는 `~/collect-runner/sock`을 `/run/collect-runner`에 읽기 전용으로 마운트하고
    `COLLECT_RUNNER_SOCKET=/run/collect-runner/runner.sock`을 씁니다.
  - 토큰은 `COLLECT_RUNNER_TOKEN`(`delivery/.env`)에 있습니다.
- **runner를 재시작할 때는 반드시 `RUNNER_SOCKET`을 함께 줍니다.** 빠뜨리면 소켓이 열리지 않아 be가
  `runner_unavailable`을 냅니다.

```bash
RUNNER_BIND=127.0.0.1 RUNNER_SOCKET=$HOME/collect-runner/sock/runner.sock ~/collect-runner/runnerctl ensure
# 확인 (서버에서)
TOKEN=$(grep '^DELIVERY_API_TOKEN=' /mnt/apps/apps/docker/libertree-delivery/delivery/.env | cut -d= -f2)
curl -s -H "X-Delivery-Token: $TOKEN" http://127.0.0.1:8080/collect/runner/health   # {"runner_ok":true,...}
ls -l ~/collect-runner/sock/runner.sock                                              # srw-rw----
```

- 소켓 경로는 리눅스 제한상 107바이트 이하여야 합니다. 긴 경로에서는 `AF_UNIX path too long`으로
  runner가 뜨지 않습니다.
- runner 자체는 자동 재시작이 없습니다. 서버를 재부팅한 뒤에는 위 명령을 다시 실행합니다.

### A-4. DB: 컨테이너는 그대로, 오늘 바뀐 데이터

운영 postgres 컨테이너(`libertree-delivery-postgres-1`, 볼륨, 53232)는 다시 만들지 않았습니다.
오늘 바뀐 데이터는 다음과 같습니다.

- `sites` 중복 행 81개 삭제. 지운 행은 백업 CSV로 남겼습니다.
- 텍스트 btree 인덱스를 `REINDEX CONCURRENTLY`했습니다.
- `documents` 날짜 열 6개를 추가했습니다(추가형).
- ui-sci-20261013 문서를 운영 DB에 합쳤습니다.
  - 새 seq_id는 운영 최대값 다음부터 붙였습니다. 운영 문서의 seq_id와 blob 파일 연결은 바뀌지 않습니다.
  - 원래 번호(`src_seq_id`)는 서버의 `/mnt/main/public/wise/merge-20261020/manifest-prod-*.jsonl`에 있습니다.
- 추가형 migrate: 새 테이블 7개, `crawl_jobs` 열 3개, `sites` 행 1개(`delivery.be.migrate`, B-4장).
- `datcollversion`이 NULL입니다(정렬 규칙 버전을 기록하지 않는 DB). 따라서 `ALTER DATABASE ...
  REFRESH COLLATION VERSION`은 해당 없습니다. glibc가 다른 postgres 이미지로 바꿀 때는 텍스트 인덱스
  `REINDEX`가 필요합니다(`migrate/README.md`).

### A-5. 되돌리기 (교체 전 이미지로)

백업은 서버의 `/mnt/main/public/wise/merge-20261020/swap-backup`에 있습니다.

```bash
cd /mnt/apps/apps/docker/libertree-delivery/delivery
# 1) 교체 때 새로 생긴 file-worker 를 멈춤(삭제하지 않음)
docker compose -f docker-compose.yml -f docker-compose.blob.yml -f docker-compose.swap-20261020.yml stop file-worker
# 2) 두 파일만으로 be/fe/worker 를 :latest(=교체 전 이미지)로 다시 띄움. postgres 는 --no-deps 로 제외
docker compose -f docker-compose.yml -f docker-compose.blob.yml up -d --no-deps be fe worker
docker compose -f docker-compose.yml -f docker-compose.blob.yml ps
```

- `--remove-orphans`를 붙이지 않습니다. 붙이면 file-worker 같은 override 전용 서비스가 지워집니다.
- 데이터 변경(A-4)은 이미지 되돌리기로 돌아가지 않습니다. 추가된 열·테이블과 합친 문서는 그대로
  남습니다. 데이터까지 되돌리려면 swap-backup의 덤프로 복원해야 하며, 이는 별도 판단이 필요합니다.
- 다시 교체하려면 세 파일로 `up -d --no-deps be fe worker file-worker`를 실행합니다.

### A-6. 남은 일과 보안 권고

- **사이트 상태 폴더**: `COLLECT_STATUS_DIR`을 서버에 설정하지 않았습니다. `/site-collect`의 상태
  칸은 "not_configured"로 나옵니다. 상태 CSV 폴더를 정하면 be에 읽기 전용으로 마운트하고 설정합니다.
- **ScienceON**: scienceon-api 문서를 운영 DB에 합치는 작업이 남았습니다. ui-sci postgres가 대량
  insert 중에 죽는 원인을 아직 모릅니다. 원인을 찾은 뒤 나눠서 진행합니다.
- **FE `0.0.0.0:3000` 공개, 로그인 없음**: FE가 서버 쪽에서 운영자 토큰을 붙여 BE를 부르므로, 3000에
  닿는 사람은 누구나 운영자 권한을 가집니다(7장). 권고: 인증하는 리버스 프록시 뒤로 옮기고 FE를
  `127.0.0.1`로 바인드하거나, 최소한 `DOCKER-USER`에 IP 허용 목록 규칙을 둡니다.
- **postgres 53232 공개**: superuser 계정이 비밀번호 하나로만 보호됩니다(7장). 권고: 당장은
  `iptables -I DOCKER-USER -i <외부NIC> -p tcp --dport 5432 -j DROP`으로 막고, 다음 정비 때 백업 후
  `127.0.0.1:53232`로 다시 바인드합니다(postgres 컨테이너 재생성이 필요).
- 8792를 공개하는 프로세스가 있으면 확인해 닫습니다(7장).

---

# B. 번들 경로: 새 서버나 다른 서버에 적용할 때

**같은 DB를 그대로 쓰고 앱 이미지만 바꿉니다.** 대상 서버에서 운영 중인 compose 프로젝트(기본
`libertree-delivery`)의 postgres(컨테이너·데이터 볼륨·호스트 포트)와 blob 디렉터리는 그대로 둡니다. 이 브랜치에서
빌드한 be·fe·worker·file-worker·runner를 별도 프로젝트 `libertree-delivery-v2`로 띄워 기존 postgres에 붙입니다.
문서를 다른 DB에서 합치는 작업은 번들과 별개입니다. 합친 뒤에는 `migrate/02_blobcheck.sh`를 다시
돌려 확인합니다(4장).

## 0. 구성

| v2 서비스 | 이미지(빌드 원본) | 호스트 포트 → 컨테이너 | DB·파일 |
|---|---|---|---|
| `be` | `delivery/Dockerfile.be` | `127.0.0.1:${BE_PORT:-8080}` → 3001 | 운영 postgres, blob 읽기 전용 |
| `fe` | `delivery/Dockerfile.fe` | `${FE_BIND:-127.0.0.1}:${FE_PORT:-3000}` → 3002 | (be 경유) |
| `worker` | `delivery/Dockerfile.worker` | 없음 | 운영 postgres, blob 읽기·쓰기 |
| `file-worker` | `delivery/Dockerfile.worker` | 없음 | 운영 postgres, blob 읽기·쓰기 |
| `runner` | `delivery/Dockerfile.runner` | **없음(공개 금지)** | docker 소켓, be→`http://runner:8793` |
| `migrate` | be 이미지 | 없음 | 1회성 스키마 변경(`--profile tools`) |

- 컨테이너 포트 3001·3002·8793은 각 Dockerfile과 기존 `delivery/docker-compose.yml`과 같습니다.
  호스트 포트 8080·3000은 운영과 같으므로, 컷오버 때 운영 be·fe를 멈춘 뒤에 v2가 그 포트를 씁니다.
- 이름: 컨테이너는 `libertree-delivery-v2-<서비스>-1`, 이미지는
  `libertree-delivery-v2-<be|fe|worker|runner>:${IMAGE_TAG}`입니다(`IMAGE_TAG` 필수). 운영 이미지는 덮어쓰지 않으므로
  되돌리기에 그대로 쓰입니다.
- **기존 postgres에 붙는 방식**: 운영 프로젝트 네트워크 `libertree-delivery_default`를 `external`로
  참조하고 컨테이너 이름 `libertree-delivery-postgres-1`로 접속합니다(`PROD_DB_NETWORK`, `PROD_DB_HOST`).
  v2 compose 파일에는 postgres 서비스도 데이터 볼륨도 없으므로 v2의 어떤 compose 명령도 운영
  postgres를 만들거나, 다시 만들거나, 지울 수 없습니다.
  - 같은 프로젝트 이름에 external 볼륨을 쓰는 방식은 택하지 않았습니다. 같은 이름의 be/fe 서비스가
    기존 컨테이너를 재생성·삭제해 되돌리기가 불가능해지고, `--remove-orphans`가 postgres를 지울 수
    있기 때문입니다.
  - 호스트 포트 53232로 붙는 방식도 택하지 않았습니다. 53232는 닫아야 할 포트이기 때문입니다(7장).
- 번들 밖: 운영 postgres, nginx-blob(8181), cloudflare-ddns. 이 번들은 셋 다 건드리지 않습니다.
- 파일
  - `docker-compose.yml`: 본 파일
  - `docker-compose.collect-status.yml`: (선택) 사이트 상태 CSV 폴더 연결
  - `.env.example`: 변수 이름 목록
  - `migrate/`: 스크립트, 상세는 `migrate/README.md`

모든 명령은 **`deploy/delivery-bundle` 디렉터리에서** 실행합니다.

## 1. 사전 준비

- Docker Engine **20.10 이상**(be의 `host.docker.internal:host-gateway`). Docker Compose v2(v2.22 이상 권장).
- 디스크:
  - 새 이미지 약 5.1GB(worker 2.4GB, be·fe·runner 각 약 0.9GB)와 빌드 캐시
  - 백업 덤프: 운영 DB 크기의 약 0.3~0.5배
  - 운영 이미지는 그대로 남으므로 그만큼 공간이 계속 필요합니다
- 빌드 때 외부 접속: Docker Hub, npm, PyPI, Playwright Chromium.
- runner 디렉터리(**필수**). 운영 runner와 **다른 폴더**를 씁니다. 되돌릴 때 운영 runner 상태를 보존하기 위해서입니다.
  `.token`은 be가 파일 하나만 bind하므로 **미리** 만들어 둡니다. runner는 이미 있는 `.token`을 그대로 씁니다.

```bash
sudo mkdir -p /srv/collect-runner-v2/units /srv/enum-pilot-run /srv/backup
sudo chmod 700 /srv/backup
openssl rand -hex 24 | sudo tee /srv/collect-runner-v2/.token >/dev/null
sudo chmod 600 /srv/collect-runner-v2/.token
sudo chown -R 1000:1000 /srv/collect-runner-v2 /srv/enum-pilot-run     # COLLECT_RUNNER_UID:GID
stat -c %g /var/run/docker.sock                                          # → DOCKER_GID
```

운영 runner 폴더의 `units/*.csv`와 등록 정보를 이어 쓰려면 2-2에서 만든 tar를 새 폴더에 풀고
소유자를 맞춥니다. `.token`은 새 것을 그대로 둡니다.

### 1-1. (선택) runner를 호스트 프로세스 + 유닉스 소켓으로 둘 때

호스트 방화벽이 컨테이너→호스트 TCP를 막는 서버(운영 서버가 그렇습니다, A-3)에서는 compose 안의
runner 대신 호스트의 `~/collect-runner`를 쓰고, be가 소켓으로 붙게 할 수 있습니다.

```bash
# .env
COMPOSE_FILE=docker-compose.yml:docker-compose.runner-socket.yml
COLLECT_RUNNER_HOME=/home/<사용자>/collect-runner        # be 가 이 폴더의 .token 을 읽음
COLLECT_RUNNER_HOST_SOCKET_DIR=/home/<사용자>/collect-runner/sock
# 호스트 runner (재시작할 때도 RUNNER_SOCKET 필수, 경로는 107바이트 이하)
RUNNER_BIND=127.0.0.1 RUNNER_SOCKET=$HOME/collect-runner/sock/runner.sock ~/collect-runner/runnerctl ensure
```

- 이 오버라이드는 be에 `COLLECT_RUNNER_SOCKET=/run/collect-runner-sock/runner.sock`을 넣고 소켓
  폴더를 읽기 전용으로 붙입니다.
- compose의 `runner` 서비스는 `in-compose-runner` 프로파일로 빠져 기동되지 않습니다.
- `00_preflight.sh`는 소켓 폴더와 `.token`을 확인합니다. `03_cutover.sh`는 runner 기동을 건너뜁니다.
- 위 1장의 `/srv/collect-runner-v2`, `DOCKER_GID` 준비는 필요 없습니다.

## 2. .env 작성과 사전 점검

```bash
cd deploy/delivery-bundle
cp .env.example .env && chmod 600 .env
```

- **운영 `.env`에서 그대로 가져올 값**: `POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`(운영 DB 계정),
  `DELIVERY_API_TOKEN`, 번역·검색 키·ScienceON 값.
  - 비밀번호는 DSN URL에 그대로 들어가므로 16진수처럼 URL에 안전한 문자여야 합니다. 특수문자가
    있으면 접속 오류가 납니다.
- **새로 정할 값**:
  - `IMAGE_TAG=20261020`(필수)
  - `BLOB_HOST_PATH=/mnt/main/public/wise/WISE/blob`
  - `COLLECT_RUNNER_*`, `DOCKER_GID`
- `PROD_DB_HOST`·`PROD_DB_NETWORK`는 운영 이름이 기본값과 같으면 비워 둡니다.
  확인: `docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' libertree-delivery-postgres-1`
- `FE_BIND`: 기본값은 `127.0.0.1`입니다. **현재 운영 FE는 3000을 모든 인터페이스에 공개하고 있으므로**,
  기본값으로 바꾸면 서버 밖에서 `http://서버:3000`으로 들어오던 접속이 끊깁니다. 7장을 읽고 의식적으로
  정하십시오.

### 2-1. 사전 점검 (필수, 읽기 전용)

```bash
migrate/00_preflight.sh
```

운영 컨테이너(postgres·be·fe·worker·file-worker, runner가 있으면 runner)의 ID·이름·상태·이미지
참조·이미지 ID를 `migrate/.state/production-libertree-delivery-v2.tsv`에 기록합니다. 다음 중 하나라도
해당하면 중단합니다(전체 목록은 `migrate/README.md`):

- 운영 컨테이너를 못 찾음
- v2 이름의 컨테이너·볼륨·네트워크가 이미 있음
- 새 이미지 이름이 운영 이미지와 같음
- blob 경로가 운영 be와 다름
- runner 디렉터리나 `.token`이 준비되지 않음

8080·3000·53232를 지금 쓰는 주체도 함께 출력합니다.

### 2-2. 빌드와 백업 (운영 영향 없음)

```bash
docker compose --profile tools build

TS=$(date +%Y%m%d-%H%M)
PG=$(awk -F '\t' '$1=="postgres"{print $3}' migrate/.state/production-libertree-delivery-v2.tsv)
migrate/01_dump.sh "$PG" /srv/backup/prod-$TS.dump              # 계획 확인 후 yes. .meta 에 sha256
sudo tar -C <운영 runner 폴더> -czf /srv/backup/runner-home-$TS.tgz .  # 운영 runner 가 있을 때
sudo find /mnt/main/public/wise/WISE/blob -type f -printf '%P\t%s\t%T@\n' \
  | LC_ALL=C sort > /srv/backup/blob-manifest-$TS.tsv              # blob 목록(복사 아님)
migrate/02_blobcheck.sh                                            # 미리 한 번(읽기 전용)
```

운영 runner 폴더는 `docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' <운영 runner> | grep RUNNER_HOME`로
확인합니다. 컨테이너 없이 호스트에서 도는 runner라면 `~/collect-runner`입니다.

## 3. 컷오버 (작업 시간대에)

```bash
migrate/03_cutover.sh
```

계획을 출력한 뒤 `yes`를 입력하면 다음 순서로 진행합니다.

1. 운영 postgres가 기록과 **같은 컨테이너 ID**로 실행 중인지 확인합니다.
2. **blob 게이트**(`02_blobcheck.sh`)를 돌립니다. 실패하면 아무것도 멈추지 않고 끝납니다.
3. 기록된 운영 be·fe·worker·file-worker·runner 중 **지금 실행 중인 것만** `docker stop`합니다.
   멈추기 전에 그 ID를 `migrate/.state/cutover-libertree-delivery-v2.stopped`에 적습니다.
   운영 postgres는 멈추지 않으며, `rm`/`down`은 하지 않습니다.
4. `docker compose run --rm migrate`로 스키마를 갱신합니다(4장).
5. v2를 be → worker·file-worker → fe → runner 순서로 띄우고 각각 healthy가 될 때까지 기다립니다.
6. 운영 postgres 컨테이너 ID가 그대로인지 다시 확인합니다.

- 계획 화면에 "실행 중 수집 작업 N 건"이 나옵니다. 0이 아니면 그 작업은 중단됩니다(worker는 최대
  60초 기다린 뒤 강제 종료). 가능하면 0일 때 진행합니다.
- 53232 클라이언트: postgres가 그대로이므로 53232로 접속하던 도구는 **같은 DB에 계속 접속됩니다**.
  주소를 바꿀 필요는 없습니다. 다만 그 도구가 쓰기를 한다면 컷오버 동안 멈추십시오.
- 같은 네트워크 주의: v2 be·worker는 운영 네트워크에도 붙어 `be`라는 이름을 함께 씁니다. 운영
  fe·be와 v2를 **동시에 실행하지 마십시오**. 스크립트는 항상 한쪽을 멈춘 뒤 다른 쪽을 켭니다.

## 4. 스키마 migrate (`delivery.be.migrate`)

코드를 확인한 결과(`crawler/db_pg.py`, `delivery/db/schema.py`, `delivery/db/seed.py`,
`crawler/document_dates.py`) **추가형이며 여러 번 실행해도 같습니다**.

- `CREATE TABLE/INDEX IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`, `CREATE EXTENSION IF NOT EXISTS pg_trgm`
- CHECK 제약은 `NOT VALID`로 추가하므로 기존 행을 검사하지 않습니다. `crawl_jobs_mode_check`는
  지운 뒤 같은 정의로 다시 붙입니다.
- `sites` 카탈로그는 `INSERT ... ON CONFLICT DO NOTHING`으로 넣습니다. 기존 행은 바뀌지 않습니다.
- 기존 행을 `UPDATE`/`DELETE`하지 않습니다.

그래도 **스키마를 바꾸므로 반드시 2-2의 백업을 먼저 합니다**. 운영 DB에 아직 없는 열이나
인덱스가 있으면 다음 부담이 생깁니다.

- `documents.fts`(생성 열) 추가는 documents 테이블 전체를 다시 씁니다. 시간이 걸리고, 테이블
  크기만큼 디스크를 더 쓰며, 그동안 잠금이 걸립니다.
- GIN 인덱스 생성은 쓰기를 막습니다.

그래서 컷오버 스크립트는 운영 앱을 멈춘 뒤에 실행합니다. 마지막 줄이 `delivery schema migration: PASS`면 성공입니다.

되돌린 뒤에도 추가된 열과 테이블은 남습니다. 이전 코드는 이를 무시합니다. 로컬 리허설에서
이전 이미지로 정상 기동되는 것을 확인했습니다. 문제가 되면 2-2의 백업으로 복원합니다(부록).

## 5. 점검

```bash
BE_PORT=$(grep '^BE_PORT=' .env | cut -d= -f2); BE_PORT=${BE_PORT:-8080}
TOKEN=$(grep '^DELIVERY_API_TOKEN=' .env | cut -d= -f2)
curl -s http://127.0.0.1:$BE_PORT/health    # {"status":"ok"}     DB 연결
curl -s http://127.0.0.1:$BE_PORT/ready     # {"status":"ready"}  필수 스키마
curl -s -H "X-Delivery-Token: $TOKEN" http://127.0.0.1:$BE_PORT/collect/runner/health   # {"runner_ok":true,...}
curl -s -H "X-Delivery-Token: $TOKEN" http://127.0.0.1:$BE_PORT/health/system
docker compose ps
docker compose logs --since 10m worker file-worker | tail -50
```

경로 근거: `delivery/be/app.py`(`/health`, `/ready`, `/health/crawlers`, `/health/system`),
`delivery/be/collect.py`(`/collect/runner/health`). 운영자 헤더는 `X-Delivery-Token`이며 값은
`DELIVERY_API_TOKEN`입니다(`delivery/be/access.py`).

FE 화면에서 볼 것:

- `/documents`: 문서 건수가 컷오버 전과 같은지, 문서 하나를 열어 PDF·텍스트가 열리는지
- `/crawlers`: 수집기 목록과 사이트별 건수
- `/site-collect` (사이트 수집): runner 상태가 정상인지, 사이트 목록·건수가 보이는지

ui-sci 문서를 합친 뒤에는 `migrate/02_blobcheck.sh`를 다시 실행합니다(종료코드 0이어야 함).

## 6. 되돌리기

```bash
migrate/04_rollback.sh
```

v2를 `docker compose stop`으로 멈춥니다(컨테이너·볼륨 유지). 그런 다음 `cutover-*.stopped`에 적힌
**정확히 그 ID만** `docker start`합니다. 운영 컨테이너는 원래 이미지 ID 그대로 다시 뜹니다(태그와
무관). 운영 postgres는 처음부터 멈추지 않았습니다. 같은 DB와 blob을 쓰므로 v2가 그사이 만든 문서와
파일도 그대로 남고, seq_id 충돌은 생기지 않습니다.

스크립트 없이 손으로 할 때:

```bash
docker compose stop
docker start $(cut -f2 migrate/.state/cutover-libertree-delivery-v2.stopped)
```

다시 컷오버하려면 `migrate/03_cutover.sh`를 실행합니다. `docker compose down`/`rm`과 운영 쪽의
`docker rm`/`compose down`은 쓰지 않습니다. 운영 컨테이너는 v2가 안정된 것을 확인한 뒤에도 한동안
멈춘 상태로 보존합니다.

## 7. 보안

- **FE = 운영자 권한.** FE에는 로그인이 없습니다. FE 서버가 `DELIVERY_API_TOKEN`을 붙여 BE를 부르므로
  (`delivery/fe/src/lib/backend-auth.ts`) **FE에 닿는 사람은 누구나 운영자**입니다. 수집·번역 작업
  시작·취소, 파일 처리 등을 모두 할 수 있습니다. 현재 운영은 3000을 모든 인터페이스에 공개하고
  있습니다. 이 번들의 기본값은 `FE_BIND=127.0.0.1`입니다. 외부에서 써야 하면 **인증을 하는 리버스
  프록시**(basic auth/SSO, TLS) 뒤에 두거나 방화벽으로 **IP 허용 목록**을 건 뒤에만 `FE_BIND=0.0.0.0`으로 바꾸십시오.
- **postgres 53232.** 현재 운영 postgres는 53232를 공개하고 있습니다. 비밀번호가 유출되거나 약하면
  DB 전체를 읽고 지울 수 있고, `libertree`는 superuser이므로 `COPY ... PROGRAM`으로 DB 컨테이너
  안에서 명령까지 실행할 수 있습니다.
  - 이 번들은 postgres를 다시 만들지 않으므로 포트 설정도 바꾸지 않습니다. 운영 postgres를
    `127.0.0.1:53232`로 다시 바인드하는 일은 컨테이너 재생성이 필요한 **별도 정비 작업**입니다
    (백업 후, 운영 compose에서).
  - 그때까지는 `DOCKER-USER` 체인에서 **외부 NIC로 들어오는 것만** 막으십시오. Docker가 공개한
    포트는 ufw를 우회합니다. 예: `iptables -I DOCKER-USER -i <외부NIC> -p tcp --dport 5432 -j DROP`.
    이 체인에서는 DNAT 뒤의 컨테이너 포트(5432)로 판단합니다. `-i`를 빼면 같은 브리지의 v2 be→postgres
    통신까지 막을 수 있으므로 반드시 붙이십시오.
  - 원격 작업은 SSH 터널로 합니다.
- **8792.** 현재 운영은 8792도 공개하고 있습니다. 수집 실행기나 운영 API라면 외부에서 실행·취소를
  호출하거나 토큰을 대입 공격할 수 있습니다. v2에서는 BE를 `127.0.0.1`에만 열고 runner는 포트가
  없습니다. 8792를 쓰던 프로세스를 확인(`ss -ltnp | grep 8792`)한 뒤, v2로 대체되었으면 멈추고
  포트를 닫으십시오.
- **runner의 docker 소켓 = 호스트 root.** runner는 비root uid로 돌지만 docker 그룹이므로 사실상
  root와 같습니다. 그래서 등록된 plan만 실행하고, `X-Runner-Token`을 요구하며, 호스트 포트를 열지
  않습니다. `ports:`를 추가하지 마십시오. `.token`은 0600이고 be는 그 파일 하나만 읽기 전용으로 봅니다.
- **비밀은 `.env`에만** 둡니다(`chmod 600`, 커밋 금지). 운영자 인증은 `DELIVERY_API_TOKEN`입니다.
  `DELIVERY_AUTH_MODE=disabled`는 운영에서 쓰지 않습니다. 토큰을 바꾸면 `docker compose up -d be fe`를 실행합니다.
- 확인: `ss -ltnp | grep -E ':(3000|8080|8792|8793|53232)\b'`, 외부 호스트에서 `nc -zv 서버 53232`.

## 8. 운영 중 명령

```bash
docker compose logs -f be
docker compose ps
# 코드 갱신: 새 IMAGE_TAG로 바꾼 뒤
docker compose --profile tools build && docker compose run --rm migrate && docker compose up -d
```

## 9. 교체 전 운영 구성과의 차이

| 항목 | 교체 전 운영 (`libertree-delivery`) | v2 |
|---|---|---|
| postgres | `libertree-delivery-postgres-1`, 53232 공개 | **같은 컨테이너 그대로**(v2는 정의하지 않음) |
| 프로젝트·컨테이너 이름 | `libertree-delivery-*` | `libertree-delivery-v2-*` |
| 이미지 | `libertree-delivery-*` | `libertree-delivery-v2-*:${IMAGE_TAG}` |
| BE | `127.0.0.1:8080`→3001 | 같음 |
| FE | `0.0.0.0:3000`→3002 | 기본 `127.0.0.1:3000`(`FE_BIND`) |
| blob | `/mnt/main/public/wise/WISE/blob` bind | 같은 경로(00_preflight가 일치 확인) |
| runner | (있으면) 운영 폴더 | 기본: compose 안 runner(별도 폴더 `/srv/collect-runner-v2`, 포트 없음). 선택: 호스트 runner + 소켓(1-1) |
| runtime 볼륨(ScienceON 토큰 캐시) | `libertree-delivery_runtime` | 새 볼륨(토큰은 다시 발급됨) |
| health-reports 볼륨 | 운영 볼륨 | 새 볼륨(수집기 점검 결과는 health 서비스를 다시 돌리기 전까지 비어 있음) |

## 10. 문제 해결

- `required variable IMAGE_TAG is missing a value`: `.env`에 `IMAGE_TAG`를 넣습니다.
- `network libertree-delivery_default declared as external, but could not be found`: `PROD_DB_NETWORK`를 확인합니다.
- be가 재시작을 반복하고 `/ready`가 503: `docker compose run --rm migrate`를 실행합니다.
- be 기동 실패 `bind source path does not exist: .../.token`: 1장의 `.token`을 만듭니다.
- runner 로그 `Permission denied`: 디렉터리 소유자가 `COLLECT_RUNNER_UID`와 다릅니다.
- `address already in use`: 운영 be·fe가 아직 떠 있습니다. 컷오버 스크립트로 진행합니다.

## 부록: 새 서버로 옮길 때

다른 서버나 다른 postgres로 DB를 옮겨야 하면 `migrate/new-server/`의 `restore.sh`, `postcheck.sh`,
`verify.sh`를 씁니다. 안전장치와 정렬 규칙(glibc) 설명은 `migrate/README.md`에 있습니다. 표준 절차
(같은 DB, 이미지 교체)에서는 쓰지 않습니다.
