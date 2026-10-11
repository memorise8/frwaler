"""Native APRA series scope evidence, explicitly separate from file inventories."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
from unittest.mock import patch

from crawler.incremental_apra import APRAAdapter
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='apra-gov-au-statistics'


def retained(source):
    source=Path(source);c=CRAWLERS[SITE](None,delay=0);a=APRAAdapter(c);pages={}
    def checked(file,sha):
        raw=(source/file).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('APRA retained source changed')
        return raw
    try:
        audit=json.loads((source/'apra-gov-au-statistics-exact-delivered-entries/audit.json').read_text())
        first=next(r for r in audit['responses'] if r['scope']=='scope-0' and r['response']['status']==200)
        file='apra-gov-au-statistics-exact-delivered-entries/'+first['file'];pages[0]=dict(raw=checked(file,first['sha256']),file=file,sha256=first['sha256'])
        follow=json.loads((source/'eighteenth-eight-followups-independent-review.json').read_text());detail=None
        for r in follow['rows']:
            if r['site_id']!=SITE:continue
            if r['scope'].startswith('scope-0-page-'):
                n=int(r['scope'].rsplit('-',1)[1]);pages[n]=dict(raw=checked(r['source_file'],r['source_sha256']),file=r['source_file'],sha256=r['source_sha256'])
            elif r['scope']=='scope-0-first-detail':
                detail=dict(raw=checked(r['source_file'],r['source_sha256']),url=r['url'],file=r['source_file'],sha256=r['source_sha256'])
        if set(pages)!=set(range(8)) or detail is None:raise ValueError('missing APRA source boundary/detail')
        items=[]
        for n,p in sorted(pages.items()):
            a.response=lambda *args,**kwargs:p['raw'].decode()
            rows,nxt,total=a.read(n,empty_after=6 if n==7 else None)
            if total!=63 or len(rows)!=(10 if n<6 else 3 if n==6 else 0) or nxt!=(n<6):
                raise ValueError('APRA retained source membership changed')
            items+=rows
        if len({r['url'] for r in items})!=63 or any(b['date']>x['date'] for x,b in zip(items,items[1:])):
            raise ValueError('APRA full series order changed')
        prior=json.loads((source/'eighteenth-native-scope-and-detail-review.json').read_text())['results']['APRA']['actual_native_first_series_record']
        a.response=lambda *args,**kwargs:detail['raw'].decode()
        if a.materialize(items[0])!=prior:raise ValueError('actual first-series native metadata changed')
        papers=[]
        def save(paper):papers.append(paper);c._last_save_created=True;return 1
        def curl(url,*args,**kwargs):
            if url==c.crawl.__globals__['_LIST_URL']:return pages[0]['raw'].decode()
            if url==detail['url']:return detail['raw'].decode()
            raise AssertionError('unretained native request')
        with patch.dict(c.crawl.__globals__,{'_curl':curl}),patch.object(c,'_save_paper',save),patch('time.sleep'),contextlib.redirect_stdout(io.StringIO()):
            if c.crawl(limit=1)!=1 or papers!=[prior]:raise ValueError('adapter differs from current native materialization')
        return pages,items,detail,prior
    finally:c._session.close()


def review(source,probe):
    pages,items,detail,prior=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text());c=CRAWLERS[SITE](None,delay=0);a=APRAAdapter(c)
    try:
        if len(audit['requests'])!=1:raise ValueError('unexpected current request count')
        r=audit['requests'][0];raw=(probe/r['body']).read_bytes();a.response=lambda *args,**kwargs:raw.decode()
        current,nxt,total=a.read(0)
        if (r['url']!=a.url(0) or r['status']!=200 or hashlib.sha256(raw).hexdigest()!=r['sha256'] or current!=items[:10] or total!=63):
            raise ValueError('APRA current first series page changed')
        code=Path(__file__).resolve().parents[2];cls=type(c)
        return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,native_scope='statistical-series pages only',
            series_URLs=63,source_order='newest_first',source_date_missing=0,source_date_ascending_pairs=0,
            source_exhaustion='total63, final pager and adjacent filtered empty page',current_first_items_match=10,
            actual_live_HTTP_requests=1,actual_native_first_paper_equal=True,actual_first_pdf_is_glossary=True,
            per_release_files_enumerated=False,other_two_delivered_scopes_configured=False,all_actual_details_verified=False,
            source_response_hashes={p['file']:p['sha256'] for p in pages.values()},actual_detail_sha256=detail['sha256'],
            current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
            source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
            dependency_source_sha256={p:hashlib.sha256((code/p).read_bytes()).hexdigest() for p in
                ('crawler/incremental_apra.py','crawler/document_dates.py','delivery/health/access_challenge.py')},
            baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)
    finally:c._session.close()


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    r=review(a.source,a.probe);atomic_json(out,r);print({'status':r['status'],'series_URLs':63,'current_first_items_match':10})
