"""ARN reporting-period IDs cannot certify publication or insertion chronology."""
import argparse
import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import re
from unittest.mock import patch

from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now

SITE='arn-se-om-arn'
REASON='reporting_period_IDs_not_arrival_chronology_and_three_original_scopes_unconfigured'
CATALOGUE='docs/deliverables/test_URL_수집대조_20260802.csv'


def capture(raw):
    c=CRAWLERS[SITE](None,delay=0);papers=[]
    def save(paper):papers.append(paper);c._last_save_created=True
    try:
        with patch.object(c,'_curl_get',return_value=raw.decode()),patch.object(c,'_save_paper',save),contextlib.redirect_stdout(io.StringIO()):
            count=c.crawl()
        if count!=len(papers) or c.delivery_source_failure:raise ValueError('ARN native replay failed')
        return papers
    finally:c._session.close()


def retained(source):
    source=Path(source);root=Path(__file__).resolve().parents[2]
    audit=json.loads((source/'arn-se-om-arn-exact-delivered-entries/audit.json').read_text())
    prior=json.loads((source/'eighteenth-arn12-date-committed-review.json').read_text())
    with (root/CATALOGUE).open(encoding='utf-8-sig',newline='') as f:
        urls={r['URL주소'] for r in csv.DictReader(f) if r['매칭크롤러(site_id)']==SITE}
    if urls!={r['url'] for r in audit['source_scope_rows']} or len(urls)!=4:raise ValueError('ARN original scopes changed')
    pages=[];c=CRAWLERS[SITE](None,delay=0)
    try:
        for row in audit['responses']:
            name='arn-se-om-arn-exact-delivered-entries/'+row['file'];raw=(source/name).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=row['sha256']:raise ValueError('ARN retained page changed')
            soup=c._make_soup(raw.decode());main=soup.find('main') or soup.find('div',id='main-content') or soup.find('div',id='content') or soup.body
            links=[a['href'] for a in main.find_all('a',href=re.compile(r'\.pdf',re.I))]
            pages.append(dict(raw=raw,url=row['url'],file=name,sha256=row['sha256'],PDF_references=len(links)))
        if [p['PDF_references'] for p in pages]!=[138,12,12,60]:raise ValueError('ARN original reference counts changed')
        annual=next(p for p in pages if p['url']==c._LIST_URL);papers=capture(annual['raw'])
        if papers!=prior['after_records']:raise ValueError('ARN native fields changed')
        if len(papers)!=12 or [p['post_number'] for p in papers]!=[str(y) for y in range(2025,2013,-1)]:raise ValueError('ARN years changed')
        if any(p['published_date'] is not None or p['listed_date'] is not None for p in papers):raise ValueError('fabricated annual date')
        soup=c._make_soup(annual['raw'].decode());main=soup.find('div',id='main-content')
        timestamps=main.select('time[datetime]')
        if len(timestamps)!=1 or 'Uppdaterad:' not in timestamps[0].parent.get_text(' ',strip=True):
            raise ValueError('ARN container update-date role changed')
        dates=[t.get('datetime') for t in timestamps]
        pdf=(source/prior['first_PDF_file']).read_bytes()
        if hashlib.sha256(pdf).hexdigest()!=prior['first_PDF_sha256'] or not pdf.startswith(b'%PDF') or len(pdf)!=prior['first_PDF_bytes']:
            raise ValueError('prior actual PDF evidence changed')
        return pages,papers,dict(original_scope_urls=sorted(urls),native_scope=c._LIST_URL,
            original_scopes=4,native_scopes=1,source_PDF_references_by_scope=[p['PDF_references'] for p in pages],
            annual_reporting_years=[p['post_number'] for p in papers],per_report_publication_dates_missing=12,
            page_update_timestamps=dates,page_update_is_not_report_publication=True,report_year_is_not_arrival_ID=True,
            native_abstract_is_constructed_not_PDF_text=True,retained_first_PDF_bytes_verified=len(pdf),
            first_PDF_sha256=prior['first_PDF_sha256'],signature_date_not_assumed_publication=True,
            original_news_reference_count_not_document_total=True,other_three_scopes_configured=False)
    finally:c._session.close()


def review(source,probe):
    pages,papers,facts=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    if audit['status']!='passed' or len(audit['requests'])!=1:raise ValueError('ARN current probe incomplete')
    row=audit['requests'][0];raw=(probe/row['body']).read_bytes();cls=CRAWLERS[SITE]
    if row['status']!=200 or row['url']!=cls._LIST_URL or hashlib.sha256(raw).hexdigest()!=row['sha256'] or capture(raw)!=papers:
        raise ValueError('ARN current native annual fields changed')
    root=Path(__file__).resolve().parents[2]
    return dict(status='passed',site_id=SITE,reviewed_at=utc_now(),reason=REASON,classification='full_rescan_required',
        **facts,actual_live_HTTP_requests=1,current_12_native_papers_all_fields_match=True,
        source_response_hashes={p['file']:p['sha256'] for p in pages},fresh_source_sha256=row['sha256'],
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in (CATALOGUE,'delivery/health/access_challenge.py')},
        actual_PDF_requests_this_batch=0,all12_PDF_bodies_verified=False,source_order=None,baseline_complete=False,
        global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();out=Path(args.out)
    if out.exists():raise FileExistsError(out)
    result=review(args.source,args.probe);atomic_json(out,result);print({k:result[k] for k in ('status','reason','original_scopes')})
