import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'nasa-gov-2025-news-releases'


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'nasa-gov-2025-news-releases'
