# 파일 수신 단계: 동작·시험·남은 한계

**합성 시험 통과이며 실제 사이트 파일 수신 성공의 증거가 아니다. 이 작업은 사이트 요청 0건, 운영·고객 DB 접속 0건으로 실행했다.**

## 실행 흐름과 대장

- 기본 본문 열거는 Adapter.file_urls → Store.enqueue_file로 URL만 대장에 넣는다. 문서 저장이 성공한 뒤 파일 대장을 등록하고 item을 saved로 표시한다. 파일 수신은 별도 단계다.
- `--files`: 본문 실행 뒤 같은 Fetcher의 남은 요청/시간 예산·호스트 게이트·robots/보류를 사용해 FilePass를 수행한다. 예산이 부족하면 pending이 남는다.
- `--files-only`: 본문을 열거하지 않고 파일 대장만 처리한다. 동일한 격리 DB 보호, SiteLease, Fetcher 안전장치를 사용한다.
- `--retry-failed`와 파일 옵션을 함께 사용하면 파일은 failed_transient만 선택한다. pending과 terminal failed/unavailable/done을 재처리하지 않는다. 플래그 없이 파일 실행은 pending과 시도 상한 미만 failed_transient를 선택한다.
- 대장 기본키는 `(site_id,seq_id,url)`. role, status, attempts, size, sha256, pages, content_type, reason, path를 기록한다. 역할 pdf는 문서의 primary blob, 나머지는 attachment 경로다.
- primary PDF: 기존 12자리 seq_id의 분산 blob 경로 → 문서 pdf_downloaded/size/hash 표시 → 수신 기록 `.pdf.receipt.json` → enum_file done 순서다. 첨부는 attachments/seq_id/hash.ext에 원자적으로 쓴다.

## 이번 변경

### CLI의 즉시 중단 정책

CLI의 두 파일 경로는 FilePass(strict_access=True)를 사용한다. 첫 robots 거부·401/403·429에서 해당 호스트를 보류하고 패스를 중단한다. robots가 거부한 파일 URL은 전송하지 않는다. 첫 429부터 멈추도록 파일 패스 동안 rate_limit_stop=1을 적용하며 끝나면 이전 Fetcher 정책을 복구한다. 다른 client/헤더/재시도로 우회하지 않는다. 이미 complete인 본문은 상태를 보존하고 file_pass_hold/cooldown 및 호스트 대장에 파일 거부를 기록한다.

기존 직접 FilePass 호출의 path-level robots 동작은 기본값 strict_access=False로 유지하여 기존 계약/회귀를 보존했다. 따라서 직접 호출자는 CLI와 동일한 정책이 필요하면 strict_access=True를 사용해야 한다. 이 옵션은 파일 입력 검증(EOF/수신 기록)도 강화한다. 본문 수집의 robots/429 정책은 수정하지 않았다.

사설주소는 원래 Fetcher가 최초 URL 및 모든 redirect hop에서 차단한다. CDN redirect도 그 호스트의 robots·게이트를 거친다. 사설주소나 로그인/인증 redirect로 우회하지 않는다. site/host 보류 해제는 기존 운영자 검토 기능이며 이번에는 실행하지 않았다.

### 내용·크기·부분 파일

전송은 bounded body이며 Content-Length 또는 실제 body가 상한을 넘으면 거부한다. FilePass도 다시 길이를 검사한다. PDF 역할/확장자/Content-Type을 고려해 PDF magic을 검사하고, HTML Content-Type의 응답은 PDF magic이 있어도 PDF로 저장하지 않는다. CLI strict 모드는 마지막 1,024바이트 내 %%EOF도 요구한다. EOF 없는 HTTP 200 응답은 bounded failed_transient → 상한 소진 시 failed로 남긴다.

중간 stream 실패는 완성된 Response나 blob을 만들지 않는다. Range 이어받기는 구현하지 않았다. 재시도는 전체 파일을 새로 받는다. 원자적 교체 전에 중단되면 기존 blob을 보존하고 임시 파일을 정리한다. 영구 프로세스 kill/전원 장애의 임시 파일 정리까지 합성 KeyboardInterrupt 시험으로 증명한 것은 아니다.

### 복구와 잘못된 완료 방지

