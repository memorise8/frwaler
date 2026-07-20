#!/usr/bin/env bash
# Migrate Docker data-root from /var/lib/docker to /data_raid/docker
# Usage:
#   sudo bash scripts/migrate_docker_to_data_raid.sh
#
# Safety:
#   - Stops docker daemon before copy.
#   - Uses rsync (resumable, preserves owners/perms/xattrs).
#   - Writes daemon.json only after successful copy.
#   - Does NOT delete the original /var/lib/docker.
#     After confirming everything works, you can delete it manually:
#         sudo rm -rf /var/lib/docker.old
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "[!] This script must be run with sudo / as root." >&2
    exit 1
fi

OLD_ROOT="/var/lib/docker"
NEW_ROOT="/data_raid/docker"
BACKUP_ROOT="/var/lib/docker.old"
DAEMON_JSON="/etc/docker/daemon.json"

log() { echo "[migrate-docker] $(date '+%F %T') $*"; }

log "Pre-check disk space"
df -h /data_raid | tail -1

log "Step 1/6 — stopping docker daemon (containers will pause)"
systemctl stop docker.socket docker || true
sleep 2

log "Step 2/6 — preparing target directory ${NEW_ROOT}"
mkdir -p "${NEW_ROOT}"
chmod 0710 "${NEW_ROOT}" || true

log "Step 3/6 — rsync ${OLD_ROOT} -> ${NEW_ROOT} (may take 10-20 min for 40GB)"
rsync -aHAX --info=progress2 "${OLD_ROOT}/" "${NEW_ROOT}/"

log "Step 4/6 — writing ${DAEMON_JSON}"
if [[ -f "${DAEMON_JSON}" ]]; then
    cp -a "${DAEMON_JSON}" "${DAEMON_JSON}.bak.$(date +%s)"
    log "  (existing daemon.json backed up)"
fi
mkdir -p /etc/docker
cat > "${DAEMON_JSON}" <<EOF
{
  "data-root": "${NEW_ROOT}"
}
EOF

log "Step 5/6 — renaming old data-root to ${BACKUP_ROOT} (kept for rollback)"
mv "${OLD_ROOT}" "${BACKUP_ROOT}"

log "Step 6/6 — starting docker daemon"
systemctl start docker
sleep 3

log "Verification:"
docker info | grep -E "Docker Root Dir|Storage Driver"
echo "---"
echo "Containers:"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

cat <<MSG

============================================================
  DONE. New data-root: ${NEW_ROOT}
  Original kept at:    ${BACKUP_ROOT}
  Disk reclaim:        run \`sudo rm -rf ${BACKUP_ROOT}\`
                       only after confirming containers work.
============================================================
MSG
