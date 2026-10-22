#!/usr/bin/env bash
# 공통 수집 틀 시범 실행 스크립트. 사용법:
#   run-pilot.sh build
#   run-pilot.sh run <plan.json> <site...>
#   run-pilot.sh report
#   run-pilot.sh cleanup [--purge-data]
# 환경변수(선택):
#   ENUM_PILOT_DATE            날짜 태그 (기본: 오늘 YYYYMMDD)
#   ENUM_PILOT_ROOT            호스트 작업 루트 (기본: /srv/enum-pilot)
#   ENUM_SEED_HOLDS            seed-holds.csv 경로 (기본: <plan.json 디렉터리>/seed-holds.csv)
#   ENUM_REFERENCE_DB          읽기 전용 참조 DB(sqlite) 호스트 경로 -> --reference-db
#   ENUM_ALLOW_EMPTY_REFERENCE 1 이면 --allow-empty-reference
#   ENUM_FILES                 1 이면 --files
#   ENUM_INCREMENTAL           1 이면 --incremental (앞쪽부터 새 글만, plan 의 incremental 설정; 최신순 목록만)
#   ENUM_KNOWN_KEYS            증분용 이미 납품된 키 파일(CSV/JSONL: site_id,post_number,meta_url) 호스트 경로 -> --known-keys
#   ENUM_WITH_BROWSER          1 이면 빌드 시 크로미움 설치
#   ENUM_EXTRA_ARGS            틀에 그대로 넘길 추가 옵션(공백 구분, 예: "--retry-failed")
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

PREFIX="enum-pilot"
DATE="${ENUM_PILOT_DATE:-$(date +%Y%m%d)}"
ROOT="${ENUM_PILOT_ROOT:-/srv/enum-pilot}"
IMAGE="${PREFIX}:${DATE}"
CONTAINER="${PREFIX}-${DATE}"
NETWORK="${PREFIX}-net"
RUN_DIR="${ROOT}/${DATE}"
DATA_DIR="${RUN_DIR}/data"
LABEL="enum-pilot.managed=true"

die() { echo "ERROR: $*" >&2; exit 1; }
warn() { echo "WARN: $*" >&2; }

# --- 안전 검사: 사용할 이름이 enum-pilot 접두이고 temis 와 무관하며 기존 자원과 충돌하지 않는다 ---
safety_check() {
  local n lower
  # '.', '..' 같은 값이 경로 계산(RUN_DIR)과 삭제 검증을 무력화하지 못하게 형식을 고정한다.
  [[ "$DATE" =~ ^[0-9]{8}([-_][A-Za-z0-9]+)?$ ]] || die "ENUM_PILOT_DATE 형식은 YYYYMMDD 또는 YYYYMMDD-태그: '$DATE'"
  for n in "$IMAGE" "$CONTAINER" "$NETWORK"; do
    lower="$(printf '%s' "$n" | tr '[:upper:]' '[:lower:]')"
    [[ "$lower" == temis* ]] && die "temis 로 시작하는 이름은 거부: $n"
    [[ "$n" == "${PREFIX}"* ]] || die "enum-pilot 접두가 아닌 이름: $n"
  done
  case "$ROOT" in
    /|"") die "ENUM_PILOT_ROOT 가 비정상: '$ROOT'" ;;
  esac
  [[ "$(printf '%s' "$ROOT" | tr '[:upper:]' '[:lower:]')" == *temis* ]] && die "경로에 temis 포함: $ROOT"
  # 이름만 조회 (읽기 전용)
  EXISTING_CONTAINERS="$(docker ps -a --format '{{.Names}}')"
  EXISTING_NETWORKS="$(docker network ls --format '{{.Name}}')"
}

container_exists() { grep -Fxq "$CONTAINER" <<<"$EXISTING_CONTAINERS"; }
network_exists() { grep -Fxq "$NETWORK" <<<"$EXISTING_NETWORKS"; }

network_is_ours() {
  [[ "$(docker network inspect -f '{{index .Labels "enum-pilot.managed"}}' "$NETWORK" 2>/dev/null)" == "true" ]]
}

cmd_build() {
  safety_check
  local args=(build -f "$SCRIPT_DIR/Dockerfile" -t "$IMAGE" --label "$LABEL")
  [[ "${ENUM_WITH_BROWSER:-0}" == "1" ]] && args+=(--build-arg WITH_BROWSER=1)
  docker "${args[@]}" "$REPO_ROOT"
  echo "built $IMAGE"
}

