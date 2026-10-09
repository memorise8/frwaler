"""Capture native requests without transport; scope/order contract regression."""
import contextlib
import io
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests

from crawler import db_libertree
from crawler.sites import CRAWLERS


class Captured(BaseException):
    def __init__(self, url): self.url = url


class Forbidden(BaseException):
    pass


class HalQueryOrderTests(unittest.TestCase):
    def test_native_queries_keep_membership_and_primary_order_with_unique_tiebreaker(self):
        contracts = json.loads((Path(__file__).parent/'fixtures/hal_query_order.json').read_text())
        def request(session, method, url, **kwargs):
            if method.upper() != 'GET': raise Forbidden()
            if kwargs.get('params'): url += '?' + urlencode(kwargs['params'], doseq=True)
            raise Captured(url)
        def command(argv, *args, **kwargs):
            if not isinstance(argv, (list, tuple)) or Path(argv[0]).name != 'curl': raise Forbidden()
            urls = [s for s in argv if str(s).startswith(('http://', 'https://'))]
            if len(urls) != 1: raise Forbidden()
            raise Captured(urls[0])
        for contract in contracts:
            with self.subTest(site=contract['site_id']), sqlite3.connect(':memory:') as conn:
                db_libertree.init_db(conn); db_libertree.upsert_site(conn, contract['site_id'], 'HAL fixture', 'https://hal.science')
                crawler = CRAWLERS[contract['site_id']](db_conn=conn)
                try:
                    with contextlib.redirect_stdout(io.StringIO()), patch.object(requests.Session, 'request', request), \
                            patch.object(subprocess, 'run', command), patch.object(socket.socket, 'connect', side_effect=Forbidden()), \
                            patch('time.sleep'), self.assertRaises(Captured) as captured:
                        crawler.crawl(limit=1)
                finally:
                    crawler._session.close()
                url = captured.exception.url
                self.assertEqual(url.split('?')[0], contract['endpoint'])
                pairs = parse_qsl(urlsplit(url).query, keep_blank_values=True)
                original = [tuple(p) for p in contract['query_pairs']]
                self.assertEqual(sorted(p for p in pairs if p[0] != 'sort'), sorted(p for p in original if p[0] != 'sort'))
                original_sort = [v for k,v in original if k == 'sort']
                expected_sort = original_sort[0]+',docid asc' if original_sort else 'docid asc'
                self.assertEqual([v for k,v in pairs if k == 'sort'], [expected_sort])


if __name__ == '__main__':
    unittest.main()
