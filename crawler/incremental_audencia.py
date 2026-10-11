"""Audencia's original Drupal press catalogue; URL discovery, native node storage."""
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from .document_dates import normalize_date
from .incremental import Entry, Page, Store, UnsupportedSource, encoded
from .incremental_adapters import Adapter


class AudenciaAdapter(Adapter):
    rows = 12
    view_selector = '.view-id-recherche_avancee.view-display-id-press_releases'

    def __init__(self, crawler, *, category=None):
        if category is not None and (not isinstance(category, str) or not category.isdecimal()):
            raise ValueError('invalid Audencia category')
        super().__init__(crawler, {'endpoint': crawler.START_URL, 'category': category, 'rows': self.rows})
        self.contract = hashlib.sha256(self.contract.encode() + Path(__file__).read_bytes()
            + Path(crawler.crawl.__code__.co_filename).read_bytes()
            + Path(__file__).with_name('document_dates.py').read_bytes()).hexdigest()

    def url(self, page):
        if self.scope['category'] is None:
            return self.crawler._list_url(page)
        return self.scope['endpoint'] + '?' + urlencode({'categories':self.scope['category'], 'page':page})

    def html(self, url, *, detail=False):
        self.http_requests += 1
        if detail: self.detail_requests += 1
        # BaseCrawler advertises br even when no Brotli decoder is installed.
        # This transport negotiates only encodings supported by this runtime.
        r = self.crawler._request(url, retries=1, headers={'Accept-Encoding':'gzip, deflate'})
        if r is None: raise ConnectionError('Audencia source response unavailable')
        soup = self.crawler._make_soup(r.text)
        if soup is None: raise ValueError('Audencia HTML unavailable')
        return r.text, soup

    def pager_number(self, href):
        p = urlsplit(urljoin(self.scope['endpoint'], href)); base = urlsplit(self.scope['endpoint'])
        q = parse_qs(p.query, keep_blank_values=True)
        if (p.scheme, p.netloc, p.path, p.fragment) != (base.scheme, base.netloc, base.path, ''):
            raise ValueError('Audencia pager route changed')
        if (set(q)-{'page','categories','titre'} or len(q.get('page',[])) != 1 or not q['page'][0].isdecimal()
                or q.get('categories',['All']) != [self.scope['category'] or 'All'] or q.get('titre',['']) != ['']):
            raise ValueError('Audencia pager filter changed')
        return int(q['page'][0])

    @staticmethod
    def date(record):
        d = normalize_date(record['listed_datetime'] or record['listed_date_raw'], order='DMY')
        if d['status'] != 'normalized' or d['precision'] != 'day':
            raise UnsupportedSource('audencia_precise_list_date_unavailable')
        return d['value']

    def read(self, page, *, allow_empty=False):
        _, soup = self.html(self.url(page)); views = soup.select(self.view_selector)
        if len(views) != 1: raise ValueError('Audencia original press view missing or ambiguous')
        view = views[0]; totals = view.select('.total-view-results')
        if len(totals) != 1 or not totals[0].get_text(strip=True).isdecimal():
            raise ValueError('Audencia source total missing')
        total = int(totals[0].get_text(strip=True)); last = max(0, (total-1)//self.rows)
        checked = view.select('input[name="categories"][checked]')
        if len(checked) != 1 or checked[0].get('value') != (self.scope['category'] or 'All'):
            raise ValueError('Audencia category filter not confirmed')
        items = self.crawler._parse_list(soup, self.url(page))
        if len(items) != len(view.select('.Card.-news')):
            raise UnsupportedSource('audencia_unparsed_or_duplicate_list_identities')
        numbers = [self.pager_number(a['href']) for a in view.select('.pager a[href]')]
        current = view.select('.pager a[aria-current="page"][href]')
        nexts = [self.pager_number(a['href']) for a in view.select('.pager__item--next a[href]')]
        empty = view.select_one('.view-empty')
        explicit_empty = empty is not None and empty.get_text(' ',strip=True) == 'Votre recherche ne retourne aucun résultat.'
        if not items:
            if not allow_empty or not explicit_empty or nexts or (total and page != last+1):
                raise ValueError('Audencia empty response is not verified exhaustion')
            if total and (len(current) != 1 or self.pager_number(current[0]['href']) != last):
                raise ValueError('Audencia adjacent empty pager mismatch')
        else:
            if (explicit_empty or page > last or len(items) != min(self.rows,total-page*self.rows)
                    or len(current) != 1 or self.pager_number(current[0]['href']) != page
                    or nexts != ([page+1] if page<last else []) or not numbers or max(numbers) != last):
                raise ValueError('Audencia total/page/next boundary mismatch')
        if any(n>last for n in numbers): raise ValueError('Audencia pager exceeds source total')
        for item in items:
            p = urlsplit(item['url'])
            if (p.scheme != 'https' or p.netloc != 'www.audencia.com' or not p.path.startswith('/actualites/')
                    or not item['slug'] or p.query or p.fragment):
                raise UnsupportedSource('audencia_list_identity_route_unverified')
        dates = [self.date(i) for i in items]
        if dates != sorted(dates, reverse=True): raise UnsupportedSource('audencia_newest_order_unverified')
        return items, total, last, hashlib.sha256(encoded(items).encode()).hexdigest()

    def fetch(self, cursor, direction):
        page = (cursor or {}).get('page',0)
        if cursor:
            _, total, _, anchor = self.read(page-1)
            if total != cursor['total'] or anchor != cursor['anchor']:
                raise ValueError('Audencia previous page changed; reconcile required')
        items,total,last,anchor = self.read(page, allow_empty=page==0)
        keys = [i['url'] for i in items]
        if cursor:
            if total != cursor['total'] or set(keys).intersection(cursor['seen']):
                raise ValueError('Audencia repeated source page or changed total')
            if items and self.date(items[0]) > cursor['last_date']:
                raise UnsupportedSource('audencia_cross_page_order_changed')
        exhausted = not items
        if items and page == last:
            following, end_total, _, _ = self.read(page+1, allow_empty=True)
            if following or end_total != total: raise ValueError('Audencia adjacent boundary contradicts final page')
            exhausted = True
        next_cursor = dict(page=page+1, total=total, anchor=anchor,
                           seen=(cursor or {}).get('seen',[])+keys, last_date=self.date(items[-1]) if items else None)
        return Page([Entry(i['url'],i,self.date(i)) for i in items],next_cursor,exhausted,
                    'validated total, final pager and adjacent explicit empty view' if exhausted else '')

    def stored(self, url, identity=None):
        c = self.crawler; db = Store(c._conn,c.site_id)
        modern = not db.sqlite or c._conn_is_libertree()
        url_col, id_col = ('meta_url','post_number') if modern else ('url','external_id')
        query = f'SELECT {url_col} AS source_url,{id_col} AS source_id FROM documents WHERE site_id=? AND ({url_col}=?'
        args = [c.site_id,url]
        if identity is not None: query += f' OR {id_col}=?'; args.append(identity)
        rows = db.execute(query+')',tuple(args)).fetchall()
        if len(rows)>1: raise UnsupportedSource('audencia_ambiguous_stored_identity')
        row = dict(rows[0]) if rows else None
        if row and (row['source_url']!=url or identity is not None and row['source_id']!=identity):
            raise UnsupportedSource('audencia_changed_stored_identity')
        return row

    def exists(self, entry):
        return self.stored(entry.key) is not None

    def materialize(self, record):
        raw,soup = self.html(record['url'],detail=True)
        if soup.select_one('article.node--type-actualite') is None:
            raise ValueError('Audencia actual news detail missing')
        parsed = self.crawler._parse_detail(soup,raw,record)
        if parsed['url'] != record['url'] or parsed['title'] != record['title']:
            raise ValueError('Audencia detail/list identity mismatch')
        if len(parsed['abstract']) < self.crawler.MIN_ABSTRACT_CHARS:
            raise ValueError('Audencia source body gap; pending retained')
        self.stored(record['url'],parsed['external_id'])
        return self.crawler._build_paper(parsed)
