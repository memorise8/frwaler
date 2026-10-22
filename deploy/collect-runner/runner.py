#!/usr/bin/env python3
"""수집 실행기: 등록된 plan 만 deploy/enum-pilot/run-pilot.sh 로 실행한다.

- HTTP 는 127.0.0.1 에서만 열고 X-Runner-Token 을 요구한다(수집 콘솔 server.py 가 대신 호출).
  RUNNER_BIND 로 듣는 주소를 바꿀 수 있다(기본 127.0.0.1, 쉼표로 여러 개 — 예 127.0.0.1,172.16.1.1 은
  루프백 + 도커 내부망 게이트웨이). RUNNER_SOCKET=<경로> 면 유닉스 소켓도 듣는다(컨테이너에 마운트해 연결). 0.0.0.0 은 외부 포트가 없는 컨테이너
  (delivery compose 의 runner) 안에서만 쓴다. 서버 호스트에서는 0.0.0.0 으로 두지 않는다.
- 같은 등록 도메인(unit)은 한 번에 하나, 전체 동시 실행은 config.max_parallel 개.
- 등록·해제·일시정지는 서버 셸의 runnerctl 로만 한다(HTTP 로는 못 바꾼다).
- 실행은 정해진 인자로만: 임의 docker 인자·ENUM_EXTRA_ARGS 를 받지 않는다.
- 누가·언제·무엇을 했는지 events 표와 events.jsonl 에 남긴다.

사용법: runner.py serve | ensure | ctl <명령> ...  (runnerctl 이 감싼다)
표준 라이브러리만 쓴다. 파이썬 3.9 이상.
"""
import argparse, csv, datetime, fcntl, glob, hashlib, hmac, json, os, re, secrets, signal, socketserver, sqlite3, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.environ.get("RUNNER_HOME") or os.path.dirname(os.path.abspath(__file__))
# 듣는 주소. 기본은 127.0.0.1(서버 설치 그대로). 컨테이너(delivery compose 의 runner)에서만 0.0.0.0 으로 바꾼다.
BIND = os.environ.get("RUNNER_BIND") or "127.0.0.1"
BINDS = [b.strip() for b in BIND.split(",") if b.strip()]
# 유닉스 소켓도 듣는다(선택). delivery be 컨테이너에 이 소켓 파일을 마운트하면 TCP·방화벽 없이 연결된다. 토큰은 똑같이 필요.
SOCKET = os.environ.get("RUNNER_SOCKET") or ""


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        sock, _ = super().get_request()
        return sock, ("unix-socket", 0)  # BaseHTTPRequestHandler 가 client_address[0] 를 쓴다
CONFIG_DEFAULTS = {
    "port": 8793,
    "max_parallel": 2,
    "runs_root": "/mnt/main/public/wise/enum-pilot-run",
    "seed_holds": "/mnt/apps/home/wise/enum-pilot-handoff/b2full-input/seed-holds.csv",
    "reference_db": "/mnt/main/public/wise/enum-pilot-run/reference_empty.db",
    "docker": "/mnt/apps/home/wise/bin/docker",
    "path_prefix": "/mnt/apps/home/wise/bin",
    "reserved_units": [],          # 지금 다른 방식으로 수집 중인 등록 도메인 (e-stat 등)
    "trial_default": 100,
    "trial_max": 1000,
    "large_threshold": 10000,      # 이 이상 예상되면 등록 때 --approved-large 가 있어야 resume 가능
    "fake_cmd": None,              # 시험용: run-pilot 대신 실행할 명령(목록). 운영에서는 null
    # 증분(incremental)용 이미 납품된 키 파일 폴더: <dir>/<site_id>.csv 또는 .jsonl 이 있으면 --known-keys 로 넘긴다.
    "known_keys_dir": None,
}
MODES = ("trial", "resume", "incremental")
# 증분은 앞쪽부터 읽는다: 최신순 목록만. crawler/enumeration/incremental.py support() 와 같은 규칙(그쪽이 실행 때 다시 판정).
FRONT_PAGEABLE = ("last_page_backward",)
TAG_RE = re.compile(r"^[0-9]{8}([-_][A-Za-z0-9]+)?$")
SITE_RE = re.compile(r"^[0-9A-Za-z._-]{1,120}$")
RUN_RE = re.compile(r"^r[0-9]{4,}$")
ACTIVE = ("queued", "running")
lock = threading.RLock()


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def path(*p):
    return os.path.join(HOME, *p)


def config():
    c = dict(CONFIG_DEFAULTS)
    if os.path.exists(path("config.json")):
        c.update(json.load(open(path("config.json"))))
    return c


