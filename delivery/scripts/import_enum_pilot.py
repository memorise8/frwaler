#!/usr/bin/env python3
"""enum-pilot 이 받은 문서(enum_pilot.db, 여러 실행 폴더)나 ScienceON 격리 DB 를 제품 DB(documents)로 옮기는 가져오기.

기본은 건조 실행(dry-run): 아무것도 쓰지 않고 사이트별로 "새로 넣을 것 / 이미 있는 것 / 충돌" 을 센다.
  - 원본은 수집 서버에만 있다(로컬 복사 금지). 서버에서 키·검사값만 SSH 로 흘려 받아 메모리에서 비교한다.
  - 대상은 제품 스키마 Postgres 컨테이너(`docker exec <컨테이너> psql`). 시험 스택 ui-check-20261013 기준으로 만들었다.

모드
  plan           (기본) 건조 실행 보고서
  rollback-test  사이트마다 새 문서 표본 N건을 트랜잭션 안에서 넣어 보고 ROLLBACK (영구 변경 없음)
  apply          실제 반영. IMPORT_APPLY_APPROVED=<배치 ID> 환경변수와 --batch 가 같아야만 돈다(사용자 승인용)
  rollback       apply 가 남긴 매니페스트로 그 배치에서 넣은 문서만 지운다

중복 판정은 제품과 같다(crawler/db_pg.find_by_dedup_key): (site_id, post_number, meta_url) 완전 일치면 이미 있음.
그 위에 가져오기 전용 안전 규칙:
  - 같은 post_number 가 대상에 다른 meta_url 로 있으면 post_conflict (넣지 않음, 검토)
  - 같은 meta_url 이 대상에 다른 post_number 로 있으면 url_conflict (식별값 규칙이 기존 수집과 다를 위험, 검토)
  - 원본 폴더끼리 같은 post_number 에 meta_url 이 다르면 source_conflict (그 post_number 는 넣지 않음)
  - 식별 규칙이 따로 있는 사이트(crawler/document_identity.py)는 자동 반영하지 않고 검토로 둔다
여러 실행 폴더 합치기: 같은 키는 하나로, 내용이 다르면 가장 최근 폴더(최대 collected_at) 의 행을 쓴다.
PDF: 원본 문서의 seq_id 경로 blob 을 쓰므로 새 seq_id 로 옮겨야 한다. 지금 원본은 pdf_downloaded=0 뿐이라 메타데이터만 옮기고,
     pdf_downloaded·text_extracted 는 0 으로 넣어 제품 파일 작업이 다시 받게 한다(파일이 있는 원본이 생기면 blob 복사 단계를 추가).
"""
import argparse, base64, csv, datetime, glob, hashlib, json, os, re, subprocess, sys

SSH = ["ssh", "-p", "57722", "-o", "BatchMode=yes", "wise@cli.pubdt.com"]
RUNS_ROOT = "/mnt/main/public/wise/enum-pilot-run"
SITE_RE = re.compile(r"^[0-9A-Za-z._-]{1,120}$")
FOLDER_RE = re.compile(r"^[0-9A-Za-z._-]+$")
# crawler/db_pg.DOCUMENT_INSERT_FIELDS 와 같은 순서(fts·seq_id 제외). collected_at 은 원본 값을 보존한다
FIELDS = ("collected_at", "site_id", "post_number", "meta_url", "title", "published_date", "listed_date", "authors", "publisher",
          "journal", "pdf_url", "keywords", "abstract", "original_filename", "pdf_downloaded", "text_extracted", "pdf_size_bytes",
          "pdf_sha256", "summary", "summary_model", "summary_at",
          "published_date_raw", "published_date_status", "published_date_precision",
          "listed_date_raw", "listed_date_status", "listed_date_precision")
CONTENT = ("title", "published_date", "listed_date", "authors", "publisher", "journal", "pdf_url", "keywords", "abstract",
           "published_date_raw", "published_date_status", "listed_date_raw", "listed_date_status")
# crawler/document_identity.py 에 사이트별 식별 규칙이 있는 곳: 자동 반영 금지
SPECIAL_IDENTITY = {"krivet-re-kr", "kei-re-kr-eliblistes", "cso-ie-en"}
# 등록 수집기가 post_number 하나에 여러 행(meta_url 다름, 예: 같은 호의 언어별 PDF)을 저장하는 사이트.
# 틀 plan 의 adapter.allow_shared_post_number 와 짝: 같은 post_number·다른 meta_url 은 충돌이 아니라 다른 문서.
# meri: 사용자 결정 10-18, bmi(PDF 폴더명 공유, fd914ae): 추천안·사용자 위임 10-19.
SHARED_POST_NUMBER = {"meri-belspo-be-site", "bmi-gv-at-114"}
# 등록 수집기가 목록·시작 쪽 주소를 모든 문서의 meta_url 로 넣는 사이트(한 meta_url 에 여러 post_number).
# 운영 DB 기존 납품도 같은 모양임을 읽기 전용 집계로 확인한 곳만(사용자 결정 10-19: "기존 납품과 같은지 보고 결정").
# 한 URL 의 여러 post_number 는 충돌이 아니라 다른 문서 — 식별은 post_number 로.
# 10-19 운영 집계(문서/고유 meta_url): garda 94/1, kefm 101/1, csic 25/1, um-dk 24/1, justitsministeriet 24/1, bmwkms 41/2,
# pbocri-lnzy3 8/1, dhs-gov-publication 29/1, bmleh 26/17(direct_pdf 만 목록 주소 — 새 수집 26/17 과 같은 모양).
SHARED_META_URL = {"garda-ie-en", "kefm-dk-aftaler", "csic-es-es", "um-dk-om-os", "justitsministeriet-dk-forskning",
                   "bmwkms-gv-at-themen", "pbocri-org-cn-lnzy3html", "dhs-gov-publication", "bmleh-de-en"}
