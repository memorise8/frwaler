#!/usr/bin/env bash
# (b) 01_dump.sh 의 덤프를 새 스택 postgres 에 복원하고 ANALYZE 합니다.
#
# 사용: ./02_restore.sh 대상컨테이너 덤프경로 [--jobs N] [-U 사용자] [-d DB] [--force] [--yes]
#   대상 DB 에 이미 documents 행(또는 다른 테이블)이 있거나, 같은 compose 프로젝트의
#   다른 컨테이너(be/worker 등)가 실행 중이면 거부합니다. --force 는 이를 무시하고
#   pg_restore --clean --if-exists 로 기존 객체를 지운 뒤 복원합니다(되돌릴 수 없음).
#   --jobs N(>1) 이거나 덤프가 디렉터리 형식이면 덤프를 컨테이너 /tmp 로 복사해 복원합니다.
# 예: ./02_restore.sh libertree-delivery-postgres-1 /srv/backup/libertree-20261020.dump
set -eu
set -o pipefail
. "$(dirname "$0")/lib.sh"

usage() { sed -n '2,10p' "$0" >&2; exit 2; }
DST=""; DUMP=""; JOBS=1; PGU=libertree; PGD=libertree; FORCE=0; ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --jobs) JOBS=$2; shift 2 ;;
    -U) PGU=$2; shift 2 ;;
    -d) PGD=$2; shift 2 ;;
    --force) FORCE=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage ;;
    -*) die "알 수 없는 옵션: $1" ;;
    *) if [ -z "$DST" ]; then DST=$1; elif [ -z "$DUMP" ]; then DUMP=$1; else usage; fi; shift ;;
  esac
done
[ -n "$DST" ] && [ -n "$DUMP" ] || usage
case "$JOBS" in ''|*[!0-9]*) die "--jobs 는 양의 정수";; esac
[ -e "$DUMP" ] || die "덤프가 없습니다: $DUMP"
if [ -d "$DUMP" ]; then
  [ -f "$DUMP/toc.dat" ] || die "디렉터리 형식 덤프가 아닙니다(toc.dat 없음): $DUMP"
  KIND=directory
else
  KIND=custom
fi

require_running "$DST"
dst_ver=$(pg_sql "$DST" "$PGU" "$PGD" "SHOW server_version")
dst_major=$(server_major "$DST" "$PGU" "$PGD")
src_ver=$(sed -n 's/^source_server_version=//p' "$DUMP.meta" 2>/dev/null || true)
if [ -n "$src_ver" ]; then
  src_major=${src_ver%%.*}
  [ "$src_major" -le "$dst_major" ] || die "원본 PostgreSQL $src_ver 이(가) 대상 $dst_ver 보다 새 버전입니다. 같은 이상 버전의 대상이 필요합니다."
fi
tables=$(pg_sql "$DST" "$PGU" "$PGD" "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')")
docs=0
if [ "$(has_documents "$DST" "$PGU" "$PGD")" = t ]; then
  docs=$(pg_sql "$DST" "$PGU" "$PGD" "SELECT count(*) FROM documents")
fi
siblings=$(project_siblings "$DST")

CLEAN=""
blockers=""
[ "$docs" = 0 ] || blockers="${blockers}  - 대상 DB documents 에 이미 $docs 건이 있습니다.\n"
[ "$tables" = 0 ] || blockers="${blockers}  - 대상 DB 에 사용자 테이블 $tables 개가 있습니다(migrate 를 먼저 돌렸거나 이전 복원 잔여물).\n"
[ -z "$siblings" ] || blockers="${blockers}  - 같은 프로젝트에서 실행 중: $(printf '%s' "$siblings" | tr '\n' ' ')(be/worker 를 먼저 멈추십시오)\n"
if [ -n "$blockers" ]; then
  printf '복원을 막는 조건:\n%b' "$blockers" >&2
  [ "$FORCE" = 1 ] || die "중단합니다. 내용을 확인했고 대상 DB 를 덮어써도 되면 --force 를 붙이십시오."
  CLEAN="--clean --if-exists"
fi

USE_CP=0
if [ "$KIND" = directory ] || [ "$JOBS" -gt 1 ]; then USE_CP=1; fi
OPTS="--no-owner --no-privileges --exit-on-error $CLEAN"
if [ "$JOBS" -gt 1 ]; then OPTS="$OPTS -j $JOBS"; else OPTS="$OPTS --single-transaction"; fi

cat >&2 <<PLAN
== 복원 계획 ==
  덤프           : $DUMP  ($KIND 형식$( [ -n "$src_ver" ] && printf ', 원본 PostgreSQL %s' "$src_ver"))
  대상 컨테이너  : $DST  (PostgreSQL $dst_ver)
  DB / 사용자    : $PGD / $PGU  (소유자는 $PGU 로 바뀜: --no-owner)
  대상 현재 상태 : 사용자 테이블 $tables 개, documents $docs 건
  pg_restore 옵션: $OPTS
  전달 방식      : $( [ "$USE_CP" = 1 ] && echo "docker cp 로 컨테이너 /tmp 에 복사 후 복원(대상 컨테이너에 덤프 크기만큼 공간 필요)" || echo "표준입력으로 스트리밍")
  이후           : ANALYZE, seq_id/시퀀스 확인. 다음 단계는 03_postcheck.sh
PLAN
[ -z "$CLEAN" ] || printf '  경고: --force 로 대상의 기존 객체를 삭제하고 복원합니다.\n' >&2
confirm

if [ "$USE_CP" = 1 ]; then
  ctmp=/tmp/libertree-restore-$$
  log "docker cp → $DST:$ctmp"
  docker cp "$DUMP" "$DST:$ctmp"
  trap 'docker exec "$DST" rm -rf "$ctmp" >/dev/null 2>&1 || true' EXIT
  log "pg_restore 시작"
  # shellcheck disable=SC2086
  docker exec "$DST" pg_restore -U "$PGU" -d "$PGD" $OPTS "$ctmp"
else
  log "pg_restore 시작(스트리밍)"
  # shellcheck disable=SC2086
  docker exec -i "$DST" pg_restore -U "$PGU" -d "$PGD" $OPTS < "$DUMP"
fi

log "ANALYZE"
pg_sql "$DST" "$PGU" "$PGD" "ANALYZE"

seq="documents 없음"; seqv=""
if [ "$(has_documents "$DST" "$PGU" "$PGD")" = t ]; then
  seq=$(pg_sql "$DST" "$PGU" "$PGD" "SELECT 'documents ' || count(*) || ' 건, seq_id ' || coalesce(min(seq_id),0) || '..' || coalesce(max(seq_id),0) FROM documents")
  seqv=$(pg_sql "$DST" "$PGU" "$PGD" "SELECT last_value FROM pg_sequences WHERE format('%I.%I', schemaname, sequencename) = pg_get_serial_sequence('public.documents','seq_id')")
fi
log "복원 완료: $seq, seq_id 시퀀스 last_value=${seqv:-?}"
log "다음: ./03_postcheck.sh $DST  그리고 ./04_verify.sh 원본컨테이너 $DST 출력폴더"
