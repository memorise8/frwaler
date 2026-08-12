# Libertree Delivery · Qwen LLM 배포 핸드오프

> 작성일: 2026-08-12  
> 기준 브랜치: `delivery-mvp-audit-20260812`  
> 기준 커밋: `6fb8ec3`

## 1. 이번 체크포인트의 결론

문서 검색·상세와 번역 운영에 필요한 Delivery FE·BE 코드는 구현·검증됐다. 다음 작업은
재사용 가능한 Qwen 배포 패키지를 저장소에 만들고, 별도 GPU 서버 `ruci@192.168.0.8`에
설치해 OpenAI 호환 endpoint로 제공하는 것이다.

Cloudflare Tunnel은 GPU 서버가 아니라 현재 개발/운영 서버에 있다. 따라서 목표 경로는 다음과 같다.

```text
고객
  → https://llm.temis.me/v1/chat/completions
  → 현재 서버의 Cloudflare Tunnel (fino-tunnel)
  → http://192.168.0.8:8088
  → 인증·제한 프록시
  → vLLM replica (127.0.0.1:8000 / 127.0.0.1:8001)
```

## 2. 완료된 Delivery 기능

### 문서 제품

- 실제 PostgreSQL 현황과 최신성 화면
- 문서 검색·필터·정렬·facet·페이지네이션 API/FE
- 문서 상세 API/FE
- 저장된 `ko-KR` 제목·초록 번역 표시
- 원문·번역·파일 상태 분리

### 번역 애플리케이션

- OpenAI 호환 외부 Provider와 Ollama 내부 Provider
- 정규화 오류: auth, quota, rate limit, timeout, invalid response, internal
- PostgreSQL `translation_jobs`
- fingerprint 기반 멱등 등록과 원문 변경 감지
- 원자적 claim, 자동 backoff, 최대 재시도, 취소·수동 재시도
- 장문 무손실 분할·결합
- URL·숫자 누락 결과의 완료 방지
- 완료 결과를 기존 `document_translations`에 저장
- 대상 preview, 작업 등록·목록·상태·취소·재시도 API
- `/translations` 운영 FE
- Provider가 미설정이면 네트워크 호출 없이 안전하게 실패

### 검증

- 전용 임시 PostgreSQL 관련 테스트 57건 통과
- FE typecheck, lint, production build 통과
- BE·FE Docker 이미지 빌드 통과
- 실데이터 문서 247의 저장 번역을 읽기 전용으로 확인
- 운영 SQLite와 실데이터 PostgreSQL에 쓰기 없음

관련 커밋:

```text
fd75423 feat(delivery): add document catalogue API
a04e9a9 feat(delivery): add document search frontend
b0fca16 feat(delivery): add translated document detail API
835ce8e feat(delivery): add translated document detail frontend
ff04eda feat(delivery): add translation provider contract
8e296b2 feat(delivery): add persistent translation jobs
24e1883 feat(delivery): add translation operations API
389d21e feat(delivery): add translation operations console
6fb8ec3 fix(delivery): preserve complete translation inputs
```

## 3. GPU 서버 감사 결과

대상: `ruci@192.168.0.8` (`temis-devlop-server`)

- Ubuntu 24.04.4 LTS, kernel 6.8
- RTX 5090 32GB × 2, compute capability 12.0
- Driver 595.71.05
- 두 GPU 모두 유휴, 각 약 32.1GB free
- Ryzen 9 9950X, 16 cores / 32 threads
- RAM 123GiB, 약 117GiB available
- Docker 29.1.3, NVIDIA runtime 설치됨
- CUDA 12.8 컨테이너에서 GPU 2장 인식 통과
- Hugging Face 접근 가능
- 포트 8000, 8001, 8088, 11434 사용 가능
- GPU 간 연결은 NVLink가 아닌 PHB
- 단일 1TB NVMe, `/` 가용 약 193GB(78% 사용)

결론:

- GPU별 독립 replica가 TP=2 단일 인스턴스보다 번역 배치에 적합하다.
- 우선 GPU 0에서 4bit 모델을 검증한 뒤 GPU 1 replica를 추가한다.
- 두 replica는 모델 캐시 하나를 공유해야 한다.
- 디스크가 제한적이므로 BF16과 여러 양자화본을 동시에 보관하지 않는다.
- GPU 진단 중 `nvidia/cuda:12.8.1-base-ubuntu24.04` 이미지를 서버에 pull했다.
  서비스·설정·사용자 데이터는 변경하지 않았다.

## 4. 모델·서빙 목표

1차 후보:

```text
Qwen3-30B-A3B-Instruct-2507 4bit
non-thinking
vLLM OpenAI-compatible API
context 16,384
max output 4,096
GPU당 동시 실행 1부터 시작
```

정확한 4bit 체크포인트와 vLLM 이미지 버전은 GPU 0 실제 기동 시험 후 고정한다. 실패 시
Qwen3-8B Q8/BF16을 기준 모델로 사용해 배포 경로를 먼저 검증한다.

## 5. Cloudflare Tunnel 현황

현재 서버:

