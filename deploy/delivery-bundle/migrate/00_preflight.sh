#!/usr/bin/env bash
# (0) 필수 사전 점검. 아무것도 만들거나 멈추지 않습니다(읽기만, 기록 파일만 씀).
#
# 사용: migrate/00_preflight.sh [--env-file 파일]
#   운영(기존) 쪽
#     - PROD_DB_HOST 컨테이너(운영 postgres)가 실행 중이고 PROD_DB_NETWORK 에 붙어 있는지
#     - 그 컨테이너의 compose 프로젝트(운영 프로젝트)에서 be, fe, worker, file-worker 를 찾음
#       (runner 는 있으면 함께). 하나라도 없으면 중단
#     - 운영 be 의 /data/blob 경로 = 이 번들의 BLOB_HOST_PATH 인지(아니면 중단)
#     - 각 컨테이너 ID·이름·상태·이미지 참조·이미지 ID 를 migrate/.state/production-<v2프로젝트>.tsv 에 기록
#   새(v2) 쪽
#     - 프로젝트 이름이 운영 프로젝트와 다르고, 이 이름의 컨테이너·볼륨·네트워크가 없는지
#     - 새 이미지 이름이 운영 컨테이너가 쓰는 이미지 이름과 겹치지 않는지(겹치면 롤백 불가 → 중단)
#     - 호스트 포트 사용 주체, blob·runner 디렉터리·.token·docker 소켓 그룹
#     - 만들어질 자원 목록
set -eu
set -o pipefail
. "$(dirname "$0")/lib.sh"

usage() { sed -n '2,17p' "$0" >&2; exit 2; }
ENV_FILE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --env-file) ENV_FILE=$2; shift 2 ;;
    -h|--help) usage ;;
    *) die "알 수 없는 인자: $1" ;;
  esac
done
[ -z "$ENV_FILE" ] || [ -f "$ENV_FILE" ] || die "env 파일이 없습니다: $ENV_FILE"
[ -n "$ENV_FILE" ] || [ -f "$BUNDLE/.env" ] || die "$BUNDLE/.env 가 없습니다(cp .env.example .env 후 작성)."

fail=0
bad()  { printf '  [중단] %s\n' "$*" >&2; fail=1; }
ok()   { printf '  [확인] %s\n' "$*" >&2; }
warn() { printf '  [주의] %s\n' "$*" >&2; }

