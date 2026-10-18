import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'broadbentinstitute-ca-research'


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'broadbentinstitute-ca-research'
