import copy
import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks

# crawl() fetches the category taxonomy once before the listing; serve it empty.
_CATEGORIES = 'https://mentalhealthcommission.ca/wp-json/wp/v2/categories?per_page=100'


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'mentalhealthcommission-ca-resources'

    def native(self, fixture=None):
        fixture = copy.deepcopy(fixture or self.fixture)
        fixture['details'][_CATEGORIES] = '[]'
        return super().native(fixture)


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'mentalhealthcommission-ca-resources'
