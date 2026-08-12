# Libertree Delivery 제품 완성 계획

> 작성일: 2026-08-12  
> 기준 브랜치: `delivery-mvp-audit-20260812`  
> 목적: compact 또는 새 세션 이후 FE·BE 제품 개발을 기능 단위로 이어가기 위한 실행 기준

## 1. 현재 완료 상태

### 데이터·기반

- 운영 정본 SQLite 읽기 전용 감사 완료.
- PostgreSQL 실데이터 이관 완료: 사이트 805, 문서 536,017.
- `max(seq_id)=536088`, `sum(seq_id)=143689463177`, FK 고아 0.
- PostgreSQL 백엔드, BE, Worker, 작업 큐, Compose 기반 구현 완료.
- 임시 PostgreSQL에서 PG/API/Worker 통합 테스트 30개 전부 통과.
- 실제 blob 호스트 마운트 오버라이드와 패키징 도구 구현 완료.

### 현재 FE

- E-CIP/Libertree 계열 정보 포털 스타일.
- 감사 스냅샷 기반 크롤러 카탈로그, 국가·자료 유형 분류.
- 크롤러 상세와 작업 등록.
- 실제 DB 현황, 국가·자료 유형별 문서 집계.
- 작업 목록·대기 취소·실패/취소 재시도.
- 7일·30일·90일 최신화 요약.
- Compose FE 서비스, typecheck, lint, production build 통과.

### 현재 BE

- `/health`, 문서 단건 조회.
- 작업 등록·단건·목록·취소·재시도.
- `/stats`, `/freshness` 읽기 전용 집계.
- PostgreSQL 저장과 dedup.
- Worker claim/완료/실패 처리.

## 2. 제품 완성 기준

1차 납품 제품은 다음 흐름을 제공해야 한다.

```text
실데이터 현황 확인
→ 문서 검색·필터
→ 문서 상세·번역 확인
→ PDF·텍스트 열람
→ 크롤러 실행
→ 작업 진행·로그·실패 확인
→ 취소·재시도
→ 최신화·스케줄 관리
```

FE는 PostgreSQL이나 blob에 직접 접근하지 않는다. 모든 데이터와 파일은 BE API를 통해 제공한다.

## 3. 개발 원칙

- 같은 세션에서 기능 단위로 `API 계약 → BE → BE 테스트 → FE → FE 검증 → 통합 검증` 순서로 개발한다.
- BE와 FE를 별도 장기 트랙으로 분리하지 않는다.
- 기능별 커밋을 유지하고 다른 단계 변경을 섞지 않는다.
- 운영 SQLite는 항상 읽기 전용이다.
- 실데이터 PostgreSQL에서는 조회 검증만 한다.
- 테이블 초기화·크롤·상태 전이 테스트는 전용 임시 PostgreSQL에서만 한다.
- 디자인 기준은 `delivery/fe`, `libertree-app`, `ecip-frontend`다. `finolaw`는 동작 참고용이며 중복 구현하지 않는다.
- 코어 열람·수집 기능은 LLM 없이 동작해야 한다.

## 4. FE·BE 개발 순서

### 단계 A — 문서 카탈로그 API

BE:

- `GET /documents` 목록 API.
- 페이지네이션: `page`, `page_size`, 전체 건수.
- 검색: PostgreSQL `fts` 및 제목 `pg_trgm`/부분일치.
- 필터: 사이트, 국가 분류 입력값, 자료 유형 입력값, 언어, 발행일, 수집일, PDF·텍스트 보유 여부.
- 정렬: 관련도, 최신 게시일, 최신 수집일, `seq_id`.
- 검색 facet API 또는 목록 응답의 facet 블록.
- 잘못된 필터·과도한 page size 제한.

완료 조건:

- 빈 DB, 검색어 없음, 다국어 검색, 조합 필터, 마지막 페이지 테스트.
- 실데이터 읽기 전용 SQL과 API 전체 건수가 일치.
- 일반 목록 쿼리가 허용 성능 범위에서 완료.

권장 커밋:

```text
feat(delivery): add document catalogue API
```

### 단계 B — 문서 탐색 FE

FE:

- `/documents` 문서 탐색 화면.
- 통합 검색창과 URL query 기반 필터.
- 국가, 자료 유형, 사이트, 날짜, 파일 보유 필터.
- 검색 결과 카드 또는 표, 페이지 이동, 정렬.
- PDF·텍스트·번역 보유 배지.
- 로딩, 빈 결과, BE 장애 상태.
- 홈 DB 현황에서 문서 탐색으로 이동.

완료 조건:

- 새로고침·공유 URL에서 검색 조건 유지.
- FE 결과 수와 API 결과 수 일치.
- typecheck, lint, build 통과.
- 모바일과 키보드 기본 탐색 확인.

권장 커밋:

```text
feat(delivery): add document search frontend
```

### 단계 C — 문서 상세·번역

BE:

- 기존 `GET /documents/{seq_id}` 응답 확장.
- `document_lang`, `document_translations` 결합.
- 원문 제목·초록과 한국어 번역을 구분해 반환.
- 사이트 정보와 파일 상태 포함.

FE:

- `/documents/{seq_id}` 상세 화면.
- 제목, 번역, 저자, 발행처, 저널, 날짜, 키워드, 초록, 요약, 원문 링크.
- 긴 본문·누락 필드·다국어 처리.

