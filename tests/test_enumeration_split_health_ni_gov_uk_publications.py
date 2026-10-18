import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'health-ni-gov-uk-publications'


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'health-ni-gov-uk-publications'
