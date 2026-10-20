# shellcheck shell=bash
# 공용 함수. 각 스크립트가 source 합니다. 직접 실행하지 않습니다.

log()  { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }
die()  { printf '오류: %s\n' "$*" >&2; exit 1; }

# 계획을 출력한 뒤 호출합니다. --yes 없이는 터미널에서 "yes" 입력을 받아야 진행합니다.
confirm() {
  if [ "${ASSUME_YES:-0}" = 1 ]; then
    log "--yes 지정: 확인 없이 진행합니다."
    return 0
  fi
  if [ ! -t 0 ]; then
    die "확인 입력을 받을 수 없습니다(터미널 아님). 계획을 검토한 뒤 --yes 로 다시 실행하십시오."
  fi
  printf '위 내용대로 진행하려면 yes 를 입력하십시오: ' >&2
  read -r answer
  [ "$answer" = yes ] || die "취소했습니다."
}

require_running() {
  state=$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null) || die "컨테이너 '$1' 을(를) 찾을 수 없습니다."
  [ "$state" = true ] || die "컨테이너 '$1' 이(가) 실행 중이 아닙니다."
}

# pg_sql CONTAINER USER DB SQL  -> 결과를 탭 구분, 헤더 없이 출력
pg_sql() {
  docker exec "$1" psql -X -q -v ON_ERROR_STOP=1 -At -F "$(printf '\t')" -U "$2" -d "$3" -c "$4"
}

# 원본 조회는 읽기 전용 트랜잭션으로 강제합니다.
pg_sql_ro() {
  docker exec -e PGOPTIONS='-c default_transaction_read_only=on' "$1" \
    psql -X -q -v ON_ERROR_STOP=1 -At -F "$(printf '\t')" -U "$2" -d "$3" -c "$4"
}

server_major() {
  pg_sql_ro "$1" "$2" "$3" "SHOW server_version_num" | awk '{print int($1/10000)}'
}

# 같은 compose 프로젝트에서 이 컨테이너 말고 실행 중인 컨테이너 목록(쓰기 주체 확인용)
project_siblings() {
  project=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$1" 2>/dev/null || true)
  [ -n "$project" ] || return 0
  docker ps --filter "label=com.docker.compose.project=$project" --format '{{.Names}}' | grep -vx "$1" || true
}

# documents 테이블이 있으면 "t". (없는 테이블을 참조하는 SQL 은 파싱 단계에서 실패하므로 먼저 확인)
has_documents() {
  pg_sql_ro "$1" "$2" "$3" "SELECT to_regclass('public.documents') IS NOT NULL"
}

# ---- 번들 compose 연동 ------------------------------------------------------
LIB_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BUNDLE=$(dirname "$LIB_DIR")
STATE_DIR="$LIB_DIR/.state"

# 번들 디렉터리에서 docker compose 실행(.env 와 그 안의 COMPOSE_FILE 을 그대로 따름).
# ENV_FILE 이 설정되면 --env-file 로 넘깁니다.
dc() {
  if [ -n "${ENV_FILE:-}" ]; then
    (cd "$BUNDLE" && docker compose --env-file "$ENV_FILE" "$@")
  else
    (cd "$BUNDLE" && docker compose "$@")
  fi
}

project_name() {
  dc config 2>/dev/null | sed -n 's/^name: //p' | head -1
}

# 정규화된 compose 설정에서 필요한 사실만 뽑습니다.
#   PORT <서비스> <host_ip> <published> <target>
#   BIND <서비스> <source> <target>
#   ENV  <서비스> <이름> <값>      (RUNNER_HOME, LIBERTREE_PG_DSN)
#   USER <서비스> <uid:gid>     GROUP <서비스> <gid>
#   NET  <키> <name> <external>
cfg_facts() {
  dc config | awk '
    /^services:/ { ins = 1; inn = 0; next }
    /^networks:/ { ins = 0; inn = 1; next }
    /^[a-z]/ { ins = 0; inn = 0 }
    inn && /^  [A-Za-z0-9_.-]+:$/ { if (nk != "") print "NET", nk, nn, ne; nk = $1; sub(":", "", nk); nn = ""; ne = "false"; next }
    inn && /^    name:/ { nn = $2 }
    inn && /^    external:/ { ne = $2 }
    END { if (nk != "") print "NET", nk, nn, ne }
    !ins { next }
    /^  [A-Za-z0-9_.-]+:$/ { svc = $1; sub(":", "", svc); inb = 0; ing = 0; next }
    /^      - mode:/ { ip = "" }
    /^        host_ip:/ { ip = $2 }
    /^        target:/ && !inb { tgt = $2 }
    /^        published:/ { p = $2; gsub("\"", "", p); print "PORT", svc, (ip == "" ? "0.0.0.0" : ip), p, tgt }
    /^      - type:/ { inb = ($3 == "bind"); src = "" }
    inb && /^        source:/ { src = $2 }
    inb && /^        target:/ { print "BIND", svc, src, $2 }
    /^      (RUNNER_HOME|LIBERTREE_PG_DSN|COLLECT_RUNNER_SOCKET):/ { k = $1; sub(":", "", k); print "ENV", svc, k, $2 }
    /^    user:/ { print "USER", svc, $2 }
    /^    group_add:/ { ing = 1; next }
    ing && /^      - / { g = $2; gsub("\"", "", g); print "GROUP", svc, g; next }
    { ing = 0 }
  '
}

# be 의 DSN 에서 운영 DB 호스트(=운영 postgres 컨테이너 이름)를 꺼냅니다.
prod_db_host() {
  cfg_facts | awk '$1=="ENV" && $2=="be" && $3=="LIBERTREE_PG_DSN" {print $4}' | sed -E 's#^[a-z]+://[^@]*@##; s#[:/].*$##'
}
prod_network() {
  cfg_facts | awk '$1=="NET" && $2=="proddb" {print $3}'
}

# 00_preflight.sh 가 남긴 운영 컨테이너 기록:
#   서비스 <TAB> 컨테이너ID <TAB> 이름 <TAB> 상태 <TAB> 이미지참조 <TAB> 이미지ID
prod_record() { printf '%s/production-%s.tsv' "$STATE_DIR" "$1"; }
prod_field() { # 기록파일 서비스 열번호
  awk -F '\t' -v s="$2" -v c="$3" '$1==s {print $c}' "$1"
}

to_epoch() { date -d "$1" +%s; }
