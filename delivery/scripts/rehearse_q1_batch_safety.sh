#!/bin/sh
# Reproducible Q1 rehearsal. Creates and removes an isolated PostgreSQL container.
set -eu

ROOT=$(CDPATH= cd -- "$(dirname "$0")/../.." && pwd)
NAME=libertree-q1-rehearsal-pg
PORT=${Q1_REHEARSAL_PORT:-55441}
IMAGE=${Q1_TEST_IMAGE:-libertree-delivery-be:q1-rehearsal}

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT INT TERM
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "refusing to replace existing container: $NAME" >&2; exit 2
fi
docker build -f "$ROOT/delivery/Dockerfile.be" -t "$IMAGE" "$ROOT"
docker run -d --rm --name "$NAME" -e POSTGRES_PASSWORD=q1rehearsal \
  -e POSTGRES_DB=libertree_q1_rehearsal -p "127.0.0.1:${PORT}:5432" postgres:16 >/dev/null
ready=false
for _ in $(seq 1 30); do
  if docker exec "$NAME" pg_isready -U postgres -d libertree_q1_rehearsal >/dev/null 2>&1; then ready=true;break;fi
  sleep 1
done
[ "$ready" = true ] || { echo "temporary PostgreSQL did not become ready" >&2;exit 1; }
docker run --rm --network host \
  -e "TEST_PG_DSN=postgresql://postgres:q1rehearsal@127.0.0.1:${PORT}/libertree_q1_rehearsal" \
  -v "$ROOT:/app:ro" -w /app "$IMAGE" python -m unittest tests.test_q1_batch_rehearsal
echo "Q1 isolated 100-job rehearsal: PASS"
