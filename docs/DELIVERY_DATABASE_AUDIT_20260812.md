# Libertree 납품 데이터베이스 감사 결과

> 측정일: 2026-08-12 (Asia/Seoul)  
> 원칙: 운영 SQLite는 URI `mode=ro`와 `PRAGMA query_only=ON`으로만 조회했다. 스키마 생성, 마이그레이션, DML, `VACUUM`, 크롤러 실행은 하지 않았다.

## 결론

- 운영 정본 SQLite는 `/mnt/raid/ruci_workspace/frwaler_job/libertree-app/data/libertree.db`이다.
- 납품 운영 DB는 위 정본을 이관한 PostgreSQL이다. 빈 DB 시작이 아니라 실데이터 동봉 방식으로 납품한다.
- PDF·텍스트 blob은 Git/DB dump에 포함하지 않고 `/mnt/raid/ruci_workspace/frwaler_job/libertree`를 별도 전송한다.
- PostgreSQL의 검증용 `seq_id=536089` 행은 제거되어 있다. BE 조회 결과 404이며 마지막 실문서 `536088`은 정상 반환된다.

## 정본 SQLite 파일

| 파일 | 크기(bytes) | 수정 시각(KST) | SHA-256 |
|---|---:|---|---|
| `libertree.db` | 7,141,666,816 | 2026-08-06 18:02:30.542 | `f4728bdb0a8d4f8f6e181d9148f6d86f52c3dda6282819a22b0304353c950367` |
| `libertree.db-wal` | 33,417,352 | 2026-08-06 18:02:30.556 | `c314305b7cb8ec0505c4d7aeb228d478f7fca884f67f509a434f11dd104ad5e4` |
| `libertree.db-shm` | 32,768 | 2026-08-12 10:13:32.238 | `22e69c5bad4dded48e01f50c140caca223076f8019161674d6f91adf95588664` |

WAL 모드 DB이므로 본체만 분리하여 납품 스냅샷을 만들지 않는다. 향후 새 스냅샷은 SQLite Backup API 또는 안전하게 중지된 시점의 일관된 파일 세트로 생성한다.

## SQLite 내용 측정

`PRAGMA quick_check` 결과는 `ok`이다.

| 항목 | 값 |
|---|---:|
| sites | 805 |
| documents | 536,017 |
| document_lang | 322,351 |
| document_translations | 579,774 |
| document_anomaly | 5 |
| max(seq_id) | 536,088 |
| sum(seq_id) | 143,689,463,177 |
| 최신 collected_at | 2026-08-06 09:02:30 UTC |
| PDF 확보 문서 | 303,245 |
| 텍스트 확보 문서 | 288,124 |

SQLite 테이블에는 위 5개 업무 테이블 외에 번역 작업 테이블, FTS5 내부 테이블, `sqlite_sequence`, `sqlite_stat1`이 존재한다.

## 과거 스냅샷 비교

두 7월 스냅샷은 내용 수치가 같지만 정본보다 54,348문서가 적어 롤백/비교용으로만 보존한다.

| 경로 | 사이트 | 문서 | max(seq_id) | 최신 collected_at | SHA-256 |
|---|---:|---:|---:|---|---|
| `.omo/cutover-snapshots/libertree-20260726T102811Z/libertree.db` | 762 | 481,669 | 481,732 | 2026-07-20 05:11:26 UTC | `5b0711e03d5984193d47b8b5ecf2e05730a2201e3af40f5f07020e4dbde85976` |
| `.omo/rollback/libertree-20260727T084701Z/libertree.db` | 762 | 481,669 | 481,732 | 2026-07-20 05:11:26 UTC | `2083b9126d2d337146eec6c23a9f4bb84ef4d1051c9f2107e1ea2cd1d0825a50` |

두 스냅샷 모두 `PRAGMA quick_check=ok`이다.

## Blob 정본

| 항목 | 값 |
|---|---:|
| 경로 | `/mnt/raid/ruci_workspace/frwaler_job/libertree` |
| 전체 파일 | 591,879 |
| 전체 바이트 | 1,818,321,413,379 |
| PDF 파일 | 303,739 |
| PDF 바이트 | 1,791,513,626,968 |

DB의 `pdf_downloaded=1` 문서는 303,245건이다. 전체 PDF 파일 수와의 차이는 재수집 산출물·비정본 파일 가능성이 있으므로 Phase 1 DB 현황 화면의 정합성 집계에서 전체 경로 대조 대상으로 둔다. 이관 검증의 DB 참조 PDF 표본 200건은 모두 존재했다.

## PostgreSQL 이관 및 검증

이관 스크립트: `delivery/scripts/migrate_sqlite_to_pg.py`

| 테이블 | SQLite | PostgreSQL |
|---|---:|---:|
| sites | 805 | 805 |
| documents | 536,017 | 536,017 |
| document_lang | 322,351 | 322,351 |
| document_translations | 579,774 | 579,774 |
| document_anomaly | 5 | 5 |

- 총 이관: 1,438,952행 / 175초
- `max(seq_id)`: 536,088 일치
- `sum(seq_id)`: 143,689,463,177 일치
- 참조 무결성 고아: 0건
- PDF 표본: 200/200 존재
- PostgreSQL DB 크기: 약 4.0GB
- BE: `http://127.0.0.1:18080/health` 200, 문서 `108944` 및 `536088` 조회 정상
- 검증용 `536089`: 현재 BE 조회 404로 제거 확인
- 다음 신규 identity 값: 536,089로 설정
- 전문검색: `climate change` 6,720건
- 제목 부분일치: `budget` 1,838건
- 한국어 번역 조회 및 dedup UNIQUE 동작 확인

### 이관 중 정규화

- PostgreSQL `text`가 허용하지 않는 NUL 문자 7개를 제거했다. 해당 문자만 제거하고 나머지 본문은 보존했다.
- 1MB를 넘는 abstract 원문은 자르지 않았다. PostgreSQL `tsvector` 생성 입력만 250,000자로 제한했다.

## 최종 고정 및 납품 체크

- [x] 운영 SQLite 정본 경로·WAL/SHM·해시 기록
- [x] SQLite 읽기 전용 무결성·건수 측정
- [x] PostgreSQL 테이블 건수·seq_id checksum·FK·PDF 표본 검증
- [x] 검증용 문서가 최종 PostgreSQL에 남아 있지 않음을 BE로 확인
- [x] 다음 신규 문서 identity가 536,089임을 이관 검증에서 확인
- [ ] 최종 PostgreSQL dump 생성
- [ ] 빈 임시 PostgreSQL에 dump restore 후 동일 검증 재실행
- [ ] blob 전송본 manifest(상대경로·크기·해시) 생성 및 수신측 대조

마지막 세 항목은 최종 납품 아카이브를 고정하는 패키징 단계에서 수행한다. 현재 실행 세션은 Docker 소켓 접근 권한이 없어 실행 중 컨테이너에서 `pg_dump`/restore를 수행하지 않았다.

