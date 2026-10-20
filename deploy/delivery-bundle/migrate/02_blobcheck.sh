#!/usr/bin/env bash
# (2) blob 게이트: 운영 DB 와 blob 디렉터리가 같은 seq_id 공간인지 확인합니다.
#     새 worker/file-worker 를 켜기 전에 반드시 통과해야 합니다(03_cutover.sh 가 자동 실행).
#     DB·blob 모두 읽기만 합니다. ui-sci 문서를 합친 뒤에도 다시 돌리십시오(합친 문서 중
#     파일이 없는데 pdf_downloaded/text_extracted=1 인 행을 잡아냄).
#
# 사용: migrate/02_blobcheck.sh [--env-file 파일] [--sample N] [--container 이름] [--blob-dir 경로]
#                               [-U 사용자] [-d DB]
#   기본 대상은 운영 postgres(compose 설정의 PROD_DB_HOST), blob 경로는 compose 설정의
#   be /data/blob bind(BLOB_HOST_PATH)입니다.
#   1) pdf_downloaded=1 또는 text_extracted=1 인 행을 무작위 N 건(기본 200) + seq_id 상위 20 건
#      뽑아 파일 존재(.pdf/.txt)와 pdf_size_bytes(값이 있을 때) 일치를 확인
#   2) blob 디렉터리의 가장 큰 seq_id 파일이 documents.seq_id 시퀀스 last_value 이하인지 확인
#      (초과하면 새 문서가 이미 파일이 있는 번호를 받게 됨)
#   하나라도 어긋나면 종료코드 1. blob 디렉터리가 읽기 권한이 없으면 sudo 로 실행하십시오.
# 파일 위치: <blob>/<seq12[0:4]>/<seq12[4:8]>/<seq12>.pdf|.txt  (crawler/blob_storage.py)
set -eu
set -o pipefail
. "$(dirname "$0")/lib.sh"

usage() { sed -n '2,19p' "$0" >&2; exit 2; }
ENV_FILE=""; N=200; DST=""; BLOB=""; PGU=libertree; PGD=libertree
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE=$2; shift 2 ;;
    --sample) N=$2; shift 2 ;;
    --container) DST=$2; shift 2 ;;
    --blob-dir) BLOB=$2; shift 2 ;;
    -U) PGU=$2; shift 2 ;;
    -d) PGD=$2; shift 2 ;;
    -h|--help) usage ;;
    *) die "알 수 없는 인자: $1" ;;
  esac
done
case "$N" in ''|*[!0-9]*) die "--sample 은 정수";; esac
[ -n "$DST" ] || DST=$(prod_db_host)
require_running "$DST"
if [ -z "$BLOB" ]; then
  BLOB=$(cfg_facts | awk '$1=="BIND" && $2=="be" && $4=="/data/blob" {print $3}')
fi
[ -n "$BLOB" ] && [ -d "$BLOB" ] || die "blob 디렉터리를 찾을 수 없습니다: '${BLOB}'"
[ -r "$BLOB" ] && [ -x "$BLOB" ] || die "blob 디렉터리를 읽을 수 없습니다: $BLOB (sudo 로 실행)"
[ "$(has_documents "$DST" "$PGU" "$PGD")" = t ] || die "$DST 에 documents 테이블이 없습니다(복원 전?)."

blob_path() { # seq_id 확장자
  name=$(printf '%012d' "$1")
  printf '%s/%s/%s/%s.%s' "$BLOB" "${name:0:4}" "${name:4:4}" "$name" "$2"
}

flag_total=$(pg_sql_ro "$DST" "$PGU" "$PGD" "SELECT count(*) FILTER (WHERE pdf_downloaded=1) || ' / ' || count(*) FILTER (WHERE text_extracted=1) FROM documents")
last=$(pg_sql_ro "$DST" "$PGU" "$PGD" "SELECT coalesce(last_value, 0) FROM pg_sequences WHERE format('%I.%I', schemaname, sequencename) = pg_get_serial_sequence('public.documents','seq_id')")
last=${last:-0}
cat >&2 <<PLAN
== blob 게이트 (읽기 전용) ==
  DB    : $DST ($PGD)   pdf_downloaded / text_extracted = $flag_total
  blob  : $BLOB
  표본  : 무작위 $N 건 + seq_id 상위 20 건
  시퀀스: documents.seq_id last_value = $last
