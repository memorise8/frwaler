# 공통 수집 틀 시범 실행 (격리 컨테이너)

`python -m crawler.enumeration.run` 을 다른 서버에서 운영 자원과 분리해 실행하기 위한 파일 모음.
설계 문서: `docs/COLLECTION_FRAMEWORK.md`. **시범은 사용자 승인 후에만 실행한다.**

## 구성

| 파일 | 설명 |
|---|---|
| `Dockerfile` | python:3.12-slim, 비루트(UID 10001), ENTRYPOINT=틀, CMD 없음, `VOLUME /data`. 크로미움은 `--build-arg WITH_BROWSER=1` 일 때만 설치 |
| `run-pilot.sh` | `build` / `run <plan.json> <site...>` / `report` / `cleanup [--purge-data]` |
| `dockerignore.append` | 루트 `.dockerignore` 에 추가를 *제안*하는 규칙 (기존 파일은 수정하지 않음) |

이미지에 포함: `crawler/` 전체, `delivery/__init__.py`, `delivery/health/{__init__,access_challenge,diagnostics,request_gate,results,scope_inventory,source_observer}.py`, `delivery/scripts/discovery_structure.py`, `delivery/requirements.txt`·`delivery/vendor/crawler-runtime-requirements.txt` 로 설치한 패키지, curl, poppler-utils.
제외: `delivery/` 의 나머지(worker, be, fe 등), `docs/`, 크로미움(기본).

## 1. 서버로 옮기기

```bash
# 개발 머신 (워크트리에서)
git bundle create enum-pilot.bundle collect-framework-20261012
scp enum-pilot.bundle <user>@<server>:/tmp/

# 서버
git clone /tmp/enum-pilot.bundle enum-pilot-src -b collect-framework-20261012
cd enum-pilot-src
deploy/enum-pilot/run-pilot.sh build
```

plan.json 과 seed-holds.csv 는 번들에 없을 수 있으므로 별도로 scp 한다. 기본 위치는 plan.json 과 같은 디렉터리의 `seed-holds.csv` (`ENUM_SEED_HOLDS` 로 변경).

## 2. 실행

```bash
deploy/enum-pilot/run-pilot.sh run /path/plan.json <site-id> [<site-id> ...]
```

컨테이너 설정: 이름 `enum-pilot-<날짜>`, 네트워크 `enum-pilot-net`(스크립트가 생성), `--cpus 2 --memory 2g --pids-limit 256`, `--read-only` + tmpfs `/tmp`, `--cap-drop ALL`, 포트 게시 없음, `--user <현재 사용자 uid:gid>` (sudo/chown 을 쓰지 않음. root 로 실행하면 스크립트가 거부).
호스트 데이터: `/srv/enum-pilot/<날짜>/data` -> 컨테이너 `/data` (`state/`, `enum_pilot.db`, `reports/`, `blob/`). plan.json 과 seed-holds.csv 는 읽기 전용 마운트.
틀 인자: `--plan /input/plan.json --state-dir /data/state --db /data/enum_pilot.db --report-dir /data/reports --blob-root /data/blob --seed-holds /data/seed-holds.csv --workers 1 --site ...`

`--seed-holds` 가 이미지의 `--help` 출력에 없으면 스크립트가 경고하고 중단한다 (틀 변경이 반영된 소스로 다시 build).

선택 환경변수: `ENUM_REFERENCE_DB`(읽기 전용 참조 sqlite -> `--reference-db`), `ENUM_ALLOW_EMPTY_REFERENCE=1`, `ENUM_FILES=1`(`--files`), `ENUM_WITH_BROWSER=1`, `ENUM_EXTRA_ARGS`(예: `--retry-failed`), `ENUM_PILOT_DATE`, `ENUM_PILOT_ROOT`.
`ENUM_ENV_FILE`: plan 의 `request_headers_env` 가 읽는 비밀값(API 키) 파일(`KEY=값` 줄) 호스트 경로 → `docker run --env-file`. 현재 사용자 소유의 일반 파일(링크 아님)이고 그룹·다른 사용자 권한이 없어야 한다(`chmod 600`). 값은 따옴표 없이 쓴다(`--env-file` 은 따옴표도 값으로 넘긴다). 폴더는 그룹·다른 사용자가 쓸 수 없어야 한다. 스크립트는 내용을 읽거나 출력하지 않는다.
같은 사이트를 이어서 돌릴 때(이어받기, `api_cursor` 기준일 사이트의 매 실행)는 늘 같은 `ENUM_PILOT_DATE` 태그를 쓴다: 상태 저장소(`<태그>/data/state`)에 커서·기준일이 있고, 새 태그는 처음부터(api_cursor 는 `initial_since` 부터) 다시 읽는다. 수집 실행기는 이어받기·증분에 사이트별 고정 폴더를 쓴다.
참조 DB 는 기본으로 사용하지 않는다. 등록 수집기 사이트에서 참조 행이 없을 때의 동작은 문서상 거부이며 `--allow-empty-reference` 일 때만 통과(보고서에 경고 기록).

