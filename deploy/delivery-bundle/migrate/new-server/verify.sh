#!/usr/bin/env bash
# [부록: 새 서버로 옮길 때]
# (d) 원본·대상 DB 를 같은 방식으로 세어 CSV 두 개와 diff 를 만듭니다. 두 DB 모두 읽기만 합니다.
#
# 사용: migrate/new-server/verify.sh 원본컨테이너 대상컨테이너 출력폴더
#         [--src-user U] [--src-db D] [--dst-user U] [--dst-db D]
# 출력: 출력폴더/source.csv, target.csv, diff.txt   (일치하면 종료코드 0, 다르면 1)
# CSV 열: kind,table,site_id,value
#   rows       테이블 전체 행 수
#   site_rows  site_id 열이 있는 테이블의 site_id 별 행 수
#   seq_min / seq_max / seq_md5   documents 의 site_id 별 seq_id 최소·최대·전체 목록 md5
#                                 (seq_id 가 하나도 바뀌지 않았음을 확인. blob 파일 연결 키)
#   seq_last   documents.seq_id identity 시퀀스 last_value
# 복원 직후(migrate 실행 전)에 돌리십시오. migrate 는 sites 등 카탈로그 행을 추가할 수 있습니다.
set -eu
set -o pipefail
. "$(dirname "$0")/../lib.sh"

usage() { sed -n '2,13p' "$0" >&2; exit 2; }
SRC=""; DST=""; OUTD=""
SU=libertree; SD=libertree; DU=libertree; DD=libertree
while [ $# -gt 0 ]; do
  case "$1" in
    --src-user) SU=$2; shift 2 ;;
    --src-db) SD=$2; shift 2 ;;
    --dst-user) DU=$2; shift 2 ;;
    --dst-db) DD=$2; shift 2 ;;
    -h|--help) usage ;;
    -*) die "알 수 없는 옵션: $1" ;;
    *) if [ -z "$SRC" ]; then SRC=$1; elif [ -z "$DST" ]; then DST=$1; elif [ -z "$OUTD" ]; then OUTD=$1; else usage; fi; shift ;;
  esac
done
[ -n "$SRC" ] && [ -n "$DST" ] && [ -n "$OUTD" ] || usage
require_running "$SRC"
require_running "$DST"
mkdir -p "$OUTD"
for f in source.csv target.csv diff.txt; do
  [ ! -e "$OUTD/$f" ] || die "$OUTD/$f 이(가) 이미 있습니다. 다른 출력폴더를 지정하십시오."
done

cat >&2 <<PLAN
== 검증 계획 (읽기 전용) ==
  원본 : $SRC  ($SD / $SU)
  대상 : $DST  ($DD / $DU)
  출력 : $OUTD/source.csv, $OUTD/target.csv, $OUTD/diff.txt
  각 테이블 count(*), site_id 별 count, documents seq_id 최소·최대·md5, 시퀀스 값
  (큰 테이블은 count(*) 에 시간이 걸릴 수 있습니다. 원본은 읽기 전용 트랜잭션으로 조회)
PLAN

TABLES_SQL="SELECT format('%I.%I', t.table_schema, t.table_name),
  EXISTS (SELECT 1 FROM information_schema.columns c WHERE c.table_schema=t.table_schema AND c.table_name=t.table_name AND c.column_name='site_id')
FROM information_schema.tables t
WHERE t.table_type='BASE TABLE' AND t.table_schema NOT IN ('pg_catalog','information_schema')
ORDER BY t.table_schema COLLATE \"C\", t.table_name COLLATE \"C\""

snapshot() { # 컨테이너 사용자 DB
  c=$1; u=$2; d=$3
  echo "kind,table,site_id,value"
  pg_sql_ro "$c" "$u" "$d" "$TABLES_SQL" | while IFS="$(printf '\t')" read -r tbl has_site; do
    pg_sql_ro "$c" "$u" "$d" "SELECT 'rows,$tbl,,' || count(*) FROM $tbl"
    if [ "$has_site" = t ]; then
      pg_sql_ro "$c" "$u" "$d" "SELECT x FROM (SELECT 'site_rows,$tbl,' || coalesce(site_id::text,'(null)') || ',' || count(*) AS x FROM $tbl GROUP BY site_id) s ORDER BY x COLLATE \"C\""
    fi
  done
  if [ "$(has_documents "$c" "$u" "$d")" = t ]; then
    pg_sql_ro "$c" "$u" "$d" "SELECT x FROM (
        SELECT site_id, 'seq_min,public.documents,' || site_id || ',' || min(seq_id) AS x FROM documents GROUP BY site_id
        UNION ALL SELECT site_id, 'seq_max,public.documents,' || site_id || ',' || max(seq_id) FROM documents GROUP BY site_id
        UNION ALL SELECT site_id, 'seq_md5,public.documents,' || site_id || ',' || md5(string_agg(seq_id::text, ',' ORDER BY seq_id)) FROM documents GROUP BY site_id
      ) s ORDER BY x COLLATE \"C\""
    pg_sql_ro "$c" "$u" "$d" "SELECT 'seq_last,public.documents,,' || coalesce((SELECT last_value::text FROM pg_sequences WHERE format('%I.%I', schemaname, sequencename) = pg_get_serial_sequence('public.documents','seq_id')), '-')"
  fi
}

log "원본 집계"
snapshot "$SRC" "$SU" "$SD" > "$OUTD/source.csv"
log "대상 집계"
snapshot "$DST" "$DU" "$DD" > "$OUTD/target.csv"

if diff -u "$OUTD/source.csv" "$OUTD/target.csv" > "$OUTD/diff.txt"; then
  echo "(차이 없음)" > "$OUTD/diff.txt"
  log "일치: $(($(wc -l < "$OUTD/source.csv") - 1)) 항목 동일. $OUTD"
  exit 0
fi
log "불일치: $OUTD/diff.txt 를 확인하십시오."
head -40 "$OUTD/diff.txt" >&2
exit 1
