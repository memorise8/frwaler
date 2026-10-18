"""L3b-20261018 (X3 follow-up): enumeration split for kirj-ee-archive.
crawl() walks archive -> journal issue pages -> issue menus (breadth-first queue), reads
article rows of each issue page (relative links resolved against the canonical issue URL)
and builds papers from the article pages; journal_hint is the archive link text inherited
along the queue. The plan discovers the same issue links (segments.follow) and reads each at
its own address (segments.link_as_url); the hint comes from the segment path/label.
Synthetic pages only (no network)."""
import ast
import contextlib
import copy
import hashlib
import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

from crawler.enumeration.adapters import Adapter, clean_paper, paper_identity
from crawler.enumeration.http import Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import ROOT, load
from tests.enumeration_fakes import Env

SITE = 'kirj-ee-archive'
HUB = 'https://kirj.ee/archive/?v=38dd815e66db'
A1 = 'https://kirj.ee/oil-shale-publications/?filter[year]=2022&filter[issue]=11&v=abc'
A2 = 'https://kirj.ee/engineering/?v=abc'  # a journal home without an issue: only a hub
I11 = 'https://kirj.ee/oil-shale-publications/?filter%5Byear%5D=2022&filter%5Bissue%5D=11'
I12 = 'https://kirj.ee/oil-shale-publications/?filter%5Byear%5D=2021&filter%5Bissue%5D=12'
I21 = 'https://kirj.ee/engineering-publications/?filter%5Byear%5D=2013&filter%5Bissue%5D=21'
ARCHIVE = (f'<html><body><div class="sisu"><a href="{A1}">Oil  Shale</a><a href="{A2}">Estonian Journal of Engineering</a>'
           '</div></body></html>')
TEXT = 'Synthetic abstract text for a regression fixture with enough words to pass the minimum length.'


def issue(menu, rows, title):
    links = ''.join(f'<a class="kirj_issue_year" href="{h}">{t}</a>' for h, t in menu)
    arts = ''.join(f'<div class="artlist_title">{t}; pp. 1-9</div><div class="pubauthorsdiv">A. B, C. D</div>'
                   f'<div class="art_type">Research article</div><a class="pdf-link" href="/wp-content/p/{i}_20310101120000.pdf">pdf</a>'
                   f'<a class="publication_list" href="?filter[year]={y}&filter[issue]={iss}&filter[publication]={i}">View</a>'
                   for i, y, iss, t in rows)
    head = f'<title>EAP - {title} publications</title>' if title else ''
    return (f'<html><head>{head}</head><body><div class="issues_menu">{links}</div>'
            f'<div class="issue_title">Vol 1, Issue 1, 2031</div>{arts}</body></html>')


MENU_OIL = [('/oil-shale-publications/?filter[year]=2022&filter[issue]=11', '2022'),
            ('/oil-shale-publications/?filter[year]=2021&filter[issue]=12', '2021')]
PAGE_OIL11 = issue(MENU_OIL, [(101, 2022, 11, 'Synthetic one'), (102, 2022, 11, 'Synthetic two')], 'Oil Shale')
PAGE_OIL12 = issue(MENU_OIL, [(103, 2021, 12, 'Synthetic three')], 'Oil Shale')
PAGE_ENG_HOME = issue([('/engineering-publications/?filter[year]=2013&filter[issue]=21', '2013')], [], 'Engineering home')
PAGE_ENG21 = issue([('/engineering-publications/?filter[year]=2013&filter[issue]=21', '2013')],
                   [(201, 2013, 21, 'Synthetic eng')], '')


def detail(i, text=TEXT):
    return (f'<html><body><div class="articleview_title">Synthetic article {i}; pp. 1-9</div><div id="authorsdiv">A. B</div>'
            f'<div class="abstractdiv">{text} {i}</div><a class="doi_article" href="https://doi.org/10.3176/syn.{i}">doi</a>'
            f'<div><b>Keywords</b></div><div>alpha, beta</div><div class="refsdiv"><p>r1</p></div></body></html>')


