"""File worker: serial queue consumption, isolated stages, leases and retries."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time

from crawler import blob_storage
from . import jobs


def run_stage(stage, directory, url=None, *, timeout=180):
    root = Path(directory)
    (root / 'result.json').unlink(missing_ok=True)
    proc = subprocess.Popen([sys.executable, '-m', 'delivery.files.stage'],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        text=True, start_new_session=True,
        env={key: value for key, value in os.environ.items()
             if key in ('PATH','PYTHONPATH','LANG','LC_ALL','PYTHONDONTWRITEBYTECODE','SSL_CERT_FILE','REQUESTS_CA_BUNDLE')})
    try:
        proc.communicate(json.dumps({'stage':stage,'directory':str(root),'url':url}), timeout=timeout)
        if proc.returncode or not (root / 'result.json').is_file():
            return {'success':False,'error':'stage_failed'}
        return json.loads((root / 'result.json').read_text())
    except subprocess.TimeoutExpired:
        return {'success':False,'error':'stage_timeout'}
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()
        if proc.stdin:
            proc.stdin.close()


def pdf_is_valid(path, expected_hash=None):
    try:
        with path.open('rb') as handle:
            prefix = handle.read(1024).lstrip(b'\xef\xbb\xbf \t\r\n\x00')
            if not prefix.startswith((b'%PDF-1.', b'%PDF-2.')):
                return False
            if expected_hash:
                handle.seek(0)
                return hashlib.file_digest(handle, 'sha256').hexdigest() == expected_hash
        return True
    except OSError:
        return False


def publish(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    source.chmod(0o644)
    with source.open("rb") as handle:
        os.fsync(handle.fileno())
    os.replace(source, target)  # Work dir and target live on the same blob volume.


def process(conn, task, *, root, executor=run_stage, timeout=180):
    from crawler import db_pg
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    pdf_done = False
    text_done = False
    def finish(status, error=None, retry=False, unavailable=False):
        jobs.finish(conn, task, status=status,
                    pdf_status='done' if pdf_done else ('unavailable' if unavailable else 'failed'),
                    text_status='done' if text_done else ('skipped' if unavailable else ('failed' if pdf_done else 'pending')),
                    error=error, retry=retry)
    # Session-level document lock also serializes separate crawl jobs referring
    # to the same PDF. Commits in the DB helpers do not release this lock.
    lock = conn.execute('SELECT pg_try_advisory_lock(84721,%s::integer) AS locked', (int(task['seq_id']) % 2147483647,)).fetchone()['locked']
    conn.commit()
    if not lock:
        conn.execute("""UPDATE document_file_jobs SET status='queued',attempts=attempts-1,token=NULL,
            lease_expires_at=NULL,next_attempt_at=now()+interval '5 seconds'
            WHERE id=%s AND token=%s""", (task['id'],task['token']))
        conn.commit()
        return
    try:
        doc = db_pg.get_document(conn, task['seq_id'])
        conn.rollback()
        if doc is None:
            finish('failed', 'document_missing')
            return
        pdf = blob_storage.get_blob_path(task['seq_id'], 'pdf', root)
        txt = blob_storage.get_blob_path(task['seq_id'], 'txt', root)
        pdf_done = bool(doc['pdf_downloaded'] and pdf_is_valid(pdf, doc.get('pdf_sha256')))
        if not pdf_done and not doc.get('pdf_url'):
            finish('skipped', 'no_pdf_url', unavailable=True)
            return
        if shutil.disk_usage(root).free < 1024 ** 3:
            finish('failed', 'disk_space_low', retry=True)
            return
        scratch = root / '.file-work'
        scratch.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='task-', dir=scratch) as directory:
            temp = Path(directory)
            if not pdf_done:
                result = executor('pdf', temp, doc['pdf_url'], timeout=timeout)
                if not jobs.owns(conn, task):
                    conn.rollback()
                    return
                if not result.get('success'):
                    finish('failed', result.get('error','download_failed'), retry=result.get('error') not in ('invalid_pdf','file_too_large'))
                    return
                candidate = temp / 'download.pdf'
                if not pdf_is_valid(candidate):
                    finish('failed', 'invalid_pdf')
                    return
                # Invalidate derived text before replacing its source. DB/file
                # operations cannot share one transaction; retries reconcile it.
                conn.execute('UPDATE documents SET text_extracted=0 WHERE seq_id=%s', (task['seq_id'],))
                publish(candidate, pdf)
                db_pg.update_document_pdf(conn, task['seq_id'], downloaded=True,
                                          size_bytes=pdf.stat().st_size, sha256=result['sha256'])
                pdf_done = True
                doc['text_extracted'] = 0
            text_done = bool(doc['text_extracted'] and txt.is_file() and txt.stat().st_size > 0)
            if text_done:
                finish('done')
                return
            if not jobs.owns(conn, task):
                conn.rollback()
                return
            conn.execute("UPDATE document_file_jobs SET pdf_status='done',text_status='pending' WHERE id=%s", (task['id'],))
            conn.commit()
            # Hard link avoids copying large PDFs; parser only reads this link.
            os.link(pdf, temp / 'source.pdf')
            result = executor('text', temp, timeout=timeout)
            if not jobs.owns(conn, task):
                conn.rollback()
                return
            if not result.get('success'):
                finish('failed', result.get('error','parse_failed'), retry=result.get('error') == 'stage_timeout')
                return
            candidate = temp / 'output.txt'
            if not candidate.is_file() or not candidate.stat().st_size:
                finish('failed', 'no_text_layer')
                return
            publish(candidate, txt)
            db_pg.update_document_text(conn, task['seq_id'], extracted=True)
            text_done = True
            finish('done')
    except Exception:
        conn.rollback()
        finish('failed', 'file_processing_failed', retry=True)
    finally:
        conn.rollback()
        conn.execute('SELECT pg_advisory_unlock(84721,%s::integer)', (int(task['seq_id']) % 2147483647,))
        conn.commit()


def beat(dsn, stop):
    from crawler import db_pg
    while not stop.is_set():
        try:
            with db_pg.open_db(dsn) as conn:
                conn.execute("""INSERT INTO file_worker_heartbeat(id,last_seen_at) VALUES(1,now())
                    ON CONFLICT(id) DO UPDATE SET last_seen_at=now()""")
        except Exception:
            pass
        stop.wait(20)


def main():
    from crawler import db_pg
    from delivery.db.schema import verify_required_schema
    def terminate(_signum, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    dsn = os.environ['LIBERTREE_PG_DSN']
    timeout = max(10, min(int(os.environ.get('FILE_STAGE_TIMEOUT_SECONDS','180')),600))
    root = os.environ.get('LIBERTREE_BLOB_ROOT','/data/blob')
    with db_pg.open_db(dsn) as conn:
        verify_required_schema(conn)
        stop = threading.Event()
        heartbeat = threading.Thread(target=beat,args=(dsn,stop),daemon=True)
        heartbeat.start()
        try:
            while True:
                jobs.recover(conn)
                task = jobs.claim(conn, lease_seconds=timeout*2+120)
                if task:
                    process(conn,task,root=root,timeout=timeout)
                else:
                    time.sleep(3)
        finally:
            stop.set()
            heartbeat.join(timeout=2)


if __name__ == '__main__':
    main()
