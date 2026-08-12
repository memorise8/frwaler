#!/bin/sh
# Representative crawler E2E against a disposable, project-scoped stack only.
#
# Flow under test: FE proxy -> BE -> crawl_jobs -> worker -> PostgreSQL.
# Live execution is intentionally awkward to start: the explicit opt-in token,
# disposable DB name, and isolated Compose project prevent accidental use of a
# delivery/production stack.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DELIVERY_DIR=$(dirname "$SCRIPT_DIR")
REPO_ROOT=$(dirname "$DELIVERY_DIR")
AUDIT_CSV="$REPO_ROOT/scripts/audit/crawler_status_final.csv"

# Every row is present in crawler_status_final.csv with status=된다 and in the
# runtime registry.  Categories describe the code path exercised, not mutually
# exclusive content taxonomy.
MATRIX='html|cedelft-eu-reports|CE Delft reports|https://cedelft.eu
api|repository-lboro-ac-uk|Loughborough Research Repository|https://repository.lboro.ac.uk
paper|hrbopenresearch-org|HRB Open Research|https://hrbopenresearch.org
report|krihs-re-kr-krihslibraryreport|KRIHS reports|https://www.krihs.re.kr
playwright|government-se-publications|Government Offices of Sweden|https://www.government.se'

usage() {
  cat <<'EOF'
Usage:
  e2e_representative_crawlers.sh --check-only
  E2E_ALLOW_EPHEMERAL=YES_I_UNDERSTAND delivery/scripts/e2e_representative_crawlers.sh --run

--check-only validates the selected sites, audit evidence, registry, and Compose
configuration without opening the Docker socket or making network requests.
--run creates and later deletes a dedicated Compose project and its volumes.
Each crawler is limited to at most 3 saved items.
EOF
}

static_check() {
  [ -f "$AUDIT_CSV" ] || { echo "missing audit CSV: $AUDIT_CSV" >&2; exit 1; }
  python3 - "$REPO_ROOT" "$AUDIT_CSV" "$MATRIX" <<'PY'
import csv
import sys
from pathlib import Path

root, audit_path, matrix_text = sys.argv[1:]
matrix = [line.split("|", 3) for line in matrix_text.splitlines() if line.strip()]
if not matrix:
    raise SystemExit("representative crawler matrix is empty")

with open(audit_path, encoding="utf-8-sig", newline="") as handle:
    audit = {row["site_id"]: row for row in csv.DictReader(handle)}

errors = []
source_root = Path(root) / "crawler" / "sites"
source_text = "\n".join(
    path.read_text(encoding="utf-8", errors="replace")
    for pattern in ("*.py", "*.json")
    for path in source_root.rglob(pattern)
)
for kind, site_id, _name, _url in matrix:
    row = audit.get(site_id)
    if row is None:
        errors.append(f"{kind}: {site_id} missing from final audit")
    elif row.get("status") != "된다":
        errors.append(f"{kind}: {site_id} audit status={row.get('status')!r}")
    if f'"{site_id}"' not in source_text and f"'{site_id}'" not in source_text:
        errors.append(f"{kind}: {site_id} missing from crawler sources")
if errors:
    raise SystemExit("\n".join(errors))
print(f"matrix check: PASS ({len(matrix)} representative crawlers)")
PY
  (
    cd "$DELIVERY_DIR"
    POSTGRES_PASSWORD=static-check-only docker compose config --quiet
  )
  echo "compose config: PASS"
}

MODE=${1:-}
case "$MODE" in
  --check-only)
    static_check
    exit 0
    ;;
  --run) ;;
  -h|--help|'') usage; exit 0 ;;
  *) usage >&2; exit 2 ;;
esac

# Strong destructive-scope guards. Do not weaken these for convenience.
[ "${E2E_ALLOW_EPHEMERAL:-}" = "YES_I_UNDERSTAND" ] || {
  echo "refusing live run: set E2E_ALLOW_EPHEMERAL=YES_I_UNDERSTAND" >&2
  exit 2
}
[ -z "${LIBERTREE_PG_DSN:-}" ] && [ -z "${DATABASE_URL:-}" ] &&
  [ -z "${PGHOST:-}" ] && [ -z "${COMPOSE_FILE:-}" ] || {
  echo "refusing live run: external DB/Compose environment is set" >&2
  exit 2
}

RUN_ID=${E2E_RUN_ID:-$(date +%Y%m%d%H%M%S)-$$}
case "$RUN_ID" in *[!a-zA-Z0-9_-]*|'') echo "invalid E2E_RUN_ID" >&2; exit 2;; esac
PROJECT="libertree-e2e-$RUN_ID"
POSTGRES_DB="libertree_e2e_$RUN_ID"
POSTGRES_USER="libertree_e2e"
POSTGRES_PASSWORD="ephemeral-$RUN_ID"
BE_PORT=${E2E_BE_PORT:-19080}
FE_PORT=${E2E_FE_PORT:-19000}
case "$BE_PORT:$FE_PORT" in *[!0-9:]*|:|*:) echo "invalid E2E port" >&2; exit 2;; esac
case "$PROJECT:$POSTGRES_DB" in libertree-e2e-*':libertree_e2e_'*) ;; *) echo "internal scope guard failed" >&2; exit 2;; esac

