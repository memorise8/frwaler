# 번들 스크립트

모두 bash이고 `set -eu`와 pipefail을 씁니다. **실행 전에 계획을 출력하고**, 바꾸는 작업은 `yes`를
입력해야 진행합니다(자동화할 때만 `--yes`). 번들 디렉터리의 `.env`(또는 `--env-file`)와 compose
설정을 읽습니다. DB 접속은 `docker exec <컨테이너> psql/pg_dump`(컨테이너 내부 소켓)로 하므로
호스트에 PostgreSQL 클라이언트가 필요 없습니다. 기록 파일은 `migrate/.state/`에 남습니다(커밋하지
않음).

## 표준 절차: 같은 DB에서 이미지만 교체

| 순서 | 스크립트 | 하는 일 | 바꾸는 것 |
|---|---|---|---|
| 0 | `00_preflight.sh` | 운영 컨테이너 찾기, ID·이미지 ID 기록, v2 충돌·경로·포트·디렉터리 확인 | `.state/` 기록만 |
| 1 | `01_dump.sh <운영 postgres> <백업파일> [--jobs N]` | 변경 전 `pg_dump -Fc`(병렬이면 `-Fd`) 백업과 `.meta` 생성 | 백업 파일만 |
| 2 | `02_blobcheck.sh [--sample N]` | 운영 DB와 blob 디렉터리의 seq_id 일치 확인(읽기 전용) | 없음 |
| 3 | `03_cutover.sh` | blob 게이트 → 운영 앱 컨테이너 `docker stop`(ID 기록) → `migrate` → v2 기동 | 운영 앱 정지, 스키마(추가형), v2 생성 |
| 4 | `04_rollback.sh` | v2 `docker compose stop` → 기록한 ID만 `docker start` | v2 정지, 운영 앱 재시작 |

- 운영 postgres는 어느 단계에서도 멈추거나 다시 만들지 않습니다. 03과 04는 처음과 끝에 운영
  postgres의 컨테이너 ID가 기록과 같은지 확인합니다.
- 지우는 명령(`rm`, `down`, `volume rm`)은 어느 스크립트에도 없습니다.
- `00_preflight.sh`는 다음 중 하나라도 해당하면 중단합니다:
  - 운영 postgres(`PROD_DB_HOST`)가 없거나 실행 중이 아님
  - 운영 프로젝트에서 be·fe·worker·file-worker를 찾지 못함
  - v2 프로젝트 이름이 운영 프로젝트와 같음
  - v2 컨테이너·볼륨·네트워크가 이미 있음
  - 새 이미지 이름이 운영 이미지와 같음
  - `BLOB_HOST_PATH`가 운영 be의 blob 경로와 다름
  - runner 디렉터리나 `.token`이 준비되지 않음
- `03_cutover.sh`는 blob 게이트가 실패하면 **아무것도 멈추지 않고** 끝납니다. 이미 컷오버된
  상태(`.state/cutover-<프로젝트>.stopped`가 있음)면 거부합니다.
- `02_blobcheck.sh`의 기준:
  - `pdf_downloaded=1`/`text_extracted=1`인 행을 무작위 N건과 seq_id 상위 20건 뽑아 파일이
    있는지(`<blob>/XXXX/YYYY/<seq12>.pdf|.txt`), 크기가 `pdf_size_bytes`와 같은지 봅니다.
  - blob 디렉터리의 최대 seq_id가 `documents.seq_id` 시퀀스 `last_value` 이하인지 봅니다.
  - 하나라도 어긋나면 종료코드 1입니다. ui-sci 문서를 운영 DB에 합친 뒤에도 다시 실행하십시오.
    합친 문서 가운데 파일 없이 `pdf_downloaded=1`인 행이 있으면 여기서 잡힙니다.

## 부록: 새 서버로 옮길 때 (`new-server/`)

표준 절차에서는 쓰지 않습니다. DB를 다른 postgres 컨테이너로 옮겨야 할 때만 씁니다.

- `new-server/restore.sh <대상> <덤프> [--jobs N] [--overwrite-db=<대상>] [--no-meta-check]`:
  `pg_restore --no-owner --no-privileges --exit-on-error`로 복원한 뒤 `ANALYZE`를 실행합니다.
  - 무조건 거부: 대상이 호스트 포트를 공개 중이거나, 같은 프로젝트의 다른 컨테이너가 실행
    중이거나, 원본 메이저 버전이 대상보다 높을 때.
  - 대상에 데이터가 있으면 `--overwrite-db=<대상 이름>`이 정확히 같을 때만 진행합니다
    (`--clean --if-exists --single-transaction`, `--jobs>1`과는 함께 쓸 수 없음).
  - `.meta`가 없으면 거부합니다. 덤프 파일은 sha256도 확인합니다.
- `new-server/postcheck.sh <대상> [--reindex]`: 정렬 규칙 버전을 확인합니다. 불일치하거나
  `--reindex`를 주면 `REINDEX DATABASE` 후 `ALTER DATABASE ... REFRESH COLLATION VERSION`을
  실행합니다. 불일치는 glibc 버전이 다른 이미지로 **같은 데이터 디렉터리**를 띄울 때
  생깁니다(예: bookworm 2.36 → trixie 2.41, 현재 `postgres:16` 태그는 trixie). dump/restore는
  새 서버에서 인덱스를 다시 만들므로 보통 불일치가 없습니다.
- `new-server/verify.sh <원본> <대상> <출력폴더>`: 테이블별·site_id별 행 수, documents의
  site_id별 seq_id 최소·최대·md5, 시퀀스 값을 `source.csv`·`target.csv`·`diff.txt`로
  남깁니다(정렬은 `COLLATE "C"`). 일치하면 0, 다르면 1을 돌려줍니다.

seq_id 보존: `documents.seq_id`는 `GENERATED ALWAYS AS IDENTITY`입니다. pg_dump/pg_restore는 값과
시퀀스 위치를 그대로 옮깁니다. 행만 다시 넣거나 시퀀스를 재설정하는 방식으로 번호를 다시 매기지
마십시오. blob 파일이 다른 문서에 연결됩니다.
