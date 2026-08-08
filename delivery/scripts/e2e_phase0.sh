#!/bin/sh
# Phase 0 end-to-end: 빈 시작 → 실제 사이트 시드 → 작업 enqueue → 워커 수집 → 조회.
set -e
cd "$(dirname "$0")/.."   # delivery/
BE="http://127.0.0.1:${BE_PORT:-8080}"

echo "[1/6] compose up"
docker compose up -d --build
echo "[2/6] be health 대기"
for i in $(seq 1 40); do
  if curl -fsS "$BE/health" >/dev/null 2>&1; then echo "  ok"; break; fi
  sleep 2
done

echo "[3/6] 대상 사이트 site 행 시드(FK 충족)"
# 가벼운 실제 크롤러 하나 선택. limit 3.
SITE=datos-gob-mx-agricultura
docker compose exec -T postgres psql -U "${POSTGRES_USER:-libertree}" -d "${POSTGRES_DB:-libertree}" \
  -c "INSERT INTO sites(site_id,site_name,site_url) VALUES ('$SITE','$SITE','https://datos.gob.mx') ON CONFLICT DO NOTHING;"

echo "[4/6] 작업 enqueue (limit 3)"
JID=$(curl -fsS -X POST "$BE/jobs" -H 'content-type: application/json' \
  -d "{\"site_id\":\"$SITE\",\"mode\":\"full\",\"limit_n\":3}" | sed 's/.*"id":\([0-9]*\).*/\1/')
echo "  job id=$JID"

echo "[5/6] 워커 처리 대기 (done 까지)"
for i in $(seq 1 60); do
  ST=$(curl -fsS "$BE/jobs/$JID" | sed 's/.*"status":"\([a-z]*\)".*/\1/')
  echo "  status=$ST"
  [ "$ST" = "done" ] && break
  [ "$ST" = "failed" ] && { echo "FAILED"; curl -fsS "$BE/jobs/$JID"; exit 1; }
  sleep 3
done

echo "[6/6] 저장 문서 조회"
N=$(docker compose exec -T postgres psql -tA -U "${POSTGRES_USER:-libertree}" -d "${POSTGRES_DB:-libertree}" \
  -c "SELECT count(*) FROM documents WHERE site_id='$SITE';")
echo "  documents for $SITE = $N"
[ "$N" -ge 1 ] || { echo "NO DOCS SAVED"; exit 1; }
echo "PHASE 0 E2E: PASS"