cmd_run() {
  [[ $# -ge 2 ]] || die "usage: run-pilot.sh run <plan.json> <site...>"
  local plan="$1"; shift
  local sites=("$@")
  [[ -f "$plan" ]] || die "plan.json 없음: $plan"
  plan="$(cd "$(dirname "$plan")" && pwd)/$(basename "$plan")"
  local seed="${ENUM_SEED_HOLDS:-$(dirname "$plan")/seed-holds.csv}"
  [[ -f "$seed" ]] || die "seed-holds.csv 없음: $seed"
  seed="$(cd "$(dirname "$seed")" && pwd)/$(basename "$seed")"

  safety_check
  docker image inspect "$IMAGE" >/dev/null 2>&1 || die "이미지 없음: $IMAGE (먼저 build)"
  container_exists && die "같은 이름의 컨테이너가 이미 있음: $CONTAINER (ENUM_PILOT_DATE 를 바꾸거나 cleanup)"

  # --seed-holds 지원 여부 확인 (틀에 아직 없으면 중단)
  if ! docker run --rm --network none --read-only --tmpfs /tmp --user 10001:10001 "$IMAGE" --help 2>&1 | grep -q -- '--seed-holds'; then
    die "이 이미지의 틀에 --seed-holds 옵션이 없음. 틀 변경이 반영된 소스로 다시 build 하세요."
  fi

  if [[ "${ENUM_INCREMENTAL:-0}" == "1" ]] && ! docker run --rm --network none --read-only --tmpfs /tmp --user 10001:10001 "$IMAGE" --help 2>&1 | grep -q -- '--incremental'; then
    die "이 이미지의 틀에 --incremental 옵션이 없음. 틀 변경이 반영된 소스로 다시 build 하세요."
  fi
  [[ -n "${ENUM_KNOWN_KEYS:-}" && "${ENUM_INCREMENTAL:-0}" != "1" ]] && die "ENUM_KNOWN_KEYS 는 ENUM_INCREMENTAL=1 일 때만"

  # 전용 네트워크 (우리가 만든 것만 재사용)
  if network_exists; then
    network_is_ours || die "네트워크 $NETWORK 가 이미 있으나 이 스크립트가 만든 것이 아님"
  else
    docker network create --label "$LABEL" "$NETWORK" >/dev/null
  fi

  # 호스트 디렉터리
  # sudo/chown 없이: 현재 사용자 소유로 만들고 컨테이너도 같은 uid:gid 로 실행한다.
  RUN_UID="$(id -u)"; RUN_GID="$(id -g)"
  [[ "$RUN_UID" != "0" ]] || die "root 로 실행하지 마세요. 일반 사용자로 실행해야 데이터 폴더가 그 사용자 소유가 됩니다."
  mkdir -p "$DATA_DIR"
  [[ -L "$RUN_DIR" || -L "$DATA_DIR" ]] && die "심볼릭 링크 경로는 거부: $RUN_DIR"
  [[ "$(stat -c %u "$DATA_DIR")" == "$RUN_UID" ]] || die "$DATA_DIR 가 현재 사용자 소유가 아님(다른 사용자나 root 가 만든 폴더). 다른 ENUM_PILOT_ROOT 를 쓰세요."
  # seed-holds 마운트 지점(빈 파일)을 미리 만들어 둔다
  [[ -e "$DATA_DIR/seed-holds.csv" ]] || : >"$DATA_DIR/seed-holds.csv"

  local mounts=(
    -v "$DATA_DIR:/data"
    -v "$plan:/input/plan.json:ro"
    -v "$seed:/data/seed-holds.csv:ro"
  )
  local fargs=(
    --plan /input/plan.json
    --state-dir /data/state
    --db /data/enum_pilot.db
    --report-dir /data/reports
    --blob-root /data/blob
    --seed-holds /data/seed-holds.csv
    --workers 1
  )
  local s
  for s in "${sites[@]}"; do fargs+=(--site "$s"); done
  if [[ -n "${ENUM_REFERENCE_DB:-}" ]]; then
    [[ -f "$ENUM_REFERENCE_DB" ]] || die "ENUM_REFERENCE_DB 파일 없음"
    mounts+=(-v "$ENUM_REFERENCE_DB:/ref/reference.db:ro")
    fargs+=(--reference-db /ref/reference.db)
  fi
  [[ "${ENUM_ALLOW_EMPTY_REFERENCE:-0}" == "1" ]] && fargs+=(--allow-empty-reference)
  [[ "${ENUM_FILES:-0}" == "1" ]] && fargs+=(--files)
  [[ "${ENUM_INCREMENTAL:-0}" == "1" ]] && fargs+=(--incremental)
  if [[ -n "${ENUM_KNOWN_KEYS:-}" ]]; then
    [[ -f "$ENUM_KNOWN_KEYS" ]] || die "ENUM_KNOWN_KEYS 파일 없음"
    case "$ENUM_KNOWN_KEYS" in
      *.jsonl|*.ndjson) mounts+=(-v "$ENUM_KNOWN_KEYS:/input/known-keys.jsonl:ro"); fargs+=(--known-keys /input/known-keys.jsonl) ;;
      *) mounts+=(-v "$ENUM_KNOWN_KEYS:/input/known-keys.csv:ro"); fargs+=(--known-keys /input/known-keys.csv) ;;
    esac
  fi
  # shellcheck disable=SC2206
  [[ -n "${ENUM_EXTRA_ARGS:-}" ]] && fargs+=(${ENUM_EXTRA_ARGS})

  echo "container=$CONTAINER image=$IMAGE network=$NETWORK data=$DATA_DIR"
  docker run --rm \
    --name "$CONTAINER" \
    --label "$LABEL" \
    --network "$NETWORK" \
    --cpus 2 --memory 2g --pids-limit 256 \
    --read-only --tmpfs /tmp:rw,noexec,nosuid,size=256m \
    --cap-drop ALL --security-opt no-new-privileges \
    --user "${RUN_UID}:${RUN_GID}" \
    "${mounts[@]}" \
    "$IMAGE" "${fargs[@]}"
}

