"""Bounded live smoke audit of the registry in an isolated Docker container.

Each crawler gets its own disposable SQLite DB. Save calls run the real SQLite
adapter. At most three distinct rows are retained as metadata samples. PDF-only
URLs/download entry points are suppressed, so these cases stay inconclusive.
No production DB/blob mounts or API credentials are needed or used.
"""
import argparse
from collections import Counter
import contextlib
import json
import multiprocessing as mp
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.parse import urlsplit


class EnoughSamples(BaseException):
    pass


def write_json(path, value):
    temporary = Path(str(path) + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def probe(site_id, out, limit):
    os.setsid()
    started = time.monotonic()
    work = tempfile.mkdtemp(prefix="crawler-probe-")
    os.environ["LIBERTREE_DB_BACKEND"] = "sqlite"
    os.environ["LIBERTREE_BLOB_ROOT"] = work + "/blob"
    os.environ["LIBERTREE_DATA_ROOT"] = work + "/data"
    result = {"id": site_id, "samples": [], "http_statuses": {}, "pdf_suppressed": 0,
              "error": None, "completed": False, "elapsed_s": 0}
    samples = {}
    progress = Path(out) / "progress" / f"{site_id}.json"
    destination = Path(out) / "results" / f"{site_id}.json"
    log_path = Path(out) / "logs" / f"{site_id}.log"
    write_json(progress, result)
    try:
        with log_path.open("w", buffering=1) as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            from crawler.sites import CRAWLERS
            from crawler import db_libertree as database, pdf_downloader, blob_storage, storage
            blob_storage.BLOB_ROOT = Path(work) / "blob"
            storage.DEFAULT_DATA_ROOT = Path(work) / "data"
            conn = database.open_db(Path(work) / "libertree.db")
            database.init_db(conn)
            cls = CRAWLERS[site_id]
            database.upsert_site(conn, site_id, str(getattr(cls, "site_name", site_id)), str(getattr(cls, "base_url", "")))

            original_insert = database.insert_document
            def insert(connection, doc):
                seq = original_insert(connection, doc)
                row = connection.execute("SELECT seq_id, title, meta_url, pdf_url, published_date, abstract, authors FROM documents WHERE seq_id=?", (seq,)).fetchone()
                if row:
                    samples[seq] = {"title": row[1], "url": row[2], "pdf_url": row[3], "date": row[4],
                                    "has_abstract": bool(row[5]), "has_authors": bool(row[6])}
                    result["samples"] = list(samples.values())
                    write_json(progress, result)
                if len(samples) >= limit:
                    raise EnoughSamples()
                return seq
            database.insert_document = insert

            def blocked_download(*args, **kwargs):
                result["pdf_suppressed"] += 1
                return {"success": False, "size_bytes": 0, "sha256": None, "path": None, "error": "audit_metadata_only"}
            pdf_downloader.download_pdf_for = blocked_download

            def is_pdf(url):
                return isinstance(url, str) and urlsplit(url).path.lower().endswith(".pdf")
            def monitor_request(original):
                def request(instance, method, url, *args, **kwargs):
                    if is_pdf(url):
                        result["pdf_suppressed"] += 1
                        import requests
                        response = requests.Response()
                        response.status_code = 200
                        response._content = b""
                        response.url = url
                        return response
                    response = original(instance, method, url, *args, **kwargs)
                    key = str(response.status_code)
                    result["http_statuses"][key] = result["http_statuses"].get(key, 0) + 1
                    write_json(progress, result)
                    return response
                return request
            import requests
            requests.Session.request = monitor_request(requests.Session.request)
            import curl_cffi.requests
            curl_cffi.requests.Session.request = monitor_request(curl_cffi.requests.Session.request)
            original_run = subprocess.run
            def run(command, *args, **kwargs):
                if isinstance(command, (list, tuple)) and command and Path(str(command[0])).name in ("curl", "wget") and any(is_pdf(arg) for arg in command[1:]):
                    result["pdf_suppressed"] += 1
                    empty = "" if kwargs.get("text") or kwargs.get("encoding") else b""
                    return subprocess.CompletedProcess(command, 0, empty, empty)
                return original_run(command, *args, **kwargs)
            subprocess.run = run
            instance = cls(db_conn=conn, delay=0.5)
            try:
                instance.crawl(limit=limit)
            except EnoughSamples:
                pass
            result["completed"] = True
            conn.close()
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"[:1000]
        with log_path.open("a") as log:
            traceback.print_exc(file=log)
    finally:
        result["elapsed_s"] = round(time.monotonic() - started, 2)
        write_json(destination, result)
        shutil.rmtree(work, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="/audit/live")
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--timeout", type=float, default=35)
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    for part in ("results", "logs", "progress"):
        (Path(args.out) / part).mkdir(parents=True, exist_ok=True)
    from crawler.sites import CRAWLERS
    targets = args.only or sorted(CRAWLERS)
    pending = [sid for sid in targets if not (Path(args.out) / "results" / f"{sid}.json").exists()]
    active = {}
    started = time.monotonic()
    completed = len(targets) - len(pending)
    print(f"targets={len(targets)}, resumed={completed}, workers={args.workers}, timeout={args.timeout}s", flush=True)
    while pending or active:
        hosts = {task[2] for task in active.values()}
        while len(active) < args.workers and pending:
            index = next((i for i, sid in enumerate(pending) if urlsplit(str(getattr(CRAWLERS[sid], "base_url", ""))).netloc not in hosts), None)
            if index is None:
                break
            sid = pending.pop(index)
            host = urlsplit(str(getattr(CRAWLERS[sid], "base_url", ""))).netloc
            process = mp.get_context("fork").Process(target=probe, args=(sid, args.out, args.limit))
            process.start()
            active[sid] = (process, time.monotonic(), host)
            hosts.add(host)
        for sid, (process, begin, host) in list(active.items()):
            expired = time.monotonic() - begin >= args.timeout
            if process.is_alive() and not expired:
                continue
            if expired and process.is_alive():
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.join(timeout=1)
            # Also reap browser/curl descendants left after a successful probe.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            path = Path(args.out) / "results" / f"{sid}.json"
            if not path.exists():
                progress = Path(args.out) / "progress" / f"{sid}.json"
                result = json.loads(progress.read_text()) if progress.exists() else {"id": sid, "samples": [], "http_statuses": {}, "pdf_suppressed": 0}
                result.update(completed=False, error="probe_timeout" if expired else f"process_exit:{process.exitcode}", elapsed_s=round(time.monotonic()-begin, 2))
                write_json(path, result)
            del active[sid]
            completed += 1
            if completed % 25 == 0 or completed == len(targets):
                print(f"completed={completed}/{len(targets)}, active={len(active)}, elapsed={round(time.monotonic()-started)}s", flush=True)
        time.sleep(0.1)
    results = [json.loads(path.read_text()) for path in (Path(args.out) / "results").glob("*.json")]
    print(json.dumps({"results": len(results), "with_saved_samples": sum(bool(r["samples"]) for r in results),
                      "errors": dict(Counter(r.get("error") for r in results))}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