PLAN

checked=0; miss_pdf=0; miss_txt=0; bad_size=0; examples=""
note() { [ "$(printf '%b' "$examples" | wc -l)" -ge 20 ] || examples="${examples}    $*\n"; }
rows=$(pg_sql_ro "$DST" "$PGU" "$PGD" "SELECT DISTINCT seq_id, pdf_downloaded, text_extracted, coalesce(pdf_size_bytes::text,'') FROM (
    (SELECT * FROM documents WHERE pdf_downloaded=1 OR text_extracted=1 ORDER BY random() LIMIT $N)
    UNION ALL
    (SELECT * FROM documents WHERE pdf_downloaded=1 OR text_extracted=1 ORDER BY seq_id DESC LIMIT 20)
  ) s ORDER BY seq_id")
while IFS="$(printf '\t')" read -r seq pdf txt size; do
  [ -n "$seq" ] || continue
  checked=$((checked + 1))
  if [ "$pdf" = 1 ]; then
    f=$(blob_path "$seq" pdf)
    if [ ! -f "$f" ]; then miss_pdf=$((miss_pdf + 1)); note "seq_id $seq: PDF 없음 $f"
    elif [ -n "$size" ] && [ "$(stat -c %s "$f")" != "$size" ]; then bad_size=$((bad_size + 1)); note "seq_id $seq: 크기 $(stat -c %s "$f") ≠ pdf_size_bytes $size"
    fi
  fi
  if [ "$txt" = 1 ]; then
    f=$(blob_path "$seq" txt)
    [ -f "$f" ] || { miss_txt=$((miss_txt + 1)); note "seq_id $seq: 텍스트 없음 $f"; }
  fi
done <<ROWS
$rows
ROWS

# blob 디렉터리에서 가장 큰 seq_id 파일 찾기(폴더 이름이 seq_id 앞자리이므로 큰 쪽부터 내려감)
max_blob=""
for d1 in $(ls -1 "$BLOB" | grep -E '^[0-9]{4}$' | sort -r); do
  for d2 in $(ls -1 "$BLOB/$d1" | grep -E '^[0-9]{4}$' | sort -r); do
    f=$(ls -1 "$BLOB/$d1/$d2" | grep -E '^[0-9]{12}\.' | sort -r | head -1 || true)
    if [ -n "$f" ]; then max_blob=$((10#${f%%.*})); break 2; fi
  done
done

fail=0
echo "== 결과 ==" >&2
printf '  표본 %d 건: PDF 없음 %d, 텍스트 없음 %d, 크기 불일치 %d\n' "$checked" "$miss_pdf" "$miss_txt" "$bad_size" >&2
[ $((miss_pdf + miss_txt + bad_size)) = 0 ] || fail=1
[ -z "$examples" ] || printf '%b' "$examples" >&2
if [ -z "$max_blob" ]; then
  printf '  blob 디렉터리에 seq_id 파일이 없습니다.\n' >&2
  [ "$checked" = 0 ] || fail=1
else
  owner=$(pg_sql_ro "$DST" "$PGU" "$PGD" "SELECT count(*) FROM documents WHERE seq_id = $max_blob")
  if [ "$max_blob" -le "$last" ]; then
    printf '  최대 blob seq_id %d ≤ 시퀀스 last_value %d: 정상\n' "$max_blob" "$last" >&2
  else
    printf '  최대 blob seq_id %d > 시퀀스 last_value %d: 새 문서가 기존 파일 번호를 받게 됩니다\n' "$max_blob" "$last" >&2
    fail=1
  fi
  [ "$owner" = 1 ] || printf '  주의: 최대 blob seq_id %d 에 해당하는 documents 행이 없습니다(삭제된 문서이거나 다른 DB 의 파일).\n' "$max_blob" >&2
fi
if [ "$fail" = 1 ]; then
  die "blob 게이트 실패: worker/file-worker 를 켜지 마십시오. DB 와 BLOB_HOST_PATH 가 같은 seq_id 공간인지 확인."
fi
log "blob 게이트 통과."
