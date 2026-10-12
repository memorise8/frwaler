import unittest
from tests.collector_split_helpers import SplitChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'ukdataservice-ac-uk-2026'