def db():
    c = sqlite3.connect(path("runner.sqlite"), timeout=30, isolation_level=None)
    c.row_factory = sqlite3.Row
    c.executescript("""
      create table if not exists runs(run_id text primary key, site_id text not null, mode text not null, lim integer,
        status text not null, tag text, unit text, requested_by text, requested_at text, started_at text, finished_at text,
        pid integer, cancel_requested integer not null default 0, result text, reason text, requeues integer not null default 0);
      create table if not exists events(id integer primary key autoincrement, at text not null, actor text, action text not null,
        run_id text, site_id text, detail text);
      create table if not exists site_folder(site_id text primary key, tag text not null, assigned_at text not null);
      create table if not exists flags(name text primary key, value text);
    """)
    if "requeues" not in [r[1] for r in c.execute("pragma table_info(runs)")]:
        c.execute("alter table runs add column requeues integer not null default 0")
    return c


def event(c, actor, action, run_id=None, site_id=None, detail=None):
    row = {"at": now(), "actor": actor, "action": action, "run_id": run_id, "site_id": site_id, "detail": detail}
    c.execute("insert into events(at,actor,action,run_id,site_id,detail) values(?,?,?,?,?,?)",
              (row["at"], actor, action, run_id, site_id, None if detail is None else json.dumps(detail, ensure_ascii=False)))
    with open(path("events.jsonl"), "a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def registry():
    p = path("registry", "registry.json")
    return json.load(open(p)) if os.path.exists(p) else {}


def save_registry(reg):
    os.makedirs(path("registry", "plans"), exist_ok=True)
    tmp = path("registry", "registry.json.tmp")
    json.dump(reg, open(tmp, "w"), ensure_ascii=False, indent=1, sort_keys=True)
    os.replace(tmp, path("registry", "registry.json"))


def paused(c):
    r = c.execute("select value from flags where name='paused'").fetchone()
    return bool(r and r[0] == "1")


# ---------- 실행 중 판정 ----------

def docker_running():
    """실행 중인 enum-pilot-* 컨테이너의 태그(폴더) 집합. docker 를 못 부르면 None(=판정 불가, 시작하지 않음)."""
    try:
        r = subprocess.run([config()["docker"], "ps", "--format", "{{.Names}}"], capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return {n[len("enum-pilot-"):] for n in r.stdout.split() if n.startswith("enum-pilot-")}


def sites_in_folder(tag):
    """실행기 밖에서 돈 컨테이너가 어떤 사이트를 수집하는지: 그 폴더 상태 저장소의 enum_site (읽기 전용)."""
    st = os.path.join(config()["runs_root"], tag, "data", "state", "enumeration.sqlite")
    try:
        c = sqlite3.connect("file:%s?mode=ro" % st, uri=True, timeout=5)
        return [r[0] for r in c.execute("select site_id from enum_site where status='running'")] or [r[0] for r in c.execute("select site_id from enum_site")]
    except sqlite3.Error:
        return []


def unit_of(site_id, reg=None):
    reg = reg if reg is not None else registry()
    if site_id in reg:
        return reg[site_id]["unit"]
    units = load_units()
    return units.get(site_id)


def load_units():
    """lanes/*.csv 사본(units/)의 site_id → unit. 등록되지 않은 외부 실행의 도메인 판정용."""
    out = {}
    for f in glob.glob(path("units", "*.csv")):
        for r in csv.DictReader(open(f, encoding="utf-8-sig")):
            if r.get("site_id") and r.get("unit"):
                out[r["site_id"]] = r["unit"]
    return out


def busy(c, reg, tags):
    """지금 바쁜 (사이트, unit, 폴더, 판정 불가 폴더): 실행기 작업 + 실행기 밖 컨테이너.
    실행기 밖 컨테이너인데 사이트나 도메인을 알 수 없으면 판정 불가로 돌려 아무것도 새로 시작하지 않게 한다."""
    sites, units, folders, unknown = set(), set(), set(), set()
    for r in c.execute("select site_id,unit,tag from runs where status='running'"):
        sites.add(r["site_id"]); units.add(r["unit"]); folders.add(r["tag"])
    ours = {r["tag"] for r in c.execute("select tag from runs where status='running'")}
    for tag in (tags or set()) - ours:
        folders.add(tag)
        found = sites_in_folder(tag)
        if not found:
            unknown.add(tag)
        for sid in found:
            sites.add(sid)
            u = unit_of(sid, reg)
            if u:
                units.add(u)
            else:
                unknown.add(tag)
    return sites, units, folders, unknown


def pid_alive(pid, run_id):
    try:
        cmd = open("/proc/%d/cmdline" % pid, "rb").read().split(b"\0")
    except (OSError, TypeError):
        return False
    return b"_exec" in cmd and run_id.encode() in cmd


# ---------- 요청 판정 ----------

def incremental_refusal(e):
    """증분 불가 사유(없으면 None). 등록된 plan 사본만 읽는다(표준 라이브러리)."""
    try:
        plan = json.load(open(e["plan_file"]))
        site = dict(plan.get("defaults") or {}, **plan["sites"][e["site_id"]])
    except (OSError, ValueError, KeyError, TypeError):
        return "plan_unreadable"
    strategy = site.get("strategy")
    order = (site.get("incremental") or {}).get("order") if isinstance(site.get("incremental"), dict) else None
    if strategy == "oldest_first_sort":
        return "listing_order_oldest_first"
    if strategy not in FRONT_PAGEABLE:
        return "strategy_not_front_pageable:%s" % strategy
    if order in ("oldest_first", "unknown"):
        return "listing_order_%s" % order
    return None


def check_request(c, site_id, mode, limit):
    """(None, 정리된 limit) 또는 (이유 코드, 설명)."""
    cfg, reg = config(), registry()
    if paused(c):
        return "runner_paused", "실행기가 일시정지 상태"
    if site_id not in reg:
        return "plan_not_registered", "실행기에 등록된 plan 없음"
    e = reg[site_id]
    if not e.get("ready"):
        return "plan_not_ready", e.get("why") or "등록됐지만 실행 불가"
    if mode == "trial":
        limit = cfg["trial_default"] if limit is None else limit
        if type(limit) is not int or not 1 <= limit <= cfg["trial_max"]:
            return "bad_limit", "시험 건수는 1~%d" % cfg["trial_max"]
    elif mode == "incremental":
        # 증분은 plan 의 incremental.max_pages(기본 50쪽)로 묶이므로 큰 수집 승인이 필요 없다.
        if limit is not None:
            return "bad_limit", "증분에는 건수를 주지 않는다(plan 의 incremental.max_pages 로 묶인다)"
        why = incremental_refusal(e)
        if why:
            return "incremental_unsupported", "증분 불가: %s (최신순 목록 plan 만)" % why
    else:
        if limit is not None:
            return "bad_limit", "이어받기에는 건수를 주지 않는다"
        exp = e.get("expected_items")
        if not e.get("approved_large") and (exp is None or exp >= cfg["large_threshold"]):
            return "large_needs_approval", "예상 건수 %s — 1만 건 이상이거나 모르면 등록 때 사용자 승인(--approved-large) 필요" % ("미상" if exp is None else "%d건" % exp)
    if e["unit"] in cfg["reserved_units"]:
        return "unit_reserved", "%s 는 운영 수집 중으로 예약됨" % e["unit"]
    if c.execute("select 1 from runs where site_id=? and status in ('queued','running')", (site_id,)).fetchone():
        return "site_busy", "같은 사이트 작업이 이미 대기·실행 중"
    tags = docker_running()
    if tags is None:
        return "docker_unavailable", "docker 상태를 읽지 못함"
    sites, units, folders, unknown = busy(c, reg, tags)
    if unknown:
        return "external_unknown", "실행기 밖 컨테이너의 사이트·도메인을 알 수 없음: %s" % ", ".join(sorted(unknown))
    if site_id in sites:
        return "site_busy", "같은 사이트가 실행기 밖에서 실행 중"
    if e["unit"] in units or c.execute("select 1 from runs where unit=? and status='queued'", (e["unit"],)).fetchone():
        return "unit_busy", "같은 등록 도메인(%s) 작업이 대기·실행 중" % e["unit"]
    if mode in ("resume", "incremental") and folder_for(c, site_id, assign=False) in folders:
        return "folder_busy", "같은 폴더 컨테이너가 실행 중"
    return None, limit


def folder_for(c, site_id, assign=True):
    e = registry().get(site_id) or {}
    if e.get("folder"):
        return e["folder"]
    r = c.execute("select tag from site_folder where site_id=?", (site_id,)).fetchone()
    if r:
        return r[0]
    if not assign:
        return None
    n = c.execute("select count(*) from site_folder").fetchone()[0] + 1
    tag = "%s-s%d" % (time.strftime("%Y%m%d"), n)
    c.execute("insert into site_folder values(?,?,?)", (site_id, tag, now()))
    return tag


def next_run_id(c):
    r = c.execute("select max(cast(substr(run_id,2) as integer)) from runs").fetchone()[0] or 0
    return "r%04d" % (r + 1)


# ---------- 실행 ----------

def scheduler():
    while True:
        try:
            tick()
        except Exception as e:  # 한 번 실패해도 계속 돈다
            print("%s scheduler error: %s" % (now(), e), flush=True)
        time.sleep(3)


def tick():
    cfg = config()
    with lock:
        c = db()
        # 끝난 작업 정리: _exec 가 스스로 기록하지만, 죽은 경우를 여기서 잡는다
        tags = docker_running()
        for r in c.execute("select * from runs where status='running'").fetchall():
            if pid_alive(r["pid"], r["run_id"]):
                continue
            if tags is not None and r["tag"] in tags:
                continue  # _exec 는 죽었지만 컨테이너가 아직 돈다: 끝날 때까지 기다린다
            if tags is None:
                continue
            finish(c, r, None, "interrupted")
        if paused(c) or tags is None:
            return
        running = c.execute("select count(*) from runs where status='running'").fetchone()[0]
        reg = registry()
        for r in c.execute("select * from runs where status='queued' order by run_id").fetchall():
            if running >= cfg["max_parallel"]:
                break
            sites, units, folders, unknown = busy(c, reg, tags)
            if unknown:
                break  # 실행기 밖 컨테이너를 판정할 수 없으면 아무것도 시작하지 않는다
            if r["site_id"] not in reg or not reg[r["site_id"]].get("ready") or r["unit"] in cfg["reserved_units"]:
                c.execute("update runs set status='failed', finished_at=?, reason='plan_not_ready' where run_id=? and status='queued'", (now(), r["run_id"]))
                event(c, "runner", "fail", r["run_id"], r["site_id"], {"reason": "plan_not_ready"})
                continue
            # 증분은 이어받기 폴더(그 사이트의 상태 저장소·문서 DB)에서 돈다: 이미 받은 것을 알고, 가져오기 경로도 같다.
            tag = r["tag"] or (folder_for(c, r["site_id"]) if r["mode"] in ("resume", "incremental") else "%s-t%s" % (time.strftime("%Y%m%d"), r["run_id"][1:]))
            if r["site_id"] in sites or r["unit"] in units or tag in folders:
                continue
            # 상태·폴더를 먼저 쓰고 나서 실행 프로세스를 띄운다(자식이 빈 폴더 값을 읽지 않게)
            if c.execute("update runs set status='running', tag=?, started_at=?, pid=null where run_id=? and status='queued'", (tag, now(), r["run_id"])).rowcount != 1:
                continue
            log = open(path("logs", r["run_id"] + ".log"), "a")
            p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "_exec", r["run_id"]], stdout=log, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True, cwd=HOME, env=dict(os.environ, RUNNER_HOME=HOME))
            c.execute("update runs set pid=? where run_id=?", (p.pid, r["run_id"]))
            event(c, "runner", "start", r["run_id"], r["site_id"], {"tag": tag, "pid": p.pid})
            running += 1


