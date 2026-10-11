"""AFD list-order evidence, kept separate from detail dates and scope completeness."""
import argparse
import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin,urlsplit

from bs4 import BeautifulSoup
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='afd-fr-fr'
REASON='publication_listing_dates_missing_and_node_IDs_detail_only_order_unverified'


def listing(raw):
    soup=BeautifulSoup(raw,'html.parser')
    if not soup.select_one('input[name="type[170]"][value="170"][checked]'):
        raise ValueError('AFD original type170 filter missing')
    items=[]
    for card in soup.select('.views-row'):
        links=card.select('a.fr-card__link[href]')
        if len(links)!=1:raise ValueError('ambiguous source card')
        url=urljoin('https://www.afd.fr',links[0]['href']);p=urlsplit(url)
        if p.netloc!='www.afd.fr' or not p.path.startswith('/fr/ressources/'):
            raise ValueError('AFD native publication URL changed scope')
        dates=[t['datetime'] for t in card.select('time[datetime]')]
        items.append(dict(url=url,title=links[0].get_text(' ',strip=True),dates=dates,
                          slug=p.path.rstrip('/').rsplit('/',1)[-1]))
    return items


def review(source,probe):
    source,probe=Path(source),Path(probe);cls=CRAWLERS[SITE];code=Path(__file__).resolve().parents[2]
    historical={}
    for folder in ('afd-fr-fr-exact-delivered-entries','sixteenth-public-afd'):
        audit=json.loads((source/folder/'audit.json').read_text())
        for r in audit['responses']:
            raw=(source/folder/r['file']).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=r['sha256']:raise ValueError('retained AFD body hash mismatch')
            historical[r['url']]=dict(body=raw,file=str(Path(folder)/r['file']),sha256=r['sha256'])
    first=cls._LIST_URL+'?'+cls._LIST_QUERY;last=first+'&page=38'
    prior=[listing(historical[u]['body']) for u in (first,last)]
    audit=json.loads((probe/'audit.json').read_text());urls=[first+'&page=0',last]
    if [r['url'] for r in audit['requests']]!=urls:raise ValueError('current native boundaries missing')
    current=[]
    for r in audit['requests']:
        raw=(probe/r['body']).read_bytes()
        if r['status']!=200 or hashlib.sha256(raw).hexdigest()!=r['sha256']:
            raise ValueError('current AFD response invalid')
        current.append(listing(raw))
    if current!=prior or [len(p) for p in current]!=[21,5]:raise ValueError('AFD current boundary membership changed')
    if any(i['dates'] or i['slug'].isdecimal() for i in current[0]):
        raise ValueError('new first-page date/identity contract requires renewed review')
    original=json.loads((source/'afd-fr-fr-exact-delivered-entries/audit.json').read_text())
    return dict(status='passed',reviewed_at=utc_now(),site_id=SITE,classification='full_rescan_required',reason=REASON,
        actual_live_HTTP_requests=2,current_boundary_card_counts=[21,5],first_page_missing_card_dates=21,
        first_page_nonnumeric_URL_identities=21,native_node_IDs_available_only_in_details=True,
        thumbnail_upload_paths_not_used_as_publication_dates=True,
        publication_order=None,whole_catalogue_memberships_not_enumerated=True,
        original_delivered_URLs=[r['url'] for r in original['responses']],native_only_type170_scope=True,
        original_press_scope_unconfigured=True,baseline_complete=False,global_source_complete=False,
        all_files_verified=False,operating_database_touched=False,
        current_probe_sha256=hashlib.sha256((probe/'audit.json').read_bytes()).hexdigest(),
        retained_boundary_evidence=[{k:v for k,v in historical[u].items() if k!='body'} for u in (first,last)],
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={'delivery/health/access_challenge.py':hashlib.sha256((code/'delivery/health/access_challenge.py').read_bytes()).hexdigest()},
        note='Current list contract cannot supply chronology for the first page; detail-date discovery/cache or a verified alternative sort would require a separate adapter contract. This is not proof that the site can never support incremental collection.')


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    a=p.parse_args();out=Path(a.out)
    if out.exists():raise FileExistsError(out)
    report=review(a.source,a.probe);atomic_json(out,report)
    print(json.dumps({k:report[k] for k in ('status','first_page_missing_card_dates','actual_live_HTTP_requests')}))
