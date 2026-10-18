import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'ukdataservice-ac-uk-2026'


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'ukdataservice-ac-uk-2026'
