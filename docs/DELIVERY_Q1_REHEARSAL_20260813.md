# Delivery Q1 격리 통합 리허설

`delivery/scripts/rehearse_q1_batch_safety.sh`는 운영 DB와 Qwen endpoint를 사용하지 않는다.
이름이 고정된 일회용 PostgreSQL과 이미지 내부의 결정적 가짜 Provider만 사용하고 종료 시 DB를
삭제한다. 원문이나 생성 결과는 출력하지 않는다.

검증 범위:

- 한국어 테스트 문서 201건을 임시 DB에만 생성
- 단일 배치 100건 enqueue/claim/완료
- 100개 시도와 실제 형식의 token usage·finish reason 저장
- 자동 품질 승인 100건과 101건 확대 baseline 개방
- endpoint unhealthy 표본에 의한 circuit open
- 최근 Provider 실패율 20%에 의한 circuit open
- 최근 p95 60초 초과에 의한 circuit open
- Worker lease 만료 시 시도 이력 보존과 작업 재대기
- JSON 구조 로그 100건과 원문 비노출
- 컨테이너·임시 DB 자동 제거

실행:

```sh
delivery/scripts/rehearse_q1_batch_safety.sh
```

이 리허설 통과는 운영 스키마 적용이나 실데이터 배치 실행을 승인하지 않는다. 다음 배포 단계에서
별도 스냅샷/빈 스택으로 스키마 적용 절차를 검증한 뒤 100건 실 endpoint canary를 수행한다.
