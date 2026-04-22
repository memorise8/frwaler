# crawler-poc 설치 가이드 (On-premise)

고객사 시스템관리자를 위한 설치 문서입니다. **예상 소요 시간: 30분 이내** (네트워크 속도 및 Docker 이미지 빌드 시간에 따라 변동).

---

## 1. 사전 요구사항 (Prerequisites)

다음 조건을 만족하는 Linux 서버가 필요합니다.

| 항목 | 최소 사양 | 권장 사양 |
|------|-----------|-----------|
| OS | Ubuntu 22.04 LTS (또는 동급 Debian 계열) | Ubuntu 22.04 LTS |
| CPU | 2 core | 4 core |
| RAM | 4 GB | 8 GB |
| 디스크 | 20 GB | 50 GB |
| 네트워크 | 인터넷 아웃바운드 가능 (OpenAI API 호출 필요) | 동일 |

### 필수 소프트웨어

- **Docker Engine 24+**
- **Docker Compose v2** (`docker compose` 커맨드 — 구버전 `docker-compose`와 다름)

설치 확인:

```bash
docker --version          # → Docker version 24.x 이상
docker compose version    # → Docker Compose version v2.x 이상
```

Docker가 없다면 [공식 가이드](https://docs.docker.com/engine/install/ubuntu/)를 따라 설치하세요.

### OpenAI API 키

- [https://platform.openai.com/api-keys](https://platform.openai.com/api-keys) 에서 발급.
- **`gpt-5.4` 모델 접근 권한이 있어야 합니다.** (Codex CLI가 이 모델을 사용)
- 월별 사용량 상한(Spend limit)을 반드시 설정하세요. Codex 1회 실행당 약 $0.5 ~ $2 소모됩니다.

---

## 2. 설치 (5단계)

### Step 1. 저장소 복제

```bash
git clone <이 저장소의 URL> crawler-poc
cd crawler-poc
```

### Step 2. 환경 변수 파일 준비

두 개의 `.env` 파일을 만듭니다.

```bash
cp .env.example .env
cp crawler/.env.example crawler/.env
```

`.env` (finolaw 관리자 게이트용) 를 편집합니다.

```bash
vi .env
```

```env
ADMIN_USER=admin              # 브라우저 접속 시 사용할 ID
ADMIN_PASSWORD=<강력한 암호>   # 반드시 변경할 것
```

`crawler/.env` (크롤러 및 Codex용) 를 편집합니다.

```bash
vi crawler/.env
```

```env
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxx   # 위 1단계에서 발급한 키
LLM_PROVIDER=gpt
```

> 두 파일 모두 `.gitignore`에 등록되어 있어 저장소에 올라가지 않습니다. 그러나 서버의 파일 권한은 반드시 제한하세요: `chmod 600 .env crawler/.env`.

### Step 3. 컨테이너 빌드

```bash
docker compose build
```

첫 빌드는 **약 10~20분** 소요됩니다 (Playwright 브라우저 + Node 22 + Python 의존성 다운로드). 이후 재빌드는 캐시로 인해 훨씬 빠릅니다.

### Step 4. 실행

```bash
docker compose up -d
```

컨테이너가 백그라운드에서 시작됩니다. 로그 확인:

```bash
docker compose logs -f
```

`ready on http://0.0.0.0:3001` 메시지가 보이면 정상 기동입니다.

### Step 5. 접속 확인

브라우저에서 다음 주소로 접속합니다.

```
http://<서버_IP>:3001
```

HTTP Basic Auth 프롬프트가 뜨면, Step 2에서 설정한 `ADMIN_USER` / `ADMIN_PASSWORD`를 입력합니다.

페이지가 로드되면 설치 완료입니다.

---

## 3. 첫 사용 (First-Use Tutorial)

### 시나리오: arxiv.org의 최신 CS.AI 논문 수집

1. 접속 후 상단 메뉴에서 **`/auto-add`** 로 이동합니다.
2. URL 입력란에 다음을 붙여넣기:
   ```
   https://arxiv.org/list/cs.AI/recent
   ```
3. **"자동 추가"** 버튼 클릭.
4. 시스템이 자동으로:
   - 사이트 구조를 분석합니다 (5~15초).
   - Codex CLI가 Python 크롤러를 생성합니다 (1~3분).
   - 생성된 크롤러를 `crawler/sites/custom/` 에 저장합니다.
5. 생성 완료 후 **`/crawler`** 메뉴로 이동해 방금 생성된 크롤러를 실행합니다.
6. 수집된 논문 목록이 **`/search`** 에서 확인 가능합니다.

### 실패할 경우

`.cache/` 폴더에 자세한 로그가 있습니다. 가장 흔한 실패 원인:

| 증상 | 원인 | 해결 |
|------|------|------|
| "OPENAI_API_KEY not set" | `crawler/.env` 미설정 또는 컨테이너 재시작 필요 | `docker compose restart` |
| "insufficient_quota" | OpenAI 계정 잔액 부족 | [platform.openai.com](https://platform.openai.com/account/billing) 에서 충전 |
| Codex가 robots.txt 차단을 보고 중단 | 대상 사이트의 `robots.txt` 정책 | 관리자 판단 하에 Month 2의 "robots.txt 무시" 옵션 사용 |

---

## 4. 운영 (Operation)

### 재시작

```bash
docker compose restart
```

### 중지

```bash
docker compose down
```

(볼륨은 유지됩니다. 데이터가 삭제되지 않습니다.)

### 로그 확인

```bash
docker compose logs -f --tail=200
```

### API 키 교체

1. `crawler/.env` 파일의 `OPENAI_API_KEY` 값을 수정.
2. `docker compose restart` 실행.

### 백업

최소한 다음 디렉토리를 정기적으로 백업하세요 (모두 호스트에 바인드 마운트됨):

```
./data/                       # SQLite DB (수집 결과)
./crawler/sites/custom/       # Codex가 생성한 크롤러
./crawler/sites/configs/      # 사이트별 설정 JSON
```

간단한 예시:

```bash
tar czf backup-$(date +%F).tar.gz data crawler/sites/custom crawler/sites/configs
```

### 업그레이드

```bash
git pull
docker compose build
docker compose up -d
```

---

## 5. 보안 주의사항

- 이 컨테이너는 자율적으로 Python 코드를 생성/실행합니다. 자세한 내용은 [`docs/security-model.md`](docs/security-model.md) 를 반드시 읽어 주세요.
- `ADMIN_PASSWORD` 를 비워 두면 인증이 비활성화됩니다. **사내 LAN 외부에 노출하지 마세요.**
- HTTPS가 필요한 경우 앞단에 reverse proxy (nginx, Caddy 등) 를 두고 TLS를 종단하세요. 이 컨테이너는 HTTP만 직접 제공합니다.
- OpenAI API 키는 로그에 기록되지 않으며, 오직 `env_file` 을 통해서만 주입됩니다.

---

## 6. 문의 및 지원

- 이슈/버그: 저장소 이슈 트래커
- 생성된 크롤러 코드 검토: `crawler/sites/custom/*.py` (파일 상단 주석에 생성 시각과 원본 URL이 기록됩니다)
- Codex 실행 로그: `.cache/codex-*.log`
