"""ARCOM's published reverse-date press filter, with strict page/count checks.

Retained ASC pagination omitted one URL at a same-date boundary. DESC resolved
all 310 distinct URLs. Any future duplicated membership fails closed.
"""
from datetime import date
import hashlib
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from .incremental import Entry, Page, Store, UnsupportedSource, encoded
from .incremental_adapters import Adapter
from delivery.health.access_challenge import access_challenge


class ARCOMAdapter(Adapter):
    def __init__(self, crawler):
        super().__init__(crawler, dict(endpoint=crawler._LIST_BASE.replace('_ASC', '_DESC'),
                                     scope='original_communique_de_presse_filter_16'))
        self.contract = hashlib.sha256(self.contract.encode() + Path(__file__).read_bytes()
            + Path(crawler.crawl.__code__.co_filename).read_bytes()
            + Path(__file__).with_name('document_dates.py').read_bytes()).hexdigest()

    def url(self, page):
        return self.scope['endpoint'] + ('&page=' + str(page) if page else '')

    def response(self, url, *, detail=False):
        self.http_requests += 1; self.detail_requests += int(detail)
        r = self.crawler._session.get(url, timeout=35, allow_redirects=False,
                                     headers={'Accept-Encoding': 'gzip, deflate'})
        r.raise_for_status()
        if r.status_code != 200 or access_challenge(r.text) or '</html>' not in r.text.lower():
            raise ValueError('ARCOM failed, incomplete or redirected source')
        return r.text

    def page_number(self, href):
        p = urlsplit(urljoin(self.scope['endpoint'], href)); base = urlsplit(self.scope['endpoint'])
        q = parse_qs(p.query, keep_blank_values=True); n = q.pop('page', ['0'])
        if ((p.scheme, p.netloc, p.path, p.fragment) != (base.scheme, base.netloc, base.path, '')
                or q != parse_qs(base.query, keep_blank_values=True) or len(n) != 1 or not n[0].isdecimal()):
            raise ValueError('ARCOM pager changed scope or sort')
        return int(n[0])

    def inspect(self, raw, page, *, empty_after=None):
        g = self.crawler.crawl.__globals__; soup = g['_make_soup'](raw)
        if soup is None or [h.get_text(' ', strip=True) for h in soup.select('h1')] != ['Espace presse']:
            raise ValueError('ARCOM catalogue shape changed')
        forms = soup.select('form#views-exposed-form-presse-block-1')
        if len(forms) != 1: raise ValueError('ARCOM missing filter form')
        form = forms[0]
        if [(i.get('name'), i.get('value')) for i in form.select('input[checked]')] != [('field_type_de_presse_target_id[16]', '16')]:
            raise ValueError('ARCOM press filter changed')
        for field in ('field_date_de_decision_value_1', 'field_date_de_decision_value'):
            node = form.find('input', attrs={'name': field})
            if node is None or node.get('value') != '': raise ValueError('ARCOM date filter changed')
        select = form.find('select', attrs={'name': 'sort_bef_combine'})
        if select is None or 'field_date_de_decision_value_DESC' not in [o.get('value') for o in select.select('option')]:
            raise UnsupportedSource('arcom_newest_order_unavailable')
        views = soup.select('.view-id-presse')
        if len(views) != 1: raise ValueError('ARCOM listing container missing')
        view = views[0]; text = view.get_text(' ', strip=True)
        totals = re.findall(r'(\d+) résultat\(s\) trouvé\(s\) sur le site\.', text)
        if len(totals) != 1: raise ValueError('ARCOM total missing')
        total = int(totals[0]); final = (total-1)//50
        current = view.select('ul.pagination li.active a[href]'); nxt = view.select('.pager__item--next a[href]')
        if total < 1 or len(current) != 1 or len(nxt) > 1: raise ValueError('ARCOM invalid pagination')
        for link in view.select('ul.pagination a[href]'): self.page_number(link['href'])
        current_page = self.page_number(current[0]['href']); cards = view.select('.views-row')
        if not cards:
            empty = view.select('.view-empty')
            if (empty_after != final or page != final+1 or current_page != final or nxt or len(empty) != 1
                    or empty[0].get_text(' ', strip=True) != 'Aucun résultat trouvé pour cette recherche, essayez une autre combinaison.'):
                raise ValueError('ARCOM missing adjacent explicit source end')
            return [], False, total
        if (current_page != page or len(cards) != min(50, total-page*50)
                or bool(nxt) != (page < final) or nxt and self.page_number(nxt[0]['href']) != page+1):
            raise ValueError('ARCOM page/cardinality/next mismatch')
        items = []
        for card in cards:
            links = card.select('.card-title a[href]'); dates = card.select('.card-infos .tag-list li')
            categories = card.select('.card-footer .tag-list li')
            if len(links) != 1 or len(dates) != 1 or 'Communiqué de presse' not in [x.get_text(' ', strip=True) for x in categories]:
                raise ValueError('ARCOM unexpected press card')
            href = links[0]['href']; url = urljoin(self.crawler.base_url, href); p = urlsplit(url)
            if p.scheme != 'https' or p.netloc != 'www.arcom.fr' or p.query or p.fragment or not re.fullmatch(r'/presse/[^/]+', p.path):
                raise UnsupportedSource('arcom_unstable_listing_identity')
            raw_date = dates[0].get_text(' ', strip=True); stamp = g['_parse_french_date'](raw_date)
            try: date.fromisoformat(stamp)
            except (TypeError, ValueError): raise UnsupportedSource('arcom_precise_listing_date_missing')
            items.append(dict(url=url, slug=p.path[len('/presse/'):], title=links[0].get_text(' ', strip=True),
                              date=stamp, date_raw=raw_date))
        if len({i['url'] for i in items}) != len(items): raise UnsupportedSource('arcom_repeated_source_identity')
        return items, bool(nxt), total

    def read(self, page, *, empty_after=None):
        return self.inspect(self.response(self.url(page)), page, empty_after=empty_after)

    @staticmethod
    def anchor(items):
        return hashlib.sha256(encoded(sorted(items, key=lambda x:x['url'])).encode()).hexdigest()

    def fetch(self, cursor, direction):
        page = cursor['page'] if cursor else 0
        if cursor:
            prior, _, prior_total = self.read(page-1)
            if prior_total != cursor['total'] or self.anchor(prior) != cursor['anchor']:
                raise ValueError('ARCOM prior page drift; pending retained')
        items, nxt, total = self.read(page); keys = [i['url'] for i in items]
        if cursor and total != cursor['total']: raise ValueError('ARCOM total changed during traversal')
        if cursor and set(keys).intersection(cursor['seen']): raise UnsupportedSource('arcom_cross_page_identity_repeat')
        dates = [i['date'] for i in items]; up = bool((cursor or {}).get('date_up')) or any(b>a for a,b in zip(dates,dates[1:]))
        if cursor and dates[0] > cursor['last_date']: up = True
        self.can_read_newest = not up
        if direction == 'newest' and up: raise UnsupportedSource('arcom_newest_order_changed')
        seen = (cursor or {}).get('seen', []) + keys
        if not nxt:
            empty, _, end_total = self.read(page+1, empty_after=page)
            if empty or end_total != total or len(seen) != total: raise ValueError('ARCOM end/count mismatch')
        state = dict(page=page+1, total=total, seen=seen, anchor=self.anchor(items), last_date=dates[-1], date_up=up)
        return Page([Entry(i['url'], i, i['date']) for i in items], state, not nxt,
                    'reverse-date press total, final pager and adjacent empty response' if not nxt else '')

    def exists(self, entry):
        c = self.crawler
        if c._conn is None: return False
        db = Store(c._conn, c.site_id); modern = not db.sqlite or c._conn_is_libertree()
        idcol, urlcol = ('post_number', 'meta_url') if modern else ('external_id', 'url')
        rows = db.execute(f'SELECT {idcol} AS identity FROM documents WHERE site_id=? AND {urlcol}=?', (c.site_id, entry.key)).fetchall()
        if len(rows)>1 or rows and rows[0]['identity'] != entry.payload['slug']:
            raise UnsupportedSource('arcom_native_identity_changed')
        return bool(rows)

    def materialize(self, item):
        self.exists(Entry(item['url'], item)); raw = self.response(item['url'], detail=True)
        soup = self.crawler.crawl.__globals__['_make_soup'](raw); canonical = soup.select('link[rel="canonical"]') if soup else []
        if len(canonical) != 1 or canonical[0].get('href') != item['url']:
            raise ValueError('ARCOM detail identity mismatch')
        paper = self.crawler._fetch_detail(item['slug'], item['url'], html=raw)
        if paper is None: raise UnsupportedSource('arcom_native_short_body_requires_full_rescan')
        if paper['published_date'] != item['date']: raise ValueError('ARCOM detail/list date mismatch; pending retained')
        return paper
