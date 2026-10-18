import unittest
from tests.collector_split2_helpers import Split2Checks, Split2RunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'kit-nl-institute'


class RegisteredSplitRun(Split2RunChecks, unittest.TestCase):
    SITE = 'kit-nl-institute'
