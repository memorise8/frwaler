# 10 파일 단계 결과

**합성 시험 통과이며 실제 사이트 파일 수신 성공의 증거가 아니다.**

- 기준 6b0da63: 380개, 건너뜀 1. 최종 전체 시험: 400개 중 399개 통과, 건너뜀 1(신규 20개).
- 구현 커밋: `bd8f02c2f1a2918dd14284d9f6eda94379b258b7`.
- 변경: crawler/enumeration/files.py, crawler/enumeration/run.py, tests/test_enumeration_files_stage.py, 본 문서와 FILES_STAGE.md.
- 파일 CLI는 첫 robots/401/403/429에 호스트 보류·즉시 중단. 파일 실패를 본문 complete/exit0으로 감추지 않음.
- retry-failed는 일시 실패 파일만 선택. 부분 stream/EOF 누락/원자적 쓰기 중단/대장 전 중단을 검증. URL·크기·SHA 수신 기록으로 새 URL을 예전 blob로 잘못 복구하는 문제 수정.
- PDF size/hash/content-type/pages/문서 표시, 동일 해시·대장 key 중복, 상한·HTML·redirect·사설주소·1.5초 게이트·CLI 경로 검증.
- 사이트 요청 0, 운영/고객/외부 DB 0, 패키지 네트워크 0. 컨테이너 내부 임시 SQLite/합성 blob과 기존 loopback 회귀 fixture만 사용.
- 실제 1,788 pending 파일, 실서버 PostgreSQL, 엄밀한 PDF 렌더/페이지 수·대용량 성능·전원 장애는 확인 못 함. 자세한 가정/기존 direct API의 strict=False 동작은 FILES_STAGE.md에 명시.

실행: python:3.12-slim --network none, local wheels --no-index; 로그 .audit/logs/baseline.log 및 release.log.
