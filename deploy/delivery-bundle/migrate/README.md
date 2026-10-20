# 데이터 이전 스크립트 (기존 통합 DB → 새 스택 postgres)

모든 스크립트는 bash, `set -eu`(+pipefail)이며 **실행 전에 계획을 출력하고 `yes` 입력을 받은 뒤**
진행합니다. 자동화할 때만 `--yes`를 붙입니다. 컨테이너 이름·출력 경로는 항상 인자로 받으며
스크립트 안에 고정된 이름은 없습니다. DB 접속은 `docker exec <컨테이너> psql/pg_dump/pg_restore`
(컨테이너 내부 로컬 소켓)이므로 호스트에 PostgreSQL 클라이언트도, 공개 포트도 필요 없습니다.
기본 계정·DB는 `libertree`/`libertree`이며 `-U`, `-d`(04는 `--src-user` 등)로 바꿉니다.

| 단계 | 스크립트 | 하는 일 | 쓰기 대상 |
|---|---|---|---|
| a | `01_dump.sh SRC OUT [--jobs N]` | `pg_dump -Fc`(기본) 또는 `-Fd -j N`(병렬) | 호스트의 `OUT`, `OUT.meta` |
| b | `02_restore.sh DST DUMP [--jobs N] [--force]` | `pg_restore --no-owner --no-privileges` → `ANALYZE` | 대상 DB |
| c | `03_postcheck.sh DST [--reindex]` | 정렬 규칙 버전 확인, 필요 시 `REINDEX DATABASE` + `REFRESH COLLATION VERSION` | 대상 DB(불일치 시만) |
| d | `04_verify.sh SRC DST OUTDIR` | 테이블별·site_id별 행 수, seq_id 지문 비교 → `source.csv`, `target.csv`, `diff.txt` | `OUTDIR`만 |

## 표준 순서

서버 예시(원본: 기존 `ui-sci-20261013` 스택의 postgres 컨테이너, 대상: 새 스택). 실제 이름은
`docker ps --format '{{.Names}}'`로 확인한 값을 넣습니다.

```bash
cd deploy/delivery-bundle
SRC=ui-sci-20261013-postgres-1          # 확인한 원본 컨테이너 이름
DST=libertree-delivery-postgres-1       # 새 스택 postgres (docker compose up -d postgres 로 기동)
DUMP=/srv/backup/libertree-$(date +%Y%m%d-%H%M).dump

# 0) 원본 스택의 쓰기 주체(be/worker/file-worker 등)를 멈춥니다. postgres 는 켜 둡니다.
# 1) 덤프 (병렬은 --jobs 4 등; 이 경우 DUMP 는 디렉터리가 됨)
migrate/01_dump.sh "$SRC" "$DUMP"
# 2) 새 스택 postgres 만 띄운 상태에서 복원
migrate/02_restore.sh "$DST" "$DUMP"
# 3) 정렬 규칙 확인
migrate/03_postcheck.sh "$DST"
# 4) 비교 (migrate 실행 전)
migrate/04_verify.sh "$SRC" "$DST" /srv/backup/verify-$(date +%Y%m%d-%H%M)
# 5) 스키마를 현재 코드 버전으로 (추가형; 기존 행을 지우거나 바꾸지 않음)
docker compose run --rm migrate
```

## 안전장치

- `01_dump.sh`: 출력 경로가 이미 있으면 거부(덮어쓰지 않음). 실패하면 `*.partial`을 지웁니다.
  같은 compose 프로젝트의 다른 컨테이너가 돌고 있으면 "덤프 이후 쓰기는 옮겨지지 않음"을 경고합니다.
  완료 후 `pg_restore -l`로 아카이브를 읽어 보고, `OUT.meta`에 원본 버전·건수·sha256을 남깁니다.
- `02_restore.sh`: 다음 중 하나라도 해당하면 **거부**합니다. `--force`를 붙이면 진행하며 이때
  `--clean --if-exists`로 대상의 기존 객체를 지우고 복원합니다(되돌릴 수 없음).
  - 대상 `documents`에 행이 있음
  - 대상에 사용자 테이블이 있음(`migrate`를 먼저 돌렸거나 이전 복원 잔여물). 복원 전에 `migrate`를
    돌리면 안 됩니다. 테이블이 겹쳐 복원이 실패합니다.
  - 대상과 같은 compose 프로젝트의 다른 컨테이너(be/worker 등)가 실행 중
  - 원본 PostgreSQL 메이저 버전이 대상(16)보다 높음(`OUT.meta`로 판단)
  병렬이 아니면 `--single-transaction`으로 복원하므로 도중에 실패하면 대상은 빈 상태로 남습니다.
  `--exit-on-error`이므로 첫 오류에서 멈춥니다.
