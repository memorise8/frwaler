"""WordPress split plans: only WordPress's own past-last-page 400 ends the listing,
and X-WP-Total is reconciled with the listed items (synthetic, real SiteRunner.run)."""
import json
import unittest
from unittest.mock import patch

from crawler.enumeration.http import Response
from crawler.enumeration.plan import site_plan
from tests.collector_split_helpers import run_split, split_plan

WORDPRESS = ['climateinstitute-ca-newsroom', 'nasa-gov-2025-news-releases', 'research-imt-nord-europe-fr-news',
             'ukdataservice-ac-uk-2026', 'srnl-gov-newsroom', 'minfin-gov-gr-grafeio-typou']
WP_END = [{'status': 400, 'json_code': 'rest_post_invalid_page_number'}]


def _no_io(*args, **kwargs):
    raise AssertionError('synthetic split run attempted network/process I/O')


class WordPressEnd(unittest.TestCase):
    RUN_SITES = [s for s in WORDPRESS if s != 'minfin-gov-gr-grafeio-typou']

    def setUp(self):
        for name in ('socket.getaddrinfo', 'socket.socket.connect', 'subprocess.Popen._execute_child'):
            p = patch(name, _no_io); p.start(); self.addCleanup(p.stop)

    def test_plans_end_only_on_the_wordpress_json_code(self):
        for site in WORDPRESS:
            with self.subTest(site=site):
                raw = split_plan(site)
                self.assertEqual(WP_END, raw['listing'].get('end_statuses'))
                self.assertEqual({'type': 'header', 'name': 'X-WP-Total'}, raw['listing'].get('total'))
                site_plan({'sites': {site: raw}}, site, registered=[site])

    def test_past_last_page_400_completes(self):
        for site in self.RUN_SITES:
            with self.subTest(site=site):
                result, docs = run_split(site, total=2)
                self.assertEqual(('complete', 'last_page_empty_reached_start'),
                                 (result['status'], result['stop_reason']), result)
                self.assertEqual(2, len(docs))

    def test_total_header_mismatch_is_reported(self):
        for site in self.RUN_SITES:
            with self.subTest(site=site):
                result, docs = run_split(site, total=3)
                self.assertEqual(('complete_with_mismatch', 'reported_total_mismatch'),
                                 (result['status'], result['stop_reason']), result)
                self.assertEqual(2, len(docs))

    def test_other_400_is_never_an_end(self):
        bodies = [json.dumps({'code': 'rest_invalid_param', 'data': {'status': 400}}), '<html>Bad Request</html>']
        for site in self.RUN_SITES:
            for body in bodies:
                with self.subTest(site=site, body=body[:20]):
                    result, _ = run_split(site, total=2, end=lambda url, b=body: Response(
                        400, {'Content-Type': 'application/json'}, b, url))
                    self.assertEqual('incomplete', result['status'], result)
                    self.assertEqual('listing_failed:unverified_error_status_400', result['stop_reason'])


if __name__ == '__main__':
    unittest.main()
