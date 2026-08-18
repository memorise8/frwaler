# libertree-delivery 설치 가이드

이 디렉터리는 `delivery/docker-compose.yml` 로 빌드/실행하는 **납품용 패키지**입니다.
프런트엔드(FE), 백엔드(BE), 워커(Worker), Postgres 네 개의 서비스로 구성됩니다.

| 서비스 | 역할 | 이미지 빌드 | 노출 포트(호스트) |
|--------|------|-------------|--------------------|
| `postgres` | Postgres 16, 데이터 저장소 | `postgres:16` (빌드 없음) | 미노출 (컨테이너 네트워크 내부용) |
| `be` | 백엔드 API | `Dockerfile.be` | `127.0.0.1:${BE_PORT:-8080}` → 컨테이너 3001 |
| `fe` | 프런트엔드(Next.js) | `Dockerfile.fe` | `127.0.0.1:${FE_PORT:-3000}` → 컨테이너 3002 |
| `worker` | 번역/수집 백그라운드 워커 | `Dockerfile.worker` | 미노출 |

`migrate` 서비스(`profiles: ["tools", "bootstrap"]`)는 스키마 마이그레이션 전용이며, 평소 `up`에는
포함되지 않습니다. 스키마가 전혀 없는 최초 설치에서는 `--profile bootstrap`으로, 이미 떠 있는
환경에서 스키마만 바꿀 때는 `--profile tools`로 `run --rm migrate` 를 명시적으로 실행합니다(둘 다
같은 서비스를 가리키며, 프로파일 이름만 "지금이 최초 설치냐 아니냐"를 구분하기 위한 것입니다).
자세한 최초 실행 순서는 3장을 참고하세요.

`postgres`/`be`/`fe`/`worker`는 모두 `restart: unless-stopped` 입니다 — 호스트 재부팅이나 Docker
데몬 재시작 후에도 사람 개입 없이 다시 올라오고, `worker`가 일시적으로 실패하더라도(예: 최초
부트스트랩 순서를 잘못 실행해 스키마가 아직 없는 상태로 시작한 경우) 백오프를 두고 계속
재시도하며 죽은 채로 방치되지 않습니다. `migrate`/`postgres-password-guard`는 1회성 작업이므로
`restart: "no"` 그대로입니다.

---

## 1. 빌드 전 준비사항 (Prerequisites)

### 네트워크 접근

빌드 시 아래 호스트에 대한 아웃바운드 네트워크 접근이 필요합니다.

| 호스트 | 용도 | 시점 |
|--------|------|------|
| `registry.npmjs.org` | FE의 npm 의존성 설치 (`npm ci`) | 빌드 시 |
| `fonts.googleapis.com`, `fonts.gstatic.com` | FE가 `next/font/google`으로 Noto Sans/Serif KR 폰트를 내려받아 셀프호스팅 | **빌드 시에만** |

FE는 `next/font/google` (`delivery/fe/src/app/fonts.ts`)을 사용해 빌드 타임에 Google Fonts에서
폰트 파일을 내려받고, 그 결과물을 이미지 안에 셀프호스팅합니다. **런타임에는 Google에 어떤 요청도
보내지 않습니다** — 폰트 요청은 오직 `docker compose build` (또는 `up --build`) 실행 시 한 번만
발생합니다.

`registry.npmjs.org`는 허용하면서 `fonts.gstatic.com`은 막혀 있는 네트워크(예: 사내 프록시 화이트리스트)에서는
FE 빌드가 **하드 실패**합니다. 에어갭/폐쇄망 환경이라면 이 폰트 다운로드를 반드시 사전에 우회해야 합니다.

> **에어갭 대안:** `next/font/google` 대신 `next/font/local`로 전환하고, 필요한 폰트 파일(`.woff2`)을
> 리포지토리에 커밋해 두는 방법이 있습니다. 다만 이 경우 Google Fonts가 요청 시 자동으로 제공하는
> 유니코드 범위별 서브셋 분할(unicode-range slicing) 최적화는 포기해야 합니다. 이 전환은 현재
> 적용되어 있지 않습니다.

### 필수 소프트웨어

- Docker Engine (Compose v2.22 이상, `docker compose run --build` 지원 버전)

---

## 2. 환경 변수

`delivery/.env.example` 을 복사해 `delivery/.env` 를 만들고 값을 채웁니다.

```bash
cp delivery/.env.example delivery/.env
```

