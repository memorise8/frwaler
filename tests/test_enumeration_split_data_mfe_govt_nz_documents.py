import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'data-mfe-govt-nz-documents'


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'data-mfe-govt-nz-documents'
