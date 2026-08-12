# Libertree 번역 기능 개발 계획

> 기준일: 2026-08-12
> 원칙: 문서 열람은 번역 서비스의 가용성과 분리하고, 원문은 절대 덮어쓰지 않는다.

## 1. 구성 경계

번역 기능을 다음 세 영역으로 분리한다.

1. **열람 계층**: BE가 PostgreSQL에 이미 저장된 완료 번역만 읽고 FE가 원문과 구분해 표시한다.
2. **번역 오케스트레이션 계층**: 대상 선별, 작업 상태, fingerprint, 재시도, 중복 방지, 비용·처리량을 관리한다.
3. **Provider 계층**: 외부 API 또는 내부 모델의 요청 형식과 오류를 공통 계약 뒤에 숨긴다.

문서 조회 요청 중에는 번역을 실행하지 않는다. 번역이 없어도 검색·상세·PDF·텍스트 열람은 정상 동작해야 한다.

## 2. 단계별 개발

### T0 — 저장 결과 열람 (이번 구현)

- `GET /documents/{seq_id}`에서 원문, 사이트, 언어, 파일 상태를 반환한다.
- `ko-KR`의 `completed` 번역 중 필드별 최신 결과만 반환한다.
- 원문 제목·초록과 번역 제목·설명을 응답에서 명확히 분리한다.
- FE 상세 화면은 번역 있음/없음, 긴 초록, 누락 필드를 안전하게 표시한다.
- 이 단계는 외부 네트워크 호출과 DB 쓰기를 하지 않는다.

### T1 — 공통 Provider 계약

```text
TranslationRequest(text, source_lang, target_locale, source_field)
TranslationResult(text, provider, model_version, usage, latency_ms)
TranslationProvider.translate(request) -> result
```

- `ExternalApiProvider`와 `InternalModelProvider`를 별도 구현한다.
- API 키·endpoint는 환경변수/secret으로 주입하고 DB나 저장소에 기록하지 않는다.
- provider 오류를 `auth`, `quota`, `rate_limit`, `timeout`, `invalid_response`, `internal` 등 안정된 코드로 정규화한다.

### T2 — 영속 작업과 오케스트레이터

- 번역 작업 테이블과 상태 이벤트: `pending → running → completed/failed/skipped`.
- 원문 SHA-256, 대상 locale, provider/model/prompt 조합으로 멱등성 보장.
- 원문 변경 시 기존 번역을 삭제하지 않고 새 fingerprint 결과를 추가한다.
- 지수 backoff, 최대 시도 횟수, lease/claim 복구, 안전한 로그 절단을 구현한다.
- 제목과 초록을 별도 작업으로 관리하고 장문은 provider 제한에 맞게 분할·결합한다.

### T3 — 외부 API Provider

- 우선 한 provider만 연결해 소량 fixture와 비용 상한으로 검증한다.
- 요청 timeout, 동시성, RPM/TPM, 일일 비용 상한과 circuit breaker를 둔다.
- 원문 전송 정책과 개인정보/민감정보 처리 기준을 운영 설정에 명시한다.

### T4 — 내부 모델 Provider

- OpenAI 호환 또는 Ollama 계열 내부 endpoint adapter를 구현한다.
- GPU별 동시성, 입력 길이, context/OOM 방어와 health check를 둔다.
- 외부 API와 동일한 결과·오류 계약을 사용한다.

### T5 — 운영 API와 FE

- 대상 건수 preview, 작업 생성, 상태·로그·실패·재시도 API.
- provider/model, 필드, 언어, 건수·비용 상한을 운영자가 선택한다.
- 문서 상세의 번역 요청 버튼은 인증·권한 단계 이후 제공한다.
- 외부/내부 처리량, 성공률, 지연, 비용을 분리해 표시한다.

### T6 — 품질·운영 검증

- 고유명사·기관명·수치·URL 보존 fixture와 언어별 표본 평가.
- 동일 입력 중복 과금 없음, 중단 후 재개, quota/timeout 복구 검증.
- provider 장애 중에도 문서 열람이 정상인지 확인한다.
- rollout은 소량 → 언어별 표본 검수 → 제한 배치 → 전체 배치 순서로 진행한다.

## 3. 기존 번역 스크립트 처리

`scripts/translate/run_translation.py`와 `gpt_translate_desc.py`는 기존 SQLite 배치 자산으로 보존한다.
새 Delivery 파이프라인은 이를 직접 호출하지 않고, 대상 선별 규칙과 prompt 경험만 참고해 PostgreSQL 작업/Provider 구조로 재구현한다.

## 4. 권장 커밋 단위

1. `feat(delivery): add translated document detail API`
2. `feat(delivery): add translated document detail frontend`
3. `feat(delivery): add translation provider contract`
4. `feat(delivery): add persistent translation jobs`
5. `feat(delivery): add external translation provider`
6. `feat(delivery): add internal translation provider`
7. `feat(delivery): add translation operations console`

외부/내부 provider 구현은 endpoint, 모델, 비용 정책이 결정된 뒤 T1부터 별도 단계로 진행한다.
