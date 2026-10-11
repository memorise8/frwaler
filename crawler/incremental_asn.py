"""Both delivered ASN report catalogues, with independent merge boundaries."""
import copy
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

from .incremental import Entry, Page, Store, UnsupportedSource, encoded
from .incremental_adapters import Adapter

BASE = 'https://reglementation-controle.asnr.fr'
SCOPES = {'expertise': {'path': '/information/publications/rapports-d-expertise', 'title': "Rapports d'expertise"},
          'annual': {'path': '/information/publications/rapports-de-l-asnr', 'title': "Rapports de l'ASNR"}}
NOT_FOUND = "La page que vous tentez d'afficher n'existe pas ou a été déplacée."


class ASNAdapter(Adapter):
    rows = 15

    def __init__(self, crawler):
        super().__init__(crawler, {'scopes': SCOPES, 'rows': self.rows, 'identity': 'source_URL_with_native_stored_ID'})
        self.native = crawler.crawl.__globals__
        self.contract = hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
            +Path(crawler.crawl.__code__.co_filename).read_bytes()
            +Path(__file__).with_name('document_dates.py').read_bytes()).hexdigest()
        state = Store(crawler._conn, crawler.site_id).state() if crawler._conn is not None else {}
        same = state.get('contract') == self.contract
        self.prior_exclusions = ((state.get('cursor') or {}).get('exclusions', {})
            if same and state.get('phase') == 'idle' and state.get('baseline_complete') else {})
        self.resume_exclusions = ((state.get('cursor') or {}).get('exclusions', {})
            if same and state.get('phase') == 'deliver' else {})

    def response(self, url, *, detail=False, allow_404=False):
        p = urlsplit(url)
        if p.scheme != 'https' or p.netloc != urlsplit(BASE).netloc:
            raise UnsupportedSource('asn_resource_outside_original_publisher')
        self.http_requests += 1
        self.detail_requests += int(detail)
        try:
            r = self.crawler._session.get(url, timeout=40, allow_redirects=False,
                                           headers={'Accept-Encoding': 'gzip, deflate'})
        except requests.HTTPError as exc:
            if exc.response is None or not (allow_404 and exc.response.status_code == 404):
                raise
            r = exc.response
        if r.is_redirect:
            raise ValueError('ASN source redirect requires renewed scope review')
        if r.status_code != 200 and not (allow_404 and r.status_code == 404):
            r.raise_for_status()
            raise ValueError('ASN unexpected source status')
        return r

    def url(self, scope, page):
        return BASE+SCOPES[scope]['path']+('?page='+str(page) if page > 1 else '')

    def number(self, href, scope):
        p = urlsplit(urljoin(BASE, href)); q = parse_qs(p.query)
        if (p.scheme != 'https' or p.netloc != urlsplit(BASE).netloc or p.path != SCOPES[scope]['path']
                or p.fragment or set(q) != {'page'} or len(q['page']) != 1 or not q['page'][0].isdecimal()):
            raise ValueError('ASN pager escaped original scope')
        return int(q['page'][0])

    def read(self, scope, index):
        page = index+1
        r = self.response(self.url(scope, page))
        c = self.crawler; soup = c._make_soup(r.text)
        if (soup is None or '</html>' not in r.text.lower()
                or [c._clean_text(h.get_text(' ', strip=True)) for h in soup.select('h1')] != [SCOPES[scope]['title']]):
            raise ValueError('ASN original catalogue shape missing')
        views = soup.select('.ListPage .Wrapper-gridMain')
        if len(views) != 1:
            raise ValueError('ASN catalogue view ambiguous')
        view = views[0]
        counts = [int(m.group(1)) for h in view.select('h3')
                  if (m := re.fullmatch(r'(\d+) publications?', h.get_text(' ', strip=True)))]
        if len(counts) != 1 or counts[0] <= 0:
            raise ValueError('ASN catalogue total missing')
        total = counts[0]; last = (total+self.rows-1)//self.rows
        pagers = view.select('ul.pagination')
        if last > 1 and not pagers:
            raise ValueError('ASN multi-page catalogue has no pager')
        for pager in pagers:
            selected = pager.select('li.active')
            if len(selected) != 1 or selected[0].get_text(' ', strip=True).split()[0] != str(page):
                raise ValueError('ASN requested page not current')
            numbers = [self.number(a['href'], scope) for a in pager.select('a[href]')]
            nexts = pager.select('li.next a[href]')
            if (any(n < 1 or n > last for n in numbers) or len(nexts) != int(page < last)
                    or nexts and self.number(nexts[0]['href'], scope) != page+1):
                raise ValueError('ASN current/next/final pager mismatch')
        if not 1 <= page <= last:
            raise ValueError('ASN page beyond reported total')
        cards = view.select('article.Teaser')
        if len(cards) != min(self.rows, total-index*self.rows):
            raise ValueError('ASN source total/row count mismatch')
        items = []
        for card in cards:
            links, dates = card.select('.Teaser-titleLink[href]'), card.select('.Teaser-date')
            if len(links) != 1 or len(dates) != 1:
                raise ValueError('ASN skipped source card')
            link = links[0]; url = urljoin(BASE, link['href']); p = urlsplit(url)
            if (p.scheme != 'https' or p.netloc != urlsplit(BASE).netloc or p.query or p.fragment
                    or not p.path.startswith(SCOPES[scope]['path']+'/')):
                raise UnsupportedSource('asn_listing_identity_outside_catalogue')
            raw_date = dates[0].get_text(' ', strip=True); posted = self.native['_parse_date_fr'](raw_date)
            try: date.fromisoformat(posted)
            except ValueError as exc: raise UnsupportedSource('asn_precise_card_date_unavailable') from exc
            teaser = card.select_one('.Teaser-text .eztext-field')
            item = dict(url=url, title=c._clean_text(link.get_text(' ', strip=True)), listed_date=posted,
                        raw_date=raw_date, teaser_text=c._clean_text(teaser.get_text(' ', strip=True)) if teaser else '',
                        slug=p.path.rstrip('/').rsplit('/', 1)[-1], _source_scope=scope)
            if not item['title']: raise ValueError('ASN empty source title')
            items.append(item)
        if len({i['url'] for i in items}) != len(items):
            raise UnsupportedSource('asn_duplicate_source_identity')
        if page == last:
            end = self.response(self.url(scope, page+1), allow_404=True)
            esoup = c._make_soup(end.text)
            if (end.status_code != 404 or esoup is None or '</html>' not in end.text.lower()
                    or esoup.select('article.Teaser') or esoup.select('.ListPage')
                    or [h.get_text(' ', strip=True) for h in esoup.select('h1')] != [NOT_FOUND]):
                raise ValueError('ASN adjacent boundary is not the original empty 404 page')
        return dict(items=items, total=total, last_page=last, next_url=None if page == last else self.url(scope, page+1),
                    page_hash=hashlib.sha256(encoded([items, total, last]).encode()).hexdigest())

    def stored(self, url, identity=None):
        c = self.crawler
        if c._conn is None: return None
        db = Store(c._conn, c.site_id); modern = not db.sqlite or c._conn_is_libertree()
        id_col, url_col = ('post_number', 'meta_url') if modern else ('external_id', 'url')
        query = f'SELECT {id_col} AS source_id,{url_col} AS source_url FROM documents WHERE site_id=? AND ({url_col}=?'
        args = [c.site_id, url]
        if identity is not None: query += f' OR {id_col}=?'; args.append(identity)
        rows = db.execute(query+')', tuple(args)).fetchall()
        if len(rows) > 1 or rows and (rows[0]['source_url'] != url or identity is not None and rows[0]['source_id'] != identity):
            raise UnsupportedSource('asn_native_content_id_collision_or_change')
        return dict(rows[0]) if rows else None

    def exists(self, entry):
        return self.stored(entry.key) is not None

    def prepare(self, item):
        c = self.crawler
        raw = self.response(item['url'], detail=True).text
        soup = c._make_soup(raw)
        if soup is None or '</html>' not in raw.lower() or soup.select_one('.ArticleHeader h1') is None:
            raise ValueError('ASN original detail shape missing')
        if [link.get('href') for link in soup.select('link[rel="canonical"]')] != [item['url']]:
            raise UnsupportedSource('asn_detail_canonical_identity_changed')
        native = c._curl_get
        def retained(url, **kwargs):
            if url != item['url']: raise ValueError('ASN materializer requested another document')
            return raw
        c._curl_get = retained
        try: paper = c._materialize_item(item)
        finally: c._curl_get = native
        if paper is None:
            if self.stored(item['url']) is not None:
                raise ValueError('ASN native short body cannot exclude an existing document')
            return None, hashlib.sha256(raw.encode()).hexdigest()
        if paper['url'] != item['url'] or not paper['external_id']:
            raise UnsupportedSource('asn_native_detail_identity_unavailable')
        self.stored(item['url'], paper['external_id'])
        # The legacy implementation covers annual reports only. The second
        # delivered catalogue needs its own category while retaining its parser.
        paper['category'] = SCOPES[item['_source_scope']]['title']
        meta = json.loads(paper['metadata']); meta['category'] = paper['category']
        paper['metadata'] = json.dumps(meta, ensure_ascii=False)
        return paper, None

    def fetch(self, cursor, direction):
        incremental = direction == 'newest'
        state = copy.deepcopy(cursor) if cursor else {
            'streams': [{'scope': s, 'page': 0, 'buffer': [], 'done': False, 'last_page': None,
                         'anchor': None, 'seen': [], 'last_date': None, 'known_pages': 0,
                         'unknown_ids': [], 'date_up': False} for s in SCOPES],
            'exclusions': copy.deepcopy(self.prior_exclusions) if incremental else {}, 'native_ids': {}}
        if [s['scope'] for s in state['streams']] != list(SCOPES):
            raise ValueError('ASN cursor scope changed')
        entries = []
        while len(entries) < self.rows:
            for stream in state['streams']:
                if stream['done'] or stream['buffer'] and (not incremental or stream['page'] >= 2): continue
                scope, page = stream['scope'], stream['page']
                if stream['anchor'] is not None:
                    previous = self.read(scope, page-1)
                    if previous['page_hash'] != stream['anchor'] or previous['last_page'] != stream['last_page']:
                        raise ValueError('ASN previous page changed; pending retained')
                data = self.read(scope, page); items = data['items']; keys = [i['url'] for i in items]
                if stream['last_page'] is not None and data['last_page'] != stream['last_page']:
                    raise ValueError('ASN catalogue boundary changed')
                if set(keys).intersection(stream['seen']): raise ValueError('ASN cross-page duplicate membership')
                dates = [i['listed_date'] for i in items]
                stream['date_up'] |= any(b > a for a, b in zip(dates, dates[1:]))
                if stream['last_date'] is not None and dates[0] > stream['last_date']: stream['date_up'] = True
                if incremental and stream['date_up']: raise UnsupportedSource('asn_native_listing_order_changed')
                unknown = [i['url'] for i in items if incremental and
                    (not Store(self.crawler._conn, self.crawler.site_id).known(i['url']) or not self.exists(Entry(i['url'], i)))]
                stream['seen'].extend(keys); stream['unknown_ids'].extend(unknown)
                stream.update(buffer=stream['buffer']+items, page=page+1, done=data['next_url'] is None,
                              last_page=data['last_page'], anchor=data['page_hash'], last_date=dates[-1],
                              known_pages=stream['known_pages']+1 if incremental and not unknown else 0)
            candidates = [s for s in state['streams'] if s['buffer']]
            if not candidates: break
            stream = max(candidates, key=lambda s: (s['buffer'][0]['listed_date'], s['buffer'][0]['url']))
            item = stream['buffer'].pop(0); key = item['url']
            if key in stream['unknown_ids']: stream['unknown_ids'].remove(key)
            if not incremental or not Store(self.crawler._conn, self.crawler.site_id).known(key) or not self.exists(Entry(key, item)):
                paper, excluded = self.prepare(item)
                if excluded:
                    state['exclusions'][key] = {'item': item, 'fingerprint': excluded, 'reason': 'native_short_body'}
                    continue
                native_id = paper['external_id']
                if native_id in state['native_ids'] and state['native_ids'][native_id] != key:
                    raise UnsupportedSource('asn_native_content_id_shared_by_multiple_source_pages')
                state['native_ids'][native_id] = key
                state['exclusions'].pop(key, None); item['_prepared_paper'] = paper
            entries.append(Entry(key, item, item['listed_date']))
        self.can_read_newest = not any(s['date_up'] for s in state['streams'])
        exhausted = all(s['done'] and not s['buffer'] for s in state['streams'])
        safe = all((s['done'] or s['known_pages'] >= 2) and not s['unknown_ids'] for s in state['streams'])
        return Page(entries, state, exhausted, 'both original catalogues and adjacent 404 boundaries verified' if exhausted else '',
                    boundary_safe=safe if incremental else True)

    def materialize(self, item):
        for excluded in self.resume_exclusions.values():
            paper, fingerprint = self.prepare(excluded['item'])
            if paper is not None or fingerprint != excluded['fingerprint']:
                raise ValueError('ASN exclusion evidence changed; pending retained')
        self.resume_exclusions = {}
        if '_prepared_paper' not in item: raise ValueError('ASN uncached delivery item')
        paper = item['_prepared_paper']; self.stored(item['url'], paper['external_id'])
        return paper
