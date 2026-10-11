"""ANSTO mixed search membership and missing list order contract."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from unittest.mock import patch

from bs4 import BeautifulSoup
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='ansto-gov-au-search'
REASON='undated_search_order_and_direct_PDF_media_omitted_by_native_parser'


def parsed(raw):
    text=raw.decode();m=re.search(r'<textarea[^>]*>(.*?)</textarea>',text,re.S)
    commands=json.loads(m[1] if m else text)
    html=next(x['data'] for x in commands if x.get('command')=='insert' and x.get('method')=='replaceWith')
    soup=BeautifulSoup(html,'html.parser');g=CRAWLERS[SITE].crawl.__globals__
    with patch.dict(g,{'_curl_get':lambda *a,**k:text}):items=g['_fetch_search_page'](0)
    return soup,items


def retained(source):
    source=Path(source);review=json.loads((source/'eighteenth-eight-followups-independent-review.json').read_text());pages={}
    for r in review['rows']:
        if r['site_id']!=SITE or r['scope'] not in ('native-AJAX-first','exact-native-AJAX-page-1','first-direct-PDF-media'):continue
        raw=(source/r['source_file']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=r['source_sha256']:raise ValueError('retained response changed')
        if r['scope']=='first-direct-PDF-media':
            if not raw.startswith(b'%PDF-') or len(raw)!=r['bytes']:raise ValueError('actual omitted PDF changed')
            pages['pdf']=dict(raw=raw,evidence=r);continue
        soup,items=parsed(raw)
        if [x['url'] for x in items]!=r['native_list_URLs']:raise ValueError('native candidate membership changed')
        media=[a['href'] for a in soup.select('article.teaser a[type^="application/pdf"]')]
        if len(soup.select('article.teaser'))!=20 or len(media)!=r['source_direct_media_records'] or soup.select('time, [datetime]'):
            raise ValueError('mixed undated search shape changed')
        pages[0 if r['scope']=='native-AJAX-first' else 1]=dict(raw=raw,items=items,media=media,evidence=r)
    if set(pages)!={0,1,'pdf'} or len(pages[0]['items'])!=20 or len(pages[1]['items'])!=8:
        raise ValueError('missing search omission evidence')
    if 'https://www.ansto.gov.au'+pages[1]['media'][0]!=pages['pdf']['evidence']['url']:
        raise ValueError('omitted PDF correspondence changed')
    return pages


def review(source,probe):
    pages=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    if len(audit['requests'])!=1:raise ValueError('unexpected live request count')
    r=audit['requests'][0];raw=(probe/r['body']).read_bytes();soup,items=parsed(raw)
    if (r['status']!=200 or r['url']!=pages[1]['evidence']['url'] or hashlib.sha256(raw).hexdigest()!=r['sha256']
            or items!=pages[1]['items'] or [a['href'] for a in soup.select('article.teaser a[type^="application/pdf"]')]!=pages[1]['media']):
        raise ValueError('current mixed membership changed')
    code=Path(__file__).resolve().parents[2];cls=CRAWLERS[SITE]
    return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,classification='full_rescan_required',reason=REASON,
        sampled_search_hits=40,native_HTML_candidates=28,omitted_direct_PDF_media=12,actual_omitted_PDF_bytes_checked=1,
        current_page1_native_items_match=8,current_page1_omitted_media_match=12,actual_live_HTTP_requests=1,
        source_date_fields_in_sampled_lists=0,detail_only_IDs_not_inferred=True,source_order=None,
        all2100_IDs_enumerated=False,native_media_scope_bug_fixed=False,PDF_path_month_not_publication_date=True,
        source_response_hashes={p['evidence']['source_file']:p['evidence']['source_sha256'] for p in pages.values()},
        current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={'delivery/health/access_challenge.py':hashlib.sha256((code/'delivery/health/access_challenge.py').read_bytes()).hexdigest()},
        baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    r=review(a.source,a.probe);atomic_json(out,r);print({'status':r['status'],'omitted_direct_media':12,'live_HTTP':1})
