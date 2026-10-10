"""Source identities and article fields for all sections of the delivered press page."""
import json
import re
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup

BASE = 'https://www.academie-sciences.fr'
START = BASE + '/espace-presse'
SECTIONS = ('Communiqués et dossiers de presse', 'Parutions dans la presse', 'Dossiers de presse')


def text(node):
    return ' '.join(node.get_text(' ', strip=True).split()) if node else ''


def source_url(base, href):
    value = urlsplit(urljoin(base, href))
    # Match the established native PDF URL identity, including parentheses.
    path = quote(unquote(value.path), safe='/._-()')
    if (value.scheme != 'https' or value.netloc != 'www.academie-sciences.fr'
            or value.query or value.fragment or not path or path == '/'):
        raise ValueError('Academie source target outside the reviewed press origin')
    return urlunsplit((value.scheme, value.netloc, path, '', ''))


def editorial_items(soup, page_num, list_url):
    """Read the three explicit source sections; emit them once on page one."""
    sections = soup.select('.NodeEdito--pressArea > .ParagraphAccordion')
    if not sections:
        if page_num == 1 and soup.select_one('.NodeEdito--pressArea') is not None:
            raise ValueError('Academie explicit press sections missing')
        return []
    found, items = set(), []
    for section in sections:
        headings = section.select('h3')
        if len(headings) != 1:
            raise ValueError('Academie source section heading missing or ambiguous')
        name = text(headings[0])
        if name not in SECTIONS or name in found:
            raise ValueError('Academie unreviewed or repeated source section')
        found.add(name)
        for card in section.select('.ParagraphAccordion-item'):
            links = card.select('a[href]')
            if len(links) != 1:
                raise ValueError('Academie source card link missing or ambiguous')
            title = text(card.select_one('h4.ParagraphAccordion-item-title'))
            if not title:
                raise ValueError('Academie source card title missing')
            url = source_url(list_url, links[0]['href'])
            pdf = urlsplit(url).path.lower().endswith('.pdf')
            filename = unquote(urlsplit(url).path.rsplit('/', 1)[-1]) if pdf else None
            items.append(dict(kind='PDF' if pdf else 'HTML', detail_url=url,
                              pdf_url=url if pdf else None, title=title,
                              source_section=name, source_card_text=text(card),
                              original_filename=filename, listed_date=None,
                              listed_datetime=None, posted_date_raw=None,
                              list_page=page_num, list_url=list_url))
    if found != set(SECTIONS):
        raise ValueError('Academie explicit press section missing')
    return items if page_num == 1 else []


