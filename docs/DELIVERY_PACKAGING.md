# 최종 납품 패키징

## PostgreSQL

실데이터 Compose 스택이 실행 중인 Docker 권한 셸에서만 다음을 실행한다.

```bash
mkdir -p /verified/backup/directory
DELIVERY_BACKUP_CONFIRM=DUMP_READONLY_RESTORE_EPHEMERAL \
BACKUP_DIR=/verified/backup/directory \
delivery/scripts/verify_delivery_backup.sh
```

스크립트는 원본 DB를 `pg_dump`로 읽고, 임시 DB에 restore한 뒤 5개 테이블 건수와 문서 `max/sum(seq_id)`를 비교한다. 임시 DB는 종료 시 삭제하고 dump와 SHA-256만 남긴다.

## Blob

먼저 작은 fixture 또는 `--no-hash`로 경로·출력 형식을 리허설한다. 최종 전달본은 해시를 포함한다.

```bash
python3 delivery/scripts/build_blob_manifest.py \
  /verified/blob/root /verified/backup/directory/blob-manifest.jsonl
```

1.8TB 전체 SHA-256은 장시간 순차 읽기가 필요하다. 전송 전에 송신측 manifest를 생성하고, 수신측에서 같은 명령으로 생성한 결과와 비교한다. manifest는 blob 루트 밖에 생성해야 한다.