- 설정: `/etc/cloudflared/config.yml`
- tunnel: `fino-tunnel`
- systemd `cloudflared.service`
- 기존 `temis.me` 서비스는 Cloudflare를 통해 HTTP 200
- `llm.temis.me` DNS/ingress는 아직 없음

기존 route를 보존해야 한다. Tunnel 변경은 다음 순서로만 수행한다.

1. 설정 백업
2. 현재 ingress validate
3. `llm.temis.me → http://192.168.0.8:8088` 규칙을 catch-all 앞에 추가
4. DNS route 생성
5. ingress validate 재실행
6. reload
7. 기존 `temis.me`, API, Grafana route 회귀 확인
8. 실패 시 즉시 백업 복구·reload

## 6. 다음 개발 순서

### L0 — 저장소 배포 패키지

`delivery/llm/` 아래에 작성한다.

```text
delivery/llm/
├── compose.yml
├── .env.example
├── proxy/
├── scripts/install.sh
├── scripts/verify.sh
├── scripts/benchmark_translation.py
└── README.md
```

- GPU·드라이버·Docker·디스크·포트 preflight
- 이미지·모델·설정 버전 고정
- 모델 캐시와 로그 영속화
- API key 비커밋
- vLLM 포트 loopback 전용
- 인증 프록시만 `192.168.0.8:8088`에 노출
- healthcheck, restart policy, rollback

### L1 — GPU 0 단일 인스턴스

- 4bit 모델 다운로드와 캐시 크기 확인
- 실제 VRAM, 시작 시간, context 확인
- `/v1/models`, `/v1/chat/completions`
- non-thinking 번역 출력 확인
- 재기동 시 캐시 재사용 확인

### L2 — 읽기 전용 benchmark

- 기존 데이터에서 20~50개 대표 원문만 조회
- DB 저장 없이 JSON/Markdown 결과 파일 생성
- 언어·긴 초록·기관명·날짜·수치·URL 표본
- 처리량, 지연, VRAM, 누락률 비교
- 품질 승인 전 대량 번역 금지

### L3 — GPU 1 replica와 프록시

- GPU 0 → 127.0.0.1:8000
- GPU 1 → 127.0.0.1:8001
- 프록시 → 0.0.0.0:8088(사설망 접근만)
- 고객 Bearer token, body/rate/concurrency limit
- request/response 원문 로그 금지

### L4 — Tunnel 연결

- `llm.temis.me → http://192.168.0.8:8088`
- 토큰 없음/오류 401, 정상 요청 200
- 허용하지 않은 경로 차단
- 기존 Tunnel route 회귀

### L5 — Delivery 연결과 소량 실험

비커밋 `.env`:

```env
TRANSLATION_EXTERNAL_ENDPOINT=https://llm.temis.me/v1/chat/completions
TRANSLATION_EXTERNAL_MODEL=<검증 후 고정한 모델 ID>
TRANSLATION_EXTERNAL_API_KEY=<고객 전용 token>
```

진행 순서:

```text
preview → 제목 5건 → 검수 → 제목·초록 20건 → 검수 → 100건 제한 배치
```

## 7. 사용자가 결정할 것

- hostname은 `llm.temis.me`로 확정할지
- 고객의 고정 IP가 있으면 allowlist를 적용할지
- API를 번역 전용으로 제한할지 일반 chat completion도 허용할지
- 고객에게 token을 전달할 안전한 경로
- 표본 번역 최종 승인 담당자

기본 권장값:

```text
고객 token 1개
분당 30 요청
동시 요청 2
HTTP body 2MB
context 16K
output 4K
번역 중심 chat completion 허용
```

## 8. 안전 규칙

- 운영 SQLite는 항상 읽기 전용.
- 실데이터 PostgreSQL은 LLM benchmark 대상 조회만 허용.
- 번역 작업·스키마·상태 전이 시험은 임시 PostgreSQL에서만 수행.
- 사용자 승인 전 실데이터 번역 작업을 등록하지 않음.
- 기존 Cloudflare ingress를 덮어쓰거나 catch-all 순서를 깨뜨리지 않음.
- API key, tunnel credential, Hugging Face token을 커밋·출력하지 않음.
- 192.168.0.8의 기존 Elasticsearch와 포트 9200을 변경하지 않음.
- 모델 설치 전 최소 100GB 이상의 운영 여유를 계산하고 확인.

## 9. 다음 세션 시작 요청

```text
/mnt/raid/ruci_workspace/frwaler-delivery의
docs/DELIVERY_LLM_HANDOFF_20260812.md를 먼저 읽어줘.

delivery-mvp-audit-20260812 브랜치에서 L0부터 진행해줘.
먼저 delivery/llm 배포 패키지를 만들고 로컬 검증 후 별도 커밋해줘.
그다음 ruci@192.168.0.8의 GPU 0에서만 L1을 진행해줘.
기존 Elasticsearch와 현재 서버의 Cloudflare Tunnel route는 수정하지 마.
실데이터 DB에는 쓰지 말고 benchmark는 읽기 전용으로 해줘.
각 단계는 검증하고 별도 커밋해줘.
```
