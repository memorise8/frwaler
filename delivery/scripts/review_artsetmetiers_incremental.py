"""Reconcile the 225 retained press references and all 223 native papers."""
import argparse
import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from crawler.sites import CRAWLERS
from crawler.incremental_artsetmetiers import ArtsetmetiersAdapter
from delivery.health.results import atomic_json, utc_now

SITE = 'artsetmetiers-fr-fr'
PRIOR_COMMIT = 'd3b83e2133bd0c8cbd3bf9547c172554367f4be1'


def retained(source):
    source = Path(source)
    root = Path(__file__).resolve().parents[2]
    with (root/'docs/deliverables/test_URL_수집대조_20260802.csv').open(encoding='utf-8-sig') as stream:
        original = [row for row in csv.DictReader(stream) if row['매칭크롤러(site_id)'] == SITE]
    if len(original) != 1 or original[0]['URL주소'] != CRAWLERS[SITE]._LIST_URL:
        raise ValueError('Arts delivered scope changed')
    record_file = source / 'nineteenth-artset225-press-memberships.jsonl'
    if hashlib.sha256(record_file.read_bytes()).hexdigest() != '3614c1e96c0c3daa6510cdceb153e172da8dc57d6a0740d117c86c4a8d3a1fe0':
        raise ValueError('Arts retained catalogue changed')
    records = [json.loads(line) for line in record_file.read_text().splitlines()]
    pages, hashes = {}, {}
    for row in records:
        n = 0 if row['scope'] == 'scope-0' else int(row['scope'].rsplit('-', 1)[-1])
        if n in pages: continue
        raw = (source / row['source_file']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != row['source_sha256']: raise ValueError('Arts retained page changed')
        pages[n] = raw; hashes[row['source_file']] = row['source_sha256']
    audit = json.loads((source / 'nineteenth-artset_press/audit.json').read_text())
    row = next(r for r in audit['responses'] if r['url'].endswith('?page=45'))
    raw = (source / 'nineteenth-artset_press' / row['file']).read_bytes()
    if hashlib.sha256(raw).hexdigest() != row['sha256']: raise ValueError('Arts retained end changed')
    pages[45] = raw; hashes['nineteenth-artset_press/'+row['file']] = row['sha256']
    if set(pages) != set(range(46)): raise ValueError('Arts retained traversal incomplete')
    c = CRAWLERS[SITE](None, delay=0); a = ArtsetmetiersAdapter(c); items = []
    try:
        for n in range(46):
            part, nxt, final = a.inspect(pages[n], n, empty_after=44 if n==45 else None)
            if final != 44 or nxt != (n<44) or len(part) != (5 if n<45 else 0):
                raise ValueError('Arts retained pagination changed')
            items += part
        if {i['pdf_url'] for i in items} != {r['url'] for r in records} or len(items) != 225:
            raise ValueError('Arts original membership changed')
        papers = [c._paper(i) for i in items if len(i['abstract']) >= 100]
        prior_raw = subprocess.check_output(['git', 'show', PRIOR_COMMIT+':crawler/sites/custom/artsetmetiers-fr-fr.py'])
        if hashlib.sha256(prior_raw).hexdigest() != '0d51dbe5a8954b839299516b3c5ed260e28a3250988e04de28a7a38180d3a36d':
            raise ValueError('Arts original source changed')
        namespace = {}; exec(compile(prior_raw, 'retained_arts_native.py', 'exec'), namespace)
        old = namespace['ArtsetmetiersFrFrCrawler'](None, delay=0); captured = []
        def save(paper): captured.append(paper); old._last_save_created = True
        try:
            with patch.object(old, '_fetch_page_html', side_effect=lambda n:pages[n].decode()), \
                    patch.object(old, '_save_paper', save), patch('time.sleep'), contextlib.redirect_stdout(io.StringIO()):
                old.crawl()
        finally: old._session.close()
        if papers != captured or len(papers) != 223: raise ValueError('Arts native fields changed')
    finally: c._session.close()
    dates = [i['date'] for i in items]
    return pages, items, papers, dict(source_PDF_references=225, native_eligible_records=223,
        native_short_summary_exclusions=[i for i in items if len(i['abstract']) < 100],
        first_date=dates[0], last_date=dates[-1], increasing_pairs=sum(x<y for x,y in zip(dates,dates[1:])),
        decreasing_pairs=sum(x>y for x,y in zip(dates,dates[1:])), all223_native_paper_fields_preserved=True,
        source_hashes=hashes, actual_retained_listing_bodies=46, source_summary_not_PDF_fulltext=True,
        original_delivered_scopes=1, original_delivered_URL=original[0]['URL주소'])


def review(source, probe):
    pages, items, papers, facts = retained(source); probe = Path(probe)
    audit = json.loads((probe/'audit.json').read_text())
    if audit['status'] != 'passed' or len(audit['requests']) != 3: raise ValueError('Arts probe incomplete')
    c = CRAWLERS[SITE](None, delay=0); a = ArtsetmetiersAdapter(c)
    try:
        for row, n in zip(audit['requests'], (0,44,45)):
            raw = (probe/row['body']).read_bytes()
            if row['url'] != a.url(n) or row['status'] != 200 or hashlib.sha256(raw).hexdigest() != row['sha256']:
                raise ValueError('Arts current response mismatch')
            kw = dict(empty_after=44) if n==45 else {}
            if a.inspect(raw,n,**kw) != a.inspect(pages[n],n,**kw): raise ValueError('Arts current source changed')
    finally: c._session.close()
    root = Path(__file__).resolve().parents[2]
    return dict(status='passed', site_id=SITE, reviewed_at=utc_now(), **facts,
        actual_live_HTTP_requests=3, source_order='newest_first', current_boundary_metadata_matches=True,
        source_sha256=hashlib.sha256(Path(CRAWLERS[SITE].crawl.__code__.co_filename).read_bytes()).hexdigest(),
        dependency_source_sha256={name:hashlib.sha256((root/name).read_bytes()).hexdigest() for name in
            ('crawler/incremental_artsetmetiers.py','delivery/health/access_challenge.py',
             'docs/deliverables/test_URL_수집대조_20260802.csv')},
        baseline_complete=False, global_source_complete=False, all_files_verified=False, operating_database_touched=False)


if __name__ == '__main__':
    p=argparse.ArgumentParser(); p.add_argument('--source',required=True); p.add_argument('--probe',required=True); p.add_argument('--out',required=True)
    args=p.parse_args(); out=Path(args.out)
    if out.exists(): raise FileExistsError(out)
    result=review(args.source,args.probe); atomic_json(out,result)
    print({k:result[k] for k in ('status','native_eligible_records','source_order')})
