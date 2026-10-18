import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'secihti-mx-sala-de-prensa'


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'secihti-mx-sala-de-prensa'