def read_result(tag, site_id):
    st = os.path.join(config()["runs_root"], tag, "data", "state", "enumeration.sqlite")
    try:
        c = sqlite3.connect("file:%s?mode=ro" % st, uri=True, timeout=10)
        s = c.execute("select status,stop_reason from enum_site where site_id=?", (site_id,)).fetchone()
        items = dict(c.execute("select status,count(*) from enum_item where site_id=? group by 1", (site_id,)).fetchall())
        out = {"site_status": s[0] if s else None, "stop_reason": s[1] if s else None, "saved": items.get("saved", 0),
               "failed": items.get("failed_permanent", 0)}
        st = c.execute("select state from enum_site where site_id=?", (site_id,)).fetchone()
        inc = (json.loads(st[0] or "{}") if st else {}).get("incremental")
        if isinstance(inc, dict):
            out["incremental"] = {k: inc.get(k) for k in ("status", "stop_reason", "requests")}
            out["incremental"]["saved_new"] = (inc.get("stats") or {}).get("saved_new")
        return out
    except sqlite3.Error as e:
        return {"error": str(e)[:200]}


MAX_REQUEUES = 3


def finish(c, r, rc, why=None):
    """작업 끝 기록. rc None = 실행 프로세스가 사라짐(재부팅 등). 아직 running 인 행만 바꾼다(다른 프로세스가 먼저 끝냈으면 그대로)."""
    cur = c.execute("select status,cancel_requested,requeues from runs where run_id=?", (r["run_id"],)).fetchone()
    if not cur or cur["status"] != "running":
        return
    cancelled = cur["cancel_requested"]
    result = read_result(r["tag"], r["site_id"]) if r["tag"] else None
    if cancelled:
        status, reason = "cancelled", "cancel_requested"
    elif rc == 0:
        status, reason = "done", None
    elif rc is None and r["mode"] in ("resume", "incremental") and cur["requeues"] < MAX_REQUEUES:
        # 이어받기는 같은 폴더로 다시 돌리면 상태 저장소가 이어받는다: 대기열로 되돌린다(최대 MAX_REQUEUES 번)
        if c.execute("update runs set status='queued', pid=null, requeues=requeues+1, reason='requeued_after_interrupt' where run_id=? and status='running'", (r["run_id"],)).rowcount:
            event(c, "runner", "requeue", r["run_id"], r["site_id"], {"why": why, "requeues": cur["requeues"] + 1})
        return
    else:
        status, reason = "failed", why or ("exit_%s" % rc)
    if not c.execute("update runs set status=?, finished_at=?, result=?, reason=? where run_id=? and status='running'",
                     (status, now(), json.dumps(result, ensure_ascii=False) if result else None, reason, r["run_id"])).rowcount:
        return
    event(c, "runner", "finish", r["run_id"], r["site_id"], {"status": status, "reason": reason, "result": result})