# review 를 구역 세션이 검토해 받아들인 사이트: 새 문서 목록 해시(new_sha)가 그때와 같을 때만 ready 로 본다.
# em-lyon-com-en: 폴더 안 같은 meta_url 에 post_number 2개인 2행 제외한 채 845건(collect-sites 10-20, work/ND/decisions.csv).
REVIEW_ACCEPTED = {"em-lyon-com-en": "f837027769dae5908f1bb9a3ff521f7682848d31d22bc0ac154da9a57bb7e6b9"}
# 구역 세션 판정(dev-flow site-status/*.csv)이 이러면 자동 반영하지 않는다
HOLD_STATES = {"needs_dev", "blocked_access", "moved", "no_crawler", "skip", "plan_written"}
# 제품 sites 에 없는 enum 사이트 ID 는 crawler/sites/configs/<id>.json 의 별도 설정형 수집기다(납품 806곳 밖).
# 납품 수집기와 식별값 규칙이 달라 ID 만 바꿔 넣으면 나중 제품 수집분과 중복된다 → 이름 바꾸기 금지.
# (2026-10-18 메인 세션 확인)
RECOLLECT_AS = {  # 범위는 같음: 납품 수집기 ID 로 다시 수집해야 함
    "esri-ie": "esri-ie-publications", "data-aeronomie-be": "data-aeronomie-be-dataset",
    "nrscotland-gov-uk": "nrscotland-gov-uk-publications", "nioo-knaw-nl": "nioo-knaw-nl-en"}
NOT_DELIVERY = {  # 범위가 다름 또는 중복: 가져오지 않음
    "sfi-ch": "보도자료(sfi-ch-en 은 publications)", "skillsdevelopmentscotland-co-uk": "news-events(납품은 publications API)",
    "dhs-gov-publications": "/publications(납품 dhs-gov-publication 은 다른 한 쪽)", "cerema-fr": "cerema-fr-fr document 구간과 같은 범위(중복)"}
TRIAL_TAG = re.compile(r"-(t[0-9a-z]+|b2probe[0-9]*)$")

# 서버에서 도는 읽기 전용 조회. 입력은 JSON 한 덩어리, 출력은 JSON 줄
SERVER_SQLITE = r'''
import glob, hashlib, json, os, sqlite3, sys
q = json.loads(sys.stdin.readline())
R, sid = q["root"], q["site"]
CONTENT = q["content"]
out = sys.stdout
keys = {}
if q["mode"] == "rows":
    for line in sys.stdin:
        k = json.loads(line)
        keys.setdefault(k[2], set()).add((k[0], k[1]))
dbs = sorted(glob.glob(R + "/*/data/enum_pilot.db"))
for p in dbs:
    folder = p[len(R) + 1:].split("/")[0]
    if q.get("folders") and folder not in q["folders"]:
        continue
    try:
        c = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=10)
        n, mx = c.execute("select count(*), max(collected_at) from documents where site_id=?", (sid,)).fetchone()
    except sqlite3.Error as e:
        out.write(json.dumps({"t": "error", "folder": folder, "error": str(e)[:200]}) + "\n"); continue
    if not n:
        continue
    st, fps = None, set()
    for sp in sorted(glob.glob("%s/%s/data/state/enumeration.sqlite" % (R, folder)) + glob.glob("%s/%s/data/enumeration-*/enumeration.sqlite" % (R, folder))):
        try:
            s = sqlite3.connect("file:%s?mode=ro" % sp, uri=True, timeout=10)
            r = s.execute("select status, stop_reason, plan_fp from enum_site where site_id=?", (sid,)).fetchone()
        except sqlite3.Error:
            continue
        if r:
            fps.add(r[2])
            if sp.endswith("/state/enumeration.sqlite"):
                st = r[:2]
    out.write(json.dumps({"t": "folder", "folder": folder, "n": n, "max_collected": mx, "state": st, "plan_fp": sorted(fps)}) + "\n")
    if q["mode"] == "keys":
        cols = ",".join("coalesce(%s,'')" % k for k in CONTENT)
        for row in c.execute("select cast(post_number as text), meta_url, collected_at, coalesce(title,'')<>'', coalesce(pdf_downloaded,0), " + cols + " from documents where site_id=?", (sid,)):
            h = hashlib.md5("\x1f".join(str(x) for x in row[5:]).encode()).hexdigest()[:16]
            out.write(json.dumps([folder, row[0], row[1], row[2], int(row[3]), row[4], h, row[5 + CONTENT.index("published_date_status")]]) + "\n")
    else:
        want = keys.get(folder)
        if not want:
            continue
        c.row_factory = sqlite3.Row
        # 같은 키가 폴더 안에 여러 행이면 가장 최근 것 하나만
        for row in c.execute("select * from documents where site_id=? order by collected_at desc, seq_id desc", (sid,)):
            k = (None if row["post_number"] is None else str(row["post_number"]), row["meta_url"])
            if k in want:
                want.discard(k)
                d = {k2: row[k2] for k2 in row.keys() if k2 in q["fields"]}
                d["post_number"] = k[0]
                out.write(json.dumps({"t": "row", "folder": folder, "row": d}, ensure_ascii=False) + "\n")
'''


