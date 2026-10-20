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
