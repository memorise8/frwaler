# 납품 blob 연결

기본 `docker-compose.yml`은 빈 named volume을 사용한다. 이 구성은 개발·빈 시작 E2E용이다. 이관된 실데이터의 PDF·텍스트를 제공할 때는 검증된 blob 디렉터리를 명시적으로 연결한다.

```bash
export BLOB_HOST_PATH=/srv/libertree/blob
docker compose \
  -f delivery/docker-compose.yml \
  -f delivery/docker-compose.blob.yml \
  up -d --build
```

- `BLOB_HOST_PATH`는 절대경로만 사용한다.
- 저장소 루트, `/`, 홈 디렉터리 또는 미확인 경로를 지정하지 않는다.
- BE에는 읽기 전용, Worker에는 쓰기 가능으로 마운트된다.
- 운영 전 blob manifest의 상대경로·크기·해시를 송신측과 수신측에서 대조한다.
- DB의 `seq_id`에서 계산한 경로와 실제 파일의 표본을 먼저 확인한 뒤 Worker 쓰기를 허용한다.

설정 렌더링만 확인하려면 다음을 실행한다. 이 명령은 컨테이너나 DB를 변경하지 않는다.

```bash
POSTGRES_PASSWORD=validation \
BLOB_HOST_PATH=/srv/libertree/blob \
docker compose \
  -f delivery/docker-compose.yml \
  -f delivery/docker-compose.blob.yml \
  config
```

