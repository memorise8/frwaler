# Libertree Delivery · 자동 요약 품질 게이트 핸드오프

> 작성일: 2026-08-12
> 기준 브랜치: `delivery-mvp-audit-20260812`
> 기준 커밋: `3344f15`

## 1. 현재 결론

Delivery 문서 검색·상세·작업 관리와 Qwen GPU 0 서빙은 구현·검증됐다. 제품의 LLM 동작은
본문 전체 번역이 아니라 다음 두 작업으로 확정됐다.

```text
title_translation → 제목 전체 한국어 번역
abstract_summary  → 초록 3~5문장 한국어 요약 + 핵심 포인트 + 원문 기관명
```

53만여 문서를 사람이 전수 검수하는 것은 불가능하다. 다음 개발 목표는 사람 검수를 필수 처리
단계로 두지 않고, 자동 품질 게이트·위험 점수·배치 중단 기준으로 대량 처리를 통제하는 것이다.

## 2. 완료된 구현

### Delivery 애플리케이션

- `translation_jobs`: 멱등 등록, 원자적 claim, backoff, 재시도, 취소, 원문 변경 감지
- 작업 종류: `translate`, `summarize`
- API 입력: `title_translation`, `abstract_summary`
- 제목 결과: 기존 `document_translations(source_field='title')`
- 요약 결과: 별도 `document_summaries`
- 요약 구조: `summary_text`, `key_points`, `institutions`, `source_facts`
- `source_facts`의 URL·날짜·숫자는 모델이 아니라 원문에서 결정적으로 추출
- 상세 API: `generated_summary` 반환
- 상세 FE: 새 요약 우선, 기존 description 번역은 하위 호환 fallback
- 운영 FE: 제목 번역·한국어 요약 작업을 구분해 preview/enqueue
- 한국어가 아닌 구조화 요약은 retryable `invalid_response`로 완료 저장 방지

관련 계약:

- `docs/DELIVERY_SUMMARY_TRANSLATION_CONTRACT_20260812.md`

### Qwen 서빙

- 서버: `ruci@192.168.0.8`
- 모델: `Qwen/Qwen3-30B-A3B-Instruct-2507-FP8`
- vLLM 0.11.0, context 16K, GPU 0만 사용
- vLLM `127.0.0.1:8000`, 인증 프록시 `0.0.0.0:8088`
- GPU 1은 미사용
- API key는 원격 `.env`에만 있고 mode 0600
- 모델·이미지 digest와 revision은 `delivery/llm/.env.example`에 고정
- 현재 vLLM/proxy healthy, 재시작 0회
- Elasticsearch `127.0.0.1:9200`과 Cloudflare Tunnel은 변경하지 않음
- `llm.temis.me` 공개 연결은 아직 하지 않음

## 3. 검증 결과

### 코드

- 임시 PostgreSQL Provider·DB·Worker·API 테스트 25건 통과
- FE typecheck, lint, production build 통과
- BE·FE Docker 이미지 build 통과
- Compose config 통과
- 운영 SQLite/실데이터 PostgreSQL 쓰기 없음

### 구조화 요약 benchmark

- 실데이터에서 10개 언어 20건을 강제 read-only로 표본 추출
- DB 연결 종료 후 GPU endpoint만 사용
- JSON 계약 20/20
- finish reason `stop` 20/20
- thinking marker 0
- 중앙 지연 9.335초, 최대 13.214초
- completion 6,606 tokens: 전체 번역 15,124 대비 56.3% 감소
- 최초 한국어 19/20
- 중국어 실패 1건은 한국어 guard·프롬프트 강화 후 재시험 통과
- 원문·번역 결과 JSONL은 GPU 서버의 Git 제외 경로에만 있고 mode 0600

상세 기록:

- `delivery/llm/L1_VALIDATION_20260812.md`
- `delivery/llm/L2_BENCHMARK_20260812.md`
- `delivery/llm/SUMMARY_BENCHMARK_20260812.md`

## 4. 이번 세션 커밋

```text
2bb3c9c feat(delivery): add Qwen LLM deployment package
7ec1c49 ops(delivery): validate Qwen on GPU 0
856bf02 test(delivery): benchmark Qwen translations read-only
49eabce feat(delivery): add structured Korean summaries
ab316ab feat(delivery): show Korean document summaries
3344f15 test(delivery): validate structured Qwen summaries
```

