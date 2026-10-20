#!/usr/bin/env bash
# (4) 되돌리기: v2 를 멈추고(지우지 않음) 컷오버 때 멈춘 운영 컨테이너 ID 만 다시 시작합니다.
#
# 사용: migrate/04_rollback.sh [--env-file 파일] [--yes]
#   migrate/.state/cutover-<v2프로젝트>.stopped 에 적힌 ID 만 docker start 합니다.
#   운영 postgres 는 컷오버 중에도 멈추지 않았으므로 건드리지 않습니다.
#   스키마 migrate 로 추가된 열·테이블은 그대로 남습니다(추가형이라 이전 코드가 무시).
#   v2 가 그동안 새로 만든 문서·파일도 같은 DB·blob 에 그대로 있습니다.
set -eu
set -o pipefail
. "$(dirname "$0")/lib.sh"

usage() { sed -n '2,9p' "$0" >&2; exit 2; }
ENV_FILE=""; ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE=$2; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage ;;
    *) die "알 수 없는 인자: $1" ;;
  esac
done

P=$(project_name)
[ -n "$P" ] || die "번들 compose 설정을 읽을 수 없습니다(.env 확인)."
STOPPED="$STATE_DIR/cutover-$P.stopped"
REC=$(prod_record "$P")
[ -f "$STOPPED" ] || die "$STOPPED 이 없습니다(컷오버 기록 없음)."
pg_id=$(prod_field "$REC" postgres 2)
pg_name=$(prod_field "$REC" postgres 3)

running=$(dc ps --format '{{.Name}}' 2>/dev/null | tr '\n' ' ')
cat >&2 <<PLAN
== 되돌리기 계획 ==
  1) v2 정지(docker compose stop, 컨테이너·볼륨 유지): ${running:-(실행 중 없음)}
  2) docker start (컷오버 때 멈춘 ID):
$(awk -F '\t' 'NF {printf "       %-12s %s (%s)\n", $1, $3, substr($2, 1, 12)}' "$STOPPED")
  운영 postgres $pg_name 은 건드리지 않음
PLAN
confirm

log "1) v2 정지"
dc stop
log "2) 운영 컨테이너 시작"
awk -F '\t' 'NF {print $2}' "$STOPPED" | while read -r id; do
  docker start "$id" >/dev/null
  log "   started $(docker inspect -f '{{slice .Name 1}}' "$id")"
done
[ "$(docker inspect -f '{{.Id}}' "$pg_name")" = "$pg_id" ] || log "경고: 운영 postgres 컨테이너 ID 가 기록과 다릅니다!"
mv "$STOPPED" "$STOPPED.rolledback-$(date +%Y%m%d-%H%M%S)"
awk -F '\t' 'NF {print $2}' "$REC" | xargs docker inspect -f '{{slice .Name 1}}{{"\t"}}{{.State.Status}}{{"\t"}}{{.Config.Image}}' >&2
log "되돌리기 완료. 다시 컷오버하려면 migrate/03_cutover.sh"
