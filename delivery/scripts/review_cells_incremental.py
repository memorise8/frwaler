"""Review the static CELLS catalogue without treating report years as dates."""
import argparse
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlsplit

from crawler.incremental import Entry, infer_order, observe_order
from crawler.sites import CRAWLERS
from delivery.health.results import atomic_json, utc_now

SITE = 'cells-es-en'
URL = 'https://www.cells.es/en/public/corporate-publications'
REASON = 'static_catalogue_reporting_years_not_publication_dates_and_native_anchor_ids_nonnumeric'


def parse(c, raw):
    soup = c._make_soup(raw)
    main = soup.find('main') if soup else None
    if main is None or '</html>' not in raw.decode('utf-8').lower():
        raise ValueError('CELLS complete original catalogue missing')
    if main.select('time,[datetime],[rel="next"],.pagination,.pager'):
        raise ValueError('CELLS new date/pager markup requires renewed review')
    items = []
    for h in main.select('h3'):
        links = h.parent.select('a[href*="@@download/file"]')
        title = h.get_text(' ', strip=True)
        year = re.fullmatch(r'Activity Report (\d{4})', title)
        if len(links) != 1 or not year or not h.get('id'):
            raise ValueError('CELLS report title and file association changed')
        href = links[0]['href']; p = urlsplit(href)
        if p.scheme or p.netloc or not p.path.startswith('/en/assets/pdfs/') or p.query or p.fragment:
            raise ValueError('CELLS file outside original scope')
        items.append(dict(anchor=h['id'], title_raw=title, reporting_year_raw=year[1],
            pdf_url=urljoin(URL, href), detail_url=URL+'#'+h['id'],
            original_filename=p.path.split('/@@download/')[0].rsplit('/', 1)[1]))
    if (not items or len(main.select('a[href*="@@download/file"]')) != len(items)
            or len({i['anchor'] for i in items}) != len(items) or len({i['pdf_url'] for i in items}) != len(items)):
        raise ValueError('CELLS incomplete/duplicate source membership')
    script_texts = [s.string or s.get_text() for s in soup.select('script')]
    scripts = [s for s in script_texts if s.startswith('window.__data=')]
    if len(scripts) != 1:
        raise ValueError('CELLS serialized original page missing')
    text = scripts[0][len('window.__data='):]
    # Parse data only. Never execute script or visit serialized internal hosts.
    text = re.sub(r'"(?:[^"\\]|\\.)*"|\bundefined\b', lambda m:'null' if m[0]=='undefined' else m[0], text)
    state, end = json.JSONDecoder().raw_decode(text)
    if text[end:] != ';': raise ValueError('CELLS trailing executable state')
    content = state['content']['data']
    if content['title'] != 'Corporate publications' or content['@type'] != 'Document':
        raise ValueError('CELLS unexpected embedded content type')
    blocks = []
    def walk(value):
        if isinstance(value, dict):
            if '@type' in value: blocks.append(value)
            for child in value.values(): walk(child)
        elif isinstance(value, list):
            for child in value: walk(child)
    walk(content['blocks'])
    if {v['@type'] for v in blocks} != {'columnsBlock', 'slate', 'image'}:
        raise ValueError('CELLS new block type requires catalogue review')
    titles = [v['plaintext'] for v in blocks if v.get('@type') == 'slate'
              and any(x.get('type') == 'h3' for x in v.get('value', []))]
    refs = [ref for v in blocks if v.get('@type') == 'image' for ref in v.get('href', [])]
    if (len(refs) != len(items) or sorted(titles) != sorted(i['title_raw'] for i in items)
            or sorted('/en/'+urlsplit(ref['@id']).path.split('/en/', 1)[1] for ref in refs)
                != sorted(urlsplit(i['pdf_url']).path for i in items)):
        raise ValueError('CELLS rendered and embedded references differ')
    if any(set(ref).intersection({'created','effective','modified','date','published','publication_date'}) for ref in refs):
        raise ValueError('CELLS per-report date metadata now available; renew review')
    proof = {}
    observe_order(proof, [Entry(i['anchor'], i, '') for i in items], False)
    return items, dict(proof=proof, inferred_order=infer_order(proof),
        container_dates={k:content.get(k) for k in ('created','effective','modified')},
        container_dates_are_not_per_report=True, embedded_block_types=sorted({v['@type'] for v in blocks}),
        embedded_reference_field_names=sorted(set().union(*(set(v) for v in refs))),
        embedded_items_total=content['items_total'], native_IDs_numeric=False)


def run(source, probe, out):
    source, probe, out = Path(source), Path(probe), Path(out)
    if out.exists(): raise FileExistsError(out)
    prior = json.loads((source/'cells-single-page-independent-review.json').read_text())
    retained = (source/prior['source_file']).read_bytes()
    assert hashlib.sha256(retained).hexdigest() == prior['source_sha256']
    manifest = source/prior['records_file']
    assert hashlib.sha256(manifest.read_bytes()).hexdigest() == prior['records_sha256']
    observed = json.loads((probe/'audit.json').read_text())['requests']
    assert len(observed) == 1 and observed[0]['url'] == URL and observed[0]['status'] == 200
    fresh = (probe/observed[0]['body']).read_bytes()
    assert hashlib.sha256(fresh).hexdigest() == observed[0]['sha256']
    c = CRAWLERS[SITE](None, delay=0)
    try:
        old, old_order = parse(c, retained); now, order = parse(c, fresh)
        assert old == now and len(now) == 12
        records = [json.loads(line) for line in manifest.read_text().splitlines()]
        assert [{k:r[k] for k in old[0]} for r in records] == old
        assert order['inferred_order'] == ('arbitrary', 'order_ambiguous')
        assert order['proof']['date']['missing'] == 12
        assert all(not i['anchor'].isdecimal() for i in now)
        result = dict(status='passed', reviewed_at=utc_now(), site_id=SITE,
            classification='full_rescan_required', reason=REASON,
            source_sha256=hashlib.sha256(Path(c.crawl.__code__.co_filename).read_bytes()).hexdigest(),
            prior_native_source_sha256=prior['native_source_sha256'],
            retained_source_sha256=prior['source_sha256'], fresh_source_sha256=observed[0]['sha256'],
            source_memberships=12, retained_and_current_all_native_listing_fields_equal=True,
            actual_reporting_years=[i['reporting_year_raw'] for i in now], order_evidence=order,
            rendered_and_embedded_memberships_equal=True, actual_live_HTTP_requests=1,
            synthetic_publication_dates_removed=True, reporting_year_preserved_as_metadata=True,
            per_report_publication_dates_unavailable=True, per_report_PDF_bytes_not_verified=True,
            global_source_complete=False, all_files_verified=False, operating_database_touched=False,
            source_documents_deleted=False,
            note='Report year is a coverage period, not a publication timestamp or monotonic storage ID. Container dates and embedded items_total=0 do not establish report chronology or an empty catalogue. Native dates are now unknown rather than invented January 1.')
        atomic_json(out, result)
        print(json.dumps({k:result[k] for k in ('status','source_memberships','reason')}))
    finally: c._session.close()


if __name__ == '__main__':
    p=argparse.ArgumentParser()
    for name in ('source','probe','out'):p.add_argument('--'+name,required=True)
    a=p.parse_args();run(a.source,a.probe,a.out)