## 5. 다음 개발: Q0 자동 품질 게이트

사람의 전수 검수 대신 모든 결과에 아래 검사를 적용한다.

### 결정적 검사

- JSON 및 필드 타입
- `summary_text` 한글 포함, 3~5문장
- `key_points` 1~5개, 각 항목 한글 포함
- 빈 출력, `<think>`, Markdown/code fence 거부
- 반복 문장·비정상 길이·출력 잘림 검사
- 기관명은 원문에 존재하는 문자열 또는 허용된 정규화 결과만 채택
- URL·날짜·숫자는 `source_facts`로 원문에서 별도 보존

### 근거와 위험도

- 각 핵심 포인트에 원문 근거 문장 또는 문장 index 연결
- 고유명사 일치율, 핵심 수치 일치율, 원문 근거 유사도 계산
- `auto_approved`, `review_recommended`, `rejected`로 분류
- 품질 점수와 실패 사유를 DB에 저장하되 원문을 로그에 남기지 않음

### 권장 초기 기준

| 항목 | 기준 |
|---|---:|
| JSON/한국어 계약 | 99.9% 이상 |
| 출력 잘림·반복 | 0.1% 이하 |
| URL source facts 보존 | 100% |
| Provider 오류 | 1% 이하 |
| 근거 확인 점수 | 95% 이상 |

## 6. Q1 배치 안전장치

- preview 결과와 예상 처리 시간·토큰 표시
- 한 batch 최대 건수 강제
- 언어별/사이트별 실패율 집계
- 최근 N건 실패율이 임계치를 넘으면 자동 중단
- GPU/endpoint 오류율, disk free, latency 임계치로 circuit breaker
- 실패 결과만 재시도하며 승인 결과는 fingerprint로 중복 방지
- 100 → 1,000 → 10,000 → 증분 전체 순으로 확대
- 모델·prompt version이 바뀌면 별도 품질 기준으로 다시 시작

## 7. Q2 품질 현황 FE

- 자동 승인/검토 권장/거부 건수
- 언어·사이트·문서 유형별 성공률
- 실패 사유 상위 목록
- 지연·토큰·GPU 처리량
- 모델·prompt version별 비교
- 위험 결과만 필터링해 확인
- 무작위 감사 비율은 0.1~0.5%로 설정 가능하되 처리 차단 조건은 아님

## 8. 아직 하지 않은 작업

- 새 `document_summaries` 스키마를 실데이터 PostgreSQL에 적용하지 않음
- 실데이터 작업 큐에 제목 번역/요약을 등록하지 않음
- Delivery Worker를 실제 Qwen endpoint와 연결하지 않음
- GPU 1 replica를 만들지 않음
- Cloudflare Tunnel과 `llm.temis.me`를 연결하지 않음
- 고객 token을 전달하지 않음
- 자동 품질 게이트·위험 점수·batch circuit breaker는 미구현

## 9. 안전 규칙

- 운영 SQLite는 항상 읽기 전용.
- 실데이터 PostgreSQL 쓰기·스키마 적용·작업 등록은 명시적 배포 단계 전 금지.
- 파괴적 테스트는 이름이 분명한 별도 임시 PostgreSQL에서만 실행 후 제거.
- 원문·번역·요약 JSONL, API key, tunnel credential을 커밋·출력하지 않음.
- Elasticsearch와 9200을 변경하지 않음.
- 기존 Cloudflare ingress를 변경하지 않음.
- 자동 품질 게이트가 통과하기 전 대량 batch 금지.

## 10. 다음 세션 시작 요청

```text
/mnt/raid/ruci_workspace/frwaler-delivery의
docs/DELIVERY_SUMMARY_QUALITY_HANDOFF_20260812.md를 먼저 읽어줘.

delivery-mvp-audit-20260812 브랜치에서 Q0부터 진행해줘.
먼저 자동 품질 게이트의 API·DB 계약을 문서화하고,
BE/Worker 구현과 임시 PostgreSQL 테스트 후 별도 커밋해줘.
그다음 품질 현황 FE를 구현·검증하고 별도 커밋해줘.

운영 SQLite와 실데이터 PostgreSQL에는 쓰지 마.
기존 Elasticsearch, Cloudflare Tunnel, GPU 1은 수정하지 마.
원문·번역·요약 결과와 API key는 출력하거나 커밋하지 마.
```
