"""Review ACMA scope omission and bounded transport evidence without guessing order."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path

from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now

SITE = 'acma-gov-au-publications'
REASON = 'original_pdf_search_scope_excluded_and_transport_unavailable_order_and_end_unverified'
SEARCH = 'https://www.acma.gov.au/search?search_api_fulltext=pdf'
DEPENDENCIES = ('crawler/stealth_fetcher.py', 'delivery/health/access_challenge.py',
                'docs/deliverables/test_URL_수집대조_20260802.csv')


def review(source, probe):
    source, probe = Path(source), Path(probe)
    code = Path(__file__).resolve().parents[2]; cls = CRAWLERS[SITE]
    association = json.loads((source/'acma3-missing-collector-scope-association.json').read_text())['associations'][0]
    csv_path = code/DEPENDENCIES[-1]
    if hashlib.sha256(csv_path.read_bytes()).hexdigest() != association['source_csv_sha256']:
        raise ValueError('original catalogue changed')
    rows = list(csv.DictReader(io.StringIO(csv_path.read_text(encoding='utf-8-sig'))))
    original = [r['url'] for r in association['delivered_scope_rows']]
    if any(u not in {r['URL주소'] for r in rows} for u in original):
        raise ValueError('original URL missing from catalogue')
    configured = [u for u, _ in cls.SECTIONS]
    if set(original)-set(configured) != {SEARCH} or len(configured) != 2:
        raise ValueError('scope contract changed; renew review')
    prior = []
    for folder, expected in [('acma-gov-au-publications-exact-delivered-entries', 92),
                             ('seventeenth-acma-http1', 28)]:
        path = source/folder/'audit.json'; audit = json.loads(path.read_text())
        if len(audit['responses']) != 1: raise ValueError('prior request count changed')
        row = audit['responses'][0]; body = (path.parent/row['file']).read_bytes()
        if hashlib.sha256(body).hexdigest() != row['sha256'] or body:
            raise ValueError('prior empty transport evidence mismatch')
        if row['response']['curl_exit'] != expected or row['response']['body_complete']:
            raise ValueError('prior transport state changed')
        prior.append(dict(file=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest(), curl_exit=expected))
    fresh = json.loads((probe/'audit.json').read_text())
    if len(fresh['requests']) != 1: raise ValueError('fresh request count changed')
    request = fresh['requests'][0]
    if request['url'] != configured[0] or request['status'] is not None or request['error_type'] != 'ReadTimeout':
        raise ValueError('new entry evidence requires renewed semantic review')
    return dict(status='passed', site_id=SITE, reviewed_at=utc_now(), reason=REASON,
        classification='full_rescan_required', original_scope_urls=original, native_configured_urls=configured,
        excluded_original_search=SEARCH, scope_equivalence_verified=False, native_page_cap_per_section=cls.MAX_PAGES_PER_SECTION,
        actual_live_HTTP_attempts=1, actual_live_responses=0, prior_transport_evidence=prior,
        current_transport_evidence=dict(file=str(probe/'audit.json'),
            sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
            error_type=request['error_type'], url=request['url']),
        source_count=None, source_order=None, source_exhaustion_observed=False, baseline_complete=False,
        global_source_complete=False, operating_database_touched=False, source_documents_deleted=False,
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={p:hashlib.sha256((code/p).read_bytes()).hexdigest() for p in DEPENDENCIES})


if __name__ == '__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    report=review(a.source,a.probe);atomic_json(out,report)
    print(json.dumps({'status':report['status'],'site_id':SITE,'actual_live_HTTP_attempts':1}))
