# Delivery 제목 번역·한국어 요약 계약

## 제품 동작

- `title_translation`: 원문 제목을 `ko-KR`로 전체 번역한다.
- `abstract_summary`: 원문 초록을 3~5문장의 한국어 요약과 최대 5개 핵심 포인트로 생성한다.
- 기존 `description` 전체 번역은 조회 호환용으로 유지하지만 신규 기본 작업에는 등록하지 않는다.
- 원문, 기존 번역, 새 요약은 서로 덮어쓰지 않는다.

## API 입력

`POST /translation/preview`와 `POST /translation/jobs`는 다음 작업 배열을 사용한다.

```json
{"tasks":["title_translation","abstract_summary"]}
```

등록 요청은 기존 provider/model/locale/filter/limit 필드를 함께 사용하며 기본 prompt version은
`title-summary-ko-v1`이다. 알 수 없는 필드는 422로 거부한다.

## 저장 결과

제목은 기존 `document_translations(source_field='title')`에 저장한다. 요약은 별도
`document_summaries`에 다음 구조로 저장한다.

```json
{
  "summary_text": "한국어 3~5문장",
  "key_points": ["핵심 사항"],
  "institutions": ["Original Institution Name"],
  "source_facts": {
    "urls": ["원문에서 직접 추출"],
    "dates": ["원문에서 직접 추출"],
    "numbers": ["원문에서 직접 추출"]
  }
}
```

`source_facts`는 모델 출력이 아니라 원문에서 결정적으로 추출한다. 따라서 요약에서 생략된
출처 URL·날짜·숫자도 검증 및 표시용 원문 사실로 보존된다.

## 상세 조회

`GET /documents/{seq_id}`는 기존 `translations`와 별도로 최신 완료 결과를
`generated_summary`에 반환한다. FE는 새 요약을 우선 표시하고, 새 요약이 없는 기존 문서는
저장된 description 번역을 fallback으로 표시한다. 상세 화면은 외부 LLM을 실시간 호출하지 않는다.

## 안전 및 승인

- 운영 DB에서 대량 작업을 자동 등록하지 않는다.
- preview → 5건 → 사람 검수 → 20건 순서로 승인한다.
- 기관명, 의미 왜곡, 환각, 핵심 수치 선택은 사람 검수 대상이다.
- 파괴적 상태 전이 테스트는 임시 PostgreSQL에서만 실행한다.
