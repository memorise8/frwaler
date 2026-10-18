import unittest
from tests.collector_split_helpers import SplitChecks, SplitRunChecks


class RegisteredSplit(SplitChecks, unittest.TestCase):
    SITE = 'newsreleases-sandia-gov-archives-by-year'


class RegisteredSplitRun(SplitRunChecks, unittest.TestCase):
    SITE = 'newsreleases-sandia-gov-archives-by-year'