기존 복구는 blob와 문서 SHA가 일치하면 pending primary를 done 처리했다. 새 URL의 파일도 이전 blob/hash로 완료할 수 있는 문제가 있어 strict 복구에는 다운로드 후보 URL·seq_id·size·hash가 일치하는 수신 기록을 요구한다. 기록이 없거나 손상/불일치하면 파일을 다시 받는다. legacy blob을 네트워크 없이 새 URL에 대응하는 파일로 추정하지 않는다.

blob magic/크기/EOF도 복구 때 검사한다. HTML blob이 문서 hash와 일치해도 done으로 복구하지 않는다. 복구 중 디스크 읽기/DB 오류는 bounded retry로 대장에 남긴다. 문서 DB 갱신 실패에는 rollback을 유지한다.

파일의 terminal 실패나 접근 보류가 있으면 본문 상태가 complete여도 CLI exit code에서 이를 숨기지 않는다(실패 1, 검토/보류 2). 파일 예산 소진/일시 실패는 이어받을 수 있는 상태로 남으며 0이 전체 PDF 완료를 뜻하지 않는다. 파일 대장 집계를 별도로 봐야 한다.

## 시험 범위

신규 test_enumeration_files_stage.py는 실제 FilePass·Store·blob·문서 표시와 실제 CLI의 files-only/retry-failed 경로를 가짜 전송으로 시험한다. socket/DNS/subprocess/외부 PostgreSQL 연결을 막으며 시각은 FakeClock으로 진행한다. SQLite와 합성 blob은 컨테이너 임시 fixture다. 전체 기존 시험은 원래의 임시 SQLite/일부 컨테이너 내부 loopback fixture를 사용한다. 운영/고객 DB나 실제 사이트 요청은 없다.

검사 항목: 정상 size/hash/type/pages/문서 표시, 동일 해시와 queue key 중복, 예산 중단·재개, atomic write 중단, blob/DB 후 ledger 전 중단, 손상 blob/새 URL의 잘못된 복구 방지, 복구 읽기 실패 재시도, 크기 상한, HTML/로그인/오류 내용, robots/401/403/429 즉시 중단, 새 Fetcher와 공유하는 응답 종료 후 1.5초 게이트, failed_transient만 재시도, 중간 stream 실패, EOF 없는 HTTP 200, CDN/사설주소 redirect, 본문+파일 실행, 파일 실패의 CLI 종료 코드.

실행은 python:3.12-slim, enum-test-* 임시 컨테이너, --rm --network none, source 및 local wheels read-only mount, pip --no-index로 수행했다. 기존 기준은 6b0da63의 380개(건너뜀 1). 최종 집계와 커밋은 FILES_STAGE_REPORT.md를 참고한다.

## 확인 못 한 것과 가정

- 시범 8곳의 pending 1,788건을 실제로 받거나 대장을 조회·수정한 것은 아니다. 이 숫자는 지시문의 배경값이다.
- `pages`는 /Type /Page 패턴의 근사 개수이며 압축 object stream·암호화·스캔 PDF에서 실제 페이지 수와 다르거나 NULL일 수 있다. EOF/magic은 완전한 PDF 파서·렌더링 검증이 아니다.
- SHA가 같은 다른 문서의 primary는 seq_id별 경로에 각각 저장한다. 전역 해시 CAS/물리적 중복 제거는 구현하지 않았다. 같은 seq_id의 동일 attachment 내용은 hash 경로를 공유할 수 있다.
- 파일/문서 DB/대장/수신 기록은 하나의 원자적 트랜잭션이 아니다. 데이터와 표시를 먼저 쓰고 후속 ledger 실패 시 재검증·재수신으로 복구한다. 전원 장애 fsync 보장은 primary blob의 기존 동작 이상으로 새로 증명하지 않았다.
- 실제 PostgreSQL 쓰기/큰 파일 메모리 사용량/실서버 게이트·robots·인증·첨부 링크 변동·콘텐츠 인코딩은 확인 못 했다. TEST_PG_ADMIN_DSN 없이 PG 시험 건너뜀은 정상이다.
- 기존 strict=False 호출은 EOF/수신 기록 요구를 적용하지 않는다. 이 제한을 보고서에서 CLI 보강과 구분한다.
