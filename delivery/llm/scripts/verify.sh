#!/usr/bin/env bash
set -Eeuo pipefail

LLM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${LLM_ENV_FILE:-$LLM_DIR/.env}"
[[ -f "$ENV_FILE" ]] || { echo "missing $ENV_FILE" >&2; exit 1; }
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a
base="http://127.0.0.1:${LLM_PROXY_PORT:-8088}"

status="$(curl -sS -o /dev/null -w '%{http_code}' "$base/v1/models")"
[[ "$status" == 401 ]] || { echo "unauthenticated request returned $status, expected 401" >&2; exit 1; }
curl --fail --silent --show-error "$base/health" >/dev/null
curl --fail --silent --show-error -H "Authorization: Bearer $LLM_API_KEY" "$base/v1/models" >/dev/null
free_gb="$(df -Pk "$(dirname "$HF_CACHE_DIR")" | awk 'NR==2 {print int($4/1024/1024)}')"
(( free_gb >= ${MIN_FREE_GB:-100} )) || { echo "only ${free_gb}GB remains; ${MIN_FREE_GB:-100}GB operational margin required" >&2; exit 1; }

response="$(mktemp)"
trap 'rm -f "$response"' EXIT
curl --fail --silent --show-error "$base/v1/chat/completions" \
  -H "Authorization: Bearer $LLM_API_KEY" -H 'Content-Type: application/json' \
  --data "{\"model\":\"$SERVED_MODEL_NAME\",\"temperature\":0,\"max_tokens\":128,\"messages\":[{\"role\":\"system\",\"content\":\"Translate into Korean. Return only the translation.\"},{\"role\":\"user\",\"content\":\"The 2025 budget is available at https://example.org/report.pdf\"}]}" >"$response"
python3 - "$response" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
text = data["choices"][0]["message"]["content"]
for token in ("2025", "https://example.org/report.pdf"):
    if token not in text:
        raise SystemExit(f"translation omitted protected token: {token}")
print("OpenAI-compatible translation check passed.")
PY
nvidia-smi --query-gpu=index,name,memory.used,memory.free,utilization.gpu --format=csv,noheader -i 0
