from importlib import import_module
import unittest


class BfeDirectSourceTest(unittest.TestCase):
    def setUp(self):
        self.crawler = import_module('crawler.sites.custom.bfe-admin-ch-bfe').BfeAdminChBfeCrawler(None)

    def test_current_list_and_downloads_share_publication_host(self):
        self.assertEqual(self.crawler._build_page_url(2), 'https://pubdb.bfe.admin.ch/de/suche?page=2&x=1')
        self.assertEqual(self.crawler._decode_exturl_href('/de/publication/download/12700'),
                         'https://pubdb.bfe.admin.ch/de/publication/download/12700')
        self.assertIsNone(self.crawler._decode_exturl_href('/unrelated'))

    def test_unrecognized_html_is_not_an_empty_success(self):
        with self.assertRaisesRegex(ValueError, 'neither publication records'):
            self.crawler._parse_list_page('<html><h1>Access restricted</h1></html>')
        self.assertEqual(self.crawler._parse_list_page('Es wurden keine Publikationen gefunden.'), ([], True))
