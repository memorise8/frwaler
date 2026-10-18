import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks
from crawler.enumeration.http import Response


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'afp-gov-au-search'


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'afp-gov-au-search'
    END_PAGES = dict(SplitRunChecks.END_PAGES, **{
        'afp-gov-au-search': (200, 'text/html', '<html><body><main></main></body></html>')})
