import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'home-cern-news'


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'home-cern-news'
