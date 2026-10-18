"""research-thea-ie-browse: OAI-PMH ListRecords with resumptionToken (ext:next_link text+template),
detail = crawl()'s GetRecord(mets) request.

Memory-only: compares the plan's output with the unchanged crawl()'s save argument and runs the
shipped plan through the real SiteRunner with a fake transport.
"""
import contextlib
import copy
import io
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

from crawler.enumeration.adapters import Adapter
from crawler.enumeration.http import PermanentFailure, Response
from crawler.enumeration.parsing import ListingParser
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import SplitChecks, load, split_fixture, split_plan

SITE = 'research-thea-ie-browse'
NS = 'xmlns="http://www.openarchives.org/OAI/2.0/"'
EMPTY_TAIL = f'<OAI-PMH {NS}><ListRecords><resumptionToken completeListSize="3" cursor="100"/></ListRecords></OAI-PMH>'
BAD_TOKEN = f'<OAI-PMH {NS}><error code="badResumptionToken">expired</error></OAI-PMH>'
XML = 'text/xml;charset=UTF-8'


def deleted_page(token=''):
    return (f'<OAI-PMH {NS}><ListRecords><record><header status="deleted"><identifier>oai:research.thea.ie:20.500.12065/1300'
            f'</identifier><datestamp>2020-01-01T00:00:00Z</datestamp></header></record>'
            f'<resumptionToken>{token}</resumptionToken></ListRecords></OAI-PMH>')


class TheaSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def parser(self):
        p = site_plan(self.plan, SITE, registered=[SITE])
        crawler, _ = load(SITE)
        return ListingParser(p.listing['parser'], adapter=Adapter(SITE, p.section('adapter'), None, crawler=crawler))

    def test_valid_empty_page_and_error_page_are_distinct(self):
        # OAI override: an empty <ListRecords> is an explicit empty page (listing_marker);
        # an OAI error, HTML or maintenance page is a listing parse error, never the end.
        parser = self.parser()
        url = 'https://research.thea.ie/oai/request?' + urlencode(self.plan['sites'][SITE]['listing']['params'])
        page = parser.parse(Response(200, {'Content-Type': XML}, EMPTY_TAIL, url))
        self.assertEqual([], page.items)
        self.assertTrue(page.extra.get('explicit_empty'))
        for body in (BAD_TOKEN, '<html>maintenance</html>', 'not xml'):
            with self.subTest(body=body):
                try:
                    bad = parser.parse(Response(200, {'Content-Type': XML}, body, url))
                except PermanentFailure as exc:
                    self.assertTrue(exc.reason.startswith('listing_parse_error:'), exc.reason)
                else:
                    self.assertTrue(bad.extra.get('parse_error'))
                    self.assertFalse(bad.extra.get('explicit_empty', False))

    def test_identity_and_pdf_choice(self):
        papers = self.pipeline()
        self.assertEqual(['20.500.12065/1188', '20.500.12065/1190'], [p['external_id'] for p in papers])
        self.assertEqual(['1188', '1190'], [p['post_number'] for p in papers])
        self.assertEqual(['https://research.thea.ie/handle/20.500.12065/1188',
                          'https://research.thea.ie/handle/20.500.12065/1190'], [p['url'] for p in papers])
        self.assertTrue(papers[0]['pdf_url'].endswith('Griffin%20Maria.pdf?sequence=1'))
        self.assertTrue(papers[1]['pdf_url'].endswith('thesis.doc'))

    def test_deleted_record_is_rule_skip_like_crawl(self):
        crawler, _ = load(SITE)
        rows = crawler._enumeration_items(deleted_page().encode(), 'https://research.thea.ie/oai/request')
        self.assertEqual(['20.500.12065/1300'], [r['external_id'] for r in rows])
        self.assertIsNone(crawler._enumeration_paper(rows[0], b''))


class TheaSplitRun(unittest.TestCase):
    """Shipped plan end to end through the real SiteRunner (fake transport)."""

    def setUp(self):
        self.denials = []
        def denied(*a, **k):
            self.denials.append('io')
            raise AssertionError('split run attempted network/process I/O')
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'socket.socket.connect_ex',
                     'subprocess.Popen._execute_child'):
            p = patch(name, denied); p.start(); self.addCleanup(p.stop)
        self.addCleanup(lambda: self.assertEqual([], self.denials))

    def run_plan(self, first=None, second=None):
        # Real OAI last page: records plus an empty resumptionToken (here one deleted record).
        second = deleted_page('') if second is None else second
        from tests.enumeration_fakes import Env
        fixture = split_fixture(SITE)
        plan = copy.deepcopy(split_plan(SITE))
        plan['request_interval_s'] = 0
        seen = []

        def handler(url):
            parts = urlsplit(url)
            if parts.path == '/robots.txt':
                return Response(404, {}, b'')
            if url in fixture['details']:
                return Response(200, {'Content-Type': XML}, fixture['details'][url], url)
            q = parse_qs(parts.query)
            seen.append(q)
            if q == {'verb': ['ListRecords'], 'metadataPrefix': ['oai_dc']}:
                return Response(200, {'Content-Type': XML}, first or fixture['listing_body'], url)
            if q == {'verb': ['ListRecords'], 'resumptionToken': ['oai_dc////100']}:
                return Response(200, {'Content-Type': XML}, second, url)
            return Response(404, {}, b'', url)

        env = Env(SITE)
        try:
            _, module = load(SITE)
            cls = next(x for x in vars(module).values() if isinstance(x, type) and getattr(x, 'site_id', None) == SITE)
            with contextlib.redirect_stdout(io.StringIO()):
                result = env.run(plan, handler, crawler=cls(env.conn))
            return result, env.docs(), seen
        finally:
            env.close()

    def test_shipped_plan_completes_end_to_end(self):
        result, docs, seen = self.run_plan()
        self.assertTrue(result['status'].startswith('complete'), result)
        self.assertEqual('exhausted_cursor_end', result['stop_reason'], result)
        self.assertEqual({'20.500.12065/1188', '20.500.12065/1190'}, {d['post_number'] for d in docs})  # DB post_number = external_id (BaseCrawler)
        self.assertIn({'verb': ['ListRecords'], 'resumptionToken': ['oai_dc////100']}, seen)

    def test_bad_resumption_token_on_second_page_is_not_the_end(self):
        result, docs, _ = self.run_plan(second=BAD_TOKEN)
        self.assertEqual('incomplete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)

    def test_error_first_page_ends_incomplete(self):
        result, docs, _ = self.run_plan(first='<html><body>Site under maintenance</body></html>')
        self.assertEqual('incomplete', result['status'], result)
        self.assertTrue(result['stop_reason'].startswith('listing_failed:listing_parse_error:'), result)
        self.assertEqual([], docs)


if __name__ == '__main__':
    unittest.main()