## 3. 읽기 전용 사전 점검 체크리스트 (명령만; 사람이 서버에서 실행)

- docker 버전: `docker version --format '{{.Server.Version}}'`
- 디스크: `df -h /srv` (그리고 `docker system df`)
- 포트: 이 시범은 포트를 게시하지 않음. 확인용 `ss -ltn`
- 기존 자원 이름: `docker ps -a --format '{{.Names}}'`, `docker network ls --format '{{.Name}}'`
- 메모리/CPU 여유: `free -h`, `nproc`
- 작업 루트 쓰기 권한: 현재 사용자가 쓸 수 있는 경로를 `ENUM_PILOT_ROOT` 로 지정 (기본 `/srv/enum-pilot` 에 쓸 수 없으면 홈 아래 새 폴더 등). sudo 는 쓰지 않음.
- `ENUM_PILOT_DATE` 는 `YYYYMMDD` 또는 `YYYYMMDD-태그` 형식만 허용 (경로 계산과 삭제 안전을 위해)

## 4. 주의사항

- 이름이 `temis` 로 시작하는 컨테이너·DB·서비스·tmux 세션·네트워크·볼륨은 건드리지 않는다. 스크립트는 `enum-pilot` 접두가 아닌 이름과 `temis*` 이름을 거부하고, 자원 충돌 여부를 이름만 조회해 확인한다.
- 문서 DB 이름은 `enum_` 로 시작해야 한다 (틀이 강제; 기본값 `enum_pilot.db`). `--i-know-this-is-isolated` 는 쓰지 않는다.
- 운영 DB 접속 정보(DSN, 비밀번호, `LIBERTREE_PG_DSN`)를 컨테이너에 넣지 않는다.
- 요청 정책: 사이트 순차(`--workers 1`, 사이트별 한 번에 하나), 응답 후 1.5초 간격(틀 기본 `interval=1.5`), 접근 차단(401/403/429/챌린지/로그인)을 우회하지 않고 보류로 기록. 보류 해제는 사람이 검토 후 `--release-host`/`--refresh` 로만.
- 파괴적 동작(`rm`, 이미지·네트워크 삭제, 데이터 삭제)은 `cleanup` 하위 명령에서, 스크립트가 만든 `enum-pilot` 자원에 대해서만 한다.
- 시범 실행은 사용자 승인 후에만.

## 5. 합격 기준 확인용 보고서

`/srv/enum-pilot/<날짜>/data/reports/` (`--report-dir`). 목록은 `run-pilot.sh report`. 상태 DB 는 `data/state/enumeration.sqlite`. 보고서 파일 이름·컬럼은 `docs/COLLECTION_FRAMEWORK.md` §11 참고 (이 README 에서는 미확인).

## 6. 정리

```bash
deploy/enum-pilot/run-pilot.sh cleanup               # 컨테이너·이미지·(미사용 시)네트워크
deploy/enum-pilot/run-pilot.sh cleanup --purge-data  # 위 + /srv/enum-pilot/<날짜> 삭제 (되돌릴 수 없음)
```

## 미확인 / 가정

- `--seed-holds` 옵션: 다른 에이전트가 추가 중이며 작성 시점에 `run.py` 에 없었음. 호스트 보류 관련 옵션은 확인한 것이 `--release-host`, `--reprocess-failed` 뿐.
- 참조 DB 없이(`--reference-db` 미지정) 등록 사이트를 실행했을 때의 정확한 동작.
- `--read-only` 에서 틀/수집기가 `/data`, `/tmp` 밖에 쓰는지 (코드상 gates·state 는 `--state-dir` 아래).
- 이미지 빌드·실행은 이 작업에서 수행하지 않았음 (파일 작성만). Dockerfile 의 delivery 최소 COPY 목록은 grep 으로만 확인.
- 크로미움 없는 이미지에서 브라우저 수집기를 틀이 거부한다는 점은 지시문 기준.
