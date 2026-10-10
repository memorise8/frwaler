"""Source-bound KEI detail fields for the eleven delivered catalogues/boards.

Parsing a source page is separate from body eligibility, native persistence and
file acceptance. In particular, image alt text and attachment filenames are not
article bodies, and a writer's timestamp is not a publication date.
"""
import re
from pathlib import PurePosixPath
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from crawler.kei_source import SCOPES, scope_query, text, title_quality


def _identity(soup, name, wanted):
    values = [n.get('value', '') for n in soup.select(f'input[name="{name}"]')]
    if not values or set(values) != {wanted}:
        raise ValueError(f'KEI detail {name} identity mismatch')


def _attachment(link, item):
    url = urljoin(item['url'], link['href'])
    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.fragment:
        raise ValueError('KEI unreviewed attachment destination')
    if item['scope'].startswith('board-'):
        query = parse_qs(parsed.query, keep_blank_values=True)
        if (parsed.netloc != 'www.kei.re.kr' or parsed.path != '/boardDownload.es'
                or set(query) != {'bid', 'list_no', 'seq'}
                or query.get('bid') != [SCOPES[item['scope']][2]]
                or query.get('list_no') != [item['source_id']]
                or len(query.get('seq', [])) != 1
                or not re.fullmatch(r'[1-9][0-9]*', query['seq'][0])):
            raise ValueError('KEI attachment source identity mismatch')
    elif (parsed.netloc != 'library.kei.re.kr' or parsed.query
          or not re.fullmatch(r'/pyxis-api/1/digital-files/'
                              r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-'
                              r'[0-9a-f]{4}-[0-9a-f]{12}', parsed.path)):
        raise ValueError('KEI unreviewed catalogue attachment')
    filename = text(link)
    return dict(url=url, filename=filename,
                extension=PurePosixPath(filename).suffix.lower(),
                source_size_label=text(link.parent.select_one('.fileSize')),
                file_bytes_verified=False)


def parse_detail(html, item):
    """Read a detail only when its scope and source ID agree with the listing."""
    scope, uid = item['scope'], item['source_id']
    if scope not in SCOPES or not isinstance(uid, str) or not re.fullmatch(r'[0-9]+', uid):
        raise ValueError('KEI invalid requested detail identity')
    board = scope.startswith('board-')
    key = 'list_no' if board else 'c_id'
    query = scope_query(item['url'], scope)
    if query.get('act') != ['view'] or query.get(key) != [uid]:
        raise ValueError('KEI detail URL identity mismatch')
    soup = BeautifulSoup(html, 'lxml')
    _identity(soup, 'mid', SCOPES[scope][1])
    _identity(soup, 'bid' if board else 'elibName', SCOPES[scope][2])
    _identity(soup, key, uid)
    mains = soup.select('.tstyle_view')
    if len(mains) != 1:
        raise ValueError('KEI detail container missing or ambiguous')
    main = mains[0]
    title = text(main.select_one(':scope > .title' if board else '.report_title .right .tit'))
    listing_title = item['title']
    if item.get('title_quality') or title_quality(listing_title) or title_quality(title):
        raise ValueError('KEI source title quality requires review')
    # Catalogue title attributes can retain repeated whitespace while the
    # displayed detail is normalized by text(). Preserve the original attribute
    # in listing_title and compare both source strings on the same basis.
    normalized_listing = ' '.join(listing_title.split())
    truncated = (normalized_listing.endswith(('..', '…'))
                 and bool(normalized_listing.rstrip('.…'))
                 and title.startswith(normalized_listing.rstrip('.…')))
    if not title or (title != normalized_listing and not truncated):
        raise ValueError('KEI detail title differs from source listing')
    fields, authors, writer = {}, [], ''
    if board:
        for node in main.select(':scope > ul.head > li'):
            label, value = text(node.find('strong')), text(node.find('span'))
            if label in fields:
                raise ValueError('KEI repeated detail field')
            if label:
                fields[label] = value
        date_node = main.select_one(':scope > ul.head > li.date')
        date_label = text(date_node.find('strong')) if date_node else ''
        date = text(date_node.find('span')) if date_node else ''
        if date_label and date_label not in ('발간일', '작성일시', '작성일', '등록일'):
            raise ValueError('KEI unreviewed source date role')
        publication = date if date_label == '발간일' else None
        listed = date if date_label in ('작성일시', '작성일', '등록일') else item.get('date_raw')
        writer = fields.get('작성자', '')
        body = main.select_one(':scope > .tb_contents')
        contents = ''
    else:
        for node in main.select('.report_title .right > ul > li'):
            label, value = text(node.find('span')), text(node.find('em'))
            if label in fields:
                raise ValueError('KEI repeated catalogue field')
            if label:
                fields[label] = value
        date_label, publication = '발간일', fields.get('발간일') or None
        listed = item.get('date_raw')
        for label in ('저자', '연구진'):
            authors.extend(part.strip() for part in re.split(r'[;,]', fields.get(label, '')) if part.strip())
        body = main.select_one('#view2')
        contents = text(main.select_one('#view1'))
    if body is None:
        raise ValueError('KEI source body container missing')
    for node in body.select('script,style,nav'):
        node.decompose()
    value = text(body)
    images = [dict(url=urljoin(item['url'], n['src']), alt=n.get('alt', ''))
              for n in body.select('img[src]')]
    files = [_attachment(link, item) for link in main.select('.add_file_list a.file-down[href]')]
    return dict(scope=scope, source_id=uid, source_key=f'{scope}:{uid}',
                source_url=item['url'], title=title, listing_title=listing_title,
                title_binding=('exact' if title == listing_title else
                               'source_title_whitespace_normalized' if title == normalized_listing else
                               'source_listing_prefix_with_ellipsis'),
                abstract=value, contents=contents,
                body_classification=('source_text_present' if value else
                                     'empty_text_with_images' if images else 'empty_text'),
                body_ends_with_ellipsis=value.endswith(('...', '…')),
                body_images=images, published_date_raw=publication,
                listed_date_raw=listed or None, source_listing_date_raw=item.get('date_raw'),
                detail_date_label=date_label, raw_fields=fields,
                authors=authors, writer=writer, attachments=files,
                primary_PDF_candidate=next((f for f in files if f['extension'] == '.pdf'), None),
                file_bytes_verified=False, full_text_fidelity_verified=False)
