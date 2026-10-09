"""Bounded native HTTP/save/cancel/reopen pilot against observed SCAHT IDs.

Only a new, explicitly named SQLite database is written. This is a 20-document
pilot, not a complete source harvest or publisher file validation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup

from crawler import db_libertree
from crawler.base_crawler import CrawlCancelled
from crawler.generic_crawler import GenericCrawler
from .request_gate import NetworkDeferred, RequestGate
from .results import atomic_json, utc_now
from .scaht_scope import HOSTS, parse_listing
from .source_observer import retry_after

ROOT = Path(__file__).resolve().parents[2]


class AuditStop(BaseException):
    pass


def run_pilot(output, gate_dir, listing_evidence):
    if os.environ.get('LIBERTREE_DB_BACKEND', 'sqlite') != 'sqlite':
        raise ValueError('pilot requires explicit SQLite backend')
    os.environ['LIBERTREE_DB_BACKEND'] = 'sqlite'
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    evidence = Path(listing_evidence).read_bytes()
    source = json.loads(evidence)
    records = source['passes'][0]['records']
    expected = list(dict.fromkeys(r['external_id'] for r in records))[:20]
    if len(expected) != 20:
        raise ValueError('reviewed listing must include at least 20 unique IDs')
    config = ROOT/'crawler/sites/configs/scaht-org.json'
    database = output/'pilot.sqlite'
    gate = RequestGate(gate_dir, delay=1.5)
    report = {'started_at': utc_now(), 'site_id': 'scaht-org', 'state': 'running',
              'database': str(database), 'operating_database_touched': False,
              'scope': 'first_20_observed_English_list_ids_native_HTTP_and_SQLite_save',
              'source_evidence_sha256': hashlib.sha256(evidence).hexdigest(),
              'config_sha256': hashlib.sha256(config.read_bytes()).hexdigest(),
              'expected_ids': expected, 'phases': [], 'requests': [],
              'full_collection_verified': False, 'publisher_files_verified': False}
    def save():
        atomic_json(output/'pilot.json', report)
    def stop(signum, frame):
        raise AuditStop('pilot_wall_budget_or_termination')
    previous = signal.signal(signal.SIGALRM, stop)
    signal.alarm(1800)
    save()
    try:
        for index, (limit, cancel_after, expected_new) in enumerate(((3, None, 3), (3, None, 3), (None, 2, 2), (12, None, 12)), 1):
            # Reopen the persisted database for each run; no in-memory cursor
            # or connection state carries the resume position across phases.
            conn = sqlite3.connect(database)
            try:
                db_libertree.init_db(conn)
                db_libertree.upsert_site(conn, 'scaht-org', 'SCAHT', 'https://www.scaht.org')
                count = lambda: conn.execute('SELECT count(*) FROM documents').fetchone()[0]
                before = count()
                prior = dict(conn.execute('SELECT post_number,seq_id FROM documents'))
                phase = {'number': index, 'started_at': utc_now(), 'limit_new': limit,
                         'cancel_after_actual_new': cancel_after, 'before': before, 'state': 'running'}
                report['phases'].append(phase)
                crawler = GenericCrawler(config, conn)
                crawler.delivery_mode = 'incremental'
                crawler._session.trust_env = False
                if cancel_after:
                    crawler.delivery_should_cancel = lambda: count() >= before + cancel_after
                original = crawler._session.request
                original_send = crawler._session.send
                def send(prepared, **kwargs):
                    u = urlsplit(prepared.url)
                    if u.scheme not in ('http', 'https') or u.hostname not in HOSTS or u.username or u.password or u.port:
                        raise AuditStop('request_outside_reviewed_source')
                    return original_send(prepared, **kwargs)
                crawler._session.send = send
                def request(method, url, **kwargs):
                    if len(report['requests']) >= 80:
                        raise AuditStop('request_budget_reached')
                    with gate.enter(url):
                        response = original(method, url, **kwargs)
                        item = {'phase': index, 'observed_at': utc_now(),
                                'http_status': response.status_code,
                                'redirect_statuses': [r.status_code for r in response.history],
                                'body_sha256': hashlib.sha256(response.content).hexdigest()}
                        report['requests'].append(item)
                        save()
                        if response.status_code == 429:
                            until = retry_after(response.headers.get('Retry-After'))
                            gate.defer(url, until, response.url)
                            raise NetworkDeferred(urlsplit(response.url).hostname, until)
                        if response.status_code in (401, 403):
                            raise AuditStop('source_access_denied')
                        if response.status_code != 200:
                            raise AuditStop('source_response_not_200')
                        u = urlsplit(response.url)
                        if u.path.rstrip('/') == '/en/publications':
                            item['role'] = 'listing'
                            page = int(parse_qs(u.query).get('page', ['1'])[0])
                            parsed = parse_listing(response.content, page)
                            if not parsed['records']:
                                raise AuditStop('unexpected_empty_pilot_listing')
                            item['list_items'] = len(parsed['records'])
                        else:
                            item['role'] = 'detail'
                            soup = BeautifulSoup(response.content, 'html.parser')
                            title = soup.select_one('.publication-title h1')
                            item['required_title_found'] = bool(title and title.get_text(strip=True))
                            if not item['required_title_found']:
                                raise AuditStop('detail_title_missing')
                        save()
                        return response
                crawler._session.request = request
                save()
                try:
                    phase['returned_new'] = crawler.crawl(limit=limit)
                    phase['state'] = 'returned'
                except CrawlCancelled:
                    phase['state'] = 'cancelled'
                finally:
                    if phase['state'] == 'running':
                        phase['state'] = 'interrupted'
                    phase.update(after=count(), actual_new=count()-before,
                                 listing_stop=crawler.delivery_listing_stop,
                                 source_failure=crawler.delivery_source_failure,
                                 exhausted=crawler.delivery_exhausted, finished_at=utc_now())
                    save()
                    crawler._session.close()
                after = dict(conn.execute('SELECT post_number,seq_id FROM documents'))
                if any(after.get(key) != seq for key, seq in prior.items()):
                    raise AuditStop('previous_identity_changed')
                if phase['actual_new'] != expected_new or phase['source_failure']:
                    raise AuditStop('phase_new_count_or_source_failure')
                if cancel_after and phase['state'] != 'cancelled':
                    raise AuditStop('cancellation_not_observed')
                if not cancel_after and phase.get('returned_new') != expected_new:
                    raise AuditStop('returned_new_count_mismatch')
            finally:
                conn.close()
        conn = sqlite3.connect(database)
        try:
            rows = conn.execute('SELECT post_number,seq_id,title,meta_url FROM documents').fetchall()
            actual = {row[0] for row in rows}
            report.update(stored_count=len(rows), stored_unique_ids=len(actual),
                          missing_expected_ids=sorted(set(expected)-actual),
                          unexpected_ids=sorted(actual-set(expected)),
                          blank_required_fields=sum(not row[2] or not row[3] for row in rows))
            report['state'] = ('bounded_native_save_cancel_reopen_verified' if
                               len(rows) == len(actual) == 20 and actual == set(expected)
                               and not report['blank_required_fields'] else 'reconciliation_failed')
        finally:
            conn.close()
    except NetworkDeferred as exc:
        report.update(state='host_cooldown_deferred', retry_at=exc.retry_at)
    except AuditStop as exc:
        report.update(state='incomplete', reason=str(exc))
    except Exception as exc:
        report.update(state='incomplete', exception_type=type(exc).__name__)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        report['finished_at'] = utc_now()
        save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('--gate-dir', required=True)
    parser.add_argument('--listing-evidence', required=True)
    args = parser.parse_args()
    result = run_pilot(args.output, args.gate_dir, args.listing_evidence)
    print(json.dumps({k: result.get(k) for k in ('state', 'stored_count', 'full_collection_verified')}))
    return 0 if result['state'] == 'bounded_native_save_cancel_reopen_verified' else 1


if __name__ == '__main__':
    raise SystemExit(main())
