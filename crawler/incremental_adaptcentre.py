"""Native ADAPT WordPress scope, ordered by observed CMS date, not publication date."""
import hashlib
from pathlib import Path
from urllib.parse import urlsplit

from .incremental import Entry, Page, UnsupportedSource, encoded
from .incremental_adapters import Adapter

ENDPOINT = 'https://www.adaptcentre.ie/wp-json/wp/v2/publications'
FIELDS = 'id,slug,link,date,title,acf,journal_type'


class AdaptCentreAdapter(Adapter):
    rows = 100

    def __init__(self, crawler):
        super().__init__(crawler, dict(endpoint=ENDPOINT, rows=self.rows, fields=FIELDS))
        self.contract = hashlib.sha256(self.contract.encode()+Path(__file__).read_bytes()
            +Path(crawler.crawl.__code__.co_filename).read_bytes()).hexdigest()

    def request(self, url, *, detail=False):
        self.http_requests += 1
        self.detail_requests += int(detail)
        try:
            r = self.crawler._session.get(url, timeout=35, allow_redirects=False,
                                         headers={'Accept-Encoding':'gzip, deflate'})
        except Exception as exc:
            r = getattr(exc, 'response', None)
            if r is None or r.status_code != 400 or detail: raise
        if r.is_redirect: raise ValueError('ADAPT redirect requires scope review')
        return r

    def read(self, page, *, allow_end=False):
        r = self.request(f'{ENDPOINT}?per_page={self.rows}&page={page}&_fields={FIELDS}')
        data = r.json()
        if (allow_end and r.status_code == 400 and isinstance(data, dict)
                and data.get('code') == 'rest_post_invalid_page_number'
                and isinstance(data.get('data'), dict) and data['data'].get('status') == 400):
            return None
        if r.status_code != 200 or not isinstance(data, list):
            raise ValueError('ADAPT list error is not source exhaustion')
        headers = {k.lower():v for k,v in r.headers.items()}
        try: total, pages = int(headers['x-wp-total']), int(headers['x-wp-totalpages'])
        except (KeyError, ValueError, TypeError): raise ValueError('ADAPT verified total headers unavailable')
        if total < 0 or pages != (total+self.rows-1)//self.rows:
            raise ValueError('ADAPT inconsistent total/page count')
        if page > max(1,pages) or len(data) != min(self.rows,max(0,total-(page-1)*self.rows)):
            raise ValueError('ADAPT truncated or shifted page')
        for item in data:
            if (not isinstance(item,dict) or type(item.get('id')) is not int or item['id'] <= 0
                    or not isinstance(item.get('date'),str) or not item['date']
                    or not isinstance(item.get('link'),str)):
                raise UnsupportedSource('adaptcentre_stable_identity_or_CMS_date_missing')
            path = urlsplit(item['link'])
            if path.scheme != 'https' or path.netloc != 'www.adaptcentre.ie' or not path.path.startswith('/publications/'):
                raise UnsupportedSource('adaptcentre_native_publication_URL_changed_scope')
        if len({i['id'] for i in data}) != len(data) or len({i['link'] for i in data}) != len(data):
            raise UnsupportedSource('adaptcentre_repeated_native_identity')
        return data, total, pages

    def fetch(self, cursor, direction):
        page = cursor['page'] if cursor else 1
        if cursor:
            old, total, pages = self.read(page-1)
            if (hashlib.sha256(encoded(old).encode()).hexdigest() != cursor['anchor']
                    or total != cursor['total'] or pages != cursor['pages']):
                raise ValueError('ADAPT page drift; pending retained for reconciliation')
        items, total, pages = self.read(page)
        if cursor and (total != cursor['total'] or pages != cursor['pages']):
            raise ValueError('ADAPT source total changed during traversal')
        keys = [str(i['id']) for i in items]; urls = [i['link'] for i in items]
        if cursor and (set(keys).intersection(cursor['seen']) or set(urls).intersection(cursor['urls'])):
            raise UnsupportedSource('adaptcentre_repeated_cross_page_identity')
        exhausted = page == pages or total == 0
        if exhausted and self.read(page+1,allow_end=True) is not None:
            raise ValueError('ADAPT reported total contradicted by adjacent page')
        dates = [i['date'] for i in items]
        up = bool((cursor or {}).get('date_up')) or any(b>a for a,b in zip(dates,dates[1:]))
        if cursor and dates and dates[0] > cursor['last_date']: up = True
        self.can_read_newest = not up
        state = dict(page=page+1,anchor=hashlib.sha256(encoded(items).encode()).hexdigest(),
            total=total,pages=pages,seen=(cursor or {}).get('seen',[])+keys,
            urls=(cursor or {}).get('urls',[])+urls,last_date=dates[-1] if dates else '',date_up=up)
        return Page([Entry(str(i['id']),i,i['date']) for i in items],state,exhausted,
                    'WP total plus adjacent explicit invalid-page response' if exhausted else '')

    def materialize(self, item):
        c = self.crawler; native = c._curl_get
        def external(url, **kwargs):
            if urlsplit(url).netloc not in ('api.openalex.org','api.crossref.org'):
                raise UnsupportedSource('adaptcentre_unreviewed_abstract_provider')
            response = self.request(url,detail=True)
            response.raise_for_status()
            return response.text
        c._curl_get = external
        try: paper = c._process_item(item)
        finally: c._curl_get = native
        # Missing external abstracts are retryable, not permanent source exclusions.
        if paper is None: raise ValueError('ADAPT native materialization unavailable; pending retained')
        if paper['external_id'] != str(item['id']) or paper['url'] != item['link']:
            raise UnsupportedSource('adaptcentre_native_storage_identity_changed')
        return paper
