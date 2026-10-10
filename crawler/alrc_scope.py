"""The two ALRC URLs in the delivered scope; counts refer to listing rows."""
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

SCOPES = ('final-report', 'delivered-search')


def list_url(scope, page):
    if scope not in SCOPES or type(page) is not int or page < 1:
        raise ValueError('invalid ALRC scope or page')
    base = 'https://www.alrc.gov.au/'
    if scope == 'final-report':
        return base + 'publications/final-report/' + (f'page/{page}/' if page > 1 else '')
    return base + (f'page/{page}/' if page > 1 else '') + '?s=pdf&type=all'


def same_url(actual, expected):
    a, b = urlsplit(actual), urlsplit(expected)
    return ((a.scheme, a.netloc, a.path, parse_qs(a.query, keep_blank_values=True))
            == (b.scheme, b.netloc, b.path, parse_qs(b.query, keep_blank_values=True))
            and not a.fragment)


def page_identity(items):
    return hashlib.sha256(json.dumps(
        [(v['url'], v['title'], v.get('listed_date_raw')) for v in items],
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def scope_contract():
    return hashlib.sha256(json.dumps([list_url(s, 1) for s in SCOPES],
                                    separators=(',', ':')).encode()).hexdigest()


def inspect_listing(soup, items, scope, page):
    """Validate publisher pagination before using a row offset or exhaustion."""
    if soup is None or not items:
        raise ValueError('ALRC listing missing or empty without source proof')
    pager = soup.select_one('.tw-pagination')
    current = pager.select_one('.current') if pager else None
    if current is None or current.get_text(strip=True) != str(page):
        raise ValueError('ALRC current page missing or changed')
    numbers = [int(v.get_text(strip=True)) for v in pager.select('.page')
               if v.get_text(strip=True).isdigit()]
    if not numbers or min(numbers) < 1:
        raise ValueError('ALRC page boundary missing')
    last = max(numbers)
    for link in pager.select('a.page[href]'):
        n = link.get_text(strip=True)
        if not n.isdigit() or not same_url(link['href'], list_url(scope, int(n))):
            raise ValueError('ALRC pager changed scope')
    next_link = pager.select_one('a.next[href]')
    if bool(next_link) != (page < last):
        raise ValueError('ALRC next page contradicts boundary')
    if next_link and not same_url(next_link['href'], list_url(scope, page + 1)):
        raise ValueError('ALRC next page skipped or repeated')
    if len(items) != len(soup.select('.section-listing-item')):
        raise ValueError('ALRC source row was silently dropped')
    return last
