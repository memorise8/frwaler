#!/bin/sh
# 일회성 테스트 Postgres. 사용:  . delivery/scripts/test_pg.sh up   /   . delivery/scripts/test_pg.sh down
set -e
NAME=libertree-test-pg
case "${1:-up}" in
  up)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    docker run -d --rm --name "$NAME" \
      -e POSTGRES_PASSWORD=test -e POSTGRES_DB=libertree \
      -p 55432:5432 postgres:16 >/dev/null
    printf 'waiting for postgres'
    for i in $(seq 1 30); do
      if docker exec "$NAME" pg_isready -U postgres -d libertree >/dev/null 2>&1; then
        echo " ready"; break; fi
      printf '.'; sleep 1
    done
    echo "export TEST_PG_DSN=postgresql://postgres:test@127.0.0.1:55432/libertree"
    ;;
  down)
    docker rm -f "$NAME" >/dev/null 2>&1 || true
    echo "stopped $NAME"
    ;;
esac