완료 조건:

- 번역 있음/없음, PDF 있음/없음 대표 문서 검증.
- 잘못된 ID와 없는 문서 404 화면.

권장 커밋:

```text
feat(delivery): add translated document detail
```

### 단계 D — PDF·텍스트 제공

BE:

- `GET /documents/{seq_id}/pdf`.
- `GET /documents/{seq_id}/text`.
- `seq_id` 기반 정규 blob 경로만 허용.
- 경로 탈출 차단, 파일 존재·확장자·DB 플래그 대조.
- PDF inline/attachment 헤더와 range 요청 검토.
- BE blob 마운트는 읽기 전용.

FE:

- PDF 열기·다운로드.
- 추출 텍스트 보기.
- 파일 누락 및 아직 미처리 상태 안내.

완료 조건:

- 정상 PDF, 정상 text, DB 플래그만 있고 파일 없음, 잘못된 ID 테스트.
- 임의 경로 입력으로 blob 루트 밖 파일을 읽을 수 없음.

권장 커밋:

```text
feat(delivery): add secure document blob delivery
```

### 단계 E — 작업 진행·로그

BE/DB/Worker:

- `crawl_job_logs` 또는 동등한 영속 로그 구조.
- queued/running/done/failed/cancelled 상태 이벤트.
- Worker 저장 건수 주기적 반영.
- 실행 시작·종료·오류와 안전하게 잘린 로그 저장.
- 실행 중 취소 요청 상태 및 Worker 협력 취소.
- 작업 상세·로그 API.

FE:

- `/jobs` 전용 화면.
- 작업 상세, 상태 자동 갱신, 저장 건수, 오류, 로그.
- 대기/실행 작업 취소와 실패 작업 재시도.
- 원본 작업과 재시도 작업 연결.

완료 조건:

- 새로고침 후 이력 유지.
- queued→running→done/failed 흐름 표시.
- 취소 경쟁 상황과 중복 재시도 테스트.

권장 커밋:

```text
feat(delivery): add persistent crawl progress and logs
```

### 단계 F — 최신화·스케줄

BE/DB:

- `schedules` 테이블과 CRUD.
- 사이트별 주기, mode, 활성 상태, 최근·다음 실행 시각.
- 스케줄러가 만기 작업을 중복 없이 enqueue.
- 증분 모드 중복 방지 및 선택적 조기 종료.
- 사이트별 마지막 수집일과 최신 게시일 분리.

FE:

- 오래된 사이트 목록.
- 개별·선택 증분 실행.
- 스케줄 생성·수정·중지.
- 7/30/90일 분포에서 대상 목록으로 이동.

완료 조건:

- 임시 PG에서 예약 작업 한 번만 등록.
- 같은 사이트 재수집 후 중복 증가 없음.
- 신규 fixture 문서만 추가됨.

권장 커밋:

```text
feat(delivery): add schedules and incremental controls
```

### 단계 G — 인증·역할

BE:

- 사용자, 비밀번호 해시, 서버 세션.
- `operator`, `technical` 역할.
- 읽기/작업 실행/설정 API 권한 강제.
- 초기 관리자 bootstrap과 비밀정보 비커밋.

FE:

- 로그인·로그아웃.
- 역할별 메뉴와 작업 버튼.
- 인증 만료 처리.

완료 조건:

- 비로그인, operator, technical 권한 계약 테스트.
- FE에서 버튼을 숨기는 것과 별개로 BE가 직접 요청을 차단.

권장 커밋:

```text
feat(delivery): add operator authentication and roles
```

### 단계 H — 제품 검수·납품

- 대표 크롤러 5종 실제 E2E.
- 증분 중복 방지 실측.
- 최신 코드 4컨테이너 실데이터 읽기 전용 화면 확인.
- PostgreSQL dump→빈 임시 DB restore 검증.
- blob manifest 생성·전송 후 대조.
- 새 서버 설치, 재부팅, 데이터 유지 확인.
- 설치·운영·백업·복구·장애 대응 문서 확정.
- 버전 태그와 최종 SHA-256 산출물.

## 5. 남은 작업 우선순위

1. 단계 A 문서 카탈로그 API.
2. 단계 B 문서 탐색 FE.
3. 단계 C 문서 상세·번역.
4. 단계 D PDF·텍스트 제공.
5. 단계 E 작업 진행·로그.
6. 단계 F 최신화·스케줄.
7. 단계 G 인증·역할.
8. 단계 H 전체 검수·패키징.

대표 크롤러 실제 E2E는 사용자의 결정에 따라 납품 직전 전체 테스트로 유보한다.

## 6. 다음 세션 시작 지시

```text
/mnt/raid/ruci_workspace/frwaler-delivery의
docs/DELIVERY_PRODUCT_PLAN_20260812.md를 먼저 읽어줘.
delivery-mvp-audit-20260812 브랜치에서 단계 A부터 진행해줘.
API 계약을 먼저 정하고 BE 구현·테스트 후 별도 커밋해줘.
그다음 같은 계약으로 FE를 구현·검증하고 별도 커밋해줘.
운영 SQLite와 실데이터 PostgreSQL에는 쓰지 마.
파괴적 테스트는 별도 임시 PostgreSQL에서만 실행해줘.
```

