# Libertree Delivery · 자동 요약 품질 게이트 계약

> 버전: `summary-quality-v1`  
> 범위: Q0 자동 판정, 저장, 조회  
> 원칙: 원문·생성 결과를 품질 로그나 API 목록 응답에 복제하지 않는다.

## 1. 처리 시점과 판정

Worker는 구조화 요약을 받은 직후, `document_summaries` 저장과 같은 트랜잭션에서 모든
결과를 평가한다. 평가기는 외부 API를 호출하지 않는 결정적 코드이며 같은 입력과 버전에는
항상 같은 결과를 반환한다.

판정은 다음 세 값이다.

- `auto_approved`: 결정적 계약을 모두 통과하고 점수가 95점 이상이다.
- `review_recommended`: 결정적 계약은 통과했지만 근거 점수가 95점 미만이다.
- `rejected`: 출력 형식·한국어·길이·반복·기관 근거 같은 필수 계약을 위반했다.

`rejected`도 감사 가능하도록 저장하지만 일반 문서 상세에는 노출하지 않는다. 번역 작업은
Provider 호출 자체가 끝났다는 의미로 `completed`가 되며 품질 판정은 별도 축으로 관리한다.

## 2. DB 계약

`document_summary_quality`는 요약 한 건에 정확히 한 행을 가진다.

| 필드 | 의미 |
|---|---|
| `summary_id` | `document_summaries` 1:1 FK |
| `gate_version` | 평가기 버전, 현재 `summary-quality-v1` |
| `decision` | `auto_approved`, `review_recommended`, `rejected` |
| `score` | 0–100 정수 |
| `reason_codes` | 안정적인 기계 판독용 코드 배열 |
| `checks` | 원문/결과를 제외한 검사별 boolean·수치 |
| `evidence` | 각 핵심 포인트의 0-based 원문 문장 index와 유사도 |
| `evaluated_at` | 평가 완료 시각 |

`checks`와 `evidence`에는 문장 내용, 요약 내용, key point, 기관명, URL, 날짜, 숫자를 넣지
않는다. 원문에서 결정적으로 추출한 `source_facts`는 기존 요약 레코드에만 유지한다.

## 3. 결정적 검사

- 요약: 한글 포함, 3–5문장, 120–2,000자
- 핵심 포인트: 1–5개, 각 항목 한글 포함, 10–500자
- `<think>`, code fence, Markdown heading/list prefix 금지
- 동일한 정규화 문장 또는 핵심 포인트 반복 금지
- 기관명: 대소문자·공백·문장부호를 정규화했을 때 제목 또는 원문에 존재해야 함
- 잘림: 말줄임표 또는 마지막 문장 종결 부호 부재를 탐지
- source facts: URL·날짜·숫자는 모델 출력이 아닌 원문에서만 추출

기관명 미근거, 형식 위반, 비한국어, 비정상 길이, 반복, 잘림은 `rejected` 사유다.

## 4. 근거 점수

원문을 문장으로 분리하고 각 핵심 포인트마다 문자 2-gram Jaccard가 가장 높은 문장의
index와 0–100 유사도를 저장한다. 점수는 다음 가중 평균이다.

- 핵심 포인트 근거 유사도 50%
- 요약에 나온 원문 숫자 보존/비근거 숫자 없음 25%
- 기관명 근거 15%
- 결정적 형식 검사 10%

번역 언어 차이로 문자열 유사도가 낮을 수 있으므로 낮은 근거 점수만으로 결과를 폐기하지 않고
`review_recommended`로 분류한다. 향후 별도 교차언어 검증기를 도입할 때 `gate_version`을
올려 비교한다.

## 5. API 계약

`GET /translation/quality` query:

- `decision`: 세 판정 중 하나(선택)
- `limit`: 1–200, 기본 50
- `offset`: 0 이상

응답:

```json
{
  "measured_at": "ISO-8601",
  "gate_version": "summary-quality-v1",
  "summary": {"total": 0, "auto_approved": 0, "review_recommended": 0, "rejected": 0},
  "score": {"average": null, "evidence_average": null},
  "top_reasons": [{"code": "low_evidence", "count": 0}],
  "by_language": [{"source_lang": "en", "total": 0, "auto_approved": 0, "review_recommended": 0, "rejected": 0}],
  "items": [{
    "summary_id": 1, "seq_id": 1, "source_lang": "en", "site_id": "site",
    "decision": "review_recommended", "score": 82,
    "reason_codes": ["low_evidence"], "model_version": "model",
    "prompt_version": "prompt", "evaluated_at": "ISO-8601"
  }],
  "limit": 50, "offset": 0
}
```

목록은 원문, 제목, 요약, 핵심 포인트, 기관명, source facts를 반환하지 않는다. 알 수 없는
판정 또는 잘못된 pagination은 422다.

## 6. 표시 및 운영 규칙

- 문서 상세는 `auto_approved` 또는 `review_recommended`만 반환하며 품질 판정·점수를 함께 표시한다.
- 품질 현황 FE는 판정 합계, 평균 점수, 언어별 현황, 상위 사유, 위험 결과 목록을 표시한다.
- Q0는 대량 배치 실행을 허가하지 않는다. Q1 circuit breaker가 구현되기 전에는 기존 최대
  1,000건 제한과 소량 단계 실행 원칙을 유지한다.
