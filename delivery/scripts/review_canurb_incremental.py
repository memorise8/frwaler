"""Review CANURB's complete public catalogue without assuming ID chronology."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from crawler.incremental import Entry, infer_order, observe_order
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now

SITE = 'canurb-org-publications'
URL = 'https://canurb.org/publications/'
REASON = 'listing_dates_absent_and_wordpress_ids_nonmonotonic_in_complete_public_catalogue'


def order_evidence(records):
    proof = {}
    # Native cards supply no publication date. Do not manufacture one from an
    # excerpt, a title year, URL or another publication's web posting date.
    observe_order(proof, [Entry(r['post_id'], r, '') for r in records], True)
    return {'items': len(records), 'first_id': records[0]['post_id'], 'last_id': records[-1]['post_id'],
            'proof': proof, 'inferred_order': infer_order(proof),
            'descending_id_inversions': [{'index': i, 'before': a['post_id'], 'after': b['post_id']}
                for i, (a, b) in enumerate(zip(records, records[1:])) if int(b['post_id']) > int(a['post_id'])],
            'link_types': dict(Counter(r['link_type'] for r in records))}


def read(c, raw):
    soup = c._make_soup(raw.decode('utf-8'))
    cards = soup.select("article[class*='publications']")
    records = c._parse_list(soup)
    if len(cards) != len(records) or not records:
        raise ValueError('unrecognized catalogue cards')
    if len({r['post_id'] for r in records}) != len(records) or any(not r['post_id'].isdecimal() for r in records):
        raise ValueError('missing or duplicate native card ID')
    if soup.select("article[class*='publications'] time, article[class*='publications'] [datetime]"):
        raise ValueError('new source date markup requires renewed order review')
    return records


def run(source, probe, out):
    source, probe, out = Path(source), Path(probe), Path(out)
    if out.exists():
        raise FileExistsError(out)
    prior = json.loads((source/'canurb-observed-persistence-independent-review.json').read_text())
    capture = source/'canurb-observed-persistence'
    audit_path = capture/'audit.json'
    assert hashlib.sha256(audit_path.read_bytes()).hexdigest() == prior['audit_sha256']
    audit = json.loads(audit_path.read_text())
    old_entry = audit['responses'][URL]
    old_raw = (capture/old_entry['file']).read_bytes()
    assert hashlib.sha256(old_raw).hexdigest() == old_entry['sha256']
    observed = json.loads((probe/'audit.json').read_text())['requests']
    assert len(observed) == 1 and observed[0]['url'] == URL and observed[0]['status'] == 200
    fresh_raw = (probe/observed[0]['body']).read_bytes()
    assert hashlib.sha256(fresh_raw).hexdigest() == observed[0]['sha256']
    c = CRAWLERS[SITE](None, delay=0)
    try:
        old, fresh = read(c, old_raw), read(c, fresh_raw)
        assert old == audit['source_records'] == fresh and len(fresh) == 98
        evidence = order_evidence(fresh)
        assert evidence['inferred_order'] == ('arbitrary', 'order_ambiguous')
        assert evidence['proof']['id']['up'] == 26 and evidence['proof']['id']['down'] > 0
        report = {'status': 'passed', 'reviewed_at': utc_now(), 'site_id': SITE,
                  'classification': 'full_rescan_required', 'reason': REASON,
                  'source_sha256': hashlib.sha256(Path(c.crawl.__code__.co_filename).read_bytes()).hexdigest(),
                  'retained_audit_sha256': prior['audit_sha256'], 'retained_list_sha256': old_entry['sha256'],
                  'fresh_list_sha256': observed[0]['sha256'], 'native_card_fields_and_sequence_unchanged': True,
                  'listed_dates_absent': True, 'order_evidence': evidence, 'fresh_requests': 1,
                  'global_source_complete': False,
                  'note': 'Numeric first and last IDs alone would suggest newest-first, but 26 interior reversals invalidate that boundary. Two publisher cards link directly to a PDF or external site and have no native date. Full rescan classification is not a claim that external attachments or the institutional scope are fully verified.'}
        atomic_json(out, report)
        print(json.dumps({'status': report['status'], 'records': 98, 'order': evidence['inferred_order'], 'id_inversions': 26}))
    finally:
        c._session.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    for name in ('source', 'probe', 'out'):
        p.add_argument('--'+name, required=True)
    a = p.parse_args()
    run(a.source, a.probe, a.out)
