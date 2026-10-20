#!/usr/bin/env bash
# (c) 복원 후 정렬 규칙(collation) 버전 확인, 필요하면 REINDEX DATABASE 와
#     ALTER DATABASE ... REFRESH COLLATION VERSION 을 실행합니다.
#
# 사용: ./03_postcheck.sh 대상컨테이너 [-U 사용자] [-d DB] [--reindex] [--yes]
#   불일치가 없으면 아무것도 바꾸지 않고 보고만 합니다.
#   --reindex : 불일치가 없어도 REINDEX DATABASE 를 실행합니다.
# 언제 필요한가: README.md "정렬 규칙 버전" 참고. pg_dump/pg_restore 로 옮기면 인덱스는
# 새 서버에서 다시 만들어지므로 보통 불일치가 없습니다. 기존 데이터 디렉터리(볼륨)를
# glibc 버전이 다른 이미지로 그대로 띄웠을 때 불일치가 생깁니다.
set -eu
set -o pipefail
. "$(dirname "$0")/lib.sh"

usage() { sed -n '2,10p' "$0" >&2; exit 2; }
DST=""; PGU=libertree; PGD=libertree; REINDEX=0; ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    -U) PGU=$2; shift 2 ;;
    -d) PGD=$2; shift 2 ;;
    --reindex) REINDEX=1; shift ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage ;;
    -*) die "알 수 없는 옵션: $1" ;;
    *) [ -z "$DST" ] || usage; DST=$1; shift ;;
  esac
done
[ -n "$DST" ] || usage
require_running "$DST"

ver=$(pg_sql "$DST" "$PGU" "$PGD" "SHOW server_version")
glibc=$(docker exec "$DST" sh -c 'ldd --version 2>/dev/null | head -1' || true)
log "대상: $DST PostgreSQL $ver / $glibc"

echo "== 데이터베이스별 정렬 규칙 버전 (기록값 / 현재 라이브러리 값) ==" >&2
pg_sql "$DST" "$PGU" "$PGD" "SELECT datname, datcollate, coalesce(datcollversion,'-'), coalesce(pg_database_collation_actual_version(oid),'-') FROM pg_database WHERE datallowconn ORDER BY datname" \
  | awk -F '\t' '{ flag = ($3 != $4) ? "  <-- 불일치" : ""; printf "  %-20s %-14s %s / %s%s\n", $1, $2, $3, $4, flag }' >&2

db_mismatch=$(pg_sql "$DST" "$PGU" "$PGD" "SELECT count(*) FROM pg_database WHERE datname = current_database() AND datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid)")
other_mismatch=$(pg_sql "$DST" "$PGU" "$PGD" "SELECT string_agg(datname, ' ') FROM pg_database WHERE datallowconn AND datname <> current_database() AND datcollversion IS DISTINCT FROM pg_database_collation_actual_version(oid)")
coll_mismatch=$(pg_sql "$DST" "$PGU" "$PGD" "SELECT coalesce(string_agg(collname, ' '), '') FROM pg_collation WHERE collversion IS NOT NULL AND collversion IS DISTINCT FROM pg_collation_actual_version(oid)")
[ -z "$coll_mismatch" ] || log "개별 collation 불일치: $coll_mismatch"
[ -z "$other_mismatch" ] || log "다른 DB 불일치(이 스크립트는 -d 로 지정한 DB 만 처리): $other_mismatch"

if [ "$db_mismatch" = 0 ] && [ -z "$coll_mismatch" ] && [ "$REINDEX" = 0 ]; then
  log "정렬 규칙 버전 일치: REINDEX/REFRESH 불필요. 변경 없음."
  exit 0
fi

cat >&2 <<PLAN
== 실행 계획 ==
  1. REINDEX DATABASE "$PGD"   (모든 인덱스 재생성, 테이블 잠금 발생 — be/worker 를 멈춘 상태에서)
  2. ALTER DATABASE "$PGD" REFRESH COLLATION VERSION
$( [ -n "$coll_mismatch" ] && printf '  3. ALTER COLLATION ... REFRESH VERSION : %s\n' "$coll_mismatch")
PLAN
confirm

log "REINDEX DATABASE 시작"
docker exec "$DST" psql -X -v ON_ERROR_STOP=1 -U "$PGU" -d "$PGD" -c "REINDEX DATABASE \"$PGD\""
pg_sql "$DST" "$PGU" "$PGD" "ALTER DATABASE \"$PGD\" REFRESH COLLATION VERSION"
for c in $coll_mismatch; do
  pg_sql "$DST" "$PGU" "$PGD" "ALTER COLLATION \"$c\" REFRESH VERSION"
done
log "완료. 다시 확인:"
pg_sql "$DST" "$PGU" "$PGD" "SELECT datname, coalesce(datcollversion,'-'), coalesce(pg_database_collation_actual_version(oid),'-') FROM pg_database WHERE datname = current_database()" >&2