# 서버 안에서 도는 넣기: 원본 sqlite 행을 읽어 같은 서버의 대상 컨테이너 psql 로 넣는다. 문서 행은 서버
# 밖으로 나오지 않고, 이 컴퓨터로는 청크마다 건수와 (seq_id, post_number, meta_url) 만 돌아온다.
# 매니페스트는 서버에도 fsync 해서 남긴다(연결이 끊겨도 되돌릴 수 있게). 아래 copy_text·insert_sql 소스를 붙여 보낸다.
SERVER_APPLY_MAIN = r'''
import glob, json, os, sqlite3, subprocess, sys, time
q = json.loads(sys.stdin.readline())
FIELDS = tuple(q["fields"])
want = {}
for line in sys.stdin:
    k = json.loads(line)
    want.setdefault(k[2], set()).add((k[0], k[1]))
manifest = os.path.expanduser(q["manifest"]) if q["finish"] == "COMMIT" else None
if manifest:
    os.makedirs(os.path.dirname(manifest), exist_ok=True)
psql = ["docker", "exec", "-i", q["target"], "psql", "-U", "libertree", "-d", "libertree", "-At", "-F", "\t"]
def flush(rows):
    r = subprocess.run(psql, input=insert_sql(q["product"], rows, q["finish"]), capture_output=True, text=True)
    if r.returncode != 0:
        print(json.dumps({"t": "error", "error": r.stderr[-500:]}), flush=True)
        sys.exit(2)
    out = [l.split("\t") for l in r.stdout.splitlines()]
    got = [[int(x[1]), x[2], x[3]] for x in out if x[0] == "row"]
    if manifest:
        with open(manifest, "a") as f:
            for seq, post, url in got:
                f.write(json.dumps({"batch": q["batch"], "target": q["target"], "seq_id": seq, "site_id": q["product"],
                                    "post_number": post, "meta_url": url}, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
    print(json.dumps({"t": "chunk", "tried": len(rows), "rows": got,
                      "inserted": next((int(x[1]) for x in out if x[0] == "inserted"), 0),
                      "fts_ok": next((int(x[1]) for x in out if x[0] == "fts_ok"), 0)}, ensure_ascii=False), flush=True)
    time.sleep(q.get("pause", 0))
buf = []
for p in sorted(glob.glob(q["root"] + "/*/data/enum_pilot.db")):
    folder = p[len(q["root"]) + 1:].split("/")[0]
    keys = want.get(folder)
    if not keys:
        continue
    c = sqlite3.connect("file:%s?mode=ro" % p, uri=True, timeout=10)
    c.row_factory = sqlite3.Row
    for row in c.execute("select * from documents where site_id=? order by collected_at desc, seq_id desc", (q["site"],)):
        k = (None if row["post_number"] is None else str(row["post_number"]), row["meta_url"])
        if k in keys:
            keys.discard(k)
            d = {f: row[f] for f in row.keys() if f in FIELDS}
            d["post_number"] = k[0]
            buf.append(d)
            if len(buf) >= q["chunk"]:
                flush(buf)
                buf = []
if buf:
    flush(buf)
print(json.dumps({"t": "done", "missing": sum(len(v) for v in want.values())}), flush=True)
'''


