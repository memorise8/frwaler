#!/usr/bin/env bash
# [부록: 새 서버로 옮길 때] 01_dump.sh 의 덤프를 새 postgres 컨테이너에 복원하고 ANALYZE 합니다.
# 같은 서버에서 이미지만 바꾸는 표준 절차(ADMIN_GUIDE.md)에서는 쓰지 않습니다.
#
# 사용: migrate/new-server/restore.sh 대상컨테이너 덤프경로 [--jobs N]
#           [--overwrite-db=<대상컨테이너>] [--no-meta-check] [--yes]
#   무조건 거부(우회 옵션 없음):
#     - 대상 postgres 가 호스트 포트를 공개 중(복원 중 외부 접속 차단)
#     - 대상과 같은 compose 프로젝트의 다른 컨테이너(be/worker 등)가 실행 중
#     - 원본 PostgreSQL 메이저 버전 > 대상
#   대상에 이미 테이블·문서가 있으면 거부. --overwrite-db=<대상컨테이너 이름>(정확히 일치)일 때만
#   pg_restore --clean --if-exists --single-transaction 으로 덮어씁니다(이때 --jobs>1 금지).
#   덤프 옆의 .meta(01_dump.sh 가 만듦)가 없으면 거부. --no-meta-check 로만 생략.
set -eu
set -o pipefail
. "$(dirname "$0")/../lib.sh"

usage() { sed -n '2,13p' "$0" >&2; exit 2; }
DST=""; DUMP=""; JOBS=1; PGU=libertree; PGD=libertree; OVERWRITE=""; NOMETA=0; ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --jobs) JOBS=$2; shift 2 ;;
    -U) PGU=$2; shift 2 ;;
    -d) PGD=$2; shift 2 ;;
    --overwrite-db=*) OVERWRITE=${1#--overwrite-db=}; [ -n "$OVERWRITE" ] || die "--overwrite-db= 에 대상 컨테이너 이름이 필요합니다"; shift ;;
    --overwrite-db) die "--overwrite-db=<대상컨테이너 이름> 형식으로 지정하십시오" ;;
    --force) die "--force 는 없어졌습니다. --overwrite-db=<대상컨테이너 이름> 을 쓰십시오." ;;
    --no-meta-check) NOMETA=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage ;;
    -*) die "알 수 없는 옵션: $1" ;;
    *) if [ -z "$DST" ]; then DST=$1; elif [ -z "$DUMP" ]; then DUMP=$1; else usage; fi; shift ;;
  esac
done
[ -n "$DST" ] && [ -n "$DUMP" ] || usage
case "$JOBS" in ''|*[!0-9]*|0) die "--jobs 는 양의 정수";; esac
[ -e "$DUMP" ] || die "덤프가 없습니다: $DUMP"
if [ -d "$DUMP" ]; then
  [ -f "$DUMP/toc.dat" ] || die "디렉터리 형식 덤프가 아닙니다(toc.dat 없음): $DUMP"
  KIND=directory
else
  KIND=custom
fi

# ---- 메타 정보 -----------------------------------------------------------
src_ver=""
if [ -f "$DUMP.meta" ]; then
  src_ver=$(sed -n 's/^source_server_version=//p' "$DUMP.meta")
  want=$(sed -n 's/^sha256=//p' "$DUMP.meta")
  if [ "$KIND" = custom ] && [ -n "$want" ]; then
    log "덤프 sha256 확인"
    [ "$(sha256sum "$DUMP" | awk '{print $1}')" = "$want" ] || die "덤프 sha256 이 $DUMP.meta 와 다릅니다(손상 또는 다른 파일)."
  fi
elif [ "$NOMETA" = 1 ]; then
  log "주의: --no-meta-check: 원본 버전·체크섬을 확인하지 않습니다."
else
  die "$DUMP.meta 가 없습니다(01_dump.sh 로 만든 덤프가 아님?). 출처를 확인했으면 --no-meta-check."
fi

# ---- 대상 확인 -----------------------------------------------------------
require_running "$DST"
[ -z "$OVERWRITE" ] || [ "$OVERWRITE" = "$DST" ] || die "--overwrite-db=$OVERWRITE 가 대상 '$DST' 와 다릅니다. 중단."

