#!/usr/bin/env bash
set -Eeuo pipefail

LLM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${LLM_ENV_FILE:-$LLM_DIR/.env}"
COMPOSE=(docker compose --env-file "$ENV_FILE" -f "$LLM_DIR/compose.yml")

die() { echo "ERROR: $*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "$1 is required"; }

[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE (copy .env.example and set a secret token)"
env_mode="$(stat -c '%a' "$ENV_FILE")"
(( (8#$env_mode & 8#077) == 0 )) || die "$ENV_FILE must not be readable by group/others (run chmod 600)"
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

need docker
need nvidia-smi
[[ "${GPU_DEVICE_ID:-}" == "0" ]] || die "L1 is restricted to GPU_DEVICE_ID=0"
[[ -n "${LLM_API_KEY:-}" && "$LLM_API_KEY" != replace-* && ${#LLM_API_KEY} -ge 32 ]] || die "LLM_API_KEY must be a non-example token of at least 32 characters"
[[ "${LLM_PROXY_PORT:-8088}" != "9200" && "${VLLM_LOOPBACK_PORT:-8000}" != "9200" ]] || die "port 9200 is reserved for Elasticsearch"
docker info >/dev/null 2>&1 || die "Docker daemon is not available"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is required"
nvidia-smi --query-gpu=index,name,memory.free --format=csv,noheader | awk -F, '$1+0 == 0 {found=1} END {exit !found}' || die "GPU 0 is not available"

for port in "${LLM_PROXY_PORT:-8088}" "${VLLM_LOOPBACK_PORT:-8000}"; do
  if ss -H -ltn "sport = :$port" | grep -q .; then
    die "TCP port $port is already in use"
  fi
done

cache_parent="$(dirname "${HF_CACHE_DIR:?set HF_CACHE_DIR}")"
mkdir -p "$HF_CACHE_DIR" "${VLLM_CACHE_DIR:?set VLLM_CACHE_DIR}" "${LLM_LOG_DIR:?set LLM_LOG_DIR}"
free_gb="$(df -Pk "$cache_parent" | awk 'NR==2 {print int($4/1024/1024)}')"
required_gb=$(( ${MIN_FREE_GB:-100} + ${MODEL_DOWNLOAD_BUDGET_GB:-50} ))
(( free_gb >= required_gb )) || die "only ${free_gb}GB free at $cache_parent; ${required_gb}GB required before download to preserve ${MIN_FREE_GB:-100}GB"

"${COMPOSE[@]}" config --quiet
echo "Preflight passed: GPU 0, ports, Docker, and ${free_gb}GB free (${required_gb}GB required before download)."
"${COMPOSE[@]}" pull
"${COMPOSE[@]}" up -d vllm-gpu0 proxy
echo "Started L1. Model download/startup can take many minutes. Run scripts/verify.sh when healthy."