`delivery/docker-compose.yml` 이 실제로 참조하는 변수는 다음과 같습니다.

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `POSTGRES_USER` | `libertree` | Postgres 계정 |
| `POSTGRES_PASSWORD` | *(없음, 필수)* | Postgres 비밀번호. 비어 있으면 `postgres` 컨테이너가 **기동을 거부**합니다 (공식 postgres 이미지의 안전장치). |
| `POSTGRES_DB` | `libertree` | Postgres 데이터베이스명 |
| `BE_PORT` | `8080` | BE를 노출할 호스트 포트 (`127.0.0.1`에만 바인딩) |
| `FE_PORT` | `3000` | FE를 노출할 호스트 포트 (`127.0.0.1`에만 바인딩) |
| `DELIVERY_API_TOKEN` | *(빈 값)* | BE/FE 간 API 인증 토큰. `openssl rand -hex 32` 로 직접 생성합니다. `DELIVERY_AUTH_MODE=token`(기본값)일 때 32자 미만이거나 `.env.example`의 예시 문자열 그대로면 BE가 시작을 거부합니다. |
| `DELIVERY_AUTH_MODE` | `token` | `token` 또는 `disabled`. 로컬 격리 개발에서만 `disabled`를 명시적으로 사용하세요. |
| `DELIVERY_DB_POOL_MIN` | `1` | BE의 DB 커넥션 풀 최소 크기 |
| `DELIVERY_DB_POOL_MAX` | `10` | BE의 DB 커넥션 풀 최대 크기 |
| `DELIVERY_AGGREGATE_CACHE_SECONDS` | `30` | BE 집계 캐시 TTL(초) |
| `TRANSLATION_INTERNAL_MODEL` | *(빈 값)* | 내부(Ollama 호환) 번역 모델명 |
| `TRANSLATION_INTERNAL_ENDPOINT` | *(빈 값)* | 내부 번역 엔드포인트 (컨테이너에서 접근 가능한 주소) |
| `TRANSLATION_EXTERNAL_MODEL` | *(빈 값)* | 외부(OpenAI 호환) 번역 모델명 |
| `TRANSLATION_EXTERNAL_ENDPOINT` | *(빈 값)* | 외부 번역 엔드포인트 |
| `TRANSLATION_EXTERNAL_API_KEY` | *(빈 값)* | 외부 번역 API 키 |
| `TRANSLATION_WORKER_ID` | `delivery-worker-1` | 워커 인스턴스 식별자 |

번역 Provider(내부/외부) 변수를 설정하지 않으면 해당 Provider를 쓰는 작업만 안전하게 실패하며,
나머지 기능에는 영향이 없습니다.

### 포트 충돌 해결

`BE_PORT`/`FE_PORT`는 모두 `127.0.0.1`에만 바인딩되지만, 같은 호스트에서 다른 프로젝트가 이미
그 포트를 쓰고 있으면 (예: `127.0.0.1:8080`을 다른 컨테이너가 선점) `up -d`가 아래와 비슷한
오류로 실패합니다.

```
Error response from daemon: driver failed programming external connectivity ... address already in use
```

이때 기본값을 바꿀 필요는 없습니다 — 먼저 무엇이 그 포트를 쓰고 있는지 확인하고, 비어 있는
포트를 골라 `.env`의 `BE_PORT`/`FE_PORT`만 바꾸면 됩니다.

```bash
# 무엇이 8080을 쓰고 있는지 확인 (다른 프로젝트의 컨테이너일 수 있으니 함부로 내리지 않습니다)
docker ps --format '{{.Names}}\t{{.Ports}}' | grep 8080
ss -ltn | grep 127.0.0.1:8080

# .env에서 충돌하지 않는 포트로 변경 후 재기동
# 예: BE_PORT=18080
docker compose -f delivery/docker-compose.yml up -d
```

`LIBERTREE_PG_DSN`은 직접 설정하지 않습니다 — `docker-compose.yml`이
`POSTGRES_USER`/`POSTGRES_PASSWORD`/`POSTGRES_DB`로부터 자동으로 조합합니다
(비밀번호는 `POSTGRES_PASSWORD` 하나로만 관리되는 단일 소스입니다).

실제 blob(PDF) 스토리지를 호스트 디렉터리에 연결하려면 `delivery/docker-compose.blob.yml`을
함께 사용하고 `BLOB_HOST_PATH`(검증된 절대경로)를 설정하세요. 이 파일을 쓰지 않으면 blob은
Docker 볼륨(`blob`)에 저장됩니다.

---

## 3. 빌드 및 실행

리포지토리 루트에서 실행합니다.

```bash
# 전체 빌드
docker compose -f delivery/docker-compose.yml build

# 특정 서비스만 빌드 (예: FE만) — Postgres를 건드리지 않으므로 .env 없이도 가능
docker compose -f delivery/docker-compose.yml build fe
```

### 최초 실행 (부트스트랩) — 스키마가 전혀 없는 새 볼륨

새로 만든 Postgres 볼륨처럼 스키마가 전혀 없는 상태에서는 아래 두 명령을 **이 순서 그대로**
실행합니다. 셸의 `&&`로 묶으면 첫 명령이 성공(종료 코드 0)했을 때만 두 번째 명령이 실행되므로,
사실상 복사-붙여넣기 한 번으로 끝나는 하나의 절차입니다.