cmd_report() {
  local d="$DATA_DIR/reports"
  [[ -d "$d" ]] || die "보고서 디렉터리 없음: $d"
  ls -l "$d"
  echo "(파일 이름·형식은 틀 버전에 따라 다름. 위 목록에서 직접 확인)"
}

cmd_cleanup() {
  safety_check
  local purge=0
  [[ "${1:-}" == "--purge-data" ]] && purge=1
  echo "정리 대상: 컨테이너 $CONTAINER, 이미지 $IMAGE, 네트워크 $NETWORK(우리가 만든 것·미사용일 때)"
  [[ $purge -eq 1 ]] && echo "       + 호스트 데이터 $RUN_DIR (되돌릴 수 없음)"
  read -r -p "계속하려면 'yes' 입력: " ans
  [[ "$ans" == "yes" ]] || die "취소됨"

  if container_exists; then
    [[ "$(docker inspect -f '{{index .Config.Labels "enum-pilot.managed"}}' "$CONTAINER")" == "true" ]] \
      || die "$CONTAINER 는 이 스크립트가 만든 컨테이너가 아님"
    docker rm -f "$CONTAINER" >/dev/null
  fi
  if docker image inspect "$IMAGE" >/dev/null 2>&1; then
    docker rmi "$IMAGE" >/dev/null
  fi
  if network_exists && network_is_ours; then
    if [[ -z "$(docker network inspect -f '{{range .Containers}}x{{end}}' "$NETWORK")" ]]; then
      docker network rm "$NETWORK" >/dev/null
    else
      warn "네트워크에 연결된 컨테이너가 있어 남겨 둠: $NETWORK"
    fi
  fi
  if [[ $purge -eq 1 ]]; then
    [[ "$RUN_DIR" == "$ROOT/$DATE" && "$DATE" != "" && ! -L "$RUN_DIR" ]] || die "삭제 경로 검증 실패"
    # sudo 는 쓰지 않는다. 지우지 못하면 소유자 권한으로 직접 지우도록 안내만 한다.
    if [[ -d "$RUN_DIR" ]]; then
      rm -rf -- "$RUN_DIR" || warn "삭제하지 못함(권한). 직접 확인 후 삭제: $RUN_DIR"
    fi
  fi
  echo "cleanup done"
}

main() {
  local sub="${1:-}"; shift || true
  case "$sub" in
    build) cmd_build "$@" ;;
    run) cmd_run "$@" ;;
    report) cmd_report "$@" ;;
    cleanup) cmd_cleanup "$@" ;;
    *) sed -n '2,6p' "${BASH_SOURCE[0]}"; exit 2 ;;
  esac
}
main "$@"
