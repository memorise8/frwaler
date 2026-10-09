"""Verify native PDF saves and isolated text extraction in a copied SQLite DB.

Input is an independently reviewed metadata database and explicit eligible-file
manifest. Source exclusions and files absent from that manifest remain open.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import tempfile
import time
from urllib.parse import urlsplit

import requests

from crawler import blob_storage, db_libertree
from crawler.pdf_downloader import download_pdf_for
from delivery.files.worker import pdf_is_valid, run_stage
from .request_gate import NetworkDeferred, RequestGate
from .results import atomic_json, utc_now
from .scaht_persistence import AuditStop
from .source_observer import retry_after


def digest(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


class AuditSession(requests.Session):
    def __init__(self, gate, hosts, report, save):
        super().__init__()
        self.trust_env = False
        self.gate, self.hosts, self.report, self.save = gate, hosts, report, save
        self.hooks['response'].append(self.observe_response)

    def observe_response(self, response, **kwargs):
        parsed = urlsplit(response.url)
        self.report['requests'].append({'at': utc_now(), 'host': parsed.hostname,
                                         'status': response.status_code})
        self.save()
        if response.status_code == 429 or (response.status_code == 503 and response.headers.get('Retry-After')):
            until = retry_after(response.headers.get('Retry-After'))
            self.gate.defer(response.url, until)
            response.close()
            raise NetworkDeferred(parsed.hostname, until)
        if response.status_code in (401, 403):
            response.close()
            raise AuditStop('source_access_denied')
        return response

    def send(self, request, **kwargs):
        parsed = urlsplit(request.url)
        if (parsed.scheme != 'https' or parsed.hostname not in self.hosts
                or parsed.username or parsed.password or parsed.port not in (None, 443)):
            raise AuditStop('redirect_or_request_outside_reviewed_hosts')
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise AuditStop('non_public_file_destination')
        # The caller holds the outer gate through streaming and file persistence.
        # Reentrant entry checks cooldowns for each redirect destination too.
        with self.gate.enter(request.url):
            if len(self.report['requests']) >= 1000:
                raise AuditStop('request_budget_reached')
            return super().send(request, **kwargs)


def run(output, database, review, manifest, gate_dir, *, resume=False, stop_after=None, retry_transport_failure=False):
    output, database = Path(output), Path(database)
    reviewed = json.loads(Path(review).read_text())
    source = json.loads(Path(manifest).read_text())
    expected = {str(r['post_number']): r for r in source['records'] if r['code_save_eligible']}
    if not 0 < len(expected) <= 500 or len(expected) != reviewed['actual_stored_rows']:
        raise ValueError('requires bounded reviewed eligible manifest')
    if digest(database) != reviewed['database_sha256']:
        raise ValueError('source database changed since independent review')
    if not all(r.get('native_primary_file_url') for r in expected.values()):
        raise ValueError('manifest contains missing primary URLs')
    hosts = {urlsplit(r['native_primary_file_url']).hostname for r in expected.values()}
    fingerprint = {'database_sha256': digest(database), 'manifest_sha256': digest(manifest), 'review_sha256': digest(review)}
    if resume:
        report = json.loads((output/'files.json').read_text())
        retryable = (retry_transport_failure and report['state'] == 'incomplete'
                     and report.get('reason') == 'native_file_download_failed'
                     and any(f.get('state') == 'download_failed' and (f.get('error') or '').startswith('request error:')
                             for f in report['files'].values())
                     and all(f.get('state') == 'verified' or (f.get('error') or '').startswith('request error:')
                             for f in report['files'].values()))
        if report['inputs'] != fingerprint or not (retryable or report['state'] in ('checkpointed', 'running', 'manifest_files_verified')):
            raise ValueError('resume requires unchanged inputs and a resumable checkpoint')
    else:
        output.mkdir(parents=True, exist_ok=False)
        report = {'started_at': utc_now(), 'state': 'running', 'inputs': fingerprint,
                  'scope': 'primary files of code-eligible records in reviewed SQLite copy',
                  'expected_files': len(expected), 'source_excluded_records': source['source_count']-len(expected),
                  'budgets': {'max_file_bytes': 100*1024**2, 'max_total_pdf_bytes': 1024**3,
                              'text_stage_seconds': 120, 'wall_seconds': 3600},
                  'files': {}, 'requests': [], 'runs': [], 'full_collection_verified': False,
                  'operating_database_touched': False, 'postgres_queue_lifecycle_verified': False}
        with sqlite3.connect('file:'+str(database.resolve())+'?mode=ro', uri=True) as src:
            with sqlite3.connect(output/'files.sqlite') as dest:
                src.backup(dest)
    os.environ['LIBERTREE_DB_BACKEND'] = 'sqlite'
    conn = sqlite3.connect(output/'files.sqlite'); conn.row_factory = sqlite3.Row
    rows = list(conn.execute('SELECT * FROM documents ORDER BY seq_id'))
    if len(rows) != len(expected) or {str(r['post_number']) for r in rows} != set(expected):
        conn.close(); raise ValueError('copied database identities differ from manifest')
    if any(r['pdf_url'] != expected[str(r['post_number'])]['native_primary_file_url'] for r in rows):
        conn.close(); raise ValueError('copied database file URLs differ from manifest')
    run_info = {'started_at': utc_now(), 'resume': resume, 'retry_transport_failure': retry_transport_failure,
                'newly_processed': 0, 'reused_verified': 0}
    report['runs'].append(run_info); report['state'] = 'running'
    report.pop('reason', None)
    def save():
        report['updated_at'] = utc_now(); atomic_json(output/'files.json', report)
    gate = RequestGate(gate_dir, delay=1.5)
    blobs = output/'blobs'; blobs.mkdir(exist_ok=True)
    previous = signal.signal(signal.SIGALRM, lambda s, f: (_ for _ in ()).throw(AuditStop('wall_budget_reached')))
    signal.alarm(3600)
    save()
    item = None
    try:
        with AuditSession(gate, hosts, report, save) as session:
            for row in rows:
                key, seq = str(row['post_number']), row['seq_id']
                pdf = blob_storage.get_blob_path(seq, 'pdf', blobs)
                txt = blob_storage.get_blob_path(seq, 'txt', blobs)
                cached = report['files'].get(key)
                if cached and cached['state'] == 'verified':
                    if (cached['seq_id'] != seq or not row['pdf_downloaded'] or not row['text_extracted']
                            or row['pdf_sha256'] != cached['pdf_sha256']
                            or not pdf_is_valid(pdf, cached['pdf_sha256'])
                            or pdf.stat().st_size != cached['pdf_bytes']
                            or not txt.is_file() or digest(txt) != cached['text_sha256']):
                        cached['state'] = 'integrity_failed'
                        raise AuditStop('cached_file_integrity_mismatch')
                    run_info['reused_verified'] += 1
                    continue
                if stop_after is not None and run_info['newly_processed'] >= stop_after:
                    report['state'] = 'checkpointed'; break
                if shutil.disk_usage(output).free < 1024**3:
                    raise AuditStop('disk_budget_reached')
                if sum(f.get('pdf_bytes', 0) for f in report['files'].values()) >= 1024**3:
                    raise AuditStop('aggregate_download_budget_reached')
                attempts = list((cached or {}).get('download_attempts', []))
                if cached and cached.get('error') and not attempts:
                    attempts.append({'success': False, 'error': cached['error'], 'from_previous_attempt': True})
                item = {'seq_id': seq, 'state': 'downloading', 'download_attempts': attempts}
                report['files'][key] = item; save()
                result = {'success': False, 'error': 'transport_attempt_budget_exhausted'}
                while len(attempts) < 2:
                    with gate.enter(row['pdf_url']):
                        result = download_pdf_for(conn, seq, row['pdf_url'], session=session,
                                                  timeout=45, max_size=100*1024**2, blob_root=blobs)
                    attempts.append({'at': utc_now(), 'success': result['success'], 'error': result['error']})
                    save()
                    if result['success'] or not (result['error'] or '').startswith('request error:'): break
                    if len(attempts) < 2: time.sleep(3)
                if not result['success']:
                    item.update(state='download_failed', error=result['error'])
                    if (result['error'] or '').startswith(('http 404:', 'http 410:', 'request error:', 'transport_attempt_budget_exhausted')):
                        run_info['newly_processed'] += 1; save(); continue
                    # HTML challenge/login pages must not trigger alternate-URL
                    # attempts or retries against other documents on the host.
                    raise AuditStop('native_file_download_failed')
                item.update(state='extracting', pdf_bytes=result['size_bytes'], pdf_sha256=result['sha256'])
                save()
                with tempfile.TemporaryDirectory(dir=output, prefix='text-') as directory:
                    work = Path(directory); os.link(pdf, work/'source.pdf')
                    parsed = run_stage('text', work, timeout=120)
                    if not parsed['success']:
                        item.update(state='text_failed', error=parsed.get('error'))
                        run_info['newly_processed'] += 1; save(); continue
                    candidate = work/'output.txt'
                    if not candidate.is_file() or not candidate.stat().st_size:
                        raise AuditStop('text_result_missing')
                    os.replace(candidate, txt)
                db_libertree.update_document_text(conn, seq, extracted=True)
                item.update(state='verified', text_sha256=digest(txt), text_bytes=txt.stat().st_size,
                            extracted_characters=parsed['chars'])
                run_info['newly_processed'] += 1; save()
            else:
                report['state'] = ('manifest_files_verified' if all(
                    report['files'].get(k, {}).get('state') == 'verified' for k in expected
                ) else 'manifest_files_incomplete')
    except NetworkDeferred as exc:
        if item is not None and item['state'] in ('downloading', 'extracting'):
            item.update(state='deferred', error='host_cooldown_deferred')
        report.update(state='host_cooldown_deferred', retry_at=exc.retry_at)
    except AuditStop as exc:
        if item is not None and item['state'] in ('downloading', 'extracting'):
            item.update(state='blocked' if str(exc) == 'source_access_denied' else 'interrupted', error=str(exc))
        report.update(state='incomplete', reason=str(exc))
    except Exception as exc:
        if item is not None and item['state'] in ('downloading', 'extracting'):
            item.update(state='interrupted', exception_type=type(exc).__name__)
        report.update(state='incomplete', exception_type=type(exc).__name__)
    finally:
        signal.alarm(0); signal.signal(signal.SIGALRM, previous)
        conn.close(); run_info.update(finished_at=utc_now(), state=report['state'])
        report['updated_at'] = utc_now(); save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for arg in ('output', 'database', 'review', 'manifest', 'gate-dir'):
        parser.add_argument('--'+arg, required=True)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--stop-after', type=int)
    parser.add_argument('--retry-transport-failure', action='store_true')
    args = parser.parse_args()
    result = run(args.output, args.database, args.review, args.manifest, args.gate_dir,
                 resume=args.resume, stop_after=args.stop_after, retry_transport_failure=args.retry_transport_failure)
    print(json.dumps({'state': result['state'], 'processed': len(result['files'])}))
    return 0 if result['state'] in ('checkpointed', 'manifest_files_verified') else 1


if __name__ == '__main__':
    raise SystemExit(main())
