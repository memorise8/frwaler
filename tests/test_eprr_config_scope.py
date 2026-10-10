"""The registered EPRR configuration must send the delivered facet unchanged."""
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qsl, urlsplit
import requests
from crawler.generic_crawler import GenericCrawler


class EprrConfiguredScope(unittest.TestCase):
    def test_actual_requests_preparation_preserves_facet_on_each_page(self):
        config = Path(__file__).resolve().parents[1] / 'crawler/sites/configs/eprr-lanl-gov.json'
        crawler = GenericCrawler(config, None)
        crawler._delay = 0
        sent = []

        def send(request, **kwargs):
            sent.append(request.url)
            response = requests.Response()
            response.status_code = 200
            response._content = b'<html><main>Retained listing fixture</main></html>'
            response.url = request.url
            return response

        try:
            with patch.object(crawler._session, 'send', side_effect=send):
                for page in (1, 2):
                    crawler._fetch_page(crawler._config['list_page']['url'],
                                        params=dict(crawler._config['list_page']['params'], page=page))
            self.assertEqual(len(sent), 2)
            for page, url in enumerate(sent, 1):
                self.assertEqual(parse_qsl(urlsplit(url).query),
                                 [('f[docType_f][]', 'Full Paper'), ('page', str(page))])
        finally:
            crawler._session.close()


if __name__ == '__main__':
    unittest.main()
