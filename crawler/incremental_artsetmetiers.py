"""Original press view: dated PDF references, without PDF/detail requests."""
from datetime import date
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from .incremental import Entry, Page, Store, UnsupportedSource, encoded
from .incremental_adapters import Adapter
from delivery.health.access_challenge import access_challenge


class ArtsetmetiersAdapter(Adapter):
    def __init__(self, crawler):
        super().__init__(crawler, dict(endpoint=crawler._LIST_URL, scope='original_press_PDF_references'))
        self.contract = hashlib.sha256(self.contract.encode() + Path(__file__).read_bytes()
            + Path(crawler.crawl.__code__.co_filename).read_bytes()).hexdigest()

    def url(self, page):
        return self.scope['endpoint'] + ('?page=' + str(page) if page else '')

    def page_number(self, href):
        p = urlsplit(urljoin(self.scope['endpoint'], href)); base = urlsplit(self.scope['endpoint'])
        q = parse_qs(p.query, keep_blank_values=True)
        if ((p.scheme, p.netloc, p.path, p.fragment) != (base.scheme, base.netloc, base.path, '')
                or set(q) != {'page'} or len(q['page']) != 1 or not q['page'][0].isdecimal()):
            raise ValueError('Arts pager changed scope')
        return int(q['page'][0])

    def inspect(self, raw, page, *, empty_after=None):
        if isinstance(raw, bytes): raw = raw.decode('utf-8')
        if access_challenge(raw) or '</html>' not in raw.lower():
            raise ValueError('Arts incomplete or access response')
        soup = self.crawler.crawl.__globals__['_make_soup'](raw)
        if soup is None or [h.get_text(' ', strip=True) for h in soup.select('h1')] != ['Espace Presse']:
            raise ValueError('Arts catalogue shape changed')
        canonical = soup.select('link[rel="canonical"]')
        views = soup.select('.view-id-espace_presse.view-display-id-page_1')
        if len(canonical) != 1 or canonical[0].get('href') != self.scope['endpoint'] or len(views) != 1:
            raise ValueError('Arts original press view missing')
        view = views[0]; forms = view.select('form#views-exposed-form-espace-presse-page-1')
        if len(forms) != 1 or forms[0].get('action') != '/fr/communiques-de-presse':
            raise ValueError('Arts press form missing')
        for name in ('combine', 'created', 'created_1', 'date_from', 'date_to'):
            fields = forms[0].find_all('input', attrs={'name': name})
            if len(fields) != 1 or fields[0].get('value') != '': raise ValueError('Arts filter changed')
        current = view.select('.pager .is-active a[href]'); nxt = view.select('.pager__item--next a[href]')
        last = view.select('.pager__item--last a[href]'); links = view.select('.pager a[href]')
        if len(current) != 1 or len(nxt) > 1 or len(last) > 1 or not links:
            raise ValueError('Arts pagination missing')
        numbers = [self.page_number(a['href']) for a in links]; final = max(numbers)
        cp = self.page_number(current[0]['href']); cards = view.select('.view-content .views-row')
        if not cards:
            if (empty_after is None or page != empty_after+1 or cp != empty_after or final != empty_after
                    or nxt or last or view.select('.view-content')):
                raise ValueError('Arts unverified adjacent source end')
            return [], False, final
        if (cp != page or not 1 <= len(cards) <= 5 or bool(nxt) != (page < final)
                or nxt and (self.page_number(nxt[0]['href']) != page+1 or len(cards) != 5)
                or last and self.page_number(last[0]['href']) != final):
            raise ValueError('Arts page/next/cardinality mismatch')
        # Scope the native parser to the press view: footer partners are not documents.
        items = self.crawler._parse_list_page(str(view))
        if len(items) != len(cards): raise ValueError('Arts native parser skipped a press row')
        for item in items:
            u = urlsplit(item['pdf_url'])
            if (u.scheme != 'https' or u.netloc != 's3-site.artsetmetiers.fr' or not u.path.startswith('/public/')
                    or not u.path.lower().endswith('.pdf') or u.query or u.fragment):
                raise UnsupportedSource('arts_unstable_PDF_identity')
            if not item['title']: raise ValueError('Arts press title missing')
            try: date.fromisoformat(item['date'])
            except (TypeError, ValueError): raise UnsupportedSource('arts_precise_date_missing')
        if len({i['pdf_url'] for i in items}) != len(items): raise UnsupportedSource('arts_repeated_identity')
        return items, bool(nxt), final

    def read(self, page, *, empty_after=None):
        self.http_requests += 1
        r = self.crawler._session.get(self.url(page), timeout=35, allow_redirects=False,
                                     headers={'Accept-Encoding': 'gzip, deflate'})
        r.raise_for_status()
        if r.status_code != 200: raise ValueError('Arts redirected response')
        return self.inspect(r.text, page, empty_after=empty_after)

    @staticmethod
    def anchor(items):
        return hashlib.sha256(encoded(sorted(items, key=lambda i:i['pdf_url'])).encode()).hexdigest()

    def fetch(self, cursor, direction):
        page = cursor['page'] if cursor else 0
        if cursor:
            prior, _, final = self.read(page-1)
            if final != cursor['final'] or self.anchor(prior) != cursor['anchor']:
                raise ValueError('Arts prior page drift; pending retained')
        items, nxt, final = self.read(page); keys = [i['pdf_url'] for i in items]
        if cursor and final != cursor['final']: raise ValueError('Arts final page drift')
        if cursor and set(keys).intersection(cursor['seen']): raise UnsupportedSource('arts_cross_page_identity_repeat')
        dates = [i['date'] for i in items]
        up = bool((cursor or {}).get('date_up')) or any(a < b for a,b in zip(dates, dates[1:]))
        if cursor and dates[0] > cursor['last_date']: up = True
        self.can_read_newest = not up
        if direction == 'newest' and up: raise UnsupportedSource('arts_newest_order_changed')
        if not nxt: self.read(page+1, empty_after=page)
        eligible = [i for i in items if len(i['abstract']) >= 100]
        state = dict(page=page+1, final=final, seen=(cursor or {}).get('seen', [])+keys,
                     anchor=self.anchor(items), last_date=dates[-1], date_up=up,
                     excluded=(cursor or {}).get('excluded', 0)+len(items)-len(eligible))
        structural = not eligible and nxt
        return Page([Entry(i['pdf_url'], i, i['date']) for i in eligible], state, not nxt,
                    'final press pager and adjacent empty same-scope view' if not nxt else
                    'native short-summary exclusions; source page advances' if structural else '',
                    checkpoint_only=structural)

    def exists(self, entry):
        c = self.crawler
        if c._conn is None: return False
        db = Store(c._conn, c.site_id); modern = not db.sqlite or c._conn_is_libertree()
        idcol, urlcol = ('post_number', 'meta_url') if modern else ('external_id', 'url')
        rows = db.execute(f'SELECT {idcol} AS identity FROM documents WHERE site_id=? AND {urlcol}=?',
                          (c.site_id, entry.key)).fetchall()
        if len(rows) > 1 or rows and rows[0]['identity'] != c._paper(entry.payload)['external_id']:
            raise UnsupportedSource('arts_native_identity_changed')
        return bool(rows)

    def materialize(self, item):
        self.exists(Entry(item['pdf_url'], item))
        return self.crawler._paper(item)