def server_apply(args, enum_site, product, keys, finish):
    """SERVER_APPLY_MAIN 을 서버에서 돌리고 청크 결과(JSON 줄)를 흘린다."""
    import inspect
    code = "import re\n" + inspect.getsource(copy_text) + "\n" + inspect.getsource(insert_sql) + SERVER_APPLY_MAIN
    q = {"root": RUNS_ROOT, "site": enum_site, "product": product, "target": args.target, "chunk": args.chunk,
         "finish": finish, "fields": FIELDS, "batch": args.batch or "", "pause": args.pause,
         "manifest": "~/enum-import/import-manifest-%s.jsonl" % (args.batch or "test")}
    b64 = base64.b64encode(code.encode()).decode()
    p = subprocess.Popen(SSH + ["PATH=$HOME/bin:$PATH python3 -c 'import base64,sys;exec(base64.b64decode(sys.argv[1]).decode())' "
                                + b64], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    p.stdin.write(json.dumps(q) + "\n")
    for k in keys:
        p.stdin.write(json.dumps(k) + "\n")
    p.stdin.close()
    for line in p.stdout:
        yield json.loads(line)
    if p.wait() != 0:
        raise RuntimeError("서버 넣기 실패(%s)" % enum_site)


def server_sqlite(site, mode, folders=None, keys=None):
    """서버에서 조회를 돌린다. 코드는 인자(base64)로, 입력(설정 한 줄 + 키 줄들)은 stdin 으로 보낸다."""
    q = {"root": RUNS_ROOT, "site": site, "mode": mode, "folders": folders, "content": CONTENT, "fields": FIELDS}
    b64 = base64.b64encode(SERVER_SQLITE.encode()).decode()
    p = subprocess.Popen(SSH + ["python3 -c 'import base64,sys;exec(base64.b64decode(sys.argv[1]).decode())' " + b64],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    p.stdin.write(json.dumps(q) + "\n")
    for k in keys or []:
        p.stdin.write(json.dumps(k) + "\n")
    p.stdin.close()
    for line in p.stdout:
        yield json.loads(line)
    if p.wait() != 0:
        raise RuntimeError("서버 조회 실패 (%s)" % site)


def unescape_copy(v):
    """COPY text 형식 필드 → 값 (\\N 은 None)."""
    if v == r"\N":
        return None
    return re.sub(r"\\(.)", lambda m: {"t": "\t", "n": "\n", "r": "\r", "b": "\b", "f": "\f", "v": "\v", "\\": "\\"}.get(m.group(1), m.group(1)), v)


def psql_copy(container, sql, ssh=False):
    """docker exec <컨테이너> psql 로 COPY (...) TO STDOUT 를 한 줄씩 흘린다."""
    # 읽기만 한다: 세션 전체를 읽기 전용 트랜잭션으로(대상이 운영·격리 스택이어도 쓰기 불가)
    cmd = ["docker", "exec", "-e", "PGOPTIONS=-c default_transaction_read_only=on", container, "psql", "-U", "libertree",
           "-d", "libertree", "-v", "ON_ERROR_STOP=1", "-At", "-c", "copy (%s) to stdout" % sql]
    if ssh:
        cmd = SSH + ["PATH=$HOME/bin:$PATH " + " ".join("'%s'" % a.replace("'", "'\\''") for a in cmd)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        yield [unescape_copy(x) for x in line.rstrip("\n").split("\t")]
    if p.wait() != 0:
        raise RuntimeError("psql 실패: %s" % sql[:120])


def lit(s):
    return "'" + s.replace("'", "''") + "'"


REMOTE_TARGET = False  # --target-remote: 대상 컨테이너가 서버에 있음(키만 SSH 로 읽음)


def target_sites(container):
    return {r[0] for r in psql_copy(container, "select site_id from sites", ssh=REMOTE_TARGET)}


def target_keys(container, site):
    by_post, by_url, exact = {}, {}, set()
    for post, url in psql_copy(container, "select post_number, meta_url from documents where site_id=%s" % lit(site),
                               ssh=REMOTE_TARGET):
        exact.add((post, url))
        by_post.setdefault(post, set()).add(url)
        by_url.setdefault(url, set()).add(post)
    return exact, by_post, by_url


def load_site_map(path, enum_ids, sites):
    """enum 사이트 ID → 제품 사이트 ID. 같은 ID 가 제품 sites 에 있을 때만 자동. 그 밖에는 사람이 확인한 --site-map 만 쓴다
    (앞부분이 같은 제품 ID 로 추정하지 않는다: 이름이 비슷한 별도 수집기라 식별값 규칙이 다르다)."""
    m = {}
    if path and os.path.exists(path):
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            m[r["enum_site_id"]] = (r["product_site_id"], "file")
    out = {}
    for e in enum_ids:
        if e in NOT_DELIVERY:
            out[e] = (None, "not_delivery")
        elif e in m:
            out[e] = m[e]
        elif e in sites:
            out[e] = (e, "same")
        elif e in RECOLLECT_AS:
            out[e] = (None, "recollect")
        else:
            out[e] = (None, "missing")
    return out


def plan_site(args, enum_site, product_site, how, sites):
    rep = {"enum_site": enum_site, "product_site": product_site, "map": how, "folders": [], "issues": []}
    if how == "not_delivery":
        rep["decision"] = "excluded"
        rep["issues"].append("납품 외 별도 수집기: " + NOT_DELIVERY[enum_site])
        return rep, None
    if how == "recollect":
        rep["decision"] = "blocked"
        rep["issues"].append("별도 수집기(식별값 규칙 다름) — 납품 수집기 %s 로 다시 수집 후 가져오기" % RECOLLECT_AS[enum_site])
        return rep, None
    if not product_site:
        rep["decision"] = "blocked"
        rep["issues"].append("제품 sites 에 없는 ID — 별도 수집기일 수 있음. 확인 뒤 --site-map")
        return rep, None
    if product_site not in sites:
        rep["decision"] = "blocked"
        rep["issues"].append("제품 sites 표에 %s 없음" % product_site)
        return rep, None
    # 원본 합치기
    merged, folder_max, posts, urls, urlf = {}, {}, {}, {}, {}
    rows = 0
    stats = {"no_title": 0, "null_post": 0, "pdf_downloaded": 0, "content_diff": 0, "date_status": {}}
    if args.source == "sqlite":
        stream = server_sqlite(enum_site, "keys", args.folders)
    else:
        stream = sci_stream(args, enum_site)
    for x in stream:
        if isinstance(x, dict):
            if x["t"] == "folder":
                rep["folders"].append({k: x.get(k) for k in ("folder", "n", "max_collected", "state", "plan_fp")})
                folder_max[x["folder"]] = (norm_ts(x["max_collected"]), x["folder"])
            elif x["t"] == "error":
                rep["issues"].append("폴더 읽기 실패 %s: %s" % (x["folder"], x["error"]))
            continue
        folder, post, url, coll, title_ok, pdf, h, dstat = x
        rows += 1
        stats["no_title"] += not title_ok
        stats["null_post"] += post is None
        stats["pdf_downloaded"] += bool(pdf)
        posts.setdefault(post, {}).setdefault(folder, set()).add(url)
        urls.setdefault(url, set()).add(post)
        urlf.setdefault(url, {})[folder] = post
        k = (post, url)
        old = merged.get(k)
        if old is None:
            merged[k] = (folder, h, dstat)
        else:
            if old[1] != h:
                stats["content_diff"] += 1
            if folder_max.get(folder, ("", "")) > folder_max.get(old[0], ("", "")):
                merged[k] = (folder, h, dstat)
    for _, (_, _, d) in merged.items():
        stats["date_status"][d or ""] = stats["date_status"].get(d or "", 0) + 1
    # 원본 안 식별 충돌
    # - 한 post_number 에 여러 URL: 그 post_number 가 있는 가장 최근 폴더에서 URL 이 하나면 그 행을 쓰고(drop 에 나머지), 여럿이면 제외
    # - 한 URL 에 여러 post_number: 가져오지 않고 보고
    source_conflict, drop = set(), set()
    shared = product_site in SHARED_POST_NUMBER
    for p, fs in posts.items():
        if len({u for us in fs.values() for u in us}) <= 1:
            continue
        if shared:
            # 여러 URL 이 원래 규칙(언어별 판). 다만 가장 최근 폴더에 없는 URL(옛 실행에만 있는 주소)은 바뀐 주소로 보고 뺀다.
            newest = max(fs, key=lambda f: folder_max.get(f, ("", "")))
            drop |= {(p, u) for f, us in fs.items() if f != newest for u in us if u not in fs[newest]}
            continue
        newest = max(fs, key=lambda f: folder_max.get(f, ("", "")))
        if len(fs[newest]) == 1:
            keep = next(iter(fs[newest]))
            drop |= {(p, u) for us in fs.values() for u in us if u != keep}
            stats["post_newest_pick"] = stats.get("post_newest_pick", 0) + 1
        else:
            source_conflict.add(p)
    shared_url = product_site in SHARED_META_URL
    url_conflict_src = set() if shared_url else {u for u, ps in urls.items() if len(ps) > 1}
    rep["merge_check"] = merge_check(args, rep["folders"], urlf)
    del urlf
    exact, by_post, by_url = target_keys(args.target, product_site)
    c = {"source_rows": rows, "merged_unique": len(merged), "source_conflict_posts": len(source_conflict), "target_existing": len(exact),
         "exists": 0, "new": 0, "post_conflict": 0, "url_conflict": 0, "excluded_source_conflict": 0, "excluded_null_post": 0}
    new = []
    for (post, url), (folder, _, _) in merged.items():
        if (post, url) in drop:
            c["superseded_by_newer_run"] = c.get("superseded_by_newer_run", 0) + 1
        elif post in source_conflict or url in url_conflict_src:
            c["excluded_source_conflict"] += 1
        elif post is None:
            c["excluded_null_post"] += 1  # 제품 고유 인덱스가 NULL post_number 를 막지 못한다
        elif (post, url) in exact:
            c["exists"] += 1
        elif post in by_post and not shared:
            c["post_conflict"] += 1
        elif post in by_post:
            # 공유 규칙 사이트: 대상에 같은 번호의 다른 주소가 이미 있음 — 새 판일 수도, 주소가 바뀐 같은 문서일 수도
            c["shared_post_new"] = c.get("shared_post_new", 0) + 1
        elif url in by_url and not shared_url:
            c["url_conflict"] += 1
        else:
            c["new"] += 1
            new.append([post, url, folder])
    new.sort()
    c["source_conflict_posts"] = len(source_conflict) + len(url_conflict_src)
    rep["new_sha"] = hashlib.sha256("\n".join("%s\t%s" % (p, u) for p, u, _ in new).encode()).hexdigest()
    rep["counts"], rep["stats"] = c, stats
    if enum_site in SPECIAL_IDENTITY or product_site in SPECIAL_IDENTITY:
        rep["issues"].append("사이트별 식별 규칙(document_identity.py) 대상 — 수동 검토")
    if c.get("shared_post_new"):
        rep["issues"].append("같은 post_number 의 다른 meta_url %d건(공유 규칙 사이트) — 새 판인지 주소가 바뀐 같은 문서인지 확인" % c["shared_post_new"])
    if c["post_conflict"] or c["url_conflict"]:
        rep["issues"].append("대상과 식별값 충돌: post %d, url %d — 기존 수집과 post_number 규칙이 다른지 확인" % (c["post_conflict"], c["url_conflict"]))
    if c["excluded_source_conflict"] or c["excluded_null_post"]:
        rep["issues"].append("원본 문제로 제외: 폴더 간 식별 충돌 %d, post_number 없음 %d" % (c["excluded_source_conflict"], c["excluded_null_post"]))
    if stats["no_title"]:
        rep["issues"].append("제목 없는 행 %d (제품 NOT NULL·빈값)" % stats["no_title"])
    if stats["pdf_downloaded"]:
        rep["issues"].append("PDF 받은 행 %d — blob 복사 단계 필요(아직 미구현)" % stats["pdf_downloaded"])
    mc = rep["merge_check"]
    if mc["result"] not in ("single_folder", "same_plan_and_version", "post_numbers_match"):
        rep["issues"].append("실행 폴더 합치기 불가(%s): %s" % (mc["result"], mc["detail"]))
    st = args.status.get(enum_site) or args.status.get(product_site)
    if st and st["state"] in HOLD_STATES:
        rep["issues"].append("구역 판정 %s (%s): %s" % (st["state"], st["file"], st["detail"][:120]))
    if rep["folders"] and all(TRIAL_TAG.search(f["folder"]) for f in rep["folders"]):
        rep["issues"].append("시험 수집 폴더만 있음 — 본 수집 뒤 가져오기")
    rep["decision"] = "ready" if not rep["issues"] else ("review" if c["new"] else "nothing_new")
    if rep["decision"] == "review" and REVIEW_ACCEPTED.get(enum_site) == rep["new_sha"]:
        rep["decision"] = "ready"
        rep["issues"].append("검토 받아들임(REVIEW_ACCEPTED, 새 문서 목록 같음)")
    if not c["new"] and not rep["issues"]:
        rep["decision"] = "nothing_new"
    return rep, new


def sci_stream(args, site):
    """ScienceON 격리 DB(서버 Postgres): 폴더 하나처럼 다룬다."""
    cols = "||chr(31)||".join("coalesce(%s::text,'')" % k for k in CONTENT)
    yield {"t": "folder", "folder": args.sci_container, "n": None, "max_collected": "", "state": None}
    sql = ("select post_number, meta_url, collected_at::text, (coalesce(title,'')<>'')::int, coalesce(pdf_downloaded,0), "
           "left(md5(%s),16), published_date_status from documents where site_id=%s" % (cols, lit(site)))
    for r in psql_copy(args.sci_container, sql, ssh=True):
        yield [args.sci_container, r[0], r[1], r[2], int(r[3]), int(r[4]), r[5], r[6]]


def collector_versions(folders):
    """폴더별 수집기 버전 = 그 폴더 이미지(enum-pilot:<폴더>) 안 crawler/*.py 해시. 네트워크 없는 읽기 전용 컨테이너로 계산.
    이미지가 없으면 None. 서버에서 한 번 계산한 값은 이 실행 동안 기억한다."""
    want = [f for f in folders if f not in VERSION_CACHE]
    if want:
        code = ("export PATH=$HOME/bin:$PATH\nfor f in %s; do v=$(docker run --rm --network none --read-only --user 10001:10001 "
                "--entrypoint python3 enum-pilot:$f -c \"import hashlib,os\nh=hashlib.sha256()\nfor d,_,fs in sorted(os.walk('crawler')):\n"
                "    for n in sorted(fs):\n        p=os.path.join(d,n)\n        if n.endswith('.py'): h.update(p.encode()); h.update(open(p,'rb').read())\n"
                "print(h.hexdigest()[:16])\" 2>/dev/null); echo \"$f ${v:-none}\"; done\n") % " ".join(want)
        r = subprocess.run(SSH + ["bash -s"], input=code, capture_output=True, text=True, timeout=1800)
        for line in r.stdout.split("\n"):
            if " " in line:
                f, v = line.split(" ", 1)
                VERSION_CACHE[f] = None if v == "none" else v
    return {f: VERSION_CACHE.get(f) for f in folders}


VERSION_CACHE = {}


def merge_check(args, folders, urlf):
    """F1: 같은 사이트 실행 폴더는 plan 지문·수집기 버전이 같을 때만 그대로 합친다.
    다르면 폴더 사이에 겹치는 meta_url 의 post_number 를 대조해 전부 같을 때만 합치고, 다르거나 겹침이 없으면 보고."""
    names = [f["folder"] for f in folders]
    if len(names) <= 1:
        return {"result": "single_folder", "detail": ""}
    ver = collector_versions(names) if args.source == "sqlite" else {}
    groups = {}
    for f in folders:
        groups.setdefault((tuple(f.get("plan_fp") or []), ver.get(f["folder"])), []).append(f["folder"])
    info = {"groups": [{"plan_fp": list(k[0]), "version": k[1], "folders": v} for k, v in groups.items()]}
    if len(groups) == 1 and all(ver.get(n) for n in names) and all(f.get("plan_fp") for f in folders):
        return dict(info, result="same_plan_and_version", detail="")
    # 그룹이 다르거나 버전·지문을 모름: 겹치는 URL 대조
    gof = {n: i for i, v in enumerate(groups.values()) for n in v}
    overlap = diff = 0
    samples = []
    for url, fp in urlf.items():
        if len({gof[f] for f in fp}) < 2:
            continue
        overlap += 1
        if len(set(fp.values())) > 1:
            diff += 1
            if len(samples) < 5:
                samples.append({"meta_url": url, "post_numbers": fp})
    info.update(overlap=overlap, post_number_diff=diff, samples=samples)
    if overlap and not diff:
        return dict(info, result="post_numbers_match", detail="겹침 %d건 post_number 일치" % overlap)
    if not overlap:
        return dict(info, result="no_overlap", detail="plan 지문·버전이 다른데 겹치는 URL 이 없어 대조 불가 (%s)" % "; ".join(
            "%s=%s/%s" % (",".join(v), ",".join(k[0]) or "?", k[1] or "?") for k, v in groups.items()))
    return dict(info, result="post_numbers_differ", detail="겹침 %d건 중 post_number 다름 %d건" % (overlap, diff))


def norm_ts(v):
    """'2026-10-14 04:46:24' 와 '2026-10-14T04:46:24+00:00' 를 같은 기준으로 비교하게."""
    return re.sub(r"(Z|[+-]\d\d(:?\d\d)?)$", "", (v or "").replace("T", " "))


def iter_rows(site, keys, chunk):
    """새 키의 원본 행을 chunk 개씩 흘린다(전체를 메모리에 올리지 않음)."""
    buf = []
    for x in server_sqlite(site, "rows", sorted({k[2] for k in keys}), keys):
        if x.get("t") == "row":
            buf.append(x["row"])
            if len(buf) >= chunk:
                yield buf
                buf = []
    if buf:
        yield buf


def copy_text(v):
    if v is None:
        return r"\N"
    return str(v).replace("\x00", "").replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def insert_sql(product_site, rows, finish):
    """임시 표에 COPY → 같은 키·같은 post_number 가 없을 때만 INSERT. finish 는 ROLLBACK 또는 COMMIT."""
    cols = ", ".join(FIELDS)
    lines = ["\\set ON_ERROR_STOP 1", "BEGIN;",
             "CREATE TEMP TABLE imp (LIKE documents INCLUDING DEFAULTS) ON COMMIT DROP;",
             "ALTER TABLE imp DROP COLUMN seq_id, DROP COLUMN fts;",
             "COPY imp (%s) FROM STDIN;" % cols]
    for r in rows:
        r = dict(r, site_id=product_site, pdf_downloaded=0, text_extracted=0, pdf_size_bytes=None, pdf_sha256=None)
        if r.get("collected_at") and re.fullmatch(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d(:\d\d(\.\d+)?)?", r["collected_at"]):
            r["collected_at"] += "+00"  # sqlite CURRENT_TIMESTAMP 는 UTC
        lines.append("\t".join(copy_text(r.get(k)) for k in FIELDS))
    lines.append("\\.")
    lines.append("CREATE TEMP TABLE ins (seq_id bigint, post_number text, meta_url text) ON COMMIT DROP;")
    lines.append("WITH x AS (INSERT INTO documents (%s) SELECT %s FROM imp i "
                 "WHERE NOT EXISTS (SELECT 1 FROM documents d WHERE d.site_id=i.site_id AND d.post_number=i.post_number) "
                 "ON CONFLICT (site_id, post_number, meta_url) DO NOTHING RETURNING seq_id, post_number, meta_url) INSERT INTO ins SELECT * FROM x;" % (cols, cols))
    lines.append("SELECT 'inserted', count(*) FROM ins;")
    lines.append("SELECT 'row', seq_id, post_number, meta_url FROM ins;")
    lines.append("SELECT 'fts_ok', count(*) FROM documents d JOIN ins USING (seq_id) WHERE d.fts IS NOT NULL;")
    lines.append(finish + ";")
    return "\n".join(lines) + "\n"


def run_psql(container, script):
    cmd = ["docker", "exec", "-i", container, "psql", "-U", "libertree", "-d", "libertree", "-At", "-F", "\t"]
    if REMOTE_TARGET:
        # 키·seq_id 만 담긴 스크립트(되돌리기)만 이 길로 간다. 문서 행은 SERVER_APPLY 로 서버 안에서만.
        cmd = SSH + ["PATH=$HOME/bin:$PATH " + " ".join("'%s'" % a.replace("'", "'\\''") for a in cmd)]
    r = subprocess.run(cmd, input=script, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError("psql 실패: " + r.stderr[-500:])
    return [l.split("\t") for l in r.stdout.splitlines()]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="plan", choices=["plan", "rollback-test", "apply", "rollback"])
    ap.add_argument("--site", action="append", default=[], help="enum 사이트 ID (여러 번)")
    ap.add_argument("--all", action="store_true", help="서버 실행 폴더에 있는 모든 사이트")
    ap.add_argument("--source", choices=["sqlite", "sci"], default="sqlite")
    ap.add_argument("--sci-container", default="ui-sci-20261013-postgres-1")
    ap.add_argument("--folders", nargs="*", help="이 실행 폴더만 (기본: 그 사이트가 있는 모든 폴더)")
    ap.add_argument("--target", default="ui-check-20261013-postgres-1", help="제품 스키마 Postgres 컨테이너")
    ap.add_argument("--target-remote", action="store_true",
                    help="대상 컨테이너가 서버에 있음: plan 은 키만 SSH 로 읽고(읽기 전용), 넣기는 서버 안에서만")
    ap.add_argument("--site-map", help="CSV enum_site_id,product_site_id")
    ap.add_argument("--sample", type=int, default=50, help="rollback-test 표본 수(사이트당)")
    ap.add_argument("--out", default="import-report.json")
    ap.add_argument("--site-status", default="/mnt/raid/ruci_workspace/frwaler-delivery-out/plan-dev-20261008/dev-flow-20261018/site-status",
                    help="구역 세션 판정 폴더(site-status/*.csv)")
    ap.add_argument("--batch", help="apply/rollback 배치 ID")
    ap.add_argument("--manifest", help="rollback 할 매니페스트")
    ap.add_argument("--approved-plan", help="apply: 사용자가 검토·승인한 plan 보고서(JSON). 다시 계산한 결과가 같아야 넣는다")
    ap.add_argument("--chunk", type=int, default=10000, help="apply: 한 트랜잭션에 넣을 행 수")
    ap.add_argument("--pause", type=float, default=1.0, help="--target-remote 넣기: 청크 사이 쉬는 초(같은 DB 의 다른 작업 부하)")
    args = ap.parse_args()
    if args.target.lower().startswith("temis") or args.sci_container.lower().startswith("temis"):
        sys.exit("temis 는 다루지 않는다")
    global REMOTE_TARGET
    REMOTE_TARGET = args.target_remote
    if REMOTE_TARGET and args.target != "ui-sci-20261013-postgres-1" and args.mode != "plan":
        # 넣기 대상으로 정해진 곳은 격리 스택 ui-sci 뿐(사용자 결정 2026-10-18). 운영 컨테이너는 넣기 금지.
        sys.exit("--target-remote 넣기·되돌리기 대상은 ui-sci-20261013-postgres-1 만")
    if args.mode == "rollback":
        return rollback(args)
    approved = {}
    if args.mode == "apply":
        if not args.batch or not re.fullmatch(r"[0-9A-Za-z._-]+", args.batch) or os.environ.get("IMPORT_APPLY_APPROVED") != args.batch:
            sys.exit("apply 는 사용자 승인 뒤에만: IMPORT_APPLY_APPROVED=<배치 ID> 와 --batch <같은 ID>")
        if args.all or not args.site or not args.approved_plan:
            sys.exit("apply 는 --site 로 사이트를 하나씩 정하고 --approved-plan <승인한 plan 보고서> 를 줘야 한다")
        if args.source != "sqlite":
            sys.exit("apply --source sci 는 아직 없다(대량 COPY 경로 필요, status/B2.md 참고)")
        plan = json.load(open(args.approved_plan))
        if bool(plan.get("target_remote")) != bool(args.target_remote):
            sys.exit("승인한 plan 의 대상 위치(원격 여부)가 지금과 다름")
        if plan.get("target") != args.target:
            sys.exit("승인한 plan 의 대상(%s)이 지금 대상(%s)과 다름" % (plan.get("target"), args.target))
        approved = {x["enum_site"]: x for x in plan["sites"]}
    args.status = {}
    for f in sorted(glob.glob(os.path.join(args.site_status or "", "*.csv"))):
        for r in csv.DictReader(open(f, encoding="utf-8-sig")):
            sid = (r.get("site_id") or "").strip()
            if sid and (r.get("updated_at") or "") >= args.status.get(sid, {}).get("updated_at", ""):
                args.status[sid] = {"state": (r.get("state") or "").strip(), "detail": r.get("detail") or "", "updated_at": r.get("updated_at") or "", "file": os.path.basename(f)}
    sites = target_sites(args.target)
    enum_ids = list(args.site)
    if args.all:
        enum_ids = sorted(set(enum_ids) | set(all_enum_sites()))
    for e in enum_ids:
        if not SITE_RE.match(e):
            sys.exit("사이트 ID 형식 오류: %s" % e)
    for f in args.folders or []:
        if not FOLDER_RE.match(f):
            sys.exit("폴더 형식 오류: %s" % f)
    smap = load_site_map(args.site_map, enum_ids, sites)
    report = {"at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"), "mode": args.mode, "source": args.source,
              "target": args.target, "target_remote": REMOTE_TARGET, "sites": []}
    for e in enum_ids:
        product, how = smap[e]
        print("== %s → %s (%s)" % (e, product, how), file=sys.stderr, flush=True)
        rep, new = plan_site(args, e, product, how, sites)
        if args.mode == "rollback-test" and new and args.source == "sqlite" and REMOTE_TARGET:
            res = {"tried": 0, "inserted": 0, "fts_ok": 0}
            for x in server_apply(args, e, product, new[:args.sample], "ROLLBACK"):
                if x["t"] == "chunk":
                    for k in res:
                        res[k] += x[k]
            rep["insert_test"] = res
        elif args.mode == "rollback-test" and new and args.source == "sqlite":
            rows = next(iter_rows(e, new[:args.sample], args.sample), [])
            out = run_psql(args.target, insert_sql(product, rows, "ROLLBACK"))
            rep["insert_test"] = {"tried": len(rows), "inserted": next((int(r[1]) for r in out if r[0] == "inserted"), 0),
                                  "fts_ok": next((int(r[1]) for r in out if r[0] == "fts_ok"), 0)}
        elif args.mode == "apply":
            ok = approved.get(e)
            if not ok or ok.get("decision") != "ready" or rep["decision"] != "ready":
                rep["applied"] = "건너뜀: 승인한 plan 이나 지금 계산 결과가 ready 아님"
            elif ok.get("new_sha") != rep["new_sha"] or ok.get("product_site") != product:
                rep["applied"] = "건너뜀: 승인한 plan 과 지금 새 문서 목록이 다름(원본이 늘었거나 대상이 바뀜) — plan 을 다시 받아 승인"
            else:
                rep["applied"] = apply_site(args, e, product, new)
        report["sites"].append(rep)
        print(json.dumps({k: rep.get(k) for k in ("decision", "counts", "issues", "insert_test", "applied")}, ensure_ascii=False), file=sys.stderr, flush=True)
    json.dump(report, open(args.out, "w"), ensure_ascii=False, indent=1)
    print("report: %s" % args.out, file=sys.stderr)


def apply_site(args, enum_site, product, new):
    """chunk 단위 트랜잭션. 커밋할 때마다 매니페스트에 바로 적고 fsync 한다(중간에 멈춰도 되돌릴 수 있게)."""
    mp = "import-manifest-%s.jsonl" % args.batch
    done = {"tried": 0, "inserted": 0, "fts_ok": 0, "chunks": 0, "manifest": mp}
    if REMOTE_TARGET:
        done["server_manifest"] = "~/enum-import/import-manifest-%s.jsonl" % args.batch
        with open(mp, "a") as f:
            for x in server_apply(args, enum_site, product, new, "COMMIT"):
                if x["t"] == "error":
                    raise RuntimeError(x["error"])
                if x["t"] == "done":
                    done["missing_source_rows"] = x["missing"]
                    continue
                for seq, post, url in x["rows"]:
                    f.write(json.dumps({"batch": args.batch, "target": args.target, "seq_id": seq, "site_id": product,
                                        "post_number": post, "meta_url": url}, ensure_ascii=False) + "\n")
                f.flush()
                os.fsync(f.fileno())
                for k in ("tried", "inserted", "fts_ok"):
                    done[k] += x[k]
                done["chunks"] += 1
                print("  %s: %d/%d" % (enum_site, done["tried"], len(new)), file=sys.stderr, flush=True)
        return done
    with open(mp, "a") as f:
        for rows in iter_rows(enum_site, new, args.chunk):
            out = run_psql(args.target, insert_sql(product, rows, "COMMIT"))
            for r in out:
                if r[0] == "row":
                    f.write(json.dumps({"batch": args.batch, "target": args.target, "seq_id": int(r[1]), "site_id": product,
                                        "post_number": r[2], "meta_url": r[3]}, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
            done["tried"] += len(rows)
            done["inserted"] += next((int(r[1]) for r in out if r[0] == "inserted"), 0)
            done["fts_ok"] += next((int(r[1]) for r in out if r[0] == "fts_ok"), 0)
            done["chunks"] += 1
            print("  %s: %d/%d" % (enum_site, done["tried"], len(new)), file=sys.stderr, flush=True)
    return done


def all_enum_sites():
    code = ("import glob,sqlite3,json\ns=set()\nfor p in glob.glob(%r+'/*/data/enum_pilot.db'):\n"
            "    try: s|={r[0] for r in sqlite3.connect('file:%%s?mode=ro'%%p,uri=True).execute('select distinct site_id from documents')}\n"
            "    except sqlite3.Error: pass\nprint(json.dumps(sorted(s)))") % RUNS_ROOT
    return json.loads(subprocess.run(SSH + ["python3 -"], input=code, capture_output=True, text=True, check=True).stdout)


def rollback(args):
    """apply 매니페스트에 적힌 문서만 지운다: 대상 컨테이너가 같고 (seq_id, site_id, post_number, meta_url) 가 모두 맞는 행만.
    그 사이 번역·번역 작업(ON DELETE RESTRICT)이 붙은 문서는 남기고 seq_id 를 보고한다."""
    if not args.manifest or not args.batch or os.environ.get("IMPORT_APPLY_APPROVED") != args.batch:
        sys.exit("rollback 도 사용자 승인 뒤에만: IMPORT_APPLY_APPROVED=<배치 ID> --batch <ID> --manifest <파일>")
    rows = [m for m in (json.loads(l) for l in open(args.manifest)) if m["batch"] == args.batch]
    if not rows:
        sys.exit("매니페스트에 그 배치 없음")
    if {m.get("target") for m in rows} != {args.target}:
        sys.exit("매니페스트의 대상(%s)이 --target(%s)과 다름" % (sorted({str(m.get("target")) for m in rows}), args.target))
    total = {"deleted": 0, "kept": [], "mismatch": 0}
    has_items = run_psql(args.target, "SELECT to_regclass('document_translation_job_items') IS NOT NULL;\n")[0][0] == "t"
    for i in range(0, len(rows), 10000):
        part = rows[i:i + 10000]
        lines = ["\\set ON_ERROR_STOP 1", "BEGIN;", "CREATE TEMP TABLE rb (seq_id bigint, site_id text, post_number text, meta_url text) ON COMMIT DROP;",
                 "COPY rb FROM STDIN;"]
        lines += ["\t".join(copy_text(m[k]) for k in ("seq_id", "site_id", "post_number", "meta_url")) for m in part]
        lines += ["\\.",
                  "CREATE TEMP TABLE hold ON COMMIT DROP AS SELECT seq_id FROM document_translations WHERE seq_id IN (SELECT seq_id FROM rb)"
                  + (" UNION SELECT seq_id FROM document_translation_job_items WHERE seq_id IN (SELECT seq_id FROM rb)" if has_items else "") + ";",
                  "SELECT 'mismatch', count(*) FROM rb WHERE NOT EXISTS (SELECT 1 FROM documents d WHERE d.seq_id=rb.seq_id AND d.site_id=rb.site_id"
                  " AND d.post_number IS NOT DISTINCT FROM rb.post_number AND d.meta_url=rb.meta_url);",
                  "SELECT 'kept', seq_id FROM hold;",
                  "WITH d AS (DELETE FROM documents d USING rb WHERE d.seq_id=rb.seq_id AND d.site_id=rb.site_id AND d.post_number IS NOT DISTINCT FROM rb.post_number"
                  " AND d.meta_url=rb.meta_url AND d.seq_id NOT IN (SELECT seq_id FROM hold) RETURNING 1) SELECT 'deleted', count(*) FROM d;",
                  "COMMIT;"]
        for r in run_psql(args.target, "\n".join(lines) + "\n"):
            if r[0] == "deleted":
                total["deleted"] += int(r[1])
            elif r[0] == "mismatch":
                total["mismatch"] += int(r[1])
            elif r[0] == "kept":
                total["kept"].append(int(r[1]))
    print(json.dumps(total))


if __name__ == "__main__":
    main()
