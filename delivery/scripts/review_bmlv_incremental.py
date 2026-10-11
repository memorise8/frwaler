"""Review original BMLV table order; preserve partial and missing dates."""
import argparse
from collections import Counter
from datetime import date
import hashlib
import json
from pathlib import Path
import re

from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now

SITE = 'bmlv-gv-at-wissen-forschung'
REASON = 'delivered_conference_scope_missing_date_invalid_detail_date_and_nonchronological_ids'


def date_precision(raw):
    for pattern, precision in ((r'\d{2}\.\d{2}\.\d{4}', 'day'),
                               (r'\d{2}\.\d{4}', 'month'), (r'\d{4}', 'year')):
        if re.fullmatch(pattern, raw):
            values = [int(v) for v in raw.split('.')]
            try:
                # Validate components only; never export fabricated day/month values.
                date(values[-1], values[-2] if len(values) > 1 else 1,
                     values[0] if len(values) == 3 else 1)
            except ValueError:
                return 'unknown'
            return precision
    return 'unknown'


def parse_table(raw):
    soup = CRAWLERS[SITE].crawl.__globals__['_make_soup'](raw)
    tables = soup.select('table.publikationen')
    if len(tables) != 1:
        raise ValueError('exactly one publication table required')
    rows = []
    for tr in tables[0].select('tr'):
        link = tr.select_one('td[headers="name"] a[href*="publikation.php?id="]')
        if link is None:
            if tr.select('td'):
                raise ValueError('unrecognized publication row')
            continue
        match = re.fullmatch(r'publikation\.php\?id=(\d+)', link['href'])
        cell = tr.select_one('td[headers="datum"]')
        if not match or cell is None:
            raise ValueError('publication identity or date column missing')
        rows.append({'id': match[1], 'date_raw': cell.get_text(' ', strip=True)})
    if not rows or len({r['id'] for r in rows}) != len(rows):
        raise ValueError('empty or duplicate publication identities')
    return rows


def order_evidence(rows):
    return {'items': len(rows), 'first': rows[0], 'last': rows[-1],
            'date_precision': dict(Counter(date_precision(r['date_raw']) for r in rows)),
            'unknown_date_ids': [r['id'] for r in rows if date_precision(r['date_raw']) == 'unknown'],
            'numeric_id_descending_inversions': [
                {'before': a['id'], 'after': b['id']}
                for a, b in zip(rows, rows[1:]) if int(b['id']) > int(a['id'])]}


def run(source, probe, out):
    source, probe, out = Path(source), Path(probe), Path(out)
    if out.exists():
        raise FileExistsError(out)
    prior = json.loads((source/'bmlv-all-tables-independent-review.json').read_text())
    manifest = source/prior['records_file']
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == prior['records_sha256']
    records = [json.loads(line) for line in manifest.read_text().splitlines()]
    audit = json.loads((probe/'audit.json').read_text())
    assert len(audit['requests']) == 4
    fresh = {}
    for r in audit['requests']:
        raw = (probe/r['body']).read_bytes()
        assert r['status'] == 200 and hashlib.sha256(raw).hexdigest() == r['sha256']
        fresh[r['url']] = raw
    scopes = []
    for scope in prior['scopes']:
        raw = (source/scope['source_file']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == scope['source_sha256']
        old, current = parse_table(raw), parse_table(fresh[scope['url']])
        expected = [{'id': r['source_publication_id'], 'date_raw': r['date_raw']}
                    for r in records if r['scope'] == scope['scope']]
        assert old == expected
        assert {r['id']: r['date_raw'] for r in old} == {r['id']: r['date_raw'] for r in current}
        scopes.append({'url': scope['url'], 'fresh_membership_and_dates_match_retained': True,
                       'fresh_sequence_matches_retained': old == current,
                       'position_changes': [{'index': i, 'before': a, 'after': b}
                                            for i, (a, b) in enumerate(zip(old, current)) if a != b],
                       **order_evidence(current)})
    detail_url = 'https://www.bmlv.gv.at/wissen-forschung/publikationen/publikation.php?id=760'
    soup = CRAWLERS[SITE].crawl.__globals__['_make_soup'](fresh[detail_url])
    raw_date = soup.select_one('meta[property="books:release_date"]')['content']
    try:
        date.fromisoformat(raw_date)
    except ValueError:
        pass
    else:
        raise AssertionError('missing listing date now has valid detail date; re-review required')
    assert 'Erscheinungsdatum:' not in soup.select_one('#content .floatbox').get_text()
    assert scopes[1]['unknown_date_ids'] == ['760']
    assert all(s['numeric_id_descending_inversions'] for s in scopes)
    cls = CRAWLERS[SITE]
    report = {'status': 'passed', 'reviewed_at': utc_now(), 'site_id': SITE,
              'classification': 'full_rescan_required', 'reason': REASON,
              'source_sha256': hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
              'manifest_sha256': prior['records_sha256'], 'scopes': scopes,
              'raw_partial_dates_preserved': True, 'unknown_item_detail_release_date': raw_date,
              'unknown_item_detail_has_displayed_date': False, 'fresh_requests': 4,
              'publication_memberships': len(records), 'unique_publications': len({r['source_publication_id'] for r in records}),
              'contribution_memberships_separate_unit': prior['source_declared_contribution_memberships'],
              'native_configured_publications': 18, 'native_unconfigured_publications': 217,
              'global_source_complete': False,
              'note': 'Partial dates alone do not prove disorder. The undated conference item has invalid detail date 0000-00-01, and numeric IDs cannot establish whole-scope chronology. No evidence of a safe date/ID boundary; retain periodic full checks. Native fallback still configures only type 14.'}
    atomic_json(out, report)
    print(json.dumps({'status': report['status'], 'scopes': len(scopes), 'publications': len(records), 'reason': REASON}))


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    for name in ('source', 'probe', 'out'):
        p.add_argument('--'+name, required=True)
    a = p.parse_args()
    run(a.source, a.probe, a.out)
