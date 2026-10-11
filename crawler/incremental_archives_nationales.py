"""Exact delivered French press filter; native versioned-file metadata contract.

Card descriptions remain the native truncated summaries. Short descriptions
are explicit exclusions, not proof that their PDF bodies have been collected.
"""
import copy
from datetime import date
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from .incremental import Entry, Page, Store, UnsupportedSource, encoded
from .incremental_adapters import Adapter


class ArchivesNationalesAdapter(Adapter):
    rows = 8
    query = 'title=&field_type_publication_target_id_1%5B32%5D=32&created%5Bvalue%5D='

    def __init__(self, crawler):
        super().__init__(crawler, {'endpoint': crawler.base_url+'/presse', 'query': self.query,
                                 'rows': self.rows, 'identity': 'native_S3_VersionId'})
        self.contract = hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
            +Path(crawler.crawl.__code__.co_filename).read_bytes()
            +Path(__file__).with_name('document_dates.py').read_bytes()).hexdigest()
        state = Store(crawler._conn, crawler.site_id).state() if crawler._conn is not None else {}
        same = state.get('contract') == self.contract
        self.prior_exclusions = ((state.get('cursor') or {}).get('exclusions', {})
            if same and state.get('phase') == 'idle' and state.get('baseline_complete') else {})

    def url(self, page):
        return self.scope['endpoint']+'?'+self.query+('&page='+str(page) if page else '')

    def number(self, href):
        p, base = urlsplit(urljoin(self.scope['endpoint'], href)), urlsplit(self.scope['endpoint'])
        q = parse_qs(p.query, keep_blank_values=True)
        number = q.pop('page', None)
        if ((p.scheme, p.netloc, p.path, p.fragment) != (base.scheme, base.netloc, base.path, '')
                or q != parse_qs(self.query, keep_blank_values=True)
                or not number or len(number) != 1 or not number[0].isdecimal()):
            raise ValueError('Archives press pager filter changed')
        return int(number[0])

    def read(self, page, *, empty_after=None):
        c = self.crawler
        self.http_requests += 1
        r = c._request(self.url(page), retries=1, headers={'Accept-Encoding': 'gzip, deflate'})
        if r is None:
            raise ConnectionError('Archives press response unavailable')
        raw = r.text
        soup = c._make_soup(raw)
        if soup is None or '</html>' not in raw.lower():
            raise ValueError('Archives incomplete HTML')
        title = soup.title.get_text(' ', strip=True) if soup.title else ''
        match = re.fullmatch(r'Presse - Page (\d+) sur (\d+) - Archives nationales', title)
        views = soup.select('#block-archives-nationales-content')
        if len(views) != 1 or (not match and not (title == 'Presse'
                and any(h.get_text(strip=True) == 'Presse' for h in soup.select('h1')))):
            raise ValueError('Archives original press view missing; challenge is not exhaustion')
        view = views[0]
        types = [(x.get('name'), x.get('value')) for x in view.select('input[checked]')
                 if (x.get('name') or '').startswith('field_type_publication_target_id_1')]
        years = view.select('select[name="created[value]"] option[selected]')
        if types != [('field_type_publication_target_id_1[32]', '32')] or len(years) != 1 or years[0].get('value') != '':
            raise ValueError('Archives delivered filter echo changed')
        selected = view.select('.fr-pagination [aria-current="page"]')
        next_links = view.select('.fr-pagination__link--next[href]')
        numbers = [self.number(a['href']) for a in view.select('.fr-pagination a[href]')]
        if len(selected) != 1 or not selected[0].get_text(strip=True).isdecimal():
            raise ValueError('Archives current page missing')
        current = int(selected[0].get_text(strip=True))
        last_links = view.select('.fr-pagination__link--last[href]')
        if len(last_links) > 1:
            raise ValueError('Archives ambiguous final page')
        total = self.number(last_links[0]['href'])+1 if last_links else max(numbers+[current-1])+1
        if (current < 1 or total < 1 or any(n >= total for n in numbers) or len(next_links) > 1
                or match and tuple(map(int, match.groups())) != (current, total)):
            raise ValueError('Archives page identity mismatch')
        has_next = bool(next_links)
        if has_next and self.number(next_links[0]['href']) != page+1:
            raise ValueError('Archives nonadjacent next page')
        cards = view.select('article.node--type-detail-publication')
        if not cards:
            if empty_after is None or page != empty_after+1 or current != total or total != page or has_next:
                raise ValueError('Archives empty page lacks adjacent end evidence')
        elif (current != page+1 or not 1 <= len(cards) <= self.rows or has_next != (current < total)
              or has_next and len(cards) != self.rows):
            raise ValueError('Archives row count/current/next mismatch')
        items = []
        for card in cards:
            titles, links = card.select('span.field--name-title'), card.select('a.fr-card__link[href]')
            dates = card.select('p.fr-card__detail')
            if len(titles) != 1 or len(links) != 1 or len(dates) != 1:
                raise ValueError('Archives unrecognized source card')
            title, url = titles[0].get_text(strip=True), links[0]['href'].strip()
            p = urlsplit(url); q = parse_qs(p.query)
            key = c._version_id(url)
            if (not title or p.scheme != 'https' or p.netloc != 's3.fr-par.scw.cloud'
                    or not p.path.startswith('/an-p0-bucket/an-public/files/') or not p.path.lower().endswith('.pdf')
                    or p.fragment or not key or not key.isdecimal() or q != {'VersionId': [key]}):
                raise UnsupportedSource('archives_native_versioned_file_identity_unverified')
            raw_date = dates[0].get_text(strip=True)
            posted = c._parse_french_date(raw_date)
            try:
                date.fromisoformat(posted)
            except ValueError as exc:
                raise UnsupportedSource('archives_precise_listing_date_unavailable') from exc
            desc = card.select_one('p.fr-card__desc')
            abstract = desc.get_text(strip=True) if desc else ''
            filename = c._url_filename(url)
            # These are the legacy crawler's fields, independently compared
            # with its retained-HTML output before accepting a rollout batch.
            paper = dict(site_id=c.site_id, external_id=key, post_number=key, title=title,
                abstract=abstract, published_date=posted, posted_date=posted, authors='',
                publisher='Archives nationales', journal='', url=url, pdf_url=url, doi='', keywords='',
                category='Communiqué de presse', original_filename=filename,
                metadata=json.dumps(dict(posted_date=posted, originalFilename=filename, version_id=key,
                                         raw_date=raw_date, page_number=page), ensure_ascii=False))
            items.append({'key': key, 'date': posted, 'paper': paper, 'eligible': len(abstract) >= 50})
        if len({i['key'] for i in items}) != len(items) or len({i['paper']['url'] for i in items}) != len(items):
            raise UnsupportedSource('archives_duplicate_source_identity')
        return items, has_next, hashlib.sha256(encoded(items).encode()).hexdigest()

    def fetch(self, cursor, direction):
        incremental = direction == 'newest'
        state = copy.deepcopy(cursor) if cursor else {'page': 0, 'seen': [], 'date_up': False,
            'exclusions': copy.deepcopy(self.prior_exclusions) if incremental else {}}
        page = state['page']
        if cursor and self.read(page-1)[2] != state['anchor']:
            raise ValueError('Archives previous source page changed; pending retained')
        items, has_next, anchor = self.read(page)
        keys = [i['key'] for i in items]
        if set(keys).intersection(state['seen']):
            raise ValueError('Archives cross-page duplicate membership')
        entries = []
        for item in items:
            key = item['key']
            if not item['eligible']:
                if self.exists(Entry(key, item)):
                    raise ValueError('Archives missing summary cannot exclude an existing document')
                state['exclusions'][key] = {'reason': 'native_short_card_description',
                    'page': page, 'url': item['paper']['url'], 'date': item['date'],
                    'characters': len(item['paper']['abstract']),
                    'fingerprint': hashlib.sha256(encoded(item).encode()).hexdigest()}
                continue
            state['exclusions'].pop(key, None)
            entries.append(Entry(key, item, item['date']))
        dates = [i['date'] for i in items]
        state['date_up'] |= any(b > a for a, b in zip(dates, dates[1:]))
        if cursor and dates and dates[0] > state['last_date']:
            state['date_up'] = True
        self.can_read_newest = not state['date_up']
        if incremental and state['date_up']:
            raise UnsupportedSource('archives_listing_order_changed_including_exclusions')
        exhausted = not has_next
        if exhausted:
            self.read(page+1, empty_after=page)
        state.update(page=page+1, anchor=anchor, seen=state['seen']+keys,
                     last_date=dates[-1] if dates else state.get('last_date'))
        return Page(entries, state, exhausted,
                    'native final pager and adjacent empty filtered view; exclusions recorded' if exhausted else
                    'native short-description exclusions only' if not entries else '',
                    checkpoint_only=not entries and not exhausted)

    def exists(self, entry):
        if self.crawler._conn is None:
            return False
        db = Store(self.crawler._conn, self.crawler.site_id)
        modern = not db.sqlite or self.crawler._conn_is_libertree()
        id_col, url_col = ('post_number', 'meta_url') if modern else ('external_id', 'url')
        rows = db.execute(f'SELECT {url_col} AS source_url FROM documents WHERE site_id=? AND {id_col}=?',
                          (self.crawler.site_id, entry.key)).fetchall()
        if len(rows) > 1 or rows and rows[0]['source_url'] != entry.payload['paper']['url']:
            raise UnsupportedSource('archives_version_id_storage_collision')
        return bool(rows)

    def materialize(self, item):
        self.exists(Entry(item['key'], item))
        if not item['eligible']:
            raise ValueError('Archives ineligible queued item')
        return item['paper']
