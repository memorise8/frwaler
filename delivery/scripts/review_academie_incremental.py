"""Review whole press scope order, including undated editorial source sections."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from crawler.academie_source import parse_article
from crawler.document_dates import normalize_date
from crawler.incremental import Entry, infer_order, observe_order
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now

SITE='academie-sciences-fr-espace-presse'
URL='https://www.academie-sciences.fr/espace-presse'
REASON='undated_editorial_sections_and_nonchronological_press_article_dates_prevent_global_early_stop'
DEPENDENCIES=('crawler/academie_source.py','crawler/academie_traversal.py','crawler/academie_pdf_dates.py')


def checked(source, name, expected):
    raw=(Path(source)/name).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=expected:raise ValueError('retained source hash mismatch: '+name)
    return raw


def native_page(c, raw, page):
    original=c._fetch_text
    def fetched(url,**kwargs):
        if url!=c._list_url(page):raise ValueError('unexpected native listing URL')
        return raw.decode('utf-8')
    c._fetch_text=fetched
    try:return c._ac_page(page)
    finally:c._fetch_text=original


def chronology(records):
    proof={}
    observe_order(proof,[Entry(r['key'],r,r.get('date') or '') for r in records],False)
    inversions=[dict(index=i,before=a['key'],after=b['key'],before_date=a['date'],after_date=b['date'])
                for i,(a,b) in enumerate(zip(records,records[1:]))
                if a.get('date') and b.get('date') and b['date']>a['date']]
    return dict(proof=proof,inferred_order=infer_order(proof),descending_date_inversions=inversions)


def retained_source(source):
    source=Path(source)
    fields=json.loads((source/'academie-all120-cursor-fields-independent-review.json').read_text())
    pages=json.loads(checked(source,fields['source_pages_file'],fields['source_pages_sha256']))
    papers=[json.loads(line) for line in checked(source,fields['records_file'],fields['records_sha256']).splitlines()]
    catalogue=json.loads((source/'academie-press-catalogue/audit.json').read_text())
    source_review=json.loads((source/'academie-all-press-sections-independent-review.json').read_text())
    assert hashlib.sha256((source/'academie-press-catalogue/audit.json').read_bytes()).hexdigest()==source_review['catalogue_audit_sha256']
    responses={r['url']:dict(raw=checked(source,r['file'],r['sha256']),kind='list',file=r['file'],sha256=r['sha256']) for r in catalogue['pages']}
    records=[json.loads(line) for line in (source/'academie-all29-source-detail-records.jsonl').read_text().splitlines()]
    for r in records:
        responses[r['url']]=dict(raw=checked(source,r['source_file'],r['source_sha256']),kind='HTML',file=r['source_file'],sha256=r['source_sha256'])
    return pages,papers,fields,responses


def run(source,probe,out):
    source,probe,out=Path(source),Path(probe),Path(out)
    if out.exists():raise FileExistsError(out)
    expected,papers,fields,responses=retained_source(source)
    c=CRAWLERS[SITE](None,delay=0)
    try:
        root=Path(__file__).resolve().parents[2]
        dependencies={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in DEPENDENCIES}
        assert all(fields['source_hashes'][k]==v for k,v in dependencies.items())
        assert hashlib.sha256(Path(c.crawl.__code__.co_filename).read_bytes()).hexdigest()==fields['source_hashes']['crawler/sites/custom/academie-sciences-fr-espace-presse.py']
        items=[]
        for page in range(9):
            data=native_page(c,responses[c._list_url(page)]['raw'],page)
            assert data==expected[page]
            assert data['has_next']==(page<7)
            items.extend(data['items'])
        assert len(items)==len({i['key'] for i in items})==120 and not expected[8]['items']
        editorial=[i for i in items if i['native'].get('source_section')]
        dated=[i for i in items if not i['native'].get('source_section')]
        assert len(editorial)==44 and len(dated)==76
        list_order=chronology([dict(key=i['key'],date=i['native'].get('listed_datetime')) for i in items])
        assert list_order['inferred_order']==('arbitrary','order_ambiguous')
        assert list_order['proof']['date']['missing']==44
        dated_order=chronology([dict(key=i['key'],date=i['native']['listed_datetime']) for i in dated])
        assert dated_order['inferred_order']==('newest_first','date')
        press=[i for i in editorial if i['native']['source_section']=='Parutions dans la presse']
        detail_dates=[]
        for item in press:
            parsed=parse_article(responses[item['url']]['raw'].decode('utf-8'),item['native'])
            d=normalize_date(parsed['listed_date_raw'],order='DMY')
            assert d['status']=='normalized' and d['precision']=='day'
            detail_dates.append(dict(key=item['key'],date=d['value']))
        detail_order=chronology(detail_dates)
        assert len(press)==22 and detail_order['inferred_order']==('arbitrary','order_ambiguous')
        assert detail_order['descending_date_inversions']
        reqs=json.loads((probe/'audit.json').read_text())['requests']
        assert len(reqs)==3 and [r['url'] for r in reqs]==[URL,press[0]['url'],press[1]['url']]
        fresh={}
        for r in reqs:
            assert r['status']==200
            fresh[r['url']]=checked(probe,r['body'],r['sha256'])
        now=native_page(c,fresh[URL],0)
        assert now==expected[0]
        soup=c._ac_soup(fresh[URL].decode('utf-8'))
        assert not soup.select('.NodeEdito--pressArea > .ParagraphAccordion time,.NodeEdito--pressArea > .ParagraphAccordion [datetime]')
        actual_dates=[]
        for item in press[:2]:
            parsed=parse_article(fresh[item['url']].decode('utf-8'),item['native'])
            d=normalize_date(parsed['listed_date_raw'],order='DMY');assert d['status']=='normalized'
            actual_dates.append(dict(url=item['url'],date=d['value']))
        assert [x['date'] for x in actual_dates]==[x['date'] for x in detail_dates[:2]]
        assert actual_dates[1]['date']>actual_dates[0]['date']
        report=dict(status='passed',reviewed_at=utc_now(),site_id=SITE,classification='full_rescan_required',reason=REASON,
            source_sha256=hashlib.sha256(Path(c.crawl.__code__.co_filename).read_bytes()).hexdigest(),
            dependency_source_sha256=dependencies,retained_source_targets=120,retained_native_eligible_metadata=119,
            retained_source_kinds=dict(Counter(i['native'].get('kind','PDF') for i in items)),
            retained_listing_pages=8,retained_adjacent_empty_page=8,source_listing_date_missing=44,
            native_whole_listing_order=list_order,dated_paginated_subset_order=dated_order,
            editorial_counts=dict(Counter(i['native']['source_section'] for i in editorial)),
            retained_press_detail_order=detail_order,fresh_first_page_all54_items_unchanged=True,
            fresh_editorial_cards_without_dates=44,fresh_nonchronological_article_pair=actual_dates,
            fresh_HTTP_requests=3,all120_current_source_targets_recrawled=False,
            stable_source_path_IDs_are_not_monotonic=True,source_body_exclusions=len(fields['exclusions']),
            global_source_complete=False,all_files_verified=False,operating_database_touched=False,source_documents_deleted=False,
            note='The dated 76-item pager is only part of the delivered scope. The other 44 editorial targets lack listing dates, and their linked press articles have verified date reversals. A partial pager baseline cannot certify whole-scope early stopping.')
        atomic_json(out,report);print(json.dumps({k:report[k] for k in ('status','retained_source_targets','source_listing_date_missing','reason')}))
    finally:c._session.close()


if __name__=='__main__':
    p=argparse.ArgumentParser()
    for name in ('source','probe','out'):p.add_argument('--'+name,required=True)
    a=p.parse_args();run(a.source,a.probe,a.out)
