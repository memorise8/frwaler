import unittest
from tests.collector_split_helpers import SplitChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'srnl-gov-newsroom'
