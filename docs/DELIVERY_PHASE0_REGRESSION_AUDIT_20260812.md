# Delivery Phase 0 회귀 감사

> 감사일: 2026-08-12  
> 범위: PostgreSQL 백엔드, SQLite 기본 경로, BE, Worker, 작업 큐, Compose  
> 원칙: 운영 SQLite와 이관된 실데이터 PostgreSQL에는 쓰기 테스트를 실행하지 않았다.

## 결과

- Python 파일 851개의 AST 구문 검사가 통과했다.
- SQLite 기본 백엔드 선택과 BaseCrawler 저장 경로 회귀 검사가 통과했다.
- storage 및 translation schema의 핵심 임시 DB 테스트가 통과했다.
- 임시 비밀번호를 사용한 `docker compose config` 렌더링이 통과했다.
- Phase 0 구현에서 즉시 수정해야 할 신규 코드 결함은 확인되지 않았다.

## 환경 때문에 미실행된 검증

- 호스트 Python에 `psycopg`, `fastapi`, `python-dateutil`이 완전히 갖춰져 있지 않다.
- `TEST_PG_DSN`이 없어 PostgreSQL/API/Worker 통합 테스트가 skip됐다.
- 현재 셸 프로세스에 Docker 그룹 권한이 반영되지 않아 임시 PostgreSQL과 Phase 0 E2E를 실행하지 못했다.

PostgreSQL 테스트는 초기화 과정에서 테이블을 `DROP`한다. 따라서 이관된 실데이터 PostgreSQL이나 운영 DB를 `TEST_PG_DSN`으로 지정하면 안 된다.

Docker 권한이 반영된 새 셸 또는 CI에서 다음 순서로 보완 검증한다.

1. `delivery/requirements.txt`와 크롤러 런타임 의존성을 설치한다.
2. `delivery/scripts/test_pg.sh`로 전용 임시 PostgreSQL을 시작한다.
3. 임시 DB만 가리키는 `TEST_PG_DSN`을 설정한다.
4. `tests/test_db_pg.py`, `test_base_crawler_backend.py`, `test_worker_jobs.py`, `test_worker_run.py`, `test_be_app.py`를 실행한다.
5. 임시 PostgreSQL과 임시 blob에서 `delivery/scripts/e2e_phase0.sh`를 실행한다.

## 기존 환경 의존 테스트

`test_translation_schema`의 symlink 오류 메시지 테스트 한 건은 현재 `data/libertree.db`가 symlink라는 과거 환경 가정을 충족하지 않아 실패했다. Phase 0 PostgreSQL 경로와 직접 관련된 회귀는 아니며, 향후 테스트가 임시 symlink fixture를 직접 생성하도록 독립적으로 정리한다.