def page_for(url):
    """Every spelling of the same address (literal or encoded brackets, v=, page=)."""
    from urllib.parse import parse_qs
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    pub = (q.get('filter[publication]') or [''])[0]
    if pub:
        return detail(pub)
    issue_id = (q.get('filter[issue]') or [''])[0]
    return {('/archive/', ''): ARCHIVE, ('/oil-shale-publications/', '11'): PAGE_OIL11,
            ('/oil-shale-publications/', '12'): PAGE_OIL12, ('/engineering/', ''): PAGE_ENG_HOME,
            ('/engineering-publications/', '21'): PAGE_ENG21}.get((parts.path, issue_id))


def plan():
    return json.loads((ROOT / 'docs/examples/collector-splits' / (SITE + '.json')).read_text())


class RegisteredSplit(unittest.TestCase):
    def native(self):
        crawler, _ = load(SITE)
        captured, calls = [], []
        crawler.detail_delay = 0
        crawler._last_save_created = True
        crawler._save_paper = lambda paper: captured.append(copy.deepcopy(paper))
        crawler._curl = lambda url, referer=None, timeout=None: calls.append(url) or page_for(url)
        with patch('time.sleep', lambda *a: None), contextlib.redirect_stdout(io.StringIO()):
            result = crawler.crawl(limit=None)
        self.assertEqual(len(captured), result)
        return captured

    def run_plan(self):
        site = copy.deepcopy(plan()['sites'][SITE])
        site['request_interval_s'] = 0
        seen = []

        def handler(url):
            seen.append(url)
            if url.endswith('/robots.txt'):
                return Response(404, {}, b'')
            body = page_for(url)
            return Response(200, {'Content-Type': 'text/html'}, body, url) if body else Response(404, {}, b'', url)
        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            captured = []
            crawler = cls(env.conn)
            original = crawler._enumeration_paper

            def capture(*a):
                paper = original(*a)
                if paper is not None:
                    captured.append(copy.deepcopy(paper))
                return paper
            crawler._enumeration_paper = capture
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(site, handler, crawler=crawler)
            docs = env.docs()
        finally:
            env.close()
        return result, docs, captured, seen

    def test_papers_and_identity_equal_original_crawl(self):
        expected = self.native()
        result, docs, actual, _ = self.run_plan()
        self.assertEqual(4, len(expected))
        self.assertIn(result['status'], ('complete', 'complete_unverified'), result)
        key = lambda p: p['external_id']
        self.assertEqual(sorted((clean_paper(x) for x in expected), key=key), sorted(actual, key=key))
        self.assertEqual(sorted(paper_identity(x) for x in expected), sorted(paper_identity(x) for x in actual))
        by_id = {p['external_id']: json.loads(p['metadata']) for p in actual}
        self.assertEqual('Oil Shale', by_id['101']['journalHint'])
        self.assertEqual('Estonian Journal of Engineering', by_id['201']['journalHint'])
        self.assertEqual('Estonian Journal of Engineering', by_id['201']['journal'])  # page title empty
        self.assertEqual(I11, by_id['101']['sourceIssueUrl'])
        self.assertEqual(I12, by_id['103']['sourceIssueUrl'])
        self.assertEqual(I21, by_id['201']['sourceIssueUrl'])
        self.assertEqual({'101', '102', '103', '201'}, {d['post_number'] for d in docs})

    def test_short_abstract_is_a_rule_skip(self):
        crawler, _ = load(SITE)
        rec = {'external_id': '9', 'url': 'https://kirj.ee/x/?filter%5Bpublication%5D=9', 'journal': 'X', 'title': 'T'}
        self.assertIsNone(crawler._enumeration_paper(detail(9, text='short'), {'key': '1', 'label': 'J'}, rec))

    def test_original_code_unchanged(self):
        code = (ROOT / 'crawler/sites/custom' / (SITE + '.py')).read_text()
        actual = {}
        for n in ast.parse(code).body:
            if isinstance(n, ast.ClassDef):
                for m in n.body:
                    if isinstance(m, ast.FunctionDef):
                        actual[f'{n.name}.{m.name}'] = hashlib.sha256(ast.get_source_segment(code, m).encode()).hexdigest()
        for name, digest in ORIGINAL_SHA256.items():
            self.assertEqual(digest, actual.get(name), name)


