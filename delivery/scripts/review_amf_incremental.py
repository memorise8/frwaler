"""Reconcile delivered AMF feed memberships with the narrower native sitemap."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='amf-france-org-fr'
REASON='native_sitemap_scope_omits_delivered_rows_and_shared_URL_history_unresolved'


def native_entries(raw):
    c=CRAWLERS[SITE](None,delay=0)
    try:
        with patch.dict(c.crawl.__globals__,{'_curl_get':lambda url:(raw.decode(),{})}),contextlib.redirect_stdout(io.StringIO()):
            return c._fetch_sitemap_urls()
    finally:c._session.close()


def retained(source):
    source=Path(source);p=source/'amf-delivered-scope-sources-independent-review.json';prior=json.loads(p.read_text())
    def checked(name,sha):
        raw=(source/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('AMF retained evidence changed')
        return raw
    raw=checked('amf-scope-support-16MiB/sitemap.body',prior['source_sitemap_sha256'])
    name='amf-all-client-listing-memberships.jsonl';rows=[json.loads(s) for s in checked(name,prior['files'][name]).decode().splitlines()]
    feed_files={r['source_file']:r['source_sha256'] for r in rows}
    for name,sha in feed_files.items():
        feed=json.loads(checked(name,sha))['data'];expected=[r['source_row'] for r in rows if r['source_file']==name]
        if feed!=expected:raise ValueError('original feed memberships differ')
    entries=native_entries(raw);native={u for u,d in entries};client={r['url'] for r in rows}
    if len(entries)!=769 or len(native)!=765 or len(rows)!=1171 or len(client)!=1167 or len(client-native)!=404:
        raise ValueError('original scope discrepancy changed')
    d=json.loads((source/'amf-scope-correspondence-details-independent-review.json').read_text())
    details=[json.loads(s) for s in checked(d['records_file'],d['records_sha256']).decode().splitlines()];actual={}
    extract=CRAWLERS[SITE].crawl.__globals__['_extract_article']
    for r in details:
        body=checked(r['source_file'],r['source_sha256']);fields=extract(body.decode(),r['url'])
        if fields['external_id']!=r['native_id'] or fields['published_date']!=r['published_date'] or hashlib.sha256(fields['abstract'].encode()).hexdigest()!=r['native_abstract_sha256']:
            raise ValueError('actual native detail fields changed')
        actual[r['url']]=dict(raw=body,fields=fields)
    if len(actual)!=41:raise ValueError('actual detail sample missing')
    return raw,entries,actual,dict(retained_native_memberships=769,retained_native_unique_URLs=765,
        retained_client_memberships=1171,retained_client_unique_URLs=1167,retained_client_missing_native=404,
        original_scope_urls=[r['url'] for r in prior['delivered_rows']],
        sitemap_only_candidates_unresolved=22,historical_memberships_without_distinct_current_details=4,
        shared_URL_history_groups=d['shared_URL_history_groups'],actual_retained_detail_fields_checked=41,
        sitemap_lastmod_is_not_publication_date=True,source_sitemap_sha256=prior['source_sitemap_sha256'],
        source_feed_hashes=feed_files,original_csv_sha256=prior['delivery_csv_sha256'])


def review(source,probe):
    raw,entries,details,facts=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    if len(audit['requests'])!=1:raise ValueError('unexpected current request count')
    r=audit['requests'][0];body=(probe/r['body']).read_bytes();cls=CRAWLERS[SITE]
    if r['url']!=cls.SITEMAP_URL or r['status']!=200 or hashlib.sha256(body).hexdigest()!=r['sha256']:
        raise ValueError('current sitemap unavailable')
    current=native_entries(body);old={u for u,d in entries};new={u for u,d in current}
    code=Path(__file__).resolve().parents[2];deps=['delivery/health/access_challenge.py','docs/deliverables/test_URL_수집대조_20260802.csv']
    if hashlib.sha256((code/deps[-1]).read_bytes()).hexdigest()!=facts['original_csv_sha256']:raise ValueError('original CSV changed')
    return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,classification='full_rescan_required',reason=REASON,
        **facts,actual_live_HTTP_requests=1,current_native_memberships=len(current),current_native_unique_URLs=len(new),
        current_native_added_URLs=sorted(new-old),current_native_removed_URLs=sorted(old-new),
        current_native_entries_match_retained=current==entries,native_attempt_cap=cls.MAX_PAGES,
        original_feed_currently_reprobed=False,scope_equivalence_verified=False,source_order=None,
        current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={p:hashlib.sha256((code/p).read_bytes()).hexdigest() for p in deps},
        baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    r=review(a.source,a.probe);atomic_json(out,r);print({k:r[k] for k in ('status','retained_client_missing_native','current_native_unique_URLs','current_native_entries_match_retained')})
