import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'research-imt-nord-europe-fr-news'


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'research-imt-nord-europe-fr-news'