# ---- 무조건 거부 조건 ----------------------------------------------------
absolute=""
ports=$(docker port "$DST" 2>/dev/null || true)
[ -z "$ports" ] || absolute="${absolute}  - 대상이 호스트 포트를 공개 중: $(printf '%s' "$ports" | tr '\n' ' ')(포트 없이 다시 기동)\n"
siblings=$(project_siblings "$DST")
[ -z "$siblings" ] || absolute="${absolute}  - 같은 프로젝트에서 실행 중: $(printf '%s' "$siblings" | tr '\n' ' ')(먼저 멈추십시오)\n"
dst_ver=$(pg_sql "$DST" "$PGU" "$PGD" "SHOW server_version")
dst_major=$(server_major "$DST" "$PGU" "$PGD")
if [ -n "$src_ver" ]; then
  src_major=${src_ver%%.*}
  [ "$src_major" -le "$dst_major" ] || absolute="${absolute}  - 원본 PostgreSQL $src_ver 이(가) 대상 $dst_ver 보다 새 버전입니다.\n"
fi
if [ -n "$absolute" ]; then
  printf '복원 불가(우회 옵션 없음):\n%b' "$absolute" >&2
  exit 1
fi

# ---- 기존 데이터 ---------------------------------------------------------
tables=$(pg_sql "$DST" "$PGU" "$PGD" "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')")
docs=0
if [ "$(has_documents "$DST" "$PGU" "$PGD")" = t ]; then
  docs=$(pg_sql "$DST" "$PGU" "$PGD" "SELECT count(*) FROM documents")
fi
CLEAN=""
if [ "$tables" != 0 ] || [ "$docs" != 0 ]; then
  printf '대상 %s 에 이미 사용자 테이블 %s 개, documents %s 건이 있습니다.\n' "$DST" "$tables" "$docs" >&2
  [ "$OVERWRITE" = "$DST" ] || die "덮어쓰려면 --overwrite-db=$DST 를 붙이십시오(대상 DB 의 기존 객체 삭제, 되돌릴 수 없음)."
  [ "$JOBS" = 1 ] || die "--overwrite-db 와 --jobs>1 은 함께 쓸 수 없습니다(--clean 병렬 복원은 원자적이지 않음)."
  CLEAN="--clean --if-exists"
fi

USE_CP=0
if [ "$KIND" = directory ] || [ "$JOBS" -gt 1 ]; then USE_CP=1; fi
OPTS="--no-owner --no-privileges --exit-on-error $CLEAN"
if [ "$JOBS" -gt 1 ]; then OPTS="$OPTS -j $JOBS"; else OPTS="$OPTS --single-transaction"; fi

cat >&2 <<PLAN
== 복원 계획 ==
  덤프           : $DUMP  ($KIND 형식${src_ver:+, 원본 PostgreSQL $src_ver})
  대상 컨테이너  : $DST  (PostgreSQL $dst_ver)
  DB / 사용자    : $PGD / $PGU  (소유자는 $PGU 로 바뀜: --no-owner)
  대상 현재 상태 : 사용자 테이블 $tables 개, documents $docs 건
  pg_restore 옵션: $OPTS
  전달 방식      : $( [ "$USE_CP" = 1 ] && echo "docker cp 로 컨테이너 /tmp 에 복사 후 복원(대상 컨테이너에 덤프 크기만큼 공간 필요)" || echo "표준입력으로 스트리밍")
  이후           : ANALYZE, seq_id/시퀀스 확인. 다음 단계는 new-server/postcheck.sh
PLAN
[ -z "$CLEAN" ] || printf '  경고: 대상의 기존 객체를 삭제하고 복원합니다(--overwrite-db=%s).\n' "$DST" >&2
confirm

if [ "$USE_CP" = 1 ]; then
  ctmp=/tmp/libertree-restore-$$
  trap 'docker exec "$DST" rm -rf "$ctmp" >/dev/null 2>&1 || true' EXIT
  log "docker cp → $DST:$ctmp"
  docker cp "$DUMP" "$DST:$ctmp"
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
log "다음: migrate/new-server/postcheck.sh $DST ; migrate/new-server/verify.sh <원본> $DST <출력폴더>"