export COMPOSE_PROJECT_NAME=$PROJECT POSTGRES_DB POSTGRES_USER POSTGRES_PASSWORD BE_PORT FE_PORT
compose() { (cd "$DELIVERY_DIR" && docker compose "$@"); }
cleanup() {
  # PROJECT is validated above and Compose is pinned to delivery/docker-compose.yml.
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT HUP INT TERM

static_check

# Refuse port collisions instead of contacting an unrelated local service.
python3 - "$BE_PORT" "$FE_PORT" <<'PY'
import socket, sys
for raw in sys.argv[1:]:
    port = int(raw)
    with socket.socket() as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError as exc:
            raise SystemExit(f"refusing run: port {port} is occupied ({exc})")
PY

echo "starting disposable project $PROJECT (database $POSTGRES_DB)"
compose up -d --build

# Confirm the running database belongs to exactly this Compose project and has
# the disposable DB name before issuing even the test-only site seed writes.
PG_CID=$(compose ps -q postgres)
[ -n "$PG_CID" ] || { echo "postgres container not found" >&2; exit 1; }
[ "$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project" }}' "$PG_CID")" = "$PROJECT" ] || {
  echo "postgres project label mismatch" >&2; exit 1;
}
[ "$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$PG_CID" | sed -n 's/^POSTGRES_DB=//p')" = "$POSTGRES_DB" ] || {
  echo "postgres database name mismatch" >&2; exit 1;
}

BE="http://127.0.0.1:$BE_PORT"
FE="http://127.0.0.1:$FE_PORT"
i=0
until curl -fsS "$BE/health" >/dev/null 2>&1 && curl -fsS "$FE/" >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 90 ] || { compose logs --tail=100; exit 1; }
  sleep 2
done

# Image-level registry check (unlike --check-only this has all crawler deps).
SITE_IDS=$(printf '%s\n' "$MATRIX" | cut -d '|' -f 2 | paste -sd, -)
compose exec -T -e E2E_SITE_IDS="$SITE_IDS" worker python3 - <<'PY'
import os
from pathlib import Path
from crawler.sites import CRAWLERS
missing = [site for site in os.environ["E2E_SITE_IDS"].split(",") if site not in CRAWLERS]
if missing:
    raise SystemExit(f"missing from runtime registry: {', '.join(missing)}")
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    executable = Path(playwright.chromium.executable_path)
    if not executable.is_file():
        raise SystemExit(
            "Playwright Chromium is missing from worker image; install it at image build time"
        )
print("runtime registry: PASS")
print("worker Playwright Chromium: PASS")
PY

printf '%s\n' "$MATRIX" | while IFS='|' read -r KIND SITE NAME URL; do
  echo "[$KIND] $SITE: seed isolated FK row"
  compose exec -T postgres psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v site="$SITE" -v name="$NAME" -v url="$URL" <<'SQL'
INSERT INTO sites(site_id, site_name, site_url)
VALUES (:'site', :'name', :'url') ON CONFLICT (site_id) DO NOTHING;
SQL

  # POST through the FE proxy, proving FE -> BE enqueue. Python parses JSON so
  # formatting changes cannot silently produce a bogus job id.
  RESPONSE=$(curl -fsS -X POST "$FE/api/jobs" -H 'content-type: application/json' \
    -d "{\"siteId\":\"$SITE\",\"mode\":\"full\",\"limit\":3}")
  JID=$(printf '%s' "$RESPONSE" | python3 -c 'import json,sys; print(int(json.load(sys.stdin)["jobId"]))')
  echo "[$KIND] $SITE: queued job $JID"

  i=0
  while :; do
    JOB=$(curl -fsS "$BE/jobs/$JID")
    STATUS=$(printf '%s' "$JOB" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')
    case "$STATUS" in
      done) break ;;
      failed) echo "$JOB" >&2; exit 1 ;;
      queued|running) ;;
      *) echo "unexpected job status: $STATUS" >&2; exit 1 ;;
    esac
    i=$((i + 1)); [ "$i" -lt 240 ] || { echo "job $JID timeout" >&2; exit 1; }
    sleep 3
  done

  ROW=$(compose exec -T postgres psql -tA -F '|' -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
    -v jid="$JID" -v site="$SITE" -c \
    "SELECT j.requested_by,j.limit_n,j.saved_count,count(d.seq_id) FROM crawl_jobs j LEFT JOIN documents d ON d.site_id=j.site_id WHERE j.id=:'jid' AND j.site_id=:'site' GROUP BY j.id")
  REQUESTED_BY=$(printf '%s' "$ROW" | cut -d '|' -f 1)
  LIMIT_N=$(printf '%s' "$ROW" | cut -d '|' -f 2)
  SAVED=$(printf '%s' "$ROW" | cut -d '|' -f 3)
  DOCS=$(printf '%s' "$ROW" | cut -d '|' -f 4)
  [ "$REQUESTED_BY" = "delivery-fe" ] && [ "$LIMIT_N" = 3 ] || {
    echo "FE enqueue metadata mismatch: $ROW" >&2; exit 1;
  }
  [ "$SAVED" -ge 1 ] && [ "$SAVED" -le 3 ] && [ "$DOCS" -ge "$SAVED" ] || {
    echo "saved document assertion failed: $ROW" >&2; exit 1;
  }
  echo "[$KIND] $SITE: PASS (saved=$SAVED, documents=$DOCS)"
done

echo "representative crawler E2E: PASS"
echo "disposable project $PROJECT will now be removed with its volumes"
