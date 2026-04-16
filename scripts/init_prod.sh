#!/usr/bin/env bash
# BJT 업스크리닝 서비스 — 서버 최초 셋업 스크립트
#
# Usage:
#   ./scripts/init_prod.sh
#
# Runs: venv 생성 → 의존성 설치 → DB 마이그레이션 → 라이선스 키 생성 → 스모크 테스트
# 재실행 안전 (idempotent).

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> 1/6  Checking prerequisites"
command -v uv >/dev/null 2>&1 || {
  echo "ERROR: uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
  exit 1
}
command -v node >/dev/null 2>&1 || {
  echo "WARN: node not found — 프론트엔드는 별도 설치 필요"
}

echo "==> 2/6  Python venv + deps (uv)"
if [ ! -d .venv ]; then
  uv venv --python 3.12
fi
uv pip install -r requirements.txt

echo "==> 3/6  .env check"
if [ ! -f .env ]; then
  cat > .env <<EOF
# LLM 키 (최소 1개 필요 — Gemini 우선, OpenAI 폴백)
GEMINI_KEY=
# PRO_OPENAI_API_KEY=sk-proj-...

PRO_SCREENING_DB_PATH=./data/screening.db
PRO_LICENSE_DB_PATH=./data/licenses.db

# 프로덕션 CORS 제한 (콤마 구분, 비워두면 전체 허용)
# PRO_ALLOWED_ORIGINS=https://your-app.vercel.app
EOF
  chmod 600 .env
  echo "    Created .env — edit it with your API keys and rerun this script."
  exit 1
fi
chmod 600 .env

# Load env
set -a
# shellcheck disable=SC1091
. ./.env
set +a

if [ -z "${GEMINI_KEY:-}" ] && [ -z "${PRO_OPENAI_API_KEY:-}" ]; then
  echo "ERROR: Set at least one of GEMINI_KEY or PRO_OPENAI_API_KEY in .env"
  exit 1
fi

echo "==> 4/6  Data dir + DB migration (startup 시 자동 실행됨, 여기서는 사전 실행)"
mkdir -p data
.venv/bin/python - <<'PY'
from pro_server.settings import pro_settings
from pro_server.migrations import run_migrations, seed_if_empty
from pro_server.auth import init_license_db
import os

os.makedirs(os.path.dirname(pro_settings.license_db_path), exist_ok=True)
os.makedirs(os.path.dirname(pro_settings.screening_db_path), exist_ok=True)

init_license_db(pro_settings.license_db_path)
run_migrations(pro_settings.screening_db_path)
seed_if_empty(pro_settings.screening_db_path,
              pro_settings.factors_seed_path,
              pro_settings.heritage_seed_path)
print("  migrations + seed OK")
PY

echo "==> 5/6  License key"
LICENSE_KEY="${LICENSE_KEY:-PROD-KEY-$(date +%s)}"
.venv/bin/python - <<PY
import sqlite3, os
db = os.environ.get('PRO_LICENSE_DB_PATH', './data/licenses.db')
conn = sqlite3.connect(db)
conn.execute(
    "INSERT OR IGNORE INTO licenses (key, owner, plan, active) VALUES (?, 'prod-user', 'pro', 1)",
    ("$LICENSE_KEY",),
)
conn.commit(); conn.close()
print(f"  license key: $LICENSE_KEY")
PY

echo "==> 6/6  Pytest smoke"
.venv/bin/python -m pytest tests/ -q --tb=short 2>&1 | tail -5

cat <<EOF

============================================================
✅ Setup complete

License key : $LICENSE_KEY
(save this — clients must send it as X-License-Key header)

Start server:
  source .venv/bin/activate
  export \$(grep -v '^#' .env | xargs)
  python -m uvicorn pro_server.main:app --host 0.0.0.0 --port 30005 --reload

Health check:
  curl http://localhost:30005/pro/api/health

Smoke test:
  curl -H "X-License-Key: $LICENSE_KEY" -X POST \\
       -d "mpn=JANSR2N2222AUB" \\
       http://localhost:30005/pro/api/screen-bjt

Frontend:
  cd web && npm install && npm run dev
  open http://localhost:3001/screening
============================================================
EOF
