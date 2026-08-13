# libertree-delivery 설치 가이드

이 디렉터리는 `delivery/docker-compose.yml` 로 빌드/실행하는 **납품용 패키지**입니다.
프런트엔드(FE), 백엔드(BE), 워커(Worker), Postgres 네 개의 서비스로 구성됩니다.

| 서비스 | 역할 | 이미지 빌드 | 노출 포트(호스트) |
|--------|------|-------------|--------------------|
| `postgres` | Postgres 16, 데이터 저장소 | `postgres:16` (빌드 없음) | 미노출 (컨테이너 네트워크 내부용) |
| `be` | 백엔드 API | `Dockerfile.be` | `127.0.0.1:${BE_PORT:-8080}` → 컨테이너 3001 |
| `fe` | 프런트엔드(Next.js) | `Dockerfile.fe` | `127.0.0.1:${FE_PORT:-3000}` → 컨테이너 3002 |
| `worker` | 번역/수집 백그라운드 워커 | `Dockerfile.worker` | 미노출 |

`migrate` 서비스(`profiles: ["tools"]`)는 스키마 마이그레이션 전용이며, 평소 `up`에는 포함되지 않고
`docker compose --profile tools run --rm migrate` 로 명시적으로 실행합니다.

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

- Docker Engine (Compose v2, `docker compose` 서브커맨드 지원 버전)

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
| `DELIVERY_API_TOKEN` | *(빈 값)* | BE/FE 간 API 인증 토큰. `DELIVERY_AUTH_MODE=token`(기본값)일 때 32자 미만이면 BE가 시작을 거부합니다. |
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

# 스키마 마이그레이션 (최초 1회, 또는 스키마 변경 시)
docker compose -f delivery/docker-compose.yml --profile tools run --rm migrate

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

`fe`/`be` 포트는 `127.0.0.1`에만 바인딩됩니다(외부 인터페이스에 노출되지 않음). 기동 후
서버에서 직접, 또는 SSH 포트포워딩 등을 통해 `http://127.0.0.1:${FE_PORT:-3000}` 으로 접속합니다.
외부에 노출하려면 앞단에 reverse proxy(nginx, Caddy 등)를 두고 TLS를 종단하는 것을 권장합니다.
