# 최종 납품 패키징

## 소스 번들

작업 트리가 깨끗한 커밋 상태에서 `build_release_bundle.sh`를 사용한다. Git에 추적된 파일만 포함하므로 `.env`, DB, blob, API key는 번들에 들어가지 않는다.

```bash
mkdir -p /verified/release
DELIVERY_RELEASE_CONFIRM=BUILD_COMMITTED_SOURCE_BUNDLE \
RELEASE_DIR=/verified/release \
delivery/scripts/build_release_bundle.sh
```

## PostgreSQL

실데이터 Compose 스택이 실행 중인 Docker 권한 셸에서만 다음을 실행한다.

```bash
mkdir -p /verified/backup/directory
DELIVERY_BACKUP_CONFIRM=DUMP_READONLY_RESTORE_EPHEMERAL \
BACKUP_DIR=/verified/backup/directory \
delivery/scripts/verify_delivery_backup.sh
```

스크립트는 원본 DB를 `pg_dump`로 읽고 임시 DB에 restore한 뒤 필수 정본 테이블(`sites`, `documents`), 존재하는 번역 보조 테이블과 문서 `max/sum(seq_id)`를 비교한다. 임시 DB는 종료 시 삭제하고 dump와 SHA-256만 남긴다.

## Blob

먼저 작은 fixture 또는 `--no-hash`로 경로·출력 형식을 리허설한다. 최종 전달본은 해시를 포함한다.

```bash
python3 delivery/scripts/build_blob_manifest.py \
  /verified/blob/root /verified/backup/directory/blob-manifest.jsonl
```

1.8TB 전체 SHA-256은 장시간 순차 읽기가 필요하다. 전송 전에 송신측 manifest를 생성하고, 수신측에서 같은 명령으로 생성한 결과와 비교한다. manifest는 blob 루트 밖에 생성해야 한다.
