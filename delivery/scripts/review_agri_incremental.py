"""Review native datatable order and scope; retain source insertion-date semantics."""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path

from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='agri-ee-ministeerium-uudised'
REASON='embedded_table_dates_nonmonotonic_and_published_history_routes_uncollected'


def retained(source):
    source=Path(source);ledger=json.loads((source/'site-progress.json').read_text())
    facts=next(r for r in ledger['sites'] if r['site_id']==SITE)['source_current_named_catalogue_facts']
    def checked(name,sha):
        raw=(source/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('retained source hash changed')
        return raw
    memberships=[json.loads(s) for s in checked(facts['membership_file'],facts['membership_sha256']).decode().splitlines()]
    if len(facts['pages'])!=1:raise ValueError('source page contract changed')
    page=facts['pages'][0];raw=checked(page['source_file'],page['source_sha256'])
    c=CRAWLERS[SITE](None,delay=0)
    try:
        soup=c._make_soup(raw.decode(),context='retained review');items=c._parse_records(soup)
        expected={r['url']:r for r in memberships}
        if len(items)!=125 or len(expected)!=125 or {r['item_url'] for r in items}!=set(expected):
            raise ValueError('125 original memberships changed')
        if len({r['native_slug'] for r in items})!=125:raise ValueError('native identity collision')
        for r in items:
            m=expected[r['item_url']]
            if (r['title'],r['row_html'],r['datatable_id'],r['row_index'],r['listed_datetime_raw'],r['posted_date_raw'])!=(m['title'],m['source_cells'],m['table_id'],m['row_index'],m['datetime_raw'],m['date_raw']):
                raise ValueError('original list fields changed')
            if r['listed_date']!=c._iso_date(m['datetime_raw']):raise ValueError('insertion date changed')
        tables=defaultdict(list)
        for r in items:tables[r['datatable_id']].append(r['listed_date'])
        order=[]
        for key,dates in tables.items():
            options=json.loads(soup.find('script',id=key+'-options').string)
            if options.get('paging') is not False:raise ValueError('static table contract changed')
            order.append(dict(table_id=key,rows=len(dates),missing=sum(not d for d in dates),
                increasing_pairs=sum(a<b for a,b in zip(dates,dates[1:]) if a and b),
                decreasing_pairs=sum(a>b for a,b in zip(dates,dates[1:]) if a and b)))
        if len(order)!=8 or not any(r['increasing_pairs'] and r['decreasing_pairs'] for r in order):
            raise ValueError('nonmonotonic evidence changed')
        archives=[a['href'] for a in soup.select('a[href]') if a['href'].startswith('https://veebiarhiiv.digar.ee/')]
        if len(set(archives))!=3 or c._next_page_url(soup,c.START_URL) is not None:
            raise ValueError('original archive/navigation contract changed')
        return raw,items,dict(source_file=page['source_file'],retained_body_sha256=page['source_sha256'],
            membership_file=facts['membership_file'],membership_sha256=facts['membership_sha256'],
            retained_unique_records=125,table_orders=order,uncollected_published_history_routes=archives)
    finally:c._session.close()


def review(source,probe):
    raw,items,facts=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    if len(audit['requests'])!=1:raise ValueError('unexpected actual request count')
    r=audit['requests'][0];body=(probe/r['body']).read_bytes();cls=CRAWLERS[SITE]
    if r['url']!=cls.START_URL or r['status']!=403 or hashlib.sha256(body).hexdigest()!=r['sha256'] or not cls.crawl.__globals__['_is_cf_challenge'](body.decode()):
        raise ValueError('current entry evidence changed; renew review')
    code=Path(__file__).resolve().parents[2]
    return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,classification='full_rescan_required',reason=REASON,
        **facts,actual_live_HTTP_requests=1,current_entry_status=403,current_entry_challenge=True,
        current_source_matches_retained=False,current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        current_source_total=None,date_column_role='insertion date; publication date equivalence unverified',
        native_slug_and_original_dates_preserved=True,native_abstract_is_generated_metadata_not_document_body=True,
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={'delivery/health/access_challenge.py':hashlib.sha256((code/'delivery/health/access_challenge.py').read_bytes()).hexdigest()},
        baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    report=review(a.source,a.probe);atomic_json(out,report)
    print(json.dumps({k:report[k] for k in ('status','retained_unique_records','actual_live_HTTP_requests','current_entry_status')}))
