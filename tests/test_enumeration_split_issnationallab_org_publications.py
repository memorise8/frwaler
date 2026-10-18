import unittest

from tests.collector_split2_helpers import Split2Checks
from tests.issnationallab_split_helpers import WpRunChecks


class RegisteredSplit(Split2Checks, unittest.TestCase):
    SITE = 'issnationallab-org-publications'


class RegisteredSplitRun(WpRunChecks, unittest.TestCase):
    SITE = 'issnationallab-org-publications'