def trial_plan(e, run_id, limit):
    """시험 수집용 plan 사본: 요청 상한을 limit 기준으로 낮춘다(틀에 '건수' 옵션이 없어서)."""
    plan = json.load(open(e["plan_file"]))
    site = plan["sites"][e["site_id"]]
    pages = max(2, -(-limit // 10))
    budget = int(limit * 1.2) + pages + 10
    site["max_requests"] = min(site.get("max_requests") or budget, budget)
    site["max_pages"] = min(site.get("max_pages") or pages, pages)
    out = path("logs", run_id + ".plan.json")
    json.dump(plan, open(out, "w"), ensure_ascii=False, indent=1)
    return out


def exec_run(run_id):
    """자식 프로세스: build → run. 실행기가 다시 떠도 이 프로세스는 계속 돈다. 어떤 오류든 failed 로 남긴다."""
    if not RUN_RE.match(run_id):
        sys.exit(2)
    c = db()
    try:
        rc, why = _exec(c, run_id), None
    except Exception as e:
        print("%s exec_error: %r" % (now(), e), flush=True)
        rc, why = 1, "exec_error: %s" % str(e)[:150]
    with lock:
        finish(c, c.execute("select * from runs where run_id=?", (run_id,)).fetchone(), rc, why)


def _exec(c, run_id):
    cfg = config()
    r = c.execute("select * from runs where run_id=?", (run_id,)).fetchone()
    e = registry().get(r["site_id"])
    if not e:
        raise RuntimeError("등록 해제됨")
    tag = r["tag"] or ""
    if not TAG_RE.match(tag) or not SITE_RE.match(r["site_id"]):
        raise RuntimeError("폴더·사이트 형식 오류")
    if hashlib.sha256(open(e["plan_file"], "rb").read()).hexdigest() != e["sha256"]:
        raise RuntimeError("등록된 plan 파일이 등록 뒤 바뀜(sha256 불일치)")
    plan = trial_plan(e, run_id, r["lim"]) if r["mode"] == "trial" else e["plan_file"]
    env = dict(os.environ, PATH=cfg["path_prefix"] + ":" + os.environ.get("PATH", "/usr/bin:/bin"),
               ENUM_PILOT_ROOT=cfg["runs_root"], ENUM_PILOT_DATE=tag, ENUM_SEED_HOLDS=cfg["seed_holds"],
               ENUM_ALLOW_EMPTY_REFERENCE="1", ENUM_REFERENCE_DB=cfg["reference_db"])
    for k in ("ENUM_EXTRA_ARGS", "ENUM_FILES", "ENUM_WITH_BROWSER", "ENUM_INCREMENTAL", "ENUM_KNOWN_KEYS"):
        env.pop(k, None)
    if e.get("files"):
        env["ENUM_FILES"] = "1"
    if r["mode"] == "incremental":
        why = incremental_refusal(e)
        if why:
            raise RuntimeError("증분 불가: %s" % why)
        env["ENUM_INCREMENTAL"] = "1"
        if cfg.get("known_keys_dir"):
            for ext in (".csv", ".jsonl"):
                kk = os.path.join(cfg["known_keys_dir"], r["site_id"] + ext)
                if os.path.isfile(kk):
                    env["ENUM_KNOWN_KEYS"] = kk
                    break
    script = os.path.join(e["src"], "deploy", "enum-pilot", "run-pilot.sh")
    steps = [cfg["fake_cmd"]] if cfg.get("fake_cmd") else [[script, "build"], [script, "run", plan, r["site_id"]]]
    rc = 0
    for cmd in steps:
        print("%s $ %s" % (now(), " ".join(cmd)), flush=True)
        if c.execute("select cancel_requested from runs where run_id=?", (run_id,)).fetchone()[0]:
            rc = 130
            break
        rc = subprocess.call(cmd, cwd=e["src"], env=env)
        print("%s rc=%s" % (now(), rc), flush=True)
        if rc != 0:
            break
    return rc


def cancel(c, run_id, actor):
    r = c.execute("select * from runs where run_id=?", (run_id,)).fetchone()
    if not r:
        return 404, {"reason": "not_found"}
    if r["status"] not in ACTIVE:
        return 409, {"reason": "not_active", "detail": r["status"]}
    event(c, actor, "cancel", run_id, r["site_id"])
    if r["status"] == "queued":
        c.execute("update runs set status='cancelled', finished_at=?, reason='cancelled_while_queued', cancel_requested=1 where run_id=?", (now(), run_id))
        return 200, {"run_id": run_id, "status": "cancelled"}
    c.execute("update runs set cancel_requested=1 where run_id=?", (run_id,))
    threading.Thread(target=stop_run, args=(run_id, r["tag"], r["pid"]), daemon=True).start()  # docker stop 은 잠금 밖에서
    return 200, {"run_id": run_id, "status": "cancelling"}


def stop_run(run_id, tag, pid):
    if TAG_RE.match(tag or ""):
        subprocess.run([config()["docker"], "stop", "enum-pilot-" + tag], capture_output=True, timeout=120)
    if pid and pid_alive(pid, run_id):
        try:
            os.killpg(pid, signal.SIGTERM)  # build 단계였다면 그 프로세스 묶음을 멈춘다
        except OSError:
            pass


# ---------- HTTP ----------

def run_row(r):
    return {"run_id": r["run_id"], "site_id": r["site_id"], "mode": r["mode"], "limit": r["lim"], "status": r["status"],
            "run_folder": r["tag"], "started_at": r["started_at"], "finished_at": r["finished_at"], "requested_at": r["requested_at"],
            "requested_by": r["requested_by"], "result": json.loads(r["result"]) if r["result"] else None, "reason": r["reason"]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def ok_token(self):
        want = open(path(".token")).read().strip()
        return hmac.compare_digest(self.headers.get("X-Runner-Token", "").encode(), want.encode())

    def do_GET(self):
        if not self.ok_token():
            return self.reply(401, {"reason": "bad_token"})
        p, _, q = self.path.partition("?")
        if p == "/runner/plans":
            return self.reply(200, [dict({k: e.get(k) for k in ("site_id", "plan_file", "strategy", "unit", "folder", "expected_items", "approved_large", "ready", "why", "registered_at")},
                                         incremental_refusal=incremental_refusal(e))
                                    for e in registry().values()])
        if p == "/runner/runs":
            m = re.search(r"(?:^|&)site_id=([0-9A-Za-z._-]+)", q)
            with lock:
                c = db()
                rows = c.execute("select * from runs where site_id=? order by run_id desc limit 200", (m.group(1),)) if m else c.execute("select * from runs order by run_id desc limit 500")
                return self.reply(200, [run_row(r) for r in rows])
        if p == "/runner/health":
            with lock:
                c = db()
                return self.reply(200, {"paused": paused(c), "max_parallel": config()["max_parallel"],
                                        "running": c.execute("select count(*) from runs where status='running'").fetchone()[0],
                                        "queued": c.execute("select count(*) from runs where status='queued'").fetchone()[0]})
        return self.reply(404, {"reason": "not_found"})

    def do_POST(self):
        if not self.ok_token():
            return self.reply(401, {"reason": "bad_token"})
        try:
            data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except ValueError:
            return self.reply(400, {"reason": "bad_json"})
        if not isinstance(data, dict):
            return self.reply(400, {"reason": "bad_json"})
        actor = str(data.get("requested_by") or "unknown")[:80]
        m = re.fullmatch(r"/runner/runs/(r[0-9]{4,})/cancel", self.path)
        with lock:
            c = db()
            if m:
                code, obj = cancel(c, m.group(1), actor)
                return self.reply(code, obj)
            if self.path != "/runner/runs":
                return self.reply(404, {"reason": "not_found"})
            site_id, mode, limit = data.get("site_id"), data.get("mode"), data.get("limit")
            if not isinstance(site_id, str) or not SITE_RE.match(site_id) or mode not in MODES:
                return self.reply(400, {"reason": "bad_request"})
            reason, x = check_request(c, site_id, mode, limit)
            if reason:
                event(c, actor, "reject", None, site_id, {"mode": mode, "limit": limit, "reason": reason})
                return self.reply(409, {"reason": reason, "detail": x})
            run_id = next_run_id(c)
            c.execute("insert into runs(run_id,site_id,mode,lim,status,unit,requested_by,requested_at) values(?,?,?,?,?,?,?,?)",
                      (run_id, site_id, mode, x if mode == "trial" else None, "queued", registry()[site_id]["unit"], actor, now()))
            event(c, actor, "request", run_id, site_id, {"mode": mode, "limit": x if mode == "trial" else None})
            return self.reply(201, {"run_id": run_id})


def serve():
    for d in ("logs", "registry", "units"):
        os.makedirs(path(d), exist_ok=True)
    if not os.path.exists(path(".token")):
        fd = os.open(path(".token"), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, secrets.token_hex(24).encode())
        os.close(fd)
    lk = open(path("runner.lock"), "w")
    try:
        fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)  # 실행기는 하나만
    except OSError:
        sys.exit("runner already running (runner.lock)")
    cfg = config()
    servers = [ThreadingHTTPServer((b, cfg["port"]), H) for b in BINDS]
    open(path("runner.pid"), "w").write(str(os.getpid()))
    with lock:
        c = db()
        event(c, "runner", "serve_start", detail={"pid": os.getpid()})
    threading.Thread(target=scheduler, daemon=True).start()
    if SOCKET:
        if os.path.exists(SOCKET):
            os.unlink(SOCKET)  # 앞 실행이 남긴 소켓 파일(runner.lock 으로 실행기는 하나만)
        os.makedirs(os.path.dirname(SOCKET) or ".", exist_ok=True)
        old = os.umask(0o117)
        try:
            servers.append(UnixHTTPServer(SOCKET, H))
        finally:
            os.umask(old)
        os.chmod(SOCKET, 0o660)
    for extra in servers[1:]:
        threading.Thread(target=extra.serve_forever, daemon=True).start()
    print("%s runner on %s:%d%s home=%s" % (now(), ",".join(BINDS), cfg["port"], " + unix:" + SOCKET if SOCKET else "", HOME), flush=True)
    servers[0].serve_forever()


def runner_alive():
    try:
        pid = int(open(path("runner.pid")).read())
        cmd = open("/proc/%d/cmdline" % pid, "rb").read()
        return b"runner.py" in cmd and b"serve" in cmd
    except (OSError, ValueError):
        return False


def ensure():
    if runner_alive():
        print("runner already running")
        return
    os.makedirs(path("logs"), exist_ok=True)
    log = open(path("logs", "runner.log"), "a")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"], stdout=log, stderr=subprocess.STDOUT,
                     stdin=subprocess.DEVNULL, start_new_session=True, cwd=HOME, env=dict(os.environ, RUNNER_HOME=HOME))
    print("runner started")


# ---------- runnerctl ----------

def validate_plan(src, plan_path, site_id):
    """소스 폴더의 plan.py 로 그 사이트를 검증. (ok, 메시지)"""
    code = ("import sys; sys.path.insert(0, %r)\nfrom crawler.enumeration.plan import load_plan, site_plan\n"
            "site_plan(load_plan(%r), %r); print('ok')") % (src, plan_path, site_id)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60, cwd=src)
    return r.returncode == 0, (r.stdout + r.stderr).strip()[-300:]


