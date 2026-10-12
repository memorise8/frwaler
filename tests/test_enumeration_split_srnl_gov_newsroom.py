import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'srnl-gov-newsroom'


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'srnl-gov-newsroom'