ORIGINAL_SHA256 = {"KirjEeArchiveCrawler.__init__": "92a1f2a87b6fa015a69f4fb6d8d1a5048356d62586ca3d1d0cdfc05f38cf7b72", "KirjEeArchiveCrawler._curl": "a35ada88bf0284e5afa0b3b7180d16c948033adcc55ea10654ef6c0702affe0f", "KirjEeArchiveCrawler._parse_html": "357a9f844f553b2e131b25402e973b455cd55fe9f23eac4217f0fd5cdaaa1459", "KirjEeArchiveCrawler._clean_text": "87d02935b687b5cba9c4d8d22b6a9558756642f98f983fd97e1fec6782204367", "KirjEeArchiveCrawler._one_line": "159d46f0e5e2328b8f5396236e4b0af55ccb7a4c6e2e7494edbc12c331a96cb2", "KirjEeArchiveCrawler._tag_text": "99fa48c034ca346da280f854ff2d35f099021cc8d426fefca74937c4157ef304", "KirjEeArchiveCrawler._dedupe": "5d8196990cb743806995aea9350949a5bddc4b3595c0b7e85a4361b38be2fe16", "KirjEeArchiveCrawler._split_authors": "8a85554cd8219bcb099299c568ef9af2e4d964a965066f34949835b1d17f9c04", "KirjEeArchiveCrawler._query_value": "cba9fbc62e720b187aa3615f365240013b8f2add9e19c67df9a2f2eea19eff3b", "KirjEeArchiveCrawler._year_from_text": "08b9aa5a93a65ba069db232efdb0ad6c4ab647f02d515a72c359402afadbfea9", "KirjEeArchiveCrawler._extract_doi": "b8682ceb2bd43a05ee42b5e24e86fec907537a8fec0a29e84898fd75649b133e", "KirjEeArchiveCrawler._pdf_timestamp": "b791dcb87e3ac92ceb5cb0f9e1d84af3d021e1cbaf02d25de49eb22739f6cfca", "KirjEeArchiveCrawler._split_title_pages": "50c9d45be0b140eae14726c58c84e6d05824ccfcf08377b8c29da1990a938084", "KirjEeArchiveCrawler._canonical_url": "7f879ab371d46d5050e183f51bfa73191b19938bc2c597d2bb61c12b52328490", "KirjEeArchiveCrawler._is_internal_content_url": "2b20a26a43b8ae592a4779e8e77b22d4898052b10108d52e42dc6e5ff8985211", "KirjEeArchiveCrawler._parse_archive_links": "6367b8f7d1995e1eb96d512cd2ccc25edfecf6577ad901867fcc7414d1cacb52", "KirjEeArchiveCrawler._parse_issue_links": "3862841de56272635fd26499376950734aa9e57a109c25f75ec52f30e1ca9dbc", "KirjEeArchiveCrawler._page_journal_name": "bd9d13fce7a66e68630a847c1d57dd54bdfada86f7e55a62fa8aff368b8aa70c", "KirjEeArchiveCrawler._parse_issue_records": "af9396d15f5b9f087cca939fc3f9587a10d7a2164a514eb991d9acf6de527b6a", "KirjEeArchiveCrawler._parse_keywords": "5f849007bdd05ce539f4e62580dc7bc6007c4377744d5062d7f7bb84fb9dcac8", "KirjEeArchiveCrawler._parse_detail_record": "2d251de7ccdec29b97179c5d6a4ee7697537a72348e64b3d39f0dd02d77afd6c", "KirjEeArchiveCrawler.crawl": "de4498feaaa11294c1273dc1dc279f3852c1bbcf3e5665ad9d0992ef1d97b826"}

if __name__ == '__main__':
    unittest.main()
