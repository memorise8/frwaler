"""Retain original scope/filter and pagination contradictions without summing scopes."""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='agriculture-gouv-fr-recherche-developpem'
REASON='original_scope_query_mismatch_and_theme_pagination_end_unverified'


def cards(raw):
    cls=CRAWLERS[SITE];soup=cls.crawl.__globals__['_make_soup'](raw);items=[]
    for body in soup.select('div.fr-card__body'):
        a=body.select_one('a.fr-card__link');date=body.select_one('span.fr-card__date')
        if a is None or date is None:continue
        href=a.get('href','')
        if not href.startswith('/') or len(href)<10 or any(x in href for x in ('espace-presse','mots-cles','thematiques','?','#')):continue
        d=date.get_text(strip=True);detail=body.select_one('p.fr-card__detail');desc=body.select_one('p.fr-card__desc')
        items.append(dict(url=cls.base_url+href,slug=href.strip('/').split('/')[-1],title=a.get_text(strip=True),
            listed_date=cls.crawl.__globals__['_parse_fr_date'](d),listed_date_raw=d,
            category=detail.get_text(strip=True).replace(d,'').strip() if detail else '',
            card_desc=desc.get_text(strip=True) if desc else ''))
    return items


def reported(raw):
    text=BeautifulSoup(raw,'html.parser').get_text(' ',strip=True)
    return [tuple(map(int,m)) for m in re.findall(r'(\d+) résultats\s*\(page (\d+) sur (\d+)\)',text)]


def retained(source):
    source=Path(source);p=source/'seventeenth-france-pagination-independent-review.json'
    review=json.loads(p.read_text());wanted=('scope-0','scope-1','scope-2','exact-native-query-first','scope-1-page-5','scope-1-page-6','scope-1-page-7');pages={}
    for r in review['responses']:
        if r['scope'] not in wanted:continue
        raw=(source/r['source_file']).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=r['sha256']:raise ValueError('retained response changed')
        soup=BeautifulSoup(raw,'html.parser')
        links=[urljoin(CRAWLERS[SITE].base_url,a['href']) for a in soup.select('div.fr-card__body a.fr-card__link[href]')]
        if links!=[i['url'] for i in r['cards']]:raise ValueError('retained card memberships changed')
        pages[r['scope']]=dict(raw=raw,evidence=r)
    if set(pages)!=set(wanted):raise ValueError('missing reviewed scope evidence')
    if reported(pages['scope-2']['raw'])!=[(1686,1,422)] or reported(pages['exact-native-query-first']['raw'])!=[(2001,1,501)]:
        raise ValueError('scope count discrepancy changed')
    urls=lambda key:{r['url'] for r in pages[key]['evidence']['cards']}
    if len(urls('scope-1-page-6'))!=20 or urls('scope-1-page-5') & urls('scope-1-page-6') or urls('scope-1-page-6')!=urls('scope-1-page-7'):
        raise ValueError('source end discrepancy changed')
    soup=BeautifulSoup(pages['scope-1']['raw'],'html.parser')
    if not any('Dernière page' in a.get_text(' ',strip=True) and a.get('href','').endswith('pg=5') for a in soup.select('a[href]')):
        raise ValueError('advertised final page changed')
    return pages


def review(source,probe):
    pages=retained(source);cls=CRAWLERS[SITE];native=cls.crawl.__globals__['_LIST_URL'].format(pg=1)
    if native!=pages['exact-native-query-first']['evidence']['url']:raise ValueError('native list contract changed')
    code=Path(__file__).resolve().parents[2];csv_path=code/'docs/deliverables/test_URL_수집대조_20260802.csv'
    original=[pages[k]['evidence']['url'] for k in ('scope-0','scope-1','scope-2')]
    catalogue=list(csv.DictReader(csv_path.open(encoding='utf-8-sig')))
    if any(u not in {r['URL주소'] for r in catalogue} for u in original) or native in original:
        raise ValueError('original scope contract changed')
    probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    if len(audit['requests'])!=1:raise ValueError('unexpected current request count')
    r=audit['requests'][0];raw=(probe/r['body']).read_bytes();items=cards(pages['exact-native-query-first']['raw'])
    if r['url']!=native or r['status']!=200 or hashlib.sha256(raw).hexdigest()!=r['sha256'] or cards(raw)!=items or reported(raw)!=[(2001,1,501)]:
        raise ValueError('current native first-page fields changed')
    return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,reason=REASON,classification='full_rescan_required',
        original_scope_urls=original,native_list_url=native,scope_equivalence_verified=False,
        retained_original_press_reported_total=1686,current_native_reported_total=2001,current_native_reported_pages=501,
        native_page_cap=cls.crawl.__globals__['_MAX_PAGES'],current_first_page_fields_match=len(items),actual_live_HTTP_requests=1,
        theme_advertised_last=5,theme_page6_new_cards=20,theme_page7_repeats6=True,
        source_order='unverified_for_original_whole_scope',whole_source_total=None,source_exhaustion_observed=False,
        original_scopes_not_summed=True,source_response_hashes={v['evidence']['source_file']:v['evidence']['sha256'] for v in pages.values()},
        current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={str(p.relative_to(code)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (csv_path,code/'delivery/health/access_challenge.py')},
        baseline_complete=False,global_source_complete=False,all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    r=review(a.source,a.probe);atomic_json(out,r)
    print(json.dumps({k:r[k] for k in ('status','actual_live_HTTP_requests','current_first_page_fields_match')}))