echo "== 도구 ==" >&2
sv=$(docker version --format '{{.Server.Version}}' 2>/dev/null) || die "docker 데몬에 접근할 수 없습니다."
major=${sv%%.*}; rest=${sv#*.}; minor=${rest%%.*}
if [ "$major" -gt 20 ] || { [ "$major" -eq 20 ] && [ "$minor" -ge 10 ]; }; then ok "Docker $sv (≥20.10)"; else bad "Docker $sv: 20.10 이상 필요(host-gateway)"; fi
ok "$(docker compose version 2>&1 | head -1)"

echo "== compose 설정 ==" >&2
cfg_err=$(dc config --quiet 2>&1) || die "compose 설정 오류: $cfg_err"
P=$(project_name)
FACTS=$(cfg_facts)
DBH=$(prod_db_host)
NET=$(prod_network)
[ -n "$P" ] && [ -n "$DBH" ] && [ -n "$NET" ] || die "프로젝트 이름/PROD_DB_HOST/PROD_DB_NETWORK 를 설정에서 읽지 못했습니다."
ok "v2 프로젝트: $P / 운영 DB 호스트: $DBH / 운영 네트워크: $NET"

echo "== 운영(기존) 컨테이너 ==" >&2
state=$(docker inspect -f '{{.State.Status}}' "$DBH" 2>/dev/null) || die "운영 postgres 컨테이너 '$DBH' 를 찾을 수 없습니다(PROD_DB_HOST 확인)."
[ "$state" = running ] || die "운영 postgres '$DBH' 가 실행 중이 아닙니다($state)."
PP=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$DBH")
[ -n "$PP" ] || die "'$DBH' 에 compose 프로젝트 라벨이 없습니다."
psvc=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$DBH")
ok "운영 postgres: $DBH (프로젝트 $PP, 서비스 $psvc, 실행 중)"
[ "$P" != "$PP" ] || die "v2 프로젝트 이름이 운영 프로젝트 '$PP' 와 같습니다. 같은 이름이면 기존 컨테이너가 재생성·삭제됩니다."
docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$DBH" | tr ' ' '\n' | grep -qx "$NET" \
  || bad "운영 postgres 가 네트워크 '$NET' 에 없습니다(PROD_DB_NETWORK 확인): $(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$DBH")"
pg_ports=$(docker port "$DBH" 2>/dev/null | tr '\n' ' ')
if [ -n "$pg_ports" ]; then
  warn "운영 postgres 호스트 포트: $pg_ports(이 번들은 바꾸지 않음. 0.0.0.0 이면 ADMIN_GUIDE.md 7장)"
fi

rec_tmp=$(mktemp)
trap 'rm -f "$rec_tmp"' EXIT
add_rec() { # 서비스 컨테이너ID
  docker inspect -f "$1"'{{"\t"}}{{.Id}}{{"\t"}}{{slice .Name 1}}{{"\t"}}{{.State.Status}}{{"\t"}}{{.Config.Image}}{{"\t"}}{{.Image}}' "$2" >> "$rec_tmp"
}
add_rec postgres "$DBH"
for s in be fe worker file-worker runner; do
  ids=$(docker ps -aq --filter "label=com.docker.compose.project=$PP" --filter "label=com.docker.compose.service=$s")
  n=$(printf '%s' "$ids" | grep -c . || true)
  if [ "$n" = 0 ]; then
    if [ "$s" = runner ]; then ok "운영 runner 없음(선택)"; else bad "운영 프로젝트 '$PP' 에서 서비스 '$s' 컨테이너를 찾지 못했습니다."; fi
    continue
  fi
  [ "$n" = 1 ] || { bad "운영 서비스 '$s' 컨테이너가 $n 개입니다(1개 기대)."; continue; }
  add_rec "$s" "$ids"
done
awk -F '\t' '{printf "  [기록] %-12s %-34s %-9s %s (%s)\n", $1, $3, $4, $5, substr($6, 8, 12)}' "$rec_tmp" >&2

oldblob=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/data/blob"}}{{.Source}}{{end}}{{end}}' \
  "$(awk -F '\t' '$1=="be" {print $2}' "$rec_tmp")" 2>/dev/null || true)
blob=$(printf '%s\n' "$FACTS" | awk '$1=="BIND" && $2=="be" && $4=="/data/blob" {print $3}')
if [ -z "$oldblob" ]; then bad "운영 be 의 /data/blob 마운트를 찾지 못했습니다."
elif [ "$oldblob" != "$blob" ]; then bad "BLOB_HOST_PATH '$blob' ≠ 운영 be 의 blob 경로 '$oldblob' (같은 seq_id 공간이어야 함)"
else ok "BLOB_HOST_PATH = 운영 be 의 blob 경로 ($blob)"; fi

echo "== v2 기존 자원(있으면 중단) ==" >&2
c=$(docker ps -a --filter "label=com.docker.compose.project=$P" --format '{{.Names}} ({{.Status}})')
if [ -n "$c" ]; then bad "프로젝트 '$P' 컨테이너가 이미 있습니다:"; printf '%s\n' "$c" | sed 's/^/      /' >&2; else ok "컨테이너 없음"; fi
v=$( { docker volume ls -q --filter "label=com.docker.compose.project=$P"; docker volume ls -q | grep "^${P}_" || true; } | sort -u)
if [ -n "$v" ]; then bad "프로젝트 '$P' 볼륨이 이미 있습니다:"; printf '%s\n' "$v" | sed 's/^/      /' >&2; else ok "볼륨 없음"; fi
n=$( { docker network ls --filter "label=com.docker.compose.project=$P" --format '{{.Name}}'; docker network ls --format '{{.Name}}' | grep "^${P}_" || true; } | sort -u)
if [ -n "$n" ]; then bad "프로젝트 '$P' 네트워크가 이미 있습니다:"; printf '%s\n' "$n" | sed 's/^/      /' >&2; else ok "네트워크 없음"; fi

echo "== 이미지 ==" >&2
old_refs=$(cut -f5 "$rec_tmp" | awk '{ if ($0 !~ /:[^\/]*$/) $0 = $0 ":latest"; print }' | sort -u)
for img in $(dc --profile tools config --images | sort -u); do
  case "$img" in postgres:*) continue ;; esac
  if printf '%s\n' "$old_refs" | grep -qx "$img"; then
    bad "새 이미지 $img 가 운영 컨테이너의 이미지 이름과 같습니다(빌드하면 롤백용 이미지가 덮어써짐). IMAGE_PREFIX/IMAGE_TAG 변경."
  elif docker image inspect "$img" >/dev/null 2>&1; then
    warn "이미지 $img 가 이미 있습니다(빌드하면 이 태그를 덮어씀)."
  else
    ok "새 이미지 $img (빌드 예정)"
  fi
done

echo "== 호스트 포트 (운영 be/fe 가 쓰고 있으면 정상: 컷오버 때 정지) ==" >&2
old_names=$(cut -f3 "$rec_tmp")
printf '%s\n' "$FACTS" | awk '$1=="PORT"' | while read -r _ svc ip port tgt; do
  holders=$(docker ps --format '{{.Names}}\t{{.Ports}}' | awk -F '\t' -v p=":$port->" 'index($2, p) {print $1}')
  others=$(printf '%s\n' "$holders" | grep -vxF "$old_names" | grep . || true)
  sock=$(ss -Hltn "sport = :$port" 2>/dev/null | awk '{print $4}' | tr '\n' ' ' || true)
  if [ -n "$others" ]; then
    warn "$svc → $ip:$port: 운영 기록에 없는 컨테이너가 사용 중: $(printf '%s' "$others" | tr '\n' ' ')(컷오버로 비워지지 않음)"
  elif [ -n "$holders" ]; then
    ok "$svc → $ip:$port: 운영 $(printf '%s' "$holders" | tr '\n' ' ')사용 중(컷오버 때 정지)"
  elif [ -n "$sock" ]; then
    warn "$svc → $ip:$port: 컨테이너 밖 프로세스가 수신 중: $sock"
  else
    ok "$svc → $ip:$port: 비어 있음"
  fi
  case "$svc:$ip" in fe:0.0.0.0|be:0.0.0.0) warn "$svc 가 모든 인터페이스(0.0.0.0)에 공개됩니다. ADMIN_GUIDE.md 7장." ;; esac