def parse_article(raw, item):
    if item.get('kind') != 'HTML' or item.get('source_section') not in SECTIONS:
        raise ValueError('Academie invalid source article scope')
    if not re.search(r'<html\b', raw, re.I) or not re.search(r'</html\s*>', raw, re.I):
        raise ValueError('Academie source HTML document incomplete')
    from delivery.health.access_challenge import access_challenge
    if access_challenge(raw):
        raise PermissionError('Academie source access challenge')
    soup = BeautifulSoup(raw, 'lxml')
    url = source_url(START, item['detail_url'])
    canonicals = soup.select('link[rel=canonical]')
    if (soup.select('base[href]') or len(canonicals) != 1
            or not canonicals[0].get('href')
            or source_url(url, canonicals[0]['href']) != url):
        raise ValueError('Academie article canonical identity mismatch')
    articles = soup.select('main .Node.NodeEdito')
    if len(articles) != 1:
        raise ValueError('Academie article container missing or ambiguous')
    article = articles[0]
    header = article.select_one(':scope > .NodeEdito-header')
    body = article.select_one(':scope > .NodeEdito-content')
    if header is None or body is None:
        raise ValueError('Academie article header or body missing')
    headings = header.select('h1')
    if len(headings) != 1 or not text(headings[0]):
        raise ValueError('Academie article title missing or ambiguous')
    title = text(headings[0])
    kind = text(header.select_one('.NodeEdito-surtitle'))
    expected = {'Actualités': 'NodeEdito--news', 'Parution dans la presse': 'NodeEdito--press-release'}
    if kind not in expected or expected[kind] not in article.get('class', []):
        raise ValueError('Academie unreviewed article type')
    if (item['source_section'] == 'Parutions dans la presse') != (kind == 'Parution dans la presse'):
        raise ValueError('Academie article type differs from the linked source section')
    dates = header.select('.NodeEdito-date')
    if len(dates) != 1:
        raise ValueError('Academie displayed date missing or ambiguous')
    # This is the portal's date, not the PDF's or the newspaper's date. Invalid
    # and partial values still belong in the raw-date columns, without invention.
    date_raw = text(dates[0]) or None
    chapo = text(header.select_one('.NodeEdito-chapo'))
    paragraphs = body.select(':scope > .ParagraphText')
    if not paragraphs:
        raise ValueError('Academie article text structure missing')
    for paragraph in paragraphs:
        for node in paragraph.select('script,style,nav'):
            node.decompose()
    parts = [text(node) for node in paragraphs]
    abstract = '\n\n'.join(value for value in [chapo] + parts if value)
    from delivery.files.text_quality import inspect_text
    if inspect_text(abstract)['state'] == 'review_required':
        raise ValueError('Academie source article text quality requires review')
    for link in header.select('a[href]'):
        if not any(set(parent.get('class', [])) & {'NodeEdito-cta', 'NodeEdito-back', 'NodeEdito-chapo'}
                   for parent in link.parents if parent is not header):
            raise ValueError('Academie unreviewed article header link role')
    links, pdfs, primary = [], {}, []
    for link in header.select('.NodeEdito-cta a[href]') + body.select('a[href]'):
        raw_url = urljoin(url, link['href'])
        parsed = urlsplit(raw_url)
        zone = 'article_header_CTA' if header in link.parents else 'article_body'
        entry = dict(url=raw_url, raw_href=link['href'], label=text(link), source_zone=zone)
        if parsed.path.lower().endswith('.pdf'):
            entry['url'] = source_url(url, link['href'])
            entry['kind'] = 'PDF'
            pdfs[entry['url']] = entry
            if zone == 'article_header_CTA':
                primary.append(entry['url'])
        else:
            entry['kind'] = 'other'
        links.append(entry)
    primary = list(dict.fromkeys(primary))
    body_primary = [url for url, link in pdfs.items()
                    if not re.search(r'english version|version anglaise', link['label'], re.I)]
    if len(primary) > 1 or (not primary and len(body_primary) > 1):
        raise ValueError('Academie primary article PDF ambiguous')
    primary_url = primary[0] if primary else (body_primary[0] if body_primary else None)
    return dict(title=title, listing_title=item['title'], source_url=url,
                source_section=item['source_section'], source_page_kind=kind,
                title_binding='exact' if title == item['title'] else 'canonical_article_with_curated_link_title',
                abstract=abstract, introduction=chapo, body_parts=parts,
                published_date_raw=None, listed_date_raw=date_raw,
                source_date_role='portal_displayed_date_not_original_document_publication',
                primary_PDF_url=primary_url, article_links=links,
                article_PDF_urls=list(pdfs),
                body_images=[dict(url=urljoin(url, n['src']), alt=n.get('alt', '')) for n in body.select('img[src]')])


def article_paper(site_id, detail):
    pdf = detail['primary_PDF_url']
    return dict(site_id=site_id, external_id=unquote(urlsplit(detail['source_url']).path).strip('/'),
                title=detail['title'], abstract=detail['abstract'], url=detail['source_url'],
                published_date=None, listed_date=detail['listed_date_raw'],
                posted_date=detail['listed_date_raw'], pdf_url=pdf,
                original_filename=unquote(urlsplit(pdf).path.rsplit('/', 1)[-1]) if pdf else None,
                publisher='Academie des sciences', authors=None,
                category=detail['source_page_kind'],
                metadata=json.dumps(detail, ensure_ascii=False, sort_keys=True))
