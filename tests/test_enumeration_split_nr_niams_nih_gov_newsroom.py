"""NR-20261018: niams-nih-gov-newsroom sitemap split (crawl() unchanged), memory-only."""
import unittest

from tests.nr_a_sitemap_helpers import NRSitemapChecks


class RegisteredSitemapSplit(NRSitemapChecks, unittest.TestCase):
    SITE = 'niams-nih-gov-newsroom'


if __name__ == '__main__':
    unittest.main()
