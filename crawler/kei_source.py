"""Reviewed KEI catalogue and board list contracts for all delivered routes.

Source totals describe listing memberships, not unique documents or body/file
coverage. Keep published links and raw dates for later detail reconciliation.
"""
import hashlib
import json
import re
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

BASE = 'https://www.kei.re.kr'
SCOPES = {
    'researchreport': ('/elibList.es', 'a10101010000', 'researchreport'),
    'environmentalforum': ('/elibList.es', 'a10102010000', 'environmentalforum'),
    'board-0028': ('/board.es', 'a10102020000', '0028'),
    'board-0031': ('/board.es', 'a10102050000', '0031'),
    'board-0032': ('/board.es', 'a10102060000', '0032'),
    'board-0033': ('/board.es', 'a10102070200', '0033'),
    'board-0054': ('/board.es', 'a10102100000', '0054'),
    'board-0057': ('/board.es', 'a10102120000', '0057'),
    'board-0075': ('/board.es', 'a10102130000', '0075'),
    'board-0081': ('/board.es', 'a10102180000', '0081'),
    'board-0009': ('/board.es', 'a10307020000', '0009'),
}
PAGE_SIZE = 10


def text(node):
    return re.sub(r'\s+', ' ', node.get_text(' ', strip=True)).strip() if node else ''


def scope_query(url, scope):
    path, mid, name = SCOPES[scope]
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != 'https' or parsed.netloc != 'www.kei.re.kr'
            or parsed.path != path or parsed.fragment or query.get('mid') != [mid]
            or any(len(values) != 1 for values in query.values())):
        raise ValueError('KEI source destination or scope mismatch')
    if path == '/board.es' and query.get('bid') != [name]:
        raise ValueError('KEI board mismatch')
    if path == '/elibList.es' and query.get('elibName', [name]) != [name]:
        raise ValueError('KEI catalogue mismatch')
    allowed = {'mid', 'bid', 'elibName', 'act', 'nPage', 'c_id', 'list_no', 'rn',
               'class_id', 'keyField', 'keyWord', 'tag'}
    if set(query) - allowed or any(query.get(k, ['']) != [''] for k in ('class_id', 'keyField', 'keyWord', 'tag')):
        raise ValueError('KEI unexpected listing filter')
    return query


def page_number(url, scope):
    q = scope_query(url, scope)
    if (q.get('act', ['list']) != ['list'] or 'c_id' in q or 'list_no' in q
            or not re.fullmatch('[1-9][0-9]*', q.get('nPage', ['1'])[0])):
        raise ValueError('KEI invalid list page destination')
    return int(q.get('nPage', ['1'])[0])


def listing_destination(url, scope):
    """Keep the catalogue selected by the validated source search form.

    KEI's forum pager omits elibName even though the next request defaults to
    researchreport. Add only that missing selector; explicit mismatches, page
    numbers, filters and destinations still pass the strict existing checks.
    """
    page_number(url, scope)
    query = scope_query(url, scope)
    if SCOPES[scope][0] == '/elibList.es' and 'elibName' not in query:
        return url + '&elibName=' + SCOPES[scope][2]
    return url


def parse_listing(html, scope, page):
    if scope not in SCOPES or type(page) is not int or page < 1:
        raise ValueError('invalid KEI requested scope or page')
    soup = BeautifulSoup(html, 'lxml')
    form = soup.select_one('.board_info form.Search')
    if form is None:
        raise ValueError('KEI source search form missing')
    scope_query(urljoin(BASE, form.get('action', '')), scope)
    path, mid, name = SCOPES[scope]
    for key, wanted in [('mid', mid), ('elibName' if path == '/elibList.es' else 'bid', name), ('b_list', '10')]:
        values = [x.get('value', '') for x in form.select(f'input[name="{key}"]')]
        if values != [wanted]:
            raise ValueError('KEI source form contract changed')
    total_text = text(soup.select_one('.page_info .total_page b'))
    current = text(soup.select_one('.page_info .current_page strong'))
    last = text(soup.select_one('.page_info .current_page b'))
    if (not re.fullmatch(r'[0-9]+(?:,[0-9]{3})*건', total_text)
            or not current.isdigit() or not last.isdigit()):
        raise ValueError('KEI source pagination counters missing')
    total, last_page = int(total_text[:-1].replace(',', '')), int(last)
    if int(current) != page or last_page != max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE) or page > last_page:
        raise ValueError('KEI source pagination counters disagree')
    cards = soup.select('.bstyle_list .lst_item > a') if scope == 'board-0009' else soup.select('.dbody#listView > ul')
    if len(cards) != min(PAGE_SIZE, total - (page - 1) * PAGE_SIZE):
        raise ValueError('KEI source list cardinality mismatch')
    items, ids = [], set()
    for card in cards:
        anchor = card if scope == 'board-0009' else card.select_one('li.title a[href]')
        if anchor is None:
            raise ValueError('KEI source detail link missing')
        url = urljoin(BASE, anchor.get('href', ''))
        query = scope_query(url, scope)
        key = 'c_id' if path == '/elibList.es' else 'list_no'
        uid = query.get(key, [''])[0]
        if query.get('act') != ['view'] or not re.fullmatch('[0-9]+', uid) or uid in ids:
            raise ValueError('KEI missing or repeated document identity')
        ids.add(uid)
        title_text = text(anchor.select_one('.title strong')) if scope == 'board-0009' else text(anchor)
        title = (anchor.get('title') or title_text).strip()
        if not title:
            raise ValueError('KEI source listing title missing')
        date = ''
        if scope == 'board-0009':
            for li in anchor.select('.more li'):
                if text(li).startswith('작성일'):
                    date = text(li.find('span'))
        else:
            date = text(card.select_one('li[class*="date"]'))
        attachments = [dict(url=urljoin(BASE, a['href']), label=a.get('title', ''),
                            kind='viewer' if urlsplit(urljoin(BASE, a['href'])).hostname == 'synap.kei.re.kr' else 'download')
                       for a in card.select('li.file a[href]')]
        items.append(dict(source_id=uid, source_key=f'{scope}:{uid}', scope=scope,
                          title=title, title_text=title_text, date_raw=date,
                          post_number=text(card.select_one('li.num')),
                          category=text(card.select_one('li.sec')), url=url, attachments=attachments))
    next_urls = set()
    for a in soup.select('.board_pager a[href]'):
        url = urljoin(BASE, a['href'])
        number = page_number(url, scope)
        if not 1 <= number <= last_page:
            raise ValueError('KEI source pager exceeds declared bounds')
        if number == page + 1:
            next_urls.add(listing_destination(url, scope))
    if page < last_page and len(next_urls) != 1:
        raise ValueError('KEI adjacent next-page link missing or ambiguous')
    if page == last_page and next_urls:
        raise ValueError('KEI terminal page still has successor')
    return dict(items=items, total=total, page=page, last_page=last_page,
                next_url=next(iter(next_urls), None),
                page_hash=hashlib.sha256(json.dumps(items, ensure_ascii=False, sort_keys=True,
                                                   separators=(',', ':')).encode()).hexdigest())
