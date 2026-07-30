#!/usr/bin/env bash
# Expose the local libertree-app (dev server) via the existing Cloudflare
# tunnel (fino-tunnel) by adding one ingress rule to /etc/cloudflared/config.yml.
#
# Safe by design:
#   - backs up the config (timestamped) before touching it
#   - idempotent: skips if the hostname rule already exists
#   - validates the new config BEFORE reloading; auto-restores on failure
#   - uses `systemctl reload` (SIGHUP) so other tunnel routes stay up
#
# Usage (run in a real terminal so sudo can prompt):
#   bash scripts/expose_libertree_tunnel.sh
#   bash scripts/expose_libertree_tunnel.sh libertree.financenow.kr 3002
#
# Rollback:
#   bash scripts/expose_libertree_tunnel.sh --rollback

set -euo pipefail

CONFIG="/etc/cloudflared/config.yml"
TUNNEL="fino-tunnel"
HOST="${1:-libertree.financenow.kr}"
PORT="${2:-3002}"

# Re-exec under sudo if not root (single password prompt).
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  exec sudo -- bash "$0" "$@"
fi

# ---- rollback mode -------------------------------------------------------
if [ "${1:-}" = "--rollback" ]; then
  latest_bak="$(ls -1t "${CONFIG}".bak.* 2>/dev/null | head -1 || true)"
  if [ -z "${latest_bak}" ]; then
    echo "[rollback] no backup found (${CONFIG}.bak.*)"; exit 1
  fi
  echo "[rollback] restoring ${latest_bak} -> ${CONFIG}"
  cp -- "${latest_bak}" "${CONFIG}"
  systemctl reload cloudflared
  echo "[rollback] done, cloudflared reloaded"
  exit 0
fi

# ---- preflight -----------------------------------------------------------
if [ ! -f "${CONFIG}" ]; then
  echo "[error] ${CONFIG} not found"; exit 1
fi

backup=""
if grep -q "hostname: ${HOST}" "${CONFIG}"; then
  echo "[info] '${HOST}' already present in ${CONFIG} — skipping edit, will (re)load to apply it."
else
  if ! grep -qE "^\s*-\s*service:\s*http_status:404" "${CONFIG}"; then
    echo "[error] catch-all 'service: http_status:404' rule not found — aborting to avoid a bad edit."
    exit 1
  fi

  # ---- backup ------------------------------------------------------------
  ts="$(date +%Y%m%d_%H%M%S)"
  backup="${CONFIG}.bak.${ts}"
  cp -- "${CONFIG}" "${backup}"
  echo "[backup] ${backup}"

  # ---- insert ingress rule before the catch-all --------------------------
  # Insert two lines directly above the first 'http_status:404' line.
  sed -i "s#^\(\s*\)-\s*service:\s*http_status:404#\1- hostname: ${HOST}\n\1  service: http://localhost:${PORT}\n\1- service: http_status:404#" "${CONFIG}"
  echo "[edit] added: ${HOST} -> http://localhost:${PORT}"
fi

# ---- validate; auto-restore on failure -----------------------------------
if ! cloudflared --config "${CONFIG}" tunnel ingress validate; then
  echo "[error] validation FAILED"
  if [ -n "${backup}" ]; then echo "[restore] ${backup} -> ${CONFIG}"; cp -- "${backup}" "${CONFIG}"; fi
  exit 1
fi
echo "[ok] config validated"

# ---- ensure DNS route (hostname -> this tunnel) --------------------------
# Without an explicit CNAME to <tunnel>.cfargotunnel.com the hostname falls
# through to any wildcard record and returns 521. cert.pem-based (not sudo):
# since we re-exec as root, point --origincert at the invoking user's cert.
invoker_home="$(getent passwd "${SUDO_USER:-root}" | cut -d: -f6)"
origin_cert="${invoker_home}/.cloudflared/cert.pem"
if [ -f "${origin_cert}" ]; then
  route_out="$(cloudflared tunnel --origincert "${origin_cert}" route dns "${TUNNEL}" "${HOST}" 2>&1 || true)"
  if echo "${route_out}" | grep -qiE "Added CNAME|already (exists|configured)|record with the same"; then
    echo "[dns] route ensured: ${HOST} -> ${TUNNEL}"
  else
    echo "[dns] note: ${route_out}"
  fi
else
  echo "[dns] skip: ${origin_cert} not found — ensure the CNAME manually:"
  echo "      cloudflared tunnel route dns ${TUNNEL} ${HOST}"
fi

# ---- reload / start ------------------------------------------------------
if systemctl is-active --quiet cloudflared; then
  systemctl reload cloudflared && echo "[reload] cloudflared reloaded (SIGHUP)"
else
  echo "[warn] cloudflared service is NOT active — starting it (this restores ALL tunnel routes)"
  systemctl start cloudflared
  sleep 1
  if systemctl is-active --quiet cloudflared; then
    echo "[start] cloudflared started"
  else
    echo "[error] failed to start cloudflared — showing recent logs:"
    journalctl -u cloudflared -n 25 --no-pager || true
    exit 1
  fi
fi

# ---- verify --------------------------------------------------------------
sleep 2
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://${HOST}/" || echo 000)"
echo "----- result -----"
echo "https://${HOST}/  ->  HTTP ${code}"
case "${code}" in
  401) echo "[success] tunnel up + app reached (401 = Basic Auth gate). Log in with admin creds." ;;
  200) echo "[success] tunnel up + app served 200." ;;
  521|522|523) echo "[warn] ${code}: tunnel route ok but origin (localhost:${PORT}) not responding — is the app running?" ;;
  000) echo "[warn] no response yet — DNS/edge may need a few seconds; retry the curl." ;;
  *)   echo "[info] got HTTP ${code} — check app/tunnel logs." ;;
esac
echo "Rollback if needed:  bash scripts/expose_libertree_tunnel.sh --rollback"
