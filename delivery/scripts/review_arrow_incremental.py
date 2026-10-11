"""Evidence-backed full rescan: unsorted OAI update/publication dates and IDs."""
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
from unittest.mock import patch
import xml.etree.ElementTree as ET

from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json,utc_now

SITE='arrow-tudublin-ie-creaart'
REASON='native_OAI_dates_and_crosslisted_IDs_nonmonotonic_HTML_year_groups_no_verified_newest_access'


def capture(raw):
    c=CRAWLERS[SITE](None,delay=0);papers=[];ns=c.crawl.__globals__['_NS']
    root=ET.fromstring(raw);records=root.findall('oai:ListRecords/oai:record',ns)
    def save(p):papers.append(p);c._last_save_created=True
    try:
        with patch.object(c,'_save_paper',save),contextlib.redirect_stdout(io.StringIO()):
            for record in records:c._process_record(record,set())
        if c.delivery_source_failure:raise ValueError('Arrow native parse failure')
        return records,papers
    finally:c._session.close()


def order(values):
    pairs=[(a,b) for a,b in zip(values,values[1:]) if a and b]
    return dict(first=values[0],last=values[-1],missing=sum(not v for v in values),
                increasing_pairs=sum(a<b for a,b in pairs),decreasing_pairs=sum(a>b for a,b in pairs))


def retained(source):
    source=Path(source);prior=json.loads((source/'nineteenth-arrow231-new-save-committed-review.json').read_text())
    follow=json.loads((source/'nineteenth-eight-followups-independent-review.json').read_text())
    ns=CRAWLERS[SITE].crawl.__globals__['_NS'];pages=[];records=[];papers=[];hashes={}
    def checked(name,sha):
        raw=(source/name).read_bytes()
        if hashlib.sha256(raw).hexdigest()!=sha:raise ValueError('Arrow retained evidence changed')
        hashes[name]=sha;return raw
    for row in follow['rows']:
        if row['site_id']!=SITE or row.get('classification')!='actual_OAI_page_reviewed':continue
        raw=checked(row['source_file'],row['source_sha256']);rr,pp=capture(raw);root=ET.fromstring(raw)
        token=root.find('oai:ListRecords/oai:resumptionToken',ns)
        if len(rr)!=row['memberships'] or token is None or token.attrib!=row['source_token_attributes']:
            raise ValueError('OAI count/token contract changed')
        pages.append(dict(raw=raw,url=row['url'],next_token=token.text.strip() if token.text else None))
        records+=rr;papers+=pp
    if [len(ET.fromstring(p['raw']).findall('oai:ListRecords/oai:record',ns)) for p in pages]!=[100,100,31] or pages[-1]['next_token']:
        raise ValueError('retained OAI traversal changed')
    if papers!=prior['after_replay']['papers'] or len(papers)!=229:raise ValueError('native OAI paper fields changed')
    html_records=[json.loads(line) for line in checked(prior['HTML_records_file'],prior['HTML_records_sha256']).decode().splitlines()]
    oai_memberships=[json.loads(line) for line in checked(prior['source_records_file'],prior['source_records_sha256']).decode().splitlines()]
    if len(html_records)!=231 or len(oai_memberships)!=231:raise ValueError('original catalogue size changed')
    oai_urls=set()
    for record in records:
        oai_urls.update(e.text for e in record.findall('oai:metadata/oai_dc:dc/dc:identifier',ns) if e.text and re.fullmatch(r'https://arrow\.tudublin\.ie/\w+/\d+',e.text))
    if oai_urls!={r['url'] for r in html_records}:raise ValueError('original HTML/OAI membership differs')
    stamps=[r.findtext('oai:header/oai:datestamp',namespaces=ns) for r in records]
    dates=[r.findtext('oai:metadata/oai_dc:dc/dc:date',namespaces=ns) for r in records]
    ids=[r.findtext('oai:header/oai:identifier',namespaces=ns) for r in records]
    proofs=dict(OAI_datestamp=order(stamps),publication_date=order(dates),native_OAI_identifier=order(ids),
                internal_numeric_suffix=order([int(i.rsplit('-',1)[-1]) for i in ids]))
    if any(not p['increasing_pairs'] or not p['decreasing_pairs'] for p in proofs.values()):raise ValueError('Arrow order evidence changed')
    # HTML grouping labels carry only years; preserve that precision.
    from bs4 import BeautifulSoup
    html_dates=[];html_numbers=[]
    for filename in dict.fromkeys(r['source_file'] for r in html_records):
        members=[r for r in html_records if r['source_file']==filename]
        raw=checked(filename,members[0]['source_sha256']);soup=BeautifulSoup(raw,'html.parser')
        labels=[h.get_text(' ',strip=True) for h in soup.select('#series-home h3[id^="year_"]')]
        if not labels or any(not re.fullmatch(r'Submissions from \d{4} \d{4}',v) for v in labels):raise ValueError('HTML year grouping changed')
        html_dates+=labels;html_numbers += [int(r['url'].rstrip('/').rsplit('/',1)[-1]) for r in members]
    html_number_order=order(html_numbers)
    return pages,papers,dict(retained_OAI_records=231,original_HTML_URLs=231,native_eligible_records=229,
        native_short_abstract_exclusions=prior['native_excluded_records'],source_order_proof=proofs,
        HTML_group_labels=html_dates,HTML_display_number_order=html_number_order,source_hashes=hashes,
        crosslisted_paths_preserved=True,all229_native_paper_fields_preserved=True,
        OAI_from_date_harvesting_implemented_or_validated=False)


def review(source,probe):
    pages,papers,facts=retained(source);probe=Path(probe);audit=json.loads((probe/'audit.json').read_text())
    if audit['status']!='passed' or len(audit['requests'])!=1:raise ValueError('current Arrow probe incomplete')
    row=audit['requests'][0];raw=(probe/row['body']).read_bytes();records,now=capture(raw);_,old=capture(pages[0]['raw'])
    if row['url']!=pages[0]['url'] or row['status']!=200 or hashlib.sha256(raw).hexdigest()!=row['sha256'] or len(records)!=100 or now!=old:
        raise ValueError('current native first-page fields changed')
    root=Path(__file__).resolve().parents[2];cls=CRAWLERS[SITE]
    return dict(status='passed',site_id=SITE,reviewed_at=utc_now(),classification='full_rescan_required',reason=REASON,
        **facts,actual_live_HTTP_requests=1,current_first_page_records=100,current_first_page_paper_fields_match=True,
        source_sha256=hashlib.sha256(Path(cls.crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={'delivery/health/access_challenge.py':hashlib.sha256((root/'delivery/health/access_challenge.py').read_bytes()).hexdigest()},
        fresh_source_sha256=row['sha256'],source_order=None,baseline_complete=False,global_source_complete=False,
        all_files_verified=False,operating_database_touched=False)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--source',required=True);p.add_argument('--probe',required=True);p.add_argument('--out',required=True)
    args=p.parse_args();out=Path(args.out)
    if out.exists():raise FileExistsError(out)
    result=review(args.source,args.probe);atomic_json(out,result);print({k:result[k] for k in ('status','native_eligible_records','reason')})
