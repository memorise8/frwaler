"""Source-bound KEI detail fields for the eleven delivered catalogues/boards.

Parsing a source page is separate from body eligibility, native persistence and
file acceptance. In particular, image alt text and attachment filenames are not
article bodies, and a writer's timestamp is not a publication date.
"""
import hashlib
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


def _known_repaired_body(nodes, html):
    return (len(nodes) == 1 and nodes[0].name in ('span', 'p')
            and re.fullmatch(r'[\s\S]*</div>\s*<div class=', nodes[0].get('style', ''))
            and html.count('<div class="view" id="view2">') == 1)


def _verify_recovery_metadata(html, item, title, fields, files):
    """A second parser must agree on metadata outside the damaged contents."""
    soup = BeautifulSoup(html, 'html.parser')
    for name, wanted in [('mid', SCOPES[item['scope']][1]),
                         ('elibName', SCOPES[item['scope']][2]),
                         ('c_id', item['source_id'])]:
        _identity(soup, name, wanted)
    mains = soup.select('.tstyle_view')
    if len(mains) != 1:
        raise ValueError('KEI PDF recovery metadata container ambiguous')
    main = mains[0]
    other_fields = {}
    for node in main.select('.report_title .right > ul > li'):
        label, value = text(node.find('span')), text(node.find('em'))
        if label in other_fields:
            raise ValueError('KEI PDF recovery metadata field ambiguous')
        if label:
            other_fields[label] = value
    other_files = [_attachment(n, item) for n in main.select('.add_file_list a.file-down[href]')]
    if (text(main.select_one('.report_title .right .tit')) != title or other_fields != fields
            or other_files != files or not _known_repaired_body(main.select('#view2'), html)):
        raise ValueError('KEI PDF recovery source metadata disagrees between parsers')


def parse_detail(html, item, *, allow_pdf_recovery=False):
    """Read a detail only when its scope and source ID agree with the listing."""
    # The server can finish an HTTP 200 response halfway through rendering a
    # report (observed at an unclosed publication-date field). HTML parsers
    # repair those tags, which must not turn a partial document into a body gap
    # or an apparently usable body. Fragments remain useful for parser callers.
    if re.search(r'<html\b', html, re.I) and not re.search(r'</html\s*>', html, re.I):
        raise ValueError('KEI source HTML document incomplete')
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
    # The catalogue and detail for 699539 use different middle-dot glyphs.
    # Keep both source strings; this limited comparison additionally requires
    # the same publication date and an identical explicit download below.
    separators = {'·', '‧', 'ㆍ'}
    separator_variant = (not board and title != normalized_listing
                         and len(title) == len(normalized_listing)
                         and all(a == b or (a in separators and b in separators)
                                 for a, b in zip(title, normalized_listing)))
    if not title or (title != normalized_listing and not truncated and not separator_variant):
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
        bodies = main.select(':scope > .tb_contents')
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
        bodies = main.select('#view2')
        contents = text(main.select_one('#view1'))
    if not bodies:
        raise ValueError('KEI source body container missing')
    # A broken quote in the preceding contents table (observed at 681569)
    # lets lxml attach id="view2" to a span and absorb the site footer.
    # Such a repaired node is neither a report body nor an empty-body proof.
    malformed = len(bodies) != 1 or bodies[0].name != 'div'
    if malformed and not (allow_pdf_recovery and not board and _known_repaired_body(bodies, html)):
        raise ValueError('KEI source body container malformed or ambiguous')
    body = bodies[0]
    if malformed:
        value, images, contents = None, None, None
    else:
        for node in body.select('script,style,nav'):
            node.decompose()
        value = text(body)
        images = [dict(url=urljoin(item['url'], n['src']), alt=n.get('alt', ''))
                  for n in body.select('img[src]')]
    files = [_attachment(link, item) for link in main.select('.add_file_list a.file-down[href]')]
    if malformed:
        _verify_recovery_metadata(html, item, title, fields, files)
        if not any(f['extension'] == '.pdf' for f in files):
            raise ValueError('KEI malformed source body has no validated PDF attachment')
    if separator_variant:
        listed_downloads = {f['url'] for f in item.get('attachments', [])
                            if f.get('kind') == 'download' and f.get('url')}
        if (not publication or publication != item.get('date_raw')
                or not listed_downloads.intersection(f['url'] for f in files)):
            raise ValueError('KEI detail title differs without date and attachment corroboration')
    result = dict(scope=scope, source_id=uid, source_key=f'{scope}:{uid}',
                source_url=item['url'], title=title, listing_title=listing_title,
                title_binding=('exact' if title == listing_title else
                               'source_title_whitespace_normalized' if title == normalized_listing else
                               'source_title_separator_variant_with_date_and_attachment' if separator_variant else
                               'source_listing_prefix_with_ellipsis'),
                abstract=value, contents=contents,
                body_classification=('malformed_source_HTML_requires_PDF' if malformed else
                                     'source_text_present' if value else
                                     'empty_text_with_images' if images else 'empty_text'),
                body_ends_with_ellipsis=None if malformed else value.endswith(('...', '…')),
                body_images=images, published_date_raw=publication,
                listed_date_raw=listed or None, source_listing_date_raw=item.get('date_raw'),
                detail_date_label=date_label, raw_fields=fields,
                authors=authors, writer=writer, attachments=files,
                primary_PDF_candidate=next((f for f in files if f['extension'] == '.pdf'), None),
                file_bytes_verified=False, full_text_fidelity_verified=False)
    if malformed:
        result['HTML_body_failure'] = dict(reason='source_body_attribute_consumed_container',
                                          source_HTML_sha256=hashlib.sha256(html.encode()).hexdigest(),
                                          repaired_tag=body.name,
                                          metadata_independently_corroborated=True)
    return result
