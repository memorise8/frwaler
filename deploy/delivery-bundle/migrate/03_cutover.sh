#!/usr/bin/env bash
# (3) 컷오버: 운영 앱 컨테이너를 멈추고(지우지 않음) 같은 DB 로 v2 를 띄웁니다.
#
# 사용: migrate/03_cutover.sh [--env-file 파일] [--sample N] [--yes]
#   전제: 00_preflight.sh 통과, docker compose --profile tools build 완료, 01_dump.sh 백업 완료.
#   순서
#     1. 기록(00_preflight)의 운영 postgres 가 같은 컨테이너 ID 로 실행 중인지 확인
#     2. 02_blobcheck.sh (운영 DB ↔ blob, 실패하면 아무것도 멈추지 않고 중단)
#     3. 기록의 운영 be/fe/worker/file-worker/runner 중 지금 실행 중인 것만 docker stop
#        멈춘 ID 는 먼저 migrate/.state/cutover-<v2프로젝트>.stopped 에 적은 뒤 멈춤
#        (운영 postgres 는 멈추지 않음. rm/down 은 하지 않음)
#     4. docker compose run --rm migrate  (추가형 스키마 변경, ADMIN_GUIDE.md 4장)
#     5. v2 be → worker/file-worker → fe → runner 기동(healthy 대기)
#     6. 운영 postgres 컨테이너 ID 가 그대로인지 다시 확인
#   3단계 이후 실패하면 migrate/04_rollback.sh 로 되돌립니다.
set -eu
set -o pipefail
. "$(dirname "$0")/lib.sh"

usage() { sed -n '2,16p' "$0" >&2; exit 2; }
ENV_FILE=""; ASSUME_YES=0; SAMPLE=200
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE=$2; shift 2 ;;
    --sample) SAMPLE=$2; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage ;;
    *) die "알 수 없는 인자: $1" ;;
  esac
done

P=$(project_name)
[ -n "$P" ] || die "번들 compose 설정을 읽을 수 없습니다(.env 확인)."
REC=$(prod_record "$P")
[ -f "$REC" ] || die "운영 컨테이너 기록 $REC 이 없습니다. migrate/00_preflight.sh 를 먼저 실행하십시오."
STOPPED="$STATE_DIR/cutover-$P.stopped"
[ ! -e "$STOPPED" ] || die "이미 컷오버된 상태입니다($STOPPED 있음). 되돌리려면 migrate/04_rollback.sh."

pg_id=$(prod_field "$REC" postgres 2)
pg_name=$(prod_field "$REC" postgres 3)
[ "$pg_name" = "$(prod_db_host)" ] || die "기록의 운영 postgres '$pg_name' ≠ 현재 설정 PROD_DB_HOST '$(prod_db_host)'"
now_id=$(docker inspect -f '{{.Id}}' "$pg_name" 2>/dev/null) || die "운영 postgres '$pg_name' 가 없습니다."
[ "$now_id" = "$pg_id" ] || die "운영 postgres 컨테이너 ID 가 기록과 다릅니다(재생성됨?). 00_preflight.sh 부터 다시."
[ "$(docker inspect -f '{{.State.Running}}' "$pg_name")" = true ] || die "운영 postgres 가 실행 중이 아닙니다."

for img in $(dc --profile tools config --images | sort -u); do
  case "$img" in postgres:*) continue ;; esac
  docker image inspect "$img" >/dev/null 2>&1 || die "이미지 $img 가 없습니다. 먼저: docker compose --profile tools build"
done

to_stop=""
while IFS="$(printf '\t')" read -r svc id name _ ref _; do
  [ "$svc" != postgres ] || continue
  st=$(docker inspect -f '{{.State.Status}}' "$id" 2>/dev/null) || die "기록된 운영 컨테이너 $name($id) 가 없습니다. 00_preflight.sh 부터 다시."
  if [ "$st" = running ]; then to_stop="$to_stop$svc	$id	$name	$ref
"; fi
done < "$REC"

busy=$(pg_sql_ro "$pg_name" libertree libertree \
  "SELECT CASE WHEN to_regclass('public.crawl_jobs') IS NULL THEN 0 ELSE (SELECT count(*) FROM crawl_jobs WHERE status IN ('running','cancelling')) END" 2>/dev/null || echo '?')

cat >&2 <<PLAN
== 컷오버 계획 ==
  실행 중 수집 작업: $busy 건 (멈추면 중단되고 재시작 후 재시도/재대기됨. 가능하면 0 일 때 진행)
  v2 프로젝트  : $P
  운영 postgres: $pg_name (${pg_id:0:12}) — 멈추지 않음, 그대로 사용
  1) blob 게이트 (02_blobcheck.sh --sample $SAMPLE) — 실패 시 여기서 중단(아무것도 안 멈춤)
  2) docker stop (기록 → $STOPPED):
$(printf '%s' "$to_stop" | awk -F '\t' 'NF {printf "       %-12s %s (%s)\n", $1, $3, substr($2, 1, 12)}')
  3) docker compose run --rm migrate (추가형 스키마 변경)
  4) v2 기동: be → worker file-worker → fe → runner
  되돌리기: migrate/04_rollback.sh (v2 stop → 위 ID 만 docker start)
PLAN
[ -n "$to_stop" ] || log "주의: 실행 중인 운영 앱 컨테이너가 없습니다."
confirm

log "1) blob 게이트"
ENV_FILE="$ENV_FILE" "$LIB_DIR/02_blobcheck.sh" ${ENV_FILE:+--env-file "$ENV_FILE"} --sample "$SAMPLE" --container "$pg_name" \
  || die "blob 게이트 실패: 아무것도 멈추지 않았습니다."

log "2) 운영 앱 컨테이너 정지"
mkdir -p "$STATE_DIR"
printf '%s' "$to_stop" > "$STOPPED"
printf '%s' "$to_stop" | awk -F '\t' 'NF {print $2}' | while read -r id; do
  docker stop -t 60 "$id" >/dev/null
  log "   stopped $(docker inspect -f '{{slice .Name 1}}' "$id")"
done

fail_hint() { die "$1 — 되돌리려면: migrate/04_rollback.sh${ENV_FILE:+ --env-file $ENV_FILE}"; }

log "3) 스키마 migrate"
dc run --rm migrate || fail_hint "migrate 실패"

log "4) v2 기동"
dc up -d --wait be || fail_hint "be 기동 실패"
dc up -d --wait worker file-worker || fail_hint "worker/file-worker 기동 실패"
dc up -d --wait fe || fail_hint "fe 기동 실패"
dc up -d runner || fail_hint "runner 기동 실패"

[ "$(docker inspect -f '{{.Id}}' "$pg_name")" = "$pg_id" ] || die "경고: 운영 postgres 컨테이너 ID 가 바뀌었습니다!"
log "운영 postgres 컨테이너 ID 그대로: ${pg_id:0:12}"
dc ps --format 'table {{.Name}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' >&2
log "컷오버 완료. ADMIN_GUIDE.md 5장 점검을 하십시오."
