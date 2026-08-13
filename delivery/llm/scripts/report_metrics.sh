#!/usr/bin/env bash
# Run on the GPU host; posts numeric GPU0/disk/health metrics without model output.
set -euo pipefail
trap 'rm -f /tmp/libertree-llm-metrics.json' EXIT
[[ -n "${DELIVERY_OBSERVATION_URL:-}" && -n "${DELIVERY_API_TOKEN:-}" && -n "${TRANSLATION_INTERNAL_MODEL:-}" ]] || { echo "observation URL/token/model required" >&2;exit 2; }
read -r GPU_FREE_MIB GPU_UTIL <<<"$(nvidia-smi -i 0 --query-gpu=memory.free,utilization.gpu --format=csv,noheader,nounits | tr -d ',' )"
DISK_FREE=$(df -B1 --output=avail "${HF_CACHE_DIR:-/}" | tail -1 | tr -d ' ')
if curl --fail --silent --max-time 3 http://127.0.0.1:${LLM_PROXY_PORT:-8088}/health >/dev/null;then HEALTH=true;else HEALTH=false;fi
python3 - "$GPU_FREE_MIB" "$GPU_UTIL" "$DISK_FREE" "$HEALTH" "${TRANSLATION_INTERNAL_MODEL:-}" > /tmp/libertree-llm-metrics.json <<'PY'
import json,sys
print(json.dumps({"worker_id":"gpu0-observer","gpu_memory_free_bytes":int(sys.argv[1])*1024*1024,
 "gpu_utilization_percent":float(sys.argv[2]),"disk_free_bytes":int(sys.argv[3]),
 "endpoint_healthy":sys.argv[4]=="true","provider":"internal","model_version":sys.argv[5] or None},separators=(",",":")))
PY
chmod 600 /tmp/libertree-llm-metrics.json
curl --fail --silent --show-error -X POST "$DELIVERY_OBSERVATION_URL" \
  -H "x-delivery-token: $DELIVERY_API_TOKEN" -H 'content-type: application/json' \
  --data-binary @/tmp/libertree-llm-metrics.json >/dev/null
echo "GPU0 metrics reported"
