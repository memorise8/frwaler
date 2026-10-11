"""One gated live annual-list request through the native loop in an owned DB."""
import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from crawler import db_pg
from crawler.incremental import Store
from crawler.sites import CRAWLERS
from delivery.health.incremental_transport import install
from delivery.health.results import atomic_json,utc_now
from delivery.scripts.review_arn_incremental import SITE,capture,retained


def run(dsn,base,out):
    if not urlsplit(dsn).path[1:].startswith('incremental_rollout_arn_'):raise ValueError('owned ARN database required')
    base=Path(base);out=Path(out);out.mkdir(parents=True,exist_ok=False)
    conn=db_pg.open_db(dsn);c=CRAWLERS[SITE](conn,delay=0);c.delivery_mode='full'
    before=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')];state=Store(conn,SITE).state()
    _,papers,_=retained(base/'continuous-20261010')
    if (len(before)!=12 or {r['post_number'] for r in before}!={p['post_number'] for p in papers}
            or state['support']!='full_rescan_required' or state['baseline_complete']):
        raise ValueError('owned ARN12 fallback fixture required')
    requests=[];attempts=[];report=dict(status='running',started_at=utc_now(),requests=requests,
        fixture_database=True,audited_session_replaces_native_curl=True,native_constructed_abstract_not_PDF_text=True,
        operating_database_touched=False)
    def persist():atomic_json(out/'audit.json',report)
    c._session.headers['Accept-Encoding']='gzip, deflate'
    install(c,base/'continuous-20261010/request-gate',out,requests,persist=persist)
    def fetch(url,*args,**kwargs):
        if url!=c._LIST_URL or requests:raise ValueError('ARN single listing-only request budget exceeded')
        raw=c._session.get(url,timeout=40).content
        if capture(raw)!=papers:raise ValueError('ARN live native fields changed')
        return raw.decode()
    native_save=c._save_paper
    def save(paper):attempts.append(paper['post_number']);return native_save(paper)
    c._curl_get=fetch;c._save_paper=save;persist()
    try:
        saved=c.crawl();after=[dict(r) for r in conn.execute('SELECT * FROM documents ORDER BY seq_id')]
        if saved or before!=after or len(attempts)!=12 or c.delivery_source_failure:
            raise ValueError('ARN native repeat changed fixture rows or failed')
        report.update(status='passed',actual_HTTP_requests=len(requests),actual_detail_requests=0,actual_PDF_requests=0,
            save_attempts=12,saved_new=0,rows=12,existing_rows_unchanged=True,
            stop_condition='native_single_page_all_links_processed',known_item_early_stop=False,
            baseline_complete=False,global_source_complete=False,all_files_verified=False,finished_at=utc_now())
    except Exception as exc:report.update(status='failed',error_type=type(exc).__name__,finished_at=utc_now());raise
    finally:persist();c._session.close();conn.close()
    print({k:report[k] for k in ('status','actual_HTTP_requests','save_attempts','saved_new')})


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--dsn',required=True);p.add_argument('--base',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();run(a.dsn,a.base,a.out)
