#!/usr/bin/env bash
# (a) 원본 DB 컨테이너에서 pg_dump 로 덤프를 뜹니다. 원본은 읽기만 합니다.
#
# 사용: ./01_dump.sh 원본컨테이너 출력경로 [--jobs N] [-U 사용자] [-d DB] [--yes]
#   --jobs 1(기본): custom 형식(-Fc) 단일 파일로 호스트에 바로 기록
#   --jobs N(>1)  : directory 형식(-Fd) 병렬 덤프. 컨테이너 /tmp 에 만든 뒤
#                   docker cp 로 출력경로(디렉터리)에 꺼내고 컨테이너 쪽은 지웁니다
#                   (컨테이너 쓰기 계층에 덤프 크기만큼 여유 공간이 필요).
# 예: ./01_dump.sh ui-sci-20261013-postgres-1 /srv/backup/libertree-20261020.dump
set -eu
set -o pipefail
. "$(dirname "$0")/lib.sh"

usage() { sed -n '2,10p' "$0" >&2; exit 2; }
SRC=""; OUT=""; JOBS=1; PGU=libertree; PGD=libertree; ASSUME_YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --jobs) JOBS=$2; shift 2 ;;
    -U) PGU=$2; shift 2 ;;
    -d) PGD=$2; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    -h|--help) usage ;;
    -*) die "알 수 없는 옵션: $1" ;;
    *) if [ -z "$SRC" ]; then SRC=$1; elif [ -z "$OUT" ]; then OUT=$1; else usage; fi; shift ;;
  esac
done
[ -n "$SRC" ] && [ -n "$OUT" ] || usage
case "$JOBS" in ''|*[!0-9]*) die "--jobs 는 양의 정수";; esac
[ "$JOBS" -ge 1 ] || die "--jobs 는 1 이상"
[ ! -e "$OUT" ] || die "출력경로가 이미 있습니다: $OUT (덮어쓰지 않습니다)"
[ ! -e "$OUT.partial" ] || die "이전 실패의 잔여물이 있습니다: $OUT.partial (확인 후 지우십시오)"
[ -d "$(dirname "$OUT")" ] || die "출력 디렉터리가 없습니다: $(dirname "$OUT")"

require_running "$SRC"
ver=$(pg_sql_ro "$SRC" "$PGU" "$PGD" "SHOW server_version")
size=$(pg_sql_ro "$SRC" "$PGU" "$PGD" "SELECT pg_size_pretty(pg_database_size(current_database()))")
if [ "$(has_documents "$SRC" "$PGU" "$PGD")" = t ]; then
  docs=$(pg_sql_ro "$SRC" "$PGU" "$PGD" "SELECT count(*)::text || ' 건, seq_id 최대 ' || coalesce(max(seq_id),0) FROM documents")
else
  docs="documents 테이블 없음"
fi
free=$(df -h "$(dirname "$OUT")" | awk 'NR==2{print $4}')
siblings=$(project_siblings "$SRC")

if [ "$JOBS" -gt 1 ]; then FMT="directory(-Fd), 병렬 $JOBS"; else FMT="custom(-Fc) 단일 파일"; fi
cat >&2 <<PLAN
== 덤프 계획 ==
  원본 컨테이너 : $SRC  (PostgreSQL $ver)
  DB / 사용자    : $PGD / $PGU
  DB 크기        : $size
  documents      : $docs
  형식           : $FMT
  출력           : $OUT  (출력 위치 여유 공간 $free)
  원본에는 쓰지 않습니다(pg_dump 는 단일 스냅샷으로 읽기만 함).
PLAN
if [ -n "$siblings" ]; then
  printf '  주의: 같은 compose 프로젝트에서 아직 실행 중인 컨테이너가 있습니다:\n%s\n' "$(printf '%s\n' "$siblings" | sed 's/^/    - /')" >&2
  printf '  덤프 이후에 들어온 쓰기는 새 스택에 옮겨지지 않습니다. 최종 덤프 전에는 be/worker 를 멈추십시오.\n' >&2
fi
confirm

done_ok=0
ctmp=/tmp/libertree-dump-$$
# 실패하든 성공하든 컨테이너 쪽 임시 덤프는 지우고, 실패하면 호스트의 .partial 도 지웁니다.
cleanup() {
  if [ "$JOBS" -gt 1 ]; then docker exec "$SRC" rm -rf "$ctmp" >/dev/null 2>&1 || true; fi
  [ "$done_ok" = 1 ] || rm -rf "$OUT.partial"
}
trap cleanup EXIT
started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if [ "$JOBS" -gt 1 ]; then
  log "병렬 덤프 시작 → 컨테이너 $ctmp"
  docker exec "$SRC" pg_dump -U "$PGU" -d "$PGD" -Fd -j "$JOBS" -f "$ctmp"
  log "docker cp → $OUT"
  docker cp "$SRC:$ctmp" "$OUT.partial"
  [ -f "$OUT.partial/toc.dat" ] || die "덤프 디렉터리에 toc.dat 이 없습니다"
else
  log "덤프 시작 → $OUT.partial"
  docker exec "$SRC" pg_dump -U "$PGU" -d "$PGD" -Fc > "$OUT.partial"
  log "아카이브 목록 확인(pg_restore -l)"
  docker exec -i "$SRC" pg_restore -l < "$OUT.partial" > /dev/null
fi
mv "$OUT.partial" "$OUT"
done_ok=1

{
  echo "source_container=$SRC"
  echo "source_server_version=$ver"
  echo "database=$PGD"
  echo "format=$FMT"
  echo "started_utc=$started"
  echo "finished_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "documents=$docs"
  if [ -f "$OUT" ]; then echo "sha256=$(sha256sum "$OUT" | awk '{print $1}')"; fi
} > "$OUT.meta"
log "완료: $OUT ($(du -sh "$OUT" | awk '{print $1}')), 메타 정보 $OUT.meta"