done

echo "== 디렉터리 ==" >&2
if [ -z "$blob" ] || [ ! -d "$blob" ]; then bad "BLOB_HOST_PATH 디렉터리가 없습니다: '$blob'"
elif [ ! -r "$blob" ]; then bad "BLOB_HOST_PATH 를 읽을 수 없습니다(권한): $blob"
else ok "BLOB_HOST_PATH $blob (seq_id 상위 폴더 $(ls -1 "$blob" | grep -cE '^[0-9]{4}$' || true) 개)"; fi
rhome=$(printf '%s\n' "$FACTS" | awk '$1=="ENV" && $2=="runner" && $3=="RUNNER_HOME" {print $4}')
runs=$(printf '%s\n' "$FACTS" | awk '$1=="BIND" && $2=="runner" && $3==$4 && $3!="/var/run/docker.sock" {print $3}')
ruser=$(printf '%s\n' "$FACTS" | awk '$1=="USER" && $2=="runner" {print $3}')
rgid=$(printf '%s\n' "$FACTS" | awk '$1=="GROUP" && $2=="runner" {print $3}')
ruid=${ruser%%:*}
for d in "$rhome" $runs; do
  if [ ! -d "$d" ]; then bad "runner 디렉터리가 없습니다: $d (필수, ADMIN_GUIDE.md 1장)"
  else
    o=$(stat -c %u "$d")
    if [ "$o" = "$ruid" ]; then ok "runner 디렉터리 $d (소유 uid $o)"; else bad "$d 소유 uid $o ≠ COLLECT_RUNNER_UID $ruid"; fi
  fi
done
oldrunner=$(awk -F '\t' '$1=="runner" {print $3}' "$rec_tmp")
if [ -n "$oldrunner" ]; then
  oldhome=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$oldrunner" | sed -n 's/^RUNNER_HOME=//p')
  [ "$oldhome" != "$rhome" ] || bad "v2 COLLECT_RUNNER_HOME 이 운영 runner 와 같은 폴더입니다($rhome). 별도 폴더로(롤백 시 상태 보존)."
fi
tok="$rhome/.token"
if [ ! -f "$tok" ]; then bad "runner 토큰 파일이 없습니다: $tok (be 가 이 파일을 bind 하므로 필수)"
else
  m=$(stat -c '%a %u' "$tok")
  if [ "$m" = "600 $ruid" ]; then ok "runner 토큰 $tok (0600, uid $ruid)"; else bad "$tok 권한/소유 '$m' (기대: 600 $ruid)"; fi
fi
sg=$(stat -c %g /var/run/docker.sock 2>/dev/null || echo '?')
if [ "$sg" = "$rgid" ]; then ok "docker 소켓 그룹 $sg = DOCKER_GID"; else bad "docker 소켓 그룹 $sg ≠ DOCKER_GID $rgid"; fi

echo "== 만들어질 자원 (운영 postgres·볼륨은 건드리지 않음) ==" >&2
for s in $(dc config --services | sort); do printf '  컨테이너 %s-%s-1\n' "$P" "$s" >&2; done
printf '  (1회성) %s-migrate-run-*\n' "$P" >&2
for vol in $(dc config --volumes | sort); do printf '  볼륨     %s_%s\n' "$P" "$vol" >&2; done
printf '  네트워크 %s_default (+ 기존 %s 에 be/worker/file-worker/migrate 연결)\n' "$P" "$NET" >&2

if [ "$fail" = 1 ]; then
  die "사전 점검 실패. 위의 [중단] 항목을 해결한 뒤 다시 실행하십시오."
fi
mkdir -p "$STATE_DIR"
rec=$(prod_record "$P")
if [ -f "$rec" ]; then cp "$rec" "$rec.$(date +%Y%m%d-%H%M%S).bak"; fi
cp "$rec_tmp" "$rec"
{ echo "PROJECT=$P"; echo "PROD_PROJECT=$PP"; echo "PROD_DB_HOST=$DBH"; echo "PREFLIGHT_AT=$(date -Iseconds)"; } > "$STATE_DIR/preflight-$P.env"
log "사전 점검 통과. 운영 컨테이너 기록: $rec"