- `03_postcheck.sh`: 불일치가 없으면 아무것도 바꾸지 않습니다.
- `04_verify.sh`: 원본·대상 모두 `default_transaction_read_only=on`으로 조회합니다.

## 정렬 규칙(collation) 버전과 REINDEX가 필요한 경우

PostgreSQL은 문자열 정렬을 OS의 glibc(또는 ICU)에 맡깁니다. glibc 버전이 바뀌면 같은 문자열의
정렬 순서가 달라질 수 있어(대표적으로 glibc 2.28 변경), **예전 glibc로 만든 텍스트 인덱스를 새
glibc에서 그대로 쓰면** 인덱스 검색이 행을 놓치거나 UNIQUE 제약이 중복을 못 막을 수 있습니다.
PostgreSQL 15+는 DB 생성 당시 버전을 `pg_database.datcollversion`에 기록하고, 현재 라이브러리
버전과 다르면 접속 때 `WARNING: database "..." has a collation version mismatch`를 냅니다.

- **dump/restore(이 절차)**: 인덱스가 새 서버(postgres:16 이미지의 glibc)에서 새로 만들어지므로
  보통 불일치가 생기지 않습니다. `03_postcheck.sh`는 확인만 하고 끝납니다.
- **필요한 경우**: 기존 데이터 디렉터리(볼륨)를 다른 OS 베이스의 이미지로 그대로 띄웠을 때
  (예: `postgres:16-bullseye`(glibc 2.31) 볼륨을 `postgres:16`(bookworm, glibc 2.36)으로, 또는
  `-alpine`(musl)과 Debian 사이), 혹은 같은 태그라도 이미지 갱신으로 베이스 OS가 바뀐 경우입니다.
  이때 `REINDEX DATABASE`로 인덱스를 새 규칙으로 다시 만든 뒤 `ALTER DATABASE ... REFRESH
  COLLATION VERSION`으로 기록값을 갱신합니다. 순서가 중요합니다. REFRESH만 하면 경고만 사라지고
  손상 가능성은 그대로 남습니다.
- REINDEX는 테이블 잠금을 잡으므로 be/worker를 멈춘 상태에서 실행합니다. 불일치가 없더라도
  `--reindex`로 강제할 수 있습니다.

## seq_id와 blob 폴더

새 스택은 기존 운영 blob 디렉터리(`BLOB_HOST_PATH`)를 그대로 마운트합니다. blob 파일은
`documents.seq_id`로 찾으므로 **seq_id가 원본과 한 값도 달라지면 안 됩니다.**

- `documents.seq_id`는 `GENERATED ALWAYS AS IDENTITY`입니다. `pg_dump`는 행을 `COPY`로 원래 값
  그대로 옮기고 identity 시퀀스의 현재값(`setval`)도 함께 복원하므로 번호가 유지되고, 새 문서는
  원본의 마지막 번호 다음부터 이어집니다.
- **다시 번호를 매기지 마십시오.** CSV/INSERT로 행만 옮기거나 `OVERRIDING SYSTEM VALUE` 없이
  다시 넣거나 `ALTER SEQUENCE ... RESTART`를 쓰면 seq_id가 바뀌어 PDF·텍스트가 다른 문서에
  연결됩니다. 이 절차는 pg_dump/pg_restore만 사용합니다.
- `04_verify.sh`는 site_id별 `seq_id` 최소·최대와 전체 목록 md5(`seq_md5`), 시퀀스
  `last_value`(`seq_last`)를 원본과 비교합니다. `diff.txt`가 "(차이 없음)"이면 seq_id가 그대로입니다.
- blob 디렉터리는 복사하지 않습니다. 새 스택에서 be는 읽기 전용, worker/file-worker는 읽기·쓰기로
  같은 디렉터리를 씁니다. **이전 스택과 새 스택의 worker를 동시에 돌리지 마십시오**(같은 seq_id
  공간에 두 DB가 파일을 쓰게 됨).

## 04_verify.sh 결과 읽기

```
kind,table,site_id,value
rows,public.documents,,12345
site_rows,public.documents,krivet-re-kr,812
seq_md5,public.documents,krivet-re-kr,5f2c...
seq_last,public.documents,,13020
```

종료코드 0이면 일치, 1이면 `diff.txt`에 `-`(원본)/`+`(대상) 줄이 나옵니다. 덤프 이후 원본에 쓰기가
있었으면 차이가 납니다. 원본 쓰기 주체를 멈추고 다시 덤프·복원(`--force`)하십시오. `migrate`를
실행한 뒤에 다시 비교하면 `sites`·새 테이블 행이 늘어날 수 있으며 이는 정상(추가형)입니다.