```bash
docker compose -f delivery/docker-compose.yml --profile bootstrap run --rm --build migrate && \
docker compose -f delivery/docker-compose.yml up -d
```

`migrate`는 `tools`/`bootstrap` 프로파일 뒤에 있어서 위 3장의 `docker compose ... build`
(프로파일 지정 없음)로는 다시 빌드되지 않습니다. `--build`가 없으면 예전에 캐시된 `migrate`
이미지가 옛 스키마 코드로 조용히 `PASS`를 찍을 수 있습니다 — 이번 리허설에서 실제로 겪은
문제입니다.

**왜 `be`가 `depends_on`으로 `migrate`를 기다리게 하지 않는가:** 더 간단해 보이지만 실제로는
동작하지 않습니다. `migrate`는 `tools`/`bootstrap` 프로파일에만 속해 있고, 평소의
`docker compose up -d`(프로파일 지정 없음)에는 포함되지 않습니다. 여기서 `be`가 `migrate`를
`depends_on`으로 가리키게 하면, Compose는 활성화되지 않은 프로파일에 속한 서비스를 참조하는
것 자체를 프로젝트 해석 오류로 취급하고 **즉시 실패**합니다 — 직접 재현해 확인한 동작입니다:

```
service "main" depends on undefined service "dep": invalid compose project
```

즉 `depends_on` 방식을 쓰면 이미 마이그레이션이 끝난 기존 환경에서도 평소의 `up -d`가 깨져
버립니다. 그래서 순서 보장은 Compose 그래프가 아니라 **명령 실행 순서**로 합니다:
`run --rm`은 컨테이너가 끝날 때까지 블로킹하므로, 두 번째 명령이 시작되는 시점에는 스키마가
이미 만들어져 있음이 보장됩니다.

부가적인 안전망으로, `be`/`worker`의 재시작 정책이 `unless-stopped`이기 때문에 혹시 두 명령을
잘못된 순서로(또는 동시에) 실행하더라도 `be`/`worker`는 스키마 부재로 한 번 죽었다가 백오프를
두고 계속 재시도하며, `migrate`가 끝나는 순간 스스로 정상화됩니다. 다만 이는 어디까지나
안전망이고, 위 두 단계 순서만이 매번 보장되는 방법이므로 항상 그 순서로 실행하세요.

### 이후 실행 — 스키마가 이미 있는 환경 (재기동, 재배포 등)

```bash
# 스키마를 바꿀 때만 명시적으로 실행 (평소 up에는 포함되지 않음)
# --build 없이는 캐시된 옛 migrate 이미지가 재사용될 수 있다
docker compose -f delivery/docker-compose.yml --profile tools run --rm --build migrate

# 기동
docker compose -f delivery/docker-compose.yml up -d

# 로그 확인
docker compose -f delivery/docker-compose.yml logs -f

# 중지
docker compose -f delivery/docker-compose.yml down
```

blob을 호스트 디렉터리에 연결해 실행하려면:

```bash
docker compose -f delivery/docker-compose.yml -f delivery/docker-compose.blob.yml up -d
```

## 4. 수집 결과 읽는 법

크롤러 실행 결과의 **"저장 N건"은 DB에 새로 추가된 문서 수**입니다. 크롤러가 실제로
처리한 문서 수와 다를 수 있습니다.

이미 수집된 문서를 다시 처리하면 기존 행이 갱신될 뿐 행 수가 늘지 않으므로 **0건으로
표시됩니다. 이는 실패가 아니라 "새 문서 없음"입니다.** 실측 예:

```
워커 로그  [bfr-bund-de-en] done. Total saved: 3     ← 크롤러가 처리한 건수
작업 기록  saved_count = 0                            ← 새로 늘어난 행 수
```

크롤러가 실제로 무엇을 처리했는지는 워커 로그로 확인합니다:

```bash
docker compose -f delivery/docker-compose.yml logs --since 10m worker | grep -v worker_heartbeat
```

로그를 볼 때는 `-t`(타임스탬프)를 함께 쓰는 것을 권장합니다. `docker compose up -d`는
종료된 기존 컨테이너를 **재시작**하는 경우가 있어, 이전 실행의 로그가 버퍼에 남아 방금
실행한 작업의 로그처럼 보일 수 있습니다.

`fe`/`be` 포트는 `127.0.0.1`에만 바인딩됩니다(외부 인터페이스에 노출되지 않음). 기동 후
서버에서 직접, 또는 SSH 포트포워딩 등을 통해 `http://127.0.0.1:${FE_PORT:-3000}` 으로 접속합니다.
외부에 노출하려면 앞단에 reverse proxy(nginx, Caddy 등)를 두고 TLS를 종단하는 것을 권장합니다.
