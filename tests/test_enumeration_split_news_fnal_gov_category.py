"""L3b-20261018: enumeration split for news-fnal-gov-category.

List: GET /wp-json/wp/v2/posts?categories=55&per_page=100&page=N&_embed=1 (X-WP-Total);
past the last page WordPress answers 400 rest_post_invalid_page_number.
Detail: GET /wp-json/wp/v2/posts/{id}?_embed=1. The crawler fetches through the module
function _curl_json (subprocess); the native run routes it to the fixture transport."""
import json
import unittest
from unittest.mock import patch

import tests.collector_split_helpers as helpers
from tests.collector_split_helpers import SplitChecks, SplitRunChecks, load

SITE = 'news-fnal-gov-category'


def _load_with_transport_routed(site):
    crawler, module = load(site)
    module._curl_json = lambda url, **kw: (200, json.loads(crawler._curl_get(url)))
    module._curl_head = lambda url, **kw: ''
    return crawler, module


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = SITE

    def native(self, fixture=None):
        with patch.object(helpers, 'load', _load_with_transport_routed):
            return super().native(fixture)

    def test_post_number_is_wp_post_id_like_original(self):
        expected, _ = self.native()
        self.assertEqual(['990001', '990000'], [p['external_id'] for p in expected])
        self.assertEqual(['990001', '990000'], [p['post_number'] for p in expected])
        self.assertEqual(['2026-02-11', '2026-01-10'], [p['published_date'] for p in expected])

    def test_date_only_reads_wordpress_iso_datetimes(self):
        _, module = load(SITE)
        self.assertEqual('2026-10-15', module._date_only('2026-10-15T10:00:00'))
        self.assertEqual('2026-10-15', module._date_only('2026-10-15T10:00:00+00:00'))
        self.assertIsNone(module._date_only('2026-10-155'))


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = SITE


if __name__ == '__main__':
    unittest.main()