def ctl(argv):
    ap = argparse.ArgumentParser(prog="runnerctl")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("register", help="plan 등록(사이트 하나)")
    g.add_argument("--site", required=True)
    g.add_argument("--plan", required=True)
    g.add_argument("--src", required=True, help="deploy/enum-pilot/run-pilot.sh 가 있는 소스 폴더")
    g.add_argument("--unit", help="등록 도메인. 없으면 units/*.csv 에서 찾는다")
    g.add_argument("--folder", help="이어받기 폴더(YYYYMMDD-태그). 기존 수집을 이어받을 때")
    g.add_argument("--expected", type=int, help="예상 건수")
    g.add_argument("--approved-large", action="store_true", help="1만 건 이상(또는 미상) 이어받기를 사용자가 승인함")
    g.add_argument("--files", action="store_true", help="파일(PDF) 단계도 실행")
    g.add_argument("--not-ready", metavar="WHY", help="등록하되 실행 불가로 표시")
    g.add_argument("--skip-validate", metavar="WHY", help="서버 파이썬으로 plan.py 를 못 불러올 때: 로컬 검증 근거를 적는다")
    u = sub.add_parser("unregister"); u.add_argument("--site", required=True)
    sub.add_parser("list"); r = sub.add_parser("runs"); r.add_argument("--site")
    sub.add_parser("pause"); sub.add_parser("unpause"); sub.add_parser("status")
    e = sub.add_parser("events"); e.add_argument("-n", type=int, default=30)
    a = ap.parse_args(argv)
    actor = "shell:%s" % os.environ.get("USER", "?")
    c = db()
    if a.cmd == "register":
        if not SITE_RE.match(a.site) or (a.folder and not TAG_RE.match(a.folder)):
            sys.exit("사이트 ID 또는 폴더 형식 오류")
        src = os.path.realpath(a.src)
        if not os.path.isfile(os.path.join(src, "deploy", "enum-pilot", "run-pilot.sh")):
            sys.exit("run-pilot.sh 없음: %s" % src)
        plan = json.load(open(a.plan))
        if a.site not in (plan.get("sites") or {}):
            sys.exit("plan 에 사이트 없음: %s" % a.site)
        one = {"version": plan.get("version", 1), "defaults": plan.get("defaults") or {}, "sites": {a.site: plan["sites"][a.site]}}
        os.makedirs(path("registry", "plans"), exist_ok=True)
        dest = path("registry", "plans", a.site + ".json")
        json.dump(one, open(dest, "w"), ensure_ascii=False, indent=1)
        if a.skip_validate:
            ok, msg = True, "검증 생략: " + a.skip_validate
        else:
            ok, msg = validate_plan(src, dest, a.site)
            if not ok:
                os.remove(dest)
                sys.exit("plan 검증 실패: " + msg)
        unit = a.unit or load_units().get(a.site)
        if not unit:
            os.remove(dest)
            sys.exit("등록 도메인을 모름: --unit 으로 주거나 units/*.csv 에 추가")
        e = {"site_id": a.site, "plan_file": dest, "sha256": hashlib.sha256(open(dest, "rb").read()).hexdigest(), "src": src,
             "unit": unit, "folder": a.folder, "expected_items": a.expected, "approved_large": a.approved_large, "files": a.files,
             "strategy": one["sites"][a.site].get("strategy"), "ready": not a.not_ready, "why": a.not_ready, "validation": msg,
             "registered_at": now(), "registered_by": actor}
        with lock:
            reg = registry(); reg[a.site] = e; save_registry(reg)
            event(c, actor, "register", site_id=a.site, detail={k: e[k] for k in ("sha256", "src", "unit", "folder", "expected_items", "approved_large", "ready")})
        print(json.dumps(e, ensure_ascii=False, indent=1))
    elif a.cmd == "unregister":
        with lock:
            reg = registry()
            if reg.pop(a.site, None) is None:
                sys.exit("등록 안 됨")
            save_registry(reg)
            event(c, actor, "unregister", site_id=a.site)
    elif a.cmd == "list":
        for e in registry().values():
            print("%-40s %-6s %-22s %s %s" % (e["site_id"], "ready" if e["ready"] else "NO", e["unit"], e.get("folder") or "-", e.get("why") or ""))
    elif a.cmd == "runs":
        q = "select * from runs" + (" where site_id=?" if a.site else "") + " order by run_id desc limit 50"
        for r in c.execute(q, (a.site,) if a.site else ()):
            print("%s %-9s %-6s %-36s %-20s %s %s" % (r["run_id"], r["status"], r["mode"], r["site_id"], r["tag"] or "-", r["requested_by"], r["reason"] or ""))
    elif a.cmd in ("pause", "unpause"):
        c.execute("insert or replace into flags values('paused',?)", ("1" if a.cmd == "pause" else "0",))
        event(c, actor, a.cmd)
    elif a.cmd == "status":
        print("runner:", "running" if runner_alive() else "NOT running (runnerctl ensure)", "| paused:", paused(c))
        for s, n in c.execute("select status,count(*) from runs group by 1"):
            print(" ", s, n)
    elif a.cmd == "events":
        for r in reversed(c.execute("select * from events order by id desc limit ?", (a.n,)).fetchall()):
            print(r["at"], r["actor"], r["action"], r["run_id"] or "", r["site_id"] or "", r["detail"] or "")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "serve":
        serve()
    elif len(sys.argv) >= 2 and sys.argv[1] == "ensure":
        ensure()
    elif len(sys.argv) == 3 and sys.argv[1] == "_exec":
        exec_run(sys.argv[2])
    elif len(sys.argv) >= 2 and sys.argv[1] == "ctl":
        ctl(sys.argv[2:])
    else:
        sys.exit(__doc__)
